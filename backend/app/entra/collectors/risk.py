"""Risk and sign-in intelligence collector.

The largest data volume in the product and the easiest place to build something that does
not scale. The governing rule, enforced by :func:`collect` and asserted by the tests:

    **No raw sign-in row ever reaches the snapshot.**

Sign-ins are paged, folded into counters in memory, and only the counters persist. A
tenant with 40 million monthly sign-ins produces a payload of a few kilobytes. When a cap
truncates the window we set ``sampled=True`` and every chart that reads the aggregates is
required to say so — a silently sampled chart is a lie, and this repository already has the
scar tissue to prove it (``/memories/repo/arg-truncation-bug.md``).

Three independent capabilities live here, each degrading on its own:

============================  ===========================  =============================
Capability                    Needs                        Degrades to
============================  ===========================  =============================
Sign-in aggregates            ``AuditLog.Read.All`` + P1   ``unlicensed`` / ``blind``
Identity Protection           ``IdentityRiskyUser.Read``   ``unlicensed`` without P2
Risky workload identities     Workload Identities Premium  one capability flag off
============================  ===========================  =============================

Pattern detection is deterministic and explainable — password spray, MFA fatigue and
failure spikes are counted, not predicted. Every pattern finding carries its raw counts in
``evidence`` so an analyst can verify the claim rather than trust it.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.entra import model
from app.entra.collectors import CollectContext, as_dict, as_list, clip, guarded
from app.entra.collectors.roles import _is_licence_error
from app.entra.graphclient import GraphClient, GraphError, GraphPermissionError

DOMAIN = "risk"

# Hard ceiling on sign-in rows READ (not stored). Beyond this the window is reported as
# sampled. 200k rows at ~40 fields is a few seconds of paging and bounded memory.
MAX_SIGNIN_ROWS = 200_000

# Graph page-size ceilings, both found against a live tenant:
#   * /auditLogs/signIns accepts the usual 999.
#   * /identityProtection/* rejects anything above 500 with
#     "Invalid page size specified: '999'. Must be between 1 and 500 inclusive."
#     which loses the entire Identity Protection dataset.
SIGNIN_PAGE = 999
RISK_PAGE = 500

# How many times a dead continuation token may be worked around before the read is treated
# as genuinely broken. Each resume costs one repeated page, not the whole dataset.
_MAX_RESUMES = 10

# Number of disjoint createdDateTime windows the sign-in read is split into.
#
# Measured against a 20k-seat tenant producing ~200k sign-ins a month, because this is the
# slowest read in the product and parallelising it looked like free speed:
#   6 windows — every window 429'd, all six exhausted the retry budget, and the domain lost
#               every sign-in it had.
#   3 windows — survived, but Graph answered with a Retry-After that stalled the read for
#               five minutes. Slower than serial and far less predictable.
#   1 window  — never throttled once across repeated full reads.
# /auditLogs/signIns is rate-limited per tenant, not per connection, so extra readers buy
# no throughput and only spend the budget faster. The windowing stays because it is how the
# shared cap and the per-window failure handling are expressed, and because the next person
# to try this deserves the measurements rather than a repeat of the experiment.
SIGNIN_SHARDS = 1

# `authenticationRequirement` does NOT exist on signIn in v1.0 — selecting it 400s the whole
# query, and it is absent even from the default payload. Requesting it cost this collector
# every sign-in on a fully-permissioned tenant. MFA enforcement is instead read from the
# applied Conditional Access grant controls, which IS available and is a narrower, honest
# claim: "Conditional Access enforced MFA", not "the user was challenged".
SIGNIN_SELECT = (
    "id", "createdDateTime", "userId", "userPrincipalName", "userDisplayName", "appId",
    "appDisplayName", "clientAppUsed", "ipAddress", "status", "location", "deviceDetail",
    "conditionalAccessStatus", "appliedConditionalAccessPolicies", "riskLevelDuringSignIn",
    "riskState", "isInteractive", "resourceDisplayName",
)

# Only the top N of each unbounded dimension survives into the payload.
_TOP_USERS = 50
_TOP_APPS = 100
_TOP_FAILURES = 40
_TOP_IPS = 40
# The non-compliant-device list is kept much longer than the other top-N slices because a
# SIGNAL intersects it with the privileged principals. Truncating it to 50 would silently
# miss an administrator who happens to rank 51st by sign-in volume — and missing exactly
# the row that matters is the failure mode this product exists to prevent.
_MAX_UNMANAGED_USERS = 5_000
# A "pattern" is a short list of detections, each with a rule. A thousand of them is not a
# pattern, it is a table — and it buries the two entries somebody needs to act on.
_MAX_PATTERNS = 100

LEGACY_CLIENT_APPS = {
    "Exchange ActiveSync", "Other clients", "IMAP4", "POP3", "SMTP", "MAPI Over HTTP",
    "Exchange Web Services", "Exchange Online PowerShell", "Autodiscover",
    "Offline Address Book", "Authenticated SMTP", "Other clients; Older Office clients",
}

# Well-known sign-in failure codes worth naming in plain English.
FAILURE_MEANINGS: dict[str, str] = {
    "50126": "Invalid username or password",
    "50053": "Account locked by smart lockout",
    "50055": "Password expired",
    "50057": "User account is disabled",
    "50058": "Silent sign-in failed — no session",
    "50074": "Multi-factor authentication required but not satisfied",
    "50076": "Multi-factor authentication required from this location",
    "50079": "User must enrol in multi-factor authentication",
    "50097": "Device authentication required",
    "50105": "User is not assigned to this application",
    "50158": "External security challenge not satisfied",
    "53000": "Device is not compliant",
    "53001": "Device is not hybrid domain joined",
    "53003": "Blocked by Conditional Access",
    "500121": "Multi-factor authentication denied or timed out by the user",
    "530032": "Blocked by a Conditional Access security policy",
    "700016": "Application not found in the directory",
}

# Deterministic pattern thresholds. Exposed here (not buried in an evaluate body) so the
# UI can state the rule alongside every pattern it reports.
SPRAY_MIN_USERS = 12          # distinct users failing 50126 from one IP
FATIGUE_MIN_DENIALS = 5       # 500121 denials for one user in the window
SPIKE_FACTOR = 3.0            # daily failures vs the trailing median


def _day(ts: str) -> str:
    return ts[:10] if ts else ""


def _bucket(store: dict[str, dict[str, Any]], key: str, seed: dict[str, Any]) -> dict[str, Any]:
    row = store.get(key)
    if row is None:
        row = dict(seed)
        store[key] = row
    return row


class _Aggregator:
    """Folds sign-in rows into counters. Never retains a row."""

    def __init__(self) -> None:
        self.total = 0
        self.success = 0
        self.failure = 0
        self.interactive = 0
        self.mfa_challenged = 0
        self.by_day: dict[str, dict[str, int]] = {}
        self.by_app: dict[str, dict[str, Any]] = {}
        self.by_user: dict[str, dict[str, Any]] = {}
        self.by_client_app: dict[str, int] = defaultdict(int)
        self.by_country: dict[str, int] = defaultdict(int)
        self.by_ca_result: dict[str, int] = defaultdict(int)
        self.by_failure: dict[str, dict[str, Any]] = {}
        self.legacy: dict[str, dict[str, Any]] = {}
        self.legacy_success_users: set[str] = set()
        self.report_only: dict[str, dict[str, Any]] = {}
        self.device_compliance: dict[str, int] = defaultdict(int)
        # Pattern inputs, bounded by construction.
        self.spray_by_ip: dict[str, set[str]] = defaultdict(set)
        self.fatigue_by_user: dict[str, dict[str, Any]] = {}
        # Users with a SUCCESSFUL interactive sign-in from a device Intune reports as
        # non-compliant. The "is this user privileged?" join belongs to the signal, which
        # can see the roles domain — a collector never reads another collector's output.
        self.unmanaged_signins: dict[str, dict[str, Any]] = {}
        self.window_start = ""
        self.window_end = ""

    # -- folding ---------------------------------------------------------------
    def add(self, raw: dict[str, Any]) -> None:
        self.total += 1
        created = str(raw.get("createdDateTime") or "")
        if created:
            if not self.window_start or created < self.window_start:
                self.window_start = created
            if not self.window_end or created > self.window_end:
                self.window_end = created

        status = as_dict(raw.get("status"))
        code = str(status.get("errorCode") or 0)
        failed = code not in ("0", "")
        if failed:
            self.failure += 1
        else:
            self.success += 1

        client_app = str(raw.get("clientAppUsed") or "unknown")
        self.by_client_app[client_app] += 1
        # `isInteractive` is authoritative and available; inferring from the client app name
        # was only ever a proxy for it.
        raw_interactive = raw.get("isInteractive")
        is_interactive = (bool(raw_interactive) if raw_interactive is not None
                          else client_app in ("Browser", "Mobile Apps and Desktop clients"))
        if is_interactive:
            self.interactive += 1

        # MFA enforcement comes from the applied Conditional Access grant controls, because
        # `authenticationRequirement` is not a v1.0 property. Narrower but true.
        mfa_enforced = any(
            "mfa" in str(control).lower()
            for applied in as_list(raw.get("appliedConditionalAccessPolicies"))
            for control in as_list(as_dict(applied).get("enforcedGrantControls"))
        )
        if mfa_enforced:
            self.mfa_challenged += 1

        day = _day(created)
        if day:
            bucket = _bucket(self.by_day, day, {"total": 0, "success": 0, "failure": 0, "mfa": 0})
            bucket["total"] += 1
            bucket["failure" if failed else "success"] += 1
            if mfa_enforced:
                bucket["mfa"] += 1

        user_id = str(raw.get("userId") or "")
        upn = str(raw.get("userPrincipalName") or "")
        display = str(raw.get("userDisplayName") or "")
        # Some sign-in rows carry no UPN (service principals, deleted users, some federated
        # flows). Falling straight through to the object id put raw GUIDs on screen where a
        # person's name belongs.
        label = upn or display or user_id
        if user_id or upn:
            row = _bucket(self.by_user, user_id or upn, {
                "user_id": user_id, "upn": upn, "display_name": display, "label": label,
                "total": 0, "failure": 0, "last_seen": "",
            })
            row["total"] += 1
            if failed:
                row["failure"] += 1
            if created > row["last_seen"]:
                row["last_seen"] = created

        app_id = str(raw.get("appId") or "")
        if app_id:
            row = _bucket(self.by_app, app_id, {
                "app_id": app_id, "display_name": str(raw.get("appDisplayName") or ""),
                "total": 0, "failure": 0, "users": set(), "last_seen": "",
            })
            row["total"] += 1
            if failed:
                row["failure"] += 1
            if user_id:
                row["users"].add(user_id)
            if created > row["last_seen"]:
                row["last_seen"] = created

        location = as_dict(raw.get("location"))
        country = str(location.get("countryOrRegion") or "")
        if country:
            self.by_country[country] += 1

        device = as_dict(raw.get("deviceDetail"))
        compliant = device.get("isCompliant")
        self.device_compliance["compliant" if compliant else
                                "not_compliant" if compliant is False else "unknown"] += 1
        if compliant is False and not failed and is_interactive and user_id:
            row = _bucket(self.unmanaged_signins, user_id, {
                "user_id": user_id, "upn": upn, "display_name": display, "label": label,
                "count": 0, "last_seen": "",
                "device": str(device.get("displayName") or ""),
            })
            row["count"] += 1
            if created > row["last_seen"]:
                row["last_seen"] = created

        if client_app in LEGACY_CLIENT_APPS:
            row = _bucket(self.legacy, client_app, {
                "protocol": client_app, "total": 0, "success": 0, "users": set(), "apps": set(),
                "last_success": "",
            })
            row["total"] += 1
            if not failed:
                row["success"] += 1
                if user_id:
                    row["users"].add(user_id)
                    self.legacy_success_users.add(user_id)
                if app_id:
                    row["apps"].add(app_id)
                if created > row["last_success"]:
                    row["last_success"] = created

        if failed:
            row = _bucket(self.by_failure, code, {
                "code": code, "meaning": FAILURE_MEANINGS.get(code, ""),
                "count": 0, "users": set(),
                "sample": str(status.get("failureReason") or "")[:200],
            })
            row["count"] += 1
            if user_id:
                row["users"].add(user_id)

            ip = str(raw.get("ipAddress") or "")
            if code == "50126" and ip and user_id:
                self.spray_by_ip[ip].add(user_id)
            if code == "500121" and user_id:
                f = _bucket(self.fatigue_by_user, user_id, {
                    "user_id": user_id, "upn": upn, "display_name": display, "label": label,
                    "denials": 0, "last_seen": "",
                })
                f["denials"] += 1
                if created > f["last_seen"]:
                    f["last_seen"] = created

        for applied in as_list(raw.get("appliedConditionalAccessPolicies")):
            entry = as_dict(applied)
            result = str(entry.get("result") or "")
            self.by_ca_result[result or "unknown"] += 1
            if result in ("reportOnlySuccess", "reportOnlyFailure", "reportOnlyInterrupted"):
                row = _bucket(self.report_only, str(entry.get("id") or ""), {
                    "policy_id": str(entry.get("id") or ""),
                    "display_name": str(entry.get("displayName") or ""),
                    "would_block": 0, "would_challenge": 0, "would_pass": 0, "users": set(),
                })
                if result == "reportOnlyFailure":
                    row["would_block"] += 1
                elif result == "reportOnlyInterrupted":
                    row["would_challenge"] += 1
                else:
                    row["would_pass"] += 1
                if user_id:
                    row["users"].add(user_id)

    # -- rendering -------------------------------------------------------------
    def payload(self, *, sampled: bool, lookback_days: int) -> dict[str, Any]:
        by_app = sorted(
            ({**r, "users": len(r["users"]),
              "failure_rate": round(r["failure"] / r["total"], 3) if r["total"] else 0.0}
             for r in self.by_app.values()),
            key=lambda r: -r["total"],
        )[:_TOP_APPS]
        by_user = sorted(
            (dict(r) for r in self.by_user.values()), key=lambda r: -r["total"],
        )[:_TOP_USERS]
        failures = sorted(
            ({**r, "users": len(r["users"])} for r in self.by_failure.values()),
            key=lambda r: -r["count"],
        )[:_TOP_FAILURES]
        legacy = sorted(
            ({**r, "users": len(r["users"]), "apps": len(r["apps"])} for r in self.legacy.values()),
            key=lambda r: -r["total"],
        )
        report_only = sorted(
            ({**r, "users": len(r["users"])} for r in self.report_only.values()),
            key=lambda r: -r["would_block"],
        )
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "lookback_days": lookback_days,
            "sampled": sampled,
            "total": self.total,
            "success": self.success,
            "failure": self.failure,
            "interactive": self.interactive,
            "mfa_challenged": self.mfa_challenged,
            # What that number MEANS. `authenticationRequirement` is not a v1.0 signIn
            # property, so this counts sign-ins where a Conditional Access policy enforced
            # MFA — a narrower claim than "the user was challenged", and the UI says so.
            "mfa_metric": "ca_enforced",
            "failure_rate": round(self.failure / self.total, 4) if self.total else 0.0,
            "by_day": [{"day": d, **v} for d, v in sorted(self.by_day.items())],
            "by_app": by_app,
            "by_user_top": by_user,
            "by_client_app": dict(sorted(self.by_client_app.items(), key=lambda kv: -kv[1])),
            "by_country": dict(sorted(self.by_country.items(), key=lambda kv: -kv[1])[:60]),
            "by_ca_result": dict(self.by_ca_result),
            "by_failure_code": failures,
            "legacy": legacy,
            "legacy_success_users": len(self.legacy_success_users),
            "report_only_impact": report_only,
            "device_compliance": dict(self.device_compliance),
            "unmanaged_signin_users": sorted(
                (dict(r) for r in self.unmanaged_signins.values()),
                key=lambda r: -r["count"],
            )[:_MAX_UNMANAGED_USERS],
            "unmanaged_signin_user_total": len(self.unmanaged_signins),
        }

    def patterns(self) -> list[dict[str, Any]]:
        """Deterministic, explainable detections over the aggregates."""
        out: list[dict[str, Any]] = []

        for ip, users in self.spray_by_ip.items():
            if len(users) >= SPRAY_MIN_USERS:
                out.append({
                    "kind": "password_spray",
                    "key": ip,
                    "label": f"Password spray from {ip}",
                    "rule": f"\u2265 {SPRAY_MIN_USERS} distinct users failed with code 50126 "
                            f"(invalid credentials) from one IP address in the window",
                    "count": len(users),
                    "evidence": {"ip": ip, "distinct_users": len(users),
                                 "threshold": SPRAY_MIN_USERS, "error_code": "50126"},
                })

        for row in self.fatigue_by_user.values():
            if row["denials"] >= FATIGUE_MIN_DENIALS:
                who = row.get("label") or row["upn"] or row["user_id"]
                out.append({
                    "kind": "mfa_fatigue",
                    "key": row["user_id"],
                    "label": f"Repeated MFA denials for {who}",
                    "rule": f"\u2265 {FATIGUE_MIN_DENIALS} multi-factor prompts denied or timed out "
                            f"(code 500121) by one user in the window",
                    "count": row["denials"],
                    "evidence": {"upn": row["upn"], "display_name": row.get("display_name", ""),
                                 "object_id": row["user_id"], "denials": row["denials"],
                                 "threshold": FATIGUE_MIN_DENIALS, "last_seen": row["last_seen"],
                                 "error_code": "500121"},
                })

        days = [v["failure"] for _, v in sorted(self.by_day.items())]
        if len(days) >= 5:
            baseline = sorted(days)[len(days) // 2] or 1
            for day, v in sorted(self.by_day.items()):
                if v["failure"] >= max(50, baseline * SPIKE_FACTOR):
                    out.append({
                        "kind": "failure_spike",
                        "key": day,
                        "label": f"Sign-in failure spike on {day}",
                        "rule": f"Daily failures exceeded {SPIKE_FACTOR}\u00d7 the trailing median "
                                f"for the window (and at least 50 failures)",
                        "count": v["failure"],
                        "evidence": {"day": day, "failures": v["failure"],
                                     "median_failures": baseline, "factor": SPIKE_FACTOR},
                    })

        # ONE aggregate row, not one per user. Emitting a row per affected account turned
        # this list into 1,261 entries on a real tenant, which buried the spray and fatigue
        # detections that actually needed attention.
        if self.unmanaged_signins:
            worst = sorted(self.unmanaged_signins.values(), key=lambda r: -r["count"])
            total_signins = sum(r["count"] for r in self.unmanaged_signins.values())
            out.append({
                "kind": "unmanaged_device_signin",
                "key": "tenant",
                "label": f"{len(self.unmanaged_signins):,} account(s) signed in successfully "
                         f"from a non-compliant device",
                "rule": "A successful interactive sign-in from a device Intune reports as "
                        "non-compliant. Severity depends on whether the account is "
                        "privileged, which is resolved where the finding is raised.",
                "count": len(self.unmanaged_signins),
                "evidence": {
                    "accounts": len(self.unmanaged_signins),
                    "sign_ins": total_signins,
                    "top_accounts": [
                        {"upn": r.get("label") or r["upn"] or r["user_id"],
                         "sign_ins": r["count"], "device": r["device"]}
                        for r in worst[:10]
                    ],
                },
            })

        return sorted(out, key=lambda p: (-p["count"], p["kind"]))[:_MAX_PATTERNS]


def _is_expired_skiptoken(exc: GraphError) -> bool:
    """Graph's continuation tokens have a lifetime shorter than a large sign-in read."""
    return "skip token" in str(exc).lower() and "expired" in str(exc).lower()


async def _read_signins(
    client: GraphClient, ctx: CollectContext, agg: "_Aggregator", since: str,
) -> tuple[int, bool, int]:
    """Page /auditLogs/signIns into ``agg``. Returns (rows read, capped, resume count).

    Pages by hand rather than through ``get_all`` for one reason: when a continuation token
    expires mid-read, ``get_all`` raises and every page it had already fetched is discarded.
    On a tenant whose read takes twenty minutes that is the difference between a complete
    sign-in picture and none at all. Rows are folded into the aggregator as each page
    arrives, so nothing is lost and no raw page is held longer than one iteration.
    """
    read = 0
    resumes = 0
    oldest = ""          # createdDateTime of the oldest row seen; the resume point
    boundary: set[str] = set()   # ids at exactly `oldest`
    skip: set[str] = set()       # ids to ignore on the first page after a resume
    params = {"$select": ",".join(SIGNIN_SELECT), "$top": SIGNIN_PAGE}
    url = ""

    while read < MAX_SIGNIN_ROWS:
        if not url:
            clause = f"createdDateTime ge {since}"
            if oldest:
                # The log is newest-first, so everything still unread is at or before the
                # oldest row we have. `le` (not `lt`) keeps rows sharing that exact second,
                # and `skip` stops the ones already counted from being counted twice. The
                # skip set applies to the resumed page only — applying it to every page
                # would drop rows forever once a timestamp repeated.
                clause += f" and createdDateTime le {oldest}"
            url = f"/auditLogs/signIns?{httpx.QueryParams({**params, '$filter': clause})}"
        try:
            body = await client.get(url)
        except GraphError as exc:
            if not _is_expired_skiptoken(exc) or not read:
                raise
            resumes += 1
            if resumes > _MAX_RESUMES:
                raise
            skip = set(boundary)
            url = ""     # rebuild from the resume point rather than the dead token
            continue

        page = body.get("value") or []
        if not isinstance(page, list) or not page:
            break
        before = read
        for raw in page:
            row = as_dict(raw)
            rid = str(row.get("id") or "")
            if rid and rid in skip:
                continue
            stamp = str(row.get("createdDateTime") or "")
            if stamp and (not oldest or stamp < oldest):
                oldest, boundary = stamp, set()
            if stamp and stamp == oldest and rid:
                boundary.add(rid)
            agg.add(row)
            read += 1
            if read >= MAX_SIGNIN_ROWS:
                return read, True, resumes
        skip = set()

        if read == before:
            # Every row on this page was already counted. Continuing would loop forever.
            break
        if read % (SIGNIN_PAGE * 25) < SIGNIN_PAGE:
            await ctx.say("info", f"Risk: {read:,} sign-in(s) read so far\u2026")
        url = str(body.get("@odata.nextLink") or "")
        if not url:
            return read, False, resumes

    return read, True, resumes


async def collect(client: GraphClient, ctx: CollectContext) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        notes: list[str] = []
        caps = {
            "signins": False, "risky_users": False, "risk_detections": False,
            "risky_workload_identities": False, "licensed_p1": True, "licensed_p2": True,
        }

        # --- sign-in aggregates ---------------------------------------------------
        # The slowest read in the product — ~7s per 999-row page, so a month of a 20k-seat
        # tenant is ~200 pages — and the only one that pages itself rather than calling
        # get_all. Two live failures forced that:
        #
        #   * Reading disjoint createdDateTime windows CONCURRENTLY looked like free speed
        #     and is not. Six readers exhausted the client's six 429 retries and lost the
        #     entire dataset: /auditLogs/signIns rate-limits far more tightly than the rest
        #     of Graph, so extra readers reach the ceiling sooner without adding throughput.
        #   * Even read serially, a ~20-minute pagination outlives its own continuation
        #     token. Graph answers "Skip token has expired. Restart pagination from the
        #     first page", and get_all raises — throwing away every page already read.
        #     That lost all 200,000 events on a full refresh.
        #
        # Graph states the remedy in the error, and createdDateTime gives us a resume point:
        # the log is newest-first, so on expiry we restart bounded at the oldest row already
        # seen and carry on. Serial, so it never throttles; resumable, so a dead token costs
        # one page instead of the whole domain.
        agg = _Aggregator()
        sampled = False
        since = (datetime.now(timezone.utc) - timedelta(days=ctx.signin_lookback_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        await ctx.say("info", f"Risk: aggregating sign-ins since {since[:10]}\u2026")
        try:
            read, sampled, resumes = await _read_signins(client, ctx, agg, since)
            caps["signins"] = True
            if resumes:
                await ctx.say("warn", f"Risk: paging token expired {resumes}x; resumed from the "
                                      "last event read")
            if sampled:
                notes.append(
                    f"Sign-in analysis was capped at {MAX_SIGNIN_ROWS:,} events \u2014 every "
                    "chart derived from it is marked as sampled.")
            await ctx.say("ok", f"Risk: {read:,} sign-in(s) aggregated")

        except GraphPermissionError as exc:
            notes.append("Sign-in logs not permitted (needs AuditLog.Read.All): "
                         f"{clip(exc.message, 110)}")
        except GraphError as exc:
            if _is_licence_error(exc):
                caps["licensed_p1"] = False
                notes.append("Sign-in logs require Entra ID P1.")
            else:
                notes.append(f"Sign-in logs: {clip(exc, 150)}")

        # --- Identity Protection ---------------------------------------------------
        risky_users: list[dict[str, Any]] = []
        try:
            rows, trunc = await client.get_all(
                "/identityProtection/riskyUsers",
                select=["id", "userPrincipalName", "userDisplayName", "riskLevel", "riskState",
                        "riskDetail", "riskLastUpdatedDateTime", "isDeleted", "isProcessing"],
                top=RISK_PAGE, max_items=20_000,
            )
            caps["risky_users"] = True
            for raw in rows:
                row = as_dict(raw)
                if row.get("isDeleted"):
                    continue
                risky_users.append({
                    "id": str(row.get("id") or ""),
                    "upn": str(row.get("userPrincipalName") or ""),
                    "name": str(row.get("userDisplayName") or ""),
                    "level": str(row.get("riskLevel") or "none"),
                    "state": str(row.get("riskState") or "none"),
                    "detail": str(row.get("riskDetail") or ""),
                    "last_updated": str(row.get("riskLastUpdatedDateTime") or ""),
                })
            if trunc:
                notes.append("Risky users were capped at 20,000.")
            await ctx.say("ok", f"Risk: {len(risky_users)} risky user(s)")
        except GraphPermissionError as exc:
            notes.append("Risky users not permitted (needs IdentityRiskyUser.Read.All): "
                         f"{clip(exc.message, 110)}")
        except GraphError as exc:
            if _is_licence_error(exc):
                caps["licensed_p2"] = False
                notes.append("Identity Protection requires Entra ID P2.")
            else:
                notes.append(f"Risky users: {clip(exc, 150)}")

        detections: list[dict[str, Any]] = []
        detection_counts: dict[str, int] = defaultdict(int)
        try:
            rows, _ = await client.get_all(
                "/identityProtection/riskDetections",
                select=["id", "riskEventType", "riskLevel", "riskState", "userId",
                        "userPrincipalName", "detectedDateTime", "ipAddress", "location",
                        "activity", "detectionTimingType"],
                filter=f"detectedDateTime ge {since}",
                top=RISK_PAGE, max_items=20_000,
            )
            caps["risk_detections"] = True
            for raw in rows:
                row = as_dict(raw)
                kind = str(row.get("riskEventType") or "unknown")
                detection_counts[kind] += 1
                loc = as_dict(row.get("location"))
                detections.append({
                    "id": str(row.get("id") or ""),
                    "type": kind,
                    "level": str(row.get("riskLevel") or "none"),
                    "state": str(row.get("riskState") or ""),
                    "user_id": str(row.get("userId") or ""),
                    "upn": str(row.get("userPrincipalName") or ""),
                    "detected_at": str(row.get("detectedDateTime") or ""),
                    "ip": str(row.get("ipAddress") or ""),
                    "country": str(loc.get("countryOrRegion") or ""),
                    "activity": str(row.get("activity") or ""),
                })
            detections = sorted(detections, key=lambda d: d["detected_at"], reverse=True)[:2000]
        except GraphPermissionError as exc:
            notes.append(f"Risk detections not permitted: {clip(exc.message, 110)}")
        except GraphError as exc:
            if not _is_licence_error(exc):
                notes.append(f"Risk detections: {clip(exc, 150)}")

        risky_sps: list[dict[str, Any]] = []
        try:
            rows, _ = await client.get_all(
                "/identityProtection/riskyServicePrincipals",
                select=["id", "appId", "displayName", "riskLevel", "riskState", "riskDetail",
                        "riskLastUpdatedDateTime", "isEnabled", "isProcessing",
                        "servicePrincipalType"],
                top=RISK_PAGE, max_items=5_000,
            )
            caps["risky_workload_identities"] = True
            for raw in rows:
                row = as_dict(raw)
                risky_sps.append({
                    "id": str(row.get("id") or ""),
                    "app_id": str(row.get("appId") or ""),
                    "name": str(row.get("displayName") or ""),
                    "level": str(row.get("riskLevel") or "none"),
                    "state": str(row.get("riskState") or "none"),
                    "detail": str(row.get("riskDetail") or ""),
                    "last_updated": str(row.get("riskLastUpdatedDateTime") or ""),
                    "enabled": bool(row.get("isEnabled", True)),
                })
        except GraphPermissionError as exc:
            notes.append("Risky workload identities not permitted "
                         f"(needs IdentityRiskyServicePrincipal.Read.All): {clip(exc.message, 90)}")
        except GraphError as exc:
            if not _is_licence_error(exc):
                notes.append(f"Risky workload identities: {clip(exc, 150)}")

        signins = agg.payload(sampled=sampled, lookback_days=ctx.signin_lookback_days)
        patterns = agg.patterns() if caps["signins"] else []
        if patterns:
            patterns = await resolve_pattern_names(client, patterns)
        data = {
            "signins": signins,
            "patterns": patterns,
            "risky_users": risky_users,
            "risk_detections": detections,
            "detection_counts": dict(sorted(detection_counts.items(), key=lambda kv: -kv[1])),
            "risky_service_principals": risky_sps,
            "capabilities": caps,
            "thresholds": {
                "spray_min_users": SPRAY_MIN_USERS,
                "fatigue_min_denials": FATIGUE_MIN_DENIALS,
                "spike_factor": SPIKE_FACTOR,
                "max_signin_rows": MAX_SIGNIN_ROWS,
            },
            "counts": {
                "signins": agg.total,
                "risky_users": len(risky_users),
                "risky_users_high": sum(1 for r in risky_users if r["level"] == "high"),
                "unremediated": sum(1 for r in risky_users
                                    if r["state"] in ("atRisk", "confirmedCompromised")),
                "risk_detections": len(detections),
                "risky_service_principals": len(risky_sps),
                "patterns": len(patterns),
            },
        }

        if not any((caps["signins"], caps["risky_users"], caps["risk_detections"])):
            reason = ("Identity Protection requires Entra ID P2 and sign-in analysis requires "
                      "Entra ID P1." if not (caps["licensed_p1"] and caps["licensed_p2"])
                      else "No risk or sign-in data could be read for this tenant.")
            if not (caps["licensed_p1"] and caps["licensed_p2"]):
                return model.unlicensed_payload(DOMAIN, reason) | {"data": data, "notes": notes}
            return model.blind_payload(DOMAIN, reason, ["AuditLog.Read.All",
                                                        "IdentityRiskyUser.Read.All"]) | {
                "data": data, "notes": notes}

        status = model.STATUS_PARTIAL if notes else model.STATUS_OK
        blockers = []
        if sampled:
            # A cap is not a permission gap and not a license gap. It is a deliberate bound,
            # and the only lever the reader has is a shorter lookback — so say the number and
            # say the lever, rather than "counts are a lower bound".
            blockers.append(model.blocker(
                model.BLOCKER_CAP,
                f"Sign-in analysis stopped at {MAX_SIGNIN_ROWS:,} events over the last "
                f"{ctx.signin_lookback_days} day(s).",
                scope=f"{MAX_SIGNIN_ROWS:,} sign-in events",
                impact="Counts are a lower bound and every derived chart is marked sampled. "
                       "Narrow the sign-in lookback to stay under the cap.",
            ))
        return model.domain_payload(
            DOMAIN, data, status=status, truncated=sampled,
            item_count=len(risky_users) + len(detections) + len(risky_sps), notes=notes,
            blockers=blockers,
        )

    return await guarded(DOMAIN, ctx, _run)


# --------------------------------------------------------------------------- helpers
def risky_by_user_id(risk_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in risk_data.get("risky_users") or [] if r.get("id")}


def signin_by_app(risk_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    signins = as_dict(risk_data.get("signins"))
    return {r["app_id"]: r for r in as_list(signins.get("by_app")) if isinstance(r, dict)}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def resolve_pattern_names(
    client: GraphClient, patterns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Turn the object ids left in pattern labels into names.

    Some sign-in rows carry neither a UPN nor a display name — service principals, deleted
    accounts and certain federated flows. The log genuinely has no name to show, so the
    label falls back to the object id, and "Repeated MFA denials for
    a928ebfd-57eb-4ec2-9c64-1a70dbdda405" is useless to the person who has to act on it.
    One bulk getByIds over the handful of pattern subjects fixes that. Best effort: a
    failure leaves the ids in place rather than losing the pattern.
    """
    unresolved = {
        str((p.get("evidence") or {}).get("object_id") or "")
        for p in patterns
        if (p.get("evidence") or {}).get("object_id")
        and not (p.get("evidence") or {}).get("upn")
        and not (p.get("evidence") or {}).get("display_name")
    }
    unresolved.discard("")
    if not unresolved:
        return patterns
    try:
        resolved = await client.get_by_ids(sorted(unresolved))
    except GraphError:
        return patterns

    for pattern in patterns:
        evidence = pattern.get("evidence") or {}
        oid = str(evidence.get("object_id") or "")
        obj = resolved.get(oid)
        if not obj:
            continue
        name = str(obj.get("displayName") or obj.get("userPrincipalName") or "")
        if not name:
            continue
        kind = str(obj.get("@odata.type") or "").rsplit(".", 1)[-1]
        evidence["display_name"] = name
        evidence["resolved_kind"] = kind
        pattern["label"] = pattern["label"].replace(oid, name)
    return patterns
