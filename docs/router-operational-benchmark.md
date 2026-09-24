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

## What changed in schema version 2

Version 1 published numbers that were substantially the harness measuring
itself. Three defects, in decreasing order of measured effect:

**Every measured server now runs in its own OS process.** Version 1 ran the
client, the upstream and both routers in one interpreter, so they contended for
a single GIL, and the routed path paid that contention twice — once at the
router and once at the upstream behind it. This was the largest of the three.

**`TCP_NODELAY` is set on every listener.** `socket.socket(AF_INET,
SOCK_STREAM)` leaves `proto` at 0 and `accept()` inherits it, while asyncio only
auto-sets `TCP_NODELAY` on an accepted socket when `sock.proto ==
IPPROTO_TCP`. Version 1 used the two-argument form, so Nagle stayed on with no
error and no log line, and small responses on a kept-alive connection waited for
the peer's delayed ACK — about 40 ms on Linux and different on macOS. The stall
disappears once responses grow past roughly 64 KiB, so it presented as
payload-dependent router cost rather than as a socket option. Its main effect
was not a constant offset but *instability*: because the stall applied to both
the direct and the routed leg, how much of it cancelled in the subtraction
varied per run and per platform.

**Nothing is sampled inside a timed region.** Version 1 ran `tracemalloc` across
the measured window, costing several milliseconds of the latency it was
reporting. This was the smallest of the three.

These are not independent factors that multiply out; the interaction is what
made version 1's output unstable. The measured combined effect on one host is
in the reference run below.

Only the harness was ever affected. The shipped gateway passes a host and port
to `uvicorn.run` (`src/ctrlrtn/cli/runtime.py`), and uvicorn's
single-process path hands those to asyncio's `loop.create_server`, which
resolves the address and so builds a listener with `proto == IPPROTO_TCP`.
(uvicorn's own `Config.bind_socket()` does leave `proto` at 0, but it is reached
only for `--reload` or `--workers`, neither of which `ctrlrtn serve` uses.)

## Report schema

`schema_version` is 2. Version 1 was never part of a tagged release — the
harness landed after `v0.1.0` and 0.2.0 is unreleased — so this breaks only
reports generated from git. Version 1 reports are not comparable with version 2
and should be re-run.

| Field | Change |
| --- | --- |
| `measurements[].python_peak_allocated_bytes` | Removed. It was the `tracemalloc` peak of the measuring process; with the servers out of process it described nothing about the router. |
| `environment.server_peak_rss_bytes` | Added, as a map of role to that process's own `VmHWM`. Null off Linux. |
| `probes.recorder_dropped` | Removed, in favour of an exact expected count. |
| `probes.recorded_traces_expected` | Added. Readiness polls `/healthz`, which the gateway serves locally and never records, so the count is exact rather than a lower bound. |
| `probes.servers_out_of_process` | Added. Derived from the pid that answered on each measured socket. |
| `probes.server_exit_codes` | Added, per role. |
| `environment.worktree_clean` | Added. |
| `measurement_notes` | Extended with the caveats below. |

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
the recorded router roughly halves per-process request throughput, and unlike
the latency figures that is not an artefact the schema-2 fixes removed.

### What version 1 reported, and why it is not a 10x correction

The committed version 1 sample reported 15.0 / 40.7 / 52.4 ms. It was produced
on an 8-core arm64 macOS host, so it cannot be differenced against the table
above. Running the version 1 harness on *this* host, five times, gives the
honest comparison:

| Scenario | v1 harness, this host | v2 harness, this host |
| --- | --- | --- |
| 1 KiB | 3.7 ms (range **1.6–6.6**) | 2.0 ms (range 1.6–2.5) |
| 1 MiB buffered | 50.8 ms (range 49.4–54.2) | 6.7 ms (range 6.3–8.2) |
| 1 MiB streamed | 26.8 ms (range **16.0–38.7**) | 10.2 ms (range 9.3–12.4) |

So the same-host correction is about 1.8x, 7.5x and 2.6x — not the tenfold
figure a naive macOS-to-Linux comparison suggests. The clearer signal is the
spread: on identical code, version 1 produced 1.6–6.6 ms across five runs of the
1 KiB scenario and 16.0–38.7 ms across five runs of the streamed scenario. It
was not measuring the router.

This applies to any report you generated yourself, not only to the committed
sample. A file with `schema_version: 1` does not describe router overhead;
discard it and re-run rather than comparing it with a version 2 report.
