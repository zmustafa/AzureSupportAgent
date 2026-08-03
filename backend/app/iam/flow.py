"""Project IAM master rows into a compact, pivot-ready edge list for the Access Map.

The Access Map answers "who can do what, where" as a flow diagram. It needs the same rows the
grid uses, but it must not ship them: a master row carries 66 columns and a large tenant has
thousands of them, so sending the grid's payload to draw a picture would move megabytes per
re-pivot. This module projects each row down to the ~18 fields the diagram's dimensions read,
then deduplicates identical projections into one fact with a count. The client can then re-pivot
instantly without another request.

Three things here are correctness, not optimisation:

**Group rows are not counted twice.** ``compose.expand_group_rows`` keeps the group's own
assignment row AND adds one row per transitive member. Summing both counts every group grant
twice — once for the group, once for each person in it. The effective basis drops the group's
own row *only when it was actually expanded*, so a group whose membership could not be read
still appears rather than silently vanishing.

**Deny assignments are not flow.** A deny removes access. A Sankey ribbon adds it. Drawing a
deny like a grant states the opposite of the truth, so denies are excluded from the facts and
returned separately for the UI to render as what they are.

**Eligible is not access.** A PIM-eligible Owner grant is permission to ask, not standing
privilege. Every fact carries its state so the caller can include or exclude eligibility
deliberately instead of having the two silently averaged into one ribbon.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

from app.iam import schema

#: Cap on distinct facts returned. A tenant that exceeds this gets a truncation flag rather
#: than a browser that stops responding. Facts are deduplicated first, so this is far larger
#: than it looks: the cap bites on genuinely distinct access, not on repetition.
MAX_FACTS = max(1000, int(os.getenv("IAM_FLOW_MAX_FACTS", "20000")))

#: Fields that identify a fact. `count` is the aggregate and is deliberately not part of the key.
FACT_KEYS: tuple[str, ...] = (
    "principal", "principal_id", "principal_type",
    "group", "group_id",
    "role", "role_category", "privileged",
    "surface", "access_path", "state", "pim_managed",
    "management_group", "subscription", "subscription_id",
    "resource_group", "resource_type", "resource",
    "scope", "scope_type", "condition",
)

STATE_ELIGIBLE = "Eligible"
STATE_ACTIVE = "Active"

ACCESS_PATH_GROUP_UNEXPANDED = "GroupUnexpanded"


def _s(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _expanded_group_ids(rows: Iterable[dict[str, Any]]) -> set[str]:
    """Group object ids that produced at least one expanded member row."""
    out: set[str] = set()
    for row in rows:
        gid = _s(row, "sourceGroupId")
        if gid:
            out.add(gid.lower())
    return out


def _is_deny(row: dict[str, Any]) -> bool:
    return (_s(row, "effect").lower() == "deny"
            or _s(row, "surface") == schema.SURFACE_DENY)


def _project(row: dict[str, Any], *, unexpanded_group: bool) -> dict[str, Any]:
    """One master row, reduced to the fields the diagram's dimensions read."""
    # The EFFECTIVE principal is the person who actually holds the access. On a direct
    # assignment it is the assignee; on a group-derived row it is the member, not the group.
    principal_id = _s(row, "effectivePrincipalId") or _s(row, "principalId")
    principal = (_s(row, "effectivePrincipalName")
                 or _s(row, "effectivePrincipalUserPrincipalName")
                 or _s(row, "principalDisplayName")
                 or _s(row, "principalUserPrincipalName")
                 or principal_id
                 or "Unknown principal")
    principal_type = _s(row, "effectivePrincipalType") or _s(row, "principalType") or "Unknown"

    access_path = _s(row, "accessPath") or "Direct"
    if unexpanded_group:
        # The group holds the grant and we could not enumerate who is in it. Saying "Direct"
        # would imply the group itself signs in; saying nothing would hide the grant entirely.
        access_path = ACCESS_PATH_GROUP_UNEXPANDED

    state = _s(row, "assignmentState") or STATE_ACTIVE
    return {
        "principal": principal,
        "principal_id": principal_id,
        "principal_type": principal_type,
        "group": _s(row, "sourceGroupName"),
        "group_id": _s(row, "sourceGroupId"),
        "role": _s(row, "roleName") or "Unnamed role",
        "role_category": _s(row, "roleCategory") or "Unknown",
        "privileged": _truthy(row.get("roleIsPrivileged")),
        "surface": _s(row, "surface") or "Unknown surface",
        "access_path": access_path,
        "state": state,
        "pim_managed": _truthy(row.get("pimManaged")),
        "management_group": _s(row, "managementGroupName") or _s(row, "managementGroupId"),
        "subscription": _s(row, "subscriptionName") or _s(row, "subscriptionId"),
        "subscription_id": _s(row, "subscriptionId"),
        "resource_group": _s(row, "resourceGroup"),
        "resource_type": _s(row, "resourceType"),
        "resource": _s(row, "resourceName"),
        "scope": _s(row, "scope"),
        "scope_type": _s(row, "scopeType") or "Unknown",
        # The condition TEXT is not shown as a dimension — it is long, unique per assignment and
        # would explode the node count. Whether one exists at all is the useful axis.
        "condition": bool(_s(row, "condition")),
    }


def build_facts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Deduplicated projection of master rows, plus the things a flow diagram cannot draw.

    Pure and CPU-bound. Callers must run it off the event loop.
    """
    expanded = _expanded_group_ids(rows)

    facts: dict[tuple, dict[str, Any]] = {}
    denies: dict[tuple, dict[str, Any]] = {}
    unexpanded_groups: set[str] = set()
    skipped_group_rows = 0
    eligible_rows = 0

    for row in rows:
        is_group_principal = _s(row, "principalType").lower() == "group"
        principal_id = _s(row, "principalId").lower()
        # A group row whose members were expanded is represented by those member rows. Keeping
        # it as well would count the grant once for the group and once for every member.
        was_expanded = is_group_principal and principal_id in expanded
        if was_expanded and _s(row, "accessPath") != "GroupTransitive":
            skipped_group_rows += 1
            continue

        unexpanded_group = is_group_principal and not was_expanded and _s(row, "accessPath") != "GroupTransitive"
        if unexpanded_group:
            name = _s(row, "principalDisplayName") or principal_id
            if name:
                unexpanded_groups.add(name)

        projected = _project(row, unexpanded_group=unexpanded_group)
        if projected["state"] == STATE_ELIGIBLE:
            eligible_rows += 1

        bucket = denies if _is_deny(row) else facts
        key = tuple(projected[k] for k in FACT_KEYS)
        existing = bucket.get(key)
        if existing:
            existing["count"] += 1
        else:
            bucket[key] = {**projected, "count": 1}

    fact_list = sorted(facts.values(), key=lambda f: (-f["count"], f["principal"], f["role"]))
    truncated = len(fact_list) > MAX_FACTS
    if truncated:
        fact_list = fact_list[:MAX_FACTS]

    notes: list[str] = []
    if unexpanded_groups:
        sample = ", ".join(sorted(unexpanded_groups)[:5])
        notes.append(
            f"{len(unexpanded_groups)} group(s) hold access whose membership could not be read "
            f"({sample}{'…' if len(unexpanded_groups) > 5 else ''}). They are shown as the group "
            f"itself rather than dropped, so the access is visible even though the people are not."
        )
    if denies:
        notes.append(
            f"{len(denies)} deny assignment(s) are excluded from the flow. A deny removes access "
            f"and a ribbon adds it, so drawing them together would state the opposite of the truth."
        )
    if truncated:
        notes.append(
            f"Showing the {MAX_FACTS:,} largest of {len(facts):,} distinct access facts. "
            f"Narrow the focus to see the rest."
        )

    return {
        "facts": fact_list,
        "denies": sorted(denies.values(), key=lambda f: (-f["count"], f["principal"])),
        "totals": {
            "rows": len(rows),
            "facts": len(facts),
            "grants": sum(f["count"] for f in facts.values()),
            "eligible_rows": eligible_rows,
            "deny_rows": sum(f["count"] for f in denies.values()),
            "group_rows_folded": skipped_group_rows,
            "unexpanded_groups": len(unexpanded_groups),
        },
        "truncated": truncated,
        "notes": notes,
    }


# ------------------------------------------------------------------------------ transport
def encode(result: dict[str, Any]) -> dict[str, Any]:
    """Intern the fact table for the wire.

    Measured on a real tenant: 5,134 distinct facts serialise to **4.0 MiB** as objects, because
    every fact repeats all twenty-one key names and most values (a subscription name, a role
    name) recur thousands of times. Interning the values into per-column dictionaries and
    emitting each fact as a row of small integers cuts that by roughly seven times, which is
    what makes re-pivoting in the browser possible without a round trip per column change.

    Deduplication alone does not solve this — principal x role x scope is close to unique per
    row, so 5,514 rows only collapse to 5,134 facts.
    """
    columns = list(FACT_KEYS)
    dictionaries: list[list[str]] = [[] for _ in columns]
    indexes: list[dict[str, int]] = [{} for _ in columns]

    def encode_rows(facts: list[dict[str, Any]]) -> list[list[int]]:
        out: list[list[int]] = []
        for fact in facts:
            row: list[int] = []
            for i, key in enumerate(columns):
                # Uniform stringification keeps the decoder trivial; booleans become "true"/
                # "false" and are converted back by column name on the client.
                value = fact[key]
                text = "true" if value is True else "false" if value is False else str(value)
                idx = indexes[i].get(text)
                if idx is None:
                    idx = len(dictionaries[i])
                    dictionaries[i].append(text)
                    indexes[i][text] = idx
                row.append(idx)
            row.append(int(fact["count"]))
            out.append(row)
        return out

    facts = encode_rows(result["facts"])
    denies = encode_rows(result["denies"])
    return {
        "columns": columns,
        "boolean_columns": [c for c in ("privileged", "condition", "pim_managed") if c in columns],
        "labels": dictionaries,
        "facts": facts,
        "denies": denies,
        "totals": result["totals"],
        "truncated": result["truncated"],
        "notes": result["notes"],
    }


def decode(payload: dict[str, Any]) -> dict[str, Any]:
    """Inverse of :func:`encode`. Exists so the wire format can be asserted round-trip-safe."""
    columns: list[str] = payload["columns"]
    labels: list[list[str]] = payload["labels"]
    booleans = set(payload.get("boolean_columns") or ())

    def decode_rows(rows: list[list[int]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows:
            fact: dict[str, Any] = {}
            for i, key in enumerate(columns):
                text = labels[i][row[i]]
                fact[key] = (text == "true") if key in booleans else text
            fact["count"] = row[len(columns)]
            out.append(fact)
        return out

    return {
        "facts": decode_rows(payload["facts"]),
        "denies": decode_rows(payload["denies"]),
        "totals": payload["totals"],
        "truncated": payload["truncated"],
        "notes": payload["notes"],
    }
