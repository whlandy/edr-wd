# Check SessionId for relevant processes
Write-Host "=== Session Info ==="
query session

Write-Host ""
Write-Host "=== Process SessionIds ==="
Get-Process | Where-Object { $_.Name -match 'python|HiSec|EDR' } | Select-Object Name, Id, SessionId | Format-Table -AutoSize

Write-Host ""
Write-Host "=== Current User ==="
whoami

Write-Host ""
Write-Host "=== Is Admin? ==="
net session 2>$null; if ($LASTEXITCODE -eq 0) { "Admin" } else { "Not Admin" }
