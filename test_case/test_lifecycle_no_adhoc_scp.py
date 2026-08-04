"""Phase 2: remove ad-hoc scp_to from lifecycle normal-path ensure/stop.

When a tracked payload is missing on the target, ensure_server_running
and stop_server MUST refuse to silently upload the script.  Instead they
call _target_integrity first and propagate the structured
target_payload_incomplete error.

These tests verify:
  - ensure_server_running refuses with target_payload_incomplete when
    any of the 5 Windows / 6 macOS payload files is missing on the target
  - stop_server refuses with target_payload_incomplete the same way
  - In neither path is scp_to invoked (zero-write contract)

SSH probes are simulated via monkeypatch; scp_to is replaced with a
recording stub that fails the test if called.
"""

from __future__ import annotations


# Track every scp_to call site on the lifecycle module under test.
# If the lifecycle backend uploads anything during ensure / stop when the
# target payload is missing, the test fails — that is the no-write
# contract Phase 2 establishes.


# ── Windows ensure_server_running ────────────────────────────────────────────


def test_windows_ensure_refuses_when_payload_incomplete(monkeypatch):
    """Missing target payload → ensure_server_running returns
    target_payload_incomplete WITHOUT scp_to or schtasks /Run."""
    from agent.lifecycle.windows import WindowsLifecycle

    calls: list[str] = []
    scp_calls: list[tuple] = []

    def fake_run_ssh(_ssh_cfg, command, **_kwargs):
        calls.append(command)
        # 1) firewall rule probe — return "exists" so we pass that gate
        if "Get-NetFirewallRule" in command:
            return (0, "exists")
        # 2) port listening probe — return "closed" so we go to integrity
        if "Get-NetTCPConnection -LocalPort" in command and "Listen" in command:
            # First port probe is the one in ensure_server_running; we want
            # it to return "closed" so the integrity branch executes.
            return (0, "closed")
        # 3) _target_integrity's 5 file probes — all "missing"
        if "Test-Path" in command:
            return (0, "missing")
        # Anything else (schtasks, etc.) — fail the test by returning an
        # error so we see the call happened.
        raise AssertionError(
            f"unexpected run_ssh call after integrity failure: {command[:200]}"
        )

    def fake_scp_to(*args, **kwargs):
        scp_calls.append((args, kwargs))
        return (0, "")

    def fake_scp_dir_to(*args, **kwargs):
        scp_calls.append((args, kwargs))
        return (0, "")

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)
    monkeypatch.setattr("agent.lifecycle.windows.scp_to", fake_scp_to)
    monkeypatch.setattr("agent.lifecycle.windows.scp_dir_to", fake_scp_dir_to)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "mcp": {"port": 8765},
        "windows": {"target_root": "C:\\edr-wd\\target"},
    }

    result = lifecycle.ensure_server_running(cfg)

    assert result["ok"] is False
    assert result["code"] == "target_payload_incomplete"
    # All 5 files should be reported as missing
    assert len(result["data"]["missing"]) == 5
    # CRITICAL: zero scp_to / scp_dir_to calls
    assert scp_calls == [], (
        f"Phase 2 no-write contract violated: scp_to was called. "
        f"Calls: {scp_calls}"
    )
    # CRITICAL: schtasks /Run was never invoked
    assert not any("schtasks" in c for c in calls), (
        f"schtasks was invoked after integrity failure: {calls}"
    )


def test_windows_ensure_proceeds_when_payload_complete(monkeypatch):
    """All 5 payload files present → ensure_server_running proceeds
    through port-kill and schtasks /Run; still no scp_to."""
    from agent.lifecycle.windows import WindowsLifecycle

    calls: list[str] = []
    scp_calls: list[tuple] = []
    task_started = False

    def fake_run_ssh(_ssh_cfg, command, **_kwargs):
        nonlocal task_started
        calls.append(command)
        if "Get-NetFirewallRule" in command:
            return (0, "exists")
        if "Get-NetTCPConnection -LocalPort" in command and "exit 0" in command:
            return (0, "closed")
        if "Get-NetTCPConnection -LocalPort" in command and "exit 1" in command:
            return (0, "open") if task_started else (1, "closed")
        if "Test-Path" in command:
            return (0, "found")
        if "Get-ScheduledTask" in command:
            return (
                0,
                "cmd=C:\\edr-wd\\target\\scripts\\start_server.ps1\n"
                "logonType=Interactive",
            )
        if "schtasks" in command:
            task_started = True
            return (0, "SUCCESS")
        if "Get-CimInstance" in command or "SessionId" in command:
            return (1, "no_connection")
        if "quser" in command:
            return (0, "not_found")
        # Default: success empty
        return (0, "")

    def fake_scp_to(*args, **kwargs):
        scp_calls.append((args, kwargs))
        return (0, "")

    def fake_scp_dir_to(*args, **kwargs):
        scp_calls.append((args, kwargs))
        return (0, "")

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)
    monkeypatch.setattr("agent.lifecycle.windows.scp_to", fake_scp_to)
    monkeypatch.setattr("agent.lifecycle.windows.scp_dir_to", fake_scp_dir_to)
    monkeypatch.setattr("agent.lifecycle.windows.time.sleep", lambda _seconds: None)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "mcp": {"port": 8765},
        "windows": {"target_root": "C:\\edr-wd\\target"},
    }

    result = lifecycle.ensure_server_running(cfg)

    # Ensure schtasks was called
    assert any("schtasks" in c for c in calls), (
        f"Expected schtasks /Run, got calls: {calls}"
    )
    # CRITICAL: still no scp_to / scp_dir_to in normal path
    assert scp_calls == [], (
        f"Phase 2 no-write contract violated: {scp_calls}"
    )


# ── Windows stop_server ──────────────────────────────────────────────────────


def test_windows_stop_refuses_when_payload_incomplete(monkeypatch):
    """Missing target payload → stop_server returns
    target_payload_incomplete WITHOUT scp_to or stop_server.ps1 execution."""
    from agent.lifecycle.windows import WindowsLifecycle

    calls: list[str] = []
    scp_calls: list[tuple] = []

    def fake_run_ssh(_ssh_cfg, command, **_kwargs):
        calls.append(command)
        if "Test-Path" in command:
            return (0, "missing")
        raise AssertionError(
            f"unexpected run_ssh call after integrity failure: {command[:200]}"
        )

    def fake_scp_to(*args, **kwargs):
        scp_calls.append((args, kwargs))
        return (0, "")

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)
    monkeypatch.setattr("agent.lifecycle.windows.scp_to", fake_scp_to)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "mcp": {"port": 8765},
        "windows": {"target_root": "C:\\edr-wd\\target"},
    }

    result = lifecycle.stop_server(cfg)

    assert result["ok"] is False
    assert result["code"] == "target_payload_incomplete"
    assert scp_calls == [], (
        f"Phase 2 no-write contract violated: {scp_calls}"
    )
    # CRITICAL: stop_server.ps1 was never INVOKED — only Test-Path
    # probes against the script path are expected (from _target_integrity).
    invoked = [c for c in calls if "stop_server.ps1" in c and "Test-Path" not in c]
    assert not invoked, (
        f"stop_server.ps1 was invoked after integrity failure: {invoked}"
    )


# ── macOS ensure_server_running ──────────────────────────────────────────────


def test_macos_ensure_refuses_when_payload_incomplete(monkeypatch):
    """Missing target payload → macOS ensure_server_running returns
    target_payload_incomplete WITHOUT scp_to or launchctl kickstart."""
    from agent.lifecycle.macos import MacOSLifecycle

    calls: list[str] = []
    scp_calls: list[tuple] = []

    def fake_run_ssh(_ssh_cfg, command, **_kwargs):
        calls.append(command)
        # 1) probe (hostname) — make sure we get past probe
        if "hostname" in command:
            return (0, "edr-mbp\nadmin")
        if "scutil" in command:
            return (0, "edr-mbp\n14.5")
        # 2) port-listening probe uses a local socket in macOS, not SSH
        # 3) port-kill uses lsof — return success
        if "lsof -tiTCP" in command and "kill" in command:
            return (0, "")
        # 4) _target_integrity's 6 file probes — all "missing"
        if "test -f" in command or "if [ -f" in command:
            return (0, "missing")
        # 5) launchctl kickstart — should NOT be called
        if "launchctl" in command:
            raise AssertionError(
                f"launchctl was invoked after integrity failure: {command[:200]}"
            )
        raise AssertionError(
            f"unexpected run_ssh call: {command[:200]}"
        )

    def fake_scp_to(*args, **kwargs):
        scp_calls.append((args, kwargs))
        return (0, "")

    def fake_scp_dir_to(*args, **kwargs):
        scp_calls.append((args, kwargs))
        return (0, "")

    monkeypatch.setattr("agent.lifecycle.macos.run_ssh", fake_run_ssh)
    monkeypatch.setattr("agent.lifecycle.macos.scp_to", fake_scp_to)
    # macOS lifecycle uses scp_to only (no scp_dir_to).

    # Make local port listening check return False (port not open)
    monkeypatch.setattr("agent.lifecycle.macos._is_port_listening", lambda *a, **k: False)

    lifecycle = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "mcp": {"port": 8765, "connect_mode": "tunnel"},
        "macos": {"root": "/Users/admin/edr-wd/target", "launch_name": "com.edr-wd.target"},
    }

    result = lifecycle.ensure_server_running(cfg)

    assert result["ok"] is False
    assert result["code"] == "target_payload_incomplete"
    assert len(result["data"]["missing"]) == 6
    # CRITICAL: zero scp_to / scp_dir_to
    assert scp_calls == [], (
        f"Phase 2 no-write contract violated: {scp_calls}"
    )


# ── macOS stop_server ────────────────────────────────────────────────────────


def test_macos_stop_refuses_when_payload_incomplete(monkeypatch):
    """Missing target payload → macOS stop_server returns
    target_payload_incomplete WITHOUT scp_to or stop_server.sh execution."""
    from agent.lifecycle.macos import MacOSLifecycle

    calls: list[str] = []
    scp_calls: list[tuple] = []

    def fake_run_ssh(_ssh_cfg, command, **_kwargs):
        calls.append(command)
        if "if [ -f" in command or "test -f" in command:
            return (0, "missing")
        raise AssertionError(
            f"unexpected run_ssh call: {command[:200]}"
        )

    def fake_scp_to(*args, **kwargs):
        scp_calls.append((args, kwargs))
        return (0, "")

    monkeypatch.setattr("agent.lifecycle.macos.run_ssh", fake_run_ssh)
    monkeypatch.setattr("agent.lifecycle.macos.scp_to", fake_scp_to)

    lifecycle = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "mcp": {"port": 8765, "connect_mode": "tunnel"},
        "macos": {"root": "/Users/admin/edr-wd/target", "launch_name": "com.edr-wd.target"},
    }

    result = lifecycle.stop_server(cfg)

    assert result["ok"] is False
    assert result["code"] == "target_payload_incomplete"
    assert scp_calls == [], (
        f"Phase 2 no-write contract violated: {scp_calls}"
    )
    # CRITICAL: stop_server.sh was never INVOKED — only file probes
    # against the script path are expected (from _target_integrity).
    invoked = [
        c for c in calls
        if "stop_server.sh" in c and "if [ -f" not in c and "test -f" not in c
    ]
    assert not invoked, (
        f"stop_server.sh was invoked after integrity failure: {invoked}"
    )
