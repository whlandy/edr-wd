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

Errors are structured:
  - backend_mismatch
  - gui_not_ready
  - deploy_nested_path_error
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Optional

from agent.ssh_runner import run_ssh, scp_to

AGENT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_SCRIPTS = AGENT_ROOT / "target" / "scripts" / "macos"
LOCAL_TARGET = AGENT_ROOT / "target"


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
    """Join path components for a remote SFTP path, Unix-style."""
    base = base.rstrip("/")
    for p in parts:
        segment = str(p).strip("/")
        if segment:
            base = f"{base}/{segment}"
    return base


def _remote_scripts_dir(macos_root: str) -> str:
    return f"{macos_root.rstrip('/')}/scripts/macos"


# ─── Script discovery ───────────────────────────────────────────────────────────

def _local_script(name: str) -> Path | None:
    p = LOCAL_SCRIPTS / name
    return p if p.exists() else None


# ─── MacOSLifecycle ────────────────────────────────────────────────────────────

class MacOSLifecycle:
    """Lifecycle backend for macOS targets (launchd LaunchAgent)."""

    @property
    def platform(self) -> str:
        return "macos"

    # ── probe ────────────────────────────────────────────────────────────────

    def probe(self, cfg: dict) -> dict:
        """
        Probe the macOS target: hostname, Python, Accessibility permissions.
        Returns structured result — never raises.
        """
        ssh_cfg = cfg["ssh"]
        mac_cfg = cfg["macos"]
        python_path = mac_cfg.get("python_path", "python3")

        stages = {}

        # hostname / whoami
        rc, out = run_ssh(ssh_cfg, "hostname && whoami", timeout=15)
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

        return self._ok("probe", data=stages)

    # ── deploy ───────────────────────────────────────────────────────────────

    def deploy(self, cfg: dict) -> dict:
        """
        Upload the local target/ directory to the remote macos.root.
        Returns structured result.
        """
        ssh_cfg = cfg["ssh"]
        mac_cfg = cfg["macos"]
        macos_root = mac_cfg["root"]

        # Upload entire local target/ directory contents to remote macos_root
        rc, msg = scp_to(ssh_cfg, str(LOCAL_TARGET), macos_root, timeout=60)
        if rc != 0:
            return self._err("deploy", "deploy_failed", msg[:300])

        # Verify key files landed at the correct level
        for fname in ["server.py", "automation/__init__.py"]:
            remote_check = _remote_join(macos_root, fname)
            rc_check, _ = run_ssh(
                ssh_cfg,
                f"test -f '{remote_check}' && echo 'found' || echo 'missing'",
                timeout=10,
            )
            if rc_check != 0:
                return self._err(
                    "deploy", "deploy_incomplete",
                    f"Expected file not found: {remote_check}",
                )

        return self._ok("deploy", data={
            "macos_root": macos_root,
            "uploaded": "target/ contents",
        })

    # ── ensure_server_running ─────────────────────────────────────────────────

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

        # Phase 2: stop any process holding the port
        stop_cmd = (
            f"lsof -tiTCP:{check_port} -sTCP:LISTEN 2>/dev/null "
            f"| xargs -r kill -TERM 2>/dev/null; "
            f"sleep 0.5; "
            f"lsof -tiTCP:{check_port} -sTCP:LISTEN 2>/dev/null "
            f"| xargs -r kill -KILL 2>/dev/null; "
            f"true"
        )
        run_ssh(ssh_cfg, stop_cmd, timeout=15)

        # Phase 3: upload start_server.sh
        start_script = _local_script("start_server.sh")
        if start_script:
            scp_to(ssh_cfg, str(start_script), _remote_scripts_dir(target_root))

        # Phase 4: kickstart LaunchAgent
        kick_cmd = (
            f"UID_VAL=$(id -u); "
            f"launchctl kickstart -k \"gui/${{UID_VAL}}/{launch_name}\" 2>&1"
        )
        rc, out = run_ssh(ssh_cfg, kick_cmd, timeout=15)

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
                f"Port {check_port} did not open within {max_wait}s. "
                f"kickstart: {(out or '').strip()[:200]}. "
                f"Tip: run install_target_task() first to register the LaunchAgent.",
            )

        time.sleep(2)
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
        Verify GUI readiness for macOS:
          1. MCP backend = macos_accessibility
          2. list_windows > 0
        """
        ssh_cfg = cfg["ssh"]
        mcp_cfg = cfg["mcp"]
        mac_cfg = cfg["macos"]

        connect_mode = mcp_cfg.get("connect_mode", "direct")
        if connect_mode == "direct":
            check_host = ssh_cfg["host"]
        else:
            check_host = "127.0.0.1"
        check_port = mcp_cfg["port"]

        backend = mac_cfg.get("backend", "macos_accessibility")
        window_count = 0

        try:
            import urllib.request
            import json as _json

            payload = _json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "status",
                    "arguments": {}
                }
            }).encode("utf-8")
            req = urllib.request.Request(
                f"http://{check_host}:{check_port}/mcp",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = _json.load(resp)
                content = result.get("result", {}).get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        try:
                            data = _json.loads(block["text"])
                            if data.get("backend") == backend:
                                # Get window count
                                wc_payload = _json.dumps({
                                    "jsonrpc": "2.0",
                                    "id": 2,
                                    "method": "tools/call",
                                    "params": {
                                        "name": "list_windows",
                                        "arguments": {}
                                    }
                                }).encode("utf-8")
                                wc_req = urllib.request.Request(
                                    f"http://{check_host}:{check_port}/mcp",
                                    data=wc_payload,
                                    headers={"Content-Type": "application/json"},
                                    method="POST",
                                )
                                with urllib.request.urlopen(wc_req, timeout=5) as wc_resp:
                                    wc_result = _json.load(wc_resp)
                                    for wc_block in wc_result.get("result", {}).get("content", []):
                                        if wc_block.get("type") == "text":
                                            try:
                                                wc_data = _json.loads(wc_block["text"])
                                                window_count = len(wc_data.get("windows", []))
                                            except Exception:
                                                pass
                        except Exception:
                            pass
        except Exception:
            pass

        server_gui_ready = window_count > 0
        # server_gui_ready only means the backend can enumerate at least one window.
        # It does NOT mean HiSec/EDR is ready for automation.

        return {
            "ready_level": "gui_ready" if server_gui_ready else "tcp_only",
            "server_gui_ready": server_gui_ready,
            # DEPRECATED alias
            "gui_ready": server_gui_ready,
            "backend": backend,
            "list_windows_count": window_count,
        }

    # ── stop_server ───────────────────────────────────────────────────────────

    def stop_server(self, cfg: dict) -> dict:
        ssh_cfg = cfg["ssh"]
        mcp_cfg = cfg["mcp"]
        mac_cfg = cfg["macos"]
        target_root = mac_cfg["root"]
        launch_name = mac_cfg["launch_name"]
        port = mcp_cfg["port"]

        stop_script = _local_script("stop_server.sh")
        if stop_script:
            scp_to(ssh_cfg, str(stop_script), _remote_scripts_dir(target_root))
            remote_stop = f"{_remote_scripts_dir(target_root)}/stop_server.sh"
            rc, out = run_ssh(ssh_cfg, f"bash '{remote_stop}' --port {port}", timeout=20)
        else:
            rc, out = run_ssh(
                ssh_cfg,
                f"lsof -tiTCP:{port} -sTCP:LISTEN 2>/dev/null "
                f"| xargs -r kill -TERM; sleep 0.5; "
                f"lsof -tiTCP:{port} -sTCP:LISTEN 2>/dev/null "
                f"| xargs -r kill -KILL; true",
                timeout=20,
            )

        port_still_open = _is_port_listening("127.0.0.1", port)
        return self._ok("stop", data={
            "port_killed": not port_still_open,
            "output": (out or "").strip()[:300],
            "launch_name": launch_name,
        })

    # ── install ───────────────────────────────────────────────────────────────

    def install(self, cfg: dict) -> dict:
        """
        Upload LaunchAgent scripts and register the agent with launchd.
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
                return self._err(
                    "install", "local_script_missing",
                    f"Local script not found: {local}",
                )
            rc, err = scp_to(ssh_cfg, str(local), remote_scripts, timeout=30)
            if rc != 0:
                return self._err(
                    "install", "script_upload_failed",
                    f"{script}: {err[:200]}",
                )
            uploaded.append(script)

        install_cmd = (
            f"bash '{remote_scripts}/install_launch_agent.sh' "
            f"--label '{launch_name}' "
            f"--root '{target_root}' "
            f"--python '{python_path}'"
        )
        rc, out = run_ssh(ssh_cfg, install_cmd, timeout=30)
        if rc != 0:
            return self._err(
                "install", "launchagent_registration_failed",
                f"install_launch_agent.sh failed (rc={rc}): {out[:300]}",
            )
        return self._ok("install", data={
            "uploaded": uploaded,
            "install_output": (out or "").strip()[:300],
            "launch_name": launch_name,
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


def backend() -> MacOSLifecycle:
    """Module-level factory for the lifecycle registry."""
    return MacOSLifecycle()
