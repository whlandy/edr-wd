"""
macos.py — macOS lifecycle backend.

Uses launchd LaunchAgent (per-user GUI session) for persistent service
definition. Communication with the target is via SSH (delegated to
agent.ssh_runner).

Why LaunchAgent and not LaunchDaemon:
  GUI automation (Accessibility API, screencapture, osascript) requires
  a user session. LaunchDaemon runs in the system context and cannot
  drive the GUI; the MCP server would be useless. We use
  `launchctl bootstrap gui/<uid>/...` and `launchctl kickstart
  gui/<uid>/<label>`.

Scripts uploaded by install() (target/scripts/macos/):
  - install_launch_agent.sh
  - start_server.sh
  - stop_server.sh
  - com.edr-wd.target.plist.template

Start trigger:  `launchctl kickstart -k gui/$(id -u)/<launch_name>`
Stop by port:   `lsof -tiTCP:<port> -sTCP:LISTEN | xargs -r kill -TERM`
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

from agent.ssh_runner import run_ssh, scp_to

AGENT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_SCRIPTS = AGENT_ROOT / "target" / "scripts" / "macos"


def _is_port_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def _remote_scripts_dir(macos_root: str) -> str:
    return f"{macos_root.rstrip('/')}/scripts/macos"


def _local_script(name: str) -> Path | None:
    p = LOCAL_SCRIPTS / name
    return p if p.exists() else None


class MacOSLifecycle:
    """Lifecycle backend for macOS targets (launchd LaunchAgent)."""

    @property
    def platform(self) -> str:
        return "macos"

    # ── ensure_server_running ────────────────────────────────────────────────

    def ensure_server_running(self, cfg: dict) -> dict:
        ssh_cfg = cfg["ssh"]
        mcp_cfg = cfg["mcp"]
        mac_cfg = cfg["macos"]
        target_root = mac_cfg["root"]
        launch_name = mac_cfg["launch_name"]

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

        # Phase 2: stop any process holding the port (avoid EADDRINUSE)
        stop_cmd = (
            f"lsof -tiTCP:{check_port} -sTCP:LISTEN 2>/dev/null "
            f"| xargs -r kill -TERM 2>/dev/null; "
            f"sleep 0.5; "
            f"lsof -tiTCP:{check_port} -sTCP:LISTEN 2>/dev/null "
            f"| xargs -r kill -KILL 2>/dev/null; "
            f"true"
        )
        run_ssh(ssh_cfg, stop_cmd)

        # Phase 3: upload start_server.sh so a manual start is possible
        # (LaunchAgent with KeepAlive=true will also restart it).
        start_script = _local_script("start_server.sh")
        if start_script:
            scp_to(ssh_cfg, str(start_script), _remote_scripts_dir(target_root))

        # Phase 4: kickstart the LaunchAgent.
        # If the agent hasn't been installed yet, kickstart will fail — caller
        # should run install() first.
        kick_cmd = (
            f"UID_VAL=$(id -u); "
            f"launchctl kickstart -k \"gui/${{UID_VAL}}/{launch_name}\" 2>&1"
        )
        rc, out = run_ssh(ssh_cfg, kick_cmd)
        # Non-zero is non-fatal at this stage: the agent might just not be
        # installed yet. We still try to wait for the port.

        # Phase 5: wait for port
        max_wait = 20
        waited = 0
        while waited < max_wait:
            if _is_port_listening(check_host, check_port):
                break
            time.sleep(1)
            waited += 1

        if waited >= max_wait:
            return {
                "ok": False, "stage": "ensure",
                "error": (
                    f"Port {check_port} did not open within {max_wait}s. "
                    f"kickstart output: {(out or '').strip()}. "
                    f"Tip: run install_target_task() first to register the LaunchAgent."
                ),
            }

        return {
            "ok": True, "stage": "ensure",
            "data": {
                "status": "started",
                "port": check_port,
                "waited_seconds": waited,
                "ready_level": "tcp_only",
                "note": "MCP initialize handled by mcp_manager",
                "kickstart_output": (out or "").strip(),
            },
        }

    # ── stop_server ──────────────────────────────────────────────────────────

    def stop_server(self, cfg: dict) -> dict:
        ssh_cfg = cfg["ssh"]
        mcp_cfg = cfg["mcp"]
        mac_cfg = cfg["macos"]
        target_root = mac_cfg["root"]
        launch_name = mac_cfg["launch_name"]
        port = mcp_cfg["port"]

        # Use stop_server.sh if uploaded; otherwise fall back to lsof kill.
        stop_script = _local_script("stop_server.sh")
        if stop_script:
            scp_to(ssh_cfg, str(stop_script), _remote_scripts_dir(target_root))
            remote_stop = f"{_remote_scripts_dir(target_root)}/stop_server.sh"
            rc, out = run_ssh(ssh_cfg, f"bash '{remote_stop}' --port {port}")
        else:
            # kill processes on the port; do NOT also bootout the LaunchAgent
            # (caller may want to restart, not fully remove the service).
            rc, out = run_ssh(
                ssh_cfg,
                f"lsof -tiTCP:{port} -sTCP:LISTEN 2>/dev/null "
                f"| xargs -r kill -TERM; sleep 0.5; "
                f"lsof -tiTCP:{port} -sTCP:LISTEN 2>/dev/null "
                f"| xargs -r kill -KILL; true",
            )

        port_still_open = _is_port_listening("127.0.0.1", port)
        return {
            "ok": True, "stage": "stop",
            "data": {
                "port_killed": not port_still_open,
                "output": (out or "").strip(),
                "launch_name": launch_name,
            },
        }

    # ── install ──────────────────────────────────────────────────────────────

    def install(self, cfg: dict) -> dict:
        """
        Upload LaunchAgent scripts and register the agent with launchd.

        Steps:
          1. Upload start_server.sh, stop_server.sh, install_launch_agent.sh
          2. Run install_launch_agent.sh — which renders the plist, copies it
             to ~/Library/LaunchAgents/, and bootstraps the agent.
        """
        ssh_cfg = cfg["ssh"]
        mac_cfg = cfg["macos"]
        target_root = mac_cfg["root"]
        launch_name = mac_cfg["launch_name"]
        python_path = mac_cfg.get("python_path", "/opt/homebrew/bin/python3")

        remote_scripts = _remote_scripts_dir(target_root)

        uploaded = []
        for script in [
            "start_server.sh",
            "stop_server.sh",
            "install_launch_agent.sh",
            "com.edr-wd.target.plist.template",
        ]:
            local = LOCAL_SCRIPTS / script
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
            f"bash '{remote_scripts}/install_launch_agent.sh' "
            f"--label '{launch_name}' "
            f"--root '{target_root}' "
            f"--python '{python_path}'"
        )
        rc, out = run_ssh(ssh_cfg, install_cmd)
        if rc != 0:
            return {
                "ok": False, "stage": "install",
                "error": f"install_launch_agent.sh failed (rc={rc}): {out}",
            }
        return {
            "ok": True, "stage": "install",
            "data": {
                "uploaded": uploaded,
                "install_output": (out or "").strip(),
                "launch_name": launch_name,
            },
        }


def backend() -> MacOSLifecycle:
    """Module-level factory for the lifecycle registry."""
    return MacOSLifecycle()
