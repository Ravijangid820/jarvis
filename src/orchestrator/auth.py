"""Password hashing/verification (PBKDF2-HMAC-SHA256, 100k iterations).

Kept dependency-free so it matches src/scripts/manage.py and is easy to unit-test.
"""
import hashlib
import secrets


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        salt, key = password_hash.split(":")
        new_key = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), 100000)
        return secrets.compare_digest(key, new_key.hex())
    except Exception:
        return False


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return f"{salt}:{key.hex()}"
