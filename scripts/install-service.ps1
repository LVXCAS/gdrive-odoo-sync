<#
.SYNOPSIS
    Register DriftWatch as a Windows scheduled task so it runs 24/7.

.DESCRIPTION
    Creates a task that starts the daemon at boot and restarts it if it dies.

    WHY a scheduled task rather than a real Windows service: a real service
    must implement the Service Control Manager protocol, which for a Python
    program means pywin32 and a wrapper, i.e. two more dependencies and a
    second thing that can be misconfigured. Task Scheduler starts a plain
    process at boot, restarts it on failure, and is inspectable from a GUI
    that already exists on the machine. The daemon supervises its own cycles;
    the OS only has to keep the process alive.

    Run elevated for true 24/7 (starts at boot, survives sign-out). Without
    elevation the task is registered to run at sign-in instead, and the
    script says so plainly rather than pretending otherwise.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-service.ps1

.EXAMPLE
    # Every 15 minutes, structural checks only (no Odoo contact)
    .\scripts\install-service.ps1 -Interval 15m -StagingOnly
#>
[CmdletBinding()]
param(
    [string] $TaskName = 'DriftWatch',
    [string] $Interval = '',
    [switch] $StagingOnly,
    [string] $Mapping = '',
    [switch] $Incremental,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'Resolve-Python.ps1')

function Say([string] $msg, [string] $colour = 'Gray') {
    Write-Host $msg -ForegroundColor $colour
}

Say "DriftWatch service install" 'Cyan'
Say "  repo   : $RepoRoot"

# --------------------------------------------------------------------------
# 1. Interpreter
# --------------------------------------------------------------------------
$python = Resolve-DriftWatchPython -RepoRoot $RepoRoot
if (-not $python) {
    throw ("No usable Python found. Install Python 3.11+ from python.org " +
           "(not the Microsoft Store build) and re-run.")
}
Say "  python : $python"

if (-not (Test-DriftWatchDeps -Python $python)) {
    Say "  deps   : MISSING" 'Yellow'
    Say ""
    Say "Install them into that exact interpreter first:" 'Yellow'
    Say "    & '$python' -m pip install -r '$RepoRoot\requirements.txt'" 'Yellow'
    throw "Dependencies are not importable by the interpreter the task would use."
}
Say "  deps   : OK"

# --------------------------------------------------------------------------
# 2. Configuration must exist BEFORE the task does.
#
# A task that boots into a missing .env fails silently at 3am. Better to
# refuse to install than to install something that cannot work.
# --------------------------------------------------------------------------
$envFile = Join-Path $RepoRoot '.env'
if (-not (Test-Path $envFile)) {
    Say ""
    Say "No .env found at $envFile" 'Yellow'
    Say "Copy the template and fill it in first:" 'Yellow'
    Say "    copy '$RepoRoot\.env.example' '$envFile'" 'Yellow'
    if (-not $Force) { throw "Refusing to install a service with no configuration (-Force to override)." }
    Say "-Force given; installing anyway." 'Yellow'
}

# --------------------------------------------------------------------------
# 3. Build the command
# --------------------------------------------------------------------------
# --interval takes 900 / 15m / 2h directly, so it passes straight through.
$argList = @('-m', 'driftwatch', 'daemon')
if ($Interval)    { $argList += @('--interval', $Interval) }
if ($StagingOnly) { $argList += '--staging-only' }
if ($Incremental) { $argList += '--incremental' }
if ($Mapping)     { $argList += @('--mapping', $Mapping) }

$arguments = ($argList | ForEach-Object {
    if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
}) -join ' '
Say "  command: $python $arguments"

# --------------------------------------------------------------------------
# 4. Register
# --------------------------------------------------------------------------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
    Say ""
    Say "Task '$TaskName' already exists. Re-run with -Force to replace it." 'Yellow'
    Say "Or remove it: .\scripts\uninstall-service.ps1 -TaskName $TaskName" 'Yellow'
    return
}

$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $RepoRoot

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$elevated = ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

$triggers = @()
if ($elevated) {
    # S4U: runs whether or not anyone is signed in, with no stored password.
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name `
        -LogonType S4U -RunLevel Limited
    $triggers += New-ScheduledTaskTrigger -AtStartup
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name `
        -LogonType Interactive -RunLevel Limited
    $triggers += New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
}

# A repeating trigger is the safety net: if the process ever exits and the
# restart budget is spent, this starts it again. MultipleInstances=IgnoreNew
# makes it a no-op while the daemon is healthy, so it can only help.
$revive = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$triggers += $revive

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)     # zero = never time it out

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings `
    -Description 'DriftWatch: read-only Google Drive vs Odoo verification.' `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Say ""
Say "Registered and started: $TaskName" 'Green'
if ($elevated) {
    Say "  starts at boot, runs whether or not you are signed in." 'Green'
} else {
    Say "  NOT elevated: this task starts at SIGN-IN and stops when you" 'Yellow'
    Say "  sign out. For true 24/7, re-run this script as Administrator." 'Yellow'
}
Say ""
Say "Check on it:"
Say "    Get-ScheduledTask $TaskName | Get-ScheduledTaskInfo"
Say "    & '$python' -m driftwatch status"
Say "    Get-Content '$RepoRoot\logs\driftwatch.log' -Tail 40 -Wait"
Say ""
Say "Prove the mail path now, rather than the first time drift is real:"
Say "    & '$python' -m driftwatch alert-test"
