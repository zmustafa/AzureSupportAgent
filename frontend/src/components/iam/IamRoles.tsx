/** Roles & Principals tab — the directory reference layer: role definitions on the left,
 *  the resolved principal directory on the right, both virtualized and filtered by one search.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import { Skeleton, useDebounced, VirtualList } from "../../utils/perf";
import { InvestigateLink, investigatableId } from "../entra/InvestigateLink";
import { useIamConnectionId } from "./IamShared";

export function RolesTab() {
  const connectionId = useIamConnectionId();
  const q = useQuery({ queryKey: ["iam", "roles", connectionId ?? ""], queryFn: () => api.iamRoles(connectionId), staleTime: 5 * 60 * 1000 });
  const [search, setSearch] = useState("");
  const dSearch = useDebounced(search, 200);
  const roleDefs = (q.data?.role_defs ?? []) as Record<string, unknown>[];
  const principals = (q.data?.principals ?? []) as Record<string, unknown>[];
  // RP3 — precompute a lowercased search blob per row ONCE (was JSON.stringify per row per render),
  // then filter against the debounced term.
  const roleBlobs = useMemo(() => roleDefs.map((r) => JSON.stringify(r).toLowerCase()), [roleDefs]);
  const princBlobs = useMemo(() => principals.map((p) => JSON.stringify(p).toLowerCase()), [principals]);
  const t = dSearch.toLowerCase();
  const fr = useMemo(() => roleDefs.filter((_, i) => !t || roleBlobs[i].includes(t)), [roleDefs, roleBlobs, t]);
  const fp = useMemo(() => principals.filter((_, i) => !t || princBlobs[i].includes(t)), [principals, princBlobs, t]);
  if (q.isLoading) return <div className="p-4"><Skeleton rows={10} /></div>;
  return (
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search roles / principals…" className="mb-3 w-72 rounded border px-2 py-1 text-sm" />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="rounded-lg border bg-white">
          <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Role definitions ({fr.length}{fr.length !== roleDefs.length ? ` of ${roleDefs.length}` : ""})</div>
          {/* RP3 — virtualized (was a plain map capped only by max-height). */}
          <VirtualList
            items={fr}
            estimateSize={34}
            max="60vh"
            render={(r: Record<string, unknown>) => (
              <div className="grid grid-cols-[1.6fr_1fr_auto] items-center gap-2 border-b px-3 py-1.5 text-sm last:border-0">
                <span className="truncate font-medium text-gray-800">{String(r.roleName ?? "")}</span>
                <span className="truncate text-gray-500">{String(r.roleCategory ?? "")}</span>
                <span>{r.roleIsPrivileged ? <span className="rounded bg-red-100 px-1.5 text-[10px] text-red-700">privileged</span> : null}</span>
              </div>
            )}
          />
        </div>
        <div className="rounded-lg border bg-white">
          <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Principal directory ({fp.length}{fp.length !== principals.length ? ` of ${principals.length}` : ""})</div>
          <VirtualList
            items={fp}
            estimateSize={34}
            max="60vh"
            render={(p: Record<string, unknown>) => {
              const name = String(p.displayName ?? "");
              // Inside the name cell, not a fourth column: the row is a fixed 3-column grid
              // and the glyph belongs against the identity anyway.
              const pid = investigatableId(String(p.principalType ?? ""), String(p.principalId ?? ""));
              return (
                <div className="grid grid-cols-[1.4fr_0.8fr_1.2fr] items-center gap-2 border-b px-3 py-1.5 text-sm last:border-0">
                  <span className="flex min-w-0 items-center gap-1">
                    <span className="truncate font-medium text-gray-800">{name}</span>
                    {pid && <InvestigateLink principalId={pid} label={name} />}
                  </span>
                  <span className="truncate text-gray-500">{String(p.principalType ?? "")}</span>
                  <span className="truncate text-[11px] text-gray-400">{String(p.userPrincipalName ?? p.appId ?? "")}</span>
                </div>
              );
            }}
          />
        </div>
      </div>
    </div>
  );
}
