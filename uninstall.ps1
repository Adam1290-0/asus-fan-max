#Requires -RunAsAdministrator
# asus-fan-max uninstaller

$TaskName = "asus-fan-max"

Write-Host "=== asus-fan-max uninstaller ==="

# Stop and delete task
schtasks /End /TN $TaskName 2>$null | Out-Null
Start-Sleep -Seconds 2
schtasks /Delete /TN $TaskName /F 2>$null | Out-Null

Write-Host "  Scheduled task removed: $TaskName"

# Kill any remaining daemon processes
Get-Process python -ErrorAction SilentlyContinue | Where-Object {
    try { $_.CommandLine -like '*fan_daemon*' } catch { $false }
} | Stop-Process -Force -ErrorAction SilentlyContinue

Write-Host "  Daemon processes stopped"
Write-Host ""
Write-Host "=== Uninstall complete ==="
Write-Host "Note: Config and log files were NOT deleted. Remove manually if desired."
