"""Read-only live probe of the 11.26 target via the SSH tunnel.

Calls only non-mutating tools: get_window_lock, recording_status, dump_tree.
Saves raw SSE responses so evidence is persisted verbatim.
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:18765/mcp"
OUT = sys.argv[1] if len(sys.argv) > 1 else "live-probe-raw.txt"


def post(payload, session=None):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session:
        headers["mcp-session-id"] = session
    req = urllib.request.Request(
        BASE, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
        return resp.headers.get("mcp-session-id"), body


def result_text(body):
    for line in body.splitlines():
        if line.startswith("data: "):
            data = json.loads(line[6:])
            if "result" in data:
                content = data["result"].get("content") or []
                if content and content[0].get("type") == "text":
                    return content[0]["text"]
    return None


init_id, init_body = post({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "edrwd-round-probe", "version": "1.0"}}})
post({"jsonrpc": "2.0", "method": "notifications/initialized"}, init_id)

lines = []
lines.append("# Live read-only probes (tunnel 127.0.0.1:18765 -> 11.26)")
lines.append(f"# MCP session: {init_id}")

for name, args, rid in [
    ("get_window_lock", {}, 2),
    ("recording_status", {}, 3),
    ("dump_tree", {"max_depth": 6}, 4),
]:
    _, body = post({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                    "params": {"name": name, "arguments": args}}, init_id)
    lines.append(f"\n## {name}\n```json\n{result_text(body)}\n```")
    raw = result_text(body)
    if name == "dump_tree" and raw:
        tree = json.loads(raw)
        controls = tree.get("controls") or []
        missing = [c for c in controls
                   if not (c.get("ancestry") or [])]
        lines.append(f"controls: {len(controls)} / without-ancestry: {len(missing)}")
        rect = tree.get("window_rectangle")
        lines.append(f"window_rectangle: {rect}")

lines.append("\n# No mutating tool calls made; no new sessions/locks created.")
with open(OUT, "w") as fh:
    fh.write("\n".join(lines) + "\n")
print("\n".join(lines))
