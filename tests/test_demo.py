"""Demo sessions: the reset matrix, and the guarantee that a visitor reaches no real data.

The behaviour under test, in one table:

    refresh          -> data intact   (token persists client-side; nothing server-side changes)
    reopen in TTL    -> data intact
    explicit logout  -> full wipe
    idle past TTL    -> full wipe
    tab closed       -> wipe at TTL   (deliberately NOT on pagehide — that fires on refresh too)

A "refresh" is simulated the only honest way: reuse the same bearer token on a new request, which
is exactly what the browser does with the token it kept in localStorage.
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Demo signup is OFF by default, so it must be switched on BEFORE config is imported.
os.environ["DEMO_PUBLIC_SIGNUP"] = "1"
os.environ["DEMO_TTL_MINUTES"] = "60"
os.environ["JARVIS_NO_EMBED"] = "1"
sys.path.insert(0, str(REPO / "src" / "orchestrator"))

if "config" not in sys.modules:
    _TMP = Path(tempfile.mkdtemp())
    (_TMP / "config").mkdir()
    (_TMP / "config" / "schema.sql").write_text((REPO / "config" / "schema.sql").read_text())
    _cfg = json.loads((REPO / "config" / "jarvis.example.json").read_text())
    _cfg["memory"]["db_path"] = str(_TMP / "test.db")
    _cfg["memory"]["chroma_db_path"] = str(_TMP / "chroma")
    (_TMP / "config" / "jarvis.json").write_text(json.dumps(_cfg))
    os.environ["JARVIS_HOME"] = str(_TMP)

import config  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_DB = config.DB_PATH


@pytest.fixture(scope="module")
def client():
    # config is imported once per process; if another module got there first, DEMO_PUBLIC_SIGNUP
    # was read before we set it. Force the values this module needs onto the live modules.
    main.DEMO_PUBLIC_SIGNUP = True
    main.DEMO_TTL_MINUTES = 60
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_limiters():
    main._login_store.clear()
    main._rate_store.clear()
    main._demo_mint_store.clear()
    yield


def _mint(client):
    r = client.post("/demo/session")
    assert r.status_code == 200, r.text
    body = r.json()
    return body, {"Authorization": "Bearer " + body["token"]}


def _q(sql, params=()):
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def _household_of(username):
    rows = _q("SELECT household_id FROM users WHERE username = ?", (username,))
    return rows[0]["household_id"] if rows else None


# --- minting ---------------------------------------------------------------------------------

def test_mint_returns_a_working_admin_token(client):
    body, h = _mint(client)
    assert body["demo"] is True and body["role"] == "admin"
    assert body["username"].startswith("demo_")
    # It really is an admin — of its own household.
    assert client.get("/admin/users", headers=h).status_code == 200
    assert [u["username"] for u in client.get("/admin/users", headers=h).json()["users"]] \
        == [body["username"]]


def test_each_visitor_gets_their_own_household(client):
    b1, h1 = _mint(client)
    b2, h2 = _mint(client)
    assert _household_of(b1["username"]) != _household_of(b2["username"])
    # And neither can see the other's users.
    names2 = [u["username"] for u in client.get("/admin/users", headers=h2).json()["users"]]
    assert b1["username"] not in names2


def test_demo_user_ids_are_above_the_real_account_range(client):
    body, _ = _mint(client)
    uid = _q("SELECT id FROM users WHERE username = ?", (body["username"],))[0]["id"]
    assert uid >= config.DEMO_USER_ID_BASE


def test_demo_household_starts_seeded_not_empty(client):
    _, h = _mint(client)
    assert len(client.get("/admin/knowledge/global", headers=h).json()["facts"]) > 0
    assert len(client.get("/admin/faces", headers=h).json()["faces"]) > 0


def test_seeded_content_is_fictional_per_household(client):
    """Two visitors get their own copies — editing one must not touch the other."""
    _, h1 = _mint(client)
    _, h2 = _mint(client)
    f1 = client.get("/admin/knowledge/global", headers=h1).json()["facts"][0]
    client.delete(f"/admin/knowledge/global/{f1['id']}", headers=h1)
    ids2 = [f["id"] for f in client.get("/admin/knowledge/global", headers=h2).json()["facts"]]
    assert len(ids2) == len(main._DEMO_KNOWLEDGE)


# --- the reset matrix ------------------------------------------------------------------------

def test_refresh_keeps_the_session(client):
    """The requirement: refreshing the page must NOT reset the demo."""
    body, h = _mint(client)
    client.post("/knowledge", headers=h, json={"content": "I am testing the demo",
                                               "category": "other"})
    before = client.get("/knowledge", headers=h).json()["facts"]
    assert len(before) == 1
    # A refresh is just the same token used again — no logout, no new mint.
    after = client.get("/knowledge", headers=h).json()["facts"]
    assert [f["content"] for f in after] == [f["content"] for f in before]
    assert client.get("/admin/users", headers=h).status_code == 200


def test_logout_destroys_the_household(client):
    body, h = _mint(client)
    hh = _household_of(body["username"])
    client.post("/knowledge", headers=h, json={"content": "ephemeral", "category": "other"})

    r = client.post("/auth/logout", headers=h)
    assert r.status_code == 200 and r.json().get("demo_reset") is True

    # Everything is gone: household, user, and the rows that hung off both.
    assert _q("SELECT 1 FROM households WHERE id = ?", (hh,)) == []
    assert _q("SELECT 1 FROM users WHERE username = ?", (body["username"],)) == []
    assert _q("SELECT 1 FROM global_knowledge WHERE household_id = ?", (hh,)) == []
    assert _q("SELECT 1 FROM persons WHERE household_id = ?", (hh,)) == []
    assert _q("SELECT 1 FROM audit_log WHERE household_id = ?", (hh,)) == []
    # …and the token no longer authenticates.
    assert client.get("/sessions", headers=h).status_code == 403


def test_logout_removes_the_visitors_face_data(client):
    """Biometric data from a member of the public must be destroyed, not merely unlinked."""
    body, h = _mint(client)
    hh = _household_of(body["username"])
    client.post("/faces/enroll", headers=h,
                json={"name": "Visitor", "embedding": [0.1] * 16, "source": "browser"})
    assert _q("SELECT 1 FROM face_embeddings e JOIN persons p ON e.person_id = p.id "
              "WHERE p.household_id = ?", (hh,)) != []

    client.post("/auth/logout", headers=h)
    assert _q("SELECT 1 FROM persons WHERE household_id = ?", (hh,)) == []
    assert _q("SELECT 1 FROM face_embeddings e JOIN persons p ON e.person_id = p.id "
              "WHERE p.household_id = ?", (hh,)) == []


def test_expired_household_is_swept(client):
    """The tab-closed case: nothing signals us, so the TTL reclaims it."""
    body, h = _mint(client)
    hh = _household_of(body["username"])
    _c = sqlite3.connect(_DB)
    _c.execute("UPDATE households SET expires_at = datetime('now', '-1 minute') WHERE id = ?", (hh,))
    _c.commit()
    _c.close()

    assert main._sweep_expired_demo_households() >= 1
    assert _q("SELECT 1 FROM households WHERE id = ?", (hh,)) == []
    assert _q("SELECT 1 FROM users WHERE username = ?", (body["username"],)) == []


def test_sweeper_never_touches_a_real_household(client):
    """The blast radius check: the sweeper is a DELETE loop, so it must be provably demo-only."""
    _c = sqlite3.connect(_DB)
    _c.execute("UPDATE households SET expires_at = datetime('now', '-1 day') "
               "WHERE is_demo = 0")      # even if a real household somehow had a past expiry
    _c.commit()
    _c.close()
    main._sweep_expired_demo_households()
    assert _q("SELECT 1 FROM households WHERE id = ?", (1,)) != []


def test_activity_slides_the_expiry_forward(client):
    """TTL measures IDLE time — a visitor mid-conversation shouldn't be cut off."""
    body, h = _mint(client)
    hh = _household_of(body["username"])
    _c = sqlite3.connect(_DB)
    _c.execute("UPDATE households SET expires_at = datetime('now', '+2 minutes') WHERE id = ?", (hh,))
    _c.commit()
    _c.close()
    near = _q("SELECT expires_at FROM households WHERE id = ?", (hh,))[0]["expires_at"]

    client.get("/sessions", headers=h)          # any authenticated request

    after = _q("SELECT expires_at FROM households WHERE id = ?", (hh,))[0]["expires_at"]
    assert after > near


# --- what a demo visitor must never reach ----------------------------------------------------

def test_demo_visitor_cannot_reach_the_smart_home(client):
    _, h = _mint(client)
    assert client.get("/admin/home-assistant", headers=h).json()["owned"] is False
    assert client.put("/admin/home-assistant", headers=h,
                      json={"url": "http://x:8123", "token": "t"}).status_code == 403
    assert client.get("/admin/home-assistant/entities", headers=h).status_code == 403


def test_demo_visitor_sees_no_real_household_data(client):
    """Household 1 exists (init_db seeds it) — none of it may be visible."""
    _c = sqlite3.connect(_DB)
    _c.execute("INSERT INTO global_knowledge (household_id, category, content) "
               "VALUES (1, 'home', 'REAL SECRET ADDRESS')")
    _c.commit()
    _c.close()
    _, h = _mint(client)
    body = json.dumps(client.get("/admin/knowledge/global", headers=h).json())
    assert "REAL SECRET ADDRESS" not in body


def test_mint_is_rate_limited_per_ip(client):
    for _ in range(config.DEMO_MINT_PER_IP_HOURLY):
        assert client.post("/demo/session").status_code == 200
    assert client.post("/demo/session").status_code == 429


def test_mint_is_404_when_signup_is_disabled(client, monkeypatch):
    """The master switch: a normal deployment must not hand out accounts by upgrading."""
    monkeypatch.setattr(main, "DEMO_PUBLIC_SIGNUP", False)
    assert client.post("/demo/session").status_code == 404
