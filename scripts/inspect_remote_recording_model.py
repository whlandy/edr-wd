"""Read-only target comparison; generated evidence stays on the agent."""
import difflib
import hashlib
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from agent.ssh_runner import _paramiko_connect, run_ssh

out = root / 'result-report/20261024-234846-raw-probe'
cfg = json.loads((root / 'config/targets.local.json').read_text())['targets']['11.26-desktop-s917l7s-win10']
remote = 'C:/Users/admin/Desktop/edr-wd/recording/models.py'
client = _paramiko_connect(cfg['ssh'], timeout=15)
try:
    sftp = client.open_sftp()
    with sftp.open(remote, 'rb') as stream:
        before = stream.read()
    sftp.close()
finally:
    client.close()
(out / 'remote-models.py').write_bytes(before)
after = (root / 'target/recording/models.py').read_bytes()
diff = ''.join(difflib.unified_diff(before.decode().splitlines(True), after.decode().splitlines(True), fromfile='recording/models.py.before', tofile='recording/models.py.after'))
(out / 'models.patch').write_text(diff)
manifest = {'remotePath': remote, 'beforeSha256': hashlib.sha256(before).hexdigest(), 'localReviewedSha256': hashlib.sha256(after).hexdigest()}
(out / 'model-hashes.json').write_text(json.dumps(manifest, indent=2))
print(json.dumps(manifest), flush=True)
print(diff, flush=True)
command = 'powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match \'python\' } | Select-Object ProcessId,SessionId,CommandLine | Format-List; Get-ScheduledTask -TaskName StartEDRMCP | Select-Object TaskName,State,Actions | Format-List; Get-NetTCPConnection -LocalPort 8765 -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess | Format-List"'
rc, output = run_ssh(cfg['ssh'], command, timeout=20)
(out / 'service-inspection.txt').write_text(output)
(out / 'service-inspection-command.json').write_text(json.dumps({'command': command, 'exitCode': rc}, indent=2))
print('service inspection exit', rc, output, flush=True)
