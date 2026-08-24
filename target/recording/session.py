"""Target-local scoped recording-session lifecycle.

This module is platform-neutral. OS hook adapters enqueue already-correlated
events through :meth:`RecordingSession.ingest`; the session enforces scope,
state, sequence, lease, and source-redaction boundaries before persistence.
"""

from __future__ import annotations

import threading
import time
import hashlib
import json
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from .models import (
    ACTION_EVENT_TYPES,
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RawRecording,
    RecordingModelError,
)
from .indicator import NullRecordingIndicator, RecordingIndicator


class CaptureSessionState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class SessionReceipt:
    session_id: str
    state: CaptureSessionState
    sequence: int
    monotonic_ms: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"sessionId": self.session_id, "state": self.state.value, "sequence": self.sequence, "monotonicMs": self.monotonic_ms, "reason": self.reason}


class CaptureSource(Protocol):
    def start(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...


class NullCaptureSource:
    def start(self) -> None: pass
    def pause(self) -> None: pass
    def resume(self) -> None: pass
    def stop(self) -> None: pass


class RecordingSession:
    def __init__(
        self,
        session_id: str,
        name: str,
        scope: CaptureScope,
        *,
        source: CaptureSource | None = None,
        evidence_attacher: Callable[[RawCaptureEvent], RawCaptureEvent] | None = None,
        lease_seconds: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.session_id = session_id;self.name = name;self.scope = scope
        self._source = source or NullCaptureSource();self._lease_seconds = lease_seconds
        self._evidence_attacher = evidence_attacher
        self._indicator: RecordingIndicator = NullRecordingIndicator()
        self._monotonic = monotonic;self._state = CaptureSessionState.IDLE
        self._events: list[RawCaptureEvent] = [];self._last_heartbeat = monotonic()
        self._lock = threading.RLock();self._reason = ""
        # Set once the session reaches a terminal state, so a second stop()
        # can wait for an in-flight one instead of losing the recording.
        self._settled = threading.Event()
        self._last_action_causal_id: str | None = None

    @property
    def state(self) -> CaptureSessionState:
        self._expire_before_operation()
        with self._lock:
            return self._state

    def attach_source(self, source: CaptureSource) -> None:
        """Attach the platform source after its sink has been bound to ingest()."""
        with self._lock:
            if self._state is not CaptureSessionState.IDLE:
                raise RecordingModelError(
                    "recording_state_invalid",
                    "capture source can only be attached in idle state",
                    path="session.state",
                )
            self._source = source

    def attach_indicator(self, indicator: RecordingIndicator) -> None:
        with self._lock:
            if self._state is not CaptureSessionState.IDLE:
                raise RecordingModelError(
                    "recording_state_invalid",
                    "recording indicator can only be attached in idle state",
                    path="session.state",
                )
            self._indicator = indicator

    def _now_ms(self) -> int:
        return int(self._monotonic() * 1000)

    def _receipt(self) -> SessionReceipt:
        return SessionReceipt(self.session_id, self._state, len(self._events), self._now_ms(), self._reason)

    def _expire_before_operation(self) -> bool:
        """Stop an expired session without holding the ingest lock while draining."""
        with self._lock:
            expired = (
                self._state in {CaptureSessionState.RECORDING, CaptureSessionState.PAUSED}
                and self._monotonic() - self._last_heartbeat > self._lease_seconds
            )
            if not expired:
                return False
            self._state = CaptureSessionState.STOPPING
            self._reason = "recording_lease_expired"
        try:
            self._source.stop()
            self._indicator.stop()
        except Exception as exc:
            with self._lock:
                self._state = CaptureSessionState.FAILED
                self._settled.set()
                self._reason = f"recording_lease_expired: {exc}"
            return True
        with self._lock:
            self._state = CaptureSessionState.STOPPED
            self._settled.set()
        return True

    def start(self) -> SessionReceipt:
        with self._lock:
            if self._state is not CaptureSessionState.IDLE:
                raise RecordingModelError("recording_state_invalid", "start requires idle state", path="session.state")
            self._state = CaptureSessionState.STARTING
            try:
                self._source.start()
                self._indicator.start()
            except Exception as exc:
                try:
                    self._source.stop()
                except Exception:
                    pass
                self._state = CaptureSessionState.FAILED;self._reason = str(exc);self._settled.set();raise
            self._state = CaptureSessionState.RECORDING;self._last_heartbeat = self._monotonic();return self._receipt()

    def heartbeat(self) -> SessionReceipt:
        self._expire_before_operation()
        with self._lock:
            if self._state not in {CaptureSessionState.RECORDING, CaptureSessionState.PAUSED}:
                raise RecordingModelError("recording_state_invalid", "heartbeat requires an active session", path="session.state")
            self._last_heartbeat = self._monotonic();return self._receipt()

    def pause(self) -> SessionReceipt:
        self._expire_before_operation()
        with self._lock:
            if self._state is not CaptureSessionState.RECORDING:
                raise RecordingModelError("recording_state_invalid", "pause requires recording state", path="session.state")
            self._source.pause();self._indicator.pause();self._state = CaptureSessionState.PAUSED
            self._last_heartbeat = self._monotonic();return self._receipt()

    def resume(self) -> SessionReceipt:
        self._expire_before_operation()
        with self._lock:
            if self._state is not CaptureSessionState.PAUSED:
                raise RecordingModelError("recording_state_invalid", "resume requires paused state", path="session.state")
            self._source.resume();self._indicator.resume();self._state = CaptureSessionState.RECORDING;self._last_heartbeat = self._monotonic();return self._receipt()

    def ingest(self, event: RawCaptureEvent) -> bool:
        self._expire_before_operation()
        with self._lock:
            if self._state not in {CaptureSessionState.RECORDING, CaptureSessionState.STOPPING}:
                return False
            if event.scope != self.scope:
                return False
            if event.type == "key_command" and event.input.get("key") == "A":
                modifiers = set(event.input.get("modifiers") or [])
                if "SHIFT" in modifiers and modifiers.intersection({"CTRL", "META"}):
                    self._last_heartbeat = self._monotonic()
                    self._open_assertion_editor()
                    return False
            if event.type in {"key_down", "key_up", "pointer_move"}:
                return False
            if event.observed_target and event.observed_target.protected is True and "value" in event.input:
                raise RecordingModelError("recording_secret_plaintext", "protected input value must be source-redacted", path="event.input.value")
            expected = len(self._events) + 1
            if event.sequence != expected:
                raise RecordingModelError("sequence_invalid", f"expected sequence {expected}, got {event.sequence}", path="event.sequence")
            if self._evidence_attacher is not None:
                event = self._evidence_attacher(event)
            self._events.append(event);self._last_heartbeat = self._monotonic()
            if event.type in ACTION_EVENT_TYPES:
                self._last_action_causal_id = event.causal_id
            self._indicator.update_count(len(self._events));return True

    def request_assertion_editor(self) -> SessionReceipt:
        self._expire_before_operation()
        with self._lock:
            if self._state not in {CaptureSessionState.RECORDING, CaptureSessionState.PAUSED}:
                raise RecordingModelError(
                    "recording_state_invalid", "assertion editor requires an active session",
                    path="session.state",
                )
            self._open_assertion_editor()
            self._last_heartbeat = self._monotonic()
            return self._receipt()

    def _open_assertion_editor(self) -> None:
        current_target = getattr(self._source, "current_observed_target", None)
        target = current_target() if callable(current_target) else None
        if not isinstance(target, ObservedTarget):
            target = getattr(self._source, "last_observed_target", None)
        default = None
        if isinstance(target, ObservedTarget):
            default = {
                "automationId": target.automation_id,
                "identifier": target.identifier,
                "controlType": target.control_type,
                "name": target.text,
            }
        self._indicator.open_assertion_editor(default)

    def add_assertion(self, payload: Mapping[str, Any]) -> SessionReceipt:
        if "expected" not in payload:
            raise RecordingModelError(
                "compile_assertion_missing_expected", "assertion requires explicit expected",
                path="assertion.expected",
            )
        assertion_type = payload.get("assertion")
        if assertion_type not in {
            "text_equals", "text_contains", "value_equals", "visible",
            "checked", "enabled", "window_open", "text_contains_time",
            "window_text_contains", "window_text_contains_time",
        }:
            raise RecordingModelError(
                "recording_assertion_invalid", f"unsupported assertion {assertion_type!r}",
                path="assertion.assertion",
            )
        expected_value = payload["expected"]
        if assertion_type in {"text_contains_time", "window_text_contains_time"}:
            if not isinstance(expected_value, str) or not expected_value:
                raise RecordingModelError(
                    "recording_assertion_invalid",
                    f"{assertion_type} expected must be a non-empty strftime pattern",
                    path="assertion.expected",
                )
            try:
                datetime.now().strftime(expected_value)
            except (ValueError, TypeError) as exc:
                raise RecordingModelError(
                    "recording_assertion_invalid",
                    f"invalid strftime pattern: {exc}",
                    path="assertion.expected",
                ) from exc
        elif assertion_type in {
            "text_equals", "text_contains", "value_equals", "window_text_contains",
        }:
            if not isinstance(expected_value, str):
                raise RecordingModelError(
                    "recording_assertion_invalid",
                    f"{assertion_type} expected must be a string",
                    path="assertion.expected",
                )
        elif assertion_type in {"visible", "checked", "enabled"}:
            if not isinstance(expected_value, bool):
                raise RecordingModelError(
                    "recording_assertion_invalid",
                    f"{assertion_type} expected must be boolean",
                    path="assertion.expected",
                )
        elif assertion_type == "window_open":
            if not isinstance(expected_value, (bool, Mapping)):
                raise RecordingModelError(
                    "recording_assertion_invalid",
                    "window_open expected must be boolean or an object",
                    path="assertion.expected",
                )
            if isinstance(expected_value, Mapping):
                allowed_window_expected = {
                    "exists", "processName", "process_name", "title", "titleRegex",
                }
                unknown = set(expected_value) - allowed_window_expected
                if unknown or (
                    "exists" in expected_value
                    and not isinstance(expected_value["exists"], bool)
                ):
                    raise RecordingModelError(
                        "recording_assertion_invalid",
                        "window_open expected object has invalid fields",
                        path="assertion.expected",
                    )
        raw_timeout = payload.get("timeoutSeconds", 10.0)
        try:
            timeout_seconds = float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise RecordingModelError(
                "recording_assertion_invalid", "timeoutSeconds must be numeric",
                path="assertion.timeoutSeconds",
            ) from exc
        if not 0 < timeout_seconds <= 300:
            raise RecordingModelError(
                "recording_assertion_invalid", "timeoutSeconds must be in (0, 300]",
                path="assertion.timeoutSeconds",
            )
        bind_previous = payload.get("bindPrevious", False)
        if not isinstance(bind_previous, bool):
            raise RecordingModelError(
                "recording_assertion_invalid", "bindPrevious must be boolean",
                path="assertion.bindPrevious",
            )
        causal_id = None
        if bind_previous:
            causal_id = self._last_action_causal_id
            if causal_id is None:
                raise RecordingModelError(
                    "recording_assertion_unbound",
                    "no preceding recorded action is available to bind this verifier to",
                    path="assertion.bindPrevious",
                )
        identity = {
            "controlType": payload.get("controlType") or None,
            "automationId": payload.get("automationId") or None,
            "identifier": payload.get("identifier") or None,
            "text": payload.get("name") or None,
        }
        # Window-scoped assertions state what the screen must show, not which
        # widget shows it, so they never need a control identity.
        identity_free = {"window_open", "window_text_contains", "window_text_contains_time"}
        if assertion_type not in identity_free and not any(identity.values()):
            raise RecordingModelError(
                "recording_target_unresolved", "assertion requires a semantic control identity",
                path="assertion.selector",
            )
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        observed = None
        if assertion_type not in identity_free:
            observed = ObservedTarget(
                snapshot_id="OBS-" + uuid.uuid4().hex,
                target_id="T0001",
                fingerprint="sha256:" + digest,
                control_type=identity["controlType"],
                automation_id=identity["automationId"],
                identifier=identity["identifier"],
                text=identity["text"],
                protected=False,
            )
        event = RawCaptureEvent(
            sequence=len(self._events) + 1,
            wall_time=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            monotonic_ms=self._now_ms(),
            type="assertion",
            scope=self.scope,
            observed_target=observed,
            assertion={
                "type": assertion_type,
                "expected": expected_value,
                "timeoutSeconds": timeout_seconds,
            },
            causal_id=causal_id,
        )
        if not self.ingest(event):
            raise RecordingModelError(
                "recording_state_invalid", "assertion could not be added in current state",
                path="session.state",
            )
        return self._receipt()

    def stop(
        self, reason: str = "user", *, settle_timeout: float = 60.0,
    ) -> tuple[SessionReceipt, RawRecording]:
        self._expire_before_operation()
        with self._lock:
            state = self._state
            if state not in {CaptureSessionState.RECORDING, CaptureSessionState.PAUSED}:
                # A session that already ended still holds everything it
                # captured.  Refusing to hand that back is how a stop race
                # with the indicator's own Stop button costs the recording.
                if state in {CaptureSessionState.STOPPED, CaptureSessionState.FAILED}:
                    return self._receipt(), self.recording()
                if state is not CaptureSessionState.STOPPING:
                    raise RecordingModelError("recording_state_invalid", "stop requires an active session", path="session.state")
            else:
                self._state = CaptureSessionState.STOPPING
                self._reason = reason
                state = CaptureSessionState.RECORDING
        if state is CaptureSessionState.STOPPING:
            # Another caller is already draining this session; wait for it
            # and return the same document rather than raising.
            if not self._settled.wait(timeout=settle_timeout):
                raise RecordingModelError(
                    "recording_state_invalid",
                    f"a concurrent stop did not settle within {settle_timeout:g}s",
                    path="session.state",
                )
            with self._lock:
                return self._receipt(), self.recording()
        try:
            self._source.stop()
            self._indicator.stop()
        except Exception as exc:
            with self._lock:
                self._state = CaptureSessionState.FAILED
                self._settled.set()
                self._reason = str(exc)
            raise
        with self._lock:
            self._state = CaptureSessionState.STOPPED
            self._settled.set()
            return self._receipt(), self.recording()

    def status(self) -> SessionReceipt:
        self._expire_before_operation()
        with self._lock:
            return self._receipt()

    def recording(self) -> RawRecording:
        return RawRecording(
            self.session_id,
            self.name,
            tuple(self._events),
            {
                "droppedPackets": int(getattr(self._source, "dropped_packets", 0)),
                "outOfScopeEvents": int(getattr(self._source, "out_of_scope_events", 0)),
                "correlationErrorCount": len(
                    getattr(self._source, "correlation_errors", ())
                ),
            },
        )


class RecordingSessionManager:
    """Own at most one target-local session to prevent overlapping hooks."""

    def __init__(self) -> None:
        self._session: RecordingSession | None = None
        self._lock = threading.Lock()

    def start(
        self,
        session: RecordingSession,
        *,
        before_start: Callable[[], None] | None = None,
    ) -> SessionReceipt:
        with self._lock:
            if self._session and self._session.state in {CaptureSessionState.RECORDING, CaptureSessionState.PAUSED}:
                raise RecordingModelError("recording_session_active", "another recording session is active", path="session")
            if before_start is not None:
                before_start()
            self._session = session;return session.start()

    def current(self) -> RecordingSession:
        if self._session is None:
            raise RecordingModelError("recording_session_missing", "no recording session exists", path="session")
        return self._session
