/**
 * Shared Compact/Expanded density switch for the coverage dashboards (Monitoring,
 * Telemetry, Backup-DR). Previously each view inlined its own copy — and Backup-DR was
 * missing it entirely. One component keeps the three grids visually identical.
 */
export function DensityToggle({
  value,
  onChange,
  title = "Compact shows just the resource-type rows; Expanded shows the full detail.",
}: {
  value: "compact" | "expanded";
  onChange: (v: "compact" | "expanded") => void;
  title?: string;
}) {
  return (
    <div className="inline-flex overflow-hidden rounded-lg border" title={title}>
      <button
        onClick={() => onChange("compact")}
        className={`px-2 py-1.5 ${value === "compact" ? "bg-gray-900 text-white" : "text-gray-600 hover:bg-gray-50"}`}
      >
        Compact
      </button>
      <button
        onClick={() => onChange("expanded")}
        className={`px-2 py-1.5 ${value === "expanded" ? "bg-gray-900 text-white" : "text-gray-600 hover:bg-gray-50"}`}
      >
        Expanded
      </button>
    </div>
  );
}
