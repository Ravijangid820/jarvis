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
import asyncio
import json
import os
import random
import sqlite3
import secrets
import time
import urllib.request
from urllib.parse import urlsplit
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import chat
import memory
from auth import hash_password, hash_token, verify_password
from budget import is_default_session
from config import (ADMIN_MAX_INPUT, ALLOWED_ORIGINS, COMPLETION_RESERVE_DEFAULT,
                    CONFIG, INDEX_HTML, LLM_URL, PIPER_BIN, PIPER_MODEL, RATE_LIMIT_RPM,
                    REACT_DIST_DIR, REGULAR_MAX_INPUT, STATIC_DIR, VALID_FACT_CATEGORIES, logger)
from db import get_db, init_db
from llm import llm_content, request_llm, request_llm_stream, synthesize_tts


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    memory.init_embeddings()   # load the model now (from cache), not at import time
    memory.start_embedding_worker()
    memory.start_memory_worker()
    logger.info("Jarvis Orchestrator started with Auth + Memory Core")
    yield
    memory.embed_queue.put(None)  # signal the embedding worker to drain and exit


app = FastAPI(title="Jarvis Orchestrator", docs_url=None, redoc_url=None, lifespan=lifespan)

# CORS: the SPA + admin panel are served same-origin; default to no cross-origin (most secure).
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# --- Rate limiting (in-process) ---------------------------------------------
_rate_store: Dict[str, List[float]] = defaultdict(list)
# Login brute-force guard, keyed on USERNAME (not client IP): behind the Tailscale subnet
# router every request shares one source IP, so an IP bucket would be one global bucket that
# any client could exhaust to lock everyone out. Per-username throttling targets the actual
# brute-force surface and can't cause a cross-account lockout.
_login_store: Dict[str, List[float]] = defaultdict(list)
LOGIN_MAX_PER_MIN = 8
_last_sweep = [0.0]


def _sweep_rate_stores(now: float) -> None:
    """Drop fully-expired buckets so the dicts don't grow unbounded with distinct keys."""
    for store in (_rate_store, _login_store):
        for k in [k for k, v in store.items() if not any(t > now - 60.0 for t in v)]:
            del store[k]


def _allow(store: Dict[str, List[float]], key: str, limit: int) -> bool:
    now = time.time()
    if now - _last_sweep[0] > 300.0:
        _sweep_rate_stores(now)
        _last_sweep[0] = now
    bucket = [t for t in store[key] if t > now - 60.0]
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
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; media-src 'self' data: blob:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


def _apply_security_headers(response: Response, cache: str = "no-store") -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = cache
    return response


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    if (request.method == "OPTIONS" or path in ["/health", "/", "/admin", "/auth/login", "/favicon.svg"]
            or path.startswith("/static/") or path.startswith("/assets/")):
        resp = await call_next(request)
        # Vite emits content-hashed bundles under /assets — safe to cache forever.
        if path.startswith("/assets/"):
            return _apply_security_headers(resp, "public, max-age=31536000, immutable")
        return _apply_security_headers(resp)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return Response(content=json.dumps({"error": "Auth required"}), status_code=401)
    token = auth_header[7:]

    conn = get_db()
    is_authenticated = False
    try:
        # 1. Web-login session token (stored hashed at rest; look up by hash).
        row = conn.execute(
            "SELECT user_id, u.role FROM auth_sessions s JOIN users u ON s.user_id = u.id "
            "WHERE s.token = ? AND s.expires_at > datetime('now')", (hash_token(token),)).fetchone()
        if row:
            request.state.user_id = row["user_id"]
            request.state.is_admin = (row["role"] == "admin")
            request.state.device_id = None     # web session → not a device-scoped principal
            is_authenticated = True
        else:
            # 2. Per-user API key (machine integrations, e.g. the voice listener / device agents).
            #    Stored hashed at rest, like session tokens — look up by hash. `device_id`, if set,
            #    binds the key to one device (enforced by /devices/* and /events).
            row = conn.execute(
                "SELECT user_id, u.role, k.device_id FROM api_keys k JOIN users u ON k.user_id = u.id "
                "WHERE k.key_string = ?", (hash_token(token),)).fetchone()
            if row:
                request.state.user_id = row["user_id"]
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
        return Response(content=json.dumps({"error": "Invalid or expired token"}), status_code=403)

    # Rate-limit ALL authenticated callers (admins included), keyed on user id.
    if not check_rate_limit(f"user:{request.state.user_id}"):
        return Response(content=json.dumps({"error": "Rate limit exceeded"}), status_code=429)

    return _apply_security_headers(await call_next(request))


# ----------------- Models -----------------
class QueryRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=ADMIN_MAX_INPUT)
    session_id: str = Field(default="default")
    temperature: Optional[float] = None
    top_k: Optional[int] = None
    top_p: Optional[float] = None
    min_p: Optional[float] = None
    repeat_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    n_predict: Optional[int] = Field(default=None, ge=1, le=8192)
    seed: Optional[int] = None
    system_prompt: Optional[str] = Field(default=None, max_length=2000)
    voice_feedback: bool = False


class SessionRenameRequest(BaseModel):
    title: str


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)
    role: Literal["user", "admin"] = "user"


class RoleUpdateRequest(BaseModel):
    role: Literal["user", "admin"]


class CreateKeyRequest(BaseModel):
    user_id: int
    description: str
    # Optional: bind the key to one device (e.g. "laptop-cam"). A bound key may ONLY post events as
    # that device (F1). Edge/camera agents need this — a plain unbound non-admin key can't post events.
    device_id: Optional[str] = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")


class KnowledgeFactRequest(BaseModel):
    content: str
    category: str = "other"


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


class VolumeRequest(BaseModel):
    action: str = Field(..., max_length=16)        # set | step | mute | unmute
    value: Optional[int] = Field(default=None, ge=-100, le=100)
    device: str = Field(default="laptop", max_length=64)


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=600)


class FaceEnrollRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    embedding: List[float] = Field(..., min_length=8, max_length=2048)   # L2-normalized vector
    source: Optional[str] = Field(default=None, max_length=64)           # device_id / "cli"
    replace: bool = False          # if true, clear this person's existing embeddings first


class FaceUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    user_id: Optional[int] = None          # link person → account (null clears the link)


# ----------------- Auth endpoints -----------------
@app.post("/auth/login")
def login(req: LoginRequest, request: Request):
    # Throttle per-username (not per-IP): login bypasses the per-user limiter, so without this
    # it's an unbounded password-guessing oracle. Keying on username also avoids the global
    # lockout that an IP bucket would cause behind the shared subnet-router source IP.
    if not check_login_rate(req.username):
        raise HTTPException(status_code=429, detail="Too many attempts; try again shortly")
    conn = get_db()
    try:
        row = conn.execute("SELECT id, password_hash, role FROM users WHERE username = ?", (req.username,)).fetchone()
        if not row or not verify_password(req.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password")
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


@app.post("/auth/logout")
def logout(request: Request):
    """Revoke the caller's current session token server-side (real logout)."""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if token:
        conn = get_db()
        try:
            conn.execute("DELETE FROM auth_sessions WHERE token = ?", (hash_token(token),))
            conn.commit()
        finally:
            conn.close()
    return {"status": "ok"}


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
def health_check() -> Dict[str, str]:
    return {"status": "ok", "model": "qwen3.5-2b"}


def _system_stats() -> Dict[str, Any]:
    """Live host telemetry for the UI diagnostics panel. Dependency-free (/proc + os)."""
    stats: Dict[str, Any] = {}
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
    _require_admin(request)
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


def _service_status() -> list:
    """Status of each subsystem for the admin console: green (active) / red (inactive), with a
    one-line detail. Camera/edge liveness is inferred from device_heartbeats (recent = running)."""
    def s(name, ok, detail=""):
        return {"name": name, "status": "active" if ok else "inactive", "detail": detail}

    services = [
        s("Orchestrator (API)", True, "serving this request"),
        s("LLM (llama.cpp)", _ping_llm(), "qwen3.5-2b (fast brain)"),
        s("Embeddings / RAG", memory.vectors_available(),
          "vector search ready" if memory.vectors_available() else "model not loaded"),
        s("Voice / TTS (Piper)", PIPER_BIN.exists() and PIPER_MODEL.exists(),
          PIPER_MODEL.name if PIPER_MODEL.exists() else "piper binary/voice missing"),
    ]

    # Camera agents (Pi / laptop): one row per device that has ever reported, active if its last
    # heartbeat/event is recent. This is the "is the model running on the hardware" indicator.
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT device_id, last_seen, (julianday('now') - julianday(last_seen)) * 86400 AS age "
            "FROM device_heartbeats ORDER BY device_id"
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
    """Per-subsystem health for the admin console (active/inactive + detail)."""
    _require_admin(request)
    return {"services": _service_status()}


# ----------------- Voice / TTS -----------------
@app.post("/tts")
def tts(req: TTSRequest, request: Request):
    """Synthesize speech (Piper) for arbitrary text → base64 WAV. The web UI uses this to speak
    the greeting; the voice bridge uses it for spoken replies."""
    audio = synthesize_tts(req.text.strip())
    if not audio:
        raise HTTPException(status_code=503, detail="TTS unavailable")
    return {"audio": audio}


def _jarvis_ack() -> str:
    """A short, time-aware JARVIS acknowledgement — the spoken reply to just the wake word."""
    h = datetime.now().hour
    part = "morning" if h < 12 else "afternoon" if h < 18 else "evening"
    return random.choice([
        "Yes, sir?", "At your service, sir.", "How can I help, sir?",
        f"Good {part}, sir.", "Standing by, sir.", "I'm here, sir.",
    ])


@app.get("/greeting")
def greeting(request: Request):
    """A JARVIS greeting (text + spoken audio), no LLM. Used by the voice bridge when it hears
    just the wake word ("Jarvis" → "Yes, sir?")."""
    text = _jarvis_ack()
    return {"text": text, "audio": synthesize_tts(text)}


# ----------------- Faces (enrollment + recognition data) -----------------
@app.post("/faces/enroll")
def enroll_face(req: FaceEnrollRequest, request: Request):
    """Register a face embedding (computed on the edge/laptop) for a person. Admin-only — faces can
    drive authorization, so enrollment is privileged. Adds to the person's embeddings (creating the
    person if new); pass replace=true to start their set over."""
    _require_admin(request)
    name = req.name.strip()
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM persons WHERE name = ?", (name,)).fetchone()
        person_id = row["id"] if row else conn.execute(
            "INSERT INTO persons (name) VALUES (?)", (name,)).lastrowid
        if req.replace:
            conn.execute("DELETE FROM face_embeddings WHERE person_id = ?", (person_id,))
        cur = conn.execute(
            "INSERT INTO face_embeddings (person_id, embedding, source) VALUES (?, ?, ?)",
            (person_id, json.dumps(req.embedding), (req.source or "").strip() or None))
        conn.commit()
        return {"status": "ok", "person_id": person_id, "embedding_id": cur.lastrowid}
    finally:
        conn.close()


@app.get("/faces/enrolled")
def enrolled_faces(request: Request):
    """The enrolled set for the edge agent: {name: [embedding, ...]} (a list per person — recognition
    matches against the best of all)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT p.name AS name, e.embedding AS embedding "
            "FROM face_embeddings e JOIN persons p ON e.person_id = p.id").fetchall()
        out: Dict[str, Any] = {}
        for r in rows:
            out.setdefault(r["name"], []).append(json.loads(r["embedding"]))
        return {"enrolled": out}
    finally:
        conn.close()


def _can_control_devices(request: Request) -> bool:
    """Authorization for device actions (lights/volume): admins always; others need the
    per-user can_control_devices flag. Enforced HERE, in code — never by the LLM."""
    if getattr(request.state, "is_admin", False):
        return True
    conn = get_db()
    try:
        row = conn.execute("SELECT can_control_devices FROM users WHERE id = ?", (request.state.user_id,)).fetchone()
        return bool(row and row["can_control_devices"])
    finally:
        conn.close()


@app.post("/devices/volume")
def queue_volume(req: VolumeRequest, request: Request):
    """Enqueue a volume command for a device agent (e.g. the Windows volume agent).

    Authorization is enforced here against the caller's identity/permissions; the command is
    a tiny validated vocabulary (no shell, no free text). The device agent pulls + executes it.
    Later, the LLM `set_volume` tool will call this same enqueue path.
    """
    if not _can_control_devices(request):
        raise HTTPException(status_code=403, detail="Not authorized to control devices")
    action = req.action.lower()
    params: Dict[str, Any] = {}
    if action == "set":
        if req.value is None or not (0 <= req.value <= 100):
            raise HTTPException(status_code=400, detail="set requires value 0–100")
        params = {"value": req.value}
    elif action == "step":
        if req.value is None:
            raise HTTPException(status_code=400, detail="step requires value (-100…100)")
        params = {"value": req.value}
    elif action not in ("mute", "unmute"):
        raise HTTPException(status_code=400, detail="action must be set|step|mute|unmute")
    conn = get_db()
    try:
        cur = conn.execute("INSERT INTO device_commands (device_id, action, params) VALUES (?, ?, ?)",
                           (req.device, action, json.dumps(params)))
        # Retention: drop delivered commands older than a day so the queue doesn't grow forever.
        conn.execute("DELETE FROM device_commands WHERE status='delivered' AND delivered_at < datetime('now','-1 day')")
        conn.commit()
        return {"status": "ok", "id": cur.lastrowid}
    finally:
        conn.close()


# Cap concurrent long-polls so a flood of GET /devices/commands can't pile up unbounded.
_poll_sem = asyncio.Semaphore(16)


def _claim_commands(device: str) -> List[Dict[str, Any]]:
    """Atomically claim (mark delivered + return) pending commands for one device. A single
    UPDATE…RETURNING — not SELECT-then-UPDATE — so two concurrent pollers can't double-deliver
    the same command (the second writer finds nothing still 'pending')."""
    conn = get_db()
    try:
        rows = conn.execute(
            "UPDATE device_commands SET status='delivered', delivered_at=datetime('now') "
            "WHERE id IN (SELECT id FROM device_commands WHERE device_id = ? AND status='pending' "
            "ORDER BY id LIMIT 50) RETURNING id, action, params",
            (device,)).fetchall()
        conn.commit()
        return [{"id": r["id"], "action": r["action"], "params": json.loads(r["params"] or "{}")} for r in rows]
    finally:
        conn.close()


@app.get("/devices/commands")
async def pull_device_commands(request: Request, device: str, wait: int = 20):
    """Device agents PULL their pending commands here (outbound-only; no inbound port on the
    device). Long-polls up to `wait` seconds, returning as soon as commands exist.

    `async` + `asyncio.sleep` so a waiting poll holds no worker thread (a sync handler would
    exhaust the thread pool under many concurrent polls). The key must be bound to `device`
    (or be an admin): a key for one device can't drain another device's queue (F1)."""
    dev = getattr(request.state, "device_id", None)
    if not getattr(request.state, "is_admin", False) and dev != device:
        raise HTTPException(status_code=403, detail="This key is not bound to that device")
    wait = max(0, min(wait, 30))
    deadline = time.time() + wait
    async with _poll_sem:
        while True:
            cmds = await run_in_threadpool(_claim_commands, device)
            if cmds:
                return {"commands": cmds}
            if time.time() >= deadline:
                return {"commands": []}
            await asyncio.sleep(0.5)


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
    conn = get_db()
    try:
        # Heartbeats are liveness pings, not events — keep only the latest per device (don't flood
        # vision_events) so the admin console can show the camera agent as active even in a quiet room.
        if req.type == "heartbeat":
            conn.execute(
                "INSERT INTO device_heartbeats (device_id, last_seen) VALUES (?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(device_id) DO UPDATE SET last_seen = CURRENT_TIMESTAMP",
                (device_id,),
            )
            # Opportunistically prune long-dead devices so the table can't grow unbounded (an admin
            # may post as any device_id) and the console doesn't list stale cameras forever.
            conn.execute("DELETE FROM device_heartbeats WHERE last_seen < datetime('now', '-30 days')")
            conn.commit()
            return {"status": "ok"}
        cur = conn.execute(
            "INSERT INTO vision_events (device_id, type, data, user_id) VALUES (?, ?, ?, ?)",
            (device_id, req.type, json.dumps(req.data or {}), request.state.user_id),
        )
        # Any real event also proves the device is alive — fold it into liveness too.
        conn.execute(
            "INSERT INTO device_heartbeats (device_id, last_seen) VALUES (?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(device_id) DO UPDATE SET last_seen = CURRENT_TIMESTAMP",
            (device_id,),
        )
        conn.execute("DELETE FROM vision_events WHERE id <= ?", (cur.lastrowid - VISION_EVENTS_CAP,))
        conn.commit()
        return {"status": "ok", "id": cur.lastrowid}
    finally:
        conn.close()


# ----------------- Chat -----------------
def _validate_chat(request: "QueryRequest", raw_request: Request):
    """Shared front-matter for /inbox and /chat/stream: returns (user_id, session_id, user_text)."""
    memory.update_activity()
    user_text = request.text.strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Empty input")
    is_admin = getattr(raw_request.state, "is_admin", False)
    if not is_admin and len(user_text) > REGULAR_MAX_INPUT:
        raise HTTPException(status_code=400, detail="Input too long (max 500 chars)")
    user_id = raw_request.state.user_id
    session_id = chat.resolve_session(request.session_id, user_id)
    chat.require_owned_session(session_id, user_id)
    return user_id, session_id, user_text


def _maybe_title(needs_title: bool, session_id: str, user_id: int, user_text: str):
    if not needs_title:
        return None
    try:
        resp = request_llm([{"role": "system", "content": "Reply with a very short title (1-4 words). No quotes."},
                            {"role": "user", "content": user_text}], temperature=0.3, n_predict=10)
        title = llm_content(resp).replace('"', "").replace(".", "").strip()
        if title:
            chat.rename_session(session_id, title, user_id)
            return title
    except Exception as e:
        logger.warning("Title generation failed: %s", e)
    return None


@app.post("/inbox")
def process_input(request: QueryRequest, raw_request: Request):
    user_id, session_id, user_text = _validate_chat(request, raw_request)
    existing = chat.get_recent_context(session_id)
    needs_title = (len(existing) == 0) and not is_default_session(session_id)
    completion_reserve = request.n_predict if (request.n_predict and request.n_predict > 0) else COMPLETION_RESERVE_DEFAULT
    messages = chat.build_messages(session_id, user_id, user_text, request.system_prompt, completion_reserve=completion_reserve)
    max_tokens = chat.clamp_completion_for(messages, request.n_predict)

    t0 = time.time()
    with memory.Inflight():
        llm_resp = request_llm(messages, temperature=request.temperature, top_k=request.top_k, top_p=request.top_p,
                               min_p=request.min_p, repeat_penalty=request.repeat_penalty, presence_penalty=request.presence_penalty,
                               frequency_penalty=request.frequency_penalty, n_predict=max_tokens, seed=request.seed)
    t1 = time.time()

    answer = llm_content(llm_resp).strip()
    comp_tokens = llm_resp.get("usage", {}).get("completion_tokens", 0)
    speed_str = ""
    timings = llm_resp.get("timings", {})
    if "predicted_per_second" in timings:
        speed_str = f"{timings['predicted_per_second']:.1f} tok/s"
    elif comp_tokens > 0 and (t1 - t0) > 0:
        speed_str = f"{(comp_tokens / (t1 - t0)):.1f} tok/s (wall)"

    audio_b64 = synthesize_tts(answer) if request.voice_feedback else None
    chat.store_message(session_id, "user", user_text)
    chat.store_message(session_id, "jarvis", answer)
    new_title = _maybe_title(needs_title, session_id, user_id, user_text)
    return {"response": answer, "speed": speed_str, "new_title": new_title, "audio": audio_b64}


@app.post("/chat/stream")
def chat_stream(request: QueryRequest, raw_request: Request):
    user_id, session_id, user_text = _validate_chat(request, raw_request)
    existing = chat.get_recent_context(session_id)
    needs_title = (len(existing) == 0) and not is_default_session(session_id)
    completion_reserve = request.n_predict if (request.n_predict and request.n_predict > 0) else COMPLETION_RESERVE_DEFAULT
    messages = chat.build_messages(session_id, user_id, user_text, request.system_prompt, completion_reserve=completion_reserve)
    max_tokens = chat.clamp_completion_for(messages, request.n_predict)

    def event_generator():
        full_answer = []
        error_occurred = False
        # In-flight for the whole generation so the fact-extraction worker won't contend.
        with memory.Inflight():
            try:
                for chunk in request_llm_stream(messages, temperature=request.temperature, top_k=request.top_k,
                                                top_p=request.top_p, min_p=request.min_p, repeat_penalty=request.repeat_penalty,
                                                presence_penalty=request.presence_penalty, frequency_penalty=request.frequency_penalty,
                                                n_predict=max_tokens, seed=request.seed):
                    full_answer.append(chunk)
                    yield f"data: {json.dumps({'content': chunk})}\n\n"
            except Exception as e:
                error_occurred = True
                logger.error("Error generating stream: %s", e)
                yield f"data: {json.dumps({'error': 'AI backend error'})}\n\n"

            answer_text = "".join(full_answer)
            # Persist the user turn even on failure; store the assistant turn only if real.
            chat.store_message(session_id, "user", user_text)
            if answer_text:
                chat.store_message(session_id, "jarvis", answer_text)

            if not answer_text:
                yield f"data: {json.dumps({'done': True, 'error': error_occurred})}\n\n"
                return

            new_title = _maybe_title(needs_title, session_id, user_id, user_text)
            audio_b64 = synthesize_tts(answer_text) if request.voice_feedback else None
            done_payload: Dict[str, Any] = {"done": True}
            if new_title:
                done_payload["new_title"] = new_title
            if audio_b64:
                done_payload["audio"] = audio_b64
            yield f"data: {json.dumps(done_payload)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ----------------- Admin -----------------
def _require_admin(request: Request):
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only")


@app.post("/admin/users")
def admin_create_user(req: CreateUserRequest, request: Request):
    _require_admin(request)
    conn = get_db()
    try:
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                     (req.username, hash_password(req.password), req.role))
        conn.commit()
        return {"status": "ok"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Username exists")
    finally:
        conn.close()


@app.get("/admin/users")
def admin_list_users(request: Request):
    _require_admin(request)
    conn = get_db()
    try:
        users = conn.execute("""
            SELECT u.id, u.username, u.role, u.created_at,
                   COUNT(DISTINCT c.id) as total_chats,
                   COUNT(m.id) as total_messages
            FROM users u
            LEFT JOIN chat_sessions c ON u.id = c.user_id
            LEFT JOIN conversation_history m ON c.id = m.session_id
            GROUP BY u.id
        """).fetchall()
        return {"users": [dict(u) for u in users]}
    finally:
        conn.close()


@app.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: int, request: Request):
    _require_admin(request)
    if user_id == request.state.user_id:
        raise HTTPException(status_code=400, detail="Cannot delete self")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")     # serialize the count check + deletes (no TOCTOU lockout race)
        target = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="No such user")
        # Never allow removing the last admin — it would lock everyone out of the console.
        if target["role"] == "admin" and \
           conn.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()["n"] <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last admin")
        sessions = conn.execute("SELECT id FROM chat_sessions WHERE user_id = ?", (user_id,)).fetchall()
        all_msg_ids = []
        for (sid,) in sessions:
            rows = conn.execute("SELECT id FROM conversation_history WHERE session_id = ?", (sid,)).fetchall()
            all_msg_ids.extend([str(r["id"]) for r in rows])
            conn.execute("DELETE FROM conversation_history WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM chat_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error("admin_delete_user(%s) failed: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Failed to delete user")
    finally:
        conn.close()
    memory.delete_vectors(all_msg_ids)
    return {"status": "ok"}


@app.put("/admin/users/{user_id}/role")
def admin_set_role(user_id: int, req: RoleUpdateRequest, request: Request):
    """Promote a user to admin or demote back to user. Refuses to demote the last admin."""
    _require_admin(request)
    conn = get_db()
    try:
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="No such user")
        # Atomic guard (no separate count→update, so no TOCTOU race): the demote applies only if it
        # won't drop the admin count to zero.
        cur = conn.execute(
            "UPDATE users SET role = ? WHERE id = ? AND "
            "(? != 'user' OR role != 'admin' OR (SELECT COUNT(*) FROM users WHERE role='admin') > 1)",
            (req.role, user_id, req.role))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=400, detail="Cannot demote the last admin")
        return {"status": "ok", "role": req.role}
    finally:
        conn.close()


@app.post("/admin/api_keys")
def admin_create_key(req: CreateKeyRequest, request: Request):
    _require_admin(request)
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM users WHERE id = ?", (req.user_id,)).fetchone():
            raise HTTPException(status_code=400, detail="No such user")
        new_key = "jk-" + secrets.token_hex(16)
        device_id = (req.device_id or "").strip() or None     # "" → NULL (unbound), like the CLI
        # Store only the hash + a short display prefix; the plaintext is shown once.
        conn.execute("INSERT INTO api_keys (key_string, key_prefix, user_id, description, device_id) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (hash_token(new_key), new_key[:10], req.user_id, req.description, device_id))
        conn.commit()
        return {"key": new_key, "device_id": device_id}
    finally:
        conn.close()


@app.get("/admin/api_keys")
def admin_list_keys(request: Request):
    _require_admin(request)
    conn = get_db()
    try:
        keys = conn.execute(
            "SELECT rowid AS id, key_prefix, user_id, description, device_id, created_at, usage_count, last_used_at "
            "FROM api_keys ORDER BY created_at DESC").fetchall()
        # Display the prefix only — the full key is never recoverable (hash at rest).
        return {"keys": [{**dict(k), "key_string": (k["key_prefix"] or "jk-") + "…"} for k in keys]}
    finally:
        conn.close()


@app.delete("/admin/api_keys/{key_id}")
def admin_delete_key(key_id: int, request: Request):
    _require_admin(request)
    conn = get_db()
    try:
        conn.execute("DELETE FROM api_keys WHERE rowid = ?", (key_id,))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@app.get("/admin/stats")
def admin_stats(request: Request):
    _require_admin(request)
    conn = get_db()
    try:
        return {
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "chats": conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM conversation_history").fetchone()[0],
        }
    finally:
        conn.close()


@app.get("/admin/events")
def admin_events(request: Request, limit: int = 50):
    """Recent edge/vision events (most recent first), for monitoring the camera agent."""
    _require_admin(request)
    limit = max(1, min(limit, 500))
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, device_id, type, data, created_at FROM vision_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        events = []
        for r in rows:
            e = dict(r)
            try:
                e["data"] = json.loads(e["data"]) if e["data"] else {}
            except (ValueError, TypeError):
                e["data"] = {}
            events.append(e)
        return {"events": events, "count": len(events)}
    finally:
        conn.close()


@app.get("/admin/faces")
def admin_list_faces(request: Request):
    """Enrolled people for the admin Faces page: name, linked user, embedding count, last sighting."""
    _require_admin(request)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT p.id, p.name, p.user_id, u.username, p.created_at, "
            "  COUNT(e.id) AS embedding_count, "
            "  (SELECT MAX(v.created_at) FROM vision_events v "
            "     WHERE v.type='face_seen' AND json_extract(v.data,'$.name')=p.name) AS last_seen "
            "FROM persons p LEFT JOIN users u ON p.user_id = u.id "
            "LEFT JOIN face_embeddings e ON e.person_id = p.id "
            "GROUP BY p.id ORDER BY p.name").fetchall()
        return {"faces": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/admin/faces/{person_id}/embeddings")
def admin_list_embeddings(person_id: int, request: Request):
    """The individual embeddings for a person (for the details/expand view)."""
    _require_admin(request)
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM persons WHERE id = ?", (person_id,)).fetchone():
            raise HTTPException(status_code=404, detail="No such person")
        rows = conn.execute(
            "SELECT id, source, created_at FROM face_embeddings WHERE person_id = ? ORDER BY id",
            (person_id,)).fetchall()
        return {"embeddings": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.put("/admin/faces/{person_id}")
def admin_update_face(person_id: int, req: FaceUpdateRequest, request: Request):
    """Rename a person and/or link them to a user account. Only the fields actually sent change
    (so a rename can't clobber the link); send user_id=null to clear the link."""
    _require_admin(request)
    fields = req.model_fields_set
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM persons WHERE id = ?", (person_id,)).fetchone():
            raise HTTPException(status_code=404, detail="No such person")
        if "name" in fields and req.name:
            if conn.execute("SELECT 1 FROM persons WHERE name = ? AND id != ?",
                            (req.name.strip(), person_id)).fetchone():
                raise HTTPException(status_code=400, detail="A person with that name already exists")
            conn.execute("UPDATE persons SET name = ? WHERE id = ?", (req.name.strip(), person_id))
        if "user_id" in fields:
            if req.user_id is not None and not conn.execute("SELECT 1 FROM users WHERE id = ?", (req.user_id,)).fetchone():
                raise HTTPException(status_code=400, detail="No such user")
            conn.execute("UPDATE persons SET user_id = ? WHERE id = ?", (req.user_id, person_id))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@app.delete("/admin/faces/{person_id}")
def admin_delete_face(person_id: int, request: Request):
    """Delete a person and all their embeddings."""
    _require_admin(request)
    conn = get_db()
    try:
        conn.execute("DELETE FROM face_embeddings WHERE person_id = ?", (person_id,))
        cur = conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="No such person")
        return {"status": "ok"}
    finally:
        conn.close()


@app.delete("/admin/faces/embeddings/{embedding_id}")
def admin_delete_embedding(embedding_id: int, request: Request):
    """Delete one embedding (the person stays — useful to prune a bad capture)."""
    _require_admin(request)
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM face_embeddings WHERE id = ?", (embedding_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="No such embedding")
        return {"status": "ok"}
    finally:
        conn.close()


# ----------------- Knowledge -----------------
@app.get("/knowledge")
def list_knowledge(request: Request):
    facts = memory.get_user_knowledge_list(request.state.user_id)
    return {"facts": facts, "count": len(facts)}


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


@app.post("/knowledge/extract-now")
def force_extraction(request: Request):
    _require_admin(request)
    unprocessed = memory.get_unprocessed_messages(batch_size=50)
    if not unprocessed:
        return {"status": "ok", "processed": 0, "message": "No unprocessed messages"}
    memory.extract_facts_batch(unprocessed)
    return {"status": "ok", "processed": len(unprocessed)}


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


@app.get("/favicon.svg")
def serve_favicon():
    # index.html links /favicon.svg, but only /assets and /static are mounted, so
    # the dist-root favicon would 404 on every page load without this route.
    favicon = REACT_DIST_DIR / "favicon.svg"
    if not favicon.exists():
        raise HTTPException(status_code=404)
    return FileResponse(favicon, media_type="image/svg+xml")


if REACT_DIST_DIR.exists():
    assets_dir = REACT_DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CONFIG["orchestrator"]["host"], port=CONFIG["orchestrator"]["port"])
