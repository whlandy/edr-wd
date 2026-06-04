"""
macos_accessibility.py — macOS automation backend (v1 minimal).

Drives the macOS GUI via shell tools that ship with macOS:

  - screencapture  — full screen capture
  - osascript      — AppleScript bridge (System Events for app/window enumeration)
  - system_profiler — JSON metadata for app bundle ids
  - cliclick       — optional, for click_at() if available
  - python ctypes  — Quartz.CGEvent for click_at() when cliclick is not present

This is a v1 backend: it intentionally does NOT expose dump_tree(),
control_id-based click(), or other Windows-specific primitives. Those
return {"ok": False, "error": "not supported on this backend"}.

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


# ── Backend ───────────────────────────────────────────────────────────────────

class MacOSAccessibilityBackend:
    """AutomationBackend implementation for macOS targets."""

    @property
    def backend(self) -> str:
        return "macos_accessibility"

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
            return {"ok": True, "matched": "pid", "pid": pid}

        if bundle_id:
            r = self.activate_app(bundle_id=bundle_id)
            if not r["ok"]:
                return r
            # Resolve pid via osascript
            self._connected_pid = self._pid_for_bundle(bundle_id)
            self._connected_app = app_name or bundle_id
            return {"ok": True, "matched": "bundle_id", "pid": self._connected_pid}

        if process_name:
            r = self.is_window_open(process_name=process_name)
            if not r.get("found"):
                if auto_activate:
                    act = self.activate_app(app_name=process_name)
                    if not act["ok"]:
                        return {"ok": False, "error": f"connect: process not visible and activate failed: {act.get('error')}"}
                    # Re-check after activate
                    r = self.is_window_open(process_name=process_name)
                    if not r.get("found"):
                        return {"ok": False, "error": f"connect: no visible window for {process_name} after activate"}
                else:
                    return {"ok": False, "error": f"connect: no visible window for {process_name}"}
            self._connected_pid = r["windows"][0]["pid"]
            self._connected_app = process_name
            return {"ok": True, "matched": "process_name", "pid": self._connected_pid}

        if app_name:
            r = self.activate_app(app_name=app_name)
            if not r["ok"]:
                return r
            self._connected_pid = self._pid_for_app(app_name)
            self._connected_app = app_name
            return {"ok": True, "matched": "app_name", "pid": self._connected_pid}

        if title_re:
            r = self.is_window_open(title_re=title_re)
            if not r.get("found"):
                return {"ok": False, "error": f"connect: no window matching {title_re!r}"}
            w = r["windows"][0]
            self._connected_pid = w["pid"]
            self._connected_app = w["app_name"]
            return {"ok": True, "matched": "title_re", "pid": self._connected_pid}

        return {"ok": False, "error": "connect: must specify pid, bundle_id, process_name, app_name, or title_re"}

    def dump_tree(self, window_title_re: Optional[str] = None, max_depth: int = 10) -> dict:
        return {
            "ok": False,
            "error": (
                "dump_tree is not supported by the macos_accessibility backend. "
                "Use list_windows/is_window_open to enumerate visible windows, "
                "or click_at(x, y) for coordinate-based interaction."
            ),
        }

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
        return {
            "ok": False,
            "error": (
                "control_id/automation_id-based click is not supported by the "
                "macos_accessibility backend. Use click_at(x, y) for "
                "coordinate-based interaction, or extend the backend to "
                "expose AX tree lookup."
            ),
        }

    def click_target(
        self,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
        parent_text: Optional[str] = None,
        automation_id: Optional[str] = None,
        timeout: float = 5.0,
    ) -> dict:
        return {
            "ok": False,
            "error": "click_target is not supported by the macos_accessibility backend.",
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
