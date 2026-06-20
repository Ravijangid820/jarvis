<#
  Save the camera's API key to config\agent.key (or config\admin.key) WITHOUT PowerShell's
  positional/quoting/encoding pitfalls (ASCII, no BOM, no trailing newline).

      powershell -ExecutionPolicy Bypass -File set-key.ps1 jk-xxxxxxxxxxxx           # device key  -> config\agent.key
      powershell -ExecutionPolicy Bypass -File set-key.ps1 jk-xxxxxxxxxxxx -Admin    # admin key   -> config\admin.key
#>
param(
  [Parameter(Mandatory = $true, Position = 0)][string]$Key,
  [switch]$Admin
)
$ErrorActionPreference = "Stop"
$cam = Split-Path -Parent $MyInvocation.MyCommand.Path
$dir = Join-Path $cam "config"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$name = if ($Admin) { "admin.key" } else { "agent.key" }
$file = Join-Path $dir $name
[System.IO.File]::WriteAllText($file, $Key.Trim())   # UTF-8 no BOM, exact bytes
Write-Host "Wrote $file  ($($Key.Trim().Length) chars)" -ForegroundColor Green
