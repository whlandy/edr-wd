"""Files that must exist for the current target runtime to start safely."""

from __future__ import annotations

import subprocess
from pathlib import Path


CORE_REQUIRED_FILES = (
    "server.py",
    "automation/__init__.py",
    "automation/ocr.py",
    "product_profiles.py",
    "product_policy.py",
    "product_specs/apps.json",
    "time_assertions.py",
    "windowing/__init__.py",
    "windowing/models.py",
    "windowing/registry.py",
    "windowing/runtime.py",
    "windowing/observation.py",
    "windowing/guard.py",
    "windowing/relations.py",
)

WINDOWS_REQUIRED_FILES = CORE_REQUIRED_FILES + (
    "automation/windows_pywinauto.py",
    "pywinauto_client.py",
    "scripts/start_server.ps1",
    "scripts/stop_server.ps1",
    "scripts/install_task.ps1",
)

MACOS_REQUIRED_FILES = CORE_REQUIRED_FILES + (
    "scripts/macos/start_server.sh",
    "scripts/macos/stop_server.sh",
    "scripts/macos/install_launch_agent.sh",
    "scripts/macos/com.edr-wd.target.plist.template",
)


# These are the target-side capabilities needed by the current product-run,
# locked observation, recording and replay paths. An on-disk payload check
# alone cannot prove an already-running process has loaded them.
REQUIRED_RUNTIME_TOOLS = frozenset({
    "status", "list_windows", "observe_window_bundle",
    "observe_product_windows", "begin_product_run", "end_product_run",
    "focus_window_role", "focus_window_selector", "execute_action", "start_recording",
    "recording_status", "add_recording_assertion", "stop_recording",
    "get_recording_capture", "replay_capture",
})


def verify_runtime_tools(target_name: str | None) -> dict:
    """Probe the live MCP process, failing closed on stale/incomplete tools."""
    if not target_name:
        return {"ok": False, "stage": "ensure", "code": "runtime_capability_probe_failed",
                "error": "target name is required for the live MCP capability probe"}
    try:
        from agent import mcp_manager

        initialized = mcp_manager.initialize(target_name)
        if not initialized.get("ok"):
            raise RuntimeError(initialized.get("error", "MCP initialize failed"))
        session = initialized["data"]
        listed = mcp_manager.unwrap_tools_list(
            mcp_manager.get_mcp_tools(session["session_id"], session["mcp_url"])
        )
        if not listed.get("ok"):
            raise RuntimeError(listed.get("error", "MCP tools/list failed"))
        names = {item.get("name") for item in listed["tools"] if isinstance(item, dict)}
    except Exception as exc:  # MCP transport/protocol failures must fail closed
        return {"ok": False, "stage": "ensure", "code": "runtime_capability_probe_failed",
                "error": str(exc)[:300]}

    missing = sorted(REQUIRED_RUNTIME_TOOLS - names)
    if missing:
        return {"ok": False, "stage": "ensure", "code": "runtime_capabilities_missing",
                "error": "running MCP server lacks required tools; restart it after deployment",
                "missing_tools": missing}
    return {"ok": True, "tool_count": len(names)}


def validate_local_payload(local_target: str | Path, required_files: tuple[str, ...]) -> None:
    """Reject a deploy whose required files are absent from its upload set.

    ``scp_dir_to`` uses Git-tracked paths when the checkout has Git metadata,
    and a filtered local-file fallback for distributed packages. Match that
    distinction before any remote upload so stale remote modules cannot make
    an incomplete local payload look successful.
    """
    root = Path(local_target).resolve()
    missing = [name for name in required_files if not (root / name).is_file()]
    if missing:
        raise ValueError("required local target files missing: " + ", ".join(missing))
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--", str(root)], cwd=root.parent,
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("local target manifest timed out") from exc
    except FileNotFoundError:
        return  # packaged distribution: scp_dir_to uses its filtered fallback
    if listed.returncode != 0:
        if "not a git repository" in listed.stderr.lower():
            return
        raise ValueError("local target manifest unavailable")
    tracked = set(listed.stdout.splitlines())
    absent = [name for name in required_files
              if (root.name + "/" + name) not in tracked]
    if absent:
        raise ValueError("required local target files untracked: " + ", ".join(absent))
