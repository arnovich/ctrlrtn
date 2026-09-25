# Configuration

Scalar settings come from a YAML file or an environment variable. Named
providers and budgets are YAML-only blocks. Later layers override earlier
ones:

```
defaults  <  YAML config file  <  environment variables  <  CLI flags
```

The YAML file is optional (`CTRLRTN_CONFIG`, else `./ctrlrtn.yaml`). `serve`
and the CLI read the same config. Relative paths resolve against each
process's working directory, so give `db_path` an absolute value when they
run from different directories, or they silently open different databases.

```yaml
# ctrlrtn.yaml
db_path: /var/lib/ctrlrtn/router.db
host: 127.0.0.1   # 0.0.0.0 only inside a container or behind a firewall
port: 4000
providers:
  ollama:
    base_url: http://localhost:11434
    api: openai
    free: true
timeout: 600
inject_cache: false
retention_days: 30  # optional; omit for manual-only retention
```

## Settings

| YAML key | Environment variable | Effect |
| --- | --- | --- |
| `db_path` | `CTRLRTN_DB` | SQLite path for recorded traffic (default `ctrlrtn.db`). |
| `host` / `port` | `CTRLRTN_HOST` / `CTRLRTN_PORT` | Bind address and port for `serve` (default `127.0.0.1:4000`). |
| `log_requests` | `CTRLRTN_LOG_REQUESTS` | Print one line per recorded call. |
| `log_level` | `CTRLRTN_LOG_LEVEL` | Verbosity for uvicorn and `ctrlrtn`: `debug`, `info`, `warning`, `error`, `critical` (default `info`). `debug` shows per-call arm decisions and snapshot refreshes. |
| `upstream` | `CTRLRTN_UPSTREAM` | Force one upstream base URL for every path (else route by path). Cannot be combined with `providers`. |
| `anthropic_upstream` / `openai_upstream` | `CTRLRTN_ANTHROPIC_UPSTREAM` / `CTRLRTN_OPENAI_UPSTREAM` | Upstream base URL for the unprefixed Anthropic and OpenAI routes. |
| `providers` | (YAML only) | Named upstreams mounted at distinct client-facing prefixes; see below. |
| `timeout` | `CTRLRTN_TIMEOUT` | Upstream request timeout in seconds. |
| `inject_cache` | `CTRLRTN_INJECT_CACHE` | Opt-in: mark the static prompt prefix with Anthropic `cache_control`. Never applied to an OpenAI-compatible upstream. |
| `kill_switch` | `CTRLRTN_KILL_SWITCH` | Block all provider traffic locally until the proxy restarts with it disabled. |
| `retention_days` | `CTRLRTN_RETENTION_DAYS` | Opt-in positive age in days. Eligible trace payloads are pruned transactionally before `serve` accepts traffic. |
| `budgets` | (YAML only) | Global, per-use-case, and per-session spend ceilings; see below. |
| `control_hosts` | `CTRLRTN_CONTROL_HOSTS` | Extra Host names the `/ctrlrtn/` control plane trusts (comma-separated, or a YAML list). `localhost` and IP literals are always trusted; other names are rejected as a DNS-rebinding guard. |
| (none) | `CTRLRTN_CONFIG` | Path to the YAML config file (else `./ctrlrtn.yaml`). |
| (none) | `CTRLRTN_PRICES` | Path to a TOML file overlaying the model price table; see below. |

An unknown key or a wrong type fails startup with an error naming the key
and its source.

## Attribution headers

Clients attach up to three independent identities to a provider request:

| Header | Meaning |
| --- | --- |
| `x-ctrlrtn-route` | Names the use-case (`tag:<name>`). Without it the proxy keys the call by a fingerprint of the request structure: system prompt, tool schemas, and response format (`fp:...`). |
| `x-ctrlrtn-task` | Groups the calls of one end-to-end task, for evaluation and app-reported outcomes. |
| `x-ctrlrtn-session` | Groups calls under an operator-defined cost boundary. |

Instrumented workflows add the step identity defined in
[workflow-identity.md](workflow-identity.md);
[instrument-a-workflow.md](instrument-a-workflow.md) shows the SDK. The
proxy removes every `x-ctrlrtn-*` header before a request, baseline or
shadow, reaches a provider. The headers are attribution hints among
cooperating applications, not authentication: a caller can forge or rotate
a session ID, so a session ceiling prevents accidental overspend and is not
a tenant quota.

## Serving precedence

The first rule that applies decides the served model. Rules are checked in
this order, from an in-memory snapshot that refreshes about every 10
seconds.

1. **Running experiment.** Applies when the use-case key has a running
   experiment whose scope matches the request's validated workflow
   identity. With `x-ctrlrtn-task` the task binds to the first experiment it
   hits and is assigned an arm; without a task header, or when the task is
   already bound to another experiment, the call passes through unchanged
   and no route is consulted. A route on the same use-case stays dormant
   until the experiment stops.
2. **Step route.** Applies when no experiment took the call and the request
   carries a complete, valid workflow identity that exactly matches a
   `workflow_routes` entry for `(workflow, workflow_version, step)`.
3. **Workflow route.** Applies when the identity exactly matches a
   `workflow_routes` entry for `(workflow, workflow_version)` with no step.
4. **Use-case route.** Applies when a `route` exists for the use-case key.
5. **Approved budget fallback.** Applies only when none of the rules above
   fired, the use-case's `fallback_at_usd` threshold is reached, an approved
   fallback exists for the use-case, and the request's model is the
   evaluated baseline. A running experiment or a route is never overridden.
6. **Pass-through.** The body is forwarded unchanged.

A partial, malformed, or unknown-version workflow identity matches no
workflow rule and falls back to the use-case rules. A route or fallback
leaves the body alone when the requested model already equals its target,
and clamps `max_tokens` down to the target model's `max_output`. Every
routed trace records the rule scope (`workflow_step`, `workflow`, or
`use_case`), the rule key, and the activated Git revision. Cache-control
injection composes with the decision and is gated by the upstream API
identity.

## Spend ceilings (`budgets`)

Budgets are optional. A hard ceiling always blocks; a use-case can also have
an earlier, evidence-gated fallback threshold:

```yaml
budgets:
  reserve_in_flight: true
  global:
    daily_usd: 25
  session:
    limit_usd: 2
  use_cases:
    tag:editor:
      daily_usd: 5
      fallback_at_usd: 4
```

| Key | Meaning |
| --- | --- |
| `global.daily_usd` | Ceiling on known spend across all traffic. Resets at midnight UTC. |
| `use_cases.<key>.daily_usd` | Ceiling for one use-case key. Resets at midnight UTC. |
| `use_cases.<key>.fallback_at_usd` | Optional. Valid only below the same use-case's `daily_usd`. At this threshold the proxy may switch to one evidence-approved candidate; see below. |
| `session.limit_usd` | Lifetime ceiling for each non-empty `x-ctrlrtn-session` value. Use a new unique value for each logical session. |
| `reserve_in_flight` | Boolean, default `false`. Count admitted but unsettled requests against every matching ceiling. Requires at least one ceiling. |

Amounts must be finite and non-negative. `global` accepts only `daily_usd`,
`session` only `limit_usd`; any other key fails startup.

A rejection happens locally, before any provider is contacted, and names
the scope, recorded spend, and limit where they apply:

| Error `type` | HTTP | When |
| --- | --- | --- |
| `ctrlrtn_budget_exceeded` | 429 | A global, use-case, or session ceiling is reached. |
| `ctrlrtn_session_required` | 400 | `session.limit_usd` is set and the request has no `x-ctrlrtn-session` header. |
| `ctrlrtn_budget_fallback_unavailable` | 429 | A `fallback_at_usd` threshold is reached and no approved fallback applies. |
| `ctrlrtn_unreservable_request` | 400 | `reserve_in_flight` is on and the request has no `max_tokens` or `max_completion_tokens` and its model has no catalog `max_output`. |
| `ctrlrtn_unpriced_model` | 503 | A budget applies and the paid model has no catalog price. |
| `ctrlrtn_unknown_session_cost` | 503 | A session ceiling applies and the session already contains a call of unknown cost, including after a restart. |

Unknown never means zero. `free: true` on a provider is the only zero-cost
declaration.

### Evidence-approved fallbacks

`fallback_at_usd` moves a use-case to a cheaper candidate before the hard
ceiling closes it. The switch needs a durable approval, taken from the JSON
artifact of a non-inferiority evaluation:

```bash
ctrlrtn replay-eval tag:editor claude-haiku-4-5 --yes --json editor-replay.json
ctrlrtn fallback approve editor-replay.json
ctrlrtn fallback list
ctrlrtn fallback clear tag:editor
```

- `fallback approve` accepts only an exact `NON_INFERIOR` verdict and stores
  the artifact's use-case, baseline, candidate, and creation time.
- The request must still use the evaluated baseline. Stale evidence for a
  changed baseline does not apply, and model names are never used to guess.
- `--provider NAME` binds the candidate to a named provider; the switch is
  permitted only when its API identity matches the current upstream.
- A running experiment or route comes first
  ([Serving precedence](#serving-precedence)).
- Step-scoped replay artifacts are rejected.
- Without an applicable approval the threshold fails closed with
  `ctrlrtn_budget_fallback_unavailable`; the hard `daily_usd` still returns
  `ctrlrtn_budget_exceeded`.

### What a budget guarantees

A budget blocks the next matching request once persisted known spend
reaches the ceiling. Accounting is eventually consistent by default: cost is
known only after a response is recorded, so calls already in flight can
overshoot. `reserve_in_flight: true` closes that same-process race by
holding a conservative estimate from admission until the enriched trace is
persisted (request bytes plus a protocol allowance at the model's highest
input or cache rate, plus the output bound at the output rate; a rejection's
`spent_usd` then includes estimates still in flight). Reservations are not
a provider billing ledger: tokenization, provider-added tokens, partial
responses, and billing after a connection failure can differ from the
estimate, multiple proxy processes do not share reservations, and a failed
persistence keeps the reservation held until restart. A budget is a local
safeguard against runaway spend, not an exact cap on the invoice.

### Inspecting spend

```bash
ctrlrtn spend       # recorded total and today's spend
ctrlrtn sessions    # calls, use-cases, errors, tokens and known spend per session
ctrlrtn budget      # kill switch, ceilings, remaining spend, blocked calls
```

`sessions` shows known remaining spend when a session ceiling is configured;
a session with any unknown-cost call shows `N/A`, and missing values group
as `(unsessioned)`. `budget` also lists each configured fallback threshold
and today's evidence-approved fallback calls. Both report persisted
accounting only; the live proxy's process-local reservations are not
included. The console shows the same budget status above its graphs
([console.md](console.md)). Recording is non-blocking: a saturated recorder
queue drops telemetry rather than delay a client response.

`kill_switch: true` is the independent emergency stop. It is fixed at
startup, blocks all provider traffic locally, and changing it needs a
restart.

## Named providers

Use a named provider when two upstreams expose the same API paths. Each
name is mounted at `/<name>` by default, and the proxy strips that prefix
before forwarding:

```yaml
providers:
  ollama:
    base_url: http://localhost:11434
    api: openai
    free: true
  local_claude:
    base_url: http://localhost:8080
    prefix: /claude-local
    api: anthropic
```

| Field | Required | Meaning |
| --- | --- | --- |
| `base_url` | yes | Upstream base URL. |
| `prefix` | no | Client-facing mount, default `/<name>`. Must start with `/`, be a non-root path without a trailing slash, be unique, and stay outside the reserved `/ctrlrtn` namespace. |
| `api` | no | Upstream wire protocol: `openai` or `anthropic`. Any other value fails startup. Omitting it keeps opaque pass-through but makes the provider ineligible for cross-provider routing. |
| `free` | no | `true` declares zero marginal token cost. Default `false`. |
| `credential` | no | Provider-owned credential read from the environment; see below. |

Point an OpenAI-compatible client at `http://127.0.0.1:4000/ollama/v1`; its
request to `/ollama/v1/chat/completions` reaches Ollama as
`/v1/chat/completions`. The unprefixed `/v1/...` routes still reach the
built-in OpenAI and Anthropic providers.

`api` scopes provider-specific behaviour: Anthropic cache-control injection
is never applied to an OpenAI-compatible upstream, and an Ollama model is
recorded at `$0` even if its name also exists on a paid provider. The
provider name and `free` policy are recorded with every trace, so a later
configuration change cannot rewrite historical pricing. Token counts and
latency are still recorded for free calls.

`providers` cannot be combined with `upstream`, which forces every request
to one destination. `openai_upstream` and `anthropic_upstream` replace the
default destination of the unprefixed routes rather than adding one.

### Provider-owned credentials

An authenticated named provider can read its own secret from the process
environment. It is never stored in YAML, SQLite, traces, or CLI output:

```yaml
providers:
  ollama_cloud:
    base_url: https://ollama.com
    api: openai
    credential:
      env: OLLAMA_API_KEY
      header: authorization  # default
      prefix: "Bearer "       # default
```

The variable must be set when the proxy starts; under the systemd unit it
goes in `/etc/ctrlrtn/env`. On a request to this provider, including a
cross-provider candidate, client credential headers and credential-named
query parameters are removed and the provider-owned credential is injected.
A provider without `credential` keeps ordinary client-credential
pass-through; local Ollama needs none.

A provider-owned credential means the proxy spends the operator's key for
any client that can reach the port; nothing authenticates the client. Such
requests get the control plane's Host check, so a rebound browser page is
refused with `403 ctrlrtn_untrusted_host`, but a host on the same network is
not. Give the provider a `budgets` ceiling and read the security model in
[deploy.md](deploy.md) before enabling one.

### Cross-provider experiments and routes

Experiments and persistent routes may select a named provider:

```bash
ctrlrtn experiment start tag:editor qwen2.5:0.5b --provider ollama --split 50
ctrlrtn route set tag:editor qwen2.5:0.5b --provider ollama
```

On the baseline arm the original provider and body pass through. On the
candidate arm the proxy swaps the model and sends the same provider-facing
path to the named provider. `route adopt` preserves the tested provider.

Baseline and candidate must declare the same `api`. A missing provider or
API mismatch fails locally with `ctrlrtn_provider_mismatch`, recorded on the
candidate arm; no request reaches an incompatible upstream. There is no
translation between the Anthropic and OpenAI APIs. When the provider
changes, credential headers and credential-named query parameters are
removed before forwarding, so a baseline key never reaches the candidate;
configure `credential` on an authenticated candidate.

### Ollama

The `ollama` provider above is the whole setup; point the application's
OpenAI-compatible client at `http://127.0.0.1:4000/ollama/v1`.
[getting-started.md](getting-started.md) walks through it, including the
`stream_options.include_usage` setting a streamed request needs.

## Model prices

The bundled table, `src/ctrlrtn/telemetry/prices.toml`, holds USD per
million tokens (`input`, `output`, `cache_read`, `cache_write`) and the
`max_output` cap for 15 models: 12 Anthropic Claude models and 3 OpenAI
models. Prices are approximate list prices; the file header states that the
Anthropic rows were verified against published pricing in 2026-07, and each
row's comment names any later check. A `ladder` table lists the cheaper
same-family models `recommendations` proposes. Prices are read once at
startup. Point `CTRLRTN_PRICES` at your own TOML to overlay the bundled
table:

```toml
[prices."gpt-4o-mini"]
input = 0.15
output = 0.60
```

An override merges per field. Models match by the longest family prefix
ending on a delimiter, so a version-suffixed id resolves to its family; add
a version-specific row only when its price differs. Quote a model id that
contains a dot. A route clamps a larger recorded `max_tokens` down to the
target model's `max_output`, so a switch to a smaller-cap model cannot fail
the whole use-case.

Token counts are not comparable across tokenizers. The header notes that
`claude-fable-5`, `claude-sonnet-5`, and `claude-opus-4-7` and later produce
about 30% more tokens for the same text than the `claude-sonnet-4`
generation, so repricing recorded counts at such a model, as
`campaign-report` does, under-counts its tokens by that margin.

## Git-backed routing state (`routing.yaml`)

Model control state can live in a Git repository as `routing.yaml`; start
from `examples/routing.yaml`:

```yaml
version: 1
routes:
  tag:editor:
    model: qwen2.5:7b
    provider: ollama
    previous_model: claude-sonnet-4-5
    note: replay eval non-inferior
experiments:
  tag:journalist:
    id: exp:journalist-local-v1
    candidate_model: qwen2.5:7b
    provider: ollama
    split_pct: 25
    max_calls_per_task: 60
workflows:
  article-pipeline:
    git:abc123:
      steps:
        research:
          allows: {fan_out: true, retry: true}
        draft:
          predecessors: [research]
          allows: {retry: true}
workflow_routes:
  article-pipeline:
    git:abc123:
      model: qwen2.5:7b
      provider: ollama
      steps:
        draft:
          model: local-writer
          provider: ollama
```

| Section | Keys |
| --- | --- |
| `version` | Must be `1`. |
| `routes.<use-case>` | `model` (required), `provider`, `previous_model`, `note`. |
| `experiments.<use-case>` | `id` (required, stable), `candidate_model` (required), `provider`, `split_pct` (1 to 99, default 50), `max_calls_per_task` (default 60), and the optional exact scope `workflow`, `workflow_version`, `step`. |
| `workflows.<name>.<version>.steps.<step>` | `predecessors` (declared step names in the same version, never the step itself), `allows: {fan_out, retry}` (booleans), `condition` (an opaque display label). |
| `workflow_routes.<name>.<version>` | `model`, `provider`, `note`, and `steps.<step>` with its own `model`, `provider`, `note`. |

Unknown keys at any level fail validation. An experiment `id` is required
and stable: changing any experiment field needs a new id, which keeps the
stopped experiment as evidence and re-buckets tasks deliberately. Named
providers must exist in the proxy's `providers` block.

`workflows` declares a descriptive, non-executable graph: stable step
names, allowed predecessors, and `fan_out`/`retry` capabilities. Conditions
are opaque labels, never evaluated code. A scoped experiment or a
`workflow_routes` entry must reference a declared workflow, exact version,
and declared step; partial, malformed, unknown-version, and inferred
identities cannot authorize these rules. The order the rules apply in is
[Serving precedence](#serving-precedence).

```bash
ctrlrtn routing-config validate routing.yaml
ctrlrtn routing-config diff routing.yaml
ctrlrtn routing-config activate routing.yaml --repo .
ctrlrtn routing-config status
```

`PATH` defaults to `routing.yaml` and `--repo` to the current directory.

| Command | Reads | Writes | Requires |
| --- | --- | --- | --- |
| `validate [PATH]` | The YAML file and the `providers` block of the proxy settings | Nothing | A schema-valid document within domain limits whose named providers exist |
| `diff [PATH]` | The YAML file and the live SQLite control state | Nothing | A schema-valid document |
| `activate [PATH] --repo REPO` | The YAML file, its Git repository, and the live SQLite control state | Every route, workflow route, workflow definition, and running experiment, plus the revision record, in one transaction | The file inside the repository, tracked, committed, and clean; named providers exist; a new stable `id` for any changed experiment |
| `status` | The live SQLite control state | Nothing | Nothing |

Activation records repository HEAD, relative path, file SHA-256, and
activation time. Any failure rolls the whole transaction back, and
unchanged objects keep their original activation timestamps. Every routed
trace records the winning rule and the activated Git revision; `ctrlrtn
show` and the console's call detail expose that explanation.

The file is authoritative: omission clears a route or stops a running
experiment. Stopped experiments, traces, outcomes, durable jobs, and
measurements are never deleted. Manual `route` or `experiment` commands can
diverge from the recorded revision; `routing-config diff` exposes that
drift and a later activation restores the declaration. Keep credential
values, raw traces, outputs, databases, and exported evidence outside this
repository. Activation performs no network access and never changes the
working tree; fetch, pull, push, and conflict resolution stay with the
operator. The console activates the same file with `g` after a diff and a
confirmation ([console.md](console.md)).
