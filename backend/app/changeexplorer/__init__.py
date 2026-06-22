"""Azure Workload Change Explorer.

A read-only analysis feature: given a tenant (connection), a workload, a time range, and a
scope mode, it collects every meaningful change to the workload's Azure resources during that
window, normalizes them into a common ``ChangeEvent`` model, classifies + risk-scores +
explains them in plain English, aggregates insights, and returns a tab-ready payload the UI
renders across nine tabs (Summary, Timeline, All Changes, Risk Insights, Resources, Actors,
Technical Diff, Dependency Impact, Export).

Architecture (deterministic backend + thin AI narration):
    collectors  -> raw change rows (ARG resourcechanges + Activity Log; mockable/demo)
    normalize   -> ChangeEvent dicts (+ ChangeEventDetail before/after diffs)
    classify    -> category (one of CATEGORIES)
    risk        -> riskScore (0-100) + riskLabel + transparent factor breakdown
    deps        -> dependency role + blast-radius hint
    explain     -> plain-English what/why/impact/why-risk/confidence
    insights    -> ChangeInsight aggregates
    service     -> orchestrates the pipeline, persists a ChangeAnalysisRun, returns tabs

The LLM never queries Azure. Collectors are deterministic; any AI only narrates already
normalized events. Everything is read-only — no remediation, no writes, no rollback.
"""
