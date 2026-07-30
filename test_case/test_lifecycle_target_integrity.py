"""Read-only integrity helper tests (Phase 1, no target writes).

These tests verify the contract of the four integrity helpers introduced in
Phase 1 of the target-sync-without-ad-hoc-scripts refactor:

  - WindowsLifecycle._target_integrity
  - WindowsLifecycle._task_integrity
  - MacOSLifecycle._target_integrity
  - MacOSLifecycle._launchagent_integrity

All helpers MUST be read-only: no scp_to / scp_dir_to / install / deploy
side effects.  Tests use a fake run_ssh installed on the lifecycle module's
own module-level reference (lifecycle top-of-file import), the same pattern
as test_target_manager_repair.py.
"""

from __future__ import annotations


# ── Windows: _target_integrity ────────────────────────────────────────────────


def test_windows_target_integrity_all_present(monkeypatch):
    """All five required Windows payload files exist on the target."""
    from agent.lifecycle.windows import WindowsLifecycle

    commands: list[str] = []

    def fake_run_ssh(_ssh_cfg, command, **_kwargs):
        commands.append(command)
        return (0, "found")

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {"target_root": "C:\\edr-wd\\target"},
    }

    result = lifecycle._target_integrity(cfg)

    assert result["ok"] is True
    assert result["stage"] == "integrity"
    assert result["data"]["missing"] == []
    assert result["data"]["platform"] == "windows"
    # 5 required files × 1 SSH call each
    assert len(commands) == 5
    # Each command probes one required file
    for fname in (
        "server.py",
        "automation/__init__.py",
        "scripts/start_server.ps1",
        "scripts/stop_server.ps1",
        "scripts/install_task.ps1",
    ):
        assert any(fname.replace("\\", "/") in c for c in commands), (
            f"Expected probe for {fname}; got commands: {commands}"
        )


def test_windows_target_integrity_partial_missing(monkeypatch):
    """Two files missing → returns target_payload_incomplete with both names."""
    from agent.lifecycle.windows import WindowsLifecycle

    def fake_run_ssh(_ssh_cfg, command, **_kwargs):
        # Simulate server.py present but start_server.ps1 and stop_server.ps1 missing.
        if "start_server.ps1" in command or "stop_server.ps1" in command:
            return (0, "missing")
        return (0, "found")

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {"target_root": "C:\\edr-wd\\target"},
    }

    result = lifecycle._target_integrity(cfg)

    assert result["ok"] is False
    assert result["stage"] == "integrity"
    assert result["code"] == "target_payload_incomplete"
    assert result["next_action"] == "Run deploy_target() then install_target_task()."
    assert set(result["data"]["missing"]) == {
        "scripts/start_server.ps1",
        "scripts/stop_server.ps1",
    }
    assert result["data"]["platform"] == "windows"


def test_windows_target_integrity_strips_whitespace_and_lowercases(monkeypatch):
    """Verifies robust parsing of SSH output (e.g. trailing whitespace)."""
    from agent.lifecycle.windows import WindowsLifecycle

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        # PowerShell may emit trailing whitespace or CRLF
        return (0, "FOUND\r\n")

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {"target_root": "C:\\edr-wd\\target"},
    }

    result = lifecycle._target_integrity(cfg)
    assert result["ok"] is True


# ── Windows: _task_integrity ──────────────────────────────────────────────────


def test_windows_task_integrity_task_missing(monkeypatch):
    """Task not registered → scheduled_task_invalid with next_action."""
    from agent.lifecycle.windows import WindowsLifecycle

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        return (0, "task_missing")

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {
            "target_root": "C:\\edr-wd\\target",
            "task_name": "StartEDRMCP",
        },
    }

    result = lifecycle._task_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "scheduled_task_invalid"
    assert result["next_action"] == "Run install_target_task()."
    assert result["data"]["task_name"] == "StartEDRMCP"


def test_windows_task_integrity_action_command_mismatch(monkeypatch):
    """Task registered but Action.Command doesn't reference target_root."""
    from agent.lifecycle.windows import WindowsLifecycle

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        return (
            0,
            "cmd=C:\\Users\\admin\\wrong-path\\start_server.ps1\nlogonType=3",
        )

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {
            "target_root": "C:\\edr-wd\\target",
            "task_name": "StartEDRMCP",
        },
    }

    result = lifecycle._task_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "scheduled_task_invalid"
    failures = result["data"]
    assert "expected_command_substring" in failures


def test_windows_task_integrity_logon_type_none(monkeypatch):
    """Task registered, command correct, but LogonType='None' rejected."""
    from agent.lifecycle.windows import WindowsLifecycle

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        return (
            0,
            "cmd=C:\\edr-wd\\target\\scripts\\start_server.ps1\nlogonType=None",
        )

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {
            "target_root": "C:\\edr-wd\\target",
            "task_name": "StartEDRMCP",
        },
    }

    result = lifecycle._task_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "scheduled_task_invalid"
    assert "not in the accepted set" in result["error"]


def test_windows_task_integrity_valid(monkeypatch):
    """Task registered, command correct, LogonType='Interactive' accepted.

    install_task.ps1 sets `-LogonType Interactive`; verify our allow-list
    accepts the actual value the install script writes.
    """
    from agent.lifecycle.windows import WindowsLifecycle

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        return (
            0,
            "cmd=C:\\edr-wd\\target\\scripts\\start_server.ps1\nlogonType=Interactive",
        )

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {
            "target_root": "C:\\edr-wd\\target",
            "task_name": "StartEDRMCP",
        },
    }

    result = lifecycle._task_integrity(cfg)

    assert result["ok"] is True
    assert result["stage"] == "task"
    assert result["data"]["task_name"] == "StartEDRMCP"
    assert result["data"]["logon_type"] == "Interactive"


# ── macOS: _target_integrity ──────────────────────────────────────────────────


def test_macos_target_integrity_all_present(monkeypatch):
    """All six macOS payload files exist on the target."""
    from agent.lifecycle.macos import MacOSLifecycle

    commands: list[str] = []

    def fake_run_ssh(_ssh_cfg, command, **_kwargs):
        commands.append(command)
        return (0, "found")

    monkeypatch.setattr("agent.lifecycle.macos.run_ssh", fake_run_ssh)

    lifecycle = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {"root": "/Users/admin/edr-wd/target"},
    }

    result = lifecycle._target_integrity(cfg)

    assert result["ok"] is True
    assert result["data"]["missing"] == []
    assert result["data"]["platform"] == "macos"
    # 6 required files × 1 SSH call each
    assert len(commands) == 6


def test_macos_target_integrity_partial_missing(monkeypatch):
    """Single file (install_launch_agent.sh) missing."""
    from agent.lifecycle.macos import MacOSLifecycle

    def fake_run_ssh(_ssh_cfg, command, **_kwargs):
        if "install_launch_agent.sh" in command:
            return (0, "missing")
        return (0, "found")

    monkeypatch.setattr("agent.lifecycle.macos.run_ssh", fake_run_ssh)

    lifecycle = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {"root": "/Users/admin/edr-wd/target"},
    }

    result = lifecycle._target_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "target_payload_incomplete"
    assert result["data"]["missing"] == ["scripts/macos/install_launch_agent.sh"]


# ── macOS: _launchagent_integrity ─────────────────────────────────────────────


def test_macos_launchagent_integrity_plist_missing(monkeypatch):
    """plist file not present at ~/Library/LaunchAgents/<name>.plist."""
    from agent.lifecycle.macos import MacOSLifecycle

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        # test -f fails and short-circuits the && chain → nonzero rc, empty stdout
        return (1, "")

    monkeypatch.setattr("agent.lifecycle.macos.run_ssh", fake_run_ssh)

    lifecycle = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {
            "root": "/Users/admin/edr-wd/target",
            "launch_name": "com.edr-wd.target",
        },
    }

    result = lifecycle._launchagent_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "launchagent_invalid"
    assert result["next_action"] == "Run install_target_task()."


def test_macos_launchagent_integrity_label_mismatch(monkeypatch):
    """plist present but Label does not match expected launch_name."""
    from agent.lifecycle.macos import MacOSLifecycle

    plist_xml = """<?xml version=\"1.0\"?>
<plist>
  <dict>
    <key>Label</key>
    <string>com.wrong-label</string>
    <key>ProgramArguments</key>
    <array>
      <string>/Users/admin/edr-wd/target/scripts/macos/start_server.sh</string>
    </array>
  </dict>
</plist>"""

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        return (0, plist_xml)

    monkeypatch.setattr("agent.lifecycle.macos.run_ssh", fake_run_ssh)

    lifecycle = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {
            "root": "/Users/admin/edr-wd/target",
            "launch_name": "com.edr-wd.target",
        },
    }

    result = lifecycle._launchagent_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "launchagent_invalid"


def test_macos_launchagent_label_must_match_label_field(monkeypatch):
    """The expected label appearing in another plist field is insufficient."""
    from agent.lifecycle.macos import MacOSLifecycle

    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.wrong</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/admin/edr-wd/target/scripts/macos/start_server.sh</string>
    <string>com.edr-wd.target</string>
  </array>
</dict>
</plist>"""

    monkeypatch.setattr(
        "agent.lifecycle.macos.run_ssh",
        lambda *_a, **_k: (0, plist),
    )

    result = MacOSLifecycle()._launchagent_integrity({
        "ssh": {"host": "127.0.0.1"},
        "macos": {
            "root": "/Users/admin/edr-wd/target",
            "launch_name": "com.edr-wd.target",
        },
    })

    assert result["ok"] is False
    assert result["code"] == "launchagent_invalid"
    assert "Label" in result["error"]


def test_macos_launchagent_integrity_program_mismatch(monkeypatch):
    """plist has correct Label but ProgramArguments references wrong script."""
    from agent.lifecycle.macos import MacOSLifecycle

    plist_xml = """<?xml version=\"1.0\"?>
<plist>
  <dict>
    <key>Label</key>
    <string>com.edr-wd.target</string>
    <key>ProgramArguments</key>
    <array>
      <string>/some/other/path/start_server.sh</string>
    </array>
  </dict>
</plist>"""

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        return (0, plist_xml)

    monkeypatch.setattr("agent.lifecycle.macos.run_ssh", fake_run_ssh)

    lifecycle = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {
            "root": "/Users/admin/edr-wd/target",
            "launch_name": "com.edr-wd.target",
        },
    }

    result = lifecycle._launchagent_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "launchagent_invalid"
    assert "ProgramArguments" in result["error"]


def test_macos_launchagent_integrity_valid(monkeypatch):
    """plist has both correct Label and ProgramArguments → ok."""
    from agent.lifecycle.macos import MacOSLifecycle

    plist_xml = """<?xml version=\"1.0\"?>
<plist>
  <dict>
    <key>Label</key>
    <string>com.edr-wd.target</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>/Users/admin/edr-wd/target/scripts/macos/start_server.sh</string>
    </array>
  </dict>
</plist>"""

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        return (0, plist_xml)

    monkeypatch.setattr("agent.lifecycle.macos.run_ssh", fake_run_ssh)

    lifecycle = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {
            "root": "/Users/admin/edr-wd/target",
            "launch_name": "com.edr-wd.target",
        },
    }

    result = lifecycle._launchagent_integrity(cfg)

    assert result["ok"] is True
    assert result["stage"] == "launchagent"
    assert result["data"]["label"] == "com.edr-wd.target"
    assert result["data"]["target_root"] == "/Users/admin/edr-wd/target"
    # The matched argument should reference the start script
    assert "start_server" in result["data"]["matched_program_arg"]
    assert result["data"]["program_arguments"] == [
        "/bin/bash",
        "/Users/admin/edr-wd/target/scripts/macos/start_server.sh",
    ]


# ── SSH failure paths (network/auth errors must not look like missing payload) ──


def test_windows_target_integrity_ssh_exception(monkeypatch):
    """run_ssh raises → target_integrity_check_failed (NOT target_payload_incomplete)."""
    from agent.lifecycle.windows import WindowsLifecycle

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        raise RuntimeError("connection refused: 127.0.0.1:22")

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {"target_root": "C:\\edr-wd\\target"},
    }

    result = lifecycle._target_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "target_integrity_check_failed"
    assert result["next_action"] == (
        "Verify SSH connectivity (auth, network, host) to "
        "the target, then retry."
    )
    # All 5 files should be reported as ssh failures
    assert len(result["data"]["ssh_failures"]) == 5
    assert all("connection refused" in f["error"] for f in result["data"]["ssh_failures"])


def test_windows_target_integrity_ssh_nonzero_rc(monkeypatch):
    """run_ssh returns rc != 0 → target_integrity_check_failed."""
    from agent.lifecycle.windows import WindowsLifecycle

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        return (255, "permission denied (publickey)")

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {"target_root": "C:\\edr-wd\\target"},
    }

    result = lifecycle._target_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "target_integrity_check_failed"
    assert result["data"]["ssh_failures"][0]["rc"] == 255
    assert "permission denied" in result["data"]["ssh_failures"][0]["output"]


def test_windows_task_integrity_ssh_exception(monkeypatch):
    """Task integrity check SSH error → target_task_check_failed."""
    from agent.lifecycle.windows import WindowsLifecycle

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        raise TimeoutError("ssh timed out after 15s")

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {
            "target_root": "C:\\edr-wd\\target",
            "task_name": "StartEDRMCP",
        },
    }

    result = lifecycle._task_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "target_task_check_failed"
    assert "timed out" in result["error"]


def test_macos_target_integrity_ssh_exception(monkeypatch):
    """macOS file integrity check SSH error → target_integrity_check_failed."""
    from agent.lifecycle.macos import MacOSLifecycle

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        raise ConnectionError("host key verification failed")

    monkeypatch.setattr("agent.lifecycle.macos.run_ssh", fake_run_ssh)

    lifecycle = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {"root": "/Users/admin/edr-wd/target"},
    }

    result = lifecycle._target_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "target_integrity_check_failed"
    assert len(result["data"]["ssh_failures"]) == 6


def test_macos_launchagent_integrity_ssh_exception(monkeypatch):
    """macOS plist check SSH error → target_launchagent_check_failed."""
    from agent.lifecycle.macos import MacOSLifecycle

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        raise RuntimeError("auth failed")

    monkeypatch.setattr("agent.lifecycle.macos.run_ssh", fake_run_ssh)

    lifecycle = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {
            "root": "/Users/admin/edr-wd/target",
            "launch_name": "com.edr-wd.target",
        },
    }

    result = lifecycle._launchagent_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "target_launchagent_check_failed"
    assert "auth failed" in result["error"]


def test_macos_launchagent_integrity_invalid_xml(monkeypatch):
    """plist returned by plutil is not valid XML → launchagent_invalid."""
    from agent.lifecycle.macos import MacOSLifecycle

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        return (0, "this is not xml at all <<<>>")

    monkeypatch.setattr("agent.lifecycle.macos.run_ssh", fake_run_ssh)

    lifecycle = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {
            "root": "/Users/admin/edr-wd/target",
            "launch_name": "com.edr-wd.target",
        },
    }

    result = lifecycle._launchagent_integrity(cfg)

    assert result["ok"] is False
    assert result["code"] == "launchagent_invalid"
    assert "not parseable" in result["error"]


def test_macos_launchagent_integrity_tolerates_bash_c_form(monkeypatch):
    """Future-proof: bash -c 'cd $ROOT; ./start_server.sh' still passes.

    A plausible future plist form bundles cd + start script into a
    single -c argument.  Note: ';' is used (not '&&') because raw '&'
    is not a valid character in plist XML and must be escaped.
    """
    from agent.lifecycle.macos import MacOSLifecycle

    # Future shape: one -c argument bundling cd + start script
    plist_xml = """<?xml version=\"1.0\"?>
<plist>
  <dict>
    <key>Label</key>
    <string>com.edr-wd.target</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>-c</string>
      <string>cd /Users/admin/edr-wd/target/scripts/macos; ./start_server.sh</string>
    </array>
  </dict>
</plist>"""

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        return (0, plist_xml)

    monkeypatch.setattr("agent.lifecycle.macos.run_ssh", fake_run_ssh)

    lifecycle = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {
            "root": "/Users/admin/edr-wd/target",
            "launch_name": "com.edr-wd.target",
        },
    }

    result = lifecycle._launchagent_integrity(cfg)

    assert result["ok"] is True
    assert result["data"]["matched_program_arg"] == (
        "cd /Users/admin/edr-wd/target/scripts/macos; ./start_server.sh"
    )


def test_windows_target_integrity_path_with_single_quote(monkeypatch):
    """Regression: Windows path containing a single quote must be escaped.

    Without `_ps_quote`, a path like `C:\\Users\\O'Brien\\target` would
    break the PowerShell single-quoted string and return
    target_payload_incomplete (false negative) or even inject commands.
    """
    from agent.lifecycle.windows import WindowsLifecycle, _ps_quote

    # Verify the helper does the right thing
    assert _ps_quote(r"C:\Users\O'Brien\target") == "C:\\Users\\O''Brien\\target"

    def fake_run_ssh(_ssh_cfg, _command, **_kwargs):
        # When quoting is correct, the path appears with '' (doubled quote)
        # inside the single-quoted string.
        return (0, "found")

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)

    lifecycle = WindowsLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {"target_root": r"C:\Users\O'Brien\target"},
    }

    result = lifecycle._target_integrity(cfg)
    assert result["ok"] is True, (
        f"Expected ok=True with single-quote escaped path; got {result}"
    )
