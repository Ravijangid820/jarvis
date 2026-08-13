"""Request-scoped guards shared by every router.

These are the checks that decide *whether* a request may proceed — tenant scoping, admin, device
control, demo mode, smart-home ownership — plus the audit trail that records the ones that act.
They live here rather than in `main` so that splitting the routes did not mean either duplicating
them or importing `main` from six places.

Call them through the module (`deps.require_admin(request)`), not by importing the names. A router
that does `from deps import require_admin` binds the function object into its own namespace, and
then a test that replaces `deps.require_admin` changes nothing for that router. Going through the
module attribute keeps one substitutable definition, which is what the tests rely on.

Nothing here decides *what* the request does — that is the router's job — and none of it is ever
delegated to the model. Every gate is code.
"""
from typing import Optional

from fastapi import HTTPException, Request

import memory
from config import JARVIS_MODE, logger
from db import get_db


# Which household owns the smart home this process is connected to.
#
# ha.py holds ONE live connection in module globals — url/token/allowlist — and the intent router
# caches exemplar embeddings derived from that allowlist. So exactly one household's Home Assistant
# is reachable at a time, and every HA route checks the caller against this id. That is what makes
# "the smart home belongs to one admin and the users under them" true, and it is why a demo
# household — which never owns HA settings — has no smart home to reach rather than merely a
# blocked button.
#
# Supporting several DIFFERENT real smart homes on one box means making ha.py stateless (a per-call
# connection) and keying the router cache by household; the household_settings table is already
# shaped for it. Until then this is a single-smart-home deployment with hard tenant isolation.
HA_HOUSEHOLD_ID: Optional[int] = None


def set_ha_household(household_id: Optional[int]) -> None:
    """Record which household's smart home this process is attached to (called at start-up)."""
    global HA_HOUSEHOLD_ID
    HA_HOUSEHOLD_ID = household_id


# --- who is asking -----------------------------------------------------------------------------

def household(request: Request) -> int:
    """The caller's household id — the tenant filter every scoped query must carry.

    Fails CLOSED. A principal with no household is a bug (the migration backfills every existing
    user into household 1, and both account-creation paths set it explicitly), and the tempting
    fallback — "assume household 1" — is precisely the leak this whole boundary exists to prevent:
    it would hand an unscoped account the real home's address, faces and camera history.
    """
    hid = getattr(request.state, "household_id", None)
    if not hid:
        logger.error("Principal user_id=%s has no household_id; refusing to serve scoped data",
                     getattr(request.state, "user_id", "?"))
        raise HTTPException(status_code=403, detail="Account is not linked to a household")
    return int(hid)


def require_admin(request: Request) -> None:
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only")


def can_control_devices(request: Request) -> bool:
    """Authorization for device actions (lights/volume): admins always; others need the
    per-user can_control_devices flag. Enforced HERE, in code — never by the LLM."""
    if getattr(request.state, "is_admin", False):
        return True
    conn = get_db()
    try:
        row = conn.execute("SELECT can_control_devices FROM users WHERE id = ?",
                           (request.state.user_id,)).fetchone()
        return bool(row and row["can_control_devices"])
    finally:
        conn.close()


def authorized_person_present(household_id: int) -> bool:
    """True if a person currently present in THIS household maps to a user of it who is allowed to
    control devices. Used only when REQUIRE_PRESENCE_FOR_CONTROL is on.

    Both sides of the join are scoped: an unscoped `persons.name IN (…)` would let a namesake in
    another household satisfy the presence check and unlock this one's devices.
    """
    names = memory.get_present_people(household_id)
    if not names:
        return False
    conn = get_db()
    try:
        ph = ",".join("?" * len(names))
        row = conn.execute(
            f"SELECT 1 FROM persons p JOIN users u ON p.user_id = u.id WHERE p.name IN ({ph}) "
            "AND p.household_id = ? AND u.household_id = ? "
            "AND (u.role = 'admin' OR u.can_control_devices = 1) LIMIT 1",
            [*names, household_id, household_id]).fetchone()
        return row is not None
    finally:
        conn.close()


# --- what the deployment allows ------------------------------------------------------------------

def require_not_demo(detail: str = "Hardware & Home Assistant control is disabled in public Demo Mode.") -> None:
    if JARVIS_MODE == "demo":
        raise HTTPException(status_code=403, detail=detail)


def owns_smart_home(request: Request) -> bool:
    """True if the caller's household is the one this process's Home Assistant belongs to."""
    return HA_HOUSEHOLD_ID is not None and household(request) == HA_HOUSEHOLD_ID


def require_smart_home(request: Request) -> None:
    """Gate every Home Assistant surface on owning the smart home.

    This is the authorization check the whole household boundary exists to support: a household that
    does not own the HA connection cannot read its config, enumerate its entities, or actuate
    anything in it. Demo households never own one, so the demo has no smart home by construction —
    not by a mode flag that someone could forget to check on a new route.
    """
    if not owns_smart_home(request):
        raise HTTPException(status_code=403,
                            detail="No smart home is linked to your household.")


# --- the record ----------------------------------------------------------------------------------

AUDIT_CAP = 5000   # keep the most recent N audit rows


def audit(request: Request, action: str, detail: str = "") -> None:
    """Append an audit entry (who did what). Best-effort — never breaks the request it's recording."""
    try:
        uid = getattr(request.state, "user_id", None)
        conn = get_db()
        try:
            uname = None
            if uid is not None:
                row = conn.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
                uname = row["username"] if row else None
            cur = conn.execute(
                "INSERT INTO audit_log (household_id, user_id, username, action, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (getattr(request.state, "household_id", None), uid, uname, action, (detail or "")[:500]))
            if cur.lastrowid % 200 == 0:   # prune occasionally, not on every write
                conn.execute("DELETE FROM audit_log WHERE id <= ?", (cur.lastrowid - AUDIT_CAP,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("audit log failed (%s): %s", action, e)
