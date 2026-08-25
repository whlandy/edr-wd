"""Strict target-to-agent wire models for desktop recording.

The target owns source scoping and source redaction.  These models deliberately
contain observation-local IDs as evidence only; the compiler must never copy them
into a replay selector.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping

RAW_RECORDING_SCHEMA = "edr.desktop-recording/v1"

#: Raw event types that compile into one executable action step.  A window
#: transition or an explicit verifier binds to the most recent event of one of
#: these types by sharing its ``causalId``.
ACTION_EVENT_TYPES = frozenset({
    "pointer_click", "pointer_double_click", "text_commit",
    "selection_change", "toggle_change", "scroll_commit", "drag_commit",
})

#: Action event types whose replay is meaningless without an explicit result
#: verifier.  The compiler keeps such a step incomplete until one is bound.
VERIFIER_REQUIRED_EVENT_TYPES = frozenset({"scroll_commit", "drag_commit"})


class RecordingModelError(ValueError):
    """Stable validation failure for recording wire data."""

    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.path = path


def _strict(data: Mapping[str, Any], allowed: set[str], path: str) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise RecordingModelError("type_error", f"{path} must be an object", path=path)
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise RecordingModelError(
            "unknown_field", f"{path} has unknown fields: {unknown}", path=path
        )
    return dict(data)


def _text(value: Any, path: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise RecordingModelError("type_error", f"{path} must be a string", path=path)
    return value


_CAPTURE_ID = re.compile(r"CAP-[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _validate_capture_metadata(value: Any, path: str) -> None:
    required = {"id", "sha256", "width", "height", "origin", "redacted", "redactions"}
    data = _strict(value, required, path)
    missing = sorted(required - set(data))
    if missing:
        raise RecordingModelError(
            "required_field_missing", f"{path} is missing fields: {missing}", path=path,
        )
    if not isinstance(data["id"], str) or _CAPTURE_ID.fullmatch(data["id"]) is None:
        raise RecordingModelError("type_error", f"{path}.id is invalid", path=f"{path}.id")
    if not isinstance(data["sha256"], str) or _SHA256.fullmatch(data["sha256"]) is None:
        raise RecordingModelError("type_error", f"{path}.sha256 is invalid", path=f"{path}.sha256")
    for key in ("width", "height"):
        if not isinstance(data[key], int) or isinstance(data[key], bool) or data[key] < 1:
            raise RecordingModelError("type_error", f"{path}.{key} must be a positive integer", path=f"{path}.{key}")
    origin = data["origin"]
    if not isinstance(origin, (list, tuple)) or len(origin) != 2 or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in origin
    ):
        raise RecordingModelError("type_error", f"{path}.origin must contain two integers", path=f"{path}.origin")
    if data["redacted"] is not True:
        raise RecordingModelError("type_error", f"{path}.redacted must be true", path=f"{path}.redacted")
    redactions = data["redactions"]
    if not isinstance(redactions, list) or not all(
        isinstance(rect, (list, tuple)) and len(rect) == 4
        and all(isinstance(item, int) and not isinstance(item, bool) for item in rect)
        for rect in redactions
    ):
        raise RecordingModelError("type_error", f"{path}.redactions must contain integer rectangles", path=f"{path}.redactions")


def _validate_evidence(value: Mapping[str, Any]) -> None:
    data = _strict(
        value,
        {
            "foregroundPid", "doubleClickIntervalMs", "beforeCapture",
            "capture", "captureError", "window",
        },
        "event.evidence",
    )
    window = data.get("window")
    if window is not None:
        # Which window the event happened in. A recorded flow moves between an
        # application's windows, and replay has to know which one each step
        # belongs to rather than assuming the entry window.
        #
        # `handle` (HWND / CGWindowNumber) only identifies the window within
        # this one live capture session — a fresh run of the application at
        # replay time gets a different handle — so it is diagnostic evidence
        # of which physical window produced the step, never a replay
        # selector field.
        fields = _strict(window, {"title", "processName", "handle"}, "event.evidence.window")
        for key in ("title", "processName"):
            if key in fields and not isinstance(fields[key], str):
                raise RecordingModelError(
                    "type_error", f"evidence.window.{key} must be a string",
                    path=f"event.evidence.window.{key}",
                )
        if "handle" in fields:
            handle = fields["handle"]
            if not isinstance(handle, int) or isinstance(handle, bool):
                raise RecordingModelError(
                    "type_error", "evidence.window.handle must be an integer",
                    path="event.evidence.window.handle",
                )
    pid = data.get("foregroundPid")
    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid < 1):
        raise RecordingModelError(
            "type_error", "foregroundPid must be a positive integer or null",
            path="event.evidence.foregroundPid",
        )
    interval = data.get("doubleClickIntervalMs")
    if interval is not None and (
        not isinstance(interval, int) or isinstance(interval, bool)
        or not 1 <= interval <= 5000
    ):
        raise RecordingModelError(
            "type_error", "doubleClickIntervalMs must be an integer in [1, 5000]",
            path="event.evidence.doubleClickIntervalMs",
        )
    for key in ("beforeCapture", "capture"):
        if key in data:
            _validate_capture_metadata(data[key], f"event.evidence.{key}")
    if "captureError" in data:
        error = _strict(data["captureError"], {"code", "message"}, "event.evidence.captureError")
        _text(error.get("code"), "event.evidence.captureError.code")
        _text(error.get("message"), "event.evidence.captureError.message")


def _validate_assertion(value: Mapping[str, Any]) -> None:
    data = _strict(value, {"type", "expected", "timeoutSeconds"}, "event.assertion")
    kind = data.get("type")
    if kind not in {
        "text_equals", "text_contains", "value_equals", "visible", "checked",
        "enabled", "window_open", "text_contains_time",
        "window_text_contains", "window_text_contains_time",
    }:
        raise RecordingModelError(
            "recording_assertion_invalid", f"unsupported assertion {kind!r}",
            path="event.assertion.type",
        )
    if "expected" not in data:
        raise RecordingModelError(
            "compile_assertion_missing_expected", "assertion requires explicit expected",
            path="event.assertion.expected",
        )
    expected = data["expected"]
    if kind in {"text_contains_time", "window_text_contains_time"}:
        # `expected` is a strftime pattern, rendered at replay time rather than
        # the literal timestamp the recorder happened to see.
        if not isinstance(expected, str) or not expected:
            raise RecordingModelError(
                "recording_assertion_invalid",
                f"{kind} expected must be a non-empty strftime pattern",
                path="event.assertion.expected",
            )
    if kind in {
        "text_equals", "text_contains", "value_equals", "window_text_contains",
    } and not isinstance(expected, str):
        raise RecordingModelError("recording_assertion_invalid", f"{kind} expected must be a string", path="event.assertion.expected")
    if kind in {"visible", "checked", "enabled"} and not isinstance(expected, bool):
        raise RecordingModelError("recording_assertion_invalid", f"{kind} expected must be boolean", path="event.assertion.expected")
    if kind == "window_open" and not isinstance(expected, (bool, Mapping)):
        raise RecordingModelError("recording_assertion_invalid", "window_open expected must be boolean or an object", path="event.assertion.expected")
    if kind == "window_open" and isinstance(expected, Mapping):
        window = _strict(
            expected,
            {"exists", "processName", "process_name", "title", "titleRegex"},
            "event.assertion.expected",
        )
        if "exists" in window and not isinstance(window["exists"], bool):
            raise RecordingModelError("recording_assertion_invalid", "window exists must be boolean", path="event.assertion.expected.exists")
        for key in ("processName", "process_name", "title", "titleRegex"):
            if key in window and not isinstance(window[key], str):
                raise RecordingModelError("recording_assertion_invalid", f"window {key} must be a string", path=f"event.assertion.expected.{key}")
    timeout = data.get("timeoutSeconds", 10.0)
    if (
        not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
        or not 0 < float(timeout) <= 300
    ):
        raise RecordingModelError("recording_assertion_invalid", "timeoutSeconds must be in (0, 300]", path="event.assertion.timeoutSeconds")


@dataclass(frozen=True)
class CaptureScope:
    target: str
    backend: str
    process_name: str
    window_title: str

    def __post_init__(self) -> None:
        for field_name in ("target", "backend", "process_name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise RecordingModelError(
                    "type_error", f"scope.{field_name} must be a non-empty string",
                    path=f"scope.{field_name}",
                )
        if not isinstance(self.window_title, str):
            raise RecordingModelError(
                "type_error", "scope.window_title must be a string",
                path="scope.window_title",
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CaptureScope":
        d = _strict(data, {"target", "backend", "processName", "windowTitle"}, "scope")
        return cls(
            _text(d.get("target"), "scope.target"),
            _text(d.get("backend"), "scope.backend"),
            _text(d.get("processName"), "scope.processName"),
            _text(d.get("windowTitle"), "scope.windowTitle", empty=True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "backend": self.backend,
            "processName": self.process_name,
            "windowTitle": self.window_title,
        }


@dataclass(frozen=True)
class ObservedTarget:
    snapshot_id: str
    target_id: str
    fingerprint: str
    control_type: str | None = None
    automation_id: str | None = None
    identifier: str | None = None
    text: str | None = None
    rect: tuple[int, int, int, int] | None = None
    ancestry: tuple[Mapping[str, str], ...] = ()
    protected: bool | None = None

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "target_id", "fingerprint"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise RecordingModelError(
                    "type_error", f"observedTarget.{field_name} must be non-empty",
                    path=f"observedTarget.{field_name}",
                )
        for field_name in (
            "control_type", "automation_id", "identifier", "text",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise RecordingModelError(
                    "type_error", f"observedTarget.{field_name} must be string or null",
                    path=f"observedTarget.{field_name}",
                )
        if self.rect is not None and (
            not isinstance(self.rect, (list, tuple)) or len(self.rect) != 4
            or not all(isinstance(item, int) and not isinstance(item, bool) for item in self.rect)
        ):
            raise RecordingModelError(
                "type_error", "observedTarget.rect must contain four integers",
                path="observedTarget.rect",
            )
        if not isinstance(self.ancestry, (list, tuple)) or not all(
            isinstance(item, Mapping)
            and all(isinstance(key, str) and isinstance(value, str) for key, value in item.items())
            for item in self.ancestry
        ):
            raise RecordingModelError(
                "type_error", "observedTarget.ancestry must contain string maps",
                path="observedTarget.ancestry",
            )
        if self.protected is not None and not isinstance(self.protected, bool):
            raise RecordingModelError(
                "type_error", "observedTarget.protected must be boolean or null",
                path="observedTarget.protected",
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ObservedTarget":
        allowed = {
            "snapshotId", "targetId", "fingerprint", "controlType",
            "automationId", "identifier", "text", "rect", "ancestry", "protected",
        }
        d = _strict(data, allowed, "observedTarget")
        rect = d.get("rect")
        if rect is not None and (
            not isinstance(rect, (list, tuple)) or len(rect) != 4
            or not all(isinstance(x, int) for x in rect)
        ):
            raise RecordingModelError("type_error", "rect must contain four integers", path="observedTarget.rect")
        ancestry = d.get("ancestry", [])
        if not isinstance(ancestry, list) or not all(isinstance(x, Mapping) for x in ancestry):
            raise RecordingModelError("type_error", "ancestry must be an array of objects", path="observedTarget.ancestry")
        protected = d.get("protected")
        if protected is not None and not isinstance(protected, bool):
            raise RecordingModelError("type_error", "protected must be boolean", path="observedTarget.protected")
        return cls(
            snapshot_id=_text(d.get("snapshotId"), "observedTarget.snapshotId"),
            target_id=_text(d.get("targetId"), "observedTarget.targetId"),
            fingerprint=_text(d.get("fingerprint"), "observedTarget.fingerprint"),
            control_type=d.get("controlType"), automation_id=d.get("automationId"),
            identifier=d.get("identifier"), text=d.get("text"),
            rect=tuple(rect) if rect is not None else None,
            ancestry=tuple(dict(x) for x in ancestry), protected=protected,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshotId": self.snapshot_id, "targetId": self.target_id,
            "fingerprint": self.fingerprint, "controlType": self.control_type,
            "automationId": self.automation_id, "identifier": self.identifier,
            "text": self.text, "rect": list(self.rect) if self.rect else None,
            "ancestry": [dict(x) for x in self.ancestry], "protected": self.protected,
        }


@dataclass(frozen=True)
class RawCaptureEvent:
    sequence: int
    wall_time: str
    monotonic_ms: int
    type: str
    scope: CaptureScope
    input: Mapping[str, Any] = field(default_factory=dict)
    observed_target: ObservedTarget | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    causal_id: str | None = None
    assertion: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1:
            raise RecordingModelError("type_error", "sequence must be positive", path="event.sequence")
        if not isinstance(self.monotonic_ms, int) or isinstance(self.monotonic_ms, bool) or self.monotonic_ms < 0:
            raise RecordingModelError("type_error", "monotonicMs must be non-negative", path="event.monotonicMs")
        _text(self.wall_time, "event.wallTime")
        _text(self.type, "event.type")
        if not isinstance(self.scope, CaptureScope):
            raise RecordingModelError("type_error", "scope has invalid type", path="event.scope")
        if not isinstance(self.input, Mapping) or not isinstance(self.evidence, Mapping):
            raise RecordingModelError("type_error", "input/evidence must be objects", path="event")
        if self.causal_id is not None and not isinstance(self.causal_id, str):
            raise RecordingModelError("type_error", "causalId must be string or null", path="event.causalId")
        if self.assertion is not None and not isinstance(self.assertion, Mapping):
            raise RecordingModelError("type_error", "assertion must be object or null", path="event.assertion")
        _validate_evidence(self.evidence)
        if self.assertion is not None:
            _validate_assertion(self.assertion)
        if (
            self.type == "text_commit"
            and "value" in self.input
            and (
                self.observed_target is None
                or self.observed_target.protected is not False
            )
        ):
            raise RecordingModelError(
                "recording_secret_plaintext",
                "text value requires an explicitly non-protected target",
                path="event.input.value",
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RawCaptureEvent":
        allowed = {"sequence", "wallTime", "monotonicMs", "type", "scope", "input", "observedTarget", "evidence", "causalId", "assertion"}
        d = _strict(data, allowed, "event")
        if not isinstance(d.get("sequence"), int) or d["sequence"] < 1:
            raise RecordingModelError("type_error", "sequence must be a positive integer", path="event.sequence")
        if not isinstance(d.get("monotonicMs"), int) or d["monotonicMs"] < 0:
            raise RecordingModelError("type_error", "monotonicMs must be non-negative", path="event.monotonicMs")
        for key in ("input", "evidence"):
            if not isinstance(d.get(key, {}), Mapping):
                raise RecordingModelError("type_error", f"{key} must be an object", path=f"event.{key}")
        assertion = d.get("assertion")
        if assertion is not None and not isinstance(assertion, Mapping):
            raise RecordingModelError("type_error", "assertion must be an object", path="event.assertion")
        return cls(
            sequence=d["sequence"], wall_time=_text(d.get("wallTime"), "event.wallTime"),
            monotonic_ms=d["monotonicMs"], type=_text(d.get("type"), "event.type"),
            scope=CaptureScope.from_dict(d.get("scope", {})), input=dict(d.get("input", {})),
            observed_target=(ObservedTarget.from_dict(d["observedTarget"]) if d.get("observedTarget") is not None else None),
            evidence=dict(d.get("evidence", {})), causal_id=d.get("causalId"),
            assertion=dict(assertion) if assertion is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        out = {
            "sequence": self.sequence, "wallTime": self.wall_time,
            "monotonicMs": self.monotonic_ms, "type": self.type,
            "scope": self.scope.to_dict(), "input": dict(self.input),
            "observedTarget": self.observed_target.to_dict() if self.observed_target else None,
            "evidence": dict(self.evidence), "causalId": self.causal_id,
            "assertion": dict(self.assertion) if self.assertion is not None else None,
        }
        return out


@dataclass(frozen=True)
class RawRecording:
    session_id: str
    name: str
    events: tuple[RawCaptureEvent, ...]
    capture_diagnostics: Mapping[str, Any] = field(default_factory=lambda: {
        "droppedPackets": 0,
        "correlationErrorCount": 0,
        "outOfScopeEvents": 0,
        "recorderUiEvents": 0,
        "unidentifiedTargetEvents": 0,
    })
    # The application's windows as this capture saw them, in first-appearance
    # order, each carrying the action that opened it. `openedBy` is the causal
    # id of that action, so the flat list reconstructs a tree of how the flow
    # moved between pages. This matters most where control identity is
    # unavailable: an application that does not expose its accessibility tree
    # yields anonymous controls, but the page structure is still recorded.
    windows: tuple[Mapping[str, Any], ...] = ()
    schema: str = RAW_RECORDING_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RAW_RECORDING_SCHEMA:
            raise RecordingModelError(
                "unsupported_schema", f"unsupported schema {self.schema!r}",
                path="recording.schema",
            )
        _text(self.session_id, "recording.sessionId")
        _text(self.name, "recording.name")
        diagnostics = _strict(
            self.capture_diagnostics,
            {
                "droppedPackets", "correlationErrorCount", "outOfScopeEvents",
                "recorderUiEvents", "unidentifiedTargetEvents",
            },
            "recording.captureDiagnostics",
        )
        # outOfScopeEvents and recorderUiEvents post-date the first
        # recordings; a document written without them is still valid and
        # reads as zero.
        missing_diagnostics = {
            "droppedPackets", "correlationErrorCount",
        } - set(diagnostics)
        if missing_diagnostics:
            raise RecordingModelError(
                "required_field_missing",
                f"captureDiagnostics is missing fields: {sorted(missing_diagnostics)}",
                path="recording.captureDiagnostics",
            )
        for key, value in diagnostics.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RecordingModelError(
                    "type_error", f"captureDiagnostics.{key} must be non-negative",
                    path=f"recording.captureDiagnostics.{key}",
                )
        for index, window in enumerate(self.windows):
            path = f"recording.windows[{index}]"
            fields = _strict(
                window,
                {
                    "title", "processName", "handle", "pid", "origin",
                    "closed", "openedBy", "reopenedBy",
                },
                path,
            )
            for key in ("title", "processName", "origin"):
                if key in fields and not isinstance(fields[key], str):
                    raise RecordingModelError(
                        "type_error", f"{path}.{key} must be a string",
                        path=f"{path}.{key}",
                    )
        if not isinstance(self.events, (list, tuple)) or not all(
            isinstance(event, RawCaptureEvent) for event in self.events
        ):
            raise RecordingModelError("type_error", "events must contain raw events", path="recording.events")
        sequences = [event.sequence for event in self.events]
        if sequences != list(range(1, len(self.events) + 1)):
            raise RecordingModelError(
                "sequence_invalid", "events must have contiguous sequence numbers starting at 1",
                path="recording.events",
            )
        if any(b.monotonic_ms < a.monotonic_ms for a, b in zip(self.events, self.events[1:])):
            raise RecordingModelError(
                "monotonic_invalid", "monotonicMs must not decrease",
                path="recording.events",
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RawRecording":
        d = _strict(
            data,
            {"schema", "sessionId", "name", "events", "captureDiagnostics", "windows"},
            "recording",
        )
        if d.get("schema") != RAW_RECORDING_SCHEMA:
            raise RecordingModelError("unsupported_schema", f"unsupported schema {d.get('schema')!r}", path="recording.schema")
        raw_events = d.get("events")
        if not isinstance(raw_events, list):
            raise RecordingModelError("type_error", "events must be an array", path="recording.events")
        diagnostics = d.get("captureDiagnostics")
        if diagnostics is None:
            raise RecordingModelError(
                "required_field_missing", "captureDiagnostics is required",
                path="recording.captureDiagnostics",
            )
        events = tuple(RawCaptureEvent.from_dict(x) for x in raw_events)
        sequences = [x.sequence for x in events]
        if sequences != list(range(1, len(events) + 1)):
            raise RecordingModelError("sequence_invalid", "events must have contiguous sequence numbers starting at 1", path="recording.events")
        if any(b.monotonic_ms < a.monotonic_ms for a, b in zip(events, events[1:])):
            raise RecordingModelError("monotonic_invalid", "monotonicMs must not decrease", path="recording.events")
        raw_windows = d.get("windows") or ()
        if not isinstance(raw_windows, (list, tuple)):
            raise RecordingModelError(
                "type_error", "windows must be an array", path="recording.windows",
            )
        # `schema` is keyword-passed: it is no longer the field right after
        # captureDiagnostics, and passing it positionally silently landed it
        # in `windows`.
        return cls(
            _text(d.get("sessionId"), "recording.sessionId"),
            _text(d.get("name"), "recording.name"),
            events,
            dict(diagnostics),
            windows=tuple(dict(w) for w in raw_windows),
            schema=d["schema"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "sessionId": self.session_id,
            "name": self.name,
            "captureDiagnostics": dict(self.capture_diagnostics),
            "windows": [dict(w) for w in self.windows],
            "events": [x.to_dict() for x in self.events],
        }
