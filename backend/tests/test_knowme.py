"""Tests for the Workload Know-Me generator helpers, registry, and revisions."""
import pytest

from app.core.utils import loads_tolerant
from app.knowme import context as kctx
from app.knowme import registry as kreg
from app.knowme import revisions as krev
from app.knowme import sections as km
from app.knowme.generator import parse_completion


# ------------------------------------------------------------------ A2: typed fields
def test_classify_field_types():
    assert km.classify_field("escalation_owner", "Escalation owner")["type"] == "person"
    assert km.classify_field("oncall_email", "On-call email")["type"] == "email"
    assert km.classify_field("rto", "Recovery time objective")["type"] == "duration"
    assert km.classify_field("", "Define threshold")["group"] == "thresholds"
    assert km.classify_field("", "Define threshold")["required"] is False
    assert km.classify_field("subscription_friendly_name", "Subscription friendly name")["group"] == "scope"
    # unknown → text/other/not-required
    meta = km.classify_field("mystery", "Some field")
    assert meta == {"type": "text", "required": False, "group": "other"}


def test_parse_todos_attaches_typed_schema():
    sections = [{"key": "contacts", "content": "Owner: ⟦TODO: Escalation owner email | key=escalation_email⟧"}]
    todos = km.parse_todos(sections)
    assert len(todos) == 1
    t = todos[0]
    assert t["type"] == "email" and t["required"] is True and t["group"] == "escalation"
    assert t["source"] == "human" and t["suggestions"] == [] and t["status"] == "open"


# ------------------------------------------------------------------ A1: auto-fill
def test_autofill_fills_subscription_region_owner():
    todos = km.parse_todos([
        {"key": "subscriptions_resources", "content":
            "Sub: ⟦TODO: Subscription friendly name | key=subscription_friendly_name⟧, "
            "region ⟦TODO: confirm region | key=resource_region_confirm⟧"},
        {"key": "data_compliance_cost", "content": "Owner: ⟦TODO: Cost-center owner | key=cost_center_owner⟧"},
    ])
    known = {
        "subscriptions": {"11111111-1111-1111-1111-111111111111": "Prod Sub"},
        "regions": ["eastus2"],
        "owner": {"display_name": "Jane Doe", "email": "jane@x.com", "team": ""},
    }
    filled = kctx.autofill_todos(todos, known)
    by_key = {t["field_key"]: t for t in todos}
    assert by_key["subscription_friendly_name"]["value"] == "Prod Sub"
    assert by_key["subscription_friendly_name"]["source"] == "auto"
    assert by_key["resource_region_confirm"]["value"] == "eastus2"
    assert by_key["cost_center_owner"]["value"] == "Jane Doe"
    assert filled == 3


def test_autofill_suggests_not_fills_escalation():
    todos = km.parse_todos([
        {"key": "support_handling", "content": "On-call: ⟦TODO: On-call group | key=oncall_group⟧"},
    ])
    known = {"subscriptions": {}, "regions": [], "owner": {"display_name": "Jane", "email": "jane@x.com"}}
    filled = kctx.autofill_todos(todos, known)
    t = todos[0]
    assert filled == 0  # escalation is suggested, not auto-filled
    assert t["status"] == "open" and "jane@x.com" in t["suggestions"]


def test_autofill_multiple_regions_suggests():
    todos = km.parse_todos([
        {"key": "subscriptions_resources", "content": "⟦TODO: confirm region | key=resource_region_confirm⟧"},
    ])
    known = {"subscriptions": {}, "regions": ["eastus2", "centralus"], "owner": None}
    filled = kctx.autofill_todos(todos, known)
    assert filled == 0 and todos[0]["suggestions"] == ["eastus2", "centralus"]


# ------------------------------------------------------------------ A3: evidence block
def test_evidence_block_renders_sections():
    ev = {
        "assessment": {"score": 43, "findings": [{"title": "TLS 1.0 enabled", "severity": "error", "pillar": "security"}]},
        "coverage": {"amba": {"coverage_pct": 60.0, "gaps": ["evhns-anl"]}},
        "performance": {"score": 70, "top_bottleneck": {"resource_name": "pg-01", "metric_name": "CPU", "pct_of_threshold": 98, "state": "breaching"}},
        "idle": ["Orphaned NIC — nic-01"],
    }
    block = kctx._evidence_block(ev)
    assert "TLS 1.0 enabled" in block and "43/100" in block
    assert "Monitoring (AMBA)" in block and "60%" in block
    assert "pg-01" in block and "98%" in block
    assert "Orphaned NIC" in block


def test_evidence_block_empty():
    assert kctx._evidence_block({"assessment": None, "coverage": {}, "performance": None, "idle": []}) == ""


@pytest.mark.asyncio
async def test_gather_known_facts_no_raise_on_minimal():
    facts = {"subscriptions": [{"id": "g", "name": "Named"}], "regions": ["eastus"], "resources": []}
    out = await kctx.gather_known_facts({"id": "wl", "tags": {}}, {"id": "a"}, "t1", "", facts)
    assert out["subscriptions"] == {"g": "Named"} and out["regions"] == ["eastus"]
    assert "block" in out


@pytest.mark.asyncio
async def test_gather_evidence_no_raise_on_empty():
    ev = await kctx.gather_evidence("a", "wl", "t1", "")
    assert set(["assessment", "coverage", "performance", "idle", "block"]).issubset(ev.keys())


# ------------------------------------------------------------------ registry preserves typed schema
def test_registry_clean_todos_preserves_type_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(kreg, "_PATH", tmp_path / "know_me.json")
    monkeypatch.setattr(krev, "_PATH", tmp_path / "know_me_revisions.json")
    base = kreg.create_know_me(architecture_id="arch-1", tenant_id="t1", actor="x")
    doc = kreg.update_know_me(
        base["id"], tenant_id="t1", actor="x",
        sections=[{"key": "contacts", "content": "⟦TODO: Owner email | key=owner_email⟧"}],
        todos=[{"id": "contacts:owner_email", "field_key": "owner_email", "label": "Owner email",
                "section_key": "contacts", "status": "done", "value": "a@b.com",
                "type": "email", "required": True, "group": "contacts",
                "suggestions": ["a@b.com"], "source": "auto", "confidence": 0.9}],
    )
    t = doc["todos"][0]
    assert t["type"] == "email" and t["required"] is True and t["group"] == "contacts"
    assert t["suggestions"] == ["a@b.com"] and t["source"] == "auto" and t["confidence"] == 0.9


# ------------------------------------------------------------------ completion parsing
def test_parse_completion_clean_json():
    raw = '{"sections": {"overview": "Event-driven.", "contacts": "x"}, "confidence": 0.8}'
    out = parse_completion(raw)
    assert out["sections"]["overview"] == "Event-driven." and out["confidence"] == 0.8


def test_parse_completion_with_prose_preamble_and_fence():
    raw = (
        "I'll transform this into a Know-Me.\n\n"
        '```json\n{"sections": {"overview": "AzSupAgent is a container app."}, "confidence": 0.7}\n```'
    )
    out = parse_completion(raw)
    assert out and out["sections"]["overview"].startswith("AzSupAgent")


def test_parse_completion_salvages_unescaped_quotes_and_newlines():
    # The real-world failure: a prose preamble, a value with a literal newline + an
    # unescaped inner double-quote + a Markdown table + a ⟦TODO⟧ token — invalid JSON, but
    # the key-delimited salvage must still recover every section.
    raw = (
        'I\'ll transform this Architecture Memory into a Know-Me.\n\n'
        '{"sections": {'
        '"overview": "The "hot path" ingests events.\nSecond line.", '
        '"diagnostics_triage": "| Step | Check |\n|---|---|\n| 1 | logs |", '
        '"contacts": "Owner: ⟦TODO: Escalation owner | key=escalation_owner⟧"'
        '}, "confidence": 0.74}'
    )
    out = parse_completion(raw)
    assert out is not None, "salvage should recover sections from invalid JSON"
    secs = out["sections"]
    assert "hot path" in secs["overview"]
    assert "Step" in secs["diagnostics_triage"] and "logs" in secs["diagnostics_triage"]
    assert "escalation_owner" in secs["contacts"]
    assert out["confidence"] == 0.74


def test_parse_completion_garbage_returns_none():
    assert parse_completion("no json here, just words") is None


# ------------------------------------------------------------------ tolerant JSON parse
def test_loads_tolerant_strict_ok():
    assert loads_tolerant('{"a": 1}') == {"a": 1}


def test_loads_tolerant_raw_newlines_in_strings():
    # A Markdown table with literal newlines inside a JSON string value — invalid for
    # strict json.loads, but the most common large-completion failure mode.
    raw = '{"sections": {"overview": "line1\nline2\n| a | b |\n"}}'
    out = loads_tolerant(raw)
    assert isinstance(out, dict) and "line1" in out["sections"]["overview"]


def test_loads_tolerant_trailing_comma():
    assert loads_tolerant('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_loads_tolerant_garbage_returns_default():
    assert loads_tolerant("not json at all", default=None) is None
    assert loads_tolerant("", default={"x": 1}) == {"x": 1}


# ------------------------------------------------------------------ sections / scope
def test_catalog_keys_unique_and_default_full():
    keys = [s["key"] for s in km.SECTION_CATALOG]
    assert len(keys) == len(set(keys))
    # default_sections must cover every catalog section (Know-Me always shows all).
    assert [s["key"] for s in km.default_sections()] == km.SECTION_KEYS


def test_render_markdown_inlines_filled_todo_values():
    doc = {
        "sections": [{"key": "contacts", "content": "Owner: ⟦TODO: Owner email | key=owner_email⟧; "
                      "Pager: ⟦TODO: Pager | key=pager⟧"}],
        "todos": [{"id": "contacts:owner_email", "status": "done", "value": "jane@x.com"}],
    }
    md = km.render_markdown(doc, "AP")
    assert "jane@x.com" in md                       # filled → inlined
    assert "**[TODO: Pager]**" in md                 # open → readable marker
    assert "⟦TODO" not in md                          # raw token never leaks to the read view


def test_render_markdown_raw_when_apply_values_false():
    doc = {"sections": [{"key": "contacts", "content": "⟦TODO: Owner | key=owner⟧"}], "todos": []}
    md = km.render_markdown(doc, "AP", apply_values=False)
    assert "⟦TODO: Owner | key=owner⟧" in md


def test_architecture_to_mermaid():
    from app.knowme import assets as kassets
    arch = {"nodes": [{"id": "a", "name": "Web", "type": "microsoft.app/containerapps"},
                      {"id": "b", "name": "DB", "type": "microsoft.dbforpostgresql/flexibleservers"}],
            "edges": [{"source": "a", "target": "b", "label": "queries"}]}
    mer = kassets.architecture_to_mermaid(arch)
    assert mer.startswith("flowchart TD")
    assert "Web" in mer and "DB" in mer and "queries" in mer and "containerapps" in mer


def test_architecture_to_mermaid_empty():
    from app.knowme import assets as kassets
    assert kassets.architecture_to_mermaid({"nodes": [], "edges": []}) == ""


def test_asset_save_read_delete_roundtrip(monkeypatch, tmp_path):
    from app.knowme import assets as kassets
    monkeypatch.setattr(kassets, "_ROOT", tmp_path / "knowme_assets")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    rec = kassets.save_asset("arch-1", data=png, content_type="image/png", filename="shot.png")
    assert rec["ref"].startswith("asset:") and rec["markdown"].startswith("![shot.png](asset:")
    got = kassets.read_asset("arch-1", rec["id"])
    assert got is not None and got[0] == png and got[1] == "image/png"
    # data-URI inlining replaces the token
    md = kassets.inline_asset_data_uris("arch-1", f"see ![x](asset:{rec['id']})")
    assert "data:image/png;base64," in md and "asset:" not in md
    # a resized image's ``?w=<px>`` width hint must be consumed (not corrupt the data-URI)
    md2 = kassets.inline_asset_data_uris("arch-1", f"![x](asset:{rec['id']}?w=120)")
    assert "data:image/png;base64," in md2 and "?w=" not in md2 and "asset:" not in md2
    assert kassets.delete_asset("arch-1", rec["id"]) is True
    assert kassets.read_asset("arch-1", rec["id"]) is None


def test_asset_rejects_unsupported_type(monkeypatch, tmp_path):
    from app.knowme import assets as kassets
    monkeypatch.setattr(kassets, "_ROOT", tmp_path / "knowme_assets")
    with pytest.raises(ValueError):
        kassets.save_asset("a", data=b"x", content_type="application/zip")


def test_section_label_fallback():
    assert km.section_label("overview") == "Workload overview"
    assert km.section_label("my_custom") == "My Custom"


def _workload():
    return {
        "name": "AnalyticsPlatform",
        "nodes": [
            {"kind": "subscription", "id": "/subscriptions/11111111-1111-1111-1111-111111111111",
             "name": "Demo Sub", "subscription_id": "11111111-1111-1111-1111-111111111111"},
            {"kind": "resource", "id": "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-anl/providers/Microsoft.EventHub/namespaces/evhns-anl",
             "name": "evhns-anl", "resource_type": "microsoft.eventhub/namespaces",
             "subscription_id": "11111111-1111-1111-1111-111111111111", "resource_group": "rg-anl", "location": "eastus2"},
        ],
    }


def test_scope_facts_extracts_subs_rgs_regions_resources():
    facts = km.scope_facts(_workload(), {"nodes": [{"name": "stanl-007", "type": "microsoft.storage/storageaccounts"}]})
    assert facts["subscriptions"] == [{"id": "11111111-1111-1111-1111-111111111111", "name": "Demo Sub"}]
    assert facts["resource_groups"] == ["rg-anl"]
    assert facts["regions"] == ["eastus2"]
    names = {r["name"] for r in facts["resources"]}
    assert "evhns-anl" in names and "stanl-007" in names  # arch node merged in


def test_scope_facts_block_includes_guid():
    block = km.scope_facts_block(km.scope_facts(_workload(), None))
    assert "11111111-1111-1111-1111-111111111111" in block
    assert "rg-anl" in block and "eastus2" in block


def test_scope_facts_empty_workload():
    facts = km.scope_facts(None, None)
    assert facts["subscriptions"] == [] and facts["resource_groups"] == []
    assert "⟦TODO⟧" in km.scope_facts_block(facts)


# ------------------------------------------------------------------ TODO parsing
def test_parse_todos_extracts_field_key_and_dedupes():
    sections = [
        {"key": "contacts", "content": "Owner: ⟦TODO: Escalation owner | key=escalation_owner⟧\nAgain: ⟦TODO: Escalation owner | key=escalation_owner⟧"},
        {"key": "support_handling", "content": "Coverage: ⟦TODO: On-call coverage (UTC)⟧"},
    ]
    todos = km.parse_todos(sections)
    ids = [t["id"] for t in todos]
    assert len(ids) == len(set(ids))  # deduped
    owner = next(t for t in todos if t["field_key"] == "escalation_owner")
    assert owner["section_key"] == "contacts" and owner["status"] == "open"
    # token without explicit key still parses (slug-based id)
    assert any(t["label"].startswith("On-call coverage") for t in todos)


def test_render_markdown_skips_empty_and_titles():
    doc = {"sections": [
        {"key": "overview", "label": "Workload overview", "content": "Event-driven."},
        {"key": "contacts", "label": "Contacts", "content": ""},
    ]}
    md = km.render_markdown(doc, "AnalyticsPlatform")
    assert "# Know-Me — AnalyticsPlatform" in md
    assert "## Workload overview" in md and "Event-driven." in md
    assert "## Contacts" not in md  # empty section skipped


# ------------------------------------------------------------------ content cleaning (P0)
def test_strip_leading_heading_dedupes_section_label():
    # A section whose body repeats its label as a heading must not render the heading twice.
    out = km.strip_leading_heading("## Contacts\n\nCustomer + support.", "Contacts")
    assert out == "Customer + support."
    # A non-matching heading is preserved.
    keep = km.strip_leading_heading("## Net read\n\nbody", "Contacts")
    assert keep.startswith("## Net read")
    # Heading that starts with the label (e.g. "Solution / architecture overview") is stripped.
    out2 = km.strip_leading_heading("## Solution / architecture overview\n\nx", "Solution / architecture overview")
    assert out2 == "x"


def test_is_placeholder_mermaid():
    assert km.is_placeholder_mermaid("flowchart TD\n  A[Client] --> B[Service]\n  B --> C[(Database)]")
    assert km.is_placeholder_mermaid("")  # empty == placeholder
    # A real diagram with named resources is NOT a placeholder.
    real = "flowchart TD\n  N0[\"evhns-anl<br/><small>Event Hub</small>\"] --> N1[\"stanl-007\"]"
    assert not km.is_placeholder_mermaid(real)


def test_render_markdown_strips_placeholder_diagram_and_dup_heading():
    doc = {"sections": [
        {"key": "overview", "label": "Workload overview",
         "content": "## Workload overview\n\n```mermaid\nflowchart TD\n  A[Client] --> B[Service]\n```\n\nReal text."},
    ]}
    md = km.render_markdown(doc, "AP")
    assert md.count("## Workload overview") == 1  # heading not duplicated
    assert "A[Client]" not in md  # placeholder diagram stripped
    assert "Real text." in md


def test_render_markdown_cover_adds_toc_and_meta():
    doc = {
        "title": "Know-Me — AP", "status": "published", "description": "Prod reference.",
        "sections": [{"key": "overview", "label": "Workload overview", "content": "Body."}],
    }
    md = km.render_markdown(doc, "AP", cover=True)
    assert "## Contents" in md and "- [Workload overview](#workload-overview)" in md
    assert "**Status:** Published" in md and "_Prod reference._" in md


def test_classify_field_network_and_identity_groups():
    assert km.classify_field("vnet_cidr", "VNet address space & subnet CIDRs")["group"] == "network"
    assert km.classify_field("private_endpoint_ip", "PE private IPs")["group"] == "network"
    assert km.classify_field("managed_identity", "Managed identity principal")["group"] == "identity"
    assert km.classify_field("rbac_role", "RBAC role assignment")["group"] == "identity"


# ------------------------------------------------------------------ choice sets (P1/P2)
def test_infer_choices_yesno_and_enums():
    yn = km.infer_choices("geo_backup", "Is geo-redundant backup enabled?")
    assert yn and yn["choices"][:2] == ["Yes", "No"] and yn["allow_custom"] is False
    crit = km.infer_choices("criticality", "Business criticality")
    assert crit and crit["choices"] == ["Critical", "High", "Medium", "Low"] and crit["allow_custom"] is False
    red = km.infer_choices("redundancy", "Storage redundancy")
    assert red and "ZRS" in red["choices"] and red["allow_custom"] is True
    env = km.infer_choices("environment", "Environment")
    assert env and "Production" in env["choices"]
    # A free-text field (name/email) has no rule choice set.
    assert km.infer_choices("escalation_owner", "Escalation owner email") is None


def test_parse_todos_attaches_rule_choices():
    sections = [{"key": "resiliency_dr", "content": "RTO: ⟦TODO: Business criticality | key=criticality⟧"}]
    todos = km.parse_todos(sections)
    t = todos[0]
    assert t["choices"] == ["Critical", "High", "Medium", "Low"]
    assert t["allow_custom"] is False and t["choice_source"] == "rule"


def test_parse_todos_token_choices_take_precedence():
    # AI-emitted choices on the token win over the rule enum.
    sections = [{"key": "resiliency_dr",
                 "content": "⟦TODO: Storage redundancy | key=redundancy | choices=ZRS; GZRS; LRS⟧"}]
    t = km.parse_todos(sections)[0]
    assert t["choices"] == ["ZRS", "GZRS", "LRS"] and t["choice_source"] == "ai"


def test_todo_re_still_renders_token_with_choices():
    # apply_todo_values must consume the whole token (incl. choices) — never leak it.
    md = km.apply_todo_values(
        "Pick: ⟦TODO: Storage redundancy | key=redundancy | choices=ZRS; GRS⟧", "resiliency_dr", {}, mark_open=True,
    )
    assert "**[TODO: Storage redundancy]**" in md and "choices=" not in md


def test_registry_persists_choices(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    kid = kreg.create_know_me(architecture_id="arch-1", tenant_id="t1", actor="x")["id"]
    doc = kreg.update_know_me(
        kid, tenant_id="t1", actor="x",
        sections=[{"key": "resiliency_dr", "content": "⟦TODO: Business criticality | key=criticality⟧"}],
        todos=[{"id": "resiliency_dr:criticality", "field_key": "criticality", "label": "Business criticality",
                "section_key": "resiliency_dr", "status": "open", "value": "",
                "choices": ["Critical", "High", "Medium", "Low"], "allow_custom": False, "choice_source": "rule"}],
    )
    t = doc["todos"][0]
    assert t["choices"] == ["Critical", "High", "Medium", "Low"]
    assert t["allow_custom"] is False and t["choice_source"] == "rule"




# ------------------------------------------------------------------ merge
def test_merge_ai_sections_full_catalog_order_and_overwrite():
    existing = km.default_sections()
    existing[0]["content"] = "old overview"
    merged = kreg.merge_ai_sections(existing, {"overview": "new overview", "diagnostics_triage": "check X"})
    assert [s["key"] for s in merged] == km.SECTION_KEYS  # full canonical order
    by_key = {s["key"]: s for s in merged}
    assert by_key["overview"]["content"] == "new overview"  # overwritten
    assert by_key["diagnostics_triage"]["content"] == "check X"
    assert by_key["contacts"]["content"] == ""  # untouched stays empty


def test_merge_ai_sections_empty_content_does_not_overwrite():
    existing = km.default_sections()
    existing[0]["content"] = "keep me"
    merged = kreg.merge_ai_sections(existing, {"overview": "   "})
    assert {s["key"]: s for s in merged}["overview"]["content"] == "keep me"


# ------------------------------------------------------------------ registry round-trip
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(kreg, "_PATH", tmp_path / "know_me.json")
    monkeypatch.setattr(krev, "_PATH", tmp_path / "know_me_revisions.json")


def test_registry_upsert_get_and_revision(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    base = kreg.create_know_me(architecture_id="arch-1", workload_id="wl-1", workload_name="AP", tenant_id="t1", actor="tester")
    kid = base["id"]
    doc = kreg.update_know_me(
        kid, workload_id="wl-1", workload_name="AP", tenant_id="t1", actor="tester",
        sections=[{"key": "overview", "content": "v1"}], reason="Generated with AI",
    )
    assert doc["architecture_id"] == "arch-1" and doc["status"] == "draft"
    got = kreg.get_know_me(kid)
    assert got and got["workload_name"] == "AP"
    revs = krev.list_revisions(kid)
    assert len(revs) == 1 and revs[0]["reason"] == "Generated with AI"


def test_registry_dedupes_identical_revision(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    kid = kreg.create_know_me(architecture_id="arch-1", tenant_id="t1", actor="x")["id"]
    kreg.update_know_me(kid, sections=[{"key": "overview", "content": "same"}], tenant_id="t1", actor="x")
    kreg.update_know_me(kid, sections=[{"key": "overview", "content": "same"}], tenant_id="t1", actor="x")
    assert len(krev.list_revisions(kid)) == 1  # no-op save dedups


def test_registry_restore_revision(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    kid = kreg.create_know_me(architecture_id="arch-1", tenant_id="t1", actor="x")["id"]
    kreg.update_know_me(kid, sections=[{"key": "overview", "content": "first"}], tenant_id="t1", actor="x")
    first_rev = krev.list_revisions(kid)[0]["id"]
    kreg.update_know_me(kid, sections=[{"key": "overview", "content": "second"}], tenant_id="t1", actor="x")
    restored = kreg.restore_revision(kid, first_rev, actor="x")
    assert restored and {s["key"]: s for s in restored["sections"]}["overview"]["content"] == "first"


def test_registry_soft_delete_and_restore(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    kid = kreg.create_know_me(architecture_id="arch-1", tenant_id="t1", actor="x")["id"]
    kreg.update_know_me(kid, sections=[{"key": "overview", "content": "x"}], tenant_id="t1", actor="x")
    assert kreg.soft_delete(kid, "x") is True
    assert kreg.list_know_me("t1") == []  # hidden from active list
    assert kreg.list_know_me("t1", include_deleted=True)  # still there
    assert kreg.restore(kid) is not None
    assert len(kreg.list_know_me("t1")) == 1


def test_registry_multiple_per_architecture(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    a = kreg.create_know_me(architecture_id="arch-1", title="Draft A", tenant_id="t1", actor="x")
    b = kreg.create_know_me(architecture_id="arch-1", title="Draft B", tenant_id="t1", actor="x")
    assert a["id"] != b["id"]
    docs = kreg.list_know_me("t1", architecture_id="arch-1")
    assert {d["id"] for d in docs} == {a["id"], b["id"]}
    # publishing one doesn't touch the other
    kreg.update_know_me(a["id"], status="published", tenant_id="t1", actor="x")
    assert kreg.get_know_me(a["id"])["status"] == "published"
    assert kreg.get_know_me(b["id"])["status"] == "draft"


def test_registry_set_reference_is_exclusive_per_workload(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    a = kreg.create_know_me(architecture_id="arch-1", workload_id="wl-1", tenant_id="t1", actor="x")
    b = kreg.create_know_me(architecture_id="arch-1", workload_id="wl-1", tenant_id="t1", actor="x")
    kreg.set_reference(a["id"], actor="x")
    assert kreg.get_know_me(a["id"])["is_reference"] is True
    # Setting b as reference clears a (only one reference per workload).
    kreg.set_reference(b["id"], actor="x")
    assert kreg.get_know_me(b["id"])["is_reference"] is True
    assert kreg.get_know_me(a["id"])["is_reference"] is False
    # Unset.
    kreg.set_reference(b["id"], is_reference=False, actor="x")
    assert kreg.get_know_me(b["id"])["is_reference"] is False


def test_registry_persists_description_and_todo_assignee(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    kid = kreg.create_know_me(architecture_id="arch-1", tenant_id="t1", actor="x")["id"]
    doc = kreg.update_know_me(
        kid, description="Prod reference", tenant_id="t1", actor="x",
        sections=[{"key": "contacts", "content": "⟦TODO: Owner | key=owner⟧"}],
        todos=[{"id": "contacts:owner", "field_key": "owner", "label": "Owner",
                "section_key": "contacts", "status": "open", "value": "",
                "assignee": "jane@x.com", "note": "ask the TAM"}],
    )
    assert doc["description"] == "Prod reference"
    t = doc["todos"][0]
    assert t["assignee"] == "jane@x.com" and t["note"] == "ask the TAM"


def test_archived_is_a_valid_status(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    kid = kreg.create_know_me(architecture_id="arch-1", tenant_id="t1", actor="x")["id"]
    kreg.update_know_me(kid, status="archived", tenant_id="t1", actor="x")
    assert kreg.get_know_me(kid)["status"] == "archived"


def test_registry_purge_and_empty_trash(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    a = kreg.create_know_me(architecture_id="arch-1", tenant_id="t1", actor="x")["id"]
    b = kreg.create_know_me(architecture_id="arch-1", tenant_id="t1", actor="x")["id"]
    kreg.update_know_me(a, sections=[{"key": "overview", "content": "x"}], tenant_id="t1", actor="x")
    assert kreg.purge(a) is True  # hard-delete one
    assert kreg.get_know_me(a) is None and krev.list_revisions(a) == []
    kreg.soft_delete(b, "x")
    assert kreg.empty_trash("t1") == 1
    assert kreg.get_know_me(b) is None


def test_registry_prune_orphans(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    keep = kreg.create_know_me(architecture_id="arch-keep", tenant_id="t1", actor="x")["id"]
    gone = kreg.create_know_me(architecture_id="arch-gone", tenant_id="t1", actor="x")["id"]
    kreg.update_know_me(gone, sections=[{"key": "overview", "content": "y"}], tenant_id="t1", actor="x")
    pruned = kreg.prune_orphans({"arch-keep"})
    assert pruned == 1
    assert kreg.get_know_me(gone) is None and kreg.get_know_me(keep) is not None
    assert krev.list_revisions(gone) == []  # revisions cascaded


def test_registry_migration_rekeys_by_id(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    import json as _json
    # Simulate the OLD architecture-keyed layout: top-level key == architecture_id.
    legacy = {"know_me": {"arch-legacy": {
        "id": "km-legacy-1", "architecture_id": "arch-legacy", "tenant_id": "t1",
        "sections": [{"key": "overview", "label": "Workload overview", "content": "old"}],
        "todos": [], "assets": [], "status": "draft", "updated_at": "2020-01-01T00:00:00+00:00",
    }}}
    (tmp_path / "know_me.json").write_text(_json.dumps(legacy), encoding="utf-8")
    got = kreg.get_know_me("km-legacy-1")  # now reachable by its id
    assert got is not None and got["architecture_id"] == "arch-legacy"
    assert kreg.get_know_me("arch-legacy") is None  # old key no longer addresses it


def test_todos_carry_value_persisted(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    kid = kreg.create_know_me(architecture_id="arch-1", tenant_id="t1", actor="x")["id"]
    doc = kreg.update_know_me(
        kid, tenant_id="t1", actor="x",
        sections=[{"key": "contacts", "content": "⟦TODO: Owner | key=owner⟧"}],
        todos=[{"id": "contacts:owner", "field_key": "owner", "label": "Owner",
                "section_key": "contacts", "status": "done", "value": "jane@x.com"}],
    )
    assert doc["todos"][0]["value"] == "jane@x.com" and doc["todos"][0]["status"] == "done"
