"""macOS CGEventTap capture and Accessibility-backed correlation."""

from __future__ import annotations

import dataclasses
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .models import CaptureScope, RawCaptureEvent, RecordingModelError
from .source import CompositeHookDriver, HookPacket, QueuedCaptureSource
from .indicator import RECORDER_UI_MARKER
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
        # AXIsProcessTrusted lives in HIServices, re-exported by
        # ApplicationServices. Older pyobjc-framework-Quartz re-exported it
        # too, but 12.x does not, so resolve it from either module and turn a
        # genuinely missing symbol into a structured error rather than letting
        # an AttributeError escape this preflight.
        ax_is_trusted = getattr(Quartz, "AXIsProcessTrusted", None)
        if not callable(ax_is_trusted):
            try:
                from ApplicationServices import (  # type: ignore[import-not-found]
                    AXIsProcessTrusted as ax_is_trusted,
                )
            except Exception as exc:
                raise RecordingModelError(
                    "recording_capture_unavailable",
                    "AXIsProcessTrusted unavailable: install "
                    f"pyobjc-framework-ApplicationServices ({exc})",
                    path="recording.permissions",
                ) from exc
        accessibility = bool(ax_is_trusted())
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


class CaptureScopeGate:
    """Decides at capture time whether input can possibly belong to the target.

    Without this, every global key press and click is queued and correlated
    before being thrown away. Ordinary typing in another application produced
    301 such packets against 2 real ones in one live session, and the
    resulting backlog delayed a real click reaching the recording by about
    ten seconds.

    The check is a rect test against a snapshot the window poller already
    maintains, so it costs no system call on the event-tap thread — where any
    latency is paid by the whole desktop, not just this recorder.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: tuple[Mapping[str, Any], ...] = ()
        self._front: Mapping[str, Any] | None = None
        self._app_in_front = True
        self._configured = False
        self.refused = 0

    def publish(
        self,
        windows: tuple[Mapping[str, Any], ...],
        app_in_front: bool,
        front: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._windows = windows
            self._app_in_front = app_in_front
            self._front = front
            self._configured = True

    def resolve(self, packet: HookPacket) -> tuple[bool, Mapping[str, Any] | None]:
        """Admit or refuse this input, and say which window it belongs to.

        The window is decided here, while the input is happening. Deciding it
        later — when correlation runs — re-reads a stacking order that has
        moved on: a live capture admitted 109 packets at the tap and then
        judged them to belong to another application milliseconds later,
        losing every one of them.
        """
        with self._lock:
            if not self._configured:
                # Never suppress input before the first snapshot exists;
                # failing open keeps a slow start from losing real steps.
                return True, None
            windows = self._windows
            app_in_front = self._app_in_front
            front = self._front
        point = packet.screen_point
        if point is None:
            # Keyboard input carries no coordinate and belongs to whatever is
            # focused, so it can only be judged by whether the application
            # holds the front window.
            if app_in_front:
                return True, front
            self.refused += 1
            return False, None
        x, y = point
        for window in windows:
            rect = window.get("rect")
            if not rect:
                continue
            left, top, width, height = rect
            if left <= x < left + width and top <= y < top + height:
                return True, window
        self.refused += 1
        return False, None

    def admits(self, packet: HookPacket) -> bool:
        return self.resolve(packet)[0]


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

    def __init__(
        self,
        preflight: MacOSPermissionPreflight | None = None,
        *,
        gate: "CaptureScopeGate | None" = None,
    ) -> None:
        self._preflight = preflight or MacOSPermissionPreflight()
        self._gate = gate
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
        gate = self._gate
        if gate is None:
            self._emit = emit
        else:
            def gated(packet: HookPacket) -> None:
                admitted, window = gate.resolve(packet)
                if not admitted:
                    return
                if window is not None:
                    # Carry the answer forward; correlation must not have to
                    # re-derive it from a stacking order that has since moved.
                    native = dict(packet.native or {})
                    native["window"] = dict(window)
                    packet = dataclasses.replace(packet, native=native)
                emit(packet)

            self._emit = gated
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
    """Resolve correlation targets, preferring single-point AX over a tree walk."""

    _AX_UNAVAILABLE = object()

    _AUTO_AX = object()
    _AUTO_WINDOWS = object()

    def __init__(
        self, backend: Any, *,
        native_ax: Any = _AUTO_AX,
        native_windows: Any = _AUTO_WINDOWS,
    ) -> None:
        self._backend = backend
        # `native_ax` is the AX seam.  Left at the default it is auto-detected
        # from ApplicationServices; pass None to force the tree-walk fallback
        # (which keeps `backend` authoritative, as the offline tests require),
        # or a stand-in module to exercise the native path deterministically.
        if native_ax is not self._AUTO_AX:
            self._ax_module = native_ax if native_ax is not None else self._AX_UNAVAILABLE
        # `native_windows` is the CGWindowList seam used by the foreground
        # handle lookup.  Left at the default it queries the real desktop;
        # pass None to force "no handle available" (matching a host without
        # Quartz), or an iterable/callable of CGWindowList-shaped dicts to
        # exercise the lookup deterministically without touching whatever
        # windows happen to be open on the machine running the tests.
        if native_windows is not self._AUTO_WINDOWS:
            self._cg_window_source = native_windows

    def foreground(self) -> Mapping[str, object]:
        """The window actually in front, not the one the session locked.

        This used to report the window lock straight back. Every captured
        event was then stamped with the locked window no matter where the
        user really clicked, `_in_scope` compared the lock against the scope
        it came from and so could never fail, and a flow that crossed into
        another of the application's windows was recorded as though it had
        never left the first one. A live capture stamped a click at
        (1846, 877) — nowhere near the locked window — as belonging to it.

        macOS has no usable "frontmost window" for the tray-style processes
        this recorder targets (they are background-only and never own the
        menu bar), so the front of the CGWindowList stacking order is the
        honest answer: the first on-screen window that is neither desktop
        chrome nor the recorder's own UI.
        """
        front = self._frontmost_window()
        if front is not None:
            return front
        # No window could be resolved from the stacking order; fall back to
        # the lock so correlation still has a scope to check rather than
        # dropping the event outright.
        lock_result = self._backend.get_window_lock()
        lock = lock_result.get("lock") if isinstance(lock_result, Mapping) else None
        lock = lock if isinstance(lock, Mapping) else {}
        snapshot = lock.get("snapshot") if isinstance(lock.get("snapshot"), Mapping) else {}
        pid = lock.get("pid") or snapshot.get("pid")
        title = snapshot.get("title") or ""
        result: dict[str, object] = {
            "processName": lock.get("process_name") or snapshot.get("process_name") or "",
            "windowTitle": title,
            "pid": pid,
        }
        handle = self._foreground_handle(pid, title)
        if handle is not None:
            result["handle"] = handle
        return result

    def recorder_ui_rect(self) -> tuple[int, int, int, int] | None:
        """Real on-screen bounds of the recorder's own window.

        Tk reports its *client* geometry, which excludes the title bar: the
        recorder's reported rect sat 32px below its real top edge, so dragging
        the recorder by its title bar was not recognised as landing on the
        recorder at all and got recorded as a drag inside the target window.
        The window server's own bounds include the frame.
        """
        for item in self._cg_windows() or ():
            if RECORDER_UI_MARKER in str(item.get("kCGWindowName") or ""):
                bounds = item.get("kCGWindowBounds") or {}
                try:
                    return (
                        int(bounds["X"]), int(bounds["Y"]),
                        int(bounds["Width"]), int(bounds["Height"]),
                    )
                except Exception:
                    return None
        return None

    def window_at(self, x: int, y: int) -> dict[str, object] | None:
        """The window under a screen point, by stacking order.

        Attributing a click to whatever window happens to be *frontmost* is
        wrong: the pointer carries its own answer. A live capture clicked
        inside the client's log window while another application was in
        front, and every one of those clicks was judged out of scope and
        dropped — 671 of them in one session.

        Returns the topmost normal window containing the point, so a click
        on a dialog stacked over its parent belongs to the dialog.
        """
        for item in self._cg_windows() or ():
            if item.get("kCGWindowLayer") != 0:
                continue
            title = str(item.get("kCGWindowName") or "")
            if RECORDER_UI_MARKER in title:
                continue
            bounds = item.get("kCGWindowBounds") or {}
            try:
                left = float(bounds.get("X", 0))
                top = float(bounds.get("Y", 0))
                width = float(bounds.get("Width", 0))
                height = float(bounds.get("Height", 0))
            except Exception:
                continue
            if width <= 1 or height <= 1:
                continue
            if not (left <= x < left + width and top <= y < top + height):
                continue
            pid = item.get("kCGWindowOwnerPID")
            number = item.get("kCGWindowNumber")
            result: dict[str, object] = {
                "processName": self._process_name_for(pid) or str(
                    item.get("kCGWindowOwnerName") or ""
                ),
                "windowTitle": title,
                "pid": int(pid) if pid is not None else None,
            }
            if number is not None:
                result["handle"] = int(number)
            return result
        return None

    def _frontmost_window(self) -> dict[str, object] | None:
        """Front of the CGWindowList stacking order, ignoring chrome and self."""
        windows = self._cg_windows()
        if not windows:
            return None
        for item in windows:
            owner = str(item.get("kCGWindowOwnerName") or "")
            title = str(item.get("kCGWindowName") or "")
            # Normal application windows sit on layer 0. The menu bar extras,
            # Dock, notification centre and Window Server shields all live on
            # other layers and are permanently in front of everything, so a
            # search that does not filter by layer always returns one of them
            # instead of the window the user is working in. Filtering by layer
            # is also why this does not need a list of chrome process names,
            # which would have to be localised (the Dock reports as "程序坞"
            # on a Chinese system).
            if item.get("kCGWindowLayer") != 0:
                continue
            if RECORDER_UI_MARKER in title:
                # The recorder's own always-on-top window is never the
                # window the user is working in.
                continue
            bounds = item.get("kCGWindowBounds") or {}
            try:
                width = float(bounds.get("Width", 0))
                height = float(bounds.get("Height", 0))
            except Exception:
                continue
            if width <= 1 or height <= 1:
                continue  # off-screen probes and 1px helper windows
            pid = item.get("kCGWindowOwnerPID")
            number = item.get("kCGWindowNumber")
            result: dict[str, object] = {
                # The executable name, matching what the capture scope is
                # expressed in; the CGWindowList owner name is the
                # application's name and can differ.
                "processName": self._process_name_for(pid) or owner,
                "windowTitle": title,
                "pid": int(pid) if pid is not None else None,
            }
            if number is not None:
                result["handle"] = int(number)
            return result
        return None

    # A CGWindowList query costs ~27ms. Correlation runs once per captured
    # input packet, so an unshared query per packet backs the worker up by
    # seconds during ordinary typing and a click then lands in the recording
    # ten seconds after the user made it. Window stacking does not change
    # meaningfully inside this window, so a burst of packets shares one
    # snapshot.
    _CG_SNAPSHOT_TTL_SECONDS = 0.05

    def _cg_windows(self) -> list[Mapping[str, object]] | None:
        """The current CGWindowList snapshot, or None when unavailable."""
        cached = getattr(self, "_cg_window_source", self._AUTO_WINDOWS)
        if cached is None:
            return None
        if cached is not self._AUTO_WINDOWS:
            source = cached() if callable(cached) else cached
            return list(source or [])
        now = time.monotonic()
        snapshot = getattr(self, "_cg_snapshot", None)
        if snapshot is not None and now - snapshot[0] < self._CG_SNAPSHOT_TTL_SECONDS:
            return snapshot[1]
        try:
            import Quartz
        except Exception:
            return None
        try:
            listing = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
        except Exception:
            return None
        windows = list(listing or [])
        self._cg_snapshot = (now, windows)
        return windows

    def _foreground_handle(self, pid: object, title: str) -> int | None:
        """CGWindowNumber of the connected window, resolved in-process.

        This is called once per captured input event (foreground() is the
        correlator's hot path), so it queries CGWindowList directly rather
        than going through the backend's list_windows() — that path is
        AppleScript-based and costs 10-20 s per call on a live desktop, the
        exact cost that made per-event AX correlation drop nearly every
        event before it was replaced with single-point AX access. A direct
        Quartz call answers this in low single-digit milliseconds.

        Returns None rather than an unrelated window's number when pid/title
        do not narrow the listing to a match — a wrong-but-valid-looking
        handle is worse than admitting the lookup failed, since a stale
        connection could otherwise silently borrow whatever window happens
        to come first in the snapshot.
        """
        windows = self._cg_windows()
        if not windows:
            return None
        candidates = windows
        if pid is not None:
            candidates = [
                item for item in candidates
                if item.get("kCGWindowOwnerPID") == pid
            ]
        if title:
            candidates = [
                item for item in candidates
                if str(item.get("kCGWindowName") or "") == title
            ]
        elif pid is None:
            # Neither pid nor title narrows the search: nothing identifies
            # which window this is supposed to be.
            return None
        for item in candidates:
            number = item.get("kCGWindowNumber")
            if number is not None:
                return int(number)
        return None

    @staticmethod
    def _process_name_for(pid: object) -> str:
        """Executable name for a pid, or "" when it cannot be resolved."""
        if pid is None:
            return ""
        try:
            import psutil

            return str(psutil.Process(int(pid)).name())
        except Exception:
            return ""

    def application_process_names(self, process_name: str) -> list[str]:
        """Process names that belong to the same application as `process_name`.

        A macOS application is not one process. HiSec ships its agent and its
        client as separate executables inside a single `.app`, and its primary
        flow crosses from one to the other — the main window opens the security
        centre, which is a different process. Scoping a recording to a single
        process name therefore drops everything the user does after that
        crossing, while still being the privacy boundary that keeps unrelated
        applications out. The bundle is the honest boundary: same application,
        any of its processes.

        Returns an empty list when the bundle cannot be resolved, which leaves
        the caller with single-process behaviour rather than a silently wider
        scope.
        """
        try:
            import psutil
        except Exception:
            return []
        expected = str(process_name or "").lower()
        if not expected:
            return []
        bundle = None
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                if str(proc.info.get("name") or "").lower() != expected:
                    continue
                match = re.match(r"(?i)(.*?\.app)/", str(proc.info.get("exe") or ""))
                if match:
                    bundle = match.group(1)
                    break
            except Exception:
                continue
        if not bundle:
            return []
        prefix = bundle.lower() + "/"
        names: set[str] = set()
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                if str(proc.info.get("exe") or "").lower().startswith(prefix):
                    name = proc.info.get("name")
                    if name:
                        names.add(str(name))
            except Exception:
                continue
        return sorted(names)

    def windows(self) -> list[Mapping[str, object]]:
        """Top-level windows with their owning process, for scope seeding.

        Without this the macOS seed was skipped entirely
        (``scope_seed_error="resolver cannot enumerate windows"``), so an
        application window that was already open when recording began was
        never admitted — every click inside it is dropped as out of scope.

        Like the Windows resolver, this reuses the backend's own enumeration
        rather than querying the desktop a second way.
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
        found: list[Mapping[str, object]] = []
        for window in items:
            if not isinstance(window, Mapping):
                continue
            pid = window.get("process_id") or window.get("pid")
            # Resolve the real process name from the pid rather than trusting
            # the listing's app_name. CGWindowList reports kCGWindowOwnerName,
            # which is the *application's* name and can differ from the
            # executable that owns the window: HiSec's client windows report
            # "HiSecEndpoint" while the process is "EDRClient". Capture-time
            # scope checks use the executable name (System Events / psutil
            # agree on it), so seeding on app_name silently refused to admit
            # exactly the windows this seeding exists to admit.
            process_name = self._process_name_for(pid) or str(
                window.get("process_name") or window.get("app_name") or ""
            )
            if not process_name:
                continue
            found.append({
                "title": str(
                    window.get("title") or window.get("window_title") or ""
                ),
                "pid": int(pid) if pid is not None else None,
                "processName": process_name,
                "handle": window.get("handle"),
            })
        return found

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
            "ownerPid": control.get("owner_pid"),
            "_identity": {
                "automation_id": control.get("automation_id"),
                "identifier": control.get("identifier"),
                "text": control.get("text") or control.get("name") or control.get("title"),
            },
        }

    # ── Native AX access ──────────────────────────────────────────────────
    #
    # Correlation runs once per captured input event, so its cost bounds how
    # fast a recording can keep up with a person typing.  Enumerating the
    # whole AX tree through AppleScript costs 10-20 s per call on a real
    # desktop (and times out outright on some applications), which loses
    # nearly every event.  The single-point AX APIs answer the same question
    # in ~3 ms, so they are the primary path and the tree walk stays only as
    # a fallback for hosts without the AX bindings.

    def _ax(self):
        """Return the ApplicationServices AX module, or None when missing."""
        cached = getattr(self, "_ax_module", None)
        if cached is self._AX_UNAVAILABLE:
            return None
        if cached is not None:
            return cached
        try:
            import ApplicationServices as module  # type: ignore[import-not-found]
        except Exception:
            self._ax_module = self._AX_UNAVAILABLE
            return None
        for name in (
            "AXUIElementCreateSystemWide",
            "AXUIElementCopyElementAtPosition",
            "AXUIElementCopyAttributeValue",
        ):
            if not hasattr(module, name):
                self._ax_module = self._AX_UNAVAILABLE
                return None
        self._ax_module = module
        return module

    def _system_wide(self):
        element = getattr(self, "_ax_system_wide", None)
        if element is None:
            module = self._ax()
            if module is None:
                return None
            element = module.AXUIElementCreateSystemWide()
            self._ax_system_wide = element
        return element

    def _attribute(self, element, name: str):
        module = self._ax()
        if module is None or element is None:
            return None
        try:
            error, value = module.AXUIElementCopyAttributeValue(element, name, None)
        except Exception:
            return None
        return value if error == 0 else None

    def _text_attribute(self, element, name: str) -> str:
        value = self._attribute(element, name)
        if value is None or isinstance(value, (list, tuple, dict)):
            return ""
        return str(value)

    def _bool_attribute(self, element, name: str) -> bool:
        value = self._attribute(element, name)
        return bool(value) if value is not None else False

    def _ax_rectangle(self, element) -> dict[str, int] | None:
        module = self._ax()
        position = self._attribute(element, "AXPosition")
        size = self._attribute(element, "AXSize")
        if module is None or position is None or size is None:
            return None
        unpack = getattr(module, "AXValueGetValue", None)
        try:
            if callable(unpack):
                ok_position, point = unpack(
                    position, module.kAXValueCGPointType, None,
                )
                ok_size, extent = unpack(size, module.kAXValueCGSizeType, None)
                if not (ok_position and ok_size):
                    return None
                return {
                    "x": int(point.x), "y": int(point.y),
                    "w": int(extent.width), "h": int(extent.height),
                }
        except Exception:
            return None
        return None

    def _ax_ancestry(self, element, *, limit: int = 12) -> list[str]:
        ancestry: list[str] = []
        current = self._attribute(element, "AXParent")
        while current is not None and len(ancestry) < limit:
            role = self._text_attribute(current, "AXRole")
            if not role:
                break
            ancestry.append(role)
            current = self._attribute(current, "AXParent")
        ancestry.reverse()
        return ancestry

    def _ax_control(self, element) -> Mapping[str, Any] | None:
        """Build the same control mapping shape that ``dump_tree`` emits.

        Keeping the shape identical is what lets ``_describe`` — and the
        ``_identity`` that :meth:`refresh` matches on — stay consistent no
        matter which path produced the control.
        """
        if element is None:
            return None
        role = self._text_attribute(element, "AXRole")
        subrole = self._text_attribute(element, "AXSubrole")
        if not role:
            return None
        owner_pid = self._element_pid(element)
        # Mirrors the AppleScript boundary: a secure field's value must never
        # be read, let alone leave the target.
        protected = any(
            marker in candidate.lower()
            for candidate in (role, subrole)
            for marker in ("securetextfield", "password")
        )
        value = "" if protected else self._text_attribute(element, "AXValue")
        title = self._text_attribute(element, "AXTitle")
        description = self._text_attribute(element, "AXDescription")
        identifier = self._text_attribute(element, "AXIdentifier")
        checked = None
        if "checkbox" in role.lower() or "radiobutton" in role.lower():
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on", "checked"}:
                checked = True
            elif normalized in {"0", "false", "no", "off", "unchecked"}:
                checked = False
        rectangle = self._ax_rectangle(element)
        return {
            "role": role,
            "class_name": role,
            "subrole": subrole,
            "title": title,
            "text": title or description or value,
            "description": description,
            "value": value,
            "automation_id": identifier,
            "identifier": identifier,
            "is_enabled": self._bool_attribute(element, "AXEnabled"),
            "is_visible": rectangle is not None,
            "rectangle": rectangle,
            "focused": self._bool_attribute(element, "AXFocused"),
            "selected": self._bool_attribute(element, "AXSelected"),
            "checked": checked,
            "protected": protected,
            "ancestry": self._ax_ancestry(element),
            # Which process actually owns this element. Scope admission looks
            # at the window; reading the element looks at raw coordinates, and
            # when those two disagree the recorder reads another application's
            # content — a live capture pulled a browser's web area into the
            # recording that way.
            "owner_pid": owner_pid,
        }

    def _element_pid(self, element) -> int | None:
        """Process that owns an accessibility element, when it can be read."""
        module = self._ax()
        getter = getattr(module, "AXUIElementGetPid", None) if module else None
        if not callable(getter):
            return None
        try:
            error, pid = getter(element, None)
        except Exception:
            return None
        return int(pid) if error == 0 and pid else None

    def _remember(self, described: Mapping[str, object], element) -> Mapping[str, object]:
        """Keep the live AX handle so refresh() can re-read without a scan."""
        handles = getattr(self, "_ax_handles", None)
        if handles is None:
            handles = self._ax_handles = {}
        identity = described.get("_identity")
        if isinstance(identity, Mapping):
            key = tuple(sorted((str(k), str(v)) for k, v in identity.items()))
            handles[key] = element
            # The recorder only ever refreshes a recent target; keeping the
            # map small avoids retaining handles for a whole session.
            if len(handles) > 64:
                for stale in list(handles)[:-64]:
                    handles.pop(stale, None)
        return described

    def _handle_for(self, target: Mapping[str, object]):
        handles = getattr(self, "_ax_handles", None)
        identity = target.get("_identity")
        if not handles or not isinstance(identity, Mapping):
            return None
        key = tuple(sorted((str(k), str(v)) for k, v in identity.items()))
        return handles.get(key)

    # ── Fallback tree walk ────────────────────────────────────────────────

    def _controls(self) -> list[Mapping[str, Any]]:
        result = self._backend.dump_tree(max_depth=12)
        if not result.get("ok"):
            return []
        return [item for item in result.get("controls", []) if isinstance(item, Mapping)]

    def _application_element(self, pid: int):
        """AX element for one application, cached per pid."""
        module = self._ax()
        create = getattr(module, "AXUIElementCreateApplication", None) if module else None
        if not callable(create):
            return None
        cache = getattr(self, "_ax_app_elements", None)
        if cache is None:
            cache = self._ax_app_elements = {}
        element = cache.get(pid)
        if element is None:
            try:
                element = create(pid)
            except Exception:
                return None
            cache[pid] = element
        return element

    def element_at(
        self, x: int, y: int, pid: int | None = None,
    ) -> Mapping[str, object] | None:
        # Hit testing the system-wide element answers "what is on top of the
        # screen here", which is a different question from "what did the user
        # click in the application being recorded". It returns the frontmost
        # application's element whatever the coordinate, so a click was
        # resolved against a chat window covering the target, and inside the
        # target it returned only the outermost container. Asking the
        # application directly answers the question actually being asked.
        root = self._application_element(pid) if pid is not None else None
        system_wide = root if root is not None else self._system_wide()
        if system_wide is not None:
            module = self._ax()
            try:
                error, element = module.AXUIElementCopyElementAtPosition(
                    system_wide, float(x), float(y), None,
                )
            except Exception:
                error, element = 1, None
            if error == 0 and element is not None:
                control = self._ax_control(element)
                if control is not None:
                    return self._remember(self._describe(control), element)
            # A hit test that legitimately lands on no element (empty desktop)
            # is not a reason to pay for a whole tree walk.
            if error == 0:
                return None
        hits = []
        for control in self._controls():
            rect = self._rectangle(control)
            if rect and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                hits.append((max(1, (rect[2] - rect[0]) * (rect[3] - rect[1])), control))
        if not hits:
            return None
        return self._describe(min(hits, key=lambda item: item[0])[1])

    def refresh(self, target: Mapping[str, object]) -> Mapping[str, object]:
        element = self._handle_for(target)
        if element is not None:
            control = self._ax_control(element)
            if control is not None:
                return self._remember(self._describe(control), element)
            # The handle went stale (element destroyed); fall through to the
            # scan rather than reporting a value that is no longer on screen.
        identity = target.get("_identity") or {}
        for control in self._controls():
            described = self._describe(control)
            if described.get("_identity") == identity:
                return described
        return target

    def focused(self) -> Mapping[str, object] | None:
        system_wide = self._system_wide()
        if system_wide is not None:
            element = self._attribute(system_wide, "AXFocusedUIElement")
            if element is not None:
                control = self._ax_control(element)
                if control is not None:
                    return self._remember(self._describe(control), element)
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

    def __init__(
        self,
        process_name: str,
        *,
        interval_seconds: float = 0.25,
        process_names: Callable[[], Any] | None = None,
        gate: "CaptureScopeGate | None" = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.process_name = process_name
        # Publishing the application's window rects here is what lets the
        # event tap reject foreign input without a system call of its own.
        self._gate = gate
        self._interval = interval_seconds
        # Resolves the application's full process set. A window opened during
        # a recording by a *different* process of the same application — HiSec
        # opens its security centre that way — is otherwise invisible here, so
        # the scope never grows to include it and every click inside the new
        # window is dropped.
        self._process_names = process_names
        self._pid_names: dict[int, str] = {}
        self._emit: Callable[[HookPacket], None] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @staticmethod
    def _matches(actual: object, expected: str) -> bool:
        return str(actual or "").lower().removesuffix(".exe") == expected.lower().removesuffix(".exe")

    def _admitted(self) -> set[str]:
        """Lowercased executable names whose windows this driver reports."""
        admitted = {self.process_name.lower().removesuffix(".exe")}
        if self._process_names is not None:
            try:
                for name in self._process_names() or ():
                    text = str(name or "").lower().removesuffix(".exe")
                    if text:
                        admitted.add(text)
            except Exception:
                pass
        return admitted

    def _name_for_pid(self, pid: int) -> str:
        """Executable name for a pid, cached across polls."""
        cached = self._pid_names.get(pid)
        if cached is not None:
            return cached
        try:
            import psutil

            name = str(psutil.Process(pid).name())
        except Exception:
            name = ""
        self._pid_names[pid] = name
        return name

    def _windows(self) -> dict[int, tuple[str, int, str]]:  # pragma: no cover - live macOS only
        import Quartz

        listing = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        admitted = self._admitted()
        found: dict[int, tuple[str, int]] = {}
        rects: list[dict[str, Any]] = []
        front_owner: str | None = None
        front: dict[str, Any] | None = None
        for item in listing:
            number = item.get("kCGWindowNumber")
            if number is None:
                continue
            pid = item.get("kCGWindowOwnerPID")
            pid = int(pid) if pid is not None else 0
            # kCGWindowOwnerName is the *application's* name and can differ
            # from the executable that owns the window: HiSec's client windows
            # report "HiSecEndpoint" while the process is "EDRClient". Match on
            # the executable, falling back to the owner name when the pid can
            # no longer be resolved.
            name = (self._name_for_pid(pid) if pid else "") or str(
                item.get("kCGWindowOwnerName") or ""
            )
            if item.get("kCGWindowLayer") == 0 and front_owner is None:
                front_owner = name.lower().removesuffix(".exe")
            if name.lower().removesuffix(".exe") not in admitted:
                continue
            title = str(item.get("kCGWindowName") or "")
            # Carry the owning executable, not the scope's process name: the
            # application spans several processes and a transition reported
            # under the wrong one mislabels which application owns the page.
            found[int(number)] = (title, pid, name)
            bounds = item.get("kCGWindowBounds") or {}
            try:
                rect = (
                    float(bounds["X"]), float(bounds["Y"]),
                    float(bounds["Width"]), float(bounds["Height"]),
                )
            except Exception:
                continue
            entry = {
                "processName": name,
                "windowTitle": title,
                "pid": pid or None,
                "handle": int(number),
                "rect": rect,
            }
            rects.append(entry)
            if front is None and item.get("kCGWindowLayer") == 0:
                front = entry
        if self._gate is not None:
            self._gate.publish(tuple(rects), front_owner in admitted, front)
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
            for number, (title, pid, owner) in current.items():
                if number not in known:
                    self._packet("opened", title, pid, number, owner)
            for number, (title, pid, owner) in known.items():
                if number not in current:
                    self._packet("closed", title, pid, number, owner)
            known = current

    def _packet(  # pragma: no cover - live macOS only
        self, kind: str, title: str, pid: int, number: int,
        owner: str | None = None,
    ) -> None:
        self._emit(HookPacket(
            kind="window_transition",
            monotonic_ms=int(time.monotonic() * 1000),
            wall_time=_utc_now(),
            native={
                "kind": kind,
                "processName": owner or self.process_name,
                "title": title,
                "pid": pid or None,
                "handle": number,
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
        resolver = MacOSAXResolver(backend)
        # One gate shared by both drivers: the window poller publishes the
        # application's on-screen rects, the event tap uses them to refuse
        # input that cannot belong to the target before it is ever queued.
        gate = CaptureScopeGate()
        source = QueuedCaptureSource(
            scope=scope,
            sink=sink,
            driver=CompositeHookDriver(
                MacOSEventTapDriver(gate=gate),
                MacOSWindowEventDriver(
                    scope.process_name,
                    gate=gate,
                    # Share the resolver so window transitions cover every
                    # process of the application, not only the one the scope
                    # happens to name.
                    process_names=lambda: resolver.application_process_names(
                        scope.process_name,
                    ),
                ),
            ),
            correlator=MacOSAXCorrelator(resolver),
        )
        source.scope_gate = gate
        return source

    return create


__all__ = [
    "MacOSAXCorrelator",
    "MacOSAXResolver",
    "CaptureScopeGate",
    "MacOSEventTapDriver",
    "MacOSPermissionPreflight",
    "MacOSWindowEventDriver",
    "macos_source_factory",
]
