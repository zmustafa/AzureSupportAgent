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
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_KEY_PATH = Path(__file__).resolve().parents[2] / ".data" / "secret.key"
_PREFIX = "enc:v1:"

# When SECRETS_ENCRYPTION_KEY is a passphrase (not a raw 32-byte urlsafe-b64 Fernet key)
# we derive the key with PBKDF2-HMAC-SHA256 instead of the old pad/truncate, which had
# almost no work factor and let a short passphrase map to a trivially low-entropy key.
# The salt is a FIXED application constant (not secret — it only pins the derivation so
# the same passphrase always yields the same key); confidentiality comes from the
# passphrase + iteration count, not the salt.
_KDF_SALT = b"aznetagent.secrets.fernet.v1"
_KDF_ITERATIONS = 480_000


def _derive_fernet_key(passphrase: str) -> bytes:
    """Derive a urlsafe-b64 32-byte Fernet key from an arbitrary passphrase via PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_KDF_SALT,
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _load_or_create_key() -> bytes:
    env_key = os.environ.get("SECRETS_ENCRYPTION_KEY", "").strip()
    if env_key:
        # Accept either a raw Fernet key or an arbitrary passphrase (KDF-derived).
        try:
            Fernet(env_key.encode("utf-8"))
            return env_key.encode("utf-8")
        except (ValueError, TypeError):
            return _derive_fernet_key(env_key)
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
