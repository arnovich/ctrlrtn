# Router operational benchmark

`scripts/router_operational_benchmark.py` provides a bounded, comparative local
measurement of ctrlrtn's HTTP data path. It starts a deterministic upstream plus
unrecorded and SQLite-recorded router instances, each in its own process, and
measures the same fixed responses directly and through both router
configurations.

Run it from the repository root:

```console
uv run python scripts/router_operational_benchmark.py \
  --requests 50 \
  --concurrency 10 \
  --output router-benchmark.json
```

The output file must not already exist, and the harness refuses to write one
from a checkout with uncommitted changes unless `--allow-dirty` is passed, so a
recorded `router_revision` always describes the code that produced it. The
harness is bounded to 10,000 requests, concurrency 256, a 16 MiB response, and
2 GiB of response data per scenario. Defaults are intentionally much smaller.

Exit status is 0 only when every correctness probe passed; 1 when a probe
failed; 2 when the run could not be completed.

## What it records

For fixed 1 KiB, 1 MiB, and 1 MiB streamed responses, the JSON contains:

- direct, unrecorded-router, and recorded-router throughput;
- nearest-rank p50, p95, and p99 request latency;
- router latency overhead and throughput ratios against the direct path;
- response sizes and total transferred bytes;
- each server process's own peak RSS (`VmHWM`; null off Linux);
- Python, OS, machine, CPU-count, router revision, worktree cleanliness, and
  the run configuration.

The harness also verifies that an upstream `503` passes through, that one
injected recorder-write failure fails neither client request, that every
expected trace is recorded and drained, and that all servers stop under their
own control. A run whose requests error, or whose recorded-trace count does not
match, is rejected rather than reported.

## Report schema

`schema_version` is 2. Reports with `schema_version: 1` were produced by a
harness that measured itself and are not comparable; discard them and re-run.

## Interpretation limits

This is a development comparison, not a production capacity claim, and several
limits are load-bearing rather than boilerplate.

**The baseline is bounded by the load generator.** The client is a single
Python process and saturates roughly one core during every `direct` scenario,
while the upstream it is measuring uses a fraction of one. Because the client is
comparatively idle on the routed path, that cost does not cancel in
`routed − direct`. Overhead is therefore understated, and no number here is a
statement about the router's capacity.

**The routed path gets more parallelism than the baseline.** Three processes
against two, on an unloaded multi-core host, so the router's CPU cost is partly
absorbed by spare cores. Co-locating them, or running on a loaded host, would
show more added latency than this reports.

**Overhead percentiles are differences of percentiles**, not percentiles of a
per-request difference, so `p95_overhead_ms` can legitimately come out below
`p50_overhead_ms`. Treat the overhead fields as indicative, not as tail
estimates.

**Sample sizes are small.** At the documented defaults the nearest-rank p99 is
close to the sample maximum, and run-to-run spread on the small-payload overhead
is comparable to the value itself. One run is an observation, not a
measurement; compare medians across several.

Loopback also removes real network latency, and every server still runs on one
host. Run comparisons on the same otherwise-idle host and keep the full JSON
rather than copying selected numbers.

No latency or throughput threshold is built into the harness. Declare those
against a representative deployment before using them as a release gate.

## Reference run

[`router-operational-benchmark-sample.json`](router-operational-benchmark-sample.json)
is one complete run on a 12-core x86_64 Linux host, 30 measured requests, three
warmups, concurrency 6, with every correctness probe passing. Because a single
run is not a measurement, the figures below are medians of five runs at that
configuration, for the recorded router:

| Scenario | p50 overhead | Throughput ratio |
| --- | --- | --- |
| 1 KiB | 2.0 ms (range 1.6–2.5) | 0.50 |
| 1 MiB buffered | 6.7 ms (range 6.3–8.2) | 0.44 |
| 1 MiB streamed | 10.2 ms (range 9.3–12.4) | 0.41 |

The throughput ratios deserve as much attention as the latencies: on loopback
the recording proxy roughly halves per-process request throughput.
