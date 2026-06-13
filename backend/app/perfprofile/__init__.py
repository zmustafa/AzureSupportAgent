"""Performance Profiler — profile a workload against the AMBA reference.

Treats each AMBA *metric* alert as a measurement spec: fetch the resource's live metric,
compare to the AMBA threshold → % of threshold, breach state, headroom, and trend. Turns
the AMBA reference from an alert-config checklist into a live performance lens that names
the binding bottleneck. Read-only — uses `az monitor metrics list` only."""
