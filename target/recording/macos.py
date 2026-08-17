"""macOS CGEventTap capture and Accessibility-backed correlation."""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .models import CaptureScope, RawCaptureEvent, RecordingModelError
from .source import CompositeHookDriver, HookPacket, QueuedCaptureSource
from .windows import WindowsUIACorrelator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MacOSPermissionPreflight:
    def check(self) -> None:  # pragma: no cover - live macOS only
        if sys.platform != "darwin":
            raise RecordingModelError(
                "recording_capture_unavailable",
                "macOS event capture requires Darwin",
                path="scope.backend",
            )
        try:
            import Quartz
        except Exception as exc:
            raise RecordingModelError(
                "recording_capture_unavailable", f"Quartz unavailable: {exc}"
            ) from exc
        accessibility = bool(Quartz.AXIsProcessTrusted())
        input_monitoring = True
        preflight = getattr(Quartz, "CGPreflightListenEventAccess", None)
        if callable(preflight):
            input_monitoring = bool(preflight())
        missing = []
        if not accessibility:
            missing.append("Accessibility")
        if not input_monitoring:
            missing.append("Input Monitoring")
        if missing:
            raise RecordingModelError(
                "recording_permission_missing",
                "missing macOS permissions: " + ", ".join(missing),
                path="recording.permissions",
            )


class MacOSEventTapDriver:
    """Listen-only CGEventTap whose callback only emits small packets."""

    _KEYS = {
        36: "ENTER", 48: "TAB", 49: "SPACE", 51: "BACKSPACE", 53: "ESCAPE",
        115: "HOME", 116: "PAGEUP", 117: "DELETE", 119: "END", 121: "PAGEDOWN",
        123: "LEFT", 124: "RIGHT", 125: "DOWN", 126: "UP",
    }
    _COMMAND_KEYS = {
        0: "A", 1: "S", 2: "D", 3: "F", 6: "Z", 7: "X", 8: "C", 9: "V",
    }
    _MODIFIER_KEY_CODES = frozenset({54, 55, 56, 57, 58, 59, 60, 61, 62})

    def __init__(self, preflight: MacOSPermissionPreflight | None = None) -> None:
        self._preflight = preflight or MacOSPermissionPreflight()
        self._emit: Callable[[HookPacket], None] | None = None
        self._thread: threading.Thread | None = None
        self._run_loop = None
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._callback = None
        self._tap = None

    @staticmethod
    def _modifiers(flags: int, Quartz) -> tuple[str, ...]:
        values = []
        for mask, name in (
            (Quartz.kCGEventFlagMaskControl, "CTRL"),
            (Quartz.kCGEventFlagMaskAlternate, "ALT"),
            (Quartz.kCGEventFlagMaskShift, "SHIFT"),
            (Quartz.kCGEventFlagMaskCommand, "META"),
        ):
            if flags & mask:
                values.append(name)
        return tuple(values)

    def _run(self) -> None:  # pragma: no cover - live macOS only
        try:
            self._preflight.check()
            import Quartz

            event_types = (
                Quartz.kCGEventLeftMouseDown,
                Quartz.kCGEventRightMouseDown,
                Quartz.kCGEventOtherMouseDown,
                Quartz.kCGEventLeftMouseUp,
                Quartz.kCGEventRightMouseUp,
                Quartz.kCGEventOtherMouseUp,
                Quartz.kCGEventScrollWheel,
                Quartz.kCGEventKeyUp,
            )
            mask = 0
            for event_type in event_types:
                mask |= Quartz.CGEventMaskBit(event_type)

            def callback(proxy, event_type, event, refcon):
                del proxy, refcon
                if self._emit is None:
                    return event
                now_ms = int(time.monotonic() * 1000)
                flags = int(Quartz.CGEventGetFlags(event))
                modifiers = self._modifiers(flags, Quartz)
                pointer_buttons = {
                    Quartz.kCGEventLeftMouseDown: ("pointer_down", "left"),
                    Quartz.kCGEventRightMouseDown: ("pointer_down", "right"),
                    Quartz.kCGEventOtherMouseDown: ("pointer_down", "middle"),
                    Quartz.kCGEventLeftMouseUp: ("pointer_up", "left"),
                    Quartz.kCGEventRightMouseUp: ("pointer_up", "right"),
                    Quartz.kCGEventOtherMouseUp: ("pointer_up", "middle"),
                }
                if event_type in pointer_buttons:
                    point = Quartz.CGEventGetLocation(event)
                    kind, button = pointer_buttons[event_type]
                    self._emit(HookPacket(
                        kind, now_ms, _utc_now(),
                        screen_point=(int(point.x), int(point.y)),
                        button=button, modifiers=modifiers,
                    ))
                elif event_type == Quartz.kCGEventScrollWheel:
                    point = Quartz.CGEventGetLocation(event)
                    delta = Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGScrollWheelEventDeltaAxis1
                    )
                    self._emit(HookPacket(
                        "scroll", now_ms, _utc_now(),
                        screen_point=(int(point.x), int(point.y)),
                        modifiers=modifiers, native={"delta": int(delta) * 120},
                    ))
                elif event_type == Quartz.kCGEventKeyUp:
                    key_code = int(Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGKeyboardEventKeycode
                    ))
                    if key_code in self._MODIFIER_KEY_CODES:
                        return event
                    key = self._KEYS.get(key_code) or (
                        self._COMMAND_KEYS.get(key_code) if modifiers else None
                    )
                    # Ordinary character keycodes are never decoded. Command
                    # combinations retain only the keycode label.
                    command_modifiers = set(modifiers).intersection({"CTRL", "ALT", "META"})
                    textual = (
                        key is None
                    ) and not command_modifiers
                    if textual:
                        self._emit(HookPacket(
                            "text_activity", now_ms, _utc_now(),
                        ))
                    elif key is not None:
                        self._emit(HookPacket(
                            "key_command", now_ms, _utc_now(),
                            key=key, modifiers=modifiers,
                        ))
                return event

            self._callback = callback
            self._tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,
                mask,
                callback,
                None,
            )
            if self._tap is None:
                raise RecordingModelError(
                    "recording_permission_missing", "CGEventTapCreate returned NULL"
                )
            source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            self._run_loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(
                self._run_loop, source, Quartz.kCFRunLoopCommonModes
            )
            Quartz.CGEventTapEnable(self._tap, True)
            self._ready.set()
            Quartz.CFRunLoopRun()
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()

    def start(self, emit: Callable[[HookPacket], None]) -> None:
        self._emit = emit
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, name="edr-wd-macos-event-tap", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RecordingModelError("recording_permission_missing", "CGEventTap startup timed out")
        if self._startup_error is not None:
            if isinstance(self._startup_error, RecordingModelError):
                raise self._startup_error
            raise RecordingModelError(
                "recording_permission_missing", str(self._startup_error),
                path="recording.permissions",
            )

    def stop(self) -> None:
        if self._run_loop is not None:  # pragma: no cover - live macOS only
            import Quartz

            Quartz.CFRunLoopStop(self._run_loop)
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("macOS event-tap thread did not stop")


class MacOSAXResolver:
    """Use the existing AX backend tree as the correlation boundary."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def foreground(self) -> Mapping[str, object]:
        lock_result = self._backend.get_window_lock()
        lock = lock_result.get("lock") if isinstance(lock_result, Mapping) else None
        lock = lock if isinstance(lock, Mapping) else {}
        snapshot = lock.get("snapshot") if isinstance(lock.get("snapshot"), Mapping) else {}
        return {
            "processName": lock.get("process_name") or snapshot.get("process_name") or "",
            "windowTitle": snapshot.get("title") or "",
            "pid": lock.get("pid") or snapshot.get("pid"),
        }

    @staticmethod
    def _rectangle(control: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
        rect = control.get("rectangle") or control.get("rect")
        if not isinstance(rect, Mapping):
            return None
        if all(key in rect for key in ("x", "y", "w", "h")):
            x, y = int(rect["x"]), int(rect["y"])
            return x, y, x + int(rect["w"]), y + int(rect["h"])
        return None

    def _describe(self, control: Mapping[str, Any]) -> Mapping[str, object]:
        protected = control.get("protected", control.get("is_password"))
        return {
            "controlType": control.get("control_type") or control.get("role") or control.get("class_name"),
            "automationId": control.get("automation_id"),
            "identifier": control.get("identifier"),
            "text": control.get("text") or control.get("name") or control.get("title"),
            "rect": list(self._rectangle(control) or ()),
            "protected": protected,
            "value": None if protected is not False else control.get("value"),
            "toggleState": control.get("checked"),
            "selected": control.get("selected"),
            "ancestry": control.get("ancestry") or [],
            "_identity": {
                "automation_id": control.get("automation_id"),
                "identifier": control.get("identifier"),
                "text": control.get("text") or control.get("name") or control.get("title"),
            },
        }

    def _controls(self) -> list[Mapping[str, Any]]:
        result = self._backend.dump_tree(max_depth=12)
        if not result.get("ok"):
            return []
        return [item for item in result.get("controls", []) if isinstance(item, Mapping)]

    def element_at(self, x: int, y: int) -> Mapping[str, object] | None:
        hits = []
        for control in self._controls():
            rect = self._rectangle(control)
            if rect and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                hits.append((max(1, (rect[2] - rect[0]) * (rect[3] - rect[1])), control))
        if not hits:
            return None
        return self._describe(min(hits, key=lambda item: item[0])[1])

    def refresh(self, target: Mapping[str, object]) -> Mapping[str, object]:
        identity = target.get("_identity") or {}
        for control in self._controls():
            described = self._describe(control)
            if described.get("_identity") == identity:
                return described
        return target

    def focused(self) -> Mapping[str, object] | None:
        for control in self._controls():
            if control.get("focused") is True:
                return self._describe(control)
        return None


class MacOSWindowEventDriver:
    """Poll the scoped process's on-screen window list for open/close events.

    macOS has no listen-only equivalent of ``SetWinEventHook`` that works
    without owning the target application's AX observer, so this driver diffs
    ``CGWindowListCopyWindowInfo`` on its own thread.  It only reads, and the
    poll interval bounds how stale a transition can be, not whether it is
    correlated: :class:`WindowsUIACorrelator` still requires a recent action.
    """

    def __init__(self, process_name: str, *, interval_seconds: float = 0.25) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.process_name = process_name
        self._interval = interval_seconds
        self._emit: Callable[[HookPacket], None] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @staticmethod
    def _matches(actual: object, expected: str) -> bool:
        return str(actual or "").lower().removesuffix(".exe") == expected.lower().removesuffix(".exe")

    def _windows(self) -> dict[int, tuple[str, int]]:  # pragma: no cover - live macOS only
        import Quartz

        listing = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        found: dict[int, tuple[str, int]] = {}
        for item in listing:
            owner = item.get("kCGWindowOwnerName")
            if not self._matches(owner, self.process_name):
                continue
            number = item.get("kCGWindowNumber")
            if number is None:
                continue
            found[int(number)] = (
                str(item.get("kCGWindowName") or ""),
                int(item.get("kCGWindowOwnerPID") or 0),
            )
        return found

    def _run(self) -> None:  # pragma: no cover - live macOS only
        try:
            known = self._windows()
        except Exception:
            known = {}
        while not self._stop.wait(self._interval):
            try:
                current = self._windows()
            except Exception:
                continue
            if self._emit is None:
                continue
            for number, (title, pid) in current.items():
                if number not in known:
                    self._packet("opened", title, pid)
            for number, (title, pid) in known.items():
                if number not in current:
                    self._packet("closed", title, pid)
            known = current

    def _packet(self, kind: str, title: str, pid: int) -> None:  # pragma: no cover - live macOS only
        self._emit(HookPacket(
            kind="window_transition",
            monotonic_ms=int(time.monotonic() * 1000),
            wall_time=_utc_now(),
            native={
                "kind": kind,
                "processName": self.process_name,
                "title": title,
                "pid": pid or None,
            },
        ))

    def start(self, emit: Callable[[HookPacket], None]) -> None:
        self._emit = emit
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="edr-wd-recording-macos-windows", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None


class MacOSAXCorrelator(WindowsUIACorrelator):
    """Shared event semantics with an AX-backed resolver."""

    @staticmethod
    def _cursor_position() -> tuple[int, int] | None:  # pragma: no cover - live macOS only
        if sys.platform != "darwin":
            return None
        import Quartz

        point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return int(point.x), int(point.y)


def macos_source_factory(
    backend: Any,
) -> Callable[[CaptureScope, Callable[[RawCaptureEvent], bool]], QueuedCaptureSource]:
    def create(scope: CaptureScope, sink: Callable[[RawCaptureEvent], bool]) -> QueuedCaptureSource:
        if sys.platform != "darwin" or backend is None:
            raise RecordingModelError(
                "recording_capture_unavailable",
                "macos_accessibility recording requires a live macOS backend",
                path="scope.backend",
            )
        return QueuedCaptureSource(
            scope=scope,
            sink=sink,
            driver=CompositeHookDriver(
                MacOSEventTapDriver(),
                MacOSWindowEventDriver(scope.process_name),
            ),
            correlator=MacOSAXCorrelator(MacOSAXResolver(backend)),
        )

    return create


__all__ = [
    "MacOSAXCorrelator",
    "MacOSAXResolver",
    "MacOSEventTapDriver",
    "MacOSPermissionPreflight",
    "MacOSWindowEventDriver",
    "macos_source_factory",
]
