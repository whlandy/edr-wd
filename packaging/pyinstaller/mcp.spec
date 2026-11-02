# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the agent-side `mcp` executable.

Builds a one-file agent CLI facade over the repository's root `agent/` and
`target/` source of truth (packaging/DESIGN.md: no duplicated package tree).

Runtime data layout inside the frozen app:

    _MEIPASS/
      target/            full deploy payload (explicit deploy/install only;
                         scp_dir_to falls back to enumerating it without git)
      config/targets.example.json   documentation-only example config

`agent/` and `target/` Python modules are frozen via the pathex/imports
machinery, not copied as loose `datas` — only data files that the runtime
reads from disk (`Path(__file__)`-relative) are bundled.

Windows builds produce `mcp.exe`; POSIX builds produce `mcp`.
"""

import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
REPO_ROOT = SPEC_DIR.parent.parent

agent_pkg = REPO_ROOT / "agent"
target_pkg = REPO_ROOT / "target"
if not (agent_pkg / "cli.py").exists() or not (target_pkg / "server.py").exists():
    raise SystemExit(
        "mcp.spec must run from packaging/pyinstaller/ inside the edr-wd "
        "repository (agent/ and target/ must exist at the repo root)."
    )

pathex = [str(REPO_ROOT), str(agent_pkg), str(target_pkg)]

hiddenimports = [
    # agent.cli imports these lazily or via subpackages that PyInstaller's
    # static analysis can miss.
    "agent.lifecycle.windows",
    "agent.lifecycle.macos",
    "agent.lifecycle.payload",
    "agent.recording.maa_export",
    "agent.recording.generate_pytest",
    "agent.subagent.target_agent",
    "agent.products.edrclient",
    "agent.products.generic",
    # target-side modules imported by agent code.
    "target.recording.models",
    "target.recording.region_assertion",
    "target.action_dispatcher",
    "target.action_catalog",
    "target.windowing.observation",
    "target.protocol_models",
    "target.observations",
    "target.time_assertions",
    "target.product_profiles",
    "target.file_transfer",
    # ssh_runner's paramiko backend
    "paramiko",
]

# Data files the frozen runtime reads from disk via __file__-relative paths.
datas = [
    # Full target/ deploy payload for the EXPLICIT deploy/install commands.
    # Exclusions below strip secrets, runtime artifacts, caches, and tests.
    (str(target_pkg), "target"),
    # Documentation-only example config (--list/--guide use it when no local
    # config exists). Local config is never bundled.
    (str(REPO_ROOT / "config" / "targets.example.json"), "config"),
]

# Never bundle credential-bearing or generated content, regardless of what a
# future change adds to datas. Mirrors packaging/DESIGN.md "must not include".
datas = [
    (src, dest)
    for (src, dest) in datas
    if not src.endswith("targets.local.json")
]

excludes = [
    "pytest",
    "setuptools",
    "pip",
    "wheel",
    "tkinter",
    "PyAutoGUI",
    "pywinauto",
    "pyobjc",
    "Quartz",
    "AppKit",
    "edr_wd",
    "mcp_edr_wd",
]

a = Analysis(
    [str(agent_pkg / "cli.py")],
    pathex=pathex,
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

# Defense-in-depth: strip what packaging/DESIGN.md forbids from the bundle,
# even if a future datas change or dependency pull-in adds it: tests, caches,
# runtime logs/screenshots/tmp, git metadata, and credential-bearing configs.
forbidden_names = {
    "config.targets.local.json",
    "config.targets.json",
    "config.test_machines.json",
    "targets.local.json",
}

def _is_forbidden(rel_posix: str) -> bool:
    parts = rel_posix.split("/")
    if any(
        seg in ("__pycache__", ".git", "tests", "tmp", "logs", "screenshots")
        or seg.endswith(".egg-info")
        for seg in parts[:-1]
    ):
        return True
    name = parts[-1]
    if name.endswith((".pyc", ".pyo", ".log", ".pid")):
        return True
    dotted = rel_posix.replace("/", ".")
    return dotted in forbidden_names

kept = []
for dep in a.datas:
    src = dep[0].replace("\\", "/")
    rel = src
    marker = "/target/"
    if marker in src:
        rel = "target/" + src.split(marker, 1)[1]
    elif src.startswith("target/"):
        rel = src
    if _is_forbidden(rel):
        continue
    kept.append(dep)
a.datas = kept

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="mcp",
)
