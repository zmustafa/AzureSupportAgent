from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from cryptography.fernet import Fernet

from app.core import crypto


def test_fallback_key_is_single_winner_across_concurrent_starts(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "shared" / "secret.key"
    monkeypatch.setattr(crypto, "_KEY_PATH", key_path)
    monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)

    with ThreadPoolExecutor(max_workers=8) as executor:
        keys = list(executor.map(lambda _index: crypto._load_or_create_key(), range(8)))

    assert len(set(keys)) == 1
    assert key_path.read_bytes().strip() == keys[0]
    Fernet(keys[0])
