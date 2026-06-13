"""Local password hashing with Argon2id."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_ph = PasswordHasher()  # argon2id defaults are sound for interactive logins


def hash_password(plaintext: str) -> str:
    return _ph.hash(plaintext)


def verify_password(password_hash: str | None, plaintext: str) -> bool:
    if not password_hash:
        return False
    try:
        return _ph.verify(password_hash, plaintext)
    except (VerifyMismatchError, InvalidHashError, Exception):  # noqa: BLE001
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(password_hash)
    except Exception:  # noqa: BLE001
        return False
