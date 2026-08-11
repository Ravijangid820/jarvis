"""The hardened unit template and the installer must agree on what the service may write.

There are two copies of that list — systemd/jarvis-orchestrator.hardened.service (read by hand,
copied by people who don't run the installer) and ORCH_WRITABLE_DIRS in install_services.sh (what
a real install actually gets). They drifted once already: the template never listed backups/, and
NEITHER listed config/, so on every hardened install /models/switch, /voice/mics/select and
POST /mcp/servers wrote to a read-only mount.

That class of bug is invisible until someone uses the feature, months later, and reads
"Read-only file system" in the journal. These tests are the alarm.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "systemd" / "jarvis-orchestrator.hardened.service"
INSTALLER = REPO / "src" / "scripts" / "install_services.sh"


def _unit_writable_dirs() -> set:
    """The ReadWritePaths= set from the template, as repo-relative directory names."""
    line = next(ln for ln in UNIT.read_text().splitlines() if ln.startswith("ReadWritePaths="))
    return {p.removeprefix("/srv/jarvis/") for p in line.split("=", 1)[1].split()}


def _installer_writable_dirs() -> set:
    """The ORCH_WRITABLE_DIRS assignment from the installer."""
    m = re.search(r'^ORCH_WRITABLE_DIRS="([^"]*)"', INSTALLER.read_text(), re.M)
    assert m, "ORCH_WRITABLE_DIRS not found in install_services.sh"
    return set(m.group(1).split())


def test_template_and_installer_agree_on_writable_paths():
    assert _unit_writable_dirs() == _installer_writable_dirs()


def test_config_is_writable():
    """Regression: the app persists runtime selections into config/.

    active_model.json (/models/switch), active_mic.json (/voice/mics/select) and
    mcp_servers.json (POST /mcp/servers) all live there. Whoever removes config/ from the
    writable set has to make those endpoints write somewhere else first.
    """
    assert "config" in _unit_writable_dirs()
    assert "config" in _installer_writable_dirs()


def test_every_writable_path_is_inside_the_repo():
    """ReadWritePaths is a hole in ProtectSystem=strict — it must not open one outside the repo."""
    line = next(ln for ln in UNIT.read_text().splitlines() if ln.startswith("ReadWritePaths="))
    for path in line.split("=", 1)[1].split():
        assert path.startswith("/srv/jarvis/"), path


def test_installer_writable_dirs_cover_every_config_writer():
    """Find BASE_DIR-rooted writes in the orchestrator and check the unit permits them.

    Not exhaustive — it only catches the `BASE_DIR / "<dir>"` idiom — but that is the idiom every
    current write site uses, so a NEW one landing in an unlisted directory fails here rather than
    in production.
    """
    writable = _installer_writable_dirs()
    src = REPO / "src" / "orchestrator"
    offenders = set()
    for py in src.glob("*.py"):
        text = py.read_text()
        for m in re.finditer(r'BASE_DIR\s*/\s*"([^"/]+)"', text):
            d = m.group(1)
            # Read-only inputs: GGUFs, certs, the Piper binary + voice, source, and
            # pyproject.toml (read once for APP_VERSION).
            if d in writable or d in {"models", "tls", "src", "frontend", "piper", "pyproject.toml"}:
                continue
            offenders.add(f"{py.name}: BASE_DIR / {d!r}")
    # config/ is the interesting one and it IS writable now; anything else needs a human decision.
    assert not offenders, (
        "these directories are referenced from BASE_DIR but are not in ORCH_WRITABLE_DIRS — "
        "if the code WRITES there, add it to install_services.sh and the unit template; if it "
        f"only reads, add it to the allowlist in this test: {sorted(offenders)}"
    )
