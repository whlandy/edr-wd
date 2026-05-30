#!/usr/bin/env python3
"""MCP client over SSE/HTTP for edr-wd"""
import json, urllib.request, urllib.error, sseclient, sys

BASE = "http://127.0.0.1:18765/mcp"

def sse_req(method, params=None, req_id=1):
    payload = json.dumps({"jsonrpc":"2.0","id":req_id,"method":method,"params":params or {}}).encode()
    req = urllib.request.Request(
        BASE, data=payload,
        headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
    )
    return urllib.request.urlopen(req, timeout=15)

# Initialize
resp = sse_req("initialize", {"protocolVersion":"1.0","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}, 1)
client = sseclient.SSEClient(resp)
for event in client.events():
    print("INIT:", event.data)
    break

# Send notifications (empty id per MCP spec)
for method in ["notifications/initialized"]:
    payload = json.dumps({"jsonrpc":"2.0","method":method,"params":{}}).encode()
    req = urllib.request.Request(BASE, data=payload,
        headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except:
        pass

# List tools
resp = sse_req("tools/list", {}, 2)
client = sseclient.SSEClient(resp)
for event in client.events():
    print("TOOLS:", event.data)
    break

# Connect to hisec-endpoint
resp = sse_req("tools/call", {"name":"connect","arguments":{"title_re":"hisec-endpoint","timeout":10}}, 3)
client = sseclient.SSEClient(resp)
for event in client.events():
    print("CONNECT:", event.data)
    break

# Dump tree
resp = sse_req("tools/call", {"name":"dump_tree","arguments":{"window_title_re":"hisec-endpoint"}}, 4)
client = sseclient.SSEClient(resp)
for event in client.events():
    print("TREE:", event.data)
    break
