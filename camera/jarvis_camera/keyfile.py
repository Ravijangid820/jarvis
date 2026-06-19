"""Security-conscious API-key loading for the camera agent + management CLIs.

Keys live in files under `camera/config/` (gitignored — never committed). Two kinds, kept
**separate on purpose** to minimise what the always-on process can do if the device is compromised:

  - **device key** (`api_key_file`, default `config/agent.key`): device-bound, *low privilege*. The
    always-on `agent` uses ONLY this. It can post events as its own device and read the enrolled
    face set — it CANNOT enroll/delete faces, control devices, or read anyone else's data.
  - **admin key** (`admin_key_file`, default `config/admin.key`): *high privilege*, used ONLY by the
    transient `add`/`delete` management commands. The running agent never loads it; keep it off the
    device when you're not actively managing faces (delete the file, or run management elsewhere).

This split means a stolen device key's blast radius is bounded to "post fake events / read enrolled
names" — it can never escalate to changing who is authorized.
"""
import logging
import os
from pathlib import Path

log = logging.getLogger("camera.keyfile")


def load_key(path, root):
    """Return the stripped key from `path` (resolved under `root` if relative), or "" if absent.

    Warns loudly if the secret is group/other-readable (POSIX) — a key file must be 0600. On
    Windows, keep it under your user profile (per-user ACLs); the POSIX bit check is skipped there.
    """
    p = Path(path)
    if not p.is_absolute():
        p = Path(root) / p
    if not p.exists():
        return ""
    try:
        if os.name == "posix" and (p.stat().st_mode & 0o077):
            try:
                p.chmod(0o600)                       # best-effort: tighten a too-open key file
                log.warning("%s was group/other-readable — tightened to 600.", p)
            except OSError:
                log.warning("%s is group/other-readable and couldn't be tightened — run: chmod 600 %s", p, p)
    except OSError:
        pass
    return p.read_text().strip()
