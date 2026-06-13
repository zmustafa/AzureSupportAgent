"""Azure Policy endpoints.

A comprehensive, read-first governance API: live inventory + scope tree + effective-policy
resolver + compliance, the deterministic advisors (promote-to-deny, exemption hygiene,
remediation gaps, conflicts) computed alongside the inventory, plus the AI advisors
(what-if impact, natural-language authoring, explain, deny-triage, coverage-gap proposals,
safe-rollout, policy-as-code drift, tag governance). Snapshots power posture-over-time and
drift-since-last-scan. Admin-only; all Azure access is read-only.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.core.azure_connections import resolve_connection
from app.core.security import Principal, require_admin
from app.policy import advisors, baselines, collector
from app.policy import registry as policy_registry
from app.workloads.registry import get_workload
from app.assessments import catalog

router = APIRouter(prefix="/policy", tags=["policy"])
logger = logging.getLogger("app.api.policy")


def _actor(p: Principal) -> str:
    return p.display_name or p.email or p.subject


def _conn(connection_id: str | None) -> dict[str, Any] | None:
    return resolve_connection(connection_id)


# ============================================================ Phase 1: read-only inventory
@router.get("/baselines")
async def get_baselines(_: Principal = Depends(require_admin)):
    return {"baselines": baselines.list_baselines()}


@router.get("/inventory")
async def get_inventory(
    connection_id: str | None = None,
    with_compliance: int = 0,
    force: int = 0,
    workload_id: str | None = None,
    principal: Principal = Depends(require_admin),
):
    """Full policy inventory + scope tree + deterministic advisors. Optionally include a
    (slower) compliance scan so promote-to-deny can confirm zero-breakage candidates.

    When ``workload_id`` is given, the whole view is scoped to that Azure Workload: only
    policies that *govern* the workload (assigned at, above via inheritance, or inside any
    of its scopes — including its subscriptions' management-group ancestor chain) are
    returned, compliance is restricted to the workload's subscriptions, and a ``workload``
    summary block is attached.

    Server-cached PERMANENTLY (per tenant + connection + workload + compliance flag) so the
    slow Azure round-trip runs only once until refreshed. ``force=1`` bypasses cache."""
    tid = principal.tenant_id
    cid = connection_id or ""
    wid = workload_id or ""
    want_compliance = bool(with_compliance)

    if not force:
        hit = policy_registry.get_inventory_cache(tid, cid, want_compliance, wid)
        if hit:
            return {**hit["payload"], "cached": True, "fetched_at": hit["fetched_at"], "age_seconds": hit["age_seconds"]}

    conn = _conn(connection_id)
    inv = await collector.collect_inventory(conn)

    # --- Workload scoping ------------------------------------------------------------
    workload_block: dict[str, Any] | None = None
    workload_subs: list[str] = []
    if wid:
        wl = get_workload(wid)
        if wl is None:
            raise HTTPException(status_code=404, detail="Workload not found.")
        wconn = _conn(connection_id or wl.get("connection_id"))
        wscope = await collector.resolve_workload_scopes(wl, wconn)
        scope_ids = wscope["scope_ids"]
        if scope_ids:
            inv["assignments"] = [a for a in inv["assignments"] if collector.scope_governs(a["scope"], scope_ids)]
            inv["exemptions"] = [e for e in inv["exemptions"] if collector.scope_governs(e["scope"], scope_ids)]
            inv["counts"]["assignments"] = len(inv["assignments"])
            inv["counts"]["exemptions"] = len(inv["exemptions"])
        workload_subs = wscope["subscriptions"]
        workload_block = {
            "id": wid,
            "name": wl.get("name", "workload"),
            "subscription_count": wscope["subscription_count"],
            "resource_group_count": wscope["resource_group_count"],
            "resource_count": wscope["resource_count"],
            "ancestor_management_groups": wscope["ancestor_management_groups"],
            "scope_ids": sorted(scope_ids)[:50],
            "error": wscope["error"],
        }

    scope_tree = collector.build_scope_tree(inv["assignments"], inv["exemptions"])

    compliance: dict[str, Any] = {"available": False, "by_assignment": {}}
    if want_compliance:
        subs = workload_subs if wid else await collector.discover_subscriptions(conn)
        compliance = await collector.compliance_summary(conn, subs)

    det = {
        "promote_to_deny": advisors.promote_to_deny_candidates(inv["assignments"], compliance),
        "exemption_hygiene": advisors.exemption_hygiene(inv["exemptions"], ""),
        "remediation_gaps": advisors.remediation_gaps(inv["assignments"]),
        "conflicts": advisors.detect_conflicts(inv["assignments"]),
    }
    payload = {
        "connection_id": cid,
        **inv,
        "scope_tree": scope_tree,
        "compliance": compliance,
        "advisors": det,
        "workload": workload_block,
    }
    fetched_at = policy_registry.set_inventory_cache(tid, cid, want_compliance, payload, wid)
    return {**payload, "cached": False, "fetched_at": fetched_at, "age_seconds": 0}


@router.get("/compliance")
async def get_compliance(
    connection_id: str | None = None, _: Principal = Depends(require_admin)
):
    conn = _conn(connection_id)
    subs = await collector.discover_subscriptions(conn)
    return await collector.compliance_summary(conn, subs)


class EffectiveReq(BaseModel):
    scope: str
    assignments: list[dict[str, Any]] = Field(default_factory=list)
    exemptions: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/effective")
async def post_effective(req: EffectiveReq, _: Principal = Depends(require_admin)):
    """Resolve the effective policy set at a scope from the already-fetched inventory
    (pure, no extra Azure calls)."""
    return collector.resolve_effective(req.scope, req.assignments, req.exemptions)


# ============================================================ Phase 2: AI advisors
class WhatIfReq(BaseModel):
    connection_id: str | None = None
    policy_json: str
    display_name: str = "Candidate policy"
    scope: str = ""


@router.post("/whatif")
async def post_whatif(req: WhatIfReq, _: Principal = Depends(require_admin)):
    """Translate a candidate policy into a Resource Graph predicate, count + sample the
    resources its deny would block, and score the blast radius."""
    pred = await advisors.whatif_predicate(req.policy_json)
    if not pred:
        return {"supported": False, "predicate": "", "count": 0, "sample": [], "blast": None,
                "message": "This policy rule couldn't be safely translated to a Resource Graph query."}
    conn = _conn(req.connection_id)
    result = await collector.count_resources(conn, pred, scope_id=req.scope)
    if result.get("error"):
        return {"supported": True, "predicate": pred, "count": 0, "sample": [], "blast": None,
                "message": result["error"]}
    blast = await advisors.blast_radius(req.display_name, result["count"], result["sample"])
    return {"supported": True, "predicate": pred, "count": result["count"],
            "sample": result["sample"], "blast": blast, "message": ""}


class AuthorReq(BaseModel):
    intent: str


@router.post("/author")
async def post_author(req: AuthorReq, _: Principal = Depends(require_admin)):
    out = await advisors.author_policy(req.intent)
    if not out:
        raise HTTPException(status_code=502, detail="The model did not return a valid policy.")
    return out


class ExplainReq(BaseModel):
    policy_json: str


@router.post("/explain")
async def post_explain(req: ExplainReq, _: Principal = Depends(require_admin)):
    return {"explanation": await advisors.explain_policy(req.policy_json)}


class TriageReq(BaseModel):
    error_text: str
    candidates: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/triage")
async def post_triage(req: TriageReq, _: Principal = Depends(require_admin)):
    out = await advisors.triage_deny(req.error_text, req.candidates)
    if not out:
        raise HTTPException(status_code=502, detail="Triage failed to produce a result.")
    return out


class CoverageReq(BaseModel):
    baseline_id: str
    assignments: list[dict[str, Any]] = Field(default_factory=list)
    definitions: list[dict[str, Any]] = Field(default_factory=list)
    with_proposals: bool = True
    workload_id: str = ""
    workload_name: str = ""
    connection_id: str = ""


@router.post("/coverage")
async def post_coverage(req: CoverageReq, principal: Principal = Depends(require_admin)):
    cov = baselines.coverage(req.baseline_id, req.assignments, req.definitions)
    if "error" in cov:
        raise HTTPException(status_code=400, detail=cov["error"])
    if req.with_proposals and cov.get("missing"):
        proposals = await advisors.coverage_proposals(cov["baseline_label"], cov["missing"])
        cov["proposals"] = (proposals or {}).get("proposals", [])
    else:
        cov["proposals"] = []
    # Persist this analysis so it appears in the Coverage-gap history (read-only — nothing is
    # applied to Azure; we just record the result). The summary carries the id + timestamp back
    # so the UI can highlight the run it just produced.
    saved = policy_registry.save_coverage_run(
        principal.tenant_id,
        {
            "result": cov,
            "workload_id": req.workload_id,
            "workload_name": req.workload_name,
            "connection_id": req.connection_id,
        },
        _actor(principal),
    )
    cov["id"] = saved.get("id", "")
    cov["created_at"] = saved.get("created_at", "")
    return cov


# ============================================================ saved coverage analyses (history)
@router.get("/coverage-runs")
async def get_coverage_runs(workload_id: str | None = None, principal: Principal = Depends(require_admin)):
    """List saved Coverage-gap analyses (compact summaries), newest first."""
    return {"runs": policy_registry.list_coverage_runs(principal.tenant_id, workload_id)}


@router.get("/coverage-runs/{run_id}")
async def get_one_coverage_run(run_id: str, principal: Principal = Depends(require_admin)):
    rec = policy_registry.get_coverage_run(principal.tenant_id, run_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Coverage analysis not found.")
    return {"run": rec}


@router.delete("/coverage-runs/{run_id}")
async def delete_one_coverage_run(run_id: str, principal: Principal = Depends(require_admin)):
    if not policy_registry.delete_coverage_run(principal.tenant_id, run_id):
        raise HTTPException(status_code=404, detail="Coverage analysis not found.")
    return {"ok": True}


class RolloutReq(BaseModel):
    intent: str
    policy_json: str = ""


@router.post("/rollout")
async def post_rollout(req: RolloutReq, _: Principal = Depends(require_admin)):
    out = await advisors.rollout_plan(req.intent, req.policy_json)
    if not out:
        raise HTTPException(status_code=502, detail="Rollout planning failed.")
    return out


# ============================================================ AI Safe-Rollout Planner
class SimulateReq(BaseModel):
    connection_id: str | None = None
    mode: str = "deploy"  # "deploy" (new policy) | "promote" (existing assignment) | "finding"
    # deploy mode:
    intent: str = ""  # natural-language description (AI authors the policy)
    policy_json: str = ""  # or paste policy JSON directly
    # promote mode:
    assignment_id: str = ""
    definition_id: str = ""
    current_effect: str = ""
    current_enforcement: str = ""
    display_name: str = ""
    non_compliant_resources: int = -1  # from inventory compliance, when known
    # finding mode (hand-off from an assessment):
    check_id: str = ""
    title: str = ""
    detection_predicate: str = ""  # the assessment check's KQL where-body (else resolved from check_id)
    known_impact_count: int = -1  # the assessment's exact flagged count
    known_sample: list[dict[str, Any]] = Field(default_factory=list)
    frameworks: dict[str, Any] = Field(default_factory=dict)
    remediation: str = ""
    resource_types: list[str] = Field(default_factory=list)
    workload_id: str = ""
    # target state:
    scope: str = ""
    target_effect: str = "deny"
    target_enforcement: str = "Default"


async def _simulate_events(req: SimulateReq) -> AsyncIterator[dict[str, Any]]:
    """The AI Safe-Rollout Planner orchestration as a stream of progress events.

    Yields SSE frames: ``{"event": "status", "data": {key, message, detail}}`` for each
    pipeline phase, a terminal ``{"event": "done", "data": <result>}``, or
    ``{"event": "error", "data": {message}}``. The non-streaming endpoint drains this; the
    streaming endpoint forwards the frames. Strictly read-only — nothing is applied."""

    def status(key: str, message: str, detail: str = "") -> dict[str, Any]:
        return {"event": "status", "data": json.dumps({"key": key, "message": message, "detail": detail})}

    def error(message: str) -> dict[str, Any]:
        return {"event": "error", "data": json.dumps({"message": message})}

    conn = _conn(req.connection_id)
    display_name = req.display_name or "Candidate policy"
    definition_id = req.definition_id
    policy_json = req.policy_json
    authored: dict[str, Any] | None = None
    rule_error = ""

    yield status("resolve", "Resolving the policy to simulate…",
                 f"{'Enforce finding' if req.mode == 'finding' else ('Promote existing' if req.mode == 'promote' else 'Deploy new')} → {req.target_effect} @ {collector.scope_label(req.scope) or req.scope}")

    # --- Resolve the policy body we'll reason over -------------------------------------
    if req.mode == "deploy":
        if not policy_json.strip() and req.intent.strip():
            yield status("author", "Authoring the policy with AI from your description…", req.intent.strip()[:160])
            authored = await advisors.author_policy(req.intent)
            if authored:
                policy_json = json.dumps(authored.get("policy_definition") or authored)
                display_name = authored.get("display_name") or display_name
                aliases = authored.get("aliases_used") or []
                yield status("author", "Policy authored.",
                             f"{display_name}" + (f" · aliases: {', '.join(aliases[:3])}" if aliases else ""))
        if not policy_json.strip():
            yield error("Provide a policy description or JSON.")
            return
    elif req.mode == "promote":  # fetch the existing definition's real rule for the what-if
        if not definition_id:
            yield error("Provide the existing policy's definition id.")
            return
        yield status("rule", "Fetching the live policy rule from Azure…", definition_id.split("/")[-1])
        rule = await collector.get_definition_rule(conn, definition_id)
        rule_error = rule.get("error") or ""
        if rule.get("error"):
            policy_json = ""  # non-fatal: fall back to compliance + a generic plan
            yield status("rule", "Couldn't read the policy rule — will rely on compliance data.", rule_error[:160])
        else:
            policy_json = json.dumps({"properties": {"mode": rule.get("mode"), "policyRule": rule.get("policy_rule")}})
            display_name = req.display_name or rule.get("display_name") or display_name
            yield status("rule", "Loaded the live policy rule.", f"{display_name} · mode {rule.get('mode')}")

    # --- Finding mode: map an assessment finding to a real built-in (or author a custom) ---
    finding_detection = ""
    finding_match: dict[str, Any] | None = None
    if req.mode == "finding":
        display_name = req.display_name or req.title or display_name
        # 1. The detection predicate IS the what-if predicate — no AI translation needed.
        finding_detection = req.detection_predicate or (
            catalog.detection_predicate(req.check_id) if req.check_id else ""
        )
        yield status("detect", "Using the assessment's detection logic as the policy rule.",
                     finding_detection[:180] or "(no predicate)")
        # 2. Find the real built-in policy that enforces this control.
        yield status("match", "Matching the finding to a built-in Azure Policy…", display_name)
        keywords = advisors.finding_keywords(req.title or display_name, req.resource_types)
        candidates = await collector.find_builtin_definitions(conn, keywords)
        if candidates:
            finding_match = await advisors.match_builtin_policy(
                title=req.title or display_name, description=req.remediation,
                resource_types=req.resource_types, detection=finding_detection,
                remediation=req.remediation, candidates=candidates,
            )
        if finding_match and finding_match.get("matched") and finding_match.get("definition_id"):
            definition_id = finding_match["definition_id"]
            display_name = finding_match.get("builtin_display_name") or display_name
            yield status("match", f"Matched built-in: {display_name}.",
                         f"{finding_match.get('reasoning', '')} · suggested effect: {finding_match.get('recommended_effect', req.target_effect)}")
        else:
            # 3. No built-in fit → author a custom policy from the detection + remediation.
            yield status("match", "No built-in fit — authoring a custom policy from the detection logic…")
            authored = await advisors.author_policy_from_finding(
                title=req.title or display_name, detection=finding_detection,
                remediation=req.remediation, resource_types=req.resource_types,
            )
            if authored:
                policy_json = json.dumps(authored.get("policy_definition") or authored)
                display_name = authored.get("display_name") or display_name
                yield status("match", "Authored a custom policy.", display_name)

    is_initiative = "/policysetdefinitions/" in (definition_id or "").lower()

    # --- Measure impact ----------------------------------------------------------------
    yield status("impact", "Measuring live impact at the target scope…")
    impact: dict[str, Any] = {"source": "none", "count": 0, "sample": [], "supported": False, "predicate": "", "message": ""}
    # Finding mode: the assessment already measured the exact violating set — reuse it.
    if req.mode == "finding" and req.known_impact_count >= 0:
        impact = {
            "source": "assessment", "count": req.known_impact_count, "sample": req.known_sample[:25],
            "supported": True, "predicate": finding_detection,
            "message": "Exact count from the assessment run (the resources it flagged are precisely what a deny would block).",
        }
        yield status("impact", f"Using the assessment's measured impact — {req.known_impact_count} resource(s).",
                     "Detected once by the assessment; reused here (no re-scan needed).")
        # Optionally refresh the sample within scope using the detection predicate.
        if finding_detection:
            res = await collector.count_resources(conn, finding_detection, scope_id=req.scope)
            if not res.get("error") and res.get("sample"):
                impact["sample"] = res["sample"]
    # Prefer Azure compliance for an existing audit policy being promoted (exact ground truth).
    if req.mode == "promote" and req.non_compliant_resources >= 0 and req.target_effect.lower() in ("deny", "denyaction"):
        impact = {
            "source": "compliance", "count": req.non_compliant_resources, "sample": [],
            "supported": True, "predicate": "",
            "message": "Exact count from the latest Azure compliance scan (resources currently flagged non-compliant by this audit policy).",
        }
        yield status("impact", f"Using Azure compliance data — {req.non_compliant_resources} non-compliant resource(s).",
                     "These are exactly what a deny would block.")
    # Otherwise (or additionally) derive a scope-bounded Resource Graph what-if.
    if policy_json.strip() and (impact["source"] == "none" or not impact["sample"]):
        yield status("predicate", "Translating the policy rule to a Resource Graph query…")
        pred = await advisors.whatif_predicate(policy_json)
        if pred:
            yield status("predicate", "Derived a Resource Graph predicate.", pred[:180])
            yield status("query", f"Querying live resources in {collector.scope_label(req.scope) or 'the target scope'}…")
            res = await collector.count_resources(conn, pred, scope_id=req.scope)
            if not res.get("error"):
                if impact["source"] == "none":
                    impact = {"source": "resource_graph", "count": res["count"], "sample": res["sample"],
                              "supported": True, "predicate": pred, "message": ""}
                else:
                    impact["sample"] = res["sample"]
                yield status("query", f"Found {res['count']} matching resource(s) in scope.",
                             ", ".join(sorted({s.get('resourceGroup', '') for s in res['sample'] if s.get('resourceGroup')})[:6]))
            elif impact["source"] == "none":
                impact["message"] = res["error"]
                yield status("query", "Resource Graph query failed.", res["error"][:160])
        elif impact["source"] == "none":
            impact["message"] = "This policy rule couldn't be translated to a Resource Graph query; run a compliance scan for exact impact."
            yield status("predicate", "Couldn't translate this rule to a query.", "Run a compliance scan for exact impact.")

    if impact["source"] == "none" and not impact["message"]:
        if req.mode == "promote" and req.non_compliant_resources < 0:
            impact["message"] = (
                "No impact data yet — run a compliance scan (top-right) so this audit policy's "
                "non-compliant resources can be counted exactly"
                + (f". (Couldn't read the policy rule: {rule_error})" if rule_error else ".")
            )
        elif rule_error:
            impact["message"] = f"Couldn't read the existing policy's rule: {rule_error}"
        else:
            impact["message"] = "Impact couldn't be measured automatically; run a compliance scan."

    affected_rgs = sorted({s.get("resourceGroup", "") for s in impact["sample"] if s.get("resourceGroup")})

    # --- Blast radius + tailored staged plan -------------------------------------------
    blast = None
    if impact["supported"] and impact["count"] >= 0:
        yield status("blast", "Scoring the blast radius…", f"{impact['count']} resource(s) potentially affected")
        blast = await advisors.blast_radius(display_name, impact["count"], impact["sample"])
        if blast:
            yield status("blast", f"Blast radius: {blast.get('risk_level', '?')} risk ({blast.get('risk_score', '?')}).",
                         blast.get("recommendation", ""))

    yield status("plan", "Generating the staged rollout plan with AI…",
                 "DoNotEnforce → audit → deny, tailored to the effect & scope")
    sim_context = {
        "change": "enforce_finding" if req.mode == "finding" else ("promote_existing" if req.mode == "promote" else "deploy_new"),
        "policy": display_name, "current_effect": req.current_effect, "current_enforcement": req.current_enforcement,
        "target_scope": collector.scope_label(req.scope) or req.scope,
        "target_effect": req.target_effect, "target_enforcement": req.target_enforcement,
        "impact_count": impact["count"], "impact_source": impact["source"],
        "affected_resource_groups": affected_rgs[:25], "sample": impact["sample"][:12],
    }
    if req.mode == "finding":
        sim_context["assessment_finding"] = req.title
        sim_context["compliance_frameworks"] = req.frameworks
        sim_context["remediation"] = req.remediation
    plan = await advisors.simulate_rollout(sim_context)
    if plan:
        yield status("plan", f"Rollout plan ready — {str(plan.get('go_no_go', '')).upper() or 'plan generated'}.",
                     plan.get("summary", "")[:160])

    yield status("artifacts", "Preparing copy-ready artifacts (nothing is executed)…")
    artifacts: dict[str, Any] = {}
    if definition_id:
        artifacts["assignment_json"] = advisors.assignment_json_template(
            display_name=display_name, definition_id=definition_id, scope=req.scope,
            effect=req.target_effect, enforcement_mode=req.target_enforcement,
        )
        artifacts["az_commands"] = advisors.az_cli_commands(
            name=display_name, definition_id=definition_id, scope=req.scope,
            effect=req.target_effect, enforcement_mode=req.target_enforcement, is_initiative=is_initiative,
        )
    elif authored:
        artifacts["policy_definition"] = authored.get("policy_definition")
        artifacts["aliases_used"] = authored.get("aliases_used", [])

    result = {
        "mode": req.mode,
        "display_name": display_name,
        "check_id": req.check_id,
        "workload_id": req.workload_id,
        "frameworks": req.frameworks,
        "builtin_match": finding_match,
        "current_state": {"effect": req.current_effect, "enforcement": req.current_enforcement},
        "target_state": {"scope": req.scope, "scope_label": collector.scope_label(req.scope), "effect": req.target_effect, "enforcement": req.target_enforcement},
        "impact": {**impact, "affected_resource_groups": affected_rgs},
        "blast": blast,
        "plan": plan,
        "artifacts": artifacts,
        "authored": authored,
    }
    yield {"event": "done", "data": json.dumps(result)}


@router.post("/simulate")
async def post_simulate(req: SimulateReq, _: Principal = Depends(require_admin)):
    """Non-streaming AI Safe-Rollout Planner: drains the simulation pipeline and returns
    the final result. (The UI uses the streaming variant for live progress.)"""
    result: dict[str, Any] | None = None
    async for ev in _simulate_events(req):
        if ev["event"] == "error":
            raise HTTPException(status_code=400, detail=json.loads(ev["data"]).get("message", "Simulation failed."))
        if ev["event"] == "done":
            result = json.loads(ev["data"])
    if result is None:
        raise HTTPException(status_code=502, detail="Simulation produced no result.")
    return result


@router.post("/simulate/stream")
async def post_simulate_stream(req: SimulateReq, _: Principal = Depends(require_admin)):
    """Streaming AI Safe-Rollout Planner (SSE): emits a ``status`` event for every phase of
    the simulation so the UI can show live progress, then a ``done`` event with the result."""

    async def _gen():
        try:
            async for ev in _simulate_events(req):
                yield ev
        except Exception as exc:  # noqa: BLE001
            logger.exception("Policy simulation failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


# ============================================================ saved simulations (Rollout Planner history)
class SaveSimulationReq(BaseModel):
    result: dict[str, Any]
    workload_id: str = ""
    workload_name: str = ""
    connection_id: str = ""


@router.get("/simulations")
async def get_simulations(workload_id: str | None = None, principal: Principal = Depends(require_admin)):
    """List saved Safe-Rollout simulations (compact summaries), newest first."""
    return {"simulations": policy_registry.list_simulations(principal.tenant_id, workload_id)}


@router.post("/simulations")
async def post_simulation(req: SaveSimulationReq, principal: Principal = Depends(require_admin)):
    """Persist a completed simulation so it can be reopened later. Read-only — this only
    records the simulation result; nothing is applied to Azure."""
    if not req.result:
        raise HTTPException(status_code=400, detail="result is required.")
    rec = policy_registry.save_simulation(
        principal.tenant_id,
        {
            "result": req.result,
            "workload_id": req.workload_id,
            "workload_name": req.workload_name,
            "connection_id": req.connection_id,
        },
        _actor(principal),
    )
    return {"simulation": rec}


@router.get("/simulations/{sim_id}")
async def get_one_simulation(sim_id: str, principal: Principal = Depends(require_admin)):
    rec = policy_registry.get_simulation(principal.tenant_id, sim_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Simulation not found.")
    return {"simulation": rec}


@router.delete("/simulations/{sim_id}")
async def delete_one_simulation(sim_id: str, principal: Principal = Depends(require_admin)):
    if not policy_registry.delete_simulation(principal.tenant_id, sim_id):
        raise HTTPException(status_code=404, detail="Simulation not found.")
    return {"ok": True}


# ============================================================ enforcement links (assessment bridge)
class EnforcementLinkReq(BaseModel):
    workload_id: str = ""
    check_id: str
    title: str = ""
    definition_id: str = ""
    builtin_name: str = ""
    target_effect: str = ""
    target_scope: str = ""
    go_no_go: str = ""
    plan_summary: str = ""
    impact_count: int = 0
    frameworks: dict[str, Any] = Field(default_factory=dict)


@router.post("/enforcement-link")
async def post_enforcement_link(req: EnforcementLinkReq, principal: Principal = Depends(require_admin)):
    """Record that an assessment finding has a planned Azure Policy guardrail. Powers the
    '✅ Guardrail planned' badge in the assessment report and reverse links in Policy."""
    if not req.check_id:
        raise HTTPException(status_code=400, detail="check_id is required.")
    rec = policy_registry.save_enforcement_link(
        principal.tenant_id, req.workload_id, req.check_id, req.model_dump(), _actor(principal)
    )
    return {"link": rec}


@router.get("/enforcement-links")
async def get_enforcement_links(workload_id: str | None = None, principal: Principal = Depends(require_admin)):
    links = policy_registry.list_enforcement_links(principal.tenant_id, workload_id)
    return {"links": links}


class TagGovReq(BaseModel):
    connection_id: str | None = None
    required_tags: list[str] = Field(default_factory=lambda: ["owner", "cost-center"])



@router.post("/tag-governance")
async def post_tag_governance(req: TagGovReq, _: Principal = Depends(require_admin)):
    tags = [t.strip() for t in req.required_tags if t.strip()][:8]
    if not tags:
        raise HTTPException(status_code=400, detail="Provide at least one required tag key.")
    conn = _conn(req.connection_id)
    # Resources missing ANY required tag (case-insensitive key check via tags object).
    clauses = " or ".join(f"isnull(tags['{t}']) and isnull(tags['{t.lower()}'])" for t in tags)
    res = await collector.count_resources(conn, f"({clauses})")
    proposal = await advisors.tag_governance(tags, res.get("sample", []))
    return {
        "required_tags": tags,
        "missing_count": res.get("count", 0),
        "sample": res.get("sample", []),
        "error": res.get("error", ""),
        "proposal": proposal or {},
    }


# ============================================================ Phase 3: drift / IaC / overlay
class IacSourceReq(BaseModel):
    content: str
    format: str = "epac"


@router.get("/iac-source")
async def get_iac_source(principal: Principal = Depends(require_admin)):
    src = policy_registry.get_iac_source(principal.tenant_id)
    return src or {"content": "", "format": "epac", "updated_at": ""}


@router.put("/iac-source")
async def put_iac_source(req: IacSourceReq, principal: Principal = Depends(require_admin)):
    return policy_registry.set_iac_source(principal.tenant_id, req.content, req.format, _actor(principal))


class DriftReq(BaseModel):
    assignments: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/drift")
async def post_drift(req: DriftReq, principal: Principal = Depends(require_admin)):
    src = policy_registry.get_iac_source(principal.tenant_id)
    if not src or not (src.get("content") or "").strip():
        raise HTTPException(status_code=400, detail="No policy-as-code source of truth saved yet.")
    out = await advisors.drift_reconcile(req.assignments, src["content"], src.get("format", "epac"))
    if not out:
        raise HTTPException(status_code=502, detail="Drift analysis failed.")
    return out


class ResourceComplianceReq(BaseModel):
    arm_ids: list[str] = Field(default_factory=list)
    assignments: list[dict[str, Any]] = Field(default_factory=list)
    exemptions: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/resource-policies")
async def post_resource_policies(req: ResourceComplianceReq, _: Principal = Depends(require_admin)):
    """For an architecture overlay: per ARM id, the effective deny/audit policy counts
    (which guardrails apply to each node). Pure over the posted inventory."""
    out: dict[str, Any] = {}
    for arm_id in req.arm_ids[:400]:
        eff = collector.resolve_effective(arm_id, req.assignments, req.exemptions)
        deny = sum(1 for e in eff["effective"] if (e.get("effect") or "").lower() in ("deny",))
        audit = sum(1 for e in eff["effective"] if (e.get("effect") or "").lower() in ("audit", "auditifnotexists"))
        dine = sum(1 for e in eff["effective"] if (e.get("effect") or "").lower() in ("deployifnotexists", "modify"))
        out[arm_id] = {"total": eff["count"], "deny": deny, "audit": audit, "dine": dine}
    return {"resources": out}


# ============================================================ snapshots / trends
@router.get("/snapshots")
async def get_snapshots(principal: Principal = Depends(require_admin)):
    return {"snapshots": policy_registry.list_snapshots(principal.tenant_id)}


class SnapshotReq(BaseModel):
    connection_id: str | None = None
    with_compliance: bool = True


@router.post("/snapshot")
async def post_snapshot(req: SnapshotReq, principal: Principal = Depends(require_admin)):
    conn = _conn(req.connection_id)
    inv = await collector.collect_inventory(conn)
    compliance: dict[str, Any] = {"available": False, "by_assignment": {}}
    if req.with_compliance:
        subs = await collector.discover_subscriptions(conn)
        compliance = await collector.compliance_summary(conn, subs)
    summary = advisors.summarize_for_snapshot(inv, compliance)
    prev = policy_registry.latest_snapshot(principal.tenant_id)
    snap = policy_registry.save_snapshot(principal.tenant_id, req.connection_id or "", summary, _actor(principal))
    drift = advisors.diff_snapshots(summary, prev["summary"]) if prev else None
    return {"snapshot": snap, "drift_since_previous": drift}


# ============================================================ drafts
@router.get("/drafts")
async def get_drafts(principal: Principal = Depends(require_admin)):
    return {"drafts": policy_registry.list_drafts(principal.tenant_id)}


class DraftReq(BaseModel):
    id: str | None = None
    title: str = "Untitled policy"
    kind: str = "definition"
    intent: str = ""
    policy_json: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


@router.put("/drafts")
async def put_draft(req: DraftReq, principal: Principal = Depends(require_admin)):
    payload = req.model_dump()
    payload["tenant_id"] = principal.tenant_id
    return policy_registry.save_draft(payload, _actor(principal))


@router.delete("/drafts/{draft_id}")
async def delete_draft(draft_id: str, _: Principal = Depends(require_admin)):
    if not policy_registry.delete_draft(draft_id):
        raise HTTPException(status_code=404, detail="Draft not found.")
    return {"ok": True}
