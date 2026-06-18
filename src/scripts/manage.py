#!/usr/bin/env python3
"""Local admin CLI for Jarvis — the recovery path that replaces the master API key.

Run on the box (it talks straight to the SQLite DB; the orchestrator may be running).

    uv run python src/scripts/manage.py list-users
    uv run python src/scripts/manage.py create-admin <username> <password>
    uv run python src/scripts/manage.py reset-password <username> <password>
    uv run python src/scripts/manage.py mint-key <username> [description] [device_id]   # prints a new API key

Password hashing matches the orchestrator (PBKDF2-HMAC-SHA256, 100k iterations).
"""
import hashlib
import json
import secrets
import sqlite3
import sys
from pathlib import Path

PBKDF2_ITERATIONS = 600_000   # keep in sync with src/orchestrator/auth.py

CONFIG = json.loads(Path("/srv/jarvis/config/jarvis.json").read_text())
DB_PATH = CONFIG["memory"]["db_path"]


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${key.hex()}"


def list_users():
    with _db() as c:
        rows = c.execute("SELECT id, username, role, created_at FROM users ORDER BY id").fetchall()
        for r in rows:
            print(f"  #{r['id']:<3} {r['username']:<20} {r['role']:<8} {r['created_at']}")
        if not rows:
            print("  (no users)")


def create_admin(username, password):
    with _db() as c:
        try:
            c.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                (username, hash_password(password)),
            )
        except sqlite3.IntegrityError:
            sys.exit(f"User '{username}' already exists (use reset-password).")
    print(f"Created admin user '{username}'.")


def reset_password(username, password):
    with _db() as c:
        cur = c.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (hash_password(password), username),
        )
        if cur.rowcount == 0:
            sys.exit(f"No such user '{username}'.")
    print(f"Password reset for '{username}'.")


def mint_key(username, description="cli-minted", device_id=None):
    """Mint an API key. If device_id is given, the key is BOUND to that device: it may
    only pull commands for / post events as that device (enforced by the orchestrator)."""
    with _db() as c:
        row = c.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            sys.exit(f"No such user '{username}'.")
        key = "jk-" + secrets.token_hex(16)
        # Store only the SHA-256 hash + a short display prefix (matches the orchestrator).
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        c.execute(
            "INSERT INTO api_keys (key_string, key_prefix, user_id, description, device_id) VALUES (?, ?, ?, ?, ?)",
            (key_hash, key[:10], row["id"], description, (device_id or None)),  # "" → NULL, so it can't match an empty ?device=
        )
    print(key)  # printed alone so it's easy to capture: KEY=$(... mint-key ...)


def main(argv):
    if not argv:
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    if cmd == "list-users":
        list_users()
    elif cmd == "create-admin" and len(rest) == 2:
        create_admin(*rest)
    elif cmd == "reset-password" and len(rest) == 2:
        reset_password(*rest)
    elif cmd == "mint-key" and len(rest) >= 1:
        mint_key(rest[0],
                 rest[1] if len(rest) > 1 else "cli-minted",
                 rest[2] if len(rest) > 2 else None)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
