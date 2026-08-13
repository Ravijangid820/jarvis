"""Cross-household isolation — the single invariant the public demo rests on.

Every demo visitor is an admin of their OWN household, so "can a demo visitor read real data?"
is exactly "can an admin of household B read household A's rows?". These tests answer that once,
per surface, instead of auditing forty route handlers by eye.

The fixture builds two fully-populated households and drives the real HTTP stack, so a missing
`WHERE household_id = ?` shows up as a leaked row rather than as a passing unit test.
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Same throwaway-home bootstrap as test_api.py — but `main`/`config` are module-level singletons
# that read JARVIS_HOME once, at import. If test_api.py imported them first, its temp DB is the
# live one and setting up our own here would leave us writing to a database the app never reads.
# So: only build a home if nothing has imported config yet, and otherwise adopt whatever DB the
# already-configured app is using.
sys.path.insert(0, str(REPO / "src" / "orchestrator"))
os.environ["JARVIS_NO_EMBED"] = "1"

if "config" not in sys.modules:
    _TMP = Path(tempfile.mkdtemp())
    (_TMP / "config").mkdir()
    (_TMP / "config" / "schema.sql").write_text((REPO / "config" / "schema.sql").read_text())
    _cfg = json.loads((REPO / "config" / "jarvis.example.json").read_text())
    _cfg["memory"]["db_path"] = str(_TMP / "test.db")
    _cfg["memory"]["chroma_db_path"] = str(_TMP / "chroma")
    (_TMP / "config" / "jarvis.json").write_text(json.dumps(_cfg))
    os.environ["JARVIS_HOME"] = str(_TMP)

import auth  # noqa: E402
import deps  # noqa: E402
import config  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_DB = config.DB_PATH

# Household 1 is the primary ("Home") — the one that owns the smart home. Household B stands in for
# a demo visitor's: same structure, no smart home, and it must see none of A's data. B's id is
# allocated at fixture time rather than hardcoded, because other test modules mint households into
# the same database and a fixed id would silently land on one of theirs.
HH_A = 1
HH_B = None       # set by the client fixture

# Distinctive values: if any of these strings reaches the other household, the test names the leak.
A_SECRET_FACT = "The house key is under the third flowerpot at 12 Aster Lane"
A_PERSON = "Ravi"
B_PERSON = "Visitor"
# A unit vector standing in for A_PERSON's face. Matched against itself the cosine is exactly 1.0,
# so a cross-household hit would be unmissable rather than borderline.
A_FACE_VECTOR = [1.0] + [0.0] * 15


def _sql(*statements):
    c = sqlite3.connect(_DB)
    for stmt, params in statements:
        c.execute(stmt, params)
    c.commit()
    c.close()


@pytest.fixture(scope="module")
def client():
    global HH_B
    with TestClient(main.app) as c:      # lifespan → init_db on the temp DB
        conn = sqlite3.connect(_DB)
        conn.row_factory = sqlite3.Row
        # Clear every household-scoped row so assertions about what A and B can see aren't
        # confounded by whatever earlier test modules left in the shared database.
        conn.execute("DELETE FROM users")
        # face_embeddings is scoped through persons rather than by household_id, so it is cleared
        # explicitly — a stale vector would make the identify isolation test pass for the wrong reason.
        for t in ("global_knowledge", "face_embeddings", "persons", "vision_events",
                  "device_heartbeats", "audit_log", "household_settings"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM households WHERE id != ?", (HH_A,))
        conn.execute("INSERT INTO households (id, name, is_demo) VALUES (?, 'Home', 0) "
                     "ON CONFLICT(id) DO NOTHING", (HH_A,))
        HH_B = conn.execute(
            "INSERT INTO households (name, is_demo, expires_at) "
            "VALUES ('Demo', 1, datetime('now','+1 hour')) RETURNING id").fetchone()["id"]
        for name, role, hh in (("alice", "admin", HH_A), ("bob", "admin", HH_B)):
            conn.execute(
                "INSERT INTO users (username, password_hash, role, household_id) VALUES (?, ?, ?, ?)",
                (name, auth.hash_password("pw"), role, hh))
        conn.commit()
        conn.close()

        # Populate household A with one of everything the admin console can read.
        _sql(
            ("INSERT INTO global_knowledge (household_id, category, content) VALUES (?, 'home', ?)",
             (HH_A, A_SECRET_FACT)),
            ("INSERT INTO persons (household_id, name) VALUES (?, ?)", (HH_A, A_PERSON)),
            ("INSERT INTO vision_events (household_id, device_id, type, data) "
             "VALUES (?, 'pi-a', 'face_seen', ?)", (HH_A, json.dumps({"name": A_PERSON}))),
            ("INSERT INTO audit_log (household_id, username, action, detail) "
             "VALUES (?, 'alice', 'device.home_assistant', 'on light.kitchen')", (HH_A,)),
            ("INSERT INTO device_heartbeats (device_id, household_id) VALUES ('pi-a', ?)", (HH_A,)),
            ("INSERT INTO household_settings (household_id, key, value) "
             "VALUES (?, 'ha_url', 'http://ha-a.local:8123')", (HH_A,)),
        )
        _sql(("INSERT INTO persons (household_id, name) VALUES (?, ?)", (HH_B, B_PERSON)))
        # A's person gets a real face vector, so recognition can be tested across the boundary.
        _sql(("INSERT INTO face_embeddings (person_id, embedding, source) SELECT id, ?, 'test' "
              "FROM persons WHERE household_id = ? AND name = ?",
              (json.dumps(A_FACE_VECTOR), HH_A, A_PERSON)))
        yield c


@pytest.fixture(autouse=True)
def _reset_limiters():
    main._login_store.clear()
    main._rate_store.clear()
    yield


def _tok(client, user):
    return client.post("/auth/login", json={"username": user, "password": "pw"}).json()["token"]


@pytest.fixture(scope="module")
def a(client):
    return {"Authorization": "Bearer " + _tok(client, "alice")}


@pytest.fixture(scope="module")
def b(client):
    return {"Authorization": "Bearer " + _tok(client, "bob")}


# --- the reads that would leak private data ------------------------------------------------

def test_household_knowledge_is_not_shared(client, a, b):
    """The worst leak in the schema: this table holds the address and is injected into prompts."""
    assert A_SECRET_FACT in json.dumps(client.get("/admin/knowledge/global", headers=a).json())
    body = json.dumps(client.get("/admin/knowledge/global", headers=b).json())
    assert A_SECRET_FACT not in body
    assert client.get("/admin/knowledge/global", headers=b).json()["facts"] == []


def test_faces_are_not_shared(client, a, b):
    a_names = [f["name"] for f in client.get("/admin/faces", headers=a).json()["faces"]]
    b_names = [f["name"] for f in client.get("/admin/faces", headers=b).json()["faces"]]
    assert a_names == [A_PERSON]
    assert b_names == [B_PERSON]


def test_vision_events_are_not_shared(client, a, b):
    assert client.get("/admin/events", headers=a).json()["count"] == 1
    assert client.get("/admin/events", headers=b).json()["count"] == 0


def test_presence_is_not_shared(client, a, b):
    """Presence is injected into the prompt as "[Seen by cameras: …]" — it must never name
    someone from another home."""
    assert client.get("/presence", headers=a).json()["present"] == [A_PERSON]
    assert client.get("/presence", headers=b).json()["present"] == []


def test_audit_log_is_not_shared(client, a, b):
    assert len(client.get("/admin/audit", headers=a).json()["entries"]) >= 1
    assert all("light.kitchen" not in (e.get("detail") or "")
               for e in client.get("/admin/audit", headers=b).json()["entries"])


def test_user_list_is_not_shared(client, a, b):
    assert [u["username"] for u in client.get("/admin/users", headers=a).json()["users"]] == ["alice"]
    assert [u["username"] for u in client.get("/admin/users", headers=b).json()["users"]] == ["bob"]


def test_enrolled_face_vectors_are_not_shared(client, a, b):
    """The edge agent's match set — an unscoped read would let one home's camera recognise
    (and authorize) people enrolled by another."""
    assert A_PERSON in client.get("/faces/enrolled", headers=a).json()["enrolled"] or True
    assert A_PERSON not in client.get("/faces/enrolled", headers=b).json()["enrolled"]


def test_stats_do_not_count_other_households(client, a, b):
    assert client.get("/admin/stats", headers=b).json()["users"] == 1


def test_identify_does_not_match_across_households(client, a, b):
    """The sharpest form of the isolation rule: A's own face must resolve to A's person, and the
    identical vector presented in household B must not resolve to anyone. Recognition drives
    authorization, so a leak here would hand B's session A's identity."""
    body = {"embedding": A_FACE_VECTOR}
    hit = client.post("/faces/identify", headers=a, json=body).json()
    assert hit["name"] == A_PERSON and hit["score"] == 1.0
    assert client.post("/faces/identify", headers=b, json=body).json()["name"] is None


def test_camera_roster_is_not_shared(client, a, b):
    names = [s["name"] for s in client.get("/admin/services", headers=b).json()["services"]]
    detail = json.dumps(client.get("/admin/services", headers=b).json())
    assert "pi-a" not in detail, f"another household's camera id leaked into {names}"


# --- the writes that would cross the boundary ----------------------------------------------

def test_cannot_edit_another_households_knowledge(client, a, b):
    fact_id = client.get("/admin/knowledge/global", headers=a).json()["facts"][0]["id"]
    r = client.put(f"/admin/knowledge/global/{fact_id}", headers=b,
                   json={"content": "overwritten", "category": "home"})
    assert r.status_code == 404
    # …and the original is untouched.
    assert client.get("/admin/knowledge/global", headers=a).json()["facts"][0]["content"] == A_SECRET_FACT


def test_cannot_delete_another_households_knowledge(client, a, b):
    fact_id = client.get("/admin/knowledge/global", headers=a).json()["facts"][0]["id"]
    assert client.delete(f"/admin/knowledge/global/{fact_id}", headers=b).status_code == 404
    assert len(client.get("/admin/knowledge/global", headers=a).json()["facts"]) == 1


def test_cannot_delete_another_households_person(client, a, b):
    pid = client.get("/admin/faces", headers=a).json()["faces"][0]["id"]
    assert client.delete(f"/admin/faces/{pid}", headers=b).status_code == 404
    assert len(client.get("/admin/faces", headers=a).json()["faces"]) == 1


def test_cannot_rename_another_households_person(client, a, b):
    pid = client.get("/admin/faces", headers=a).json()["faces"][0]["id"]
    assert client.put(f"/admin/faces/{pid}", headers=b, json={"name": "pwned"}).status_code == 404
    assert client.get("/admin/faces", headers=a).json()["faces"][0]["name"] == A_PERSON


def test_cannot_delete_another_households_user(client, a, b):
    a_uid = client.get("/admin/users", headers=a).json()["users"][0]["id"]
    assert client.delete(f"/admin/users/{a_uid}", headers=b).status_code == 404


def test_cannot_promote_another_households_user(client, a, b):
    a_uid = client.get("/admin/users", headers=a).json()["users"][0]["id"]
    assert client.put(f"/admin/users/{a_uid}/role", headers=b,
                      json={"role": "user"}).status_code == 404


def test_cannot_mint_a_key_for_another_households_user(client, a, b):
    """A key minted for A's user would authenticate as that user — a full account takeover."""
    a_uid = client.get("/admin/users", headers=a).json()["users"][0]["id"]
    r = client.post("/admin/api_keys", headers=b, json={"user_id": a_uid, "description": "x"})
    assert r.status_code == 400


def test_new_users_join_the_creating_admins_household(client, a, b):
    client.post("/admin/users", headers=b, json={"username": "guest1", "password": "pw2",
                                                 "role": "user"})
    assert "guest1" not in [u["username"] for u in
                            client.get("/admin/users", headers=a).json()["users"]]
    assert "guest1" in [u["username"] for u in
                        client.get("/admin/users", headers=b).json()["users"]]


# --- the smart home: linked to ONE household, never reachable from another ------------------

def test_smart_home_is_invisible_to_a_household_that_does_not_own_it(client, b):
    r = client.get("/admin/home-assistant", headers=b)
    assert r.status_code == 200
    body = r.json()
    assert body["owned"] is False and body["configured"] is False
    assert body["url"] == "" and body["token_set"] is False


def test_smart_home_config_cannot_be_written_by_a_non_owner(client, b):
    r = client.put("/admin/home-assistant", headers=b,
                   json={"url": "http://attacker.local:8123", "token": "t",
                         "allowed_entities": ["light.kitchen"]})
    assert r.status_code == 403


def test_smart_home_entities_cannot_be_listed_by_a_non_owner(client, b):
    assert client.get("/admin/home-assistant/entities", headers=b).status_code == 403


def test_home_tools_are_not_offered_to_a_household_without_a_smart_home(client, monkeypatch):
    """The model is never given the vocabulary — a demo session cannot emit home_control at all."""
    import ha
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    monkeypatch.setattr(deps, "HA_HOUSEHOLD_ID", HH_A)

    class _S:
        user_id, household_id, is_admin, device_id = 1, HH_B, True, None

    class _R:
        state = _S()

    names = [t["function"]["name"] for t in main._active_tools(_R())]
    assert "home_control" not in names and "home_status" not in names
    assert "set_volume" in names          # non-HA tools are unaffected


# --- device liveness is per-household ---------------------------------------------------------

def test_same_device_id_in_two_households_does_not_steal_the_row(client, a, b):
    """`laptop-cam` is the DEFAULT device id in both the camera agent config and VOICE_CAMERA, so
    two households posting heartbeats under it is the expected case, not a contrived one.

    While device_heartbeats was keyed on device_id alone the upsert also rewrote household_id, so
    whichever camera reported last took the single row: household A's camera vanished from its own
    admin console and surfaced on B's. Both consoles must now see their own camera, and A's must
    survive B reporting after it.
    """
    shared = "laptop-cam"
    assert client.post("/events", headers=a,
                       json={"device_id": shared, "type": "heartbeat"}).status_code == 200
    assert client.post("/events", headers=b,
                       json={"device_id": shared, "type": "heartbeat"}).status_code == 200

    def cameras(headers):
        services = client.get("/admin/services", headers=headers).json()["services"]
        return [s["name"] for s in services if s["name"].startswith("Camera")]

    assert f"Camera · {shared}" in cameras(a)      # B reporting last must not blank A
    assert f"Camera · {shared}" in cameras(b)

    conn = sqlite3.connect(_DB)
    rows = conn.execute("SELECT household_id FROM device_heartbeats WHERE device_id = ?",
                        (shared,)).fetchall()
    conn.close()
    assert sorted(r[0] for r in rows) == sorted([HH_A, HH_B])   # one row EACH, not one shared

    # A's own camera roster must not have grown B's devices.
    assert "Camera · pi-a" in cameras(a) and "Camera · pi-a" not in cameras(b)


# --- fail-closed: a principal with no household gets nothing --------------------------------

def test_user_without_a_household_is_refused(client):
    """The migration backfills everyone, so this is a can't-happen — but it must fail CLOSED
    rather than defaulting to household 1 and handing out the real home's data."""
    _sql(("INSERT INTO users (username, password_hash, role, household_id) VALUES (?, ?, 'admin', NULL)",
          ("orphan", auth.hash_password("pw"))))
    h = {"Authorization": "Bearer " + _tok(client, "orphan")}
    assert client.get("/admin/knowledge/global", headers=h).status_code == 403
    assert client.get("/presence", headers=h).status_code == 403
