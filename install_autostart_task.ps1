param(
  [string]$TaskName = "SelfImproverBot",
  [string]$PythonExe = "python",
  [string]$ConfigPath = "config.json"
)

$workdir = (Get-Location).Path
$runCmd = "cd `"$workdir`"; $PythonExe .\bot_guardian.py --config `"$ConfigPath`""

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -Command $runCmd"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Autonomous self-improving bot loop" -Force
Write-Output "Scheduled task '$TaskName' installed."
