# Getting started: route a call and read the numbers

Assumes the proxy is installed as in the README and the application speaks
the Anthropic or OpenAI API, directly or through a framework.

## 1. Start the proxy

```bash
uv run ctrlrtn serve --log-requests
```

It listens on `http://127.0.0.1:4000` and records to `ctrlrtn.db` in the
current directory. Every command reads the same configuration, so give
`db_path` an absolute value if `serve` and the CLI run from different
directories. Settings come from the YAML file named by `CTRLRTN_CONFIG`
(default `./ctrlrtn.yaml`) or from `CTRLRTN_*` environment variables;
environment wins:

```yaml
# ctrlrtn.yaml
db_path: /var/lib/ctrlrtn/router.db
host: 127.0.0.1
port: 4000
log_requests: true
```

[Configuration](configure.md) lists every key.

The proxy does not fail open. If it is down, the application's calls fail
with a connection error until it is back; nothing bypasses to the provider.

## 2. Point the application at it

The proxy speaks the provider's own API, so the only change is the base URL:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export OPENAI_BASE_URL=http://127.0.0.1:4000/v1
```

- Upstream is chosen by path: `/v1/messages` and `/v1/complete` go to
  Anthropic; `/v1/chat/completions`, `/v1/completions`, `/v1/responses` and
  `/v1/embeddings` go to OpenAI.
- Usage and cost are extracted for the messages and chat-completions
  endpoints; the others are forwarded and recorded with unknown cost.
- A streamed OpenAI call needs `stream_options.include_usage` set to `true`,
  or the final chunk carries no token counts and the cost is unknown.
- The application keeps its own API key. The proxy forwards it on each call,
  redacts it from the recorded trace at capture time, and never stores it.

A local model server that speaks the OpenAI API can be a named provider,
mounted under its own prefix:

```yaml
providers:
  ollama:
    base_url: http://localhost:11434
    api: openai
    free: true
```

Point that client at `http://127.0.0.1:4000/ollama/v1`. `free: true` records
its calls at zero cost.

Send a request and check the recording:

```bash
uv run ctrlrtn calls            # the most recent calls
uv run ctrlrtn show <call-id>   # one call in full; headers redacted,
                                # bodies stored and shown verbatim
```

## 3. Name the work

Three optional request headers turn a list of calls into a picture of the
application:

| Header | Meaning |
| --- | --- |
| `x-ctrlrtn-route` | The role making the call, such as `editor`. Same route, same use-case, keyed `tag:editor`. Name roles, not calls: use-cases are what you evaluate and route. |
| `x-ctrlrtn-task` | One id shared by every call of one job, such as an agent run or a batch item. It prices the whole task and pairs a reported outcome with the calls that produced it. A reused id merges two jobs. |
| `x-ctrlrtn-session` | A spend boundary you define, such as a customer or a nightly batch. Budgets can cap a session's lifetime spend. |

Without a route header, calls are grouped by a fingerprint of the request's
system prompt, tool schemas and response format, keyed `fp:...`, or shown as
`(unkeyed)` when the request has none of those.

Set them by hand for any HTTP client:

```bash
curl http://127.0.0.1:4000/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "x-ctrlrtn-task: run-2026-09-24-001" \
  -H "x-ctrlrtn-route: editor" \
  -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-4-5","max_tokens":64,"messages":[{"role":"user","content":"Hi"}]}'
```

For Python the SDK sets the task and route headers and reports outcomes;
the session header is yours to add. See
[Instrument a workflow](instrument-a-workflow.md). The proxy strips every
`x-ctrlrtn-*` header before forwarding, so providers never see them.

## 4. Report outcomes

When a job finishes, report how it went. Live A/B verdicts rest on these
reports, and the task views show them beside cost:

```bash
curl -X POST http://127.0.0.1:4000/ctrlrtn/outcome \
  -H "content-type: application/json" \
  -d '{"task_id": "run-2026-09-24-001", "success": true, "score": 0.9}'
```

`success` is the application's own verdict; `score` is optional, on any
scale you choose. The endpoint is part of the unauthenticated control plane,
which is why the proxy binds to localhost by default.

## 5. Read the numbers

```bash
uv run ctrlrtn usecases          # use-cases ranked by spend
uv run ctrlrtn tasks             # cost per task, with reported outcomes
uv run ctrlrtn sessions          # spend per session
uv run ctrlrtn spend             # total and today's spend
uv run ctrlrtn propagation       # are tasks linking their sub-agent calls?
uv run ctrlrtn recommendations   # where a cheaper model is worth a test
```

`recommendations` names a use-case when the bundled price table knows a
cheaper model of the same family and the projected saving is material: a
pointer to what to test, not a verdict.

`usecases` is the table to start from:

```text
use-case                   calls     in_tok    out_tok   avg_ms       cost
--------------------------------------------------------------------------
tag:financial_journalist     452    1204331     318207     8412   $20.0031
tag:technical_analyst        231     411020      92118     3105    $1.6712
tag:editor                   105     388905      41332     5220    $1.5688
```

- Costs come from a bundled price table keyed by model family. A model the
  table does not know is recorded with an unknown cost, never zero: `calls`
  shows `-`, `spend` counts unknown-priced calls, and per-use-case and
  per-task totals sum the known costs only. Point `CTRLRTN_PRICES` at your
  own TOML to add or override prices.
- `propagation` is the gate for everything task-level: it says whether most
  successful calls carry a task id and whether one id links several roles.
  Failed calls do not count. Until it reports `PROPAGATING`, task costs and
  live A/B verdicts are not trustworthy.

The console shows the same data live, with traffic graphs and a call feed:

```bash
uv sync --extra tui
uv run ctrlrtn console
```

## 6. Keep the recording bounded

Recorded bodies grow. `prune` clears request and response payloads older
than a cutoff and keeps every derived number; it is a dry run without
`--apply`:

```bash
uv run ctrlrtn prune --older-than-days 30
uv run ctrlrtn prune --older-than-days 30 --apply
```

Payloads a queued or running evaluation job still needs are protected until
it finishes. `retention_days` in the configuration makes this automatic. To
reclaim disk afterwards, stop the gateway and run `prune --apply --compact`.

## Upgrade

With a checkout, `git pull && uv sync --extra tui` and restart `serve`. An
existing database is upgraded in place when next opened.

## Next

Once `usecases` shows a role worth testing on a cheaper model,
[run an experiment](run-an-experiment.md).
