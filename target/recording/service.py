"""Recording management facade used by MCP tools and tests."""

from __future__ import annotations

import uuid
from typing import Callable

from .models import CaptureScope, RecordingModelError
from .session import CaptureSource, RecordingSession, RecordingSessionManager
from .indicator import IndicatorCallbacks, NullRecordingIndicator, RecordingIndicator
from .evidence import RecordingEvidenceStore


def unavailable_source_factory(
    scope: CaptureScope,
    sink: Callable,
) -> CaptureSource:
    del sink
    raise RecordingModelError(
        "recording_capture_unavailable",
        f"desktop capture source is not available for backend {scope.backend!r}",
        path="scope.backend",
    )


class RecordingService:
    def __init__(
        self,
        *,
        target_name: str,
        backend: str,
        source_factory: Callable[[CaptureScope, Callable], CaptureSource] = unavailable_source_factory,
        manager: RecordingSessionManager | None = None,
        indicator_factory: Callable[[str, CaptureScope, IndicatorCallbacks], RecordingIndicator] | None = None,
        backend_object=None,
        evidence_store: RecordingEvidenceStore | None = None,
    ) -> None:
        self.target_name = target_name
        self.backend = backend
        self.source_factory = source_factory
        self.manager = manager or RecordingSessionManager()
        self.indicator_factory = indicator_factory or (
            lambda _name, _scope, _callbacks: NullRecordingIndicator()
        )
        self.evidence_store = evidence_store or (
            RecordingEvidenceStore(backend_object) if backend_object is not None else None
        )

    def start(self, *, name: str, process_name: str, window_title: str, lease_seconds: float = 300.0) -> dict:
        if not all(isinstance(x, str) and x.strip() for x in (name, process_name, window_title)):
            raise RecordingModelError("recording_scope_not_unique", "name, process_name, and window_title are required", path="scope")
        scope = CaptureScope(self.target_name, self.backend, process_name, window_title)
        evidence_initialization = None
        session = RecordingSession(
            "REC-" + uuid.uuid4().hex,
            name,
            scope,
            evidence_attacher=(self.evidence_store.attach if self.evidence_store else None),
            lease_seconds=lease_seconds,
        )
        source = self.source_factory(scope, session.ingest)
        session.attach_source(source)
        indicator = self.indicator_factory(
            name,
            scope,
            IndicatorCallbacks(
                pause=session.pause,
                resume=session.resume,
                stop=lambda: session.stop("indicator"),
                add_assertion=session.add_assertion,
            ),
        )
        session.attach_indicator(indicator)
        def initialize_evidence() -> None:
            nonlocal evidence_initialization
            if self.evidence_store is not None:
                self.evidence_store.clear()
                evidence_initialization = self.evidence_store.initialize()
                if not evidence_initialization.get("ok"):
                    raise RecordingModelError(
                        "recording_capture_evidence_unavailable",
                        str(evidence_initialization.get("error") or "INIT screenshot unavailable"),
                        path="recording.evidenceInitialization",
                    )

        result = {
            "ok": True,
            **self.manager.start(session, before_start=initialize_evidence).to_dict(),
            "scope": scope.to_dict(),
            # Which of the application's existing windows the capture admitted.
            # An empty list next to a busy application is the visible symptom
            # of input being silently refused as out of scope.
            "seededScope": list(getattr(source, "seeded_scope", ()) or ()),
            # Which processes count as this application. An empty list beside
            # a multi-process product is the visible symptom of a flow that
            # crosses into a sibling process being refused.
            "scopeProcessNames": list(
                getattr(getattr(source, "_correlator", None), "scope_process_names", ())
                or ()
            ),
            "seededScopeError": getattr(
                getattr(source, "_correlator", None), "scope_seed_error", None,
            ),
        }
        if evidence_initialization is not None:
            result["evidenceInitialization"] = evidence_initialization
        return result

    def status(self, *, heartbeat: bool = False) -> dict:
        session = self.manager.current()
        receipt = session.heartbeat() if heartbeat else session.status()
        return {"ok": True, **receipt.to_dict(), "scope": session.scope.to_dict(), "name": session.name}

    def pause(self) -> dict:
        return {"ok": True, **self.manager.current().pause().to_dict()}

    def resume(self) -> dict:
        return {"ok": True, **self.manager.current().resume().to_dict()}

    def stop(self) -> dict:
        receipt, recording = self.manager.current().stop()
        return {"ok": True, **receipt.to_dict(), "recording": recording.to_dict()}

    def assertion(self) -> dict:
        return {"ok": True, **self.manager.current().request_assertion_editor().to_dict()}

    def capture(self, *, capture_id: str) -> dict:
        if self.evidence_store is None:
            raise RecordingModelError(
                "recording_capture_missing", "recording capture evidence is unavailable"
            )
        return self.evidence_store.get(capture_id)


def error_result(exc: Exception) -> dict:
    if isinstance(exc, RecordingModelError):
        return {"ok": False, "code": exc.code, "error": str(exc), "path": exc.path}
    return {"ok": False, "code": "recording_internal_error", "error": str(exc)}


def source_factory_for_backend(
    backend: str,
    backend_object=None,
) -> Callable[[CaptureScope, Callable], CaptureSource]:
    if backend == "windows_pywinauto":
        from .windows import windows_source_factory

        return windows_source_factory(backend_object)
    if backend == "macos_accessibility":
        from .macos import macos_source_factory

        return macos_source_factory(backend_object)
    return unavailable_source_factory
