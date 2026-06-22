"""Azure Tag Intelligence.

A read-first toolkit that discovers, normalizes, governs, groups, and (on explicit
approval) remediates Azure tags across an estate. Everything is computed on top of the
existing read-only inventory payload (``app.inventory.service.collect``) and cost overlay
(``app.inventory.cost``), so Tag Intelligence shares the same Resource Graph scan and
server-side cache the Inventory screen already populates — no new Azure calls for the
analysis layer.

Modules
-------
- ``scale``       : whole-estate guardrail (5k cap, 500/batch) mirrored from workloads.autopilot.
- ``analysis``    : pure functions — census, key/value clustering, coverage, workload
                    inference, billing map, cost allocation. Operate on a list of normalized
                    resource dicts; no I/O.
- ``catalog``     : the canonical tag catalog registry (per tenant, JSON-persisted).
- ``drift``       : point-in-time tag snapshots + diff over time.
- ``policygen``   : Azure Policy (audit/append/inherit/deny) + initiative generation.
- ``remediation`` : dry-run -> preview -> approval change-sets + script/rollback generation.
- ``rbac_advice`` : static least-privilege role guidance per action.
- ``ask``         : the natural-language tag console (deterministic-first, AI-narrated).
"""
