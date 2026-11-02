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
    VERIFIER_REQUIRED_EVENT_TYPES,
)
from .indicator import NullRecordingIndicator, RecordingIndicator
from .assertion_draft import validate_assertion_draft as validate_assertion_draft_payload
from .picker import PickerController


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
    # Actions the user performed. `sequence` counts every observed event,
    # including the window transitions the compiler folds into the preceding
    # step's verifier, so it reads ahead of the steps a recording will
    # actually contain and cannot be shown as a step count.
    action_count: int = 0
    # Seconds left before the capture lease expires while RECORDING/PAUSED
    # (None otherwise). Pure display field: empty recordings used to surface
    # only at `record stop` as `recording_lease_expired`, because nothing told
    # the operator the lease was running out.
    lease_seconds_remaining: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"sessionId": self.session_id, "state": self.state.value, "sequence": self.sequence, "monotonicMs": self.monotonic_ms, "reason": self.reason, "actionCount": self.action_count, "leaseSecondsRemaining": self.lease_seconds_remaining}


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


def _assertion_default(target: ObservedTarget | None) -> dict:
    """把选中组件映射成断言编辑器要预填的字段。

    现在会带上组件的当前取值 `value`：让编辑器把「当前内容」预填进 expected，
    正好是「选组件 → 抓内容 → 再设断言」。读不到 value 就缺省 None。
    """
    if not isinstance(target, ObservedTarget):
        return {}
    return {
        "automationId": target.automation_id,
        "identifier": target.identifier,
        "controlType": target.control_type,
        "name": target.text,
        "value": target.value,
    }


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
        # Actions the user performed, which is what the indicator reports.
        # Distinct from len(self._events), which also holds result evidence.
        self._action_count = 0
        self._lock = threading.RLock();self._reason = ""
        # Set once the session reaches a terminal state, so a second stop()
        # can wait for an in-flight one instead of losing the recording.
        self._settled = threading.Event()
        self._last_action_causal_id: str | None = None
        self._assertion_window: dict[str, Any] | None = None
        # 区域断言向导进入前的窗口快照：cancel 只恢复现场，不落事件。
        self._region_saved_window: dict[str, Any] | None = None
        # requestId -> saved assertion result. A retried save with the same
        # requestId must not append a second assertion event (double-click on
        # the wizard's save button, or a UI retry after a lost ack).
        self._assertion_results: dict[str, dict] = {}
        # Wizard picking (U2): PICKING is a *view* sub-state — the capture
        # state enum never changes. Pending drafts live only in this session;
        # a draft from another session or an expired one is refused on commit.
        self._picker: PickerController | None = None
        self._pending_drafts: dict[str, dict] = {}
        self._pick_binding: tuple[int, str] | None = None
        # Lease watchdog: the lazy expiry above only runs when a session
        # operation arrives. If the agent dies mid-recording and the operator
        # never touches the recorder UI, nothing ever arrives — the capture
        # hook and the recorder window would stay up with no deadline. This
        # thread enforces the same lease on its own and exits once the session
        # reaches a terminal state.
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(timeout=0.25):
            try:
                if self._expire_before_operation():
                    break
            except Exception:
                # A watchdog must never take the process down; the next tick
                # retries, and the lazy path still covers expiry.
                continue

    def _start_watchdog(self) -> None:
        thread = threading.Thread(
            target=self._watchdog_loop,
            name=f"recording-lease-watchdog-{self.session_id}",
            daemon=True,
        )
        self._watchdog_thread = thread
        self._watchdog_stop.clear()
        thread.start()

    def _shutdown_watchdog(self) -> None:
        self._watchdog_stop.set()
        thread = self._watchdog_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
            if thread.is_alive():
                # Timed out waiting — leave it; the flag already told it to
                # exit at its next tick, and the thread is daemon so it cannot
                # block process exit.
                self._watchdog_thread = None

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
        remaining: float | None = None
        if self._state in {CaptureSessionState.RECORDING, CaptureSessionState.PAUSED}:
            remaining = max(
                0.0, self._lease_seconds - (self._monotonic() - self._last_heartbeat)
            )
        return SessionReceipt(
            self.session_id, self._state, len(self._events), self._now_ms(),
            self._reason, self._action_count, remaining,
        )

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
        # Whoever ran this (lazy path or watchdog), the watch has nothing left
        # to enforce — tell it to exit at its next tick.
        self._watchdog_stop.set()
        indicator_error = None
        try:
            # Tear the UI down first. Draining the correlator can take tens
            # of seconds when a correlation call is stuck, and doing that with
            # the recorder window still on screen makes a stop that is working
            # look like one that has hung.
            self._indicator.stop()
        except Exception as exc:
            # Same as the user-stop path: the source's own teardown must run
            # regardless, and the indicator's failure still surfaces.
            indicator_error = exc
        try:
            self._source.stop()
        except Exception as exc:
            with self._lock:
                self._state = CaptureSessionState.FAILED
                self._settled.set()
                self._reason = f"recording_lease_expired: {exc}"
            if indicator_error is not None:
                raise exc from indicator_error
            raise
        if indicator_error is not None:
            with self._lock:
                self._state = CaptureSessionState.FAILED
                self._settled.set()
                self._reason = f"recording_lease_expired: {indicator_error}"
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
                self._bind_recorder_ui_rect()
            except Exception as exc:
                try:
                    self._source.stop()
                except Exception:
                    pass
                self._state = CaptureSessionState.FAILED;self._reason = str(exc);self._settled.set();raise
            self._state = CaptureSessionState.RECORDING;self._last_heartbeat = self._monotonic()
            self._start_watchdog()
            return self._receipt()

    def _bind_recorder_ui_rect(self) -> None:
        """Let the correlator know where the recorder's own UI is on screen.

        The indicator is always-on-top, so anything clicked inside it never
        reached the target application and must not enter the recording.
        """
        correlator = getattr(self._source, "_correlator", None)
        bind = getattr(correlator, "set_recorder_ui_rect", None)
        if not callable(bind):
            return
        # Prefer the window server's own bounds: the toolkit reports client
        # geometry that excludes the title bar, and a drag on that title bar
        # would then read as input inside the target window.
        rect = None
        resolve = getattr(getattr(correlator, "_resolver", None), "recorder_ui_rect", None)
        if callable(resolve):
            try:
                rect = resolve()
            except Exception:
                rect = None
        bind(rect or getattr(self._indicator, "window_rect", None))

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
            # 旧断言快捷键（Shift+Ctrl/Cmd+A 内嵌入口）已按 06 设计移除：
            # Ctrl+Alt+Q 是唯一断言入口，picker/向导路径不再有会话内快捷键。
            # 该按键组合之后按普通业务 key_command 处理（编译器如不支持会
            # 如实报 compile_action_unsupported，而不是静默丢弃）。
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
            # Count what the user did, not everything observed. A window
            # opening or closing is evidence that the preceding click worked —
            # the compiler folds it into that step's verifier — so counting it
            # made the indicator read ahead of the steps the recording will
            # actually contain (18 observed against 15 compiled in one live
            # capture), for something the user never performed. An explicit
            # assertion does count: the user authored it, and an unbound one
            # becomes a step of its own.
            if event.type in ACTION_EVENT_TYPES or event.assertion is not None:
                self._action_count += 1
                # Only notify on a change; the indicator lives in another
                # process and every update is a pipe write.
                self._indicator.update_count(self._action_count)
            return True

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

    @staticmethod
    def _event_window(event: RawCaptureEvent | None) -> dict[str, Any] | None:
        raw = event.evidence.get("window") if event is not None else None
        if (
            not isinstance(raw, Mapping)
            and event is not None
            and event.type == "window_transition"
            and event.input.get("kind") == "opened"
        ):
            # A CLI-authored assertion may follow a newly opened window before
            # any pointer/key event has stamped its foreground identity.
            raw = event.input
        if not isinstance(raw, Mapping):
            return None
        title, process = raw.get("title"), raw.get("processName")
        if not isinstance(title, str) or not title or not isinstance(process, str) or not process:
            return None
        window = {"title": title, "processName": process}
        root = raw.get("rootAutomationId")
        if isinstance(root, str) and root:
            window["rootAutomationId"] = root
        handle = raw.get("handle")
        if isinstance(handle, int) and not isinstance(handle, bool):
            window["handle"] = handle
        return window

    def _open_assertion_editor(self, trigger: RawCaptureEvent | None = None) -> None:
        # The recorder UI takes foreground while the editor is open. Keep the
        # product window that received its hotkey, not the session entry window
        # or the recorder UI's current foreground window.
        recent = self._events[-1] if self._events else None
        self._assertion_window = self._event_window(trigger) or self._event_window(recent)
        current_target = getattr(self._source, "current_observed_target", None)
        target = current_target() if callable(current_target) else None
        if not isinstance(target, ObservedTarget):
            target = getattr(self._source, "last_observed_target", None)
        # 向导路径：把当前指针组件冻结成类型化草稿（draftId 即保存 requestId，
        # U0 去重生效）。读取失败/未知时也产出草稿——readStatus 如实携带，
        # 由向导披露"当前值未能读取"，绝不预填空值冒充观测。
        draft = self._freeze_editor_draft(target)
        self._indicator.open_assertion_editor(draft)

    def _freeze_editor_draft(self, target: ObservedTarget | None) -> dict:
        from .assertion_draft import build_assertion_draft
        observed: dict = {}
        read_status, read_error = "unknown", None
        if isinstance(target, ObservedTarget):
            observed = {
                "automationId": target.automation_id, "identifier": target.identifier,
                "controlType": target.control_type, "name": target.text,
                "text": target.text, "value": target.value,
                "protected": target.protected,
            }
            read_status = "ok" if any(
                observed.get(k) for k in ("automationId", "identifier")
            ) or observed.get("name") else "unknown"
        draft = build_assertion_draft(
            observed, "text_equals",
            str(observed.get("value")) if observed.get("value") is not None else "",
            read_status=read_status, read_error=read_error,
            # 未知保护必须是显式的 "None" 字符串而不是缺省——保存边界据此
            # 区分「从未携带保护元数据」与「读取了但未知」。
            protection_status="None" if observed.get("protected") is None else str(observed.get("protected")),
            window=dict(self._assertion_window or {}),
            binding=getattr(self, "_pick_binding", None),
        )
        self._pending_drafts[draft.draft_id] = draft.to_dict()
        return draft.to_dict()

    def add_assertion(self, payload: Mapping[str, Any]) -> dict:
        # Explicit assertion saves are *management* submissions, not captured
        # input: they must work while the session is PAUSED (the editor is
        # allowed open there) without silently resuming, and native business
        # input stays rejected by ingest's own state gate. The saved result is
        # keyed by the form's requestId so a retry never appends twice.
        request_id = payload.get("requestId")
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id.strip()
        ):
            raise RecordingModelError(
                "recording_assertion_invalid", "requestId must be a non-empty string",
                path="assertion.requestId",
            )
        with self._lock:
            self._expire_before_operation()
            if self._state not in {
                CaptureSessionState.RECORDING, CaptureSessionState.PAUSED,
            }:
                raise RecordingModelError(
                    "recording_state_invalid",
                    "assertion save requires an active (recording or paused) session",
                    path="session.state",
                )
            if request_id is not None and request_id in self._assertion_results:
                return dict(self._assertion_results[request_id], duplicate=True)
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
            "text_time_range", "window_text_time_range",
        }:
            raise RecordingModelError(
                "recording_assertion_invalid", f"unsupported assertion {assertion_type!r}",
                path="assertion.assertion",
            )
        expected_value = payload["expected"]
        duration_seconds = payload.get("durationSeconds")
        if assertion_type in {"text_time_range", "window_text_time_range"}:
            try:
                try:
                    from ..time_assertions import parse_time_range
                except ImportError:  # target-local deployment
                    from time_assertions import parse_time_range
                if isinstance(duration_seconds, str) and duration_seconds.strip():
                    duration_seconds = int(duration_seconds)
                elif duration_seconds == "":
                    duration_seconds = None
                parse_time_range(expected_value, duration_seconds)
            except (ValueError, TypeError) as exc:
                raise RecordingModelError(
                    "recording_assertion_invalid", str(exc), path="assertion.expected",
                ) from exc
        elif duration_seconds not in (None, ""):
            raise RecordingModelError(
                "recording_assertion_invalid", "durationSeconds is only valid for time ranges",
                path="assertion.durationSeconds",
            )
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
            if assertion_type in {"text_contains", "window_text_contains"} and not expected_value:
                raise RecordingModelError(
                    "recording_assertion_invalid",
                    "contains assertion requires non-empty expected text",
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
        explicit_causal_id = payload.get("bindCausalId")
        if explicit_causal_id is not None and not isinstance(explicit_causal_id, str):
            raise RecordingModelError(
                "recording_assertion_invalid", "bindCausalId must be a string",
                path="assertion.bindCausalId",
            )
        causal_id = None
        if explicit_causal_id is not None:
            # U3: the wizard binds a *named* action (sequence/causalId shown in
            # the panel), never the action that happened to be last at submit
            # time.  The binding must reference a recorded action of this
            # session — a cross-session or unknown id is refused.
            recorded = {e.causal_id for e in self._events
                        if e.type in ACTION_EVENT_TYPES}
            if explicit_causal_id not in recorded:
                raise RecordingModelError(
                    "recording_assertion_unbound",
                    "bindCausalId does not match a recorded action of this session",
                    path="assertion.bindCausalId",
                )
            causal_id = explicit_causal_id
        elif bind_previous:
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
        identity_free = {"window_open", "window_text_contains", "window_text_contains_time", "window_text_time_range"}
        stable_identity = (
            identity["automationId"] or identity["identifier"]
            or (identity["controlType"] and identity["text"])
        )
        if assertion_type not in identity_free and not stable_identity:
            raise RecordingModelError(
                "recording_target_unresolved", "assertion requires a semantic control identity",
                path="assertion.selector",
            )
        # Content assertions against a protected field (or one whose
        # sensitivity the picker could not determine) would persist the
        # field's live value verbatim as `expected` — the same leak the
        # text_commit boundary refuses.  The wizard freeze scrubs the
        # prefill and stamps protectionStatus on the payload; a payload
        # that carries that provenance and fails the check is refused
        # outright.  A payload with NO protection provenance at all is the
        # classic editor path (operator-typed expectation, no observed
        # value prefill): it keeps the historical contract but loses the
        # `value` shortcut — that observed content is never persisted for
        # it, so nothing can leak through `expected` it did not type.
        from .assertion_draft import protection_blocks_content
        has_provenance = (
            "protectionStatus" in payload or "protected" in payload
        )
        if has_provenance and protection_blocks_content(
            assertion_type,
            payload.get("protectionStatus"),
            {"protected": payload.get("protected"),
             "controlType": identity["controlType"]},
        ):
            raise RecordingModelError(
                "recording_secret_plaintext",
                "content assertion on a protected or unknown-sensitivity target is refused;"
                " use a state/existence assertion or an explicitly non-protected target",
                path="assertion.expected",
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
                # 编辑器抓到的「当前内容」原样记进观测，与编辑器里看到的取值一致。
                # 不参与 identity/fingerprint，不影响 golden 契约。
                value=payload.get("value"),
                protected=False,
            )
        # A wizard draft carries the window it was picked in; the classic
        # editor path keeps the session's assertion window.
        draft_window = payload.get("window")
        if draft_window is not None and not isinstance(draft_window, Mapping):
            raise RecordingModelError(
                "recording_assertion_invalid", "window must be an object",
                path="assertion.window",
            )
        event = RawCaptureEvent(
            sequence=len(self._events) + 1,
            wall_time=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            monotonic_ms=self._now_ms(),
            type="assertion",
            scope=self.scope,
            observed_target=observed,
            evidence={"window": dict(draft_window or self._assertion_window or {})}
            if (draft_window or self._assertion_window) else {},
            assertion={
                "type": assertion_type,
                "expected": expected_value,
                "timeoutSeconds": timeout_seconds,
                **({"durationSeconds": duration_seconds} if duration_seconds is not None else {}),
            },
            causal_id=causal_id,
        )
        saved = self._append_assertion_event(
            event, request_id, window=draft_window if draft_window else None,
        )
        self._assertion_window = None
        return saved

    # -- Region / multi-component assertion (Ctrl+Alt+Q unified entry) ------

    def begin_region_assertion(self) -> dict:
        """Enter the region-assertion editor: validate state + target window.

        Mirrors ``request_assertion_editor``'s gate (RECORDING/PAUSED, active
        session) and stamps the assertion window from the most recent scoped
        event that carries an identifiable window, walking backwards through
        history — real input streams interleave window-transition events with
        no window stamp after an action, so a single ``_events[-1]`` sample
        would spuriously refuse a perfectly identifiable target.  An explicit
        ``_assertion_window`` stamp still wins.  The previous window is kept
        so ``cancel_region_assertion`` can restore the exact pre-entry state
        without appending any event.
        """
        self._expire_before_operation()
        with self._lock:
            if self._state not in {
                CaptureSessionState.RECORDING, CaptureSessionState.PAUSED,
            }:
                raise RecordingModelError(
                    "recording_state_invalid",
                    "region assertion editor requires an active (recording or paused) session",
                    path="session.state",
                )
            window = self._assertion_window
            if window is None:
                for event in reversed(self._events):
                    window = self._event_window(event)
                    if window is not None:
                        break
            if window is None:
                raise RecordingModelError(
                    "recording_assertion_window_unknown",
                    "region assertion requires an identifiable target window",
                    path="session.assertionWindow",
                )
            self._region_saved_window = self._assertion_window
            self._assertion_window = dict(window)
            self._last_heartbeat = self._monotonic()
            # 浮窗编排（ duck-typing：测试桩/空实现没有这些方法时是 no-op；
            # Tk 浮窗侧由 A 轨补 hide_for_region_selection/restore 方法）。
            hide = getattr(self._indicator, "hide_for_region_selection", None)
            if callable(hide):
                hide()
            # assertionWindow 与 window 同值：A 轨读取 assertionWindow，
            # 旧读取方继续拿 window。
            return {
                "ok": True,
                "state": self._state.value,
                "assertionWindow": dict(window),
                "window": dict(window),
            }

    def cancel_region_assertion(self) -> dict:
        """Leave the region editor: restore the pre-entry window, no event."""
        self._expire_before_operation()
        with self._lock:
            self._assertion_window = getattr(self, "_region_saved_window", None)
            self._region_saved_window = None
            restore = getattr(
                self._indicator, "restore_after_region_selection", None)
            if callable(restore):
                restore()
            return {"ok": True, "state": self._state.value}

    def add_region_assertion(self, draft: Mapping[str, Any]) -> dict:
        """Validate and persist one region assertion draft as a raw event.

        Management save path (RECORDING/PAUSED) with the same requestId
        idempotency and in-lock re-check as ``add_assertion`` — a retried
        save never appends twice.  The draft is validated by the shared
        region validator; the window identity only rides in ``evidence.window``
        (agent-side golden selectors forbid ephemeral handle/pid fields), and
        ``observed_target`` carries the container's stable identity.
        """
        from .region_assertion import (
            draft_for_persistence, render_region_summary, validate_region_draft,
        )
        if not isinstance(draft, Mapping):
            raise RecordingModelError(
                "recording_assertion_invalid", "draft must be an object",
                path="draft",
            )
        request_id = draft.get("requestId") or draft.get("draftId")
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id.strip()
        ):
            raise RecordingModelError(
                "recording_assertion_invalid", "requestId must be a non-empty string",
                path="draft.requestId",
            )
        with self._lock:
            self._expire_before_operation()
            if self._state not in {
                CaptureSessionState.RECORDING, CaptureSessionState.PAUSED,
            }:
                raise RecordingModelError(
                    "recording_state_invalid",
                    "region assertion save requires an active (recording or paused) session",
                    path="session.state",
                )
            if request_id is not None and request_id in self._assertion_results:
                return dict(self._assertion_results[request_id], duplicate=True)
        try:
            validated = validate_region_draft(draft)
        except RecordingModelError as exc:
            raise RecordingModelError(
                "recording_assertion_invalid", str(exc), path=exc.path or "draft",
            ) from exc
        binding = validated.get("binding")
        causal_id = None
        if binding is not None:
            # 与 add_assertion 的 bindCausalId 同一规则：绑定的必须是本会话
            # 已录制动作，跨会话/未知 id 拒绝。
            recorded = {e.causal_id for e in self._events
                        if e.type in ACTION_EVENT_TYPES}
            if binding["causalId"] not in recorded:
                raise RecordingModelError(
                    "recording_assertion_unbound",
                    "binding does not match a recorded action of this session",
                    path="draft.binding.causalId",
                )
            causal_id = binding["causalId"]
        container = (validated.get("target") or {}).get("container") or None
        observed = None
        if container:
            identity = {
                "controlType": container.get("controlType"),
                "automationId": container.get("automationId"),
                "identifier": container.get("identifier"),
                "text": container.get("name"),
            }
            digest = hashlib.sha256(
                json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            # ObservedTarget wire 不含 value：只承载容器稳定身份。
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
        draft_window = dict(validated.get("window") or {})
        evidence_window = {
            key: value for key, value in (
                ("title", draft_window.get("title")),
                ("processName", draft_window.get("processName")),
                ("handle", draft_window.get("handle")),
            ) if value is not None
        }
        persisted = draft_for_persistence(validated)
        render_region_summary(validated)  # 摘要渲染保持权威（校验不通过即保存失败）
        event = RawCaptureEvent(
            sequence=len(self._events) + 1,
            wall_time=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            monotonic_ms=self._now_ms(),
            type="assertion",
            scope=self.scope,
            observed_target=observed,
            evidence={"window": evidence_window} if evidence_window else {},
            assertion={
                "type": "region_query",
                "draft": persisted,
                "timeoutSeconds": (validated.get("observation") or {}).get(
                    "deadlineSeconds", 60.0),
            },
            causal_id=causal_id,
        )
        saved = self._append_assertion_event(
            event, request_id, window=dict(draft_window) or None,
        )
        self._assertion_window = None
        self._region_saved_window = None
        # 保存成功 = 区域断言流程结束：恢复浮窗/面板（同 cancel 语义）。
        restore = getattr(
            self._indicator, "restore_after_region_selection", None)
        if callable(restore):
            restore()
        return saved

    def recent_actions(self, limit: int = 5) -> list[dict]:
        """最近已录制动作清单（向导「绑定到最近动作」候选数据源）。

        只读投影：不含绑定语义，调用方自行决定是否把 causalId 写进 draft。
        时间为事件自带的墙钟/单调毫秒，供候选展示排序。
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise RecordingModelError(
                "type_error", "limit must be a positive integer",
                path="limit",
            )
        with self._lock:
            picked = [e for e in self._events
                      if e.type in ACTION_EVENT_TYPES][-limit:]
            actions: list[dict] = []
            for event in reversed(picked):
                target = event.observed_target
                evidence_window = (
                    event.evidence.get("window") if isinstance(
                        event.evidence, Mapping) else None
                ) or {}
                actions.append({
                    "sequence": event.sequence,
                    "type": event.type,
                    "causalId": event.causal_id,
                    "wallTime": event.wall_time,
                    "monotonicMs": event.monotonic_ms,
                    "controlType": getattr(target, "control_type", None),
                    "automationId": getattr(target, "automation_id", None),
                    "name": getattr(target, "text", None),
                    "windowTitle": evidence_window.get("title"),
                })
            return actions


    def _append_assertion_event(
        self, event: RawCaptureEvent, request_id: str | None,
        *, window: dict[str, Any] | None = None,
    ) -> dict:
        """Append an authored assertion under the lock.

        Shared by RECORDING and PAUSED submissions. Mirrors ingest's sequence
        check, evidence attachment and action counting, but replaces its
        capture-state gate — the gate for management saves is the one in
        add_assertion, which admits RECORDING/PAUSED and refuses everything
        else without resuming the session.
        """
        with self._lock:
            # 原子提交点：add_assertion 的入口校验与这里的追加之间隔着无锁的
            # 构造段，stop 可能在其中完成 —— 状态与 requestId 必须在同一把锁
            # 里复核，否则会向调用方谎报“已保存”，而交出的停止结果并没有它。
            if self._state not in {
                CaptureSessionState.RECORDING, CaptureSessionState.PAUSED,
            }:
                raise RecordingModelError(
                    "recording_state_invalid",
                    "the recording stopped before this assertion could be saved",
                    path="session.state",
                )
            if request_id is not None and request_id in self._assertion_results:
                return dict(self._assertion_results[request_id], duplicate=True)
            expected = len(self._events) + 1
            if event.sequence != expected:
                raise RecordingModelError(
                    "sequence_invalid",
                    f"expected sequence {expected}, got {event.sequence}",
                    path="event.sequence",
                )
            if self._evidence_attacher is not None:
                event = self._evidence_attacher(event)
            self._events.append(event)
            self._last_heartbeat = self._monotonic()
            self._action_count += 1
            self._indicator.update_count(self._action_count)
            result = {
                "ok": True,
                "requestId": request_id,
                "sequence": event.sequence,
                "actionCount": self._action_count,
                "state": self._state.value,
                "receipt": self._receipt().to_dict(),
            }
            if request_id is not None:
                self._assertion_results[request_id] = result
            return result

    # -- Assertion wizard: picking and typed draft commit (U2) -------------

    def _picker_controller(self) -> PickerController:
        if self._picker is None:
            self._picker = PickerController(
                self._source,
                previous_state_provider=lambda: self._state.value,
                restore_state=self._restore_picker_view,
            )
        return self._picker

    def _restore_picker_view(self, previous: str) -> None:
        """PICKING is a view sub-state; the capture state never left
        RECORDING/PAUSED.  Confirm/cancel only verify the session is still the
        one the picker started in — a stopped session refuses a late draft."""
        if self._state.value != previous:
            raise RecordingModelError(
                "session_stopped", "the recording ended while picking",
                path="session.state",
            )

    def begin_pick(self, binding: tuple[int, str] | None = None) -> dict:
        """Start picking.  ``binding=(sequence, causalId)`` marks the draft as
        the named action's result verifier (U3 panel guidance) — never the
        action that happens to be last at save time."""
        with self._lock:
            receipt = self._receipt()
            if binding is not None and (
                not isinstance(binding, tuple) or len(binding) != 2
            ):
                raise RecordingModelError(
                    "recording_assertion_invalid",
                    "binding must be (sequence, causalId)",
                    path="binding",
                )
            self._pick_binding = binding
        result = self._picker_controller().begin()
        return {**result, "sessionId": receipt.session_id}

    def update_pick_pointer(self, point) -> dict:
        """向导持续采样：把指针位置命中结果读进当前拾取状态（≤10Hz）。"""
        return self._picker_controller().update_pointer(point)

    def freeze_pick(self) -> dict:
        """Freeze the picked component into a default typed draft.

        The draft is held in this session only: committing it later validates
        session membership, so drafts cannot leak across sessions or outlive
        a stop."""
        draft = self._picker_controller().confirm()
        draft_id = draft["draft"]["draftId"]
        window = dict(self._assertion_window or {})
        draft["draft"]["window"] = window
        if getattr(self, "_pick_binding", None):
            draft["draft"]["binding"] = list(self._pick_binding)
        self._pending_drafts[draft_id] = draft["draft"]
        return draft

    def confirm_pick_with_type(self, assertion_type: str, typed_expected: Any) -> dict:
        """Freeze the picked component directly with a wizard-chosen type and
        expectation (the trial-then-save path)."""
        draft = self._picker_controller().confirm_with_type(assertion_type, typed_expected)
        draft_id = draft["draft"]["draftId"]
        window = dict(self._assertion_window or {})
        draft["draft"]["window"] = window
        if getattr(self, "_pick_binding", None):
            draft["draft"]["binding"] = list(self._pick_binding)
        self._pending_drafts[draft_id] = draft["draft"]
        return draft

    def cancel_pick(self) -> dict:
        return self._picker_controller().cancel()

    def commit_assertion_draft(self, draft: Mapping[str, Any]) -> dict:
        """Save a wizard draft through the existing assertion rules.

        The draftId is the requestId (U0 dedup applies).  Membership is
        checked first: a draft that is not pending in *this* session is
        unknown — never silently committed.
        """
        draft_id = draft.get("draftId")
        if isinstance(draft_id, str) and draft_id in self._assertion_results:
            # 已提交过的草稿重试：走 U0 的结果缓存返回同 sequence，
            # 不新增事件，也不把 pending 状态当丢失。
            self._pending_drafts.pop(draft_id, None)
            return dict(self._assertion_results[draft_id], duplicate=True)
        pending = self._pending_drafts.get(draft_id) if isinstance(draft_id, str) else None
        if pending is None:
            raise RecordingModelError(
                "recording_draft_unknown",
                "draft is unknown to this session (already saved, expired, or foreign)",
                path="draft.draftId",
            )
        merged = {**pending, **dict(draft)}
        # 校验失败必须保留草稿（表单修正后用同一 draftId 重试）；成功追加
        # 之后再移除。add_assertion 内部的 requestId 缓存挡住并发重复。
        payload = validate_assertion_draft_payload(merged)
        saved = self.add_assertion(payload)
        self._pending_drafts.pop(draft_id, None)
        return saved

    def control_inventory(self) -> list[dict]:
        """Flattened component list from the recording's own control-tree
        snapshots — the design's allowed fallback when native key picking is
        unavailable.  Only components with a stable identity are offered.

        macOS source 无事件驱动的快照机制：无快照时对当前 scope 窗口做一次
        现场 walk（source.control_tree → 生产 dump_tree），同样不依赖坐标。
        """
        entries: list[dict] = []
        snapshots = list(getattr(self._source, "control_snapshots", ()) or ())
        if not snapshots:
            walk = getattr(self._source, "control_tree", None)
            if callable(walk):
                try:
                    tree = walk(self.scope.window_title, 10)
                except Exception:
                    tree = None
                if isinstance(tree, Mapping) and tree.get("ok"):
                    snapshots = [{
                        "snapshotId": "live-walk",
                        "window": {
                            "title": self.scope.window_title,
                            "processName": tree.get("process_name")
                            or self.scope.process_name,
                        },
                        "controls": tree.get("controls") or [],
                    }]
        for snapshot in snapshots:
            window = dict(snapshot.get("window") or {})
            for control in snapshot.get("controls") or []:
                if not isinstance(control, Mapping):
                    continue
                component = {
                    "automationId": control.get("automationId") or control.get("automation_id"),
                    "controlType": control.get("controlType") or control.get("control_type"),
                    "name": control.get("name") or control.get("text"),
                    "value": control.get("value"),
                }
                if not component["automationId"] and not (
                    component["controlType"] and component["name"]
                ):
                    continue   # 无稳定身份的组件不进列表，也不会被包装成坐标断言
                # rect 仅用于实机框选高亮，绝不进入身份白名单（无 handle/pid）。
                rect = control.get("rect")
                if rect is None or not (
                    isinstance(rect, (list, tuple)) and len(rect) == 4
                    and all(isinstance(n, int) and not isinstance(n, bool)
                            for n in rect)
                ):
                    rect = None
                entry = {
                    "window": window,
                    "snapshotId": snapshot.get("snapshotId"),
                    "component": component,
                    "label": (f"{component.get('controlType') or '?'} · "
                              f"{component.get('name') or component.get('automationId')}"
                              f" · {window.get('title') or ''}"),
                }
                if rect is not None:
                    entry["rect"] = list(rect)
                entries.append(entry)
        return entries

    def freeze_component_draft(
        self, component: Mapping[str, Any], *, binding: tuple[int, str] | None = None,
    ) -> dict:
        """Freeze a list-selected component into a typed draft and open the
        wizard on the indicator.  Identity comes from the selected entry —
        no pointer hit test, no business click, no coordinates wrapped as an
        assertion."""
        from .assertion_draft import build_assertion_draft
        observed = {
            "automationId": component.get("automationId"),
            "controlType": component.get("controlType"),
            "name": component.get("name"),
            "text": component.get("name"),
            "value": component.get("value"),
            "protected": None,       # 列表条目不含脱敏状态，沿用保守策略
        }
        read_status = "ok" if component.get("value") is not None else "unknown"
        draft = build_assertion_draft(
            observed, "text_equals",
            str(component.get("value")) if component.get("value") is not None else "",
            read_status=read_status,
            protection_status=(str(observed["protected"])
                               if observed["protected"] is not None else None),
            window=dict(component.get("window") or {}),
            binding=getattr(self, "_pick_binding", None),
        )
        self._pending_drafts[draft.draft_id] = draft.to_dict()
        self._pick_binding = None
        self._indicator.open_assertion_editor(draft.to_dict())
        return {"ok": True, "draftId": draft.draft_id}

    def pending_verifiers(self) -> list[dict]:
        """Recorded scroll/drag actions that still have no bound result
        verifier — the data behind the panel's "第 N 个动作尚无结果验证" hint.
        Readiness itself stays the compiler's decision."""
        with self._lock:
            bound = {e.causal_id for e in self._events
                     if e.type == "assertion" and e.causal_id}
            return [
                {"sequence": e.sequence, "causalId": e.causal_id, "type": e.type}
                for e in self._events
                if e.type in VERIFIER_REQUIRED_EVENT_TYPES and e.causal_id not in bound
            ]

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
            self._shutdown_watchdog()
            with self._lock:
                return self._receipt(), self.recording()
        indicator_error = None
        try:
            # Tear the UI down first. Draining the correlator can take tens
            # of seconds when a correlation call is stuck, and doing that with
            # the recorder window still on screen makes a stop that is working
            # look like one that has hung.
            self._indicator.stop()
        except Exception as exc:
            # The indicator is display-only, but its error is still surfaced:
            # a source teardown that succeeds anyway cannot make a stop look
            # clean when the recorder window never came down.
            indicator_error = exc
        try:
            self._source.stop()
        except Exception as exc:
            with self._lock:
                self._state = CaptureSessionState.FAILED
                self._settled.set()
                self._reason = str(exc)
            if indicator_error is not None:
                raise exc from indicator_error
            raise
        if indicator_error is not None:
            with self._lock:
                self._state = CaptureSessionState.FAILED
                self._settled.set()
                self._reason = str(indicator_error)
            self._shutdown_watchdog()
            raise indicator_error
        with self._lock:
            self._state = CaptureSessionState.STOPPED
            self._settled.set()
        self._shutdown_watchdog()
        return self._receipt(), self.recording()

    def status(self) -> SessionReceipt:
        self._expire_before_operation()
        with self._lock:
            return self._receipt()

    def recording(self) -> RawRecording:
        correlator = getattr(self._source, "_correlator", None)
        windows = tuple(
            dict(entry) for entry in getattr(correlator, "window_registry", ()) or ()
        )
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
                # Input the user aimed at the target but that the recorder's
                # own always-on-top UI swallowed instead.
                "recorderUiEvents": int(getattr(
                    getattr(self._source, "_correlator", None),
                    "recorder_ui_events", 0,
                ) or 0),
                # Clicks that landed on nothing the application can name.
                # Not steps — replaying one would click a coordinate and hit
                # whatever happened to be there — but never silently dropped.
                "unidentifiedTargetEvents": int(getattr(
                    getattr(self._source, "_correlator", None),
                    "unidentified_target_events", 0,
                ) or 0),
            },
            windows=windows,
            control_snapshots=tuple(
                dict(entry)
                for entry in getattr(self._source, "control_snapshots", ()) or ()
            ),
            # Persisted, not just counted. A capture that dropped a click the
            # user remembers making is only explainable if the recording says
            # which window that click resolved to and why it was refused.
            rejections=tuple(
                dict(entry) for entry in getattr(correlator, "rejections", ()) or ()
            ),
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
