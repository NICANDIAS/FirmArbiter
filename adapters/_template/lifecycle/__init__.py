# adapters/_template/lifecycle/__init__.py
"""
Shared lifecycle primitives for VERITAS adapters. An adapter author should
only ever need to import from here, not reach into individual files.
"""

from .events import EventWriter, EventContractError
from .heartbeat import HeartbeatWorker
from .request import load_and_validate_request, RequestContractError
from .signals import ShutdownCoordinator

__all__ = [
    "EventWriter", "EventContractError",
    "HeartbeatWorker",
    "load_and_validate_request", "RequestContractError",
    "ShutdownCoordinator",
]
