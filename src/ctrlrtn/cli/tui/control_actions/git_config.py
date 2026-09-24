"""Experiment, shadow, routing, and Git-config TUI actions."""

from __future__ import annotations

import sqlite3

from ctrlrtn.cli.tui.forms import (
    ConfirmScreen,
    GitConfigScreen,
    LiveExperimentScreen,
    RouteScreen,
    ShadowExperimentScreen,
    _provider_exists,
    _unknown_provider,
)
from ctrlrtn.control.service import (
    AdoptionPlan,
    apply_adoption,
    prepare_adoption,
    prepare_route_change,
)
from ctrlrtn.control_config import (
    ControlConfig,
    ControlConfigError,
    ControlRevision,
    config_diff,
    live_config,
    load_control_config,
    verify_git_revision,
)
from ctrlrtn.policy.experiment import (
    Experiment,
)
from ctrlrtn.policy.route import Route
from ctrlrtn.policy.shadow import ShadowExperiment


class GitConfigControlActions:
    """Review and activate Git-backed control configuration."""

    def action_git_config(self) -> None:
        try:
            desired = load_control_config(self._routing_config_path)
            for provider in {
                *(r.provider for r in desired.routes if r.provider is not None),
                *(
                    r.provider
                    for r in desired.workflow_routes
                    if r.provider is not None
                ),
                *(
                    e.candidate_provider
                    for e in desired.experiments
                    if e.candidate_provider is not None
                ),
            }:
                provider_error = _unknown_provider(provider)
                if provider_error:
                    raise ControlConfigError(provider_error)
            revision = verify_git_revision(
                self._routing_repo, self._routing_config_path
            )
            current = live_config(
                self._store.routes(),
                self._experiments,
                self._store.workflow_routes(),
                self._store.workflow_definitions(),
            )
            diff = config_diff(current, desired) or "No routing changes.\n"
        except (ControlConfigError, OSError) as exc:
            self._notice(str(exc), error=True)
            return
        message = (
            f"revision: {revision.revision}\n"
            f"source: {revision.source_path}\n"
            f"sha256: {revision.document_sha256}\n\n{diff}"
        )
        self.push_screen(
            GitConfigScreen(message),
            lambda confirmed: self._activate_git_config(
                desired, revision, confirmed
            ),
        )

    def _activate_git_config(
        self,
        config: ControlConfig,
        revision: ControlRevision,
        confirmed: bool,
    ) -> None:
        if not confirmed:
            return
        # Re-verify immediately before writing: the file may have changed while
        # its preview modal was open. Never activate a stale preview.
        try:
            fresh = verify_git_revision(
                self._routing_repo, self._routing_config_path
            )
            if (
                fresh.revision != revision.revision
                or fresh.document_sha256 != revision.document_sha256
            ):
                raise ControlConfigError(
                    "routing config changed after preview; review it again"
                )
            store = self._control_store()
            try:
                store.activate_control_config(config, fresh)
            finally:
                store.close()
        except (ControlConfigError, ValueError, sqlite3.Error, OSError) as exc:
            self._notice(
                f"could not activate routing config: {exc}", error=True
            )
            return
        self._notice(
            f"Activated {fresh.source_path} at {fresh.revision[:12]}; "
            "gateway updates within ~10s."
        )
        self._reload()
