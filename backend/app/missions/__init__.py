"""Workload Mission Control — package.

A "mission" is one sweep over a single workload that runs many per-workload analyses
(architecture, memory, assessment, monitoring/telemetry/backup coverage, performance,
retirement radar) and rolls their outcomes into a go/warn/nogo readiness verdict.

- ``systems``: the registry of analysis "systems", each an adapter over an existing
  collector/runner, exposing ``run`` (execute) + ``last_state`` (cached freshness).
- ``orchestrator``: the in-process manager that drives a mission, streams progress, and
  persists a ``MissionRun`` row incrementally so partial progress survives a crash.
"""
