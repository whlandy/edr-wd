# EDR-WD Packaging

This directory is the home for executable packaging work.

The intended Windows deliverable is:

```text
mcp.exe
```

`mcp.exe` is an agent-side executable facade. It should wrap the current
root-level `agent/` and `target/` source of truth instead of carrying a duplicate
implementation under a second package tree.

## Directory Contract

Future packaging files should live here:

```text
packaging/
  README.md
  DESIGN.md
  pyinstaller/
    README.md
    mcp.spec
    build.ps1
    build.sh
    smoke.ps1
    smoke.sh
```

The repository root should not accumulate one-off packaging scripts, temporary
spec files, build logs, frozen executables, or copied source trees.

## Build Boundary

Packaging may include:

- a reviewed executable entrypoint for agent-local control.
- PyInstaller spec/build scripts.
- smoke-test scripts for `mcp.exe --help`, target listing, health checks, and
  no-write relay/session calls.
- required package metadata for FastMCP and runtime imports.

Packaging must not include:

- `config/targets.local.json` or any secret-bearing local config.
- screenshots, logs, caches, or generated reports.
- duplicated long-lived copies of `agent/` or `target/`.
- relay behavior that auto-deploys, auto-installs, or writes target files from
  ordinary connect/tool-call/test paths.

## Lock File Policy

If uv is selected as the packaging installer, keep `uv.lock` at the repository
root. `uv.lock` pins the full dependency resolution so repeated packaging builds
use the same transitive dependency versions. It is not a runtime file and should
not be copied to the target or bundled into `mcp.exe` unless the build tool
explicitly requires it.
