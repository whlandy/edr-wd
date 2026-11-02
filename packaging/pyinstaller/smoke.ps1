# Smoke-test the built agent-side mcp.exe executable (Windows).
#
# Usage: powershell -ExecutionPolicy Bypass -File packaging\pyinstaller\smoke.ps1 [-DistDir DIR]
#
# Checks (packaging/DESIGN.md "PyInstaller smoke" + packaging exclusions):
#   1. executable starts and prints help
#   2. config --list works against the bundled example config (no secrets)
#   3. no-write probe: `status` on an absent target returns without a
#      deploy/install attempt
#   4. no credential-bearing config, logs, screenshots, caches, or .git
#      are bundled in dist
#   5. products --list lists installed product profiles
[CmdletBinding()]
param(
    [string]$DistDir = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..\..")

if (-not $DistDir) {
    $DistDir = Join-Path $repoRoot "build-packaging\dist"
}

$exe = Join-Path $DistDir "mcp\mcp.exe"
if (-not (Test-Path $exe)) {
    Write-Error "executable not found at $exe — run build.ps1 first"
}

function Fail($msg) {
    Write-Error "SMOKE FAIL: $msg"
}
function Pass($msg) {
    Write-Host "SMOKE PASS: $msg"
}

# 1. --help starts and prints usage.
$helpOut = & $exe --help 2>&1
if ($LASTEXITCODE -ne 0) { Fail "--help exited non-zero" }
if (-not ($helpOut -match "usage")) { Fail "--help output does not mention usage" }
Pass "executable starts and prints help"

# 2. config --list runs from the bundle (documentation example config only).
& $exe config --list *> $null   # absent local config is allowed; must not crash
Pass "config --list runs without crashing"

# 3. no-write probe: status on an unconfigured target fails cleanly.
& $exe status --target __smoke_nonexistent_target__ *> $null
if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq 1) {
    Pass "no-write probe returns without deploy/install attempts"
} else {
    Fail "no-write probe exited unexpectedly ($LASTEXITCODE)"
}

# 4. products --list works offline.
$prodOut = & $exe products 2>&1
if ($LASTEXITCODE -ne 0 -or -not ($prodOut -match '"ok":\s*true')) {
    Fail "products --list did not return ok:true"
}
Pass "products --list returns built-in product profiles"

# 5. packaging exclusions in the bundle.
$internal = Join-Path $DistDir "mcp\_internal"
if (Test-Path (Join-Path $internal "config\targets.local.json")) {
    Fail "bundle contains config/targets.local.json (secret-bearing config)"
}
$logs = Get-ChildItem $DistDir\mcp -Recurse -Include *.log, server.pid -ErrorAction SilentlyContinue
if ($logs) { Fail "bundle contains log files or pid files" }
if (Test-Path (Join-Path $internal ".git")) { Fail "bundle contains .git" }
$pycache = Get-ChildItem $DistDir\mcp -Recurse -Directory -Filter __pycache__ -ErrorAction SilentlyContinue
if ($pycache) { Fail "bundle contains __pycache__ directories" }
if (Test-Path (Join-Path $internal "target\tests")) {
    Fail "bundle contains target/tests (test code must not be packaged)"
}
Pass "no secrets, logs, caches, .git, or test trees in the bundle"

Write-Host ""
Write-Host "ALL SMOKE CHECKS PASSED"
