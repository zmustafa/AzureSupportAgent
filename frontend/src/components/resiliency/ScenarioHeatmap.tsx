import { useMemo } from "react";
import type { ResiliencyScenario, ResiliencyVerdict } from "../../api";
import { azurePortalResourceUrl } from "../../utils/azurePortal";
import { AzureIcon, friendlyResourceType } from "../AzureIcon";
import { useGroupedCollapse, type GroupDimension } from "../../utils/useGroupedCollapse";

/**
 * The scenario heatmap — the product's thesis made visible in one glance.
 *
 * A row green across the first three columns and red across the last two is the insight no
 * zone-centric tool surfaces: the resource is flawlessly redundant AND has no recovery path
 * from a bad deployment. That reading has to survive a two-second look, which drives every
 * rule below.
 *
 *  1. `unknown` must not look like a pass. Distinct glyph AND distinct color.
 *  2. `none` is not "red". "No recovery path exists" is categorically worse than slow, and
 *     collapsing them loses the most valuable finding the module produces.
 *  3. Color is never the only channel — the export is often printed greyscale, and roughly
 *     one reader in twelve cannot separate the red from the green.
 *  4. Not-applicable is a muted dot, never green: a stateless front end showing green for
 *     data corruption implies a protection it does not have.
 */

export type CellState = "met" | "breached" | "no_path" | "unknown" | "not_applicable";

const CELL: Record<CellState, { glyph: string; cls: string; label: string }> = {
  met: { glyph: "●", cls: "text-emerald-600", label: "Meets the objective" },
  breached: { glyph: "▲", cls: "text-amber-600", label: "Breaches the objective" },
  // Deliberately the heaviest treatment on the page.
  no_path: { glyph: "✖", cls: "text-rose-700 font-bold", label: "No recovery path exists" },
  unknown: { glyph: "?", cls: "text-gray-400", label: "Unknown — a source could not be read" },
  not_applicable: { glyph: "·", cls: "text-gray-200", label: "Does not apply to this resource" },
};

export function cellState(verdict: ResiliencyVerdict | undefined): CellState {
  if (!verdict || !verdict.applicable) return "not_applicable";
  if (verdict.rto_class === "none" || verdict.rpo_state === "none") return "no_path";
  if (verdict.rto_class === "unknown") return "unknown";
  const breach = verdict.breach?.state;
  if (breach === "breached") return "breached";
  if (breach === "undetermined") return "unknown";
  return "met";
}

export function rpoText(verdict: ResiliencyVerdict | undefined): string {
  if (!verdict) return "";
  if (verdict.rpo_state === "none") return "no recovery point";
  if (verdict.rpo_state === "unknown" || verdict.rpo_minutes === null) return "unknown";
  const m = verdict.rpo_minutes;
  if (m === 0) return "0 (synchronous)";
  if (m % 1440 === 0) return `${m / 1440}d`;
  if (m % 60 === 0) return `${m / 60}h`;
  return `${m}m`;
}

export function bandText(verdict: ResiliencyVerdict | undefined): string {
  if (!verdict?.rto_band_minutes) return "";
  const [low, high] = verdict.rto_band_minutes;
  return high >= 120 ? `${Math.round(low / 60)}–${Math.round(high / 60)}h` : `${low}–${high}m`;
}

/** The full reason a cell reads the way it does — never a bare value. */
export function cellTitle(
  verdict: ResiliencyVerdict | undefined, scenarioLabel: string, classLabel: string,
): string {
  if (!verdict) return scenarioLabel;
  if (!verdict.applicable) {
    return `${scenarioLabel}: ${verdict.basis[0]?.detail ?? "does not apply to this resource"}`;
  }
  const lines = [
    `${scenarioLabel}`,
    `RTO: ${classLabel}${bandText(verdict) ? ` (${bandText(verdict)})` : ""}`,
    `RPO: ${rpoText(verdict)}`,
    `Confidence: ${verdict.confidence}`,
  ];
  if (verdict.basis.length) lines.push("", "Why:", ...verdict.basis.map((b) => `• ${b.detail}`));
  if (verdict.rto_assumptions?.length) {
    lines.push("", "Assumptions:", ...verdict.rto_assumptions.map((a) => `• ${a}`));
  }
  return lines.join("\n");
}

export function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-3 text-[11px] text-gray-600"
         data-testid="resiliency-legend">
      {(Object.keys(CELL) as CellState[]).map((state) => (
        <span key={state} className="inline-flex items-center gap-1">
          <span className={CELL[state].cls} aria-hidden="true">{CELL[state].glyph}</span>
          <span>{CELL[state].label}</span>
        </span>
      ))}
    </div>
  );
}

/** Renders nothing when the id or cloud cannot produce a defensible URL — a link that lands
 *  on the wrong Azure cloud is worse than no link. Demo ids never qualify. */
export function PortalLink({ resourceId, portalHost, label = "Open in Azure portal", className = "" }: {
  resourceId?: string | null; portalHost?: string; label?: string; className?: string;
}) {
  const url = azurePortalResourceUrl(resourceId, portalHost);
  if (!url) return null;
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
      data-testid="resiliency-portal-link"
      className={`inline-flex shrink-0 items-center text-brand hover:underline ${className}`}
      aria-label={label}
      title={label}
    >
      ↗<span className="sr-only">{label}</span>
    </a>
  );
}

export function HeatCell({
  verdict, scenarioLabel, classLabel, onClick,
}: {
  verdict: ResiliencyVerdict | undefined;
  scenarioLabel: string;
  classLabel: string;
  onClick?: () => void;
}) {
  const state = cellState(verdict);
  const meta = CELL[state];
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid="resiliency-cell"
      data-state={state}
      title={cellTitle(verdict, scenarioLabel, classLabel)}
      aria-label={`${scenarioLabel}: ${meta.label}`}
      className={`h-7 w-full rounded text-center text-sm hover:bg-gray-100 ${meta.cls}`}
    >
      <span aria-hidden="true">{meta.glyph}</span>
    </button>
  );
}

export type HeatRow = {
  id: string; name: string; type?: string; verdicts: Record<string, ResiliencyVerdict>;
};

/** Grouping the reader can choose. Resource type first: "every storage account is
 *  unrecoverable for region loss" is one fix, and forty rows is a backlog. */
const HEAT_DIMENSIONS: GroupDimension<HeatRow>[] = [
  { key: "type", label: "Resource type",
    of: (r) => (r.type || "unknown").toLowerCase(),
    labelOf: (t) => (t === "unknown" ? "Unknown type" : friendlyResourceType(t)) },
];

/** The worst cell in a row, used to sort both rows and their groups. */
function worstOf(row: HeatRow, scenarios: { id: ResiliencyScenario }[]): number {
  const rank: Record<CellState, number> = {
    no_path: 0, breached: 1, unknown: 2, met: 3, not_applicable: 4,
  };
  return Math.min(...scenarios.map((s) => rank[cellState(row.verdicts[s.id])]));
}

function ResourceCell({ row, portalHost }: { row: HeatRow; portalHost?: string }) {
  return (
    <td className="max-w-[280px] px-2 py-1 font-medium text-gray-800">
      <span className="flex items-center gap-1.5">
        <AzureIcon kind="resource" type={row.type} className="h-3.5 w-3.5" />
        <span className="truncate" title={row.name}>{row.name}</span>
        <PortalLink resourceId={row.id} portalHost={portalHost}
                    label={`Open ${row.name} in the Azure portal`} />
      </span>
    </td>
  );
}

export function ScenarioHeatmap({
  rows, scenarios, classLabels, portalHost, onOpen, storagePrefix = "azsup.resiliency.heatmap",
}: {
  rows: HeatRow[];
  scenarios: { id: ResiliencyScenario; label: string }[];
  classLabels: Record<string, string>;
  portalHost?: string;
  onOpen?: (resourceId: string, scenario: string) => void;
  storagePrefix?: string;
}) {
  // Worst rows first: the point of the screen is what is broken, not alphabetical order.
  const ordered = useMemo(
    () => [...rows].sort((a, b) => worstOf(a, scenarios) - worstOf(b, scenarios)
      || a.name.localeCompare(b.name)),
    [rows, scenarios]);

  const grouped = useGroupedCollapse(ordered, HEAT_DIMENSIONS, {
    storagePrefix, defaultGroupBy: "none",
  });
  const sections = grouped.sections;

  const cells = (row: HeatRow) => scenarios.map((s) => (
    <td key={s.id} className="px-1 py-0.5">
      <HeatCell
        verdict={row.verdicts[s.id]}
        scenarioLabel={s.label}
        classLabel={classLabels[row.verdicts[s.id]?.rto_class ?? "unknown"] ?? ""}
        onClick={onOpen ? () => onOpen(row.id, s.id) : undefined}
      />
    </td>
  ));

  return (
    <div data-testid="resiliency-heatmap">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-gray-600">
        <label className="inline-flex items-center gap-1">
          <span>Group by</span>
          <select value={grouped.groupBy} onChange={(e) => grouped.setGroupBy(e.target.value)}
                  data-testid="heatmap-group-by"
                  className="rounded border px-1.5 py-0.5 text-[11px]">
            <option value="none">Nothing</option>
            {HEAT_DIMENSIONS.map((d) => (
              <option key={d.key} value={d.key}>{d.label}</option>
            ))}
          </select>
        </label>
        {sections && (
          <>
            <button onClick={grouped.collapseAll} className="rounded border px-1.5 py-0.5 hover:bg-gray-50">
              Collapse all
            </button>
            <button onClick={grouped.expandAll} className="rounded border px-1.5 py-0.5 hover:bg-gray-50">
              Expand all
            </button>
            <span className="text-gray-400">{sections.length} groups · {ordered.length} resources</span>
          </>
        )}
      </div>

      <div className="overflow-auto">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-white">
            <tr className="text-left text-gray-500">
              <th className="px-2 py-1.5 font-medium">Resource</th>
              {scenarios.map((s) => (
                <th key={s.id} className="px-1 py-1.5 text-center font-medium" title={s.label}>
                  {s.label.replace(" loss", "").replace("Accidental deletion", "Deletion")}
                </th>
              ))}
            </tr>
          </thead>
          {sections ? (
            sections.map((section) => {
              const collapsed = grouped.isCollapsed(section.key);
              // Worst class in the group, so a collapsed header still carries the finding.
              const worst = Math.min(...section.items.map((r) => worstOf(r, scenarios)));
              const worstState = (["no_path", "breached", "unknown", "met", "not_applicable"] as CellState[])[worst];
              return (
                <tbody key={section.key} data-testid="heatmap-group">
                  <tr className="border-t bg-gray-50">
                    <th colSpan={scenarios.length + 1} className="px-2 py-1 text-left font-medium">
                      <button onClick={() => grouped.toggle(section.key)}
                              data-testid="heatmap-group-header"
                              className="flex w-full items-center gap-1.5 text-left text-gray-700">
                        <span aria-hidden="true" className="text-gray-400">{collapsed ? "▸" : "▾"}</span>
                        <AzureIcon kind="resource" type={section.key} className="h-3.5 w-3.5" />
                        <span>{section.label}</span>
                        <span className="text-gray-400">({section.total})</span>
                        {/* The group's worst cell, so collapsing never hides the finding. */}
                        <span className={CELL[worstState].cls} title={CELL[worstState].label}
                              aria-label={`Worst in group: ${CELL[worstState].label}`}>
                          {CELL[worstState].glyph}
                        </span>
                      </button>
                    </th>
                  </tr>
                  {!collapsed && section.items.map((row) => (
                    <tr key={row.id} className="border-t hover:bg-gray-50" data-testid="resiliency-row">
                      <ResourceCell row={row} portalHost={portalHost} />
                      {cells(row)}
                    </tr>
                  ))}
                </tbody>
              );
            })
          ) : (
            <tbody>
              {ordered.map((row) => (
                <tr key={row.id} className="border-t hover:bg-gray-50" data-testid="resiliency-row">
                  <ResourceCell row={row} portalHost={portalHost} />
                  {cells(row)}
                </tr>
              ))}
            </tbody>
          )}
        </table>
        {!ordered.length && (
          <div className="p-6 text-center text-sm text-gray-400">No resources in scope.</div>
        )}
      </div>
    </div>
  );
}
