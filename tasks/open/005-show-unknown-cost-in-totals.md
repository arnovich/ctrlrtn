---
title: Mark unknown-priced calls in per-use-case and per-task totals
state: open
priority: low
labels: [reporting, pricing, cli]
---

# Mark unknown-priced calls in per-use-case and per-task totals

## Context

A call on a model the price table does not know is recorded with an unknown
cost. `calls` shows it as `-` and `spend` counts unknown-priced calls, but
`usecases` and `tasks` sum the known costs and print the result without a
marker, so a use-case with unpriced calls reads as cheaper than it is. The
console's use-case and task tables have the same gap.

## Outcome

- `usecases` and `tasks` show, per row, how many calls had no price, or mark
  the total as partial, in the CLI and in the console.
- A test records one priced and one unpriced call for a use-case and asserts
  the rendering shows the gap.
