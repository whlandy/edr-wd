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
#: v2 adds recorded window instances (instanceId) and the append-only
#: windowTimeline (design doc 09 §4). New recorders write v2; new readers
#: accept both v1 (no timeline, nothing fabricated) and v2.
RAW_RECORDING_SCHEMA_V2 = "edr.desktop-recording/v2"
RECORDED_WINDOW_TIMELINE_SCHEMA = "edr.recorded-window-timeline/v1"
#: Timeline `kind` values are the native facts (design 09 §4.1): never the
#: old manifest's opened/closed projection.
TIMELINE_KINDS = {"initial_observed", "created", "shown", "hidden",
                  "destroyed", "identity_changed", "lost", "observed"}
TIMELINE_SOURCES = {"initial_inventory", "native_show", "native_hide",
                    "native_destroy", "native_identity_change", "poller",
                    "tracking_loss", "coverage", "input_observation"}
#: Association labels (design 09 §4.3): the SOURCE of the link, never a
#: confidence. "explicit" is reserved for user-confirmed binding assertions
#: and is never auto-filled from temporal proximity or legacy openedBy.
TIMELINE_ASSOCIATIONS = {"explicit", "temporal_forward", "temporal_backfill",
                         "unknown"}

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


def window_root_identity(target: "ObservedTarget | None") -> str | None:
    """Name the window a control belongs to, from the control itself.

    Titles are the least stable identity this recorder can key on: two HiSec
    processes both title a window `logo1`, and one alternates between `logo1`
    and `HiSecEndpointAgent` while it runs. Handles change when a window is
    replaced and pids change on restart. A hierarchical automation id does not
    — `EdrMainWindow.baseWidget...` names its own window in the first
    component — and every hit-tested control already carries it.

    A bare id such as `btnApply` names no window: taking it would invent an
    identity per control in applications that do not use paths.
    """
    if target is None:
        return None

    def _root(automation_id: object) -> str | None:
        text = str(automation_id or "")
        if "." not in text:
            return None
        return text.split(".", 1)[0] or None

    root = _root(getattr(target, "automation_id", None))
    if root:
        return root
    for ancestor in reversed(tuple(getattr(target, "ancestry", ()) or ())):
        root = _root(ancestor.get("automationId") if isinstance(ancestor, Mapping) else None)
        if root:
            return root
    return None


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
            "capture", "captureError", "window", "handle",
            # S1 (design 09 §4): the recorded-window instance and the timeline
            # event that observed this transition. Recording-session-scoped
            # references, like evidence.window.handle — diagnostic joins, not
            # replay selectors.
            "windowInstanceId", "windowTimelineEventId",
        },
        "event.evidence",
    )
    for ref_key in ("windowInstanceId", "windowTimelineEventId"):
        ref = data.get(ref_key)
        if ref is not None and (not isinstance(ref, str) or not ref):
            raise RecordingModelError(
                "type_error", f"evidence.{ref_key} must be a non-empty string or absent",
                path=f"event.evidence.{ref_key}",
            )
    # A window event names the window it reports, and that name is what joins
    # the transition to the control inventory captured for the same window.
    # Session-local, like `window.handle`, and diagnostic for the same reason.
    handle = data.get("handle")
    if handle is not None and (not isinstance(handle, int) or isinstance(handle, bool)):
        raise RecordingModelError(
            "type_error", "evidence.handle must be an integer",
            path="event.evidence.handle",
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
        fields = _strict(
            window, {"title", "processName", "handle", "rootAutomationId",
                     "instanceId"},
            "event.evidence.window",
        )
        if "instanceId" in fields and (
                not isinstance(fields["instanceId"], str)
                or not fields["instanceId"]):
            raise RecordingModelError(
                "type_error",
                "evidence.window.instanceId must be a non-empty string",
                path="event.evidence.window.instanceId",
            )
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
    # region_query is a structured draft assertion: it carries no scalar
    # ``expected``; the whole v2 draft rides in ``draft`` and is validated by
    # the shared region validator (lazy import keeps this module importable
    # standalone, like the time-range parsers above).
    if isinstance(value, Mapping) and value.get("type") == "region_query":
        data = _strict(value, {"type", "draft", "timeoutSeconds"}, "event.assertion")
        draft = data.get("draft")
        if not isinstance(draft, Mapping):
            raise RecordingModelError(
                "recording_assertion_invalid", "region_query requires a draft object",
                path="event.assertion.draft",
            )
        try:
            from .region_assertion import validate_region_draft
        except ImportError:  # target-local deployment
            from region_assertion import validate_region_draft  # type: ignore[no-redef]
        # 落盘 draft 已去掉窗口身份（只在 evidence.window），线格式复检不再要求它。
        validate_region_draft(draft, require_window=False)
        timeout = data.get("timeoutSeconds", 60.0)
        if (
            not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not 0 < float(timeout) <= 300
        ):
            raise RecordingModelError(
                "recording_assertion_invalid", "timeoutSeconds must be in (0, 300]",
                path="event.assertion.timeoutSeconds",
            )
        return
    data = _strict(value, {"type", "expected", "timeoutSeconds", "durationSeconds"}, "event.assertion")
    kind = data.get("type")
    if kind not in {
        "text_equals", "text_contains", "value_equals", "visible", "checked",
        "enabled", "window_open", "text_contains_time",
        "window_text_contains", "window_text_contains_time",
        "text_time_range", "window_text_time_range",
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
    if kind in {"text_time_range", "window_text_time_range"}:
        try:
            try:
                from ..time_assertions import parse_time_range
            except ImportError:  # target-local deployment
                from time_assertions import parse_time_range
            parse_time_range(expected, data.get("durationSeconds"))
        except ValueError as exc:
            raise RecordingModelError("recording_assertion_invalid", str(exc), path="event.assertion.expected") from exc
    elif "durationSeconds" in data:
        raise RecordingModelError("recording_assertion_invalid", "durationSeconds is only valid for time ranges", path="event.assertion.durationSeconds")
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
    # 组件当前的「内容/取值」（编辑框文本、状态文本、选中项等），与 text
    # （辅助功能名/标签）区分。给 record assert 编辑器预填 expected 用；
    # 读不到就是 None，绝不因此炸录制。不参与 golden schema。
    value: str | None = None
    # 同父下与本控件共享 (control_type, text) 的兄弟数量（含自身）。
    # 弱身份（无 automationId/identifier）控件靠 name+ancestry 定位时，
    # 只有该值能区分「唯一链」与「多行同名歧义」；None=未知（不强判）。
    # 仅在录制端真正可读时填充，不参与 golden schema。
    same_name_siblings: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "target_id", "fingerprint"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise RecordingModelError(
                    "type_error", f"observedTarget.{field_name} must be non-empty",
                    path=f"observedTarget.{field_name}",
                )
        for field_name in (
            "control_type", "automation_id", "identifier", "text", "value",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise RecordingModelError(
                    "type_error", f"observedTarget.{field_name} must be string or null",
                    path=f"observedTarget.{field_name}",
                )
        if self.same_name_siblings is not None and (
            not isinstance(self.same_name_siblings, int)
            or isinstance(self.same_name_siblings, bool)
            or self.same_name_siblings < 1
        ):
            raise RecordingModelError(
                "type_error",
                "observedTarget.sameNameSiblings must be a positive integer or null",
                path="observedTarget.sameNameSiblings",
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
            "sameNameSiblings",
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
        same = d.get("sameNameSiblings")
        if same is not None and (
            not isinstance(same, int) or isinstance(same, bool) or same < 1
        ):
            raise RecordingModelError(
                "type_error", "sameNameSiblings must be a positive integer or null",
                path="observedTarget.sameNameSiblings",
            )
        return cls(
            snapshot_id=_text(d.get("snapshotId"), "observedTarget.snapshotId"),
            target_id=_text(d.get("targetId"), "observedTarget.targetId"),
            fingerprint=_text(d.get("fingerprint"), "observedTarget.fingerprint"),
            control_type=d.get("controlType"), automation_id=d.get("automationId"),
            identifier=d.get("identifier"), text=d.get("text"),
            rect=tuple(rect) if rect is not None else None,
            ancestry=tuple(dict(x) for x in ancestry), protected=protected,
            same_name_siblings=same,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshotId": self.snapshot_id, "targetId": self.target_id,
            "fingerprint": self.fingerprint, "controlType": self.control_type,
            "automationId": self.automation_id, "identifier": self.identifier,
            "text": self.text, "rect": list(self.rect) if self.rect else None,
            "ancestry": [dict(x) for x in self.ancestry], "protected": self.protected,
            "sameNameSiblings": self.same_name_siblings,
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


def _validate_window_timeline(timeline: Mapping[str, Any],
                              windows: tuple) -> None:
    """Strict validation for the v2 windowTimeline (design doc 09 §4).

    The timeline is the append-only factual record of window lifecycle
    observations: unified ingress `order`, observed `monotonicMs` kept
    verbatim, native kinds (never the old opened/closed projection), an
    association label that is a SOURCE (never "explicit" unless a user
    confirmed it, which the recorder never writes), and a coverage statement.
    Every instanceId must name a window the document actually declares.
    """
    d = _strict(timeline, {"schema", "events", "coverage"},
                "recording.windowTimeline")
    if d.get("schema") != RECORDED_WINDOW_TIMELINE_SCHEMA:
        raise RecordingModelError(
            "unsupported_schema",
            f"unsupported windowTimeline schema {d.get('schema')!r}",
            path="recording.windowTimeline.schema",
        )
    events = d.get("events")
    if not isinstance(events, (list, tuple)):
        raise RecordingModelError(
            "type_error", "windowTimeline.events must be an array",
            path="recording.windowTimeline.events",
        )
    declared = set()
    for index, window in enumerate(windows):
        instance_id = window.get("instanceId") if isinstance(window, Mapping) else None
        if isinstance(instance_id, str) and instance_id:
            declared.add(instance_id)
    seen_ids = set()
    previous_order = None
    for index, event in enumerate(events):
        path = f"recording.windowTimeline.events[{index}]"
        item = _strict(
            event,
            {"id", "order", "monotonicMs", "instanceId", "kind", "source",
             "associatedActionCausalId", "association", "ingressSequence"},
            path,
        )
        for key in ("id", "kind", "source"):
            value = item.get(key)
            if not isinstance(value, str) or not value:
                raise RecordingModelError(
                    "type_error", f"{path}.{key} must be a non-empty string",
                    path=f"{path}.{key}",
                )
        if item["id"] in seen_ids:
            raise RecordingModelError(
                "window_timeline_invalid", f"duplicate timeline event id {item['id']!r}",
                path=f"{path}.id",
            )
        seen_ids.add(item["id"])
        if item["kind"] not in TIMELINE_KINDS:
            raise RecordingModelError(
                "window_timeline_invalid",
                f"{path}.kind {item['kind']!r} is not a native fact kind",
                path=f"{path}.kind",
            )
        if item["source"] not in TIMELINE_SOURCES:
            raise RecordingModelError(
                "window_timeline_invalid",
                f"{path}.source {item['source']!r} is not a declared source",
                path=f"{path}.source",
            )
        order = item.get("order")
        if not isinstance(order, int) or isinstance(order, bool) or order < 0:
            raise RecordingModelError(
                "type_error", f"{path}.order must be a non-negative integer",
                path=f"{path}.order",
            )
        if previous_order is not None and order < previous_order:
            raise RecordingModelError(
                "window_timeline_invalid",
                "windowTimeline.events order must be non-decreasing "
                "(ingress order); observed monotonicMs may be out of order "
                "and stays verbatim",
                path=f"{path}.order",
            )
        previous_order = order
        ms = item.get("monotonicMs")
        if not isinstance(ms, int) or isinstance(ms, bool):
            raise RecordingModelError(
                "type_error", f"{path}.monotonicMs must be an integer",
                path=f"{path}.monotonicMs",
            )
        ingress = item.get("ingressSequence")
        if ingress is not None and (not isinstance(ingress, int)
                                    or isinstance(ingress, bool) or ingress < 1):
            raise RecordingModelError(
                "type_error",
                f"{path}.ingressSequence must be a positive integer or absent",
                path=f"{path}.ingressSequence",
            )
        instance_id = item.get("instanceId")
        if instance_id is not None:
            if not isinstance(instance_id, str) or not instance_id:
                raise RecordingModelError(
                    "type_error",
                    f"{path}.instanceId must be a non-empty string or absent",
                    path=f"{path}.instanceId",
                )
            if instance_id not in declared:
                raise RecordingModelError(
                    "window_timeline_invalid",
                    f"{path}.instanceId {instance_id!r} does not name a declared "
                    "window instance",
                    path=f"{path}.instanceId",
                )
        association = item.get("association")
        if association is not None:
            if association not in TIMELINE_ASSOCIATIONS:
                raise RecordingModelError(
                    "window_timeline_invalid",
                    f"{path}.association {association!r} is not a declared source label",
                    path=f"{path}.association",
                )
            if association == "explicit":
                # The recorder never writes explicit (design 09 §4.3): only a
                # user-confirmed binding assertion may claim it, and that is
                # not a recording-time fact.
                raise RecordingModelError(
                    "window_timeline_invalid",
                    f"{path}.association must not be 'explicit' in recorded data",
                    path=f"{path}.association",
                )
        if "associatedActionCausalId" in item and not isinstance(
                item["associatedActionCausalId"], str):
            raise RecordingModelError(
                "type_error",
                f"{path}.associatedActionCausalId must be a string or absent",
                path=f"{path}.associatedActionCausalId",
            )
    coverage = d.get("coverage")
    if not isinstance(coverage, Mapping):
        raise RecordingModelError(
            "type_error", "windowTimeline.coverage must be an object",
            path="recording.windowTimeline.coverage",
        )
    # Coverage is a strict, schema-aligned contract (review 0457 P2): the
    # model and the JSON schema must agree on fields, enums and types, so a
    # document one accepts and the other rejects cannot exist.
    cov = _strict(coverage, {"destroyEvents", "droppedEvents"},
                  "recording.windowTimeline.coverage")
    destroy = cov.get("destroyEvents")
    if destroy not in {"supported", "unsupported", "unknown"}:
        raise RecordingModelError(
            "window_timeline_invalid",
            f"coverage.destroyEvents {destroy!r} must be "
            "'supported' | 'unsupported' | 'unknown'",
            path="recording.windowTimeline.coverage.destroyEvents",
        )
    dropped = cov.get("droppedEvents")
    if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped < 0:
        raise RecordingModelError(
            "type_error",
            "coverage.droppedEvents must be a non-negative integer",
            path="recording.windowTimeline.coverage.droppedEvents",
        )


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
    # Complete control trees for the windows the flow visited, in capture
    # order. Events name the one control the user acted on, which is enough to
    # replay a step and not enough to *generate* one: a MAA node table or a
    # pytest script written from this recording can only refer to controls the
    # recording knows exist. Deduplicated by content, so a window walked twice
    # without changing appears once and earlier steps still resolve to it.
    control_snapshots: tuple[Mapping[str, Any], ...] = ()
    # Input this capture saw and did not record, with the window it resolved
    # to. The counts in captureDiagnostics say a recording was short; only
    # these say whether the recorder misjudged which window the user was in.
    rejections: tuple[Mapping[str, Any], ...] = ()
    schema: str = RAW_RECORDING_SCHEMA
    #: v2 only. The append-only recorded-window timeline: native show/hide/
    #: destroy facts with a unified ingress order and the observed monotonic
    #: time kept verbatim. Absent on v1 documents — nothing is fabricated.
    window_timeline: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.schema not in {RAW_RECORDING_SCHEMA, RAW_RECORDING_SCHEMA_V2}:
            raise RecordingModelError(
                "unsupported_schema", f"unsupported schema {self.schema!r}",
                path="recording.schema",
            )
        if self.window_timeline is not None and self.schema != RAW_RECORDING_SCHEMA_V2:
            raise RecordingModelError(
                "type_error", "windowTimeline requires the v2 recording schema",
                path="recording.schema",
            )
        if self.window_timeline is not None:
            _validate_window_timeline(self.window_timeline, self.windows)
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
            allowed_window_fields = {
                "title", "processName", "handle", "pid", "origin",
                "closed", "openedBy", "reopenedBy",
            }
            if self.schema == RAW_RECORDING_SCHEMA_V2:
                allowed_window_fields.add("instanceId")
            fields = _strict(
                window,
                allowed_window_fields,
                path,
            )
            for key in ("title", "processName", "origin"):
                if key in fields and not isinstance(fields[key], str):
                    raise RecordingModelError(
                        "type_error", f"{path}.{key} must be a string",
                        path=f"{path}.{key}",
                    )
        for index, snapshot in enumerate(self.control_snapshots):
            path = f"recording.controlSnapshots[{index}]"
            fields = _strict(
                snapshot,
                {
                    "snapshotId", "capturedAtMs", "reason", "durationMs",
                    "window", "contentHash", "stateHash", "truncated",
                    "complete", "controlCount", "controls",
                },
                path,
            )
            _text(fields.get("snapshotId"), f"{path}.snapshotId")
            _text(fields.get("contentHash"), f"{path}.contentHash")
            if "stateHash" in fields:
                _text(fields["stateHash"], f"{path}.stateHash")
            for bool_key in ("truncated", "complete"):
                # When the field is present it must be a real boolean. A null
                # (or any non-bool like "false"/0) is rejected here, matching
                # the JSON Schema's strict `"type": "boolean"` — the schema
                # must not pass what the model accepts, nor vice versa.
                if bool_key in fields and not isinstance(fields[bool_key], bool):
                    raise RecordingModelError(
                        "type_error", f"{path}.{bool_key} must be a boolean",
                        path=f"{path}.{bool_key}",
                    )
            if not isinstance(fields.get("controls"), (list, tuple)):
                raise RecordingModelError(
                    "type_error", f"{path}.controls must be an array",
                    path=f"{path}.controls",
                )
            if not isinstance(fields.get("window"), Mapping):
                raise RecordingModelError(
                    "type_error", f"{path}.window must be an object",
                    path=f"{path}.window",
                )
        snapshot_ids = [
            snapshot.get("snapshotId") for snapshot in self.control_snapshots
        ]
        if len(set(snapshot_ids)) != len(snapshot_ids):
            raise RecordingModelError(
                "sequence_invalid", "controlSnapshots ids must be unique",
                path="recording.controlSnapshots",
            )
        for index, rejection in enumerate(self.rejections):
            if not isinstance(rejection, Mapping):
                raise RecordingModelError(
                    "type_error", "rejections must contain objects",
                    path=f"recording.rejections[{index}]",
                )
            _text(rejection.get("reason"), f"recording.rejections[{index}].reason")
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
            {
                "schema", "sessionId", "name", "events", "captureDiagnostics",
                "windows", "controlSnapshots", "rejections", "windowTimeline",
            },
            "recording",
        )
        if d.get("schema") not in {RAW_RECORDING_SCHEMA, RAW_RECORDING_SCHEMA_V2}:
            raise RecordingModelError("unsupported_schema", f"unsupported schema {d.get('schema')!r}", path="recording.schema")
        window_timeline = d.get("windowTimeline")
        if window_timeline is not None and d.get("schema") != RAW_RECORDING_SCHEMA_V2:
            raise RecordingModelError(
                "type_error", "windowTimeline requires the v2 recording schema",
                path="recording.schema",
            )
        if window_timeline is not None and not isinstance(window_timeline, Mapping):
            raise RecordingModelError(
                "type_error", "windowTimeline must be an object",
                path="recording.windowTimeline",
            )
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
        raw_snapshots = d.get("controlSnapshots") or ()
        if not isinstance(raw_snapshots, (list, tuple)):
            raise RecordingModelError(
                "type_error", "controlSnapshots must be an array",
                path="recording.controlSnapshots",
            )
        raw_rejections = d.get("rejections") or ()
        if not isinstance(raw_rejections, (list, tuple)):
            raise RecordingModelError(
                "type_error", "rejections must be an array",
                path="recording.rejections",
            )
        return cls(
            _text(d.get("sessionId"), "recording.sessionId"),
            _text(d.get("name"), "recording.name"),
            events,
            dict(diagnostics),
            windows=tuple(dict(w) for w in raw_windows),
            control_snapshots=tuple(dict(x) for x in raw_snapshots),
            rejections=tuple(dict(x) for x in raw_rejections),
            schema=d["schema"],
            window_timeline=dict(window_timeline) if window_timeline is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "schema": self.schema,
            "sessionId": self.session_id,
            "name": self.name,
            "captureDiagnostics": dict(self.capture_diagnostics),
            "windows": [dict(w) for w in self.windows],
            "controlSnapshots": [dict(x) for x in self.control_snapshots],
            "rejections": [dict(x) for x in self.rejections],
            "events": [x.to_dict() for x in self.events],
        }
        if self.window_timeline is not None:
            timeline = dict(self.window_timeline)
            events = timeline.get("events")
            if isinstance(events, tuple):
                timeline["events"] = [dict(e) for e in events]
            data["windowTimeline"] = timeline
        return data
