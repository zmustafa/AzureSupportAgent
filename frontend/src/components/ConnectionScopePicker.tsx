import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type TenantOption } from "../api";

/**
 * A top-right Azure tenant / connection picker for the directory-wide scans (Identity, RBAC)
 * — the analog of the Performance Profiler's workload ScopePicker, but scoped to a connection
 * (tenant) rather than a workload. Lists the configured Azure connections; selecting one
 * re-scopes the page to that connection's tenant. Single-connection tenants effectively see
 * a static label. Closes on outside-click / Escape.
 *
 * The selected connection id is owned by the parent (persisted per-page) and passed to the
 * page's API calls as ``connection_id``. ``value === ""`` means "the default connection".
 */
export function ConnectionScopePicker({
  value,
  onChange,
  align = "right",
  disabled = false,
  disabledTitle,
}: {
  value: string;
  onChange: (connectionId: string) => void;
  /** Which edge the popover aligns to. Use "left" when the picker sits at the left of a bar so
   *  the menu opens rightward instead of off-screen. Defaults to "right" (top-right placements). */
  align?: "left" | "right";
  /** Render a quiet static label (no dropdown) — e.g. when the connection is locked to a
   *  selected workload. */
  disabled?: boolean;
  disabledTitle?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  // Effective menu alignment. Starts from the requested `align`, but is flipped on open if that
  // edge would push the 288px-wide (w-72) menu off-screen (e.g. a left-of-toolbar picker whose
  // default right-alignment would extend the menu leftward under the sidebar).
  const [placement, setPlacement] = useState<"left" | "right">(align);

  const connQ = useQuery({ queryKey: ["azure-connections"], queryFn: api.azureConnections, retry: false });
  const conns: TenantOption[] = connQ.data?.connections ?? [];

  useEffect(() => {
    if (!open) { setPlacement(align); return; }
    const btn = ref.current?.getBoundingClientRect();
    if (!btn) return;
    const MENU_W = 288; // w-72
    const margin = 8;
    // Prefer opening RIGHTWARD (left-aligned): a leftward (right-aligned) menu can slide under a
    // fixed left sidebar even while technically on-screen. Only open leftward when rightward would
    // overflow the right viewport edge (e.g. a picker pinned to the top-right of a bar).
    const fitsRightward = btn.left + MENU_W <= window.innerWidth - margin;
    setPlacement(fitsRightward ? "left" : "right");
  }, [open, align]);

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Nothing to pick when there is at most one connection — render a quiet static label.
  if (conns.length <= 1) {
    const only = conns[0];
    if (!only) return null;
    return (
      <span className="inline-flex items-center gap-1.5 rounded-md border bg-white px-2.5 py-1.5 text-xs text-gray-500" title={only.tenant_id}>
        <span>🏢</span>
        <span className="max-w-[160px] truncate">{only.display_name}</span>
      </span>
    );
  }

  const active = conns.find((c) => c.id === value) ?? conns.find((c) => c.is_default) ?? conns[0];

  // Locked (e.g. connection follows the selected workload) — show a quiet static label.
  if (disabled) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-md border bg-white px-2.5 py-1.5 text-xs text-gray-400" title={disabledTitle || active?.tenant_id}>
        <span>🏢</span>
        <span className="max-w-[160px] truncate">{active?.display_name ?? "—"}</span>
      </span>
    );
  }

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title="Azure tenant / connection to scope this page"
        className="flex items-center gap-1.5 rounded-md border bg-white px-2.5 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50"
      >
        <span>🏢</span>
        <span className="max-w-[160px] truncate">{active?.display_name ?? "Select tenant"}</span>
        <span className="text-gray-400">▾</span>
      </button>
      {open && (
        <div className={`absolute z-50 mt-1 w-72 rounded-md border bg-white p-1.5 shadow-lg ${placement === "left" ? "left-0" : "right-0"}`}>
          <div className="px-2 py-1 text-[10px] font-medium uppercase tracking-wide text-gray-400">Azure tenant</div>
          {conns.map((c) => (
            <button
              key={c.id}
              onClick={() => {
                onChange(c.id);
                setOpen(false);
              }}
              className={`flex w-full items-start justify-between gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-gray-50 ${
                c.id === active?.id ? "bg-brand/5 text-brand" : "text-gray-700"
              }`}
            >
              <span className="min-w-0">
                <span className="block truncate font-medium">{c.display_name}</span>
                <span className="block truncate text-[11px] text-gray-400">{c.tenant_id}</span>
              </span>
              <span className="flex shrink-0 items-center gap-1 pt-0.5">
                {c.is_default && <span className="rounded bg-gray-100 px-1.5 text-[10px] text-gray-500">default</span>}
                {c.read_only && <span className="rounded bg-amber-50 px-1.5 text-[10px] text-amber-600">read-only</span>}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
