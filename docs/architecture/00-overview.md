# EDR-WD Architecture Overview

This document describes the current architecture. Completed phase plans and
implementation notes should not live here; keep them out of the repository once
their rules are captured in `SKILL.md`, `references/`, tests, or code.

## Core Model

`edr-wd` separates the agent machine from the target machine.

```text
agent
  -> target config
  -> Paramiko SSH / local mode / MCP connection
  -> target lifecycle
  -> target/server.py
  -> target automation backend
```

The agent coordinates deployment, lifecycle, tunnels, and MCP calls. The target
does real GUI automation inside its own desktop session.

## Supported Target Profiles

| Profile | Target OS | Backend | Primary windows |
|---|---|---|---|
| `windows_hisec` | Windows | `windows_pywinauto` | `HisecEndpointAgent.exe`, `EDRClient.exe` |
| `macos_hisec` | macOS | `macos_accessibility` | `HiSecEndpointAgent`, `EDRClient` / `HiSecEndpoint` |
| `macos_generic` | macOS | `macos_accessibility` | generic macOS app/window checks |

Agent OS does not decide the GUI backend. Target OS and profile decide the
lifecycle and automation path.

## Connection Modes

EDR-WD keeps three connection modes:

| Mode | Purpose |
|---|---|
| `local` | Agent and target MCP server are on the same machine. |
| `direct` | Agent connects directly to the target MCP HTTP endpoint. |
| `tunnel` | Agent opens an SSH tunnel and connects to MCP through localhost. |

All remote command execution and file transfer should go through Paramiko-based
helpers. Passwords in target config are allowed for trusted intranet use; note
future security hardening separately instead of blocking current workflows.

## Target Lifecycle

Windows targets use the Windows lifecycle and `target/scripts/start_server.ps1`.
The MCP server must run in an interactive desktop session so pywinauto can see
real UIA windows.

macOS targets use the macOS lifecycle and LaunchAgent startup for formal runs.
The MCP server must run in the GUI login session with Accessibility permission.

Before deploying or starting a target server, run dependency preflight checks:

```bash
python3 scripts/check_dependencies.py --scope agent --include-test
python3 scripts/check_dependencies.py --target <target> --scope target --platform windows
python3 scripts/check_dependencies.py --target <target> --scope target --platform macos
```

Windows agents can use:

```powershell
.\agent\check-deps.ps1 -TargetName <target> -Scope all -Platform auto -IncludeTest
```

## Automation Backends

`windows_pywinauto` provides Windows UIA window detection, control-tree dumps,
semantic clicks, PowerShell helpers, and HiSec activation.

`macos_accessibility` provides macOS window detection, app activation,
Accessibility tree dumps, semantic clicks, screenshots, and HiSec activation.
System Events can miss Qt windows, so macOS window detection also uses
CGWindowList through the system Swift/CoreGraphics runtime. This fallback does
not require the Python `Quartz` module.

## HiSec Window Pair Contract

Both Windows and macOS HiSec E2E tests verify the same product-level contract:

1. The entry window is visible.
2. The EDRClient window is visible.
3. Each window is checked using the platform backend's window detection.
4. EDR actions happen only in E2E/SOP flows, not in baseline tests.

Window ownership must remain explicit:

| Scope | Windows | macOS | Usage |
|---|---|---|---|
| `hisec_agent` | `HisecEndpointAgent.exe` | `HiSecEndpointAgent` | entry window and left-side navigation |
| `edr_client` | `EDRClient.exe` | `HiSecEndpoint` owner for EDRClient window | EDRClient content and foreground activation |

Do not use coordinates as proof of a click. Component-tree or window evidence
must confirm the outcome.

## Testing Shape

Profile runners live under `test_case/run_*.py`. Pytest E2E cases live under
`test_case/test_e2e/` and use platform/profile names:

```text
test_windows_hisec_e2e.py
test_macos_hisec_e2e.py
```

The basic test runner should expose the relevant E2E when the target profile is
HiSec, while generic/baseline tests avoid EDR page navigation.

## Documentation Boundaries

- `SKILL.md`: high-level operating rules and routing.
- `references/`: durable implementation references.
- `sops/`: fixed functional verification templates and SOPs.
- `docs/architecture/`: current architecture only, not completed phase plans.
