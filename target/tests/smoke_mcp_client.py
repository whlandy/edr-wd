#!/usr/bin/env python3
"""Smoke test for the EDR-WD MCP HTTP server.

This script validates the live server workflow end-to-end:
1. Establish an MCP session over HTTP.
2. Initialize the protocol.
3. Verify core tools are exposed.
4. Run a synchronous PowerShell command.
5. Run an asynchronous PowerShell job and poll its result.
6. Optionally verify GUI connection / activation flows.

Usage:
    python target/tests/smoke_mcp_client.py
    python target/tests/smoke_mcp_client.py --base-url http://127.0.0.1:8765/mcp --gui
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


DEFAULT_BASE_URL = "http://127.0.0.1:8765/mcp"


def request_json(url: str, method: str, *, headers=None, data=None, timeout=10):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, resp.headers, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, exc.headers, body


def extract_session_id(headers) -> str:
    for key in ("Mcp-Session-Id", "mcp-session-id"):
        value = headers.get(key)
        if value:
            return value
    raise RuntimeError("Mcp-Session-Id header missing")


def establish_session(base_url: str) -> str:
    status, headers, _ = request_json(
        base_url,
        "GET",
        headers={"Accept": "text/event-stream"},
        timeout=5,
    )
    if status not in (200, 400):
        raise RuntimeError(f"Unexpected session probe status: {status}")
    return extract_session_id(headers)


def rpc(base_url: str, session_id: str, method: str, params: dict, request_id: int):
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
    ).encode("utf-8")
    status, headers, body = request_json(
        base_url,
        "POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Session-Id": session_id,
        },
        data=payload,
        timeout=15,
    )
    if status not in (200, 202, 400):
        raise RuntimeError(f"RPC {method} failed with HTTP {status}: {body}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            try:
                return json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
        raise RuntimeError(f"RPC {method} returned non-JSON body: {body[:500]}") from exc


def call_tool(base_url: str, session_id: str, name: str, arguments: dict, request_id: int):
    result = rpc(
        base_url,
        session_id,
        "tools/call",
        {"name": name, "arguments": arguments},
        request_id,
    )
    if "result" in result:
        return result["result"]
    return result


def unwrap_tool_text(result) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if "text" in result and isinstance(result["text"], str):
            return result["text"]
        if "content" in result and result["content"]:
            first = result["content"][0]
            if isinstance(first, dict):
                if "text" in first:
                    return first["text"]
                if "content" in first:
                    return first["content"]
    return json.dumps(result, ensure_ascii=False)


def parse_tool_json(result):
    text = unwrap_tool_text(result)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def main():
    parser = argparse.ArgumentParser(description="Smoke test the EDR-WD MCP server")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Also test a GUI flow. Windows uses HiSec connect/dump_tree; macOS uses Finder window plumbing.",
    )
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    session_id = establish_session(args.base_url)
    print(f"[ok] session_id={session_id}")

    init_result = rpc(
        args.base_url,
        session_id,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "edr-wd-smoke-test", "version": "1.0"},
        },
        1,
    )
    print(f"[ok] initialize status={list(init_result.keys())}")

    status_result = call_tool(args.base_url, session_id, "status", {}, 2)
    status_json = parse_tool_json(status_result)
    backend_name = status_json.get("backend", "unknown")
    backend_kind = status_json.get("backend_kind", backend_name)
    print(f"[ok] status backend={backend_name!r} kind={backend_kind!r}")
    action_space = status_json.get("action_space", {})
    if not isinstance(action_space, dict):
        raise RuntimeError(f"status.action_space malformed: {action_space!r}")
    if backend_kind == "windows_pywinauto":
        for key in ("click", "dump_tree", "find_control", "type_text", "select", "click_at", "drag", "scroll", "lock_window", "verify_window_lock", "restore_edr"):
            if action_space.get(key) is not True:
                raise RuntimeError(f"status.action_space missing supported windows action {key!r}: {action_space}")
    elif backend_kind == "macos_accessibility":
        for key in ("click", "click_target", "dump_tree", "find_control", "click_at", "click_window_at", "double_click_at", "right_click_at", "middle_click_at", "hover_at", "drag", "scroll", "lock_window", "verify_window_lock", "restore_edr"):
            if action_space.get(key) is not True:
                raise RuntimeError(f"status.action_space missing supported macOS action {key!r}: {action_space}")
        for key in ("type_text", "select", "get_text"):
            if action_space.get(key) is not False:
                raise RuntimeError(f"status.action_space should mark macOS action {key!r} unsupported: {action_space}")

    tool_list = rpc(args.base_url, session_id, "tools/list", {}, 3)
    tool_names = [tool["name"] for tool in tool_list.get("result", {}).get("tools", [])]
    expected = {
        "connect",
        "status",
        "activate_app",
        "list_windows",
        "is_window_open",
        "wait_window",
        "screenshot",
        "lock_window",
        "unlock_window",
        "get_window_lock",
        "verify_window_lock",
        "find_control",
        "click_at",
        "click_window_at",
        "double_click_at",
        "right_click_at",
        "middle_click_at",
        "hover_at",
        "drag",
        "scroll",
        "type_text",
        "select",
        "get_text",
        "restore_edr",
    }
    if backend_kind == "windows_pywinauto":
        expected.update({"run_powershell", "start_powershell", "get_job", "cancel_job", "activate_edr", "dump_tree"})
    elif backend_kind == "macos_accessibility":
        expected.update({"dump_tree", "click", "click_target"})
    missing = sorted(expected - set(tool_names))
    if missing:
        raise RuntimeError(f"Missing tools: {', '.join(missing)}")
    print(f"[ok] tools={', '.join(sorted(expected))}")

    if backend_kind == "windows_pywinauto":
        sync_result = call_tool(
            args.base_url,
            session_id,
            "run_powershell",
            {"command": "Write-Output smoke-sync", "timeout": 10},
            4,
        )
        sync_json = parse_tool_json(sync_result)
        if not sync_json.get("ok"):
            raise RuntimeError(f"run_powershell failed: {sync_json}")
        print(f"[ok] run_powershell stdout={sync_json.get('stdout', '').strip()!r}")

        job_result = call_tool(
            args.base_url,
            session_id,
            "start_powershell",
            {"command": 'Start-Sleep -Seconds 1; Write-Output "smoke-async"', "timeout": 10},
            5,
        )
        job_json = parse_tool_json(job_result)
        job_id = job_json.get("job_id")
        if not job_id:
            raise RuntimeError(f"start_powershell did not return job_id: {job_json}")

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            poll_result = call_tool(
                args.base_url,
                session_id,
                "get_job",
                {"job_id": job_id},
                6,
            )
            poll_json = parse_tool_json(poll_result)
            if poll_json.get("status") == "done":
                if not poll_json.get("ok"):
                    raise RuntimeError(f"Async job failed: {poll_json}")
                print(f"[ok] async job stdout={poll_json.get('stdout', '').strip()!r}")
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Timed out waiting for async job to finish")

    if args.gui:
        if backend_kind == "windows_pywinauto":
            connect_result = call_tool(
                args.base_url,
                session_id,
                "connect",
                {"title_re": ".*HiSec.*", "timeout": 5, "auto_activate": True},
                7,
            )
            connect_json = parse_tool_json(connect_result)
            print(f"[info] connect result={connect_json}")
            if not connect_json.get("ok"):
                raise RuntimeError(f"GUI connect failed: {connect_json}")

            dump_result = call_tool(
                args.base_url,
                session_id,
                "dump_tree",
                {"max_depth": 3},
                8,
            )
            dump_json = parse_tool_json(dump_result)
            controls = dump_json.get("controls", [])
            if not isinstance(controls, list):
                raise RuntimeError(f"dump_tree returned unexpected payload: {dump_json}")
            print(f"[ok] dump_tree controls={len(controls)}")
        elif backend_kind == "macos_accessibility":
            list_result = call_tool(args.base_url, session_id, "list_windows", {}, 6)
            list_json = parse_tool_json(list_result)
            if not list_json.get("ok"):
                raise RuntimeError(f"list_windows failed: {list_json}")
            print(f"[ok] list_windows count={list_json.get('count', 0)}")

            connect_result = call_tool(
                args.base_url,
                session_id,
                "connect",
                {"process_name": "Finder", "timeout": 5, "auto_activate": True},
                8,
            )
            connect_json = parse_tool_json(connect_result)
            print(f"[info] connect result={connect_json}")
            if not connect_json.get("ok"):
                raise RuntimeError(f"GUI connect failed: {connect_json}")

            activate_result = call_tool(
                args.base_url,
                session_id,
                "activate_app",
                {"app_name": "Finder"},
                9,
            )
            activate_json = parse_tool_json(activate_result)
            if not activate_json.get("ok"):
                raise RuntimeError(f"activate_app failed: {activate_json}")
            print("[ok] activate_app Finder")

            dump_result = call_tool(
                args.base_url,
                session_id,
                "dump_tree",
                {"max_depth": 2},
                10,
            )
            dump_json = parse_tool_json(dump_result)
            controls = dump_json.get("controls", [])
            if not dump_json.get("ok") or not isinstance(controls, list):
                raise RuntimeError(f"macOS dump_tree returned unexpected payload: {dump_json}")
            print(f"[ok] dump_tree controls={len(controls)}")

            find_result = call_tool(
                args.base_url,
                session_id,
                "find_control",
                {"role": "window", "max_depth": 1},
                11,
            )
            find_json = parse_tool_json(find_result)
            if not isinstance(find_json.get("matches"), list):
                raise RuntimeError(f"macOS find_control returned unexpected payload: {find_json}")
            print(f"[ok] find_control matches={find_json.get('count', 0)}")
        else:
            print(f"[warn] GUI smoke skipped for unsupported backend {backend_kind!r}")

    print("[ok] smoke test finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
