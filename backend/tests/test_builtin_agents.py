"""Tests for the curated starter sub-agents that ship pre-loaded with the product.

The agents are bundled in the package (``app/automations/builtin_agents.json``) and seeded
into the registry on first run only (when it is empty), so a fresh install/deploy comes up
with a full Azure troubleshooting team. The seed is portable: every agent carries an empty
provider/model/connection_id so it inherits the deployment's active LLM provider + default
Azure connection at run time.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.automations import agents


@pytest.fixture()
def temp_registry(monkeypatch):
    """Point the agents registry at a throwaway file so tests never touch real data."""
    tmp = Path(tempfile.mkdtemp()) / "custom_agents.json"
    monkeypatch.setattr(agents, "_PATH", tmp)
    return tmp


def test_builtin_seed_file_is_valid_and_portable():
    """The bundled seed file exists, is non-trivial, and contains no environment-specific
    identity (no baked connection id, provider, model, or dev author)."""
    seed = agents._read_builtin_seed()
    assert len(seed) >= 15
    for aid, a in seed.items():
        assert aid.startswith("builtin-"), aid
        assert a.get("name", "").strip(), aid
        assert a.get("instructions", "").strip(), aid
        # Portable: must NOT bake in a tenant connection or a specific provider/model.
        assert not a.get("connection_id"), f"{aid} leaks a connection id"
        assert not a.get("provider"), f"{aid} pins a provider"
        assert not a.get("model"), f"{aid} pins a model"


def test_seed_if_empty_populates_registry(temp_registry):
    n = agents.seed_if_empty()
    assert n >= 15
    listed = agents.list_agents()
    assert len(listed) == n
    for a in listed:
        assert a["provider"] == ""
        assert a["connection_id"] == ""
        assert a["enabled"] is True
        assert a["category"] in agents._CATEGORY_IDS
        assert a["run_mode"] in ("review", "autonomous")


def test_seed_if_empty_is_idempotent(temp_registry):
    first = agents.seed_if_empty()
    assert first >= 15
    # Second call must be a no-op (registry no longer empty).
    assert agents.seed_if_empty() == 0
    assert len(agents.list_agents()) == first


def test_deleted_starter_stays_deleted(temp_registry):
    agents.seed_if_empty()
    listed = agents.list_agents()
    victim = listed[0]["id"]
    assert agents.delete_agent(victim) is True
    # Re-seeding must NOT resurrect a deleted starter (registry is non-empty).
    assert agents.seed_if_empty() == 0
    assert agents.get_agent(victim) is None
    assert len(agents.list_agents()) == len(listed) - 1


def test_seed_skips_when_registry_already_has_agents(temp_registry):
    agents.upsert_agent({"name": "My Custom Agent", "instructions": "do things"})
    # A pre-existing agent means first run already happened — don't seed starters.
    assert agents.seed_if_empty() == 0
    names = [a["name"] for a in agents.list_agents()]
    assert names == ["My Custom Agent"]


def test_seeded_agents_cover_multiple_categories(temp_registry):
    agents.seed_if_empty()
    cats = {a["category"] for a in agents.list_agents()}
    # The team spans the core Azure domains, not just one bucket.
    assert {"networking", "compute", "data", "security", "operations", "cost"}.issubset(cats)
