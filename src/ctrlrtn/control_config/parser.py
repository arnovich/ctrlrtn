"""Parse and validate Git-backed desired-state YAML documents."""

from __future__ import annotations

import yaml

from ctrlrtn.control_config.models import (
    VERSION,
    ControlConfig,
    ControlConfigError,
    WorkflowDefinition,
    WorkflowStepDefinition,
)
from ctrlrtn.policy.experiment import (
    DEFAULT_MAX_CALLS_PER_TASK,
    DEFAULT_MAX_COST_USD_PER_TASK,
    Experiment,
)
from ctrlrtn.policy.route import Route, WorkflowRoute


def _mapping(value: object, label: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ControlConfigError(f"{label} must be a mapping")
    return value


def _required(fields: dict, key: str, message: str) -> str:
    """Return a present field so the parser fails before the model would."""
    value = fields.get(key)
    if value is None:
        raise ControlConfigError(message)
    return value


def _fields(raw: object, label: str, allowed: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise ControlConfigError(f"{label} must be a mapping")
    unknown = set(raw) - allowed
    if unknown:
        raise ControlConfigError(
            f"unknown {label} key(s): {', '.join(sorted(unknown))}"
        )
    return raw


def load_control_config(path: str) -> ControlConfig:
    try:
        with open(path, encoding="utf-8") as handle:
            raw_bytes = handle.read()
    except FileNotFoundError:
        raise ControlConfigError(f"routing config not found: {path}") from None
    try:
        data = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        raise ControlConfigError(f"invalid YAML in {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ControlConfigError(f"routing config {path} must be a mapping")
    unknown = set(data) - {
        "version",
        "routes",
        "experiments",
        "workflow_routes",
        "workflows",
    }
    if unknown:
        raise ControlConfigError(
            f"unknown top-level key(s): {', '.join(sorted(unknown))}"
        )
    if data.get("version") != VERSION:
        raise ControlConfigError(
            f"routing config version must be {VERSION} (got {data.get('version')!r})"
        )

    routes = []
    for use_case, value in _mapping(data.get("routes"), "routes").items():
        if not isinstance(use_case, str) or not use_case:
            raise ControlConfigError(
                "route use-case keys must be non-empty strings"
            )
        fields = _fields(
            value,
            f"route {use_case!r}",
            {"model", "provider", "previous_model", "note"},
        )
        model = _required(
            fields,
            "model",
            f"invalid route {use_case!r}: model must be non-empty",
        )
        try:
            routes.append(
                Route(
                    use_case_key=use_case,
                    model=model,
                    provider=fields.get("provider"),
                    previous_model=fields.get("previous_model"),
                    note=fields.get("note"),
                    ts=0.0,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ControlConfigError(
                f"invalid route {use_case!r}: {exc}"
            ) from None

    experiments = []
    for use_case, value in _mapping(
        data.get("experiments"), "experiments"
    ).items():
        if not isinstance(use_case, str) or not use_case:
            raise ControlConfigError(
                "experiment use-case keys must be non-empty strings"
            )
        fields = _fields(
            value,
            f"experiment {use_case!r}",
            {
                "id",
                "candidate_model",
                "provider",
                "split_pct",
                "max_calls_per_task",
                "max_cost_usd_per_task",
                "workflow",
                "workflow_version",
                "step",
            },
        )
        if not fields.get("id"):
            raise ControlConfigError(
                f"experiment {use_case!r} requires a stable non-empty id"
            )
        candidate_model = _required(
            fields,
            "candidate_model",
            f"invalid experiment {use_case!r}: candidate_model is required",
        )
        try:
            experiments.append(
                Experiment(
                    experiment_id=fields["id"],
                    use_case_key=use_case,
                    candidate_model=candidate_model,
                    candidate_provider=fields.get("provider"),
                    split_pct=fields.get("split_pct", 50),
                    max_calls_per_task=fields.get(
                        "max_calls_per_task", DEFAULT_MAX_CALLS_PER_TASK
                    ),
                    max_cost_usd_per_task=fields.get(
                        "max_cost_usd_per_task",
                        DEFAULT_MAX_COST_USD_PER_TASK,
                    ),
                    created_epoch=0.0,
                    workflow=fields.get("workflow"),
                    workflow_version=fields.get("workflow_version"),
                    step=fields.get("step"),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ControlConfigError(
                f"invalid experiment {use_case!r}: {exc}"
            ) from None
    workflows = []
    declared: dict[tuple[str, str], set[str]] = {}
    for workflow, versions_value in _mapping(
        data.get("workflows"), "workflows"
    ).items():
        if not isinstance(workflow, str) or not workflow:
            raise ControlConfigError("workflow names must be non-empty strings")
        for version, value in _mapping(
            versions_value, f"workflow {workflow!r}"
        ).items():
            if not isinstance(version, str) or not version:
                raise ControlConfigError(
                    "workflow versions must be non-empty strings"
                )
            fields = _fields(
                value, f"workflow {workflow!r}@{version!r}", {"steps"}
            )
            raw_steps = _mapping(fields.get("steps"), "steps")
            if not raw_steps:
                raise ControlConfigError(
                    f"workflow {workflow!r}@{version!r} requires steps"
                )
            names = set(raw_steps)
            steps = []
            for step, step_value in raw_steps.items():
                if not isinstance(step, str) or not step:
                    raise ControlConfigError(
                        "workflow step names must be non-empty strings"
                    )
                step_fields = _fields(
                    step_value or {},
                    f"workflow step {workflow!r}@{version!r}/{step!r}",
                    {"predecessors", "allows", "condition"},
                )
                predecessors = step_fields.get("predecessors", [])
                if not isinstance(predecessors, list) or not all(
                    isinstance(item, str) and item for item in predecessors
                ):
                    raise ControlConfigError(
                        "predecessors must be an array of step names"
                    )
                if set(predecessors) - names or step in predecessors:
                    raise ControlConfigError(
                        f"invalid predecessors for workflow step {step!r}"
                    )
                allows = _fields(
                    step_fields.get("allows") or {},
                    f"allows for workflow step {step!r}",
                    {"fan_out", "retry"},
                )
                if any(not isinstance(flag, bool) for flag in allows.values()):
                    raise ControlConfigError(
                        "workflow capability flags must be booleans"
                    )
                condition = step_fields.get("condition")
                if condition is not None and (
                    not isinstance(condition, str) or not condition
                ):
                    raise ControlConfigError(
                        "condition must be a non-empty label"
                    )
                steps.append(
                    WorkflowStepDefinition(
                        step,
                        tuple(predecessors),
                        allows.get("fan_out", False),
                        allows.get("retry", False),
                        condition,
                    )
                )
            workflows.append(
                WorkflowDefinition(workflow, version, tuple(steps))
            )
            declared[(workflow, version)] = names

    for experiment in experiments:
        # ExperimentScope sets workflow and workflow_version together, so
        # this is exactly "not workflow-scoped".
        if experiment.workflow is None or experiment.workflow_version is None:
            continue
        declared_steps = declared.get(
            (experiment.workflow, experiment.workflow_version)
        )
        if declared_steps is None:
            raise ControlConfigError(
                f"experiment {experiment.experiment_id!r} scope is not "
                "a declared workflow version"
            )
        if (
            experiment.scope.is_step_scoped
            and experiment.step not in declared_steps
        ):
            raise ControlConfigError(
                f"experiment {experiment.experiment_id!r} scope is not "
                "a declared workflow step"
            )

    workflow_routes = []
    seen: set[tuple[str, str, str | None]] = set()
    raw_workflows = _mapping(data.get("workflow_routes"), "workflow_routes")
    for workflow, versions_value in raw_workflows.items():
        if not isinstance(workflow, str) or not workflow:
            raise ControlConfigError(
                "workflow route names must be non-empty strings"
            )
        versions = _mapping(versions_value, f"workflow route {workflow!r}")
        for version, value in versions.items():
            if not isinstance(version, str) or not version:
                raise ControlConfigError(
                    "workflow route versions must be non-empty strings"
                )
            fields = _fields(
                value,
                f"workflow route {workflow!r}@{version!r}",
                {"model", "provider", "note", "steps"},
            )
            definitions: list[tuple[str | None, dict]] = [(None, fields)]
            for step, step_value in _mapping(
                fields.get("steps"), "steps"
            ).items():
                if not isinstance(step, str) or not step:
                    raise ControlConfigError(
                        "workflow step keys must be non-empty strings"
                    )
                definitions.append(
                    (
                        step,
                        _fields(
                            step_value,
                            f"workflow step {workflow!r}@{version!r}/{step!r}",
                            {"model", "provider", "note"},
                        ),
                    )
                )
            for step, definition in definitions:
                if step is None and definition.get("model") is None:
                    continue
                key = (workflow, version, step)
                declared_steps = declared.get((workflow, version))
                if declared_steps is None:
                    raise ControlConfigError(
                        f"workflow route {workflow!r}@{version!r} has no "
                        "declared workflow"
                    )
                if step is not None and step not in declared_steps:
                    raise ControlConfigError(
                        f"workflow route step {step!r} is not declared"
                    )
                if key in seen:
                    raise ControlConfigError(
                        f"duplicate workflow route {key!r}"
                    )
                seen.add(key)
                model = _required(
                    definition,
                    "model",
                    f"invalid workflow route {key!r}: model must be non-empty",
                )
                try:
                    workflow_routes.append(
                        WorkflowRoute(
                            workflow=workflow,
                            workflow_version=version,
                            step=step,
                            model=model,
                            provider=definition.get("provider"),
                            note=definition.get("note"),
                            ts=0.0,
                        )
                    )
                except (TypeError, ValueError) as exc:
                    raise ControlConfigError(
                        f"invalid workflow route {key!r}: {exc}"
                    ) from None
    return ControlConfig(
        tuple(routes),
        tuple(experiments),
        tuple(workflow_routes),
        tuple(workflows),
    )
