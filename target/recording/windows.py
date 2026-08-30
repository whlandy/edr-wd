"""Windows low-level input hooks and UI Automation event correlation.

The native hook callbacks only construct :class:`HookPacket` objects and call
the queue emitter.  Foreground ownership checks and UIA hit testing happen on
the queue worker through :class:`WindowsUIACorrelator`.
"""

from __future__ import annotations

import ctypes
import dataclasses
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
    window_root_identity,
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

    def __init__(self, backend: object | None = None) -> None:
        # The automation backend, when the server supplies it. Scope seeding
        # asks it for the window list rather than enumerating the desktop a
        # second time.
        self._backend = backend

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
            "handle": int(hwnd),
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

    #: Directories applications are installed into. An executable's first
    #: directory beneath one of these is the application, the way a `.app`
    #: bundle is on macOS.
    _PROGRAM_ROOT_VARS = (
        "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData",
    )

    @classmethod
    def _install_root(cls, executable: str) -> str | None:
        """The directory that holds one application, given one of its exes."""
        import os as _os
        from pathlib import PureWindowsPath

        path = PureWindowsPath(executable)
        roots = [
            _os.environ.get(name) for name in cls._PROGRAM_ROOT_VARS
        ]
        roots.append(
            str(PureWindowsPath(_os.environ.get("LOCALAPPDATA", ""), "Programs"))
            if _os.environ.get("LOCALAPPDATA") else None
        )
        lowered = str(path).lower()
        best = None
        for root in roots:
            if not root:
                continue
            prefix = root.rstrip("\\/").lower() + "\\"
            if not lowered.startswith(prefix):
                continue
            remainder = str(path)[len(prefix):].split("\\")
            if not remainder or not remainder[0]:
                continue
            candidate = str(PureWindowsPath(root, remainder[0]))
            # Prefer the deepest matching root: Program Files (x86) is not a
            # subdirectory of Program Files, but LOCALAPPDATA\Programs is a
            # subdirectory of LOCALAPPDATA-derived roots on some layouts.
            if best is None or len(candidate) > len(best):
                best = candidate
        if best is not None:
            return best
        parent = path.parent
        return str(parent) if str(parent) not in ("", ".") else None

    def application_process_names(self, process_name: str) -> list[str]:
        """Process names belonging to the same application as `process_name`.

        A Windows application is not one process. HiSec installs its agent and
        its client as separate executables under one installation directory,
        and its primary flow crosses from one to the other: the agent's window
        opens the security centre, which is a different process. Scoping a
        recording to a single process name drops everything the user does
        after that crossing, while the installation directory keeps unrelated
        applications out — the same boundary the macOS side draws with a
        bundle.

        Returns an empty list when the application cannot be resolved, leaving
        the caller with single-process behaviour rather than a wider scope
        chosen by accident.
        """
        try:
            import psutil
        except Exception:
            return []
        expected = str(process_name or "").lower().removesuffix(".exe")
        if not expected:
            return []
        root = None
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = str(proc.info.get("name") or "").lower().removesuffix(".exe")
                if name != expected:
                    continue
                executable = str(proc.info.get("exe") or "")
                if executable:
                    root = self._install_root(executable)
                    if root:
                        break
            except Exception:
                continue
        if not root:
            return []
        prefix = root.rstrip("\\/").lower() + "\\"
        names: set[str] = set()
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                executable = str(proc.info.get("exe") or "")
                if executable and executable.lower().startswith(prefix):
                    names.add(str(proc.info.get("name") or "").lower().removesuffix(".exe"))
            except Exception:
                continue
        names.discard("")
        return sorted(names)

    def windows(self) -> list[Mapping[str, object]]:  # pragma: no cover - Windows only
        """Top-level windows with their owning process, for scope seeding.

        This reuses the backend's own window enumeration rather than walking
        the desktop again. A second implementation of the same query is how the
        seed silently came back empty while `list_windows` was returning eight
        windows on the same machine.
        """
        listing = self._backend.list_windows() if self._backend is not None else None
        if not isinstance(listing, Mapping) or not listing.get("ok"):
            raise RecordingModelError(
                "recording_scope_seed_failed",
                f"window enumeration failed: {listing}",
                path="scope.seed",
            )
        items = listing.get("windows")
        if not isinstance(items, list):
            raise RecordingModelError(
                "recording_scope_seed_failed",
                "window enumeration returned no windows array",
                path="scope.seed",
            )
        import psutil

        names: dict[int, str] = {}
        found: list[Mapping[str, object]] = []
        for window in items:
            if not isinstance(window, Mapping):
                continue
            pid = window.get("process_id") or window.get("pid")
            if pid is None:
                continue
            pid = int(pid)
            if pid not in names:
                try:
                    names[pid] = psutil.Process(pid).name()
                except Exception:
                    names[pid] = ""
            found.append({
                "title": str(window.get("title") or ""),
                "pid": pid,
                "processName": names[pid],
            })
        return found

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
        transition_attribution_ms: int = 120_000,
        transition_lookback_ms: int = 3000,
        scroll_gesture_ms: int = 500,
    ) -> None:
        self._resolver = resolver or WindowsUIAResolver()
        self._active_edit: Mapping[str, object] | None = None
        self._edit_dirty = False
        self._pending_click: tuple[int, str, str, str] | None = None
        self._pending_press: dict[str, object] | None = None
        self._last_action_causal_id: str | None = None
        self._last_action_ms: int | None = None
        self._pending_scroll: dict[str, object] | None = None
        if scroll_gesture_ms < 0:
            raise ValueError("scroll_gesture_ms must be non-negative")
        self._scroll_gesture_ms = scroll_gesture_ms
        # Windows this recording caused to open.  Input inside them belongs to
        # the flow being recorded even though their titles cannot match the
        # scope regex the user supplied for the entry window.
        #
        # Title is the fallback identity, kept for windows a source cannot
        # report a native handle for.  It is unreliable on its own: a title
        # can change after admission (an edited document gains "*", a tab
        # switches) and drop the window right back out of scope, and two
        # windows can legitimately share a title and get confused for each
        # other.  A native window handle (HWND / CGWindowNumber) does not
        # have either problem, so it is checked first wherever a source can
        # supply one.
        self._derived_scope_titles: set[str] = set()
        self._derived_scope_handles: set[int] = set()
        self.out_of_scope_events = 0
        # Screen rect of the recorder's own always-on-top UI, when it has
        # one.  Input that lands here never reached the target application,
        # so it must not be recorded as though it had.
        self._recorder_ui_rect: tuple[int, int, int, int] | None = None
        self.recorder_ui_events = 0
        # Clicks that landed on nothing the application can name.
        self.unidentified_target_events = 0
        # Sibling process names admitted alongside `scope.process_name`,
        # resolved at seed time from the application the scope names. An
        # application is not one process — HiSec's own flow crosses from its
        # agent to its client — so scoping to a single process name drops
        # everything after that crossing.
        self._scope_process_names: set[str] = set()
        self.scope_process_names: tuple[str, ...] = ()
        # Every window of the application this capture saw, in the order it
        # first appeared, with the action that opened it. Control identity is
        # not always available — an application that does not expose its
        # accessibility tree yields only anonymous groups — but which pages
        # existed and how each was reached still describes the flow.
        self.window_registry: list[dict[str, object]] = []
        self._window_index: dict[object, dict[str, object]] = {}
        # Why recent input was not recorded, with the window it resolved to.
        # A bare count says a capture was short; it never says whether the
        # recorder misjudged which window the user was in.
        self.rejections: list[dict[str, object]] = []
        # Windows seen opening before the action that caused them existed.
        self._windows_awaiting_cause: list[tuple[int, dict[str, object]]] = []
        # Input that belonged to some other application entirely.
        self.foreign_app_events = 0
        if transition_attribution_ms < 0:
            raise ValueError("transition_attribution_ms must be non-negative")
        if transition_lookback_ms < 0:
            raise ValueError("transition_lookback_ms must be non-negative")
        # Forward: the action already happened and we are waiting for its
        # effect, so only another action ends its claim. Backward: the window
        # already existed, and blaming it on whatever the user did next is a
        # guess that has to stay tightly bounded.
        self._transition_attribution_ms = transition_attribution_ms
        self._transition_lookback_ms = transition_lookback_ms
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

    def _process_in_scope(self, actual: object, scope: CaptureScope) -> bool:
        """Does this process belong to the application the scope names?

        The scope's own process always qualifies. Sibling processes of the
        same application qualify once seeding has resolved them; until then
        (or when the bundle cannot be resolved) this is exactly the old
        single-process behaviour.
        """
        if self._process_matches(actual, scope.process_name):
            return True
        normalized = str(actual or "").lower().removesuffix(".exe")
        return bool(normalized) and normalized in self._scope_process_names

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
        if not self._process_in_scope(foreground.get("processName"), scope):
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
    ) -> RawCaptureEvent | tuple[RawCaptureEvent, ...] | None:
        """Commit the final focused value when recording stops."""
        foreground = self._resolver.foreground()
        if not self._process_in_scope(foreground.get("processName"), scope):
            return None
        try:
            if not re.search(scope.window_title, str(foreground.get("windowTitle") or "")):
                return None
        except re.error:
            return None
        # A recording can stop with the wheel still spinning; that gesture is
        # real input and must not be dropped just because nothing followed it.
        scrolled = self._flush_scroll(scope, sequence)
        if scrolled is not None:
            sequence += 1
        committed = self._text_commit(
            packet=HookPacket(
                kind="flush",
                monotonic_ms=int(time.monotonic() * 1000),
                wall_time=_utc_now(),
            ),
            scope=scope,
            sequence=sequence,
            foreground=foreground,
        )
        produced = tuple(x for x in (scrolled, committed) if x is not None)
        if not produced:
            return None
        return produced[0] if len(produced) == 1 else produced

    def seed_scope(self, scope: CaptureScope) -> tuple[str, ...]:
        """Admit the application's already-open windows before capture starts.

        Growth by causal transition only covers windows this recording opened.
        A window that was already on screen when recording began never emits an
        open event, so without seeding every click inside it is dropped — a
        live capture lost 102 of 103 events exactly that way.
        """
        self.scope_seed_error: str | None = None
        # Resolve the application's other processes first, so the window
        # filtering below admits every window of the application rather than
        # only those of the single process the scope happens to name.
        self._seed_application_processes(scope)
        enumerate_windows = getattr(self._resolver, "windows", None)
        if not callable(enumerate_windows):
            self.scope_seed_error = "resolver cannot enumerate windows"
            return ()
        try:
            windows = enumerate_windows()
        except Exception as exc:
            # Returning an empty seed silently is what made a live recording
            # drop 41 of 42 events with no indication why.
            self.scope_seed_error = f"{type(exc).__name__}: {exc}"
            return ()
        seeded = []
        for window in windows or ():
            if not isinstance(window, Mapping):
                continue
            if not self._process_in_scope(window.get("processName"), scope):
                continue
            title = window.get("title")
            if isinstance(title, str) and title:
                seeded.append(title)
            handle = window.get("handle")
            if isinstance(handle, int):
                # A handle is exact; tracking the title too would let a later
                # unrelated window that happens to share it ride in on the
                # title-derived fallback, defeating the point of the handle.
                self._derived_scope_handles.add(handle)
            elif isinstance(title, str) and title:
                self._derived_scope_titles.add(title)
            self._register_window(
                title=title if isinstance(title, str) else "",
                process_name=str(window.get("processName") or ""),
                handle=handle if isinstance(handle, int) else None,
                pid=window.get("pid"),
                origin="already_open",
            )
        return tuple(seeded)

    def _register_window(
        self,
        *,
        title: str,
        process_name: str,
        handle: int | None,
        pid: object,
        origin: str,
        opened_by: str | None = None,
        appeared_ms: int | None = None,
    ) -> None:
        """Record a window of the application, keyed by handle when there is one."""
        key = handle if handle is not None else ("title", process_name, title)
        existing = self._window_index.get(key)
        if existing is not None:
            if existing.get("closed") and origin == "opened":
                # Re-opened: the flow reached this page again.
                existing["closed"] = False
                existing["reopenedBy"] = opened_by
            if opened_by is not None and existing.get("openedBy") is None:
                # The poller and the input tap feed the same queue from
                # different threads, so a window can be reported open before
                # the action that opened it has been correlated. Learning the
                # cause later is what keeps the entry from staying "opened by
                # nothing" for the rest of the recording.
                existing["openedBy"] = opened_by
            return
        entry: dict[str, object] = {
            "title": title,
            "processName": process_name,
            "origin": origin,
            "closed": False,
        }
        if handle is not None:
            entry["handle"] = handle
        if pid is not None:
            try:
                entry["pid"] = int(pid)  # type: ignore[arg-type]
            except Exception:
                pass
        # The action that caused this window to appear — the edge that makes
        # the registry a tree rather than a flat list. Always present, so a
        # cause learned later has somewhere to go.
        entry["openedBy"] = opened_by
        self._window_index[key] = entry
        self.window_registry.append(entry)
        if opened_by is None and appeared_ms is not None:
            # A window opens on mouse-down while the click that caused it is
            # only produced on mouse-up, so looking backwards for a cause
            # finds nothing. Hold the entry open for the action that is about
            # to arrive.
            self._windows_awaiting_cause.append((appeared_ms, entry))
            del self._windows_awaiting_cause[:-16]

    def _claim_windows_awaiting_cause(self, event: RawCaptureEvent) -> None:
        """Attribute windows that appeared just before this action to it.

        This direction stays tightly bounded: the window already existed when
        the action happened, so attributing it to that action is a guess, and
        an old window must stay unattributed rather than be blamed on whatever
        the user did next. The forward direction is the opposite case and is
        bounded by the next action instead.
        """
        if not self._windows_awaiting_cause:
            return
        remaining = []
        for appeared_ms, entry in self._windows_awaiting_cause:
            gap = event.monotonic_ms - appeared_ms
            if 0 <= gap <= self._transition_lookback_ms and entry.get("openedBy") is None:
                entry["openedBy"] = event.causal_id
            elif gap < 0 or gap <= self._transition_lookback_ms:
                remaining.append((appeared_ms, entry))
        self._windows_awaiting_cause = remaining

    def _close_window(self, *, title: str, process_name: str, handle: int | None) -> None:
        key = handle if handle is not None else ("title", process_name, title)
        entry = self._window_index.get(key)
        if entry is not None:
            entry["closed"] = True

    def set_recorder_ui_rect(self, rect: tuple[int, int, int, int] | None) -> None:
        """Tell the correlator where its own always-on-top UI sits on screen."""
        self._recorder_ui_rect = tuple(int(v) for v in rect) if rect else None

    def _lands_on_recorder_ui(self, packet: HookPacket) -> bool:
        rect = self._recorder_ui_rect
        point = packet.screen_point
        if rect is None or point is None:
            return False
        x, y, width, height = rect
        px, py = point
        return x <= px < x + width and y <= py < y + height

    def _seed_application_processes(self, scope: CaptureScope) -> None:
        """Admit the sibling processes of the scope's application, if known."""
        resolve = getattr(self._resolver, "application_process_names", None)
        if not callable(resolve):
            return
        try:
            names = resolve(scope.process_name) or ()
        except Exception as exc:
            # Never widen the scope on a failed lookup; single-process
            # behaviour is the safe fallback, but it must be visible.
            self.scope_seed_error = f"{type(exc).__name__}: {exc}"
            return
        admitted = {
            str(name).lower().removesuffix(".exe")
            for name in names if str(name or "")
        }
        self._scope_process_names = admitted
        self.scope_process_names = tuple(sorted(admitted))

    def _in_scope(self, foreground: Mapping[str, object], scope: CaptureScope) -> bool:
        """Is this foreground window part of the recording's scope?

        The scope starts as the user's process + window-title regex and grows
        to include every window the recording causally opened.  Without that,
        a click that opens a dialog is recorded but everything the user then
        does inside the dialog is silently dropped.
        """
        if not self._process_in_scope(foreground.get("processName"), scope):
            return False
        # The entry window is always checked against the user's own regex
        # first. That match must not depend on seeding having succeeded, so
        # it runs even when a handle is present.
        title = str(foreground.get("windowTitle") or "")
        try:
            if re.search(scope.window_title, title):
                return True
        except re.error as exc:
            raise RecordingModelError(
                "recording_scope_not_unique", str(exc), path="scope.windowTitle"
            )
        # A native handle is exact and immune to a title changing after the
        # window was admitted — a document gaining "*", a tab switching — so
        # it decides scope for every window beyond the entry one wherever the
        # source supplied one, ahead of the title fallback.
        handle = foreground.get("handle")
        if isinstance(handle, int) and handle in self._derived_scope_handles:
            return True
        return title in self._derived_scope_titles

    def _scroll_continues(self, packet: HookPacket) -> bool:
        """Does this notch belong to the gesture already in progress?

        Scrolling moves content under a stationary pointer, so the control the
        pointer is over changes constantly and cannot decide this. The pointer
        position, direction and cadence can.
        """
        pending = self._pending_scroll
        if pending is None or packet.kind != "scroll":
            return False
        delta = packet.native.get("delta", 0)
        if not isinstance(delta, (int, float)) or isinstance(delta, bool) or delta == 0:
            return False
        if (1 if delta > 0 else -1) != pending["sign"]:
            return False
        if packet.monotonic_ms - int(pending["last_ms"]) > self._scroll_gesture_ms:
            return False
        point, origin = packet.screen_point, pending["point"]
        if point is None or origin is None:
            return False
        return (
            abs(point[0] - origin[0]) <= self._drag_threshold[0]
            and abs(point[1] - origin[1]) <= self._drag_threshold[1]
        )

    def _flush_scroll(self, scope: CaptureScope, sequence: int) -> RawCaptureEvent | None:
        """Emit the accumulated gesture as the single event it always was."""
        pending = self._pending_scroll
        self._pending_scroll = None
        if pending is None:
            return None
        return RawCaptureEvent(
            sequence=sequence,
            wall_time=str(pending["wall_time"]),
            monotonic_ms=int(pending["last_ms"]),
            type="scroll_commit",
            scope=scope,
            input={
                "delta": pending["delta"],
                "screenPoint": list(pending["point"]),
                "notches": pending["notches"],
            },
            observed_target=pending["observed"],
            evidence={"foregroundPid": pending["pid"]},
            causal_id=str(pending["causal_id"]),
        )

    def _unbound_transition(
        self,
        packet: HookPacket,
        scope: CaptureScope,
        sequence: int,
        kind: str,
        process_name: object,
        title: object,
    ) -> RawCaptureEvent:
        """Record a transition no action plausibly explains, without a cause.

        Dropping it would repeat the mistake this whole area keeps making:
        something the recorder saw disappears with no trace. The compiler turns
        an unbound transition into a visible incomplete step instead.
        """
        event_input: dict[str, object] = {
            "kind": kind,
            "processName": str(process_name or scope.process_name),
        }
        if isinstance(title, str) and title:
            event_input["title"] = title
        return RawCaptureEvent(
            sequence=sequence,
            wall_time=packet.wall_time,
            monotonic_ms=packet.monotonic_ms,
            type="window_transition",
            scope=scope,
            input=event_input,
            evidence={"foregroundPid": packet.native.get("pid")},
            causal_id=None,
        )

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
        if not self._process_in_scope(process_name, scope):
            return None
        # The scope follows the window either way. A window that opened on its
        # own is not a recorded step — nothing the user did explains it — but
        # they may well click inside it next, and dropping that input silently
        # is how a recording ends up empty.
        title = packet.native.get("title")
        handle = packet.native.get("handle")
        if kind == "opened":
            self._register_window(
                title=title if isinstance(title, str) else "",
                process_name=str(process_name or scope.process_name),
                handle=handle if isinstance(handle, int) else None,
                pid=packet.native.get("pid"),
                origin="opened",
                opened_by=self._last_action_causal_id,
                appeared_ms=packet.monotonic_ms,
            )
        else:
            self._close_window(
                title=title if isinstance(title, str) else "",
                process_name=str(process_name or scope.process_name),
                handle=handle if isinstance(handle, int) else None,
            )
        if isinstance(handle, int):
            # A handle is exact; also title-tracking this window would let a
            # later unrelated window that happens to share the title ride in
            # on the title-derived fallback, defeating the point of the
            # handle.
            if kind == "opened":
                self._derived_scope_handles.add(handle)
            else:
                self._derived_scope_handles.discard(handle)
        elif isinstance(title, str) and title:
            if kind == "opened":
                self._derived_scope_titles.add(title)
            else:
                self._derived_scope_titles.discard(title)
        if self._last_action_causal_id is None or self._last_action_ms is None:
            return None
        latency_ms = packet.monotonic_ms - self._last_action_ms
        # What breaks causality is the user doing something else, not a
        # stopwatch. A fixed window silently discarded any window slower than
        # it — and an application's main UI can take many seconds to appear,
        # so the recording lost exactly the transitions worth asserting. The
        # last action stands as the cause until another action replaces it;
        # the elapsed time becomes evidence that sizes the replay wait rather
        # than a gate that drops the event.
        if latency_ms < 0:
            return None
        if latency_ms > self._transition_attribution_ms:
            # Far enough out that attributing it to that action would be a
            # guess. Still recorded, without a cause, so the compiler surfaces
            # it as an unbound transition instead of it vanishing.
            return self._unbound_transition(packet, scope, sequence, kind, process_name, title)
        event_input: dict[str, object] = {
            "kind": kind,
            "processName": str(process_name or scope.process_name),
            # Replay waits proportionally to what the recording actually
            # observed, never on a fixed sleep.
            "timeoutSeconds": min(60.0, max(5.0, round(latency_ms * 3 / 1000, 1))),
        }
        if isinstance(title, str) and title:
            event_input["title"] = title
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

    @staticmethod
    def _window_root(event: RawCaptureEvent) -> str | None:
        return window_root_identity(event.observed_target)

    @classmethod
    def _with_window(cls, event: RawCaptureEvent, window: Mapping[str, object]) -> RawCaptureEvent:
        """Stamp the event with the window it happened in."""
        title = window.get("windowTitle")
        if not isinstance(title, str) or not title:
            return event
        evidence = dict(event.evidence)
        stamp: dict[str, object] = {
            "title": title,
            "processName": str(window.get("processName") or ""),
        }
        # The handle only ever identifies a window within this one live
        # session — the same title/process is what replay must re-resolve
        # against a freshly launched window later, so this is diagnostic
        # evidence, not a replay selector field.
        handle = window.get("handle")
        if isinstance(handle, int):
            stamp["handle"] = handle
        root = cls._window_root(event)
        if root:
            stamp["rootAutomationId"] = root
        evidence["window"] = stamp
        return dataclasses.replace(event, evidence=evidence)

    def correlate(
        self,
        packet: HookPacket,
        scope: CaptureScope,
        sequence: int,
    ) -> RawCaptureEvent | tuple[RawCaptureEvent, ...] | None:
        if packet.kind == "window_transition":
            return self._window_transition(packet, scope, sequence)
        if self._lands_on_recorder_ui(packet):
            # The recorder UI is topmost, so this click was consumed by it and
            # the target application never saw it.  Recording it against the
            # target would put a step in the trace that never happened there —
            # a live HiSec capture did exactly that before this check existed.
            self.recorder_ui_events += 1
            self._note_rejection("recorder_ui", packet, None)
            return None
        # Resolve the owning window once: this is a live window-server query
        # and doing it twice per packet doubled the correlation cost that the
        # worker pays for every keystroke.
        foreground = self._window_for(packet)
        result = self._correlate(packet, scope, sequence, foreground)
        produced = (
            result if isinstance(result, tuple)
            else () if result is None else (result,)
        )
        if not produced and packet.kind in {"pointer_up", "scroll"}:
            # In scope, not on the recorder, yet nothing came out. Without
            # this the step simply never appears and there is nothing to
            # explain why.
            self._note_rejection("no_event_produced", packet, foreground, scope=scope)
        stamped = tuple(self._with_window(event, foreground) for event in produced)
        for event in stamped:
            if event.type in ACTION_EVENT_TYPES:
                self._last_action_causal_id = event.causal_id
                self._last_action_ms = event.monotonic_ms
                self._claim_windows_awaiting_cause(event)
        if not stamped:
            return None
        return stamped[0] if len(stamped) == 1 else stamped

    def _element_at(
        self,
        point: tuple[int, int],
        foreground: Mapping[str, object] | None,
    ) -> Mapping[str, object] | None:
        pid = (foreground or {}).get("pid")
        if pid is not None:
            try:
                return self._resolver.element_at(*point, int(pid))
            except TypeError:
                pass  # resolver predates the pid argument
            except Exception:
                return None
        return self._resolver.element_at(*point)

    @staticmethod
    def _identifiable(target_data: Mapping[str, object] | None) -> bool:
        """Can a replay selector name this control at all?

        `controlType` alone cannot: a bare container type matches any number
        of elements. Only an automation id, an identifier or visible text
        distinguishes one control from its siblings.
        """
        if not target_data:
            return False
        return any(
            str(target_data.get(key) or "").strip()
            for key in ("automationId", "identifier", "text")
        )

    def _target_owned_by_scope(
        self,
        target_data: Mapping[str, object],
        foreground: Mapping[str, object] | None,
        scope: CaptureScope,
    ) -> bool:
        """Does the resolved element belong to the application being recorded?

        Only enforced when the resolver can say which process owns the
        element; a resolver that cannot is left exactly as permissive as
        before rather than silently dropping every step.
        """
        owner = target_data.get("ownerPid")
        if owner is None:
            return True
        try:
            owner = int(owner)
        except (TypeError, ValueError):
            return True
        expected = (foreground or {}).get("pid")
        if expected is not None:
            try:
                if int(expected) == owner:
                    return True
            except (TypeError, ValueError):
                pass
        # The admitted window's pid is the strongest signal, but a flow that
        # legitimately crosses the application's processes must still pass.
        for window in self.window_registry:
            if window.get("pid") == owner:
                return True
        return False

    def _note_rejection(
        self,
        reason: str,
        packet: HookPacket,
        window: Mapping[str, object] | None,
        *,
        scope: CaptureScope | None = None,
    ) -> None:
        # Input aimed at a *different* application is expected and enormous in
        # volume — ordinary typing elsewhere fills any buffer instantly and
        # buries the one rejection that explains a lost click. Only input that
        # resolved to this application's own windows is diagnostic.
        if window is not None and scope is not None:
            if not self._process_in_scope(window.get("processName"), scope):
                self.foreign_app_events += 1
                return
        if len(self.rejections) >= 60:
            self.rejections.pop(0)  # keep the most recent, not the first
        entry: dict[str, object] = {"reason": reason, "kind": packet.kind}
        if packet.screen_point is not None:
            entry["point"] = list(packet.screen_point)
        if window:
            entry["resolvedProcess"] = window.get("processName")
            entry["resolvedTitle"] = window.get("windowTitle")
            entry["resolvedHandle"] = window.get("handle")
        self.rejections.append(entry)

    def _window_for(self, packet: HookPacket) -> Mapping[str, object]:
        """Which window this input belongs to.

        Pointer input carries its own answer: the window under the pointer.
        Using the *frontmost* window instead attributes a click to whatever
        happened to be in front, which in a live capture meant clicks inside
        the target were judged out of scope and dropped whenever another
        application was stacked above it.

        Keyboard input has no coordinate, so it genuinely belongs to the
        focused window and still resolves through `foreground()`.
        """
        # Prefer the window resolved while the input was happening. Deciding
        # it here instead re-reads a stacking order that has moved on, which
        # is how clicks admitted at the tap were judged to belong to another
        # application milliseconds later and lost.
        stamped = (packet.native or {}).get("window")
        if isinstance(stamped, Mapping) and stamped.get("processName"):
            return stamped
        point = packet.screen_point
        locate = getattr(self._resolver, "window_at", None)
        if point is not None and callable(locate):
            try:
                located = locate(*point)
            except Exception:
                located = None
            if located:
                return located
        return self._resolver.foreground()

    def _correlate(
        self,
        packet: HookPacket,
        scope: CaptureScope,
        sequence: int,
        foreground: Mapping[str, object] | None = None,
    ) -> RawCaptureEvent | tuple[RawCaptureEvent, ...] | None:
        if foreground is None:
            foreground = self._window_for(packet)
        if not self._in_scope(foreground, scope):
            # Out-of-scope input is expected (the user may alt-tab), but it
            # must be countable so a short recording is never mistaken for a
            # complete one.
            self.out_of_scope_events += 1
            self._note_rejection("out_of_scope", packet, foreground, scope=scope)
            return None

        target_data = None
        if packet.screen_point is not None:
            # Hit test inside the admitted window's application, not against
            # whatever is on top of the screen.
            target_data = self._element_at(packet.screen_point, foreground)
        elif packet.kind == "key_command":
            focused = getattr(self._resolver, "focused", None)
            target_data = focused() if callable(focused) else None
        if target_data is not None and not self._target_owned_by_scope(
            target_data, foreground, scope,
        ):
            # Scope admission judges the window; the hit test judges raw
            # coordinates. When those disagree the recorder reads another
            # application's content — a live capture pulled a browser's web
            # area in this way. The recording must never carry it.
            self.out_of_scope_events += 1
            self._note_rejection("target_outside_scope", packet, foreground, scope=scope)
            return None
        observed = self._observed(target_data)

        if self._scroll_continues(packet):
            pending = self._pending_scroll
            pending["delta"] += packet.native.get("delta", 0)
            pending["notches"] = int(pending["notches"]) + 1
            pending["last_ms"] = packet.monotonic_ms
            pending["wall_time"] = packet.wall_time
            pending["pid"] = foreground.get("pid")
            return None

        # Any other input ends the gesture, and the gesture is recorded before
        # whatever ended it.  Nothing is persisted while the wheel is still
        # turning, so one flick is one step rather than one step per notch.
        pending_scroll = self._flush_scroll(scope, sequence)
        if pending_scroll is not None:
            sequence += 1

        if packet.kind == "pointer_down":
            self._pending_press = {
                "point": tuple(packet.screen_point or ()),
                "button": packet.button or "left",
                "monotonic_ms": packet.monotonic_ms,
                "observed": observed,
            }
            return pending_scroll

        if packet.kind == "text_activity":
            focused = getattr(self._resolver, "focused", None)
            focused_target = focused() if callable(focused) else None
            control_type = str(
                (focused_target or {}).get("controlType") or ""
            ).lower().removeprefix("ax")
            if any(tag in control_type for tag in ("edit", "document", "textfield", "textarea")):
                self._active_edit = focused_target
                self._edit_dirty = True
            return pending_scroll

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
                return pending_scroll
            if (
                packet.key == "SPACE"
                and control_type in {"checkbox", "radiobutton"}
                and (target_data or {}).get("toggleState") is not None
            ):
                toggled = RawCaptureEvent(
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
                return toggled if pending_scroll is None else (pending_scroll, toggled)
            if (
                packet.key in {"UP", "DOWN", "LEFT", "RIGHT", "HOME", "END"}
                and control_type in {"listitem", "menuitem", "treeitem", "tabitem"}
                and (target_data or {}).get("selected") is not None
            ):
                selected = RawCaptureEvent(
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
                return selected if pending_scroll is None else (pending_scroll, selected)

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
                ordered = tuple(
                    x for x in (pending_scroll, pending_commit, event) if x is not None
                )
                return ordered[0] if len(ordered) == 1 else ordered
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
                if not self._identifiable(target_data):
                    # The pointer landed on nothing the target application can
                    # name — no automation id, no identifier, no text. Such a
                    # step cannot be replayed as a click on anything: replay
                    # would click a coordinate and hit whatever happens to be
                    # there. It also makes selector synthesis ambiguous, which
                    # marks the whole recording incomplete. The user did not
                    # press a control, so this is not a step.
                    self.unidentified_target_events += 1
                    self._note_rejection(
                        "target_not_identifiable", packet, foreground, scope=scope,
                    )
                    return pending_scroll
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
            delta = packet.native.get("delta", 0)
            if not isinstance(delta, (int, float)) or isinstance(delta, bool) or delta == 0:
                return pending_scroll
            self._pending_scroll = {
                "delta": delta,
                "notches": 1,
                "sign": 1 if delta > 0 else -1,
                "point": tuple(packet.screen_point or ()),
                "last_ms": packet.monotonic_ms,
                "wall_time": packet.wall_time,
                "observed": observed,
                "pid": foreground.get("pid"),
                "causal_id": "CAUSE-" + uuid.uuid4().hex,
            }
            return pending_scroll
        elif packet.kind == "key_command":
            event_type = "key_command"
            event_input = {"key": packet.key, "modifiers": list(packet.modifiers)}
        else:
            return pending_scroll
        def _emit(*produced):
            ordered = tuple(x for x in produced if x is not None)
            return ordered[0] if len(ordered) == 1 else (ordered or None)

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
        return _emit(pending_scroll, pending_commit, event)


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
                        "handle": handle,
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


def windows_source_factory(backend: object | None = None):
    """Build the Windows capture source, optionally bound to the backend."""

    def create(scope: CaptureScope, sink: Callable[[RawCaptureEvent], bool]):
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
            correlator=WindowsUIACorrelator(WindowsUIAResolver(backend)),
        )

    return create
