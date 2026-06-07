"""
windows.py — Windows lifecycle backend.

Uses Task Scheduler (schtasks) for persistent service definition and
PowerShell for stop-by-port operations. Communicates with the target
via SSH (delegated to agent.ssh_runner).

Service definition: Windows Task Scheduler task (created by
target/scripts/install_task.ps1 — uploaded by install()).

Start trigger:  `schtasks /Run /TN <task_name> /I`
Stop by port:   `Get-NetTCPConnection -LocalPort <port> -State Listen |
                 Stop-Process -Force`
Status: TCP port probe on the resolved mcp.host:mcp.port.

Session detection:
  After server start, verifies:
    1. server is not in Session 0
    2. target session is active (not disconnected)
    3. InputDesktop is accessible
    4. list_windows > 0  (GUI automation readiness gate)

Errors are structured:
  - session0_or_desktop_unavailable
  - desktop_session_disconnected
  - deploy_nested_path_error
  - python_not_found / powershell_not_found
  - backend_mismatch
  - gui_not_ready
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Optional

from agent.ssh_runner import run_ssh, scp_to, scp_dir_to, SSHAuthError

AGENT_ROOT = Path(__file__).resolve().parents[2]  # .../edr-wd
LOCAL_SCRIPTS = AGENT_ROOT / "target" / "scripts"
LOCAL_TARGET = AGENT_ROOT / "target"


# ─── Structured error classes ─────────────────────────────────────────────────

class WindowsLifecycleError(Exception):
    """Base exception for Windows lifecycle errors."""
    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"[{code}] {message}")


class Session0Error(WindowsLifecycleError):
    def __init__(self, session_id: int, details: Optional[dict] = None):
        super().__init__(
            "session0_or_desktop_unavailable",
            f"Server is in Session {session_id}, cannot access GUI desktop",
            details,
        )


class DesktopDisconnectedError(WindowsLifecycleError):
    def __init__(self, session_id: int, details: Optional[dict] = None):
        super().__init__(
            "desktop_session_disconnected",
            f"Desktop session {session_id} is disconnected — GUI automation unavailable",
            details,
        )


class DeployNestedPathError(WindowsLifecycleError):
    def __init__(self, nested_path: str):
        super().__init__(
            "deploy_nested_path_error",
            f"Deploy created nested path (target/ inside target/): {nested_path}",
        )


# ─── TCP helper ────────────────────────────────────────────────────────────────

def _is_port_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


# ─── Remote path helpers ───────────────────────────────────────────────────────

def _remote_join(base: str, *parts: str) -> str:
    """Join path components for a remote SFTP path, always using forward slashes."""
    base = base.replace("\\", "/").rstrip("/")
    for p in parts:
        segment = str(p).replace("\\", "/").strip("/")
        if segment:
            base = f"{base}/{segment}"
    return base


def _remote_scripts_path(target_root: str) -> str:
    root = target_root.replace("\\", "/").rstrip("/")
    return f"{root}/scripts"


# ─── Script discovery ───────────────────────────────────────────────────────────

def _local_script(name: str) -> Path | None:
    p = LOCAL_SCRIPTS / name
    return p if p.exists() else None


# ─── WindowsLifecycle ───────────────────────────────────────────────────────────

class WindowsLifecycle:
    """Lifecycle backend for Windows targets (Task Scheduler + PowerShell)."""

    @property
    def platform(self) -> str:
        return "windows"

    # ── probe ────────────────────────────────────────────────────────────────

    def probe(self, cfg: dict) -> dict:
        """
        Probe the Windows target: hostname, Python, PowerShell.
        Returns structured result — never raises.
        """
        ssh_cfg = cfg["ssh"]
        win_cfg = cfg["windows"]
        python_path = win_cfg.get("python_path", "python")

        stages = {}

        # hostname / whoami
        rc, out = run_ssh(ssh_cfg, "hostname & whoami", timeout=15)
        stages["ssh"] = {"ok": rc == 0, "output": out.strip()[:200]}
        if rc != 0:
            return self._err("probe", "ssh_failed", out[:300], details=stages)

        # Python
        rc, out = run_ssh(
            ssh_cfg,
            f'"{python_path}" -c "import sys; print(sys.version)"',
            timeout=15,
        )
        stages["python"] = {"ok": rc == 0, "output": out.strip()[:100]}
        if rc != 0:
            return self._err(
                "probe", "python_not_found",
                f"Python at '{python_path}' failed: {out[:200]}",
                details=stages,
            )

        # PowerShell
        rc, out = run_ssh(
            ssh_cfg,
            'powershell -NoProfile -Command "$PSVersionTable.PSVersion.ToString()"',
            timeout=15,
        )
        ps_version = out.strip()[:20] if rc == 0 else "unavailable"
        stages["powershell"] = {"ok": rc == 0, "version": ps_version}
        if rc != 0:
            return self._err(
                "probe", "powershell_not_found",
                f"PowerShell not available: {out[:200]}",
                details=stages,
            )

        return self._ok("probe", data=stages)

    # ── deploy ───────────────────────────────────────────────────────────────

    def deploy(self, cfg: dict) -> dict:
        """
        Upload the local target/ directory to the remote target_root.
        Returns structured result.

        Checks for nested-path mistake: target/server.py inside target_root/server.py.
        """
        ssh_cfg = cfg["ssh"]
        win_cfg = cfg["windows"]
        target_root = win_cfg["target_root"]

        # Check for nested path mistake before uploading
        nested_check_cmd = (
            f'powershell -NoProfile -Command "'
            f'if (Test-Path \\"{target_root}\\\\target\\\\server.py\\") '
            f'{{ Write-Output \\"nested_found\\" }} else {{ Write-Output \\"ok\\" }}"'
        )
        rc, out = run_ssh(ssh_cfg, nested_check_cmd, timeout=10)
        if rc == 0 and "nested_found" in out:
            return self._err(
                "deploy", "deploy_nested_path_error",
                f"Remote already has nested target/server.py — clean target_root first",
            )

        # Upload tracked local target/ contents to remote target_root
        # (git ls-files enforced inside scp_dir_to — no untracked scripts)
        rc, msg = scp_dir_to(ssh_cfg, str(LOCAL_TARGET), target_root, timeout=60)
        if rc != 0:
            return self._err("deploy", "deploy_failed", msg[:300])

        # Verify key files landed at the correct level (not nested)
        for fname in ["server.py", "automation/__init__.py"]:
            remote_check = _remote_join(target_root, fname)
            rc_check, _ = run_ssh(
                ssh_cfg,
                f'powershell -NoProfile -Command "Test-Path \\"{remote_check}\\""',
                timeout=10,
            )
            if rc_check != 0:
                return self._err(
                    "deploy", "deploy_incomplete",
                    f"Expected file not found at {remote_check}",
                )

        return self._ok("deploy", data={
            "target_root": target_root,
            "uploaded": "target/ contents",
        })

    # ── session detection helpers ─────────────────────────────────────────────

    def _get_server_session(self, cfg: dict) -> tuple[int, str]:
        """Return (session_id, session_state) for the server process by port."""
        ssh_cfg = cfg["ssh"]
        mcp_cfg = cfg["mcp"]
        win_cfg = cfg["windows"]
        port = mcp_cfg["port"]

        cmd = (
            f'powershell -NoProfile -Command "'
            f'$c = Get-NetTCPConnection -LocalPort {port} -State Listen '
            f'-ErrorAction SilentlyContinue | Select-Object -First 1; '
            f'if ($c) {{ '
            f'$p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; '
            f'if ($p) {{ '
            f'$s = (Get-Process -Id $p.Id).SessionId; '
            f'Write-Output \\"$s\\" }} else {{ Write-Output \\"no_process\\" }} '
            f'}} else {{ Write-Output \\"no_connection\\" }}"'
        )
        rc, out = run_ssh(ssh_cfg, cmd, timeout=15)
        out = out.strip()
        if out == "no_connection":
            return -1, "no_connection"
        if out == "no_process":
            return -1, "no_process"
        try:
            sid = int(out)
            return sid, "unknown"
        except ValueError:
            return -1, f"parse_error: {out[:50]}"

    def _get_session_state(self, cfg: dict, session_id: int) -> str:
        """Return RDP session state: active, disconnected, etc."""
        ssh_cfg = cfg["ssh"]
        cmd = (
            f'powershell -NoProfile -Command "'
            f'$s = quser 2>$null | Select-String \\"Sessionid 0*{session_id}\\*\\"; '
            f'if ($s) {{ $s.ToString().Trim() }} else {{ \\"not_found\\" }}"'
        )
        rc, out = run_ssh(ssh_cfg, cmd, timeout=15)
        if rc != 0:
            return f"query_failed: {out[:50]}"
        out = out.strip()
        if "Disc" in out or "disc" in out:
            return "disconnected"
        if "Active" in out or "Run" in out:
            return "active"
        return out[:80] if out else "unknown"

    def _check_input_desktop(self, cfg: dict, session_id: int) -> bool:
        """
        DEPRECATED: OpenInputDesktop is not a PowerShell built-in.
        Desktop accessibility is now inferred from session state.
        This method always returns True; real check is via session_state == "active".
        Kept for backward compat only.
        """
        return True

    # ── ensure_server_running ─────────────────────────────────────────────────

    def ensure_server_running(self, cfg: dict) -> dict:
        ssh_cfg = cfg["ssh"]
        mcp_cfg = cfg["mcp"]
        win_cfg = cfg["windows"]
        target_root = win_cfg["target_root"]

        connect_mode = mcp_cfg.get("connect_mode", "direct")
        if connect_mode == "direct":
            check_host = ssh_cfg["host"]
        else:
            check_host = "127.0.0.1"
        check_port = mcp_cfg["port"]

        # Phase 1: TCP probe — if already running, do a full GUI readiness check
        if _is_port_listening(check_host, check_port):
            gui_check = self._check_gui_readiness(cfg)
            return {
                "ok": True,
                "stage": "ensure",
                "data": {
                    "status": "already_running",
                    "port": check_port,
                    **gui_check,
                },
            }

        # Phase 2: stop any existing process on the port
        stop_cmd = (
            f'powershell -NoProfile -Command "'
            f'Get-NetTCPConnection -LocalPort {check_port} -State Listen '
            f"-ErrorAction SilentlyContinue | Stop-Process -Force "
            f"-ErrorAction SilentlyContinue; exit 0\""
        )
        run_ssh(ssh_cfg, stop_cmd, timeout=15)

        # Phase 3: upload updated start_server.ps1
        start_script = _local_script("start_server.ps1")
        if start_script:
            scp_to(ssh_cfg, str(start_script), _remote_scripts_path(target_root))

        # Phase 4: trigger scheduled task
        task_name = win_cfg.get("task_name", "StartEDRMCP")
        trigger_cmd = f'schtasks /Run /TN "{task_name}" /I'
        rc, out = run_ssh(ssh_cfg, trigger_cmd, timeout=15)
        if rc != 0:
            return self._err(
                "ensure", "server_start_failed",
                f"schtasks /Run failed (rc={rc}): {out[:300]}",
            )

        # Phase 5: wait for port
        max_wait = 20
        waited = 0
        while waited < max_wait:
            if _is_port_listening(check_host, check_port):
                break
            time.sleep(1)
            waited += 1

        if waited >= max_wait:
            return self._err(
                "ensure", "server_start_timeout",
                f"Port {check_port} did not open within {max_wait}s after schtasks /Run",
            )

        # Phase 6: GUI readiness check
        time.sleep(2)  # allow server to fully initialize
        gui_check = self._check_gui_readiness(cfg)

        return {
            "ok": True,
            "stage": "ensure",
            "data": {
                "status": "started",
                "port": check_port,
                "waited_seconds": waited,
                **gui_check,
            },
        }

    def _check_gui_readiness(self, cfg: dict) -> dict:
        """
        Verify GUI automation prerequisites:
          1. Server not in Session 0
          2. Target session is active
          3. InputDesktop accessible
          4. list_windows > 0 via MCP HTTP

        Returns dict to merge into result data.
        """
        ssh_cfg = cfg["ssh"]
        mcp_cfg = cfg["mcp"]

        connect_mode = mcp_cfg.get("connect_mode", "direct")
        if connect_mode == "direct":
            check_host = ssh_cfg["host"]
        else:
            check_host = "127.0.0.1"
        check_port = mcp_cfg["port"]

        # 1. Get server session
        sid, sid_detail = self._get_server_session(cfg)
        session_ok = sid > 0

        # 2. Get session state
        session_state = self._get_session_state(cfg, sid) if session_ok else "session_0_or_invalid"
        session_active = session_state == "active"

        # 3. MCP health via mcp_manager (correct SSE/MCP session handling)
        #    This also calls status + list_windows inside the MCP session.
        mcp_ready = False
        window_count = 0
        backend = None
        target_name = cfg.get("_target_name", "unknown")
        try:
            from agent import mcp_manager
            hd = mcp_manager.health_detail(target_name)
            if hd.get("ok"):
                window_count = hd.get("list_windows_count", 0)
                backend = hd.get("backend")
                mcp_ready = hd.get("server_gui_ready", False)
        except Exception:
            pass

        # input_desktop_ok is inferred from session state (OpenInputDesktop not
        # usable in plain PowerShell; P/Invoke deferred)
        input_desktop_ok = session_active
        gui_ready = session_ok and session_active and window_count > 0

        return {
            "ready_level": "gui_ready" if gui_ready else "tcp_only",
            "server_session_id": sid,
            "session_state": session_state,
            "input_desktop_accessible": input_desktop_ok,
            "list_windows_count": window_count,
            "server_gui_ready": gui_ready,
            "backend": backend,
        }

    # ── stop_server ───────────────────────────────────────────────────────────

    def stop_server(self, cfg: dict) -> dict:
        ssh_cfg = cfg["ssh"]
        mcp_cfg = cfg["mcp"]
        win_cfg = cfg["windows"]
        target_root = win_cfg["target_root"]
        port = mcp_cfg["port"]

        stop_script = _local_script("stop_server.ps1")
        if stop_script:
            remote_stop = f"{_remote_scripts_path(target_root)}\\stop_server.ps1"
            scp_to(ssh_cfg, str(stop_script), _remote_scripts_path(target_root))
            rc, out = run_ssh(
                ssh_cfg,
                f"powershell -NoProfile -ExecutionPolicy Bypass -File '{remote_stop}' -Port {port}",
                timeout=20,
            )
        else:
            rc, out = run_ssh(
                ssh_cfg,
                f'powershell -NoProfile -Command "'
                f'Get-NetTCPConnection -LocalPort {port} '
                f"-ErrorAction SilentlyContinue | Stop-Process -Force "
                f"-ErrorAction SilentlyContinue; exit 0\"",
                timeout=20,
            )

        port_still_open = _is_port_listening(ssh_cfg["host"], port)
        return self._ok("stop", data={
            "port_killed": not port_still_open,
            "output": (out or "").strip()[:300],
        })

    # ── install ──────────────────────────────────────────────────────────────

    def install(self, cfg: dict) -> dict:
        """Upload install scripts and register the Task Scheduler task."""
        ssh_cfg = cfg["ssh"]
        win_cfg = cfg["windows"]
        target_root = win_cfg["target_root"]

        local_scripts = LOCAL_SCRIPTS
        remote_scripts = _remote_scripts_path(target_root)

        uploaded = []
        for script in ["install_task.ps1", "start_server.ps1", "stop_server.ps1"]:
            local = local_scripts / script
            if not local.exists():
                return self._err("install", "local_script_missing", f"Local script not found: {local}")
            rc, err = scp_to(ssh_cfg, str(local), remote_scripts, timeout=30)
            if rc != 0:
                return self._err("install", "script_upload_failed", f"{script}: {err[:200]}")
            uploaded.append(script)

        task_name = win_cfg.get("task_name", "StartEDRMCP")
        install_cmd = (
            f"powershell -NoProfile -ExecutionPolicy Bypass -File "
            f"'{remote_scripts}\\install_task.ps1' "
            f"-TaskName '{task_name}' "
            f"-TargetRoot '{target_root}'"
        )
        rc, out = run_ssh(ssh_cfg, install_cmd, timeout=30)
        if rc != 0:
            return self._err(
                "install", "task_registration_failed",
                f"install_task.ps1 failed (rc={rc}): {out[:300]}",
            )
        return self._ok("install", data={
            "uploaded": uploaded,
            "install_output": (out or "").strip()[:300],
        })

    # ── Result helpers ─────────────────────────────────────────────────────────

    def _ok(self, stage: str, data: Optional[dict] = None) -> dict:
        return {"ok": True, "stage": stage, "data": data or {}}

    def _err(
        self,
        stage: str,
        code: str,
        message: str,
        details: Optional[dict] = None,
    ) -> dict:
        return {
            "ok": False,
            "stage": stage,
            "error": message,
            "code": code,
            "details": details or {},
        }


def backend() -> WindowsLifecycle:
    """Module-level factory for the lifecycle registry."""
    return WindowsLifecycle()
