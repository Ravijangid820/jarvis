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
import re
import shutil
import sqlite3
import secrets
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
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
from intents import is_gesture_volume, parse_reminder, parse_volume
from budget import is_default_session
from config import (ADMIN_MAX_INPUT, ALLOWED_ORIGINS, APP_VERSION, BASE_DIR, CHROMA_DB_PATH,
                    COMPLETION_RESERVE_DEFAULT, CONFIG, HA_TOKEN_FROM_ENV, HA_URL_FROM_ENV,
                    INDEX_HTML, LLM_URL, PIPER_BIN, PIPER_MODEL,
                    RATE_LIMIT_RPM, REACT_DIST_DIR, REGULAR_MAX_INPUT, REQUIRE_PRESENCE_FOR_CONTROL,
                    STATIC_DIR, VALID_FACT_CATEGORIES, logger)
import ha
from db import get_db, get_setting, init_db, set_setting
from llm import llm_content, request_llm, request_llm_stream, request_llm_tools, synthesize_tts


@asynccontextmanager
def _load_ha_settings():
    """Apply the DB-stored (admin-UI-managed) Home Assistant settings at startup. Environment vars
    win — a field set via env stays as config.py resolved it and the UI shows it read-only."""
    try:
        url = None if HA_URL_FROM_ENV else get_setting("ha_url")
        token = None if HA_TOKEN_FROM_ENV else get_setting("ha_token")
        ents_raw = get_setting("ha_allowed_entities")
        allowed = None
        if ents_raw is not None:
            try:
                allowed = json.loads(ents_raw)
            except (ValueError, TypeError):
                allowed = []
        ha.configure(url=url, token=token, allowed=allowed)
    except Exception as e:
        logger.warning("Could not load Home Assistant settings from DB: %s", e)


async def lifespan(app: FastAPI):
    init_db()
    _load_ha_settings()        # runtime HA config (env > DB), before anything serves
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

# Default device a spoken volume command targets (the Windows volume agent's device id). Matches
# VolumeRequest's default; spoken commands don't name a device, so they go here.
VOICE_DEVICE = "laptop"
VOICE_CAMERA = "laptop-cam"      # camera a spoken "volume" (gesture) request engages

# Gesture-volume mode: a spoken "Jarvis, volume" opens a short, voice-authorized window during which
# the camera reports hand height and the SERVER maps movement → volume steps (so the camera key needs
# no device-control permission). State is in-memory; entries expire on their own.
_GESTURE_MODES: Dict[str, Dict[str, Any]] = {}   # camera_device_id -> {expires, last_y, target}
_GESTURE_TTL_S = 12.0            # mode lifetime, refreshed on each hand report
_GESTURE_GAIN = 110              # normalized Δy (0..1) → volume %  (~half-frame swing ≈ 55%)
_GESTURE_DEADZONE = 0.015        # ignore sub-threshold jitter
_GESTURE_STEP_CLAMP = 25         # max % change per report (smoothness / anti-jump)

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
    if (request.method == "OPTIONS" or path in ["/health", "/", "/admin", "/auth/login", "/favicon.svg", "/ca.crt"]
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

    # Rate-limit ALL authenticated callers (admins included), keyed on user id. Exempt the gesture
    # report — it posts at video rate but is gated by an active, separately-authorized mode.
    if path != "/devices/gesture" and not check_rate_limit(f"user:{request.state.user_id}"):
        return Response(
            content=json.dumps({"error": "Rate limit exceeded",
                                "detail": "Rate limit exceeded — slow down a moment and retry."}),
            status_code=429, media_type="application/json", headers={"Retry-After": "5"})

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


class HAConfigRequest(BaseModel):
    url: Optional[str] = None
    token: Optional[str] = None                 # blank/omitted on save = keep the stored token
    allowed_entities: Optional[List[str]] = None


class CreateKeyRequest(BaseModel):
    user_id: int
    description: str
    # Optional: bind the key to one device (e.g. "laptop-cam"). A bound key may ONLY post events as
    # that device (F1). Edge/camera agents need this — a plain unbound non-admin key can't post events.
    device_id: Optional[str] = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")


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


class VolumeRequest(BaseModel):
    action: str = Field(..., max_length=16)        # set | step | mute | unmute
    value: Optional[int] = Field(default=None, ge=-100, le=100)
    device: str = Field(default="laptop", max_length=64)


class GestureReport(BaseModel):
    y: float = Field(..., ge=0.0, le=1.0)          # normalized hand height (0=top, 1=bottom of frame)


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


class EnrollRequestCreate(BaseModel):
    # Enroll FOR a user account (preferred — links the face → account); name is derived from the
    # user, or may be given directly (e.g. the CLI / a non-account person).
    user_id: Optional[int] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    device_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")


class EnrollResult(BaseModel):
    request_id: int
    embedding: Optional[List[float]] = Field(default=None, min_length=8, max_length=2048)
    error: Optional[str] = Field(default=None, max_length=200)


class EnrollPreview(BaseModel):
    request_id: int
    image: str = Field(..., max_length=700000)   # base64 JPEG (annotated frame); ~500 KB cap
    captured: int = 0
    total: int = 0


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


def _service_status() -> list:
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
    if ha.configured():
        services.append(s("Home Assistant", ha.ping(),
                          f"{len(ha.HA_ALLOWED_ENTITIES)} entities allowlisted · {ha.HA_URL}"))

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
    """Per-subsystem health for the admin console (active/inactive + detail), plus the app version and
    an at-a-glance operational summary (how many subsystems are up)."""
    _require_admin(request)
    services = _service_status()
    up = sum(1 for x in services if x["status"] == "active")
    return {
        "services": services,
        "version": APP_VERSION,
        "summary": {"up": up, "total": len(services), "operational": up == len(services)},
    }


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


@app.get("/faces/enroll-request")
def get_enroll_request(request: Request, device: Optional[str] = None):
    """The camera agent polls this for a pending enroll request for ITS device (provenance bound to
    the key, like /events). Returns {"request": {id, name}} or {"request": null}."""
    dev = getattr(request.state, "device_id", None)
    if dev:
        device_id = dev                          # device key → only its own requests
    elif getattr(request.state, "is_admin", False) and device:
        device_id = device                       # admin may inspect any device (testing)
    else:
        raise HTTPException(status_code=403, detail="device-scoped key (or admin + ?device=) required")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, name FROM enroll_requests WHERE device_id = ? AND status = 'pending' "
            "ORDER BY id LIMIT 1", (device_id,)).fetchone()
        return {"request": dict(row) if row else None}
    finally:
        conn.close()


@app.post("/faces/enroll-result")
def post_enroll_result(req: EnrollResult, request: Request):
    """The agent submits the captured embedding (or an error) for a pending request. A device key may
    only fulfill a request for ITS OWN device — it cannot enroll arbitrary faces (an admin must have
    created the request first)."""
    dev = getattr(request.state, "device_id", None)
    is_admin = getattr(request.state, "is_admin", False)
    if not dev and not is_admin:
        raise HTTPException(status_code=403, detail="device-scoped key (or admin) required")
    conn = get_db()
    try:
        r = conn.execute("SELECT device_id, name, status, user_id FROM enroll_requests WHERE id = ?",
                         (req.request_id,)).fetchone()
        if r is None:
            raise HTTPException(status_code=404, detail="No such request")
        if not is_admin and r["device_id"] != dev:
            raise HTTPException(status_code=403, detail="Not your device's request")
        if r["status"] != "pending":
            raise HTTPException(status_code=409, detail="Request already handled")
        if req.error or not req.embedding:
            conn.execute("UPDATE enroll_requests SET status='failed', detail=?, completed_at=datetime('now') "
                         "WHERE id=?", ((req.error or "no face captured")[:200], req.request_id))
            conn.commit()
            return {"status": "failed"}
        # Resolve the person: prefer the linked account (so the face maps to a user), else by name.
        prow = None
        if r["user_id"] is not None:
            prow = conn.execute("SELECT id FROM persons WHERE user_id = ?", (r["user_id"],)).fetchone()
        if prow is None:
            prow = conn.execute("SELECT id FROM persons WHERE name = ?", (r["name"],)).fetchone()
        if prow is None:
            person_id = conn.execute("INSERT INTO persons (name, user_id) VALUES (?, ?)",
                                     (r["name"], r["user_id"])).lastrowid
        else:
            person_id = prow["id"]
            if r["user_id"] is not None:           # make sure an existing person is linked to the account
                conn.execute("UPDATE persons SET user_id = ? WHERE id = ?", (r["user_id"], person_id))
        conn.execute("INSERT INTO face_embeddings (person_id, embedding, source) VALUES (?, ?, ?)",
                     (person_id, json.dumps(req.embedding), r["device_id"]))
        conn.execute("UPDATE enroll_requests SET status='done', completed_at=datetime('now') WHERE id=?",
                     (req.request_id,))
        conn.commit()
        return {"status": "ok", "person_id": person_id}
    finally:
        conn.close()


# Live enroll preview frames — kept ONLY in memory (never written to disk/DB), short TTL, admin-only
# to view. This is the one place imagery leaves the device, and only while an admin-initiated enroll
# is active.
_ENROLL_PREVIEWS: Dict[int, Dict[str, Any]] = {}
_PREVIEW_TTL_S = 30


@app.post("/faces/enroll-preview")
def post_enroll_preview(req: EnrollPreview, request: Request):
    """The agent posts annotated frames for an in-progress enroll (device key, bound to its request)."""
    dev = getattr(request.state, "device_id", None)
    is_admin = getattr(request.state, "is_admin", False)
    if not dev and not is_admin:
        raise HTTPException(status_code=403, detail="device-scoped key (or admin) required")
    conn = get_db()
    try:
        r = conn.execute("SELECT device_id FROM enroll_requests WHERE id = ?", (req.request_id,)).fetchone()
    finally:
        conn.close()
    if r is None:
        raise HTTPException(status_code=404, detail="No such request")
    if not is_admin and r["device_id"] != dev:
        raise HTTPException(status_code=403, detail="Not your device's request")
    now = time.time()
    for k in [k for k, v in _ENROLL_PREVIEWS.items() if now - v["ts"] > _PREVIEW_TTL_S]:
        _ENROLL_PREVIEWS.pop(k, None)
    if len(_ENROLL_PREVIEWS) > 20:                 # safety cap (RAM-only)
        _ENROLL_PREVIEWS.clear()
    _ENROLL_PREVIEWS[req.request_id] = {"image": req.image, "captured": req.captured,
                                        "total": req.total, "ts": now}
    return {"status": "ok"}


@app.get("/faces/enroll-preview")
def get_enroll_preview(request: Request, request_id: int):
    """Latest preview frame for an enroll request (admin-only — it's imagery)."""
    _require_admin(request)
    p = _ENROLL_PREVIEWS.get(request_id)
    if not p or time.time() - p["ts"] > _PREVIEW_TTL_S:
        return {"preview": None}
    return {"preview": {"image": p["image"], "captured": p["captured"], "total": p["total"]}}


@app.get("/faces/enroll-preview-stream")
async def stream_enroll_preview(request: Request, request_id: int):
    """Smooth live preview during enrollment over ONE connection (admin-only). Pushes each NEW frame
    the agent posts as a line of NDJSON {image, captured, total} — so the UI shows ~10 fps video
    without polling per frame. Bounded: stops on client disconnect, when frames go stale, or after
    a hard cap (a capture finishes in seconds)."""
    _require_admin(request)

    async def gen():
        last_ts, start = 0.0, time.time()
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            now = time.time()
            p = _ENROLL_PREVIEWS.get(request_id)
            if p and p["ts"] != last_ts and now - p["ts"] <= _PREVIEW_TTL_S:
                last_ts, idle = p["ts"], 0
                yield json.dumps({"image": p["image"], "captured": p["captured"],
                                  "total": p["total"]}) + "\n"
            else:
                idle += 1
                if idle > 150:            # ~12s with no new frame → capture is over/agent gone
                    break
            if now - start > 90:          # hard safety cap
                break
            await asyncio.sleep(0.08)

    return StreamingResponse(gen(), media_type="application/x-ndjson")


_AUDIT_CAP = 5000   # keep the most recent N audit rows


def _audit(request: Request, action: str, detail: str = "") -> None:
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
                "INSERT INTO audit_log (user_id, username, action, detail) VALUES (?, ?, ?, ?)",
                (uid, uname, action, (detail or "")[:500]))
            if cur.lastrowid % 200 == 0:   # prune occasionally, not on every write
                conn.execute("DELETE FROM audit_log WHERE id <= ?", (cur.lastrowid - _AUDIT_CAP,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("audit log failed (%s): %s", action, e)


@app.get("/admin/audit")
def admin_audit(request: Request, limit: int = 100):
    """Recent audit entries (most recent first) — device control + admin changes."""
    _require_admin(request)
    limit = max(1, min(limit, 1000))
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, created_at, user_id, username, action, detail FROM audit_log "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return {"entries": [dict(r) for r in rows]}
    finally:
        conn.close()


# ----------------- Backups -----------------
BACKUP_DIR = BASE_DIR / "backups"
_BACKUP_NAME_RE = re.compile(r"^jarvis-backup-[0-9]{8}-[0-9]{6}\.tar\.gz$")


def _create_backup(ts: str) -> Dict[str, Any]:
    """Snapshot the irreplaceable data into backups/jarvis-backup-<ts>.tar.gz: a CONSISTENT online
    copy of the SQLite DB (VACUUM INTO) + the ChromaDB dir. Models/config are re-creatable, so excluded
    (and config holds secrets). `ts` is passed in (scripts can't call Date.now)."""
    BACKUP_DIR.mkdir(exist_ok=True)
    name = f"jarvis-backup-{ts}.tar.gz"
    out = BACKUP_DIR / name
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = get_db()
        try:
            conn.execute("VACUUM INTO ?", (str(tmp / "jarvis.db"),))   # consistent, online
        finally:
            conn.close()
        chroma = Path(str(CHROMA_DB_PATH))
        if chroma.exists():
            shutil.copytree(chroma, tmp / "chroma_db")
        with tarfile.open(out, "w:gz") as tar:
            for p in sorted(tmp.iterdir()):
                tar.add(p, arcname=p.name)
    os.chmod(out, 0o600)   # contains password/token hashes + embeddings — keep it owner-only
    return {"name": name, "size": out.stat().st_size}


@app.post("/admin/backup")
def admin_backup(request: Request):
    """Create a backup now (admin). Returns the filename + size."""
    _require_admin(request)
    try:
        info = _create_backup(datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    except Exception as e:
        logger.error("backup failed: %s", e)
        raise HTTPException(status_code=500, detail="Backup failed")
    _audit(request, "backup.create", f"{info['name']} ({info['size']} bytes)")
    return {"status": "ok", **info}


@app.get("/admin/backups")
def admin_list_backups(request: Request):
    _require_admin(request)
    if not BACKUP_DIR.exists():
        return {"backups": []}
    items = []
    for p in sorted(BACKUP_DIR.glob("jarvis-backup-*.tar.gz"), reverse=True):
        st = p.stat()
        items.append({"name": p.name, "size": st.st_size,
                      "created_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")})
    return {"backups": items}


@app.get("/admin/backups/{name}")
def admin_download_backup(name: str, request: Request):
    _require_admin(request)
    if not _BACKUP_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Bad backup name")
    p = BACKUP_DIR / name
    if not p.exists():
        raise HTTPException(status_code=404, detail="No such backup")
    _audit(request, "backup.download", name)
    return FileResponse(str(p), media_type="application/gzip", filename=name)


@app.delete("/admin/backups/{name}")
def admin_delete_backup(name: str, request: Request):
    _require_admin(request)
    if not _BACKUP_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Bad backup name")
    p = BACKUP_DIR / name
    if not p.exists():
        raise HTTPException(status_code=404, detail="No such backup")
    p.unlink()
    _audit(request, "backup.delete", name)
    return {"status": "ok"}


@app.get("/presence")
def presence(request: Request):
    """Who the cameras have recognized recently (household context). Any authenticated user."""
    return {"present": memory.get_present_people()}


@app.get("/arrivals")
def arrivals(request: Request, since_id: int = 0):
    """Recent 'someone arrived' events (last 2 min) for the UI to greet. Poll with since_id to get
    only new ones."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, data, created_at FROM vision_events WHERE type='presence_arrival' "
            "AND id > ? AND created_at > datetime('now', '-120 seconds') ORDER BY id", (since_id,)).fetchall()
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


def _authorized_person_present() -> bool:
    """True if a currently-present recognized person maps to a user allowed to control devices.
    Used only when REQUIRE_PRESENCE_FOR_CONTROL is on."""
    names = memory.get_present_people()
    if not names:
        return False
    conn = get_db()
    try:
        ph = ",".join("?" * len(names))
        row = conn.execute(
            f"SELECT 1 FROM persons p JOIN users u ON p.user_id = u.id WHERE p.name IN ({ph}) "
            "AND (u.role = 'admin' OR u.can_control_devices = 1) LIMIT 1", names).fetchone()
        return row is not None
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


def _enqueue_volume(action: str, value: Optional[int], device: str) -> int:
    """Validate + enqueue one volume command (the tiny vocabulary set|step|mute|unmute). Shared by
    the REST endpoint and the voice fast-path. Raises HTTPException on a bad command. NOT an authz
    check — callers must gate on _can_control_devices first."""
    action = (action or "").lower()
    params: Dict[str, Any] = {}
    if action == "set":
        if value is None or not (0 <= value <= 100):
            raise HTTPException(status_code=400, detail="set requires value 0–100")
        params = {"value": value}
    elif action == "step":
        if value is None:
            raise HTTPException(status_code=400, detail="step requires value (-100…100)")
        params = {"value": max(-100, min(value, 100))}
    elif action not in ("mute", "unmute"):
        raise HTTPException(status_code=400, detail="action must be set|step|mute|unmute")
    conn = get_db()
    try:
        cur = conn.execute("INSERT INTO device_commands (device_id, action, params) VALUES (?, ?, ?)",
                           (device, action, json.dumps(params)))
        # Retention: drop delivered commands older than a day so the queue doesn't grow forever.
        conn.execute("DELETE FROM device_commands WHERE status='delivered' AND delivered_at < datetime('now','-1 day')")
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _spoken_volume_ack(action: str, value: Optional[int]) -> str:
    """A short, speakable confirmation for the voice path."""
    if action == "set":
        return f"Volume set to {value} percent."
    if action == "step":
        return f"Turning it {'up' if (value or 0) >= 0 else 'down'} by {abs(value or 0)} percent."
    if action == "mute":
        return "Muted."
    if action == "unmute":
        return "Unmuted."
    return "Done."


def _open_gesture_mode(camera: str, target: str) -> None:
    """Authorize a time-boxed gesture→volume window for `camera` and signal it via the command
    channel (long-poll). The server-side mode entry gates POST /devices/gesture."""
    now = time.time()
    _GESTURE_MODES[camera] = {"expires": now + _GESTURE_TTL_S, "last_y": None, "target": target}
    for k in [k for k, v in _GESTURE_MODES.items() if v["expires"] < now]:   # prune stale
        _GESTURE_MODES.pop(k, None)
    conn = get_db()
    try:
        conn.execute("INSERT INTO device_commands (device_id, action, params) VALUES (?, ?, ?)",
                     (camera, "gesture_mode", json.dumps({"mode": "volume", "ttl": int(_GESTURE_TTL_S)})))
        conn.execute("DELETE FROM device_commands WHERE status='delivered' AND delivered_at < datetime('now','-1 day')")
        conn.commit()
    finally:
        conn.close()


def _handle_volume_command(user_text: str, raw_request: Request) -> Optional[str]:
    """If user_text is a recognized volume command, authorize + act on it and return a short spoken
    ack; otherwise None (→ caller falls through to the LLM). Shared by /inbox and /chat/stream."""
    vol = parse_volume(user_text)
    is_gesture = vol is None and is_gesture_volume(user_text)
    if (vol is not None or is_gesture) and REQUIRE_PRESENCE_FOR_CONTROL and not _authorized_person_present():
        return "I don't see anyone authorized in the room, so I can't change that right now."
    if vol is not None:
        if not _can_control_devices(raw_request):
            return "Sorry — you're not authorized to control devices."
        _enqueue_volume(vol["action"], vol.get("value"), VOICE_DEVICE)
        _audit(raw_request, "device.volume", f"{vol['action']} {vol.get('value', '')}".strip())
        return _spoken_volume_ack(vol["action"], vol.get("value"))
    if is_gesture_volume(user_text):                 # "Jarvis, volume" → hand-gesture control
        if not _can_control_devices(raw_request):
            return "Sorry — you're not authorized to control devices."
        _open_gesture_mode(VOICE_CAMERA, VOICE_DEVICE)
        _audit(raw_request, "device.gesture_mode", VOICE_CAMERA)
        return "Gesture volume control on — raise or lower your hand."
    return None


# ---- LLM tool-calling (voice path). Rule fast-paths still run first; this catches phrasings they
# miss, and is where new actions (e.g. lights) plug in. Single round-trip + templated confirmation. ----
TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "set_volume", "description": "Set or change the speaker volume.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["set", "step", "mute", "unmute"],
                       "description": "set=absolute level, step=relative change, mute/unmute"},
            "value": {"type": "integer", "description": "0-100 for set; positive/negative for step"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "create_reminder", "description": "Create a reminder/timer that fires after some minutes.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "what to remind about"},
            "in_minutes": {"type": "integer", "description": "minutes from now"}},
            "required": ["in_minutes"]}}},
    {"type": "function", "function": {
        "name": "get_presence", "description": "Who the cameras currently recognize as present.",
        "parameters": {"type": "object", "properties": {}}}},
]

# HA tools are kept SEPARATE and merged per-request by _active_tools(): HA config is runtime-mutable
# (admin UI / DB), so an import-time `if ha.configured()` would freeze the menu — configuring HA via
# the UI would never expose the tools to the model (the v2.5.0 bug).
HA_TOOLS = [
    {"type": "function", "function": {
        "name": "home_control",
        "description": "Turn a smart-home device (light, switch, plug) on or off, or toggle it.",
        "parameters": {"type": "object", "properties": {
            "device": {"type": "string", "description": "which device, e.g. 'kitchen light'"},
            "action": {"type": "string", "enum": ["on", "off", "toggle"]}},
            "required": ["device", "action"]}}},
    {"type": "function", "function": {
        "name": "home_status",
        "description": "Get the current on/off state of the smart-home devices.",
        "parameters": {"type": "object", "properties": {
            "device": {"type": "string", "description": "one device; omit for all"}}}}},
]


def _active_tools():
    """The tool menu offered to the model on THIS request — reflects live HA config."""
    return TOOLS_SPEC + (HA_TOOLS if ha.configured() else [])


def _tool_set_volume(args, raw_request):
    if not _can_control_devices(raw_request):
        return "Sorry — you're not authorized to control devices."
    if REQUIRE_PRESENCE_FOR_CONTROL and not _authorized_person_present():
        return "I don't see anyone authorized in the room, so I can't change that right now."
    action, value = str(args.get("action", "set")).lower(), args.get("value")
    try:
        _enqueue_volume(action, value, VOICE_DEVICE)
    except HTTPException:
        return "I couldn't make that volume change."
    _audit(raw_request, "device.volume", f"{action} {value if value is not None else ''} (tool)".strip())
    return _spoken_volume_ack(action, value)


def _tool_create_reminder(args, raw_request):
    try:
        mins = int(args.get("in_minutes"))
    except (TypeError, ValueError):
        mins = 0
    if mins <= 0:
        return "When would you like to be reminded?"
    text = (str(args.get("text") or "Reminder")).strip()[:200] or "Reminder"
    due = datetime.now() + timedelta(minutes=mins)
    conn = get_db()
    try:
        conn.execute("INSERT INTO reminders (user_id, text, due_at) VALUES (?, ?, ?)",
                     (raw_request.state.user_id, text, due.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()
    _audit(raw_request, "reminder.create", f"{text} @ {due.strftime('%Y-%m-%d %H:%M')} (tool)")
    if text in ("Timer", "Reminder"):
        return f"{text} set for {due.strftime('%H:%M')}."
    return f"Okay — I'll remind you to {text} at {due.strftime('%H:%M')}."


def _tool_get_presence(args, raw_request):
    names = memory.get_present_people()
    return ("I can see " + ", ".join(names) + ".") if names else "I don't see anyone right now."


def _tool_home_control(args, raw_request):
    if not _can_control_devices(raw_request):
        return "Sorry — you're not authorized to control devices."
    if REQUIRE_PRESENCE_FOR_CONTROL and not _authorized_person_present():
        return "I don't see anyone authorized in the room, so I can't change that right now."
    action = str(args.get("action", "")).lower()
    entity = ha.resolve_entity(str(args.get("device", "")))
    if entity is None:
        allowed = ", ".join(e.partition(".")[2].replace("_", " ") for e in ha.HA_ALLOWED_ENTITIES)
        return f"I'm not sure which device you mean. I can control: {allowed or 'nothing yet — the allowlist is empty'}."
    if not ha.turn(entity, action):
        return "I couldn't reach Home Assistant to do that."
    _audit(raw_request, "device.home_assistant", f"{action} {entity} (tool)")
    nice = entity.partition(".")[2].replace("_", " ")
    return f"Okay — {nice} {'toggled' if action == 'toggle' else action}."


def _tool_home_status(args, raw_request):
    if not _can_control_devices(raw_request):
        return "Sorry — you're not authorized to view device states."
    device = str(args.get("device") or "").strip()
    entities = [ha.resolve_entity(device)] if device else list(ha.HA_ALLOWED_ENTITIES)
    if not entities or entities[0] is None:
        return "I'm not sure which device you mean."
    parts = []
    for ent in entities:
        st = ha.get_state(ent)
        if st:
            name = (st.get("attributes") or {}).get("friendly_name") or ent.partition(".")[2].replace("_", " ")
            parts.append(f"{name} is {st.get('state')}")
    return ("; ".join(parts) + ".") if parts else "I couldn't reach Home Assistant."


_TOOLS = {"set_volume": _tool_set_volume, "create_reminder": _tool_create_reminder,
          "get_presence": _tool_get_presence,
          "home_control": _tool_home_control, "home_status": _tool_home_status}


def _run_tool_calls(message: Dict[str, Any], raw_request: Request) -> Optional[str]:
    """Execute the first tool call in an assistant message; return a spoken reply, or None if none."""
    calls = message.get("tool_calls") or []
    if not calls:
        return None
    fn = calls[0].get("function", {})
    handler = _TOOLS.get(fn.get("name"))
    if not handler:
        return None
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (ValueError, TypeError):
        args = {}
    try:
        return handler(args, raw_request)
    except Exception as e:
        logger.warning("tool %s failed: %s", fn.get("name"), e)
        return None


def _handle_reminder(user_text: str, raw_request: Request) -> Optional[str]:
    """If user_text is a reminder/timer, store it for the caller and return a confirmation; else None."""
    now = datetime.now()
    r = parse_reminder(user_text, now)
    if r is None:
        return None
    due = r["due_at"]
    conn = get_db()
    try:
        conn.execute("INSERT INTO reminders (user_id, text, due_at) VALUES (?, ?, ?)",
                     (raw_request.state.user_id, r["text"], due.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()
    _audit(raw_request, "reminder.create", f"{r['text']} @ {due.strftime('%Y-%m-%d %H:%M')}")
    when = due.strftime("%H:%M")
    if r["text"] in ("Timer", "Reminder"):
        return f"{r['text']} set for {when}."
    return f"Okay — I'll remind you to {r['text']} at {when}."


@app.post("/devices/volume")
def queue_volume(req: VolumeRequest, request: Request):
    """Enqueue a volume command for a device agent (e.g. the Windows volume agent).

    Authorization is enforced here against the caller's identity/permissions; the command is
    a tiny validated vocabulary (no shell, no free text). The device agent pulls + executes it.
    """
    if not _can_control_devices(request):
        raise HTTPException(status_code=403, detail="Not authorized to control devices")
    cmd_id = _enqueue_volume(req.action, req.value, req.device)
    _audit(request, "device.volume", f"{req.action} {req.value or ''} -> {req.device}".strip())
    return {"status": "ok", "id": cmd_id}


@app.post("/devices/gesture")
def report_gesture(req: GestureReport, request: Request):
    """The camera reports normalized hand height while in gesture mode; the server maps movement to
    volume steps for the mode's target. Gated by an active, voice-authorized mode for THIS camera, so
    the camera key needs no device-control permission. Returns {active} so the camera knows when to stop."""
    dev = getattr(request.state, "device_id", None)
    if not dev and getattr(request.state, "is_admin", False):
        dev = request.query_params.get("device")          # admin may drive it for testing
    if not dev:
        raise HTTPException(status_code=403, detail="device-scoped key (or admin + ?device=) required")
    now = time.time()
    mode = _GESTURE_MODES.get(dev)
    if not mode or mode["expires"] < now:
        _GESTURE_MODES.pop(dev, None)
        return {"active": False}
    if mode["last_y"] is not None:
        dy = mode["last_y"] - req.y                        # hand up = smaller y = louder
        if abs(dy) >= _GESTURE_DEADZONE:
            step = max(-_GESTURE_STEP_CLAMP, min(int(round(dy * _GESTURE_GAIN)), _GESTURE_STEP_CLAMP))
            if step != 0:
                _enqueue_volume("step", step, mode["target"])
    mode["last_y"] = req.y
    mode["expires"] = now + _GESTURE_TTL_S                 # refresh while the hand is active
    return {"active": True, "expires_in": int(mode["expires"] - now)}


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
        # Arrival detection: a recognized person not seen recently → emit a one-off presence_arrival
        # the UI announces ("welcome home"). Tracked in-memory so it's cheap on the events hot path.
        if req.type == "face_seen":
            nm = (req.data or {}).get("name")
            if nm and nm != "unknown":
                now = time.time()
                if now - _present_since.get(nm, 0.0) > ARRIVAL_GAP_S:
                    conn.execute("INSERT INTO vision_events (device_id, type, data, user_id) VALUES (?, ?, ?, ?)",
                                 (device_id, "presence_arrival", json.dumps({"name": nm}), request.state.user_id))
                _present_since[nm] = now
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

    # Fast-paths handled directly (instant, offline, no LLM): volume/gesture, then reminders.
    ack = _handle_volume_command(user_text, raw_request) or _handle_reminder(user_text, raw_request)
    if ack is not None:
        chat.store_message(session_id, "user", user_text)
        chat.store_message(session_id, "jarvis", ack)
        return {"response": ack, "speed": "", "new_title": None,
                "audio": synthesize_tts(ack) if request.voice_feedback else None}

    existing = chat.get_recent_context(session_id)
    needs_title = (len(existing) == 0) and not is_default_session(session_id)
    completion_reserve = request.n_predict if (request.n_predict and request.n_predict > 0) else COMPLETION_RESERVE_DEFAULT
    messages = chat.build_messages(session_id, user_id, user_text, request.system_prompt, completion_reserve=completion_reserve)
    max_tokens = chat.clamp_completion_for(messages, request.n_predict)

    t0 = time.time()
    with memory.Inflight():
        # One call with tools offered: the model either invokes a tool (a command) or just answers.
        llm_resp = request_llm_tools(messages, _active_tools(), temperature=request.temperature, n_predict=max_tokens)
    t1 = time.time()

    msg = (llm_resp.get("choices") or [{}])[0].get("message", {})
    tool_reply = _run_tool_calls(msg, raw_request)
    answer = tool_reply if tool_reply is not None else (llm_content(llm_resp).strip() or "…")
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

    # Fast-paths (volume/gesture, reminders) short-circuit the LLM and stream back the ack.
    ack = _handle_volume_command(user_text, raw_request) or _handle_reminder(user_text, raw_request)
    if ack is not None:
        def vol_gen():
            chat.store_message(session_id, "user", user_text)
            chat.store_message(session_id, "jarvis", ack)
            yield f"data: {json.dumps({'content': ack})}\n\n"
            done: Dict[str, Any] = {"done": True}
            if request.voice_feedback:
                audio = synthesize_tts(ack)
                if audio:
                    done["audio"] = audio
            yield f"data: {json.dumps(done)}\n\n"
        return StreamingResponse(vol_gen(), media_type="text/event-stream")

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
        conn.execute("BEGIN IMMEDIATE")               # serialize id selection against concurrent creates
        new_id = _lowest_free_user_id(conn)           # reuse a freed id, but only a residue-free one
        conn.execute("INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, ?, ?)",
                     (new_id, req.username, hash_password(req.password), req.role))
        conn.commit()
        _audit(request, "user.create", f"{req.username} role={req.role} id={new_id}")
        return {"status": "ok", "id": new_id}
    except sqlite3.IntegrityError:
        conn.rollback()
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


# Every table keyed by user_id — kept in one place so a purge can't miss one (and so id-reuse can
# prove an id is residue-free before handing it to a new account).
_USER_REF_TABLES = ("chat_sessions", "auth_sessions", "api_keys", "user_knowledge",
                    "persons", "vision_events", "enroll_requests")


def _purge_user(conn, user_id: int) -> List[str]:
    """Delete EVERYTHING tied to user_id so a freed id carries no residue. Personal data (chats,
    knowledge, keys, sessions) is removed; faces and camera events are UNLINKED (user_id→NULL) so the
    household's recognition data survives but no longer points at the account. Returns the message ids
    to drop from ChromaDB (caller commits, then calls memory.delete_vectors)."""
    msg_ids = [str(r["id"]) for r in conn.execute(
        "SELECT id FROM conversation_history WHERE session_id IN "
        "(SELECT id FROM chat_sessions WHERE user_id = ?)", (user_id,)).fetchall()]
    conn.execute("DELETE FROM conversation_history WHERE session_id IN "
                 "(SELECT id FROM chat_sessions WHERE user_id = ?)", (user_id,))
    conn.execute("DELETE FROM chat_sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM api_keys WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM user_knowledge WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM enroll_requests WHERE user_id = ?", (user_id,))
    conn.execute("UPDATE persons SET user_id = NULL WHERE user_id = ?", (user_id,))
    conn.execute("UPDATE vision_events SET user_id = NULL WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return msg_ids


def _id_has_residue(conn, uid: int) -> bool:
    """True if any user-scoped table still holds rows for uid (defense-in-depth before id reuse)."""
    for t in _USER_REF_TABLES:   # table names are a fixed allowlist, not user input
        if conn.execute(f"SELECT 1 FROM {t} WHERE user_id = ? LIMIT 1", (uid,)).fetchone():
            return True
    return False


def _lowest_free_user_id(conn) -> int:
    """Smallest positive id that's neither in use nor carrying residue — so a reused id is provably
    clean. (Reuse is the operator's choice; this makes it safe.)"""
    used = {r["id"] for r in conn.execute("SELECT id FROM users")}
    nid = 1
    while nid in used or _id_has_residue(conn, nid):
        nid += 1
    return nid


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
        all_msg_ids = _purge_user(conn, user_id)
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
    _audit(request, "user.delete", f"id={user_id}")
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
        _audit(request, "user.role", f"id={user_id} -> {req.role}")
        return {"status": "ok", "role": req.role}
    finally:
        conn.close()


@app.get("/admin/home-assistant")
def admin_ha_get(request: Request):
    """Current HA config for the admin UI. Never returns the token itself — only whether one is set."""
    _require_admin(request)
    return {
        "configured": ha.configured(),
        "url": ha.HA_URL,
        "token_set": bool(ha.HA_TOKEN),
        "allowed_entities": list(ha.HA_ALLOWED_ENTITIES),
        "env_managed": HA_URL_FROM_ENV or HA_TOKEN_FROM_ENV,   # set via env → UI is read-only
        "connected": ha.ping(),
    }


@app.put("/admin/home-assistant")
def admin_ha_put(req: HAConfigRequest, request: Request):
    """Save HA config (url/token/allowlist) to the DB and apply it live — no restart."""
    _require_admin(request)
    if HA_URL_FROM_ENV or HA_TOKEN_FROM_ENV:
        raise HTTPException(status_code=409,
                            detail="Home Assistant is configured via environment variables — edit those instead.")
    url = (req.url or "").rstrip("/")
    set_setting("ha_url", url)
    if req.token:                                   # blank = keep the existing token
        set_setting("ha_token", req.token)
    token = get_setting("ha_token") or ""
    allowed = list(req.allowed_entities if req.allowed_entities is not None else ha.HA_ALLOWED_ENTITIES)
    set_setting("ha_allowed_entities", json.dumps(allowed))
    ha.configure(url=url, token=token, allowed=allowed)
    _audit(request, "ha.config", f"url={url or '(cleared)'} entities={len(allowed)}")
    return {"status": "ok", "configured": ha.configured(), "connected": ha.ping()}


@app.post("/admin/home-assistant/test")
def admin_ha_test(req: HAConfigRequest, request: Request):
    """Probe a URL/token (blank token = use the stored one) before saving."""
    _require_admin(request)
    ok, detail = ha.test_connection(req.url, req.token or ha.HA_TOKEN)
    return {"ok": ok, "detail": detail}


@app.get("/admin/home-assistant/entities")
def admin_ha_entities(request: Request):
    """Controllable HA entities for the device picker (uses the currently-saved connection)."""
    _require_admin(request)
    return {"entities": ha.list_entities()}


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
        _audit(request, "key.create", f"user={req.user_id} device={device_id or '-'} ({new_key[:10]}…)")
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
        _audit(request, "key.delete", f"id={key_id}")
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
def admin_events(request: Request, limit: int = 50, type: Optional[str] = None, since_id: int = 0):
    """Recent edge/vision events (most recent first). `type` filters (e.g. face_seen for the
    recognitions panel / verify); `since_id` returns only events newer than an id (efficient polling)."""
    _require_admin(request)
    limit = max(1, min(limit, 500))
    conn = get_db()
    try:
        q = "SELECT id, device_id, type, data, created_at FROM vision_events WHERE id > ?"
        params: List[Any] = [since_id]
        if type:
            q += " AND type = ?"
            params.append(type)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
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
        _audit(request, "face.delete", f"person_id={person_id}")
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


@app.post("/admin/faces/enroll-request")
def admin_create_enroll_request(req: EnrollRequestCreate, request: Request):
    """Ask a camera device to enroll a face: queues a pending request its agent will pick up,
    capture, and fulfill. The capture happens on the device that has the camera."""
    _require_admin(request)
    conn = get_db()
    try:
        name = req.name.strip() if req.name else None
        if req.user_id is not None:
            urow = conn.execute("SELECT username FROM users WHERE id = ?", (req.user_id,)).fetchone()
            if not urow:
                raise HTTPException(status_code=400, detail="No such user")
            name = name or urow["username"]        # default the person's name to the account's
        if not name:
            raise HTTPException(status_code=400, detail="Pick a user (or pass a name)")
        cur = conn.execute(
            "INSERT INTO enroll_requests (device_id, name, user_id, requested_by) VALUES (?, ?, ?, ?)",
            (req.device_id, name, req.user_id, request.state.user_id))
        conn.execute("DELETE FROM enroll_requests WHERE created_at < datetime('now', '-7 days')")
        conn.commit()
        _audit(request, "face.enroll_request", f"{name} -> {req.device_id}")
        return {"status": "ok", "id": cur.lastrowid}
    finally:
        conn.close()


@app.get("/admin/faces/enroll-requests")
def admin_list_enroll_requests(request: Request):
    """Recent enroll requests + their status, for the UI to show progress."""
    _require_admin(request)
    conn = get_db()
    try:
        # Expire requests the agent never completed (offline/crashed mid-capture) so the UI stops
        # polling a zombie's live preview forever (which would burn the caller's rate budget). A real
        # capture finishes in seconds; 3 min is generous.
        conn.execute(
            "UPDATE enroll_requests SET status='failed', "
            "detail='timed out — agent did not complete', completed_at=datetime('now') "
            "WHERE status='pending' AND created_at < datetime('now','-3 minutes')")
        conn.commit()
        rows = conn.execute(
            "SELECT id, device_id, name, status, detail, created_at, completed_at "
            "FROM enroll_requests ORDER BY id DESC LIMIT 20").fetchall()
        return {"requests": [dict(r) for r in rows]}
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


@app.get("/admin/knowledge/global")
def list_global_knowledge(request: Request):
    """Household/global facts (shared by all users). Admin-only — these go into everyone's prompt."""
    _require_admin(request)
    facts = memory.get_global_knowledge_list()
    return {"facts": facts, "count": len(facts)}


@app.post("/admin/knowledge/global")
def add_global_knowledge(req: KnowledgeFactRequest, request: Request):
    """Add a household fact (admin-only). An external tool (e.g. a loader script) can call this too."""
    _require_admin(request)
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty content")
    category = (req.category or "other").lower().strip()
    if category not in VALID_FACT_CATEGORIES:
        category = "other"
    fact_id = memory.store_global_fact(category, content, source="manual")
    _audit(request, "knowledge.global.add", f"[{category}] {content[:120]}")
    return {"id": fact_id, "status": "ok"}


@app.put("/admin/knowledge/global/{fact_id}")
def edit_global_knowledge(fact_id: int, req: KnowledgeFactRequest, request: Request):
    _require_admin(request)
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty content")
    if not memory.update_global_fact(fact_id, content,
                                     req.category.lower().strip() if req.category else None):
        raise HTTPException(status_code=404, detail="No such fact")
    return {"status": "ok"}


@app.delete("/admin/knowledge/global/{fact_id}")
def remove_global_knowledge(fact_id: int, request: Request):
    _require_admin(request)
    if not memory.delete_global_fact(fact_id):
        raise HTTPException(status_code=404, detail="No such fact")
    _audit(request, "knowledge.global.delete", f"id={fact_id}")
    return {"status": "ok"}


@app.post("/admin/knowledge/global/chat")
def global_knowledge_chat(req: GlobalChatRequest, request: Request):
    """Admin 'global chat': each non-empty line of the message becomes a household fact and is stored
    immediately. Deterministic (no LLM) so it's instant and never mis-files what you said."""
    _require_admin(request)
    lines = [ln.strip() for ln in req.text.splitlines() if ln.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="Nothing to save")
    saved = [{"id": memory.store_global_fact("household", ln, source="global-chat"), "content": ln}
             for ln in lines]
    _audit(request, "knowledge.global.chat", f"+{len(saved)} fact(s)")
    return {"reply": f"Saved {len(saved)} fact{'s' if len(saved) != 1 else ''} to household knowledge.",
            "saved": saved, "count": len(memory.get_global_knowledge_list())}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CONFIG["orchestrator"]["host"], port=CONFIG["orchestrator"]["port"])
