"""
windows.py — Windows lifecycle backend.

Uses Task Scheduler (schtasks) for persistent service definition and
PowerShell for stop-by-port operations. Communicates with the target
via SSH (delegated to agent.ssh_runner).

Service definition: Windows Task Scheduler task (created by
target/scripts/windows/install_task.ps1 — uploaded by install()).

Start trigger:  `schtasks /Run /TN <task_name> /I`
Stop by port:   `Get-NetTCPConnection -LocalPort <port> -State Listen |
                 Stop-Process -Force`
Status: TCP port probe on the resolved mcp.host:mcp.port.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

from agent.ssh_runner import run_ssh, scp_to

AGENT_ROOT = Path(__file__).resolve().parents[2]  # .../edr-wd
LOCAL_SCRIPTS = AGENT_ROOT / "target" / "scripts"


def _is_port_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def _remote_scripts_path(target_root: str) -> str:
    root = target_root.rstrip("\\/")
    return f"{root}\\scripts"


def _local_script(name: str) -> Path | None:
    p = LOCAL_SCRIPTS / name
    return p if p.exists() else None


class WindowsLifecycle:
    """Lifecycle backend for Windows targets (Task Scheduler + PowerShell)."""

    @property
    def platform(self) -> str:
        return "windows"

    # ── ensure_server_running ────────────────────────────────────────────────

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

        # Phase 1: TCP probe
        if _is_port_listening(check_host, check_port):
            return {
                "ok": True, "stage": "ensure",
                "data": {
                    "status": "already_running",
                    "port": check_port,
                    "ready_level": "tcp_only",
                    "note": "MCP initialize handled by mcp_manager",
                },
            }

        # Phase 2: stop any existing process on the port
        stop_cmd = (
            f"powershell -NoProfile -Command \""
            f"Get-NetTCPConnection -LocalPort {check_port} -State Listen "
            f"-ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue; "
            f"exit 0\""
        )
        run_ssh(ssh_cfg, stop_cmd)

        # Phase 3: upload updated start_server.ps1
        start_script = _local_script("start_server.ps1")
        if start_script:
            scp_to(ssh_cfg, str(start_script), _remote_scripts_path(target_root))

        task_name = win_cfg.get("task_name", "StartEDRMCP")

        # Phase 4: trigger scheduled task
        trigger_cmd = f'schtasks /Run /TN "{task_name}" /I'
        rc, out = run_ssh(ssh_cfg, trigger_cmd)
        if rc != 0:
            return {
                "ok": False, "stage": "ensure",
                "error": f"schtasks /Run failed (rc={rc}): {out}",
            }

        # Phase 5: wait for port
        max_wait = 15
        waited = 0
        while waited < max_wait:
            if _is_port_listening(check_host, check_port):
                break
            time.sleep(1)
            waited += 1

        if waited >= max_wait:
            return {
                "ok": False, "stage": "ensure",
                "error": f"Port {check_port} did not open within {max_wait}s after schtasks /Run",
            }

        return {
            "ok": True, "stage": "ensure",
            "data": {
                "status": "started",
                "port": check_port,
                "waited_seconds": waited,
                "ready_level": "tcp_only",
                "note": "MCP initialize handled by mcp_manager",
            },
        }

    # ── stop_server ──────────────────────────────────────────────────────────

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
            )
        else:
            rc, out = run_ssh(
                ssh_cfg,
                f"powershell -NoProfile -Command \""
                f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue "
                f"| Stop-Process -Force -ErrorAction SilentlyContinue; exit 0\"",
            )

        port_still_open = _is_port_listening("127.0.0.1", port)
        return {
            "ok": True, "stage": "stop",
            "data": {
                "port_killed": not port_still_open,
                "output": (out or "").strip(),
            },
        }

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
                return {
                    "ok": False, "stage": "install",
                    "error": f"Local script not found: {local}",
                }
            rc, err = scp_to(ssh_cfg, str(local), remote_scripts)
            if rc != 0:
                return {
                    "ok": False, "stage": "install",
                    "error": f"SCP upload failed for {script}: {err}",
                }
            uploaded.append(script)

        install_cmd = (
            f"powershell -NoProfile -ExecutionPolicy Bypass -File "
            f"'{remote_scripts}\\install_task.ps1' "
            f"-TaskName '{win_cfg.get('task_name', 'StartEDRMCP')}' "
            f"-TargetRoot '{target_root}'"
        )
        rc, out = run_ssh(ssh_cfg, install_cmd)
        if rc != 0:
            return {
                "ok": False, "stage": "install",
                "error": f"install_task.ps1 failed (rc={rc}): {out}",
            }
        return {
            "ok": True, "stage": "install",
            "data": {"uploaded": uploaded, "install_output": (out or "").strip()},
        }


def backend() -> WindowsLifecycle:
    """Module-level factory for the lifecycle registry."""
    return WindowsLifecycle()
