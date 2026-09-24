# Architecture

The proxy is one local process with two different halves:

- The **data plane** forwards provider requests and streams responses. It does
  only the work required to route, protect an experiment, and capture a trace.
- The **control plane** reads recorded traces to report spend, evaluate models,
  manage experiments, and install explicit routes.

The project favors direct functions and immutable data over framework layers.
SQLite, `httpx`, and Starlette are the main runtime building blocks.

## Request flow

```mermaid
flowchart LR
    Client[Application / SDK client]
    CLI[CLI]
    Console[Console]
    Config[Settings]
    Resolver[Provider resolver]

    subgraph Gateway[Request path: gateway package]
        App[ASGI app]
        Policy[Snapshot router]
        Proxy[Streaming proxy]
        Inject[Request injection]
        Budget[Budget gate]
        Shadow[Shadow mirror queue]
    end

    subgraph ServingPolicy[Serving policy]
        Identify[Use-case fingerprint]
        Experiment[Experiments]
        Route[Persistent routes]
        Fallback[Budget fallbacks]
    end

    subgraph Recording[Off-response-path recording]
        Recorder[Recorder queue and worker]
        Redact[Capture-time redaction]
        Enrich[Trace enrichment]
        TraceRepo[TraceRepository]
    end

    subgraph Storage[Storage adapters]
        ServingRepo[ServingRepository and ExperimentRepository]
        SQLite[SQLite repository]
        Schema[Schema and migrations]
    end

    subgraph Offline[Analysis and offline work]
        Telemetry[Usage and pricing]
        Analysis[Reports campaigns and recommendations]
        Workflow[Workflow discovery and graphs]
        Jobs[Durable jobs and worker]
    end

    Client -->|HTTP request| App
    Config --> App
    Config --> Resolver
    App --> Resolver
    App --> Proxy
    Proxy -->|request bytes| Policy
    Policy --> Identify
    Policy --> Inject
    Policy --> Experiment
    Policy --> Route
    Policy --> Fallback
    Policy -->|decision or rewritten body| Proxy
    Proxy --> Budget
    Budget -. fallback action .-> Policy
    Proxy -->|forward and stream| Upstream[Configured upstream provider]
    Proxy -. mirrors selected inputs .-> Shadow
    Shadow -->|candidate trace| Recorder
    Proxy --> Redact
    Proxy -->|captured trace| Recorder
    Recorder --> Enrich
    Enrich --> Telemetry
    Recorder --> TraceRepo
    TraceRepo --> SQLite
    SQLite --> Schema
    Policy -. refreshes snapshots .-> ServingRepo
    ServingRepo --> SQLite
    Recorder -. persisted-trace observer .-> Budget
    ControlConfig[Git-backed desired state] -. reconciled transactionally .-> SQLite
    CLI --> Config
    CLI --> ControlConfig
    CLI --> Analysis
    CLI --> Workflow
    CLI --> Jobs
    CLI --> SQLite
    Console --> SQLite
    Analysis --> SQLite
    Workflow --> SQLite
    Jobs --> SQLite
    Telemetry --> Analysis
```

Solid arrows are ordinary request, data, and control flow. The dashed paths
are asynchronous snapshot refresh, off-response-path mirroring,
persisted-accounting observation, control-state reconciliation, and budget
fallback control; none introduces a database read while deciding a request.

Note the direction of the storage edges: the request path depends on the
repository contracts in `recorder/repositories.py`, never on the SQLite
adapter, and nothing under `recorder/` depends on `gateway/`.
`ServingRepository` has no mutators, so the request path cannot perform a
control-plane write even by mistake.

The order in which experiments, routes, fallbacks, and pass-through apply to
a call is listed in [configure.md](configure.md#serving-precedence).
Workflow and step routes match only a complete, validated workflow identity
([workflow-identity.md](workflow-identity.md)).

Cache-control injection composes with that decision and is gated by the
resolved API identity, not guessed from the URL. The proxy records the
client's original path and body plus the provider, its pricing policy, and the
model actually served, so historical re-enrichment stays honest. A named
mount affects transport only; the same body keeps the same use-case
fingerprint across providers. A decision may name another configured
provider. The proxy switches transport only when the candidate's API identity
equals the baseline's, strips client credentials whenever the provider
changes, and injects a provider-owned credential only while forwarding. An
unknown provider or an API mismatch terminates locally with
`ctrlrtn_provider_mismatch` and is recorded; there is no translation between
APIs.

## Code map

| Package | Owns | Key modules |
| --- | --- | --- |
| `gateway/` | The request path: ASGI lifecycle, `/healthz`, the local `/ctrlrtn/` endpoints, byte forwarding, per-request serving decisions, shadow mirroring, request injection, and forward-time credential stripping | `app.py`, `proxy/handler.py`, `proxy/headers.py`, `proxy/recording.py`, `proxy/models.py`, `serving.py`, `decision.py`, `shadow.py`, `inject.py`, `redact.py` |
| `config/` | Runtime settings: immutable values and errors, scalar defaults and coercion, provider and budget blocks, YAML and environment sources, and their layering | `models.py`, `schema.py`, `providers.py`, `budgets.py`, `sources.py`, `loader.py` |
| `routing.py` | Resolving a client path to a provider identity, base URL, and provider-facing path; named mounts are stripped here | `routing.py` |
| `identify/` | The use-case key every experiment, route, and report shares: `tag:<name>` from `x-ctrlrtn-route`, else `fp:<digest>` over system prompt, tool schemas, and response format, with generic dates and times normalized away | `fingerprint.py` |
| `policy/` | Budget admission, the immutable experiment, route, fallback, and shadow primitives the request path consults, and the workflow-step scope they share | `budget.py`, `experiment.py`, `route.py`, `fallback.py`, `shadow.py`, `scope.py` |
| `control_config/` | Git-backed desired state: immutable documents and revision provenance, YAML validation, canonical rendering and active-versus-desired diffs, and proof of a clean tracked revision | `models.py`, `parser.py`, `rendering.py`, `provenance.py` |
| `control/` | Route and experiment-adoption planning shared by the CLI and the console; storage applies adoption as one transaction | `service.py` |
| `recorder/` | Off-path persistence: the queue and worker, the trace record, store result values, capture-time credential redaction, storage contracts named by capability, and the SQLite and in-memory stores | `recorder.py`, `trace.py`, `models.py`, `redaction.py`, `repositories.py`, `protocols.py`, `sqlite/`, `memory/` |
| `telemetry/` | Model, token, and cost data derived from recorded traffic, and the default price table | `enrich.py`, `usage.py`, `pricing.py`, `prices.toml` |
| `analysis/` | Recommendations, propagation checks, campaign summaries, and reports over recorded data; nothing here is on the request path | `recommend.py`, `propagation.py`, `campaign.py`, `report.py` |
| `eval/` | Pure evaluation and statistics code, plus the one live HTTP adapter | `ni.py`, `tripwire.py`, `judge.py`, `calibration.py`, `replay.py`, `dataset_manifest.py`, `live.py` |
| `sdk/` | Client instrumentation: task, route, and workflow context, httpx request stamping, edition, step, and tool-operation context managers, event transport, and a late-bound reporting seam | `context.py`, `http.py`, `lifecycle.py`, `reporting.py`, `dispatch.py` |
| `workflow/` | Workflow identity and tool-operation facts, step metrics and per-task graphs, tool-link inference, family discovery and its durable jobs, proposals, and advisory recommendations; all of it analysis only | `identity.py`, `tool_operation.py`, `metrics.py`, `graph.py`, `step_detail.py`, `catalog.py`, `inference.py`, `discovery/`, `discovery_job/`, `discovery_projection.py`, `proposal.py`, `recommend.py`, `tool_batching.py` |
| `jobs/` | Durable background execution: job state, cooperative progress and cancellation, the claim-and-heartbeat worker, and offline replay as one injected handler | `models.py`, `context.py`, `worker.py`, `replay.py` |
| `cli/` | The terminal edge: the composition root, parser assembly by command family, one thin adapter per family, shared rendering, and the Textual console | `commands.py`, `parser.py`, `arguments/`, `runtime.py`, `operations.py`, `reporting.py`, `dataset.py`, `evaluation/`, `control.py`, `workflow/`, `render.py`, `console.py`, `tui/` |

`recorder/repositories.py` names storage contracts by capability
(`ServingRepository`, `ShadowRepository`, `ReportingRepository`,
`TraceRepository`, `ExperimentRepository`), and `protocols.py` composes the
whole-store contracts from them.

The SQLite store, `recorder/sqlite/store.py`, is composed from capability
mixins (traces, workflow, jobs, control, maintenance, reporting) over a typed
base, `recorder/sqlite/capability.py`. The base declares the shared
connection attributes and the abstract methods the mixins call on `self`;
each abstract method is implemented by exactly one mixin, so a composition
that leaves one out fails at class creation and mypy reports it. The
in-memory store, `recorder/memory_store.py`, mirrors that shape:
`recorder/memory/core.py` declares the shared state and the mixins under
`recorder/memory/` implement each capability. `recorder/sqlite/schema.py`
owns tables, indexes, triggers, and in-place column upgrades; `queries.py`
and `results.py` hold shared SQL and result values. Module boundaries never
split a transaction: atomic multi-table operations run as one method on the
shared connection.

Domain decisions stay below the CLI adapters.

## Dependency rules

1. The request path must not perform database or network control-plane
   reads while deciding how to serve a request. Serving reads in-memory
   snapshots.
2. Domain modules do not import the CLI, console, Starlette app, or SQLite.
3. Code that only needs stored values or a persistence interface imports
   `recorder.models` or `recorder.repositories`, not the SQLite
   implementation. Ask for the narrowest contract that fits:
   `ServingRepository` declares no mutators, so a hot-path component cannot
   perform a control-plane write.
4. Package dependencies run one way: `gateway/` and `cli/` may depend on
   `policy/`, `recorder/`, `telemetry/`, and `workflow/`; none of those may
   depend on `gateway/` or `cli/`. Storage never imports the HTTP edge. When
   a helper is needed by both, it belongs to whichever side owns the
   guarantee, which is why capture-time redaction lives under `recorder/`
   and forward-time credential stripping under `gateway/`.
5. Provider HTTP calls used by evaluations stay in `eval/live.py`;
   statistical functions accept injected callables and remain
   offline-testable.
6. Resources are owned explicitly. The application closes clients and stores
   it creates and leaves injected resources to their caller.
7. Provider-specific request mutations are selected by the resolved API
   identity. A coincidentally similar path is not sufficient.
8. Provider pricing policy is captured on the trace at request time.
   Historical re-enrichment must not depend on the proxy's current
   configuration.
9. Cross-provider serving is allowed only between equal, explicit API
   identities. Supporting different APIs requires a separately designed
   translation boundary and is not inferred from model names.

## Persistence

SQLite is the durable source for traces, outcomes, workflow lifecycle and
tool-operation events, inferred edges, experiments, routes, workflow routes
and definitions, shadow experiments and their counters, approved fallbacks,
durable jobs, discovery invalidations, and the active routing-config
revision. One connection is protected by a lock, and blocking operations run
outside the event loop. Schema changes are additive, so an existing database
opens in place. Workflow identity is extracted off-path during enrichment
into dedicated trace columns; a partial or malformed header set is stored as
a diagnostic, not as identity. A successful fallback call carries a separate
trace fact; it is a forwarded call, not a terminal rejection.

The console's monitoring connection opens the database with SQLite's
read-only URI. It cannot create the file, run DDL, or take a write lock, and
it still sees every WAL commit from the live proxy; a confirmed action opens
a short-lived writer and leaves activation to the normal snapshot refresh.
Every writable process holds a shared maintenance lock next to the database
file, and `prune --compact` needs its exclusive form
([deploy.md](deploy.md)).

Git-backed control state is reconciled in one transaction that records
repository HEAD, relative path, file digest, and activation time. Serving
matches only explicit validated headers, records the winning rule and
revision on every routed trace, and never consults observed or inferred
projections.

Everything `workflow/` derives is a projection recomputed from retained rows
at query time, with two persisted exceptions: the inferred-edge table that
`workflow infer` replaces, and the frozen inputs and checksummed results of
discovery jobs in the job ledger. No data-plane module reads either.
[workflow-discovery.md](workflow-discovery.md) describes what they hold and
how pruning and erasure treat them.

### Budget admission

Budget admission reads an in-memory snapshot: UTC-day global and use-case
totals plus lifetime totals and unknown-cost state per session. SQLite seeds
it at startup and the recorder updates it only after a trace is persisted, so
no database I/O happens on the request path. Optional in-flight reservations
are owned by the same gate and settled only after enriched persistence. A
budget rejection is an ordinary non-blocking synthetic trace, inside or
outside an experiment; when the recorder queue is saturated, dropped
telemetry is preferred over a delayed client response.

A budget fallback is a durable approval derived only from a `NON_INFERIOR`
replay artifact. Admission emits a fallback action at the explicit lower
use-case threshold; serving applies it only to the evaluated
baseline model and never over an experiment or a route. The hard ceiling is
checked again after the rewrite and remains a shutoff. The `budget` command
and the console share one renderer and the same read-only queries; both
report persisted accounting only, so process-local reservations stay outside
that cross-process view.

## Deliberate limits

- The recommended deployment is one process on the same host as the client.
- Live per-task counters and ceilings are process-local; durable measurement
  comes from recorded traces.
- Spend ceilings are eventually consistent and can overshoot by work already
  in flight. Opt-in reservations close the same-process race but are not a
  shared or exact provider billing ledger.
- Session enforcement loads every persisted session ID into process memory
  and assumes IDs are unique and short-lived. It is not an unbounded
  multi-tenant ledger.
- Requests and streamed responses are held in memory for full recording.
  Capture is not bounded and is not spooled to disk.
- There is no translation between the Anthropic and OpenAI APIs.
- The proxy observes and routes; it never reorders, merges, retries, or
  parallelizes application steps
  ([workflow-identity.md](workflow-identity.md)).
