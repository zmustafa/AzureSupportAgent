import { useMemo, useState } from "react";
import type { CoverageResource } from "../api";

/** Shorts an ARM type like "microsoft.compute/virtualmachines" → "virtualmachines". */
function shortType(t: string): string {
  const seg = t.split("/").pop() || t;
  return seg;
}

/**
 * Flat, filterable table of every in-scope resource for a coverage screen — shared by the
 * AMBA / Telemetry / Backup-DR "All Resources" tabs. Shows which resources fall outside the
 * reference set (so users see the full footprint, not just covered types).
 */
export function AllResourcesTab({ resources }: { resources: CoverageResource[] }) {
  const [text, setText] = useState("");
  const [typeSel, setTypeSel] = useState("all");
  const [rgSel, setRgSel] = useState("all");
  const [refSel, setRefSel] = useState<"all" | "in" | "out">("all");

  const types = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of resources) m.set(r.type, (m.get(r.type) || 0) + 1);
    return [...m.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1));
  }, [resources]);

  const rgs = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of resources) if (r.resource_group) m.set(r.resource_group, (m.get(r.resource_group) || 0) + 1);
    return [...m.entries()].sort((a, b) => (a[0].toLowerCase() < b[0].toLowerCase() ? -1 : 1));
  }, [resources]);

  const filtered = useMemo(() => {
    const t = text.trim().toLowerCase();
    return resources.filter((r) => {
      if (typeSel !== "all" && r.type !== typeSel) return false;
      if (rgSel !== "all" && r.resource_group !== rgSel) return false;
      if (refSel === "in" && !r.in_reference) return false;
      if (refSel === "out" && r.in_reference) return false;
      if (t) {
        const hay = `${r.name} ${r.type} ${r.resource_group} ${r.location}`.toLowerCase();
        if (!hay.includes(t)) return false;
      }
      return true;
    });
  }, [resources, text, typeSel, rgSel, refSel]);

  const inRefCount = useMemo(() => resources.filter((r) => r.in_reference).length, [resources]);

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Search resources…"
          className="w-52 rounded-lg border px-2.5 py-1.5 outline-none focus:border-gray-400"
        />
        <select value={typeSel} onChange={(e) => setTypeSel(e.target.value)} className="rounded-lg border px-2 py-1.5">
          <option value="all">All types ({types.length})</option>
          {types.map(([t, n]) => (
            <option key={t} value={t}>
              {shortType(t)} ({n})
            </option>
          ))}
        </select>
        <select value={rgSel} onChange={(e) => setRgSel(e.target.value)} className="rounded-lg border px-2 py-1.5">
          <option value="all">All resource groups ({rgs.length})</option>
          {rgs.map(([rg, n]) => (
            <option key={rg} value={rg}>
              {rg} ({n})
            </option>
          ))}
        </select>
        <select value={refSel} onChange={(e) => setRefSel(e.target.value as "all" | "in" | "out")} className="rounded-lg border px-2 py-1.5">
          <option value="all">All ({resources.length})</option>
          <option value="in">In reference ({inRefCount})</option>
          <option value="out">Not in reference ({resources.length - inRefCount})</option>
        </select>
        <span className="text-gray-500">
          {filtered.length} of {resources.length} resource(s)
        </span>
      </div>

      {/* Table */}
      {!resources.length ? (
        <div className="py-16 text-center text-sm text-gray-400">No resources found in this scope.</div>
      ) : !filtered.length ? (
        <div className="py-16 text-center text-sm text-gray-400">No resources match the current filters.</div>
      ) : (
        <div className="overflow-hidden rounded-xl border bg-white">
          <table className="w-full text-xs">
            <thead className="bg-gray-50 text-left text-gray-500">
              <tr>
                <th className="px-3 py-2 font-medium">Resource</th>
                <th className="px-3 py-2 font-medium">Type</th>
                <th className="px-3 py-2 font-medium">Resource group</th>
                <th className="px-3 py-2 font-medium">Region</th>
                <th className="px-3 py-2 text-center font-medium">Reference</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => (
                <tr key={r.id} className="border-t hover:bg-gray-50">
                  <td className="px-3 py-2 font-medium text-gray-800">{r.name}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-gray-500">{r.type}</td>
                  <td className="px-3 py-2 text-gray-600">{r.resource_group || "—"}</td>
                  <td className="px-3 py-2 text-gray-600">{r.location || "—"}</td>
                  <td className="px-3 py-2 text-center">
                    {r.in_reference ? (
                      <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">covered</span>
                    ) : (
                      <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">not in reference</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
