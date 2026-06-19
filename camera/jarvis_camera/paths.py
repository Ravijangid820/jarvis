"""Where config/, models/, and keys live — works both from source and as a PyInstaller .exe.

Normally this is the `camera/` directory. When frozen into a single .exe, it's the folder the .exe
sits in (so config/agent.key + models/ live next to the executable). Keeping this in one place means
the rest of the code is identical in both modes.
"""
import sys
from pathlib import Path


def base_dir():
    if getattr(sys, "frozen", False):           # running as the packaged .exe
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]   # camera/
