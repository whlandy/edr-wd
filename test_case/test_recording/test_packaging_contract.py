"""Packaging contract regression tests (batch 9 remainder).

Locks the packaging/pyinstaller/ directory contract from packaging/README.md
and packaging/DESIGN.md:

- the five deliverable files exist and build.sh/smoke.sh are valid bash
- the spec bundles the deploy payload and example config as datas
- the spec never bundles credential-bearing local config, by construction
  (source-filter) and by defense-in-depth (post-Analysis filter)
- the spec excludes Windows-only GUI deps that agent code never imports
  (they appear only inside generated remote commands)
- build artifacts stay out of the repository root (build-packaging/)
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYINSTALLER_DIR = REPO_ROOT / "packaging" / "pyinstaller"
SPEC_PATH = PYINSTALLER_DIR / "mcp.spec"


def _spec_text() -> str:
    return SPEC_PATH.read_text(encoding="utf-8")


class TestDirectoryContract:
    def test_all_five_deliverable_files_exist(self):
        for name in ("mcp.spec", "build.ps1", "build.sh", "smoke.ps1", "smoke.sh"):
            path = PYINSTALLER_DIR / name
            assert path.exists(), f"missing packaging deliverable: {name}"

    def test_root_has_no_packaging_scripts(self):
        for name in ("mcp.spec", "mcp-edr-wd.spec", "build.ps1", "build.sh",
                     "smoke.ps1", "smoke.sh"):
            assert not (REPO_ROOT / name).exists(), (
                f"{name} must live in packaging/pyinstaller/, not the repo root "
                "(packaging/README.md directory contract)"
            )

    def test_shell_scripts_are_valid_bash(self):
        if shutil.which("bash") is None:
            pytest.skip("bash unavailable")
        for name in ("build.sh", "smoke.sh"):
            proc = subprocess.run(
                ["bash", "-n", str(PYINSTALLER_DIR / name)],
                capture_output=True, text=True,
            )
            assert proc.returncode == 0, f"{name} bash syntax: {proc.stderr}"

    def test_powershell_scripts_parse(self):
        # AST-parse the .ps1 files only when PowerShell is unavailable is not
        # possible; instead assert they are non-trivial and carry the param
        # contract. A full parse happens on Windows CI.
        for name in ("build.ps1", "smoke.ps1"):
            text = (PYINSTALLER_DIR / name).read_text(encoding="utf-8")
            assert "[CmdletBinding()]" in text, f"{name} missing CmdletBinding"
            assert "param(" in text, f"{name} missing param block"
            assert "ErrorActionPreference" in text, f"{name} must stop on errors"


class TestSpecContract:
    def test_spec_is_valid_python_ast(self):
        ast.parse(_spec_text())

    def test_spec_bundles_deploy_payload_and_example_config(self):
        text = _spec_text()
        assert '(str(target_pkg), "target")' in text, (
            "spec must bundle target/ as datas for the explicit deploy/install path"
        )
        assert "targets.example.json" in text, (
            "spec must bundle the documentation-only example config"
        )

    def test_spec_source_filter_rejects_local_config(self):
        text = _spec_text()
        assert 'endswith("targets.local.json")' in text, (
            "spec's datas source-filter must exclude credential-bearing local "
            "config at its source (not only via the post-Analysis filter)"
        )

    def test_spec_post_analysis_filter_names_forbidden_files(self):
        text = _spec_text()
        assert "forbidden_names" in text
        for secret in ("targets.local.json", "test_machines.json"):
            assert secret in text, (
                f"defense-in-depth filter must explicitly list {secret}"
            )

    def test_spec_excludes_gui_deps_agent_never_imports(self):
        # pywinauto/PyAutoGUI/tkinter are target-side runtime deps; agent code
        # references them only inside generated remote verification commands.
        text = _spec_text()
        for excluded in ("PyAutoGUI", "pywinauto", "tkinter"):
            assert f'"{excluded}"' in text
        # But Pillow IS imported by agent recording code — must NOT be excluded.
        assert '"PIL"' not in text and "Pillow" not in text

    def test_spec_uses_portable_paths(self):
        text = _spec_text()
        assert "SPECPATH" in text, "spec must derive paths from SPECPATH"
        assert not re.search(r'"[A-Za-z]:\\\\', text), (
            "spec must not hardcode Windows drive-letter path literals"
        )

    def test_spec_output_executable_is_mcp(self):
        text = _spec_text()
        assert 'name="mcp"' in text, "EXE must be named mcp (mcp.exe on Windows)"

    def test_spec_uses_onedir_layout(self):
        text = _spec_text()
        assert "COLLECT" in text and "exclude_binaries=True" in text, (
            "spec must use onedir COLLECT so datas stay editable and excluded "
            "content can be audited before release"
        )

    def test_spec_filter_actually_strips_forbidden_datas(self):
        """Execute the spec's real filtering logic against PyInstaller stubs.

        Grepping the source is not enough: a silently disabled filter
        (e.g. `return False` injected into _is_forbidden) must fail here.
        """
        collected: dict = {}

        class _Analysis:
            def __init__(self, scripts, pathex, binaries, datas, hiddenimports,
                         hookspath, runtime_hooks, excludes, noarchive):
                collected["hiddenimports"] = hiddenimports
                expanded = []
                for src, dest in datas:
                    p = Path(src)
                    if p.is_dir():
                        for f in p.rglob("*"):
                            if f.is_file():
                                expanded.append(
                                    (str(f), str(Path(dest) / f.relative_to(p))))
                    else:
                        expanded.append((str(src), dest))
                self.datas = expanded
                self.pure: list = []
                self.scripts: list = []
                self.binaries: list = []

        class _PYZ:
            def __init__(self, pure):
                pass

        class _EXE:
            def __init__(self, *args, **kw):
                collected["exe_name"] = kw.get("name")

        class _COLLECT:
            def __init__(self, *args, **kw):
                collected["collect_name"] = kw.get("name")

        spec_globals = {
            "SPECPATH": str(PYINSTALLER_DIR),
            "Analysis": _Analysis, "PYZ": _PYZ, "EXE": _EXE,
            "COLLECT": _COLLECT,
        }
        exec(compile(_spec_text(), "mcp.spec", "exec"), spec_globals)
        a = spec_globals["a"]
        names = [d[0].replace("\\", "/") for d in a.datas]

        forbidden = [
            n for n in names
            if "__pycache__" in n
            or n.endswith((".pyc", ".pyo", ".log", ".pid"))
            or "/tests/" in n
            or "/.git/" in n
            or "/tmp/" in n
            or "targets.local.json" in n
            or "test_machines.json" in n
        ]
        assert forbidden == [], (
            f"spec filter must strip forbidden datas, kept: {forbidden[:5]}"
        )

        required = (
            "server.py",
            "pywinauto_client.py",
            "product_specs/apps.json",
            "scripts/start_server.ps1",
            "scripts/install_task.ps1",
            "automation/windows_pywinauto.py",
            "windowing/observation.py",
        )
        for req in required:
            assert any(n.endswith(req) for n in names), (
                f"deploy payload must include {req}"
            )
        assert any(n.endswith("targets.example.json") for n in names), (
            "documentation example config must be bundled"
        )
        assert collected["exe_name"] == "mcp"

    def test_spec_hiddenimports_cover_dynamic_dispatch(self):
        """get_lifecycle/create_backend resolve modules via importlib at
        runtime; PyInstaller's static analysis misses those strings."""
        text = _spec_text()
        for module in ("agent.lifecycle.windows", "agent.lifecycle.macos",
                       "target.recording.models", "target.action_dispatcher"):
            assert f'"{module}"' in text


class TestNoWriteSmokeContract:
    def test_smoke_scripts_probe_status_without_writes(self):
        for name in ("smoke.sh", "smoke.ps1"):
            text = (PYINSTALLER_DIR / name).read_text(encoding="utf-8")
            assert "status --target" in text, (
                f"{name} must include the no-write status probe"
            )
            assert "targets.local.json" in text, (
                f"{name} must assert the secret config is absent from the bundle"
            )

    def test_build_scripts_do_not_deploy_or_install(self):
        for name in ("build.sh", "build.ps1", "smoke.sh", "smoke.ps1"):
            text = (PYINSTALLER_DIR / name).read_text(encoding="utf-8")
            assert "deploy_target" not in text
            assert "install_target" not in text
