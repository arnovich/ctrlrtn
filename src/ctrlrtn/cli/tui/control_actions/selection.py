"""Experiment, shadow, routing, and Git-config TUI actions."""

from __future__ import annotations

from ctrlrtn.policy.experiment import (
    Experiment,
)


class ControlSelectionMixin:
    """Resolve the current use-case and experiment selections."""

    def _selected_use_case(self) -> str:
        if self._active_table() == "usecases":
            return self._selected.get("usecases") or ""
        if self._active_table() == "experiments":
            experiment_id = self._selected.get("experiments")
            exp = next(
                (
                    e
                    for e in self._experiments
                    if e.experiment_id == experiment_id
                ),
                None,
            )
            if exp is not None:
                return exp.use_case_key
        if self._active_table() == "shadows":
            shadow_id = self._selected.get("shadows")
            shadow = next(
                (s for s in self._shadows if s.shadow_id == shadow_id), None
            )
            if shadow is not None:
                return shadow.use_case_key
        return self._rankings[0].use_case if self._rankings else ""

    def _selected_experiment(self) -> Experiment | None:
        if self._active_table() != "experiments":
            return None
        experiment_id = self._selected.get("experiments")
        return next(
            (e for e in self._experiments if e.experiment_id == experiment_id),
            None,
        )
