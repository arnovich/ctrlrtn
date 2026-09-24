# Workflow identity and propagation contract

Status: accepted design for the workflow persistence slice. This document does
not authorize the router to schedule, merge, fuse, skip, or parallelize work.

## Purpose and boundary

The router needs to distinguish a stable logical step from one execution of that
step. Prompt fingerprints and use-case tags describe the kind of model call;
they do not describe causality. This contract makes workflow identity explicit
so recorded calls can be grouped, attributed, visualized, and eventually routed
at step scope without inferring control facts from prompt text.

The application remains the orchestrator. The router observes declared identity
and lifecycle events. It may use a validated declared definition for routing,
but observed or inferred graph structure is analysis-only.

## Identity model

| Field | Meaning | Stability and ownership |
|---|---|---|
| `workflow` | Workflow/task-type name, such as `article-pipeline` | Stable, application-declared |
| `workflow_version` | Exact definition revision | Immutable for a definition; Git commit when available, otherwise a content digest or application version |
| `task_id` | One end-to-end execution | Existing globally unique `x-ctrlrtn-task` value |
| `step` | Logical step name, such as `research` | Stable within one workflow version; distinct from use-case/agent role |
| `step_run_id` | One runtime invocation of a step | Globally unique, application/SDK-generated |
| `parent_step_run_id` | Immediate spawning or controlling invocation | Optional runtime edge within the same task |
| `dependency_step_run_ids` | Invocations whose results this run depends on | Zero or more runtime edges within the same task |
| `attempt` | Retry/loop attempt for this logical invocation | Positive integer, starting at 1 |

The stable step key is `(workflow, workflow_version, step)`. The execution key
is `(task_id, step_run_id)`. Neither `task_id` nor `step_run_id` is a normal
routing target. Temporary task overrides are a separate, expiring operational
feature if they are ever added.

One step run may contain multiple provider calls. Those traces share the same
step-run identity and remain separate calls. Retries and loop iterations create
new step-run IDs while retaining the stable step name and incrementing
`attempt`. Dynamic fan-out uses one run ID per child. A join lists every actual
dependency run, so concurrency is represented without guessing from timestamps.

`parent_step_run_id` means "created or controlled by" and is not a substitute
for data dependencies. A child may have one parent and several dependencies.
All referenced runs must share its `task_id`; cross-task relationships need a
future explicit link type and must not be smuggled into these fields.

## Wire contract

Instrumented LLM requests carry these lowercase semantic headers (HTTP casing is
irrelevant):

```text
x-ctrlrtn-task
x-ctrlrtn-workflow
x-ctrlrtn-workflow-version
x-ctrlrtn-step
x-ctrlrtn-step-run
x-ctrlrtn-step-parent                 # optional
x-ctrlrtn-step-dependencies           # optional comma-separated run IDs
x-ctrlrtn-step-attempt                # optional; defaults to 1
```

Workflow metadata is all-or-nothing for an authoritative step trace:

- `workflow`, `workflow_version`, `task_id`, `step`, and `step_run_id` are
  required together;
- a parent or dependency without that complete identity is invalid metadata;
- identifiers use the ASCII grammar
  `[A-Za-z0-9][A-Za-z0-9._:/-]*`; empty values, control characters, duplicate
  dependency IDs, an attempt below one, and identifiers over their configured
  limit are rejected from workflow attribution;
- malformed metadata must not break ordinary proxying in compatibility mode,
  but is recorded as a propagation diagnostic; strict SDK mode raises before
  sending;
- the gateway never derives a routable step identity from request bodies.

Initial limits should match the existing bounded task-ID approach: 128 ASCII
bytes for names and versions, 128 bytes for run IDs, at most 32 dependencies,
and at most 4 KiB for the complete workflow header set. The persistence slice
must define constants once and use them in both SDK and gateway validation.

ctrlrtn headers are router control metadata. The proxy records their normalized
values in dedicated columns and removes all `x-ctrlrtn-*` headers before contacting
an upstream provider. They must not disclose internal graph names or identifiers
to model providers merely because the client supplied them.

## SDK contract

The intended API extends the existing context-variable model:

```python
with sdk.edition(
    workflow="article-pipeline",
    workflow_version="git:8d23f1a",
    report_to=GATEWAY_URL,
) as run:
    with run.step("research") as research:
        collect_sources(client)
        research.report(success=True)

    with run.step(
        "draft",
        dependencies=[research.step_run_id],
    ) as draft:
        write_draft(client)
```

`edition()` creates or binds the task and workflow context. `step()` creates a
new run ID unless one is explicitly supplied for distributed propagation. It
stamps every nested LLM request and emits best-effort lifecycle events:
`started`, then exactly one of `completed`, `failed`, `cancelled`, or `skipped`.
An uncaught exception emits `failed` and is re-raised. Event delivery failure is
visible in diagnostics but never changes application execution.

Context variables propagate naturally to async child tasks. `sdk.bind()` must
copy the complete workflow and step context across thread boundaries. A
subprocess or another service receives an explicit serializable carrier created
by the SDK and imports it after validation; Python context copying is not a
cross-process protocol. Creating concurrent children from one captured context
requires opening a distinct `step()` inside each child so run IDs are not
accidentally shared.

Explicit header stamping remains supported for non-httpx clients. Caller values
must not partially override a live SDK context: either use the current complete
context or supply a complete validated carrier.

## Lifecycle and status events

Request traces prove that a provider call occurred, not when a step began or
whether a step with no model call completed. The persistence slice therefore
adds a host-protected local endpoint for workflow events with the same deployment
trust boundary as the existing outcome endpoint. An event contains:

```text
event_id, ts, task_id, workflow, workflow_version, step, step_run_id,
parent_step_run_id, dependency_step_run_ids, attempt, status,
optional success/score/error_code
```

`event_id` is unique and makes retries idempotent. State transitions are
append-only facts; reports select the latest valid event by arrival order while
retaining contradictory or late events for diagnostics. Events cannot rewrite
trace identity. A trace received before its `started` event is valid because
delivery can reorder.

Valid terminal transitions are `started -> completed|failed|cancelled` and
`skipped` without provider calls. Repeated identical events are harmless.
Conflicting terminal events mark the run inconsistent rather than choosing the
more favorable outcome.

## Declared, observed, and inferred graphs

The authoritative definition is declared by the application and versioned as
configuration. It contains stable steps and allowed dependencies, including
explicit markers for dynamic fan-out, loops, and conditional branches. A Git
revision is preferred; otherwise the router records the canonical document's
SHA-256 digest. Runtime activation records both the source revision and document
digest, using the same stale-preview protection as routing configuration.

Each execution produces an observed graph from explicit run IDs and lifecycle
events. It may legitimately be a subset of the declared graph because branches
and skipped steps are normal. A declared edge not observed in one task is not a
failure unless the definition marks it required.

Off-path reconstruction may add inferred nodes or edges from request history,
tool calls, and tool results. Every inferred fact stores:

- source trace/event IDs;
- inference algorithm and version;
- confidence and creation time;
- whether an explicit fact later confirmed or contradicted it.

Inferred facts are rendered differently, excluded from propagation certification
by default, and forbidden as routing, experiment-assignment, or execution-control
inputs. Accuracy must be measured against explicitly instrumented workflows
before an inference method is enabled by default.

The first implemented inference method is `tool-id-link/v1`. It runs only when
the operator invokes `ctrlrtn workflow infer`. Within one task it matches an
Anthropic `tool_use.id` or OpenAI `tool_calls[].id` in a recorded response to the
first later request carrying the corresponding `tool_use_id`/`tool_call_id`.
Ambiguous producers are skipped, cross-task matches are forbidden, and repeated
copies in accumulated conversation history do not create additional edges. Only
the tool identifier's SHA-256 digest is retained as evidence; tool payloads are
not copied into the derived table.

Persisted inferred edges record their source/target trace IDs, algorithm version,
confidence, and `confirmed`, `contradicted`, or `unverified` state. Precision and
recall are reported only against tasks with explicit lifecycle ground truth.
This table has no import path into serving, routing configuration, or experiment
assignment.

### Discovering legacy workflow families

`ctrlrtn workflow discover` analyzes task-scoped traffic that has no
declared workflow identity. Each call becomes an observed node labelled by its
stable use-case route and produced tool-name shape. Exact Anthropic/OpenAI tool
call and result IDs create evidence-only edges; ambiguous IDs create no edge.
For streamed responses, discovery also reads assistant tool calls echoed in a
later ordinary-JSON request. A newly observed call accompanied by its matching
result is attributed only to the immediately preceding provider trace;
conflicting recorded-response evidence remains ambiguous and unlinked.
Calls without `x-ctrlrtn-task` receive analysis-only synthetic grouping when an
exact provider tool ID identifies one prior fragment or one request conversation
is a strict extension of exactly one prior fragment. Timestamps, connections,
route fingerprints, and prompt similarity never join calls. Equal candidates
remain separate and are counted as ambiguous; singleton fragments remain
visible as uncorrelated coverage. Synthetic identifiers are internal inputs to
discovery and cannot become declared task or workflow identity.
Task-specific IDs and payload values are removed, raw task IDs are replaced by
short digests, and structurally similar node/edge multisets are grouped using a
deterministic threshold and minimum support.

The report exposes support, exact structural variants, within-family cohesion,
the fraction of tasks containing an exact tool link, recurring nodes and edges,
and member provenance digests. Optional nodes and retry-count differences may
therefore remain in one family. Families without exact links express only a
recurring role/tool-shape multiset, not a causal chain. Single-link clustering
can also connect two distant variants through an intermediate variant, so the
cohesion score remains visible rather than being called confidence.

Discovery is read-only and analysis-only. It excludes already declared
workflows and has no routing, experiment, recommendation, or execution reader.
`workflow identify FAMILY WORKFLOW VERSION --output NEW.json` can turn one
reviewed result into an immutable, checksummed proposal containing its family
snapshot, provenance digests, and a suggested descriptive control-config
fragment. `workflow identify-verify PATH` verifies that artifact. The proposal
is deliberately inert: it is not a routing-config input and creates no workflow
authority until an operator reviews and deliberately incorporates its fragment
into the Git-backed control configuration.

The console exposes the complete identification workflow: `u` freezes and
queues legacy discovery, selecting a completed job or family displays its
result, nodes, exact edges, variants, cohesion, and member task digests, `i`
creates a new inert proposal, and `y` verifies a proposal file. The standard
worker performs bounded pairwise clustering with durable progress, cooperative
cancellation, restart recovery, and frozen-input digest verification. Its
checksummed result contains no trace IDs or bodies and remains analysis-only.
Each eligible task digest has an assigned or unclustered record with its family,
representative similarity, structural variant fingerprint, trace count,
explicit/synthetic correlation source, ambiguity, and reason. Snapshot
comparison matches families one-to-one by normalized node/edge shape before
reporting support and membership changes, because representative-derived family
IDs may legitimately change as new variants arrive. Differing algorithms or
parameters remain visibly incompatible rather than being silently normalized.
Artifact v3 also binds model, provider, token, cost, latency, and HTTP status
fields into the frozen evidence digest and derives a passive projection for
each family. It shows representative timelines, path frequencies, tools,
retries/loops, branches/joins, and aggregate operational metrics in the
console, plus digest-only per-task timelines and metrics. Because HTTP success
is not a logical workflow outcome, every passive
member remains in the visible unknown-outcome denominator. `workflow
discovery-project JOB FAMILY` exports dotted-edge Mermaid or authority-labelled
JSON without raw task IDs or bodies.
Artifact v4 adds an immutable discovery scope over task last-observed time,
provider, served model, experiment, and arm. A cohort match includes the whole
task trajectory rather than deleting non-matching calls from its graph.
Comparison labels same-scope, time-window, provider, model, and
same-experiment-arm relationships; multiple simultaneous differences are
rejected by default as unrelated. This makes cross-arm and cross-model analysis
explicit without claiming that a structurally matched inferred family is a
declared workflow version.
Artifact v5 records the bounded-selection denominator and strategy, including
truncated and excluded task counts, unscoped coverage, payload-pruned calls, and
unkeyable calls. These appear next to uncorrelated and ambiguous correlation
counts so family support cannot masquerade as total traffic coverage. Completed
artifacts stay immutable after retention, but a durable invalidation record
makes pruned source evidence visible and blocks ordinary export.
The console's long-lived trace store remains read-only; explicit queue and
proposal actions use narrow writes, and proposal files must not already exist.

A declared definition is descriptive rather than executable. Its minimum
canonical form is versioned YAML containing a workflow name and stable step
names, with allowed predecessor step names and capability flags:

```yaml
version: 1
workflow: article-pipeline
steps:
  research:
    allows: {fan_out: true, retry: true}
  draft:
    predecessors: [research]
    allows: {retry: true}
  fact_check:
    predecessors: [draft]
  publish:
    predecessors: [draft, fact_check]
    condition: fact_check_passed       # display label, not executable code
```

The document says which shapes are valid; it does not schedule them. Runtime
run IDs express the exact fan-out members and join dependencies. Loop and retry
permission is explicit, but a condition is an opaque reviewable label rather
than an expression for the router to evaluate. Canonicalization and hashing must
be deterministic, and unknown keys fail validation rather than being ignored.

## Graph and diagram representation

Normalized persisted nodes and typed edges are the canonical representation.
The Textual console should first render a time-ordered step-run list with status,
indentation/edge markers, and a selected-node detail pane; this remains usable in
narrow terminals and for loops too dense for a chart. Mermaid `flowchart` is the
review/export format because it is textual and Git-diffable. IDs are generated
from internal keys and all user labels are escaped, so labels cannot inject
Mermaid syntax. Mermaid is a projection, never imported as authoritative state.

This projection is now implemented. The console's recent-workflows table opens
a per-task timeline with step status, attempts, calls, cost, provider latency,
and models. Solid `->` links are explicit application facts; dotted `~>` links
are persisted analysis-only inference. `ctrlrtn workflow diagram TASK`
exports the same graph as deterministic Mermaid, using opaque node IDs and
escaped labels; `--output PATH` writes it for review or Git history. Neither
view is an orchestration definition or an import format.

The aggregate flow/Sankey projection is implemented over one exact workflow
version at a time. It reports stable-step task/run/call/failure counts, cost and
latency, plus edge task counts and source-task branching denominators. Explicit
and inferred edges are separate in text; the Mermaid Sankey intentionally
contains explicit application facts only. The selected console workflow shows
this aggregate beneath its per-task timeline, and `workflow flow WORKFLOW
VERSION --output FLOW.mmd` exports it. The projection is analysis-only.

## Outcome attribution

Outcomes are explicit measurements, not causal conclusions. The existing task
outcome remains workflow-run scoped and backward compatible. The event API adds
step-run outcomes keyed by `(task_id, step_run_id)`; a stable-step aggregate is
computed only across step runs with matching workflow versions.

A workflow outcome must not be copied onto every step. A step inherits no score
unless the application explicitly reports one or a separately named attribution
method produces an estimate. Derived attribution stores its method, inputs, and
confidence and is labelled inferred. Correlation between a failed workflow and
the last step is not causation.

This boundary is implemented by `ctrlrtn workflow steps`. Metrics aggregate
traces and terminal lifecycle events by `(task_id, step_run_id)`, then by the
stable `(workflow, workflow_version, step)` key. They include calls, provider
errors, tokens, cost, call latency, lifecycle duration, terminal states, and
explicit step success/score coverage. Event-only skipped steps are retained.
Active runs and runs without a step outcome are counted; conflicting terminal
states are marked inconsistent and excluded from success/score statistics. A run
whose ID maps to more than one stable identity is excluded entirely and surfaced
by diagnostics.

Tool operations now have a provider-independent application identity: the exact
workflow step plus stable logical `operation_id`, unique per-attempt ID and
positive attempt number, operation name, and one conservative effect contract
(`unknown`, `pure`, `idempotent`, or `stateful`). Missing knowledge is
`unknown`; ctrlrtn never infers purity from a tool name or provider schema.

Applications emit idempotent `started` and terminal (`completed`, `failed`, or
`cancelled`) events through the SDK. Terminal events require explicit success
and may report a bounded error code, latency, and cost. The gateway persists
these separately and connected step detail labels them as explicit application
facts. Provider-owned tool IDs remain inference evidence and cannot authorize
batching, fusion, retry collapse, or execution changes.

## Routing and experiment semantics

The implemented precedence is:

```text
validated workflow-version + step rule
> validated workflow-level rule
> role/use-case rule
> default/pass-through
```

Rules match stable definition keys, never inferred edges or display labels.
Every decision trace records the winning rule ID and activated configuration
revision. Unknown versions and incomplete workflow metadata fall back to current
use-case behavior; they do not approximately match another version.

Offline replay, online shadow, and split experiments may eventually select a
stable step. Their evidence remains step scoped unless the candidate owns the
whole trajectory and a workflow outcome measures it. A candidate continuing a
baseline-generated history is not evidence of end-to-end workflow equivalence.

## Compatibility and rollout

Existing clients with only `x-ctrlrtn-task` and `x-ctrlrtn-route` continue unchanged.
They remain task/use-case observable but have no authoritative workflow graph.
The rollout order is:

1. SDK types, validation, and propagation diagnostics;
2. additive SQLite columns/tables and gateway extraction;
3. lifecycle event ingestion;
4. declared definitions and conformance reports;
5. read-only console/timeline views;
6. only then step-scoped control rules and experiments.

All schema changes are additive and nullable. Historical traces can receive
inferred annotations, but never fabricated explicit identity.

## Security, privacy, and retention

Workflow headers are assertions from the calling application, not authentication
or authorization. The current same-host deployment and Host checks remain the
trust boundary. A remotely exposed gateway needs authenticated client identity
before workflow metadata can authorize routing or outcome submission.

Identifiers must not contain secrets, user text, prompt excerpts, credentials,
or personal data. Raw errors stay in protected trace/artifact storage; lifecycle
events use bounded machine-readable error codes. Configuration repositories hold
definitions and revisions, never trace bodies, runtime IDs, outcomes, or provider
credentials.

Derived graphs can reveal business logic and failure paths. Retention, pruning,
export, and erasure operate transitively: deleting a source task removes its
runtime graph, inferred facts, and generated diagrams. Aggregates must either be
recomputed or document their non-reversible anonymization policy.

SQLite graphs, metrics, and flow aggregates are query-time projections and are
therefore recomputed from retained source rows. Payload pruning transactionally
invalidates every inferred edge whose evidence referenced a pruned trace. Use
`ctrlrtn workflow erase-task TASK` to preview complete task erasure and add
`--apply` to remove its traces, workflow and tool events, inferred edges, and
outcomes together; queued or running jobs fail the applied operation closed.
Generated diagrams are not persisted by the store. Evidence and exports written
to user-selected files are immutable external artifacts: operators must delete
or regenerate them under the same retention policy, and any approval bound to a
changed artifact must be revalidated.

## Orchestration safety boundary

Observation is not permission to execute. This contract permits recording,
validation, visualization, attribution, routing at declared scope, and later
optimization recommendations. It does not permit the router to reorder, run,
merge, fuse, skip, cache, retry, or parallelize application steps.

Any execution transformation belongs in the separately designed, opt-in
[execution transformation component](execution-transformations.md), with
declared dependency and side-effect semantics, idempotency, resource limits,
cancellation, complete-trajectory offline evidence, guarded online rollout, and
immediate rollback. Tool calls are stateful and non-idempotent unless explicitly
declared otherwise. The design is accepted; no executor runtime is implemented.

## Persistence-slice acceptance tests

The next slice is complete only when tests demonstrate:

- a branching workflow, concurrent fan-out/join, retry, loop, skipped step, and
  multi-call step persist without identity collisions;
- async tasks, bound threads, and exported/imported carriers preserve identity;
- malformed, partial, oversized, cross-task, and contradictory metadata is
  diagnosed without changing legacy proxy behavior;
- workflow headers are not forwarded upstream;
- explicit and inferred edges remain distinguishable in storage and queries;
- workflow and step outcomes never cross scopes or workflow versions;
- old databases and clients continue to work without workflow metadata.
