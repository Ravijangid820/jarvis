"""Admin surfaces: users, API keys, the model inventory, Home Assistant config, backups,
the audit trail, and the event/stats views the console reads.

Everything here is behind deps.require_admin — but note that admin is not the same as trusted
operator. In demo mode every visitor is an admin OF THEIR OWN HOUSEHOLD, so anything reaching
process-wide state (the model selection, MCP config) carries deps.require_not_demo as well, and
everything else is scoped by household so one admin cannot read another's.
"""
import json
import os
import secrets
import re
import sqlite3
import shutil
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import deps
import ha
import ha_config
import purge
import memory
from auth import hash_password, hash_token
from config import (BASE_DIR, CHROMA_DB_PATH, HA_TOKEN_FROM_ENV, HA_URL_FROM_ENV,
                    LLM_URL, logger)
from db import get_db, get_household_setting, set_household_setting

router = APIRouter(tags=["admin"])


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


class ModelSwitchRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=128)


# ----------------- Multi-Model Discovery & Switching -----------------
@router.get("/models")
def get_available_models(request: Request):
    """Return an admin-safe inventory of installed GGUF models.

    Model files and their absolute paths are server implementation details, so regular
    chat users never receive them. A selected model is only active after llama-server
    has actually restarted and reported it through /props.
    """
    deps.require_admin(request)
    models = []
    models_dir = BASE_DIR / "models"
    active_name = "Qwen3.5 2B"
    requested_name = None
    try:
        cfg_path = BASE_DIR / "config" / "active_model.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if data.get("active_model"):
                requested_name = data["active_model"]
        p = urlsplit(LLM_URL)
        with urllib.request.urlopen(f"{p.scheme}://{p.netloc}/props", timeout=1.5) as r:
            props = json.loads(r.read().decode("utf-8"))
            dgs = props.get("default_generation_settings") or {}
            model_path = props.get("model_path") or dgs.get("model") or ""
            if model_path:
                active_name = os.path.basename(str(model_path)).removesuffix(".gguf")
    except Exception:
        pass

    if models_dir.exists():
        for gguf in sorted(models_dir.rglob("*.gguf")):
            name = gguf.name.removesuffix(".gguf")
            size_bytes = gguf.stat().st_size
            size_mb = round(size_bytes / (1024 * 1024))
            models.append({
                "id": name,
                "name": name,
                "size_mb": size_mb,
                "active": (name == active_name or name in active_name or active_name in name),
                "requested": name == requested_name,
            })
    return {"models": models, "active": active_name, "requested": requested_name}


@router.post("/models/switch")
def switch_model(req: ModelSwitchRequest, request: Request):
    """Stage the model selected for the next deployment-managed llama-server restart.

    The server process belongs to systemd/Docker, not the web process. Persisting the
    requested model without pretending the live process changed prevents a UI/LLM
    mismatch and leaves the actual restart under the deployment supervisor.
    """
    deps.require_admin(request)
    models_dir = BASE_DIR / "models"
    target = None
    if models_dir.exists():
        for gguf in models_dir.rglob("*.gguf"):
            if gguf.name.removesuffix(".gguf") == req.model or gguf.name == req.model:
                target = gguf
                break
    if not target:
        raise HTTPException(status_code=404, detail=f"Model '{req.model}' not found on server disk.")

    try:
        cfg_path = BASE_DIR / "config" / "active_model.json"
        cfg_path.write_text(json.dumps({"active_model": target.name.removesuffix(".gguf"), "path": str(target)}, indent=2), encoding="utf-8")
        deps.audit(request, "model.stage", target.name)
        return {"status": "restart_required", "requested": target.name.removesuffix(".gguf"),
                "message": "Model selection saved. Restart llama-server to activate it."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update active model config: {e}")


@router.get("/admin/audit")
def admin_audit(request: Request, limit: int = 100):
    """Recent audit entries (most recent first) — device control + admin changes."""
    deps.require_admin(request)
    limit = max(1, min(limit, 1000))
    conn = get_db()
    try:
        # Scoped: the audit trail names users and the devices they drove, so an admin of one
        # household must not read another's.
        rows = conn.execute(
            "SELECT id, created_at, user_id, username, action, detail FROM audit_log "
            "WHERE household_id = ? ORDER BY id DESC LIMIT ?", (deps.household(request), limit)).fetchall()
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


@router.post("/admin/backup")
def admin_backup(request: Request):
    """Create a backup now (admin). Returns the filename + size."""
    deps.require_admin(request)
    try:
        info = _create_backup(datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    except Exception as e:
        logger.error("backup failed: %s", e)
        raise HTTPException(status_code=500, detail="Backup failed")
    deps.audit(request, "backup.create", f"{info['name']} ({info['size']} bytes)")
    return {"status": "ok", **info}


@router.get("/admin/backups")
def admin_list_backups(request: Request):
    deps.require_admin(request)
    if not BACKUP_DIR.exists():
        return {"backups": []}
    items = []
    for p in sorted(BACKUP_DIR.glob("jarvis-backup-*.tar.gz"), reverse=True):
        st = p.stat()
        items.append({"name": p.name, "size": st.st_size,
                      "created_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")})
    return {"backups": items}


@router.get("/admin/backups/{name}")
def admin_download_backup(name: str, request: Request):
    deps.require_admin(request)
    if not _BACKUP_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Bad backup name")
    p = BACKUP_DIR / name
    if not p.exists():
        raise HTTPException(status_code=404, detail="No such backup")
    deps.audit(request, "backup.download", name)
    return FileResponse(str(p), media_type="application/gzip", filename=name)


@router.delete("/admin/backups/{name}")
def admin_delete_backup(name: str, request: Request):
    deps.require_admin(request)
    if not _BACKUP_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Bad backup name")
    p = BACKUP_DIR / name
    if not p.exists():
        raise HTTPException(status_code=404, detail="No such backup")
    p.unlink()
    deps.audit(request, "backup.delete", name)
    return {"status": "ok"}


@router.post("/admin/users")
def admin_create_user(req: CreateUserRequest, request: Request):
    deps.require_admin(request)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")               # serialize id selection against concurrent creates
        new_id = purge.lowest_free_user_id(conn)           # reuse a freed id, but only a residue-free one
        # New accounts join the CREATING admin's household — there is deliberately no way to
        # create a user into someone else's, so an admin cannot plant an account in another home.
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, household_id) VALUES (?, ?, ?, ?, ?)",
            (new_id, req.username, hash_password(req.password), req.role, deps.household(request)))
        conn.commit()
        deps.audit(request, "user.create", f"{req.username} role={req.role} id={new_id}")
        return {"status": "ok", "id": new_id}
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=400, detail="Username exists")
    finally:
        conn.close()


@router.get("/admin/users")
def admin_list_users(request: Request):
    deps.require_admin(request)
    conn = get_db()
    try:
        users = conn.execute("""
            SELECT u.id, u.username, u.role, u.created_at,
                   COUNT(DISTINCT c.id) as total_chats,
                   COUNT(m.id) as total_messages
            FROM users u
            LEFT JOIN chat_sessions c ON u.id = c.user_id
            LEFT JOIN conversation_history m ON c.id = m.session_id
            WHERE u.household_id = ?
            GROUP BY u.id
        """, (deps.household(request),)).fetchall()
        return {"users": [dict(u) for u in users]}
    finally:
        conn.close()


# Every table keyed by user_id — kept in one place so a purge can't miss one (and so id-reuse can
# prove an id is residue-free before handing it to a new account).
@router.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: int, request: Request):
    deps.require_admin(request)
    if user_id == request.state.user_id:
        raise HTTPException(status_code=400, detail="Cannot delete self")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")     # serialize the count check + deletes (no TOCTOU lockout race)
        household_id = deps.household(request)
        target = conn.execute("SELECT role FROM users WHERE id = ? AND household_id = ?",
                              (user_id, household_id)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="No such user")
        # Never allow removing a household's last admin — it would lock that household out of its
        # own console. The count is per-household: another home's admins are no help here.
        if target["role"] == "admin" and conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND household_id = ?",
                (household_id,)).fetchone()["n"] <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last admin")
        all_msg_ids = purge.purge_user(conn, user_id)
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
    deps.audit(request, "user.delete", f"id={user_id}")
    return {"status": "ok"}


@router.put("/admin/users/{user_id}/role")
def admin_set_role(user_id: int, req: RoleUpdateRequest, request: Request):
    """Promote a user to admin or demote back to user. Refuses to demote the last admin."""
    deps.require_admin(request)
    conn = get_db()
    try:
        household_id = deps.household(request)
        if conn.execute("SELECT 1 FROM users WHERE id = ? AND household_id = ?",
                        (user_id, household_id)).fetchone() is None:
            raise HTTPException(status_code=404, detail="No such user")
        # Atomic guard (no separate count→update, so no TOCTOU race): the demote applies only if it
        # won't drop THIS household's admin count to zero.
        cur = conn.execute(
            "UPDATE users SET role = ? WHERE id = ? AND household_id = ? AND "
            "(? != 'user' OR role != 'admin' OR "
            " (SELECT COUNT(*) FROM users WHERE role='admin' AND household_id = ?) > 1)",
            (req.role, user_id, household_id, req.role, household_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=400, detail="Cannot demote the last admin")
        deps.audit(request, "user.role", f"id={user_id} -> {req.role}")
        return {"status": "ok", "role": req.role}
    finally:
        conn.close()


@router.get("/admin/home-assistant")
def admin_ha_get(request: Request):
    """Current HA config for the admin UI. Never returns the token itself — only whether one is set."""
    deps.require_admin(request)
    if not deps.owns_smart_home(request):
        # Not an error for a household without a smart home — just nothing to show. Reporting
        # "unconfigured" rather than 403 also avoids confirming that some OTHER household has one.
        return {"configured": False, "url": "", "token_set": False, "allowed_entities": [],
                "env_managed": False, "connected": False, "owned": False}
    return {
        "owned": True,
        "configured": ha.configured(),
        "url": ha.HA_URL,
        "token_set": bool(ha.HA_TOKEN),
        "allowed_entities": list(ha.HA_ALLOWED_ENTITIES),
        "env_managed": HA_URL_FROM_ENV or HA_TOKEN_FROM_ENV,   # set via env → UI is read-only
        "connected": ha.ping(),
    }


@router.put("/admin/home-assistant")
def admin_ha_put(req: HAConfigRequest, request: Request):
    """Save HA config (url/token/allowlist) to the DB and apply it live — no restart."""
    deps.require_admin(request)
    deps.require_smart_home(request)
    if HA_URL_FROM_ENV or HA_TOKEN_FROM_ENV:
        raise HTTPException(status_code=409,
                            detail="Home Assistant is configured via environment variables — edit those instead.")
    hid = deps.household(request)
    url = (req.url or "").rstrip("/")
    set_household_setting(hid, "ha_url", url)
    if req.token:                                   # blank = keep the existing token
        set_household_setting(hid, "ha_token", req.token)
    token = get_household_setting(hid, "ha_token") or ""
    allowed = list(req.allowed_entities if req.allowed_entities is not None else ha.HA_ALLOWED_ENTITIES)
    set_household_setting(hid, "ha_allowed_entities", json.dumps(allowed))
    ha.configure(url=url, token=token, allowed=allowed, household_id=hid)
    ha_config.rebuild_intent_router()
    deps.audit(request, "ha.config", f"url={url or '(cleared)'} entities={len(allowed)}")
    return {"status": "ok", "configured": ha.configured(), "connected": ha.ping()}


@router.post("/admin/home-assistant/test")
def admin_ha_test(req: HAConfigRequest, request: Request):
    """Probe a URL/token (blank token = use the stored one) before saving."""
    deps.require_admin(request)
    deps.require_smart_home(request)
    ok, detail = ha.test_connection(req.url, req.token or ha.HA_TOKEN)
    return {"ok": ok, "detail": detail}


@router.get("/admin/home-assistant/entities")
def admin_ha_entities(request: Request):
    """Controllable HA entities for the device picker (uses the currently-saved connection)."""
    deps.require_admin(request)
    deps.require_smart_home(request)
    return {"entities": ha.list_entities()}


@router.post("/admin/api_keys")
def admin_create_key(req: CreateKeyRequest, request: Request):
    deps.require_admin(request)
    conn = get_db()
    try:
        # A key may only ever be minted FOR a member of the admin's own household — otherwise an
        # admin could issue themselves a credential that authenticates as another household's user.
        if not conn.execute("SELECT 1 FROM users WHERE id = ? AND household_id = ?",
                            (req.user_id, deps.household(request))).fetchone():
            raise HTTPException(status_code=400, detail="No such user")
        new_key = "jk-" + secrets.token_hex(16)
        device_id = (req.device_id or "").strip() or None     # "" → NULL (unbound), like the CLI
        # Store only the hash + a short display prefix; the plaintext is shown once.
        conn.execute("INSERT INTO api_keys (key_string, key_prefix, user_id, description, device_id) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (hash_token(new_key), new_key[:10], req.user_id, req.description, device_id))
        conn.commit()
        deps.audit(request, "key.create", f"user={req.user_id} device={device_id or '-'} ({new_key[:10]}…)")
        return {"key": new_key, "device_id": device_id}
    finally:
        conn.close()


@router.get("/admin/api_keys")
def admin_list_keys(request: Request):
    deps.require_admin(request)
    conn = get_db()
    try:
        keys = conn.execute(
            "SELECT k.rowid AS id, k.key_prefix, k.user_id, k.description, k.device_id, "
            "       k.created_at, k.usage_count, k.last_used_at "
            "FROM api_keys k JOIN users u ON k.user_id = u.id "
            "WHERE u.household_id = ? ORDER BY k.created_at DESC",
            (deps.household(request),)).fetchall()
        # Display the prefix only — the full key is never recoverable (hash at rest).
        return {"keys": [{**dict(k), "key_string": (k["key_prefix"] or "jk-") + "…"} for k in keys]}
    finally:
        conn.close()


@router.delete("/admin/api_keys/{key_id}")
def admin_delete_key(key_id: int, request: Request):
    deps.require_admin(request)
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM api_keys WHERE rowid = ? AND user_id IN "
            "(SELECT id FROM users WHERE household_id = ?)",
            (key_id, deps.household(request)))
        conn.commit()
        deps.audit(request, "key.delete", f"id={key_id}")
        return {"status": "ok"}
    finally:
        conn.close()


@router.get("/admin/stats")
def admin_stats(request: Request):
    deps.require_admin(request)
    conn = get_db()
    try:
        hid = deps.household(request)
        # Counts are scoped too — an instance-wide total would tell a demo visitor how many real
        # users and conversations exist on the box.
        return {
            "users": conn.execute("SELECT COUNT(*) FROM users WHERE household_id = ?", (hid,)).fetchone()[0],
            "chats": conn.execute(
                "SELECT COUNT(*) FROM chat_sessions s JOIN users u ON s.user_id = u.id "
                "WHERE u.household_id = ?", (hid,)).fetchone()[0],
            "messages": conn.execute(
                "SELECT COUNT(*) FROM conversation_history h "
                "JOIN chat_sessions s ON h.session_id = s.id JOIN users u ON s.user_id = u.id "
                "WHERE u.household_id = ?", (hid,)).fetchone()[0],
        }
    finally:
        conn.close()


@router.get("/admin/events")
def admin_events(request: Request, limit: int = 50, type: Optional[str] = None, since_id: int = 0):
    """Recent edge/vision events (most recent first). `type` filters (e.g. face_seen for the
    recognitions panel / verify); `since_id` returns only events newer than an id (efficient polling)."""
    deps.require_admin(request)
    limit = max(1, min(limit, 500))
    conn = get_db()
    try:
        q = ("SELECT id, device_id, type, data, created_at FROM vision_events "
             "WHERE household_id = ? AND id > ?")
        params: List[Any] = [deps.household(request), since_id]
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
