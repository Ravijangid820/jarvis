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


def test_events_ingest_and_admin_list(client):
    tok = _tok(client, "pepper", "pw-user")   # any valid token/key may post (devices use API keys)
    r = client.post("/events", headers={"Authorization": "Bearer " + tok},
                    json={"device_id": "pi-test", "type": "face_seen", "data": {"name": "Ravi"}})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    # admin can read it back, with data round-tripped from JSON
    admin = _tok(client, "tony", "pw-admin")
    got = client.get("/admin/events", headers={"Authorization": "Bearer " + admin}).json()
    assert got["count"] >= 1
    latest = got["events"][0]
    assert latest["device_id"] == "pi-test" and latest["type"] == "face_seen"
    assert latest["data"] == {"name": "Ravi"}


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
