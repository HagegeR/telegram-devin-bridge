# Install the bridge in place (this checkout) on Windows, in polling mode,
# started at logon by a scheduled task that runs deploy/windows/run.ps1.
# Needs Python >= 3.12 and Git for Windows (its sh.exe runs self-update.sh).
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 [-DryRun] [-NoTask]
param([switch]$DryRun, [switch]$NoTask)
$ErrorActionPreference = 'Stop'
$TaskName = 'telegram-devin-bridge'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

function Find-Python {
    # probing missing versions writes to stderr, which 'Stop' turns fatal
    $ErrorActionPreference = 'Continue'
    $candidates = @(
        @('py', '-3.14'), @('py', '-3.13'), @('py', '-3.12'),
        @('python3'), @('python')
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $pyArgs = @($c | Select-Object -Skip 1)
        $exe = & $c[0] @pyArgs -c 'import sys; print(sys.executable) if sys.version_info >= (3, 12) else None' 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) { return "$exe".Trim() }
    }
    return $null
}

function Find-GitSh {
    # <Git>\bin\sh.exe (next to <Git>\cmd\git.exe) puts coreutils on PATH;
    # a bare <Git>\usr\bin\sh.exe would lack them
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        $candidate = Join-Path (Split-Path (Split-Path $git.Source)) 'bin\sh.exe'
        if (Test-Path $candidate) { return $candidate }
    }
    $sh = Get-Command sh -ErrorAction SilentlyContinue
    if ($sh) { return $sh.Source }
    return $null
}

$python = Find-Python
$sh = Find-GitSh
if ($DryRun) {
    "bridge home: $root"
    "python: $(if ($python) { $python } else { 'missing' })"
    "git: $(if (Get-Command git -ErrorAction SilentlyContinue) { 'found' } else { 'missing' })"
    "sh: $(if ($sh) { $sh } else { 'missing' })"
    exit 0
}
if (-not $python) { throw 'Python >= 3.12 is required: winget install Python.Python.3.12' }
if (-not $sh) { throw 'Git for Windows is required: winget install Git.Git' }
if (-not (Test-Path (Join-Path $root '.git'))) {
    Write-Warning 'not a git checkout; /update will be unavailable'
}

Set-Location $root
if (-not (Test-Path '.venv\Scripts\python.exe')) { & $python -m venv .venv }
& .venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }
function Write-EnvValue([string]$Key, [string]$Value) {
    $lines = @(Get-Content '.env' -Encoding UTF8)
    $line = "$Key=$Value"
    if ($lines -match "^$Key=") {
        $lines = $lines -replace "^$Key=.*", $line.Replace('$', '$$')
    } else {
        $lines += $line
    }
    # UTF-8 without BOM: python-dotenv would read a BOM into the first key
    [IO.File]::WriteAllLines((Join-Path $root '.env'), [string[]]$lines)
}
# forward slashes: SELF_UPDATE_COMMAND is split with POSIX shlex rules;
# the outer single quotes keep python-dotenv from rejecting the inner ones
$shPosix = $sh.Replace('\', '/')
Write-EnvValue 'TELEGRAM_MODE' 'polling'
Write-EnvValue 'ADMIN_LOG_PATH' 'logs/bridge.log'
Write-EnvValue 'SELF_UPDATE_COMMAND' "'`"$shPosix`" deploy/self-update.sh'"
# .env holds the bot and Devin tokens: current user only, no inherited ACEs
icacls .env /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'could not restrict .env permissions' }

if ($NoTask) {
    "installed; run: powershell -ExecutionPolicy Bypass -File $root\deploy\windows\run.ps1"
    exit 0
}
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\deploy\windows\run.ps1`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# no execution time limit (the default stops tasks after 72 h)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
# run.ps1 stops a poller the previous task instance left behind
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
"started scheduled task $TaskName; logs: $root\logs\bridge.log"
"fill in $root\.env, then: Stop-ScheduledTask $TaskName; Start-ScheduledTask $TaskName"
