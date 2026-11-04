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
    [switch]$StartupShortcut,
    [string]$TaskName = "FitnessCoach-Sync"
)

$ErrorActionPreference = "Stop"

$repo   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\pythonw.exe"
$script = Join-Path $repo "scripts\sync_now.py"
$startupDir = [Environment]::GetFolderPath("Startup")
$shortcut   = Join-Path $startupDir "FitnessCoach.lnk"

if ($StartupShortcut) {
    # The admin-free path: start the app at logon and let the APScheduler loop inside it do
    # the syncing. Weaker than a scheduled task -- it only runs while logged on, and a laptop
    # that stays shut all weekend syncs nothing -- but it needs no privileges beyond writing
    # a file into the user's own Startup folder, and it is what the Scheduled Task was for.
    if (-not (Test-Path $python)) { $python = Join-Path $repo ".venv\Scripts\python.exe" }
    $serve = Join-Path $repo "scripts\serve.py"
    if (-not (Test-Path $serve)) { throw "Missing $serve." }

    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($shortcut)
    $lnk.TargetPath = $python
    $lnk.Arguments = "`"$serve`""
    $lnk.WorkingDirectory = $repo
    $lnk.Description = "Fitness Coach - local app and maintenance loop"
    $lnk.Save()

    Write-Output "Created $shortcut"
    Write-Output "  Starts the app at logon; it syncs every SYNC_INTERVAL_MINUTES (default 60)"
    Write-Output "  while running, and serves http://127.0.0.1:8800"
    Write-Output "  Remove it:  Remove-Item '$shortcut'"
    return
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled task '$TaskName'."
    } else {
        Write-Output "No scheduled task named '$TaskName'."
    }
    if (Test-Path $shortcut) {
        Remove-Item $shortcut
        Write-Output "Removed $shortcut"
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

# An explicit Interactive principal, which is the difference between this registering and
# failing with 0x80070005 "Access is denied" on a standard account.
#
# Register-ScheduledTask leaves the principal empty by default, and the service then falls
# back to S4U -- "run whether the user is logged on or not" -- which requires the "Log on as a
# batch job" right. Standard users do not have it, and the error says only "Access is denied",
# which reads like it wants elevation. It does not; it wants a different logon type. Interactive
# is also what this task was documented as doing in the first place.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description "Fitness Coach: pull intervals.icu data, roll the plan, push the horizon." `
        -Force | Out-Null
} catch {
    Write-Warning "Could not register the scheduled task: $($_.Exception.Message)"
    Write-Output ""
    Write-Output "This account is not an administrator, so if the Task Scheduler service is"
    Write-Output "refusing task creation outright there is no way around it from here."
    Write-Output ""
    Write-Output "Admin-free alternative -- run the app at logon and let its own loop do the work:"
    Write-Output "    powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -StartupShortcut"
    Write-Output ""
    Write-Output "It syncs on the same interval, but only while you are logged on."
    exit 1
}

Write-Output "Registered '$TaskName': every $IntervalMinutes minutes, and ~2 minutes after logon."
Write-Output "  python : $python"
Write-Output "  script : $script"
Write-Output "  log    : $(Join-Path $repo 'data\maintenance.log')"
Write-Output ""
Write-Output "Run it now:      Start-ScheduledTask -TaskName $TaskName"
Write-Output "Check last run:  Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Output "Remove it:       powershell -File scripts\install_task.ps1 -Uninstall"
