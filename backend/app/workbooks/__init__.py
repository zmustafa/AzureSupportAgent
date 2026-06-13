"""Workbooks: named, parameterized, AI-augmented Azure operations.

A *workbook* is a saved az-CLI / Azure Resource Graph (KQL) / PowerShell snippet with
typed parameters. Running it executes the (parameter-interpolated) snippet bound to an
Azure connection, then "AI'fies" the raw output — summarize, extract to a schema,
classify severity, diff vs the previous run. The structured result powers dashboard
tiles, notification events, and reuse from agents/playbooks.
"""
