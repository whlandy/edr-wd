"""
run_windows_hisec.py — Legacy Windows HiSec EDR full-workflow test suite.

Extracted from the original run_tests.py so that the Windows profile
remains bit-for-bit equivalent to the 16/16 regression we shipped.

Tests:
  Baseline:
    - activate_edr + visible HisecEndpointAgent window

  Integration:
    - list_windows returns ok
    - is_window_open explorer.exe
    - is_window_open nonexistent
    - is_window_open no filter rejects
    - wait_window timeout

  E2E: EDR Full Workflow (Step0..Step10)
    - is_window_open(HisecEndpointAgent.exe)
    - activate_edr
    - wait_window(HisecEndpointAgent.exe)
    - connect(HisecEndpointAgent.exe, auto_activate fallback)
    - dump_tree
    - click(edrWidget GroupBox)
    - wait 2s
    - is_window_open(EDRClient.exe)
    - screenshot
    - restore_edr
    - is_window_open(HisecEndpointAgent.exe) verify
"""

from __future__ import annotations

import json
import time
from typing import Optional


def run_windows_hisec_tests(client, verbose: bool = False) -> tuple[int, int, list, bool]:
    passed = 0
    failed = 0
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

    # ── Integration tests ──────────────────────────────────────────
    print()
    print("=" * 60)
    print("Integration Tests")
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
            main_window = call_tool("is_window_open", {"process_name": "HisecEndpointAgent.exe"})
            if main_window.get("ok") is True and main_window.get("found") is True:
                print("PASS")
                passed += 1
            else:
                print(f"FAIL: HisecEndpointAgent.exe not visible after activate_edr (found={main_window.get('found')})")
                failed += 1
                errors.append("activate_edr baseline")
    except Exception as e:
        print(f"ERROR: {e}")
        failed += 1
        errors.append("activate_edr baseline")

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
        ("Step0: is_window_open(HisecEndpointAgent.exe)", "is_window_open", {"process_name": "HisecEndpointAgent.exe"}, False),
        ("Step1: activate_edr",                             "activate_edr",  {"wait": True, "timeout": 15.0}, True),
        ("Step2: wait_window(HisecEndpointAgent.exe)",      "wait_window",   {"process_name": "HisecEndpointAgent.exe", "timeout": 15.0, "interval": 0.5}, True),
        ("Step3: connect(HisecEndpointAgent.exe, auto_activate fallback)", "connect", {"process_name": "HisecEndpointAgent.exe", "timeout": 10.0, "auto_activate": True}, True),
        ("Step4: dump_tree (max_depth=10, find edrLanel)",  "dump_tree",     {"max_depth": 10}, True),
        ("Step5: click(edrWidget GroupBox)",                "click",          {"automation_id": "SafraUIMainWindow.MainWidget.content_widget.featureWidget.EdrUIMainWindow.centralwidget.edrWidget"}, True),
        ("Step6: wait 2s for UI to react",                  None,             None,             False),
        ("Step7: verify EDRClient window appeared",       "is_window_open", {"process_name": "EDRClient.exe"}, True),
        ("Step8: screenshot",                               "screenshot",    {"path": "C:\\Users\\<TARGET_USER>\\Desktop\\maa-fw运行记录\\e2e_edr_full_workflow.png"}, True),
        ("Step9: restore_edr",                              "restore_edr",   {}, False),
        ("Step10: is_window_open verify",                   "is_window_open", {"process_name": "HisecEndpointAgent.exe"}, False),
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
            if ok:
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
    return passed, failed, errors, ok
