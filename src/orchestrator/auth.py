"""Password hashing/verification (PBKDF2-HMAC-SHA256).

Kept dependency-free so it matches src/scripts/manage.py and is easy to unit-test.

New hashes use the `pbkdf2_sha256$<iters>$<salt>$<hex>` format with PBKDF2_ITERATIONS.
verify_password also accepts the legacy `<salt>:<hex>` format (100k iterations) so existing
users keep working — they're transparently re-hashed at the higher cost on next password change.
"""
import hashlib
import secrets

PBKDF2_ITERATIONS = 600_000   # OWASP-current floor for PBKDF2-HMAC-SHA256
_LEGACY_ITERATIONS = 100_000  # original format: "<salt>:<hex>"


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        if password_hash.startswith("pbkdf2_sha256$"):
            _, iters_s, salt, key = password_hash.split("$")
            iters = int(iters_s)
        elif ":" in password_hash:
            salt, key = password_hash.split(":")
            iters = _LEGACY_ITERATIONS
        else:
            return False
        new_key = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), iters)
        return secrets.compare_digest(key, new_key.hex())
    except Exception:
        return False


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${key.hex()}"


def hash_token(token: str) -> str:
    """Hash a bearer/session token for storage at rest.

    Plain SHA-256 (not PBKDF2) is appropriate here: tokens are already 256 bits of
    secrets.token_hex entropy, so they aren't brute-forceable and need no salt/stretching.
    Storing only the hash means a DB/backup leak no longer yields usable live tokens.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
