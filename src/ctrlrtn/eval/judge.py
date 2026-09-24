"""Blinded pairwise judge for shadow-replay eval.

For one recorded input, replay produces a baseline output and a candidate
output. A judge has *position bias* (it may favour whichever response it sees
first). So we:

* present the two outputs as anonymous **A** and **B** — the judge never sees
  which arm, or any model name;
* **swap which arm is A** across replicates so position bias hits both arms
  equally and cancels — which requires an **even** number of replicates (each
  arm in each position the same number of times), enforced below;
* call an injected judge function, then **de-blind** the scores back to
  ``(baseline, candidate)``.

What this does *not* fix: a judge may still re-identify an arm from the output's
**style** (characteristic phrasing/formatting) and carry *differential bias by
arm*. A/B label-blinding can't remove that; it is a residual left to the live
calibration step (judge reliability + a human-labelled calibration set — see
``docs/history/eval-design.md`` §6). Replicates reduce per-pairing judge noise; that
noise is then absorbed into the between-pairing spread the paired NI test
already uses (``ni``), so it need not be propagated separately.

The judge call is injected (``JudgeFn``) so this module is pure and
headless-testable; live wiring passes an Anthropic-backed judge.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass

# prompt -> raw judge response (expected to contain a JSON object with the
# per-response scores). The live implementation calls the judge model.
JudgeFn = Callable[[str], str]

_MIN_SCORE = 0.0
_MAX_SCORE = 10.0  # must match the 0-10 scale stated in _PROMPT

_PROMPT = """\
You are grading two responses, A and B, to the same task. Judge only their
quality for the task; do not speculate about which system produced either.
A response may be one or more tool calls, shown as [tool_use: name] followed
by JSON arguments — judge whether the tool choice is right and its arguments
(including any content inside them, like article text) are correct, complete
and well-crafted for the task.
The task may include source data (shown as [tool result] sections). Check each
response's claims against that data — fabricated or misquoted numbers and
facts are serious defects, however well-written the response.
Score each response from 0.0 (useless) to 10.0 (excellent).

# Task
{task}

# Response A
{a}

# Response B
{b}

Reply with ONLY a JSON object and nothing else:
{{"score_a": <number 0-10>, "score_b": <number 0-10>}}"""


@dataclass
class Pairing:
    """One replayed input together with both arms' outputs."""

    task: str  # the input/task description shown to the judge
    baseline_output: str
    candidate_output: str


@dataclass
class PairScore:
    """Aggregated, de-blinded scores for one pairing."""

    baseline: float
    candidate: float
    replicates: int

    @property
    def diff(self) -> float:
        """``candidate - baseline``; positive means the candidate scored higher."""
        return self.candidate - self.baseline


def build_judge_prompt(task: str, output_a: str, output_b: str) -> str:
    """The blinded prompt — only 'A'/'B', never arm or model identity."""
    return _PROMPT.format(task=task, a=output_a, b=output_b)


def judge_pairing(
    judge_fn: JudgeFn, pairing: Pairing, *, replicates: int = 2
) -> PairScore:
    """Score one pairing with ``replicates`` judge calls, alternating which arm
    is shown as 'A' so position bias cancels. Returns de-blinded means.

    ``replicates`` must be even and >= 2: position bias only cancels when each
    arm appears as 'A' exactly half the time. A single judgment (or any odd
    count) leaves a directional position bias that survives into the NI diff."""
    if replicates < 2 or replicates % 2 != 0:
        raise ValueError("replicates must be an even number >= 2")
    baseline_scores: list[float] = []
    candidate_scores: list[float] = []
    for i in range(replicates):
        candidate_is_a = i % 2 == 1  # alternate positions across replicates
        if candidate_is_a:
            prompt = build_judge_prompt(
                pairing.task, pairing.candidate_output, pairing.baseline_output
            )
        else:
            prompt = build_judge_prompt(
                pairing.task, pairing.baseline_output, pairing.candidate_output
            )
        score_a, score_b = _parse_scores(judge_fn(prompt))
        if candidate_is_a:
            candidate_scores.append(score_a)
            baseline_scores.append(score_b)
        else:
            baseline_scores.append(score_a)
            candidate_scores.append(score_b)
    return PairScore(
        baseline=sum(baseline_scores) / len(baseline_scores),
        candidate=sum(candidate_scores) / len(candidate_scores),
        replicates=replicates,
    )


def _parse_scores(raw: str) -> tuple[float, float]:
    """Pull and validate ``score_a``/``score_b`` from the judge's reply. Every
    failure mode (no JSON, missing key, non-numeric, NaN/inf, out of the 0-10
    scale) is normalised to ``ValueError`` and surfaced — never silently
    corrupting a score that feeds the NI diff."""
    obj = _find_score_object(raw)
    try:
        a = float(obj["score_a"])
        b = float(obj["score_b"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"judge response lacks numeric scores: {exc}") from exc
    for score in (a, b):
        if not math.isfinite(score) or not _MIN_SCORE <= score <= _MAX_SCORE:
            raise ValueError(f"judge score out of [0, 10]: {score}")
    return a, b


def _find_score_object(raw: str) -> dict:
    """First JSON object in ``raw`` that carries both score keys (tolerates
    prose, braces, and extra objects around it)."""
    decoder = json.JSONDecoder()
    for i, char in enumerate(raw):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(raw[i:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "score_a" in obj and "score_b" in obj:
            return obj
    raise ValueError("no JSON object with score_a/score_b in judge response")
