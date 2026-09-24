"""The recorder: a queue + background worker that persist traces off the hot
path.

``enqueue`` is non-blocking and drops (with a counter, never silently) when the
queue is full, so recording can never slow or stall request handling.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from ctrlrtn.recorder.models import Outcome
from ctrlrtn.recorder.repositories import TraceRepository
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.identity import WorkflowEvent
from ctrlrtn.workflow.tool_operation import ToolOperationEvent

logger = logging.getLogger(__name__)

_DEFAULT_MAXSIZE = 10_000
# Cap the backpressure an "important" enqueue may apply before it gives up and
# drops (loudly). Only reached when the writer is wedged and the queue is full;
# bounds the post-stream wait so a stuck store can never hang a request.
_DEFAULT_IMPORTANT_TIMEOUT = 5.0
# Cap how long shutdown waits to drain the queue before cancelling the worker.
_DEFAULT_DRAIN_TIMEOUT = 5.0


class Recorder:
    """The queue and worker that persist traces without touching the request
    path. ``enqueue`` never blocks and drops loudly when full; low-volume
    outcome and lifecycle events bypass the queue and are written directly
    because their callers want an acknowledgement."""

    def __init__(
        self,
        store: TraceRepository,
        *,
        enrich: Callable[[Trace], None] | None = None,
        on_record: Callable[[Trace], None] | None = None,
        on_discard: Callable[[Trace], None] | None = None,
        maxsize: int = _DEFAULT_MAXSIZE,
        important_timeout: float = _DEFAULT_IMPORTANT_TIMEOUT,
        drain_timeout: float = _DEFAULT_DRAIN_TIMEOUT,
    ) -> None:
        self._store = store
        self._enrich = enrich
        self._on_record = on_record
        self._discard_observers = [] if on_discard is None else [on_discard]
        self._queue: asyncio.Queue[Trace] = asyncio.Queue(maxsize=maxsize)
        self._worker: asyncio.Task | None = None
        self._dropped = 0
        self._dropped_important = 0
        self._important_timeout = important_timeout
        self._drain_timeout = drain_timeout

    def start(self) -> None:
        """Launch the worker. Idempotent (lifespan and tests may both call)."""
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def record_outcome(self, outcome: Outcome) -> None:
        """Persist an app-reported task outcome. Control-plane, not the hot
        path: low volume and the caller wants an ack, so it writes directly
        rather than going through the drop-on-full trace queue."""
        await self._store.save_outcome(outcome)

    async def record_workflow_event(self, event: WorkflowEvent) -> None:
        """Persist a low-volume, idempotent workflow lifecycle event."""
        await self._store.save_workflow_event(event)

    async def record_tool_operation_event(
        self, event: ToolOperationEvent
    ) -> None:
        """Persist an authoritative low-volume tool lifecycle event."""
        await self._store.save_tool_operation_event(event)

    def add_discard_observer(self, callback: Callable[[Trace], None]) -> None:
        """Observe traces persistence could not retain.

        Discard observers run after enrichment so accounting systems can settle
        an in-flight reservation even when the durable writer is unavailable.
        """
        if callback not in self._discard_observers:
            self._discard_observers.append(callback)

    def enqueue(self, trace: Trace) -> bool:
        """Hand a trace to the worker without blocking the caller."""
        try:
            self._queue.put_nowait(trace)
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            self._discard(trace)
            logger.warning(
                "trace queue full; dropped trace (total dropped=%d)",
                self._dropped,
            )
            return False

    async def enqueue_important(self, trace: Trace) -> bool:
        """Enqueue a candidate-arm trace that should not be dropped (a lost
        candidate call is an MNAR confounder that biases the experiment),
        applying BOUNDED backpressure instead of dropping on full.

        Call this ONLY off the client's response path (the post-stream finally),
        so the wait can't delay a response the client already has. If the queue
        stays full past ``important_timeout`` (a wedged writer) or the wait is
        cancelled (client disconnect), the trace is dropped and counted loudly in
        ``dropped_important`` — never silently, and never hanging the request.
        """
        try:
            await asyncio.wait_for(
                self._queue.put(trace), self._important_timeout
            )
            return True
        except TimeoutError:
            self._dropped_important += 1
            self._discard(trace)
            logger.warning(
                "important trace dropped after %.1fs backpressure "
                "(total important dropped=%d)",
                self._important_timeout,
                self._dropped_important,
            )
            return False
        except asyncio.CancelledError:
            # A cancelled put does NOT enqueue the item, so count the loss before
            # propagating the cancellation (else it would vanish unobserved).
            self._dropped_important += 1
            self._discard(trace)
            logger.warning(
                "important trace lost to cancellation "
                "(total important dropped=%d)",
                self._dropped_important,
            )
            raise

    async def _run(self) -> None:
        while True:
            trace = await self._queue.get()
            try:
                if self._enrich is not None:
                    self._safe_enrich(trace)
                await self._store.save(trace)
                if self._on_record is not None:
                    self._safe_on_record(trace)
            except asyncio.CancelledError:
                self._discard(trace, enrich=False)
                raise
            except Exception:  # one bad trace must not kill the worker
                logger.exception("failed to persist trace")
                self._discard(trace, enrich=False)
            finally:
                self._queue.task_done()

    def _safe_enrich(self, trace: Trace) -> None:
        assert self._enrich is not None
        try:
            self._enrich(trace)
        except Exception:  # enrichment must not stop a trace being saved
            logger.exception("trace enrichment failed")

    def _safe_on_record(self, trace: Trace) -> None:
        assert self._on_record is not None
        try:
            self._on_record(trace)
        except Exception:  # an observer must not break recording
            logger.exception("on_record callback failed")

    def _discard(self, trace: Trace, *, enrich: bool = True) -> None:
        if enrich and self._enrich is not None:
            self._safe_enrich(trace)
        for callback in self._discard_observers:
            try:
                callback(trace)
            except Exception:
                logger.exception("trace discard observer failed")

    async def join(self) -> None:
        """Block until every queued trace has been processed."""
        await self._queue.join()

    async def aclose(self) -> None:
        if self._worker is None:
            return
        # Drain the backlog (bounded) before cancelling, so queued traces —
        # including undroppable candidate ones — are persisted on shutdown rather
        # than discarded with the worker.
        try:
            await asyncio.wait_for(self._queue.join(), self._drain_timeout)
        except TimeoutError:
            logger.warning(
                "recorder shutdown: %d traces left undrained",
                self._queue.qsize(),
            )
        self._worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._worker
        while True:
            try:
                trace = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._discard(trace)
            self._queue.task_done()
        self._worker = None

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def dropped_important(self) -> int:
        return self._dropped_important
