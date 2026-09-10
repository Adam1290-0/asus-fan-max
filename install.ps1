#Requires -RunAsAdministrator
# asus-fan-max installer
# Creates a SYSTEM scheduled task that runs the daemon on logon.

param(
    [string]$PythonPath = "",
    [string]$ScriptDir = ""
)

$ErrorActionPreference = "Stop"
$TaskName = "asus-fan-max"

# Auto-detect script directory
if (-not $ScriptDir) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}

# Auto-detect Python
if (-not $PythonPath) {
    $PythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $PythonPath) {
        Write-Error "Python not found. Specify path with -PythonPath"
        exit 1
    }
}

$DaemonScript = Join-Path $ScriptDir "fan_daemon.py"
if (-not (Test-Path $DaemonScript)) {
    Write-Error "fan_daemon.py not found in $ScriptDir"
    exit 1
}

Write-Host "=== asus-fan-max installer ==="
Write-Host "  Python: $PythonPath"
Write-Host "  Script: $DaemonScript"
Write-Host ""

# Create wrapper batch file (loops so a crashed daemon restarts automatically;
# uses ping for delay since timeout is unreliable in SYSTEM's console-less session)
$BatPath = Join-Path $ScriptDir "run_daemon.bat"
@"
@echo off
:loop
"$PythonPath" "$DaemonScript" --config "$ScriptDir\config.json"
ping -n 6 127.0.0.1 > nul
goto loop
"@ | Out-File -FilePath $BatPath -Encoding ASCII

Write-Host "  Wrapper: $BatPath"

# Remove existing task
schtasks /Delete /TN $TaskName /F 2>$null | Out-Null

# Create scheduled task
$result = schtasks /Create /TN $TaskName /TR $BatPath /SC ONLOGON /RU SYSTEM /RL HIGHEST /F
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to create scheduled task: $result"
    exit 1
}

Write-Host ""
Write-Host "  Task created: $TaskName (ONLOGON, SYSTEM)"

# Start immediately
schtasks /Run /TN $TaskName | Out-Null
Start-Sleep -Seconds 3

# Verify
$status = schtasks /Query /TN $TaskName /FO CSV 2>$null
if ($status -match "Running") {
    Write-Host "  Status: RUNNING" -ForegroundColor Green
} else {
    Write-Host "  Status: Created (will start on next logon)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Installation complete ==="
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Run calibration: Start-Process python -Verb RunAs -ArgumentList '$DaemonScript --calibrate'"
Write-Host "  2. Edit config.json to adjust thresholds"
Write-Host "  3. Monitor log: Get-Content '$ScriptDir\asus-fan-max.log' -Wait -Tail 10"
