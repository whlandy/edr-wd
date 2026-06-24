param(
    [string]$TargetName,
    [ValidateSet('auto', 'windows', 'macos')]
    [string]$Platform = 'auto',
    [ValidateSet('agent', 'target', 'all')]
    [string]$Scope = 'all',
    [switch]$IncludeTest,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'

function Get-RepoRoot {
    return Split-Path $PSScriptRoot -Parent
}

function Get-PythonExe {
    $python = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($python) { return $python.Source }

    $py = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) { return $py.Source }

    throw "Python not found. Install Python or add it to PATH."
}

$RepoRoot = Get-RepoRoot
$PythonExe = Get-PythonExe
$ArgsList = @((Join-Path $RepoRoot 'scripts/check_dependencies.py'), '--scope', $Scope, '--platform', $Platform)

if ($TargetName) {
    $ArgsList += @('--target', $TargetName)
}
if ($IncludeTest) {
    $ArgsList += '--include-test'
}
if ($Json) {
    $ArgsList += '--json'
}

Set-Location $RepoRoot
& $PythonExe @ArgsList
exit $LASTEXITCODE
