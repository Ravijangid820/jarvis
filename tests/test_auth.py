"""Unit tests for the auth primitives (dependency-free: only auth.py is imported,
so these run in CI without the config file or the embedding model)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "orchestrator"))

from auth import hash_password, hash_token, verify_password  # noqa: E402


def test_password_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong password", h)


def test_password_hash_is_salted():
    # Same password hashes differently each time (random salt), but both verify.
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2
    assert verify_password("same", h1) and verify_password("same", h2)


def test_verify_password_rejects_malformed_hash():
    assert not verify_password("anything", "not-a-valid-hash")
    assert not verify_password("anything", "")


def test_hash_token_is_deterministic_and_sha256():
    t = "a" * 64
    assert hash_token(t) == hash_token(t)          # deterministic → DB lookup works
    assert len(hash_token(t)) == 64                 # sha256 hex
    assert hash_token(t) != t                       # never stores the plaintext
    assert hash_token("x") != hash_token("y")       # distinct tokens → distinct hashes
