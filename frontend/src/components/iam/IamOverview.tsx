/** Overview tab — KPIs, refresh controls with the live SSE progress log, per-scope freshness
 *  and the shared directory layer's state.
 */
import { api, type IamOverview } from "../../api";
import { KpiTile, RefreshConsole, ScopeTable, StaleBadge, StatusPill, useIamConnectionId, type IamRefreshCtl } from "./IamShared";

export function OverviewTab({
  data,
  refreshCtl,
  onPurgeDemo,
  purging,
}: {
  data: IamOverview;
  refreshCtl: IamRefreshCtl;
  onPurgeDemo: () => void;
  purging: boolean;
}) {
  const k = data.kpis;
  // Without this the link carries no connection_id and the backend falls back to the DEFAULT
  // connection: on a tenant with 5,514 grants the button downloaded a different tenant's 83.
  // A wrong-tenant export is worse than a failed one — it looks like a successful review.
  const connectionId = useIamConnectionId();
  return (
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <div className="mb-3 flex items-center gap-2">
        {/* "Refresh all scopes" moved to the page header, next to the freshness badge, so it is
            reachable from every tab rather than this one. These two stay because they operate on
            what is directly below them: the per-scope freshness table. */}
        <button
          onClick={refreshCtl.refreshChanged}
          disabled={refreshCtl.isBusy}
          title="Ask Resource Graph which subscriptions had authorization activity since their last collection and re-collect only those. Falls back to a full refresh if that question cannot be answered."
          className="rounded border px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          ⚡ Quick refresh
        </button>
        <button
          onClick={refreshCtl.refreshDirectory}
          disabled={refreshCtl.isBusy}
          className="rounded border px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          {refreshCtl.refreshing.has("directory") ? "Refreshing…" : "↻ Refresh directory"}
        </button>
        {/* Seeding lives on the empty state only. Once a tenant has real access data, an
            adjacent "load fake data" button in the main toolbar is a mis-click away from making
            a review of a live tenant unreadable. Removing demo data stays here, and stays
            conditional, because that is the only way out once it IS loaded. */}
        {data.demo && (
          <button onClick={onPurgeDemo} disabled={purging} className="rounded border border-red-200 px-3 py-1.5 text-sm text-red-600 hover:bg-red-50 disabled:opacity-50">
            {purging ? "Removing…" : "🗑️ Remove demo data"}
          </button>
        )}
        {data.demo && <span className="rounded bg-violet-100 px-2 py-0.5 text-xs text-violet-700">demo dataset</span>}
        <a
          href={api.iamWorkbookUrl({ connection_id: connectionId })}
          className="rounded border border-green-300 bg-green-50 px-3 py-1.5 text-sm font-medium text-green-700 hover:bg-green-100"
          title="Download a comprehensive multi-sheet Excel workbook of every IAM view"
        >
          ⬇ Export to Excel
        </a>
        <span className="ml-auto text-xs text-gray-500">
          {data.connection_configured ? "Azure connection configured" : "No Azure connection — use demo data"}
        </span>
      </div>

      {/* Thirteen tiles, and 13 is prime — so any fixed column count leaves an orphan row. At
          six columns that was a third row holding ONE tile: 5 empty cells and 204px (a fifth of
          the viewport) spent before the reader reaches the scope table.
          Seven columns fits them in two rows, but ONLY from xl up: measured at 1024 it squeezed
          the tile to 96px and truncated "Key Vault policies", "Classic admins" and "Deny
          assignments" to ellipses. A KPI you cannot read is worse than one extra row, so the
          column count steps down with the width rather than holding 7 everywhere. */}
      <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-7">
        <KpiTile label="Total grants" value={k.total_assignments} />
        <KpiTile label="Principals" value={k.unique_principals} tone="sky" />
        <KpiTile label="Privileged" value={k.privileged} tone="red" />
        <KpiTile label="Data-plane" value={k.data_plane} tone="amber" />
        <KpiTile label="Via groups" value={k.group_derived} tone="amber" />
        <KpiTile label="SP owners" value={k.owners} tone="amber" />
        <KpiTile label="Entra roles" value={k.entra_roles} />
        <KpiTile label="PIM eligible" value={k.eligible} />
        <KpiTile label="Key Vault policies" value={k.key_vault_policies ?? 0} tone="amber" />
        <KpiTile label="Classic admins" value={k.classic_admins ?? 0} tone={(k.classic_admins ?? 0) > 0 ? "red" : undefined} />
        <KpiTile label="Deny assignments" value={k.deny_assignments ?? 0} tone="sky" />
        <KpiTile label="Scopes" value={k.scopes} />
        <KpiTile label="Subscriptions" value={k.subscriptions} />
      </div>

      <RefreshConsole ctl={refreshCtl} />

      <div className="mb-4 rounded-lg border bg-white">
        <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Per-scope freshness</div>
        <ScopeTable scopes={data.scopes} refresh={refreshCtl.refreshScope} refreshing={refreshCtl.refreshing} />
      </div>

      <div className="rounded-lg border bg-white">
        <div className="flex items-center justify-between border-b px-3 py-2">
          <span className="text-sm font-semibold text-gray-800">Directory layer (Entra roles, groups, SP owners)</span>
          <div className="flex items-center gap-2">
            {data.directory.loaded ? <StatusPill status={data.directory.status} /> : <span className="text-xs text-gray-400">not loaded</span>}
            <StaleBadge age={data.directory.age_seconds} stale={(data.directory.age_seconds ?? Infinity) >= data.ttl_s} />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-2 p-3 text-sm sm:grid-cols-4">
          <div><span className="text-gray-500">Rows: </span>{data.directory.row_count}</div>
          <div><span className="text-gray-500">Role defs: </span>{data.directory.role_def_count}</div>
          <div><span className="text-gray-500">Principals: </span>{data.directory.principal_count}</div>
          <div><span className="text-gray-500">Groups: </span>{data.directory.group_count}</div>
        </div>
      </div>
    </div>
  );
}
