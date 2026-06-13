# Inventory - Exhaustive Test Plan

> Date: 2026-06-10
> Scope: Inventory section - grid, facet filters, NL/AI search, cost, optimization,
> location map, changes/snapshots/drift, resource detail overlays. Click every control;
> >= 20 permutations per scenario.

## 1. Surface Under Test

Backend: `app/api/inventory.py` + `app/inventory/{service,cache,cost,ai,snapshots,optimization}.py`.
Frontend: `InventoryView.tsx`, `InventoryLocationMap.tsx`, `api.ts` clients.

Tabs (URL `/inventory/:tab`): grid, overview, location, cost, optimization, changes.

Filter state: typeSel, locSel, subSel, rgSel, wlSel, flagSel, tagKey/tagValue, text,
skuContains, kqlIds. Scope: workloads | tenant.

## 2. Endpoint Inventory (auth = admin)

| Method | Path | Params | Notes |
| --- | --- | --- | --- |
| GET | /inventory | connection_id, force | resources+facets+summary; permanent cache; force=1 re-collects |
| GET | /inventory/cost | connection_id, force, cached_only | cached_only=1 peeks, never calls Azure |
| GET | /inventory/cost-rollup | connection_id, force, cached_only | by_workload/type/location/subscription/rg + top_resources |
| GET | /inventory/optimization | connection_id | cache-only; available=false if inventory not loaded |
| POST | /inventory/nl-search | query, types/locations/workloads/subscriptions | mode filter|kql; matched_ids |
| POST | /inventory/explain | resource | never 500 |
| GET | /inventory/insights | connection_id | ai|local; never fails |
| GET | /inventory/snapshots | connection_id | list |
| POST | /inventory/snapshots | connection_id | take snapshot + drift_since_previous |
| GET | /inventory/drift | connection_id, baseline_id | diff vs baseline/latest |
| DELETE | /inventory/snapshots/{id} | - | ok |
| POST | /inventory/governance | resource_id, connection_id | effective policy |
| POST | /inventory/findings | resource_id | assessment findings |

## 3. Permutation Matrix (>= 20 per scenario)

### 3.1 Core inventory load (>= 20)
- GET /inventory (cached) and force=1 (fresh).
- With/without connection_id (default vs explicit).
- Validate shape: resources[], facets{types,locations,subscriptions,resource_groups,workloads}, summary, fetched_at, age_seconds, cached flag.
- Staleness: age_seconds vs 6h threshold reasoning.
- Re-request returns cached=true and stable counts.
- 20 permutations = {force 0/1} x {conn default/explicit} x repeated reads + facet-shape assertions + summary counts + truncated_subscriptions presence.

### 3.2 NL / AI search (>= 20)
Run 20+ distinct queries covering:
- type intents: "storage accounts", "virtual machines", "sql databases", "logic apps", "key vaults", "app services", "function apps", "public ips", "network interfaces", "disks".
- location intents: "resources in eastus", "everything in west europe".
- hygiene intents: "untagged resources", "orphaned disks", "idle public ips".
- sku intents: "premium storage", "D-series vms", "burstable vms".
- tag intents: "resources tagged Environment=prod".
- combined: "untagged storage in eastus".
- nonsense: "asdfghjkl" (graceful empty/explanation, no 500).
Assert each returns mode in {filter,kql}, no 500, matched_ids array present or filter object present, explanation string.

### 3.3 Cost (>= 20)
- cost cached_only=1 (peek) returns available true/false without Azure.
- cost-rollup cached_only=1 shape: by_workload/type/location/subscription/resource_group/top_resources arrays.
- If available: total numeric, currency string, period string, by_subscription map.
- Permutations across {cached_only 1} x {conn default/explicit} x {cost, cost-rollup} x repeated + each rollup dimension array asserted + unattributed/unassigned fields.

### 3.4 Optimization (>= 20)
- GET /inventory/optimization: available bool; categories[] each {flag,label,count,monthly_cost}; items[]; total_count; currency.
- Each category flag in {unattached_disk, idle_public_ip, orphaned_nic}.
- items reference real resource ids present in inventory.
- Permutations: repeated reads stable; with/without conn; cross-check counts vs hygiene flags in inventory facets.

### 3.5 Snapshots + drift (>= 20)
- list snapshots (initial).
- take snapshot -> returns snapshot{id,total_resources,...} + drift_since_previous.
- take a 2nd snapshot -> drift computed (added/removed/changed counts, all >=0).
- drift vs explicit baseline_id.
- drift with no baseline (latest).
- delete snapshot -> ok; list shrinks.
- Permutations: multiple take/delete cycles, drift count invariants (added/removed/changed integers >=0, capped <=500), snapshot fields present, created_by set.

### 3.6 Resource detail overlays (>= 20)
- For 10+ distinct resources: POST /inventory/explain -> explanation non-empty, no 500.
- POST /inventory/governance for several -> effective array (>=0), count integer.
- POST /inventory/findings for several -> findings array, count integer.
- insights endpoint -> headline + insights[] each severity in {info,warning,critical}.

### 3.7 Filter predicate permutations (frontend logic, >= 20)
Validated in browser by setting state and reading filtered count:
- single type; single location; single subscription; single RG; single workload.
- type+location; type+subscription; location+RG; workload+type.
- tagKey only; tagKey+tagValue.
- flag untagged; flag unattached_disk; __cleanup__ OR.
- text substring (name); text substring (id fragment).
- sku contains.
- scope workloads vs tenant + __unassigned__.
- clear filters resets to full count.
- cascading facets: selecting a type keeps type facet list full (except dimension) but narrows others.

## 4. Frontend Click-Through (every control)

Header: connection dropdown, Refresh, staleness badge, summary.
Tabs: grid, overview, location, cost, optimization, changes (navigate each; no console errors).
Grid toolbar: NL search submit, text filter, Clear filters, Group by (all options), column picker (toggle each), density toggle, Load cost, Export.
Facet sidebar: scope toggle, type/location/subscription/RG/workload facet clicks, flags, tag key/value, clear all.
Location: region chips, multi-region select, zoom +/-/reset, pan, dimension switch (Region/Group/Type/Sub), right-panel list selection, workloads-here pills.
Cost: Load cost, Refresh cost, summary tables.
Optimization: category groups, item rows.
Changes: take snapshot, delete snapshot, drift display.
Row: open detail drawer, Explain, Governance tab, Findings tab, Copy ID, checkbox bulk select, Export selected.

## 5. Execution Strategy

1. Backend permutation pass via authenticated fetches (>=20 per scenario), capturing 500s/shape errors.
2. Frontend browser click-through of every tab + control, watching console/page errors.
3. Filter-predicate permutations exercised in the live grid.
4. Fix any defect, re-run affected pass, then regression.

## 6. Exit Criteria

- No 500s across all endpoint permutations.
- Every NL query returns a valid mode + explanation, no crash.
- Cost/optimization/snapshot/drift shapes correct; drift invariants hold.
- Every tab + control clickable with zero console/page errors.
- Filters narrow correctly and Clear resets to full count.
