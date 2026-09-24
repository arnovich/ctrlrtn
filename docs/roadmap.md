# Roadmap

Planned work is tracked as task files under [`tasks/open/`](../tasks/open/),
one per concern, each with the context behind it and a checkable outcome.
This page is the short list of directions; the task files are the plan.

**Ready to pick up**

- Measure router capacity, not just loopback latency, in the operational
  benchmark (task 001).
- Type-check the CLI and console, the one package mypy does not cover yet
  (task 002).
- Test the fail-open paths in the shadow mirror, manifest verification and
  the discovery-job guards (task 003).
- Enforce or drop the per-task cost ceiling on experiments (task 004).

**Directions without a task yet**

- A PyPI release, so `pip install ctrlrtn` works.
- Provider coverage beyond the Anthropic and OpenAI wire shapes: Azure
  OpenAI, Bedrock, Vertex, and native Ollama.
- An OpenTelemetry bridge that preserves the explicit-versus-inferred
  distinction the workflow model keeps.
- Authenticated application identity and namespaces for a router shared by
  several applications.
- A second reference workload from an unrelated agent framework.

To propose something, open an issue first; a task file follows once the
outcome is agreed.
