"""Isolated live physical-input smoke; preserves raw MCP responses."""
import json
import runpy
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
ctx = runpy.run_path(str(root / 'scripts/recording_raw_probe.py'))
request, out = ctx['request'], ctx['out']
def call(label, name, args):
    raw = request(label, 'tools/call', {'name': name, 'arguments': args})
    env = json.loads(next(line[5:].strip() for line in raw.splitlines() if line.startswith('data:')))
    result = json.loads(env['result']['content'][0]['text'])
    (out / (label + '-payload.json')).write_text(json.dumps(result, indent=2))
    return result

if ctx['receipt'].get('code') != 'recording_session_missing' and ctx['receipt'].get('state') != 'stopped':
    raise SystemExit('Abort: existing session requires review')
from agent.ssh_runner import run_ssh
cfg = json.loads((root / 'config/targets.local.json').read_text())['targets']['11.26-desktop-s917l7s-win10']
# Reuse the independently observed fixture PID; do not launch a duplicate.
title = r'^EDR-WD Native Recording QA [[]qa-native-02[]]$'
bound = call('05-connect', 'connect', {'title_re': title, 'pid': 7440, 'process_name': 'powershell.exe', 'timeout': 10, 'auto_activate': False})
if not bound.get('ok'):
    raise SystemExit('Fixture connect failed')
locked = call('05b-lock', 'lock_window', {'title_re': title, 'pid': bound['pid'], 'process_name': 'powershell.exe', 'strict': True, 'activate': True})
if not locked.get('ok'):
    raise SystemExit('Strict fixture lock failed')
verified = call('05c-verify', 'verify_window_lock', {'activate': False})
if not verified.get('ok'):
    raise SystemExit('Strict fixture verification failed')
call('06-tree', 'dump_tree', {'max_depth': 2})
started = call('07-start', 'start_recording', {'name': 'native-physical-smoke', 'process_name': 'powershell.exe', 'window_title': title, 'lease_seconds': 120})
if not started.get('ok'):
    raise SystemExit('Start failed')
try:
    for label, autoid in [('08-check', 'FixtureCheck'), ('09-save', 'FixtureSave')]:
        result = call(label, 'click_target', {'automation_id': autoid, 'expected_process_name': 'powershell.exe', 'parent_fallback': False})
        if not result.get('ok'):
            raise RuntimeError('Physical click failed: ' + label)
    call('10-tree-after', 'dump_tree', {'max_depth': 2})
finally:
    stopped = call('11-stop', 'stop_recording', {})
    call('12-status', 'recording_status', {'heartbeat': False})
if stopped.get('ok'):
    from agent.cli import _target_agent
    from agent.recording.collection import collect_recording_artifacts
    artifacts, issues = collect_recording_artifacts(_target_agent('11.26-desktop-s917l7s-win10', None), stopped, out / 'artifacts', None, timeout=20)
    print('artifacts', artifacts.directory, 'capture issues', issues, flush=True)
