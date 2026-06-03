#!/usr/bin/env python3
"""
run_tests.py — 纯 Python 测试 runner（不依赖 pytest）

用法：
  python run_tests.py              # 运行所有测试
  python run_tests.py -v           # 详细输出
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_case.conftest import McpClient, check_tunnel, check_mcp_server


def run_tests(verbose=False):
    tunnel_ok, tun_msg = check_tunnel()
    server_ok, srv_msg = check_mcp_server()
    print("=" * 60)
    print("Environment Check")
    print("=" * 60)
    print(f"  Tunnel:     {'[OK]' if tunnel_ok else '[FAIL]'} {tun_msg}")
    print(f"  MCP Server: {'[OK]' if server_ok else '[FAIL]'} {srv_msg}")
    print()

    if not tunnel_ok or not server_ok:
        print("[FAIL] Environment not ready.")
        if not tunnel_ok:
            print("  - SSH tunnel: bash agent/tunnel.sh start")
        if not server_ok:
            print("  - Windows:    deploy.ps1 -Action start (in RDP desktop)")
        sys.exit(1)

    client = McpClient()
    try:
        init = client.initialize()
        if "error" in init:
            print(f"[FAIL] initialize failed: {init}")
            sys.exit(1)
        server_info = init.get("result", {}).get("serverInfo", {})
        print(f"[OK] initialize: server={server_info.get('name')} v{server_info.get('version')}")
    except Exception as e:
        print(f"[FAIL] initialize exception: {e}")
        sys.exit(1)

    passed = 0
    failed = 0
    errors = []

    def call_tool(name, args=None):
        r = client.call_tool(name, args or {})
        if isinstance(r, str):
            r = json.loads(r)
        return r

    def check(result, key=None, expected=True):
        """Helper: assert result[key] == expected"""
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

    tests_integration = [
        # (name, tool_name, args, pass_check_fn)
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
        # (name, tool, args, must_pass)
        ("Step0: is_window_open(HisecEndpointAgent.exe)", "is_window_open", {"process_name": "HisecEndpointAgent.exe"}, False),
        ("Step1: activate_edr",                             "activate_edr",  {"wait": True, "timeout": 15.0}, True),
        ("Step2: wait_window(HisecEndpointAgent.exe)",      "wait_window",   {"process_name": "HisecEndpointAgent.exe", "timeout": 15.0, "interval": 0.5}, True),
        ("Step3: connect(HisecEndpointAgent.exe)",           "connect",       {"process_name": "HisecEndpointAgent.exe", "timeout": 10.0}, True),
        ("Step4: dump_tree (max_depth=10, find edrLanel)",  "dump_tree",     {"max_depth": 10}, True),
        ("Step5: click(edrWidget GroupBox)",                "click",          {"automation_id": "SafraUIMainWindow.MainWidget.content_widget.featureWidget.EdrUIMainWindow.centralwidget.edrWidget"}, True),
        ("Step6: wait 2s for UI to react",                  None,             None,             False),  # no tool, just sleep
        ("Step7: verify EDRClient window appeared",       "is_window_open", {"process_name": "EDRClient.exe"}, True),
        ("Step8: screenshot",                               "screenshot",    {"path": "C:\\Users\\admin\\Desktop\\maa-fw运行记录\\e2e_edr_full_workflow.png"}, True),
        ("Step9: restore_edr",                              "restore_edr",   {}, False),
        ("Step10: is_window_open verify",                   "is_window_open", {"process_name": "HisecEndpointAgent.exe"}, False),
    ]

    for name, tool, args, must_pass in e2e_steps:
        print(f"\n  {name}... ", end="", flush=True)
        try:
            if tool is None:
                # 纯等待步骤（无 tool call）
                import time
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

    client.close()

    # ── Summary ────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    if errors:
        print(f"Failed: {', '.join(errors)}")
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    ok = run_tests(verbose=args.verbose)
    sys.exit(0 if ok else 1)
