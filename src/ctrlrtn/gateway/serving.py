"""The live A/B serving path: swap in a candidate model for a task assigned to a
running experiment's candidate arm, else pass the request through.

This is where the router becomes actively-mutating, so it is built to stay cheap
and correct. Cheap: the common case (nothing running) is an empty snapshot, a
dict miss, and a pass-through — no DB read per request. Correct: it keys the
experiment lookup on the SAME fingerprint the recorder will store (so the arm is
attributed to exactly the recorded use-case), binds a whole task to one
experiment (so an edition can't straddle two and become an uncontrolled 2x2
factorial), and reports the arm only via the ServeDecision the proxy threads
into the Trace — never as its own write.

State is minimal and touched only from the single event-loop thread (``decide``
is synchronous, so its read-modify-write of the per-task counter runs atomically
against both other requests and the snapshot refresh): a periodically-refreshed
snapshot of running experiments and a bounded per-task binding + divergence
counter. ``decide`` therefore does mutate internal state (the per-task candidate
call count) before it returns — an intentional, purely in-memory exception to
the hook's "side-effect-free" note; it still performs no I/O and no DB write.
Arm assignment itself is stateless (see ``experiment.bucket``).

NOTE this per-task state is per-process: run the gateway single-process (the CLI
default). Under multiple workers a task's calls split across counters, so the
ceiling and the one-experiment-per-task guard weaken (arm assignment, being
stateless, still holds). A restart likewise resets the counters — measurement
comes from durable traces, not this counter, so only live enforcement is
affected.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass

from ctrlrtn.gateway.decision import ServingDecision
from ctrlrtn.gateway.inject import inject_cache_control
from ctrlrtn.gateway.proxy import TerminalError
from ctrlrtn.identify.fingerprint import fingerprint
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.experiment import decide as resolve_arm
from ctrlrtn.policy.fallback import ApprovedFallback, FallbackDecision
from ctrlrtn.policy.route import Route, RouteDecision, WorkflowRoute
from ctrlrtn.recorder.repositories import ServingRepository
from ctrlrtn.telemetry import pricing
from ctrlrtn.workflow.identity import identity_from_headers

logger = logging.getLogger(__name__)

_TASK_HEADER = "x-ctrlrtn-task"
_DEFAULT_REFRESH_SECONDS = 10.0
# Bound the task binding table: editions are short-lived, so an LRU cap far
# above the in-flight set never evicts a live task while capping memory.
_MAX_TASK_BINDINGS = 50_000
# Status the client sees when a candidate arm breaches its per-task ceiling.
_CEILING_STATUS = 429


@dataclass
class _TaskState:
    """Per-task serving state: which experiment the task is bound to (one only),
    how many candidate calls it has served (the divergence ceiling), and whether
    a ceiling breach has already been recorded — so one runaway is counted as ONE
    failure, not once per client retry of the terminal."""

    experiment_id: str
    candidate_calls: int = 0
    ceiling_recorded: bool = False


class ExperimentRouter:
    """Builds the proxy ``decide`` hook from a live experiment snapshot."""

    def __init__(
        self,
        store: ServingRepository,
        *,
        inject_cache: bool,
        refresh_seconds: float = _DEFAULT_REFRESH_SECONDS,
    ) -> None:
        self._store = store
        self._inject_cache = inject_cache
        self._refresh_seconds = refresh_seconds
        self._snapshot: dict[str, Experiment] = {}
        self._routes: dict[str, Route] = {}
        self._workflow_routes: dict[
            tuple[str, str, str | None], WorkflowRoute
        ] = {}
        self._control_revision: str | None = None
        self._fallbacks: dict[str, ApprovedFallback] = {}
        self._tasks: OrderedDict[str, _TaskState] = OrderedDict()
        self._refresh_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Load the initial snapshot and begin refreshing it in the background."""
        if self._refresh_task is not None:
            return  # idempotent: a second start() must not orphan the loop
        await self.refresh()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def aclose(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None

    async def refresh(self) -> None:
        # running_experiments/routes take the store's blocking lock, so run
        # them off the event loop (the slice-1 contract).
        self._snapshot = await asyncio.to_thread(
            self._store.running_experiments
        )
        self._routes = {
            r.use_case_key: r
            for r in await asyncio.to_thread(self._store.routes)
        }
        workflow_routes = await asyncio.to_thread(self._store.workflow_routes)
        self._workflow_routes = {route.key: route for route in workflow_routes}
        revision = await asyncio.to_thread(self._store.control_revision)
        self._control_revision = (
            revision.revision if revision is not None else None
        )
        fallbacks = await asyncio.to_thread(self._store.fallbacks)
        self._fallbacks = {
            fallback.use_case_key: fallback for fallback in fallbacks
        }
        logger.debug(
            "snapshot refreshed: %d experiment(s), %d route(s), "
            "%d workflow route(s), %d fallback(s)",
            len(self._snapshot),
            len(self._routes),
            len(self._workflow_routes),
            len(self._fallbacks),
        )

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            try:
                await self.refresh()
            except Exception:  # a refresh failure must not kill the loop
                logger.exception("experiment snapshot refresh failed")

    def decide(
        self,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        upstream_api: str | None = None,
    ) -> tuple[bytes, ServingDecision | None]:
        """The proxy hook: CPU-only, non-blocking, side-effect-free until it
        returns. Reads the in-memory snapshot only — never the DB."""
        forward, serve = self._route(headers, body)
        if self._inject_cache and upstream_api in (None, "anthropic"):
            forward = inject_cache_control(path, forward)
        return forward, serve

    def _route(
        self, headers: Mapping[str, str], body: bytes
    ) -> tuple[bytes, ServingDecision | None]:
        """Assign an arm and, on the candidate arm, the model-swapped body. The
        body is parsed at most once here for the model read + rewrite (the
        fingerprint does its own parse); nothing else touches it."""
        snapshot = self._snapshot  # one read; decide is sync, so never torn
        routes = self._routes  # same: a dict swap is atomic under the GIL
        workflow_routes = self._workflow_routes
        if not snapshot and not routes and not workflow_routes:
            return body, None
        if not routes and not workflow_routes and not headers.get(_TASK_HEADER):
            # Experiments-only and untasked: never eligible — skip the
            # fingerprint (a JSON parse + hash) on this hot path.
            return body, None
        use_case = fingerprint(headers, body)
        identity, _ = identity_from_headers(headers)
        experiment = snapshot.get(use_case) if use_case is not None else None
        if experiment is not None and not experiment.scope.matches_identity(
            identity
        ):
            experiment = None
        if experiment is None:
            # Exact explicit identity is required: partial or malformed headers
            # never authorize a workflow-scoped routing decision.
            route = None
            scope = "use_case"
            rule_key = use_case or "(unkeyed)"
            if identity is not None:
                route = workflow_routes.get(
                    (
                        identity.workflow,
                        identity.workflow_version,
                        identity.step,
                    )
                )
                if route is not None:
                    scope = "workflow_step"
                    rule_key = (
                        f"{identity.workflow}@{identity.workflow_version}/"
                        f"{identity.step}"
                    )
                else:
                    route = workflow_routes.get(
                        (identity.workflow, identity.workflow_version, None)
                    )
                    if route is not None:
                        scope = "workflow"
                        rule_key = (
                            f"{identity.workflow}@{identity.workflow_version}"
                        )
            if route is None:
                route = routes.get(use_case) if use_case is not None else None
            if route is None:
                return body, None
            return self._apply_route(
                route,
                use_case or "(unkeyed)",
                body,
                rule_scope=scope,
                rule_key=rule_key,
            )
        # An experiment in flight owns the traffic split; a route on the same
        # use-case stays dormant until the experiment stops.
        task_id = headers.get(_TASK_HEADER)
        if not task_id:  # untasked calls never enter an experiment
            return body, None
        # One experiment per task: a task binds to the first experiment it hits;
        # a later call for a DIFFERENT experiment's use-case is left untouched so
        # the edition never straddles two experiments.
        existing = self._tasks.get(task_id)
        if (
            existing is not None
            and existing.experiment_id != experiment.experiment_id
        ):
            return body, None
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return body, None
        model = payload.get("model")
        if not isinstance(model, str) or not model:
            return body, None
        state = self._bind(task_id, experiment.experiment_id)
        serve = resolve_arm(experiment, task_id, model)
        logger.debug(
            "arm decision: task=%s use_case=%s arm=%s model=%s->%s",
            task_id,
            use_case,
            "candidate" if serve.is_candidate else "baseline",
            model,  # requested
            serve.served_model,  # actually served (the candidate, on that arm)
        )
        if not serve.is_candidate:
            return body, serve  # baseline is the incumbent; no ceiling
        if state.candidate_calls >= experiment.max_calls_per_task:
            # Divergence ceiling: a candidate that keeps calling within one task
            # (a loop / runaway) is stopped and COUNTED as a failure, rather than
            # spending unbounded or vanishing into 'unreported' (the MNAR trap).
            # Record it exactly once — a client that retries the terminal must
            # not inflate the candidate's failure count by re-recording. The
            # counter stays pinned at the cap; every retry re-raises cheaply.
            first_breach = not state.ceiling_recorded
            state.ceiling_recorded = True
            if first_breach:
                logger.warning(
                    "divergence ceiling: task=%s experiment=%s hit "
                    "max_calls_per_task=%d",
                    task_id,
                    experiment.experiment_id,
                    experiment.max_calls_per_task,
                )
            raise TerminalError(
                _CEILING_STATUS,
                "candidate exceeded max_calls_per_task "
                f"({experiment.max_calls_per_task}) for this task",
                serve,
                record=first_breach,
            )
        state.candidate_calls += 1
        payload["model"] = serve.served_model  # swap in the candidate model
        return json.dumps(payload).encode("utf-8"), serve

    def fallback(
        self,
        path: str,
        headers: Mapping[str, str],
        original_body: bytes,
        served_body: bytes,
        upstream_api: str | None = None,
    ) -> tuple[bytes, ServingDecision | None]:
        """Resolve a budget downgrade from the in-memory approval snapshot.

        Running experiments and persistent routes own their traffic and are
        never overridden. Evidence is also bound to the exact baseline model
        it evaluated, so a later model change fails closed.
        """
        use_case = fingerprint(headers, original_body)
        if use_case is None:
            return served_body, None
        if use_case in self._snapshot or use_case in self._routes:
            return served_body, None
        approved = self._fallbacks.get(use_case)
        if approved is None:
            return served_body, None
        try:
            payload = json.loads(served_body)
        except (json.JSONDecodeError, ValueError):
            return served_body, None
        model = payload.get("model")
        if model != approved.baseline_model or model == approved.model:
            return served_body, None
        payload["model"] = approved.model
        cap = pricing.max_output_tokens(approved.model)
        requested_max = payload.get("max_tokens")
        if (
            cap is not None
            and isinstance(requested_max, int)
            and not isinstance(requested_max, bool)
            and requested_max > cap
        ):
            payload["max_tokens"] = cap
        decision = FallbackDecision(
            use_case_key=use_case,
            served_model=approved.model,
            original_model=model,
            provider=approved.provider,
        )
        logger.info(
            "budget fallback: use_case=%s model=%s->%s",
            use_case,
            model,
            approved.model,
        )
        return json.dumps(payload).encode("utf-8"), decision

    def _apply_route(
        self,
        route: Route | WorkflowRoute,
        use_case: str,
        body: bytes,
        *,
        rule_scope: str = "use_case",
        rule_key: str | None = None,
    ) -> tuple[bytes, ServingDecision | None]:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return body, None
        model = payload.get("model")
        if not isinstance(model, str) or not model:
            return body, None
        if model == route.model:
            return body, None  # already on the routed model; nothing to swap
        payload["model"] = route.model
        # A route hits 100% of the use-case's traffic: a recorded max_tokens
        # above the routed model's output cap would 400 EVERY call (a full
        # outage until `route clear`). Clamp down to the known cap — possible
        # truncation beats a hard outage; `route set` warns about it upfront.
        cap = pricing.max_output_tokens(route.model)
        requested_max = payload.get("max_tokens")
        if (
            cap is not None
            and isinstance(requested_max, int)
            and requested_max > cap
        ):
            payload["max_tokens"] = cap
            logger.debug(
                "route clamp: use_case=%s max_tokens %d -> %d (%s cap)",
                use_case,
                requested_max,
                cap,
                route.model,
            )
        logger.debug(
            "route override: use_case=%s model=%s->%s",
            use_case,
            model,
            route.model,
        )
        serve = RouteDecision(
            use_case_key=use_case,
            served_model=route.model,
            original_model=model,
            provider=route.provider,
            rule_scope=rule_scope,
            rule_key=rule_key or use_case,
            control_revision=self._control_revision,
        )
        return json.dumps(payload).encode("utf-8"), serve

    def _bind(self, task_id: str, experiment_id: str) -> _TaskState:
        state = self._tasks.get(task_id)
        if state is None or state.experiment_id != experiment_id:
            state = _TaskState(experiment_id=experiment_id)
            self._tasks[task_id] = state
        self._tasks.move_to_end(task_id)
        while len(self._tasks) > _MAX_TASK_BINDINGS:
            self._tasks.popitem(last=False)  # evict least-recent
        return state
