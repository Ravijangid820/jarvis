<#
  Run the camera agent in the FOREGROUND (testing) on Windows — no service, nothing persistent.
  Ctrl-C stops it. (Run setup.ps1 first.) Extra args pass through to the agent.

      powershell -ExecutionPolicy Bypass -File run.ps1              # live
      powershell -ExecutionPolicy Bypass -File run.ps1 --dry-run    # logs events, sends nothing
#>
$ErrorActionPreference = "Stop"
$cam = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $cam ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "No venv - run:  powershell -ExecutionPolicy Bypass -File setup.ps1"; exit 1 }
Set-Location $cam
& $py -m jarvis_camera.agent @args
