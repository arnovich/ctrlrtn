"""Bounded, off-response-path execution for online shadow experiments."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from ctrlrtn.gateway.redact import strip_query_credentials
from ctrlrtn.identify.fingerprint import fingerprint
from ctrlrtn.policy.shadow import ShadowExperiment, selected
from ctrlrtn.recorder.recorder import Recorder
from ctrlrtn.recorder.redaction import (
    CREDENTIAL_HEADERS,
    redact_headers,
    redact_query,
)
from ctrlrtn.recorder.repositories import ShadowRepository
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry import pricing
from ctrlrtn.workflow.identity import CTRLRTN_HEADER_PREFIX

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowPair:
    shadow_id: str
    pair_id: str


@dataclass(frozen=True)
class _Work:
    experiment: ShadowExperiment
    pair_id: str
    method: str
    path: str
    forward_path: str
    query: str
    headers: dict[str, str]
    body: bytes
    base_url: str
    provider: str | None
    provider_free: bool
    credential: object | None


class ShadowManager:
    def __init__(
        self,
        store: ShadowRepository,
        recorder: Recorder,
        resolver,
        *,
        client: httpx.AsyncClient,
        queue_size: int = 100,
        workers: int = 2,
        refresh_seconds: float = 10.0,
    ) -> None:
        self._store = store
        self._recorder = recorder
        self._resolver = resolver
        self._client = client
        self._queue: asyncio.Queue[_Work] = asyncio.Queue(maxsize=queue_size)
        self._worker_count = workers
        self._refresh_seconds = refresh_seconds
        self._snapshot: dict[str, ShadowExperiment] = {}
        self._workers: list[asyncio.Task] = []
        self._refresh_task: asyncio.Task | None = None
        self._dropped: dict[str, int] = {}

    async def start(self) -> None:
        await self.refresh()
        self._workers = [
            asyncio.create_task(self._run(), name=f"shadow-worker-{index}")
            for index in range(self._worker_count)
        ]
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="shadow-refresh"
        )

    async def refresh(self) -> None:
        self._snapshot = await asyncio.to_thread(
            self._store.running_shadow_experiments
        )

    async def aclose(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
        try:
            await asyncio.wait_for(self._queue.join(), 10.0)
        except TimeoutError:
            logger.warning(
                "shadow shutdown: %d mirrors undrained", self._queue.qsize()
            )
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        await self._flush_drops()

    def submit(
        self,
        *,
        method: str,
        path: str,
        provider_path: str,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
        baseline_base_url: str,
        baseline_api: str | None,
        baseline_provider: str | None,
        baseline_free: bool,
        baseline_credential,
    ) -> ShadowPair | None:
        use_case = fingerprint(headers, body)
        experiment = self._snapshot.get(use_case or "")
        if experiment is None:
            return None
        if not experiment.scope.matches_headers(headers):
            return None
        unit = headers.get("x-ctrlrtn-task") or uuid.uuid4().hex
        if not selected(experiment, unit):
            return None
        try:
            payload = json.loads(body)
            original_model = payload.get("model")
            if not isinstance(original_model, str) or not original_model:
                return None
            payload["model"] = experiment.candidate_model
            cap = pricing.max_output_tokens(experiment.candidate_model)
            requested = payload.get("max_tokens")
            if (
                cap is not None
                and isinstance(requested, int)
                and requested > cap
            ):
                payload["max_tokens"] = cap
            candidate_body = json.dumps(payload).encode()
        except (ValueError, TypeError):
            return None

        target = None
        if experiment.candidate_provider is not None:
            target = self._resolver.resolve_provider(
                experiment.candidate_provider, provider_path
            )
            if target is None or target.api != baseline_api:
                self._drop(experiment.shadow_id)
                return None
        pair_id = "pair:" + uuid.uuid4().hex
        work = _Work(
            experiment=experiment,
            pair_id=pair_id,
            method=method,
            path=path,
            forward_path=provider_path,
            query=query,
            headers=dict(headers),
            body=candidate_body,
            base_url=target.base_url if target else baseline_base_url,
            provider=target.name if target else baseline_provider,
            provider_free=target.free if target else baseline_free,
            credential=target.credential if target else baseline_credential,
        )
        try:
            self._queue.put_nowait(work)
        except asyncio.QueueFull:
            self._drop(experiment.shadow_id)
            return None
        return ShadowPair(experiment.shadow_id, pair_id)

    def _drop(self, shadow_id: str) -> None:
        self._dropped[shadow_id] = self._dropped.get(shadow_id, 0) + 1

    async def _run(self) -> None:
        while True:
            work = await self._queue.get()
            try:
                await asyncio.to_thread(
                    self._store.increment_shadow_stats,
                    work.experiment.shadow_id,
                    submitted=1,
                )
                await self._mirror(work)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One transient store/recorder failure must not permanently
                # remove a worker and strand every later mirror in the queue.
                self._drop(work.experiment.shadow_id)
                logger.exception(
                    "shadow worker failed; mirror counted as dropped"
                )
            finally:
                self._queue.task_done()

    async def _mirror(self, work: _Work) -> None:
        started = time.monotonic()
        failed = False
        status = 502
        response_headers: dict[str, str] = {}
        response_body = b"shadow send failed"
        try:
            credential_headers = (
                work.credential.headers() if work.credential is not None else {}
            )
            skip = {"host", "content-length", "connection"}
            if work.credential is not None:
                skip |= CREDENTIAL_HEADERS
            headers = {
                k: v
                for k, v in work.headers.items()
                if k.lower() not in skip
                and not k.lower().startswith(CTRLRTN_HEADER_PREFIX)
            }
            headers.update(credential_headers)
            query = (
                strip_query_credentials(work.query)
                if work.credential is not None
                else work.query
            )
            target = work.base_url.rstrip("/") + work.forward_path
            if query:
                target += "?" + query
            response = await self._client.request(
                work.method, target, headers=headers, content=work.body
            )
            status = response.status_code
            response_headers = redact_headers(response.headers)
            response_body = response.content
            failed = status >= 400
        except Exception as exc:  # shadow failure never reaches the user
            failed = True
            response_body = str(exc).encode()[:1000]
            logger.warning("shadow request failed: %s", exc)
        trace = Trace(
            method=work.method,
            path=work.path,
            query=redact_query(work.query),
            request_headers=redact_headers(work.headers),
            request_body=work.body,
            status_code=status,
            response_headers=response_headers,
            response_body=response_body,
            latency_ms=(time.monotonic() - started) * 1000,
            provider=work.provider,
            provider_free=work.provider_free,
            served_model=work.experiment.candidate_model,
            shadow_experiment_id=work.experiment.shadow_id,
            shadow_pair_id=work.pair_id,
            shadow_role="candidate",
            terminal_reason="shadow_failure" if failed else None,
        )
        recorded = await self._recorder.enqueue_important(trace)
        if recorded is False:
            await asyncio.to_thread(
                self._store.increment_shadow_stats,
                work.experiment.shadow_id,
                dropped=1,
            )
            return
        await asyncio.to_thread(
            self._store.increment_shadow_stats,
            work.experiment.shadow_id,
            failed=1 if failed else 0,
            completed=0 if failed else 1,
        )

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            try:
                await self.refresh()
                await self._flush_drops()
            except Exception:
                logger.exception("shadow snapshot refresh failed")

    async def _flush_drops(self) -> None:
        pending, self._dropped = self._dropped, {}
        for shadow_id, count in pending.items():
            try:
                await asyncio.to_thread(
                    self._store.increment_shadow_stats,
                    shadow_id,
                    dropped=count,
                )
            except Exception:
                self._dropped[shadow_id] = (
                    self._dropped.get(shadow_id, 0) + count
                )
                raise
