"""Symmetric encryption for secrets at rest (service-principal secrets, tokens).

Enterprise posture: connection credentials are NEVER stored in plaintext. They are
encrypted with Fernet (AES-128-CBC + HMAC-SHA256) before being written to the
connections registry on disk.

Key resolution order:
1. ``SECRETS_ENCRYPTION_KEY`` env var (a urlsafe base64 32-byte Fernet key) — use this
   in production (mount it from Key Vault / a secret store).
2. A locally generated key persisted to ``backend/.data/secret.key`` (dev convenience).
   The file is created with 0600 perms where the OS supports it.

Rotating the key invalidates previously stored secrets — re-enter them after rotation.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_KEY_PATH = Path(__file__).resolve().parents[2] / ".data" / "secret.key"
_PREFIX = "enc:v1:"


def _load_or_create_key() -> bytes:
    env_key = os.environ.get("SECRETS_ENCRYPTION_KEY", "").strip()
    if env_key:
        # Accept either a raw Fernet key or arbitrary text (derive a key from it).
        try:
            Fernet(env_key.encode("utf-8"))
            return env_key.encode("utf-8")
        except (ValueError, TypeError):
            digest = base64.urlsafe_b64encode(env_key.encode("utf-8").ljust(32)[:32])
            return digest
    if _KEY_PATH.exists():
        return _KEY_PATH.read_text(encoding="utf-8").strip().encode("utf-8")
    # Generate and persist a new key (dev).
    key = Fernet.generate_key()
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KEY_PATH.write_text(key.decode("utf-8"), encoding="utf-8")
    try:
        os.chmod(_KEY_PATH, 0o600)
    except OSError:  # pragma: no cover - non-POSIX
        pass
    return key


_fernet = Fernet(_load_or_create_key())


def encrypt(plaintext: str) -> str:
    """Encrypt a secret. Returns a tagged token string safe to persist as JSON."""
    if plaintext == "":
        return ""
    token = _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt(value: str) -> str:
    """Decrypt a value produced by :func:`encrypt`. Plaintext/empty passes through so
    the function is safe to call on legacy or already-decrypted values."""
    if not value:
        return ""
    if not value.startswith(_PREFIX):
        # Not an encrypted blob (legacy plaintext) — return as-is.
        return value
    raw = value[len(_PREFIX):]
    try:
        return _fernet.decrypt(raw.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(_PREFIX)
