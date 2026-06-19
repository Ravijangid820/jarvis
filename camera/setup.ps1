<#
  Bootstrap the Jarvis camera agent on a WINDOWS laptop — to test the camera WITHOUT a Pi.

  Everything is sandboxed: a uv-managed Python 3.12 + a project-local .venv. Nothing is installed
  globally (uv itself is a single user-level binary; all packages live in camera\.venv).

  Run from the camera\ directory:
      powershell -ExecutionPolicy Bypass -File setup.ps1            # camera + motion only
      powershell -ExecutionPolicy Bypass -File setup.ps1 -WithFaces # + face/pose/gesture + identity
#>
param([switch]$WithFaces)
$ErrorActionPreference = "Stop"
$cam = Split-Path -Parent $MyInvocation.MyCommand.Path
function Info($m) { Write-Host "==> $m" -ForegroundColor Cyan }

Info "Checking for uv (the env/sandbox manager)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "  uv not found. Install it once (user-level, NOT global), then re-run this script:" -ForegroundColor Yellow
  Write-Host '    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"' -ForegroundColor Yellow
  Write-Host "  (then open a new terminal so PATH picks up uv)" -ForegroundColor Yellow
  exit 1
}

# MediaPipe has no Python 3.13 wheels yet — pin 3.12 (uv fetches a managed CPython into its own
# cache under your user profile; it does NOT touch any system Python).
Info "Creating sandboxed Python 3.12 venv at camera\.venv"
uv venv --python 3.12 "$cam\.venv"
$py = Join-Path $cam ".venv\Scripts\python.exe"

Info "Installing desktop deps into the venv (opencv-python + numpy + requests)"
uv pip install --python $py -r "$cam\requirements-desktop.txt"

if ($WithFaces) {
  Info "Installing face/pose/gesture deps (mediapipe + onnxruntime) into the venv"
  uv pip install --python $py "mediapipe>=0.10,<0.11" "onnxruntime>=1.17,<2"
}

Info "Config"
New-Item -ItemType Directory -Force -Path "$cam\config" | Out-Null
if (-not (Test-Path "$cam\config\config.json")) {
  Copy-Item "$cam\config.example.json" "$cam\config\config.json"
  Write-Host "  wrote config\config.json — review it (see step 1 below)"
}

Write-Host ""
Write-Host "Setup done. Run everything via the venv's Python (fully sandboxed):" -ForegroundColor Green
Write-Host "  1. Edit config\config.json:" -ForegroundColor Green
Write-Host "       device_id = `"laptop-cam`"   server.url = `"http://192.168.0.101:5000`""
Write-Host "       camera.backend = `"auto`"     (for faces: detectors.faces.enabled = true)"
Write-Host "  2. Test with NO server first (proves the webcam + detectors):"
Write-Host "       .venv\Scripts\python -m jarvis_camera.bench --frames 60"
Write-Host "       .venv\Scripts\python -m jarvis_camera.agent --dry-run"
Write-Host "  3. Go live: on the SERVER mint a device key, save it to camera\config\agent.key, then:"
Write-Host "       .venv\Scripts\python -m jarvis_camera.agent"
Write-Host "     Watch it turn green in the admin -> Overview -> 'Camera . laptop-cam'."
