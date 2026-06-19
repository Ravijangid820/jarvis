r"""Single entry point for the packaged Windows .exe (built by .github/workflows/build-camera-exe.yml).

    jarvis-camera.exe                  run the agent (default)
    jarvis-camera.exe run [--dry-run]  run the agent
    jarvis-camera.exe verify           one-shot: who is at the camera now (local)
    jarvis-camera.exe setup            download+verify models + write a config template
    jarvis-camera.exe install-service  start at logon (per-user Scheduled Task; NOT elevated)
    jarvis-camera.exe uninstall-service
    jarvis-camera.exe status

Everything lives next to the .exe: config\config.json, config\agent.key, models\. No admin needed;
the Scheduled Task runs as you, not elevated, and the agent opens no listening port (outbound-only).
"""
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import base_dir
from . import models

TASK = "JarvisCamera"


def _cfg_path():
    return base_dir() / "config" / "config.json"


def _ensure_setup():
    base = base_dir()
    models.ensure_models(base / "models")
    cfg = _cfg_path()
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        example = base / "config.example.json"
        if not example.exists():                       # PyInstaller onefile bundles it under _MEIPASS
            example = Path(getattr(sys, "_MEIPASS", base)) / "config.example.json"
        shutil.copyfile(example, cfg)
        print(f"Wrote {cfg}\n  → set server.url, and put your device key in config\\agent.key")


def _install_service():
    if not getattr(sys, "frozen", False):
        sys.exit("install-service is for the packaged .exe. From source use service.ps1 / service.sh.")
    exe = sys.executable
    subprocess.run(["schtasks", "/Create", "/TN", TASK, "/TR", f'"{exe}" run',
                    "/SC", "ONLOGON", "/RL", "LIMITED", "/F"], check=True)
    subprocess.run(["schtasks", "/Run", "/TN", TASK], check=False)
    print(f"Installed Scheduled Task '{TASK}' — starts at your logon, as you, not elevated.")


def _uninstall_service():
    subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"], check=False)
    print(f"Removed Scheduled Task '{TASK}'.")


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "run"
    if cmd == "install-service":
        _ensure_setup(); _install_service()
    elif cmd == "uninstall-service":
        _uninstall_service()
    elif cmd == "status":
        subprocess.run(["schtasks", "/Query", "/TN", TASK], check=False)
    elif cmd == "setup":
        _ensure_setup()
    elif cmd == "verify":
        from . import facecli
        sys.argv = ["facecli", "verify"] + argv[1:]
        facecli.main()
    else:  # run (default)
        from . import agent
        _ensure_setup()
        agent.run(str(_cfg_path()), dry_run=("--dry-run" in argv))


if __name__ == "__main__":
    main()
