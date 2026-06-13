"""Telemetry Intelligence — AI correlation & triage over Application Insights.

Mines the App Insights telemetry customers already pay to collect: natural-language → KQL,
AI failure triage (the requests↔exceptions↔dependencies join by operation_Id), a cross-
signal correlation timeline (telemetry signals + deploy/config change events), a Smart
Detection aggregator across the workload's App Insights resources, and end-to-end
transaction reconstruction by operation_Id. Read-only — no telemetry is modified."""
