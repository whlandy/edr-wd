#!/usr/bin/env python3
"""Deploy the CURRENT target/ tree (tracked + untracked) to the 11.26 target.

Safety model (mirrors scripts/deploy_win26_recording_snapshot.py):
  1. remote full backup: Copy-Item <target_root> -> <target_root>.backup-<stamp>
  2. upload tracked + untracked target/**.py via SFTP
  3. every uploaded file sha256-verified by re-reading it back
No remote deletion, no service restart, no git mutation.

After upload the MCP server must be restarted by the EXISTING scheduled-task
mechanism for the new code to serve; this script only REPORTS the task name
and does not restart it unless --restart is passed (explicit authorization).
"""
import base64
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.ssh_runner import _paramiko_connect, run_ssh

cfg = json.loads(
    (ROOT / 'config/targets.local.json').read_text()
)['targets']['11.26-desktop-s917l7s-win10']
ssh = cfg['ssh']
remote = cfg['windows']['target_root'].replace('\\', '/').replace('//', '/')
stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
backup = remote + '.backup-' + stamp


def ps(script: str, timeout=180):
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    return run_ssh(ssh, 'powershell -NoProfile -EncodedCommand ' + encoded,
                   timeout=timeout)


def main() -> int:
    # 1. backup (no overwrite)
    rc, out = ps(
        f"if (Test-Path '{backup}') {{ Write-Output 'BACKUP_EXISTS'; exit 1 }}; "
        f"Copy-Item -LiteralPath '{remote}' -Destination '{backup}' -Recurse "
        f"-ErrorAction Stop; Write-Output 'BACKUP_OK'")
    print('backup', rc, out.strip()[:120], flush=True)
    if rc or 'BACKUP_OK' not in out:
        raise SystemExit('Backup failed; no upload performed')

    # 2. file set: tracked + untracked target/**.py (and needed data files)
    tracked = subprocess.check_output(
        ['git', 'ls-files', '--', 'target'], cwd=ROOT, text=True).splitlines()
    untracked = subprocess.check_output(
        ['git', 'ls-files', '--others', '--exclude-standard', '--', 'target'],
        cwd=ROOT, text=True).splitlines()
    files = sorted(set(tracked) | set(untracked))
    files = [f for f in files if f.endswith(
        ('.py', '.json', '.ps1')) and (ROOT / f).is_file()]

    client = _paramiko_connect(ssh, timeout=15)
    sftp = client.open_sftp()
    manifest = {}
    try:
        for relative in files:
            source = ROOT / relative
            destination = remote + '/' + relative.removeprefix('target/')
            parent = destination.rsplit('/', 1)[0]
            parts = parent.split('/')
            for i in range(2, len(parts) + 1):
                directory = '/'.join(parts[:i])
                try:
                    sftp.stat(directory)
                except IOError:
                    sftp.mkdir(directory)
            sftp.put(str(source), destination)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with sftp.open(destination, 'rb') as uploaded:
                assert hashlib.sha256(uploaded.read()).hexdigest() == digest, \
                    relative
            manifest[relative] = digest
    finally:
        sftp.close()
        client.close()

    manifest_path = ROOT / 'result-report' / 's5-windows-acceptance' / \
        'deploy-manifest.json'
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({
        'backup': backup, 'uploaded': len(manifest),
        'at': stamp, 'files': manifest,
    }, ensure_ascii=False, indent=1), encoding='utf-8')
    new_modules = [f for f in manifest
                   if f in ('target/windowing/binding.py',
                            'target/windowing/attempt_ledger.py',
                            'target/windowing/effect_gate.py',
                            'target/windowing/report_view.py')]
    print(json.dumps({
        'uploaded': len(manifest),
        'new_s3_s4_modules_deployed': new_modules,
        'manifest': str(manifest_path),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
