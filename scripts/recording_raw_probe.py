"""Production MCP evidence probe. Never starts services or recordings."""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.mcp_manager import _post_sse

root = Path(__file__).resolve().parents[1]
out = root / 'result-report' / datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S-raw-probe')
out.mkdir(parents=True)
headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
url = 'http://127.0.0.1:18765/mcp'

def request(label, method, params):
    payload = {'jsonrpc': '2.0', 'id': label, 'method': method, 'params': params}
    (out / (label + '-request.json')).write_text(json.dumps(payload, indent=2))
    try:
        status, response_headers, raw = _post_sse(url, json.dumps(payload).encode(), headers, timeout=15)
        (out / (label + '-response.txt')).write_text(raw)
        (out / (label + '-http.json')).write_text(json.dumps({'status': status, 'headers': response_headers}, indent=2))
        session = next((v for k, v in response_headers.items() if k.lower() == 'mcp-session-id'), None)
        if session:
            headers['Mcp-Session-Id'] = session
        print(label, status, raw[:1000], flush=True)
        return raw
    except Exception as exc:
        (out / (label + '-exception.txt')).write_text(repr(exc))
        raise

print(out, flush=True)
request('01-init', 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'recording-evidence-probe', 'version': '1'}})
request('02-tools', 'tools/list', {})
raw = request('03-status', 'tools/call', {'name': 'recording_status', 'arguments': {'heartbeat': False}})
envelope = json.loads(next(line[5:].strip() for line in raw.splitlines() if line.startswith('data:')))
receipt = json.loads(envelope['result']['content'][0]['text'])
if receipt.get('state') == 'stopped' and receipt.get('sessionId') == 'REC-87ebead55c0749e5bfe78beb848ffa3a':
    # Production stop is idempotent for an already-stopped session. This
    # re-requests that same document without creating or interrupting a run.
    request('04-stopped-result', 'tools/call', {'name': 'stop_recording', 'arguments': {}})
