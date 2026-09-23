<#
    Registers a Windows Task Scheduler job that runs the NAV catalog report
    export every day at 5:00 AM (machine local time — set the machine to US
    Eastern, or pass -At to match).

    Run once, in an elevated PowerShell (Run as Administrator):

        powershell -ExecutionPolicy Bypass -File scripts\register_nav_report_task.ps1

    Options:
        -At    "05:00"        time of day to run (local machine time)
        -Name  "NAV Catalog Report"   task name

    To remove it later:  Unregister-ScheduledTask -TaskName "NAV Catalog Report"
#>
param(
    [string]$At = '05:00',
    [string]$Name = 'NAV Catalog Report'
)
$ErrorActionPreference = 'Stop'

$runner = Join-Path $PSScriptRoot 'run_nav_report.ps1'
if (-not (Test-Path $runner)) { throw "Runner not found: $runner" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Daily per-xltoken NAV catalog report -> Google Sheets/Drive' `
    -Force

Write-Host "Registered scheduled task '$Name' to run daily at $At (local time)."
Write-Host "Runner: $runner"