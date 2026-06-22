"""Validated, identity-bound host command execution with live streaming.

The Run button lets the agent's suggested Azure-CLI commands actually execute on the
host, bound to the same Azure identity the chat's MCP session uses. Because the command
text originates from an LLM it is treated as untrusted: we allow only an explicit set of
CLI binaries, reject every shell metacharacter (no chaining/redirection/subshells), and
gate mutating verbs behind an approval click. Output streams back token-by-token over SSE.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.core.app_settings import load_settings

# Shell metacharacters that enable chaining, redirection, subshells, or globbing tricks.
# Any of these in the raw command string is an immediate rejection.
# Shell metacharacters that enable chaining, redirection, or subshells. We scan for
# these only OUTSIDE quoted strings — inside quotes (e.g. a KQL `-q "... | project ..."`
# argument) they are literal data, and since we run via exec (no shell) they are never
# interpreted. Newlines are always rejected (a code block must be a single command).
_FORBIDDEN_OPERATORS = (";", "|", "&", ">", "<", "`")


def _has_unquoted_shell_operator(raw: str) -> bool:
    """True if a shell operator or command-substitution appears outside quotes."""
    in_single = False
    in_double = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == "\\" and i + 1 < n:
                i += 2  # backslash escape inside double quotes
                continue
            if ch == '"':
                in_double = False
            elif ch == "`":  # backtick command substitution is active in double quotes
                return True
            elif ch == "$" and i + 1 < n and raw[i + 1] == "(":
                return True
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch in _FORBIDDEN_OPERATORS:
                return True
            elif ch == "$" and i + 1 < n and raw[i + 1] == "(":
                return True
        i += 1
    return False

# Mutating verbs across az / azd / kubectl. Presence (as a bare token) marks a command
# "destructive", which requires an explicit confirm and is blocked in read-only tenants.
_MUTATING_VERBS = {
    # az
    "create", "delete", "update", "set", "add", "remove", "purge", "restart", "start",
    "stop", "deallocate", "redeploy", "reset", "regenerate", "rotate", "renew", "assign",
    "unassign", "grant", "revoke", "disable", "enable", "import", "move", "attach",
    "detach", "approve", "reject", "deny", "cancel", "clear", "restore", "failover",
    "lock", "unlock", "invoke-action", "generate", "upload", "publish",
    # azd
    "up", "down", "provision", "deploy", "destroy",
    # kubectl
    "apply", "patch", "edit", "scale", "drain", "cordon", "uncordon", "rollout",
    "replace", "label", "annotate", "exec", "run", "expose", "taint",
}

MAX_OUTPUT_BYTES = 256_000  # truncate runaway output to protect the browser/feed
# A much larger cap for server-side Resource Graph captures that are PARSED (not streamed to the
# browser). A single subscription's worth of resources (≤1000 rows with tags) can far exceed the
# 256 KB browser cap; truncating it produces invalid JSON that parsers turn into a misleading
# "0 resources". Resource-graph collectors pass this so the data actually comes through.
KQL_RESOURCE_CAPTURE_BYTES = 12_000_000


def parse_kql_rows(stdout: str) -> list[dict[str, Any]]:
    """Parse Resource Graph / CLI JSON output into a row list, SALVAGING a result that the output
    cap truncated mid-array. ``az graph query --query data[]`` and the REST path both emit a JSON
    array; a truncation cuts the final element, which plain ``json.loads`` rejects — turning a big
    result into an empty one. Here we recover every COMPLETE object that arrived before the cut.

    Returns ``[]`` only for genuinely empty / unparseable output."""
    if not stdout:
        return []
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            data = data.get("data") or data.get("value") or []
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        pass
    # Salvage a truncated array: pull complete top-level objects until the incomplete tail.
    s = stdout.lstrip()
    if not s.startswith("["):
        return []
    decoder = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    i, n = 1, len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n,":
            i += 1
        if i >= n or s[i] == "]":
            break
        try:
            obj, end = decoder.raw_decode(s, i)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


@dataclass
class Validation:
    ok: bool
    binary: str = ""
    argv: list[str] = field(default_factory=list)
    destructive: bool = False
    error: str = ""


def validate_command(command: str, allowlist: list[str]) -> Validation:
    """Parse and safety-check a command. Returns argv + whether it's destructive."""
    raw = (command or "").strip()
    if not raw:
        return Validation(ok=False, error="Empty command.")
    if len(raw) > 4000:
        return Validation(ok=False, error="Command is too long.")
    if "\n" in raw or "\r" in raw:
        return Validation(
            ok=False,
            error="Only a single-line command is allowed — run one command per code block.",
        )
    if _has_unquoted_shell_operator(raw):
        return Validation(
            ok=False,
            error=(
                "Only a single command is allowed — shell operators, pipes, "
                "redirection and subshells are blocked. (Quotes around query text are fine.)"
            ),
        )
    try:
        argv = shlex.split(raw, posix=True)
    except ValueError:
        return Validation(ok=False, error="Could not parse the command (unbalanced quotes?).")
    if not argv:
        return Validation(ok=False, error="Empty command.")
    binary = argv[0].lower()
    if binary not in {b.lower() for b in allowlist}:
        allowed = ", ".join(allowlist) or "(none)"
        return Validation(
            ok=False,
            error=f"'{argv[0]}' is not an allowed command. Allowed: {allowed}.",
        )
    bare = {t.lower() for t in argv[1:] if not t.startswith("-")}
    destructive = bool(bare & _MUTATING_VERBS)
    return Validation(ok=True, binary=binary, argv=argv, destructive=destructive)


def _run_env(conn: dict[str, Any] | None, config_dir: str | None) -> dict[str, str]:
    """Environment for the actual command run (no secrets — auth is via the CLI's own
    login context / config dir)."""
    env = dict(os.environ)
    # Never expose SP secrets to the executed command's environment.
    for k in ("AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH"):
        env.pop(k, None)
    if conn:
        if conn.get("tenant_id"):
            env["AZURE_TENANT_ID"] = conn["tenant_id"]
        if conn.get("default_subscription"):
            env["AZURE_SUBSCRIPTION_ID"] = conn["default_subscription"]
    if config_dir:
        env["AZURE_CONFIG_DIR"] = config_dir
    # Make az emit plain, non-paged output.
    env["AZURE_CORE_NO_COLOR"] = "true"
    env["AZURE_CORE_ONLY_SHOW_ERRORS"] = "false"
    # Auto-install any missing CLI extension (e.g. resource-graph for `az graph query`)
    # without an interactive prompt — otherwise the command hangs waiting on a TTY we
    # don't have and eventually times out.
    env["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] = "yes_without_prompt"
    env["AZURE_CORE_DISABLE_CONFIRM_PROMPT"] = "true"
    return env


def _is_service_principal(conn: dict[str, Any] | None) -> bool:
    return bool(conn) and conn.get("auth_method") in (
        "service_principal",
        "service_principal_cert",
    )


# --------------------------------------------------------- non-SP ARM-REST data bridge
# A non-service-principal connection (pasted ARM token / managed identity) has NO ambient
# `az login` for its tenant, so an `az` CLI data read silently fails or returns nothing for it
# — even when the connection's identity has valid Azure access. (This is the class of bug that
# made the Change Explorer Activity Log, Perf Profiler metrics, etc. come back empty on a
# pasted-token connection.) Resource Graph already dodges this by branching to ARM REST with
# the connection's own token; these helpers extend the SAME pattern to every other read path.
#
# Decision (mirrors run_kql_stream): non-SP → acquire an ARM token; if we get one, run the read
# over REST; if we CAN'T and there's no ambient `az` (az_cli_token / managed identity), FAIL
# CLOSED with a clear message — NEVER let a CLI silently return zero. Audiences a pasted ARM
# token can't serve (Log Analytics / App Insights / Key Vault data-plane) also fail closed.
async def _arm_rest_mode(conn: dict[str, Any] | None) -> tuple[str, str | None, str]:
    """Decide how a connection should run a read-only Azure data call:

    - ``("cli", None, "")``      service principal, or local dev with ambient ``az`` — run the CLI.
    - ``("rest", token, "")``    non-SP with an acquirable ARM token — run it over ARM REST.
    - ``("error", None, msg)``   non-SP that can't get a token and has no ambient ``az`` — fail closed.
    """
    if _is_service_principal(conn):
        return ("cli", None, "")
    from app.azure.credentials import get_arm_token

    token, terr = await get_arm_token(conn) if conn else (None, "no connection")
    if token:
        return ("rest", token, "")
    method = (conn or {}).get("auth_method", "")
    if method == "az_cli_token" or os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"):
        return ("error", None, terr or "Could not acquire an Azure token for this connection.")
    return ("cli", None, "")


def _az_argv_audience(argv_tail: list[str]) -> str:
    """Classify the token AUDIENCE a read-only ``az`` argv needs, so a non-SP connection can be
    routed (ARM → REST) or failed closed with the right message (LA/App Insights/Key Vault can't
    be served by a pasted ARM token)."""
    a = [str(x).lower() for x in (argv_tail or [])]
    if "log-analytics" in a:
        return "log_analytics"
    if "app-insights" in a:
        return "app_insights"
    if "keyvault" in a:
        return "key_vault"
    if a[:1] == ["rest"]:
        # `az rest --url <u>`: ARM only when the url targets management.azure.com.
        from app.azure.arm import is_arm_url

        try:
            url = argv_tail[argv_tail.index("--url") + 1]
        except (ValueError, IndexError):
            url = ""
        return "arm" if is_arm_url(url) else "other"
    return "arm"


def _non_sp_unavailable_msg(audience: str, terr: str = "") -> str:
    """A clear, actionable fail-closed message for a read a non-SP connection can't perform."""
    if audience == "log_analytics":
        return ("Log Analytics queries need a service-principal connection (the pasted-token / "
                "managed-identity connection can't sign the Azure CLI into Log Analytics).")
    if audience == "app_insights":
        return ("Application Insights queries need a service-principal connection (the pasted-token / "
                "managed-identity connection can't sign the Azure CLI into App Insights).")
    if audience == "key_vault":
        return ("Key Vault data-plane reads need a service-principal connection (a pasted ARM token "
                "can't authenticate the Key Vault data plane).")
    if terr:
        return f"This Azure read needs a connection token: {terr}"
    return ("This Azure read isn't available for a pasted-token / managed-identity connection — "
            "use a service-principal connection (the Azure CLI can't sign into this tenant without one).")


async def _arm_rest_for_argv(argv_tail: list[str], token: str) -> tuple[bool, str, str | None]:
    """Translate a recognized read-only ARM ``az`` argv to an ARM REST call with ``token``.

    Returns ``(handled, json_text, error)``. ``handled`` is False when the argv isn't a
    known ARM read (caller then fails closed / falls through). The ``json_text`` is shaped to
    match the corresponding ``az`` command's stdout so existing parsers consume it unchanged."""
    from app.azure import arm

    a = [str(x) for x in (argv_tail or [])]
    low = [x.lower() for x in a]

    def _opt(name: str) -> str:
        try:
            return a[low.index(name) + 1]
        except (ValueError, IndexError):
            return ""

    # az monitor diagnostic-settings list --resource <id>
    if low[:3] == ["monitor", "diagnostic-settings", "list"]:
        rid = _opt("--resource")
        if rid:
            text, err = await arm.get_diagnostic_settings(token, rid)
            return True, text, err
    # az monitor metrics list-definitions --resource <id>
    if low[:3] == ["monitor", "metrics", "list-definitions"]:
        rid = _opt("--resource")
        if rid:
            text, err = await arm.get_metric_definitions(token, rid)
            return True, text, err
    # az rest --method <m> --url <ARM url> [--body @file | --body <json>]
    if low[:1] == ["rest"]:
        url = _opt("--url")
        if url and arm.is_arm_url(url):
            method = (_opt("--method") or "get")
            body = _read_az_rest_body(_opt("--body"))
            text, err = await arm.arm_rest(token, method, url, body)
            return True, text, err
    return False, "", None


def _read_az_rest_body(raw: str) -> dict[str, Any] | None:
    """Parse an ``az rest --body`` value: ``@file`` reads JSON from that file; otherwise the
    value is treated as an inline JSON string. Returns None when empty/unparseable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("@"):
            with open(raw[1:].lstrip("@"), encoding="utf-8") as fh:
                return json.load(fh)
        return json.loads(raw)
    except (OSError, ValueError):
        return None


def _thread_reader(
    pipe: Any,
    kind: str,
    queue: "asyncio.Queue",
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Blocking reader run in a worker thread: read a process pipe line-by-line and
    forward each decoded line onto the asyncio queue (thread-safe). Ends with a None
    sentinel. Used instead of asyncio subprocess streams so command execution works on
    ANY event loop — notably the Windows SelectorEventLoop, where asyncio subprocesses
    raise NotImplementedError."""
    try:
        for raw in iter(pipe.readline, b""):
            text = raw.decode("utf-8", errors="replace")
            loop.call_soon_threadsafe(queue.put_nowait, (kind, text))
    except Exception:  # noqa: BLE001 - a broken pipe just ends this stream
        pass
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, None))  # stream done
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass


async def _sp_login(conn: dict[str, Any], az_path: str, config_dir: str) -> str | None:
    """Ephemeral `az login --service-principal` into an isolated config dir.

    Returns an error string on failure, or None on success. The SP secret/cert never
    persists outside this throwaway config dir.
    """
    tenant = conn.get("tenant_id", "")
    client_id = conn.get("client_id", "")
    if not (tenant and client_id):
        return "Service-principal connection is missing tenant or client id."
    argv = [az_path, "login", "--service-principal", "-u", client_id, "--tenant", tenant, "--only-show-errors"]
    cleanup: list[str] = []
    if conn.get("auth_method") == "service_principal_cert":
        pem = conn.get("certificate_pem", "")
        if not pem:
            return "Certificate connection is missing its PEM."
        fd = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, encoding="utf-8")
        fd.write(pem)
        fd.close()
        cleanup.append(fd.name)
        argv += ["-p", fd.name]
    else:
        secret = conn.get("client_secret", "")
        if not secret:
            return "Service-principal connection is missing its secret."
        argv += ["-p", secret]
    env = dict(os.environ)
    env["AZURE_CONFIG_DIR"] = config_dir
    try:
        # Run via the blocking subprocess module in a worker thread so this works on any
        # event loop (the Windows SelectorEventLoop can't spawn asyncio subprocesses).
        result = await asyncio.to_thread(
            subprocess.run,
            argv,
            capture_output=True,
            env=env,
            timeout=60,
        )
        if result.returncode != 0:
            msg = (
                result.stderr.decode("utf-8", errors="replace")[:300]
                if result.stderr
                else "login failed"
            )
            return f"Service-principal sign-in failed: {msg}"
        return None
    except subprocess.TimeoutExpired:
        return "Service-principal sign-in timed out."
    finally:
        for p in cleanup:
            try:
                os.unlink(p)
            except OSError:
                pass


async def _stream_process(
    run_argv: list[str],
    env: dict[str, str],
    timeout: int,
    *,
    label: str,
    destructive: bool,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> AsyncIterator[dict[str, Any]]:
    """Spawn a process and stream its stdout/stderr as SSE-ready events."""
    yield {"type": "exec_start", "command": label, "destructive": destructive}
    started = time.monotonic()
    loop = asyncio.get_running_loop()
    # Use the blocking subprocess module (in threads) rather than asyncio subprocesses,
    # so this works on any event loop — including the Windows SelectorEventLoop, where
    # asyncio.create_subprocess_exec raises NotImplementedError.
    proc = await asyncio.to_thread(
        subprocess.Popen,
        run_argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdout and proc.stderr
    queue: asyncio.Queue = asyncio.Queue()
    pumps = [
        threading.Thread(
            target=_thread_reader, args=(proc.stdout, "stdout", queue, loop), daemon=True
        ),
        threading.Thread(
            target=_thread_reader, args=(proc.stderr, "stderr", queue, loop), daemon=True
        ),
    ]
    for t in pumps:
        t.start()
    finished = 0
    total = 0
    truncated = False
    timed_out = False
    while finished < 2:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            timed_out = True
            break
        try:
            kind, text = await asyncio.wait_for(queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            timed_out = True
            break
        if text is None:
            finished += 1
            continue
        total += len(text)
        if total > max_bytes:
            truncated = True
            break
        yield {"type": kind, "text": text}

    if timed_out or truncated:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        reason = (
            f"Command timed out after {timeout}s." if timed_out
            else f"Output truncated at {max_bytes // 1000} KB."
        )
        yield {"type": "error", "message": reason}
        try:
            await asyncio.to_thread(proc.wait, 5)
        except subprocess.TimeoutExpired:
            pass
        return

    await asyncio.to_thread(proc.wait)
    duration_ms = int((time.monotonic() - started) * 1000)
    yield {"type": "exit", "code": proc.returncode, "duration_ms": duration_ms}


async def run_command_stream(
    command: str,
    connection: dict[str, Any] | None,
    *,
    read_only: bool,
    confirm: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Validate, optionally authenticate, then run a command and yield SSE-ready events.

    Event shapes (``type`` field): ``exec_start``, ``status``, ``stdout``, ``stderr``,
    ``approval_required``, ``exit`` (code/duration_ms), ``error``.
    """
    settings = load_settings()
    if not settings.get("command_execution_enabled", False):
        yield {"type": "error", "message": "Command execution is disabled by the administrator."}
        return
    allowlist = settings.get("command_allowlist") or ["az"]

    val = validate_command(command, allowlist)
    if not val.ok:
        yield {"type": "error", "message": val.error}
        return

    if val.destructive:
        if read_only:
            yield {
                "type": "error",
                "message": (
                    "This is a mutating command, but the selected Azure connection is "
                    "read-only. Switch to a writable connection or run a read-only command."
                ),
            }
            return
        if not confirm:
            yield {
                "type": "approval_required",
                "command": command,
                "message": "This command modifies Azure resources. Run it anyway?",
            }
            return

    az_path = shutil.which(val.binary)
    if not az_path:
        yield {"type": "error", "message": f"'{val.binary}' is not installed on the host."}
        return

    timeout = int(settings.get("command_timeout_seconds", 120))
    config_dir: str | None = None
    sp = _is_service_principal(connection)
    try:
        if sp:
            config_dir = tempfile.mkdtemp(prefix="azexec-")
            yield {"type": "status", "text": "Authenticating with the service principal…"}
            err = await _sp_login(connection, az_path, config_dir)
            if err:
                yield {"type": "error", "message": err}
                return

        env = _run_env(connection, config_dir)
        # argv[0] is the binary name; replace with the resolved absolute path.
        run_argv = [az_path, *val.argv[1:]]
        async for ev in _stream_process(
            run_argv, env, timeout, label=command, destructive=val.destructive
        ):
            yield ev
    finally:
        if config_dir:
            shutil.rmtree(config_dir, ignore_errors=True)


# Max rows a single Resource Graph query run returns (protects the browser/feed).
KQL_MAX_ROWS = 1000


def _format_rows_table(rows: list[dict[str, Any]]) -> str:
    """Render Resource Graph rows as a simple aligned table, mimicking
    ``az ... --output table`` so the REST path matches the CLI path for the chat KQL
    Run view. JSON callers get raw JSON instead (see run_kql_stream)."""
    import json as _json

    if not rows:
        return ""
    cols: list[str] = []
    for r in rows:
        if isinstance(r, dict):
            for k in r.keys():
                if k not in cols:
                    cols.append(k)
    if not cols:
        return ""

    def cell(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return _json.dumps(v, separators=(",", ":"))
        return str(v)

    widths = {c: len(c) for c in cols}
    for r in rows:
        for c in cols:
            widths[c] = max(widths[c], len(cell(r.get(c) if isinstance(r, dict) else "")))
    lines = [
        "  ".join(c.ljust(widths[c]) for c in cols),
        "  ".join("-" * widths[c] for c in cols),
    ]
    for r in rows:
        lines.append("  ".join(cell(r.get(c) if isinstance(r, dict) else "").ljust(widths[c]) for c in cols))
    return "\n".join(lines)


async def run_kql_stream(
    kql: str,
    connection: dict[str, Any] | None,
    *,
    output: str = "table",
    session_config_dir: str | None = None,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> AsyncIterator[dict[str, Any]]:
    """Run a KQL query via Azure Resource Graph and stream results.

    Two execution paths share this entry point:

    - **Service-principal** connections run ``az graph query`` after an (optionally
      reused) ``az login --service-principal``. The KQL is passed to ``az`` as a single
      argv element (data, never a shell string), so there is no injection surface.
    - **All other** connections (managed identity / host identity / pasted token) have no
      ambient ``az login`` — notably in Azure Container Apps — so they run Resource Graph
      over ARM REST using the connection's own token. This is what makes discovery work
      under a managed identity.

    Resource Graph is a read-only query API, so this is always non-destructive.

    ``session_config_dir``: when provided (and the connection is a service principal),
    reuse this already-authenticated AZURE_CONFIG_DIR instead of doing a fresh ``az login``
    per query — a big speedup when running many queries (e.g. cache prefetch). The caller
    owns that dir's lifecycle.
    """
    settings = load_settings()

    query = (kql or "").strip()
    if not query:
        yield {"type": "error", "message": "Empty query."}
        return
    if len(query) > 8000:
        yield {"type": "error", "message": "Query is too long."}
        return
    # Collapse to a single line: on Windows `az` is a batch wrapper (az.cmd) that
    # truncates an argument at the first newline, which would silently drop everything
    # after the first KQL line. KQL is whitespace-insensitive between tokens, so a
    # one-line form is equivalent. Also strip `// ...` line comments first.
    query = re.sub(r"//[^\n]*", "", query)
    query = re.sub(r"\s*\n\s*", " ", query).strip()
    query = re.sub(r"[ \t]{2,}", " ", query)

    timeout = int(settings.get("command_timeout_seconds", 120))
    sp = _is_service_principal(connection)

    # --- REST path: non-service-principal connections (managed identity / pasted token).
    # These have no ambient `az login` in the cloud, so `az graph query` would return
    # nothing. Use the connection's ARM token to query Resource Graph directly.
    if not sp:
        from app.azure.arm import query_resource_graph
        from app.azure.credentials import get_arm_token

        token, terr = await get_arm_token(connection)
        if token:
            yield {"type": "exec_start", "command": "resource-graph (REST)", "destructive": False}
            start = time.time()
            rows, qerr = await query_resource_graph(token, query, top=KQL_MAX_ROWS)
            duration_ms = int((time.time() - start) * 1000)
            if qerr:
                yield {"type": "error", "message": qerr}
                yield {"type": "exit", "code": 1, "duration_ms": duration_ms}
                return
            import json as _json

            text = _json.dumps(rows) if output == "json" else _format_rows_table(rows)
            yield {"type": "stdout", "text": text}
            yield {"type": "exit", "code": 0, "duration_ms": duration_ms}
            return
        # No token. For pasted-token or managed-identity environments there is no ambient
        # `az` to fall back to, so surface the auth error instead of silently returning
        # empty. Pure local dev (default_chain, no managed identity) falls through to the
        # ambient `az graph query` path below.
        method = (connection or {}).get("auth_method", "")
        if method == "az_cli_token" or os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"):
            yield {"type": "error", "message": terr or "Could not acquire an Azure token for this connection."}
            return

    # --- CLI path: service principals (and local ambient `az` fallback). ----------------
    az_path = shutil.which("az")
    if not az_path:
        yield {"type": "error", "message": "'az' is not installed on the host."}
        return
    config_dir: str | None = None
    own_config = False
    try:
        if sp:
            if session_config_dir:
                # Reuse a pre-authenticated session (the caller logged in once and will
                # clean up) — skips the slow per-query `az login`.
                config_dir = session_config_dir
            else:
                config_dir = tempfile.mkdtemp(prefix="azexec-")
                own_config = True
                yield {"type": "status", "text": "Authenticating with the service principal…"}
                err = await _sp_login(connection, az_path, config_dir)
                if err:
                    yield {"type": "error", "message": err}
                    return

        env = _run_env(connection, config_dir)
        run_argv = [
            az_path, "graph", "query",
            "-q", query,
            "--first", str(KQL_MAX_ROWS),
            # The graph result is a wrapper {count, data, ...}; extract just the rows so
            # `--output table` renders the projected columns instead of a count summary.
            "--query", "data[]",
            "--output", output,
        ]
        label = f"az graph query (KQL, first {KQL_MAX_ROWS})"
        async for ev in _stream_process(run_argv, env, timeout, label=label, destructive=False, max_bytes=max_bytes):
            yield ev
    finally:
        # Only remove a config dir we created here; a shared session dir is the caller's.
        if config_dir and own_config:
            shutil.rmtree(config_dir, ignore_errors=True)


@dataclass
class CaptureResult:
    """The full, captured result of a (non-streamed) command/KQL run."""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_ms: int | None = None
    error: str = ""
    destructive: bool = False


async def _capture(stream: AsyncIterator[dict[str, Any]], *, max_bytes: int = MAX_OUTPUT_BYTES) -> CaptureResult:
    """Drain a run_*_stream generator into a single captured result.

    ``max_bytes`` caps the captured stdout. The default protects the browser/feed; server-side
    callers that parse the result (and aren't streaming it to a client) may pass a larger cap so
    a big-but-legitimate result isn't truncated into invalid JSON."""
    out: list[str] = []
    errparts: list[str] = []
    res = CaptureResult(ok=False)
    async for ev in stream:
        kind = ev.get("type")
        if kind == "exec_start":
            res.destructive = bool(ev.get("destructive"))
        elif kind == "stdout":
            out.append(ev.get("text", ""))
        elif kind == "stderr":
            errparts.append(ev.get("text", ""))
        elif kind == "approval_required":
            res.error = "This workbook is a mutating command and requires confirmation."
            res.ok = False
            return res
        elif kind == "error":
            res.error = ev.get("message", "Command failed.")
        elif kind == "exit":
            res.exit_code = ev.get("code")
            res.duration_ms = ev.get("duration_ms")
    res.stdout = "".join(out)[:max_bytes]
    res.stderr = "".join(errparts)[:8000]
    if res.error:
        res.ok = False
    else:
        res.ok = res.exit_code == 0
        if not res.ok and not res.error:
            res.error = res.stderr.strip()[:500] or f"Exited with code {res.exit_code}."
    return res


async def run_command_capture(
    command: str,
    connection: dict[str, Any] | None,
    *,
    read_only: bool,
    confirm: bool = False,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> CaptureResult:
    """Run an allowlisted command and capture its full output (non-streaming)."""
    return await _capture(
        run_command_stream(command, connection, read_only=read_only, confirm=confirm),
        max_bytes=max_bytes,
    )


async def run_kql_capture(
    kql: str,
    connection: dict[str, Any] | None,
    *,
    output: str = "json",
    session_config_dir: str | None = None,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> CaptureResult:
    """Run a Resource Graph (KQL) query and capture its full output (non-streaming).

    ``session_config_dir`` reuses a pre-authenticated SP session (see run_kql_stream).
    ``max_bytes`` overrides the stdout cap for server-side callers that parse a large result
    (e.g. change history with verbose before/after diffs) rather than streaming it to a client."""
    return await _capture(
        run_kql_stream(kql, connection, output=output, session_config_dir=session_config_dir, max_bytes=max_bytes),
        max_bytes=max_bytes,
    )


# Paged ceiling for a single collected Resource Graph query (vs the 1000-row single page
# of run_kql_capture). Assessment controls page up to this many violating resources so a
# large estate's true violation count isn't silently truncated to 1000.
KQL_COLLECT_MAX_ROWS = 5000
# Substrings in CLI stderr that indicate a transient/throttle condition worth retrying.
_CLI_RETRYABLE = ("429", "toomanyrequests", "throttl", "rate limit", "timed out", "503", "502", "504", "500 ")


@dataclass
class KqlResult:
    """The result of a fully-paged, fail-closed Resource Graph collection.

    ``ok`` is False on ANY hard failure (auth, throttle-exhausted, JSON parse). Callers MUST
    treat ``ok is False`` as "could not evaluate" — never as an empty (passing) result. This
    is the contract that prevents a truncated/garbled response from masquerading as a clean
    pass. ``complete`` is False when ``max_rows`` capped the violating set (more exist)."""

    ok: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    complete: bool = True
    pages: int = 0
    total: int | None = None  # ARG's full record count (accurate even when rows are capped)


def _normalize_kql(kql: str) -> tuple[str, str]:
    """Normalize a KQL body to a single safe line (mirrors run_kql_stream). Returns
    (query, error) — error non-empty if the query is empty or too long."""
    query = (kql or "").strip()
    if not query:
        return "", "Empty query."
    if len(query) > 8000:
        return "", "Query is too long."
    query = re.sub(r"//[^\n]*", "", query)
    query = re.sub(r"\s*\n\s*", " ", query).strip()
    query = re.sub(r"[ \t]{2,}", " ", query)
    return query, ""


async def _graph_page_cli(
    az_path: str,
    query: str,
    env: dict[str, str],
    timeout: int,
    page_size: int,
    skip_token: str,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> CaptureResult:
    """Run ONE page of `az graph query` (full wrapper output incl. skip_token), captured.

    ``max_bytes`` overrides the capture cap: a single 1000-row page of even a light projection can
    exceed the 256 KB browser cap, so paged COLLECTION (parsed server-side, never streamed) passes
    a large cap to avoid truncating a page into invalid JSON."""
    run_argv = [
        az_path, "graph", "query",
        "-q", query,
        "--first", str(page_size),
        "--output", "json",
    ]
    if skip_token:
        run_argv += ["--skip-token", skip_token]
    return await _capture(
        _stream_process(run_argv, env, timeout, label="az graph query (paged)", destructive=False, max_bytes=max_bytes),
        max_bytes=max_bytes,
    )


async def run_kql_collect(
    kql: str,
    connection: dict[str, Any] | None,
    *,
    session_config_dir: str | None = None,
    max_rows: int = KQL_COLLECT_MAX_ROWS,
    page_size: int = 1000,
    max_bytes: int = KQL_RESOURCE_CAPTURE_BYTES,
) -> KqlResult:
    """Run a Resource Graph (KQL) query and collect ALL rows across pages, FAIL-CLOSED.

    This is the assessment-grade replacement for ``run_kql_capture`` + ``json.loads``:
    - Pages through ``$skipToken`` (REST) / ``skip_token`` (CLI) up to ``max_rows``.
    - Retries throttle (429) and transient 5xx with exponential backoff + jitter.
    - Returns ``ok=False`` on ANY auth/throttle/parse failure so a control can be marked
      ``error`` (excluded from the score) instead of a false ``pass``.

    Mirrors ``run_kql_stream``'s SP-vs-REST branching so it works under service principals,
    managed identity / pasted tokens (REST), and ambient local ``az`` login.
    """
    import asyncio
    import json as _json
    import random

    query, qerr = _normalize_kql(kql)
    if qerr:
        return KqlResult(ok=False, error=qerr)

    settings = load_settings()
    timeout = int(settings.get("command_timeout_seconds", 120))
    page_size = max(1, min(1000, int(page_size)))
    sp = _is_service_principal(connection)

    # --- REST path: non-service-principal (managed identity / pasted token). ----------------
    if not sp:
        from app.azure.arm import query_resource_graph_paged
        from app.azure.credentials import get_arm_token

        token, terr = await get_arm_token(connection)
        if token:
            rows, err, complete, total = await query_resource_graph_paged(
                token, query, page_size=page_size, max_rows=max_rows
            )
            if err:
                return KqlResult(ok=False, rows=rows, error=err, complete=False, total=total)
            return KqlResult(ok=True, rows=rows, complete=complete, total=total)
        # No token: only error out when we KNOW there is no ambient `az` to fall back to.
        method = (connection or {}).get("auth_method", "")
        if method == "az_cli_token" or os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"):
            return KqlResult(ok=False, error=terr or "Could not acquire an Azure token for this connection.")
        # else: fall through to ambient CLI (local dev default_chain).

    # --- CLI path: service principals (and ambient local `az`). ------------------------------
    az_path = shutil.which("az")
    if not az_path:
        return KqlResult(ok=False, error="'az' is not installed on the host.")

    config_dir: str | None = None
    own_config = False
    try:
        if sp:
            if session_config_dir:
                config_dir = session_config_dir
            else:
                config_dir = tempfile.mkdtemp(prefix="azexec-")
                own_config = True
                err = await _sp_login(connection, az_path, config_dir)
                if err:
                    return KqlResult(ok=False, error=err)
        env = _run_env(connection, config_dir)

        rows: list[dict[str, Any]] = []
        skip_token = ""
        complete = True
        pages = 0
        total: int | None = None
        max_retries = 4
        for _page in range(200):
            cap: CaptureResult | None = None
            for attempt in range(max_retries + 1):
                cap = await _graph_page_cli(az_path, query, env, timeout, page_size, skip_token, max_bytes=max_bytes)
                if cap.ok:
                    break
                blob = f"{cap.error} {cap.stderr}".lower()
                transient = any(s in blob for s in _CLI_RETRYABLE)
                if transient and attempt < max_retries:
                    await asyncio.sleep(min(60.0, (2 ** attempt) + random.uniform(0, 0.5)))
                    continue
                break
            if cap is None or not cap.ok:
                return KqlResult(ok=False, rows=rows, error=(cap.error if cap else "Query failed."), complete=False, total=total)
            # Fail-closed parse: a truncated/garbled wrapper is an ERROR, not an empty pass.
            try:
                wrapper = _json.loads(cap.stdout or "{}")
            except (ValueError, TypeError) as e:
                return KqlResult(ok=False, rows=rows, error=f"Result parse error: {e}", complete=False, total=total)
            if isinstance(wrapper, list):
                # Defensive: some az versions may already project to a list.
                page_rows = wrapper
                skip_token = ""
            elif isinstance(wrapper, dict):
                page_rows = wrapper.get("data", [])
                skip_token = wrapper.get("skip_token") or wrapper.get("skipToken") or ""
                if total is None:
                    tr = wrapper.get("total_records")
                    if tr is None:
                        tr = wrapper.get("totalRecords")
                    if isinstance(tr, (int, float)):
                        total = int(tr)
            else:
                page_rows, skip_token = [], ""
            if isinstance(page_rows, list):
                rows.extend(page_rows)
            pages += 1
            if len(rows) >= max_rows:
                rows = rows[:max_rows]
                complete = not bool(skip_token)
                break
            if not skip_token:
                break
        else:
            complete = not bool(skip_token)
        return KqlResult(ok=True, rows=rows, complete=complete, pages=pages, total=total)
    finally:
        if config_dir and own_config:
            shutil.rmtree(config_dir, ignore_errors=True)


async def open_sp_session(connection: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Log a service-principal connection in ONCE into a fresh config dir, for reuse
    across many subsequent ``run_kql_capture(..., session_config_dir=...)`` calls.

    Returns (config_dir, error). For non-SP connections returns (None, None) — no login
    needed. The caller MUST call ``close_sp_session(config_dir)`` when done."""
    if not _is_service_principal(connection):
        return None, None
    az_path = shutil.which("az")
    if not az_path:
        return None, "'az' is not installed on the host."
    config_dir = tempfile.mkdtemp(prefix="azexec-")
    err = await _sp_login(connection, az_path, config_dir)
    if err:
        shutil.rmtree(config_dir, ignore_errors=True)
        return None, err
    return config_dir, None


def close_sp_session(config_dir: str | None) -> None:
    if config_dir:
        shutil.rmtree(config_dir, ignore_errors=True)


# ============================ Generic az JSON argv runner ============================
async def _run_az_argv_stream(
    argv_tail: list[str],
    connection: dict[str, Any] | None,
    *,
    label: str,
    session_config_dir: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run ``az <argv_tail>`` bound to a connection's identity and stream output.

    Like ``run_kql_stream`` but for any read-only ``az`` sub-command (Log Analytics,
    metrics, resource health, …). The argv tail is passed verbatim — NO shell parsing —
    so embedded KQL/JMESPath with pipes is safe. Always read-only / non-destructive.
    """
    settings = load_settings()
    if not settings.get("command_execution_enabled", False):
        yield {"type": "error", "message": "Command execution is disabled by the administrator."}
        return
    allowlist = {b.lower() for b in (settings.get("command_allowlist") or ["az"])}
    if "az" not in allowlist:
        yield {"type": "error", "message": "This widget requires 'az' to be allowed."}
        return
    az_path = shutil.which("az")
    if not az_path:
        yield {"type": "error", "message": "'az' is not installed on the host."}
        return

    timeout = int(settings.get("command_timeout_seconds", 120))
    config_dir: str | None = None
    own_config = False
    sp = _is_service_principal(connection)
    try:
        if sp:
            if session_config_dir:
                config_dir = session_config_dir
            else:
                config_dir = tempfile.mkdtemp(prefix="azexec-")
                own_config = True
                err = await _sp_login(connection, az_path, config_dir)
                if err:
                    yield {"type": "error", "message": err}
                    return
        env = _run_env(connection, config_dir)
        run_argv = [az_path, *argv_tail]
        async for ev in _stream_process(run_argv, env, timeout, label=label, destructive=False):
            yield ev
    finally:
        if config_dir and own_config:
            shutil.rmtree(config_dir, ignore_errors=True)


# Caps for the read-only telemetry queries Monitor widgets issue.
LA_MAX_ROWS = 1000
METRICS_MAX_POINTS = 2000


async def run_la_capture(
    kql: str,
    workspace_id: str,
    connection: dict[str, Any] | None,
    *,
    timespan: str = "P1D",
    session_config_dir: str | None = None,
) -> CaptureResult:
    """Run a Log Analytics KQL query (`az monitor log-analytics query`) and capture JSON.

    ``timespan`` is an ISO-8601 duration (e.g. ``PT1H``, ``P1D``). Distinct from Resource
    Graph: this targets a Log Analytics *workspace* and supports the full KQL surface.
    """
    query = re.sub(r"//[^\n]*", "", (kql or "").strip())
    query = re.sub(r"\s*\n\s*", " ", query).strip()
    if not query:
        return CaptureResult(ok=False, error="Empty query.")
    if not workspace_id:
        return CaptureResult(ok=False, error="No Log Analytics workspace id configured on the connection.")
    if len(query) > 8000:
        return CaptureResult(ok=False, error="Query is too long.")
    # Log Analytics is a distinct token audience (api.loganalytics.io) a pasted ARM token can't
    # serve. A non-SP connection with no ambient `az` login fails closed with a clear message
    # rather than a CLI call that returns nothing.
    mode, _t, _e = await _arm_rest_mode(connection)
    if mode != "cli":
        return CaptureResult(ok=False, error=_non_sp_unavailable_msg("log_analytics"))
    argv_tail = [
        "monitor", "log-analytics", "query",
        "--workspace", workspace_id,
        "--analytics-query", query,
        "--timespan", timespan or "P1D",
        "--output", "json",
    ]
    return await _capture(
        _run_az_argv_stream(
            argv_tail, connection,
            label="az monitor log-analytics query",
            session_config_dir=session_config_dir,
        )
    )


async def run_metrics_capture(
    resource_id: str,
    metrics: list[str],
    connection: dict[str, Any] | None,
    *,
    aggregation: str | list[str] = "Average",
    interval: str = "PT5M",
    timespan: str | None = None,
    end_time: str | None = None,
    dimension_filter: str | None = None,
    session_config_dir: str | None = None,
) -> CaptureResult:
    """Run `az monitor metrics list` for a resource + metric(s) and capture JSON.

    ``timespan`` is an ISO-8601 start datetime passed to ``--start-time`` (or omitted to let
    the CLI default to the last hour); ``end_time`` is an optional ``--end-time`` so an
    explicit start/end window can be requested. ``interval`` is the grain (e.g. ``PT5M``).
    ``aggregation`` may be a single name or a list (``az`` accepts several, e.g. requesting
    ``Average Total Maximum`` so the response carries every column — useful when different
    metrics have different primary aggregations).
    """
    if not resource_id:
        return CaptureResult(ok=False, error="No resource id provided.")
    metric_names = [m for m in (metrics or []) if m]
    if not metric_names:
        return CaptureResult(ok=False, error="No metric name provided.")
    if isinstance(aggregation, (list, tuple)):
        aggs = [str(a) for a in aggregation if a] or ["Average"]
    else:
        aggs = [aggregation or "Average"]
    # Non-service-principal connections can't run `az monitor metrics list` (no ambient login);
    # collect the same metrics over ARM REST with the connection's token, or fail closed.
    mode, token, terr = await _arm_rest_mode(connection)
    if mode != "cli":
        if mode == "rest" and token:
            from app.azure.arm import get_metrics

            text, err = await get_metrics(
                token, resource_id, metricnames=metric_names, aggregations=aggs,
                interval=interval or "PT5M", start_time=timespan, end_time=end_time,
                dimension_filter=dimension_filter,
            )
            return CaptureResult(ok=(err is None), stdout=text, error=err or "")
        return CaptureResult(ok=False, error=_non_sp_unavailable_msg("arm", terr))
    argv_tail = [
        "monitor", "metrics", "list",
        "--resource", resource_id,
        "--metrics", *metric_names,
        "--aggregation", *aggs,
        "--interval", interval or "PT5M",
        "--output", "json",
    ]
    if timespan:
        argv_tail += ["--start-time", timespan]
    if end_time:
        argv_tail += ["--end-time", end_time]
    if dimension_filter:
        # Splits the metric by an Azure Monitor dimension, e.g. "StatusCode eq '403'".
        argv_tail += ["--filter", dimension_filter]
    return await _capture(
        _run_az_argv_stream(
            argv_tail, connection,
            label="az monitor metrics list",
            session_config_dir=session_config_dir,
        )
    )


async def run_az_json_capture(
    argv_tail: list[str],
    connection: dict[str, Any] | None,
    *,
    label: str = "az",
    session_config_dir: str | None = None,
) -> CaptureResult:
    """Run an arbitrary read-only az sub-command (argv tail) and capture JSON output.

    Non-service-principal connections (pasted ARM token / managed identity) have no ambient
    ``az`` login, so a known read-only ARM command is translated to ARM REST with the
    connection's token; an audience a pasted ARM token can't serve (Log Analytics / App
    Insights / Key Vault data-plane), or an unrecognized command on a connection with no
    ambient ``az``, fails CLOSED with a clear message instead of a silent empty result."""
    mode, token, terr = await _arm_rest_mode(connection)
    if mode != "cli":
        if mode == "rest" and token:
            handled, text, err = await _arm_rest_for_argv(argv_tail, token)
            if handled:
                return CaptureResult(ok=(err is None), stdout=text, error=err or "")
        # Not a REST-translatable ARM read (or no token) → fail closed with an audience-aware hint.
        return CaptureResult(ok=False, error=_non_sp_unavailable_msg(_az_argv_audience(argv_tail), terr))
    return await _capture(
        _run_az_argv_stream(argv_tail, connection, label=label, session_config_dir=session_config_dir)
    )


async def run_app_insights_capture(
    kql: str,
    app_id: str,
    connection: dict[str, Any] | None,
    *,
    timespan: str = "P1D",
    session_config_dir: str | None = None,
) -> CaptureResult:
    """Run a KQL query against a classic Application Insights resource via
    ``az monitor app-insights query --app <appId>`` and capture JSON.

    Distinct from ``run_la_capture`` (which targets a Log Analytics workspace): this hits
    the App Insights query API directly, for components NOT in workspace-based mode.
    Read-only; the same query-sanitization + length cap as Log Analytics applies."""
    query = re.sub(r"//[^\n]*", "", (kql or "").strip())
    query = re.sub(r"\s*\n\s*", " ", query).strip()
    if not query:
        return CaptureResult(ok=False, error="Empty query.")
    if not app_id:
        return CaptureResult(ok=False, error="No Application Insights app id provided.")
    if len(query) > 8000:
        return CaptureResult(ok=False, error="Query is too long.")
    # App Insights is a distinct token audience (api.applicationinsights.io) a pasted ARM token
    # can't serve — fail closed for a non-SP connection rather than return nothing.
    mode, _t, _e = await _arm_rest_mode(connection)
    if mode != "cli":
        return CaptureResult(ok=False, error=_non_sp_unavailable_msg("app_insights"))
    argv_tail = [
        "monitor", "app-insights", "query",
        "--app", app_id,
        "--analytics-query", query,
        "--offset", timespan or "P1D",
        "--output", "json",
    ]
    return await _capture(
        _run_az_argv_stream(
            argv_tail, connection,
            label="az monitor app-insights query",
            session_config_dir=session_config_dir,
        )
    )
