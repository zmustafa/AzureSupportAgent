"""Replica-safe persistence for the flat JSON registries.

The app persists many small registries as JSON files on an Azure Files SMB share mounted
at ``/app/.data`` (connectors, workloads, ownership, app settings, …). Every read used to
do a full ``path.read_text()`` + ``json.loads`` and every write a full re-serialize — and
SMB op latency makes those repeated full reads the dominant cost on config-heavy pages.

This helper adds two things WITHOUT changing the on-disk format:

* **Read cache** — parsed values are cached briefly, then revalidated from disk. The bounded
    cache lifetime avoids relying forever on Azure Files timestamp propagation.
* **Atomic writes** — a unique same-directory temporary file is committed with ``os.replace``.
* **Whole-mutation serialization** — :func:`mutate_json` holds one coordination lock across
    read, mutation, and replace. PostgreSQL deployments use a transaction-independent advisory
    lock; SQLite/local deployments use a sidecar OS file lock. The latter coordinates separate
    local processes, while PostgreSQL gives replicas sharing Azure Files one authoritative lock.

Only the short file transaction runs while the advisory-lock connection is held. Callers must
do network, model, and other slow work before entering :func:`mutate_json`.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import queue
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy.engine import make_url

_T = TypeVar("_T")
_CACHE_MAX_AGE_SECONDS = 0.5
_LOCK_TIMEOUT_SECONDS = 30.0
NO_CHANGE = object()

# path(str) -> (checked_monotonic, mtime_ns, size, parsed_object)
_CACHE: dict[str, tuple[float, int, int, Any]] = {}
# path(str) -> lock serializing writes (and the cache update) for that file.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path_str: str) -> threading.RLock:
    lock = _LOCKS.get(path_str)
    if lock is None:
        with _LOCKS_GUARD:
            lock = _LOCKS.get(path_str)
            if lock is None:
                lock = threading.RLock()
                _LOCKS[path_str] = lock
    return lock


def _canonical_key(path: Path) -> str:
    # Replica mount roots are expected to match, but using the data-relative suffix keeps the
    # advisory key stable for local/container paths that expose the same mounted .data folder.
    resolved = path.resolve(strict=False)
    parts = resolved.parts
    try:
        data_index = next(index for index, part in enumerate(parts) if part == ".data")
    except StopIteration:
        return resolved.as_posix().casefold()
    return Path(*parts[data_index:]).as_posix().casefold()


def _advisory_key(path: Path) -> int:
    raw = hashlib.sha256(_canonical_key(path).encode("utf-8")).digest()[:8]
    return int.from_bytes(raw, byteorder="big", signed=True)


def _postgres_connect_kwargs() -> dict[str, Any] | None:
    # Lazy import avoids making the low-level JSON helper part of config module import order.
    from app.core.config import get_settings

    raw = get_settings().resolved_database_url
    if not raw.startswith("postgresql"):
        return None
    url = make_url(raw)
    options: dict[str, Any] = {
        "host": url.host,
        "port": url.port or 5432,
        "database": url.database,
        "user": url.username,
        "password": url.password,
        "timeout": _LOCK_TIMEOUT_SECONDS,
    }
    # ``asyncpg.connect(dsn)`` treats unknown URI query keys as PostgreSQL runtime
    # parameters. Azure's ``?ssl=require`` would therefore fail with
    # CantChangeRuntimeParamError instead of establishing TLS. Pass it through the
    # driver's dedicated argument, exactly as SQLAlchemy's asyncpg dialect does.
    ssl_mode = url.query.get("ssl") or url.query.get("sslmode")
    if ssl_mode:
        options["ssl"] = ssl_mode
    return options


async def _postgres_locked(path: Path, action: Callable[[], _T]) -> _T:
    import asyncpg

    connect_kwargs = _postgres_connect_kwargs()
    if connect_kwargs is None:  # pragma: no cover - guarded by _coordinated
        raise RuntimeError("PostgreSQL coordination requested without PostgreSQL settings")
    connection = await asyncpg.connect(**connect_kwargs)
    key = _advisory_key(path)
    acquired = False
    deadline = asyncio.get_running_loop().time() + _LOCK_TIMEOUT_SECONDS
    try:
        while not acquired:
            acquired = bool(
                await connection.fetchval("SELECT pg_try_advisory_lock($1::bigint)", key)
            )
            if acquired:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Timed out coordinating JSON mutation for {path.name}")
            await asyncio.sleep(0.05)
        return action()
    finally:
        if acquired:
            try:
                await connection.fetchval("SELECT pg_advisory_unlock($1::bigint)", key)
            except Exception:  # noqa: BLE001 - closing the connection also releases the lock
                pass
        await connection.close()


def _run_coroutine(coroutine) -> _T:  # noqa: ANN001
    """Run a short coordinator coroutine from sync registry APIs, including event-loop calls."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            results.put((True, asyncio.run(coroutine)))
        except BaseException as exc:  # noqa: BLE001 - relay to the calling request
            results.put((False, exc))

    thread = threading.Thread(target=_worker, name="jsonstore-postgres-lock", daemon=True)
    thread.start()
    thread.join()
    ok, value = results.get_nowait()
    if not ok:
        raise value
    return value


@contextmanager
def _file_lock(path: Path):
    """Cross-process fallback for SQLite and local development."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _coordinated(path: Path, action: Callable[[], _T]) -> _T:
    key = str(path.resolve(strict=False))
    with _lock_for(key):
        if _postgres_connect_kwargs() is not None:
            return _run_coroutine(_postgres_locked(path, action))
        with _file_lock(path):
            return action()


def _read_uncached(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return copy.deepcopy(default)


def _atomic_write(
    path: Path,
    data: Any,
    *,
    indent: int | None,
    separators: tuple[str, str] | None,
    json_default: Callable[[Any], Any] | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        data, indent=indent, separators=separators, default=json_default
    )
    tmp = path.with_suffix(
        path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
    )
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        stat = path.stat()
        _CACHE[str(path)] = (
            time.monotonic(), stat.st_mtime_ns, stat.st_size, json.loads(payload)
        )
    except OSError:
        _CACHE.pop(str(path), None)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(
        path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
    )
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    _CACHE.pop(str(path), None)


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
    if (
        cached is not None
        and time.monotonic() - cached[0] <= _CACHE_MAX_AGE_SECONDS
        and cached[1] == sig_mtime
        and cached[2] == sig_size
    ):
        return copy.deepcopy(cached[3])
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return copy.deepcopy(default)
    _CACHE[key] = (time.monotonic(), sig_mtime, sig_size, parsed)
    return copy.deepcopy(parsed)


def write_json(
    path: Path,
    data: Any,
    *,
    indent: int | None = 2,
    separators: tuple[str, str] | None = None,
    json_default: Callable[[Any], Any] | None = None,
) -> None:
    """Atomically persist ``data`` as JSON and refresh the read cache (write-through).

    Serialized per-path so two concurrent writers can't interleave; the write is atomic
    (temp file + ``os.replace``) so a reader never sees a half-written file.
    """
    _coordinated(
        path,
        lambda: _atomic_write(
            path,
            data,
            indent=indent,
            separators=separators,
            json_default=json_default,
        ),
    )


def write_bytes(path: Path, payload: bytes) -> None:
    """Atomically replace a binary sidecar under replica-wide coordination."""
    _coordinated(path, lambda: _atomic_write_bytes(path, payload))


def mutate_json(
    path: Path,
    default: Any,
    mutator: Callable[[Any], Any | None],
    *,
    indent: int | None = 2,
    separators: tuple[str, str] | None = None,
    json_default: Callable[[Any], Any] | None = None,
) -> Any:
    """Atomically read, mutate, and replace one JSON document across all replicas.

    ``mutator`` receives a detached current value. It may mutate that value in place and
    return ``None``, or return a replacement document. The persisted document is returned as
    another detached value.
    """

    def _mutate() -> Any:
        current = _read_uncached(path, default)
        replacement = mutator(current)
        if replacement is NO_CHANGE:
            return copy.deepcopy(current)
        persisted = current if replacement is None else replacement
        _atomic_write(
            path,
            persisted,
            indent=indent,
            separators=separators,
            json_default=json_default,
        )
        return copy.deepcopy(persisted)

    return _coordinated(path, _mutate)


def delete_json(path: Path) -> bool:
    """Delete a JSON document under the same replica-wide coordination lock."""

    def _delete() -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            deleted = False
        else:
            deleted = True
        _CACHE.pop(str(path), None)
        return deleted

    return _coordinated(path, _delete)


def invalidate(path: Path) -> None:
    """Drop any cached copy of ``path`` (e.g. after an external delete)."""
    with _lock_for(str(path.resolve(strict=False))):
        _CACHE.pop(str(path), None)
