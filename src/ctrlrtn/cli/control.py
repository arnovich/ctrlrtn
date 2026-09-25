"""CLI adapter for mutable routing and experiment controls.

The parser and top-level command runner stay small by delegating this bounded
control-plane surface here.  Domain decisions remain in ``control.service``;
this module owns terminal I/O and short-lived SQLite writers.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import NoReturn

from ctrlrtn.cli.render import (
    render_experiment_started,
    render_experiments,
    render_fallbacks,
    render_routes,
)
from ctrlrtn.config import load_settings
from ctrlrtn.control.service import (
    ControlNotice,
    apply_adoption,
    prepare_adoption,
    prepare_route_change,
    provider_error,
)
from ctrlrtn.control_config import (
    ControlConfig,
    ControlConfigError,
    config_diff,
    live_config,
    load_control_config,
    verify_git_revision,
)
from ctrlrtn.policy.experiment import Experiment
from ctrlrtn.policy.fallback import approved_fallback_from_replay
from ctrlrtn.policy.route import route_savings
from ctrlrtn.policy.shadow import ShadowExperiment
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore

DatabasePath = Callable[[], str]
Fail = Callable[[str], NoReturn]


class ControlCommands:
    """Bound CLI handlers with explicit runtime dependencies."""

    def __init__(self, database_path: DatabasePath, fail: Fail) -> None:
        self._database_path = database_path
        self._fail = fail

    @staticmethod
    def _snapshot_note() -> str:
        return "Takes effect on the gateway within ~10s (snapshot refresh)."

    @staticmethod
    def _provider_exists(name: str) -> bool:
        resolver = load_settings().resolver()
        return resolver.resolve_provider(name, "/") is not None

    def _require_provider(self, name: str | None) -> None:
        """Fail clearly when a command names no configured provider."""

        error = provider_error(name, self._provider_exists)
        if error:
            self._fail(error)

    @staticmethod
    def _print_control_notices(notices: Iterable[ControlNotice]) -> None:
        for notice in notices:
            print(f"Note: {notice.message}.", file=sys.stderr)

    def _experiment_start(self, args: argparse.Namespace) -> None:
        self._require_provider(args.provider)
        fields = {
            "use_case_key": args.use_case,
            "candidate_model": args.candidate,
            "candidate_provider": args.provider,
            "split_pct": args.split,
            "max_calls_per_task": args.max_calls,
            "workflow": args.workflow,
            "workflow_version": args.workflow_version,
            "step": args.step,
        }
        if args.id:
            fields["experiment_id"] = args.id
        try:
            experiment = Experiment(**fields)
        except ValueError as exc:
            self._fail(f"invalid experiment: {exc}")
        store = SqliteTraceStore(self._database_path())
        try:
            try:
                store.create_experiment(experiment)
            except ValueError as exc:
                self._fail(str(exc))
        finally:
            store.close()
        print(render_experiment_started(experiment))

    def _experiment_list(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            print(render_experiments(store.experiments(args.limit)))
        finally:
            store.close()

    def _experiment_stop(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            stopped = store.stop_experiment(args.experiment_id)
        finally:
            store.close()
        if not stopped:
            self._fail(
                f"No running experiment with id {args.experiment_id!r} "
                "(already stopped or unknown)."
            )
        print(f"Stopped experiment {args.experiment_id}.")

    def _shadow_start(self, args: argparse.Namespace) -> None:
        self._require_provider(args.provider)
        try:
            experiment = ShadowExperiment(
                use_case_key=args.use_case,
                candidate_model=args.candidate,
                candidate_provider=args.provider,
                sample_pct=args.sample,
                workflow=args.workflow,
                workflow_version=args.workflow_version,
                step=args.step,
                **({"shadow_id": args.id} if args.id else {}),
            )
        except ValueError as exc:
            self._fail(f"invalid shadow experiment: {exc}")
        store = SqliteTraceStore(self._database_path())
        try:
            try:
                store.create_shadow_experiment(experiment)
            except ValueError as exc:
                self._fail(str(exc))
        finally:
            store.close()
        print(
            f"Started {experiment.shadow_id}: mirror {experiment.sample_pct}% of "
            f"{experiment.scope.label} to {experiment.candidate_model}; no shadow "
            "output is served to users."
        )
        print(self._snapshot_note())

    def _shadow_list(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            rows = store.shadow_experiments(args.limit)
            if not rows:
                print("No shadow experiments.")
                return
            print(
                "id                    use-case             candidate              "
                "sample  state    done/fail/drop"
            )
            for row in rows:
                stats = store.shadow_stats(row.shadow_id)
                counts = (
                    f"{stats.completed}/{stats.failed}/{stats.dropped}"
                    if stats
                    else "-/-/-"
                )
                print(
                    f"{row.shadow_id[:21]:<21} {row.scope.label[:20]:<20} "
                    f"{row.candidate_model[:22]:<22} {row.sample_pct:>3}%   "
                    f"{row.status:<8} {counts}"
                )
        finally:
            store.close()

    def _shadow_stop(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            stopped = store.stop_shadow_experiment(args.shadow_id)
        finally:
            store.close()
        if not stopped:
            self._fail(
                f"No running shadow experiment with id {args.shadow_id!r}."
            )
        print(f"Stopped shadow experiment {args.shadow_id}.")

    def _route_set(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            try:
                plan = prepare_route_change(
                    store,
                    use_case_key=args.use_case,
                    model=args.model,
                    previous_model=args.previous,
                    note=args.note,
                    provider=args.provider,
                    provider_exists=self._provider_exists,
                )
            except ValueError as exc:
                self._fail(str(exc))
            self._print_control_notices(plan.notices)
            store.set_route(plan.route)
        finally:
            store.close()
        previous = plan.route.previous_model
        print(
            f"Routing {args.use_case} -> {args.model}"
            + (f" (was {previous})." if previous else ".")
        )
        if previous is None:
            print(
                "Note: could not infer the model this replaces (no recorded "
                "traffic); savings won't be computable. Re-set with --previous "
                "to fix.",
                file=sys.stderr,
            )
        if plan.dormant_experiment_id is not None:
            print(
                f"Note: experiment {plan.dormant_experiment_id} is RUNNING on "
                "this use-case and takes precedence — the route stays dormant "
                "until it stops (`ctrlrtn experiment stop "
                f"{plan.dormant_experiment_id}`).",
                file=sys.stderr,
            )
        print(self._snapshot_note())

    def _route_clear(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            cleared = store.clear_route(args.use_case)
        finally:
            store.close()
        if not cleared:
            self._fail(f"No route for use-case {args.use_case!r}.")
        print(f"Cleared route for {args.use_case}; back to pass-through.")
        print(self._snapshot_note())

    def _route_adopt(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            try:
                plan = prepare_adoption(
                    store,
                    args.experiment_id,
                    provider_exists=self._provider_exists,
                )
                self._print_control_notices(plan.notices)
                applied = apply_adoption(store, plan)
            except ValueError as exc:
                self._fail(str(exc))
            if not applied:
                self._fail(f"No experiment with id {args.experiment_id!r}.")
        finally:
            store.close()
        experiment = plan.experiment
        previous = plan.route.previous_model
        if experiment.is_running:
            print(f"Stopped experiment {experiment.experiment_id}.")
        print(
            f"Routing {experiment.use_case_key} -> {experiment.candidate_model}"
            + (f" (was {previous})." if previous else ".")
        )
        print(self._snapshot_note())

    def _route_list(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            rows = []
            for route in store.routes():
                usage = store.use_case_usage_since(
                    route.use_case_key, route.ts, swapped_to=route.model
                )
                rows.append(
                    {
                        "route": route,
                        "usage": usage,
                        "saved": route_savings(usage, route.previous_model),
                    }
                )
        finally:
            store.close()
        print(render_routes(rows))

    def _fallback_approve(self, args: argparse.Namespace) -> None:
        self._require_provider(args.provider)
        try:
            with open(args.evidence, encoding="utf-8") as handle:
                evidence = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self._fail(
                f"could not read fallback evidence {args.evidence!r}: {exc}"
            )
        if not isinstance(evidence, dict):
            self._fail("fallback evidence must be a JSON object")
        try:
            fallback = approved_fallback_from_replay(
                evidence, provider=args.provider
            )
        except ValueError as exc:
            self._fail(f"invalid fallback evidence: {exc}")
        store = SqliteTraceStore(self._database_path())
        try:
            store.set_fallback(fallback)
        finally:
            store.close()
        print(
            f"Approved {fallback.use_case_key} -> {fallback.model} "
            "from NON_INFERIOR replay evidence."
        )
        print(self._snapshot_note())

    def _fallback_list(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            fallbacks = store.fallbacks()
        finally:
            store.close()
        print(render_fallbacks(fallbacks))

    def _fallback_clear(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            cleared = store.clear_fallback(args.use_case)
        finally:
            store.close()
        if not cleared:
            self._fail(f"No approved fallback for use-case {args.use_case!r}.")
        print(f"Cleared approved fallback for {args.use_case}.")
        print(self._snapshot_note())

    def _routing_config_validate(self, args: argparse.Namespace) -> None:
        config = load_control_config(args.path)
        self._validate_control_providers(config)
        print(
            f"Valid routing config {args.path}: {len(config.routes)} route(s), "
            f"{len(config.workflow_routes)} workflow route(s), "
            f"{len(config.workflows)} workflow definition(s), "
            f"{len(config.experiments)} running experiment(s)."
        )

    def _routing_config_status(self, args: argparse.Namespace) -> None:
        store = SqliteTraceStore(self._database_path())
        try:
            revision = store.control_revision()
            routes = store.routes()
            workflow_routes = store.workflow_routes()
            workflows = store.workflow_definitions()
            experiments = list(store.running_experiments().values())
        finally:
            store.close()
        if revision is None:
            print("No Git-backed routing config has been activated.")
        else:
            activated = datetime.fromtimestamp(
                revision.activated_at, tz=UTC
            ).isoformat()
            print(f"revision:  {revision.revision}")
            print(f"source:    {revision.source_path}")
            print(f"sha256:    {revision.document_sha256}")
            print(f"activated: {activated}")
        print(
            f"active:    {len(routes)} route(s), "
            f"{len(workflow_routes)} workflow route(s), "
            f"{len(workflows)} workflow definition(s), "
            f"{len(experiments)} experiment(s)"
        )

    def _routing_config_diff(self, args: argparse.Namespace) -> None:
        desired = load_control_config(args.path)
        store = SqliteTraceStore(self._database_path())
        try:
            current = live_config(
                store.routes(),
                store.experiments(limit=10000),
                store.workflow_routes(),
                store.workflow_definitions(),
            )
        finally:
            store.close()
        diff = config_diff(current, desired)
        print(diff if diff else "No routing changes.", end="" if diff else "\n")

    @staticmethod
    def _validate_control_providers(config: ControlConfig) -> None:
        resolver = load_settings().resolver()
        providers = (
            {
                route.provider
                for route in config.routes
                if route.provider is not None
            }
            | {
                route.provider
                for route in config.workflow_routes
                if route.provider is not None
            }
            | {
                experiment.candidate_provider
                for experiment in config.experiments
                if experiment.candidate_provider is not None
            }
        )
        for provider in sorted(providers):
            if resolver.resolve_provider(provider, "/") is None:
                raise ControlConfigError(
                    f"unknown provider {provider!r}; configure it under providers:"
                )

    def _routing_config_activate(self, args: argparse.Namespace) -> None:
        config = load_control_config(args.path)
        self._validate_control_providers(config)
        revision = verify_git_revision(args.repo, args.path)
        store = SqliteTraceStore(self._database_path())
        try:
            try:
                store.activate_control_config(config, revision)
            except sqlite3.IntegrityError as exc:
                raise ControlConfigError(
                    "could not activate routing config: an experiment id was "
                    "already used; changed experiments need a new stable id "
                    f"({exc})"
                ) from None
        finally:
            store.close()
        print(
            f"Activated {revision.source_path} at {revision.revision[:12]}: "
            f"{len(config.routes)} route(s), "
            f"{len(config.workflow_routes)} workflow route(s), "
            f"{len(config.workflows)} workflow definition(s), "
            f"{len(config.experiments)} experiment(s)."
        )
        print(self._snapshot_note())
