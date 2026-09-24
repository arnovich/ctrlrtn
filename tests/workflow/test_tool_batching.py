"""Tool batching remains adapter-specific and preserves every operation."""

from ctrlrtn.workflow.identity import WorkflowIdentity
from ctrlrtn.workflow.tool_batching import (
    ToolBatchCapabilities,
    ToolBatchItemResult,
    ToolBatchOperation,
    run_tool_batch,
)
from ctrlrtn.workflow.tool_operation import ToolOperationIdentity


def _operation(index: int, *, effect: str = "idempotent") -> ToolBatchOperation:
    workflow = WorkflowIdentity("task", "flow", "v1", "tools", "step-run")
    identity = ToolOperationIdentity(
        workflow,
        "lookup",
        f"lookup:{index}",
        f"attempt:{index}",
        effect=effect,
    )
    return ToolBatchOperation(identity, {"key": index})


class Adapter:
    capabilities = ToolBatchCapabilities(
        "fixture",
        10,
        True,
        True,
        True,
        True,
        True,
        True,
    )

    def __init__(self, *, fail: int | None = None, reverse: bool = False):
        self.fail = fail
        self.reverse = reverse

    def execute_batch(self, operations):
        rows = tuple(
            ToolBatchItemResult(
                operation.identity.attempt_id,
                index != self.fail,
                value=index if index != self.fail else None,
                error_code="lookup_failed" if index == self.fail else None,
            )
            for index, operation in enumerate(operations)
        )
        return tuple(reversed(rows)) if self.reverse else rows


def test_adapter_batch_preserves_order_and_per_operation_results():
    result = run_tool_batch(
        Adapter(),
        (_operation(0), _operation(1)),
        lambda: "original",
        opt_in=True,
    )
    assert result.mode == "batched"
    assert [item.attempt_id for item in result.items] == [
        "attempt:0",
        "attempt:1",
    ]


def test_partial_failure_is_attributed_and_original_is_not_replayed():
    originals = []
    result = run_tool_batch(
        Adapter(fail=1),
        (_operation(0), _operation(1)),
        lambda: originals.append(True),
        opt_in=True,
    )
    assert result.mode == "partial"
    assert result.items[1].error_code == "lookup_failed"
    assert originals == []


def test_unsafe_operations_or_incomplete_adapter_bypass_before_dispatch():
    unsafe = (_operation(0), _operation(1, effect="stateful"))
    assert (
        run_tool_batch(Adapter(), unsafe, lambda: "original", opt_in=True).mode
        == "bypass"
    )

    adapter = Adapter()
    adapter.capabilities = ToolBatchCapabilities(
        "unsafe", 10, True, True, False, True, True, True
    )
    result = run_tool_batch(
        adapter, (_operation(0), _operation(1)), lambda: "original", opt_in=True
    )
    assert result.mode == "bypass" and result.value == "original"


def test_bad_attribution_fails_closed_without_replaying_original():
    originals = []
    result = run_tool_batch(
        Adapter(reverse=True),
        (_operation(0), _operation(1)),
        lambda: originals.append(True),
        opt_in=True,
    )
    assert result.mode == "needs_operator"
    assert "not replayed" in result.reason
    assert originals == []
