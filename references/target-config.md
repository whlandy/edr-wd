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

`password_env` and key auth remain compatibility paths, but are not the default
workflow. Do not print real credentials in assistant responses, logs intended
for sharing, or docs committed to the repository. Keep real values in
`config/targets.local.json` or the file pointed to by `EDR_WD_CONFIG`, and make
sure those files stay local.

## Connect Modes

Use only these MCP connection modes:

- `direct`: agent connects directly to `ssh.host:mcp.port`.
- `local`: MCP server runs on the same machine as the agent; URL resolves to
  localhost.
- `tunnel`: agent uses an SSH tunnel and connects to `127.0.0.1:local_port`.

Do not hard-code one mode in code paths. Let `TargetConfig.build_mcp_url()`
decide from the active target.

## Platform/Profile Safety

Known profile defaults:

- `platform=windows` -> `windows_hisec`
- `platform=macos` -> `macos_generic`

Do not silently run Windows HiSec tests against a macOS target. If a live backend
does not match the configured profile, fail clearly or route only through the
existing explicit profile-resolution logic.
