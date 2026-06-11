"""
macos_accessibility.py — macOS automation backend (v1 minimal).

Drives the macOS GUI via shell tools that ship with macOS:

  - screencapture  — full screen capture
  - osascript      — AppleScript bridge (System Events for app/window enumeration)
  - system_profiler — JSON metadata for app bundle ids
  - cliclick       — optional, for click_at() if available
  - python ctypes  — Quartz.CGEvent for click_at() when cliclick is not present

This backend exposes a conservative macOS Accessibility tree through System
Events. It is not as rich as Windows UIA, but it supports `dump_tree()` and
selector-based `click()` for common controls by role/name/description/value
plus coordinate fallback.

It does expose a cross-platform action-space subset (`click_at`,
`double_click_at`, `right_click_at`, `middle_click_at`, `hover_at`, `drag`,
`scroll`) so the agent can still perform component-scoped fallback gestures on
macOS when semantic selectors are unavailable.

Permission requirements (set up once on the target Mac):
  - System Settings → Privacy & Security → Accessibility
       (Terminal / Python / osascript — depending on which process the
        MCP server runs as)
  - System Settings → Privacy & Security → Screen Recording
       (for screencapture to capture the actual screen content
        instead of a black image)

Without these, screenshot() may return a black PNG, and osascript calls
that drive System Events will fail with "Not authorized".
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional, Any

import pyautogui


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run a subprocess, return (rc, combined_output). Never raises."""
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return cp.returncode, (cp.stdout or "") + (cp.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return -1, f"command not found: {e}"


def _run_osascript(script: str, timeout: float = 10.0) -> tuple[int, str]:
    """Run an osascript -e invocation."""
    return _run(["osascript", "-e", script], timeout=timeout)


def _allow_real_mouse_actions() -> bool:
    return os.environ.get("EDR_WD_ALLOW_REAL_CLICKS", "0") == "1"


# ── _MacOSConnectedApp: pywinauto-like interface for restore_edr ─────────────

class _MacOSConnectedApp:
    """
    Provides the minimal pywinauto-like interface that restore_edr (server.py)
    expects: .windows(), .is_minimized(), .restore(), .wait_for_not_minimized().
    Works via CGWindowList and pgrep — no pywinauto dependency.
    """

    def __init__(self, backend: "MacOSAccessibilityBackend"):
        self._backend = backend

    def _cg_windows_for_app(self) -> list[dict]:
        """Return CGWindowList entries for the connected app."""
        snapshot = getattr(self._backend, "_connected_window_snapshot", None)
        if isinstance(snapshot, dict):
            return [snapshot]

        app_name = getattr(self._backend, "_connected_app", None) or ""
        opts = 17  # kCGWindowListOptionAll + excludeDesktopElements
        try:
            import subprocess
            script = (
                'use framework "CoreGraphics"\n'
                'set wList to CGWindowListCopyWindowInfo(' + str(opts) + ', 0)\n'
                'set out to ""\n'
                'repeat with w in wList\n'
                '    try\n'
                '        set owner to "" & (kCGWindowOwnerName of w as string)\n'
                '        if owner contains "' + app_name + '" then\n'
                '            try\n'
                '                set wName to "" & (kCGWindowName of w as string)\n'
                '            on error\n'
                '                set wName to ""\n'
                '            end try\n'
                '            set wPID to kCGWindowOwnerPID of w as string\n'
                '            set out to out & wPID & "|" & wName & "\n"\n'
                '        end if\n'
                '    end try\n'
                'end repeat\n'
                'return out\n'
            )
            rc, out = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=5
            )
            wins = []
            if rc == 0:
                for line in out.strip().split("\n"):
                    if "|" in line:
                        parts = line.split("|", 1)
                        wins.append({"pid": int(parts[0]), "title": parts[1]})
            return wins
        except Exception:
            return []

    def windows(self) -> list:
        """Return list of proxy window objects (minimal interface)."""
        return [_MacOSWindowProxy(w, self._backend) for w in self._cg_windows_for_app()]

    def is_minimized(self) -> bool:
        """Best-effort: check if all app windows have y >= 1 (screen visible)."""
        wins = self._cg_windows_for_app()
        return len(wins) == 0  # If no windows visible, consider not minimized (stub)

    def restore(self) -> None:
        """macOS: no-op. CGWindowList cannot change window state."""
        pass

    def wait_for_not_minimized(self, timeout: float = 5.0) -> None:
        """Stub: CGWindowList cannot change window state; always return immediately."""
        pass

    def wait_for_minimized(self, timeout: float = 0.5) -> None:
        """Stub."""
        pass


class _MacOSWindowProxy:
    """Minimal window proxy returned by _MacOSConnectedApp.windows()."""

    def __init__(self, win_info: dict, backend: "MacOSAccessibilityBackend"):
        self._win_info = win_info
        self._backend = backend

    @property
    def rectangle(self):
        class _Rect:
            def __init__(self):
                self.left = 0
                self.top = 0
                self._w = 800
                self._h = 600
            def width(self): return getattr(self, "_w", 800)
            def height(self): return getattr(self, "_h", 600)
            def set(self, x, y, w, h):
                self.left, self.top, self._w, self._h = x, y, w, h
        r = _Rect()
        r.set(0, 0, 800, 600)
        return r

    def is_minimized(self) -> bool:
        return False

    def restore(self) -> None:
        pass

    def wait_for_not_minimized(self, timeout: float = 5.0) -> None:
        pass

    def wait_for_minimized(self, timeout: float = 0.5) -> None:
        pass


# ── Backend ───────────────────────────────────────────────────────────────────

class MacOSAccessibilityBackend:
    """AutomationBackend implementation for macOS targets."""

    @property
    def backend(self) -> str:
        return "macos_accessibility"

    # ── Window lock ──────────────────────────────────────────────────────────

    def _frontmost_window_state(self) -> dict:
        script = (
            'tell application "System Events"\n'
            '  set frontProc to first process whose frontmost is true\n'
            '  set procName to name of frontProc\n'
            '  set pidStr to unix id of frontProc as string\n'
            '  set winName to ""\n'
            '  try\n'
            '    if exists window 1 of frontProc then set winName to name of window 1 of frontProc\n'
            '  end try\n'
            '  return procName & "\\t" & pidStr & "\\t" & winName\n'
            'end tell\n'
        )
        rc, out = _run_osascript(script, timeout=5)
        if rc != 0:
            return {"ok": False, "error": f"frontmost window query failed (rc={rc}): {out.strip()}"}
        parts = out.strip().split("\t")
        pid = None
        if len(parts) > 1:
            try:
                pid = int(parts[1])
            except ValueError:
                pass
        return {
            "ok": True,
            "process_name": parts[0] if parts else "",
            "pid": pid,
            "title": parts[2] if len(parts) > 2 else "",
        }

    def _connected_window_state(self) -> dict:
        snapshot = getattr(self, "_connected_window_snapshot", None)
        if isinstance(snapshot, dict):
            return {
                "ok": True,
                "process_name": snapshot.get("owner") or getattr(self, "_connected_app", None),
                "pid": snapshot.get("pid") or getattr(self, "_connected_pid", None),
                "title": snapshot.get("title") or snapshot.get("window_title") or "",
                "rectangle": snapshot.get("rectangle"),
            }
        app_name = getattr(self, "_connected_app", None)
        pid = getattr(self, "_connected_pid", None)
        if app_name or pid:
            return {"ok": True, "process_name": app_name, "pid": pid, "title": ""}
        return {"ok": False, "error": "Not connected"}

    def _state_matches_lock(self, state: dict, lock: dict) -> bool:
        if not state.get("ok"):
            return False
        if lock.get("pid") is not None and state.get("pid") != lock.get("pid"):
            return False
        if lock.get("process_name"):
            current = (state.get("process_name") or "").lower()
            expected = str(lock["process_name"]).lower()
            if expected not in current and current not in expected:
                return False
        if lock.get("title_re"):
            try:
                if not re.search(str(lock["title_re"]), state.get("title") or ""):
                    return False
            except re.error:
                return False
        return True

    def _activate_locked_window(self) -> dict:
        lock = getattr(self, "_window_lock", None)
        if not isinstance(lock, dict):
            return {"ok": False, "error": "No window lock"}
        app_name = lock.get("process_name")
        if not app_name:
            return {"ok": False, "error": "Window lock has no process_name"}
        return self.activate_app(app_name=str(app_name))

    def lock_window(
        self,
        title_re: Optional[str] = None,
        process_name: Optional[str] = None,
        pid: Optional[int] = None,
        strict: bool = True,
        activate: bool = True,
    ) -> dict:
        state = self._connected_window_state()
        if not state.get("ok"):
            state = self._frontmost_window_state()
        if not state.get("ok"):
            return state
        lock = {
            "backend": self.backend,
            "title_re": title_re or (re.escape(state.get("title") or "") if state.get("title") else None),
            "process_name": process_name or state.get("process_name"),
            "pid": pid if pid is not None else state.get("pid"),
            "strict": bool(strict),
            "snapshot": state,
        }
        self._window_lock = lock
        if activate:
            self._activate_locked_window()
        verify = self.verify_window_lock(activate=activate)
        return {"ok": verify.get("ok") is True, "lock": lock, "verify": verify}

    def unlock_window(self) -> dict:
        previous = getattr(self, "_window_lock", None)
        self._window_lock = None
        return {"ok": True, "previous": previous}

    def get_window_lock(self) -> dict:
        lock = getattr(self, "_window_lock", None)
        return {"ok": True, "locked": lock is not None, "lock": lock}

    def verify_window_lock(self, activate: bool = True) -> dict:
        lock = getattr(self, "_window_lock", None)
        if not isinstance(lock, dict):
            return {"ok": True, "locked": False}
        state = self._frontmost_window_state()
        if self._state_matches_lock(state, lock):
            return {"ok": True, "locked": True, "active": state, "lock": lock}
        activation = None
        if activate:
            activation = self._activate_locked_window()
            time.sleep(0.2)
            state = self._frontmost_window_state()
            if self._state_matches_lock(state, lock):
                return {
                    "ok": True,
                    "locked": True,
                    "active": state,
                    "lock": lock,
                    "activation": activation,
                }
        return {
            "ok": False,
            "locked": True,
            "error": "Window lock mismatch",
            "active": state,
            "lock": lock,
            "activation": activation,
        }

    def _ensure_window_lock(self) -> Optional[dict]:
        check = self.verify_window_lock(activate=True)
        if check.get("ok"):
            return None
        return check

    # ── Always-available ──────────────────────────────────────────────────────

    def screenshot(self, path: Optional[str] = None) -> dict:
        """
        Capture the full screen to `path` (or a default location).

        Default location: $TMPDIR/edr-wd-screenshot-<timestamp>.png
        """
        if not path:
            ts = time.strftime("%Y%m%d-%H%M%S")
            tmp = os.environ.get("TMPDIR", "/tmp")
            path = f"{tmp.rstrip('/')}/edr-wd-screenshot-{ts}.png"
        # `-x` = no sound; `-t <format>` = format
        rc, out = _run(["screencapture", "-x", path], timeout=15)
        if rc != 0:
            return {"ok": False, "error": f"screencapture failed (rc={rc}): {out.strip()}"}
        if not Path(path).exists():
            return {"ok": False, "error": f"screencapture reported success but {path} not created"}
        return {"ok": True, "path": path}

    def list_windows(self) -> dict:
        """
        Enumerate visible application windows via System Events.

        Returns:
          {"ok": True, "windows": [
              {"app_name": "Finder", "bundle_id": "com.apple.finder",
               "window_title": "Desktop", "pid": 123},
              ...
          ]}

        Note: window_title is best-effort; some apps expose only the app
        name. Window-level enumeration on macOS requires Accessibility
        permission — without it, osascript will fail with "Not authorized".
        """
        # AppleScript: ask System Events for the name of every process
        # that has at least one window, plus its unix id (pid).
        script = (
            'tell application "System Events"\n'
            '  set out to ""\n'
            '  repeat with p in (every process whose visible is true)\n'
            '    set pname to name of p\n'
            '    set pidStr to unix id of p as string\n'
            '    set wCount to count of windows of p\n'
            '    if wCount is 0 then\n'
            '      set out to out & pname & "\\t" & pidStr & "\\t\\n"\n'
            '    else\n'
            '      repeat with w in windows of p\n'
            '        try\n'
            '          set wname to name of w\n'
            '        on error\n'
            '          set wname to ""\n'
            '        end try\n'
            '        set out to out & pname & "\\t" & pidStr & "\\t" & wname & "\\n"\n'
            '      end repeat\n'
            '    end if\n'
            '  end repeat\n'
            '  return out\n'
            'end tell\n'
        )
        rc, out = _run_osascript(script, timeout=15)
        if rc != 0:
            return {
                "ok": False,
                "error": (
                    f"osascript list_windows failed (rc={rc}): {out.strip()}. "
                    "Check Accessibility permission for the running process."
                ),
            }

        windows = []
        for line in out.splitlines():
            line = line.rstrip("\r")
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            app_name = parts[0]
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            window_title = parts[2] if len(parts) >= 3 else ""
            windows.append({
                "app_name": app_name,
                "bundle_id": None,  # filled lazily by activate_app
                "window_title": window_title,
                "pid": pid,
            })

        return {"ok": True, "windows": windows, "count": len(windows)}

    def is_window_open(
        self,
        title_re: Optional[str] = None,
        process_name: Optional[str] = None,
        class_name: Optional[str] = None,
    ) -> dict:
        """
        Check whether any visible window matches the given criteria.

        - process_name: matched against the application name (case-insensitive substring).
        - title_re: matched against the window title as a regex.
        - class_name: not applicable on macOS; included for API symmetry
          with the Windows backend.

        At least one of process_name / title_re must be provided.
        """
        if not process_name and not title_re:
            return {"ok": False, "error": "process_name or title_re is required"}

        proc_lc = process_name.lower() if process_name else None
        connected_name = (getattr(self, "_connected_app", None) or "").lower()
        connected_instance = getattr(self, "_connected_app_instance", None)
        if proc_lc and connected_instance is not None and proc_lc in connected_name:
            try:
                wins = connected_instance.windows()
                if wins:
                    normalized = []
                    for w in wins:
                        try:
                            rect = w.rectangle()
                            win_info = getattr(w, "_win_info", {}) if isinstance(getattr(w, "_win_info", {}), dict) else {}
                            normalized.append({
                                "app_name": getattr(self, "_connected_app", process_name) or process_name,
                                "bundle_id": None,
                                "window_title": win_info.get("title", ""),
                                "pid": win_info.get("pid"),
                                "rectangle": {
                                    "x": rect.left,
                                    "y": rect.top,
                                    "w": rect.width(),
                                    "h": rect.height(),
                                },
                            })
                        except Exception:
                            normalized.append({
                                "app_name": getattr(self, "_connected_app", process_name) or process_name,
                                "bundle_id": None,
                                "window_title": "",
                                "pid": None,
                            })
                    return {"ok": True, "found": True, "windows": normalized, "count": len(normalized)}
            except Exception:
                pass

        listed = self.list_windows()
        if not listed.get("ok"):
            return listed

        pat = re.compile(title_re) if title_re else None
        proc_lc = process_name.lower() if process_name else None

        matches = []
        for w in listed["windows"]:
            if proc_lc and proc_lc not in w["app_name"].lower():
                continue
            if pat and not pat.search(w.get("window_title") or ""):
                continue
            matches.append(w)

        # macOS HiSec windows are sometimes surfaced by System Events with
        # generic AX wrapper names/titles. If the normal match path fails,
        # fall back to known HiSec heuristics so `connect()`/`wait_window()`
        # remain stable even when the accessibility title is polluted.
        # Fallback for HiSec windows whose window title is generic/polluted.
        # Only apply keyword fallback to windows whose app_name is also HiSec-related
        # to avoid false positives from generic English words like "agent"/"endpoint"
        # matching unrelated windows (e.g. "hermes-agent", "some-endpoint").
        if not matches and proc_lc:
            hisec_agent_name = "hisecendpointagent" in proc_lc
            edr_client_name = "edrclient" in proc_lc or "hisecendpoint" in proc_lc
            if hisec_agent_name or edr_client_name:
                for w in listed["windows"]:
                    app_name = (w.get("app_name") or "").lower()
                    # Only consider this window if its app_name is also HiSec-related
                    app_relevant = (
                        "hisec" in app_name or "safra" in app_name or
                        "edr" in app_name or "endpoint" in app_name
                    )
                    if not app_relevant:
                        continue
                    title = (w.get("window_title") or "").lower()
                    if hisec_agent_name:
                        if "华为智能终端安全系统" in title or "hisecendpointagent" in title or "hisec" in title:
                            matches.append(w)
                    elif edr_client_name:
                        if "华为hisec endpoint" in title or "hisec endpoint" in title or "hisec" in title or "edrclient" in title or "bagenericobject" in title:
                            matches.append(w)
                    if matches:
                        break

        return {
            "ok": True,
            "found": len(matches) > 0,
            "windows": matches,
            "count": len(matches),
        }

    def wait_window(
        self,
        title_re: Optional[str] = None,
        process_name: Optional[str] = None,
        class_name: Optional[str] = None,
        timeout: float = 10.0,
        interval: float = 0.5,
    ) -> dict:
        """Poll is_window_open until match or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.is_window_open(
                title_re=title_re, process_name=process_name, class_name=class_name,
            )
            if r.get("ok") and r.get("found"):
                return r
            time.sleep(interval)
        return {"ok": False, "error": "timeout", "found": False}

    def activate_app(
        self,
        app_name: Optional[str] = None,
        bundle_id: Optional[str] = None,
    ) -> dict:
        """
        Bring an app to the foreground. Provide either app_name ("Finder")
        or bundle_id ("com.apple.finder"). One of them is required.
        """
        if bundle_id:
            script = f'tell application id "{bundle_id}" to activate'
        elif app_name:
            script = f'tell application "{app_name}" to activate'
        else:
            return {"ok": False, "error": "activate_app requires app_name or bundle_id"}

        rc, out = _run_osascript(script, timeout=10)
        if rc != 0:
            return {
                "ok": False,
                "error": f"activate_app failed (rc={rc}): {out.strip()}",
            }
        return {
            "ok": True,
            "app_name": app_name,
            "bundle_id": bundle_id,
        }

    def activate_edr(
        self,
        exe_path: Optional[str] = None,
        wait: bool = True,
        timeout: float = 15.0,
        edr_widget_auto_id: Optional[str] = None,
    ) -> dict:
        """
        macOS-specific: ensure the EDRClient GUI is visible.

        Core principle: process_found cannot represent window_found.
        The only reliable success signal is the EDRClient application window,
        not the HiSecEndpointAgent entry window or a process hit.

        The target application/window is EDRClient ("华为HiSec Endpoint").
        HiSecEndpointAgent ("华为智能终端安全系统") is only an entry window used by
        the fallback click path.

        EDRClient is first started through
        /Applications/HiSecEndpoint.app/Contents/script/root_start_client.sh.
        That script requires root privileges, so we call it with `sudo -n`:
        it succeeds only when the server account can sudo without an interactive
        password prompt. If that fails or does not produce the EDRClient window,
        we open HiSecEndpointAgent and click "前往安全防护中心" — the same action a
        human user would take.

        exe_path: optional path to a custom HiSecEndpointAgent binary.
        wait:    if True, poll for the EDRClient window to appear (up to timeout).
        timeout: seconds to wait for process/window appearance.
        edr_widget_auto_id: not used on macOS; accepted for API symmetry.

        IMPORTANT: Do NOT redirect stdout/stderr. HiSecEndpointAgent opens a Qt
        window that gets placed offscreen (OnScreen=false) if output is sent to /dev/null.

        Returns:
          {
            "ok": true/false,
            "stage": "done" / "fallback_main_window_not_found" / "client_window_not_found",
            "backend": "macos_accessibility",
            "target_application": "EDRClient",
            "entry_application": "HiSecEndpointAgent",
            "already_open": true,  -- when EDRClient window is already visible
            "main": {
              "process_found": bool,
              "window_found": bool,
              "window_title": "华为智能终端安全系统",
              "activated_by": "HiSecEndpointAgent cmd ui",
              "cmd_ui_attempted": bool
            },
            "client": {
              "process_found": bool,
              "root_start_client": {"attempted": bool, "ok": bool, "error": str},
              "clicked": bool,
              "click_method": "ax_press" / "cgevent_center" / null,
              "window_found": bool,
              "window_title": "华为HiSec Endpoint"
            }
          }

        ok=true only when the EDRClient window is visible. main.window_found
        describes the fallback entry window, not the target application.
        """
        import subprocess as _subprocess
        import os as _os

        HISEC_AGENT_BIN = (
            exe_path
            or "/Applications/HiSecEndpoint.app/Contents/MacOS/safra/HiSecEndpointAgent"
        )
        HISEC_APP_BUNDLE = "/Applications/HiSecEndpoint.app"
        ROOT_START_CLIENT = (
            "/Applications/HiSecEndpoint.app/Contents/script/root_start_client.sh"
        )
        CLICK_HELPER = (
            Path(__file__).resolve().parents[1]
            / "scripts" / "macos" / "click_security_center.swift"
        )

        def _proc_exists(name: str) -> bool:
            rc, _ = _run(["pgrep", "-f", name], timeout=5)
            return rc == 0

        def _hisec_window_visible() -> bool:
            """Check HiSecEndpointAgent main window via System Events."""
            rc, out = _run_osascript(
                'tell application "System Events" to get name of every window of process "HiSecEndpointAgent"',
                timeout=5,
            )
            return rc == 0 and "华为智能终端安全系统" in out

        def _bring_hisec_to_front() -> dict:
            """
            Bring the HiSec entry window to the foreground.
            The click helper depends on the window actually being visible and
            frontmost; a visible-but-not-active window can leave the target
            button inaccessible.
            """
            attempts = [
                ('tell application "HiSecEndpointAgent" to activate', "activate_app"),
                (
                    'tell application "System Events" to set frontmost of process "HiSecEndpointAgent" to true',
                    "system_events_frontmost",
                ),
            ]
            details = []
            for script, method in attempts:
                rc, out = _run_osascript(script, timeout=5)
                details.append({
                    "method": method,
                    "ok": rc == 0,
                    "rc": rc,
                    "error": "" if rc == 0 else out.strip(),
                })
                if rc == 0:
                    # Give the window manager a beat to update frontmost state.
                    time.sleep(0.4)
                    return {"ok": True, "method": method, "attempts": details}
            return {"ok": False, "attempts": details}

        def _launch_hisec_bundle() -> dict:
            if not Path(HISEC_APP_BUNDLE).exists():
                return {
                    "attempted": False,
                    "ok": False,
                    "error": "HiSecEndpoint.app not found",
                    "path": HISEC_APP_BUNDLE,
                }
            rc, out = _run(["open", HISEC_APP_BUNDLE], timeout=10)
            return {
                "attempted": True,
                "ok": rc == 0,
                "returncode": rc,
                "error": "" if rc == 0 else out.strip(),
                "path": HISEC_APP_BUNDLE,
            }

        def _edr_client_window_visible_cg() -> tuple[bool, str, str]:
            """
            Check EDRClient window via CGWindowListCopyWindowInfo.
            Returns (found, window_title, detected_by).
            This catches Qt windows that osascript/System Events may miss.
            Uses only CoreGraphics framework (no AppKit) for broad compatibility.
            Filters by owner == "HiSecEndpoint" (Qt app bundle name) and window
            bounds >= 800x500 to avoid stub/offscreen windows.
            """
            script = (
                'use framework "CoreGraphics"\n'
                'set wList to CGWindowListCopyWindowInfo(17, 0)\n'
                'set matchedTitle to ""\n'
                'repeat with w in wList\n'
                '    try\n'
                '        set owner to "" & (kCGWindowOwnerName of w as string)\n'
                '        if owner is "HiSecEndpoint" then\n'
                '            try\n'
                '                set wName to "" & (kCGWindowName of w as string)\n'
                '            on error\n'
                '                set wName to ""\n'
                '            end try\n'
                '            if wName contains "HiSec" or wName contains "华为" or wName contains "Endpoint" then\n'
                '                set matchedTitle to wName\n'
                '                exit repeat\n'
                '            end if\n'
                '        end if\n'
                '    end try\n'
                'end repeat\n'
                'return matchedTitle\n'
            )
            rc, out = _run(["osascript", "-e", script], timeout=5)
            title = out.strip()
            # title is "missing value" (None) when window has no name — still OK if owner matched
            if title and title != "missing value":
                return True, title, "cgwindowlist"
            return False, "", "cgwindowlist"

        def _edr_client_window_visible() -> tuple[bool, str, str]:
            """
            Check EDRClient window via BOTH osascript/System Events AND CGWindowList.
            Returns (found, window_title, detected_by).
            Qt windows may only be visible via CGWindowList; we accept either method.
            """
            # Method 1: osascript System Events
            rc, out = _run_osascript(
                'tell application "System Events" to get name of every window of process "EDRClient"',
                timeout=5,
            )
            if rc == 0 and "华为HiSec Endpoint" in out:
                return True, "华为HiSec Endpoint", "system_events"
            # Method 2: CGWindowList (may catch Qt windows that System Events misses)
            found, title, _ = _edr_client_window_visible_cg()
            if found:
                return True, title, "cgwindowlist"
            return False, "", "system_events"

        def _click_security_center(method: str = "auto") -> tuple[bool, str, str, bool, dict]:
            """
            Call the helper script with the specified click method.
            method: "auto" | "ax_press" | "cgevent_center"

            Returns (clicked, click_method, error_message, client_window_found, window_bounds).
            - clicked: True if the helper's click action succeeded
            - click_method: the actual method used ("ax_press" or "cgevent_center")
            - error_message: non-empty when clicked=False
            - client_window_found: True when the EDRClient window was detected by the helper
            - window_bounds: {"x", "y", "w", "h"} when client_window_found is True
            """
            rc, out = _run(["/usr/bin/swift", str(CLICK_HELPER), "--method", method], timeout=10)
            import json as _json
            try:
                data = _json.loads(out.strip())
                ok = bool(data.get("ok", False))
                clicked = ok
                click_method_out = data.get("click_method", method)
                bounds = data.get("window_bounds", {})
                error = ""
                if not ok:
                    error = str(data.get("error", "")).strip()
                return clicked, click_method_out, error, data.get("client_window_found", False), bounds
            except _json.JSONDecodeError:
                pass
            # Fallback: old "OK <method>" plain text format
            if rc == 0 and out.startswith("OK "):
                actual_method = out.strip().split(" ")[1]
                return True, actual_method, "", False, {}
            err = out.strip()
            if not err:
                err = f"swift exited rc={rc}"
            return False, "", err, False, {}

        def _run_root_start_client() -> dict:
            """
            Try the official HiSec root launcher without prompting for a password.
            Returns a structured result so callers can see why the fallback path
            was used.
            """
            if not Path(ROOT_START_CLIENT).exists():
                return {
                    "attempted": False,
                    "ok": False,
                    "error": "root_start_client.sh not found",
                    "path": ROOT_START_CLIENT,
                }

            rc, out = _run(["/usr/bin/sudo", "-n", ROOT_START_CLIENT], timeout=15)
            return {
                "attempted": True,
                "ok": rc == 0,
                "returncode": rc,
                "error": "" if rc == 0 else out.strip(),
                "path": ROOT_START_CLIENT,
            }

        def _mark_edr_client_connected() -> dict:
            """
            Best-effort record of the EDRClient window as the connected target.
            This keeps later backend state aligned with the app we actually
            activated; HiSecEndpointAgent is only a fallback entry window.
            """
            pid = self._pid_for_app("EDRClient") or getattr(self, "_connected_pid", None)
            found, title, detected_by = _edr_client_window_visible()
            if found:
                self._connected_pid = pid
                self._connected_app = "EDRClient"
                self._connected_window_snapshot = {
                    "pid": pid,
                    "title": title or "华为HiSec Endpoint",
                    "owner": "HiSecEndpoint",
                    "rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
                }
                self._connected_app_instance = _MacOSConnectedApp(self)
                return {"ok": True, "pid": self._connected_pid, "title": self._connected_window_snapshot["title"], "detected_by": detected_by}
            return {"ok": False, "error": "EDRClient window not visible"}

        def _result(
            *,
            ok: bool,
            stage: str,
            already_open: bool,
            main_window_found: bool,
            hisec_process_found: bool,
            cmd_ui_attempted: bool,
            client_window_found: bool,
            root_start_client: dict,
            click_attempts: list,
            successful_click_method: Optional[str],
            click_error: str,
            detected_window_title: str,
            detected_by: Optional[str],
            connected: Optional[dict] = None,
            error: Optional[str] = None,
            activated_by: str = "HiSecEndpointAgent cmd ui",
        ) -> dict:
            out = {
                "ok": ok,
                "stage": stage,
                "backend": "macos_accessibility",
                "target_application": "EDRClient",
                "entry_application": "HiSecEndpointAgent",
                "already_open": already_open,
                "main": {
                    "application": "HiSecEndpointAgent",
                    "role": "fallback_entry_window",
                    "process_found": hisec_process_found,
                    "window_found": main_window_found,
                    "window_title": "华为智能终端安全系统",
                    "activated_by": activated_by,
                    "cmd_ui_attempted": cmd_ui_attempted,
                },
                "client": {
                    "application": "EDRClient",
                    "role": "target_window",
                    "process_found": _proc_exists("EDRClient"),
                    "root_start_client": root_start_client,
                    "clicked": successful_click_method is not None,
                    "click_attempts": click_attempts,
                    "successful_click_method": successful_click_method,
                    "click_error": click_error,
                    "window_found": client_window_found,
                    "window_title": detected_window_title or "华为HiSec Endpoint",
                    "detected_by": detected_by,
                    "connected": connected or {"ok": False},
                },
            }
            if error:
                out["error"] = error
            return out

        # Stage 1: check process existence (separate from window existence)
        hisec_process_found = _proc_exists("HiSecEndpointAgent")

        # Stage 2: check if the target EDRClient window already exists
        main_window_found = _hisec_window_visible()
        # Stage 1: check if EDRClient window is already visible.
        client_window_found, detected_window_title, detected_by = _edr_client_window_visible()

        root_start_client = {
            "attempted": False,
            "ok": False,
            "error": "not needed",
            "path": ROOT_START_CLIENT,
        }
        click_attempts = []       # list of {method, error} per attempt
        click_errors = []          # all errors seen (for structured return)
        successful_click_method = None
        cmd_ui_attempted = False
        activated_by = "HiSecEndpointAgent cmd ui"

        # already_open means the target app window is visible.  CGWindowList
        # can report a stale window (process killed but X window still in the
        # list), so when the initial detection came from CGWindowList we use
        # the helper's strict bounds check (width>=800 && height>=500) to
        # confirm before accepting already_open=True.
        if client_window_found and detected_by == "cgwindowlist":
            _, _, _, helper_found, _ = _click_security_center(method="ax_press")
            if not helper_found:
                client_window_found = False
                detected_window_title = ""
                detected_by = None

        already_open = client_window_found
        if already_open:
            connected = _mark_edr_client_connected()
            return _result(
                ok=True,
                stage="done",
                already_open=True,
                main_window_found=main_window_found,
                hisec_process_found=hisec_process_found,
                cmd_ui_attempted=False,
                client_window_found=True,
                root_start_client=root_start_client,
                click_attempts=click_attempts,
                successful_click_method=None,
                click_error="",
                detected_window_title=detected_window_title,
                detected_by=detected_by,
                connected=connected,
                activated_by=activated_by,
            )

        # Stage 3: primary EDRClient path. This does not require the
        # HiSecEndpointAgent entry window.
        root_start_client = _run_root_start_client()
        if root_start_client.get("ok"):
            deadline = time.time() + min(timeout, 5.0)
            while time.time() < deadline:
                found, title, detection_method = _edr_client_window_visible()
                if found:
                    connected = _mark_edr_client_connected()
                    return _result(
                        ok=True,
                        stage="done",
                        already_open=False,
                        main_window_found=_hisec_window_visible(),
                        hisec_process_found=_proc_exists("HiSecEndpointAgent"),
                        cmd_ui_attempted=False,
                        client_window_found=True,
                        root_start_client=root_start_client,
                        click_attempts=click_attempts,
                        successful_click_method=None,
                        click_error="",
                        detected_window_title=title,
                        detected_by=detection_method,
                        connected=connected,
                        activated_by=activated_by,
                    )
                time.sleep(0.5)
            root_start_client["error"] = (
                "root_start_client.sh returned success, but EDRClient window "
                "did not appear within 5s"
            )

        # Stage 4: fallback: ensure the HiSecEndpointAgent entry window exists.
        if not main_window_found:
            cmd_ui_attempted = True
            if not Path(HISEC_AGENT_BIN).exists():
                return _result(
                    ok=False,
                    stage="fallback_main_window_not_found",
                    already_open=False,
                    main_window_found=False,
                    hisec_process_found=hisec_process_found,
                    cmd_ui_attempted=False,
                    client_window_found=False,
                    root_start_client=root_start_client,
                    click_attempts=click_attempts,
                    successful_click_method=None,
                    click_error="HiSecEndpointAgent binary not found",
                    detected_window_title="",
                    detected_by=None,
                    error="HiSecEndpointAgent binary not found",
                    activated_by=activated_by,
                )

            bundle_launch = _launch_hisec_bundle()
            if bundle_launch.get("ok"):
                activated_by = "open /Applications/HiSecEndpoint.app"
                deadline = time.time() + min(timeout, 3.0)
                while time.time() < deadline:
                    if _hisec_window_visible():
                        main_window_found = True
                        break
                    time.sleep(0.4)
                if not main_window_found:
                    bundle_launch["error"] = (
                        "open returned success, but HiSecEndpointAgent window did not appear within 3s"
                    )

            if not main_window_found:
                activated_by = "HiSecEndpointAgent cmd ui"
                # Main path: HiSecEndpointAgent cmd ui (not activate_app)
                # IMPORTANT: do not redirect stdout/stderr; the GUI can go offscreen.
                _subprocess.Popen(
                    [HISEC_AGENT_BIN, "cmd", "ui"],
                    cwd="/Applications/HiSecEndpoint.app/Contents/MacOS/safra",
                    env=_os.environ.copy(),
                )
                hisec_process_found = True

                # Wait for main window to appear (poll up to timeout)
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if _hisec_window_visible():
                        main_window_found = True
                        break
                    time.sleep(0.5)
                else:
                    # Timeout: main window never appeared
                    found, title, detection_method = _edr_client_window_visible()
                    return _result(
                        ok=False,
                        stage="fallback_main_window_not_found",
                        already_open=False,
                        main_window_found=False,
                        hisec_process_found=hisec_process_found,
                        cmd_ui_attempted=True,
                        client_window_found=found,
                        root_start_client=root_start_client,
                        click_attempts=click_attempts,
                        successful_click_method=None,
                        click_error="main window did not appear within timeout",
                        detected_window_title=title,
                        detected_by=detection_method if found else None,
                        error="HiSecEndpointAgent fallback window did not appear within timeout",
                        activated_by=activated_by,
                    )
        if main_window_found:
            _bring_hisec_to_front()

        # Stage 5: fallback click from HiSecEndpointAgent to open EDRClient.
        if not client_window_found:
            for click_method_label in ["ax_press", "cgevent_center", "auto"]:
                _bring_hisec_to_front()
                clicked, actual_method, click_err, helper_client_found, helper_bounds = _click_security_center(method=click_method_label)
                click_attempts.append({
                    "method": actual_method or click_method_label,
                    "clicked": clicked,
                    "helper_window_found": helper_client_found,
                    "error": click_err if not clicked else "",
                })
                if not clicked:
                    click_errors.append(click_err)

                if helper_client_found:
                    # The helper confirmed the EDRClient window appeared.
                    # Record PID from pgrep — osascript cannot see Qt windows.
                    edr_pid = None
                    ps_rc, ps_out = _run(["pgrep", "-a", "HiSecEndpoint"], timeout=5)
                    if ps_rc == 0:
                        import re
                        m = re.search(r"^\s*(\d+)", ps_out.strip())
                        if m:
                            edr_pid = int(m.group(1))
                    if edr_pid:
                        self._connected_pid = edr_pid
                        self._connected_app = "EDRClient"
                    self._connected_app_instance = _MacOSConnectedApp(self)
                    client_window_found = True
                    successful_click_method = actual_method
                    detected_by = "swift_helper"
                    detected_window_title = "华为HiSec Endpoint"
                    break
                elif clicked:
                    # Click succeeded but window detection missed it — poll briefly
                    deadline = time.time() + 2.0
                    while time.time() < deadline:
                        found, title, detection_method = _edr_client_window_visible()
                        if found:
                            client_window_found = True
                            successful_click_method = actual_method
                            detected_by = detection_method
                            detected_window_title = title
                            break
                        time.sleep(0.3)

                if client_window_found:
                    break  # Success — don't try second click method

        ok = client_window_found
        stage = "done" if ok else "client_window_not_found"

        # Final activation error: prefer click helper errors, otherwise report
        # why the root_start_client path did not produce a visible window.
        final_click_error = click_errors[-1] if click_errors else root_start_client.get("error", "")

        connected = _mark_edr_client_connected() if client_window_found else {"ok": False}

        # ── P0 enhanced diagnostics: snapshot state at failure time ──
        # These fields are only populated when the E2E fails so the caller
        # can see exactly why client_window_not_found was reached.
        diagnostics: dict[str, Any] = {}
        if not ok:
            # 1. EDRClient process state
            edr_proc_rc, edr_proc_out = _run(["pgrep", "-a", "EDRClient"], timeout=5)
            diagnostics["edrclient_process"] = {
                "running": edr_proc_rc == 0,
                "ps_line": edr_proc_out.strip() or None,
            }
            # 2. Full window list at failure time (cross-check what was visible)
            lw = self.list_windows()
            diagnostics["windows_at_failure"] = lw.get("windows", []) if lw.get("ok") else lw
            # 3. CGWindowList EDRClient check (may catch windows osascript misses)
            cg_found, cg_title, _ = _edr_client_window_visible_cg()
            diagnostics["cgwindowlist_edrclient"] = {
                "found": cg_found,
                "title": cg_title or None,
            }

        ret = _result(
            ok=ok,
            stage=stage,
            already_open=False,
            main_window_found=main_window_found,
            hisec_process_found=hisec_process_found,
            cmd_ui_attempted=cmd_ui_attempted,
            client_window_found=client_window_found,
            root_start_client=root_start_client,
            click_attempts=click_attempts,
            successful_click_method=successful_click_method,
            click_error=final_click_error,
            detected_window_title=detected_window_title,
            detected_by=detected_by,
            connected=connected,
            error=None if ok else final_click_error,
            activated_by=activated_by,
        )
        if diagnostics:
            ret["diagnostics"] = diagnostics
        return ret

    def click_at(self, x: int, y: int) -> dict:
        """
        Click at absolute screen coordinates (x, y).

        By default this is a DRY RUN — the click is not actually performed.
        Real coordinate clicks can move the mouse, dismiss dialogs, hit
        the wrong target if the user has been editing the screen, and
        are impossible to undo. The first phase of the macOS backend
        is capability plumbing; the second phase (app-specific
        workflows) can opt in to real clicks by setting the
        EDR_WD_ALLOW_REAL_CLICKS=1 environment variable on the target
        server, OR by passing dry_run=False from a controlled caller.

        Tries `cliclick` first (small CLI; install via
        `brew install cliclick`). Falls back to AppleScript-driven
        click via System Events.

        Note: AppleScript click at {x, y} interprets the coordinates as
        window-relative in some contexts. We use System Events'
        "click at" which is screen-absolute on a 1-pt coordinate
        system; this matches the units used by screencapture on a
        non-Retina display and by PyAutoGUI on macOS in default
        configuration. If the target Mac has a Retina display,
        callers should divide pixel coordinates by 2 before passing
        in (or set their capture/click DPI explicitly).
        """
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        import os as _os
        allow_real = _os.environ.get("EDR_WD_ALLOW_REAL_CLICKS", "0") == "1"
        if not allow_real:
            return {
                "ok": True,
                "method": "dry_run",
                "x": x,
                "y": y,
                "note": (
                    "click_at is in dry-run mode. Real clicks are disabled "
                    "until EDR_WD_ALLOW_REAL_CLICKS=1 is set on the target. "
                    "This is the macOS backend's default for safety — see "
                    "SKILL.md / macos_accessibility docs for the rationale."
                ),
            }
        # Try cliclick
        rc, out = _run(["cliclick", f"c:{x},{y}"], timeout=5)
        if rc == 0:
            return {"ok": True, "method": "cliclick", "x": x, "y": y}

        # Fallback: System Events
        script = (
            'tell application "System Events"\n'
            f'  click at {{{x}, {y}}}\n'
            'end tell\n'
        )
        rc2, out2 = _run_osascript(script, timeout=10)
        if rc2 != 0:
            return {
                "ok": False,
                "stage": "tool_missing",
                "tool": "cliclick" if rc != 0 else None,
                "error": (
                    f"click_at failed: cliclick rc={rc} ({out.strip()}); "
                    f"osascript rc={rc2} ({out2.strip()}). "
                    "Install cliclick (`brew install cliclick`) or grant "
                    "Accessibility permission to the running process."
                ),
            }
        return {"ok": True, "method": "osascript", "x": x, "y": y}

    def double_click_at(self, x: int, y: int) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        if not _allow_real_mouse_actions():
            return {
                "ok": True,
                "method": "dry_run",
                "x": int(x),
                "y": int(y),
                "note": "double_click_at is in dry-run mode; set EDR_WD_ALLOW_REAL_CLICKS=1 to enable real clicks.",
            }
        try:
            pyautogui.doubleClick(int(x), int(y), button="left")
            return {"ok": True, "method": "pyautogui.doubleClick", "x": int(x), "y": int(y)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def right_click_at(self, x: int, y: int) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        if not _allow_real_mouse_actions():
            return {
                "ok": True,
                "method": "dry_run",
                "x": int(x),
                "y": int(y),
                "note": "right_click_at is in dry-run mode; set EDR_WD_ALLOW_REAL_CLICKS=1 to enable real clicks.",
            }
        try:
            pyautogui.rightClick(int(x), int(y))
            return {"ok": True, "method": "pyautogui.rightClick", "x": int(x), "y": int(y)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def middle_click_at(self, x: int, y: int) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        if not _allow_real_mouse_actions():
            return {
                "ok": True,
                "method": "dry_run",
                "x": int(x),
                "y": int(y),
                "note": "middle_click_at is in dry-run mode; set EDR_WD_ALLOW_REAL_CLICKS=1 to enable real clicks.",
            }
        try:
            pyautogui.middleClick(int(x), int(y))
            return {"ok": True, "method": "pyautogui.middleClick", "x": int(x), "y": int(y)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def hover_at(self, x: int, y: int) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        if not _allow_real_mouse_actions():
            return {
                "ok": True,
                "method": "dry_run",
                "x": int(x),
                "y": int(y),
                "note": "hover_at is in dry-run mode; set EDR_WD_ALLOW_REAL_CLICKS=1 to enable real mouse movement.",
            }
        try:
            pyautogui.moveTo(int(x), int(y))
            return {"ok": True, "method": "pyautogui.moveTo", "x": int(x), "y": int(y)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.25) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        if not _allow_real_mouse_actions():
            return {
                "ok": True,
                "method": "dry_run",
                "start": {"x": int(x1), "y": int(y1)},
                "end": {"x": int(x2), "y": int(y2)},
                "duration": float(duration),
                "note": "drag is in dry-run mode; set EDR_WD_ALLOW_REAL_CLICKS=1 to enable real drags.",
            }
        try:
            pyautogui.moveTo(int(x1), int(y1))
            pyautogui.dragTo(int(x2), int(y2), duration=float(duration), button="left")
            return {
                "ok": True,
                "method": "pyautogui.dragTo",
                "start": {"x": int(x1), "y": int(y1)},
                "end": {"x": int(x2), "y": int(y2)},
                "duration": float(duration),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def scroll(self, clicks: int, x: Optional[int] = None, y: Optional[int] = None) -> dict:
        if (lock_error := self._ensure_window_lock()) is not None:
            return lock_error
        if not _allow_real_mouse_actions():
            payload = {
                "ok": True,
                "method": "dry_run",
                "clicks": int(clicks),
                "note": "scroll is in dry-run mode; set EDR_WD_ALLOW_REAL_CLICKS=1 to enable real scrolling.",
            }
            if x is not None and y is not None:
                payload["point"] = {"x": int(x), "y": int(y)}
            return payload
        try:
            if x is not None and y is not None:
                pyautogui.moveTo(int(x), int(y))
            pyautogui.scroll(int(clicks))
            payload = {"ok": True, "method": "pyautogui.scroll", "clicks": int(clicks)}
            if x is not None and y is not None:
                payload["point"] = {"x": int(x), "y": int(y)}
            return payload
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Connect-required ──────────────────────────────────────────────────────

    def connect(
        self,
        title_re: Optional[str] = None,
        process_name: Optional[str] = None,
        pid: Optional[int] = None,
        app_name: Optional[str] = None,
        bundle_id: Optional[str] = None,
        timeout: float = 10.0,
        auto_activate: bool = False,
    ) -> dict:
        """
        Connect to a macOS app. We treat "connect" as: identify the
        target app + bring it to the foreground + record its pid for
        later status() calls.

        Matchers (first one wins):
          pid, bundle_id, process_name, app_name, title_re
        """
        if pid:
            self._connected_pid = pid
            self._connected_app = app_name or process_name
            self._connected_window_snapshot = {
                "pid": pid,
                "title": title_re or app_name or process_name or "",
                "owner": app_name or process_name or "",
                "rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
            }
            self._connected_app_instance = _MacOSConnectedApp(self)
            return {"ok": True, "matched": "pid", "pid": pid}

        if bundle_id:
            r = self.activate_app(bundle_id=bundle_id)
            if not r["ok"]:
                return r
            # Resolve pid via osascript
            self._connected_pid = self._pid_for_bundle(bundle_id)
            self._connected_app = app_name or bundle_id
            self._connected_window_snapshot = {
                "pid": self._connected_pid,
                "title": app_name or bundle_id or "",
                "owner": app_name or bundle_id or "",
                "rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
            }
            self._connected_app_instance = _MacOSConnectedApp(self)
            return {"ok": True, "matched": "bundle_id", "pid": self._connected_pid}

        if process_name:
            r = self.is_window_open(process_name=process_name)
            if not r.get("found"):
                norm_proc = process_name.lower()
                if any(tag in norm_proc for tag in ("edrclient", "hisecendpoint", "hisecendpointagent")):
                    act = self.activate_edr(wait=True, timeout=timeout)
                    if act.get("ok"):
                        connected = getattr(self, "_connected_pid", None)
                        if connected:
                            self._connected_app = "EDRClient"
                            self._connected_app_instance = _MacOSConnectedApp(self)
                            return {"ok": True, "matched": "process_name", "pid": connected}
                    # Fall through to the generic auto-activate path below if activate_edr didn't help.
                if auto_activate:
                    act = self.activate_app(app_name=process_name)
                    if not act["ok"]:
                        return {"ok": False, "error": f"connect: process not visible and activate failed: {act.get('error')}"}
                    # Re-check after activate
                    r = self.is_window_open(process_name=process_name)
                    if not r.get("found"):
                        # Fallback: use _connected_pid if we already connected to this process via activate_edr
                        if hasattr(self, "_connected_pid") and self._connected_pid and                            self._connected_app and process_name.lower() in self._connected_app.lower():
                            return {"ok": True, "matched": "process_name", "pid": self._connected_pid}
                        return {"ok": False, "error": f"connect: no visible window for {process_name} after activate"}
                else:
                    # Fallback: use _connected_pid if we already connected to this process via activate_edr
                    if hasattr(self, "_connected_pid") and self._connected_pid and                        self._connected_app and process_name.lower() in self._connected_app.lower():
                        return {"ok": True, "matched": "process_name", "pid": self._connected_pid}
                    return {"ok": False, "error": f"connect: no visible window for {process_name}"}
            self._connected_pid = r["windows"][0]["pid"]
            self._connected_app = process_name
            self._connected_window_snapshot = {
                "pid": self._connected_pid,
                "title": r["windows"][0].get("window_title", ""),
                "owner": r["windows"][0].get("app_name", process_name),
                "rectangle": r["windows"][0].get("rectangle", {"x": 0, "y": 0, "w": 800, "h": 600}),
            }
            self._connected_app_instance = _MacOSConnectedApp(self)
            return {"ok": True, "matched": "process_name", "pid": self._connected_pid}

        if app_name:
            r = self.activate_app(app_name=app_name)
            if not r["ok"]:
                return r
            self._connected_pid = self._pid_for_app(app_name)
            self._connected_app = app_name
            self._connected_window_snapshot = {
                "pid": self._connected_pid,
                "title": app_name,
                "owner": app_name,
                "rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
            }
            self._connected_app_instance = _MacOSConnectedApp(self)
            return {"ok": True, "matched": "app_name", "pid": self._connected_pid}

        if title_re:
            r = self.is_window_open(title_re=title_re)
            if not r.get("found"):
                return {"ok": False, "error": f"connect: no window matching {title_re!r}"}
            w = r["windows"][0]
            self._connected_pid = w["pid"]
            self._connected_app = w["app_name"]
            self._connected_window_snapshot = {
                "pid": w["pid"],
                "title": w.get("window_title", title_re or ""),
                "owner": w.get("app_name", ""),
                "rectangle": w.get("rectangle", {"x": 0, "y": 0, "w": 800, "h": 600}),
            }
            self._connected_app_instance = _MacOSConnectedApp(self)
            return {"ok": True, "matched": "title_re", "pid": self._connected_pid}

        return {"ok": False, "error": "connect: must specify pid, bundle_id, process_name, app_name, or title_re"}

    def _connected_process_name(self) -> Optional[str]:
        app_name = getattr(self, "_connected_app", None)
        if isinstance(app_name, str) and app_name.strip():
            return app_name.strip()
        snapshot = getattr(self, "_connected_window_snapshot", None)
        if isinstance(snapshot, dict):
            owner = snapshot.get("owner")
            if isinstance(owner, str) and owner.strip():
                return owner.strip()
        return None

    def _escape_applescript_string(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _ax_tree_script(self, process_name: str, max_depth: int) -> str:
        process_name = self._escape_applescript_string(process_name)
        max_depth = max(0, int(max_depth))
        return f'''
on cleanText(v)
    try
        set s to v as text
    on error
        set s to ""
    end try
    set AppleScript's text item delimiters to tab
    set parts to text items of s
    set AppleScript's text item delimiters to " "
    set s to parts as text
    set AppleScript's text item delimiters to linefeed
    set parts to text items of s
    set AppleScript's text item delimiters to " "
    set s to parts as text
    set AppleScript's text item delimiters to ""
    return s
end cleanText

on attrText(e, attrName)
    try
        return cleanText(value of attribute attrName of e)
    on error
        return ""
    end try
end attrText

on boolText(e, propName)
    try
        if propName is "enabled" then
            if enabled of e then return "true"
            return "false"
        end if
    on error
    end try
    return "unknown"
end boolText

on describeElement(e, depth, pathText, controlId, windowName)
    set roleText to ""
    set titleText to ""
    set descText to ""
    set valueText to ""
    try
        set roleText to cleanText(role of e)
    end try
    try
        set titleText to cleanText(name of e)
    end try
    try
        set descText to cleanText(description of e)
    end try
    try
        set valueText to cleanText(value of e)
    end try
    set identifierText to attrText(e, "AXIdentifier")
    set subroleText to attrText(e, "AXSubrole")
    set enabledText to boolText(e, "enabled")
    set xText to ""
    set yText to ""
    set wText to ""
    set hText to ""
    try
        set posValue to position of e
        set xText to (item 1 of posValue) as text
        set yText to (item 2 of posValue) as text
    end try
    try
        set sizeValue to size of e
        set wText to (item 1 of sizeValue) as text
        set hText to (item 2 of sizeValue) as text
    end try
    return (controlId as text) & tab & (depth as text) & tab & cleanText(windowName) & tab & roleText & tab & subroleText & tab & titleText & tab & descText & tab & valueText & tab & identifierText & tab & enabledText & tab & xText & tab & yText & tab & wText & tab & hText & tab & pathText
end describeElement

on walkElement(e, depth, pathText, windowName)
    global outText
    global nextId
    global depthLimit
    set myId to nextId
    set nextId to nextId + 1
    set outText to outText & describeElement(e, depth, pathText, myId, windowName) & linefeed
    if depth >= depthLimit then return
    try
        set kids to UI elements of e
        set childIndex to 1
        repeat with childElement in kids
            my walkElement(childElement, depth + 1, pathText & "." & (childIndex as text), windowName)
            set childIndex to childIndex + 1
        end repeat
    end try
end walkElement

global outText
global nextId
global depthLimit
set outText to ""
set nextId to 1
set depthLimit to {max_depth}

tell application "System Events"
    tell process "{process_name}"
        set frontmost to true
        set windowIndex to 1
        repeat with win in windows
            set winName to ""
            try
                set winName to cleanText(name of win)
            end try
            my walkElement(win, 0, "w" & (windowIndex as text), winName)
            set windowIndex to windowIndex + 1
        end repeat
    end tell
end tell
return outText
'''

    def _parse_ax_tree_lines(self, output: str) -> list[dict]:
        controls: list[dict] = []
        for raw in output.splitlines():
            if not raw.strip():
                continue
            parts = raw.split("\t")
            if len(parts) < 15:
                continue
            def _int_or_none(value: str) -> Optional[int]:
                try:
                    if value == "":
                        return None
                    return int(float(value))
                except ValueError:
                    return None
            control_id = _int_or_none(parts[0])
            depth = _int_or_none(parts[1]) or 0
            x = _int_or_none(parts[10])
            y = _int_or_none(parts[11])
            w = _int_or_none(parts[12])
            h = _int_or_none(parts[13])
            rect = None
            if x is not None and y is not None and w is not None and h is not None:
                rect = {"x": x, "y": y, "w": w, "h": h}
            enabled_value = parts[9].strip().lower()
            controls.append({
                "control_id": control_id,
                "depth": depth,
                "window_title": parts[2],
                "role": parts[3],
                "class_name": parts[3],
                "subrole": parts[4],
                "title": parts[5],
                "text": parts[5] or parts[6] or parts[7],
                "description": parts[6],
                "value": parts[7],
                "automation_id": parts[8],
                "identifier": parts[8],
                "is_enabled": enabled_value != "false",
                "is_visible": rect is not None,
                "rectangle": rect,
                "path": parts[14],
            })
        return controls

    def _load_ax_controls(
        self,
        window_title_re: Optional[str] = None,
        max_depth: int = 10,
    ) -> tuple[Optional[dict], list[dict]]:
        process_name = self._connected_process_name()
        if not process_name:
            return {"ok": False, "error": "Not connected"}, []

        rc, out = _run_osascript(self._ax_tree_script(process_name, max_depth), timeout=20)
        if rc != 0:
            return {
                "ok": False,
                "error": (
                    f"AX tree enumeration failed for {process_name!r} (rc={rc}): {out.strip()}. "
                    "Check Accessibility permission for the MCP server process."
                ),
            }, []

        controls = self._parse_ax_tree_lines(out)
        if window_title_re:
            try:
                rx = re.compile(window_title_re)
                controls = [c for c in controls if rx.search(c.get("window_title") or "")]
            except re.error as e:
                return {"ok": False, "error": f"invalid window_title_re: {e}"}, []

        return None, controls

    def _selector_match_score(
        self,
        control: dict,
        *,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
        parent_text: Optional[str] = None,
        automation_id: Optional[str] = None,
        auto_id_contains: Optional[str] = None,
        auto_id_suffix: Optional[str] = None,
        parent_of: Optional[str] = None,
        control_type: Optional[str] = None,
    ) -> int:
        score = 0
        haystack = " ".join(str(control.get(k) or "") for k in ("title", "text", "description", "value"))
        role = str(control.get("role") or "")
        identifier = str(control.get("automation_id") or "")

        if control_id is not None:
            if control.get("control_id") != control_id:
                return -1
            score += 1000
        if automation_id:
            if identifier != automation_id:
                return -1
            score += 900
        if auto_id_contains:
            if auto_id_contains not in identifier:
                return -1
            score += 650
        if auto_id_suffix:
            if not identifier.endswith(auto_id_suffix):
                return -1
            score += 650
        if class_name:
            if role != class_name and class_name.lower() not in role.lower():
                return -1
            score += 300
        if control_type:
            if control_type.lower() not in role.lower():
                return -1
            score += 300
        if text:
            if text == control.get("title") or text == control.get("text"):
                score += 500
            elif text in haystack:
                score += 250
            else:
                try:
                    if re.search(text, haystack):
                        score += 200
                    else:
                        return -1
                except re.error:
                    return -1
        if parent_text:
            if parent_text not in str(control.get("path") or "") and parent_text not in haystack:
                return -1
            score += 50
        if parent_of:
            if parent_of not in haystack:
                return -1
            score += 50
        if score == 0:
            return -1
        rect = control.get("rectangle")
        if isinstance(rect, dict) and rect.get("w", 0) and rect.get("h", 0):
            score += 25
        if control.get("is_enabled"):
            score += 10
        return score

    def _find_ax_control(self, **selector) -> tuple[Optional[dict], Optional[dict]]:
        max_depth = int(selector.pop("max_depth", 10) or 10)
        err, controls = self._load_ax_controls(max_depth=max_depth)
        if err:
            return err, None
        scored = []
        for control in controls:
            score = self._selector_match_score(control, **selector)
            if score >= 0:
                scored.append((score, control))
        if not scored:
            return {
                "ok": False,
                "error": "No macOS AX control matched selector",
                "selector": {k: v for k, v in selector.items() if v is not None},
                "control_count": len(controls),
            }, None
        scored.sort(key=lambda item: item[0], reverse=True)
        return None, scored[0][1]

    def dump_tree(self, window_title_re: Optional[str] = None, max_depth: int = 10) -> dict:
        err, controls = self._load_ax_controls(window_title_re=window_title_re, max_depth=max_depth)
        if err:
            return err
        return {
            "ok": True,
            "backend": self.backend,
            "process_name": self._connected_process_name(),
            "window_title_re": window_title_re,
            "max_depth": max_depth,
            "controls": controls,
            "count": len(controls),
        }

    def _click_control_center(self, control: dict, *, x_offset: int = 0, y_offset: int = 0) -> dict:
        rect = control.get("rectangle")
        if not isinstance(rect, dict):
            return {"ok": False, "error": "matched control has no rectangle", "control": control}
        w = int(rect.get("w") or 0)
        h = int(rect.get("h") or 0)
        if w <= 0 or h <= 0:
            return {"ok": False, "error": "matched control has an empty rectangle", "control": control}
        x = int(rect.get("x") or 0) + (w // 2) + int(x_offset)
        y = int(rect.get("y") or 0) + (h // 2) + int(y_offset)
        click_result = self.click_at(x, y)
        click_result["matched_control"] = control
        click_result["strategy"] = "ax_center_click"
        return click_result

    def click(
        self,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
        parent_text: Optional[str] = None,
        automation_id: Optional[str] = None,
        auto_id_contains: Optional[str] = None,
        auto_id_suffix: Optional[str] = None,
        parent_of: Optional[str] = None,
        control_type: Optional[str] = None,
        parent_fallback: bool = True,
        timeout: float = 5.0,
    ) -> dict:
        err, control = self._find_ax_control(
            control_id=control_id,
            text=text,
            class_name=class_name,
            parent_text=parent_text,
            automation_id=automation_id,
            auto_id_contains=auto_id_contains,
            auto_id_suffix=auto_id_suffix,
            parent_of=parent_of,
            control_type=control_type,
        )
        if err:
            return err
        return self._click_control_center(control)

    def click_target(
        self,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
        parent_text: Optional[str] = None,
        automation_id: Optional[str] = None,
        auto_id_contains: Optional[str] = None,
        auto_id_suffix: Optional[str] = None,
        parent_of: Optional[str] = None,
        control_type: Optional[str] = None,
        x_offset: int = 0,
        y_offset: int = 0,
        parent_fallback: bool = True,
        timeout: float = 5.0,
    ) -> dict:
        err, control = self._find_ax_control(
            control_id=control_id,
            text=text,
            class_name=class_name,
            parent_text=parent_text,
            automation_id=automation_id,
            auto_id_contains=auto_id_contains,
            auto_id_suffix=auto_id_suffix,
            parent_of=parent_of,
            control_type=control_type,
        )
        if err:
            return err
        return self._click_control_center(control, x_offset=x_offset, y_offset=y_offset)

    def find_control(
        self,
        text: Optional[str] = None,
        role: Optional[str] = None,
        identifier: Optional[str] = None,
        title_re: Optional[str] = None,
        max_depth: int = 10,
    ) -> dict:
        err, controls = self._load_ax_controls(max_depth=max_depth)
        if err:
            return err
        matches = []
        title_rx = None
        if title_re:
            try:
                title_rx = re.compile(title_re)
            except re.error as e:
                return {"ok": False, "error": f"invalid title_re: {e}"}
        for control in controls:
            haystack = " ".join(str(control.get(k) or "") for k in ("title", "text", "description", "value"))
            if text and text not in haystack:
                continue
            if role and role.lower() not in str(control.get("role") or "").lower():
                continue
            if identifier and identifier != control.get("identifier"):
                continue
            if title_rx and not title_rx.search(haystack):
                continue
            matches.append(control)
        return {
            "ok": bool(matches),
            "matches": matches,
            "count": len(matches),
            "error": None if matches else "No macOS AX control matched selector",
        }

    def click_window_at(self, x: int, y: int, window_title_re: Optional[str] = None) -> dict:
        # No window-relative coordinates on macOS without a fuller AX bridge.
        # Forward to click_at as best-effort.
        return self.click_at(x, y)

    def type_text(
        self,
        text: str,
        control_id: Optional[int] = None,
        class_name: Optional[str] = None,
    ) -> dict:
        return {
            "ok": False,
            "error": (
                "type_text is not supported by the macos_accessibility backend "
                "in v1. Use osascript 'keystroke' from the agent, or extend "
                "this backend to drive System Events keyboard input."
            ),
        }

    def select(
        self,
        item: Optional[str] = None,
        index: Optional[int] = None,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
    ) -> dict:
        return {
            "ok": False,
            "error": "select is not supported by the macos_accessibility backend in v1.",
        }

    def get_text(
        self,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
    ) -> dict:
        return {
            "ok": False,
            "error": "get_text is not supported by the macos_accessibility backend in v1.",
        }

    # ── Identity / status helpers ────────────────────────────────────────────

    @property
    def connected_app(self):
        return getattr(self, "_connected_app", None)

    @property
    def connected_app_instance(self):
        return getattr(self, "_connected_app_instance", None)

    @property
    def main_window(self):
        return getattr(self, "_connected_pid", None)

    # ── Internal helpers ────────────────────────────────────────────────────

    def _pid_for_bundle(self, bundle_id: str) -> Optional[int]:
        """Best-effort pid lookup by bundle id via osascript."""
        script = (
            'tell application "System Events"\n'
            f'  set procs to (every process whose bundle identifier is "{bundle_id}")\n'
            '  if (count of procs) is 0 then return ""\n'
            '  return unix id of (first item of procs) as string\n'
            'end tell\n'
        )
        rc, out = _run_osascript(script, timeout=5)
        if rc != 0:
            return None
        out = out.strip()
        try:
            return int(out) if out else None
        except ValueError:
            return None

    def _pid_for_app(self, app_name: str) -> Optional[int]:
        """Best-effort pid lookup by application name via osascript."""
        script = (
            'tell application "System Events"\n'
            f'  set procs to (every process whose name is "{app_name}")\n'
            '  if (count of procs) is 0 then return ""\n'
            '  return unix id of (first item of procs) as string\n'
            'end tell\n'
        )
        rc, out = _run_osascript(script, timeout=5)
        if rc != 0:
            return None
        out = out.strip()
        try:
            return int(out) if out else None
        except ValueError:
            return None


def diagnose_windows() -> dict:
    """
    Probe all windows on the Mac desktop using BOTH CGWindowList and osascript,
    then cross-reference to identify discrepancies.
    Used for debugging window detection mismatches.
    """
    results = {}

    # ── Method 1: CGWindowList (used by list_windows) ─────────────────────────
    import subprocess as _subprocess, json as _json
    cg_script = (
        'use framework "CoreGraphics"\n'
        'use framework "AppKit"\n'
        'set wList to CGWindowListCopyWindowInfo(17, 0)\n'
        'set out to ""\n'
        'repeat with w in wList\n'
        '    set owner to "" & (kCGWindowOwnerName of w as string)\n'
        '    set wName to "" & (kCGWindowName of w as string)\n'
        '    set wPID to kCGWindowOwnerPID of w\n'
        '    set layer to 0\n'
        '    try\n'
        '        set layer to kCGWindowLayer of w\n'
        '    end try\n'
        '    if wName is not "" then\n'
        '        set out to out & owner & "|" & wName & "|" & (wPID as string) & "|" & (layer as string) & "\n"\n'
        '    end if\n'
        'end repeat\n'
        'return out\n'
    )
    rc1, out1 = _run(["osascript", "-e", cg_script], timeout=10)
    cg_windows = []
    if rc1 == 0:
        for line in out1.strip().split("\n"):
            if line.strip():
                parts = line.split("|")
                if len(parts) >= 4:
                    cg_windows.append({
                        "owner": parts[0],
                        "title": parts[1],
                        "pid": int(parts[2]) if parts[2].isdigit() else 0,
                        "layer": int(parts[3]) if parts[3].isdigit() else 0,
                    })
    results["cgwindowlist"] = cg_windows
    results["cgwindowlist_count"] = len(cg_windows)
    results["cgwindowlist_error"] = None if rc1 == 0 else f"rc={rc1}"

    # ── Method 2: osascript System Events (used by _hisec_window_visible) ─────
    osa_script = (
        'tell application "System Events"\n'
        '  set out to ""\n'
        '  repeat with p in (every process)\n'
        '    set pName to name of p\n'
        '    try\n'
        '      repeat with w in (every window of p)\n'
        '        set wName to name of w\n'
        '        set out to out & pName & "|" & wName & "\n"\n'
        '      end repeat\n'
        '    end try\n'
        '  end repeat\n'
        'end tell\n'
        'return out\n'
    )
    rc2, out2 = _run_osascript(osa_script, timeout=10)
    osa_windows = []
    if rc2 == 0:
        for line in out2.strip().split("\n"):
            if "|" in line:
                parts = line.split("|", 1)
                osa_windows.append({"owner": parts[0], "title": parts[1]})
    results["osascript"] = osa_windows
    results["osascript_count"] = len(osa_windows)
    results["osascript_error"] = None if rc2 == 0 else f"rc={rc2}"

    # ── Cross-reference for HiSec-related windows ─────────────────────────────
    hisec_keywords = ["hisecond", "haisec", "华为", "endpoint", "baseline"]
    results["hisec_analysis"] = []
    cg_owners = {w["owner"] for w in cg_windows}
    for w in osa_windows:
        owner = w["owner"].lower()
        title = w["title"].lower()
        matched_kw = [k for k in hisec_keywords if k in owner or k in title]
        if matched_kw:
            in_cg = w["owner"] in cg_owners
            results["hisec_analysis"].append({
                "owner": w["owner"],
                "title": w["title"],
                "matched_keywords": matched_kw,
                "seen_by_cgwindowlist": in_cg,
                "seen_by_osascript": True,
            })
    for w in cg_windows:
        owner = w["owner"].lower()
        title = w["title"].lower()
        matched_kw = [k for k in hisec_keywords if k in owner or k in title]
        if matched_kw and not any(a["owner"] == w["owner"] for a in results["hisec_analysis"]):
            results["hisec_analysis"].append({
                "owner": w["owner"],
                "title": w["title"],
                "pid": w.get("pid"),
                "layer": w.get("layer"),
                "matched_keywords": matched_kw,
                "seen_by_cgwindowlist": True,
                "seen_by_osascript": False,
            })

    return results
