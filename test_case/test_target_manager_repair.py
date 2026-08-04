"""Target manager repair-path contract tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_waits(monkeypatch):
    """Unit contracts mock remote state, so production backoff is unnecessary."""
    monkeypatch.setattr("agent.target_manager.time.sleep", lambda _seconds: None)


def test_kill_ports_uses_powershell_for_windows(monkeypatch):
    from agent import target_manager

    commands = []

    monkeypatch.setattr(
        "agent.ssh_runner.run_ssh",
        lambda _ssh_cfg, command, **_kwargs: commands.append(command) or (0, ""),
    )

    target_manager._kill_ports_on_target(
        {"platform": "windows", "ssh": {"host": "127.0.0.1"}},
        [8765],
    )

    assert commands
    assert "Get-NetTCPConnection" in commands[0]
    assert "8765" in commands[0]


def test_kill_ports_uses_lsof_for_macos(monkeypatch):
    from agent import target_manager

    commands = []

    monkeypatch.setattr(
        "agent.ssh_runner.run_ssh",
        lambda _ssh_cfg, command, **_kwargs: commands.append(command) or (0, ""),
    )

    target_manager._kill_ports_on_target(
        {"platform": "macos", "ssh": {"host": "127.0.0.1"}},
        [8765],
    )

    assert commands
    assert "lsof -tiTCP:8765" in commands[0]
    assert "Get-NetTCPConnection" not in commands[0]


def test_ensure_server_running_repair_true_clears_target_port(monkeypatch):
    from agent import target_manager

    cleared = []

    class FakeTargetConfig:
        def get_default_target(self):
            return "unit-target"

        def get_target(self, _name):
            return {
                "platform": "windows",
                "ssh": {"host": "127.0.0.1"},
                "mcp": {"connect_mode": "direct", "port": 8765, "path": "/mcp"},
            }

        def get_resolved_target(self, name):
            cfg = self.get_target(name)
            cfg["windows"] = {"target_root": "C:\\edr-wd", "task_name": "StartEDRMCP"}
            return cfg

    class FakeLifecycle:
        def ensure_server_running(self, _cfg):
            return {"ok": True, "stage": "ensure", "data": {"status": "started"}}

    monkeypatch.setattr(target_manager, "TargetConfig", FakeTargetConfig)
    monkeypatch.setattr(target_manager, "_is_port_listening", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(target_manager, "_kill_ports_on_target", lambda _cfg, ports: cleared.extend(ports))
    monkeypatch.setattr(target_manager, "_dispatch_lifecycle", lambda _cfg: (FakeLifecycle(), None))
    monkeypatch.setattr(target_manager, "health_detail", lambda _name: {"server_gui_ready": True})

    result = target_manager.ensure_server_running("unit-target", repair=True)

    assert result["ok"] is True
    assert cleared == [8765]


def test_ensure_server_running_repair_false_does_not_clear_ports(monkeypatch):
    from agent import target_manager

    cleared = []

    class FakeTargetConfig:
        def get_default_target(self):
            return "unit-target"

        def get_target(self, _name):
            return {
                "platform": "macos",
                "ssh": {"host": "127.0.0.1"},
                "mcp": {"connect_mode": "direct", "port": 8765, "path": "/mcp"},
            }

        def get_resolved_target(self, name):
            cfg = self.get_target(name)
            cfg["macos"] = {"root": "/tmp/edr-wd", "launch_name": "com.edr-wd.target"}
            return cfg

    class FakeLifecycle:
        def ensure_server_running(self, _cfg):
            return {"ok": True, "stage": "ensure", "data": {"status": "started"}}

    monkeypatch.setattr(target_manager, "TargetConfig", FakeTargetConfig)
    monkeypatch.setattr(target_manager, "_is_port_listening", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(target_manager, "_kill_ports_on_target", lambda _cfg, ports: cleared.extend(ports))
    monkeypatch.setattr(target_manager, "_dispatch_lifecycle", lambda _cfg: (FakeLifecycle(), None))
    monkeypatch.setattr(target_manager, "health_detail", lambda _name: {"server_gui_ready": True})

    result = target_manager.ensure_server_running("unit-target")

    assert result["ok"] is True
    assert cleared == []
