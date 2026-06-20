"""End-to-end HTTP tests of the auth middleware via FastAPI's TestClient.

Made possible by (a) the config BASE_DIR/example-fallback refactor and (b) the lazy
embedding load: we point JARVIS_HOME at a throwaway dir with a temp DB and set
JARVIS_NO_EMBED=1, so importing/booting the real app is fast and touches nothing real.
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Configure a throwaway home + config BEFORE importing the app, and skip the model.
_TMP = Path(tempfile.mkdtemp())
(_TMP / "config").mkdir()
(_TMP / "config" / "schema.sql").write_text((REPO / "config" / "schema.sql").read_text())
_cfg = json.loads((REPO / "config" / "jarvis.example.json").read_text())
_DB = _TMP / "test.db"
_cfg["memory"]["db_path"] = str(_DB)
_cfg["memory"]["chroma_db_path"] = str(_TMP / "chroma")
(_TMP / "config" / "jarvis.json").write_text(json.dumps(_cfg))
os.environ["JARVIS_HOME"] = str(_TMP)
os.environ["JARVIS_NO_EMBED"] = "1"

sys.path.insert(0, str(REPO / "src" / "orchestrator"))
import auth  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _seed_user(username, password, role):
    c = sqlite3.connect(_DB)
    c.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
              (username, auth.hash_password(password), role))
    c.commit()
    c.close()


def _seed_device_key(username, device_id):
    """Mint an API key bound to a device for `username`; returns the plaintext key."""
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    uid = c.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
    key = f"devkey-{device_id}"
    c.execute("INSERT INTO api_keys (key_string, key_prefix, user_id, description, device_id) "
              "VALUES (?, ?, ?, ?, ?)", (auth.hash_token(key), key[:10], uid, "test", device_id))
    c.commit()
    c.close()
    return key


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:   # runs lifespan → init_db on the temp DB
        _seed_user("tony", "pw-admin", "admin")
        _seed_user("pepper", "pw-user", "user")
        yield c


@pytest.fixture(autouse=True)
def _reset_limiters():
    # Each test starts with fresh rate-limit buckets (they're per-process module globals).
    main._login_store.clear()
    main._rate_store.clear()
    yield


def _login(client, u, p):
    return client.post("/auth/login", json={"username": u, "password": p})


def _tok(client, u, p):
    return _login(client, u, p).json()["token"]


def test_requires_auth(client):
    assert client.get("/sessions").status_code == 401          # missing Bearer


def test_invalid_token(client):
    assert client.get("/sessions", headers={"Authorization": "Bearer nope"}).status_code == 403


def test_login_and_authed_request(client):
    r = _login(client, "tony", "pw-admin")
    assert r.status_code == 200 and r.json()["role"] == "admin"
    tok = r.json()["token"]
    assert client.get("/sessions", headers={"Authorization": "Bearer " + tok}).status_code == 200


def test_login_wrong_password(client):
    assert _login(client, "tony", "nope").status_code == 401


def test_admin_gate(client):
    user_tok = _tok(client, "pepper", "pw-user")
    admin_tok = _tok(client, "tony", "pw-admin")
    assert client.get("/admin/users", headers={"Authorization": "Bearer " + user_tok}).status_code == 403
    assert client.get("/admin/users", headers={"Authorization": "Bearer " + admin_tok}).status_code == 200


def test_session_ownership_over_http(client):
    ptok = _tok(client, "pepper", "pw-user")
    ttok = _tok(client, "tony", "pw-admin")
    sid = client.post("/sessions", headers={"Authorization": "Bearer " + ptok}).json()["id"]
    # Owner reads their own history; a different user (even an admin) is forbidden.
    assert client.get("/history/" + sid, headers={"Authorization": "Bearer " + ptok}).status_code == 200
    assert client.get("/history/" + sid, headers={"Authorization": "Bearer " + ttok}).status_code == 403


def test_login_throttled_by_ip(client):
    codes = [_login(client, "tony", "nope").status_code for _ in range(9)]
    assert codes[:8].count(429) == 0     # first 8 attempts allowed through (then 401)
    assert codes[8] == 429               # 9th is rate-limited


def test_tokens_stored_hashed(client):
    tok = _tok(client, "tony", "pw-admin")
    c = sqlite3.connect(_DB)
    rows = [r[0] for r in c.execute("SELECT token FROM auth_sessions").fetchall()]
    c.close()
    assert tok not in rows                       # plaintext never persisted
    assert auth.hash_token(tok) in rows          # only the hash is stored


def test_events_ingest_requires_auth(client):
    assert client.post("/events", json={"device_id": "pi", "type": "motion"}).status_code == 401


def test_events_plain_user_forbidden(client):
    # A plain web user (no device-scoped key) may NOT post events (matters once events drive authz).
    tok = _tok(client, "pepper", "pw-user")
    r = client.post("/events", headers={"Authorization": "Bearer " + tok},
                    json={"device_id": "pi-test", "type": "motion"})
    assert r.status_code == 403


def test_events_admin_ingest_and_admin_list(client):
    admin = _tok(client, "tony", "pw-admin")   # admins may post synthetic events as any device
    r = client.post("/events", headers={"Authorization": "Bearer " + admin},
                    json={"device_id": "pi-test", "type": "face_seen", "data": {"name": "Ravi"}})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    got = client.get("/admin/events", headers={"Authorization": "Bearer " + admin}).json()
    assert got["count"] >= 1
    latest = got["events"][0]
    assert latest["device_id"] == "pi-test" and latest["type"] == "face_seen"
    assert latest["data"] == {"name": "Ravi"}


def test_events_device_key_provenance(client):
    # A device-scoped key records events under ITS OWN device_id — the body can't spoof another.
    key = _seed_device_key("pepper", "pi-cam")
    r = client.post("/events", headers={"Authorization": "Bearer " + key},
                    json={"device_id": "SOMEONE-ELSE", "type": "face_seen"})
    assert r.status_code == 200
    admin = _tok(client, "tony", "pw-admin")
    latest = client.get("/admin/events", headers={"Authorization": "Bearer " + admin}).json()["events"][0]
    assert latest["device_id"] == "pi-cam"          # bound to the key, not the spoofed body value


def test_events_data_too_large_rejected(client):
    admin = _tok(client, "tony", "pw-admin")
    big = {"blob": "x" * 5000}
    r = client.post("/events", headers={"Authorization": "Bearer " + admin},
                    json={"device_id": "pi", "type": "motion", "data": big})
    assert r.status_code == 422


def test_admin_services_requires_admin(client):
    assert client.get("/admin/services").status_code == 401
    user = _tok(client, "pepper", "pw-user")
    assert client.get("/admin/services", headers={"Authorization": "Bearer " + user}).status_code == 403


def test_admin_services_reports_subsystems(client):
    admin = _tok(client, "tony", "pw-admin")
    svc = client.get("/admin/services", headers={"Authorization": "Bearer " + admin}).json()["services"]
    names = [s["name"] for s in svc]
    assert "Orchestrator (API)" in names                      # always present + active
    assert any(s["name"] == "Orchestrator (API)" and s["status"] == "active" for s in svc)
    assert all(s["status"] in ("active", "inactive") for s in svc)


def test_heartbeat_marks_camera_active_and_is_not_an_event(client):
    key = _seed_device_key("pepper", "pi-hb")
    assert client.post("/events", headers={"Authorization": "Bearer " + key},
                       json={"device_id": "pi-hb", "type": "heartbeat"}).status_code == 200
    admin = _tok(client, "tony", "pw-admin")
    svc = client.get("/admin/services", headers={"Authorization": "Bearer " + admin}).json()["services"]
    cam = next((s for s in svc if s["name"] == "Camera · pi-hb"), None)
    assert cam is not None and cam["status"] == "active"      # recent heartbeat → green
    # heartbeats are liveness pings, not stored in the vision_events feed
    events = client.get("/admin/events", headers={"Authorization": "Bearer " + admin}).json()["events"]
    assert all(e["type"] != "heartbeat" for e in events)


def test_admin_mint_device_bound_key_via_api(client):
    # The admin UI can mint a DEVICE-BOUND key (device_id) — the kind an edge/camera agent needs.
    admin = _tok(client, "tony", "pw-admin")
    h = {"Authorization": "Bearer " + admin}
    uid = next(u["id"] for u in client.get("/admin/users", headers=h).json()["users"] if u["username"] == "pepper")
    r = client.post("/admin/api_keys", headers=h,
                    json={"user_id": uid, "description": "laptop cam", "device_id": "ui-cam"})
    assert r.status_code == 200 and r.json()["device_id"] == "ui-cam"
    key = r.json()["key"]
    # listed with its device binding
    assert any(k.get("device_id") == "ui-cam" for k in client.get("/admin/api_keys", headers=h).json()["keys"])
    # and it actually works as an edge key: it can post events (recorded under ITS device, not a spoof)
    assert client.post("/events", headers={"Authorization": "Bearer " + key},
                       json={"device_id": "SPOOF", "type": "heartbeat"}).status_code == 200
    svc = client.get("/admin/services", headers=h).json()["services"]
    assert any(s["name"] == "Camera · ui-cam" and s["status"] == "active" for s in svc)


def test_admin_mint_unbound_key_cannot_post_events(client):
    # Contrast: an UNBOUND key for a non-admin user may NOT post events (so device_id matters).
    admin = _tok(client, "tony", "pw-admin")
    h = {"Authorization": "Bearer " + admin}
    uid = next(u["id"] for u in client.get("/admin/users", headers=h).json()["users"] if u["username"] == "pepper")
    key = client.post("/admin/api_keys", headers=h,
                      json={"user_id": uid, "description": "generic"}).json()["key"]
    assert client.post("/events", headers={"Authorization": "Bearer " + key},
                       json={"device_id": "x", "type": "motion"}).status_code == 403


def test_device_key_never_wields_admin(client):
    # Defense-in-depth: a device-bound key minted under an ADMIN account must NOT have admin powers
    # (device-binding scopes the device, it must also drop privilege). Bounds a stolen camera key.
    key = _seed_device_key("tony", "cam-admin")     # tony is an admin user
    h = {"Authorization": "Bearer " + key}
    assert client.get("/admin/faces", headers=h).status_code == 403       # no admin surface
    assert client.get("/admin/services", headers=h).status_code == 403
    assert client.get("/faces/enrolled", headers=h).status_code == 200    # but read-only is fine
    assert client.post("/events", headers=h,                              # and it can do its job
                       json={"device_id": "cam-admin", "type": "motion"}).status_code == 200


def _uid(client, h, username):
    return next(u["id"] for u in client.get("/admin/users", headers=h).json()["users"] if u["username"] == username)


def test_role_promote_then_demote(client):
    h = {"Authorization": "Bearer " + _tok(client, "tony", "pw-admin")}
    _seed_user("roletmp", "pw", "user")
    uid = _uid(client, h, "roletmp")
    assert client.put(f"/admin/users/{uid}/role", headers=h, json={"role": "admin"}).status_code == 200
    assert next(u["role"] for u in client.get("/admin/users", headers=h).json()["users"] if u["id"] == uid) == "admin"
    assert client.put(f"/admin/users/{uid}/role", headers=h, json={"role": "user"}).status_code == 200
    client.delete(f"/admin/users/{uid}", headers=h)          # cleanup → back to tony-only admin


def test_promoted_user_gains_admin_access(client):
    h = {"Authorization": "Bearer " + _tok(client, "tony", "pw-admin")}
    _seed_user("promoteme", "pw-pm", "user")
    uid = _uid(client, h, "promoteme")
    ut = {"Authorization": "Bearer " + _tok(client, "promoteme", "pw-pm")}
    assert client.get("/admin/users", headers=ut).status_code == 403     # not admin yet
    client.put(f"/admin/users/{uid}/role", headers=h, json={"role": "admin"})
    assert client.get("/admin/users", headers=ut).status_code == 200     # promotion is live for the session
    client.delete(f"/admin/users/{uid}", headers=h)          # 2 admins → deleting promoteme leaves tony


def test_cannot_demote_last_admin(client):
    h = {"Authorization": "Bearer " + _tok(client, "tony", "pw-admin")}
    tony_id = _uid(client, h, "tony")                        # the only admin
    assert client.put(f"/admin/users/{tony_id}/role", headers=h, json={"role": "user"}).status_code == 400
    assert next(u["role"] for u in client.get("/admin/users", headers=h).json()["users"] if u["id"] == tony_id) == "admin"


def test_role_change_requires_admin(client):
    ut = {"Authorization": "Bearer " + _tok(client, "pepper", "pw-user")}
    assert client.put("/admin/users/1/role", headers=ut, json={"role": "admin"}).status_code == 403


def test_device_id_charset_rejected(client):
    # device_id is constrained to [A-Za-z0-9._:-] (no spaces/newlines/control chars).
    admin = _tok(client, "tony", "pw-admin")
    h = {"Authorization": "Bearer " + admin}
    uid = _uid(client, h, "pepper")
    assert client.post("/admin/api_keys", headers=h,
                       json={"user_id": uid, "description": "x", "device_id": "bad id!"}).status_code == 422
    assert client.post("/events", headers=h,
                       json={"device_id": "a\nb", "type": "motion"}).status_code == 422
    # a clean id still works
    assert client.post("/admin/api_keys", headers=h,
                       json={"user_id": uid, "description": "x", "device_id": "ok-cam.1"}).status_code == 200


def test_volume_authz_denies_unprivileged(client):
    # pepper (plain user, can_control_devices=0) must NOT be able to queue a device command.
    tok = _tok(client, "pepper", "pw-user")
    r = client.post("/devices/volume", headers={"Authorization": "Bearer " + tok},
                    json={"action": "set", "value": 30})
    assert r.status_code == 403


def test_volume_queue_and_pull(client):
    admin = _tok(client, "tony", "pw-admin")   # admins may control devices
    r = client.post("/devices/volume", headers={"Authorization": "Bearer " + admin},
                    json={"action": "set", "value": 40, "device": "laptop"})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    # the agent pulls its command (wait=0 so the test doesn't block), then the queue drains
    pulled = client.get("/devices/commands?device=laptop&wait=0",
                        headers={"Authorization": "Bearer " + admin}).json()
    assert any(c["action"] == "set" and c["params"] == {"value": 40} for c in pulled["commands"])
    again = client.get("/devices/commands?device=laptop&wait=0",
                       headers={"Authorization": "Bearer " + admin}).json()
    assert again["commands"] == []     # delivered commands aren't re-served


def test_volume_validation(client):
    admin = _tok(client, "tony", "pw-admin")
    h = {"Authorization": "Bearer " + admin}
    assert client.post("/devices/volume", headers=h, json={"action": "set", "value": 200}).status_code == 422
    assert client.post("/devices/volume", headers=h, json={"action": "frobnicate"}).status_code == 400
    assert client.post("/devices/volume", headers=h, json={"action": "set"}).status_code == 400


def test_device_commands_bound_to_key(client):
    # Enqueue for "laptop" (admin), then: the laptop-bound key can pull it; a key bound to a
    # DIFFERENT device cannot (F1 — a key can't drain another device's queue).
    admin = _tok(client, "tony", "pw-admin")
    client.post("/devices/volume", headers={"Authorization": "Bearer " + admin},
                json={"action": "mute", "device": "laptop"})
    other = _seed_device_key("pepper", "other-dev")
    forbidden = client.get("/devices/commands?device=laptop&wait=0",
                           headers={"Authorization": "Bearer " + other})
    assert forbidden.status_code == 403                       # wrong device key → denied
    laptop = _seed_device_key("pepper", "laptop")
    pulled = client.get("/devices/commands?device=laptop&wait=0",
                        headers={"Authorization": "Bearer " + laptop}).json()
    assert any(c["action"] == "mute" for c in pulled["commands"])


def test_login_throttle_is_per_username(client):
    # Locking out one username must NOT block logins for a different account (no global lockout).
    for _ in range(9):
        _login(client, "tony", "nope")
    assert _login(client, "tony", "nope").status_code == 429       # tony throttled
    assert _login(client, "pepper", "pw-user").status_code == 200  # pepper unaffected


def test_system_is_admin_only(client):
    user = _tok(client, "pepper", "pw-user")
    admin = _tok(client, "tony", "pw-admin")
    assert client.get("/system", headers={"Authorization": "Bearer " + user}).status_code == 403
    assert client.get("/system", headers={"Authorization": "Bearer " + admin}).status_code == 200


def test_rename_unowned_session_forbidden(client):
    ptok = _tok(client, "pepper", "pw-user")
    ttok = _tok(client, "tony", "pw-admin")
    sid = client.post("/sessions", headers={"Authorization": "Bearer " + ptok}).json()["id"]
    r = client.put("/sessions/" + sid, headers={"Authorization": "Bearer " + ttok},
                   json={"title": "hijacked"})
    assert r.status_code == 403


def test_delete_missing_knowledge_404(client):
    tok = _tok(client, "pepper", "pw-user")
    assert client.delete("/knowledge/999999", headers={"Authorization": "Bearer " + tok}).status_code == 404


def test_create_user_role_must_be_valid(client):
    admin = _tok(client, "tony", "pw-admin")
    r = client.post("/admin/users", headers={"Authorization": "Bearer " + admin},
                    json={"username": "x", "password": "y", "role": "superuser"})
    assert r.status_code == 422            # role is constrained to user|admin


def test_logout_all_revokes_sessions(client):
    a = _tok(client, "pepper", "pw-user")
    b = _tok(client, "pepper", "pw-user")    # second device/session
    assert client.post("/auth/logout-all", headers={"Authorization": "Bearer " + a}).status_code == 200
    # both tokens are now dead
    assert client.get("/sessions", headers={"Authorization": "Bearer " + a}).status_code == 403
    assert client.get("/sessions", headers={"Authorization": "Bearer " + b}).status_code == 403


def test_security_headers_present(client):
    r = client.get("/health")
    assert "default-src 'self'" in r.headers.get("Content-Security-Policy", "")
    assert r.headers.get("Referrer-Policy") == "no-referrer"


def test_tts_requires_auth(client):
    assert client.post("/tts", json={"text": "hi"}).status_code == 401


def test_tts_validation_and_synthesis(client):
    admin = _tok(client, "tony", "pw-admin")
    h = {"Authorization": "Bearer " + admin}
    assert client.post("/tts", headers=h, json={"text": ""}).status_code == 422   # empty rejected
    # valid text → 200 with audio, or 503 if Piper isn't present in the test env
    assert client.post("/tts", headers=h, json={"text": "Good evening"}).status_code in (200, 503)


def test_greeting(client):
    assert client.get("/greeting").status_code == 401
    admin = _tok(client, "tony", "pw-admin")
    r = client.get("/greeting", headers={"Authorization": "Bearer " + admin})
    assert r.status_code == 200 and "text" in r.json()


def test_face_enroll_requires_admin(client):
    tok = _tok(client, "pepper", "pw-user")
    r = client.post("/faces/enroll", headers={"Authorization": "Bearer " + tok},
                    json={"name": "x", "embedding": [0.1] * 16})
    assert r.status_code == 403


def test_face_person_multi_embedding_link_rename_delete(client):
    admin = _tok(client, "tony", "pw-admin")
    h = {"Authorization": "Bearer " + admin}
    # two enrollments for the same name → one person with TWO embeddings
    assert client.post("/faces/enroll", headers=h, json={"name": "Ravi", "embedding": [0.1] * 16}).status_code == 200
    assert client.post("/faces/enroll", headers=h, json={"name": "Ravi", "embedding": [0.2] * 16, "source": "laptop-cam"}).status_code == 200
    enrolled = client.get("/faces/enrolled", headers=h).json()["enrolled"]
    assert "Ravi" in enrolled and len(enrolled["Ravi"]) == 2 and len(enrolled["Ravi"][0]) == 16   # list-per-person
    person = next(f for f in client.get("/admin/faces", headers=h).json()["faces"] if f["name"] == "Ravi")
    pid_face, _ = person["id"], None
    assert person["embedding_count"] == 2
    # list the individual embeddings, delete one → count drops to 1
    embs = client.get(f"/admin/faces/{pid_face}/embeddings", headers=h).json()["embeddings"]
    assert len(embs) == 2
    assert client.delete(f"/admin/faces/embeddings/{embs[0]['id']}", headers=h).status_code == 200
    assert next(f for f in client.get("/admin/faces", headers=h).json()["faces"] if f["id"] == pid_face)["embedding_count"] == 1
    # link to a user, then rename must NOT clobber the link
    c = sqlite3.connect(_DB); uid = c.execute("SELECT id FROM users WHERE username='pepper'").fetchone()[0]; c.close()
    assert client.put(f"/admin/faces/{pid_face}", headers=h, json={"user_id": uid}).status_code == 200
    assert client.put(f"/admin/faces/{pid_face}", headers=h, json={"name": "Ravi J"}).status_code == 200
    row = next(f for f in client.get("/admin/faces", headers=h).json()["faces"] if f["id"] == pid_face)
    assert row["name"] == "Ravi J" and row["user_id"] == uid and row["username"] == "pepper"
    # delete the person → gone, and its embeddings gone from the edge feed
    assert client.delete(f"/admin/faces/{pid_face}", headers=h).status_code == 200
    assert "Ravi J" not in client.get("/faces/enrolled", headers=h).json()["enrolled"]


def test_face_enroll_replace(client):
    admin = _tok(client, "tony", "pw-admin")
    h = {"Authorization": "Bearer " + admin}
    client.post("/faces/enroll", headers=h, json={"name": "Repl", "embedding": [0.1] * 16})
    client.post("/faces/enroll", headers=h, json={"name": "Repl", "embedding": [0.2] * 16})
    client.post("/faces/enroll", headers=h, json={"name": "Repl", "embedding": [0.3] * 16, "replace": True})
    assert len(client.get("/faces/enrolled", headers=h).json()["enrolled"]["Repl"]) == 1   # replaced
    pid = next(f["id"] for f in client.get("/admin/faces", headers=h).json()["faces"] if f["name"] == "Repl")
    client.delete(f"/admin/faces/{pid}", headers=h)
