export type SortDirection = "asc" | "desc";

export function SortableHeader<K extends string>({
  column,
  label,
  sortColumn,
  sortDirection,
  onSort,
  className = "px-3 py-2",
}: {
  column: K;
  label: string;
  sortColumn: K | null;
  sortDirection: SortDirection;
  onSort: (column: K) => void;
  className?: string;
}) {
  const active = sortColumn === column;
  const nextDirection: SortDirection = active && sortDirection === "asc" ? "desc" : "asc";
  const title = active
    ? `Sort ${label} ${nextDirection === "asc" ? "ascending" : "descending"}`
    : `Sort ${label} ascending`;
  return (
    <th className={className} aria-sort={active ? (sortDirection === "asc" ? "ascending" : "descending") : "none"}>
      <button type="button" onClick={() => onSort(column)} title={title} aria-label={title} className={`inline-flex items-center gap-1 whitespace-nowrap rounded-sm font-medium hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 ${active ? "text-gray-900" : "text-gray-500"}`}>
        <span>{label}</span>
        <span aria-hidden="true" className={`text-[10px] ${active ? "text-brand" : "text-gray-400"}`}>{active ? (sortDirection === "asc" ? "↑" : "↓") : "↕"}</span>
      </button>
    </th>
  );
}
