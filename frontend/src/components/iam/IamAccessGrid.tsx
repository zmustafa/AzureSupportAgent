/** The virtualized access grid — one component, many lenses (Effective / Privileged).
 *
 *  Rows come from the server-side composed cache and are paged as you scroll (RP6), so the
 *  full result set is reachable without ever putting more than a screenful in the DOM.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api } from "../../api";
import { useDebounced } from "../../utils/perf";
import { AzureIcon } from "../AzureIcon";
import { useExportDownload } from "../ExportProgress";
import { FilterRail } from "./IamFilterRail";
import { IAM_PAGE, PATH_LABEL, PrivBadge, EffectChip, scopeCell, type AccessFilter, useIamConnectionId } from "./IamShared";
import { InvestigateLink } from "../entra/InvestigateLink";
import { WhyPanel } from "./IamWhyPanel";

type WhyTarget = { principalId: string; principalName: string; scope: string };

/** `initialPrivOnly` is how the legacy `/iam/privileged` route survives the tab merge: the
 *  Privileged tab was never anything but this grid with one checkbox ticked, so the route now
 *  seeds the checkbox instead of selecting a separate tab. An explicit `?privileged=` in the
 *  URL always wins over it — a shared link must mean what it says regardless of which route
 *  rendered it. */
export function AccessGrid({ tab, initialPrivOnly = false }: { tab: string; initialPrivOnly?: boolean }) {
  const [search, setSearch] = useState("");
  const dSearch = useDebounced(search, 250); // RP2 — don't re-query the server on every keystroke
  const [surface, setSurface] = useState("");
  const [ptype, setPtype] = useState("");
  const [privOnly, setPrivOnly] = useState(() => {
    const p = new URLSearchParams(window.location.search).get("privileged");
    return p === null ? initialPrivOnly : p === "1" || p === "true";
  });
  const [filter, setFilter] = useState<AccessFilter | null>(null);
  const [why, setWhy] = useState<WhyTarget | null>(null);
  const connectionId = useIamConnectionId();

  // Deep-link handoff: opened from the Estate Graph ("IAM →") with `?workload_id=` (and an
  // optional `?workload_name=` for the chip label) → scope the access grid to that workload.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const wid = params.get("workload_id");
    if (wid) setFilter({ type: "workload", label: params.get("workload_name") || wid, workload_id: wid });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Reflect the privileged lens in the URL so it is shareable — `replaceState`, not a router
  // navigation: this must not push a history entry per checkbox click, and it must not remount
  // the grid (which would discard every loaded page and scroll position).
  useEffect(() => {
    const url = new URL(window.location.href);
    if (privOnly) url.searchParams.set("privileged", "1");
    else url.searchParams.delete("privileged");
    if (url.href !== window.location.href) window.history.replaceState(window.history.state, "", url);
  }, [privOnly]);
  const q = useInfiniteQuery({
    queryKey: ["iam", "access", tab, dSearch, surface, ptype, privOnly, filter?.scope_id ?? "", filter?.workload_id ?? "", connectionId ?? ""],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      api.iamAccess({
        tab,
        search: dSearch,
        surface,
        principal_type: ptype,
        privileged_only: privOnly,
        offset: pageParam as number,
        limit: IAM_PAGE,
        scope_id: filter?.scope_id,
        subscription_ids: filter?.subscription_ids,
        workload_id: filter?.workload_id,
        connection_id: connectionId,
      }),
    // RP6 — page through the full result set (was a hard 500-row cap). Each page is `IAM_PAGE`
    // rows; the virtualizer requests the next page as it nears the end (see effect below).
    getNextPageParam: (last) => {
      const loaded = last.offset + last.rows.length;
      return loaded < last.total ? loaded : undefined;
    },
    // Keep the current grid visible while a new tab/search/filter loads, instead of
    // flashing an empty table on every keystroke/tab switch (the rows come from a
    // server-side computed cache, so refetches are common as filters change).
    // NOT across a connection change: the tenant label updates instantly, so holding the old
    // rows shows one tenant's access under another tenant's name for the length of the refetch.
    placeholderData: (prev) => prev,
    staleTime: 60 * 1000,
  });
  const rows = useMemo(() => (q.data?.pages ?? []).flatMap((p) => p.rows), [q.data]);
  const total = q.data?.pages?.[0]?.total ?? 0;
  // Virtualize the grid body: only the visible window of rows is in the DOM, so a 500-row
  // result stays at ~20 live <tr> instead of 500 (× 7 cells), keeping scroll/INP smooth.
  const scrollRef = useRef<HTMLDivElement>(null);
  const rowVirt = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 44,   // ~1.5-line row; measured precisely after mount
    overscan: 12,
  });
  const vItems = rowVirt.getVirtualItems();
  const padTop = vItems.length ? vItems[0].start : 0;
  const padBottom = vItems.length ? rowVirt.getTotalSize() - vItems[vItems.length - 1].end : 0;

  // RP6 — fetch the next page when the virtualizer scrolls within ~24 rows of the loaded end.
  const lastIndex = vItems.length ? vItems[vItems.length - 1].index : 0;
  useEffect(() => {
    if (lastIndex >= rows.length - 24 && q.hasNextPage && !q.isFetchingNextPage) {
      void q.fetchNextPage();
    }
  }, [lastIndex, rows.length, q.hasNextPage, q.isFetchingNextPage, q]);
  // EVERY filter on screen goes into the export, not just the scope rail. The row export used
  // to carry only scope/workload, so a CSV taken while searching or with the privileged lens on
  // quietly held rows the grid was not showing — and that file is what gets attached to the
  // access review. The workbook is a different artifact (every view, scope-wide) and
  // deliberately keeps taking only the scope narrowing.
  const exportFilter = {
    scope_id: filter?.scope_id,
    subscription_ids: filter?.subscription_ids,
    workload_id: filter?.workload_id,
    connection_id: connectionId,
    surface,
    principal_type: ptype,
    privileged_only: privOnly,
    search: dSearch,
  };
  const workbookFilter = {
    scope_id: filter?.scope_id,
    subscription_ids: filter?.subscription_ids,
    workload_id: filter?.workload_id,
    connection_id: connectionId,
  };
  // One instance serves all three buttons; the label passed to `start` keeps their duration
  // estimates apart, because a filtered CSV and the scope-wide workbook are not the same wait.
  const download = useExportDownload("IAM export");

  return (
    <div className="flex h-full min-h-0">
      {download.dialog}
      <FilterRail
        filter={filter}
        onChange={setFilter}
        collapsible
        storageKey="azsup.iam.accessGrid.filterRail"
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center gap-2 border-b bg-white px-4 py-2">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search principal / role / scope…"
            className="w-64 rounded border px-2 py-1 text-sm"
          />
          {/* These option VALUES are the backend's SURFACE_* constants — not display strings. */}
          <select value={surface} onChange={(e) => setSurface(e.target.value)} className="rounded border px-2 py-1 text-sm">
            <option value="">All surfaces</option>
            <option value="Azure RBAC">Azure RBAC</option>
            <option value="Entra ID RBAC">Entra ID RBAC</option>
            <option value="Key Vault Access Policy">Key Vault</option>
            <option value="Classic Admin">Classic Admin</option>
            <option value="Deny Assignment">Deny Assignment</option>
          </select>
          <select value={ptype} onChange={(e) => setPtype(e.target.value)} className="rounded border px-2 py-1 text-sm">
            <option value="">All principal types</option>
            <option value="User">User</option>
            <option value="Group">Group</option>
            <option value="ServicePrincipal">Service Principal</option>
          </select>
          <label className="flex items-center gap-1 text-sm text-gray-700">
            <input type="checkbox" checked={privOnly} onChange={(e) => setPrivOnly(e.target.checked)} /> Privileged only
          </label>
          <span className="ml-auto text-xs text-gray-500">{rows.length < total ? `${rows.length.toLocaleString()} / ${total.toLocaleString()}` : total.toLocaleString()} grant(s)</span>
          {q.isFetchingNextPage && <span className="text-[11px] text-gray-400">loading more…</span>}
          <button type="button" onClick={() => download.start(api.iamExportUrl("csv", tab, exportFilter), `iam-access-${tab}.csv`, "CSV export")} disabled={download.phase !== "idle"} title="Export the current grid (honors scope, search, surface, principal-type & privileged filters)" className="rounded border px-2 py-1 text-xs text-brand hover:bg-gray-50 disabled:opacity-50">⬇ CSV</button>
          <button type="button" onClick={() => download.start(api.iamExportUrl("json", tab, exportFilter), `iam-access-${tab}.json`, "JSON export")} disabled={download.phase !== "idle"} title="Export the current grid (honors the active filters)" className="rounded border px-2 py-1 text-xs text-brand hover:bg-gray-50 disabled:opacity-50">⬇ JSON</button>
          <button type="button" onClick={() => download.start(api.iamWorkbookUrl(workbookFilter), "iam-access-review.xlsx", "IAM workbook")} disabled={download.phase !== "idle"} title="Multi-tab workbook of every IAM view (honors the active scope/workload, not the grid's search or lens)" className="rounded border border-green-300 bg-green-50 px-2 py-1 text-xs font-medium text-green-700 hover:bg-green-100 disabled:opacity-50">⬇ Excel (all tabs)</button>
        </div>
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto">
          {q.isLoading ? (
            <div className="p-6 text-sm text-gray-500">Loading…</div>
          ) : rows.length === 0 ? (
            <div className="p-6 text-sm text-gray-500">
              {filter ? `No access matches "${filter.label}". Try a broader scope or clear the filter.` : "No matching access. Run an access scan from the Overview tab."}
            </div>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead className="sticky top-0 z-10 bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-500">
                <tr>
                  <th className="px-3 py-2">Principal</th>
                  <th className="px-3 py-2">Type</th>
                  <th className="px-3 py-2">Role</th>
                  <th className="px-3 py-2">Scope</th>
                  <th className="px-3 py-2">Path</th>
                  <th className="px-3 py-2">Surface</th>
                  <th className="px-3 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {padTop > 0 && <tr style={{ height: padTop }} aria-hidden />}
                {vItems.map((vi) => {
                  const r = rows[vi.index];
                  const i = vi.index;
                  const who = (r.effectivePrincipalName || r.principalDisplayName || r.effectivePrincipalId || "—") as string;
                  const upn = (r.effectivePrincipalUserPrincipalName || r.principalUserPrincipalName || "") as string;
                  const scope = scopeCell(r);
                  const path = (r.accessPath as string) || "";
                  return (
                    <tr key={i} ref={rowVirt.measureElement} data-index={i} className={`border-b last:border-0 hover:bg-gray-50 ${r.effect === "Deny" ? "bg-red-50/60" : ""}`}>
                      <td className="px-3 py-1.5">
                        <div className="flex items-center gap-1">
                          <span className="min-w-0 truncate font-medium text-gray-800">{who}</span>
                          <InvestigateLink
                            principalId={(r.effectivePrincipalId || r.principalId || "") as string}
                            label={who}
                          />
                        </div>
                        {upn && <div className="text-[11px] text-gray-400">{upn}</div>}
                      </td>
                      <td className="px-3 py-1.5 text-gray-600">{(r.effectivePrincipalType || r.principalType || "") as string}</td>
                      <td className="px-3 py-1.5">
                        <span className="text-gray-800">{r.roleName as string}</span> <EffectChip row={r} /> <PrivBadge row={r} />
                      </td>
                      <td className="max-w-[280px] px-3 py-1.5 text-gray-600" title={r.scope as string}>
                        <div className="flex items-center gap-1.5">
                          {scope.icon && <AzureIcon kind={scope.icon} className="h-3.5 w-3.5 shrink-0" />}
                          <span className="min-w-0 truncate">{scope.label}</span>
                        </div>
                      </td>
                      <td className="px-3 py-1.5 text-gray-600">
                        {PATH_LABEL[path] || path}
                        {path === "GroupTransitive" && r.sourceGroupName ? ` (${r.sourceGroupName})` : ""}
                      </td>
                      <td className="px-3 py-1.5 text-[11px] text-gray-500">{r.surface as string}</td>
                      <td className="px-3 py-1.5">
                        <button
                          type="button"
                          onClick={() =>
                            setWhy({
                              principalId: (r.effectivePrincipalId || r.principalId || "") as string,
                              principalName: who,
                              scope: (r.scope || "") as string,
                            })
                          }
                          className="rounded border px-1.5 py-0.5 text-[11px] text-brand hover:bg-gray-50"
                          title="Can this principal perform a given action here, and why?"
                        >
                          Why?
                        </button>
                      </td>
                    </tr>
                  );
                })}
                {padBottom > 0 && <tr style={{ height: padBottom }} aria-hidden />}
              </tbody>
            </table>
          )}
        </div>
      </div>
      {why && (
        <WhyPanel
          principalId={why.principalId}
          principalName={why.principalName}
          scope={why.scope}
          onClose={() => setWhy(null)}
        />
      )}
    </div>
  );
}
