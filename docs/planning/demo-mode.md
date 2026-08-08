# Public Demo — design plan

**Status:** Phases 1–2 landed (households + scoping + demo lifecycle) · **Date:** 2026-08-08

Goal: let anyone at `https://ravi-mk42.me/jarvis/` try chat, RAG, the admin panel and face
recognition, without any path from a visitor to a real user's data, and without smart-home
access. Demo state survives a page refresh, resets on logout, and expires on its own.

---

## 1. The core decision: households, not a demo flag

The v3.0.0 `JARVIS_MODE=demo` switch (config.py:200) is instance-wide and
`reset_demo_session` (chat.py:85) deletes rows for *every* user in the DB — so it can never be
enabled where real data lives. It also can't express "this visitor sees this data."

Instead of extending it, introduce the tenant boundary the product already needs:

```sql
CREATE TABLE households (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    is_demo     INTEGER NOT NULL DEFAULT 0,
    expires_at  DATETIME,              -- NULL = permanent; set for demo households
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE users ADD COLUMN household_id INTEGER REFERENCES households(id);
```

`household_id` then lands on every table that is currently household-wide or global:
`global_knowledge`, `persons`, `vision_events`, `enroll_requests`, `device_commands`,
`device_heartbeats`, `audit_log`, and the HA keys in `app_settings`.

Migration: create household 1 ("Home"), assign every existing row to it. Nothing changes for
the current deployment.

**Why this beats a demo flag:** a demo visitor is isolated by the same mechanism that isolates
two real households. The invariant is one sentence — *every query filters on the caller's
`household_id`* — and it is provable with a single test fixture (household A must never see
household B) rather than an audit of 40 endpoints.

### Scope of the change

~75 query sites across `main.py`, `memory.py`, `chat.py` touch these tables. The mechanical
part is adding a filter; the design part is a single `request.state.household_id` set in
`security_middleware` (main.py:238) alongside `user_id`/`is_admin`, plus a helper that refuses
to build a query without it.

---

## 2. Demo household lifecycle

### Mint

`POST /demo/session` — unauthenticated. Creates:

- a `households` row with `is_demo=1`, `expires_at = now + 60 min`
- one `users` row inside it, `role='admin'`, username `demo_<8 hex>` — so the visitor
  gets the admin panel, scoped to their own empty household
- an `auth_sessions` row whose `expires_at` matches the household's

Returns a normal bearer token. The client stores it in `localStorage.jarvis_token` exactly as
a real login does — no new client-side storage path.

### The reset matrix

| Event | Behaviour | Mechanism |
|---|---|---|
| **Page refresh** | data intact | token persists in `localStorage`; same household row |
| **Reopen tab later (within TTL)** | data intact | same |
| **Explicit logout** | full wipe | `/auth/logout` (main.py:477) also purges the household when `is_demo` |
| **Idle / TTL expiry** | full wipe | sweeper in the lifespan worker (main.py:82) |
| **Tab closed without logout** | wipe at TTL | *not* on `pagehide` — see below |

**Do not wire `navigator.sendBeacon` to `pagehide`.** `pagehide` fires on refresh too, so it
would destroy exactly the case that must survive. TTL is the correct backstop for an abandoned
tab.

### Purge

`_purge_user` (main.py:2100) is already the right primitive — it deletes chats, history,
knowledge, api keys, enroll requests and returns the msg ids for vector cleanup. It needs a
household-level sibling that calls it for each member, then drops the household's
`global_knowledge`, `persons`, `face_embeddings`, `vision_events`, `audit_log` rows.

**Known trap:** `_lowest_free_user_id` (main.py:2129) recycles user ids, and `_id_has_residue`
only checks SQLite tables — never ChromaDB. If a vector delete fails, the next visitor issued
that id inherits the previous visitor's RAG memories. Demo purges must delete vectors by
`where={"user_id": uid}`, not by the message-id list, and demo user ids should come from a
dedicated high range (e.g. ≥ 100000) so they never collide with real accounts.

---

## 3. What a demo visitor sees

The visitor is an admin *of their own household*, so the panel is fully functional and fully
empty of real data. Every panel is populated from a **seed** run at mint time: a handful of
synthetic users, enrolled people, audit entries and household knowledge, so the UI looks alive
rather than blank.

| Surface | Demo behaviour |
|---|---|
| Chat, RAG, memory, knowledge | full, scoped to the demo household |
| Users / roles | full CRUD on synthetic members |
| Faces & recognitions | full (see §5) |
| Audit log | full, seeded + their own actions |
| Stats | counts for their household only |
| **Home Assistant** | **blocked** — `is_demo` households have no HA config and every HA route 403s |
| Devices / volume / gestures | blocked — same rule |
| Backups | blocked — DoS + it dumps the whole DB |
| Model switch | blocked — restarts llama-server |
| System telemetry | blocked — host infrastructure detail |

Note the last four are blocked as **availability/infrastructure** concerns, not privacy ones —
they'd be unsafe even in a world with perfect tenant isolation.

---

## 4. Pre-flight: leaks that exist today

These bypass user scoping by design and must be fixed before anything is public, independent of
the household work:

1. **`global_knowledge` is injected into every prompt** (chat.py:205). Household facts —
   address, family names, room layout — would appear in a demo visitor's chat context. Fixed by
   `household_id` on the table.
2. **`get_present_people()` puts `[Seen by cameras: …]` in every prompt** (chat.py:226,
   memory.py:281). Leaks who is home, by name, in real time. Fixed by scoping `vision_events`.
3. **`GET /admin/backups/{name}`** (main.py:1138) streams the entire SQLite DB — password
   hashes, hashed tokens, HA token, every chat. Must be unreachable for demo principals.
4. **`POST /demo/session` is unauthenticated**, so it creates rows for anyone who asks. It needs
   a per-IP mint limit — not for throughput, but so the households/users tables can't be grown
   without bound by a script.

RAG retrieval itself is already correctly scoped (memory.py:487) — it filters on `user_id`.

---

## 5. Face recognition — browser webcam enrollment

The visitor enrolls their own face from the browser and sees themselves recognised. `YuNet` +
`SFace` run client-side via `onnxruntime-web`, which is **already vendored at 1.24.3** for the
Whisper STT path and already works under the CSP (`wasm-unsafe-eval`) and the
COOP/COEP cross-origin isolation headers (main.py:205).

Flow: capture frames → YuNet detect → SFace embed → POST the L2-normalised vector to the
existing `/faces/enroll`, now household-scoped. Recognition compares against the demo
household's own embeddings only. Imagery never leaves the browser; only the vector is sent —
matching the existing on-edge design.

**Biometric handling.** A face embedding from a member of the public is biometric data, so:

- explicit opt-in before the camera starts, stating that a vector (not an image) is stored and
  that it is deleted on logout or within the TTL
- a visible "delete my face data now" control
- the household purge must cover `face_embeddings` — verified by test, not by inspection
- no vision events retained past household expiry

---

## 6. Phasing

| Phase | Deliverable | Gate | Status |
|---|---|---|---|
| **0/1** | `households` table + `household_id` scoping + isolation tests | full suite green | **done** |
| **2** | Demo lifecycle: mint, purge-on-logout, TTL sweeper | reset matrix (§2) covered by tests | **done** |
| **3** | Seed content so panels look alive | manual review of each panel | **done** (backend) |
| **4** | Browser webcam enrollment (YuNet + SFace via onnxruntime-web) | consent flow + purge test | next |

Phases 0/1 merged in practice: leaks 1 and 2 in §4 are *fixed by* the household scoping rather
than being separable from it. What landed:

- `households` + `household_settings` tables; `household_id` on `users`, `global_knowledge`,
  `persons`, `vision_events`, `enroll_requests`, `device_commands`, `device_heartbeats`,
  `audit_log`. Idempotent migration backfills every existing row into household 1, verified
  against a real pre-migration database.
- `persons.name` uniqueness moved from global to per-household (table rebuild — SQLite can't
  drop a column constraint via ALTER).
- `request.state.household_id` resolved in the auth middleware for both principal types; the
  `_household()` accessor fails **closed** on a principal without one.
- Every scoped read *and* write filtered, including the ones where the filter is the
  authorization check (fact edit/delete, person rename/delete, key minting, user delete/promote
  — all previously IDOR-able by id guess).
- Home Assistant gated on `_owns_smart_home()`; the HA **tools are withheld from the model**
  for a non-owning household, so a demo session has no vocabulary for home control at all.
- `tests/test_households.py`: 23 cross-household tests over the real HTTP stack. Verified
  non-vacuous by removing a `WHERE household_id = ?` and confirming the suite goes red.

Still open from this phase: `ha.py` keeps ONE live connection in module globals, so the
deployment supports one smart home owned by one household (§1). Several *different* real smart
homes needs ha.py made stateless and the router's exemplar cache keyed by household.

**Phase 2/3** (`tests/test_demo.py`, 15 tests):

- `POST /demo/session` mints an isolated household + its admin, seeded with fictional knowledge
  and people so the panels aren't empty. Gated behind `DEMO_PUBLIC_SIGNUP` (**off by default** —
  no deployment starts handing out accounts by upgrading).
- Logout purges the whole household; the TTL sweeper (60 s tick) reclaims abandoned tabs; the
  expiry slides forward on activity, so the TTL measures *idle* time.
- Face embeddings are **destroyed** with a demo household, not unlinked the way `_purge_user`
  treats a departing member of a continuing home — it's biometric data from a member of the
  public.
- Vectors are dropped both by message id and by a `user_id` metadata sweep, so an embedding still
  in the worker queue at purge time can't outlive the session.
- Demo user ids come from `DEMO_USER_ID_BASE` (100 000+) so a purged demo id is never recycled
  into a real account — closing the ChromaDB-residue path noted in §2.

Verified end-to-end against a running server: a visitor sees only seeded fiction, the real
household's private facts do not appear, HA writes 403, a refresh keeps the session, logout
zeroes every table, and the sweeper reclaimed a force-expired household in ~33 s while
household 1 was untouched. Both the sweeper's `is_demo` guard and the logout purge were checked
by sabotage — remove either and the suite goes red.

Throughput is explicitly **not** a gate on this work: the box is slow and known to be slow, and
if demo traffic makes it too slow that's a signal to act on then, not a constraint to design
around now.

---

## 7. Open questions

- Does the public-domain instance currently hold the real household's HA token and face data?
  The design is safe either way, but it determines whether Phase 0 is urgent or merely required.
- Demo TTL: 60 min is a starting guess.
- Should a demo visitor be able to *see* that other households exist (a count) or should the
  system appear single-tenant to them? Recommend the latter.
