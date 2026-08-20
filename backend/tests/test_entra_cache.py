"""Cache behavior: gzipped sidecars, the parse memo, schema versioning and user state.

The two properties that matter operationally:

* **User state survives collection.** A suppression that disappears on the next refresh is
  worse than no suppression at all.
* **An unreadable or outdated payload degrades to "not loaded"**, never to an exception —
  a corrupt cache must not take the page down.
"""
from __future__ import annotations

import gzip
import json

import pytest

from app.entra import cache, model


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path):
    cache.set_root_for_tests(tmp_path / "entra")
    yield
    cache.clear_memo()


def test_domain_roundtrip_is_gzipped():
    payload = model.domain_payload("people", {"users": [{"id": "u1"}]}, item_count=1)
    cache.write_domain("t1", "people", payload)
    path = cache.tenant_dir("t1") / "people.json.gz"
    assert path.exists()
    # Stored compressed, not as plain JSON — tenant payloads are large.
    body = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    assert body["data"]["users"][0]["id"] == "u1"
    assert cache.read_domain("t1", "people")["data"]["users"][0]["id"] == "u1"


def test_memo_avoids_re_decompressing_the_same_file(monkeypatch):
    cache.write_domain("t1", "people", model.domain_payload("people", {"users": []}))
    calls = {"n": 0}
    real = gzip.decompress

    def counting(blob):
        calls["n"] += 1
        return real(blob)

    monkeypatch.setattr(cache.gzip, "decompress", counting)
    cache.clear_memo()
    cache.read_domain("t1", "people")
    cache.read_domain("t1", "people")
    cache.read_domain("t1", "people")
    # rbac/compose.py re-reads and re-gunzips on every request; this module must not.
    assert calls["n"] == 1


def test_writing_invalidates_the_memo():
    cache.write_domain("t1", "people", model.domain_payload("people", {"users": [{"id": "a"}]}))
    assert cache.read_domain("t1", "people")["data"]["users"][0]["id"] == "a"
    cache.write_domain("t1", "people", model.domain_payload("people", {"users": [{"id": "b"}]}))
    assert cache.read_domain("t1", "people")["data"]["users"][0]["id"] == "b"


def test_incompatible_schema_version_reads_as_absent_not_as_an_error():
    cache.write_domain("t1", "people", model.domain_payload("people", {"users": []}))
    path = cache.tenant_dir("t1") / "people.json.gz"
    body = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    body["schema_version"] = cache.SCHEMA_VERSION + 99
    path.write_bytes(gzip.compress(json.dumps(body).encode("utf-8")))
    cache.clear_memo()
    assert cache.read_domain("t1", "people") is None


def test_corrupt_sidecar_reads_as_absent_not_as_an_exception():
    path = cache.tenant_dir("t1") / "people.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a gzip file")
    assert cache.read_domain("t1", "people") is None


def test_index_records_domain_meta():
    cache.write_domain("t1", "apps", model.domain_payload(
        "apps", {}, status=model.STATUS_PARTIAL, item_count=7, truncated=True, notes=["capped"]))
    meta = cache.domain_meta("t1", "apps")
    assert meta["status"] == model.STATUS_PARTIAL
    assert meta["item_count"] == 7
    assert meta["truncated"] is True
    assert meta["notes"] == ["capped"]


def test_blind_payload_names_the_missing_permission():
    payload = model.blind_payload("ca", "Forbidden.", ["Policy.Read.All"])
    assert payload["status"] == model.STATUS_BLIND
    assert payload["missing_permissions"] == ["Policy.Read.All"]
    assert "Policy.Read.All" in model.domain_reason(cache.meta_of(payload), "ca")


def test_state_files_survive_a_collection_run():
    cache.write_state("t1", "findings_state", {"suppressed": ["fp-1"], "breakglass": {"u1": {"confirmed": True}}})
    cache.write_domain("t1", "people", model.domain_payload("people", {"users": []}))
    state = cache.read_state("t1", "findings_state")
    assert state["suppressed"] == ["fp-1"]
    assert state["breakglass"]["u1"]["confirmed"] is True


def test_score_history_is_appended_and_capped():
    for i in range(10):
        cache.append_score_history("t1", {"at": f"2026-01-{i + 1:02d}", "score": i}, cap=5)
    hist = cache.score_history("t1")
    assert len(hist) == 5
    assert [h["score"] for h in hist] == [5, 6, 7, 8, 9]


def test_domain_usable_only_for_readable_statuses():
    assert model.domain_usable({"status": model.STATUS_OK})
    assert model.domain_usable({"status": model.STATUS_PARTIAL})
    assert not model.domain_usable({"status": model.STATUS_BLIND})
    assert not model.domain_usable({"status": model.STATUS_UNLICENSED})
    assert not model.domain_usable({"status": model.STATUS_NOT_COLLECTED})
    assert not model.domain_usable(None)
