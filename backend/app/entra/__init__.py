"""Entra ID Support Agent — tenant-wide identity posture, policy and access analysis.

A second product surface (routes under ``/api/entra``) that answers *who can do what in
this tenant, what is exposed, and what breaks if I change it* — as opposed to the Azure
estate tooling which answers *what is wrong with my resources*.

Layout::

    graphclient.py      shared paged / batched / throttled Microsoft Graph client
    licenses.py         /subscribedSkus -> P1 / P2 / Governance / workload-premium flags
    permissions_probe.py token roles-claim + live probe -> per-domain blindness map
    cache.py            .data/entra/<tenant>/<domain>.json.gz  (+ index.json, state files)
    collectors/         one module per domain, each independently permission-gated
    signals.py          SignalSpec registry (the single source of truth for every check)
    signal_defs/        one module per pillar exporting SPECS
    score.py            pillar rollup, coverage normalisation, history
    ca_engine.py        Conditional Access normalisation, coverage matrix, conflicts
    snapshot.py         assembles domain payloads into one snapshot + evaluates signals
    job.py              per-(tenant, domain) background refresh with SSE progress
    demo.py             synthetic tenant so the whole area works offline

Design rules (see docs/improvement-plans/entra-support-agent/):
  * GET endpoints read cache only. ``POST /entra/refresh`` is the only collector trigger.
  * Fail-open per domain — a missing permission degrades one pillar, never the page.
  * Blind is not zero: an unmeasured pillar is EXCLUDED from the score denominator.
  * Read-only. No Graph write is ever issued and no write scope is ever requested.

Payloads are plain JSON-serialisable dicts (matching the identity/ and rbac/ modules)
rather than dataclasses, so cached sidecars round-trip without a serialisation layer.
"""
from __future__ import annotations

__all__ = ["DOMAINS"]

# Domains collected today. Adding one means: a collectors/<name>.py, an entry here, and
# (optionally) signals that declare it in ``SignalSpec.domains``.
DOMAINS: tuple[str, ...] = ("tenant", "people", "apps", "roles", "pim", "activations",
                            "ca", "risk", "governance")
