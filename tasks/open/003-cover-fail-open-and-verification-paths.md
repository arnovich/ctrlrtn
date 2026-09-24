---
title: Test the fail-open and verification paths that only comments promise
state: open
priority: medium
labels: [quality, tests, shadow, evaluation]
---

# Test the fail-open and verification paths that only comments promise

## Context

The suite sits just above the 85 % branch-coverage floor, and the least
covered code is exactly the code that protects users when something goes
wrong:

- `gateway/shadow.py`: the provider-mismatch drop, the full-queue drop, the
  "a shadow failure never reaches the user" exception swallow, the
  recorder-refused accounting, and the refresh-loop error path are asserted
  by comments, not tests.
- `eval/dataset_manifest.py`: most of `verify_dataset_manifest`'s rejection
  branches (wrong version or kind, bad partition, tampered digest) are
  untested, yet the replay worker trusts that check before spending money.
- `workflow/discovery_job/planning.py` and `handler.py`: the frozen-input
  integrity guards, including the digest check, have no tests.
- `jobs/replay.py`: the argument guards in `prepare_replay_job` and the
  missing-API-key path are untested.

## Outcome

- Each listed branch has a test that forces it and asserts the observable
  result: the live response is unaffected by a shadow failure, a tampered
  manifest is refused with its reason, a replay plan with bad arguments
  fails before any provider call.
- Branch coverage is at or above 88 % so a single removed test cannot cross
  the floor.
