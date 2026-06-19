<#
  One-click Windows install: runs setup (deps + verified model download) THEN installs the
  persistent logon service. This script does nothing on its own except chain the other two —
  read setup.ps1 and service.ps1 to see exactly what runs (nothing needs admin).

      powershell -ExecutionPolicy Bypass -File install.ps1            # camera + faces
      powershell -ExecutionPolicy Bypass -File install.ps1 -WithPose  # also pose + gestures
#>
param([switch]$WithPose)
$ErrorActionPreference = "Stop"
$cam = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "==> Jarvis Camera: one-click install (setup + service)" -ForegroundColor Cyan
$setupArgs = @(); if ($WithPose) { $setupArgs += '-WithPose' }
& "$cam\setup.ps1" @setupArgs
if ($LASTEXITCODE) { throw "setup.ps1 failed (exit $LASTEXITCODE) - fix the above and re-run." }
& "$cam\service.ps1" install

Write-Host ""
Write-Host "==> Installed. The camera agent now starts at your logon." -ForegroundColor Green
Write-Host "    Before it can recognize anyone: save your DEVICE key to  camera\config\agent.key"
Write-Host "    Manage:  service.ps1 status   |   service.ps1 uninstall"
