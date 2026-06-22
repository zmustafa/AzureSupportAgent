"""Knowledge-graph assembly for the central ``/graph`` visualization surface.

Turns the application's existing knowledge — Azure connections, subscriptions,
resource groups, resources (inventory cache), workloads, architectures, architecture
memory, and assessment findings — into a navigable node/edge graph that the frontend
renders with Cytoscape.js.

The assembler is an *aggregator of existing app knowledge*, not a fresh data silo: it
reads file-backed registries (workloads, architectures, memory), the server-side
inventory cache, and the assessment-run history in the DB. Page load is **cache-only**
(no live Azure calls); expensive estate scans stay behind the existing Inventory /
Assessment refresh buttons.
"""
