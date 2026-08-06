"""Regression tests for stale Paramiko tunnel recovery."""

from types import SimpleNamespace

from agent import tunnel


def test_ensure_tunnel_replaces_owned_but_unhealthy_process(monkeypatch, tmp_path):
    terminated = []
    spawned = []
    health = iter([False, True])

    monkeypatch.setattr(
        tunnel,
        "_cfg",
        lambda _target: ({"host": "192.0.2.10"}, 18765, 8765),
    )
    monkeypatch.setattr(tunnel, "_port_open", lambda _port: True)
    monkeypatch.setattr(tunnel, "_owned_tunnel_pid", lambda _port: 1234)
    monkeypatch.setattr(tunnel, "_mcp_responding", lambda _port: next(health))
    monkeypatch.setattr(
        tunnel, "_terminate_owned_tunnel", lambda port: terminated.append(port) or 1234
    )
    monkeypatch.setattr(tunnel, "_log_file", lambda _port: tmp_path / "tunnel.log")
    monkeypatch.setattr(
        tunnel.subprocess,
        "Popen",
        lambda *args, **kwargs: spawned.append((args, kwargs)) or SimpleNamespace(pid=5678),
    )

    result = tunnel.ensure_tunnel("win-dev", timeout=0.2)

    assert result["ok"] is True
    assert result["status"] == "started"
    assert terminated == [18765]
    assert len(spawned) == 1


def test_ensure_tunnel_never_replaces_unowned_listener(monkeypatch):
    monkeypatch.setattr(
        tunnel,
        "_cfg",
        lambda _target: ({"host": "192.0.2.10"}, 18765, 8765),
    )
    monkeypatch.setattr(tunnel, "_port_open", lambda _port: True)
    monkeypatch.setattr(tunnel, "_owned_tunnel_pid", lambda _port: None)

    result = tunnel.ensure_tunnel("win-dev")

    assert result["ok"] is False
    assert "non-EDR-WD" in result["error"]
