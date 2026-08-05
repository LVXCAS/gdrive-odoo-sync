<#
.SYNOPSIS
    Stop and remove the DriftWatch scheduled task.

.DESCRIPTION
    Removes the task only. The datastore, the logs and .env are left exactly
    where they are -- driftwatch.sqlite3 holds a copy of real business data,
    and an uninstall script is not the right place to decide that should go.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall-service.ps1
#>
[CmdletBinding()]
param([string] $TaskName = 'DriftWatch')

$ErrorActionPreference = 'Stop'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "No scheduled task named '$TaskName'. Nothing to do." -ForegroundColor Gray
    return
}

if ($task.State -eq 'Running') {
    Write-Host "Stopping $TaskName ..." -ForegroundColor Gray
    Stop-ScheduledTask -TaskName $TaskName
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
Write-Host "The datastore, logs and .env were left in place." -ForegroundColor Gray
