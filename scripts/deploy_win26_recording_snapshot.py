"""Explicit local recording snapshot deploy; no git mutation or remote deletion."""
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

cfg = json.loads((ROOT / 'config/targets.local.json').read_text())['targets']['11.26-desktop-s917l7s-win10']
ssh = cfg['ssh']
remote = cfg['windows']['target_root'].replace('\\', '/').replace('//', '/')
stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
backup = remote + '.backup-' + stamp
command = f"Copy-Item -LiteralPath '{remote}' -Destination '{backup}' -Recurse -ErrorAction Stop; Write-Output 'BACKUP_OK'"
encoded = base64.b64encode(command.encode('utf-16le')).decode()
rc, out = run_ssh(ssh, 'powershell -NoProfile -EncodedCommand ' + encoded, timeout=120)
print('backup', rc, out, flush=True)
if rc or 'BACKUP_OK' not in out:
    raise SystemExit('Backup failed; no upload performed')
tracked = subprocess.check_output(['git', 'ls-files', '--', 'target'], cwd=ROOT, text=True).splitlines()
extra = [str(p.relative_to(ROOT)) for p in (ROOT / 'target/recording').glob('*.py')]
files = sorted(set(tracked + extra))
client = _paramiko_connect(ssh, timeout=15)
sftp = client.open_sftp()
manifest = {}
try:
    for relative in files:
        source = ROOT / relative
        if not source.is_file():
            continue
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
            assert hashlib.sha256(uploaded.read()).hexdigest() == digest, relative
        manifest[relative] = digest
finally:
    sftp.close()
    client.close()
print(json.dumps({'backup': backup, 'verified_files': len(manifest), 'recording_modules': sum(p.startswith('target/recording/') for p in manifest)}, ensure_ascii=False), flush=True)
