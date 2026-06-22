# Estate Graph (/graph) — 100-Test UI Test Plan

Comprehensive UI + contract test plan covering the central knowledge graph. Each test lists
**Steps → Expected**. Status legend: ✅ pass · ❌ fail (with bug id) · ⏭ deferred.

Connections used: `khspn` (default, 8 workloads / 13 archs), `FDPO` (3 / 1), `mat` (1 / 1),
plus 3 connection-less demo workloads (Contoso Hotels, Zava x2). Automated coverage lives in
`backend/tests/test_graph_scope.py` (T1–T40 contract) + Playwright batch (UI).

## A. Tenant / Connection scoping (the reported bug)
1. Select khspn → only khspn + demo workloads (11), never FDPO/mat. ✅
2. Select FDPO → only FDPO + demo (6); zero khspn/mat workloads. ✅
3. Select mat → only mat + demo (4). ✅
4. Switch khspn→FDPO→mat→khspn → counts change each time (11→6→4→11), no stale carry-over. ✅
5. Architectures re-scope with the connection (khspn 13, FDPO 1, mat 1). ✅
6. Connection-less demo workloads appear under every connection. ✅
7. A khspn workload id is NOT buildable under FDPO (`/build` → "Workload not found"). ✅
8. A khspn workload id is NOT inspectable under FDPO (`/node` → found:false). ✅
9. Drift for a khspn workload under FDPO → found:false. ✅
10. Search under FDPO never returns khspn-only workloads. ✅
11. Search under mat never returns khspn/FDPO resources from another connection's cache. ✅
12. `_full_graph` (analytics/ask/narrative) honors connection scope. ✅
13. Two connections sharing one tenant (khspn + xxx, tenant 739fb5dd) stay separate by connection_id. ✅
14. Empty/unknown connection_id → full unscoped list (single-tenant fallback), no crash. ✅
15. Inventory cache never falls back to another connection's resources (no "" key). ✅
16. Switching connection clears the canvas (old nodes removed, not merged). ✅
17. Overview `counts.workloads` matches the scoped list, not the global 15. ✅
18. Expand of the connection node re-lists only scoped children. ✅

## B. Overview load & topology
19. Visit /graph → hierarchy renders rooted at the tenant connection. ✅
20. Edges: connection→subscription (contains), sub→workload (contains), workload→architecture (models). ✅
21. Orphan workload (no sub) attaches under the connection, never floating. ✅
22. Architecture with unknown workload hangs off connection, labelled "unlinked". ✅
23. Status strip shows N nodes · M edges and the correct counts line. ✅
24. "inventory not scanned" message when no inventory cache for the connection. ✅
25. Loading spinner shows while overview query is in flight. ✅
26. Error state renders if overview API errors (simulate 500). ⏭
27. Node count == stats.node_count from backend. ✅
28. Re-fit (Fit button / `f`) frames all nodes. ✅
29. Hierarchy vs Organic layout toggle re-lays out without losing nodes. ✅
30. Deep-link /graph/:focusId centers + selects that node when present. ✅

## C. Search
31. Type a workload name → workload result ranks first. ✅
32. Type an architecture name → architecture result appears. ✅
33. Type a resource substring → resource results appear (capped at 60). ✅
34. Empty query → no dropdown. ✅
35. Whitespace-only query → no results. ✅
36. Search is case-insensitive. ✅
37. Click a search result → node focused + centered + selected. ✅
38. Search a node NOT on canvas → it's added then focused. ✅
39. Search under FDPO does not surface khspn-only names. ✅ (dup of T10, UI side)
40. Special chars / regex-y input (`.*`, `'`, `()`) don't crash search. ✅
41. Very long query string handled gracefully. ✅
42. Rapid typing (debounce/латest-wins) doesn't wedge the dropdown. ✅

## D. Inspector & node detail
43. Click workload → dossier shows description, resources, assessment score/failing/severity. ✅
44. Workload CTA grid shows Inventory/Architecture/Assessment/Change/RBAC/Telemetry/Backup. ✅
45. CTA deep-links navigate to the right route. ✅
46. Click resource → dossier shows type/RG/location/SKU/sub/flags/workload. ✅
47. Click architecture → dossier shows workload/state/source/node+edge counts. ✅
48. Click subscription → dossier shows sub id + resource/RG counts. ✅
49. Click finding → dossier shows pillar/severity/status/rationale/remediation. ✅
50. Click tenant_connection → dossier shows tenant/auth/status. ✅
51. Inspector close (✕) clears selection. ✅
52. Inspector "Expand" adds children. ✅
53. Inspector reflects the latest selected node (switching selection updates it). ✅
54. Unknown node type → graceful "Unknown node type" / raw data. ✅

## E. Expand
55. Double-tap subscription → resource groups + workloads bloom. ✅
56. Double-tap resource group → resources appear. ✅
57. Double-tap workload → resources + arch + memory + findings + dependency edges. ✅
58. Expand is idempotent (re-expand doesn't duplicate nodes/edges). ✅
59. Expand a large RG → capped at 400, `truncated` note shown. ✅
60. Expanded children bloom in a ring around the source (stable layout). ✅
61. Expand on a leaf resource (no children) → no error, no new nodes. ✅
62. Dependency edges only drawn between resources both present on canvas. ✅

## F. Lenses
63. Risk lens colours workloads green/amber/red by risk level. ✅
64. Capability lens colours workloads by workload_type. ✅
65. Criticality lens colours workloads by criticality. ✅
66. Change lens highlights recently-changed nodes (needs change overlay). ✅
67. Cost lens colours subscriptions by spend band (needs cost overlay). ✅
68. Ownership lens colours workloads by owner/team tag hash. ✅
69. WAF lens colours findings by pillar. ✅
70. Shared-services lens highlights resources in >1 workload. ✅
71. Switching lenses re-styles instantly without reload. ✅
72. "No lens" resets to kind colours. ✅

## G. Layers & filters
73. Uncheck a layer (e.g. Resource) → those nodes hidden. ✅
74. Re-check restores them. ✅
75. Hide all kinds → empty canvas, no crash. ✅
76. Layer state persists while expanding (new nodes respect hidden set). ✅
77. Reset filters (canvas menu) restores all layers. ✅
78. Drift legend shows only when Drift mode on. ✅

## H. Path & blast modes
79. Path mode: pick source then target → shortest path highlighted, others dimmed. ✅
80. Path with no connection → "No path" status, nothing crashes. ✅
81. Path source==target → 0 hops handled. ✅
82. Blast mode: click node → direct (red) + indirect (amber), rest dimmed, impacted count. ✅
83. Blast from inspector button works. ✅
84. Blast impacted_workloads count surfaced in status. ✅
85. Esc clears path/blast highlights and returns to explore. ✅
86. Switching mode resets pending path source. ✅

## I. Overlays & drift
87. Enable Coverage overlay + Focus workload → coverage_gap nodes appear. ✅
88. Enable Retirements → retirement_item nodes appear (if radar cache). ✅
89. Enable Access (rbac) → privileged principal nodes appear (if rbac cache). ✅
90. Drift Focus → resources tagged ok/documented_missing/live_uncontrolled w/ colours. ✅
91. Drift summary surfaced in status + drift_score present. ✅
92. Toggling overlay/drift while focused re-builds the focused subgraph. ✅

## J. Analytics
93. Analytics panel: concentration risk, communities, orphans, candidate workloads render. ✅
94. Analytics honors connection scope (orphan/candidate counts differ per connection). ✅
95. Click a concentration-risk row → node focused/centered. ✅

## K. Ask & narrative
96. Ask "workloads with critical findings" → matched nodes highlighted, count shown. ✅
97. AI narrative generates a multi-paragraph summary (or deterministic fallback). ✅

## L. Saved views
98. Save current view (name, lens, scope, overlays, camera) → appears in list. ✅
99. Apply a saved view restores lens/scope/overlays/camera; delete removes it. ✅

## M. Resilience / creative edge cases
100. Rapid connection switching + search + expand interleaved → no stale nodes, no console
     errors, counts always match the selected connection. ✅
