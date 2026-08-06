"""Mutation check: break a guarantee, confirm the test suite notices, restore.

A test that passes against broken code is worse than no test — it converts an unverified
assumption into a documented one. Each mutation below removes exactly one property the suite
claims to protect. Any mutation that survives is a hole.

Usage: .venv\\Scripts\\python.exe scripts\\iam_mutation_check.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

SIGNALS = "tests/test_iam_signals.py"
ARG = "tests/test_iam_arg.py"
EFF = "tests/test_iam_effective.py"
ESC = "tests/test_iam_escalation.py"
BYP = "tests/test_iam_bypass.py"
GOV = "tests/test_iam_governance.py"
CIEM = "tests/test_iam_ciem.py"
DIS = "tests/test_iam_disabled.py"

# (test file, source file, description, old, new)
MUTATIONS = [
    # ---- P2: the findings engine and its score -----------------------------------------
    (
        SIGNALS,
        "app/iam/score.py",
        "unmeasured signals count towards the score as passes",
        "        measured = [s for s in rs if s.measured]",
        "        measured = list(rs)",
    ),
    (
        SIGNALS,
        "app/iam/score.py",
        "a pillar with no signals scores instead of reporting not_implemented",
        "        if not rs:",
        "        if False:",
    ),
    (
        SIGNALS,
        "app/iam/score.py",
        "a wholly blind pillar scores instead of reporting blind",
        "        elif measured_weight == 0:",
        "        elif False:",
    ),
    (
        SIGNALS,
        "app/iam/score.py",
        "the grade is published regardless of coverage",
        "    show_grade = score is not None and coverage >= MIN_COVERAGE_FOR_GRADE",
        "    show_grade = score is not None",
    ),
    (
        SIGNALS,
        "app/iam/score.py",
        "coverage ignores signal weight within a pillar",
        "            fraction = measured_weight / registered if registered else 0.0",
        "            fraction = len(measured) / len(rs) if rs else 0.0",
    ),
    (
        SIGNALS,
        "app/iam/score.py",
        "findings are not penalised by severity",
        "    base = _SEVERITY_COST.get(worst.severity, 0.5)",
        "    base = 0.5",
    ),
    (
        SIGNALS,
        "app/iam/score.py",
        "a noisy signal is allowed to zero its pillar",
        "    volume = min(len(result.findings), _SATURATION) / _SATURATION",
        "    volume = float(len(result.findings))",
    ),
    (
        SIGNALS,
        "app/iam/signals.py",
        "fingerprint includes the volatile count",
        'raw = f"{self.signal_id}|{self.subject}".lower()',
        'raw = f"{self.signal_id}|{self.subject}|{self.count}".lower()',
    ),
    (
        SIGNALS,
        "app/iam/signals.py",
        "fingerprint is case sensitive on the subject",
        'raw = f"{self.signal_id}|{self.subject}".lower()',
        'raw = f"{self.signal_id}|{self.subject}"',
    ),
    (
        SIGNALS,
        "app/iam/signal_defs/ext.py",
        "a signal stamps a foreign id on its findings",
        '                signal_id="ext.guest_access",',
        '                signal_id="ext.guest_privileged" if privileged else "ext.guest_access",',
    ),
    (
        SIGNALS,
        "app/iam/signal_defs/priv.py",
        "a signal attributes its findings to the wrong pillar",
        '            pillar="priv",',
        '            pillar="hyg",',
    ),
    # ---- P3: the Resource Graph pivot and delta refresh ---------------------------------
    (
        ARG,
        "app/iam/arg.py",
        "a failed Resource Graph sweep reports success with zero rows",
        "    if not res.ok:\n        st.status = _status_for_kql(res)\n        st.message = (res.error or \"\")[:300]\n        return {}, st\n\n    index: dict[str, dict[str, Any]] = {}",
        "    if False:\n        st.status = _status_for_kql(res)\n        st.message = (res.error or \"\")[:300]\n        return {}, st\n\n    index: dict[str, dict[str, Any]] = {}",
    ),
    (
        ARG,
        "app/iam/arg.py",
        "throttling is not distinguished from a generic failure",
        '    if any(t in err for t in ("429", "throttl", "toomanyrequests", "rate limit")):\n        return schema.STATUS_THROTTLED',
        '    if False:\n        return schema.STATUS_THROTTLED',
    ),
    (
        ARG,
        "app/iam/arg.py",
        "a capped sweep is reported as complete",
        "    if not res.complete:\n        st.status = schema.STATUS_PARTIAL\n        st.message = f\"Capped at {MAX_ROLE_DEF_ROWS} role definitions.\"",
        "    if False:\n        st.status = schema.STATUS_PARTIAL\n        st.message = f\"Capped at {MAX_ROLE_DEF_ROWS} role definitions.\"",
    ),
    (
        ARG,
        "app/iam/arg.py",
        "RBAC-authorization Key Vaults are included, double-counting every grant",
        '        if str(rbac_flag).strip().lower() == "true":\n            continue',
        '        if False:\n            continue',
    ),
    (
        ARG,
        "app/iam/arg.py",
        "an expired change-feed window is treated as 'nothing changed'",
        "    if datetime.now(timezone.utc) - since_dt > timedelta(days=CHANGE_RETENTION_DAYS):",
        "    if False:",
    ),
    (
        ARG,
        "app/iam/arg.py",
        "a capped change feed is trusted as the full changed set",
        '    if not res.complete:\n        return None, "change feed result was capped, so the changed set is not trustworthy"',
        '    if False:\n        return None, "change feed result was capped, so the changed set is not trustworthy"',
    ),
    (
        ARG,
        "app/iam/arg.py",
        "a failed change feed is treated as 'nothing changed'",
        "    if not res.ok:\n        return None, (res.error or \"change feed unavailable\")[:200]",
        "    if False:\n        return None, (res.error or \"change feed unavailable\")[:200]",
    ),
    (
        ARG,
        "app/iam/orchestrator.py",
        "a subscription Resource Graph returned nothing for is trusted as empty",
        "        return self.usable and bool(self.assignments.get(scope))",
        "        return self.usable",
    ),
    (
        ARG,
        "app/iam/cache.py",
        "verifying a scope stamps it as freshly collected",
        '    entry["verified_at"] = _now_iso()',
        '    entry["generated_at"] = _now_iso()\n    entry["verified_at"] = _now_iso()',
    ),
    (
        ARG,
        "app/iam/orchestrator.py",
        "a scope whose last collection failed is skipped by delta refresh",
        'if not meta or meta.get("status") in schema.UNTRUSTWORTHY_STATUSES:',
        "if not meta:",
    ),
    (
        ARG,
        "app/iam/orchestrator.py",
        "delta compares against the newest collection instead of the oldest",
        "        if not oldest or gen < oldest:",
        "        if not oldest or gen > oldest:",
    ),
    (
        ARG,
        "app/iam/orchestrator.py",
        "a merely degraded (Partial) scope forces a full re-collect",
        'if not meta or meta.get("status") in schema.UNTRUSTWORTHY_STATUSES:',
        'if not meta or meta.get("status") in schema.ATTENTION_STATUSES:',
    ),
    (
        ARG,
        "app/iam/schema.py",
        "Partial is treated as having produced no trustworthy rows",
        "UNTRUSTWORTHY_STATUSES = frozenset(\n    {STATUS_UNAUTHORIZED, STATUS_THROTTLED, STATUS_FAILED}\n)",
        "UNTRUSTWORTHY_STATUSES = frozenset(\n    {STATUS_PARTIAL, STATUS_UNAUTHORIZED, STATUS_THROTTLED, STATUS_FAILED}\n)",
    ),
    (
        ARG,
        "app/iam/orchestrator.py",
        "the PIM licence memo latches on any failure, not just a licence verdict",
        'if st.status == schema.STATUS_SKIPPED and "licen" in (st.message or "").lower():',
        "if st.status != schema.STATUS_SUCCEEDED:",
    ),
    (
        ARG,
        "app/iam/orchestrator.py",
        "a sweep that cannot name its roles is trusted anyway",
        "    if total and unnamed / total > UNNAMED_ROLE_TOLERANCE:",
        "    if False:",
    ),
    (
        ARG,
        "app/iam/orchestrator.py",
        "built-in role definitions are not merged in from ARM",
        "        out.role_defs.update(builtin)",
        "        pass",
    ),
    # ---- P4: the effective-permission engine --------------------------------------------
    (
        EFF,
        "app/iam/effective.py",
        "a deny assignment is evaluated after role assignments instead of before",
        "    if dec.denying:\n        dec.verdict = DENIED",
        "    if False:\n        dec.verdict = DENIED",
    ),
    (
        EFF,
        "app/iam/effective.py",
        "doNotApplyToChildScopes is ignored, so every deny cascades",
        '    if _truthy(row.get("doNotApplyToChildScopes")):',
        "    if False:",
    ),
    (
        EFF,
        "app/iam/effective.py",
        "notActions veto other roles instead of subtracting from their own",
        "        if excluded:\n            # notActions is a subtraction from THIS role only",
        "        if excluded:\n            return dec  # notActions is a subtraction from THIS role only",
    ),
    (
        EFF,
        "app/iam/effective.py",
        "control-plane actions are consulted for a data-plane question",
        "        if plane == PLANE_DATA:",
        "        if False:",
    ),
    (
        EFF,
        "app/iam/effective.py",
        "an assignment below the target scope is treated as granting",
        "    return t.startswith(a + \"/\")",
        "    return t.startswith(a) or a.startswith(t)",
    ),
    (
        EFF,
        "app/iam/effective.py",
        "scope prefixes are compared as raw strings (/subscriptions/abc covers /subscriptions/abcdef)",
        "    return t.startswith(a + \"/\")",
        "    return t.startswith(a)",
    ),
    (
        EFF,
        "app/iam/effective.py",
        "an unevaluated ABAC condition is reported as allowed",
        "        if str(row.get(\"condition\", \"\")).strip():\n            conditioned.append(ref)\n            continue",
        "        if False:\n            conditioned.append(ref)\n            continue",
    ),
    (
        EFF,
        "app/iam/effective.py",
        "an uncollected role definition is treated as granting nothing",
        "    if dec.unknown_roles:\n        dec.verdict = INDETERMINATE",
        "    if False:\n        dec.verdict = INDETERMINATE",
    ),
    (
        EFF,
        "app/iam/effective.py",
        "PIM-eligible access is reported as current access",
        "        if row.get(\"assignmentState\") == schema.STATE_ELIGIBLE:",
        "        if False:",
    ),
    (
        EFF,
        "app/iam/effective.py",
        "regex metacharacters in an action pattern are not escaped",
        'return re.compile("^" + ".*".join(re.escape(p) for p in parts) + "$", re.IGNORECASE)',
        'return re.compile("^" + ".*".join(parts) + "$", re.IGNORECASE)',
    ),
    (
        EFF,
        "app/iam/effective.py",
        "who_can merges indeterminate principals into the allowed list",
        "        elif dec.verdict == INDETERMINATE:\n            indeterminate.append(entry)",
        "        elif dec.verdict == INDETERMINATE:\n            allowed.append(entry)",
    ),
    (
        EFF,
        "app/iam/effective.py",
        "an Entra directory role is reported as an unresolved ARM role",
        "    if surface == schema.SURFACE_ENTRA:",
        "    if False:",
    ),
    (
        EFF,
        "app/iam/effective.py",
        "a Key Vault access policy is treated as a control-plane grant",
        '        return "grant" if plane == PLANE_DATA else "skip"',
        '        return "grant"',
    ),
    (
        EFF,
        "app/iam/effective.py",
        "classic administrator names are matched literally, so CoAdministrator never matches",
        '        name = _squash(str(row.get("roleName", "")))',
        '        name = str(row.get("roleName", "")).lower()',
    ),
    (
        EFF,
        "app/iam/effective.py",
        "every non-ARM surface is skipped, losing classic-admin and Key Vault access",
        '    return "resolve"',
        '    return "skip"',
    ),
    (
        EFF,
        "app/iam/effective.py",
        "the decider tie-break is arbitrary, so identical queries can disagree",
        '        dec.granting.sort(key=lambda r: (-_scope_depth(str(r.get("scope", ""))), str(r.get("roleName", ""))))',
        '        dec.granting.sort(key=lambda r: -_scope_depth(str(r.get("scope", ""))))',
    ),
    # ---- P5: the escalation graph ---------------------------------------------------------
    (
        ESC,
        "app/iam/escalation.py",
        "an edge pointing at a missing node is emitted, blanking the Cytoscape canvas",
        '        if edge["source"] not in present or edge["target"] not in present:\n            dropped += 1\n            continue',
        "        if False:\n            dropped += 1\n            continue",
    ),
    (
        ESC,
        "app/iam/escalation.py",
        "self-loops are emitted",
        '        if edge["source"] == edge["target"]:\n            dropped += 1\n            continue',
        "        if False:\n            dropped += 1\n            continue",
    ),
    (
        ESC,
        "app/iam/escalation.py",
        "fan-out is unbounded, so one principal draws 224 arrows",
        "        if count > MAX_FAN_OUT:",
        "        if False:",
    ),
    (
        ESC,
        "app/iam/escalation.py",
        "the weaker edge wins, masking a high-confidence path",
        '            if rank > _CONFIDENCE_RANK.get(existing["data"]["confidence"], 0):',
        "            if False:",
    ),
    (
        ESC,
        "app/iam/escalation.py",
        "limitations are not published, so 'we could not look' reads as 'no paths exist'",
        "    graph = _finish(nodes, edges, limitations=limitations, fan_out_total=fan_out_total)",
        "    graph = _finish(nodes, edges, limitations=[], fan_out_total=fan_out_total)",
    ),
    (
        ESC,
        "app/iam/escalation.py",
        "min_confidence is ignored",
        '            if _CONFIDENCE_RANK[prim["confidence"]] < min_rank:\n                continue',
        "            if False:\n                continue",
    ),
    (
        ESC,
        "app/iam/escalation.py",
        "already-privileged principals are not flagged, so Owners are reported as escalating",
        "        already_tier0 = any(",
        "        already_tier0 = False and any(",
    ),
    (
        ESC,
        "app/iam/escalation.py",
        "a loose federated-credential subject is not detected",
        '    if "*" in low:',
        "    if False:",
    ),
    (
        ESC,
        "app/iam/escalation.py",
        "a pull_request federated subject is accepted",
        '    if "pull_request" in low:',
        "    if False:",
    ),
    (
        ESC,
        "app/iam/signals.py",
        "the signal registry silently skips a pillar module",
        '        if info.name.startswith("_"):\n            continue',
        '        if info.name.startswith("_") or info.name == "esc":\n            continue',
    ),
    # ---- P6: shadow access / RBAC bypass --------------------------------------------------
    (
        BYP,
        "app/iam/bypass/specs.py",
        "an absent property is read as 'disabled', reporting a wide-open estate as locked down",
        'def _is_false(value: Any) -> bool:\n    """Explicitly false. Anything else — including absent — is NOT false."""\n    return str(value).strip().lower() == "false"',
        'def _is_false(value: Any) -> bool:\n    """Explicitly false. Anything else — including absent — is NOT false."""\n    return str(value).strip().lower() in ("false", "", "none", "null")',
    ),
    (
        BYP,
        "app/iam/bypass/specs.py",
        "an absent disableLocalAuth is read as local auth being off",
        'def _is_true(value: Any) -> bool:\n    return str(value).strip().lower() == "true"',
        'def _is_true(value: Any) -> bool:\n    return str(value).strip().lower() != "false"',
    ),
    (
        BYP,
        "app/iam/bypass/service.py",
        "a failed sweep reports zero findings instead of a failure per family",
        "    if not res.ok:",
        "    if False:",
    ),
    (
        BYP,
        "app/iam/bypass/service.py",
        "a family with no resources is flagged as needing attention",
        "            st.status = schema.STATUS_SKIPPED\n            st.message = \"No resources of this type were returned.\"",
        "            st.status = schema.STATUS_FAILED\n            st.message = \"No resources of this type were returned.\"",
    ),
    (
        BYP,
        "app/iam/bypass/service.py",
        "the RBAC-only percentage is 0 rather than None when nothing was assessed",
        '        "rbac_only_pct": round(100 * clean / assessed) if assessed else None,',
        '        "rbac_only_pct": round(100 * clean / assessed) if assessed else 0,',
    ),
    (
        BYP,
        "app/iam/bypass/service.py",
        "environment does not modulate severity",
        "    if any(m in env for m in _PROD_MARKERS):\n        sev = _BUMP[sev]",
        "    if False:\n        sev = _BUMP[sev]",
    ),
    (
        BYP,
        "app/iam/bypass/service.py",
        "an unavailable reachability join is indistinguishable from nobody holding the credential",
        '                    "reachabilityAvailable": bool(reachability_available and spec.credential_action),',
        '                    "reachabilityAvailable": True,',
    ),
    (
        BYP,
        "app/iam/bypass/service.py",
        "reachableBy is not filtered to the scopes that cover the resource",
        '                if not effective.scope_covers(h["scope"], rid):\n                    continue',
        "                if False:\n                    continue",
    ),
    (
        BYP,
        "app/iam/bypass/service.py",
        "the 'door not the room' scope limitation is dropped",
        '    limitations = [\n        "This reports the door, not the room.',
        '    limitations = [\n        "" and "This reports the door, not the room.',
    ),
    (
        BYP,
        "app/iam/signal_defs/byp.py",
        "bypass signals return no findings instead of 'not measured' when the sweep never ran",
        "    ctx.require(\n        bool(ctx.bypass_rows) or ctx.bypass_assessed > 0,",
        "    ctx.require(\n        True or bool(ctx.bypass_rows) or ctx.bypass_assessed > 0,",
    ),
    (
        BYP,
        "app/iam/bypass/service.py",
        "a principal is recorded only at the first scope that grants the action",
        '                    holders.append({"principalId": pid, "principalName": names.get(pid, pid), "scope": scope})\n        out[action] = holders',
        '                    holders.append({"principalId": pid, "principalName": names.get(pid, pid), "scope": scope})\n                    break\n        out[action] = holders',
    ),
    (
        BYP,
        "app/iam/bypass/service.py",
        "reachableCount counts principal/scope pairs rather than people",
        "                if h[\"principalId\"] in seen_principals:\n                    continue",
        "                if False:\n                    continue",
    ),
    (
        BYP,
        "app/iam/cache.py",
        "a never-run sweep reads back an empty summary instead of a well-formed one",
        '        "summary": payload.get("summary") or _empty_bypass_summary(),',
        '        "summary": payload.get("summary") or {},',
    ),
    # ---- P7: the governance workflow ---------------------------------------------------
    (
        GOV,
        "app/iam/diff.py",
        "the diff keys on principalId, so group-membership changes go unseen",
        '    return str(\n        row.get("effectivePrincipalId")\n        or row.get("effectivePrincipalName")\n        or row.get("principalId")\n        or ""\n    ).lower()',
        '    return str(row.get("principalId") or "").lower()',
    ),
    (
        GOV,
        "app/iam/diff.py",
        "assignmentState is dropped from the key, hiding PIM activations",
        '        str(row.get("assignmentState", "")),\n    ))',
        '        "",\n    ))',
    ),
    (
        GOV,
        "app/iam/diff.py",
        "an unknown custom role is tiered as granting nothing, inventing de-escalations",
        "    # Unknown custom role: assume it grants something. Assuming otherwise manufactures\n    # de-escalations out of roles nobody has classified.\n    return TIER_WRITE",
        "    return TIER_NONE",
    ),
    (
        GOV,
        "app/iam/diff.py",
        "path_changed is only looked for among key differences, where it can never appear",
        '        if orow.get("accessPath") != nrow.get("accessPath"):\n            changes.append(_entry(PATH_CHANGED, orow, nrow))',
        '        if False:\n            changes.append(_entry(PATH_CHANGED, orow, nrow))',
    ),
    (
        GOV,
        "app/iam/attribution.py",
        "the Activity Log window is not clamped to real retention",
        "    allowed = min(asked, ACTIVITY_LOG_RETENTION_DAYS)",
        "    allowed = asked",
    ),
    (
        GOV,
        "app/iam/attribution.py",
        "an ambiguous scope match names one of several possible actors",
        "    if len(candidates) != 1:\n        return dict(UNKNOWN_ACTOR)",
        "    if not candidates:\n        return dict(UNKNOWN_ACTOR)",
    ),
    (
        GOV,
        "app/iam/attribution.py",
        "an unmatched change is left blank rather than explicitly unknown",
        '    "changeSource": SOURCE_UNKNOWN,\n    "confidence": "unknown",',
        '    "changeSource": "",\n    "confidence": "unknown",',
    ),
    (
        GOV,
        "app/iam/remediation.py",
        "a reduce revokes the wide role before granting the narrow one",
        '            f"# 1. Grant the narrower role FIRST so access is never interrupted.\\n"\n            f"az role assignment create --assignee {_q(principal)} --role {_q(target_role)} --scope {_q(scope)}\\n"\n            f"# 2. Only then remove the wider one.\\n"\n            f"az role assignment delete --assignee {_q(principal)} --role {_q(current)} --scope {_q(scope)}"',
        '            f"az role assignment delete --assignee {_q(principal)} --role {_q(current)} --scope {_q(scope)}\\n"\n            f"az role assignment create --assignee {_q(principal)} --role {_q(target_role)} --scope {_q(scope)}"',
    ),
    (
        GOV,
        "app/iam/remediation.py",
        "group-derived access is not ordered before direct assignments",
        '    group_first = 0 if path and path != "Direct" else 1',
        "    group_first = 1",
    ),
    (
        GOV,
        "app/iam/remediation.py",
        "a generated artifact containing a credential is emitted instead of refused",
        "    for pat in _SECRET_PATTERNS:\n        if pat.search(text):",
        "    for pat in _SECRET_PATTERNS:\n        if False:",
    ),
    (
        GOV,
        "app/iam/frameworks.py",
        "a control nothing measured is reported as passing",
        '        if entry["measured_signals"] == 0:\n            entry["state"] = NOT_MEASURED',
        '        if False:\n            entry["state"] = NOT_MEASURED',
    ),
    (
        GOV,
        "app/iam/campaigns.py",
        "a reviewer is assigned to certify their own access",
        "    for reviewer, source in candidates:\n        if reviewer.lower() != subject:\n            return reviewer, source",
        "    for reviewer, source in candidates:\n        return reviewer, source",
    ),
    (
        GOV,
        "app/iam/campaigns.py",
        "deny assignments are put in front of a reviewer as if they granted access",
        '    live = [r for r in rows if r.get("effect") != schema.EFFECT_DENY]',
        "    live = list(rows)",
    ),
    (
        GOV,
        "app/iam/campaigns.py",
        "an unknown selector kind silently matches nothing instead of failing",
        '        raise CampaignError(f"unknown selector kind {kind!r}")',
        "        out = []",
    ),
    (
        GOV,
        "app/iam/campaigns.py",
        "the evidence pack stops saying undecided items were not approved",
        '            "Undecided items were NOT approved. A campaign that closed with undecided items is "\n            "recorded as incomplete and the count is above.",',
        '            "All items were reviewed.",',
    ),
    (
        GOV,
        "app/iam/signal_defs/drift.py",
        "drift signals report no findings instead of 'not measured' with no baseline",
        "    ctx.require(\n        ctx.drift_available,",
        "    ctx.require(\n        True or ctx.drift_available,",
    ),
    (
        GOV,
        "app/iam/signal_defs/drift.py",
        "after-hours is judged in raw UTC rather than the reader's local time",
        "    return (stamp.astimezone(timezone.utc) + timedelta(minutes=offset_minutes)).hour",
        "    return stamp.astimezone(timezone.utc).hour",
    ),
    (
        GOV,
        "app/iam/signal_defs/drift.py",
        "an unattributed change is accused of being a self-grant",
        '        if not actor_id or actor_id != str(c.get("principalId", "")).lower():',
        '        if actor_id != str(c.get("principalId", "")).lower():',
    ),
    (
        GOV,
        "app/iam/store.py",
        "the run is pruned before its id exists, wiping the snapshot it just wrote",
        "        await db.flush()\n        assert run.id,",
        "        assert True or run.id,",
    ),
    (
        GOV,
        "app/iam/store.py",
        "a pinned run loses its rows when the buffer rolls",
        "                IamScanRun.pinned.is_(False),\n                IamScanRun.id != run.id,",
        "                IamScanRun.id != run.id,",
    ),
    # ---- P8: CIEM ----------------------------------------------------------------------
    (
        CIEM,
        "app/iam/usage.py",
        "an unmeasured usage payload is treated as measured, so unused means never used",
        '    if payload.get("measured") is False:\n        return False',
        "    if False:\n        return False",
    ),
    (
        CIEM,
        "app/iam/usage.py",
        "the usage window is not clamped to Activity Log retention",
        "    allowed = min(asked, MAX_WINDOW_DAYS)",
        "    allowed = asked",
    ),
    (
        CIEM,
        "app/iam/usage.py",
        "confidence ignores a window too short to see the workload's cadence",
        "    if cadence_days and window_days < cadence_days:",
        "    if False:",
    ),
    (
        CIEM,
        "app/iam/usage.py",
        "zero recorded events still reaches high confidence",
        "    if events == 0:\n        return MEDIUM, (",
        "    if False:\n        return MEDIUM, (",
    ),
    (
        CIEM,
        "app/iam/usage.py",
        "break-glass accounts are not recognised",
        "    return any(marker in haystack for marker in BREAK_GLASS_MARKERS)",
        "    return False",
    ),
    (
        CIEM,
        "app/iam/usage.py",
        "a wildcard is counted as a member of the action universe rather than a claim over it",
        '            if text and "*" not in text:\n                out.add(text)',
        "            if text:\n                out.add(text)",
    ),
    (
        CIEM,
        "app/iam/rightsize.py",
        "unmeasured usage yields an empty recommendation list instead of saying so",
        "    if not usage.is_measured(usage_payload):",
        "    if False:",
    ),
    (
        CIEM,
        "app/iam/rightsize.py",
        "a break-glass principal is given a removal recommendation",
        "        if usage.is_break_glass(row, break_glass):",
        "        if False:",
    ),
    (
        CIEM,
        "app/iam/rightsize.py",
        "data-plane roles are right-sized even though the log cannot see data-plane activity",
        "        if role.data_actions and not data_plane_logged:\n            continue",
        "        if False:\n            continue",
    ),
    (
        CIEM,
        "app/iam/rightsize.py",
        "a role whose permissions were never collected wins the set-cover search",
        "        if not role.known:",
        "        if False:",
    ),
    (
        CIEM,
        "app/iam/rightsize.py",
        "Owner is proposed as the narrower replacement role",
        '    usable = [r for r in catalogue if not r.is_custom and r.role_name.lower() not in NEVER_PROPOSE]',
        "    usable = [r for r in catalogue if not r.is_custom]",
    ),
    (
        CIEM,
        "app/iam/rightsize.py",
        "a narrower scope is proposed that does not contain all the observed activity",
        "    if not candidates or len(candidates) < len(exercised_scopes):",
        "    if False:",
    ),
    (
        CIEM,
        "app/iam/rightsize.py",
        "the scope prefix is not truncated to an addressable boundary",
        "    return _truncate_to_scope(shared)",
        '    return "/" + "/".join(shared) if shared else ""',
    ),
    (
        CIEM,
        "app/iam/simulator.py",
        "an unknown change kind is ignored instead of raising",
        "    if kind not in CHANGE_KINDS:",
        "    if False:",
    ),
    (
        CIEM,
        "app/iam/simulator.py",
        "a change against an id that does not exist is applied silently",
        '            raise MissingReferent(f"no assignment {change.assignment_id!r} in this snapshot")',
        "            pass",
    ),
    (
        CIEM,
        "app/iam/simulator.py",
        "a required field on a change is not enforced",
        "    if missing:",
        "    if False:",
    ),
    (
        CIEM,
        "app/iam/simulator.py",
        "grants are keyed by access, so a duplicate path hides the revocation entirely",
        "    before_keys = {_grant_key(r): r for r in rows}\n    after_keys = {_grant_key(r): r for r in after}",
        "    before_keys = {diff_mod.row_key(r): r for r in rows}\n    after_keys = {diff_mod.row_key(r): r for r in after}",
    ),
    (
        CIEM,
        "app/iam/simulator.py",
        "retention is checked at the exact scope rather than any covering one",
        '        if effective.scope_covers(str(candidate.get("scope", "")), scope):',
        '        if str(candidate.get("scope", "")) == scope:',
    ),
    (
        CIEM,
        "app/iam/simulator.py",
        "privileged rows are sampled away",
        "    chosen = always + rng.sample(rest, min(keep, len(rest)))",
        "    chosen = rng.sample(items, threshold)",
    ),
    (
        CIEM,
        "app/iam/signal_defs/ciem.py",
        "CIEM signals return no findings instead of 'not measured' when usage was never collected",
        "    ctx.require(\n        usage.is_measured(ctx.usage),",
        "    ctx.require(\n        True or usage.is_measured(ctx.usage),",
    ),
    (
        CIEM,
        "app/iam/signal_defs/ciem.py",
        "owner-used-as-reader is asserted from an empty activity log",
        "        actions = used.get(pid)\n        if not actions:",
        "        actions = used.get(pid)\n        if False:",
    ),
    (
        CIEM,
        "app/iam/signal_defs/rolehygiene.py",
        "role hygiene reports clean when role definitions were never collected",
        "    ctx.require(\n        bool(defs),",
        "    ctx.require(\n        True or bool(defs),",
    ),
    (
        CIEM,
        "app/iam/signal_defs/rolehygiene.py",
        "group-expanded rows are counted against the assignment limit",
        "        if r.get(\"accessPath\") == schema.PATH_GROUP:\n            # Group-expanded rows are not separate assignments",
        "        if False:\n            # Group-expanded rows are not separate assignments",
    ),
    (
        CIEM,
        "app/iam/signal_defs/rolehygiene.py",
        "a subset of a much larger built-in is called an equivalent, recommending Owner",
        "            if _jaccard(mine, theirs) < BUILTIN_EQUIVALENT_SIMILARITY and len(theirs) > len(mine) * 3:",
        "            if False:",
    ),
    # ---- disabled-but-entitled: every one of these turns a blind spot into a clean bill ----
    (
        DIS,
        "app/iam/schema.py",
        "an unknown account state counts as disabled (or, inverted, disabled reads as fine)",
        "    return str(row.get(\"principalAccountEnabled\") or \"\") == ENABLED_FALSE",
        "    return str(row.get(\"principalAccountEnabled\") or \"\") != ENABLED_TRUE",
    ),
    (
        DIS,
        "app/iam/schema.py",
        "a row nobody stamped defaults to ENABLED rather than unknown",
        "        elif col in (\"principalAccountEnabled\", \"principalOnPremSynced\"):\n            row[col] = values.get(col) or ENABLED_UNKNOWN",
        "        elif col in (\"principalAccountEnabled\", \"principalOnPremSynced\"):\n            row[col] = values.get(col) or ENABLED_TRUE",
    ),
    (
        DIS,
        "app/iam/compose.py",
        "account state is keyed on the ASSIGNEE, hiding every disabled member of a group",
        "        pid = str(r.get(\"effectivePrincipalId\") or r.get(\"principalId\") or \"\").strip().lower()\n        ptype = str(r.get(\"effectivePrincipalType\") or r.get(\"principalType\") or \"\")",
        "        pid = str(r.get(\"principalId\") or \"\").strip().lower()\n        ptype = str(r.get(\"effectivePrincipalType\") or r.get(\"principalType\") or \"\")",
    ),
    (
        DIS,
        "app/iam/leavers.py",
        "an uncollected tenant reports a measured, empty result instead of a wall",
        "    if not measured:",
        "    if False:",
    ),
    (
        DIS,
        "app/iam/leavers.py",
        "owning a service principal is not treated as live-now access",
        "                \"tier\": TIER_LIVE if owner_rows else TIER_RESTORABLE,",
        "                \"tier\": TIER_RESTORABLE,",
    ),
    (
        DIS,
        "app/iam/orchestrator.py",
        "a refresh that collected no account state deletes the cached map",
        "    if principal_state:\n        return {str(k): dict(v) for k, v in principal_state.items()}",
        "    if True:\n        return {str(k): dict(v) for k, v in (principal_state or {}).items()}",
    ),
    (
        DIS,
        "app/iam/collectors.py",
        "a capped or failed disabled sweep still declares the remainder enabled",
        "        elif complete:",
        "        elif True:",
    ),
    (
        DIS,
        "app/iam/signal_defs/hyg.py",
        "the disabled signals report a clean pass when account state was never collected",
        "    return bool(ctx.directory.get(\"principal_state\"))",
        "    return True",
    ),
    (
        DIS,
        "app/iam/orchestrator.py",
        "group members are excluded from the account-state lookup, hiding the very case the feature exists for",
        "    state_seed: list[dict[str, Any]] = list(principals)",
        "    state_seed: list[dict[str, Any]] = list(principals)\n    groups = {}",
    ),
    (
        DIS,
        "app/iam/signal_defs/hyg.py",
        "recycle-bin principals are reported as ordinary orphans, whose remediation advice is wrong for 30 days",
        "        if isinstance(s, dict) and s.get(\"deletedDateTime\")",
        "        if isinstance(s, dict) and False",
    ),
    (
        DIS,
        "app/iam/campaigns.py",
        "the disabled campaign selector sweeps in principals whose state is merely unknown",
        "        out = [r for r in live if schema.is_disabled(r)]",
        "        out = [r for r in live if not schema.is_disabled(r) or True]",
    ),
    (
        DIS,
        "app/iam/leavers.py",
        "an unmeasured sign-in history is reported as 'never signed in'",
        "    if not known:\n        return DORMANCY_UNKNOWN, None",
        "    if not known:\n        return DORMANCY_NEVER, None",
    ),
    (
        DIS,
        "app/iam/leavers.py",
        "an unparseable sign-in timestamp is reported as 'never' rather than unknown",
        "    if when is None:\n        return DORMANCY_UNKNOWN, None",
        "    if when is None:\n        return DORMANCY_NEVER, None",
    ),
    (
        DIS,
        "app/iam/leavers.py",
        "a capped grant list is not flagged, so a truncated table reads as complete",
        '"grantDetailTruncated": len(prows) > MAX_GRANT_DETAIL,',
        '"grantDetailTruncated": False,',
    ),
    (
        DIS,
        "app/iam/leavers.py",
        "the ARM structure of each scope is dropped again, so the Where panel cannot group",
        '"resources": _resource_tree(prows),',
        '"resources": [],',
    ),
    (
        DIS,
        "app/iam/leavers.py",
        "an unknown on-prem sync state is filed under cloud-only",
        '    if on_prem == ON_PREM_CLOUD:\n        out = [i for i in out if i.get("onPremSynced") == schema.ENABLED_FALSE]',
        '    if on_prem == ON_PREM_CLOUD:\n        out = [i for i in out if i.get("onPremSynced") != schema.ENABLED_TRUE]',
    ),
    # NOTE: there is deliberately no mutation for "drop the activityMeasured gate while keeping
    # activityConclusive". `activityConclusive` is computed from `covered`, which already requires
    # `usage["available"]` — the same flag `activityMeasured` is built from — so conclusive implies
    # measured and that mutation is EQUIVALENT code, not an uncaught bug. It is unkillable by
    # construction, and adding a test that appeared to kill it would only be testing a
    # hand-built dict that the pipeline cannot produce. The gate stays in the source because it
    # states the intent; the two mutations below are the ones that carry real behaviour.
    (
        DIS,
        "app/iam/campaigns.py",
        "the campaign selector ignores the screen's filters, covering far more than was shown",
        "        rollup_keys = {",
        "        rollup_keys = {} and {",
    ),
    (
        DIS,
        "app/iam/campaigns.py",
        "an identity-level filter with no tenant is silently dropped instead of raising",
        "            if not tenant_id:\n                raise CampaignError(",
        "            if False:\n                raise CampaignError(",
    ),
    (
        DIS,
        "app/iam/leavers.py",
        "an explicit selection REPLACES the filters, so a stale id resurrects an excluded identity",
        '    ids = q.get("principal_ids")\n    if ids:\n        wanted = {str(x).lower() for x in ids}\n        out = [i for i in out if str(i.get("principalId", "")).lower() in wanted]',
        '    ids = q.get("principal_ids")\n    if ids:\n        wanted = {str(x).lower() for x in ids}\n        return [i for i in identities if str(i.get("principalId", "")).lower() in wanted]',
    ),
    (
        DIS,
        "app/iam/leavers.py",
        "escalation counts every node on a path, flagging the machinery instead of the principal",
        '        key = str(path.get("from") or "").lower()',
        '        key = str(path.get("to") or path.get("from") or "").lower()',
    ),
    (
        DIS,
        "app/iam/leavers.py",
        "a TRUNCATED activity log is treated as complete, so a prefix proves disuse",
        '        if "truncated" in payload\n        else any(usage.TRUNCATION_MARKER in str(n).lower() for n in notes)',
        "        if True\n        else any(usage.TRUNCATION_MARKER in str(n).lower() for n in notes)",
    ),
    (
        DIS,
        "app/iam/usage.py",
        "the truncation marker matches the bare word, so a note DENYING truncation trips it",
        'TRUNCATION_MARKER = "and was truncated"',
        'TRUNCATION_MARKER = "truncated"',
    ),
    (
        DIS,
        "app/iam/leavers.py",
        "a usage window that closes before the account died still yields a 'never used' verdict",
        '        covered = bool(usage["available"] and window_start and best and best >= window_start)',
        '        covered = bool(usage["available"])',
    ),
    (
        DIS,
        "app/iam/leavers.py",
        "'never used' drops the conclusiveness gate and matches on absence alone",
        '            if i.get("activityMeasured")\n            and i.get("activityConclusive")\n            and not i.get("lastActivity")',
        '            if i.get("activityMeasured")\n            and not i.get("lastActivity")',
    ),
    # --- the generated script must target the API that actually governs the access.
    # Reported from a real run: 522 of 527 grants on a live tenant would have produced a command
    # that exits cleanly and removes nothing.
    (
        DIS,
        "app/iam/remediation.py",
        "group-derived access is revoked as an ARM assignment, which matches nothing",
        '    if str(row.get("accessPath", "")) == schema.PATH_GROUP:\n        return PLANE_GROUP_MEMBERSHIP',
        "    if False:\n        return PLANE_GROUP_MEMBERSHIP",
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "an Entra directory role is revoked through ARM, where the role does not exist",
        "    if surface == schema.SURFACE_ENTRA:\n        return PLANE_ENTRA_ROLE",
        "    if False:\n        return PLANE_ENTRA_ROLE",
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "service-principal ownership is treated as a role assignment",
        '    if str(row.get("accessPath", "")) == schema.PATH_OWNER:\n        return PLANE_SP_OWNER',
        "    if False:\n        return PLANE_SP_OWNER",
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "a PIM-eligible assignment is deleted like an active one, so eligibility survives",
        '    if str(row.get("assignmentState", "")) == schema.STATE_ELIGIBLE:\n        return PLANE_PIM_ELIGIBLE',
        "    if False:\n        return PLANE_PIM_ELIGIBLE",
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "one membership removal is emitted once per role, so all but the first step fail",
        '    ordered = sorted(_fold_duplicates(actions), key=lambda a: (a.get("order_hint", 99), a.get("label", "")))',
        '    ordered = sorted(actions, key=lambda a: (a.get("order_hint", 99), a.get("label", "")))',
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "a multi-line dry run leaves its later lines uncommented and runnable",
        '        dry_lines = [f"{comment}   {ln}" for ln in str(a["dry_run"]).splitlines() or [""]]',
        "        dry_lines = [f\"{comment}   {a['dry_run']}\"]",
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "the owner removal uses a CLI verb that does not exist (`az ad sp owner remove`)",
        "        cmd = f\"az rest --method DELETE --url {_q(f'{owners_url}/{owner}/$ref')}  # {quoting}\"",
        '        cmd = f"az ad sp owner remove --id {_q(sp)} --owner-object-id {_q(owner)}"',
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "an AD-mastered group still gets a runnable removal that can only ever fail",
        "    if synced_state == schema.ENABLED_TRUE:",
        "    if False:",
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "the run-time guard is dropped, so an unchecked group can be targeted directly",
        "        cmd = guarded",
        "        cmd = f\"az ad group member remove --group {_q(group)} --member-id {_q(member)}\"",
    ),
    (
        DIS,
        "app/iam/compose.py",
        "group sync state is keyed on the MEMBER, so a synced group reads as editable",
        '        gid = str(r.get("membershipGroupId") or r.get("sourceGroupId") or "").strip().lower()',
        '        gid = str(r.get("effectivePrincipalId") or "").strip().lower()',
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "the folded step stops naming what it removes, hiding the blast radius",
        '        cover_lines = [f"{comment}   removes: {c}" for c in covers] if len(covers) > 1 else []',
        "        cover_lines = []",
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "the AD-mastered step hands an az CLI line to a PowerShell script",
        '            dry = f"(Get-AzADGroup -ObjectId {_dq(group)}).OnPremisesSyncEnabled  # {known}"',
        '            dry = f"az ad group show --group {_q(group)} --query onPremisesSyncEnabled  # {known}"',
    ),
    (
        DIS,
        "app/iam/collectors.py",
        "nested groups are discarded from the expansion, so the nesting is invisible",
        '                "nested": [m.get("id", "") for m in members if _is_group(m) and m.get("id")],',
        '                "nested": [],',
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "the removal targets the assignment group, not the group the membership is in",
        '    group = str(row.get("membershipGroupId") or "")',
        '    group = ""',
    ),
    (
        DIS,
        "app/iam/compose.py",
        "a member sitting in two sibling groups is resolved by picking one of them",
        "    if len(deepest) != 1:",
        "    if False:",
    ),
    (
        DIS,
        "app/iam/compose.py",
        "a child group that could not be expanded is assumed to hold no membership",
        "    incomplete = any(n not in groups for n in nested)",
        "    incomplete = False",
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "ARM commands stop naming the subscription, so they run against the operator's default",
        "    sub = str(row.get(\"subscriptionId\") or \"\")\n    if not sub:\n        return \"\"",
        '    sub = ""\n    if not sub:\n        return ""',
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "the PIM request name goes back to a placeholder ARM cannot parse",
        "    request_name = str(uuid.uuid4())",
        '    request_name = "<new-guid>"',
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "the AdminRemove request stops naming WHICH eligibility it removes",
        '        props.append(f\'"targetRoleEligibilityScheduleInstanceId":"{instance_id}"\')',
        "        pass",
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "folding keys on the assignment group, so one removal is emitted once per parent group",
        "            f\"{str(row.get('membershipGroupId') or row.get('sourceGroupId') or '')}\"",
        "            f\"{str(row.get('sourceGroupId') or '')}\"",
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "a role-assignable group still gets a CLI removal that 403s for everyone, GA included",
        '    if str(row.get("membershipGroupRoleAssignable") or "") == schema.ENABLED_TRUE:',
        "    if False:",
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "a dynamic group is told to delete a membership that is computed from a rule",
        '    if str(row.get("membershipGroupDynamic") or "") == schema.ENABLED_TRUE:',
        "    if False:",
    ),
    (
        DIS,
        "app/iam/collectors.py",
        "the group's writability properties are never read, so every group reads as ordinary",
        '                "roleAssignable": _tristate(props.get("isAssignableToRole")) if props else schema.ENABLED_UNKNOWN,',
        '                "roleAssignable": schema.ENABLED_FALSE,',
    ),
    (
        DIS,
        "app/iam/compose.py",
        "writability is read from the principal directory, where a nested child never appears",
        '            mgrp = groups.get(mid) or {}',
        "            mgrp = {}",
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "the Graph sign-in block is dropped, so the steps that need it cannot be run",
        '    revoke_lines = _header("REVOKE", "Every step here has an undo in the rollback script.") + signin',
        '    revoke_lines = _header("REVOKE", "Every step here has an undo in the rollback script.")',
    ),
    (
        DIS,
        "app/iam/remediation.py",
        "the rollback omits the sign-in, so the undo cannot run in a fresh session",
        '        "Run these to restore the access the revoke script removed. Reverse order of removal.",\n    ) + signin',
        '        "Run these to restore the access the revoke script removed. Reverse order of removal.",\n    )',
    ),
]


def run_tests(target: str) -> bool:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "-p", "no:randomly", "-x"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return proc.returncode == 0


def main() -> int:
    for target in sorted({m[0] for m in MUTATIONS}):
        if not run_tests(target):
            print(f"baseline is already failing for {target} — fix that first")
            return 1

    survivors = []
    for target, rel, desc, old, new in MUTATIONS:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"SKIP  {desc}\n      anchor not found in {rel}: {old!r}")
            survivors.append(desc)
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            caught = not run_tests(target)
        finally:
            path.write_text(original, encoding="utf-8")
        print(f"{'caught' if caught else 'SURVIVED':>9}  {desc}")
        if not caught:
            survivors.append(desc)

    if survivors:
        print(f"\n{len(survivors)} mutation(s) survived — the suite does not actually guard these:")
        for s in survivors:
            print(f"  - {s}")
        return 1
    print(f"\nall {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
