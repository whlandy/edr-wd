"""Ensure startup validates the registered platform scheduler."""


def test_windows_ensure_returns_task_integrity_error(monkeypatch):
    from agent.lifecycle.windows import WindowsLifecycle

    lifecycle = WindowsLifecycle()
    monkeypatch.setattr(
        "agent.lifecycle.windows._is_port_listening", lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        lifecycle, "_ensure_firewall_rule", lambda _cfg: {"ok": True},
    )
    monkeypatch.setattr(
        lifecycle, "_target_integrity", lambda _cfg: {"ok": True},
    )
    monkeypatch.setattr(
        lifecycle,
        "_task_integrity",
        lambda _cfg: {
            "ok": False,
            "code": "scheduled_task_invalid",
            "stage": "task",
        },
    )
    ssh_calls = []
    monkeypatch.setattr(
        "agent.lifecycle.windows.run_ssh",
        lambda *_a, **_k: ssh_calls.append(_a[1]) or (0, "closed"),
    )

    result = lifecycle.ensure_server_running({
        "ssh": {"host": "127.0.0.1"},
        "mcp": {"port": 8765},
        "windows": {"target_root": "C:\\edr-wd\\target"},
    })

    assert result["code"] == "scheduled_task_invalid"
    assert not any("schtasks /Run" in command for command in ssh_calls)


def test_macos_ensure_returns_launchagent_integrity_error(monkeypatch):
    from agent.lifecycle.macos import MacOSLifecycle

    lifecycle = MacOSLifecycle()
    monkeypatch.setattr(
        "agent.lifecycle.macos._is_port_listening", lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        lifecycle, "_target_integrity", lambda _cfg: {"ok": True},
    )
    monkeypatch.setattr(
        lifecycle,
        "_launchagent_integrity",
        lambda _cfg: {
            "ok": False,
            "code": "launchagent_invalid",
            "stage": "launchagent",
        },
    )
    ssh_calls = []
    monkeypatch.setattr(
        "agent.lifecycle.macos.run_ssh",
        lambda *_a, **_k: ssh_calls.append(_a[1]) or (0, ""),
    )

    result = lifecycle.ensure_server_running({
        "ssh": {"host": "127.0.0.1"},
        "mcp": {"port": 8765, "connect_mode": "tunnel"},
        "macos": {
            "root": "/Users/admin/edr-wd/target",
            "launch_name": "com.edr-wd.target",
        },
    })

    assert result["code"] == "launchagent_invalid"
    assert not any("launchctl kickstart" in command for command in ssh_calls)
