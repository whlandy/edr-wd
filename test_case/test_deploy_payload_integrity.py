"""Deploy must not report success for missing current-runtime modules."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from agent.lifecycle.payload import (CORE_REQUIRED_FILES, MACOS_REQUIRED_FILES,
                                     REQUIRED_RUNTIME_TOOLS, WINDOWS_REQUIRED_FILES,
                                     validate_local_payload,
                                     verify_runtime_tools)


pytestmark = pytest.mark.unit


def test_required_target_modules_are_in_tracked_only_payload():
    root = Path(__file__).resolve().parents[1]
    listed = subprocess.run(
        ["git", "ls-files", "--", "target"], cwd=root,
        capture_output=True, text=True, check=True,
    )
    tracked = set(listed.stdout.splitlines())
    assert all((root / "target" / name).is_file() for name in CORE_REQUIRED_FILES)
    assert {"target/" + name for name in CORE_REQUIRED_FILES} <= tracked
    validate_local_payload(root / "target", WINDOWS_REQUIRED_FILES)
    validate_local_payload(root / "target", MACOS_REQUIRED_FILES)


def test_required_runtime_tools_are_registered_by_current_server():
    from target import server

    advertised = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert REQUIRED_RUNTIME_TOOLS <= advertised


def test_local_preflight_rejects_missing_or_untracked_required_file(monkeypatch, tmp_path):
    from agent.lifecycle import payload

    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ValueError, match="required local target files missing: server.py"):
        validate_local_payload(target, ("server.py",))

    (target / "server.py").write_text("# runtime\n")
    monkeypatch.setattr(
        payload.subprocess, "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    with pytest.raises(ValueError, match="required local target files untracked: server.py"):
        validate_local_payload(target, ("server.py",))


@pytest.mark.parametrize("platform", ["windows", "macos"])
def test_deploy_stops_before_remote_io_when_local_payload_is_incomplete(monkeypatch, platform):
    from agent.lifecycle import macos, windows

    module = windows if platform == "windows" else macos
    lifecycle = windows.WindowsLifecycle() if platform == "windows" else macos.MacOSLifecycle()
    monkeypatch.setattr(module, "validate_local_payload",
                        lambda *_args: (_ for _ in ()).throw(ValueError("untracked: time_assertions.py")))
    monkeypatch.setattr(module, "run_ssh", lambda *_args, **_kwargs: pytest.fail("remote probe before preflight"))
    monkeypatch.setattr(module, "scp_dir_to", lambda *_args, **_kwargs: pytest.fail("upload before preflight"))
    cfg = ({"ssh": {}, "windows": {"target_root": "C:\\edr-wd\\target"}}
           if platform == "windows" else
           {"ssh": {}, "macos": {"root": "/tmp/edr-wd-target"}})
    result = lifecycle.deploy(cfg)
    assert result["code"] == "deploy_incomplete"
    assert "time_assertions.py" in result["error"]


def test_windows_deploy_rejects_missing_runtime_module_even_when_ssh_exits_zero(
        monkeypatch):
    from agent.lifecycle import windows

    monkeypatch.setattr(windows, "scp_dir_to", lambda *_args, **_kwargs: (0, "uploaded"))

    def run_ssh(_cfg, command, **_kwargs):
        if "nested_found" in command:
            return 0, "ok"
        return 0, "missing" if "time_assertions.py" in command else "found"

    monkeypatch.setattr(windows, "run_ssh", run_ssh)
    result = windows.WindowsLifecycle().deploy({
        "ssh": {}, "windows": {"target_root": "C:\\edr-wd\\target"},
    })
    assert result["code"] == "deploy_incomplete"
    assert "time_assertions.py" in result["error"]
    assert "time_assertions.py" in WINDOWS_REQUIRED_FILES


def test_macos_deploy_rejects_missing_runtime_module_even_when_ssh_exits_zero(
        monkeypatch):
    from agent.lifecycle import macos

    monkeypatch.setattr(macos, "scp_dir_to", lambda *_args, **_kwargs: (0, "uploaded"))
    monkeypatch.setattr(
        macos, "run_ssh",
        lambda _cfg, command, **_kwargs: (
            0, "missing" if "product_specs/apps.json" in command else "found"),
    )
    result = macos.MacOSLifecycle().deploy({
        "ssh": {}, "macos": {"root": "/tmp/edr-wd-target"},
    })
    assert result["code"] == "deploy_incomplete"
    assert "product_specs/apps.json" in result["error"]
    assert "product_specs/apps.json" in MACOS_REQUIRED_FILES


def test_tracked_only_upload_rejects_empty_manifest(monkeypatch, tmp_path):
    from agent import ssh_runner

    source = tmp_path / "target"
    source.mkdir()
    monkeypatch.setattr(
        ssh_runner.subprocess, "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b""),
    )
    monkeypatch.setattr(
        ssh_runner, "_paramiko_scp_to",
        lambda *_args, **_kwargs: pytest.fail("empty deployment uploaded a file"),
    )
    code, message = ssh_runner.scp_dir_to({}, source, "/tmp/target")
    assert code != 0
    assert "empty deployment" in message


def test_windows_running_server_does_not_bypass_target_payload_integrity(monkeypatch):
    from agent.lifecycle import windows

    expected = {"ok": False, "stage": "integrity",
                "code": "target_payload_incomplete", "error": "missing current module"}
    lifecycle = windows.WindowsLifecycle()
    monkeypatch.setattr(windows, "run_ssh", lambda *_args, **_kwargs: (0, "open"))
    monkeypatch.setattr(lifecycle, "_target_integrity", lambda _cfg: expected)
    monkeypatch.setattr(lifecycle, "_check_gui_readiness",
                        lambda _cfg: pytest.fail("readiness cannot bypass payload integrity"))

    result = lifecycle.ensure_server_running({
        "ssh": {"host": "127.0.0.1"},
        "mcp": {"port": 8765, "connect_mode": "ssh_tunnel"},
        "windows": {"target_root": "C:\\edr-wd\\target"},
    })
    assert result == expected


def test_macos_running_server_does_not_bypass_target_payload_integrity(monkeypatch):
    from agent.lifecycle import macos

    expected = {"ok": False, "stage": "integrity",
                "code": "target_payload_incomplete", "error": "missing current module"}
    lifecycle = macos.MacOSLifecycle()
    monkeypatch.setattr(macos, "_is_port_listening", lambda *_args: True)
    monkeypatch.setattr(lifecycle, "_get_listener_info", lambda *_args: {
        "managed": True, "listener_pid": "123", "listener_cwd_matches": True,
    })
    monkeypatch.setattr(lifecycle, "_target_integrity", lambda _cfg: expected)
    monkeypatch.setattr(lifecycle, "_check_gui_readiness",
                        lambda _cfg: pytest.fail("readiness cannot bypass payload integrity"))

    result = lifecycle.ensure_server_running({
        "ssh": {"host": "127.0.0.1"},
        "mcp": {"port": 8765, "connect_mode": "ssh_tunnel"},
        "macos": {"root": "/tmp/edr-wd-target", "launch_name": "com.edr-wd.target"},
    })
    assert result == expected


def test_runtime_tool_probe_rejects_stale_server(monkeypatch):
    from agent import mcp_manager

    monkeypatch.setattr(mcp_manager, "initialize", lambda _name: {
        "ok": True, "data": {"session_id": "session", "mcp_url": "http://target/mcp"},
    })
    monkeypatch.setattr(mcp_manager, "get_mcp_tools", lambda *_args: {
        "ok": True, "data": {"result": {"tools": [
            {"name": "status"}, {"name": "list_windows"},
        ]}},
    })

    result = verify_runtime_tools("test-target")
    assert not result["ok"]
    assert result["code"] == "runtime_capabilities_missing"
    assert "observe_window_bundle" in result["missing_tools"]
    assert "begin_product_run" in result["missing_tools"]


def test_runtime_tool_probe_accepts_current_tool_set(monkeypatch):
    from agent import mcp_manager

    monkeypatch.setattr(mcp_manager, "initialize", lambda _name: {
        "ok": True, "data": {"session_id": "session", "mcp_url": "http://target/mcp"},
    })
    monkeypatch.setattr(mcp_manager, "get_mcp_tools", lambda *_args: {
        "ok": True, "data": {"result": {"tools": [
            {"name": name} for name in sorted(REQUIRED_RUNTIME_TOOLS)
        ]}},
    })
    assert verify_runtime_tools("test-target") == {
        "ok": True, "tool_count": len(REQUIRED_RUNTIME_TOOLS),
    }


def test_runtime_tool_probe_fails_closed_on_transport_error(monkeypatch):
    from agent import mcp_manager

    monkeypatch.setattr(mcp_manager, "initialize",
                        lambda _name: (_ for _ in ()).throw(TimeoutError("MCP timeout")))
    result = verify_runtime_tools("test-target")
    assert result["code"] == "runtime_capability_probe_failed"
    assert not result["ok"]


@pytest.mark.parametrize("platform", ["windows", "macos"])
def test_running_server_rejects_missing_runtime_tools(monkeypatch, platform):
    from agent.lifecycle import macos, windows

    expected = {"ok": False, "code": "runtime_capabilities_missing",
                "missing_tools": ["observe_window_bundle"]}
    if platform == "windows":
        lifecycle = windows.WindowsLifecycle()
        monkeypatch.setattr(windows, "run_ssh", lambda *_args, **_kwargs: (0, "open"))
        monkeypatch.setattr(windows, "verify_runtime_tools", lambda _name: expected)
        cfg = {"ssh": {"host": "127.0.0.1"},
               "mcp": {"port": 8765, "connect_mode": "ssh_tunnel"},
               "windows": {"target_root": "C:\\edr-wd\\target"},
               "_target_name": "test-target"}
    else:
        lifecycle = macos.MacOSLifecycle()
        monkeypatch.setattr(macos, "_is_port_listening", lambda *_args: True)
        monkeypatch.setattr(lifecycle, "_get_listener_info", lambda *_args: {
            "managed": True, "listener_pid": "123", "listener_cwd_matches": True,
        })
        monkeypatch.setattr(macos, "verify_runtime_tools", lambda _name: expected)
        cfg = {"ssh": {"host": "127.0.0.1"},
               "mcp": {"port": 8765, "connect_mode": "ssh_tunnel"},
               "macos": {"root": "/tmp/edr-wd-target", "launch_name": "com.edr-wd.target"},
               "_target_name": "test-target"}
    monkeypatch.setattr(lifecycle, "_target_integrity", lambda _cfg: {"ok": True})
    monkeypatch.setattr(lifecycle, "_check_gui_readiness",
                        lambda _cfg: pytest.fail("stale runtime cannot be ready"))

    result = lifecycle.ensure_server_running(cfg)
    assert result["code"] == "runtime_capabilities_missing"
    assert not result["ok"]
