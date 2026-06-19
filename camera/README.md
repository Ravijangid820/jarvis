# Jarvis Camera — on-device vision agent (code)

On-device camera agent (motion, faces + identity, pose, gestures) that sends **high-level events**
(not video) to the orchestrator. Runs on a **Windows/macOS/Linux laptop webcam** *and* a **Raspberry
Pi** camera — same code, the capture layer picks the backend automatically.

## Platform support (why one shared codebase)

The camera hardware differs by platform, but that difference is isolated to **`capture.py`**, which
returns a plain BGR frame whether it came from a Pi CSI camera (`picamera2`) or a USB/laptop webcam
(OpenCV) — every detector and the agent are backend-agnostic. The only other platform-specific
files are the deps and the bootstrap:

| Differs per platform | Lives in |
|---|---|
| Camera backend (CSI vs webcam) | `capture.py` (auto-selected) + `config.json` (`camera.backend`/`device`) |
| Dependencies (apt vs pip) | `requirements.txt` (Pi) · `requirements-desktop.txt` (laptop/Windows) |
| Bootstrap | `setup.sh` (Pi/Unix) · `setup.ps1` (Windows) |

So Windows and Pi share the detectors, agent, recognition, and event/credential code (one place to
keep secure) and differ only where they must. The OpenCV/laptop path is tested; the picamera2/Pi
path is written to the standard API but **unrun on real Pi hardware** yet.

📖 **Setup & full docs:** [docs/setup/camera.md](../docs/setup/camera.md)
