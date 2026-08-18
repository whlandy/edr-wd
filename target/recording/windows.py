"""Windows low-level input hooks and UI Automation event correlation.

The native hook callbacks only construct :class:`HookPacket` objects and call
the queue emitter.  Foreground ownership checks and UIA hit testing happen on
the queue worker through :class:`WindowsUIACorrelator`.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Mapping

from .models import (
    ACTION_EVENT_TYPES,
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RecordingModelError,
)
from .source import CompositeHookDriver, HookPacket, QueuedCaptureSource


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WindowsLowLevelHookDriver:
    """Own WH_MOUSE_LL/WH_KEYBOARD_LL hooks on a dedicated message-loop thread."""

    WH_KEYBOARD_LL = 13
    WH_MOUSE_LL = 14
    WM_QUIT = 0x0012
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONDOWN = 0x0204
    WM_RBUTTONUP = 0x0205
    WM_MBUTTONDOWN = 0x0207
    WM_MBUTTONUP = 0x0208
    WM_MOUSEWHEEL = 0x020A

    _KEYS = {
        0x08: "BACKSPACE", 0x09: "TAB", 0x0D: "ENTER", 0x1B: "ESCAPE",
        0x20: "SPACE", 0x21: "PAGEUP", 0x22: "PAGEDOWN", 0x23: "END",
        0x24: "HOME", 0x25: "LEFT", 0x26: "UP", 0x27: "RIGHT",
        0x28: "DOWN", 0x2E: "DELETE",
    }
    _MODIFIER_VKS = frozenset({0x10, 0x11, 0x12, 0x5B, 0x5C})

    def __init__(self) -> None:
        self._emit: Callable[[HookPacket], None] | None = None
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._mouse_hook = None
        self._keyboard_hook = None
        self._callbacks: tuple[object, object] | None = None

    def _modifiers(self, user32) -> tuple[str, ...]:
        active = []
        for code, name in ((0x11, "CTRL"), (0x12, "ALT"), (0x10, "SHIFT"), (0x5B, "META"), (0x5C, "META")):
            if user32.GetAsyncKeyState(code) & 0x8000 and name not in active:
                active.append(name)
        return tuple(active)

    def _key_name(self, vk_code: int) -> str | None:
        if vk_code in self._KEYS:
            return self._KEYS[vk_code]
        if 0x70 <= vk_code <= 0x87:
            return f"F{vk_code - 0x6F}"
        if 0x30 <= vk_code <= 0x39 or 0x41 <= vk_code <= 0x5A:
            return chr(vk_code)
        return None

    def _run(self) -> None:  # pragma: no cover - exercised on Windows acceptance target
        try:
            if sys.platform != "win32":
                raise RecordingModelError(
                    "recording_capture_unavailable",
                    "Windows low-level hooks require win32",
                    path="scope.backend",
                )
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ULONG_PTR = ctypes.c_size_t

            class MSLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [
                    ("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR),
                ]

            class KBDLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [
                    ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR),
                ]

            hook_proc = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t
            )
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int, hook_proc, ctypes.c_void_p, wintypes.DWORD,
            ]
            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            user32.CallNextHookEx.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t,
            ]
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p

            def mouse_callback(code, message, pointer):
                if code >= 0 and self._emit is not None and message in {
                    self.WM_LBUTTONDOWN, self.WM_LBUTTONUP,
                    self.WM_RBUTTONDOWN, self.WM_RBUTTONUP,
                    self.WM_MBUTTONDOWN, self.WM_MBUTTONUP, self.WM_MOUSEWHEEL,
                }:
                    data = ctypes.cast(pointer, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    button = {
                        self.WM_LBUTTONDOWN: "left", self.WM_LBUTTONUP: "left",
                        self.WM_RBUTTONDOWN: "right", self.WM_RBUTTONUP: "right",
                        self.WM_MBUTTONDOWN: "middle", self.WM_MBUTTONUP: "middle",
                    }.get(message)
                    native = {}
                    kind = "pointer_up"
                    if message in {
                        self.WM_LBUTTONDOWN, self.WM_RBUTTONDOWN, self.WM_MBUTTONDOWN,
                    }:
                        kind = "pointer_down"
                    elif message == self.WM_MOUSEWHEEL:
                        kind = "scroll"
                        delta = ctypes.c_short((data.mouseData >> 16) & 0xFFFF).value
                        native = {"delta": int(delta)}
                    self._emit(HookPacket(
                        kind=kind,
                        monotonic_ms=int(time.monotonic() * 1000),
                        wall_time=_utc_now(),
                        screen_point=(int(data.pt.x), int(data.pt.y)),
                        button=button,
                        modifiers=self._modifiers(user32),
                        native=native,
                    ))
                return user32.CallNextHookEx(None, code, message, pointer)

            def keyboard_callback(code, message, pointer):
                if code >= 0 and message in {self.WM_KEYUP, self.WM_SYSKEYUP} and self._emit is not None:
                    data = ctypes.cast(pointer, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    vk_code = int(data.vkCode)
                    if vk_code in self._MODIFIER_VKS:
                        return user32.CallNextHookEx(None, code, message, pointer)
                    key = self._key_name(vk_code)
                    modifiers = self._modifiers(user32)
                    # Never enqueue ordinary character input.  Text is read as
                    # a final UIA value by a commit observer, not from keystrokes.
                    command_modifiers = set(modifiers).intersection({"CTRL", "ALT", "META"})
                    textual = (
                        key is None
                        or key in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                    ) and not command_modifiers
                    if textual:
                        self._emit(HookPacket(
                            kind="text_activity",
                            monotonic_ms=int(time.monotonic() * 1000),
                            wall_time=_utc_now(),
                        ))
                    elif key:
                        self._emit(HookPacket(
                            kind="key_command",
                            monotonic_ms=int(time.monotonic() * 1000),
                            wall_time=_utc_now(),
                            key=key,
                            modifiers=modifiers,
                        ))
                return user32.CallNextHookEx(None, code, message, pointer)

            mouse_proc = hook_proc(mouse_callback)
            keyboard_proc = hook_proc(keyboard_callback)
            self._callbacks = (mouse_proc, keyboard_proc)
            module = kernel32.GetModuleHandleW(None)
            self._mouse_hook = user32.SetWindowsHookExW(self.WH_MOUSE_LL, mouse_proc, module, 0)
            self._keyboard_hook = user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, keyboard_proc, module, 0)
            if not self._mouse_hook or not self._keyboard_hook:
                raise OSError(ctypes.get_last_error(), "SetWindowsHookExW failed")
            self._thread_id = int(kernel32.GetCurrentThreadId())
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            if sys.platform == "win32":
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                for hook in (self._mouse_hook, self._keyboard_hook):
                    if hook:
                        user32.UnhookWindowsHookEx(hook)

    def start(self, emit: Callable[[HookPacket], None]) -> None:
        self._emit = emit
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, name="edr-wd-windows-hooks", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RecordingModelError("recording_permission_missing", "Windows hook startup timed out")
        if self._startup_error is not None:
            raise RecordingModelError(
                "recording_permission_missing",
                str(self._startup_error),
                path="recording.permissions",
            )

    def stop(self) -> None:
        if self._thread_id is not None and sys.platform == "win32":
            ctypes.WinDLL("user32", use_last_error=True).PostThreadMessageW(
                self._thread_id, self.WM_QUIT, 0, 0
            )
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("Windows hook thread did not stop")


class WindowsUIAResolver:
    """Resolve foreground ownership and one UIA element for a hook packet."""

    def foreground(self) -> Mapping[str, object]:  # pragma: no cover - Windows only
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        import psutil

        return {
            "processName": psutil.Process(pid.value).name(),
            "windowTitle": title.value,
            "pid": int(pid.value),
        }

    def _describe(self, wrapper) -> Mapping[str, object]:  # pragma: no cover - Windows only
        info = wrapper.element_info
        rectangle = wrapper.rectangle()
        protected = getattr(info, "is_password", None)
        if protected is None:
            try:
                protected = bool(wrapper.legacy_properties().get("IsPassword"))
            except Exception:
                protected = None
        value = None
        if protected is False:
            try:
                value = wrapper.iface_value.CurrentValue
            except Exception:
                try:
                    value = wrapper.window_text()
                except Exception:
                    pass
        toggle_state = None
        try:
            toggle_state = bool(wrapper.get_toggle_state())
        except Exception:
            pass
        selected = None
        try:
            selected = bool(wrapper.is_selected())
        except Exception:
            pass
        ancestry = []
        try:
            parent = wrapper.parent()
            for _ in range(6):
                if parent is None:
                    break
                parent_info = parent.element_info
                item = {
                    "controlType": getattr(parent_info, "control_type", None),
                    "automationId": getattr(parent_info, "automation_id", None),
                    "name": getattr(parent_info, "name", None),
                }
                ancestry.append({
                    key: str(value) for key, value in item.items()
                    if value not in (None, "")
                })
                parent = parent.parent()
        except Exception:
            ancestry = []
        return {
            "controlType": getattr(info, "control_type", None),
            "automationId": getattr(info, "automation_id", None),
            "text": getattr(info, "name", None),
            "rect": [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom],
            "protected": protected,
            "value": value,
            "toggleState": toggle_state,
            "selected": selected,
            "ancestry": ancestry,
            "_nativeElement": wrapper,
        }

    def element_at(self, x: int, y: int) -> Mapping[str, object] | None:  # pragma: no cover - Windows only
        from pywinauto import Desktop

        wrapper = Desktop(backend="uia").from_point(x, y)
        return self._describe(wrapper)

    def refresh(self, target: Mapping[str, object]) -> Mapping[str, object]:  # pragma: no cover - Windows only
        return self._describe(target["_nativeElement"])

    def focused(self) -> Mapping[str, object] | None:  # pragma: no cover - Windows only
        from pywinauto import Desktop

        hwnd = ctypes.WinDLL("user32", use_last_error=True).GetForegroundWindow()
        window = Desktop(backend="uia").window(handle=hwnd)
        for wrapper in (window, *window.descendants()):
            try:
                if wrapper.has_keyboard_focus():
                    return self._describe(wrapper)
            except Exception:
                continue
        return None


class WindowsUIACorrelator:
    def __init__(
        self,
        resolver: WindowsUIAResolver | None = None,
        *,
        double_click_ms: int | None = None,
        drag_threshold: tuple[int, int] | None = None,
        transition_window_ms: int = 3000,
    ) -> None:
        self._resolver = resolver or WindowsUIAResolver()
        self._active_edit: Mapping[str, object] | None = None
        self._edit_dirty = False
        self._pending_click: tuple[int, str, str, str] | None = None
        self._pending_press: dict[str, object] | None = None
        self._last_action_causal_id: str | None = None
        self._last_action_ms: int | None = None
        # Windows this recording caused to open.  Input inside them belongs to
        # the flow being recorded even though their titles cannot match the
        # scope regex the user supplied for the entry window.
        self._derived_scope_titles: set[str] = set()
        self.out_of_scope_events = 0
        if transition_window_ms < 0:
            raise ValueError("transition_window_ms must be non-negative")
        self._transition_window_ms = transition_window_ms
        self._drag_threshold = drag_threshold if drag_threshold is not None else self._system_drag_threshold()
        if (
            not isinstance(self._drag_threshold, tuple) or len(self._drag_threshold) != 2
            or not all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 1
                for value in self._drag_threshold
            )
        ):
            raise ValueError("drag_threshold must be two positive integers")
        if double_click_ms is not None and (
            not isinstance(double_click_ms, int) or isinstance(double_click_ms, bool)
            or not 1 <= double_click_ms <= 5000
        ):
            raise ValueError("double_click_ms must be an integer in [1, 5000]")
        self._double_click_ms = (
            double_click_ms if double_click_ms is not None
            else self._system_double_click_ms()
        )

    @staticmethod
    def _system_double_click_ms() -> int:
        try:
            if sys.platform == "win32":
                value = int(ctypes.WinDLL("user32", use_last_error=True).GetDoubleClickTime())
                if 1 <= value <= 5000:
                    return value
            if sys.platform == "darwin":
                from AppKit import NSEvent

                value = round(float(NSEvent.doubleClickInterval()) * 1000)
                if 1 <= value <= 5000:
                    return value
        except Exception:
            pass
        return 500

    @staticmethod
    def _system_drag_threshold() -> tuple[int, int]:
        """Read the OS drag threshold so a shaky click is not recorded as a drag."""
        try:
            if sys.platform == "win32":
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                width = int(user32.GetSystemMetrics(68))  # SM_CXDRAG
                height = int(user32.GetSystemMetrics(69))  # SM_CYDRAG
                if width >= 1 and height >= 1:
                    return width, height
        except Exception:
            pass
        return 4, 4

    @staticmethod
    def _cursor_position() -> tuple[int, int] | None:  # pragma: no cover - Windows only
        if sys.platform != "win32":
            return None
        from ctypes import wintypes

        point = wintypes.POINT()
        if not ctypes.WinDLL("user32", use_last_error=True).GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    @staticmethod
    def _process_matches(actual: object, expected: str) -> bool:
        return str(actual or "").lower().removesuffix(".exe") == expected.lower().removesuffix(".exe")

    @staticmethod
    def _observed(target_data: Mapping[str, object] | None) -> ObservedTarget | None:
        if not target_data:
            return None
        stable_identity = {
            key: target_data.get(key)
            for key in ("controlType", "automationId", "identifier", "text", "rect")
        }
        stable = json.dumps(stable_identity, sort_keys=True, ensure_ascii=False, default=str).encode()
        return ObservedTarget(
            snapshot_id="OBS-" + uuid.uuid4().hex,
            target_id="T0001",
            fingerprint="sha256:" + hashlib.sha256(stable).hexdigest(),
            control_type=target_data.get("controlType"),
            automation_id=target_data.get("automationId"),
            identifier=target_data.get("identifier"),
            text=target_data.get("text"),
            rect=tuple(target_data["rect"]) if target_data.get("rect") else None,
            ancestry=tuple(
                {
                    str(key): str(value) for key, value in item.items()
                    if value is not None
                }
                for item in (target_data.get("ancestry") or [])
                if isinstance(item, Mapping)
            ),
            protected=target_data.get("protected"),
        )

    def current_target(self, scope: CaptureScope) -> ObservedTarget | None:
        """Hit-test the current pointer while preserving the recording scope."""
        foreground = self._resolver.foreground()
        if not self._process_matches(foreground.get("processName"), scope.process_name):
            return None
        try:
            if not re.search(scope.window_title, str(foreground.get("windowTitle") or "")):
                return None
        except re.error as exc:
            raise RecordingModelError(
                "recording_scope_not_unique", str(exc), path="scope.windowTitle"
            ) from exc
        point_getter = getattr(self._resolver, "cursor_position", None)
        point = point_getter() if callable(point_getter) else self._cursor_position()
        if point is None:
            return None
        return self._observed(self._resolver.element_at(*point))

    @classmethod
    def _stable_identity(cls, target_data: Mapping[str, object] | None) -> dict[str, object] | None:
        """Semantic identity of a control, without observation-local IDs."""
        observed = cls._observed(target_data)
        if observed is None:
            return None
        return {
            "controlType": observed.control_type,
            "automationId": observed.automation_id,
            "identifier": observed.identifier,
            "text": observed.text,
            "ancestry": [dict(item) for item in observed.ancestry],
            "fingerprint": observed.fingerprint,
            "rect": list(observed.rect) if observed.rect else None,
        }

    def _drag_input(
        self,
        press: Mapping[str, object] | None,
        packet: HookPacket,
        target_data: Mapping[str, object] | None,
    ) -> dict[str, object] | None:
        """Classify a press/release pair as a drag once it clears the OS threshold."""
        if press is None or press.get("observed") is None:
            return None
        if press.get("button") != (packet.button or "left"):
            return None
        start = press.get("point")
        end = tuple(packet.screen_point or ())
        if not isinstance(start, tuple) or len(start) != 2 or len(end) != 2:
            return None
        if (
            abs(end[0] - start[0]) <= self._drag_threshold[0]
            and abs(end[1] - start[1]) <= self._drag_threshold[1]
        ):
            return None
        elapsed_ms = max(0, packet.monotonic_ms - int(press.get("monotonic_ms") or 0))
        return {
            "button": press["button"],
            "screenPoint": list(start),
            "endPoint": list(end),
            "durationSeconds": round(elapsed_ms / 1000, 3),
            "endTarget": self._stable_identity(target_data),
        }

    def _causal_id(
        self,
        event_type: str,
        packet: HookPacket,
        observed: ObservedTarget | None,
    ) -> str:
        causal_id = "CAUSE-" + uuid.uuid4().hex
        if event_type != "pointer_click" or observed is None:
            return causal_id
        button = packet.button or "left"
        previous = self._pending_click
        if (
            previous is not None
            and 0 <= packet.monotonic_ms - previous[0] <= self._double_click_ms
            and previous[1] == observed.fingerprint
            and previous[2] == button
        ):
            self._pending_click = None
            return previous[3]
        self._pending_click = (
            packet.monotonic_ms, observed.fingerprint, button, causal_id,
        )
        return causal_id

    def _text_commit(
        self,
        *,
        packet: HookPacket,
        scope: CaptureScope,
        sequence: int,
        foreground: Mapping[str, object],
    ) -> RawCaptureEvent | None:
        if self._active_edit is None:
            return None
        target = self._resolver.refresh(self._active_edit)
        if not self._edit_dirty:
            self._active_edit = None
            return None
        protected = target.get("protected")
        if protected is True:
            event_input = {
                "textSource": {"kind": "env", "name": f"EDR_WD_SECRET_{sequence}"}
            }
        elif protected is False:
            event_input = {"value": target.get("value", "")}
        else:
            # Unknown sensitivity must never copy the live value.  The compiler
            # leaves this step incomplete for explicit secret review.
            event_input = {}
        self._active_edit = None
        self._edit_dirty = False
        return RawCaptureEvent(
            sequence=sequence,
            wall_time=packet.wall_time,
            monotonic_ms=packet.monotonic_ms,
            type="text_commit",
            scope=scope,
            input=event_input,
            observed_target=self._observed(target),
            evidence={"foregroundPid": foreground.get("pid")},
            causal_id="CAUSE-" + uuid.uuid4().hex,
        )

    def flush(
        self,
        scope: CaptureScope,
        sequence: int,
    ) -> RawCaptureEvent | None:
        """Commit the final focused value when recording stops."""
        foreground = self._resolver.foreground()
        if not self._process_matches(foreground.get("processName"), scope.process_name):
            return None
        try:
            if not re.search(scope.window_title, str(foreground.get("windowTitle") or "")):
                return None
        except re.error:
            return None
        return self._text_commit(
            packet=HookPacket(
                kind="flush",
                monotonic_ms=int(time.monotonic() * 1000),
                wall_time=_utc_now(),
            ),
            scope=scope,
            sequence=sequence,
            foreground=foreground,
        )

    def _in_scope(self, foreground: Mapping[str, object], scope: CaptureScope) -> bool:
        """Is this foreground window part of the recording's scope?

        The scope starts as the user's process + window-title regex and grows
        to include every window the recording causally opened.  Without that,
        a click that opens a dialog is recorded but everything the user then
        does inside the dialog is silently dropped.
        """
        if not self._process_matches(foreground.get("processName"), scope.process_name):
            return False
        title = str(foreground.get("windowTitle") or "")
        try:
            if re.search(scope.window_title, title):
                return True
        except re.error as exc:
            raise RecordingModelError(
                "recording_scope_not_unique", str(exc), path="scope.windowTitle"
            )
        return title in self._derived_scope_titles

    def _window_transition(
        self,
        packet: HookPacket,
        scope: CaptureScope,
        sequence: int,
    ) -> RawCaptureEvent | None:
        """Bind a platform window event to the action that caused it.

        Window scope is deliberately process-only here: a click usually opens a
        *different* window (a dialog) whose title cannot match the recording's
        window-title regex.  A transition that no recent user action explains is
        background noise rather than input, and is not recorded at all — every
        transition this recorder persists is causally bound by construction.
        """
        kind = packet.native.get("kind")
        if kind not in {"opened", "closed"}:
            return None
        process_name = packet.native.get("processName")
        if not self._process_matches(process_name, scope.process_name):
            return None
        if self._last_action_causal_id is None or self._last_action_ms is None:
            return None
        latency_ms = packet.monotonic_ms - self._last_action_ms
        if not 0 <= latency_ms <= self._transition_window_ms:
            return None
        event_input: dict[str, object] = {
            "kind": kind,
            "processName": str(process_name or scope.process_name),
            # Replay waits proportionally to what the recording actually
            # observed, never on a fixed sleep.
            "timeoutSeconds": min(30.0, max(5.0, round(latency_ms * 3 / 1000, 1))),
        }
        title = packet.native.get("title")
        if isinstance(title, str) and title:
            event_input["title"] = title
            if kind == "opened":
                self._derived_scope_titles.add(title)
            else:
                self._derived_scope_titles.discard(title)
        return RawCaptureEvent(
            sequence=sequence,
            wall_time=packet.wall_time,
            monotonic_ms=packet.monotonic_ms,
            type="window_transition",
            scope=scope,
            input=event_input,
            evidence={"foregroundPid": packet.native.get("pid")},
            causal_id=self._last_action_causal_id,
        )

    def correlate(
        self,
        packet: HookPacket,
        scope: CaptureScope,
        sequence: int,
    ) -> RawCaptureEvent | tuple[RawCaptureEvent, ...] | None:
        if packet.kind == "window_transition":
            return self._window_transition(packet, scope, sequence)
        result = self._correlate(packet, scope, sequence)
        produced = (
            result if isinstance(result, tuple)
            else () if result is None else (result,)
        )
        for event in produced:
            if event.type in ACTION_EVENT_TYPES:
                self._last_action_causal_id = event.causal_id
                self._last_action_ms = event.monotonic_ms
        return result

    def _correlate(
        self,
        packet: HookPacket,
        scope: CaptureScope,
        sequence: int,
    ) -> RawCaptureEvent | tuple[RawCaptureEvent, ...] | None:
        foreground = self._resolver.foreground()
        if not self._in_scope(foreground, scope):
            # Out-of-scope input is expected (the user may alt-tab), but it
            # must be countable so a short recording is never mistaken for a
            # complete one.
            self.out_of_scope_events += 1
            return None

        target_data = None
        if packet.screen_point is not None:
            target_data = self._resolver.element_at(*packet.screen_point)
        elif packet.kind == "key_command":
            focused = getattr(self._resolver, "focused", None)
            target_data = focused() if callable(focused) else None
        observed = self._observed(target_data)

        if packet.kind == "pointer_down":
            # A press only becomes evidence once its release proves whether the
            # user clicked or dragged.  Nothing is persisted here.
            self._pending_press = {
                "point": tuple(packet.screen_point or ()),
                "button": packet.button or "left",
                "monotonic_ms": packet.monotonic_ms,
                "observed": observed,
            }
            return None

        if packet.kind == "text_activity":
            focused = getattr(self._resolver, "focused", None)
            focused_target = focused() if callable(focused) else None
            control_type = str(
                (focused_target or {}).get("controlType") or ""
            ).lower().removeprefix("ax")
            if any(tag in control_type for tag in ("edit", "document", "textfield", "textarea")):
                self._active_edit = focused_target
                self._edit_dirty = True
            return None

        if packet.kind == "key_command":
            control_type = str(
                (target_data or {}).get("controlType") or ""
            ).lower().removeprefix("ax")
            command_modifiers = set(packet.modifiers).intersection({"CTRL", "ALT", "META"})
            is_edit = any(
                tag in control_type
                for tag in ("edit", "document", "textfield", "textarea")
            )
            if is_edit and (
                packet.key in {"SPACE", "BACKSPACE", "DELETE"}
                or (
                    packet.key in {"V", "X", "Y", "Z"}
                    and command_modifiers.intersection({"CTRL", "META"})
                )
            ):
                self._active_edit = target_data
                self._edit_dirty = True
                return None
            if (
                packet.key == "SPACE"
                and control_type in {"checkbox", "radiobutton"}
                and (target_data or {}).get("toggleState") is not None
            ):
                return RawCaptureEvent(
                    sequence=sequence,
                    wall_time=packet.wall_time,
                    monotonic_ms=packet.monotonic_ms,
                    type="toggle_change",
                    scope=scope,
                    input={"value": bool(target_data["toggleState"])},
                    observed_target=observed,
                    evidence={"foregroundPid": foreground.get("pid")},
                    causal_id="CAUSE-" + uuid.uuid4().hex,
                )
            if (
                packet.key in {"UP", "DOWN", "LEFT", "RIGHT", "HOME", "END"}
                and control_type in {"listitem", "menuitem", "treeitem", "tabitem"}
                and (target_data or {}).get("selected") is not None
            ):
                return RawCaptureEvent(
                    sequence=sequence,
                    wall_time=packet.wall_time,
                    monotonic_ms=packet.monotonic_ms,
                    type="selection_change",
                    scope=scope,
                    input={
                        "value": target_data.get("text"),
                        "selected": bool(target_data["selected"]),
                    },
                    observed_target=observed,
                    evidence={"foregroundPid": foreground.get("pid")},
                    causal_id="CAUSE-" + uuid.uuid4().hex,
                )

        pending_commit = None
        if self._active_edit is not None and (
            packet.kind == "pointer_up" or
            (packet.kind == "key_command" and packet.key in {"ENTER", "TAB"})
        ):
            pending_commit = self._text_commit(
                packet=packet,
                scope=scope,
                sequence=sequence,
                foreground=foreground,
            )
            if pending_commit is not None:
                sequence += 1
                if observed is None:
                    observed = pending_commit.observed_target

        if packet.kind == "pointer_up":
            press = self._pending_press
            self._pending_press = None
            drag = self._drag_input(press, packet, target_data)
            if drag is not None:
                event = RawCaptureEvent(
                    sequence=sequence,
                    wall_time=packet.wall_time,
                    monotonic_ms=packet.monotonic_ms,
                    type="drag_commit",
                    scope=scope,
                    input=drag,
                    observed_target=press["observed"],
                    evidence={"foregroundPid": foreground.get("pid")},
                    causal_id="CAUSE-" + uuid.uuid4().hex,
                )
                return (pending_commit, event) if pending_commit is not None else event
            control_type = str(
                (target_data or {}).get("controlType") or ""
            ).lower().removeprefix("ax")
            is_primary_click = (packet.button or "left") == "left"
            if is_primary_click and control_type in {"checkbox", "radiobutton"} and (target_data or {}).get("toggleState") is not None:
                event_type = "toggle_change"
                event_input = {"value": bool(target_data["toggleState"])}
            elif is_primary_click and control_type in {"listitem", "menuitem", "treeitem", "tabitem"} and (target_data or {}).get("selected") is not None:
                event_type = "selection_change"
                event_input = {"value": target_data.get("text"), "selected": bool(target_data["selected"])}
            else:
                event_type = "pointer_click"
                event_input = {
                    "button": packet.button or "left",
                    "clickCount": 1,
                    "screenPoint": list(packet.screen_point or ()),
                }
            if control_type in {"edit", "document"}:
                self._active_edit = target_data
                self._edit_dirty = False
        elif packet.kind == "scroll":
            event_type = "scroll_commit"
            event_input = {
                "delta": packet.native.get("delta", 0),
                "screenPoint": list(packet.screen_point or ()),
            }
        elif packet.kind == "key_command":
            event_type = "key_command"
            event_input = {"key": packet.key, "modifiers": list(packet.modifiers)}
        else:
            return None
        evidence = {"foregroundPid": foreground.get("pid")}
        if event_type == "pointer_click":
            evidence["doubleClickIntervalMs"] = self._double_click_ms
        event = RawCaptureEvent(
            sequence=sequence,
            wall_time=packet.wall_time,
            monotonic_ms=packet.monotonic_ms,
            type=event_type,
            scope=scope,
            input=event_input,
            observed_target=observed,
            evidence=evidence,
            causal_id=self._causal_id(event_type, packet, observed),
        )
        return (pending_commit, event) if pending_commit is not None else event


class WindowsWinEventDriver:
    """Subscribe to top-level window show/destroy events for one process.

    ``EVENT_OBJECT_DESTROY`` arrives when the window is already gone, so titles
    seen at show time are remembered and replayed into the close packet.  The
    callback stays inside the same bounded-emit rule as the input hooks.
    """

    EVENT_OBJECT_DESTROY = 0x8001
    EVENT_OBJECT_SHOW = 0x8002
    EVENT_OBJECT_HIDE = 0x8003
    OBJID_WINDOW = 0
    GA_ROOT = 2
    WINEVENT_OUTOFCONTEXT = 0x0000
    WINEVENT_SKIPOWNPROCESS = 0x0002
    WM_QUIT = 0x0012

    def __init__(self, process_name: str) -> None:
        self.process_name = process_name
        self._emit: Callable[[HookPacket], None] | None = None
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._hook = None
        self._callback = None
        self._titles: dict[int, str] = {}

    @staticmethod
    def _matches(actual: object, expected: str) -> bool:
        return str(actual or "").lower().removesuffix(".exe") == expected.lower().removesuffix(".exe")

    def _run(self) -> None:  # pragma: no cover - exercised on Windows acceptance target
        try:
            if sys.platform != "win32":
                raise RecordingModelError(
                    "recording_capture_unavailable",
                    "Windows window-event hooks require win32",
                    path="scope.backend",
                )
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)

            proc = ctypes.WINFUNCTYPE(
                None, ctypes.c_void_p, wintypes.DWORD, wintypes.HWND,
                wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
            )
            user32.SetWinEventHook.argtypes = [
                wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, proc,
                wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            ]
            user32.SetWinEventHook.restype = ctypes.c_void_p
            user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]

            def process_name_of(pid: int) -> str:
                handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
                if not handle:
                    return ""
                try:
                    buffer = ctypes.create_unicode_buffer(260)
                    if psapi.GetModuleBaseNameW(handle, None, buffer, 260):
                        return buffer.value
                    return ""
                finally:
                    kernel32.CloseHandle(handle)

            def window_title(hwnd) -> str:
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return ""
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                return buffer.value

            def callback(hook, event, hwnd, id_object, id_child, thread_id, timestamp):
                del hook, thread_id, timestamp
                if self._emit is None or not hwnd:
                    return
                if id_object != self.OBJID_WINDOW or id_child != 0:
                    return
                if user32.GetAncestor(hwnd, self.GA_ROOT) != hwnd:
                    return
                handle = int(hwnd)
                if event == self.EVENT_OBJECT_SHOW:
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    name = process_name_of(int(pid.value))
                    if not self._matches(name, self.process_name):
                        return
                    title = window_title(hwnd)
                    self._titles[handle] = title
                    kind, resolved_pid = "opened", int(pid.value)
                else:
                    title = self._titles.pop(handle, None)
                    if title is None:
                        return
                    name, kind, resolved_pid = self.process_name, "closed", None
                self._emit(HookPacket(
                    kind="window_transition",
                    monotonic_ms=int(time.monotonic() * 1000),
                    wall_time=_utc_now(),
                    native={
                        "kind": kind,
                        "processName": name,
                        "title": title,
                        "pid": resolved_pid,
                    },
                ))

            self._callback = proc(callback)
            self._hook = user32.SetWinEventHook(
                self.EVENT_OBJECT_DESTROY, self.EVENT_OBJECT_HIDE,
                None, self._callback, 0, 0,
                self.WINEVENT_OUTOFCONTEXT | self.WINEVENT_SKIPOWNPROCESS,
            )
            if not self._hook:
                raise RecordingModelError(
                    "recording_permission_missing", "SetWinEventHook failed"
                )
            self._thread_id = kernel32.GetCurrentThreadId()
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            if self._hook:
                try:
                    ctypes.WinDLL("user32", use_last_error=True).UnhookWinEvent(self._hook)
                except Exception:
                    pass
                self._hook = None

    def start(self, emit: Callable[[HookPacket], None]) -> None:
        self._emit = emit
        self._ready.clear()
        self._startup_error = None
        self._titles.clear()
        self._thread = threading.Thread(
            target=self._run, name="edr-wd-recording-window-events", daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RecordingModelError(
                "recording_permission_missing", "window-event hook startup timed out"
            )
        if self._startup_error is not None:
            raise self._startup_error

    def stop(self) -> None:
        if self._thread_id is not None and sys.platform == "win32":  # pragma: no cover
            ctypes.WinDLL("user32", use_last_error=True).PostThreadMessageW(
                self._thread_id, self.WM_QUIT, 0, 0
            )
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None
        self._thread_id = None


def windows_source_factory(scope: CaptureScope, sink: Callable[[RawCaptureEvent], bool]):
    if sys.platform != "win32":
        raise RecordingModelError(
            "recording_capture_unavailable",
            "windows_pywinauto recording requires a Windows target",
            path="scope.backend",
        )
    return QueuedCaptureSource(
        scope=scope,
        sink=sink,
        driver=CompositeHookDriver(
            WindowsLowLevelHookDriver(),
            WindowsWinEventDriver(scope.process_name),
        ),
        correlator=WindowsUIACorrelator(),
    )
