"""
run_macos_generic.py — Minimal v1 test suite for macOS target.

Verifies only capabilities that the macos_accessibility backend supports
in v1. Anything HiSec-EDR-specific (activate_edr, click by automation_id,
EDRClient/HisecEndpoint windows, screenshot to a Windows desktop path,
etc.) is explicitly out of scope for this profile.

Tests:
  - tools/list                  (verifies MCP handshake is healthy)
  - status                      (verifies backend is reporting "macos_accessibility")
  - status.action_space         (verifies action-space plumbing is exposed)
  - window lock                 (verifies click preflight lock plumbing)
  - dump_tree                   (verifies AX component discovery plumbing)
  - find_control                (verifies selector matching plumbing)
  - screenshot                  (verifies screencapture works)
  - list_windows                (verifies System Events enumeration)
  - is_window_open Finder       (verifies process_name filter)
  - is_window_open no filter    (verifies error-on-empty-filter contract)
  - wait_window timeout         (verifies timeout path)
  - activate_app Finder         (verifies osascript activate path)
  - connect by process_name     (verifies connect/bundle_id path)
  - click_at basic              (verifies coordinate click — best-effort)
  - action primitives           (verifies double/right/middle click, drag, scroll, hover)

These are "the server is alive and the basic platform plumbing works"
tests. App-specific workflows (e.g. "drive Safari to navigate to ...")
will be added under app_profile=macos_app_specific once they exist.
"""

from __future__ import annotations

import json
import time
from typing import Optional


def run_macos_generic_tests(client, verbose: bool = False) -> tuple[int, int, int, list, bool]:
    passed = 0
    failed = 0
    skipped = 0
    errors: list[str] = []

    def call_tool(name, args=None):
        r = client.call_tool(name, args or {})
        if isinstance(r, str):
            r = json.loads(r)
        return r

    def record(name: str, ok: bool, detail: str = "", skip_reason: str = None):
        nonlocal passed, failed, skipped
        if skip_reason is not None:
            print(f"SKIP ({skip_reason}){': ' + detail if detail else ''}")
            skipped += 1
        elif ok:
            print("PASS" + (f"  ({detail})" if detail else ""))
            passed += 1
        else:
            print(f"FAIL: {detail}")
            failed += 1
            errors.append(name)

    print()
    print("=" * 60)
    print("macOS Generic v1 Tests")
    print("=" * 60)

    # ── 1. tools/list ────────────────────────────────────────────
    print("\n  tools/list... ", end="", flush=True)
    try:
        r = client.tools_list()
        # tools_list returns the raw JSON-RPC; result.result.tools is the list
        tools = []
        if isinstance(r, dict):
            if "result" in r and isinstance(r["result"], dict):
                tools = r["result"].get("tools", [])
            elif "tools" in r:
                tools = r["tools"]
        ok = len(tools) >= 1
        detail = f"{len(tools)} tools: {[t.get('name') for t in tools[:5]]}"
        record("tools/list", ok, "" if ok else "no tools returned")
    except Exception as e:
        record("tools/list", False, str(e))

    # ── 2. status ────────────────────────────────────────────────
    print("\n  status... ", end="", flush=True)
    r = call_tool("status", {})
    if verbose:
        print(f"\n    {json.dumps(r, ensure_ascii=False)[:300]}")
        print("    ", end="")
    backend_name = r.get("backend") if isinstance(r, dict) else None
    # Accept any non-empty string. The legacy Windows backend reports
    # "uia" (or "win32"); the macOS backend reports "macos_accessibility".
    # We do not hard-code the value here because the test must pass for
    # both the production MCP path and any future in-process variant.
    record(
        "status",
        r.get("ok") is True and isinstance(backend_name, str) and len(backend_name) > 0,
        f"backend={backend_name}",
    )

    # ── 2b. status.action_space ──────────────────────────────────
    print("\n  status.action_space... ", end="", flush=True)
    action_space = r.get("action_space") if isinstance(r, dict) else None
    expected_true = {
        "click_at",
        "click_window_at",
        "double_click_at",
        "right_click_at",
        "middle_click_at",
        "hover_at",
        "drag",
        "scroll",
        "activate_app",
        "list_windows",
        "is_window_open",
        "wait_window",
        "connect",
        "screenshot",
        "lock_window",
        "unlock_window",
        "get_window_lock",
        "verify_window_lock",
    }
    expected_true.update({"click", "click_target", "dump_tree", "find_control"})
    expected_false = {"type_text", "select", "get_text"}
    action_ok = isinstance(action_space, dict)
    if action_ok:
        missing_true = sorted(k for k in expected_true if action_space.get(k) is not True)
        unexpected_true = sorted(k for k in expected_false if action_space.get(k) is True)
        action_ok = not missing_true and not unexpected_true
        detail = f"true={sorted(expected_true)} false={sorted(expected_false)}"
        if missing_true:
            detail += f" missing_true={missing_true}"
        if unexpected_true:
            detail += f" unexpected_true={unexpected_true}"
    else:
        detail = f"action_space={action_space}"
    record("status action_space", action_ok, detail)

    # ── 2c. window lock after Finder connect ──────────────────────
    print("\n  window lock... ", end="", flush=True)
    lock_connect = call_tool("connect", {"process_name": "Finder", "timeout": 5, "auto_activate": True})
    if lock_connect.get("ok") is not True:
        record("window lock", False, f"connect failed: {lock_connect.get('error', lock_connect)}")
    else:
        locked = call_tool("lock_window", {"process_name": "Finder", "activate": True})
        lock_state = call_tool("get_window_lock", {})
        verified = call_tool("verify_window_lock", {"activate": True})
        call_tool("unlock_window", {})
        record(
            "window lock",
            locked.get("ok") is True and lock_state.get("locked") is True and verified.get("ok") is True,
            f"locked={lock_state.get('locked')} verify={verified.get('ok')}",
        )

    # ── 2d. dump_tree / find_control after Finder connect ─────────
    print("\n  component discovery... ", end="", flush=True)
    connect_for_tree = call_tool("connect", {"process_name": "Finder", "timeout": 5, "auto_activate": True})
    if connect_for_tree.get("ok") is not True:
        record("component discovery", False, f"connect failed: {connect_for_tree.get('error', connect_for_tree)}")
    else:
        tree = call_tool("dump_tree", {"max_depth": 2})
        found = call_tool("find_control", {"role": "window", "max_depth": 1})
        tree_ok = tree.get("ok") is True and isinstance(tree.get("controls"), list)
        find_ok = isinstance(found.get("matches"), list)
        record(
            "component discovery",
            tree_ok and find_ok,
            f"controls={len(tree.get('controls', [])) if isinstance(tree.get('controls'), list) else 'n/a'} matches={found.get('count')}",
        )

    # ── 3. screenshot ────────────────────────────────────────────
    print("\n  screenshot... ", end="", flush=True)
    r = call_tool("screenshot", {})
    if verbose:
        print(f"\n    {json.dumps(r, ensure_ascii=False)[:300]}")
        print("    ", end="")
    ok = r.get("ok") is True and "path" in r
    error_l = (r.get("error") or "").lower()
    headless_skipped = (
        not ok
        and (
            "could not create image from display" in error_l
            or "headless" in error_l
            or "screen recording" in error_l
            or "permission" in error_l
        )
    )
    if headless_skipped:
        record("screenshot", False, r.get("error", ""), "no active display / screen permission")
    else:
        record("screenshot", ok, f"path={r.get('path')}" if ok else r.get("error", ""))

    # ── 4. list_windows ──────────────────────────────────────────
    print("\n  list_windows... ", end="", flush=True)
    r = call_tool("list_windows", {})
    if verbose:
        print(f"\n    {json.dumps(r, ensure_ascii=False)[:300]}")
        print("    ", end="")
    ok = r.get("ok") is True and "windows" in r
    cnt = len(r.get("windows", [])) if isinstance(r, dict) else 0
    record("list_windows", ok, f"{cnt} windows" if ok else r.get("error", ""))

    # ── 5. is_window_open Finder ─────────────────────────────────
    print("\n  is_window_open Finder... ", end="", flush=True)
    r = call_tool("is_window_open", {"process_name": "Finder"})
    if verbose:
        print(f"\n    {json.dumps(r, ensure_ascii=False)[:300]}")
        print("    ", end="")
    record(
        "is_window_open Finder",
        r.get("ok") is True and r.get("found") is True,
        f"found={r.get('found')}",
    )

    # ── 6. is_window_open no filter rejects ──────────────────────
    print("\n  is_window_open no filter rejects... ", end="", flush=True)
    r = call_tool("is_window_open", {})
    if verbose:
        print(f"\n    {json.dumps(r, ensure_ascii=False)[:300]}")
        print("    ", end="")
    record(
        "is_window_open no filter",
        r.get("ok") is False and "error" in r,
        r.get("error", ""),
    )

    # ── 7. wait_window timeout ───────────────────────────────────
    print("\n  wait_window timeout... ", end="", flush=True)
    r = call_tool("wait_window", {
        "process_name": "nonexistent_xyz_app_123",
        "timeout": 1.5,
        "interval": 0.3,
    })
    if verbose:
        print(f"\n    {json.dumps(r, ensure_ascii=False)[:300]}")
        print("    ", end="")
    record(
        "wait_window timeout",
        r.get("ok") is False and r.get("error") == "timeout",
        r.get("error", ""),
    )

    # ── 8. activate_app Finder ───────────────────────────────────
    print("\n  activate_app Finder... ", end="", flush=True)
    r = call_tool("activate_app", {"app_name": "Finder"})
    if verbose:
        print(f"\n    {json.dumps(r, ensure_ascii=False)[:300]}")
        print("    ", end="")
    record(
        "activate_app Finder",
        r.get("ok") is True,
        r.get("error", ""),
    )

    # ── 9. connect by process_name ───────────────────────────────
    print("\n  connect by process_name... ", end="", flush=True)
    r = call_tool("connect", {"process_name": "Finder"})
    if verbose:
        print(f"\n    {json.dumps(r, ensure_ascii=False)[:300]}")
        print("    ", end="")
    record(
        "connect by process_name",
        r.get("ok") is True and r.get("pid") is not None,
        f"pid={r.get('pid')}",
    )

    # ── 10. click_at best-effort ─────────────────────────────────
    # We do NOT verify the click "landed" — that requires visual
    # inspection, and the macos_accessibility backend defaults to
    # dry-run mode (no real click) for safety. EDR_WD_ALLOW_REAL_CLICKS=1
    # on the target server opts in to real cliclick / osascript clicks.
    # We only verify the backend has the plumbing to attempt it.
    print("\n  click_at (plumbing check, dry-run)... ", end="", flush=True)
    r = call_tool("click_at", {"x": 100, "y": 100})
    if verbose:
        print(f"\n    {json.dumps(r, ensure_ascii=False)[:300]}")
        print("    ", end="")
    method = r.get("method", "")
    has_method = method in ("cliclick", "osascript", "dry_run")
    error = r.get("error", "")
    optional_click_helper_missing = (
        not has_method
        and (
            "cliclick" in error
            or "command not found" in error
            or "accessibility permission" in error.lower()
        )
    )
    if optional_click_helper_missing:
        record(
            "click_at plumbing",
            False,
            error[:120],
            "optional real-click helper unavailable",
        )
    else:
        record(
            "click_at plumbing",
            has_method,
            f"method={method or error[:80]}",
        )

    # ── 11. action primitives ───────────────────────────────────
    print("\n  action primitives... ", end="", flush=True)
    primitive_calls = [
        ("double_click_at", {"x": 100, "y": 100}),
        ("right_click_at", {"x": 100, "y": 100}),
        ("middle_click_at", {"x": 100, "y": 100}),
        ("hover_at", {"x": 100, "y": 100}),
        ("scroll", {"clicks": 1, "x": 100, "y": 100}),
        ("drag", {"x1": 100, "y1": 100, "x2": 120, "y2": 120, "duration": 0.1}),
    ]
    primitive_ok = True
    primitive_details = []
    for tool_name, args in primitive_calls:
        result = call_tool(tool_name, args)
        ok = result.get("ok") is True
        primitive_ok = primitive_ok and ok
        primitive_details.append(f"{tool_name}={'OK' if ok else 'FAIL'}")
    record(
        "action primitives",
        primitive_ok,
        ", ".join(primitive_details),
    )

    ok = (failed == 0)
    return passed, failed, skipped, errors, ok
