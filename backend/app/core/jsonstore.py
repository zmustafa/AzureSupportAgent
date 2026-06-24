"""In-process, mtime-validated cache for the flat JSON registries.

The app persists many small registries as JSON files on an Azure Files SMB share mounted
at ``/app/.data`` (connectors, workloads, ownership, app settings, …). Every read used to
do a full ``path.read_text()`` + ``json.loads`` and every write a full re-serialize — and
SMB op latency makes those repeated full reads the dominant cost on config-heavy pages.

This helper adds two things WITHOUT changing the on-disk format:

* **Read cache** — the parsed object is cached in process memory, keyed by absolute path,
  and reused until the file's ``mtime``/``size`` changes (so an out-of-band edit, or another
  process on the shared volume, is still picked up). On a cache hit there is **no disk I/O**.
* **Atomic, serialized writes** — writes go to a temp file then ``os.replace`` (atomic on
  the same filesystem), guarded by a per-path lock so a concurrent read-modify-write from two
  requests can't interleave into a torn/last-writer-wins file. The cache is refreshed
  write-through so the next read is served from memory.

NOTE (multi-replica): the cache is per-process. Today the live app runs a single replica, so
this is correct and simplest. If the app is ever scaled to multiple replicas sharing the same
Azure Files volume, the mtime check still bounds staleness (a write by replica B bumps the
mtime, which replica A notices on its next read), but writes from different replicas should be
treated as last-writer-wins — move to a shared store (Redis/DB) if strong consistency is needed.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

# path(str) -> (mtime_ns, size, parsed_object)
_CACHE: dict[str, tuple[int, int, Any]] = {}
# path(str) -> lock serializing writes (and the cache update) for that file.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path_str: str) -> threading.Lock:
    lock = _LOCKS.get(path_str)
    if lock is None:
        with _LOCKS_GUARD:
            lock = _LOCKS.get(path_str)
            if lock is None:
                lock = threading.Lock()
                _LOCKS[path_str] = lock
    return lock


def read_json(path: Path, default: Any) -> Any:
    """Return the parsed JSON at ``path``, served from an mtime-validated in-memory cache.

    On a missing/corrupt file (or any read error) a deep copy of ``default`` is returned and
    nothing is cached (so a transient error doesn't poison the cache). The returned value is a
    **deep copy** so callers can mutate it freely without corrupting the cached object.
    """
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return copy.deepcopy(default)
    sig_mtime, sig_size = st.st_mtime_ns, st.st_size
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == sig_mtime and cached[1] == sig_size:
        return copy.deepcopy(cached[2])
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return copy.deepcopy(default)
    _CACHE[key] = (sig_mtime, sig_size, parsed)
    return copy.deepcopy(parsed)


def write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    """Atomically persist ``data`` as JSON and refresh the read cache (write-through).

    Serialized per-path so two concurrent writers can't interleave; the write is atomic
    (temp file + ``os.replace``) so a reader never sees a half-written file.
    """
    key = str(path)
    with _lock_for(key):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=indent)
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        try:
            st = path.stat()
            _CACHE[key] = (st.st_mtime_ns, st.st_size, json.loads(payload))
        except OSError:
            _CACHE.pop(key, None)


def invalidate(path: Path) -> None:
    """Drop any cached copy of ``path`` (e.g. after an external delete)."""
    _CACHE.pop(str(path), None)
