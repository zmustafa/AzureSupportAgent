# Architectures — Enterprise Feature Plan

> Reverse-engineer and design Azure application architectures, per workload, manually
> (drag‑drop + connect) or with AI (from a live Azure Resource Graph property dump).

## 1. Vision & scope

A new top‑level **Architectures** section (sidebar, above Settings) where a user can:

1. **Author manually** — drag Azure resource types onto an interactive canvas and connect
   them (dependencies, data flows, network paths), grouped into containers (subscription,
   resource group, VNet, logical tier).
2. **Reverse‑engineer with AI** — pick a Workload; the backend pulls every member
   resource **with its full Azure Resource Graph `properties`** (the real configuration),
   sends that grounding to the LLM, which infers the application architecture (nodes,
   edges, groups, tiers, rationale). The result renders as an editable diagram.
3. **Edit, version, export** — refine the AI output by hand, re‑run/enhance with AI,
   export as PNG/SVG/JSON/Mermaid, and keep it attached to the workload.

This makes the product not just *assess* and *automate* an estate, but **understand and
visualize** how an application is actually built — directly from cloud truth.

## 2. Why this is hard (and how we ground it)

The hard problem is **relationship inference**: Resource Graph returns a flat list of
resources; the *edges* (who talks to whom) live inside each resource's `properties`. The
plan extracts edges from real config, not guesses:

| Relationship | Inferred from (in `properties`) |
| --- | --- |
| NIC → Subnet → VNet | `networkInterfaces[].ipConfigurations[].subnet.id` |
| VM → NIC / Disk | `virtualMachines.networkProfile.networkInterfaces[].id`, `storageProfile.osDisk.managedDisk.id` |
| Private Endpoint → target | `privateEndpoints.privateLinkServiceConnections[].privateLinkServiceId` |
| App Service → Plan | `sites.serverFarmId` |
| Function → Storage | app settings `AzureWebJobsStorage` / linked storage |
| AKS → ACR / Subnet / LA | `managedClusters.agentPoolProfiles[].vnetSubnetID`, `addonProfiles`, `networkProfile` |
| SQL DB → Server | parent ARM id segment |
| Anything → Key Vault | `keyVaultProperties`, references in connection strings/settings |
| Managed identity → resource | `identity.userAssignedIdentities`, role assignments |
| Diagnostic → Log Analytics / Storage | `diagnosticSettings` (best‑effort) |
| Front Door / App Gateway → backends | `backendPools`, `routingRules` |

The LLM gets the **full `properties` blob** per resource (size‑budgeted) plus a curated
"how to read Azure properties for relationships" rubric, so it produces faithful edges.

## 3. Architecture data model

A single JSON registry: `backend/.data/architectures.json` (mirrors workbooks/playbooks).

```jsonc
Architecture {
  id, name, description,
  workload_id,            // optional link to the source workload
  connection_id,          // Azure tenant used for reverse-engineering
  tenant_id,
  source: "manual" | "ai",
  nodes: [ Node ],
  edges: [ Edge ],
  groups: [ Group ],
  ai: { model, generated_at, rationale, confidence, resource_count },
  created_by, created_at, updated_at
}

Node {
  id,                     // stable canvas id
  arm_id,                 // ARM resource id ("" for palette/annotation nodes)
  name, type,             // type = ARM type, e.g. microsoft.web/sites
  category,               // compute|web|data|networking|security|integration|ai|monitoring|identity|storage|other
  layer,                  // edge|presentation|application|data|integration|networking|security|shared
  resource_group, subscription_id, location, sku,
  meta: { string: string },   // 3–6 key facts shown on the card (tier, capacity, ...)
  group_id,               // optional parent group
  x, y                    // canvas position
}

Edge {
  id, source, target,     // node ids
  label,                  // e.g. "private endpoint", "reads/writes"
  kind,                   // depends_on|connects_to|data_flow|network|identity|monitors
  dashed                  // boolean (logical vs physical)
}

Group {
  id, name,
  kind,                   // subscription|resource_group|vnet|tier|custom
  color, x, y, w, h
}
```

`category` and `layer` drive node color/icon and the layered auto‑layout.

## 4. Backend design

New package `backend/app/architectures/`:

- **`catalog.py`** — `CATEGORY_META` (id→label/color), `categorize(arm_type)` → category,
  `layer_for(category)` → tier, and `PALETTE` (curated common Azure types for the manual
  drag‑drop palette, grouped by category). Pure, deterministic, no Azure calls.
- **`registry.py`** — JSON CRUD (`list/get/upsert/delete_architecture`) exactly like
  `workbooks/registry.py`, with `DEFAULTS` + `_read/_write/_merge`.
- **`reverse.py`** — `resolve_scope(workload, connection)` (ported from the assessments
  runner) → KQL predicate; `dump_resources(workload, connection)` runs one Resource Graph
  query projecting `id,name,type,location,resourceGroup,subscriptionId,kind,sku,identity,
  zones,tags,properties` over the scope (batched via an SP session), returns the rows;
  `compact(rows, budget)` trims oversized `properties` to keep within a token budget while
  preserving relationship‑relevant keys.
- **`designer.py`** — mirrors `workbooks/designer.py`:
  `generate_architecture(workload_name, resources)` → one LLM call (no tools) returning the
  full architecture (nodes/edges/groups/rationale); `enhance_architecture(arch, resources,
  goal)` → refine an existing diagram. Uses `build_provider()` + `provider.stream` +
  `safe_json_parse`. Normalizes/validates output (drops edges to unknown nodes, clamps
  enums, assigns `category`/`layer`, runs auto‑layout if coords missing).
- **`layout.py`** — lightweight layered auto‑layout: assign each node a layer (by tier,
  else topological depth), spread nodes horizontally per layer, stack groups; returns
  `(x,y)` per node and group rectangles. Used for AI output and a manual "Tidy" button.

New API `backend/app/api/architectures.py` (prefix `/architectures`, `Depends(get_principal)`):

| Method & path | Purpose |
| --- | --- |
| `GET /catalog` | category meta + palette (for the manual builder) |
| `GET ""` | list architectures (tenant‑scoped) |
| `GET /{id}` | one architecture |
| `PUT ""` | upsert (manual saves) |
| `DELETE /{id}` | delete |
| `POST /from-workload` (SSE) | reverse‑engineer: stream status → dump → AI → `done{architecture}` |
| `POST /{id}/enhance` | AI refine an existing diagram |
| `POST /{id}/layout` | server‑side re‑layout (optional; mostly client‑side) |
| `GET /workload/{wid}/inventory` | raw resource inventory (debug / "what will be sent to AI") |

Register `architectures.router` in `app/main.py`. Add an **"Architecture Builder"** group to
`ai_prompts.py` with `architecture_generate` and `architecture_enhance` (guidance + locked
JSON contract), so admins can tune the prompts on the System Prompts screen.

**Reverse‑engineer flow (`POST /from-workload`, SSE):**
1. `status` resolving scope → 2. `status` querying Resource Graph (N resources) →
3. `status` reading configuration / compacting → 4. `status` asking the AI architect →
5. `done` with the persisted architecture (saved with `source="ai"`).
Streamed so a large estate shows live progress (same pattern as assessments).

## 5. Frontend design

Add **`@xyflow/react`** (React Flow) — the industry‑standard node‑editor (pan/zoom, drag,
connect handles, minimap, selection, export). The Architectures panel is lazy‑loaded, so it
never bloats the main bundle.

- **`ArchitecturesView.tsx`** (`AssessmentsPanel`‑style):
  - **List view** — cards per architecture (name, node/edge counts, source badge, workload
    link, updated time). Buttons: **+ Blank**, **✨ From a workload (AI)**, open/delete.
  - **Editor view** (`/architectures/:id`) — the canvas.
- **`ArchitectureCanvas.tsx`** — React Flow canvas:
  - **Custom Azure node** — a card with the resource's `AzureIcon`, friendly type, name,
    SKU/tier chips and key `meta`; colored by `category`; connect handles on all sides.
  - **Group nodes** — translucent containers (subscription/RG/VNet/tier) behind resources.
  - **Edges** — labeled, styled by `kind` (solid network vs dashed logical), arrowheads.
  - **Left palette** — searchable list of Azure resource types (from `/catalog`), drag onto
    canvas to add a node. Grouped by category with icons.
  - **Inspector** (right) — edit selected node (name, type, tier, meta) or edge (label,
    kind, dashed); delete.
  - **Toolbar** — Save, **Tidy** (auto‑layout), **✨ AI generate/enhance**, **Export**
    (PNG / SVG / JSON / Mermaid), fit‑view, minimap toggle.
  - **AI** — "Generate from workload" opens a workload picker → calls the SSE endpoint with
    a live progress overlay → loads nodes/edges/groups. "Enhance" sends a goal + current
    diagram for refinement.
- **`api.ts`** — `Architecture`/`ArchNode`/`ArchEdge`/`ArchGroup` types; methods
  `architectures`, `architecture`, `upsertArchitecture`, `deleteArchitecture`,
  `architectureCatalog`, `enhanceArchitecture`, `workloadInventory`, and a
  `streamArchitectureFromWorkload(...)` SSE helper (mirrors `streamAssessment`).
- **`ChatView.tsx`** — add `inArchitectures` + a sidebar link (rail + expanded) **between
  Assessments and Settings**, with an `ArchitectureIcon`, and mount the lazy
  `ArchitecturesPanel` in the panel switch.
- **`App.tsx`** — routes `/architectures` and `/architectures/:id` → `<ChatView/>`.

## 6. Build phases

1. **Plan doc** (this file).
2. **Backend catalog + registry** — types, palette, JSON CRUD.
3. **Backend reverse** — scope resolution + Resource Graph property dump + compaction.
4. **Backend designer + prompts** — AI generate/enhance + ai_prompts entries + layout.
5. **Backend API + router** — endpoints (incl. SSE) + main.py wiring.
6. **Frontend dep + api.ts** — install React Flow, add types/methods.
7. **Frontend panel** — list + create flows.
8. **Frontend canvas** — interactive editor (nodes/edges/palette/inspector/AI/layout/export).
9. **Frontend nav + routes** — sidebar link above Settings + route + panel mount.
10. **Verify** — `ruff` + `npm run build` clean; live e2e (manual build a diagram; AI
    reverse‑engineer a real workload end‑to‑end); keep results.

## 7. Security & guardrails

- All Resource Graph queries are **read‑only** KQL via the existing `run_kql_capture` path
  (no writes, no arbitrary `az`).
- Tenant‑scoped registry; `Depends(get_principal)` like workloads.
- `properties` size‑budgeted before sending to the LLM (truncate per‑resource, cap total).
- AI output is **validated/normalized** server‑side (enum clamps, drop dangling edges) so a
  bad LLM response can't corrupt the canvas.
- The AI never executes anything — it only returns a diagram description.
