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


def _seed_user(username, password, role, household_id=1):
    """Seed a user. household_id defaults to 1 — the primary household every migration backfills
    into — so the existing single-tenant tests read as before; the isolation tests pass a second."""
    c = sqlite3.connect(_DB)
    c.execute("INSERT INTO users (username, password_hash, role, household_id) VALUES (?, ?, ?, ?)",
              (username, auth.hash_password(password), role, household_id))
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
    with TestClient(main.app) as c:   # runs lifespan -> init_db on the temp DB
        conn = sqlite3.connect(_DB)
        conn.execute("DELETE FROM users")
        conn.commit()
        conn.close()
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
    # A recognized face also emits a presence_arrival event, so find the face_seen explicitly.
    seen = [e for e in got["events"] if e["type"] == "face_seen" and e["device_id"] == "pi-test"]
    assert seen and seen[0]["data"] == {"name": "Ravi"}


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


def test_mcp_and_model_inventory_are_admin_only(client):
    user = _tok(client, "pepper", "pw-user")
    admin = _tok(client, "tony", "pw-admin")
    assert client.get("/mcp/servers").status_code == 401
    assert client.get("/models").status_code == 401
    assert client.get("/mcp/servers", headers={"Authorization": "Bearer " + user}).status_code == 403
    assert client.get("/models", headers={"Authorization": "Bearer " + user}).status_code == 403
    assert client.get("/mcp/servers", headers={"Authorization": "Bearer " + admin}).status_code == 200
    assert client.get("/models", headers={"Authorization": "Bearer " + admin}).status_code == 200


def test_chat_token_estimate_uses_llama_counter_or_fallback(client, monkeypatch):
    admin = _tok(client, "tony", "pw-admin")
    monkeypatch.setattr(main, "count_prompt_tokens", lambda messages: {"tokens": 42, "source": "llama.cpp"})
    r = client.post("/chat/token-estimate", headers={"Authorization": "Bearer " + admin},
                    json={"text": "Count this prompt", "session_id": "default"})
    assert r.status_code == 200
    assert r.json()["tokens"] == 42
    assert r.json()["source"] == "llama.cpp"


def test_mcp_tool_discovery_is_admin_only_and_returns_reviewable_tools(client, monkeypatch):
    admin = _tok(client, "tony", "pw-admin")
    user = _tok(client, "pepper", "pw-user")
    monkeypatch.setattr(main.mcp, "get_servers", lambda: [{"name": "weather", "url": "https://mcp.example/tools"}])
    monkeypatch.setattr(main.mcp, "discover_tools", lambda url: [{
        "name": "forecast", "description": "Get a forecast", "inputSchema": {"type": "object", "properties": {}},
    }])
    assert client.get("/mcp/servers/weather/tools", headers={"Authorization": "Bearer " + user}).status_code == 403
    r = client.get("/mcp/servers/weather/tools", headers={"Authorization": "Bearer " + admin})
    assert r.status_code == 200
    assert r.json()["tools"][0]["name"] == "forecast"


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


def test_voice_kiosk_shell_is_served_like_admin(client):
    """/voice is an SPA view, so the shell must be reachable without a token — the page itself
    renders the login screen until one exists, and every endpoint it calls is authenticated.
    Without the route AND the middleware allowlist entry, opening it is a bare 401/404."""
    r = client.get("/voice")
    assert r.status_code in (200, 404)          # 404 only when no frontend build is present
    if r.status_code == 200:
        assert "text/html" in r.headers["content-type"]
    # the middleware must not demand a Bearer token for it (that would be a 401)
    assert r.status_code != 401


# --- personal memory: the "About me" panel's backing store -------------------------------------

def test_personal_facts_reach_the_users_own_prompt(client):
    """A fact added here has to end up in the USER PROFILE block, or the panel is decorative."""
    import chat
    tok = _tok(client, "pepper", "pw-user")
    h = {"Authorization": "Bearer " + tok}
    assert client.post("/knowledge", headers=h,
                       json={"content": "I prefer concise answers.", "category": "preferences"}).status_code == 200
    c = sqlite3.connect(_DB)
    user_id = c.execute("SELECT id FROM users WHERE username='pepper'").fetchone()[0]
    c.close()
    system_prompt = chat.build_messages("s-mem", user_id, 1, "hello")[0]["content"]
    assert "--- USER PROFILE ---" in system_prompt
    assert "I prefer concise answers." in system_prompt


def test_personal_facts_report_their_prompt_budget(client):
    """The panel shows a budget meter because the block is truncated head-first at the cap — past
    it, facts silently stop reaching the model. The numbers have to come from the server so the
    UI can't drift from truncate_to_tokens' actual conversion."""
    h = {"Authorization": "Bearer " + _tok(client, "pepper", "pw-user")}
    d = client.get("/knowledge", headers=h).json()
    assert d["prompt_char_budget"] > 0
    assert d["prompt_chars"] > 0                      # pepper has a fact by now
    assert d["prompt_chars"] <= d["prompt_char_budget"]


def test_personal_facts_are_private_to_their_owner(client):
    """Unlike household knowledge, these are per-USER: another account in the same household must
    neither read nor delete them."""
    ph = {"Authorization": "Bearer " + _tok(client, "pepper", "pw-user")}
    th = {"Authorization": "Bearer " + _tok(client, "tony", "pw-admin")}
    fid = client.post("/knowledge", headers=ph,
                      json={"content": "Pepper's private note.", "category": "personal"}).json()["id"]
    mine = client.get("/knowledge", headers=ph).json()
    theirs = client.get("/knowledge", headers=th).json()
    assert any(f["id"] == fid for f in mine["facts"])
    assert not any(f["content"] == "Pepper's private note." for f in theirs["facts"])
    # an admin of the same household still cannot reach another user's fact by id
    assert client.delete(f"/knowledge/{fid}", headers=th).status_code == 404
    assert client.put(f"/knowledge/{fid}", headers=th,
                      json={"content": "hijacked", "category": "personal"}).status_code == 404


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


def test_csp_allows_wasm_but_never_plain_eval(client):
    """The in-browser Whisper runtime needs WebAssembly.instantiate, which CSP gates.

    'wasm-unsafe-eval' grants exactly that. 'unsafe-eval' would additionally re-enable
    eval()/new Function() for ordinary JavaScript, handing any future XSS a code-execution
    primitive — so widening this to the blunter keyword must fail loudly here.
    """
    csp = client.get("/health").headers.get("Content-Security-Policy", "")
    script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
    assert "'wasm-unsafe-eval'" in script_src
    assert "'unsafe-eval'" not in script_src.replace("'wasm-unsafe-eval'", "")


def test_immutable_assets_do_not_pin_a_csp_into_the_browser_cache(client):
    """Immutable, content-hashed asset responses must NOT carry a CSP.

    A dedicated Web Worker enforces the policy delivered with its own script response, and an
    immutable response is cached headers-and-all — so a CSP shipped here freezes into the browser
    for a year and a later policy change never reaches the worker, while the document already has
    the new one. Workers inherit the creating document's policy, which is no-store and therefore
    always current, so omitting it here loses nothing.
    """
    r = client.get("/assets/does-not-exist.js")
    assert "immutable" in r.headers.get("Cache-Control", "")
    assert "Content-Security-Policy" not in r.headers
    # The document itself must still carry it.
    assert "Content-Security-Policy" in client.get("/health").headers


def test_immutable_caching_only_on_content_addressed_paths(client):
    """`immutable` tells a browser never to revalidate — only honest when the URL changes with
    the bytes. /ort/<version>/ carries the runtime version, so it qualifies. /stt-models/ has
    fixed filenames, so re-pinning the bundle would leave clients on year-old weights.
    """
    ort = client.get("/ort/1.2.3/ort-wasm-simd-threaded.wasm")
    assert "immutable" in ort.headers.get("Cache-Control", "")
    stt = client.get("/stt-models/onnx-community/whisper-base/config.json")
    assert "immutable" not in stt.headers.get("Cache-Control", "")


def test_cross_origin_isolation_headers_present(client):
    """Without both of these the browser withholds SharedArrayBuffer and the in-browser
    speech-to-text runtime silently drops to a single thread."""
    r = client.get("/health")
    assert r.headers.get("Cross-Origin-Opener-Policy") == "same-origin"
    assert r.headers.get("Cross-Origin-Embedder-Policy") == "require-corp"


def test_stt_model_host_is_reachable_from_the_browser(client):
    """The browser fetches Whisper weights from huggingface.co, which 302s them to a CDN
    host under hf.co. Allowing only the first host breaks the download mid-redirect."""
    csp = client.get("/health").headers.get("Content-Security-Policy", "")
    connect_src = next(d for d in csp.split(";") if d.strip().startswith("connect-src"))
    assert "https://huggingface.co" in connect_src
    assert "https://*.hf.co" in connect_src


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


def test_ca_cert_is_public(client):
    # /ca.crt must be reachable WITHOUT a token (devices bootstrap trust from it). In the test env
    # there's no tls/ca.crt, so it's 404 (reachable, no cert) — crucially NOT 401 (auth required).
    assert client.get("/ca.crt").status_code == 404


def test_faces_enrolled_is_not_readable_by_ordinary_members(client):
    """The enrolled set is every face TEMPLATE in the household — enough to replay someone's
    identity — so it is device-keys-and-admins only. Ordinary members recognise via /faces/identify,
    which answers a question without handing over the material to answer it themselves."""
    user = _tok(client, "pepper", "pw-user")
    assert client.get("/faces/enrolled", headers={"Authorization": "Bearer " + user}).status_code == 403
    admin = _tok(client, "tony", "pw-admin")
    assert client.get("/faces/enrolled", headers={"Authorization": "Bearer " + admin}).status_code == 200
    key = _seed_device_key("pepper", "cam-enrolled")          # a camera still needs the set locally
    assert client.get("/faces/enrolled", headers={"Authorization": "Bearer " + key}).status_code == 200


def test_face_identify_matches_names_and_rejects_strangers(client):
    admin = _tok(client, "tony", "pw-admin")
    h = {"Authorization": "Bearer " + admin}
    user = {"Authorization": "Bearer " + _tok(client, "pepper", "pw-user")}
    # Nobody enrolled yet → name is null, which the UI must distinguish from "unknown".
    assert client.post("/faces/identify", headers=user, json={"embedding": [1.0] + [0.0] * 15}
                       ).json()["name"] is None
    vec = [1.0] + [0.0] * 15                                  # unit vector, as the client sends
    assert client.post("/faces/enroll", headers=h, json={"name": "Ident", "embedding": vec}).status_code == 200
    # The same face → the person's name, at cosine 1.0.
    hit = client.post("/faces/identify", headers=user, json={"embedding": vec}).json()
    assert hit["name"] == "Ident" and hit["score"] == 1.0
    # An orthogonal vector scores 0.0, below the 0.363 threshold → "unknown", never a false accept.
    miss = client.post("/faces/identify", headers=user, json={"embedding": [0.0, 1.0] + [0.0] * 14}).json()
    assert miss["name"] == "unknown" and miss["score"] == 0.0
    # Recognising must not leak the vectors themselves back to an ordinary member.
    assert "embedding" not in hit and "embedding" not in miss
    pid = next(f["id"] for f in client.get("/admin/faces", headers=h).json()["faces"] if f["name"] == "Ident")
    client.delete(f"/admin/faces/{pid}", headers=h)


def test_face_identify_ignores_wrong_width_vectors(client):
    """A vector from a different model can't be compared. It must be skipped, not zip()-truncated
    into a bogus partial cosine that could clear the threshold."""
    admin = _tok(client, "tony", "pw-admin")
    h = {"Authorization": "Bearer " + admin}
    assert client.post("/faces/enroll", headers=h, json={"name": "Wide", "embedding": [1.0] + [0.0] * 31}
                       ).status_code == 200
    got = client.post("/faces/identify", headers=h, json={"embedding": [1.0] + [0.0] * 15}).json()
    assert got["name"] is None and got["score"] is None
    pid = next(f["id"] for f in client.get("/admin/faces", headers=h).json()["faces"] if f["name"] == "Wide")
    client.delete(f"/admin/faces/{pid}", headers=h)


# --- the public-path allowlist must not match by accident ---------------------------------------
# The allowlist mixed exact membership with endswith()/in substring tests, so any path merely
# ENDING in "/ca.crt" or a favicon name skipped authentication entirely. GET /history/ca.crt reached
# its handler unauthenticated and 500'd on the user_id the middleware never set — the crash was the
# only thing standing in for the auth check.

@pytest.mark.parametrize("path", [
    "/history/ca.crt",
    "/history/favicon.svg",
    "/history/favicon.ico",
    "/history/favicon.png",
    "/admin/backups/ca.crt",
    "/sessions/ca.crt",
])
def test_a_route_named_like_a_public_file_still_requires_auth(client, path):
    r = client.get(path)
    assert r.status_code == 401, (
        f"{path} returned {r.status_code}; a 500 means it reached the handler with no auth, "
        "and a 403 means it reached an in-handler check rather than the gate")


@pytest.mark.parametrize("path", [
    "/history/x/assets/y",       # the "/assets/" in path test
    "/history/a/static/b",       # the "/static/" in path test
    "/history/a/ort/b",          # the "/ort/" in path test
    "/history/a/stt-models/b",
])
def test_a_route_containing_an_asset_prefix_still_requires_auth(client, path):
    """The substring tests were the other half: a path merely CONTAINING /assets/ was public."""
    assert client.get(path).status_code in (401, 404), \
        f"{path} must be authenticated (or simply not exist), never silently public"


@pytest.mark.parametrize("path", ["/health", "/ca.crt", "/favicon.ico", "/auth/login"])
def test_genuinely_public_paths_are_still_public(client, path):
    """The fix must not over-correct: these are unauthenticated by design."""
    assert client.get(path).status_code != 401, f"{path} should not require a token"


def test_auth_failures_carry_the_security_headers(client):
    """401/403 were returned as bare Responses that bypassed the header helper, so the CSP,
    nosniff and framing protections were on every success and absent on exactly the responses an
    attacker provokes."""
    r = client.get("/sessions")
    assert r.status_code == 401
    assert r.headers.get("content-security-policy"), "no CSP on the 401"
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("content-type", "").startswith("application/json")


# --- password rotation ---------------------------------------------------------------------------
def test_password_can_be_changed_and_the_old_one_stops_working(client):
    _seed_user("rotator", "old-password-1", "user")
    tok = _tok(client, "rotator", "old-password-1")
    h = {"Authorization": "Bearer " + tok}
    r = client.post("/auth/password", headers=h,
                    json={"current_password": "old-password-1", "new_password": "new-password-2"})
    assert r.status_code == 200
    assert client.post("/auth/login", json={"username": "rotator", "password": "old-password-1"}).status_code == 401
    assert client.post("/auth/login", json={"username": "rotator", "password": "new-password-2"}).status_code == 200


def test_a_stolen_token_alone_cannot_take_over_the_account(client):
    """The current password is verified rather than the session trusted — otherwise a leaked token
    would be enough to lock the real owner out."""
    _seed_user("victim", "victim-password", "user")
    h = {"Authorization": "Bearer " + _tok(client, "victim", "victim-password")}
    r = client.post("/auth/password", headers=h,
                    json={"current_password": "wrong-guess", "new_password": "attacker-set-1"})
    assert r.status_code == 403
    assert client.post("/auth/login", json={"username": "victim", "password": "victim-password"}).status_code == 200


def test_changing_the_password_revokes_other_sessions_but_not_this_one(client):
    _seed_user("multi", "multi-password", "user")
    stale = _tok(client, "multi", "multi-password")     # a second, older session
    mine = _tok(client, "multi", "multi-password")
    r = client.post("/auth/password", headers={"Authorization": "Bearer " + mine},
                    json={"current_password": "multi-password", "new_password": "multi-password-2"})
    assert r.status_code == 200 and r.json()["other_sessions_revoked"] >= 1
    # 403, not 401: the middleware distinguishes "no Bearer header" from "a token that is no
    # longer valid", and the revoked one is the latter.
    assert client.get("/sessions", headers={"Authorization": "Bearer " + stale}).status_code == 403
    assert client.get("/sessions", headers={"Authorization": "Bearer " + mine}).status_code == 200


def test_a_legacy_hash_upgrades_itself_on_the_next_login(client):
    """verify_password still accepts the old 100k-iteration form, six times below the floor this
    code sets for itself. There was no rotation path, so those hashes were stranded forever."""
    import hashlib
    salt = "0123456789abcdef"
    key = hashlib.pbkdf2_hmac("sha256", b"legacy-pass", salt.encode(), 100_000).hex()
    c = sqlite3.connect(_DB)
    c.execute("INSERT INTO users (username, password_hash, role, household_id) VALUES (?,?,?,1)",
              ("legacyuser", f"{salt}:{key}", "user"))
    c.commit(); c.close()

    assert client.post("/auth/login", json={"username": "legacyuser", "password": "legacy-pass"}).status_code == 200
    c = sqlite3.connect(_DB)
    stored = c.execute("SELECT password_hash FROM users WHERE username='legacyuser'").fetchone()[0]
    c.close()
    assert stored.startswith("pbkdf2_sha256$"), "the legacy hash should have healed on sign-in"
    # and the account still works afterwards
    assert client.post("/auth/login", json={"username": "legacyuser", "password": "legacy-pass"}).status_code == 200


# --- CORS must not default to "*" ----------------------------------------------------------------
# "*" let any site a LAN browser visits read this API's unauthenticated responses. Not forwarding a
# port is no defence: the request is made BY a browser that is already inside the network.

def test_no_cross_origin_access_by_default(client):
    r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200, "the request itself still succeeds; the browser is what blocks"
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}, \
        "an unconfigured deployment must not hand its responses to arbitrary sites"


def test_same_origin_use_is_unaffected(client):
    """The bundled SPA calls the API with relative URLs, so it never sends an Origin at all. This
    is the check that tightening CORS cannot lock the owner out of their own UI.

    Asserts the request is SERVED, not that the LLM is up: /health reports "offline" wherever
    llama-server is not running, which is every CI runner. The first version of this test asserted
    status == "ok" and passed only on the author's box, where the model happens to be live —
    the same mistake as the ffmpeg-dependent mic test before it.
    """
    r = client.get("/health")
    assert r.status_code == 200
    assert "status" in r.json()          # served and parsed; "offline" here is a fine answer
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}, \
        "a same-origin request needs no CORS header, and getting one back would mean the " \
        "middleware is answering requests it should be ignoring"
