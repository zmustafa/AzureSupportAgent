"""AMBA (Azure Monitor Baseline Alerts) Monitoring Coverage feature.

Computes how well a workload's (or subscription's) resources are covered against an
editable, versioned baseline-alert reference set. Detection runs entirely on the
read-only Azure Resource Graph path (alert rules + action groups are ARM resources), so
it never needs the gated command-execution path. Coverage snapshots are server-side
cached (the Resource Graph scans are slow), mirroring the Identity dashboard.
"""
