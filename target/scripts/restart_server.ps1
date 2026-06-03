#!/usr/bin/env -pwsh
# restart_server.ps1 — Restart EDR-WD MCP server
#
# Usage:
#   .\restart_server.ps1
#   powershell -ExecutionPolicy Bypass -File restart_server.ps1

& "$PSScriptRoot\stop_server.ps1"
if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& "$PSScriptRoot\start_server.ps1"
exit $LASTEXITCODE
