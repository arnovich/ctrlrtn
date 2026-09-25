# Changelog

## Unreleased

### Breaking

- The SDK's run context manager is `ctrlrtn.sdk.task()` and its handle is
  `Task`; `edition()` and `Edition` are gone. Nothing else about the SDK
  changed. The name now matches the `x-ctrlrtn-task` header and the unit the
  evaluation clusters on.

## 0.1.0 — 2026-09-24

First public release. The proxy was developed privately from June 2026 as
an internal router; its history was squashed at the public rename.

- Drop-in proxy for the Anthropic and OpenAI wire formats with named
  upstream providers, capture-time credential redaction, and SQLite recording.
- Per-use-case spend, task and session reporting from the CLI and a live
  terminal console.
- Offline paired replay evaluation with a blinded judge and a non-inferiority
  test, judge calibration against human labels, online shadow runs, and live
  A/B splits judged by application-reported outcomes.
- Persistent routes adopted from experiment verdicts, budgets with
  evidence-approved fallbacks, and Git-backed routing configuration.
- Workflow identity headers, an instrumentation SDK, passive workflow
  discovery, per-task graphs, and step-scoped experiments.
