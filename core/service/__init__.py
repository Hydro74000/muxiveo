"""Couche de service : opérations Muxiveo indépendantes du transport.

Chaque fonctionnalité est une opération (requête JSON typée → événements → résultat JSON).
Le CLI en est un adaptateur (``cli/service_adapter.py``) ; un futur daemon en sera un autre.
Le paquet n'importe ni argparse, ni ``cli``, et ne crée aucune application Qt.
"""

from core.service.context import CancelToken, ExecutionContext, Overrides
from core.service.errors import ApiError, ErrorCode, ErrorDetail
from core.service.events import (
    EVENT_VERSION,
    CallbackSink,
    Emitter,
    Event,
    EventSink,
    EventType,
    JobState,
    ListSink,
    NullSink,
)
from core.service.manager import JobInfo, JobManager, JobSummary
from core.service.models import Model, field
from core.service.operations import API_VERSION, Catalog, OperationSpec, call, execute, operation

__all__ = [
    "API_VERSION",
    "EVENT_VERSION",
    "ApiError",
    "CallbackSink",
    "CancelToken",
    "Catalog",
    "Emitter",
    "ErrorCode",
    "ErrorDetail",
    "Event",
    "EventSink",
    "EventType",
    "ExecutionContext",
    "JobInfo",
    "JobManager",
    "JobState",
    "JobSummary",
    "ListSink",
    "Model",
    "NullSink",
    "OperationSpec",
    "Overrides",
    "call",
    "execute",
    "field",
    "operation",
]
