#!/usr/bin/env python3
"""S5 只读环境检查(11.26):MCP status + 工具面 + capability 事实。

只读:不 begin_product_run、不 connect、不点击、不改服务。
输出落 result-report/s5-windows-acceptance/readonly-check/。
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "http://127.0.0.1:18765/mcp"
OUT = Path(__file__).resolve().parents[1] / "result-report" / \
    "s5-windows-acceptance" / "readonly-check"
OUT.mkdir(parents=True, exist_ok=True)

_session = {"id": None}


def rpc(method, params=None, notification=False):
    payload = {"jsonrpc": "2.0",
               "method": method}
    if params is not None:
        payload["params"] = params
    if not notification:
        payload["id"] = rpc._next
        rpc._next += 1
    req = urllib.request.Request(
        BASE, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 **({"mcp-session-id": _session["id"]}
                    if _session["id"] else {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        sid = resp.headers.get("mcp-session-id")
        if sid:
            _session["id"] = sid
        body = resp.read().decode()
    for line in body.splitlines():
        if line.startswith("data:"):
            data = json.loads(line[5:].strip())
            if "result" in data or "error" in data:
                return data
    return {"raw": body}


rpc._next = 1


def main() -> int:
    results = {}
    init = rpc("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "s5-readonly-check", "version": "0.1"}})
    results["server"] = init["result"]["serverInfo"]
    rpc("notifications/initialized", notification=True)
    tools = rpc("tools/list")
    names = sorted(t["name"] for t in tools["result"]["tools"])
    results["tools"] = names
    results["binding_tools_registered"] = sorted(
        n for n in names if n in {
            "bind_entry_window", "focus_bound_window",
            "window_binding_state", "register_window_declaration",
            "reverify_bound_window"})
    # 只读状态类工具(不产生 GUI 副作用)。
    for tool, args in (("get_status", {}),
                       ("status", {})):
        if tool in names:
            try:
                results[f"tool:{tool}"] = rpc("tools/call", {
                    "name": tool, "arguments": args})
            except Exception as exc:  # noqa: BLE001
                results[f"tool:{tool}"] = {"error": str(exc)}
            break
    results["checkedAt"] = datetime.now(timezone.utc).isoformat()
    out_file = OUT / "readonly-check.json"
    out_file.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
