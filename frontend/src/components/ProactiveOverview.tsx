// Proactive Support landing page — the bare /proactive view. Mirrors the Settings overview
// (AdminView › SettingsOverview): a searchable, grouped grid of cards over PROACTIVE_NAV,
// using the same category clusters the sidebar submenu draws. Lazy-loaded by ChatView.
import { useState } from "react";
import { Link } from "react-router-dom";
import { PROACTIVE_NAV } from "./navConfig";

export function ProactiveOverviewPanel() {
  const [q, setQ] = useState("");
  const term = q.trim().toLowerCase();

  // Preserve the PROACTIVE_NAV order but split into the same groups the sidebar uses. Each
  // item joins the group of the nearest preceding item that declared one.
  const groups: { name: string; items: typeof PROACTIVE_NAV }[] = [];
  for (const item of PROACTIVE_NAV) {
    if (item.group || groups.length === 0) {
      groups.push({ name: item.group ?? "Proactive Support", items: [] });
    }
    groups[groups.length - 1].items.push(item);
  }

  const matches = (i: (typeof PROACTIVE_NAV)[number]) =>
    !term || i.label.toLowerCase().includes(term) || (i.desc ?? "").toLowerCase().includes(term);
  const visibleGroups = groups
    .map((g) => ({ ...g, items: g.items.filter(matches) }))
    .filter((g) => g.items.length > 0);

  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="mx-auto max-w-5xl space-y-6 p-8">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-gray-900">Proactive Support</h1>
            <p className="mt-1 text-sm text-gray-500">
              Design and own your estate, assess its posture, measure monitoring &amp; backup
              coverage, and investigate changes, identity and cost — all in one place.
            </p>
          </div>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search proactive tools…"
            className="w-56 rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand-dark focus:outline-none"
          />
        </div>

        {visibleGroups.length === 0 ? (
          <p className="rounded-xl border border-dashed bg-white p-10 text-center text-sm text-gray-400">
            No tools match "{q}".
          </p>
        ) : (
          visibleGroups.map((g) => (
            <div key={g.name}>
              <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
                {g.name}
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {g.items.map((i) => (
                  <Link
                    key={i.id}
                    to={i.to}
                    className="group flex items-start gap-3 rounded-xl border bg-white p-4 transition hover:border-brand-dark/40 hover:shadow-sm"
                  >
                    <span className="text-xl leading-none">{i.icon}</span>
                    <span className="min-w-0">
                      <span className="block font-medium text-gray-800 group-hover:text-brand-dark">{i.label}</span>
                      {i.desc && <span className="mt-0.5 block text-xs leading-relaxed text-gray-500">{i.desc}</span>}
                    </span>
                  </Link>
                ))}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
