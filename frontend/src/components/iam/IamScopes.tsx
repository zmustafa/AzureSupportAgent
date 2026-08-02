/** Scopes tab — every cached scope with its collection status, grant count and freshness,
 *  plus a per-scope refresh so one subscription can be re-scanned without touching the rest.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import { ScopeTable, useIamConnectionId, type IamRefreshCtl } from "./IamShared";

export function ScopesTab({ refreshCtl }: { refreshCtl: IamRefreshCtl }) {
  const connectionId = useIamConnectionId();
  const q = useQuery({ queryKey: ["iam", "scopes", connectionId ?? ""], queryFn: () => api.iamScopes(connectionId), staleTime: 5 * 60 * 1000 });
  if (q.isLoading) return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  const scopes = q.data?.scopes ?? [];
  return (
    <div className="min-h-0 flex-1 overflow-auto p-4">
      <div className="rounded-lg border bg-white">
        <div className="border-b px-3 py-2 text-sm font-semibold text-gray-800">Scopes ({scopes.length})</div>
        <ScopeTable scopes={scopes} refresh={refreshCtl.refreshScope} refreshing={refreshCtl.refreshing} />
      </div>
    </div>
  );
}
