---
title: Make the operational benchmark measure router capacity, not just loopback latency
state: open
priority: medium
labels: [benchmark, measurement, tooling]
---

# Make the operational benchmark measure router capacity, not just loopback latency

## Context

The operational benchmark reports loopback latency overhead, but its numbers
are soft for reasons `docs/router-operational-benchmark.md` lists under
"Interpretation limits" and does not address: the single-threaded load
generator bounds the baseline, the routed path gets more parallelism than
the direct one, run-to-run spread is never reported, the small default
sample understates the recorded path and the tail, and `throughput_ratio`
is not a capacity measurement.

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
