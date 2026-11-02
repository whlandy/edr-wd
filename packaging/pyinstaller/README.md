# PyInstaller Packaging

Builds the agent-side executable facade (`mcp.exe` on Windows, `mcp` on
POSIX) over the repository's root `agent/` + `target/` source of truth —
no duplicated package tree (see `../DESIGN.md`).

Delivered files:

```text
packaging/pyinstaller/
  README.md     ← this file
  mcp.spec      ← one-file... onedir spec: entry agent/cli.py, output mcp
  build.sh      ← POSIX build (output build-packaging/dist/mcp/mcp)
  build.ps1     ← Windows build (output build-packaging\dist\mcp\mcp.exe)
  smoke.sh      ← POSIX smoke checks
  smoke.ps1     ← Windows smoke checks
```

## Spec contract (mcp.spec)

- entry point: `agent/cli.py` — the same `edr-wd` console-script `main`.
- output name: `mcp` (Windows bootloader appends `.exe`), onedir COLLECT
  layout so bundle contents stay auditable before a release.
- paths derive from `SPECPATH` — no drive-letter literals.
- `datas` bundles:
  - `target/` — the deploy payload for the EXPLICIT `deploy`/`install`
    commands (`scp_dir_to` enumerates it in a frozen app, where git is
    unavailable). Secret-bearing configs, tests, caches, logs, tmp,
    screenshots, and `.git` are stripped by a source filter plus a
    post-Analysis `_is_forbidden` filter (locked by
    `test_case/test_recording/test_packaging_contract.py`).
  - `config/targets.example.json` — documentation-only example.
- `hiddenimports` covers the importlib-based factories
  (`agent.lifecycle.{windows,macos}` via `get_lifecycle`) and lazy imports
  (`agent.recording.maa_export`, `agent.recording.generate_pytest`).
- `excludes` drops target-side-only GUI deps (`pywinauto`, `PyAutoGUI`,
  `tkinter`, `pyobjc`) that agent code references only inside generated
  remote verification commands — never as module imports. Pillow stays
  bundled (agent recording code imports PIL).

## Build

```bash
# POSIX
packaging/pyinstaller/build.sh            # add --clean for a fresh build
packaging/pyinstaller/smoke.sh
```

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File packaging\pyinstaller\build.ps1 [-Clean]
powershell -ExecutionPolicy Bypass -File packaging\pyinstaller\smoke.ps1
```

Build outputs go to `build-packaging/` (gitignored) — never the repo root.

## Smoke checks

1. `--help` starts and prints usage.
2. `config --list` runs from the bundle (absent local config is allowed).
3. no-write probe: `status --target __smoke_nonexistent_target__` returns
   without any deploy/install attempt.
4. `products` lists built-in product profiles offline.
5. bundle exclusions: no `targets.local.json`, logs, pid files, `.git`,
   `__pycache__`, or `target/tests` in the output.

## CI

`.github/workflows/ci.yml` builds and smokes this bundle on every push to
`main` and every pull request:

- **macOS job** — offline unit suite (`python -m pytest`, the repo default
  marker selection needs no target machine), then `build.sh` + `smoke.sh`.
- **Windows job** — packaging contract regressions, then `build.ps1` +
  `smoke.ps1` (this is where `mcp.exe` is produced and verified).

The workflow does not run live-target or e2e tests and never deploys
anywhere.

## Lock file policy

`uv.lock` is intentionally absent: the current build path is plain
`pip + PyInstaller` (see `../DESIGN.md` decision). Add it only if the
project standardizes on `uv` for release builds.

## What this does NOT do

- No auto-deploy or auto-install from normal CLI/tool paths. `deploy` and
  `install` remain explicit write commands of `agent/cli.py`.
- No local credentials in the bundle; `config/targets.local.json` is
  filtered by construction.
- No duplicate `agent/`/`target/` source trees; the spec imports the repo
  source of truth at build time.
