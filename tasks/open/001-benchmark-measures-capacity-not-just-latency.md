---
title: Make the operational benchmark measure router capacity, not just loopback latency
state: open
priority: medium
labels: [benchmark, measurement, tooling]
---

# Make the operational benchmark measure router capacity, not just loopback latency

## Context

Schema version 2 (`97ba2ba`) fixed three defects that made the harness measure
itself: no `TCP_NODELAY` on its listeners, every server sharing one interpreter,
and `tracemalloc` inside the timed region. A four-role panel review of that
commit confirmed the fixes but found that the corrected numbers are still soft,
for reasons the harness now documents in `docs/router-operational-benchmark.md`
under "Interpretation limits" but does not address.

The findings, with what was measured:

- **The baseline is bounded by the load generator.** The single-threaded httpx
  client burns 0.97–0.98 of a core during every `direct` scenario, while the
  upstream it is measuring uses 0.16–0.41. On the routed path the client drops
  to 0.55–0.69, so the client term does not cancel in `routed − direct`.
  Overhead is understated and router capacity is never measured. This is the
  same class of defect as the GIL sharing that version 2 removed — narrowed to
  one process rather than eliminated.
- **The routed path gets more parallelism than the baseline.** Three processes
  against two, consuming 1.68–2.21 concurrent cores versus 1.14–1.39. In the
  1 MiB recorded scenario the router used roughly 1.28 cores across the window
  — about 2.5 ms of router CPU per request against the upstream's 0.125 ms —
  to add ~7 ms of latency. Co-located or on a loaded host, added latency would
  converge on added CPU.
- **Run-to-run spread is 6–54% and is never reported.** One run is published as
  the reference; the docs work around this by quoting medians of five runs, but
  the harness has no `--repeat`.
- **n=30 biases the recorded path low by 10–16%.** The recorder is a background
  queue, and at n=30 the window is short enough that the writer's work lands
  after the measurement. At n=400 the same overheads rise. The unrecorded path
  moves the other way (−7 to −14%), so the two biases have opposite signs.
- **At n=30 the p99 label is the sample maximum** and p95 the second-largest,
  and they underestimate the real tail by about 2x against n=400.
- **The recorder's drain of scenario k runs inside scenario k+1's baseline.**
  Scenario order is fixed and there is no quiesce; `_await_traces` is called
  once, at the end. Reordering shifted every recorded overhead by 0.12–0.52 ms,
  which is inside the run-to-run noise band — meaning the harness cannot
  currently distinguish an ordering artefact from router cost.
- **`throughput_ratio` is not a capacity measurement.** At fixed closed-loop
  concurrency it is approximately an inverted mean-latency ratio, its
  denominator is the client-limited direct throughput, and the committed sample
  derives one of them from a 9.9 ms window.
- **Host state is neither controlled nor recorded**: no CPU affinity, load
  average, governor, SMT topology or cgroup limits, and nothing is pinned.
- **The committed evidence config (30/6/3) is below the harness's own
  documented defaults (50/10/5).**

Two portability items belong here rather than with the fixes already made:
`stopped_cleanly` infers a graceful stop from `-SIGTERM`, which is uvicorn's
signal-restoration behaviour and is wrong-shaped on Windows, where
`terminate()` yields exit code 1; a sentinel written by the child before exit
would be vendor-independent and portable. And the hidden `--role` flag is
reachable from a shell, so it can start a real recording gateway against an
arbitrary database path.

## Outcome

The harness reports a figure that transfers off an idle 12-core developer box,
and its published evidence carries dispersion rather than a single draw.
Specifically:

- Per-role CPU seconds per scenario (`utime+stime` deltas) and a derived
  `router_cpu_ms_per_request` appear in the report. This number is
  contention-independent and is the honest measure of router cost.
- The load generator no longer bounds the baseline: either several client
  processes or an external generator. The report carries client CPU per wall
  second, and a run where it exceeds roughly 0.7 is marked invalid.
- `--repeat N` exists, the report carries median plus spread per comparison,
  and values are rounded to the noise floor rather than to three significant
  figures.
- Committed evidence is generated at or above the documented defaults, with the
  recorder quiesced between scenarios and scenario order counterbalanced across
  repeats.
- `throughput_ratio` is either removed or replaced by a saturation measurement
  with the client proven not to be the limiter.
- `environment` records CPU affinity, load average and scaling governor, and a
  run started on a loaded host says so.
- `clean_shutdown` is derived from a sentinel the child writes, not from the
  exit code's sign, and the module imports on Windows.

Done when `docs/router-operational-benchmark.md` no longer needs the
"Interpretation limits" caveats about the client-bound baseline and the unequal
process counts, because the report measures around them.
