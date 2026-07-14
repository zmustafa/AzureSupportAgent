import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { AzureIcon } from "./AzureIcon";

export function ManagementGroupPicker({
  value,
  valueName,
  connectionId,
  refreshToken = 0,
  onPick,
}: {
  value: string;
  valueName: string;
  connectionId?: string;
  refreshToken?: number;
  onPick: (id: string, name: string) => void;
}) {
  const groupsQ = useQuery({
    queryKey: ["management-group-picker", connectionId, refreshToken],
    queryFn: () => api.workloadTree({ connection_id: connectionId ?? "", group_by: "mg_flat", refresh: refreshToken > 0 }),
    staleTime: 24 * 60 * 60 * 1000,
  });
  const groups = (groupsQ.data?.nodes ?? []).filter((node) => node.kind === "mg");

  return (
    <div className="relative">
      <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2">
        <AzureIcon kind="mg" className="h-3.5 w-3.5" />
      </span>
      <select
        value={value}
        onChange={(event) => {
          const selected = groups.find((group) => group.id === event.target.value);
          onPick(event.target.value, selected?.name ?? event.target.value);
        }}
        disabled={groupsQ.isLoading || groupsQ.isFetching}
        title="Management group scope"
        aria-label="Management group scope"
        className="max-w-[260px] rounded-lg border py-1.5 pl-7 pr-2 text-xs disabled:opacity-50"
      >
        <option value="">{groupsQ.isLoading || groupsQ.isFetching ? "Refreshing management groups…" : "Select management group…"}</option>
        {groups.map((group) => (
          <option key={group.id} value={group.id}>
            {group.depth ? `${"  ".repeat(group.depth)}↳ ${group.name}` : group.name}
          </option>
        ))}
      </select>
      {value && valueName && <span className="sr-only">Selected {valueName}</span>}
    </div>
  );
}
