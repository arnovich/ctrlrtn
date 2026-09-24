# Documentation

The top-level [README](../README.md) is the front door. These pages go
deeper, in the order a new user needs them.

## Guides

- [Getting started](getting-started.md): start the proxy, point an
  application at it, name the work with three headers, report outcomes,
  read the per-role numbers, keep the recording bounded.
- [Run an experiment](run-an-experiment.md): offline replay, judge
  calibration, shadow, live A/B, adoption and rollback, candidates on
  another provider, step scope, Git-backed routing, budget fallbacks.
- [Instrument a workflow](instrument-a-workflow.md): the Python SDK, task
  identity, declaring steps and tools, what step-level evidence unlocks.
- [The campaign](campaign.md): the whole loop scripted end to end on a
  multi-agent application, producing the README's table and chart.

## Reference

- [Evaluation](evaluation.md): how a verdict is reached and what it does not
  claim.
- [Console](console.md): the live terminal view, every key, and the actions
  it can take after confirmation.
- [Configure](configure.md): every setting, named providers, budgets, the
  price table, and the `routing.yaml` schema.
- [Deploy](deploy.md): the security model first, then same-box and
  dedicated-box topologies, Docker, and day-two operations.
- [Architecture](architecture.md): request flow, code map, dependency rules,
  persistence.
- [Workflow identity](workflow-identity.md): the header and event contract
  the SDK implements.
- [Operational benchmark](router-operational-benchmark.md): measured
  overhead of the proxy on the request path, and how to reproduce it.

## Planning

- [Roadmap](roadmap.md), and the task files under [`tasks/open`](../tasks/open/).
