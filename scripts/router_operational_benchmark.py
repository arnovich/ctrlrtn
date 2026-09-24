#!/usr/bin/env python3
"""Measure local router overhead and exercise bounded failure contracts.

This is a comparative development benchmark, not a production capacity claim.
It runs an upstream and two router configurations over real loopback TCP,
reports direct-versus-router measurements, and never enforces timing thresholds
that would vary by machine.

Two properties keep the timings meaningful, and both were absent before schema
version 2 (see ``docs/router-operational-benchmark.md`` for the measurements
that motivated them):

* **Every measured server runs in its own OS process.** Sharing one interpreter
  makes the upstream and the router contend for a single GIL, and the routed
  path pays that contention twice. In-process runs reported roughly ten times
  the real overhead.
* **Nagle is off on every listener.** See ``_listener`` for the specific trap.

Nothing is sampled or profiled inside a timed region.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import platform
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from ctrlrtn.config import Settings
from ctrlrtn.gateway.app import create_app
from ctrlrtn.recorder.memory_store import InMemoryTraceStore
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.telemetry.enrich import enrich_trace

SCHEMA_VERSION = 2
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_BUDGET = 2 * 1024 * 1024 * 1024


class BenchmarkError(ValueError):
    """An unsafe or incoherent benchmark configuration."""


@dataclass(frozen=True)
class BenchmarkConfig:
    """Bounded inputs for one comparative local run."""

    requests: int = 50
    concurrency: int = 10
    warmup: int = 5
    small_bytes: int = 1024
    large_bytes: int = 1024 * 1024
    stream_chunk_bytes: int = 16 * 1024
    stream_chunks: int = 64
    timeout_seconds: float = 30.0

    def validate(self) -> None:
        """Reject runs that are invalid or unexpectedly resource-intensive."""
        if not 1 <= self.requests <= 10_000:
            raise BenchmarkError("requests must be between 1 and 10000")
        if not 1 <= self.concurrency <= min(self.requests, 256):
            raise BenchmarkError(
                "concurrency must be between 1 and requests (maximum 256)"
            )
        if not 0 <= self.warmup <= 1_000:
            raise BenchmarkError("warmup must be between 0 and 1000")
        for name, value in (
            ("small_bytes", self.small_bytes),
            ("large_bytes", self.large_bytes),
            ("stream_chunk_bytes", self.stream_chunk_bytes),
        ):
            if not 1 <= value <= _MAX_RESPONSE_BYTES:
                raise BenchmarkError(
                    f"{name} must be between 1 and {_MAX_RESPONSE_BYTES}"
                )
        if not 1 <= self.stream_chunks <= 1024:
            raise BenchmarkError("stream_chunks must be between 1 and 1024")
        stream_bytes = self.stream_chunk_bytes * self.stream_chunks
        if stream_bytes > _MAX_RESPONSE_BYTES:
            raise BenchmarkError(
                f"stream response exceeds {_MAX_RESPONSE_BYTES} bytes"
            )
        if (
            max(self.small_bytes, self.large_bytes, stream_bytes)
            * self.requests
            > _MAX_RESPONSE_BUDGET
        ):
            raise BenchmarkError("per-scenario response budget exceeds 2 GiB")
        if (
            not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 300
        ):
            raise BenchmarkError(
                "timeout_seconds must be finite and between 0 and 300"
            )


class _FailFirstTraceStore(InMemoryTraceStore):
    """Inject one persistence failure for the fail-open contract probe."""

    def __init__(self) -> None:
        super().__init__()
        self.save_attempts = 0

    async def save(self, trace) -> None:
        self.save_attempts += 1
        if self.save_attempts == 1:
            raise OSError("injected benchmark persistence failure")
        await super().save(trace)


def _listener() -> socket.socket:
    """Bind an ephemeral loopback listener with Nagle disabled.

    The protocol argument is not cosmetic. ``socket.socket(AF_INET,
    SOCK_STREAM)`` leaves ``proto`` at 0 and ``accept()`` inherits it, while
    asyncio only auto-sets ``TCP_NODELAY`` on an accepted socket when
    ``sock.proto == IPPROTO_TCP``. Omitting it therefore leaves Nagle on with no
    error and no log line: small responses on a kept-alive connection wait for
    the peer's delayed ACK, which is ~40 ms on Linux and differs on macOS. That
    stall dwarfed everything this harness is meant to measure, and because it is
    payload-size dependent it looked like router cost rather than a socket
    option. Set the option explicitly too -- accepted sockets inherit it -- so
    the behaviour does not depend on asyncio internals.

    Only this harness was affected. The shipped gateway calls ``uvicorn.run``
    with a host and port, which lets uvicorn create the socket itself.
    """
    listener = socket.socket(
        socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP
    )
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(256)
    return listener


@contextmanager
def _running_server(app, *, startup_timeout: float = 5.0) -> Iterator[str]:
    """Run an ASGI app on an ephemeral loopback listener, in this process.

    Used only by the correctness probes, which need in-process access to an
    injected store. Timed traffic never goes through here -- see
    ``_server_process``.
    """
    listener = _listener()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            lifespan="on",
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name=f"benchmark-uvicorn-{port}",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + startup_timeout
    while not server.started and thread.is_alive():
        if time.monotonic() >= deadline:
            server.should_exit = True
            thread.join(timeout=1)
            listener.close()
            raise BenchmarkError(f"server on port {port} did not start")
        time.sleep(0.01)
    if not thread.is_alive():
        listener.close()
        raise BenchmarkError(f"server on port {port} exited during startup")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=startup_timeout)
        listener.close()
        if thread.is_alive():
            raise BenchmarkError(f"server on port {port} did not stop")


_ROLES = ("upstream", "router_unrecorded", "router_recorded")


def _build_role_app(
    role: str, config: BenchmarkConfig, upstream_url: str | None, db: str | None
) -> tuple[Starlette, SqliteTraceStore | None]:
    """Build the ASGI app a child process serves."""
    if role == "upstream":
        return _upstream(config), None
    if upstream_url is None:
        raise BenchmarkError(f"role {role} needs an upstream URL")
    settings = Settings(upstream_base_url=upstream_url, timeout=5.0)
    if role == "router_unrecorded":
        return create_app(settings), None
    if db is None:
        raise BenchmarkError("role router_recorded needs a database path")
    store = SqliteTraceStore(Path(db))
    app = create_app(
        settings,
        recorder=Recorder(store, enrich=enrich_trace),
        store=store,
        # Nothing after ``server.run`` runs (see _serve_role), so the lifespan
        # is the only place the WAL gets checkpointed.
        close_store_on_shutdown=True,
    )
    return app, store


def _serve_role(
    role: str,
    config: BenchmarkConfig,
    upstream_url: str | None,
    db: str | None,
    port_file: str,
) -> int:
    """Child-process entry point: serve one role until terminated.

    Port and pid are published through a file rather than stdout, so the parent
    never has to interleave with uvicorn's own output. The pid is what lets the
    parent *derive* that measured traffic was served out of process instead of
    asserting a constant that any refactor could leave behind.

    Nothing runs after ``server.run``: uvicorn's ``capture_signals`` restores
    the default disposition and re-raises the signal it caught, so the process
    dies inside that call. The store is therefore closed by the ASGI lifespan
    (``close_store_on_shutdown``), not here.
    """
    config.validate()
    app, _store = _build_role_app(role, config, upstream_url, db)
    listener = _listener()
    payload = json.dumps(
        {"port": listener.getsockname()[1], "pid": os.getpid()}
    )
    tmp = Path(f"{port_file}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(Path(port_file))  # atomic publish; no torn read
    server = uvicorn.Server(
        uvicorn.Config(
            app, lifespan="on", log_level="warning", access_log=False
        )
    )
    server.run(sockets=[listener])
    return 0


def _await_publication(
    port_file: Path, proc: subprocess.Popen, deadline: float
) -> tuple[int, int]:
    """Wait for the child to publish its port and its own pid."""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise BenchmarkError(
                f"server process exited during startup "
                f"(returncode {proc.returncode})"
            )
        try:
            text = port_file.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            try:
                published = json.loads(text)
                return int(published["port"]), int(published["pid"])
            except (ValueError, KeyError, TypeError) as exc:
                raise BenchmarkError(
                    f"unreadable server publication in {port_file}: {exc}"
                ) from exc
        time.sleep(0.01)
    raise BenchmarkError(f"server process never published a port ({port_file})")


def _await_ready(url: str, proc: subprocess.Popen, deadline: float) -> None:
    """Poll ``/healthz`` until the server answers.

    Deliberately not a proxied path. The gateway serves ``/healthz`` locally, so
    readiness costs no upstream call and records no trace, which keeps the
    expected trace count exact.
    """
    last: Exception | None = None
    with httpx.Client(timeout=1.0) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise BenchmarkError(
                    f"server at {url} exited before becoming ready "
                    f"(returncode {proc.returncode})"
                )
            try:
                if client.get(f"{url}/healthz").status_code == 200:
                    return
            except httpx.HTTPError as exc:  # not listening yet
                last = exc
            time.sleep(0.02)
    raise BenchmarkError(f"server at {url} never became ready ({last})")


@dataclass
class _ServerHandle:
    """A server running in its own process, and how it stopped."""

    role: str
    proc: subprocess.Popen
    url: str = ""
    pid: int = 0  # as reported by the child itself, not by Popen
    peak_rss_bytes: int | None = None
    killed: bool = False

    @property
    def stopped_cleanly(self) -> bool:
        """Whether the server shut down under its own control.

        uvicorn drains, restores the default disposition and re-raises the
        signal it caught, so a *graceful* SIGTERM stop reports ``-SIGTERM``
        rather than 0. Needing SIGKILL is the real failure.
        """
        return not self.killed and self.proc.returncode in (
            0,
            -signal.SIGTERM,
        )


@contextmanager
def _server_process(
    role: str,
    config: BenchmarkConfig,
    root: Path,
    *,
    upstream_url: str | None = None,
    db: str | None = None,
    startup_timeout: float = 30.0,
) -> Iterator[_ServerHandle]:
    """Run one role in its own OS process.

    Measured traffic must never share an interpreter with the server answering
    it: one GIL serialises the client, the upstream and the router, and the
    routed path pays that twice. Doing so inflated the reported overhead by
    roughly an order of magnitude.
    """
    if role not in _ROLES:
        raise BenchmarkError(f"unknown server role {role!r}")
    port_file = root / f"{role}.port"
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--role",
        role,
        "--role-config",
        json.dumps(asdict(config)),
        "--role-port-file",
        str(port_file),
    ]
    if upstream_url is not None:
        argv += ["--role-upstream", upstream_url]
    if db is not None:
        argv += ["--role-db", db]

    port_file.unlink(missing_ok=True)  # never read a previous run's port
    proc = subprocess.Popen(argv, preexec_fn=_die_with_parent)
    try:
        handle = _ServerHandle(role=role, proc=proc)
        deadline = time.monotonic() + startup_timeout
        port, pid = _await_publication(port_file, proc, deadline)
        handle.url = f"http://127.0.0.1:{port}"
        handle.pid = pid
        _await_ready(handle.url, proc, deadline)
        yield handle
    finally:
        # Sample RSS while the child is still alive; it is unreadable after.
        handle.peak_rss_bytes = _peak_rss_bytes(proc.pid)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                handle.killed = True
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:  # uninterruptible sleep
                    pass


def _upstream(config: BenchmarkConfig) -> Starlette:
    small = b"s" * config.small_bytes
    large = b"l" * config.large_bytes
    chunk = b"c" * config.stream_chunk_bytes

    async def small_response(request: Request) -> Response:
        return Response(small, media_type="application/octet-stream")

    async def large_response(request: Request) -> Response:
        return Response(large, media_type="application/octet-stream")

    async def stream_response(request: Request) -> StreamingResponse:
        async def chunks():
            for _ in range(config.stream_chunks):
                yield chunk
                await asyncio.sleep(0)

        return StreamingResponse(
            chunks(), media_type="application/octet-stream"
        )

    async def error_response(request: Request) -> Response:
        return Response(
            b"intentional upstream failure",
            status_code=503,
            headers={"x-benchmark-error": "upstream"},
        )

    async def healthz(request: Request) -> Response:
        return Response(b"ok", media_type="text/plain")

    async def whoami(request: Request) -> Response:
        """The pid actually answering on this socket.

        Lets a test prove the server is a different process rather than trust a
        constant in the report.
        """
        return Response(str(os.getpid()).encode(), media_type="text/plain")

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/whoami", whoami),
            Route("/small", small_response),
            Route("/large", large_response),
            Route("/stream", stream_response),
            Route("/error", error_response),
        ]
    )


def _percentiles(latencies_ms: list[float]) -> dict[str, float]:
    """Return deterministic nearest-rank latency percentiles."""
    if not latencies_ms:
        raise BenchmarkError("cannot summarize an empty latency sample")
    ordered = sorted(latencies_ms)

    def nearest_rank(percentile: float) -> float:
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return round(ordered[index], 3)

    return {
        "p50_ms": nearest_rank(0.50),
        "p95_ms": nearest_rank(0.95),
        "p99_ms": nearest_rank(0.99),
    }


async def _measure(
    base_url: str,
    path: str,
    expected_bytes: int,
    config: BenchmarkConfig,
) -> dict:
    limits = httpx.Limits(
        max_connections=config.concurrency,
        max_keepalive_connections=config.concurrency,
    )
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=config.timeout_seconds,
        limits=limits,
    ) as client:
        for _ in range(config.warmup):
            response = await client.get(f"/{path}")
            response.raise_for_status()
            if len(response.content) != expected_bytes:
                raise BenchmarkError(f"warmup payload mismatch for {path}")

        semaphore = asyncio.Semaphore(config.concurrency)
        latencies: list[float] = []
        errors = 0

        async def one_request() -> None:
            # A failure's latency is not a latency sample: a timeout would
            # otherwise set both p95 and p99, which at these sample sizes are
            # the top two order statistics.
            nonlocal errors
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.get(f"/{path}")
                except httpx.HTTPError:
                    errors += 1
                    return
                if (
                    response.status_code != 200
                    or len(response.content) != expected_bytes
                ):
                    errors += 1
                    return
                latencies.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        await asyncio.gather(*(one_request() for _ in range(config.requests)))
        elapsed = time.perf_counter() - started

    if errors:
        raise BenchmarkError(
            f"{errors}/{config.requests} requests failed on /{path} at "
            f"{base_url}; a loopback run with errors is not a measurement"
        )

    return {
        "completed_requests": config.requests - errors,
        "errors": errors,
        "response_bytes": expected_bytes,
        "total_response_bytes": expected_bytes * config.requests,
        "elapsed_seconds": round(elapsed, 6),
        "throughput_rps": round(config.requests / elapsed, 3),
        **_percentiles(latencies),
    }


def _measure_all(
    targets: tuple[tuple[str, str], ...], config: BenchmarkConfig
) -> list[dict]:
    sizes = {
        "small": config.small_bytes,
        "large": config.large_bytes,
        "stream": config.stream_chunk_bytes * config.stream_chunks,
    }
    measurements = []
    for path, expected_bytes in sizes.items():
        for mode, base_url in targets:
            result = asyncio.run(
                _measure(base_url, path, expected_bytes, config)
            )
            measurements.append({"path": path, "mode": mode, **result})
    return measurements


def _comparisons(measurements: list[dict]) -> list[dict]:
    """Compare each router mode with the direct measurement for that path."""
    by_key = {(item["path"], item["mode"]): item for item in measurements}
    comparisons = []
    for path in ("small", "large", "stream"):
        direct = by_key[(path, "direct")]
        for mode in ("router_unrecorded", "router_recorded"):
            routed = by_key[(path, mode)]
            comparisons.append(
                {
                    "path": path,
                    "mode": mode,
                    "p50_overhead_ms": round(
                        routed["p50_ms"] - direct["p50_ms"], 3
                    ),
                    "p95_overhead_ms": round(
                        routed["p95_ms"] - direct["p95_ms"], 3
                    ),
                    "throughput_ratio": round(
                        routed["throughput_rps"] / direct["throughput_rps"],
                        4,
                    ),
                }
            )
    return comparisons


def _die_with_parent() -> None:  # pragma: no cover - runs in the child
    """Ask the kernel to SIGTERM this child if the parent dies.

    Without it a SIGKILLed parent orphans its servers, which keep listening and
    (for the recorded role) keep an open SQLite writer. Linux only; elsewhere
    the context manager's terminate path is the only cleanup.
    """
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            1, int(signal.SIGTERM), 0, 0, 0  # PR_SET_PDEATHSIG
        )
    except Exception:
        pass


def _peak_rss_bytes(pid: int) -> int | None:
    """Peak RSS of one specific process, read while it is still alive.

    Schema version 2 first reported ``getrusage(RUSAGE_CHILDREN).ru_maxrss``,
    which is a running maximum over every descendant ever reaped -- it folded
    in the ``git rev-parse`` child and could not be attributed to any server.
    ``VmHWM`` is per-process and exact. Linux only; ``None`` elsewhere rather
    than a number that means something different.
    """
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _count_traces(db: Path) -> int:
    """Count recorded traces from outside the process that wrote them.

    Opened read-only on purpose. The recorded router owns this file and its
    migrations; a second writer against one SQLite file is how the schema gets
    corrupted.
    """
    if not db.exists():
        return 0  # the child has not created it yet
    uri = f"file:{db}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
        row = conn.execute("SELECT count(*) FROM traces").fetchone()
    return int(row[0]) if row else 0


def _await_traces(db: Path, expected: int, timeout: float = 10.0) -> int:
    """Wait for the recorder queue to drain, then report what landed."""
    deadline = time.monotonic() + timeout
    count = _count_traces(db)
    while count < expected and time.monotonic() < deadline:
        time.sleep(0.02)
        count = _count_traces(db)
    return count


def _error_passthrough_probe(direct_url: str, router_url: str) -> bool:
    with httpx.Client(timeout=5.0) as client:
        direct = client.get(f"{direct_url}/error")
        routed = client.get(f"{router_url}/error")
    return (
        routed.status_code == direct.status_code == 503
        and routed.content == direct.content
        and routed.headers.get("x-benchmark-error") == "upstream"
    )


def _recorder_failure_probe(upstream_url: str) -> bool:
    store = _FailFirstTraceStore()
    recorder = Recorder(store)
    app = create_app(
        Settings(upstream_base_url=upstream_url, timeout=5.0),
        recorder=recorder,
    )
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with _running_server(app) as router_url:
            with httpx.Client(base_url=router_url, timeout=5.0) as client:
                statuses = [client.get("/small").status_code for _ in range(2)]
    finally:
        logging.disable(previous_disable)
    return (
        statuses == [200, 200]
        and store.save_attempts == 2
        and len(store.traces) == 1
    )


def _worktree_clean() -> bool | None:
    """Whether the checkout had no uncommitted changes.

    Schema version 2's first sample recorded a ``router_revision`` that could
    not have produced it, because the run happened in a dirty tree and
    ``_git_revision`` reports a bare ``rev-parse HEAD``. Provenance that can be
    wrong is worse than absent.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return not result.stdout.strip()


def _git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def run_benchmark(config: BenchmarkConfig) -> dict:
    """Run the bounded benchmark and return its machine-readable report."""
    config.validate()

    with tempfile.TemporaryDirectory(prefix="ctrlrtn-benchmark-") as root:
        root_path = Path(root)
        db = root_path / "recorded.db"
        expected_traces = (config.requests + config.warmup) * 3

        with _server_process("upstream", config, root_path) as upstream:
            with _server_process(
                "router_unrecorded",
                config,
                root_path,
                upstream_url=upstream.url,
            ) as unrecorded:
                with _server_process(
                    "router_recorded",
                    config,
                    root_path,
                    upstream_url=upstream.url,
                    db=str(db),
                ) as recorded:
                    measurements = _measure_all(
                        (
                            ("direct", upstream.url),
                            ("router_unrecorded", unrecorded.url),
                            ("router_recorded", recorded.url),
                        ),
                        config,
                    )
                    recorded_before_shutdown = _await_traces(
                        db, expected_traces
                    )
                    error_passthrough = _error_passthrough_probe(
                        upstream.url, unrecorded.url
                    )
                # The recorded router has stopped, draining its queue on the
                # way out, so the file is final.
                recorded_after_shutdown = _count_traces(db)
            recorder_failure = _recorder_failure_probe(upstream.url)
        servers = (upstream, unrecorded, recorded)
        exit_codes = {s.role: s.proc.returncode for s in servers}
        clean_shutdown = all(s.stopped_cleanly for s in servers)
        # Derived, not asserted: each child reported its own pid, so reverting
        # the servers to threads in this interpreter makes this false rather
        # than leaving a stale constant behind.
        measuring_pid = os.getpid()
        out_of_process = all(s.pid not in (0, measuring_pid) for s in servers)
        peak_rss = {s.role: s.peak_rss_bytes for s in servers}

    if recorded_after_shutdown != expected_traces:
        raise BenchmarkError(
            f"recorded {recorded_after_shutdown} traces, expected "
            f"{expected_traces}; the run is not a valid measurement"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "router_revision": _git_revision(),
            "worktree_clean": _worktree_clean(),
            "server_peak_rss_bytes": peak_rss,
        },
        "config": asdict(config),
        "measurement_notes": {
            "transport": "real loopback TCP via Uvicorn and HTTPX",
            "isolation": (
                "every measured server runs in its own OS process, so the "
                "client, the upstream and the router never share one GIL"
            ),
            "nagle": (
                "TCP_NODELAY is set on every listener; without it keep-alive "
                "traffic stalls on the peer's delayed ACK and that stall "
                "dominates the measurement"
            ),
            "memory": (
                "server_peak_rss_bytes is each server's own VmHWM, sampled "
                "before shutdown; null off Linux"
            ),
            "overhead": (
                "p50/p95_overhead_ms difference two independently measured "
                "percentiles rather than percentiles of a per-request "
                "difference, so p95 overhead can fall below p50 overhead"
            ),
            "baseline": (
                "the load generator is one Python process and saturates a core "
                "on the direct path, so it bounds the baseline and overhead is "
                "understated; see the interpretation limits in the docs"
            ),
            "thresholds": "none; compare like-for-like runs on the same host",
        },
        "measurements": measurements,
        "comparisons": _comparisons(measurements),
        "probes": {
            "upstream_error_passthrough": error_passthrough,
            "recorder_failure_isolated": recorder_failure,
            "recorded_traces_expected": expected_traces,
            "recorded_traces_before_shutdown": recorded_before_shutdown,
            "recorded_traces_after_shutdown": recorded_after_shutdown,
            "clean_shutdown": clean_shutdown,
            "server_exit_codes": exit_codes,
            "servers_out_of_process": out_of_process,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--small-bytes", type=int, default=1024)
    parser.add_argument("--large-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--stream-chunk-bytes", type=int, default=16 * 1024)
    parser.add_argument("--stream-chunks", type=int, default=64)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--output",
        type=Path,
        help="write JSON to this new file instead of stdout",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="write evidence from a checkout with uncommitted changes",
    )
    # Internal: how the harness re-invokes itself to run one server in its own
    # process. Not part of the documented interface.
    parser.add_argument("--role", choices=_ROLES, help=argparse.SUPPRESS)
    parser.add_argument("--role-config", help=argparse.SUPPRESS)
    parser.add_argument("--role-upstream", help=argparse.SUPPRESS)
    parser.add_argument("--role-db", help=argparse.SUPPRESS)
    parser.add_argument("--role-port-file", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.role is not None:
        if not args.role_config or not args.role_port_file:
            print(
                "benchmark failed: --role needs --role-config and "
                "--role-port-file",
                file=sys.stderr,
            )
            return 2
        return _serve_role(
            args.role,
            BenchmarkConfig(**json.loads(args.role_config)),
            args.role_upstream,
            args.role_db,
            args.role_port_file,
        )
    config = BenchmarkConfig(
        requests=args.requests,
        concurrency=args.concurrency,
        warmup=args.warmup,
        small_bytes=args.small_bytes,
        large_bytes=args.large_bytes,
        stream_chunk_bytes=args.stream_chunk_bytes,
        stream_chunks=args.stream_chunks,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        if (
            args.output is not None
            and not args.allow_dirty
            and _worktree_clean() is False
        ):
            raise BenchmarkError(
                "refusing to write evidence from a dirty checkout: the "
                "recorded router_revision would not describe the code that "
                "produced it (pass --allow-dirty to override)"
            )
        report = run_benchmark(config)
        encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            sys.stdout.write(encoded)
        else:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
    except (BenchmarkError, OSError, subprocess.SubprocessError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 2

    probes = report["probes"]
    failed = [
        name
        for name in (
            "upstream_error_passthrough",
            "recorder_failure_isolated",
            "clean_shutdown",
            "servers_out_of_process",
        )
        if not probes[name]
    ]
    if failed:
        # The docs say a failed probe invalidates the run; make the exit code
        # say so too, so CI and shell callers cannot read success from it.
        print(f"benchmark probes failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
