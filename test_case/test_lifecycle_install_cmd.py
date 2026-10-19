"""Regression: install() command quoting for the cmd.exe remote shell.

The remote command is executed by Windows OpenSSH's default shell
(cmd.exe), which does NOT treat single quotes as quoting.  A single-quoted
``-File 'C:/.../scripts\\install_task.ps1'`` argument therefore reached
PowerShell with literal quote characters inside the path and failed with
"The format of the path is not supported" on the live 11.26 target
(2026-10-20 repair run, stage=install, task_registration_failed).

The command must use double quotes (stripped by cmd.exe before
PowerShell parses the arguments) and a pure-backslash script path,
consistent with every other remote command in this module.
"""

from __future__ import annotations


def _capture_install(monkeypatch):
    from agent.lifecycle.windows import WindowsLifecycle

    commands: list[str] = []

    def fake_run_ssh(_ssh_cfg, command, **_kwargs):
        commands.append(command)
        return (0, "Task created")

    def fake_scp_to(_ssh_cfg, _local, _remote, **_kwargs):
        return (0, "")

    monkeypatch.setattr("agent.lifecycle.windows.run_ssh", fake_run_ssh)
    monkeypatch.setattr("agent.lifecycle.windows.scp_to", fake_scp_to)
    return WindowsLifecycle(), commands


def _install_cmd_for(commands: list[str]) -> str:
    return next(c for c in commands if "install_task.ps1" in c)


def test_install_file_argument_uses_double_quotes_not_single(monkeypatch):
    lifecycle, commands = _capture_install(monkeypatch)
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {
            "target_root": "C:\\Users\\admin\\Desktop\\edr-wd",
            "task_name": "StartEDRMCP",
        },
    }

    result = lifecycle.install(cfg)

    assert result["ok"] is True
    cmd = _install_cmd_for(commands)
    # The -File path must be double-quoted so cmd.exe strips the quotes.
    assert '-File "' in cmd, f"-File must use double quotes; got: {cmd}"
    # No literal single quote may remain anywhere in the command: cmd.exe
    # passes them through verbatim and PowerShell rejects the malformed path.
    assert "'" not in cmd, f"single quote survives into the remote command: {cmd}"


def test_install_file_path_is_pure_backslash_windows_path(monkeypatch):
    lifecycle, commands = _capture_install(monkeypatch)
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "windows": {
            # Forward-slash config must also arrive as a Windows path.
            "target_root": "C:/Users/admin/Desktop/edr-wd",
            "task_name": "StartEDRMCP",
        },
    }

    result = lifecycle.install(cfg)

    assert result["ok"] is True
    cmd = _install_cmd_for(commands)
    assert "C:\\Users\\admin\\Desktop\\edr-wd\\scripts\\install_task.ps1" in cmd, (
        f"-File path must be pure-backslash; got: {cmd}"
    )
    # -TargetRoot is a PowerShell-side value: backslash form, double-quoted.
    assert '-TargetRoot "C:\\Users\\admin\\Desktop\\edr-wd"' in cmd, cmd
    assert '-TaskName "StartEDRMCP"' in cmd, cmd
