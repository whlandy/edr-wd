"""Target-side desktop recording wire contracts."""

from .models import (
    RAW_RECORDING_SCHEMA,
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RawRecording,
    RecordingModelError,
)
from .session import CaptureSessionState, RecordingSession, RecordingSessionManager, SessionReceipt
from .service import RecordingService, source_factory_for_backend

__all__ = [
    "RAW_RECORDING_SCHEMA",
    "CaptureScope",
    "ObservedTarget",
    "RawCaptureEvent",
    "RawRecording",
    "RecordingModelError",
    "CaptureSessionState", "RecordingSession", "RecordingSessionManager", "SessionReceipt",
    "RecordingService", "source_factory_for_backend",
]
