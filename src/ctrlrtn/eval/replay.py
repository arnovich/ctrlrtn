"""Shadow-replay runner — the primary eval signal (docs/evaluation.md).

Re-runs a use-case's recorded inputs through baseline and candidate, judges each
(baseline, candidate) pair blind (``judge``), and runs a paired non-inferiority
test (``ni``). The two network-touching steps — *replaying* a recorded call and
*calling the judge* — are injected, so the orchestration here is pure and
headless-testable; the CLI passes Anthropic-backed implementations and runs it
from a real terminal with keys.

Only use for **replayable** use-cases: re-sending a recorded request must have no
external side effects (true for a pure text transform like the editor, not for an
agent whose tool calls act on the world).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from ctrlrtn.eval.judge import JudgeFn, Pairing, judge_pairing
from ctrlrtn.eval.ni import NIResult, paired_ni
from ctrlrtn.telemetry.usage import decode_body

# (recorded request bytes, model) -> the model's output text. The live impl
# swaps the model into the request, sends it upstream, and extracts the text.
ReplayFn = Callable[[bytes, str], str]
ProgressFn = Callable[[int, int, str], None]

# Refuse to conclude if more than this fraction of samples failed or produced no
# usable output from either arm — the batch is too degraded to trust.
_MAX_UNUSABLE_FRACTION = 0.2

# How many distinct failure reasons to keep for the report — enough to tell a
# bad key (every sample 401) from a real candidate regression, without hoarding.
_MAX_FAILURE_REASONS = 5


@dataclass
class ReplaySample:
    """One recorded call to replay."""

    request_body: bytes
    cluster: object = (
        None  # correlation group (edition id); None -> independent
    )
    task_hint: str = ""  # input description for the judge; derived if empty


@dataclass
class ReplayReport:
    """What one replay batch produced: the NI verdict plus how many samples
    yielded a usable pairing, failed, or were blank, so a reader can judge
    how degraded the batch was before trusting the verdict."""

    result: NIResult
    n_pairings: int  # pairings that produced a usable diff
    n_failed: int  # samples a replay/judge call raised on (skipped)
    n_blank: int  # samples where both arms produced no text (skipped)
    mean_diff: float
    baseline_model: str
    candidate_model: str
    failures: tuple[str, ...] = ()  # distinct reasons samples failed, if any


def run_replay(
    samples: list[ReplaySample],
    *,
    baseline_model: str,
    candidate_model: str,
    replay_fn: ReplayFn,
    judge_fn: JudgeFn,
    margin: float,
    replicates: int = 2,
    confidence: float = 0.95,
    progress_fn: ProgressFn | None = None,
) -> ReplayReport:
    """Replay each sample on both arms, judge the pairs, run the paired NI test.

    Per-sample failures are isolated and counted, not fatal; a pairing where
    *both* arms produced no text is dropped (no signal); and the whole batch
    refuses to conclude if too much of it was unusable."""
    if replicates < 2 or replicates % 2 != 0:  # fail before spending any calls
        raise ValueError("replicates must be an even number >= 2")

    diffs: list[float] = []
    labels: list = []
    failures: list[str] = []
    n_failed = 0
    n_blank = 0
    for index, sample in enumerate(samples, start=1):
        diff = None
        try:
            baseline_output = replay_fn(sample.request_body, baseline_model)
            candidate_output = replay_fn(sample.request_body, candidate_model)
            if not _has_text(baseline_output) and not _has_text(
                candidate_output
            ):
                n_blank += 1  # neither arm said anything usable -> no signal
            else:
                task = sample.task_hint or extract_input_summary(
                    sample.request_body
                )
                diff = judge_pairing(
                    judge_fn,
                    Pairing(
                        task=task,
                        baseline_output=baseline_output,
                        candidate_output=candidate_output,
                    ),
                    replicates=replicates,
                ).diff
        except Exception as exc:
            # one bad sample must not nuke an expensive batch
            n_failed += 1
            reason = _describe_failure(exc)
            if reason not in failures and len(failures) < _MAX_FAILURE_REASONS:
                failures.append(reason)
        else:
            if diff is None:
                if progress_fn is not None:
                    progress_fn(index, len(samples), "blank input skipped")
                continue
            diffs.append(diff)
            # None -> a fresh sentinel so the sample stays its own cluster (a
            # shared None key would otherwise fuse independent samples).
            labels.append(
                sample.cluster if sample.cluster is not None else object()
            )
        if progress_fn is not None:
            progress_fn(index, len(samples), "replayed and judged input")

    total = len(samples)
    unusable = n_failed + n_blank
    too_degraded = total > 0 and unusable / total > _MAX_UNUSABLE_FRACTION
    mean_diff = sum(diffs) / len(diffs) if diffs else 0.0
    if not diffs or too_degraded:
        result = NIResult(
            non_inferior=False,
            mean_diff=mean_diff,
            lower_bound=float("-inf"),
            n=len(diffs),
            n_units=0,
            margin=margin,
            confidence=confidence,
            underpowered=True,
        )
    else:
        result = paired_ni(
            diffs, margin=margin, clusters=labels, confidence=confidence
        )
    return ReplayReport(
        result=result,
        n_pairings=len(diffs),
        n_failed=n_failed,
        n_blank=n_blank,
        mean_diff=result.mean_diff,
        baseline_model=baseline_model,
        candidate_model=candidate_model,
        failures=tuple(failures),
    )


def _has_text(output: str) -> bool:
    return bool(output and output.strip())


def _describe_failure(exc: Exception) -> str:
    """A short, operator-readable reason a sample failed. Prefers an HTTP error
    body (which names the real cause, e.g. an unsupported ``thinking`` block or a
    missing beta header) over the bare exception type — without importing the
    HTTP client, so the runner stays provider-agnostic."""
    response = getattr(exc, "response", None)
    body = getattr(response, "text", "") if response is not None else ""
    detail = (body or str(exc)).strip().replace("\n", " ")
    name = type(exc).__name__
    return f"{name}: {detail[:200]}" if detail else name


def swap_model(request_body: bytes, model: str) -> bytes:
    """Return ``request_body`` with its ``model`` set to ``model`` and streaming
    disabled (replay reads a single JSON body, not an SSE stream).

    Caller's responsibility for cross-model replay: clamp ``max_tokens`` to the
    candidate's limit and drop an unsupported ``thinking`` block — both can 400
    on a smaller model. Same-provider swaps only (bodies aren't translated)."""
    payload = json.loads(request_body)
    if not isinstance(payload, dict):
        raise ValueError("request body is not a JSON object")
    payload["model"] = model
    payload.pop("stream", None)
    return json.dumps(payload).encode("utf-8")


def extract_output_text(
    response_body: bytes, content_encoding: str | None = None
) -> str:
    """The assistant's output from an Anthropic or OpenAI response body, as
    judgeable text: text blocks verbatim, each tool call rendered as
    ``[tool_use: name]`` plus its pretty-printed arguments. Agent traffic is
    mostly all-tool_use turns — there the call's arguments ARE the output (an
    article lives inside write_article's input), so they must be compared,
    not skipped. Returns "" only when there is neither text nor a tool call —
    the caller treats a both-arms-blank pairing as no signal, not a tie."""
    payload = json.loads(decode_body(response_body, content_encoding))
    content = payload.get("content")
    if isinstance(content, list):  # Anthropic Messages
        return _render_blocks(content)
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            parts: list[str] = []
            inner = message.get("content")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):  # OpenAI parts array
                parts.append(_render_blocks(inner))
            for call in message.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict):
                    parts.append(
                        _render_tool_call(
                            fn.get("name"), _parse_args(fn.get("arguments"))
                        )
                    )
            return "\n".join(p for p in parts if p)
    return ""


def _parse_args(raw) -> dict | list | str | None:
    """OpenAI tool arguments arrive as a JSON string; decode when possible so
    both providers render identically (and key order can be normalized)."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _render_tool_call(name, args) -> str:
    # sort_keys: identical arguments render byte-identically on both arms, so
    # the judge compares content, not dict ordering.
    rendered = (
        json.dumps(args, indent=2, sort_keys=True)
        if isinstance(args, (dict, list))
        else str(args or "")
    )
    return f"[tool_use: {name or '?'}]\n{rendered}"


def _render_blocks(blocks: list) -> str:
    """Consecutive text blocks concatenate verbatim (providers split text
    mid-word across blocks); tool_use blocks render as labelled JSON on their
    own lines. Thinking/other block types are internal and excluded."""
    parts: list[str] = []
    text_buf: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and b.get("text"):
            text_buf.append(b["text"])
        elif b.get("type") == "tool_use":
            if text_buf:
                parts.append("".join(text_buf))
                text_buf = []
            parts.append(_render_tool_call(b.get("name"), b.get("input")))
    if text_buf:
        parts.append("".join(text_buf))
    return "\n".join(parts)


def _join_text_blocks(blocks: list) -> str:
    """Text blocks only — used for INPUT summaries, where the task description
    is wanted, not tool mechanics."""
    return "".join(
        (b.get("text") or "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    )


def _tool_result_text(block: dict) -> str:
    """The text inside a tool_result block (string or nested content list)."""
    inner = block.get("content")
    if isinstance(inner, str):
        return inner
    if isinstance(inner, list):
        return "".join(
            (b.get("text") or "")
            for b in inner
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def extract_input_summary(
    request_body: bytes, *, max_chars: int = 12000
) -> str:
    """The task shown to the judge: the user messages' text AND their
    tool_result payloads (the static system prompt is the role, not the task).

    Tool results are load-bearing for agent traffic — they carry the source
    data an output is grounded in (a market feed the article quotes). A judge
    that never sees them can compare style but cannot catch a fabricated
    number, which is exactly where a cheaper model fails quietly. Truncates by
    keeping both ends, since a distinguishing instruction often sits at the
    tail."""
    payload = json.loads(request_body)
    parts: list[str] = []
    for message in payload.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"])
                elif block.get("type") == "tool_result":
                    text = _tool_result_text(block)
                    if text:
                        parts.append(f"[tool result]\n{text}")
    text = "\n".join(p for p in parts if p)
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n…\n" + text[-half:]
