#!/usr/bin/env python3
"""Check EDR-WD agent and target Python dependencies.

This script is intentionally read-only. It validates local imports, optional
test imports, target config, Paramiko SSH login, and remote target imports
without deploying or restarting MCP.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shlex
import sys
from dataclasses import dataclass, asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.ssh_runner import run_ssh, SSHAuthError  # noqa: E402
from agent.target_config import TargetConfig  # noqa: E402


LOCAL_AGENT_IMPORTS = {
    "fastmcp": "fastmcp",
    "paramiko": "paramiko",
    "psutil": "psutil",
    "Pillow": "PIL",
    "PyAutoGUI": "pyautogui",
}

LOCAL_TEST_IMPORTS = {
    "pytest": "pytest",
    "httpx": "httpx",
}

TARGET_CORE_IMPORTS = {
    "fastmcp": "fastmcp",
    "psutil": "psutil",
    "Pillow": "PIL",
}

TARGET_GUI_IMPORTS = {
    "windows": {
        "pywinauto": "pywinauto",
        "PyAutoGUI": "pyautogui",
    },
    "macos": {
        "PyAutoGUI": "pyautogui",
    },
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""


def _module_ok(import_name: str) -> bool:
    return importlib.util.find_spec(import_name) is not None


def _check_local_imports(include_test: bool) -> list[Check]:
    checks: list[Check] = [
        Check(
            "local.python.version",
            sys.version_info >= (3, 9),
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "Use Python >= 3.9.",
        )
    ]
    for package, import_name in LOCAL_AGENT_IMPORTS.items():
        checks.append(
            Check(
                f"local.import.{package}",
                _module_ok(import_name),
                import_name,
                "Install project dependencies: python -m pip install -e .",
            )
        )
    if include_test:
        for package, import_name in LOCAL_TEST_IMPORTS.items():
            checks.append(
                Check(
                    f"local.test_import.{package}",
                    _module_ok(import_name),
                    import_name,
                    "Install test dependencies: python -m pip install -r test_case/requirements_test.txt",
                )
            )
    return checks


def _load_target(target_name: str | None) -> tuple[TargetConfig, str | None, dict | None, list[Check]]:
    checks: list[Check] = []
    try:
        tc = TargetConfig()
    except Exception as exc:
        checks.append(Check("config.load", False, str(exc), "Create config/targets.local.json."))
        return TargetConfig.__new__(TargetConfig), None, None, checks

    try:
        errors = tc.validate()
    except Exception as exc:
        errors = [str(exc)]
    checks.append(
        Check(
            "config.validate",
            not errors,
            f"{len(errors)} error(s)" if errors else "ok",
            "; ".join(errors[:3]) if errors else "",
        )
    )

    try:
        resolved_name = target_name or tc.get_default_target()
        if not resolved_name:
            raise KeyError("default_target is not configured")
        cfg = tc.get_resolved_target(resolved_name)
        checks.append(Check("target.resolve", True, resolved_name))
        return tc, resolved_name, cfg, checks
    except Exception as exc:
        checks.append(
            Check(
                "target.resolve",
                False,
                str(exc),
                "Pass --target <name> or configure default_target.",
            )
        )
        return tc, None, None, checks


def _quote_cmd_arg(value: str, platform_name: str) -> str:
    if platform_name == "windows":
        escaped = value.replace('"', r'\"')
        return f'"{escaped}"'
    return shlex.quote(value)


def _remote_python_cmd(python_path: str, code: str, platform_name: str) -> str:
    py = _quote_cmd_arg(python_path, platform_name)
    if platform_name == "windows":
        escaped = code.replace('"', r'\"')
        return f'{py} -c "{escaped}"'
    return f"{py} -c {shlex.quote(code)}"


def _remote_import_code(imports: dict[str, str]) -> str:
    lines = ["missing=[]"]
    for package, import_name in imports.items():
        lines.append(
            "try:\n"
            f"    __import__({import_name!r})\n"
            "except Exception:\n"
            f"    missing.append({package!r})"
        )
    lines.append(
        "import sys\n"
        "print('missing=' + ','.join(missing))\n"
        "sys.exit(1 if missing else 0)"
    )
    return "\n".join(lines)


def _check_target(target_name: str | None, include_test: bool, platform_override: str) -> list[Check]:
    _tc, resolved_name, cfg, checks = _load_target(target_name)
    if not cfg or not resolved_name:
        return checks

    configured_platform = cfg.get("platform", "windows")
    platform_name = configured_platform if platform_override == "auto" else platform_override
    ssh_cfg = cfg.get("ssh", {})
    mcp_cfg = cfg.get("mcp", {})
    python_path = (
        cfg.get("windows", {}).get("python_path")
        if platform_name == "windows"
        else cfg.get("macos", {}).get("python_path")
    ) or "python"

    checks.append(
        Check(
            "target.platform",
            platform_name in TARGET_GUI_IMPORTS,
            f"{platform_name} (configured={configured_platform}, requested={platform_override})",
            "Use --platform windows or --platform macos, or set target.platform correctly.",
        )
    )

    checks.append(
        Check(
            "target.connect_mode",
            mcp_cfg.get("connect_mode") in {"direct", "tunnel", "local"},
            str(mcp_cfg.get("connect_mode")),
            "Use mcp.connect_mode direct, tunnel, or local.",
        )
    )

    try:
        rc, out = run_ssh(ssh_cfg, "hostname", timeout=15)
        checks.append(
            Check(
                "target.ssh.paramiko",
                rc == 0,
                "command ran" if rc == 0 else f"exit={rc}",
                "Fix ssh.host / ssh.auth username-password in config.",
            )
        )
    except SSHAuthError as exc:
        checks.append(Check("target.ssh.paramiko", False, str(exc), "Fix target SSH auth config."))
        return checks
    except Exception as exc:
        checks.append(Check("target.ssh.paramiko", False, str(exc), "Check network, SSH service, and config."))
        return checks

    version_cmd = _remote_python_cmd(
        python_path,
        "import sys; print(sys.executable); print(sys.version)",
        platform_name,
    )
    rc, out = run_ssh(ssh_cfg, version_cmd, timeout=20)
    checks.append(
        Check(
            "target.python.version",
            rc == 0,
            "ok" if rc == 0 else f"exit={rc}",
            f"Fix configured Python path: {python_path!r}.",
        )
    )
    if rc != 0:
        return checks

    core_cmd = _remote_python_cmd(
        python_path,
        _remote_import_code(TARGET_CORE_IMPORTS),
        platform_name,
    )
    rc, out = run_ssh(ssh_cfg, core_cmd, timeout=30)
    missing = _missing_from_output(out)
    checks.append(
        Check(
            f"target.{platform_name}.import.core",
            rc == 0 and not missing,
            "ok" if not missing else f"missing={','.join(missing)}",
            "Install target core dependencies in the configured Python runtime.",
        )
    )

    gui_imports = TARGET_GUI_IMPORTS.get(platform_name, {})
    if gui_imports:
        gui_cmd = _remote_python_cmd(
            python_path,
            _remote_import_code(gui_imports),
            platform_name,
        )
        rc, out = run_ssh(ssh_cfg, gui_cmd, timeout=30)
        missing = _missing_from_output(out)
        checks.append(
            Check(
                f"target.{platform_name}.import.gui",
                rc == 0 and not missing,
                "ok" if not missing else f"missing={','.join(missing)}",
                f"Install {platform_name} GUI automation dependencies.",
            )
        )

    if include_test:
        checks.append(
            Check(
                "target.test_deps",
                True,
                "not required on target",
                "Install test deps on the runner/agent, not normally on target.",
            )
        )

    return checks


def _missing_from_output(output: str) -> list[str]:
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith("missing="):
            rest = line.split("=", 1)[1].strip()
            return [item for item in rest.split(",") if item]
    return []


def _print_text(checks: list[Check]) -> None:
    width = max((len(c.name) for c in checks), default=10)
    for check in checks:
        mark = "OK" if check.ok else "FAIL"
        detail = f" — {check.detail}" if check.detail else ""
        print(f"[{mark}] {check.name:<{width}}{detail}")
        if not check.ok and check.hint:
            print(f"      hint: {check.hint}")
    ok = all(c.ok for c in checks)
    print()
    print(f"Summary: {'PASS' if ok else 'FAIL'} ({sum(c.ok for c in checks)}/{len(checks)} checks)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check EDR-WD dependencies without deploying MCP.")
    parser.add_argument("--target", help="Target name from config/targets.local.json")
    parser.add_argument(
        "--platform",
        choices=("auto", "windows", "macos"),
        default="auto",
        help="Target dependency profile to check (default: auto from target config).",
    )
    parser.add_argument(
        "--scope",
        choices=("agent", "target", "all"),
        default="all",
        help="Checks to run (default: all).",
    )
    parser.add_argument("--include-test", action="store_true", help="Also check local pytest/httpx.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args()

    checks: list[Check] = []
    if args.scope in {"agent", "all"}:
        checks.extend(_check_local_imports(args.include_test))
    if args.scope in {"target", "all"}:
        checks.extend(_check_target(args.target, args.include_test, args.platform))

    if args.json:
        print(json.dumps({"ok": all(c.ok for c in checks), "checks": [asdict(c) for c in checks]}, indent=2))
    else:
        print(f"EDR-WD dependency check ({platform.system()} agent)")
        print(f"repo: {ROOT}")
        print()
        _print_text(checks)

    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
