# ctrlrtn

[![ci](https://github.com/arnovich/ctrlrtn/actions/workflows/ci.yml/badge.svg)](https://github.com/arnovich/ctrlrtn/actions/workflows/ci.yml)

Alpha, unreleased. Python 3.12 or newer. MIT.

A self-hosted proxy for the Anthropic and OpenAI APIs that records every call
your agents make, shows what each role costs, tests whether a cheaper model
is good enough for that role, and switches only when the evidence says so.

Point an application at it instead of the provider. Nothing else changes: the
proxy forwards the request byte for byte, streams the response back, and keeps
a copy of the exchange in a local SQLite file. From that recording it answers
three questions per use-case: what does this cost, would a cheaper model do
as well, and did switching pay off. A gateway routes and an observability
tool records; this one records in order to run the cheaper-model experiment
for you, and refuses the switch when the evidence is thin.

It is built for agentic applications, where one job fans out into many calls
from different roles and the right model differs per role. The router never
switches by itself. It recommends, you decide.

## Install

Python 3.12 or newer. Not on PyPI yet, so install from the repository:

```bash
git clone https://github.com/arnovich/ctrlrtn && cd ctrlrtn
uv sync --extra tui            # or: pip install -e ".[tui]"
uv run ctrlrtn serve           # listens on http://127.0.0.1:4000
```

Commands below are shown as `uv run ctrlrtn`; with a pip install, drop the
`uv run`.

## First numbers

Point your application at the proxy. Credentials stay in the application; the
proxy forwards them on each call and never persists them.

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export OPENAI_BASE_URL=http://127.0.0.1:4000/v1
```

Add three headers to each LLM request. They are optional, but everything
downstream gets sharper with them:

| Header | Meaning |
| --- | --- |
| `x-ctrlrtn-task` | One id per job, shared by every call the job makes. Evaluations cluster by it. |
| `x-ctrlrtn-route` | A stable name per role, such as `editor`. Becomes the use-case key `tag:editor`. |
| `x-ctrlrtn-session` | An operator-defined spend boundary, such as a customer or a batch. |

Without a route header, calls are grouped by a prompt fingerprint instead
(`fp:...`). The Python SDK sets the task and route headers for you; the
session header is yours to add. See
[Instrument a workflow](docs/instrument-a-workflow.md). Run your application
once, then look:

```bash
uv run ctrlrtn usecases          # spend per use-case
uv run ctrlrtn recommendations   # where a cheaper model is worth testing
uv run ctrlrtn console           # live view of all of it
```

`recommendations` names a use-case only when the bundled price table knows a
cheaper model of the same family. [Getting started](docs/getting-started.md)
walks through this with a real application.

## The loop: evaluate, A/B, switch

**Evaluate offline.** Replay a use-case's recorded inputs through the incumbent
and the candidate, score each pair with a blinded judge, and run a paired
non-inferiority test. No production traffic is touched. It bills your key, so
it prints the plan first and only spends with `--yes`:

```bash
uv run ctrlrtn replay-eval tag:editor claude-haiku-4-5 --margin 1.0
```

**Confirm live.** A shadow mirrors a sample of live inputs to the candidate
without serving its output, so you can check cost and failure rate with no
user exposure. A live split serves the candidate to a share of tasks and
checks for gross regression against the outcomes your application reports:

```bash
uv run ctrlrtn shadow start tag:editor claude-haiku-4-5 --sample 10
uv run ctrlrtn experiment start tag:editor claude-haiku-4-5 --split 50
uv run ctrlrtn experiment status <id>
```

**Switch.** A route serves the candidate to all of the use-case's traffic and
tracks the realized saving. Clearing it is the rollback:

```bash
uv run ctrlrtn route adopt <experiment-id>
uv run ctrlrtn route list
uv run ctrlrtn route clear tag:editor
```

Routes and running experiments can also be declared in a committed
`routing.yaml`, so switches have a Git history. [Run an experiment](docs/run-an-experiment.md)
covers each step, and [Evaluation](docs/evaluation.md) explains why the
verdicts can be trusted and what they do not claim.

## A real run

A multi-agent newspaper built on [Hugin](https://github.com/arnovich/gimle-hugin),
more than 20 editions of real traffic recorded, each Sonnet role replayed on
Haiku and judged blind against the source data. The table and chart are the
output of `campaign-report`:

![Recorded workload repriced at the candidate](docs/campaign-chart.svg)

| role | calls | recorded cost | at candidate prices | saving | replay verdict | live A/B |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| tag:financial_journalist | 452 | $20.00 | $6.67 | +67% | ✗ worse | — |
| tag:technical_analyst | 231 | $1.67 | - | - | — not evaluated | — |
| tag:editor | 105 | $1.57 | $0.52 | +67% | ✓ non-inferior | — |

The editor switches: non-inferior within the one-point margin, with a mean
difference of +0.07 points and a lower bound of −0.09. The journalist stays
on Sonnet: a mean difference of −1.18 with a lower bound of −1.75, well past
the margin. The analyst was not evaluated because it already ran on Haiku,
and no live A/B was run for this table. The tempting saving on the most
expensive role is exactly the switch the proxy exists to refuse. The saving
that survives is $1.04 of $23.23, or 4%. The verdict is per role, and it
differs.

Forty pairings per role, blinded position-swapped judging, a 1.0-point
margin at 95% one-sided confidence; repricing assumes the same token mix, an
upper bound. Reproduce it on your own workload with
[The cost-saving campaign](docs/campaign.md).

## What it does not do

- It never switches a model on its own. Every route is an explicit action.
- It does not fail open. If the proxy is down, the application's calls fail
  with a connection error; nothing bypasses to the provider.
- Its replay and judge calls use the Anthropic API, so a replay verdict needs
  an Anthropic key even when the application uses another provider. Recording,
  shadowing and live A/B work with any supported provider.
- It never translates between provider APIs. A candidate on another provider
  must speak the same API shape, for example OpenAI to an OpenAI-compatible
  local server.
- It never persists credentials. They are redacted at capture time, before
  anything reaches the database.
- It has no authentication on its control plane. Bind it to localhost or a
  private network, never the public internet. [Deploy](docs/deploy.md)
  explains the security model first.

## Documentation

- [Getting started](docs/getting-started.md): route a call, read the numbers.
- [Run an experiment](docs/run-an-experiment.md): offline replay, shadow, live
  A/B, adoption, rollback, Git-backed routing.
- [Instrument a workflow](docs/instrument-a-workflow.md): the SDK, workflow
  identity, step-scoped evidence, discovery and diagrams.
- [Evaluation](docs/evaluation.md): how a verdict is reached.
- [Console](docs/console.md), [Configuration](docs/configure.md),
  [Deployment](docs/deploy.md), [Architecture](docs/architecture.md),
  [Workflow discovery](docs/workflow-discovery.md).
- [Roadmap](docs/roadmap.md) and the open tasks under `tasks/open/`.

## Develop

```bash
uv sync --extra test --extra tui
uv run pre-commit install
uv run pytest -q                 # under a minute, offline, no API keys
```

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed.
