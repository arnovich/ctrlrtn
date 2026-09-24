# Docs

A map of what lives here. The top-level `README.md` is the front door; these are
the deeper guides. History docs are frozen snapshots kept for rationale — the
guides below them describe current behavior where the two differ.

## Operator guides — how it works and how to run it

- [architecture.md](architecture.md) — system overview: request flow, code map,
  dependency rules, persistence, and the deliberate limits.
- [configure.md](configure.md) — the `ctrlrtn.yaml` reference: daily
  budgets, named providers, provider-owned credentials, cross-provider
  experiments and routes, and the Ollama quick start.
- [deploy.md](deploy.md) — deployment: the security model first, then same-box
  and dedicated-box topologies, Docker, and operating notes.
- [router-operational-benchmark.md](router-operational-benchmark.md) — bounded
  direct-versus-router latency, throughput, per-server peak RSS, and
  failure probes.
- [campaign.md](campaign.md) — the end-to-end worked example: record → paired
  replay verdict → live A/B → report → adopt. Doubles as the full-pipeline
  integration test.

## Protocol and subsystem references

- [workflow-identity.md](workflow-identity.md) — how a request declares which
  workflow, version, and step it belongs to.

## Generated artifact

- [campaign-report.md](campaign-report.md) + [campaign-chart.svg](campaign-chart.svg)
  — sample output of `campaign-report`, embedded in the top-level README.
  Regenerate on your own workload with the steps in [campaign.md](campaign.md).
