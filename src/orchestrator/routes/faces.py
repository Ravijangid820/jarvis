"""Face enrollment, recognition, and the admin views over both.

A face is a credential here: it can gate device control (REQUIRE_PRESENCE_FOR_CONTROL), so
enrollment is admin-only and templates are not handed out to ordinary members. Matching happens
server-side — the browser computes a vector on-device and asks who it belongs to — which is what
keeps the household's templates on the server and the threshold defined exactly once.
"""
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import deps
from db import get_db

router = APIRouter(tags=["faces"])


# SFace's calibrated cosine cutoff (OpenCV's recommended value). THE definition of "recognized" for
# this deployment: matching happens here, so clients never carry a threshold of their own to drift
# out of step with this one.
FACE_RECOGNIZE_THRESHOLD = 0.363


class FaceEnrollRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    embedding: List[float] = Field(..., min_length=8, max_length=2048)   # L2-normalized vector
    source: Optional[str] = Field(default=None, max_length=64)           # device_id / "cli"
    replace: bool = False          # if true, clear this person's existing embeddings first


class FaceUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    user_id: Optional[int] = None          # link person → account (null clears the link)


class FaceIdentifyRequest(BaseModel):
    # One freshly-computed L2-normalized vector to match against this household's enrolled set.
    # Same shape as FaceEnrollRequest.embedding — the client computes it exactly the same way,
    # the only difference is that nothing is stored.
    embedding: List[float] = Field(..., min_length=8, max_length=2048)


@router.post("/faces/enroll")
def enroll_face(req: FaceEnrollRequest, request: Request):
    """Register a face embedding (computed on the edge/laptop) for a person. Admin-only — faces can
    drive authorization, so enrollment is privileged. Adds to the person's embeddings (creating the
    person if new); pass replace=true to start their set over."""
    deps.require_admin(request)
    household_id = deps.household(request)
    name = req.name.strip()
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM persons WHERE household_id = ? AND name = ?",
                           (household_id, name)).fetchone()
        person_id = row["id"] if row else conn.execute(
            "INSERT INTO persons (household_id, name) VALUES (?, ?)", (household_id, name)).lastrowid
        if req.replace:
            conn.execute("DELETE FROM face_embeddings WHERE person_id = ?", (person_id,))
        cur = conn.execute(
            "INSERT INTO face_embeddings (person_id, embedding, source) VALUES (?, ?, ?)",
            (person_id, json.dumps(req.embedding), (req.source or "").strip() or None))
        conn.commit()
        return {"status": "ok", "person_id": person_id, "embedding_id": cur.lastrowid}
    finally:
        conn.close()


@router.get("/faces/enrolled")
def enrolled_faces(request: Request):
    """The enrolled set for an always-on camera agent: {name: [embedding, ...]} (a list per person —
    recognition matches against the best of all).

    **Device keys and admins only.** This hands out every face template in the household, so it is
    not something an ordinary logged-in member should be able to pull: a face is a credential here
    (it can drive device authorization), and a template is enough to replay one. A headless camera
    still needs the set locally — it matches motion-gated at several frames a second and must keep
    working through a server blip — and trusting a device-bound key you minted for a camera in your
    own home is a deliberate, revocable grant. Interactive clients (the browser) match through
    /faces/identify instead and never see a template but their own.
    """
    if not (getattr(request.state, "device_id", None) or getattr(request.state, "is_admin", False)):
        raise HTTPException(status_code=403, detail="device-scoped key (or admin) required")
    household_id = deps.household(request)
    conn = get_db()
    try:
        # Scoped: a camera must only ever be handed the face vectors of the household it belongs
        # to. Unscoped, one household's agent would recognise (and greet, and authorize) people
        # enrolled by another.
        rows = conn.execute(
            "SELECT p.name AS name, e.embedding AS embedding "
            "FROM face_embeddings e JOIN persons p ON e.person_id = p.id "
            "WHERE p.household_id = ?", (household_id,)).fetchall()
        out: Dict[str, Any] = {}
        for r in rows:
            out.setdefault(r["name"], []).append(json.loads(r["embedding"]))
        return {"enrolled": out}
    finally:
        conn.close()


@router.post("/faces/identify")
def identify_face(req: FaceIdentifyRequest, request: Request):
    """Match one freshly-computed embedding against this household's enrolled people.

    This is the whole recognition path for interactive clients. The browser computes the vector
    on-device (YuNet + SFace in a worker, pixels never leaving the machine) and sends only the
    vector; the server answers who it belongs to. Matching lives here rather than in the client so
    that (a) the household's face templates are never handed out to make a comparison, and (b) the
    threshold and the best-of-many-embeddings rule have exactly one definition.

    Returns {"name": null} when nobody is enrolled, "unknown" when the best match is below
    FACE_RECOGNIZE_THRESHOLD, and the person's name otherwise. `score` is the best cosine seen
    either way, which is what makes a failed match diagnosable ("0.31, try better lighting").
    """
    household_id = deps.household(request)
    vec = req.embedding
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT p.name AS name, e.embedding AS embedding "
            "FROM face_embeddings e JOIN persons p ON e.person_id = p.id "
            "WHERE p.household_id = ?", (household_id,)).fetchall()
    finally:
        conn.close()
    best, best_sim = None, -1.0
    for r in rows:
        cand = json.loads(r["embedding"])
        if len(cand) != len(vec):        # a vector from a different model can't be compared
            continue
        # Both sides are L2-normalized on the way in, so the dot product IS the cosine.
        sim = sum(a * b for a, b in zip(vec, cand))
        if sim > best_sim:
            best, best_sim = r["name"], sim
    if best is None:
        return {"name": None, "score": None}
    # A precise similarity turns this into a hill-climbing oracle: submit a vector, nudge it toward
    # a higher score, repeat, and a face template can be reconstructed without ever seeing one. The
    # 120 rpm limit makes that slow rather than impossible, and faces can gate device control when
    # REQUIRE_PRESENCE_FOR_CONTROL is on. Admins keep the exact figure because it is the number that
    # makes a failed match diagnosable ("0.31 — try better lighting"); everyone else gets one
    # decimal, which still distinguishes "nearly matched" from "nowhere close" but carries far too
    # little gradient to climb.
    precise = bool(getattr(request.state, "is_admin", False))
    def _score(v):
        return round(v, 3) if precise else round(v, 1)
    if best_sim >= FACE_RECOGNIZE_THRESHOLD:
        return {"name": best, "score": _score(best_sim)}
    return {"name": "unknown", "score": _score(best_sim)}


@router.get("/admin/faces")
def admin_list_faces(request: Request):
    """Enrolled people for the admin Faces page: name, linked user, embedding count, last sighting."""
    deps.require_admin(request)
    conn = get_db()
    try:
        # The last_seen subquery matches on NAME, so it needs the household filter too — without
        # it, a namesake in another household would set this household's "last seen" timestamp and
        # leak the fact that someone by that name was sighted elsewhere.
        rows = conn.execute(
            "SELECT p.id, p.name, p.user_id, u.username, p.created_at, "
            "  COUNT(e.id) AS embedding_count, "
            "  (SELECT MAX(v.created_at) FROM vision_events v "
            "     WHERE v.household_id = p.household_id AND v.type='face_seen' "
            "       AND json_extract(v.data,'$.name')=p.name) AS last_seen "
            "FROM persons p LEFT JOIN users u ON p.user_id = u.id "
            "LEFT JOIN face_embeddings e ON e.person_id = p.id "
            "WHERE p.household_id = ? "
            "GROUP BY p.id ORDER BY p.name", (deps.household(request),)).fetchall()
        return {"faces": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.get("/admin/faces/{person_id}/embeddings")
def admin_list_embeddings(person_id: int, request: Request):
    """The individual embeddings for a person (for the details/expand view)."""
    deps.require_admin(request)
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM persons WHERE id = ? AND household_id = ?",
                            (person_id, deps.household(request))).fetchone():
            raise HTTPException(status_code=404, detail="No such person")
        rows = conn.execute(
            "SELECT id, source, created_at FROM face_embeddings WHERE person_id = ? ORDER BY id",
            (person_id,)).fetchall()
        return {"embeddings": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.put("/admin/faces/{person_id}")
def admin_update_face(person_id: int, req: FaceUpdateRequest, request: Request):
    """Rename a person and/or link them to a user account. Only the fields actually sent change
    (so a rename can't clobber the link); send user_id=null to clear the link."""
    deps.require_admin(request)
    fields = req.model_fields_set
    household_id = deps.household(request)
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM persons WHERE id = ? AND household_id = ?",
                            (person_id, household_id)).fetchone():
            raise HTTPException(status_code=404, detail="No such person")
        if "name" in fields and req.name:
            if conn.execute("SELECT 1 FROM persons WHERE name = ? AND household_id = ? AND id != ?",
                            (req.name.strip(), household_id, person_id)).fetchone():
                raise HTTPException(status_code=400, detail="A person with that name already exists")
            conn.execute("UPDATE persons SET name = ? WHERE id = ?", (req.name.strip(), person_id))
        if "user_id" in fields:
            # The target account must be in the SAME household — otherwise a face here could be
            # linked to a user over there, handing them this household's device authorization.
            if req.user_id is not None and not conn.execute(
                    "SELECT 1 FROM users WHERE id = ? AND household_id = ?",
                    (req.user_id, household_id)).fetchone():
                raise HTTPException(status_code=400, detail="No such user")
            conn.execute("UPDATE persons SET user_id = ? WHERE id = ?", (req.user_id, person_id))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@router.delete("/admin/faces/{person_id}")
def admin_delete_face(person_id: int, request: Request):
    """Delete a person and all their embeddings."""
    deps.require_admin(request)
    conn = get_db()
    try:
        household_id = deps.household(request)
        # Scope the person delete, and drop embeddings only for a person that is actually ours —
        # otherwise a guessed id would wipe another household's biometric data.
        conn.execute("DELETE FROM face_embeddings WHERE person_id IN "
                     "(SELECT id FROM persons WHERE id = ? AND household_id = ?)",
                     (person_id, household_id))
        cur = conn.execute("DELETE FROM persons WHERE id = ? AND household_id = ?",
                           (person_id, household_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="No such person")
        deps.audit(request, "face.delete", f"person_id={person_id}")
        return {"status": "ok"}
    finally:
        conn.close()


@router.delete("/admin/faces/embeddings/{embedding_id}")
def admin_delete_embedding(embedding_id: int, request: Request):
    """Delete one embedding (the person stays — useful to prune a bad capture)."""
    deps.require_admin(request)
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM face_embeddings WHERE id = ? AND person_id IN "
            "(SELECT id FROM persons WHERE household_id = ?)",
            (embedding_id, deps.household(request)))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="No such embedding")
        return {"status": "ok"}
    finally:
        conn.close()
