<#
  Make the camera agent PERSISTENT on Windows via a **Scheduled Task** that starts at YOUR logon.
  Secure by design: runs as the current user (NOT SYSTEM), NOT elevated, only while you're logged on
  (a webcam needs your interactive session anyway), no third-party service wrapper, no listening
  socket. Run setup.ps1 first.

      powershell -ExecutionPolicy Bypass -File service.ps1 install
      powershell -ExecutionPolicy Bypass -File service.ps1 uninstall
      powershell -ExecutionPolicy Bypass -File service.ps1 status
#>
param([Parameter(Mandatory)][ValidateSet("install","uninstall","status")]$Action)
$ErrorActionPreference = "Stop"
$cam = Split-Path -Parent $MyInvocation.MyCommand.Path
$task = "JarvisCamera"

switch ($Action) {
  "install" {
    # pythonw.exe = no console window; fall back to python.exe if absent
    $py = Join-Path $cam ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path $py)) { $py = Join-Path $cam ".venv\Scripts\python.exe" }
    if (-not (Test-Path $py)) { throw "No venv - run:  powershell -ExecutionPolicy Bypass -File setup.ps1" }
    $action  = New-ScheduledTaskAction -Execute $py -Argument "-m jarvis_camera.agent" -WorkingDirectory $cam
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    # Least privilege: your account, interactive (so it can use the webcam), NOT elevated.
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                   -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Principal $principal `
                           -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $task
    Write-Host "Installed + started '$task' — starts at your logon, as you, not elevated." -ForegroundColor Green
    Write-Host "  (No admin needed; it's a per-user task. Stop/remove with: service.ps1 uninstall)"
  }
  "uninstall" {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed '$task'."
  }
  "status" {
    $t = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    if ($null -eq $t) { Write-Host "Not installed." } else { $t | Get-ScheduledTaskInfo }
  }
}
