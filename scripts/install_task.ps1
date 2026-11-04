<#
.SYNOPSIS
    Register (or remove) the Windows Scheduled Task that keeps the training data fresh.

.DESCRIPTION
    Runs scripts/sync_now.py on a schedule, whether or not the web app happens to be running.
    That distinction is the whole point: the in-server APScheduler loop only works while the
    server is up, and the six-week data gap this project exists to fix happened because
    nothing ran when nobody was looking.

    Overlap with the in-server loop is safe. run_cycle() takes a cross-process file lock and
    whichever loses records a skip rather than an error.

    The task runs only when a user is logged on and does not wake the machine. Both are
    deliberate: it needs the network and the .env credentials, and a laptop that wakes hourly
    to sync a training plan is a worse problem than a plan that syncs a bit later. Missed runs
    are started as soon as possible afterwards, and the cycle re-derives everything from
    scratch, so a late run is as good as an on-time one.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -IntervalMinutes 120
    powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Uninstall
#>
param(
    [int]$IntervalMinutes = 60,
    [switch]$Uninstall,
    [string]$TaskName = "FitnessCoach-Sync"
)

$ErrorActionPreference = "Stop"

$repo   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\pythonw.exe"
$script = Join-Path $repo "scripts\sync_now.py"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled task '$TaskName'."
    } else {
        Write-Output "No scheduled task named '$TaskName'."
    }
    return
}

# pythonw.exe rather than python.exe so an hourly job does not flash a console window all day.
# It ships with the venv; fall back only if it genuinely is not there.
if (-not (Test-Path $python)) {
    $python = Join-Path $repo ".venv\Scripts\python.exe"
}
if (-not (Test-Path $python)) {
    throw "No interpreter at $python. Create the venv first."
}
if (-not (Test-Path $script)) {
    throw "Missing $script."
}

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $repo

# Two separate triggers rather than one logon trigger with a repetition grafted onto it.
#
# Leaving -RepetitionDuration unset is what means "repeat indefinitely": it serialises to an
# empty Duration in the task XML. Passing [TimeSpan]::MaxValue looks like it should say the
# same thing and does not -- it serialises to P99999999DT23H59M59S, which Task Scheduler
# rejects outright with "a value which is incorrectly formatted or out of range".
#
# StopAtDurationEnd defaults to True, which is harmless against an empty duration but says
# the opposite of what is meant, so it is set explicitly.
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$logonTrigger.Delay = "PT2M"

$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$repeatTrigger.Repetition.StopAtDurationEnd = $false

$trigger = @($logonTrigger, $repeatTrigger)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Fitness Coach: pull intervals.icu data, roll the plan, push the horizon." `
    -Force | Out-Null

Write-Output "Registered '$TaskName': every $IntervalMinutes minutes, and ~2 minutes after logon."
Write-Output "  python : $python"
Write-Output "  script : $script"
Write-Output "  log    : $(Join-Path $repo 'data\maintenance.log')"
Write-Output ""
Write-Output "Run it now:      Start-ScheduledTask -TaskName $TaskName"
Write-Output "Check last run:  Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Output "Remove it:       powershell -File scripts\install_task.ps1 -Uninstall"
