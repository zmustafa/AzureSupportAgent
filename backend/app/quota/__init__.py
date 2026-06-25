"""Quota Monitoring — proactive Azure quota/limit posture by subscription & region.

A modular, collector-based framework that discovers current quota usage, limits, remaining
headroom, risk level, and whether a limit appears adjustable, hard, or needs manual review —
across Compute, Network, Storage, App Service, SQL, Key Vault, Azure Monitor, AI/ML, and ARM
governance limits, plus an ARM API-throttling lane.

Design goals (mirrors the reservations / radar features):
- Modular ``IQuotaCollector`` implementations registered in a ``QuotaCollectorRegistry`` so new
  Azure services can be added without touching the orchestrator.
- A layered discovery strategy — Microsoft.Quota → resource-provider usage APIs → Azure Resource
  Graph counts → Azure Monitor metrics → documented static limits — with every result stamping
  the ``source_type`` it came from.
- Fail-soft: one collector failing never sinks the whole scan; its failure is surfaced as an
  error result so partial coverage is explicit and auditable.
- Latest snapshot cached on the data volume (``.data/quota_cache.json``); compact run history
  persisted to ``quota_scan_runs`` for trend charting.

NOTE: Quota approval does not guarantee real-time regional/SKU capacity — the two are distinct.
"""
