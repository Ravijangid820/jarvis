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
import sqlite3
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import chat
import memory
from auth import hash_password, hash_token, verify_password
from budget import is_default_session
from config import (ADMIN_HTML, ADMIN_MAX_INPUT, ALLOWED_ORIGINS, COMPLETION_RESERVE_DEFAULT,
                    CONFIG, INDEX_HTML, RATE_LIMIT_RPM, REACT_DIST_DIR, REGULAR_MAX_INPUT,
                    STATIC_DIR, VALID_FACT_CATEGORIES, logger)
from db import get_db, init_db
from llm import llm_content, request_llm, request_llm_stream, synthesize_tts


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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

# --- Rate limiting (in-process, per user) -----------------------------------
_rate_store: Dict[str, List[float]] = defaultdict(list)


def check_rate_limit(key: str) -> bool:
    now = time.time()
    window_start = now - 60.0
    _rate_store[key] = [t for t in _rate_store[key] if t > window_start]
    if len(_rate_store[key]) >= RATE_LIMIT_RPM:
        return False
    _rate_store[key].append(now)
    return True


# Stricter, IP-keyed limiter for the unauthenticated login endpoint (brute-force guard).
_login_store: Dict[str, List[float]] = defaultdict(list)
LOGIN_MAX_PER_MIN = 8


def check_login_rate(ip: str) -> bool:
    now = time.time()
    _login_store[ip] = [t for t in _login_store[ip] if t > now - 60.0]
    if len(_login_store[ip]) >= LOGIN_MAX_PER_MIN:
        return False
    _login_store[ip].append(now)
    return True


def _apply_security_headers(response: Response, cache: str = "no-store") -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
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
            is_authenticated = True
        else:
            # 2. Per-user API key (machine integrations, e.g. the voice listener)
            row = conn.execute(
                "SELECT user_id, u.role FROM api_keys k JOIN users u ON k.user_id = u.id "
                "WHERE k.key_string = ?", (token,)).fetchone()
            if row:
                request.state.user_id = row["user_id"]
                request.state.is_admin = (row["role"] == "admin")
                is_authenticated = True
                try:
                    conn.execute("UPDATE api_keys SET usage_count = usage_count + 1, "
                                 "last_used_at = datetime('now') WHERE key_string = ?", (token,))
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
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


class CreateKeyRequest(BaseModel):
    user_id: int
    description: str


class KnowledgeFactRequest(BaseModel):
    content: str
    category: str = "other"


# ----------------- Auth endpoints -----------------
@app.post("/auth/login")
def login(req: LoginRequest, request: Request):
    # Throttle by client IP — login is unauthenticated and bypasses the per-user
    # limiter, so without this it's an unbounded password-guessing oracle.
    client_ip = request.client.host if request.client else "unknown"
    if not check_login_rate(client_ip):
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
    # Auth-gated by the middleware (not in the bypass list).
    return _system_stats()


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
        sessions = conn.execute("SELECT id FROM chat_sessions WHERE user_id = ?", (user_id,)).fetchall()
        all_msg_ids = []
        for (sid,) in sessions:
            rows = conn.execute("SELECT id FROM conversation_history WHERE session_id = ?", (sid,)).fetchall()
            all_msg_ids.extend([str(r["id"]) for r in rows])
            conn.execute("DELETE FROM conversation_history WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM chat_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
    memory.delete_vectors(all_msg_ids)
    return {"status": "ok"}


@app.post("/admin/api_keys")
def admin_create_key(req: CreateKeyRequest, request: Request):
    _require_admin(request)
    conn = get_db()
    try:
        new_key = "jk-" + secrets.token_hex(16)
        conn.execute("INSERT INTO api_keys (key_string, user_id, description) VALUES (?, ?, ?)",
                     (new_key, req.user_id, req.description))
        conn.commit()
        return {"key": new_key}
    finally:
        conn.close()


@app.get("/admin/api_keys")
def admin_list_keys(request: Request):
    _require_admin(request)
    conn = get_db()
    try:
        try:
            keys = conn.execute("SELECT key_string, user_id, description, created_at, usage_count, last_used_at FROM api_keys").fetchall()
        except sqlite3.OperationalError:
            keys = conn.execute("SELECT key_string, user_id, description, created_at, 0 as usage_count, NULL as last_used_at FROM api_keys").fetchall()
        masked = []
        for k in keys:
            km = dict(k)
            km["full_key"] = km["key_string"]
            km["key_string"] = km["key_string"][:6] + "..." + km["key_string"][-4:]
            masked.append(km)
        return {"keys": masked}
    finally:
        conn.close()


@app.delete("/admin/api_keys/{key_string}")
def admin_delete_key(key_string: str, request: Request):
    _require_admin(request)
    conn = get_db()
    try:
        conn.execute("DELETE FROM api_keys WHERE key_string = ?", (key_string,))
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
    fact_id = memory.store_fact(request.state.user_id, category, content, source="manual")
    return {"id": fact_id, "status": "ok"}


@app.put("/knowledge/{fact_id}")
def edit_knowledge(fact_id: int, req: KnowledgeFactRequest, request: Request):
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty content")
    memory.update_fact(fact_id, request.state.user_id, content,
                       req.category.lower().strip() if req.category else None)
    return {"status": "ok"}


@app.delete("/knowledge/{fact_id}")
def remove_knowledge(fact_id: int, request: Request):
    memory.delete_fact(fact_id, request.state.user_id)
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
    if not ADMIN_HTML.exists():
        raise HTTPException(status_code=404)
    return FileResponse(ADMIN_HTML, media_type="text/html")


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
