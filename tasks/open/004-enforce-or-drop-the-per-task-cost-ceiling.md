---
title: Enforce or drop the per-task cost ceiling on experiments
state: open
priority: low
labels: [experiments, budget, cli]
---

# Enforce or drop the per-task cost ceiling on experiments

## Context

`experiment start --max-cost` records `max_cost_usd_per_task` with the
experiment and the CLI and console show it beside the enforced
`max_calls_per_task` ceiling, but nothing reads it: cost is only known after
the response, during enrichment, and the pre-request hook cannot price a
call. An option that is shown as a ceiling and never enforced misleads.

Enforcing it needs a per-task running cost in the budget snapshot (the
recorder already updates per-session and per-use-case totals after
persistence) and a serving check that cuts the candidate over the cap, the
same way the call ceiling does.

## Outcome

Either:

- the candidate arm is cut off with a counted failure once a task's known
  spend on it exceeds `max_cost_usd_per_task`, with a test that forces it,
  and the help text no longer says "not enforced"; or
- the option, the column and the display are removed, with a schema
  migration that tolerates existing databases.
