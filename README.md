# ctrlrtn

[![ci](https://github.com/arnovich/ctrlrtn/actions/workflows/ci.yml/badge.svg)](https://github.com/arnovich/ctrlrtn/actions/workflows/ci.yml)

Alpha, version 0.1.0. Python 3.12 or newer. MIT.

A self-hosted proxy for the Anthropic and OpenAI APIs. It records every call
your agents make, prices each role, and tests whether a cheaper model is good
enough for that role before you switch.

Point an application at it instead of the provider. Requests pass through
unchanged unless a route or experiment you declared applies, and every
exchange is recorded to a local SQLite file. From that recording it answers,
per use-case: what does this cost, would a cheaper model do as well, did
switching pay off.

The test is the point. The proxy replays a role's own recorded inputs on the
candidate, has a blinded judge score each pair, and runs a paired
non-inferiority test that gives no verdict on fewer than 20 independent
tasks. It never switches by itself.

## Install

Not on PyPI yet, so install from the repository:

```bash
git clone https://github.com/arnovich/ctrlrtn && cd ctrlrtn
uv sync --extra tui            # or: pip install -e ".[tui]"
uv run ctrlrtn serve           # listens on http://127.0.0.1:4000
```

With a pip install, drop the `uv run` from the commands below.

## First numbers

Point your application at the proxy. Credentials stay in the application and
are forwarded on each call, never stored.

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export OPENAI_BASE_URL=http://127.0.0.1:4000/v1
```

Three optional request headers sharpen everything downstream:

| Header | Meaning |
| --- | --- |
| `x-ctrlrtn-task` | One id per job, shared by every call the job makes. Evaluations cluster by it. |
| `x-ctrlrtn-route` | The role a call plays, such as `editor`. Becomes the use-case key `tag:editor`. |
| `x-ctrlrtn-session` | A spend boundary you define, such as a customer or a batch. |

Without a route header, calls are grouped by a prompt fingerprint (`fp:...`).
The Python SDK sets the task and route headers and reports outcomes
([Instrument a workflow](https://github.com/arnovich/ctrlrtn/blob/main/docs/instrument-a-workflow.md)).
Run your application once, then look:

```bash
uv run ctrlrtn usecases          # spend per use-case
uv run ctrlrtn recommendations   # where a cheaper model is worth testing
uv run ctrlrtn console           # live view of all of it
```

![The console: experiments, use-cases, models and tasks on the left, live graphs and the selected experiment's verdict on the right](https://raw.githubusercontent.com/arnovich/ctrlrtn/main/docs/console.png)

`recommendations` names a use-case only when the bundled price table knows a
cheaper model of the same family.
[Getting started](https://github.com/arnovich/ctrlrtn/blob/main/docs/getting-started.md)
walks through this with a real application.

## The loop: evaluate, A/B, switch

**Evaluate offline.** Replay a use-case's recorded inputs through the
incumbent and the candidate, judge each pair blind, and run a paired
non-inferiority test. Production traffic is untouched. The replay speaks the
Anthropic Messages API and bills your Anthropic key; it is a dry run without
`--yes`.

```bash
uv run ctrlrtn replay-eval tag:editor claude-haiku-4-5 --margin 1.0
```

**Confirm live.** A shadow mirrors a sample of live inputs to the candidate
without serving its output. A live split serves the candidate to a share of
tasks and checks for gross regression against the outcomes your application
reports.

```bash
uv run ctrlrtn shadow start tag:editor claude-haiku-4-5 --sample 10
uv run ctrlrtn experiment start tag:editor claude-haiku-4-5 --split 50
uv run ctrlrtn experiment status <id>
```

**Switch.** A route serves the candidate to all of the use-case's traffic and
tracks the realized saving. Clearing it is the rollback.

```bash
uv run ctrlrtn route adopt <experiment-id>
uv run ctrlrtn route list
uv run ctrlrtn route clear tag:editor
```

Routes and experiments can also be declared in a committed `routing.yaml`.
[Run an experiment](https://github.com/arnovich/ctrlrtn/blob/main/docs/run-an-experiment.md)
covers each step;
[Evaluation](https://github.com/arnovich/ctrlrtn/blob/main/docs/evaluation.md)
explains what a verdict claims and what it does not.

## A real replay

A multi-agent newspaper built on [Hugin](https://github.com/arnovich/gimle-hugin):
more than 20 editions recorded, each Sonnet role replayed on Haiku and judged
blind against the source data. The table and chart are the output of
`campaign-report`:

![Recorded workload repriced at the candidate](https://raw.githubusercontent.com/arnovich/ctrlrtn/main/docs/campaign-chart.svg)

| role | calls | recorded cost | at candidate prices | saving | replay verdict | live A/B |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| tag:financial_journalist | 452 | $20.00 | $6.67 | +67% | ✗ worse | — |
| tag:technical_analyst | 231 | $1.67 | - | - | — not evaluated | — |
| tag:editor | 105 | $1.57 | $0.52 | +67% | ✓ non-inferior | — |

The editor switches (mean difference +0.07 points, lower bound −0.09, within
the 1.0-point margin). The journalist stays on Sonnet (mean −1.18, lower
bound −1.75). The analyst already ran on Haiku. What survives is $1.04 of
$23.23: the biggest saving is the one the evidence rules out.

Forty pairings per role, blinded position-swapped judging by Claude Opus
(same vendor as both arms), 95% one-sided. Repricing assumes the same token
mix, so the saving is an upper bound. No live A/B was run, and the workload
is the author's own. Reproduce it with
[The cost-saving campaign](https://github.com/arnovich/ctrlrtn/blob/main/docs/campaign.md).

## Limits

- It never switches a model on its own.
- It does not fail open: if the proxy is down, calls fail with a connection
  error.
- Replay and judge calls go straight to the Anthropic API. A verdict needs an
  Anthropic key and a use-case recorded on the Messages API. Recording,
  shadows and live A/B work with any supported provider.
- It never translates between provider APIs.
- Credentials are redacted at capture time and never stored.
- It has no authentication. Anyone who can reach the port can send traffic
  and change routes, so bind it to localhost or a private network.
  [Deploy](https://github.com/arnovich/ctrlrtn/blob/main/docs/deploy.md)
  explains the security model.
- It is one Python process, a few milliseconds of overhead per call; see
  [the benchmark](https://github.com/arnovich/ctrlrtn/blob/main/docs/router-operational-benchmark.md).
- Prices come from a bundled table. An unknown model is recorded but not
  priced.

## Documentation

- [Getting started](https://github.com/arnovich/ctrlrtn/blob/main/docs/getting-started.md),
  [Run an experiment](https://github.com/arnovich/ctrlrtn/blob/main/docs/run-an-experiment.md),
  [Instrument a workflow](https://github.com/arnovich/ctrlrtn/blob/main/docs/instrument-a-workflow.md),
  [The cost-saving campaign](https://github.com/arnovich/ctrlrtn/blob/main/docs/campaign.md).
- [Evaluation](https://github.com/arnovich/ctrlrtn/blob/main/docs/evaluation.md),
  [Console](https://github.com/arnovich/ctrlrtn/blob/main/docs/console.md),
  [Configuration](https://github.com/arnovich/ctrlrtn/blob/main/docs/configure.md),
  [Deployment](https://github.com/arnovich/ctrlrtn/blob/main/docs/deploy.md),
  [Architecture](https://github.com/arnovich/ctrlrtn/blob/main/docs/architecture.md),
  [Workflow identity](https://github.com/arnovich/ctrlrtn/blob/main/docs/workflow-identity.md),
  [Workflow discovery](https://github.com/arnovich/ctrlrtn/blob/main/docs/workflow-discovery.md).
- [Roadmap](https://github.com/arnovich/ctrlrtn/blob/main/docs/roadmap.md)
  and [`tasks/open`](https://github.com/arnovich/ctrlrtn/tree/main/tasks/open).

## Develop

```bash
uv sync --extra test --extra tui
uv run pre-commit install
uv run pytest -q --fail-on-skip  # under a minute, offline, no API keys
```

See [CONTRIBUTING.md](https://github.com/arnovich/ctrlrtn/blob/main/CONTRIBUTING.md).
