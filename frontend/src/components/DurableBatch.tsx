import { useEffect, useMemo } from "react";
import { useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";
import { api, type WorkBatch } from "../api";

export function useDurableBatch(feature: string, invalidateKeys: QueryKey[] = []) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["workBatch", feature],
    queryFn: () => api.workBatchLatest(feature),
    refetchInterval: (state) => {
      const status = state.state.data?.batch?.status;
      return status === "queued" || status === "running" ? 1000 : false;
    },
    refetchOnWindowFocus: true,
  });
  const batch = query.data?.batch ?? null;
  const active = batch?.status === "queued" || batch?.status === "running";

  useEffect(() => {
    if (!batch || active) return;
    for (const key of invalidateKeys) void queryClient.invalidateQueries({ queryKey: key });
    // Query keys are fixed per caller; batch identity/status are the intended triggers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batch?.id, batch?.status, active, queryClient]);

  const itemsByWorkload = useMemo(
    () => new Map((batch?.items ?? []).map((item) => [item.workload_id || item.item_key, item])),
    [batch],
  );

  async function launch(workloadIds: string[], config: Record<string, unknown> = {}, connectionId = "") {
    const result = await api.createWorkBatch({
      feature,
      workload_ids: workloadIds,
      connection_id: connectionId,
      config,
      idempotency_key: `${feature}:${crypto.randomUUID()}`,
    });
    queryClient.setQueryData(["workBatch", feature], result);
    return result.batch;
  }

  async function retry() {
    if (!batch) return null;
    const result = await api.retryWorkBatch(batch.id, `${feature}:retry:${crypto.randomUUID()}`);
    queryClient.setQueryData(["workBatch", feature], result);
    return result.batch;
  }

  async function cancel() {
    if (!batch || !active) return null;
    const result = await api.cancelWorkBatch(batch.id);
    queryClient.setQueryData(["workBatch", feature], result);
    return result.batch;
  }

  return { query, batch, active, itemsByWorkload, launch, retry, cancel };
}

export function DurableBatchBar({ batch, onCancel, onRetry, cancelling = false }: {
  batch: WorkBatch | null;
  onCancel?: () => void;
  onRetry?: () => void;
  cancelling?: boolean;
}) {
  if (!batch) return null;
  const active = batch.status === "queued" || batch.status === "running";
  const tone = batch.status === "failed"
    ? "border-red-200 bg-red-50 text-red-700"
    : batch.status === "partial"
      ? "border-amber-200 bg-amber-50 text-amber-800"
      : "border-blue-200 bg-blue-50 text-blue-800";
  return (
    <div data-testid={`work-batch-${batch.feature}`} className={`mt-2 flex flex-wrap items-center gap-3 rounded-md border px-3 py-2 text-xs ${tone}`}>
      <span className="font-medium">Batch {batch.status}</span>
      <span>{batch.completed}/{batch.total} complete</span>
      <span>{batch.succeeded} succeeded · {batch.partial} partial · {batch.failed} failed{batch.cancelled ? ` · ${batch.cancelled} cancelled` : ""}</span>
      <span className="text-current/70">Server-owned · safe to navigate or reload</span>
      {active && onCancel && (
        <button onClick={onCancel} disabled={cancelling} className="ml-auto rounded border border-current/30 px-2 py-0.5 font-medium disabled:opacity-50">
          {cancelling ? "Cancelling…" : "Cancel pending"}
        </button>
      )}
      {!active && onRetry && (batch.failed > 0 || batch.partial > 0 || batch.cancelled > 0) && (
        <button onClick={onRetry} className="ml-auto rounded border border-current/30 px-2 py-0.5 font-medium">
          Retry failed/partial
        </button>
      )}
    </div>
  );
}
