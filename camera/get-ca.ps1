<#
  Download THIS deployment's CA certificate from its own server into config\ca.crt, so the agent can
  verify the server over HTTPS. The bootstrap fetch is over an untrusted connection (you don't trust
  the CA yet) — so VERIFY the printed fingerprint matches what setup_tls.sh showed on the server.

      powershell -ExecutionPolicy Bypass -File get-ca.ps1
#>
$ErrorActionPreference = "Stop"
$cam = Split-Path -Parent $MyInvocation.MyCommand.Path
$py  = Join-Path $cam ".venv\Scripts\python.exe"
$cfg = Join-Path $cam "config\config.json"
if (-not (Test-Path $cfg)) { throw "No config\config.json — run setup.ps1 first." }
$url = (& $py -c "import json,sys;print(json.load(open(sys.argv[1]))['server']['url'].rstrip('/'))" $cfg)

New-Item -ItemType Directory -Force -Path (Join-Path $cam "config") | Out-Null
$out = Join-Path $cam "config\ca.crt"
Write-Host "Fetching CA from $url/ca.crt (untrusted bootstrap)..."
& curl.exe -fsSk "$url/ca.crt" -o $out          # curl.exe (not the PS alias); -k: we don't trust it YET
if ($LASTEXITCODE -ne 0) { throw "download failed — is the server up and server.url correct?" }

$fp = (Get-FileHash $out -Algorithm SHA256).Hash.ToLower()
Write-Host "Saved $out" -ForegroundColor Green
Write-Host "VERIFY this SHA-256 matches the server's setup_tls.sh output before trusting it:" -ForegroundColor Yellow
Write-Host "  $fp"
