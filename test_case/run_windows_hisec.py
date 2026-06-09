"""
run_windows_hisec.py — Legacy Windows HiSec EDR full-workflow test suite.

Extracted from the original run_tests.py. The current Windows profile uses
EDRClient.exe 17 --show as the primary activation path, with the legacy
HisecEndpointAgent edrWidget click retained as activate_edr fallback.

Tests:
  Baseline:
    - activate_edr + visible EDRClient window

  Integration:
    - list_windows returns ok
    - is_window_open explorer.exe
    - is_window_open nonexistent
    - is_window_open no filter rejects
    - wait_window timeout

  E2E: EDR Full Workflow (Step0..Step10)
    - is_window_open(EDRClient.exe)
    - open HisecEndpointAgent.exe and verify its desktop window
    - activate_edr
    - wait_window(EDRClient.exe)
    - verify both HisecEndpointAgent.exe and EDRClient.exe desktop windows
    - connect(EDRClient.exe, auto_activate fallback)
    - dump_tree
    - screenshot
    - restore_edr
    - is_window_open(EDRClient.exe) verify
"""

from __future__ import annotations

import json
import time
from typing import Optional


def run_windows_hisec_tests(client, verbose: bool = False) -> tuple[int, int, int, list, bool]:
    passed = 0
    failed = 0
    skipped = 0
    errors: list[str] = []

    def call_tool(name, args=None):
        r = client.call_tool(name, args or {})
        if isinstance(r, str):
            r = json.loads(r)
        return r

    def check(result, key=None, expected=True):
        if key:
            actual = result.get(key)
            if expected is True and not actual:
                return False, f"{key}={actual}, expected truthy"
            if expected is False and actual:
                return False, f"{key}={actual}, expected falsy"
        if result.get("ok") is False:
            return False, f"ok=false: {result.get('error', '')}"
        return True, None

    def launch_hisec_agent_entry_window() -> dict:
        """Open the HisecEndpointAgent entry window through MCP PowerShell."""
        command = (
            "$p = 'C:\\Program Files\\HiSec-Endpoint\\core\\safra\\HisecEndpointAgent.exe'; "
            "if (-not (Test-Path $p)) { throw \"HisecEndpointAgent.exe not found: $p\" }; "
            "Start-Process -FilePath $p -ArgumentList @('cmd','ui'); "
            "Write-Output 'started'"
        )
        return call_tool("run_powershell", {"command": command, "timeout": 10})

    # ── Basic / integration tests ─────────────────────────────────
    print()
    print("=" * 60)
    print("Basic / Integration Tests")
    print("=" * 60)

    print("\n  activate_edr baseline... ", end="", flush=True)
    try:
        baseline = call_tool("activate_edr", {"wait": True, "timeout": 15.0})
        if verbose:
            print(f"\n    {json.dumps(baseline, ensure_ascii=False)[:400]}")
            print("    ", end="")
        if baseline.get("ok") is not True:
            print(f"FAIL: {baseline.get('error', 'unknown')}")
            failed += 1
            errors.append("activate_edr baseline")
        else:
            edr_window = call_tool("is_window_open", {"process_name": "EDRClient.exe"})
            if edr_window.get("ok") is True and edr_window.get("found") is True:
                print("PASS")
                passed += 1
            else:
                print(f"FAIL: EDRClient.exe not visible after activate_edr (found={edr_window.get('found')})")
                failed += 1
                errors.append("activate_edr baseline")
    except Exception as e:
        print(f"ERROR: {e}")
        failed += 1
        errors.append("activate_edr baseline")

    print("\n  Basic E2E: HisecEndpointAgent + EDRClient desktop windows... ", end="", flush=True)
    try:
        launch = launch_hisec_agent_entry_window()
        if launch.get("ok") is not True:
            print(f"FAIL: launch HisecEndpointAgent failed: {launch.get('error', launch)}")
            failed += 1
            errors.append("Basic E2E window pair")
        else:
            hisec_wait = call_tool(
                "wait_window",
                {"process_name": "HisecEndpointAgent.exe", "timeout": 15.0, "interval": 0.5},
            )
            activate = call_tool("activate_edr", {"wait": True, "timeout": 15.0})
            edr_wait = call_tool(
                "wait_window",
                {"process_name": "EDRClient.exe", "timeout": 15.0, "interval": 0.5},
            )
            hisec_ok = hisec_wait.get("ok") is not False and hisec_wait.get("found") is True
            edr_ok = (
                activate.get("ok") is True
                and edr_wait.get("ok") is not False
                and edr_wait.get("found") is True
            )
            if hisec_ok and edr_ok:
                print("PASS")
                passed += 1
            else:
                print(
                    "FAIL: "
                    f"HisecEndpointAgent found={hisec_wait.get('found')} "
                    f"EDRClient found={edr_wait.get('found')} "
                    f"activate_ok={activate.get('ok')}"
                )
                failed += 1
                errors.append("Basic E2E window pair")
    except Exception as e:
        print(f"ERROR: {e}")
        failed += 1
        errors.append("Basic E2E window pair")

    tests_integration = [
        ("list_windows returns ok",
         "list_windows", {},
         lambda r: (r.get("ok") is True and "windows" in r, r.get("error", ""))),

        ("is_window_open explorer.exe",
         "is_window_open", {"process_name": "explorer.exe"},
         lambda r: (r.get("ok") is True and "found" in r, r.get("error", ""))),

        ("is_window_open nonexistent",
         "is_window_open", {"process_name": "nonexistent_process_xyz.exe"},
         lambda r: (r.get("ok") is True and r.get("found") is False, "")),

        ("is_window_open no filter rejects",
         "is_window_open", {},
         lambda r: (r.get("ok") is False and "error" in r, "")),

        ("wait_window timeout",
         "wait_window", {"process_name": "nonexistent_xyz.exe", "timeout": 2.0, "interval": 0.3},
         lambda r: (r.get("ok") is False and r.get("error") == "timeout", "")),
    ]

    for name, tool, args, check_fn in tests_integration:
        print(f"\n  {name}... ", end="", flush=True)
        try:
            result = call_tool(tool, args)
            ok, err = check_fn(result)
            if verbose and not ok:
                print(f"\n    FAIL: {err}\n    detail: {json.dumps(result, ensure_ascii=False)[:300]}")
                print("    ", end="")
            if ok:
                print("PASS")
                passed += 1
            else:
                print(f"FAIL: {err}")
                failed += 1
                errors.append(name)
        except Exception as e:
            print(f"ERROR: {e}")
            failed += 1
            errors.append(name)

    # ── E2E tests ─────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("E2E: EDR Full Workflow")
    print("=" * 60)

    e2e_steps = [
        ("Step0: is_window_open(EDRClient.exe)",            "is_window_open", {"process_name": "EDRClient.exe"}, False),
        ("Step1: open HisecEndpointAgent entry window",     "run_powershell", {"command": "$p = 'C:\\Program Files\\HiSec-Endpoint\\core\\safra\\HisecEndpointAgent.exe'; if (-not (Test-Path $p)) { throw \"HisecEndpointAgent.exe not found: $p\" }; Start-Process -FilePath $p -ArgumentList @('cmd','ui'); Write-Output 'started'", "timeout": 10}, True),
        ("Step2: wait_window(HisecEndpointAgent.exe)",      "wait_window",    {"process_name": "HisecEndpointAgent.exe", "timeout": 15.0, "interval": 0.5}, True),
        ("Step3: activate_edr",                             "activate_edr",   {"wait": True, "timeout": 15.0}, True),
        ("Step4: wait_window(EDRClient.exe)",               "wait_window",    {"process_name": "EDRClient.exe", "timeout": 15.0, "interval": 0.5}, True),
        ("Step5: verify HisecEndpointAgent window",         "is_window_open", {"process_name": "HisecEndpointAgent.exe"}, True),
        ("Step6: verify EDRClient window",                  "is_window_open", {"process_name": "EDRClient.exe"}, True),
        ("Step7: connect(EDRClient.exe, auto_activate fallback)", "connect", {"process_name": "EDRClient.exe", "timeout": 10.0, "auto_activate": True}, True),
        ("Step8: dump_tree (max_depth=10)",                 "dump_tree",      {"max_depth": 10}, True),
        ("Step9: screenshot",                               "screenshot",     {}, True),
        ("Step10: restore_edr",                             "restore_edr",    {}, False),
        ("Step11: is_window_open verify",                   "is_window_open", {"process_name": "EDRClient.exe"}, False),
    ]

    for name, tool, args, must_pass in e2e_steps:
        print(f"\n  {name}... ", end="", flush=True)
        try:
            if tool is None:
                time.sleep(2)
                print("OK (wait 2s)")
                passed += 1
                continue
            result = call_tool(tool, args)
            if verbose:
                print(f"\n    {json.dumps(result, ensure_ascii=False)[:400].replace(chr(10), chr(10) + '    ')}")
                print("    ", end="")
            ok = result.get("ok") is not False
            if must_pass and "found" in result:
                ok = ok and result.get("found") is True
            if ok:
                if tool == "restore_edr":
                    rect = result.get("rectangle")
                    if not isinstance(rect, dict) or not all(k in rect for k in ("x", "y", "w", "h")):
                        print(f"FAIL: restore_edr missing rectangle: {result}")
                        failed += 1
                        errors.append(name)
                        continue
                extra = ""
                if "windows" in result:
                    extra = f" ({len(result.get('windows', []))} windows)"
                elif "controls" in result:
                    extra = f" ({len(result.get('controls', []))} controls)"
                elif "found" in result:
                    extra = f" (found={result.get('found')})"
                print(f"PASS{extra}")
                passed += 1
            else:
                print(f"FAIL: {result.get('error', 'unknown')}")
                failed += 1
                errors.append(name)
        except Exception as e:
            print(f"ERROR: {e}")
            failed += 1
            errors.append(name)

    ok = (failed == 0)
    return passed, failed, skipped, errors, ok
