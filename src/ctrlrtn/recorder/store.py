"""Compatibility imports for the recorder persistence API.

New code should import models, protocols, or a concrete store directly. This
module keeps the original public surface stable for existing users.
"""

from ctrlrtn.recorder.memory_store import InMemoryTraceStore
from ctrlrtn.recorder.models import (
    UNKEYED,
    UNSESSIONED,
    UNTASKED,
    ModelRanking,
    Outcome,
    SessionSummary,
    TaskSummary,
    UseCaseRanking,
)
from ctrlrtn.recorder.protocols import ExperimentStore, TraceStore
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore

_UNKEYED = UNKEYED
_UNSESSIONED = UNSESSIONED
_UNTASKED = UNTASKED

__all__ = [
    "ExperimentStore",
    "InMemoryTraceStore",
    "ModelRanking",
    "Outcome",
    "SessionSummary",
    "SqliteTraceStore",
    "TaskSummary",
    "TraceStore",
    "UseCaseRanking",
]
