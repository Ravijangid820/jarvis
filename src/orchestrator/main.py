"""
Jarvis AI Orchestrator — FastAPI app: routing, auth middleware, and request handling.

Domain logic lives in focused modules:
  config   — configuration + tunables + logging
  db       — SQLite connections + schema init
  auth     — password hashing
  llm      — LLM client (blocking/streaming) + Piper TTS
  memory   — embeddings, vector store, knowledge base, idle fact extraction
  chat     — sessions, message persistence, context-window-aware prompt assembly
  budget   — pure prompt-token-budgeting helpers (unit-tested)
"""
import json
import os
import secrets
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import chat
import deps
import ha_config
import purge
import memory
from routes import admin as routes_admin
from routes import chat as routes_chat
from routes import devices as routes_devices
from routes import faces as routes_faces
from routes import mcp as routes_mcp
from routes import voice as routes_voice
from auth import hash_password, hash_token, verify_password
from config import (ALLOWED_ORIGIN_REGEX, ALLOWED_ORIGINS, APP_VERSION, BASE_DIR, CONFIG, DEMO_MINT_PER_IP_HOURLY,
                    DEMO_PASSWORD, DEMO_PUBLIC_SIGNUP, DEMO_TTL_MINUTES,
                    DEMO_USER_ID_BASE, DEMO_USERNAME,
                    INDEX_HTML, KNOWLEDGE_TOKEN_CAP, LLM_URL, PIPER_BIN, PIPER_MODEL,
                    RATE_LIMIT_RPM, REACT_DIST_DIR, FACE_MODELS_DIR, STATIC_DIR, STT_MODELS_DIR, VALID_FACT_CATEGORIES,
                    WAKE_MODELS_DIR,
                    JARVIS_MODE, logger)
import ha
from db import (get_db, init_db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ha_config.load_settings()        # runtime HA config (env > DB), before anything serves
    memory.init_embeddings()   # load the model now (from cache), not at import time
    ha_config.rebuild_intent_router()   # semantic device-intent index (needs both of the above)
    memory.start_memory_worker()
    if DEMO_PUBLIC_SIGNUP:
        # Purge anything left over from a previous run before serving: a crash mid-session must not
        # leave a demo visitor's data sitting in the database until the first sweep.
        _sweep_expired_demo_households()
        threading.Thread(target=_demo_sweeper_loop, daemon=True).start()
        logger.info("Public demo signup ENABLED (TTL %d min, %d mints/hour/IP)",
                    DEMO_TTL_MINUTES, DEMO_MINT_PER_IP_HOURLY)
    logger.info("Jarvis Orchestrator started with Auth + Memory Core")
    yield
    # Nothing to drain on the way out: pending embeddings live in conversation_history.embedded,
    # so whatever has not been flushed is picked up by the next start rather than lost here.


app = FastAPI(title="Jarvis Orchestrator", docs_url=None, redoc_url=None, lifespan=lifespan)

# CORS. Empty means NO cross-origin caller is allowed — it used to mean "*", which is backwards:
# the least-configured deployment got the most permissive policy. "LAN only" is no defence here
# either, because the request is made by a BROWSER already inside the LAN; a page on any site the
# owner visits can reach 192.168.x.y and, under "*", read the reply. Port forwarding never enters
# into it.
#
# Nothing is lost by defaulting to none: the bundled SPA is served by this same process and calls
# the API with relative URLs, so it is same-origin and never consults CORS. Set allowed_origins
# (exact origins — the spec has no CIDR form) or allowed_origin_regex only for a genuinely
# separate front end, such as a Vite dev server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX or None,
    # Safe to enable now that neither field can be "*": credentials plus a wildcard is the
    # combination browsers refuse outright, and the reason this was previously switched off.
    allow_credentials=bool(ALLOWED_ORIGINS or ALLOWED_ORIGIN_REGEX),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Default device a spoken volume command targets (the Windows volume agent's device id). Matches
# VolumeRequest's default; spoken commands don't name a device, so they go here.
# Greet-on-arrival: a recognized person not seen for ARRIVAL_GAP_S counts as a fresh arrival.
_present_since: Dict[str, float] = {}
ARRIVAL_GAP_S = 300.0

# --- Rate limiting (in-process) ---------------------------------------------
_rate_store: Dict[str, List[float]] = defaultdict(list)
# Login brute-force guard, keyed on USERNAME (not client IP): behind the Tailscale subnet
# router every request shares one source IP, so an IP bucket would be one global bucket that
# any client could exhaust to lock everyone out. Per-username throttling targets the actual
# brute-force surface and can't cause a cross-account lockout.
_login_store: Dict[str, List[float]] = defaultdict(list)
LOGIN_MAX_PER_MIN = 8
_last_sweep = [0.0]


# Demo-session minting, keyed on client IP over an HOUR (not a minute) — see _client_ip for why
# this is a bound on table growth rather than a security control.
_demo_mint_store: Dict[str, List[float]] = defaultdict(list)


# Endpoints the UI polls on a timer rather than because the user did something. They must NOT
# count as activity: the app polls /system every 5s, /arrivals every 15s and /reminders/due every
# 20s while a tab is open, so treating those as activity would slide a demo session's expiry
# forever and an ABANDONED OPEN TAB would never be reclaimed — precisely the case the TTL sweeper
# exists for. test_demo.py pins this, so adding a new poller without listing it here fails the
# suite rather than silently resurrecting the bug.
_PASSIVE_PATHS = frozenset({"/system", "/arrivals", "/reminders/due", "/demo/status"})


def _touch_demo_session(conn, household_id: int, token_hash: str) -> None:
    """Push a demo household's (and its token's) expiry out to now + TTL. Best-effort: a failed
    refresh only means the session expires earlier, never that it outlives its TTL."""
    try:
        window = f"+{int(DEMO_TTL_MINUTES)} minutes"
        conn.execute("UPDATE households SET expires_at = datetime('now', ?) WHERE id = ?",
                     (window, household_id))
        conn.execute("UPDATE auth_sessions SET expires_at = datetime('now', ?) WHERE token = ?",
                     (window, token_hash))
        conn.commit()
    except Exception as e:
        logger.debug("demo session touch failed for household %s: %s", household_id, e)


def _sweep_rate_stores(now: float) -> None:
    """Drop fully-expired buckets so the dicts don't grow unbounded with distinct keys."""
    for store, window in ((_rate_store, 60.0), (_login_store, 60.0), (_demo_mint_store, 3600.0)):
        for k in [k for k, v in store.items() if not any(t > now - window for t in v)]:
            del store[k]


def _allow(store: Dict[str, List[float]], key: str, limit: int, window_s: float = 60.0) -> bool:
    now = time.time()
    if now - _last_sweep[0] > 300.0:
        _sweep_rate_stores(now)
        _last_sweep[0] = now
    bucket = [t for t in store[key] if t > now - window_s]
    if len(bucket) >= limit:
        store[key] = bucket
        return False
    bucket.append(now)
    store[key] = bucket
    return True


def check_rate_limit(key: str) -> bool:
    return _allow(_rate_store, key, RATE_LIMIT_RPM)


def check_login_rate(username: str) -> bool:
    return _allow(_login_store, f"login:{username.lower()}", LOGIN_MAX_PER_MIN)


# Tight CSP: the SPA is a Vite build with an external module bundle (no inline <script>), so
# script-src can stay 'self'. style-src needs 'unsafe-inline' for React inline styles; media/img
# allow data: for TTS audio + inline SVG. This is the second line of defence behind the
# render-as-React-nodes / http(s)-only-links invariants — it neutralises any future XSS regression.
_CSP = (
    # 'wasm-unsafe-eval' permits WebAssembly.instantiate — required by the in-browser Whisper
    # runtime — and NOTHING else. It is deliberately not 'unsafe-eval', which would re-enable
    # eval()/new Function() for ordinary JavaScript and hand any future XSS a code-execution
    # primitive. This keyword grants WASM compilation only, so the guarantee that matters here
    # (no attacker-supplied *script* can be evaluated) is unchanged.
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; media-src 'self' data: blob:; "
    # connect-src: 'self' covers the API and the /stt-models failsafe bundle. The two HF hosts are
    # the browser-side speech-to-text model's OFFICIAL source — huggingface.co serves the metadata
    # and 302s the weights to a CDN host under hf.co (currently us.aws.cdn.hf.co), so BOTH are
    # required or the redirect is blocked mid-download. This is the one deliberate egress in the
    # app: STT model weights only, fetched by the browser (never the server), first-party, and it
    # falls back to our own copy if it fails. No conversation data ever leaves the network.
    "connect-src 'self' https://huggingface.co https://*.hf.co; "
    # ORT spawns its WASM threading workers from blob URLs; without blob: the multi-threaded
    # runtime silently degrades to single-threaded.
    "worker-src 'self' blob:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


def _apply_security_headers(response: Response, cache: str = "no-store", csp: bool = True,
                            request: Optional[Request] = None) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    # csp=False for immutable, content-hashed assets. A dedicated Web Worker enforces the CSP
    # delivered with ITS OWN script response, and an immutable response is cached headers-and-all —
    # so a CSP shipped on such an asset gets frozen into the browser for a year, and a later policy
    # change silently fails to reach the worker while the document already has the new one. (That is
    # exactly how a stale `script-src 'self'` kept blocking WASM after the policy was widened.)
    # Omitting it is safe: workers inherit the creating document's policy, and the document is
    # no-store, so the effective policy is always current. Documents get the header; caches don't.
    if csp:
        response.headers["Content-Security-Policy"] = _CSP
    response.headers["Referrer-Policy"] = "no-referrer"
    # Cross-origin isolation — required for SharedArrayBuffer, which is what lets the in-browser
    # Whisper runtime use multiple threads instead of one. require-corp (not credentialless) because
    # the HF CDN answers with `access-control-allow-origin: *`, so the model fetch satisfies it, and
    # require-corp has the wider browser support of the two.
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    response.headers["Cache-Control"] = cache
    # HSTS only where it is honest. Over plain HTTP the header is ignored anyway, and asserting a
    # year of HTTPS-only for a deployment that terminates TLS elsewhere would be a promise made on
    # someone else's behalf. Set here rather than at each return so no exit can forget it.
    if request is not None and request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response



# Paths reachable WITHOUT a token. Split into exact documents and prefix-anchored trees, because
# the previous form mixed `path in [...]` with `endswith()` and `in` substring tests — and those
# two matched far more than the routes they were written for. Any request whose path merely ENDED
# in "/ca.crt" or a favicon name skipped authentication entirely, so `GET /history/ca.crt` reached
# its handler unauthenticated (it 500'd on the user_id the middleware never set, which is the only
# reason nothing leaked). A route added later that reads the database before touching
# request.state would have been silently public to anyone who named their resource "ca.crt".
#
# The rule now: exact membership for documents, startswith for trees. Nothing is public by accident,
# and adding a route cannot quietly opt it out of auth.
PUBLIC_PATHS = frozenset({
    "/health", "/", "/admin", "/voice", "/auth/login",
    # /demo/session is unauthenticated by design — it is how a visitor GETS a credential, exactly
    # like /auth/login. It creates its own isolated household and can reach no existing data; its
    # own per-IP limit stands in for the auth check.
    "/demo/session",
    "/favicon.svg", "/favicon.png", "/favicon.ico", "/ca.crt",
})
# Static trees. Every mount is absolute at the root (see app.mount below) and there is no root_path
# or base-path deployment, so anchoring these at "/" loses nothing the suffix tests provided.
PUBLIC_PREFIXES = ("/assets/", "/static/", "/stt-models/", "/face-models/", "/wake-models/", "/ort/")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        resp = await call_next(request)
        # Vite emits content-hashed bundles under /assets — safe to cache forever.
        if path.startswith("/assets/"):
            return _apply_security_headers(resp, "public, max-age=31536000, immutable", csp=False, request=request)
        # The STT bundle is unauthenticated on purpose: it is a public, SHA-256-pinned upstream
        # model — no secret — and the Web Worker that fetches it cannot attach a Bearer token.
        # Immutable because the pinned files only change with a version bump.
        # `immutable` is only ever honest for a CONTENT-ADDRESSED url. /ort/<version>/… is
        # (the ORT version is in the path), so it may be cached forever. /stt-models/… is NOT:
        # the filenames are fixed, so re-pinning the bundle would swap the bytes underneath a
        # url browsers had been told never to revalidate — and they would keep the old weights
        # for a year. It gets a revalidating policy instead; StaticFiles serves ETags, so the
        # normal case is a cheap 304 rather than a re-download.
        if path.startswith("/ort/"):
            return _apply_security_headers(resp, "public, max-age=31536000, immutable", csp=False, request=request)
        # Same reasoning as /stt-models: fixed filenames, so `immutable` would be a lie if the
        # pinned weights were ever re-pinned. Revalidate; StaticFiles' ETag makes that a cheap 304.
        if path.startswith(("/stt-models/", "/face-models/", "/wake-models/")):
            return _apply_security_headers(resp, "public, no-cache", csp=False, request=request)
        return _apply_security_headers(resp, request=request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        # Through the header helper, not around it: these are the responses an attacker
        # provokes, and they were the only ones shipping no CSP, no nosniff and no framing
        # protection. media_type so the JSON body is not sniffable as anything else.
        return _apply_security_headers(Response(
            content=json.dumps({"error": "Auth required"}), status_code=401,
            media_type="application/json"), request=request)
    token = auth_header[7:]

    conn = get_db()
    is_authenticated = False
    try:
        # 1. Web-login session token (stored hashed at rest; look up by hash).
        row = conn.execute(
            "SELECT user_id, u.role, u.household_id, h.is_demo FROM auth_sessions s "
            "JOIN users u ON s.user_id = u.id LEFT JOIN households h ON u.household_id = h.id "
            "WHERE s.token = ? AND s.expires_at > datetime('now')", (hash_token(token),)).fetchone()
        if row:
            request.state.user_id = row["user_id"]
            request.state.is_admin = (row["role"] == "admin")
            request.state.household_id = row["household_id"]
            request.state.device_id = None     # web session → not a device-scoped principal
            request.state.is_demo = bool(row["is_demo"])
            is_authenticated = True
            if row["is_demo"] and path not in _PASSIVE_PATHS:
                # Slide the expiry forward on REAL activity, so the TTL measures idle time. A
                # visitor reading a long answer shouldn't have the session vanish underneath them.
                # Background polls are excluded (see _PASSIVE_PATHS) or an open tab would never
                # expire. The token's expiry moves in step so the two can't disagree.
                _touch_demo_session(conn, row["household_id"], hash_token(token))
        else:
            # 2. Per-user API key (machine integrations, e.g. the voice listener / device agents).
            #    Stored hashed at rest, like session tokens — look up by hash. `device_id`, if set,
            #    binds the key to one device (enforced by /devices/* and /events).
            row = conn.execute(
                "SELECT user_id, u.role, u.household_id, k.device_id FROM api_keys k "
                "JOIN users u ON k.user_id = u.id WHERE k.key_string = ?", (hash_token(token),)).fetchone()
            if row:
                request.state.user_id = row["user_id"]
                request.state.household_id = row["household_id"]
                request.state.device_id = row["device_id"]
                # Defense-in-depth: a DEVICE-scoped key never wields admin, even if minted under an
                # admin account. A camera/edge key is for posting events + reading the enrolled set;
                # it must not be usable for /admin/* or enrollment. This bounds a stolen device key's
                # blast radius regardless of which user it belongs to.
                request.state.is_admin = (row["role"] == "admin") and not row["device_id"]
                is_authenticated = True
                try:
                    conn.execute("UPDATE api_keys SET usage_count = usage_count + 1, "
                                 "last_used_at = datetime('now') WHERE key_string = ?", (hash_token(token),))
                    conn.commit()
                except Exception as e:
                    logger.warning("api_keys usage bump failed: %s", e)
    finally:
        conn.close()

    if not is_authenticated:
        return _apply_security_headers(Response(
            content=json.dumps({"error": "Invalid or expired token"}), status_code=403,
            media_type="application/json"), request=request)

    # Rate-limit ALL authenticated callers (admins included), keyed on user id. Exempt the gesture
    # report — it posts at video rate but is gated by an active, separately-authorized mode.
    if path != "/devices/gesture" and not check_rate_limit(f"user:{request.state.user_id}"):
        return Response(
            content=json.dumps({"error": "Rate limit exceeded",
                                "detail": "Rate limit exceeded — slow down a moment and retry."}),
            status_code=429, media_type="application/json", headers={"Retry-After": "5"})

    return _apply_security_headers(await call_next(request), request=request)


# ----------------- Models -----------------
class SessionRenameRequest(BaseModel):
    title: str


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class KnowledgeFactRequest(BaseModel):
    content: str
    category: str = "other"


class GlobalChatRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


class EventRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    type: str = Field(..., min_length=1, max_length=32)
    ts: Optional[str] = Field(default=None, max_length=40)
    data: Optional[Dict[str, Any]] = None

    @field_validator("data")
    @classmethod
    def _cap_data(cls, v):
        # Bound the stored JSON so a caller can't bloat the DB toward disk exhaustion.
        if v is not None and len(json.dumps(v)) > 4096:
            raise ValueError("event data too large (max 4 KB serialized)")
        return v



# ----------------- Auth endpoints -----------------
@app.post("/auth/login")
def login(req: LoginRequest, request: Request):
    # Throttle per-username (not per-IP): login bypasses the per-user limiter, so without this
    # it's an unbounded password-guessing oracle. Keying on username also avoids the global
    # lockout that an IP bucket would cause behind the shared subnet-router source IP.
    if not check_login_rate(req.username):
        raise HTTPException(status_code=429, detail="Too many attempts; try again shortly")
    # On the public demo runtime the suggested demo/demo credentials mint a FRESH sandbox rather
    # than signing in to a shared account. That keeps the hint on the login screen honest (typing
    # what it suggests gets you in) without two visitors ever landing in the same household. The
    # branch is unreachable on any other runtime, where DEMO_PUBLIC_SIGNUP is false.
    if (DEMO_PUBLIC_SIGNUP and req.username.strip().lower() == DEMO_USERNAME
            and req.password == DEMO_PASSWORD):
        return _mint_demo_session(request)
    conn = get_db()
    try:
        row = conn.execute("SELECT id, password_hash, role FROM users WHERE username = ?", (req.username,)).fetchone()
        if not row or not verify_password(req.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        # Self-heal a legacy hash. verify_password still accepts the old "<salt>:<hex>" form at
        # 100k iterations — six times below the floor this code sets for itself — and the comment
        # promising those are "re-hashed on next password change" was empty, because until now
        # there was no way to change a password at all. The plaintext is in hand exactly here, so
        # the upgrade costs nothing and needs no action from the account holder.
        if not row["password_hash"].startswith("pbkdf2_sha256$"):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (hash_password(req.password), row["id"]))
            logger.info("Upgraded legacy password hash for user %d on login", row["id"])
        token = secrets.token_hex(32)
        expires = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        # Store only the hash; the plaintext token is returned to the client once.
        conn.execute("INSERT INTO auth_sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                     (hash_token(token), row["id"], expires))
        conn.execute("DELETE FROM auth_sessions WHERE expires_at <= datetime('now')")  # opportunistic purge
        conn.commit()
        return {"token": token, "role": row["role"]}
    finally:
        conn.close()


def _demo_household_for_token(token: str) -> Optional[int]:
    """The demo household this session token belongs to, or None (expired, unknown, or a real
    account). Used by logout to decide between 'revoke the token' and 'destroy the household'."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT h.id FROM auth_sessions s JOIN users u ON s.user_id = u.id "
            "JOIN households h ON u.household_id = h.id "
            "WHERE s.token = ? AND h.is_demo = 1", (hash_token(token),)).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


# Content every new demo household starts with, so the panels show something instead of empty
# tables. Entirely fictional — a demo visitor must never see a real person's name or address.
_DEMO_KNOWLEDGE = [
    ("home", "The flat has a living room, a kitchen, a study and two bedrooms."),
    ("home", "The study is upstairs at the end of the hall."),
    ("people", "Sam works from the study most weekdays."),
    ("preferences", "The household prefers the lights dim after 9pm."),
]
_DEMO_PEOPLE = ("Sam", "Alex")


def _seed_demo_household(household_id: int, owner_user_id: int) -> None:
    """Populate a fresh demo household with fictional content.

    Best-effort: a demo that starts with empty panels is worse than a demo, but it is not worth
    failing the mint over. Everything written here is destroyed with the household.
    """
    try:
        conn = get_db()
        try:
            for category, content in _DEMO_KNOWLEDGE:
                conn.execute(
                    "INSERT INTO global_knowledge (household_id, category, content, source) "
                    "VALUES (?, ?, ?, 'demo-seed')", (household_id, category, content))
            for name in _DEMO_PEOPLE:
                conn.execute("INSERT INTO persons (household_id, name) VALUES (?, ?)",
                             (household_id, name))
            conn.execute(
                "INSERT INTO audit_log (household_id, user_id, username, action, detail) "
                "VALUES (?, ?, 'system', 'demo.start', 'Demo household created')",
                (household_id, owner_user_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("demo seed for household %s failed: %s", household_id, e)


def _sweep_expired_demo_households() -> int:
    """Destroy demo households past their expires_at. Returns how many were purged.

    This is the backstop for the case the UI cannot signal: a visitor who closes the tab without
    logging out. It is deliberately NOT driven from the browser — a pagehide/sendBeacon hook would
    also fire on a page REFRESH, destroying exactly the session a refresh has to preserve.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id FROM households WHERE is_demo = 1 AND expires_at IS NOT NULL "
            "AND expires_at <= datetime('now')").fetchall()
        ids = [r["id"] for r in rows]
    finally:
        conn.close()
    for hid in ids:
        purge.purge_household_now(hid)
    if ids:
        logger.info("Demo sweeper purged %d expired household(s): %s", len(ids), ids)
    return len(ids)


def _demo_sweeper_loop() -> None:
    """Background sweeper. Runs every minute; a demo household therefore outlives its TTL by at
    most that, and its auth_sessions row has already expired regardless."""
    while True:
        time.sleep(60.0)
        try:
            _sweep_expired_demo_households()
        except Exception as e:
            logger.warning("demo sweeper iteration failed: %s", e)


def _client_ip(request: Request) -> str:
    """Best-effort client identity for the demo mint limit only.

    Deliberately NOT used for anything security-critical: behind the Tailscale subnet router (and
    behind Pages/Cloudflare) many clients share a source IP, and X-Forwarded-For is caller-supplied
    and trivially spoofed. It is good enough to stop a naive script from minting households in a
    loop, which is all it is for.
    """
    xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return xff or (request.client.host if request.client else "unknown")


def _mint_demo_session(request: Request) -> Dict[str, Any]:
    """Create a throwaway demo household and return a token for its admin.

    The visitor becomes an admin OF THEIR OWN, EMPTY household, so the admin console is fully
    usable and contains nothing real. The token is an ordinary session token, stored in
    localStorage like a normal login — which is what makes a page refresh keep the session while
    logout and expiry destroy it.

    Shared by POST /demo/session (the "Try the demo" button) and the demo/demo credentials the
    login screen suggests, so both routes produce the same isolated, expiring sandbox.
    """
    if not DEMO_PUBLIC_SIGNUP:
        raise HTTPException(status_code=404, detail="Not found")
    if not _allow(_demo_mint_store, f"demo:{_client_ip(request)}", DEMO_MINT_PER_IP_HOURLY,
                  window_s=3600.0):
        raise HTTPException(status_code=429,
                            detail="Too many demo sessions from this address; try again later.")
    suffix = secrets.token_hex(4)
    username = f"demo_{suffix}"
    # A random password that is never returned: the account is reachable only via the token minted
    # here, so there is no guessable credential and no way to log back into a demo account later.
    password_hash = hash_password(secrets.token_hex(32))
    token = secrets.token_hex(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=DEMO_TTL_MINUTES)
    expires_s = expires_at.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")      # serialize id selection against concurrent mints
        hh_id = conn.execute(
            "INSERT INTO households (name, is_demo, expires_at) VALUES (?, 1, ?) RETURNING id",
            (f"Demo {suffix}", expires_s)).fetchone()["id"]
        # Demo ids live above DEMO_USER_ID_BASE so they are never recycled into real accounts —
        # see the note on DEMO_USER_ID_BASE in config.py.
        row = conn.execute("SELECT MAX(id) AS m FROM users WHERE id >= ?",
                           (DEMO_USER_ID_BASE,)).fetchone()
        new_id = max(DEMO_USER_ID_BASE, (row["m"] or 0) + 1)
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, can_control_devices, household_id) "
            "VALUES (?, ?, ?, 'admin', 0, ?)",
            (new_id, username, password_hash, hh_id))
        # The auth session expires WITH the household, so an abandoned tab's token dies on schedule
        # even if the sweeper has not run yet.
        conn.execute("INSERT INTO auth_sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                     (hash_token(token), new_id, expires_s))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("demo session mint failed: %s", e)
        raise HTTPException(status_code=500, detail="Could not start a demo session")
    finally:
        conn.close()
    _seed_demo_household(hh_id, new_id)
    logger.info("Demo session minted: household=%d user=%s expires=%s", hh_id, username, expires_s)
    return {"token": token, "role": "admin", "demo": True, "username": username,
            "expires_at": expires_s, "ttl_minutes": DEMO_TTL_MINUTES}


@app.post("/demo/session")
def demo_session(request: Request):
    """Start a demo session ("Try the demo"). 404 on any runtime that isn't the public demo."""
    return _mint_demo_session(request)


@app.get("/demo/status")
def demo_status(request: Request):
    """Time left in the caller's demo session, for the countdown banner.

    Listed in _PASSIVE_PATHS: the banner polls this, and if polling counted as activity the
    countdown would top itself up simply by being watched.
    """
    if not getattr(request.state, "is_demo", False):
        return {"demo": False}
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT expires_at, CAST((julianday(expires_at) - julianday('now')) * 86400 AS INTEGER) "
            "AS remaining FROM households WHERE id = ?", (deps.household(request),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"demo": False}
    return {"demo": True, "expires_at": row["expires_at"],
            "seconds_remaining": max(0, row["remaining"] or 0),
            "ttl_minutes": DEMO_TTL_MINUTES}


@app.post("/auth/logout")
def logout(request: Request):
    """Revoke the caller's current session token server-side (real logout).

    For a DEMO household this is also the reset: logging out destroys the household and everything
    in it, which is the behaviour the demo promises. A refresh does not come through here (the
    token simply persists in localStorage), so the two cases stay properly distinct.
    """
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    demo_household = _demo_household_for_token(token) if token else None
    if demo_household is not None:
        purge.purge_household_now(demo_household)
        logger.info("Demo household %d purged on logout", demo_household)
        return {"status": "ok", "demo_reset": True}
    if token:
        conn = get_db()
        try:
            conn.execute("DELETE FROM auth_sessions WHERE token = ?", (hash_token(token),))
            conn.commit()
        finally:
            conn.close()
    return {"status": "ok"}


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=200)
    new_password: str = Field(..., min_length=8, max_length=200)


@app.post("/auth/password")
def change_password(req: PasswordChangeRequest, request: Request):
    """Change the caller's own password.

    Until this existed, rotation required shell access on the box (manage.py reset-password), while
    db.py and the README both told users to "change password via /admin UI" — a UI that was never
    built. That gap also stranded every legacy 100k-iteration hash, since the upgrade path was
    documented as happening "on next password change".

    Verifies the CURRENT password rather than trusting the session: a stolen token should not be
    enough to take ownership of an account. On success every other session is revoked, so if the
    reason for changing it was a leak, the leak is closed by the same action.
    """
    if req.current_password == req.new_password:
        raise HTTPException(status_code=400, detail="The new password must be different")
    conn = get_db()
    try:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?",
                           (request.state.user_id,)).fetchone()
        if not row or not verify_password(req.current_password, row["password_hash"]):
            # Deliberately the same shape of failure as a bad login, and rate-limited by the
            # per-user limiter the middleware already applies.
            raise HTTPException(status_code=403, detail="Current password is incorrect")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (hash_password(req.new_password), request.state.user_id))
        # Keep THIS session alive and drop the rest: the caller stays signed in where they are,
        # and anyone holding an older token for this account does not.
        auth_header = request.headers.get("Authorization", "")
        current = hash_token(auth_header[7:]) if auth_header.startswith("Bearer ") else ""
        cur = conn.execute("DELETE FROM auth_sessions WHERE user_id = ? AND token != ?",
                           (request.state.user_id, current))
        conn.commit()
        revoked = cur.rowcount
    finally:
        conn.close()
    deps.audit(request, "auth.password_change", f"other sessions revoked: {revoked}")
    return {"status": "ok", "other_sessions_revoked": revoked}


@app.post("/auth/logout-all")
def logout_all(request: Request):
    """Revoke every session for the caller ("log out everywhere") — e.g. after a suspected
    token leak. API keys are unaffected (revoke those via the admin panel / manage.py)."""
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (request.state.user_id,))
        conn.commit()
        return {"status": "ok", "revoked": cur.rowcount}
    finally:
        conn.close()


# ----------------- Session endpoints -----------------
@app.get("/sessions")
def list_sessions(request: Request):
    return {"sessions": chat.get_sessions(request.state.user_id)}


@app.post("/sessions")
def new_session(request: Request):
    s_id = chat.create_session("New Chat", request.state.user_id)
    return {"id": s_id, "title": "New Chat"}


@app.put("/sessions/{session_id}")
def update_session(session_id: str, req: SessionRenameRequest, request: Request):
    chat.require_owned_session(session_id, request.state.user_id)   # 403 on not-yours / missing
    chat.rename_session(session_id, req.title, request.state.user_id)
    return {"status": "ok"}


@app.delete("/sessions/{session_id}")
def remove_session(session_id: str, request: Request):
    chat.delete_session(session_id, request.state.user_id)
    return {"status": "ok"}


@app.get("/history/{session_id}")
def get_session_history(session_id: str, request: Request):
    session_id = chat.resolve_session(session_id, request.state.user_id)
    chat.require_owned_session(session_id, request.state.user_id)
    context = chat.get_recent_context(session_id, limit=100)
    return {"messages": context, "count": len(context)}


@app.get("/health")
def health_check() -> Dict[str, Any]:
    ok, detail = _llm_status()
    n_ctx = 4096
    model_name = "Qwen3.5 2B"
    try:
        p = urlsplit(LLM_URL)
        with urllib.request.urlopen(f"{p.scheme}://{p.netloc}/props", timeout=1.5) as r:
            props = json.loads(r.read().decode("utf-8"))
            dgs = props.get("default_generation_settings") or {}
            model_path = props.get("model_path") or dgs.get("model") or ""
            if model_path:
                model_name = os.path.basename(str(model_path)).removesuffix(".gguf")
            n_ctx = dgs.get("n_ctx") or props.get("n_ctx") or 4096
    except Exception:
        pass
    # demo_signup drives the LOGIN SCREEN, which renders before any token exists — so it has to
    # ride on this unauthenticated endpoint. It says only "this runtime offers demo sessions",
    # which is already implied by mode; a production/lab container reports false and its UI shows
    # no demo affordance at all.
    return {"status": "ok" if ok else "offline", "model": model_name, "detail": detail,
            "n_ctx": n_ctx, "mode": JARVIS_MODE, "demo_signup": DEMO_PUBLIC_SIGNUP,
            "demo_ttl_minutes": DEMO_TTL_MINUTES if DEMO_PUBLIC_SIGNUP else None}


def _system_stats() -> Dict[str, Any]:
    """Live host telemetry for the UI diagnostics panel. Dependency-free (/proc + os)."""
    stats: Dict[str, Any] = {"mode": JARVIS_MODE}
    try:
        load1 = os.getloadavg()[0]
        cpus = os.cpu_count() or 1
        stats["load1"] = round(load1, 2)
        stats["cpus"] = cpus
        stats["cpu_pct"] = min(100, round(load1 / cpus * 100))
    except Exception:
        pass
    try:
        meminfo: Dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                meminfo[k] = int(v.strip().split()[0])  # values are in kB
        total, avail = meminfo.get("MemTotal", 0), meminfo.get("MemAvailable", 0)
        if total:
            stats["mem_total_mb"] = round(total / 1024)
            stats["mem_used_mb"] = round((total - avail) / 1024)
            stats["mem_pct"] = round((total - avail) / total * 100)
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            stats["uptime_sec"] = int(float(f.read().split()[0]))
    except Exception:
        pass
    return stats


@app.get("/system")
def system_stats(request: Request) -> Dict[str, Any]:
    # Admin-only: host telemetry (load/mem/uptime) is infrastructure detail, not for every user.
    deps.require_admin(request)
    return _system_stats()


DEVICE_ACTIVE_WINDOW_S = 90   # an edge device is "active" if seen within this many seconds


def _ping_llm() -> bool:
    """True if the llama backend answers its /health quickly (so the admin board reflects reality)."""
    try:
        p = urlsplit(LLM_URL)
        with urllib.request.urlopen(f"{p.scheme}://{p.netloc}/health", timeout=1.5) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _llm_status() -> tuple:
    """(ok, detail): the LLM's REAL loaded model + context, read from llama-server's /props — so the
    board shows what's actually running, not a hardcoded name (and self-corrects if LLM_MODEL changes).
    Falls back to a /health ping for the up/down signal if /props isn't available."""
    try:
        p = urlsplit(LLM_URL)
        with urllib.request.urlopen(f"{p.scheme}://{p.netloc}/props", timeout=1.5) as r:
            props = json.loads(r.read().decode("utf-8"))
        dgs = props.get("default_generation_settings") or {}
        model_path = props.get("model_path") or dgs.get("model") or ""
        model = os.path.basename(str(model_path)).removesuffix(".gguf") or "model"
        n_ctx = dgs.get("n_ctx") or props.get("n_ctx")
        return True, (f"{model} · ctx {n_ctx}" if n_ctx else model)
    except Exception:
        return _ping_llm(), "fast brain"


def _embedding_detail(emb: Dict[str, Any]) -> str:
    """One-line detail for the Embeddings row: model · dim · N memories (best-effort)."""
    if not emb.get("available"):
        return "model not loaded"
    bits = []
    if emb.get("model"):
        bits.append(os.path.basename(emb["model"]))          # google/embeddinggemma-300m -> embeddinggemma-300m
    if emb.get("dim"):
        bits.append(f"dim {emb['dim']}")
    if emb.get("count") is not None:
        bits.append(f"{emb['count']} memories")
    if emb.get("runtime", "").startswith("onnx"):
        bits.append(emb["runtime"])          # highlight the torch-free runtime when active
    return " · ".join(bits) or "vector search ready"


def _service_status(household_id: int) -> list:
    """Status of each subsystem for the admin console: green (active) / red (inactive), with a
    one-line detail. Camera/edge liveness is inferred from device_heartbeats (recent = running)."""
    def s(name, ok, detail=""):
        return {"name": name, "status": "active" if ok else "inactive", "detail": detail}

    llm_ok, llm_detail = _llm_status()
    emb = memory.embedding_status()
    services = [
        s("Orchestrator (API)", True, "serving this request"),
        s("LLM (llama.cpp)", llm_ok, llm_detail),
        s("Embeddings / RAG", emb.get("available", False), _embedding_detail(emb)),
        s("Voice / TTS (Piper)", PIPER_BIN.exists() and PIPER_MODEL.exists(),
          PIPER_MODEL.name if PIPER_MODEL.exists() else "piper binary/voice missing"),
    ]
    # Only the household that owns the smart home sees its row — the HA URL is infrastructure
    # detail about someone's home network.
    if ha.configured() and household_id == deps.HA_HOUSEHOLD_ID:
        services.append(s("Home Assistant", ha.ping(),
                          f"{len(ha.HA_ALLOWED_ENTITIES)} entities allowlisted · {ha.HA_URL}"))

    # Camera agents (Pi / laptop): one row per device that has ever reported, active if its last
    # heartbeat/event is recent. This is the "is the model running on the hardware" indicator.
    conn = get_db()
    try:
        # Scoped: the camera roster is household infrastructure — device ids and liveness of
        # someone else's cameras are not this admin's business.
        rows = conn.execute(
            "SELECT device_id, last_seen, (julianday('now') - julianday(last_seen)) * 86400 AS age "
            "FROM device_heartbeats WHERE household_id = ? ORDER BY device_id",
            (household_id,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        services.append(s("Camera agent", False, "no edge device has reported yet"))
    else:
        for r in rows:
            age = r["age"]
            ok = age is not None and age < DEVICE_ACTIVE_WINDOW_S
            if age is None:
                detail = "last seen: unknown"
            elif age < 90:
                detail = f"last seen {int(age)}s ago"
            elif age < 3600:
                detail = f"last seen {int(age // 60)}m ago"
            else:
                detail = f"last seen {int(age // 3600)}h ago"
            services.append(s(f"Camera · {r['device_id']}", ok, detail))
    return services


@app.get("/admin/services")
def admin_services(request: Request) -> Dict[str, Any]:
    """Per-subsystem health for the admin console (active/inactive + detail), plus the app version and
    an at-a-glance operational summary (how many subsystems are up)."""
    deps.require_admin(request)
    services = _service_status(deps.household(request))
    up = sum(1 for x in services if x["status"] == "active")
    return {
        "services": services,
        "version": APP_VERSION,
        "summary": {"up": up, "total": len(services), "operational": up == len(services)},
    }


@app.get("/presence")
def presence(request: Request):
    """Who the cameras have recognized recently (household context). Any authenticated user."""
    return {"present": memory.get_present_people(deps.household(request))}


@app.get("/arrivals")
def arrivals(request: Request, since_id: int = 0):
    """Recent 'someone arrived' events (last 2 min) for the UI to greet. Poll with since_id to get
    only new ones."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, data, created_at FROM vision_events WHERE household_id = ? "
            "AND type='presence_arrival' AND id > ? AND created_at > datetime('now', '-120 seconds') "
            "ORDER BY id", (deps.household(request), since_id)).fetchall()
        out = []
        for r in rows:
            try:
                nm = (json.loads(r["data"]) or {}).get("name")
            except (ValueError, TypeError):
                nm = None
            if nm:
                out.append({"id": r["id"], "name": nm, "created_at": r["created_at"]})
        return {"arrivals": out}
    finally:
        conn.close()


@app.get("/reminders")
def list_reminders(request: Request):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, text, due_at, status, created_at FROM reminders "
            "WHERE user_id = ? AND status = 'pending' ORDER BY due_at", (request.state.user_id,)).fetchall()
        return {"reminders": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/reminders/due")
def due_reminders(request: Request):
    """Pending reminders whose time has arrived — the client announces them, then acks. ('due' is just
    a query: due_at <= local now, so no background scheduler is needed.)"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, text, due_at FROM reminders WHERE user_id = ? AND status = 'pending' "
            "AND due_at <= datetime('now', 'localtime') ORDER BY due_at", (request.state.user_id,)).fetchall()
        return {"due": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/reminders/{rid}/ack")
def ack_reminder(rid: int, request: Request):
    conn = get_db()
    try:
        cur = conn.execute("UPDATE reminders SET status = 'done' WHERE id = ? AND user_id = ?",
                           (rid, request.state.user_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="No such reminder")
        return {"status": "ok"}
    finally:
        conn.close()


@app.delete("/reminders/{rid}")
def cancel_reminder(rid: int, request: Request):
    conn = get_db()
    try:
        cur = conn.execute("UPDATE reminders SET status = 'cancelled' WHERE id = ? AND user_id = ? "
                           "AND status = 'pending'", (rid, request.state.user_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="No such reminder")
        return {"status": "ok"}
    finally:
        conn.close()


VISION_EVENTS_CAP = 5000   # keep only the most recent N events (disk-bound, never pruned otherwise)


@app.post("/events")
def ingest_event(req: EventRequest, request: Request):
    """Ingest a high-level event from an edge device (e.g. the Pi camera agent).

    Provenance is bound to the API key (F1): a device-scoped key records events under ITS OWN
    device_id regardless of the body, so a key can't spoof events as another device. Admins may
    post as any device_id (for testing). Other principals (a plain web user) may not post events
    — this matters because face/presence events will drive authorization later.
    """
    is_admin = getattr(request.state, "is_admin", False)
    dev = getattr(request.state, "device_id", None)
    if dev:
        device_id = dev                  # trust the key, not the client-supplied device_id
    elif is_admin:
        device_id = req.device_id        # admins may post synthetic/test events as any device
    else:
        raise HTTPException(status_code=403, detail="Only device-scoped API keys (or admins) may post events")
    household_id = deps.household(request)
    conn = get_db()
    try:
        # Heartbeats are liveness pings, not events — keep only the latest per device (don't flood
        # vision_events) so the admin console can show the camera agent as active even in a quiet room.
        if req.type == "heartbeat":
            # Conflict target is (household_id, device_id), not device_id: the id is unique only
            # WITHIN a household, so a second household running its own `laptop-cam` gets its own
            # row instead of taking over this one.
            conn.execute(
                "INSERT INTO device_heartbeats (device_id, household_id, last_seen) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(household_id, device_id) DO UPDATE SET last_seen = CURRENT_TIMESTAMP",
                (device_id, household_id),
            )
            # Opportunistically prune long-dead devices so the table can't grow unbounded (an admin
            # may post as any device_id) and the console doesn't list stale cameras forever.
            conn.execute("DELETE FROM device_heartbeats WHERE last_seen < datetime('now', '-30 days')")
            conn.commit()
            return {"status": "ok"}
        cur = conn.execute(
            "INSERT INTO vision_events (household_id, device_id, type, data, user_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (household_id, device_id, req.type, json.dumps(req.data or {}), request.state.user_id),
        )
        # Arrival detection: a recognized person not seen recently → emit a one-off presence_arrival
        # the UI announces ("welcome home"). Tracked in-memory so it's cheap on the events hot path.
        if req.type == "face_seen":
            nm = (req.data or {}).get("name")
            if nm and nm != "unknown":
                now = time.time()
                # Keyed by (household, name): two households may each know an "Alice", and a bare
                # name key would let one household's sighting suppress the other's arrival event.
                seen_key = (household_id, nm)
                if now - _present_since.get(seen_key, 0.0) > ARRIVAL_GAP_S:
                    conn.execute(
                        "INSERT INTO vision_events (household_id, device_id, type, data, user_id) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (household_id, device_id, "presence_arrival", json.dumps({"name": nm}),
                         request.state.user_id))
                _present_since[seen_key] = now
        # Any real event also proves the device is alive — fold it into liveness too.
        conn.execute(
            "INSERT INTO device_heartbeats (device_id, household_id, last_seen) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(household_id, device_id) DO UPDATE SET last_seen = CURRENT_TIMESTAMP",
            (device_id, household_id),
        )
        conn.execute("DELETE FROM vision_events WHERE id <= ?", (cur.lastrowid - VISION_EVENTS_CAP,))
        conn.commit()
        return {"status": "ok", "id": cur.lastrowid}
    finally:
        conn.close()


# ----------------- Knowledge -----------------
@app.get("/knowledge")
def list_knowledge(request: Request):
    """This user's own remembered facts, plus how much of the prompt they currently occupy.

    The budget is reported because the profile block is injected into EVERY prompt and truncated
    head-first at KNOWLEDGE_TOKEN_CAP — so past the cap, facts stop reaching the model with no
    other outward sign. On a 4096-token context that ceiling is reachable, and a user who can't
    see it would just be quietly wrong about what Jarvis knows.
    """
    facts = memory.get_user_knowledge_list(request.state.user_id)
    block = memory.get_user_knowledge(request.state.user_id)
    return {"facts": facts, "count": len(facts),
            "prompt_chars": len(block),
            "prompt_char_budget": KNOWLEDGE_TOKEN_CAP * 4}   # truncate_to_tokens' own conversion


@app.post("/knowledge")
def add_knowledge(req: KnowledgeFactRequest, request: Request):
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty content")
    category = req.category.lower().strip()
    if category not in VALID_FACT_CATEGORIES:
        category = "other"
    # Request path: skip inline embedding (word-overlap dedup only) to avoid burning
    # a worker on the 300M model and contending with the LLM.
    fact_id = memory.store_fact(request.state.user_id, category, content, source="manual", use_embeddings=False)
    return {"id": fact_id, "status": "ok"}


@app.put("/knowledge/{fact_id}")
def edit_knowledge(fact_id: int, req: KnowledgeFactRequest, request: Request):
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty content")
    if not memory.update_fact(fact_id, request.state.user_id, content,
                              req.category.lower().strip() if req.category else None):
        raise HTTPException(status_code=404, detail="No such fact")
    return {"status": "ok"}


@app.delete("/knowledge/{fact_id}")
def remove_knowledge(fact_id: int, request: Request):
    if not memory.delete_fact(fact_id, request.state.user_id):
        raise HTTPException(status_code=404, detail="No such fact")
    return {"status": "ok"}


@app.get("/admin/knowledge/global")
def list_global_knowledge(request: Request):
    """Household/global facts (shared by all users). Admin-only — these go into everyone's prompt."""
    deps.require_admin(request)
    facts = memory.get_global_knowledge_list(deps.household(request))
    return {"facts": facts, "count": len(facts)}


@app.post("/admin/knowledge/global")
def add_global_knowledge(req: KnowledgeFactRequest, request: Request):
    """Add a household fact (admin-only). An external tool (e.g. a loader script) can call this too."""
    deps.require_admin(request)
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty content")
    category = (req.category or "other").lower().strip()
    if category not in VALID_FACT_CATEGORIES:
        category = "other"
    fact_id = memory.store_global_fact(deps.household(request), category, content, source="manual")
    deps.audit(request, "knowledge.global.add", f"[{category}] {content[:120]}")
    return {"id": fact_id, "status": "ok"}


@app.put("/admin/knowledge/global/{fact_id}")
def edit_global_knowledge(fact_id: int, req: KnowledgeFactRequest, request: Request):
    deps.require_admin(request)
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty content")
    if not memory.update_global_fact(deps.household(request), fact_id, content,
                                     req.category.lower().strip() if req.category else None):
        raise HTTPException(status_code=404, detail="No such fact")
    return {"status": "ok"}


@app.delete("/admin/knowledge/global/{fact_id}")
def remove_global_knowledge(fact_id: int, request: Request):
    deps.require_admin(request)
    if not memory.delete_global_fact(deps.household(request), fact_id):
        raise HTTPException(status_code=404, detail="No such fact")
    deps.audit(request, "knowledge.global.delete", f"id={fact_id}")
    return {"status": "ok"}


@app.post("/admin/knowledge/global/chat")
def global_knowledge_chat(req: GlobalChatRequest, request: Request):
    """Admin 'global chat': each non-empty line of the message becomes a household fact and is stored
    immediately. Deterministic (no LLM) so it's instant and never mis-files what you said."""
    deps.require_admin(request)
    lines = [ln.strip() for ln in req.text.splitlines() if ln.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="Nothing to save")
    saved = [{"id": memory.store_global_fact(deps.household(request), "household", ln, source="global-chat"), "content": ln}
             for ln in lines]
    deps.audit(request, "knowledge.global.chat", f"+{len(saved)} fact(s)")
    return {"reply": f"Saved {len(saved)} fact{'s' if len(saved) != 1 else ''} to household knowledge.",
            "saved": saved, "count": len(memory.get_global_knowledge_list(deps.household(request)))}


@app.post("/knowledge/extract-now")
def force_extraction(request: Request):
    deps.require_admin(request)
    unprocessed = memory.get_unprocessed_messages(batch_size=50)
    if not unprocessed:
        return {"status": "ok", "processed": 0, "message": "No unprocessed messages"}
    memory.extract_facts_batch(unprocessed)
    return {"status": "ok", "processed": len(unprocessed)}


# ----------------- Routers -----------------
# Registered here rather than at the top of the file so their paths are matched in roughly the
# order they used to be defined in. Nothing under routes/ imports main, so the graph stays a tree.
app.include_router(routes_admin.router)
app.include_router(routes_chat.router)
app.include_router(routes_devices.router)
app.include_router(routes_faces.router)
app.include_router(routes_mcp.router)
app.include_router(routes_voice.router)


# ----------------- Static UI -----------------
@app.get("/")
def serve_ui():
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=404)
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/admin")
def serve_admin():
    # The admin console is now a view inside the React SPA; serve the same bundle and
    # let the client render it for /admin (admin-gated client-side and on every endpoint).
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=404)
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/voice")
def serve_voice():
    # Live voice kiosk — another view inside the same SPA (see /admin). Serving the shell is
    # unauthenticated exactly like "/" is; the page itself renders the login screen until a token
    # exists, and every endpoint it then calls is authenticated on its own.
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=404)
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/favicon.{ext}")
def serve_favicon(ext: str):
    if ext not in ("png", "ico", "svg"):
        raise HTTPException(status_code=404)
    media_types = {"png": "image/png", "ico": "image/x-icon", "svg": "image/svg+xml"}
    favicon = REACT_DIST_DIR / f"favicon.{ext}"
    if not favicon.exists():
        raise HTTPException(status_code=404)
    return FileResponse(favicon, media_type=media_types[ext])


@app.get("/ca.crt")
def serve_ca_cert():
    """Public: this deployment's local-CA certificate, so any device/browser can trust the server.
    Only the PUBLIC cert is served — the CA private key never leaves the box. 404 if TLS isn't set up.
    (Per-deployment: each install generates its own CA via src/scripts/setup_tls.sh.)"""
    ca = BASE_DIR / "tls" / "ca.crt"
    if not ca.exists():
        raise HTTPException(status_code=404, detail="No CA cert (TLS not set up on this server)")
    return FileResponse(ca, media_type="application/x-pem-file", filename="ca.crt")


if REACT_DIST_DIR.exists():
    assets_dir = REACT_DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
# Failsafe copy of the browser-side speech-to-text model, laid out as transformers.js expects
# (<repo>/models/stt/<org>/<model>/...). Absent unless download_models.sh fetched it — the mount is
# skipped rather than erroring, and the worker simply has no fallback if the official source fails.
if STT_MODELS_DIR.exists():
    app.mount("/stt-models", StaticFiles(directory=str(STT_MODELS_DIR)), name="stt-models")
# Face models, same posture as the STT bundle: public, SHA-256-pinned upstream weights — no secret
# — and the Web Worker that fetches them cannot attach a Bearer token.
if FACE_MODELS_DIR.exists():
    app.mount("/face-models", StaticFiles(directory=str(FACE_MODELS_DIR)), name="face-models")
# Wake-word models, same posture as the face and STT bundles: public, SHA-256-pinned upstream
# weights, no secret in them, and the Web Worker that fetches them cannot attach a Bearer token.
if WAKE_MODELS_DIR.exists():
    app.mount("/wake-models", StaticFiles(directory=str(WAKE_MODELS_DIR)), name="wake-models")
# ONNX Runtime WASM backend, vendored into the SPA build by frontend/scripts/copy-ort.mjs.
# Served from our own origin so the runtime never reaches for a CDN (see whisper-worker.js).
_ORT_DIR = REACT_DIST_DIR / "ort"
if _ORT_DIR.exists():
    app.mount("/ort", StaticFiles(directory=str(_ORT_DIR)), name="ort")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CONFIG["orchestrator"]["host"], port=CONFIG["orchestrator"]["port"])
