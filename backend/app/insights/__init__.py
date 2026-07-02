"""AI Insight Packs.

An *Insight Pack* is a reusable, scope-agnostic definition (config + natural-language AI
instructions) that, when run against a chosen scope (tenant / subscription / workload),
executes a four-stage loop:

    gather  -> pull structured data from the app's deterministic engines (Change Explorer,
               Radar, ...) for the assignment's scope + lookback.
    reason  -> ask the active LLM to interpret that data and return a structured result
               (verdict + headline + bullet findings + a normalized table).
    gate    -> decide whether the run is worth a notification (a deterministic "always
               notify" floor OR the AI verdict clearing the pack's threshold).
    deliver -> persist the digest always; notify (in-app + connectors) only when gated in.

Packs live in a JSON-backed library (``registry``); each scheduled run is a ``ScheduledTask``
with ``target_type="insight_pack"`` and ``target_config={pack_id, scope, overrides}`` (an
*assignment*). The pack definition is portable Markdown with a YAML frontmatter header — see
``packfile``.
"""
