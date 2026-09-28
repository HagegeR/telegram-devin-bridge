# Supervisor loop for the bridge on Windows: /update and the admin restart
# exit the process and this loop starts it again. Registered at logon by
# deploy/windows/install.ps1; run it by hand to test.
$ErrorActionPreference = 'Continue'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $root
New-Item -ItemType Directory -Force logs | Out-Null
$env:BRIDGE_SUPERVISED = '1'
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONUTF8 = '1'
# stopping the scheduled task kills this loop but not its python child, so a
# restarted task would poll twice; the venv launcher path scopes this to one
# checkout, and /T also ends the base interpreter it spawned
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.ExecutablePath -eq $venvPython } |
    ForEach-Object { taskkill /F /T /PID $_.ProcessId | Out-Null }
while ($true) {
    # cmd redirection keeps the log UTF-8; Windows PowerShell's >> writes UTF-16
    cmd /c ".venv\Scripts\python.exe -m app.poll >> logs\bridge.log 2>&1"
    Start-Sleep -Seconds 5
}
