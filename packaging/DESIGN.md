# Package And Relay Risk Review

## Status

Todo design. This document reviews the local desktop `edr-wd` prototype that
contains packaging, `src/mcp_edr_wd/`, and relay MCP work. The review is based
only on local files and must not connect to CodeHub or any other remote.

The packaging direction should be preserved. The relay/MCP-to-MCP direction
is usable, but only as an agent-local relay/control surface. It must be
redesigned before it can be merged into the current `hermes` line.

## Current Decision

MCP-to-MCP is acceptable for EDR-WD when it accelerates the already-installed
target path:

```text
LLM/Codex
  -> agent-local relay MCP
  -> TargetConfig + session/tunnel cache
  -> target MCP server
  -> Windows/macOS GUI backend
```

The relay should give the agent one stable local MCP endpoint, hide repeated
session setup details, cache target tool metadata, and forward GUI actions to
the selected target MCP server. This reduces repeated SSH probing, handwritten
commands, and LLM drift during target connection.

The relay must not become an implicit deployer. Runtime calls such as
`connect_target`, `call_target_tool`, `run_profile_test`, `run_e2e_test`, and
SOP execution are no-write paths. They may initialize sessions, repair only the
local tunnel, and call target MCP tools. They must not upload files, generate
scripts, install dependencies, register services, or repair target payloads.

Deployment remains explicit and agent-driven. If the agent can SSH to the
target, explicit `deploy_target` and `install_target` operations may use
Paramiko SSH/SFTP/SCP to transfer reviewed install payloads and run tracked
install/start scripts. These write-capable operations must be separate tools
with clear names, explicit user intent, and auditable results.

## Scope Reviewed

Desktop prototype paths reviewed:

- `pyproject.toml`
- `mcp-edr-wd.spec`
- `src/mcp_edr_wd/relay/`
- `src/mcp_edr_wd/server/`
- `src/mcp_edr_wd/target/`
- `src/mcp_edr_wd/protocol/`
- `src/mcp_edr_wd/tests/test_relay_manager.py`
- `SKILL.md`
- `references/widget-info.md`

The current mainline remains the root-level `agent/` + `target/` implementation
with explicit deploy/install and no implicit target-side script writes during
normal connect/test/E2E/SOP flows.

## Code Review Findings

### P0: Relay `_call` Auto-Ensures The Target

The relay prototype makes tool forwarding call `get_or_ensure_session()` before
every target tool call. That path calls `ensure_server_running()` when a session
is not initialized.

Risk:

- a normal GUI tool call can trigger lifecycle work before the caller has
  explicitly asked for it.
- if the underlying lifecycle allows repair/deploy, the relay can reintroduce
  hidden target writes.
- even without repair, this blurs the difference between connect, start,
  deploy, test, and SOP execution.

Required design:

- relay `_call` must be session-only by default.
- if no initialized session exists, return `session_not_initialized`.
- expose explicit lifecycle tools separately:
  - `check_target_health`
  - `ensure_target_session`
  - `deploy_target`
  - `install_target_task`
- make any repair/write operation require an explicit `repair=true` or an
  explicit deploy/install tool.

### P0: Relay Documentation Says Auto-Deploy

The relay docstrings describe the flow as:

```text
TCP probe -> auto-deploy -> auto-start -> GUI readiness check
```

This contradicts the current target file contract. Normal connect/test/E2E/SOP
flows must not deploy, install, upload scripts, or repair payloads implicitly.

Required design:

- replace all "auto-deploy" language with "explicit deploy/install only".
- define default relay behavior as "initialize existing target MCP session".
- define repair behavior as opt-in and auditable.

### P0: Package Prototype Duplicates Root Implementations

The desktop prototype contains both:

- root-level `agent/` and `target/`
- package-level `src/mcp_edr_wd/agent/`, `src/mcp_edr_wd/target/`,
  `src/mcp_edr_wd/server/`

Risk:

- two copies of lifecycle, backend, tools, and target scripts drift quickly.
- fixes such as no implicit script upload, PowerShell control-plane safety, and
  HiSec window-pair activation can land in one tree but not the other.
- deployment may accidentally package stale code.

Required design:

- choose one source of truth before package migration.
- short-term: keep root `agent/` + `target/` as source of truth.
- package build should either import/adapt root modules or migrate in one
  controlled branch with parity tests.
- do not keep long-lived duplicate implementations.

### P0: `relay/__init__.py` Contains Duplicate And Invalid Imports

The prototype `relay/__init__.py` has two module headers and two `__all__`
blocks. The second block imports through `src.mcp_edr_wd...` and references
`RelayManager`, which does not match the reviewed manager class name.

Risk:

- packaged imports can fail depending on `sys.path`.
- PyInstaller builds can accidentally freeze a broken import path.

Required design:

- use only package imports rooted at `mcp_edr_wd`.
- export `LifecycleAwareRelayManager`, not a nonexistent or stale
  `RelayManager`.
- add an import smoke test for the installed package and the frozen executable.

### P1: PyInstaller Spec Is Valuable But Too Broad

The prototype `mcp-edr-wd.spec` is worth keeping because it establishes a
one-file executable target. However, it currently copies the whole
`src/mcp_edr_wd` tree as `datas` and uses a Windows path literal for the entry
script.

Risk:

- broad `datas` can hide import/package mistakes because source files are copied
  as loose runtime files.
- packaging can include tests, duplicate target code, or stale helper modules.
- Windows path literals make the spec less portable.

Required design:

- keep PyInstaller packaging as a first-class goal.
- move packaging files under a clear location such as `packaging/pyinstaller/`
  or keep a root spec with documentation.
- use POSIX-compatible or `os.path.join` paths in the spec.
- include only required metadata and runtime assets:
  - FastMCP metadata
  - target scripts/templates
  - package resources needed by the executable
- exclude tests, caches, generated screenshots, local config, and untracked
  artifacts.
- add `pyinstaller --clean --noconfirm` build docs and a smoke command.

### P1: CLI Dependency Set Is Incomplete

The prototype `pyproject.toml` adds `typer`, but the CLI imports `rich`. `rich`
is not listed as a direct dependency.

Risk:

- installed CLI can fail at startup even though package installation succeeded.

Required design:

- add `rich` explicitly if Typer console output depends on it.
- decide whether CLI packaging belongs to base dependencies or an extra such as
  `edr-wd[cli]`.

### P1: Tool Modules Use Inconsistent Import Roots

The package prototype mixes import styles:

- `from src.mcp_edr_wd...`
- `from mcp_edr_wd...`
- `from target...`
- `from backends...`

Risk:

- direct script execution, installed package execution, and PyInstaller
  execution can behave differently.
- target-server imports may work only because paths are mutated at runtime.

Required design:

- package modules must import from `mcp_edr_wd...`.
- root-level direct execution wrappers may adjust `sys.path`, but package code
  should not import through `src`.
- add tests for:
  - `python -m mcp_edr_wd.relay.cli --help`
  - `python -m mcp_edr_wd.target.server --help`
  - installed console script import
  - PyInstaller executable startup

### P1: Relay Result Unwrapping Is Too Assumptive

The relay `_call` unwraps target results by assuming a nested FastMCP shape and
accessing fields such as `result.get("data").get("result")`.

Risk:

- a target tool that returns plain JSON, a content envelope, an error envelope,
  or a structuredContent variant can break the relay.
- error details can be lost.

Required design:

- reuse the current mainline MCP result parser/unwrapper.
- preserve `ok`, `error`, `code`, and raw envelope when parsing fails.
- add tests for plain JSON, content text JSON, structuredContent JSON, tool
  errors, and protocol errors.

### P2: Desktop Skill Encourages Generated Workflow Scripts

The desktop `SKILL.md` instructs generated Python files to use
`workflow-{TaskName}.py`.

Risk:

- this normalizes generated workflow scripts and may recreate the exact
  "useless file" problem the current skill is trying to prevent.
- limiting the name does not prevent unnecessary file creation.

Required design:

- remove this rule from any merged skill text.
- if workflow code is needed, it must live in a reviewed `sops/` executor or a
  tracked test module, not ad hoc generated files.

## What To Keep

Keep these ideas from the desktop prototype:

- PyInstaller packaging goal.
- executable goal, named `mcp.exe` for Windows builds.
- console script goal for development use, likely named `edr-wd` or
  `mcp-edr-wd`, as long as it points at the same executable entrypoint.
- `src/mcp_edr_wd/protocol/` style typed constants/errors, after cleanup.
- `references/widget-info.md` as candidate product/component knowledge, after
  review and redaction.
- package import smoke tests.

Do not keep these as-is:

- relay as the default target control path.
- auto-ensure / auto-deploy on proxy tool calls.
- duplicated root and package implementations.
- generated `workflow-*.py` instruction.
- `src.` import paths inside package code.

## Recommended Architecture

Preserve the current runtime model:

```text
Agent Python orchestration
  -> Paramiko for explicit lifecycle/deploy/install
  -> MCP client for target GUI tools
  -> one target MCP server in the target desktop session
```

Deployment remains agent-driven. When the agent can SSH to the target, it may
use Paramiko SSH/SFTP/SCP to upload the reviewed `target/` payload and install
files, then run the tracked installer. After installation, runtime connection
should be config-only plus MCP/tunnel/session initialization; it should not
generate or upload more target-side scripts.

Add packaging around that model:

```text
mcp.exe / development console script
  -> CLI facade over existing agent APIs
  -> optional relay facade for external orchestrators
  -> no implicit target writes by default
```

Relay, if kept, should be a facade with explicit modes:

| Operation | Default write behavior | Notes |
|---|---|---|
| `list_targets` | no writes | reads config only |
| `probe_target` | no writes | SSH/read-only checks only |
| `check_target_health` | no writes | MCP/tunnel/port/status only |
| `ensure_target_session` | no writes by default | starts already-deployed payload only |
| `_call` / `call_target_tool` | no writes | requires initialized session |
| `run_profile_test` | no writes | baseline/profile tests only; no EDR page clicks |
| `run_e2e_test` | no writes to target files | may activate EDR windows and collect artifacts only |
| `run_sop` | no writes to lifecycle files | executes reviewed SOPs through MCP tools |
| `deploy_target` | writes | explicit user action only; transfers tracked payload over Paramiko SSH/SFTP/SCP |
| `install_target_task` | writes | explicit user action only; runs tracked installer after payload transfer |
| `repair_target` | writes | optional future explicit command |

## Packaging Design

First-class packaging should be introduced without changing runtime semantics.

Target outcomes:

- `pip install .` exposes a development console script.
- PyInstaller builds a one-file Windows executable named `mcp.exe`.
- package imports are deterministic.
- generated executable can:
  - print version/help.
  - list targets.
  - check dependencies.
  - initialize an already-running target MCP.
  - run health/status without target writes.

Packaging should not initially:

- auto-deploy target payloads from a relay proxy call.
- replace root `agent/` + `target/` implementations with duplicate package
  copies.
- package local credentials, screenshots, logs, or config.

### Packaging Directory Layout

All packaging work should be grouped under `packaging/`, not scattered in the
repository root:

```text
packaging/
  README.md
  pyinstaller/
    mcp.spec
    build.ps1
    build.sh
    smoke.ps1
    smoke.sh
```

Rules:

- root `mcp-edr-wd.spec` from the desktop prototype should be migrated to
  `packaging/pyinstaller/mcp.spec`, then renamed around the final `mcp.exe`
  artifact.
- build scripts belong in `packaging/pyinstaller/`, not root `scripts/`.
- generated `dist/`, `build/`, `artifacts/`, `outputs/`, and frozen executables
  stay ignored unless a release process explicitly publishes them.
- packaging must import the current source of truth; it must not copy a
  long-lived duplicate `src/mcp_edr_wd/agent` or `src/mcp_edr_wd/target` tree.

### `uv.lock` Policy

`uv.lock` is a dependency lock file produced by `uv`. It records the exact
resolved package versions, including transitive dependencies, for reproducible
installs and repeatable PyInstaller builds.

Use it when:

- the project standardizes on `uv` for packaging/build machines.
- reproducible `mcp.exe` builds matter.
- CI or release scripts install dependencies with `uv sync`.

Do not use it as:

- target runtime config.
- a file to upload to target machines during connect/test/SOP.
- a substitute for `pyproject.toml`; `pyproject.toml` remains the source of
  declared direct dependencies.

Decision: keep `uv.lock` only if the packaging implementation adopts `uv` as a
supported build path. If packaging remains plain `pip + PyInstaller`, do not
copy the desktop `uv.lock` into mainline yet.

## Migration Plan

1. Import the packaging metadata only:
   - add `typer`/`rich` if CLI is accepted.
   - add PyInstaller dev dependency.
   - add packaging docs under `packaging/`.

2. Add minimal CLI over current root APIs:
   - `mcp.exe targets`
   - `mcp.exe health <target>`
   - `mcp.exe ensure <target>` with no implicit repair.
   - `mcp.exe deploy <target>` as explicit write.

3. Add PyInstaller spec:
   - avoid broad copy of the whole source tree.
   - include only package/runtime assets.
   - add build and smoke tests.

4. Decide package source of truth:
   - either migrate root code into `src/mcp_edr_wd` in one controlled branch, or
   - keep root code and make package CLI import it.

5. Redesign relay:
   - make it optional.
   - disable auto-deploy/auto-repair by default.
   - make `_call` require initialized session.
   - add tests that prove proxy tool calls perform no deploy/install/upload.

6. Only after parity tests pass, consider merging relay/package work into the
   mainline.

## Required Tests Before Merge

- `python -m py_compile` for packaging/relay modules.
- package import smoke:
  - `python -c "import mcp_edr_wd"`
  - `python -m mcp_edr_wd.relay.cli --help`
- CLI no-write tests:
  - health/status do not call deploy/install.
  - target tool calls do not call deploy/install.
- relay no-write tests:
  - `_call` without a session returns `session_not_initialized`.
  - `_call` does not invoke `ensure_server_running`.
  - `ensure_target_session(repair=False)` does not deploy/install.
- PyInstaller smoke:
  - executable starts and prints help.
  - executable lists tools without local config secrets.
- packaging exclusion tests:
  - no `config/targets.local.json`.
  - no logs/screenshots/caches.
  - no `.git`.

## Acceptance Criteria

This design is ready to implement when:

- packaging is accepted as a goal.
- relay is explicitly classified as optional, not the default path.
- every target write remains behind explicit deploy/install/repair commands.
- no generated workflow scripts are introduced.
- there is one implementation source of truth for lifecycle/backend/tool code.
- package and PyInstaller smoke tests are part of the normal test set.
