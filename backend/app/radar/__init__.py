"""Retirement & Breaking-Change Radar.

Aggregates Azure Service Health retirement events and Advisor service-upgrade/retirement
recommendations into one deadline-driven, workload-scoped, owner-mapped list — plus a
dedicated Azure OpenAI / Foundry model-lifecycle lane — so retirements and permanent
breaking changes stop being "everyone's problem = no one's problem". Reuses Resource
Graph (read-only), the assessments finding machinery, ticketing, notifications, the
automation scheduler, and the War Room handoff."""
