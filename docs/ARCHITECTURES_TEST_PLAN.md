# Architectures - Exhaustive Test Plan

> Date: 2026-06-10
> Scope: Architectures area - diagrams (CRUD, nodes/components/edges/lines/groups, properties,
> move/layout), workload association/disassociation, collections, lifecycle state, clone,
> revisions/restore, activity log, and Architecture Memory (sections, versioning, AI).

## 1. Surface Under Test

Backend: `app/api/architectures.py`, `app/architectures/{registry,memory,revisions,memory_revisions,catalog,collections,activity,jobs,layout,designer,reverse}.py`.
Frontend: `ArchitecturesView.tsx`, `ArchitectureCanvas.tsx`, `ArchitectureMemoryView.tsx`, `AIDesigner.tsx`, `api.ts` clients.

Data shapes:
- Node: `{id, arm_id, name, type, category, layer, resource_group, subscription_id, location, sku, meta, group_id, x, y}`
- Edge: `{id, source, target, label, kind, dashed}` (kind in depends_on|connects_to|data_flow|network|identity|monitors)
- Group: `{id, name, kind, color, x, y, w, h}` (kind in subscription|resource_group|vnet|tier|custom)
- Lifecycle state: draft -> in_review -> ready; archived (restorable).

## 2. Test Matrix

### 2.1 Create / Read

1. Create blank architecture (name only) -> draft, empty nodes/edges/groups, created_at set.
2. Create with full nodes/edges/groups payload.
3. Get by id returns merged defaults (all keys present).
4. List is tenant-scoped (blank tenant visible; foreign tenant hidden).
5. Catalog returns categories, layers, palette.
6. Route ordering: `/catalog`, `/jobs`, `/collections`, `/memory/catalog`, `/memories` are not captured as `/{id}`.

### 2.2 Modify diagram

7. Rename -> activity RENAMED, revision snapshot.
8. Add nodes (append) and persist.
9. Add edges between existing nodes.
10. Add groups; assign node.group_id.
11. Move node (change x/y) persists.
12. Change node properties (name, sku, meta up to many keys, location, rg).
13. Change edge properties (label, kind, dashed).
14. Remove a node; remove an edge; remove a group.
15. Large diagram (100+ nodes, 150+ edges) saves and reloads intact.
16. Node id uniqueness preserved as sent (server does not rewrite ids).
17. Edge with same source/target as a node id remains valid.
18. Partial save (nodes only) does not clobber workload link (workload_id None skip-merge).
19. Empty diagram save (nodes=[]) clears nodes.

### 2.3 Workload association

20. Link workload via `/workload` -> workload_id+name set, diagram untouched, activity WORKLOAD_CHANGED.
21. Re-link to a different workload.
22. Disassociate (workload_id="") -> unlinked, activity logged.
23. Diagram PUT with workload_id omitted keeps link intact.
24. Link to non-existent workload id still stores id (no crash) [boundary].

### 2.4 Lifecycle state

25. set_state draft->in_review->ready->archived; each logs STATE_CHANGED + snapshot.
26. Invalid state rejected (400/ValueError).
27. State change never modifies diagram.

### 2.5 Collections / category

28. Create collection; assign architecture; list reflects.
29. Reorder collections.
30. Delete collection -> members reassigned to Uncategorized (count returned).
31. Assign empty category = Uncategorized.

### 2.6 Clone

32. Clone -> new id, name "(copy)", state draft, own revision history, source diagram unchanged.
33. Clone preserves nodes/edges/groups/workload link.

### 2.7 Revisions (diagram)

34. Each meaningful change creates a revision; identical save deduped (no new revision).
35. List newest-first with node/edge counts.
36. Preview a revision returns full content.
37. Restore an older revision swaps content; pre-restore version snapshotted (nothing lost).
38. Cap at 50 revisions (oldest pruned).
39. Delete architecture deletes its revisions + activity.

### 2.8 Activity log

40. Activity newest-first; events for create/rename/edit/state/category/workload/clone/restore.
41. Cap at 200 events.

### 2.9 Memory CRUD

42. memory/catalog returns 19 sections + 7 default keys.
43. Create memory (default sections) for an architecture.
44. Edit sections content; add custom section; reorder.
45. Markdown render reflects title + sections.
46. sections=None preserves existing; sections=[] overwrites to empty (documented).
47. enabled_for_investigations toggle persists.
48. Delete memory removes it + its revisions.
49. memories index joins arch + workload names, filled-count.

### 2.10 Memory revisions

50. Each memory save snapshots; dedup on identical content.
51. List + preview (with markdown) + restore.
52. Restore snapshots pre-restore version.
53. Cap at 50.

### 2.11 Negative / boundary

54. Get/modify non-existent architecture -> 404.
55. Memory for non-existent architecture -> 404 on GET.
56. Restore non-existent revision -> 404.
57. Oversized name/description rejected by Pydantic (max_length).
58. Malformed node/edge (missing required id/source/target) rejected.

### 2.12 Frontend (browser)

59. Architectures list renders; open editor.
60. Canvas renders nodes/edges; add node from palette.
61. Draw an edge between two nodes.
62. Move a node; Save persists position.
63. Edit node properties panel.
64. Memory tab: add section, type content, live markdown preview.
65. Memory generate (AI) status->done (if provider available) or graceful error.
66. History tab: preview + restore a revision.
67. Workload link/unlink from editor.

## 3. Execution Strategy

1. Backend API pass via authenticated fetches against the running server (real Pydantic+routes+registry).
2. Direct-registry pass for dedup/cap/section-merge edge cases.
3. Frontend browser pass for canvas/editor/memory interactions.
4. Fix any defect, re-run the affected pass, then full regression.

## 4. Exit Criteria

- All API + registry assertions pass.
- No cross-tenant leakage; route ordering intact.
- Revision dedup + cap correct; restore lossless.
- Memory section semantics correct; markdown render correct.
- Frontend canvas/editor/memory smoke passes with no console errors.
