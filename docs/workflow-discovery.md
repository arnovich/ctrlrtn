# Workflow discovery and analysis

What the proxy derives from recorded traffic after the fact. "Legacy
traffic" here means calls recorded without workflow identity headers: calls
that carry only `x-ctrlrtn-task`, and calls with no task header at all. The
declared contract is in [workflow-identity.md](workflow-identity.md).
Everything on this page is analysis only: no command routes a call, changes
a route or an experiment, or executes a step. Serving reads only the
declared rules in [configure.md](configure.md#serving-precedence).

## Commands

Every command reads the SQLite database named by the shared configuration.

| Command | Reads | Writes | Authority |
| --- | --- | --- | --- |
| `workflow infer` | Traces and explicit lifecycle facts | Replaces the inferred-edge table | Analysis only |
| `workflow inferred` | The inferred-edge table | Nothing | Analysis only |
| `workflow discover [--background] [scope flags]` | Legacy traffic, bounded by `--limit` | Nothing; with `--background`, one queued job | Analysis only |
| `workflow discovery-compare OLD NEW` | Two completed discovery jobs | Nothing | Analysis only |
| `workflow discovery-project JOB FAMILY` | One completed discovery job | A new file with `--output` | Analysis only |
| `workflow identify FAMILY WORKFLOW VERSION --output PATH` | Legacy traffic | One new proposal file | Analysis only |
| `workflow identify-verify PATH` | One proposal file | Nothing | Analysis only |
| `workflow diagnostics` | Traces and lifecycle events | Nothing | Analysis only |
| `workflow steps [--workflow W] [--version V]` | Traces and lifecycle events | Nothing | Analysis only |
| `workflow diagram TASK [--output PATH]` | One task's traces, events, and inferred edges | A file with `--output` | Analysis only |
| `workflow flow WORKFLOW VERSION [--limit N] [--output PATH]` | The task graphs of one version | A new file with `--output` | Analysis only |
| `workflow catalog [--workflow W]` | Runs, task outcomes, routes, experiments, shadows | Nothing | Analysis only |
| `workflow compare WORKFLOW LEFT RIGHT` | The catalog rows of two versions | Nothing | Analysis only |
| `workflow step-detail TASK STEP_RUN` | One step run's events, trace metadata, tool events | Nothing | Analysis only |
| `workflow recommendations [--workflow W] [--version V]` | Step metrics, declared definitions, task graphs | Nothing | Analysis only |
| `workflow erase-task TASK [--apply]` | One task's rows | With `--apply`, deletes them | Erasure only; no routing effect |

`ctrlrtn workflow <command> --help` lists every option. The console runs
discovery, identification, verification and comparison with `u`, `f`, `i`,
`y` and `j` ([console.md](console.md)).

## Explicit and inferred graphs

Each task with workflow identity has an observed graph built from its traces
and lifecycle events. Nodes are step runs. An edge is `explicit` when the
application declared it through `parent_step_run_id` or
`dependency_step_run_ids`, and `inferred` when analysis reconstructed it. A
run's status is its one terminal status, `active` when only `started` was
seen, `observed` when only traces exist, or `inconsistent` when terminal
statuses, identities or attempts conflict.

An inferred edge carries its provenance: source and target trace IDs, the
algorithm and version, a confidence, and a confirmation state.

| State | Meaning |
| --- | --- |
| `confirmed` | The application declared the same dependency |
| `contradicted` | The target run has explicit lifecycle facts that do not declare this dependency |
| `unverified` | The target run has no explicit ground truth |

Inferred edges are rendered differently from explicit ones and are never
read by routing, experiment assignment or execution control.

## Tool-result link inference

`workflow infer` runs the `tool-id-link/v1` method and replaces the
persisted edge table. Within one task and workflow version it matches an
Anthropic `tool_use.id` or an OpenAI `tool_calls[].id` in a recorded
response to the first later request carrying the same `tool_use_id` or
`tool_call_id`. A tool ID with more than one producer is skipped as
ambiguous, cross-task matches are never made, and a tool ID repeated in
accumulated conversation history creates no further edge. Only the SHA-256
digest of the tool ID is kept as evidence. The command prints precision and
recall against tasks that have explicit lifecycle facts. `workflow inferred`
lists the persisted edges with their confidence and state. Payload pruning
deletes every inferred edge whose evidence trace was pruned
([deploy.md](deploy.md)).

## Family discovery over legacy traffic

`workflow discover` clusters legacy traffic into recurring families. A task
is eligible when none of its calls carries workflow identity. Each call
becomes a node labelled by its use-case key and the tool names it produced.
Exact tool-call and tool-result IDs create the only edges; timestamps,
connections, route fingerprints and prompt similarity never join calls. For a
streamed response, the assistant tool call echoed in the next request is
attributed to the immediately preceding call of the same task.

Calls without `x-ctrlrtn-task` are grouped by exact continuity only: a call
joins one earlier fragment when it consumes a tool ID that fragment
produced, or when its message history strictly extends that fragment's
history, and exactly one fragment qualifies. Equal candidates count as
ambiguous and stay apart. These synthetic identifiers never become declared
task or workflow identity. Raw task IDs appear only as short digests, and
bodies are read only to recover tool links.

Structurally similar node and edge multisets are grouped by a deterministic
threshold (`--similarity`, default 0.75) and a minimum support
(`--min-support`, default 2, never lower). The report shows each family's
support, structural variants, cohesion (mean pairwise similarity), the
fraction of members with an exact tool link, recurring nodes and edges, and
member digests. A family without exact links is a recurring role and
tool-shape multiset, not a dependency chain. Single-link clustering can
chain distant variants through a third, so the score is cohesion, not
confidence. The report also counts unclustered, uncorrelated
and ambiguous inputs, so family support cannot pass for total traffic
coverage.

### Durable discovery jobs and scope

`workflow discover --background` freezes the selected trace IDs, their
evidence digests and the scope into one job for `ctrlrtn worker`. The worker
re-verifies the frozen inputs, reports progress, honours cancellation, and
writes a checksummed result holding family assignments, per-family
projections and metrics, without trace IDs or bodies. `--limit` is capped at
2000 tasks and a job holds at most 20000 traces. A queued or running job
protects its traces from pruning and erasure. Pruning a completed job's
source traces leaves the result unchanged and records an invalidation that
`jobs export` refuses without `--allow-invalidated`.

The scope flags `--since`, `--until`, `--provider`, `--model`,
`--experiment` and `--arm` select tasks by last-observed time, provider,
served model, experiment and arm; `--arm` requires `--experiment`. A
matching task contributes every call it made. Calls without a task header
are included only when no provider, model, experiment or arm filter is set.

`workflow discovery-compare OLD NEW` matches the families of two completed
jobs one to one by normalized node and edge shape (`--similarity`, default
0.6), not by ID. It labels the scope relationship as same-scope,
time-windows, providers, models or experiment-arms; scopes that differ in
more than one way are refused unless `--allow-unrelated` is given. Jobs with
different algorithms or parameters stay visibly incompatible.

`workflow discovery-project JOB FAMILY` renders one family from a completed
job as text, Mermaid or JSON (`--format`): representative and per-task
timelines, path frequencies, tools, retries and loops, branches and joins,
and provider, model, token, cost, latency and HTTP-failure totals. Tasks
appear as digests, and every member stays in the unknown-outcome
denominator, because HTTP success is not a workflow outcome.

## Identification proposals

`workflow identify FAMILY WORKFLOW VERSION --output PATH` re-runs discovery
with the given limits, finds the family, and writes an immutable,
checksummed proposal: the family snapshot, member digests and a suggested
`workflows` fragment for `routing.yaml`. The output path must not exist.
`workflow identify-verify PATH` checks the proposal's digest and shape. A
proposal is inert: it grants no authority until an operator copies its
fragment into the Git-backed configuration and activates it
([configure.md](configure.md)).

## Per-task and aggregate views

| Command | Shows |
| --- | --- |
| `steps` | Per stable step: runs, calls, cost, latency, run duration, terminal-state counts, explicit outcomes and scores. Runs group by `(task_id, step_run_id)`, then by `(workflow, workflow_version, step)`. `--version` requires `--workflow`; versions are never mixed. |
| `diagram` | One task graph as deterministic Mermaid with opaque node IDs and escaped labels. Explicit edges are solid, inferred edges dotted. |
| `flow` | One exact version aggregated over its tasks: per-step task, run, call and failure counts, cost and latency, edge task counts, and branching denominators. `--output` writes a Mermaid Sankey of explicit edges only. |
| `catalog` | Every exact version: tasks, calls, cost, average latency, task-outcome counts, models, providers, and the routes and experiments that target it. |
| `compare` | Two versions of one workflow side by side, from the catalog rows. |
| `step-detail` | One step run: identity, lifecycle events, trace metadata (status, provider, model, cost, latency, experiment, winning route rule and revision), and tool-operation events. No payloads. |
| `diagnostics` | Valid and invalid workflow traces, reused run IDs, cross-task dependencies, conflicting terminal runs, unreported and inconsistent step runs, and error counts. |

A step inherits no outcome from its task: step outcomes come only from
explicit terminal step events, and a stable-step aggregate covers only runs
of one workflow version. Mermaid is an export format; the proxy never
imports it.

## Recommendations

`workflow recommendations` derives review prompts from explicit step
metrics, declared definitions and task graphs. The kinds are
`expensive_step`, `repeated_calls`, `parallel_candidate`,
`fusion_candidate`, `failure_branch`, `retry_amplification`,
`expensive_common_path` and `sequential_critical_path`. Each item states a
scenario-based expected benefit, a `low` or `medium` confidence from run
counts, its evidence, and the hazards that stop the proxy from acting on it.
Step-level items need at least 3 runs. Nothing here is executable policy.

## Erasing a task

`workflow erase-task TASK` previews complete erasure of one task. With
`--apply` it deletes the task's traces, workflow events, tool-operation
events, inferred edges and outcomes in one transaction; a queued or running
job that binds the task or any of its traces blocks it. Graphs, metrics and
flow aggregates are query-time projections, so they disappear with their
source rows, and the database keeps no generated diagrams. Files written
with `--output`, exported job evidence and proposals are external artifacts
the operator deletes under the same retention policy. Disk reclamation and
backups are in [deploy.md](deploy.md).
