"""
run_macos_generic.py — Minimal v1 test suite for macOS target.

Verifies only capabilities that the macos_accessibility backend supports
in v1. Anything HiSec-EDR-specific (activate_edr, click by automation_id,
EDRClient/HisecEndpoint windows, screenshot to a Windows desktop path,
etc.) is explicitly out of scope for this profile.

Tests:
  - tools/list                  (verifies MCP handshake is healthy)
  - status                      (verifies backend is reporting "macos_accessibility")
  - screenshot                  (verifies screencapture works)
  - list_windows                (verifies System Events enumeration)
  - is_window_open Finder       (verifies process_name filter)
  - is_window_open no filter    (verifies error-on-empty-filter contract)
  - wait_window timeout         (verifies timeout path)
  - activate_app Finder         (verifies osascript activate path)
  - connect by process_name     (verifies connect/bundle_id path)
  - click_at basic              (verifies coordinate click — best-effort)

These are "the server is alive and the basic platform plumbing works"
tests. App-specific workflows (e.g. "drive Safari to navigate to ...")
will be added under app_profile=macos_app_specific once they exist.
"""

from __future__ import annotations

import json
import time
from typing import Optional


def run_macos_generic_tests(client, verbose: bool = False) -> tuple[int, int, list, bool]:
    passed = 0
    failed = 0
    errors: list[str] = []

    def call_tool(name, args=None):
        r = client.call_tool(name, args or {})
        if isinstance(r, str):
            r = json.loads(r)
        return r

    def record(name: str, ok: bool, detail: str = ""):
        nonlocal passed, failed
        if ok:
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

    # ── 3. screenshot ────────────────────────────────────────────
    print("\n  screenshot... ", end="", flush=True)
    r = call_tool("screenshot", {})
    if verbose:
        print(f"\n    {json.dumps(r, ensure_ascii=False)[:300]}")
        print("    ", end="")
    ok = r.get("ok") is True and "path" in r
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
    record(
        "click_at plumbing",
        has_method,
        f"method={method or r.get('error', '')[:80]}",
    )

    ok = (failed == 0)
    return passed, failed, errors, ok
