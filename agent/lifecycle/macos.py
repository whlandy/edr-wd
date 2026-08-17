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

from agent.ssh_runner import run_ssh, scp_to, scp_dir_to
from agent.target_config import verify_observed_identity

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

        rc, out = run_ssh(
            ssh_cfg,
            "scutil --get LocalHostName && sw_vers -productVersion",
            timeout=15,
        )
        identity_lines = [line.strip() for line in out.splitlines() if line.strip()]
        if rc != 0 or len(identity_lines) < 2:
            return self._err(
                "probe",
                "target_identity_probe_failed",
                "Could not read macOS hostname and major version",
                details=stages,
            )
        try:
            identity = verify_observed_identity(
                cfg, "macos", identity_lines[0], identity_lines[1]
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

        # Recording requires a target-local indicator plus Quartz event tap,
        # Accessibility, and in-memory CoreGraphics PNG capture.
        dep_cmd = (
            f'"{python_path}" -c '
            f'"import fastmcp, PIL, pyautogui, tkinter, Quartz, AppKit; '
            f'print(\\"macos target deps ok\\")"'
        )
        rc, out = run_ssh(ssh_cfg, dep_cmd, timeout=20)
        stages["python_dependencies"] = {
            "ok": rc == 0,
            "requires": [
                "fastmcp", "Pillow", "PyAutoGUI", "tkinter",
                "pyobjc-framework-Quartz", "pyobjc-framework-Cocoa",
            ],
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

        return self._ok("probe", data=stages)

    # ── deploy ───────────────────────────────────────────────────────────────

    def deploy(self, cfg: dict) -> dict:
        """
        Upload the local target/ directory to the remote macos.root.

        Uses scp_dir_to with tracked_only=True so untracked caches, logs,
        screenshots, and local config never leak into the target payload
        (matches Windows deploy behavior, see design doc step 5).
        """
        ssh_cfg = cfg["ssh"]
        mac_cfg = cfg["macos"]
        macos_root = mac_cfg["root"]

        # Upload tracked local target/ contents to remote macos_root.
        # tracked_only=True excludes untracked files (logs/, *.pyc, .tmp,
        # local config) from the sync, mirroring Windows scp_dir_to.
        rc, msg = scp_dir_to(
            ssh_cfg, str(LOCAL_TARGET), macos_root,
            timeout=60, tracked_only=True,
        )
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

    # ── integrity helpers (Phase 1, read-only) ───────────────────────────────

    def _target_integrity(self, cfg: dict) -> dict:
        """
        Read-only check: are required tracked macOS target payload files
        present on the target?  Does not upload or modify anything.

        Required files (see references/agent-workflow.md lifecycle docs):
          <root>/server.py
          <root>/automation/__init__.py
          <root>/scripts/macos/start_server.sh
          <root>/scripts/macos/stop_server.sh
          <root>/scripts/macos/install_launch_agent.sh
          <root>/scripts/macos/com.edr-wd.target.plist.template

        Returns:
          ok=True:  {"ok": True, "stage": "integrity",
                     "data": {"missing": [], "target_root": root}}
          ok=False: {"ok": False, "stage": "integrity",
                     "code": "target_payload_incomplete",
                     "data": {"missing": [...], "target_root": root},
                     "next_action": "Run deploy_target() then install_target_task()."}
        SSH failure is reported as code="target_integrity_check_failed".
        """
        ssh_cfg = cfg["ssh"]
        mac_cfg = cfg["macos"]
        target_root = mac_cfg["root"]

        required = [
            "server.py",
            "automation/__init__.py",
            "scripts/macos/start_server.sh",
            "scripts/macos/stop_server.sh",
            "scripts/macos/install_launch_agent.sh",
            "scripts/macos/com.edr-wd.target.plist.template",
        ]

        missing: list[str] = []
        ssh_failures: list[dict] = []
        for fname in required:
            remote = _remote_join(target_root, fname)
            # Escape single quotes for shell single-quoted string.
            sh_escaped = remote.replace("'", "'\\''")
            cmd = (
                "if [ -f '" + sh_escaped + "' ]; then "
                "printf 'found'; else printf 'missing'; fi"
            )
            try:
                rc, out = run_ssh(ssh_cfg, cmd, timeout=10)
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
            if (out or "").strip() != "found":
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

    def _launchagent_integrity(self, cfg: dict) -> dict:
        """
        Read-only check: is the macOS LaunchAgent plist present, with the
        expected label, and ProgramArguments containing an argument that
        references <target_root>/.../start_server.sh?

        Does not upload or modify anything.

        ProgramArguments is parsed leniently: we extract the array of
        <string> entries under the ProgramArguments key, and require at
        least one argument to contain both `target_root` and
        `start_server` (basename match).  This tolerates the common
        <string>/bin/bash</string><string>/path/start_server.sh</string>
        shape AND a future `<string>bash</string><string>-c</string>
        <string>cd $ROOT && ./start_server.sh</string>` form.

        Returns:
          ok=True:  plist exists, label matches `macos.launch_name`,
                    ProgramArguments references target_root/.../start_server.sh.
          ok=False: code="launchagent_invalid" with details, or
                    code="target_launchagent_check_failed" for SSH errors.
        """
        ssh_cfg = cfg["ssh"]
        mac_cfg = cfg["macos"]
        target_root = mac_cfg["root"]
        launch_name = mac_cfg["launch_name"]
        plist_path = f"~/Library/LaunchAgents/{launch_name}.plist"

        # Read plist (XML form) — returns non-zero if missing or invalid.
        try:
            rc, out = run_ssh(
                ssh_cfg,
                f"test -f {plist_path} && plutil -convert xml1 -o - {plist_path}",
                timeout=10,
            )
        except Exception as exc:
            return {
                "ok": False,
                "stage": "launchagent",
                "code": "target_launchagent_check_failed",
                "error": f"SSH probe failed: {str(exc)[:200]}",
                "data": {"plist_path": plist_path, "platform": self.platform},
                "next_action": "Verify SSH connectivity, then retry.",
            }
        if rc != 0:
            return {
                "ok": False,
                "stage": "launchagent",
                "code": "launchagent_invalid",
                "error": (
                    f"LaunchAgent plist not found at {plist_path}; "
                    "run install_target_task()."
                ),
                "data": {"plist_path": plist_path, "platform": self.platform},
                "next_action": "Run install_target_task().",
            }

        plist_xml = (out or "").strip()

        # ProgramArguments check — parse the plist as a dict and check
        # that ProgramArguments contains at least one entry that references
        # both `target_root` and the `start_server` basename.  This is
        # robust against the common
        #   <string>/bin/bash</string><string>/path/start_server.sh</string>
        # shape AND against a future
        #   <string>bash</string><string>-c</string>
        #   <string>cd $ROOT && ./start_server.sh</string>
        # form that bundles the script into a single -c argument.
        import plistlib
        try:
            plist = plistlib.loads(plist_xml.encode("utf-8"))
        except Exception as exc:
            return {
                "ok": False,
                "stage": "launchagent",
                "code": "launchagent_invalid",
                "error": f"plist is not parseable: {str(exc)[:200]}",
                "data": {"plist_path": plist_path, "platform": self.platform},
                "next_action": "Run install_target_task().",
            }

        if not isinstance(plist, dict):
            return {
                "ok": False,
                "stage": "launchagent",
                "code": "launchagent_invalid",
                "error": "plist root is not a dict",
                "data": {"plist_path": plist_path, "platform": self.platform},
                "next_action": "Run install_target_task().",
            }

        label_ok = plist.get("Label") == launch_name
        program_args = plist.get("ProgramArguments", []) or []
        if not isinstance(program_args, list):
            program_args = [str(program_args)]

        arg_matches = [
            a for a in program_args
            if isinstance(a, str) and target_root in a and "start_server" in a
        ]

        failures: list[str] = []
        if not label_ok:
            failures.append(
                f"plist Label does not match expected launch_name '{launch_name}'"
            )
        if not arg_matches:
            failures.append(
                f"plist ProgramArguments (got {program_args!r}) does not "
                f"contain any argument referencing both target_root and "
                f"start_server"
            )

        if failures:
            return {
                "ok": False,
                "stage": "launchagent",
                "code": "launchagent_invalid",
                "error": (
                    "LaunchAgent plist is invalid: " + "; ".join(failures)
                ),
                "data": {
                    "plist_path": plist_path,
                    "expected_launch_name": launch_name,
                    "expected_target_root": target_root,
                    "program_arguments": program_args,
                    "platform": self.platform,
                },
                "next_action": "Run install_target_task().",
            }

        return self._ok(
            "launchagent",
            data={
                "label": launch_name,
                "target_root": target_root,
                "matched_program_arg": arg_matches[0],
                "program_arguments": program_args,
                "plist_path": plist_path,
                "platform": self.platform,
            },
        )

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
            # Port is open — verify it is OUR managed server, not a stale orphan.
            listener_info = self._get_listener_info(ssh_cfg, check_host, check_port, target_root)
            if not listener_info["managed"]:
                return {
                    "ok": False,
                    "stage": "ensure",
                    "code": "stale_listener",
                    "error": (
                        f"Port {check_port} is occupied by an unmanaged process "
                        f"(pid={listener_info['listener_pid']}). "
                        f"The process is not the current LaunchAgent-managed server "
                        f"for this target."
                    ),
                    "details": {
                        "port": check_port,
                        "listener_pid": listener_info["listener_pid"],
                        "contains_server_py": listener_info["contains_server_py"],
                        "cwd_matches_target_root": listener_info["listener_cwd_matches"],
                        "next_action": (
                            "stop_server() then ensure_server_running() again, "
                            "or manually stop the stale process."
                        ),
                    },
                }
            gui_check = self._check_gui_readiness(cfg)
            return {
                "ok": True,
                "stage": "ensure",
                "data": {
                    "status": "already_running",
                    "port": check_port,
                    "listener_pid": listener_info["listener_pid"],
                    "cwd_matches_target_root": listener_info["listener_cwd_matches"],
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

        # Phase 2b (refactor): verify tracked target payload is present
        # BEFORE invoking the LaunchAgent.  No ad-hoc scp_to here — the
        # lifecycle backend only inspects state.  If payload is missing
        # the operator must run deploy_target() then install_target_task().
        # SSH errors propagate as target_integrity_check_failed.
        integrity = self._target_integrity(cfg)
        if not integrity.get("ok"):
            return integrity

        launchagent = self._launchagent_integrity(cfg)
        if not launchagent.get("ok"):
            return launchagent

        # Phase 3: kickstart LaunchAgent (launchd pulls the tracked
        # start_server.sh from scripts/macos/ — no per-call scp_to here)
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

    def _get_listener_info(
        self, ssh_cfg: dict, host: str, port: int, target_root: str,
    ) -> dict:
        """
        Inspect the process listening on (host, port) via SSH + lsof + ps.

        Returns a dict with:
          managed              — True only if the listener's cwd equals the
                                 target_root AND its command line contains
                                 server.py.  Using cwd is more reliable than
                                 scanning the command line for the target path,
                                 because a server started with `cd <root>;
                                 python3 server.py` shows no root path in ps.
          listener_pid         — PID of the listening process, or None
          listener_cwd_matches — whether the PID's cwd equals target_root
          contains_server_py  — whether the command line mentions server.py

        No real paths or command-line content are returned to callers; the
        caller receives only structured booleans so that error messages can be
        composed without leaking local paths.
        """
        # Get PID
        pid_cmd = (
            f"lsof -iTCP:{port} -sTCP:LISTEN -n -P 2>/dev/null "
            f"| head -1 | awk '{{print $2}}' 2>/dev/null || echo ''"
        )
        _, pid_out = run_ssh(ssh_cfg, pid_cmd, timeout=10)
        listener_pid = pid_out.strip() or None

        if not listener_pid:
            return {
                "managed": False,
                "listener_pid": None,
                "listener_cwd_matches": False,
                "contains_server_py": False,
            }

        # Get command line
        cmd_cmd = f"ps -p {listener_pid} -o command= 2>/dev/null || echo ''"
        _, cmdline = run_ssh(ssh_cfg, cmd_cmd, timeout=10)
        cmdline = cmdline.strip()
        contains_server_py = "server.py" in cmdline

        # Get cwd via lsof
        cwd_cmd = (
            f"lsof -a -p {listener_pid} -d cwd -Fn 2>/dev/null "
            f"| sed -n 's/^n//p' | head -n1 || echo ''"
        )
        _, cwd_out = run_ssh(ssh_cfg, cwd_cmd, timeout=10)
        cwd_raw = cwd_out.strip().replace("\\", "/")
        norm_target = target_root.replace("\\", "/").rstrip("/")
        norm_cwd = cwd_raw.rstrip("/")
        listener_cwd_matches = (
            norm_cwd == norm_target if cwd_raw else False
        )

        managed = contains_server_py and listener_cwd_matches
        return {
            "managed": managed,
            "listener_pid": listener_pid,
            "listener_cwd_matches": listener_cwd_matches,
            "contains_server_py": contains_server_py,
        }

    def _check_gui_readiness(self, cfg: dict) -> dict:
        """
        Verify GUI readiness for macOS:
          1. MCP backend = macos_accessibility
          2. list_windows > 0

        Uses mcp_manager.health_detail() which correctly handles SSE/MCP
        session initialization — the same path that works for Windows and
        for the real MCP tool calls.  Do NOT use raw urllib/curl here;
        FastMCP StreamableHTTP requires proper session initialization and
        will return 406 otherwise.
        """
        target_name = cfg.get("_target_name", None)
        if not target_name:
            # Fallback if called without target_name in cfg
            return {
                "ready_level": "unknown",
                "server_gui_ready": False,
                "gui_ready": False,
                "backend": None,
                "list_windows_count": 0,
                "error": "target_name not set in cfg",
            }

        try:
            from agent import mcp_manager
            hd = mcp_manager.health_detail(target_name)
            if hd.get("ok"):
                return {
                    "ready_level": hd.get("ready_level", "unknown"),
                    "server_gui_ready": hd.get("server_gui_ready", False),
                    "gui_ready": hd.get("gui_ready", False),
                    "backend": hd.get("backend"),
                    "list_windows_count": hd.get("list_windows_count", 0),
                }
            else:
                return {
                    "ready_level": "unreachable",
                    "server_gui_ready": False,
                    "gui_ready": False,
                    "backend": None,
                    "list_windows_count": 0,
                    "error": hd.get("error", "health_detail failed"),
                }
        except Exception as e:
            return {
                "ready_level": "error",
                "server_gui_ready": False,
                "gui_ready": False,
                "backend": None,
                "list_windows_count": 0,
                "error": str(e),
            }

    # ── stop_server ───────────────────────────────────────────────────────────

    def stop_server(self, cfg: dict) -> dict:
        ssh_cfg = cfg["ssh"]
        mcp_cfg = cfg["mcp"]
        mac_cfg = cfg["macos"]
        target_root = mac_cfg["root"]
        launch_name = mac_cfg["launch_name"]
        port = mcp_cfg["port"]

        # Phase 2 (refactor): verify tracked target payload is present
        # BEFORE attempting to stop the server.  No ad-hoc scp_to here —
        # the lifecycle backend only inspects state.
        integrity = self._target_integrity(cfg)
        if not integrity.get("ok"):
            return integrity

        # Phase 3: invoke the tracked stop_server.sh on the target
        # (it was placed by install_target_task; do NOT re-scp it).
        remote_stop = f"{_remote_scripts_dir(target_root)}/stop_server.sh"
        rc, out = run_ssh(ssh_cfg, f"bash '{remote_stop}' --port {port}", timeout=20)

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
