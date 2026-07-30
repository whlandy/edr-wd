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
from agent.target_config import verify_observed_identity

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


def _ps_quote(value: str) -> str:
    """
    Escape a value for safe interpolation into a PowerShell single-quoted
    string literal.  PowerShell single-quoted strings escape a single quote
    by doubling it: ' → ''.

    Required because `Test-Path -LiteralPath`, `Get-ScheduledTask`, and
    similar cmdlets receive user-controlled target_root values that may
    legitimately contain a single quote (e.g. `C:\\Users\\O'Brien\\edr-wd`).
    Without this escape, an unescaped quote would terminate the string and
    cause a parse error (or, worse, inject a PowerShell command).
    """
    return value.replace("'", "''")


# Task Scheduler 2.0 LogonType element values that can present user context
# to the desktop.  Read from <LogonType> in the task XML — these are string
# enums, NOT numbers (install_task.ps1 sets `Interactive`).
#
# Rejected values:  None, Group, ServiceAccount, Batch — none of them give
# the scheduled task a desktop session usable by EDR-WD GUI automation.
# Unknown / empty values are also rejected (fail closed).
ACCEPTED_LOGON_TYPES: frozenset[str] = frozenset({
    "Password",
    "S4U",
    "Interactive",
    "InteractiveToken",
})


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

        # Runtime dependencies
        dep_cmd = (
            f'"{python_path}" -c '
            f'"import fastmcp, psutil, PIL, pywinauto, pyautogui; '
            f'print(\\"windows target deps ok\\")"'
        )
        rc, out = run_ssh(ssh_cfg, dep_cmd, timeout=20)
        stages["python_dependencies"] = {
            "ok": rc == 0,
            "requires": ["fastmcp", "psutil", "Pillow", "pywinauto", "PyAutoGUI"],
            "output": out.strip()[:300],
        }
        if rc != 0:
            return self._err(
                "probe",
                "python_dependencies_missing",
                (
                    "Target Python cannot import required EDR-WD runtime "
                    f"dependencies: {out[:300]}"
                ),
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

        rc, out = run_ssh(
            ssh_cfg,
            'powershell -NoProfile -Command "$env:COMPUTERNAME; '
            '(Get-CimInstance Win32_OperatingSystem).Caption"',
            timeout=15,
        )
        identity_lines = [line.strip() for line in out.splitlines() if line.strip()]
        if rc != 0 or len(identity_lines) < 2:
            return self._err(
                "probe",
                "target_identity_probe_failed",
                "Could not read Windows hostname and major version",
                details=stages,
            )
        try:
            identity = verify_observed_identity(
                cfg, "windows", identity_lines[0], identity_lines[1]
            )
        except ValueError as exc:
            return self._err(
                "probe", "target_identity_probe_failed", str(exc), details=stages
            )
        stages["identity"] = identity
        if not identity["ok"]:
            return self._err(
                "probe",
                "target_identity_mismatch",
                (
                    f"Configured target '{identity['configured_name']}' does not match "
                    f"live target '{identity['observed_name']}'"
                ),
                details=stages,
            )

        return self._ok("probe", data=stages)

    def _ensure_firewall_rule(self, cfg: dict) -> dict:
        """Ensure inbound TCP mcp.port is allowed for direct Windows targets."""
        ssh_cfg = cfg["ssh"]
        mcp_cfg = cfg["mcp"]
        port = int(mcp_cfg.get("port", 8765))
        rule_name = f"EDR-WD MCP {port}"
        cmd = (
            'powershell -NoProfile -Command "'
            f'$name = \\"{rule_name}\\"; '
            f'$port = {port}; '
            '$existing = Get-NetFirewallRule -DisplayName $name '
            '-ErrorAction SilentlyContinue; '
            'if (-not $existing) { '
            'New-NetFirewallRule -DisplayName $name '
            '-Direction Inbound -Action Allow -Protocol TCP '
            '-LocalPort $port | Out-Null; '
            'Write-Output \\"created\\" '
            '} else { Write-Output \\"exists\\" }"'
        )
        rc, out = run_ssh(ssh_cfg, cmd, timeout=20)
        return {
            "ok": rc == 0,
            "rule": rule_name,
            "port": port,
            "output": out.strip()[:200],
        }

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

    # ── integrity helpers (Phase 1, read-only) ───────────────────────────────

    def _target_integrity(self, cfg: dict) -> dict:
        """
        Read-only check: are required tracked target payload files present
        on the target?  Does not upload or modify anything.

        Required files (per docs/todo/target-sync-without-ad-hoc-scripts.md):
          <target_root>/server.py
          <target_root>/automation/__init__.py
          <target_root>/scripts/start_server.ps1
          <target_root>/scripts/stop_server.ps1
          <target_root>/scripts/install_task.ps1

        Returns:
          ok=True:  {"ok": True, "stage": "integrity",
                     "data": {"missing": [], "target_root": target_root}}
          ok=False: {"ok": False, "stage": "integrity",
                     "code": "target_payload_incomplete",
                     "error": "...",
                     "data": {"missing": [...], "target_root": target_root},
                     "next_action": "Run deploy_target() then install_target_task()."}
        SSH failure is reported as code="target_integrity_check_failed" with
        the underlying (rc, output) tail in data so callers can distinguish
        "payload missing" from "cannot reach target".
        """
        ssh_cfg = cfg["ssh"]
        win_cfg = cfg["windows"]
        target_root = win_cfg["target_root"]

        required = [
            "server.py",
            "automation/__init__.py",
            "scripts/start_server.ps1",
            "scripts/stop_server.ps1",
            "scripts/install_task.ps1",
        ]

        missing: list[str] = []
        ssh_failures: list[dict] = []
        for fname in required:
            remote = _remote_join(target_root, fname)
            ps_cmd = (
                'powershell -NoProfile -Command "'
                f'if (Test-Path -LiteralPath \'{_ps_quote(remote)}\' '
                '-PathType Leaf) { '
                'Write-Output \'found\' '
                '} else { '
                'Write-Output \'missing\' '
                '}}"'
            )
            try:
                rc, out = run_ssh(ssh_cfg, ps_cmd, timeout=10)
            except Exception as exc:
                ssh_failures.append({"file": fname, "error": str(exc)[:200]})
                continue
            if rc != 0:
                ssh_failures.append({
                    "file": fname,
                    "rc": rc,
                    "output": (out or "")[:200],
                })
                continue
            out_clean = (out or "").strip().lower()
            if out_clean != "found":
                missing.append(fname)

        if ssh_failures:
            return {
                "ok": False,
                "stage": "integrity",
                "code": "target_integrity_check_failed",
                "error": (
                    f"SSH probe failed for {len(ssh_failures)} file(s); "
                    "cannot confirm target payload state."
                ),
                "data": {
                    "target_root": target_root,
                    "platform": self.platform,
                    "ssh_failures": ssh_failures,
                },
                "next_action": (
                    "Verify SSH connectivity (auth, network, host) to "
                    "the target, then retry."
                ),
            }

        if missing:
            return {
                "ok": False,
                "stage": "integrity",
                "code": "target_payload_incomplete",
                "error": (
                    "Target payload is incomplete; run explicit "
                    "deploy_target() then install_target_task()."
                ),
                "data": {
                    "missing": missing,
                    "target_root": target_root,
                    "platform": self.platform,
                },
                "next_action": "Run deploy_target() then install_target_task().",
            }
        return self._ok(
            "integrity",
            data={"missing": [], "target_root": target_root, "platform": self.platform},
        )

    def _task_integrity(self, cfg: dict) -> dict:
        """
        Read-only check: is the Windows scheduled task present, registered
        with a usable logon type, and pointing at our tracked target_root
        start_server.ps1?

        Does not upload or modify anything.

        LogonType values come from the Task Scheduler 2.0 schema as STRING
        enums (not numbers) — install_task.ps1 sets `Interactive`.  We
        accept anything in ACCEPTED_LOGON_TYPES (Password / S4U /
        Interactive / InteractiveToken) and reject None, Group,
        ServiceAccount, Batch, or any unknown / empty value (fail closed).

        Returns:
          ok=True:  task exists, command references <target_root>/scripts/start_server.ps1,
                    LogonType is in ACCEPTED_LOGON_TYPES.
          ok=False: code="scheduled_task_invalid" with details, or
                    code="target_task_check_failed" for SSH errors.
        """
        ssh_cfg = cfg["ssh"]
        win_cfg = cfg["windows"]
        target_root = win_cfg["target_root"]
        task_name = win_cfg.get("task_name", "StartEDRMCP")
        expected_script = f"{target_root}\\scripts\\start_server.ps1".lower()

        ps_cmd = (
            'powershell -NoProfile -Command "'
            f'$t = Get-ScheduledTask -TaskName \'{_ps_quote(task_name)}\' '
            '-ErrorAction SilentlyContinue; '
            'if ($null -eq $t) { '
            'Write-Output \'task_missing\' '
            '} else { '
            '$xml = ([xml]$t.Xml).Task; '
            '$cmd = $xml.Actions.Exec.Command; '
            '$logonType = [string]$xml.Principals.Principal.LogonType; '
            'Write-Output (\'cmd=\' + $cmd) '
            'Write-Output (\'logonType=\' + $logonType) '
            '}}"'
        )
        try:
            rc, out = run_ssh(ssh_cfg, ps_cmd, timeout=15)
        except Exception as exc:
            return {
                "ok": False,
                "stage": "task",
                "code": "target_task_check_failed",
                "error": f"SSH probe failed: {str(exc)[:200]}",
                "data": {"task_name": task_name, "platform": self.platform},
                "next_action": "Verify SSH connectivity, then retry.",
            }
        if rc != 0:
            return {
                "ok": False,
                "stage": "task",
                "code": "target_task_check_failed",
                "error": f"Get-ScheduledTask exited rc={rc}: {(out or '')[:200]}",
                "data": {
                    "task_name": task_name,
                    "rc": rc,
                    "output": (out or "")[:200],
                    "platform": self.platform,
                },
                "next_action": "Verify SSH connectivity, then retry.",
            }
        raw = (out or "").strip()

        if "task_missing" in raw:
            return {
                "ok": False,
                "stage": "task",
                "code": "scheduled_task_invalid",
                "error": f"Scheduled task '{task_name}' is not registered.",
                "data": {"task_name": task_name, "platform": self.platform},
                "next_action": "Run install_target_task().",
            }

        # Parse key=value lines from PowerShell output
        info: dict[str, str] = {}
        for line in raw.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                info[k.strip()] = v.strip()

        cmd = info.get("cmd", "")
        logon_type = info.get("logonType", "")

        failures: list[str] = []
        if expected_script not in cmd.lower():
            failures.append(
                f"Action.Command does not reference '{expected_script}'"
            )
        # LogonType is a string enum; check membership in the allow list.
        # Empty / unknown / None / Group / ServiceAccount / Batch all fail.
        if logon_type not in ACCEPTED_LOGON_TYPES:
            failures.append(
                f"Principal LogonType='{logon_type}' is not in the "
                f"accepted set {sorted(ACCEPTED_LOGON_TYPES)}"
            )

        if failures:
            return {
                "ok": False,
                "stage": "task",
                "code": "scheduled_task_invalid",
                "error": (
                    "Scheduled task does not point at target payload: "
                    + "; ".join(failures)
                ),
                "data": {
                    "task_name": task_name,
                    "expected_command_substring": expected_script,
                    "observed_command": cmd,
                    "logon_type": logon_type,
                    "accepted_logon_types": sorted(ACCEPTED_LOGON_TYPES),
                    "platform": self.platform,
                },
                "next_action": "Run install_target_task().",
            }

        return self._ok(
            "task",
            data={
                "task_name": task_name,
                "command": cmd,
                "logon_type": logon_type,
                "platform": self.platform,
            },
        )

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

        def _target_port_listening() -> bool:
            rc, out = run_ssh(
                ssh_cfg,
                (
                    'powershell -NoProfile -Command "'
                    f'$conn = Get-NetTCPConnection -LocalPort {check_port} '
                    '-State Listen -ErrorAction SilentlyContinue; '
                    'if ($conn) { Write-Output \\"open\\"; exit 0 } '
                    'else { Write-Output \\"closed\\"; exit 1 }"'
                ),
                timeout=10,
            )
            return rc == 0 and "open" in (out or "").lower()

        firewall_result = None
        if connect_mode == "direct":
            firewall_result = self._ensure_firewall_rule(cfg)
            if not firewall_result.get("ok"):
                return self._err(
                    "ensure",
                    "firewall_rule_failed",
                    "Failed to ensure Windows inbound firewall rule for MCP port",
                    details=firewall_result,
                )

        # Phase 1: TCP probe — if already running, do a full GUI readiness check
        if _target_port_listening():
            gui_check = self._check_gui_readiness(cfg)
            return {
                "ok": True,
                "stage": "ensure",
                "data": {
                    "status": "already_running",
                    "port": check_port,
                    "firewall": firewall_result,
                    **gui_check,
                },
            }

        # Phase 2 (refactor): verify tracked target payload is present
        # BEFORE attempting to start the server.  No ad-hoc scp_to here —
        # the lifecycle backend only inspects state.  If payload is
        # missing the operator must run deploy_target() then
        # install_target_task() explicitly.  SSH errors propagate as
        # target_integrity_check_failed.
        integrity = self._target_integrity(cfg)
        if not integrity.get("ok"):
            return integrity

        # Phase 3: stop any existing process on the port
        stop_cmd = (
            f'powershell -NoProfile -Command "'
            f'Get-NetTCPConnection -LocalPort {check_port} -State Listen '
            f'-ErrorAction SilentlyContinue | '
            f'ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}; '
            f'exit 0"'
        )
        run_ssh(ssh_cfg, stop_cmd, timeout=15)

        # Phase 4: trigger scheduled task (TaskScheduler pulls the tracked
        # start_server.ps1 from scripts/ — no per-call scp_to here)
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
            if _target_port_listening():
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
                "firewall": firewall_result,
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

        # Phase 2 (refactor): verify tracked target payload is present
        # BEFORE attempting to stop the server.  No ad-hoc scp_to here —
        # the lifecycle backend only inspects state.
        integrity = self._target_integrity(cfg)
        if not integrity.get("ok"):
            return integrity

        # Phase 3: invoke the tracked stop_server.ps1 on the target
        # (it was placed by install_target_task; do NOT re-scp it).
        remote_stop = f"{_remote_scripts_path(target_root)}\\stop_server.ps1"
        rc, out = run_ssh(
            ssh_cfg,
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{remote_stop}" -Port {port}',
            timeout=20,
        )

        rc_check, out_check = run_ssh(
            ssh_cfg,
            (
                'powershell -NoProfile -Command "'
                f'$conn = Get-NetTCPConnection -LocalPort {port} '
                '-State Listen -ErrorAction SilentlyContinue; '
                'if ($conn) { Write-Output \\"open\\"; exit 1 } '
                'else { Write-Output \\"closed\\"; exit 0 }"'
            ),
            timeout=10,
        )
        port_still_open = rc_check != 0 or "open" in (out_check or "").lower()
        if rc != 0 or port_still_open:
            return self._err(
                "stop",
                "server_stop_failed",
                "MCP server port is still listening after stop attempt",
                details={
                    "port": port,
                    "stop_rc": rc,
                    "stop_output": (out or "").strip()[:300],
                    "check_rc": rc_check,
                    "check_output": (out_check or "").strip()[:300],
                },
            )
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
