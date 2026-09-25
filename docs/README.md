# Documentation

The [README](../README.md) is the front door. These pages go deeper, in the
order a new user needs them.

## Guides

- [Getting started](getting-started.md): start the proxy, point an
  application at it, name the work, report outcomes, read the numbers.
- [Run an experiment](run-an-experiment.md): offline replay, calibration,
  shadow, live A/B, adoption and rollback, Git-backed routing.
- [Instrument a workflow](instrument-a-workflow.md): the Python SDK, task
  identity, steps and tools.
- [The campaign](campaign.md): the whole loop scripted end to end,
  producing the README's table and chart.

## Reference

- [Evaluation](evaluation.md): how a verdict is reached and what it does
  not claim.
- [Console](console.md): the live terminal view, every key, every action.
- [Configure](configure.md): every setting, named providers, budgets, the
  price table, `routing.yaml`.
- [Deploy](deploy.md): the security model, topologies, Docker, day-two
  operations.
- [Architecture](architecture.md): request flow, code map, dependency
  rules, persistence.
- [Workflow identity](workflow-identity.md): the header and event contract
  the SDK implements.
- [Workflow discovery and analysis](workflow-discovery.md): what the proxy
  derives from recorded traffic, and every `workflow` command.
- [Operational benchmark](router-operational-benchmark.md): measured
  overhead on the request path, and how to reproduce it.

## Planning

- [Roadmap](roadmap.md), and the task files under [`tasks/open`](../tasks/open/).

## Generated

- [campaign-report.md](campaign-report.md) and
  [campaign-chart.svg](campaign-chart.svg): the output of `campaign-report`
  on the recorded workload in the README.
