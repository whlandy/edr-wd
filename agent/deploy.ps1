param(
    [ValidateSet(
        'guide',
        'config-guide',
        'config-init',
        'config-validate',
        'config-list',
        'deploy',
        'install',
        'up',
        'down',
        'status',
        'push',
        'smoke'
    )]
    [string]$Action = 'guide',
    [string]$TargetName,
    [string[]]$Source,
    [string]$To,
    [switch]$Gui,
    [string]$BaseUrl
)

$ErrorActionPreference = 'Stop'

function Get-RepoRoot {
    return Split-Path $PSScriptRoot -Parent
}

function Get-PythonExe {
    $python = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($python) {
        return $python.Source
    }
    $py = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        return $py.Source
    }
    throw "Python not found. Install Python or add it to PATH."
}

function Invoke-PythonSnippet {
    param(
        [string]$Code,
        [string[]]$Args = @()
    )
    $pythonExe = Get-PythonExe
    $tempFile = New-TemporaryFile
    try {
        Set-Content -Path $tempFile -Value $Code -Encoding UTF8
        & $pythonExe $tempFile @Args
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    } finally {
        Remove-Item $tempFile -Force -ErrorAction SilentlyContinue
    }
}

function Write-Guide {
    Write-Host "EDR-WD agent deploy" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Configuration workflow:" -ForegroundColor Gray
    Write-Host "  1. Generate config skeleton:" -ForegroundColor Gray
    Write-Host "     .\deploy.ps1 -Action config-init"
    Write-Host "  2. Read the config guide:" -ForegroundColor Gray
    Write-Host "     .\deploy.ps1 -Action config-guide"
    Write-Host "  3. Validate config:" -ForegroundColor Gray
    Write-Host "     .\deploy.ps1 -Action config-validate"
    Write-Host "  4. List targets:" -ForegroundColor Gray
    Write-Host "     .\deploy.ps1 -Action config-list"
    Write-Host ""
    Write-Host "Target operations:" -ForegroundColor Gray
    Write-Host "  .\deploy.ps1 -Action deploy -TargetName win-dev" -ForegroundColor Gray
    Write-Host "  .\deploy.ps1 -Action install -TargetName win-dev" -ForegroundColor Gray
    Write-Host "  .\deploy.ps1 -Action up -TargetName win-dev" -ForegroundColor Gray
    Write-Host "  .\deploy.ps1 -Action status -TargetName win-dev" -ForegroundColor Gray
    Write-Host "  .\deploy.ps1 -Action smoke -TargetName win-dev -Gui" -ForegroundColor Gray
    Write-Host "  .\deploy.ps1 -Action down -TargetName win-dev" -ForegroundColor Gray
    Write-Host ""
    Write-Host "Notes:" -ForegroundColor Gray
    Write-Host "  - Uses config/targets.local.json or EDR_WD_CONFIG." -ForegroundColor Gray
    Write-Host "  - Windows and macOS targets share the same Python orchestration." -ForegroundColor Gray
    Write-Host "  - For Windows shell users, agent/edr-wd.sh remains available on POSIX hosts." -ForegroundColor Gray
}

$RepoRoot = Get-RepoRoot
Set-Location $RepoRoot

switch ($Action) {
    'guide' {
        Write-Guide
        exit 0
    }
    'config-guide' {
        Invoke-PythonSnippet @'
import sys
from agent.target_config import main
sys.argv = ["agent.target_config", "--guide"]
main()
'@
        exit 0
    }
    'config-init' {
        Invoke-PythonSnippet @'
import sys
from agent.target_config import main
sys.argv = ["agent.target_config", "--init"]
main()
'@
        exit 0
    }
    'config-validate' {
        Invoke-PythonSnippet @'
import sys
from agent.target_config import main
sys.argv = ["agent.target_config", "--validate"]
main()
'@
        exit 0
    }
    'config-list' {
        Invoke-PythonSnippet @'
import sys
from agent.target_config import main
sys.argv = ["agent.target_config", "--list"]
main()
'@
        exit 0
    }
    'deploy' {
        Invoke-PythonSnippet @'
import json
import sys
from agent.target_manager import deploy_target
target = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
print(json.dumps(deploy_target(target), ensure_ascii=False, indent=2))
'@ @($TargetName)
        exit 0
    }
    'install' {
        Invoke-PythonSnippet @'
import json
import sys
from agent.target_manager import install_target_task
target = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
print(json.dumps(install_target_task(target), ensure_ascii=False, indent=2))
'@ @($TargetName)
        exit 0
    }
    'up' {
        Invoke-PythonSnippet @'
import json
import sys
from agent.target_manager import ensure_server_running
target = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
print(json.dumps(ensure_server_running(target), ensure_ascii=False, indent=2))
'@ @($TargetName)
        exit 0
    }
    'down' {
        Invoke-PythonSnippet @'
import json
import sys
from agent.target_manager import stop_server
target = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
print(json.dumps(stop_server(target), ensure_ascii=False, indent=2))
'@ @($TargetName)
        exit 0
    }
    'status' {
        Invoke-PythonSnippet @'
import json
import sys
from agent.target_manager import check_server_health, probe_target
target = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
health = check_server_health(target)
print(json.dumps(health, ensure_ascii=False, indent=2))
try:
    probe = probe_target(target)
    print(json.dumps(probe, ensure_ascii=False, indent=2))
except Exception:
    pass
'@ @($TargetName)
        exit 0
    }
    'push' {
        if (-not $Source -or $Source.Count -eq 0) {
            throw "push requires at least one source path via -Source"
        }
        Invoke-PythonSnippet @'
import json
import sys
from pathlib import Path
from agent.target_config import TargetConfig
from agent.ssh_runner import scp_dir_to, scp_to

target_name = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
remote_override = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
sources = [Path(p) for p in sys.argv[3:]]

tc = TargetConfig()
cfg = tc.get_resolved_target(target_name)
ssh_cfg = cfg["ssh"]
remote_base = remote_override or cfg.get("windows", {}).get("target_root") or cfg.get("macos", {}).get("root")
if not remote_base:
    raise SystemExit("No remote target root configured")
remote_dest = f"{remote_base.rstrip('/')}/incoming/"

results = []
for src in sources:
    if src.is_dir():
        rc, msg = scp_dir_to(ssh_cfg, str(src), remote_dest, timeout=120)
    else:
        rc, msg = scp_to(ssh_cfg, str(src), remote_dest, timeout=120)
    results.append({"source": str(src), "ok": rc == 0, "message": msg})

print(json.dumps({"ok": all(r["ok"] for r in results), "results": results}, ensure_ascii=False, indent=2))
'@ @($TargetName, $To) + $Source
        exit 0
    }
    'smoke' {
        $smokeArgs = @()
        if ($Gui) { $smokeArgs += '--gui' }
        Invoke-PythonSnippet @'
import json
import sys
from agent.target_manager import check_server_health
from agent.target_config import TargetConfig

target_name = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
base_url_override = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else ""
extra_args = sys.argv[3:]

tc = TargetConfig()
health = check_server_health(target_name)
if not health.get("ok"):
    raise SystemExit(json.dumps(health, ensure_ascii=False, indent=2))

base_url = base_url_override or health["data"]["mcp_url"]
from target.tests.smoke_mcp_client import main as smoke_main
sys.argv = ["smoke_mcp_client.py", "--base-url", base_url] + extra_args
raise SystemExit(smoke_main())
'@ @($TargetName, $BaseUrl) + $smokeArgs
        exit 0
    }
}
