# Instrument a workflow: the SDK, task identity and step-level evidence

The proxy works on plain HTTP headers, so any client in any language can
be instrumented. The Python SDK, `ctrlrtn.sdk`, sets those headers for you,
reports outcomes, and can describe an agentic workflow step by step so that
evidence and routing can be scoped to a single step.

## Headers by hand

Three headers do most of the work; [getting started](getting-started.md)
introduces them. `x-ctrlrtn-task` is one id per job, `x-ctrlrtn-route` is
the role, `x-ctrlrtn-session` is a spend boundary. Any client that can add
request headers can send them. When a job ends, POST its result:

```json
POST /ctrlrtn/outcome
content-type: application/json

{"task_id": "run-42", "success": true, "score": 0.9}
```

The proxy removes every `x-ctrlrtn-*` header before forwarding, so a
provider never sees them.

## The SDK for Python applications

The SDK ships with the package and depends only on `httpx`. An edition is
one job; every request made through an SDK-wrapped client inside the block
carries its task id, and sub-agents in the same process and async context
inherit it:

```python
from openai import OpenAI
from ctrlrtn import sdk

client = OpenAI(
    base_url="http://127.0.0.1:4000/v1",
    http_client=sdk.http_client(),
)

with sdk.edition(report_to="http://127.0.0.1:4000") as run:
    with sdk.route("researcher"):
        notes = research(client)
    with sdk.route("editor"):
        article = edit(client, notes)
    run.report(success=article is not None, score=quality(article))
```

`sdk.route(...)` sets the use-case for the calls inside it, one per role.
`run.report(...)` posts the outcome; if the block exits with an exception the
edition reports failure by itself. Without `report_to` nothing is sent.

Provider SDKs built on `httpx2`, such as the Anthropic SDK from version 1,
reject an `httpx.Client`. Give them their own client with the SDK's hooks:

```python
import httpx2
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:4000",
    http_client=httpx2.Client(event_hooks=sdk.event_hooks()),
)
```

For a client the SDK cannot wrap, stamp the headers yourself:

```python
headers = sdk.stamp({})          # task, route and step identity, if any
```

Context does not cross threads or processes on its own. Wrap a callable with
`sdk.bind(func)` before handing it to a thread pool. With `CTRLRTN_STRICT=1`
an unbound request made while an edition is active raises instead of being
recorded as a stray call.

## Describe the workflow

An agentic application is a graph of steps. Declaring that graph lets the
proxy attribute cost, latency and outcomes to a stable step rather than to a
prompt fingerprint, and lets experiments and routes target one step of one
workflow version. Name the workflow and its version on the edition, and open
each logical operation as a step:

```python
with sdk.edition(
    workflow="article-pipeline",
    workflow_version="git:8d23f1a",
    report_to="http://127.0.0.1:4000",
) as run:
    with run.step("research") as research:
        sources = collect_sources(client)
        research.report(success=bool(sources))

    with run.step("draft", dependencies=[research.step_run_id]) as draft:
        with draft.tool("search", operation_id="q-1", effect="idempotent"):
            hits = search(sources)
        text = write(client, hits)
        draft.report(success=bool(text))
```

A step's outcome comes only from its own `report(...)`; it is not inherited
from the edition's, and a step that leaves without one shows as having no
outcome in `workflow diagnostics`.

The stable step key is `(workflow, workflow_version, step)`; each `step(...)`
call is one run of it with a fresh run id. Retries and loop iterations are
new runs with a higher `attempt`. `dependencies` lists the runs whose
results a step consumes, and `parent_step_run_id` defaults to the enclosing
step, so fan-out and joins are represented without guessing from
timestamps. A tool block declares one tool attempt with its side-effect
class, so a replay can tell which operations were safe to repeat.

Every step emits lifecycle events, `started` and then exactly one of
`completed`, `failed`, `cancelled` or `skipped`, to `/ctrlrtn/workflow-events`.
Event delivery is best effort: a failure is logged and never changes what
the application does.

A subprocess or remote worker continues a step under the same identity
through a carrier:

```python
carrier = sdk.export_carrier()            # in the parent, inside a step
with sdk.import_carrier(carrier), sdk.route("researcher"):   # in the worker
    call_model(client)
```

The carrier holds the task and step identity, not the route, so the worker
sets its own role.

[Workflow identity](workflow-identity.md) is the full contract and
[Workflow discovery and analysis](workflow-discovery.md) covers every
`workflow` command: the header
names, the identifier grammar, size limits and what the proxy records.

## What instrumentation unlocks

Per task, the proxy can now show the graph it observed and the cost of each
step:

```bash
uv run ctrlrtn workflow steps                 # cost, latency, outcomes per step
uv run ctrlrtn workflow diagram <task-id>     # one task as a Mermaid flowchart
uv run ctrlrtn workflow flow article-pipeline git:8d23f1a   # paths as a Sankey
uv run ctrlrtn workflow catalog               # versions and what routes them
uv run ctrlrtn workflow diagnostics           # identity consistency problems
uv run ctrlrtn workflow recommendations       # where evidence suggests a change
```

Explicit dependencies and inferred links stay distinct in every view.
Recommendations are read-only observations; the proxy never reorders,
merges or skips application steps.

Experiments, shadows and routes accept a step scope:

```bash
uv run ctrlrtn replay-eval tag:editor claude-haiku-4-5 \
  --workflow article-pipeline --workflow-version git:8d23f1a --step draft
```

Step-scoped evidence is reported as evidence about that step only. In
`routing.yaml`, a `workflows` block declares step names and their allowed
shapes, and `workflow_routes` routes an exact workflow version or one of its
steps to a model. Step rules beat workflow rules, which beat use-case
routes.

## Without instrumentation

Traffic recorded before any workflow identity existed, called legacy traffic
in the discovery commands, can still be explored. `workflow discover`
clusters recurring task shapes into families from role and tool patterns
alone, and `workflow identify` writes a proposal for naming one family as a
workflow. Both are analysis only: a discovered family has no routing
authority until an application declares the workflow and sends its
identity.
