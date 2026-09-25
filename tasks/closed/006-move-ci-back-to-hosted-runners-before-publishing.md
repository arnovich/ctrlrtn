---
title: Move CI back to GitHub-hosted runners before the repository goes public
state: closed
priority: high
labels: [ci, security, release]
---

# Move CI back to GitHub-hosted runners before the repository goes public

## Context

While the repository is private, GitHub-hosted runners are not available to
this account, so `.github/workflows/ci.yml` and `nightly-hardening.yml`
run on the shared self-hosted runner (`runs-on: [self-hosted, Linux, X64,
hetzner, shared-ci]`). A self-hosted runner on a public repository executes
workflow code from forks, so this must not survive the switch to public.

## Outcome

- Every `runs-on` in both workflows is `ubuntu-latest` again and the
  `timeout-minutes` values are back to what hosted runners need.
- The self-hosted runner registration for this repository is removed.
- One CI run on hosted runners is green for the commit that makes the
  repository public.

## Notes

Closed 2026-09-25. PR #6 put every `runs-on` on `ubuntu-latest` with
10/20/10-minute timeouts; the runner registration was deleted from the
repository and the runner removed on its host; the repository went public
the same day and run 36113802848 on `main` was green on hosted runners
(quality and package in 13 s, the test matrix in about two minutes).
