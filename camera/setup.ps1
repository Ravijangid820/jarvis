<#
  Bootstrap the Jarvis camera agent on a WINDOWS laptop. Sandboxed: a uv-managed Python 3.12 + a
  project-local .venv. Nothing is installed globally (uv is a single user-level binary; all packages
  live in camera\.venv). Faces (YuNet+SFace) need only opencv — the models are downloaded and
  sha256-verified below. For Linux/macOS/Pi use setup.sh.

  Run from the camera\ directory:
      powershell -ExecutionPolicy Bypass -File setup.ps1            # camera + faces
      powershell -ExecutionPolicy Bypass -File setup.ps1 -WithPose  # also mediapipe (pose + gestures)
#>
param([switch]$WithPose)
$ErrorActionPreference = "Stop"
$cam = Split-Path -Parent $MyInvocation.MyCommand.Path
function Info($m) { Write-Host "==> $m" -ForegroundColor Cyan }

Info "Platform: Windows"
Info "Checking for uv (the env/sandbox manager)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "  uv not found. Install it once (user-level, NOT global), then re-run this script:" -ForegroundColor Yellow
  Write-Host "    winget install astral-sh.uv        # preferred: auditable + pinned, no pipe-to-run" -ForegroundColor Yellow
  Write-Host '    or: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"' -ForegroundColor Yellow
  Write-Host "  (then open a new terminal so PATH picks up uv)" -ForegroundColor Yellow
  exit 1
}

# Pin 3.12 (keeps the optional mediapipe path open — it has no 3.13 wheels yet). uv fetches a managed
# CPython into its own cache under your user profile; it does NOT touch any system Python.
Info "Creating sandboxed Python 3.12 venv at camera\.venv"
uv venv --python 3.12 "$cam\.venv"
$py = Join-Path $cam ".venv\Scripts\python.exe"

Info "Installing deps into the venv (opencv-python + numpy + requests)"
uv pip install --python $py -r "$cam\requirements-desktop.txt"

if ($WithPose) {
  Info "Optional pose/gestures: mediapipe"
  uv pip install --python $py "mediapipe>=0.10,<0.11"
}

# ---- face models: official OpenCV Zoo, sha256-verified ----
Info "Face models (YuNet + SFace) - official OpenCV Zoo, sha256-verified"
$zoo = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
$models = @(
  @{ name = "face_detection_yunet_2023mar.onnx";
     url  = "$zoo/face_detection_yunet/face_detection_yunet_2023mar.onnx";
     sha  = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4" },
  @{ name = "face_recognition_sface_2021dec.onnx";
     url  = "$zoo/face_recognition_sface/face_recognition_sface_2021dec.onnx";
     sha  = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79" }
)
New-Item -ItemType Directory -Force -Path "$cam\models" | Out-Null
foreach ($m in $models) {
  $out = Join-Path $cam ("models\" + $m.name)
  if ((Test-Path $out) -and ((Get-FileHash $out -Algorithm SHA256).Hash.ToLower() -eq $m.sha)) {
    Write-Host "  $($m.name) cached"; continue
  }
  Write-Host "  downloading $($m.name) ..."
  Invoke-WebRequest -Uri $m.url -OutFile $out
  if ((Get-FileHash $out -Algorithm SHA256).Hash.ToLower() -ne $m.sha) {
    Remove-Item $out; throw "SHA-256 mismatch for $($m.name) - refusing (supply-chain check failed)"
  }
  Write-Host "  $($m.name) verified"
}

Info "Config"
New-Item -ItemType Directory -Force -Path "$cam\config" | Out-Null
if (-not (Test-Path "$cam\config\config.json")) {
  Copy-Item "$cam\config.example.json" "$cam\config\config.json"
  Write-Host "  wrote config\config.json - review it (see step 1 below)"
}

Write-Host ""
Write-Host "Setup done. Run everything via the venv's Python (fully sandboxed):" -ForegroundColor Green
Write-Host "  1. Edit config\config.json: device_id, server.url (e.g. http://192.168.0.101:5000)."
Write-Host "  2. On the SERVER: admin -> Keys, mint a DEVICE key (Device ID = this camera; under a"
Write-Host "     NON-admin user), then save it with the helper (avoids quoting pitfalls):"
Write-Host "       powershell -ExecutionPolicy Bypass -File set-key.ps1 jk-yourkey"
Write-Host "  3. Test first (no key/server needed):"
Write-Host "       .venv\Scripts\python -m jarvis_camera.agent --dry-run    # events logged, not sent"
Write-Host "  4. Go live:  .venv\Scripts\python -m jarvis_camera.agent      # turns green in admin -> Overview"
Write-Host "  (To enroll faces, also: set-key.ps1 jk-ADMINkey -Admin   then  facecli add --name '...'; remove admin.key after.)"
