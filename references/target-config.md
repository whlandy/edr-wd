# Target Config Reference

Use this reference when adding a target, changing `connect_mode`, or editing
`agent/target_config.py`.

## Config Discovery

`TargetConfig` loads runtime config in this order:

1. `EDR_WD_CONFIG`
2. `config/targets.local.json`

`config/targets.example.json` is for `--init` and documentation only. It must
not be used as a real target source.

Useful commands:

```bash
python -m agent.target_config --init
python -m agent.target_config --validate
python -m agent.target_config --list
python -m agent.target_config --guide
```

## Minimal Target Shape

```json
{
  "default_target": "win-dev",
  "targets": {
    "win-dev": {
      "platform": "windows",
      "app_profile": "windows_hisec",
      "ssh": {
        "host": "<TARGET_IP>",
        "port": 22,
        "user": "<TARGET_USER>",
        "auth": {"type": "password", "password": "<TARGET_PASSWORD>"}
      },
      "mcp": {
        "host": "0.0.0.0",
        "port": 8765,
        "path": "/mcp",
        "connect_mode": "direct",
        "tunnel": {"enabled": false, "local_port": 18765}
      },
      "windows": {
        "python_path": "<REMOTE_PYTHON>",
        "target_root": "<REMOTE_TARGET_ROOT>",
        "task_name": "StartEDRMCP",
        "run_with_highest_privileges": true
      }
    }
  }
}
```

For macOS targets use:

```json
{
  "platform": "macos",
  "app_profile": "macos_generic",
  "macos": {
    "python_path": "/opt/homebrew/bin/python3",
    "root": "<REMOTE_REPO_ROOT>",
    "backend": "macos_accessibility",
    "launch_name": "com.edr-wd.target"
  }
}
```

## Auth Policy

This skill is designed for trusted intranet target automation. The local runtime
config is allowed to store target IPs, usernames, and passwords directly.

Prefer inline password auth:

```json
"auth": {"type": "password", "password": "<TARGET_PASSWORD>"}
```

All SSH command execution and file transfer goes through Paramiko. `password_env`
and key auth remain compatibility paths, but are not the default workflow. Do
not print real credentials in assistant responses, logs intended for sharing, or
docs committed to the repository. Keep real values in `config/targets.local.json`
or the file pointed to by `EDR_WD_CONFIG`, and make sure those files stay local.

## Connect Modes

Use only these MCP connection modes:

- `direct`: agent connects directly to `ssh.host:mcp.port`.
- `local`: MCP server runs on the same machine as the agent; URL resolves to
  localhost.
- `tunnel`: agent uses an SSH tunnel and connects to `127.0.0.1:local_port`.

For `direct`, prepare the target host firewall before testing MCP. The default
MCP port is TCP `8765`; Windows targets need an inbound allow rule unless the
environment already allows it. For `tunnel`, the external firewall does not need
to expose `8765` because the agent connects to the local tunnel port.

Do not hard-code one mode in code paths. Let `TargetConfig.build_mcp_url()`
decide from the active target.

## Python Runtime Preconditions

The configured `windows.python_path` or `macos.python_path` must point to the
same Python runtime that will start `target/server.py`. Before deployment or
startup, verify that runtime can import the full target dependency set:

```bash
<REMOTE_PYTHON> -c 'import fastmcp, psutil, PIL; print("core deps ok")'
```

Windows GUI backend additionally requires:

```powershell
"<REMOTE_PYTHON>" -c "import pywinauto, pyautogui; print('windows gui deps ok')"
```

macOS GUI backend additionally requires:

```bash
'<REMOTE_PYTHON>' -c 'import pyautogui; print("mac gui deps ok")'
```

Do not treat a valid `python_path` string as sufficient. The interpreter must
exist, import all required packages, and have the platform GUI permissions
needed by the selected backend.

The runtime dependency source of truth is `pyproject.toml`. If pytest suites are
run from this checkout, also install `test_case/requirements_test.txt` on the
test runner and verify `pytest`/`httpx` imports there.

## Platform/Profile Safety

Known profile defaults:

- `platform=windows` -> `windows_hisec`
- `platform=macos` -> `macos_generic`

Do not silently run Windows HiSec tests against a macOS target. If a live backend
does not match the configured profile, fail clearly or route only through the
existing explicit profile-resolution logic.
