/** Insights tab — the precomputed pivots, scoped by the shared filter rail. */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import { useExportDownload } from "../ExportProgress";
import { FilterRail } from "./IamFilterRail";
import { type AccessFilter, useIamConnectionId } from "./IamShared";

function PivotCard({ title, items }: { title: string; items: { label: string; count: number }[] }) {
  const max = Math.max(1, ...items.map((i) => i.count));
  return (
    <div className="rounded-lg border bg-white p-3">
      <div className="mb-2 text-sm font-semibold text-gray-800">{title}</div>
      {items.length === 0 ? (
        <div className="text-xs text-gray-400">No data.</div>
      ) : (
        <div className="space-y-1">
          {items.slice(0, 8).map((it) => (
            <div key={it.label} className="flex items-center gap-2 text-xs">
              <div className="w-40 truncate text-gray-600" title={it.label}>{it.label}</div>
              <div className="h-3 flex-1 rounded bg-gray-100">
                <div className="h-3 rounded bg-brand/70" style={{ width: `${(it.count / max) * 100}%` }} />
              </div>
              <div className="w-8 text-right tabular-nums text-gray-500">{it.count}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function InsightsTab() {
  const [filter, setFilter] = useState<AccessFilter | null>(null);
  const connectionId = useIamConnectionId();
  const download = useExportDownload("IAM workbook");
  const q = useQuery({
    queryKey: ["iam", "pivots", filter?.scope_id ?? "", filter?.workload_id ?? "", connectionId ?? ""],
    queryFn: () =>
      api.iamPivots({
        scope_id: filter?.scope_id,
        subscription_ids: filter?.subscription_ids,
        workload_id: filter?.workload_id,
        connection_id: connectionId,
      }),
    staleTime: 5 * 60 * 1000,
  });
  const pivots = q.data?.pivots ?? {};
  const labels = q.data?.labels ?? {};
  const keys = Object.keys(labels);
  const exportFilter = {
    scope_id: filter?.scope_id,
    subscription_ids: filter?.subscription_ids,
    workload_id: filter?.workload_id,
    connection_id: connectionId,
  };
  return (
    <div className="flex h-full min-h-0">
      {download.dialog}
      <FilterRail filter={filter} onChange={setFilter} />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center gap-2 border-b bg-white px-4 py-2">
          <span className="text-sm font-medium text-gray-700">Insights</span>
          {filter && <span className="text-xs text-gray-500">· filtered to <b>{filter.label}</b></span>}
          <button
            type="button"
            onClick={() => download.start(api.iamWorkbookUrl(exportFilter), "iam-access-review.xlsx")}
            disabled={download.phase !== "idle"}
            className="ml-auto rounded border border-green-300 bg-green-50 px-2 py-1 text-xs font-medium text-green-700 hover:bg-green-100 disabled:opacity-50"
          >
            ⬇ Excel (all tabs)
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-4">
          {q.isLoading ? (
            <div className="text-sm text-gray-500">Loading…</div>
          ) : keys.length === 0 ? (
            <div className="text-sm text-gray-500">No insights yet. Run an access scan or seed demo data.</div>
          ) : (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {keys.map((kk) => (
                <PivotCard key={kk} title={labels[kk]} items={pivots[kk] ?? []} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
