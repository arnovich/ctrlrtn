# Configuration

Scalar settings can be given in a **YAML file** or as an **environment
variable**, so the same image runs locally, on a VPS, or hosted. Named
providers use the structured, YAML-only `providers` block. Layered, most
specific wins:

```
defaults  <  YAML config file  <  environment variables  <  CLI flags
```

The YAML file is optional (`CTRLRTN_CONFIG`, else `./ctrlrtn.yaml`).
`serve` and the CLI read the same config — but relative paths resolve against
each process's cwd, so if you run them from different directories use an
**absolute** `db_path` or they'll silently open different databases.

```yaml
# ctrlrtn.yaml
db_path: /var/lib/ctrlrtn/router.db
host: 0.0.0.0
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

| YAML key | Environment variable | Effect |
| --- | --- | --- |
| `db_path` | `CTRLRTN_DB` | SQLite path for recorded traffic (default `ctrlrtn.db`). |
| `host` / `port` | `CTRLRTN_HOST` / `CTRLRTN_PORT` | Bind address/port for `serve` (default `127.0.0.1:4000`). |
| `log_requests` | `CTRLRTN_LOG_REQUESTS` | Print one line per recorded call. |
| `log_level` | `CTRLRTN_LOG_LEVEL` | Verbosity for uvicorn and `ctrlrtn` (`debug`/`info`/`warning`/`error`, default `info`). `serve --log-level debug` shows per-call arm decisions and snapshot refreshes. |
| `upstream` | `CTRLRTN_UPSTREAM` | Force one upstream base URL for every path (else route by path). |
| `anthropic_upstream` / `openai_upstream` | `CTRLRTN_ANTHROPIC_UPSTREAM` / `CTRLRTN_OPENAI_UPSTREAM` | Per-provider upstream base URL. |
| `providers` | — | Named upstreams mounted at distinct client-facing prefixes; see below. |
| `timeout` | `CTRLRTN_TIMEOUT` | Upstream request timeout, seconds. |
| `inject_cache` | `CTRLRTN_INJECT_CACHE` | Opt-in: mark the static prompt prefix with Anthropic `cache_control`. |
| `kill_switch` | `CTRLRTN_KILL_SWITCH` | Block all provider traffic locally until the router restarts with it disabled. |
| `retention_days` | `CTRLRTN_RETENTION_DAYS` | Opt-in positive age in days; transactionally prune eligible trace payloads before startup begins serving. |
| `budgets` | — | YAML-only global/use-case daily and per-session spend ceilings; see below. |
| `control_hosts` | `CTRLRTN_CONTROL_HOSTS` | Extra Host names the `/ctrlrtn/` control plane trusts (comma-separated / YAML list). `localhost` and IP literals are always trusted; other names are rejected as a DNS-rebinding guard. |
| — | `CTRLRTN_CONFIG` | Path to the YAML config file (else `./ctrlrtn.yaml`). |
| — | `CTRLRTN_PRICES` | Path to a TOML file overlaying the model prices. |

## Attribution headers

Clients can attach three independent identities to provider requests:

- `x-ctrlrtn-route` gives an agent role a stable use-case name.
- `x-ctrlrtn-task` groups calls for evaluation and app-reported outcomes.
- `x-ctrlrtn-session` groups calls under an operator-defined cost boundary.

Instrumented agentic workflows can additionally attach the complete workflow
identity defined in [`workflow-identity.md`](workflow-identity.md). Prefer the
SDK so partial identities cannot be created manually:

```python
from ctrlrtn import sdk

with sdk.edition(
    workflow="article-pipeline",
    workflow_version="git:abc123",
    report_to="http://localhost:4000",
) as run:
    with run.step("research") as research:
        collect_sources()
        research.report(success=True)
    with run.step("draft", dependencies=[research.step_run_id]):
        write_draft()
```

Nested steps automatically record a parent. `sdk.bind()` carries the complete
context into a thread; `sdk.export_carrier()` and `sdk.import_carrier()` are the
validated subprocess/service boundary. The SDK posts idempotent start/terminal
events to `/ctrlrtn/workflow-events`. Failures are observable but never allowed to
break the application. Calls without workflow metadata retain existing
task/use-case behavior.

Workflow fields are persisted in dedicated trace columns and malformed partial
sets are retained as diagnostics rather than authoritative identity. All
`x-ctrlrtn-*` headers are removed before either the baseline or shadow request is
sent to a model provider.

Rebuild and inspect evidence-only links offline:

```bash
ctrlrtn workflow infer         # exact tool-call/result IDs + accuracy
ctrlrtn workflow inferred      # persisted projections and confirmation
ctrlrtn workflow discover      # recurring legacy task structures
ctrlrtn workflow discover --background # durable frozen-input discovery
ctrlrtn workflow discover --background --since EPOCH --until EPOCH
ctrlrtn workflow discover --background --provider anthropic
ctrlrtn workflow discover --background --model MODEL
ctrlrtn workflow discover --background --experiment EXP --arm candidate
ctrlrtn workflow discovery-compare OLD_JOB NEW_JOB
ctrlrtn workflow discovery-project JOB FAMILY
ctrlrtn workflow discovery-project JOB FAMILY --format mermaid --output family.mmd
ctrlrtn workflow discovery-project JOB FAMILY --format json --output family.json
ctrlrtn workflow identify FAMILY NAME VERSION --output proposal.json
ctrlrtn workflow identify-verify proposal.json
ctrlrtn workflow diagnostics   # malformed/conflicting explicit facts
ctrlrtn workflow steps         # explicit step cost/latency/outcomes
ctrlrtn workflow diagram TASK  # Mermaid task graph on stdout
ctrlrtn workflow recommendations # advisory optimization opportunities
```

Inference never runs on the request path. The command replaces the current
algorithm version's derived rows, skips ambiguous evidence, and prints
precision/recall against explicitly instrumented dependencies. An unverified or
even confirmed inferred edge remains analysis-only.

The interactive console provides the same workflow-identification operations:
`u` freezes default inputs and queues a discovery job, `f` opens the scoped
discovery dialog, `i` creates an immutable proposal
for the selected family, and `y` verifies a proposal. Run `ctrlrtn worker`
to execute queued discovery; progress, cancellation, restart state, and the
completed result appear in Jobs. The newest valid result automatically fills
the discovered-family table. `jobs export JOB PATH` writes its checksummed
analysis artifact. Focus a completed discovery job and press `w` to compare it
with the previous completed snapshot. Assignments retain task digests, variant
fingerprints, match scores, identity source, ambiguity, and unclustered reasons;
the renderer caps long lists while the artifact remains complete. These
artifacts are not active routing configuration.

Artifact v3 also stores privacy-safe family projections. Selecting a family
shows its representative timeline, path frequencies, branches, joins,
retries/loops, tool operations, provider/model mix, tokens, cost, latency, HTTP
failures, ambiguity, and unknown end-to-end outcome denominator. Digest-only
per-task timelines and metrics remain available in the artifact, while the
console bounds their display. The projection retains no raw task IDs or bodies.
Mermaid edges are dotted and both exports are
analysis-only; neither is accepted as workflow or routing authority.

Discovery scopes are task-cohort selectors. Time bounds apply to the task's
last observed call; provider and served-model filters select tasks containing a
matching call but retain their complete trajectories. Experiment arms require
an experiment ID. The immutable scope is printed in job details and comparison
output. Snapshot comparison accepts equal scopes or a single intentional
difference—time window, provider, model, or arm within one experiment. It
rejects scopes that differ across multiple dimensions unless the CLI operator
explicitly supplies `--allow-unrelated`; inferred families are never promoted
to declared workflow versions.

Artifact v5 freezes selection diagnostics alongside the exact input bindings:
available and selected explicit tasks, truncation, scope and declared-workflow
exclusions, unscoped coverage, selected trace count, pruned payloads, and
unkeyable calls. The CLI and console show these denominators beside discovery's
uncorrelated and ambiguous counts. Selection is deterministically bounded by
the most recently observed task and retains each selected task completely.

Retention never rewrites a completed discovery artifact. When payload pruning
touches its frozen input, the database records a separate source invalidation;
the console marks the job prominently. `jobs export` then fails closed unless
`--allow-invalidated` is supplied, in which case it exports the immutable stale
artifact with a warning. Queued and running inputs remain protected.

`workflow steps` can be scoped with `--workflow NAME --version REVISION` and
`workflow diagram TASK --output graph.mmd` writes the same safe, read-only
projection to a file. Explicit application edges use solid arrows; persisted
analysis-only inferred edges use dotted arrows. The export is never accepted as
workflow configuration. The step report never mixes versions. Its outcome
columns come only from terminal step events; the existing task outcome is
deliberately not copied to every step. The report counts active, inconsistent,
and outcome-missing runs so incomplete attribution cannot look like success.

`workflow recommendations` derives review prompts from explicit step metrics
and committed descriptive definitions. It can flag expensive steps, repeated
model calls, declared siblings worth investigating for parallel execution, and
single-successor chains worth reviewing for possible fusion.
Every item includes a scenario-based expected benefit, evidence confidence, and
hazards. These are analysis artifacts only: the command cannot route, schedule,
fuse, parallelize, or otherwise change a workflow. Use `--workflow` and
`--version` to narrow the report.

The future executor has a reviewed
[design contract](execution-transformations.md). A separate proposed-plan
schema can be checked with `execution-plan validate` or described with
`execution-plan explain`; both commands are inert and accept only
`stage: proposed`. These files are not accepted by `routing-config`, and no
executor or activation path is implemented. Recommendations cannot be activated
as transformations.

`execution-plan simulate PLAN SCENARIO --state FILE` can exercise a parallel
proposal with static fixtures. `simulation-status` and `simulation-cancel`
inspect or control its dedicated state file from another process. This is an
offline state-machine test, not an experiment arm or executable routing config;
it cannot call a model or tool.

`execution-plan evidence-build`, `evidence-approve`, and `evidence-verify`
create and check immutable complete-trajectory artifacts separately from routing
configuration. Approval is offline-only, operator identity is an unauthenticated
assertion, and neither the gateway nor the parallel primitive reads it. These
commands therefore cannot activate or promote a transformation.

`ctrlrtn sessions` reports calls, use-cases, errors, tokens, known spend,
and unknown-cost calls per value. When a session ceiling is configured it also
shows known remaining spend; a session with any unknown-cost call shows `N/A`
instead. Missing or empty values group as `(unsessioned)` when reporting.

These client-supplied headers are attribution hints among cooperating
applications, not authentication or access control. A caller can forge or rotate
a session ID, so a session ceiling prevents accidental overspend by a cooperating
client; it is not a tenant quota.

## Spend ceilings

Budgets are optional. Hard ceilings always block; a use-case can also have an
earlier, evidence-gated fallback threshold:

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

Global and use-case accounting resets at midnight UTC. A session ceiling is a
lifetime limit for each non-empty `x-ctrlrtn-session` value; use a new unique value
for each logical session. When `session.limit_usd` is configured, model requests
without the header fail locally with HTTP 400 and `ctrlrtn_session_required`.

At a reached ceiling, the next matching call fails locally with HTTP 429 and
`ctrlrtn_budget_exceeded`; the response names the scope, recorded spend, and
limit. By default, accounting is eventually consistent: cost is known only after
a response is recorded, so calls already in flight can overshoot the configured
amount. This default mode is not a reservation or billing ledger.

`fallback_at_usd` is valid only below the same use-case's `daily_usd`. At that
threshold the router may switch to one explicitly approved candidate; the hard
daily ceiling still returns `ctrlrtn_budget_exceeded`. Approval consumes the
machine-readable artifact already produced by the non-inferiority evaluation:

```bash
uv run ctrlrtn replay-eval tag:editor gpt-4o-mini \
  --yes --json editor-replay.json
uv run ctrlrtn fallback approve editor-replay.json
uv run ctrlrtn fallback list
```

To evaluate one stable workflow step, add the complete exact scope to replay,
shadow, or live split commands:

```bash
ctrlrtn replay-eval tag:editor local-writer --baseline incumbent \
  --workflow article-pipeline --workflow-version git:abc123 --step draft
ctrlrtn shadow start tag:editor local-writer --sample 10 \
  --workflow article-pipeline --workflow-version git:abc123 --step draft
ctrlrtn experiment start tag:editor local-writer --split 50 \
  --workflow article-pipeline --workflow-version git:abc123 --step draft
```

All three fields are required together. Offline replay selects only successful
traces at that exact stable key. Shadow and split modes ignore incomplete,
malformed, other-version, and other-step traffic. Scoped live tripwires use
explicit step lifecycle outcomes and never inherit the task outcome. These
results support claims about that step only; they do not show that the candidate
can own or preserve the complete workflow trajectory. Scoped replay artifacts
are therefore rejected by use-case fallback approval and excluded from the
whole-role campaign report.

`fallback approve` accepts only an exact `NON_INFERIOR` verdict and stores the
artifact's use-case, baseline, candidate, and creation time. The request must
still use that exact evaluated baseline; stale evidence for a changed baseline
does not apply. `--provider NAME` can bind the candidate to a named provider,
but the proxy permits the switch only when its API identity matches the current
upstream. A running experiment or active route keeps precedence and is never
overridden. If no approval applies, the threshold fails closed with HTTP 429 and
`ctrlrtn_budget_fallback_unavailable`; model names are never used to guess.

Set `reserve_in_flight: true` to include admitted but unsettled requests in each
matching ceiling. The router estimates a maximum request cost from the served
request body: its byte length plus a small protocol allowance at the model's
highest input/cache rate, plus the requested output limit at the output rate.
`max_tokens` or `max_completion_tokens` supplies that output bound; when neither
is present, the model catalog's `max_output` is used. If no bound is available,
the request fails locally with HTTP 400 and `ctrlrtn_unreservable_request`.
For a reservation-driven rejection, the response's `spent_usd` is the effective
admitted amount—settled spend plus estimates still in flight.

The reservation is process-local and remains held until the enriched trace is
persisted. Settlement replaces the estimate with recorded actual cost. Completed
response settlement uses bounded recorder backpressure. Failure telemetry stays
non-blocking; if it is dropped, or persistence otherwise fails, the reservation
remains held until restart rather than admitting more work against uncertain
capacity. This closes the concurrent-admission race, but it is still not an exact
provider billing guarantee: proprietary tokenization, provider-added tokens,
partial responses, and billing after a connection failure can differ from the
estimate. Multiple router processes also do not share reservations.

When a budget applies, a paid provider/model without a catalog price fails
locally with HTTP 503 and `ctrlrtn_unpriced_model`. A session containing an earlier
unknown-cost call also fails closed with `ctrlrtn_unknown_session_cost`, including
after restart. Unknown never means zero; `free: true` is the explicit
provider-wide zero-cost declaration. Use `ctrlrtn spend` for historical
totals, `ctrlrtn sessions` for each session, and `ctrlrtn budget` for
the current kill-switch state, configured ceilings, today's known remaining
spend, whether in-flight reservations are enabled, unknown-priced calls, and
budget rejection counts. It also shows each configured fallback threshold and
the number of evidence-approved fallback calls recorded today. The command runs
in a separate process and therefore does not include the live gateway's
process-local reserved amount.

`ctrlrtn console` renders the same status above its live traffic graphs and
refreshes it from the read-only SQLite connection. It has the same persistence
boundary: settled/recorded accounting is visible; the gateway process's current
in-memory reservations are not.

The `budget` view is operational telemetry, not a billing ledger. Remaining
amounts use persisted known spend only, and blocked counts cover locally
recorded budget rejections since this version was deployed. Recording is
non-blocking, so a saturated recorder queue can drop telemetry rather than delay
a client response.

`kill_switch: true` is the independent emergency stop. It is fixed at startup
and blocks all provider traffic locally; changing it requires a restart.

## Dataset lineage for future local-model experiments

Create a payload-free manifest from successful calls in one use-case:

```console
ctrlrtn dataset create tag:editor editor-dataset.json \
  --limit 1000 --train-percent 80 --salt campaign-2026-08
ctrlrtn dataset verify editor-dataset.json
```

For step-specific work, supply `--workflow`, `--workflow-version`, and `--step`
together. Selection uses the replay sampler's deterministic task-aware ordering.
Every call belonging to one `x-ctrlrtn-task` cluster stays wholly in train or
evaluation; calls without a task identity are independent units. At least two
clusters are required and both splits are always non-empty.

The manifest contains trace IDs, hashed cluster identity, request/response
SHA-256 bindings, scope, partition parameters, counts, and a self-digest. It
contains no payloads or raw task IDs and grants no export, training, routing, or
execution authority. Hashes are not anonymization: guessed low-entropy content
can be checked against them, so protect the artifact as sensitive experiment
metadata. Verification fails after payload pruning or any trace-byte change.

Bind an offline replay evaluation to the held-out partition with:

```console
ctrlrtn replay-eval tag:editor candidate-model \
  --baseline baseline-model --dataset-manifest editor-dataset.json
```

Manifest mode evaluates every trace in the evaluation split and therefore does
not accept `--limit`. The positional use-case must match. A step-scoped manifest
supplies its own exact workflow/version/step scope; explicitly repeated scope
flags must match it. Both foreground and background modes verify the complete
manifest against live SQLite before showing the spend plan. A durable job stores
only the selected evaluation bindings and rechecks them when claimed, before
opening provider clients. Machine-readable replay evidence carries the manifest
digest, `evaluation` split label, and evaluated trace count.

## Named providers

Use a named provider when two upstreams expose the same API paths. Each name is
mounted at `/<name>` by default; the router strips that prefix before forwarding
the request:

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

Point the Ollama client's base URL at `http://localhost:4000/ollama`. A request
to `/ollama/v1/chat/completions` reaches Ollama as `/v1/chat/completions`.
Named providers augment the built-in unprefixed OpenAI and Anthropic routes, so
the ordinary `/v1/...` endpoints continue to reach their default providers.

`base_url` is required. `prefix` defaults to `/<name>` and must be a non-root
path without a trailing slash. `api` identifies the upstream wire protocol;
the only accepted values are `openai` and `anthropic`, and typos fail startup.
Omitting it retains legacy opaque pass-through but makes the provider ineligible
for protocol-checked cross-provider routing. `free: true` declares zero
marginal token cost. This keeps provider-specific behavior correctly scoped—for
example, Anthropic cache-control injection is never applied to an
OpenAI-compatible upstream, and an Ollama model is recorded at `$0` even if its
model name also exists on a paid provider.

The router records the provider name and `free` policy with every trace. A later
configuration change therefore cannot rewrite the historical pricing context.
Token counts and latency are still recorded for free calls; `free` changes only
their calculated cost.

### Provider-owned credentials

An authenticated named provider can read its own secret from the process
environment. The secret is not stored in YAML, SQLite, traces, or CLI output:

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

The configured environment variable must be set when the router starts. On a
request to this provider (including a cross-provider candidate), client
credential headers and credential-named query parameters are removed and the
provider-owned credential is injected. A provider without `credential` retains
ordinary client-credential pass-through; local Ollama needs none.

`providers` cannot be combined with `upstream`, which deliberately forces every
request to one destination. The older `openai_upstream` and
`anthropic_upstream` settings remain supported for simple replacement setups.

### Cross-provider experiments and routes

Experiments and persistent routes may select a named provider:

```sh
ctrlrtn experiment start tag:editor qwen2.5:0.5b \
  --provider ollama --split 50
ctrlrtn route set tag:editor qwen2.5:0.5b --provider ollama
```

On an experiment's baseline arm, the original provider and body pass through.
On its candidate arm, the router swaps the model and sends the same
provider-facing path to the named provider. `route adopt` preserves the provider
tested by the experiment.

The baseline and candidate must declare the same `api`. A missing provider or
API mismatch fails locally with `ctrlrtn_provider_mismatch` and is recorded on the
candidate arm; no request reaches an incompatible upstream. Only matching wire
formats are supported—there is deliberately no Anthropic↔OpenAI translation.

When the provider changes, the router removes credential headers and
credential-named query parameters before forwarding. This prevents a baseline
provider key from leaking to the candidate. Configure `credential` on an
authenticated candidate to inject its provider-owned key; local Ollama needs
none.

## Ollama quick start

Ollama's OpenAI-compatible API works directly as a named upstream. Start Ollama
and create `ctrlrtn.yaml`:

```yaml
providers:
  ollama:
    base_url: http://localhost:11434
    api: openai
    free: true
```

```sh
ollama serve
ctrlrtn serve
```

Configure the application's OpenAI-compatible client to use
`http://localhost:4000/ollama` and call `/v1/chat/completions` as usual. The
unprefixed OpenAI route remains available at `http://localhost:4000`. Only
Ollama's `/v1` OpenAI-compatible endpoints are supported. Native `/api/chat` is
an intentional non-goal: it uses a separate streaming and usage contract, while
the compatibility surface already supports recording, routing, and experiments.

For a one-provider setup, the older
`CTRLRTN_OPENAI_UPSTREAM=http://localhost:11434` shortcut still works, but
it replaces the unprefixed OpenAI destination rather than adding a second one.

For streaming requests, set `stream_options.include_usage` to `true`. Ollama
then emits token counts in the final SSE chunk, allowing the router to record
usage. With `free: true`, those calls record their measured tokens and latency
with an explicit `$0` marginal cost.

**Model prices** (and max-output caps): point `CTRLRTN_PRICES` at your
own TOML to overlay the bundled table (USD per 1M tokens; entries merge per
field — see `src/ctrlrtn/telemetry/prices.toml` for the defaults and
matching rules).

## Git-backed routing state

Gateway settings describe the deployment and provider credentials. Model
control state can separately live in a Git repository as `routing.yaml`; start
from `routing.example.yaml`:

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
    max_cost_usd_per_task: 5.0
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

Experiment `id` is required and stable. Changing any experiment field requires
a new id, preserving the stopped experiment as historical evidence and
re-bucketing tasks deliberately. Splits remain 1–99, and named providers must
exist in the gateway's normal `providers:` configuration.

`workflows` declares a descriptive, non-executable graph of stable step names,
allowed predecessors, and `fan_out`/`retry` capabilities. Conditions are opaque
labels, never evaluated code. A `workflow_routes` entry must reference a
declared workflow, exact version, and declared step. At serving time the
precedence is step route, workflow route, use-case route, then pass-through
(a running use-case experiment continues to own its split). Partial, malformed,
unknown-version, and inferred identities cannot authorize these rules.

```bash
ctrlrtn routing-config validate routing.yaml
ctrlrtn routing-config diff routing.yaml
ctrlrtn routing-config activate routing.yaml --repo .
ctrlrtn routing-config status
```

`validate` checks schema, domain limits, and providers without writing. `diff`
compares the file with live SQLite state. `activate` additionally requires the
file to be inside the named Git repository, tracked, committed, and clean. It
reconciles every persistent route and running experiment in one SQLite
transaction, recording repository HEAD, relative path, file SHA-256, and
activation time. Any failure rolls the entire transaction back; unchanged
objects retain their original activation timestamps.

Every routed trace records the winning rule scope/key and activated Git
revision. The CLI `show` view and console call detail expose that explanation.

The file is authoritative: omission clears a route or stops a running
experiment. Stopped experiments, traces, outcomes, durable jobs, and
measurements are never deleted. Manual `route` or `experiment` commands can
intentionally diverge from the recorded revision; `routing-config diff` exposes
that drift and a later activation restores the declaration.

Keep credential values, raw traces, outputs, databases, and exported evidence
outside this repository. Git fetch, pull, push, and conflict resolution are
explicit operator actions; activation performs no network access and never
changes the working tree.

The console uses `routing.yaml` and `.` by default. Override those locations
when launching it:

```bash
ctrlrtn console \
  --routing-config /etc/ctrlrtn/control/routing.yaml \
  --routing-repo /etc/ctrlrtn/control
```

Its routing status line shows the active revision. Press `g` to validate the
clean committed file and review a scrollable active-versus-desired diff, then
confirm local activation. The revision and document hash are checked again after
confirmation, closing the window where the file could change after review. The
console does not fetch, pull, push, commit, or resolve conflicts.

## Online shadow experiments

`shadow start` mirrors a deterministic percentage of task-tagged traffic (and a
per-call percentage for untasked traffic) without putting candidate output on
the response path. Only one shadow or live split may own a use-case:

```bash
ctrlrtn shadow start tag:editor qwen2.5:7b \
  --provider ollama --sample 10
ctrlrtn shadow list
ctrlrtn shadow stop shadow:...
```

Mirrors use a separate HTTP client and bounded worker queue. Both traces carry a
shadow experiment id, pair id, and `actual`/`candidate` role. `shadow list`
reports submitted, completed, failed, and dropped counts; treat non-zero failure
or drop rates as attrition, not successful evidence. Drops are accumulated
off-path and flushed periodically and during graceful shutdown.

The console exposes the same lifecycle with confirmation: press `h` to start a
shadow and `z` to stop the focused running shadow. Its Shadows table shows all
experiments, and the detail pane compares the newest actual (served) and
candidate (never served) traces. One-sided pairs are explicitly marked as
attrition.
