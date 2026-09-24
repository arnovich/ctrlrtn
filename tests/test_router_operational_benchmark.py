"""Contracts for the standalone router operational benchmark.

The benchmark's own defects were invisible for a release because its tests
asserted what the implementation declared rather than how it behaved. The
regression tests here deliberately assert *behaviour* -- a latency cliff, the
pid that answered -- because each defect this file guards has a reintroduction
that leaves every constant in the report unchanged.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import signal
import socket
import sqlite3
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

import httpx
import pytest

SCRIPT = (
    Path(__file__).parents[1] / "scripts" / "router_operational_benchmark.py"
)
SPEC = importlib.util.spec_from_file_location(
    "router_operational_benchmark", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)

SMALL = 1024


def _tiny(**overrides) -> "benchmark.BenchmarkConfig":
    base = dict(requests=4, concurrency=2, warmup=1, small_bytes=64)
    base.update(overrides)
    return benchmark.BenchmarkConfig(**base)


def test_config_rejects_unbounded_or_incoherent_workloads():
    with pytest.raises(benchmark.BenchmarkError, match="concurrency"):
        benchmark.BenchmarkConfig(requests=2, concurrency=3).validate()
    with pytest.raises(benchmark.BenchmarkError, match="large_bytes"):
        benchmark.BenchmarkConfig(large_bytes=20 * 1024 * 1024).validate()
    with pytest.raises(benchmark.BenchmarkError, match="response budget"):
        benchmark.BenchmarkConfig(
            requests=10_000,
            concurrency=10,
            large_bytes=1024 * 1024,
        ).validate()


def test_nearest_rank_percentiles_are_deterministic():
    assert benchmark._percentiles([5.0, 1.0, 4.0, 2.0, 3.0]) == {
        "p50_ms": 3.0,
        "p95_ms": 5.0,
        "p99_ms": 5.0,
    }


def test_listener_disables_nagle_on_accepted_sockets():
    """Both halves of the fix, asserted on the socket that carries bytes.

    ``socket.socket(AF_INET, SOCK_STREAM)`` leaves ``proto`` at 0, which is
    exactly when asyncio declines to set TCP_NODELAY on an accepted socket --
    silently. Linux inherits the explicit sockopt across ``accept()`` whatever
    the proto, so asserting the sockopt alone would not catch a proto-only
    revert; BSD does not reliably inherit it, so asserting proto alone would
    not catch a dropped sockopt. Both are load-bearing.
    """
    listener = benchmark._listener()
    try:
        assert listener.proto == socket.IPPROTO_TCP
        assert listener.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1
        client = socket.create_connection(listener.getsockname(), timeout=5)
        try:
            accepted, _ = listener.accept()
            with accepted:
                assert accepted.proto == socket.IPPROTO_TCP
                assert (
                    accepted.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
                    == 1
                )
        finally:
            client.close()
    finally:
        listener.close()


def test_cli_refuses_to_overwrite_evidence(tmp_path, monkeypatch, capsys):
    output = tmp_path / "evidence.json"
    output.write_text("preserve me")
    monkeypatch.setattr(
        benchmark,
        "run_benchmark",
        lambda config: {"schema_version": benchmark.SCHEMA_VERSION},
    )
    monkeypatch.setattr(benchmark, "_worktree_clean", lambda: True)

    assert benchmark.main(["--output", str(output)]) == 2
    assert output.read_text() == "preserve me"
    assert "File exists" in capsys.readouterr().err


def test_cli_refuses_to_write_evidence_from_a_dirty_checkout(
    tmp_path, monkeypatch, capsys
):
    """Provenance that can be wrong is worse than absent.

    The first schema-2 sample recorded a router_revision that could not have
    produced it, because the run happened in a dirty tree.
    """
    monkeypatch.setattr(benchmark, "_worktree_clean", lambda: False)
    monkeypatch.setattr(
        benchmark,
        "run_benchmark",
        lambda config: pytest.fail("should not run from a dirty tree"),
    )

    output = tmp_path / "evidence.json"
    assert benchmark.main(["--output", str(output)]) == 2
    assert not output.exists()
    assert "dirty checkout" in capsys.readouterr().err


def test_cli_rejects_role_without_its_required_arguments(capsys):
    assert benchmark.main(["--role", "upstream"]) == 2
    assert "--role-config" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        benchmark.main(["--role", "not-a-role"])


def test_role_apps_require_their_dependencies():
    config = _tiny()
    with pytest.raises(benchmark.BenchmarkError, match="upstream URL"):
        benchmark._build_role_app("router_unrecorded", config, None, None)
    with pytest.raises(benchmark.BenchmarkError, match="database path"):
        benchmark._build_role_app(
            "router_recorded", config, "http://127.0.0.1:1", None
        )


@pytest.mark.parametrize(
    "returncode,killed,expected",
    [
        (0, False, True),
        (-signal.SIGTERM, False, True),
        (-signal.SIGKILL, False, False),
        (0, True, False),
        (1, False, False),
    ],
)
def test_stopped_cleanly_distinguishes_a_drain_from_a_kill(
    returncode, killed, expected
):
    """uvicorn drains, restores the default disposition and re-raises the
    signal it caught, so a graceful stop reports -SIGTERM rather than 0.
    Needing SIGKILL is the real failure."""

    class _Proc:
        pass

    proc = _Proc()
    proc.returncode = returncode
    handle = benchmark._ServerHandle(role="upstream", proc=proc, killed=killed)
    assert handle.stopped_cleanly is expected


def test_count_traces_surfaces_read_failures(tmp_path):
    """A read error must not be indistinguishable from an empty table.

    Returning 0 on any sqlite error made a locked or unreadable database look
    like a drained one -- the same fail-silently shape this harness exists to
    rule out.
    """
    missing = tmp_path / "absent.db"
    assert benchmark._count_traces(missing) == 0

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a database")
    with pytest.raises(sqlite3.DatabaseError):
        benchmark._count_traces(corrupt)

    populated = tmp_path / "traces.db"
    with sqlite3.connect(populated) as conn:
        conn.execute("CREATE TABLE traces (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO traces (id) VALUES (?)", [(1,), (2,)])
    assert benchmark._count_traces(populated) == 2


def test_await_traces_returns_the_shortfall_rather_than_hanging(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(benchmark, "_count_traces", lambda db: 1)
    assert benchmark._await_traces(tmp_path / "x.db", 5, timeout=0.05) == 1


@pytest.mark.integration
def test_measured_traffic_is_served_by_another_process(tmp_path):
    """Regression: sharing one interpreter inflated overhead 3-7x.

    Derived from the pid that actually answers, not from a constant in the
    report -- a thread-based reimplementation publishes the measuring process's
    own pid and fails here.
    """
    config = _tiny()
    with benchmark._server_process("upstream", config, tmp_path) as server:
        assert server.pid == server.proc.pid
        assert server.pid != os.getpid()
        with httpx.Client(timeout=5.0) as client:
            answering = int(client.get(f"{server.url}/whoami").text)
            assert answering == server.pid
            assert len(client.get(f"{server.url}/small").content) == 64
    assert server.stopped_cleanly
    assert server.peak_rss_bytes is None or server.peak_rss_bytes > 0


@pytest.mark.integration
def test_small_keepalive_responses_do_not_stall(tmp_path):
    """Regression: Nagle plus the peer's delayed ACK added a flat ~40 ms.

    The unit test above pins the socket options; this pins the behaviour, and
    so also catches a refactor that keeps ``_listener`` correct but lets
    uvicorn build the socket itself (its ``bind_socket`` has the same defect).
    Sequential keep-alive requests are what trigger the stall. The healthy
    value here is well under 1 ms and the stalled value is ~41 ms, so the
    threshold has roughly an order of magnitude of headroom.
    """
    config = _tiny(small_bytes=SMALL)
    with benchmark._server_process("upstream", config, tmp_path) as server:
        with httpx.Client(base_url=server.url, timeout=10.0) as client:
            for _ in range(3):
                client.get("/small")
            samples = []
            for _ in range(20):
                started = time.perf_counter()
                response = client.get("/small")
                samples.append((time.perf_counter() - started) * 1000)
                assert len(response.content) == SMALL
    assert (
        statistics.median(samples) < 20.0
    ), f"median {statistics.median(samples):.1f} ms suggests Nagle is back"


@pytest.mark.integration
def test_nothing_is_sampled_inside_the_timed_region(tmp_path, monkeypatch):
    """Regression: tracemalloc ran across the measured window.

    Asserts the property rather than the absence of one dict key, so a rename
    or a different sampler does not slip through.
    """
    config = _tiny()
    observed: list[bool] = []
    original = httpx.AsyncClient.get

    async def recording_get(self, *args, **kwargs):
        observed.append(tracemalloc.is_tracing())
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", recording_get)
    with benchmark._server_process("upstream", config, tmp_path) as server:
        asyncio.run(benchmark._measure(server.url, "small", 64, config))

    assert observed, "the measurement never issued a request"
    assert not any(observed), "a memory profiler ran during a timed region"


@pytest.mark.integration
def test_measure_refuses_to_report_a_run_with_errors(tmp_path):
    """Failed requests are not latency samples, and a run with them is not a
    measurement -- previously their latency landed in p95/p99 and the harness
    still exited 0."""
    # warmup=0 so the mismatch is reached inside the timed region rather than
    # by the warmup guard, which is what this test is about.
    config = _tiny(warmup=0)
    with benchmark._server_process("upstream", config, tmp_path) as server:
        with pytest.raises(benchmark.BenchmarkError, match="requests failed"):
            asyncio.run(
                benchmark._measure(server.url, "small", 999_999, config)
            )


@pytest.mark.integration
def test_benchmark_exercises_real_router_and_failure_contracts():
    config = benchmark.BenchmarkConfig(
        requests=4,
        concurrency=2,
        warmup=1,
        small_bytes=64,
        large_bytes=256,
        stream_chunk_bytes=32,
        stream_chunks=2,
    )
    report = benchmark.run_benchmark(config)

    assert report["schema_version"] == 2
    assert report["config"]["requests"] == 4
    assert report["environment"]["python"]
    assert len(report["measurements"]) == 9
    paths = {item["path"] for item in report["measurements"]}
    assert paths == {"small", "large", "stream"}
    assert {
        (item["path"], item["mode"]) for item in report["measurements"]
    } == {
        (path, mode)
        for path in paths
        for mode in ("direct", "router_unrecorded", "router_recorded")
    }
    for item in report["measurements"]:
        assert item["completed_requests"] == 4
        assert item["errors"] == 0
        assert item["throughput_rps"] > 0
        assert "python_peak_allocated_bytes" not in item

    probes = report["probes"]
    assert probes["upstream_error_passthrough"] is True
    assert probes["recorder_failure_isolated"] is True
    assert probes["clean_shutdown"] is True
    assert probes["servers_out_of_process"] is True

    # Readiness polls /healthz, which the gateway serves locally, so every
    # recorded trace is a measured request. Derived from the run rather than
    # restated as a literal, so adding a payload path cannot desynchronise it.
    expected = (config.requests + config.warmup) * len(paths)
    assert probes["recorded_traces_expected"] == expected
    assert probes["recorded_traces_before_shutdown"] == expected
    assert probes["recorded_traces_after_shutdown"] == expected

    # Pinned as a set, not an exact value: the -SIGTERM is uvicorn's signal
    # restoration, and a future version returning 0 is not a regression.
    for role, code in probes["server_exit_codes"].items():
        assert code in (0, -signal.SIGTERM), role
