import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams, useLocation } from "react-router-dom";
import { api, type Architecture, type ArchitectureCollection, type ArchitectureJob, type ArchitectureRevision, type ArchitectureState, type Workload } from "../api";
import { formatError, formatTimestamp } from "../utils/format";
import { ArchitectureCanvas, ArchitecturePreview } from "./ArchitectureCanvas";
import { MemoryEditor, MemoryIndex } from "./ArchitectureMemoryView";

// ---------------- Lifecycle states (fixed workflow) ----------------
const STATE_META: Record<ArchitectureState, { label: string; badge: string; dot: string }> = {
  draft: { label: "Draft", badge: "bg-gray-100 text-gray-600", dot: "#9ca3af" },
  in_review: { label: "In Review", badge: "bg-amber-100 text-amber-700", dot: "#d97706" },
  ready: { label: "Ready", badge: "bg-green-100 text-green-700", dot: "#16a34a" },
  archived: { label: "Archived", badge: "bg-slate-100 text-slate-500", dot: "#64748b" },
};
const STATE_ORDER: ArchitectureState[] = ["draft", "in_review", "ready", "archived"];

function StateBadge({ state }: { state: ArchitectureState }) {
  const m = STATE_META[state] ?? STATE_META.draft;
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${m.badge}`}>
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: m.dot }} />
      {m.label}
    </span>
  );
}

/** Health score badge — pure render from a score already resolved by the parent (the
 *  list fetches the assessment portfolio ONCE and passes each score down, avoiding an
 *  N+1 of /assessments/runs?workload_id=… — one request per architecture card). */
function HealthBadge({ score }: { score: number | null | undefined }) {
  if (score == null) return null;
  const s = score;
  const cls = s >= 80 ? "bg-green-100 text-green-700" : s >= 50 ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-700";
  return <span className={`rounded px-1.5 py-0.5 font-medium ${cls}`} title="Latest Well-Architected assessment score">🛡 {s}/100</span>;
}

/** Compact native-select to change an architecture's lifecycle state inline. */
function StateSelect({ value, onChange, disabled }: { value: ArchitectureState; onChange: (s: ArchitectureState) => void; disabled?: boolean }) {
  const m = STATE_META[value] ?? STATE_META.draft;
  return (
    <select
      value={value}
      disabled={disabled}
      onClick={(e) => e.stopPropagation()}
      onChange={(e) => { e.stopPropagation(); onChange(e.target.value as ArchitectureState); }}
      className={`rounded-md border px-1.5 py-1 text-[11px] font-medium focus:outline-none focus:ring-1 focus:ring-brand ${m.badge}`}
      title="Lifecycle state"
    >
      {STATE_ORDER.map((s) => <option key={s} value={s}>{STATE_META[s].label}</option>)}
    </select>
  );
}

/** Compact native-select to assign an architecture to a category/solution inline. */
function CategorySelect({ value, collections, onChange, disabled }: { value: string; collections: ArchitectureCollection[]; onChange: (id: string) => void; disabled?: boolean }) {
  return (
    <select
      value={value}
      disabled={disabled}
      onClick={(e) => e.stopPropagation()}
      onChange={(e) => { e.stopPropagation(); onChange(e.target.value); }}
      className="max-w-[10rem] truncate rounded-md border px-1.5 py-1 text-[11px] text-gray-600 focus:outline-none focus:ring-1 focus:ring-brand"
      title="Category / solution"
    >
      <option value="">Uncategorized</option>
      {collections.map((c) => <option key={c.id} value={c.id}>{c.icon} {c.name}</option>)}
    </select>
  );
}

/** Compact native-select to link an architecture to a workload (or none) inline. */
function WorkloadSelect({ value, workloads, onChange, disabled }: { value: string; workloads: { id: string; name: string }[]; onChange: (id: string) => void; disabled?: boolean }) {
  return (
    <select
      value={value}
      disabled={disabled}
      onClick={(e) => e.stopPropagation()}
      onChange={(e) => { e.stopPropagation(); onChange(e.target.value); }}
      className="max-w-[11rem] truncate rounded-md border px-1.5 py-1 text-[11px] text-gray-600 focus:outline-none focus:ring-1 focus:ring-brand"
      title="Linked workload"
    >
      <option value="">🔗 No workload</option>
      {workloads.map((w) => <option key={w.id} value={w.id}>🧩 {w.name}</option>)}
    </select>
  );
}

// ---------------- List + create ----------------
function ArchitecturesList() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const q = useQuery({ queryKey: ["architectures"], queryFn: api.architectures });
  const collQ = useQuery({ queryKey: ["architectureCollections"], queryFn: api.architectureCollections });
  // One portfolio fetch powers every card's health badge (was an N+1 per workload).
  const portfolioQ = useQuery({ queryKey: ["assessmentPortfolio"], queryFn: api.assessmentPortfolio, staleTime: 60_000 });
  // Which architectures have a Memory (one cheap call → set of architecture ids).
  const memoriesQ = useQuery({ queryKey: ["architectureMemories"], queryFn: api.architectureMemories, staleTime: 60_000 });
  const [creating, setCreating] = useState(false);
  const [managing, setManaging] = useState(false);
  const [msg, setMsg] = useState("");
  const [search, setSearch] = useState("");
  const [stateFilter, setStateFilter] = useState<"active" | ArchitectureState>("active");
  const [catFilter, setCatFilter] = useState("all");
  const [sortBy, setSortBy] = useState<"updated" | "name" | "health" | "resources">("updated");
  const [showThumbs, setShowThumbs] = useState(true);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const architectures = q.data?.architectures ?? [];
  const collections = collQ.data?.collections ?? [];
  // workload_id -> latest overall score, from the single portfolio query.
  const scoreByWorkload = useMemo(() => {
    const m = new Map<string, number | null>();
    for (const row of portfolioQ.data?.workloads ?? []) m.set(row.workload_id, row.overall_score);
    return m;
  }, [portfolioQ.data]);

  // architecture ids that have a Memory authored (for the card chip).
  const memoryIds = useMemo(
    () => new Set((memoriesQ.data?.memories ?? []).map((m) => m.architecture_id)),
    [memoriesQ.data],
  );

  async function blank() {
    try {
      const res = await api.upsertArchitecture({ name: "New architecture", source: "manual", nodes: [], edges: [], groups: [] });
      qc.invalidateQueries({ queryKey: ["architectures"] });
      navigate(`/architectures/${res.architecture.id}`);
    } catch (e) { setMsg(formatError(e)); }
  }

  async function del(id: string) {
    if (!window.confirm("Delete this architecture?")) return;
    try { await api.deleteArchitecture(id); qc.invalidateQueries({ queryKey: ["architectures"] }); }
    catch (e) { setMsg(formatError(e)); }
  }

  async function changeState(id: string, state: ArchitectureState) {
    try { await api.setArchitectureState(id, state); qc.invalidateQueries({ queryKey: ["architectures"] }); }
    catch (e) { setMsg(formatError(e)); }
  }
  async function changeCategory(id: string, categoryId: string) {
    try { await api.setArchitectureCategory(id, categoryId); qc.invalidateQueries({ queryKey: ["architectures"] }); }
    catch (e) { setMsg(formatError(e)); }
  }
  async function clone(id: string) {
    try { await api.cloneArchitecture(id); qc.invalidateQueries({ queryKey: ["architectures"] }); }
    catch (e) { setMsg(formatError(e)); }
  }

  // Apply search + state filter.
  const term = search.trim().toLowerCase();
  const filtered = architectures.filter((a) => {
    if (term && !(`${a.name} ${a.description} ${a.workload_name}`.toLowerCase().includes(term))) return false;
    const st = a.state ?? "draft";
    if (stateFilter === "active") return st !== "archived";
    return st === stateFilter;
  }).sort((a, b) => {
    if (sortBy === "name") return a.name.localeCompare(b.name);
    if (sortBy === "resources") return (b.nodes.length) - (a.nodes.length);
    if (sortBy === "health") return (scoreByWorkload.get(b.workload_id) ?? -1) - (scoreByWorkload.get(a.workload_id) ?? -1);
    return (b.updated_at || "").localeCompare(a.updated_at || ""); // updated (default)
  });

  // Build category sections (collections in order, then Uncategorized), honoring catFilter.
  const byCat = (cid: string) => filtered.filter((a) => (a.category_id || "") === cid);
  type Section = { id: string; label: string; icon: string; color: string; items: Architecture[] };
  const sections: Section[] = [];
  for (const c of collections) {
    if (catFilter !== "all" && catFilter !== c.id) continue;
    const items = byCat(c.id);
    if (items.length || catFilter === c.id) sections.push({ id: c.id, label: c.name, icon: c.icon, color: c.color, items });
  }
  if (catFilter === "all" || catFilter === "") {
    const uncategorized = byCat("");
    if (uncategorized.length) sections.push({ id: "", label: "Uncategorized", icon: "🗂️", color: "#9ca3af", items: uncategorized });
  }

  function toggleSection(id: string) {
    setCollapsed((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  }

  function renderCard(a: Architecture) {
    return (
      <div key={a.id} className="group rounded-xl border bg-white p-4 shadow-sm transition hover:shadow-md">
        <button onClick={() => navigate(`/architectures/${a.id}`)} className="block w-full text-left">
          {showThumbs && a.nodes.length > 0 && (
            <div className="mb-2 h-28 overflow-hidden rounded-lg border bg-gray-50/50">
              <div className="pointer-events-none h-full w-full">
                <ArchitecturePreview arch={a} />
              </div>
            </div>
          )}
          <div className="flex items-center gap-2">
            <span className="truncate font-semibold text-gray-800 group-hover:text-brand">{a.name}</span>
            {a.source === "ai" && <span className="rounded-full bg-violet-100 px-1.5 py-0.5 text-[10px] font-medium text-violet-700">✨ AI</span>}
            {memoryIds.has(a.id) && <span title="Has Architecture Memory" className="rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">🧠 Memory</span>}
            <span className="ml-auto"><StateBadge state={a.state ?? "draft"} /></span>
          </div>
          {a.description && <p className="mt-0.5 line-clamp-2 text-xs text-gray-500">{a.description}</p>}
          <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-gray-400">
            <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-600">{a.nodes.length} resources</span>
            <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-600">{a.edges.length} links</span>
            {a.workload_id && <HealthBadge score={scoreByWorkload.get(a.workload_id)} />}
            {a.workload_name && <span>· {a.workload_name}</span>}
            {(a.updated_by || a.created_by) && <span>· by {a.updated_by || a.created_by}</span>}
            {a.updated_at && <span className="ml-auto">{formatTimestamp(a.updated_at)}</span>}
          </div>
        </button>
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <StateSelect value={a.state ?? "draft"} onChange={(s) => void changeState(a.id, s)} />
          <CategorySelect value={a.category_id || ""} collections={collections} onChange={(c) => void changeCategory(a.id, c)} />
          <div className="ml-auto flex gap-1.5">
            <button onClick={() => navigate(`/architectures/${a.id}`)} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">Open</button>
            <button onClick={() => void clone(a.id)} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">Clone</button>
            <button onClick={() => void del(a.id)} className="rounded-lg border border-red-200 px-2.5 py-1 text-xs text-red-600 hover:bg-red-50">Delete</button>
          </div>
        </div>
      </div>
    );
  }

  const grouped = catFilter === "all";

  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="space-y-5 p-6">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-gray-800">Architectures</h1>
            <p className="mt-1 text-sm text-gray-500">
              Diagram your applications — drag &amp; drop Azure resources and connect them, or
              reverse-engineer an architecture from a workload with AI (built from the live
              Azure Resource Graph configuration of every resource).
            </p>
          </div>
          <div className="flex shrink-0 gap-2">
            <button onClick={() => navigate("/architectures/memory")} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50">🧠 Memory</button>
            <button onClick={() => setManaging(true)} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50">🗂️ Categories</button>
            <button onClick={() => setCreating(true)} className="rounded-lg border border-brand/40 px-3 py-1.5 text-sm font-medium text-brand hover:bg-brand/5">✨ From a workload (AI)</button>
            <button onClick={() => void blank()} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90">+ Blank</button>
          </div>
        </div>

        {msg && <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{msg}</div>}
        {creating && <FromWorkloadModal onClose={() => setCreating(false)} onQueued={() => setCreating(false)} />}
        {managing && <ManageCategoriesModal collections={collections} onClose={() => setManaging(false)} />}

        <GenerationJobs />

        {/* Toolbar: search + state filter + category filter */}
        {architectures.length > 0 && (
          <div className="flex flex-wrap items-center gap-2">
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search architectures…"
              className="w-48 rounded-lg border px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand" />
            <div className="flex rounded-lg border bg-white p-0.5 text-xs">
              {([["active", "Active"], ["draft", "Draft"], ["in_review", "In Review"], ["ready", "Ready"], ["archived", "Archived"]] as [typeof stateFilter, string][]).map(([v, label]) => (
                <button key={v} onClick={() => setStateFilter(v)} className={`rounded-md px-2.5 py-1 font-medium ${stateFilter === v ? "bg-brand text-white" : "text-gray-500 hover:text-gray-700"}`}>{label}</button>
              ))}
            </div>
            {collections.length > 0 && (
              <select value={catFilter} onChange={(e) => setCatFilter(e.target.value)}
                className="rounded-lg border px-2.5 py-1.5 text-sm text-gray-600 focus:outline-none focus:ring-2 focus:ring-brand">
                <option value="all">All categories</option>
                {collections.map((c) => <option key={c.id} value={c.id}>{c.icon} {c.name}</option>)}
                <option value="">Uncategorized</option>
              </select>
            )}
            <select value={sortBy} onChange={(e) => setSortBy(e.target.value as typeof sortBy)} title="Sort"
              className="rounded-lg border px-2.5 py-1.5 text-sm text-gray-600 focus:outline-none focus:ring-2 focus:ring-brand">
              <option value="updated">Sort: Last updated</option>
              <option value="name">Sort: Name</option>
              <option value="health">Sort: Health</option>
              <option value="resources">Sort: Resources</option>
            </select>
            <button onClick={() => setShowThumbs((v) => !v)} title="Toggle diagram thumbnails"
              className={`rounded-lg border px-2.5 py-1.5 text-sm ${showThumbs ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}>
              🖼️ Thumbnails
            </button>
          </div>
        )}

        {q.isLoading && <div className="text-sm text-gray-500">Loading…</div>}
        {!q.isLoading && architectures.length === 0 && (
          <div className="rounded-xl border border-dashed bg-white px-6 py-12 text-center">
            <div className="text-3xl">🗺️</div>
            <p className="mt-2 text-sm text-gray-500">No architectures yet. Reverse-engineer one from a workload with AI, or start from a blank canvas.</p>
          </div>
        )}
        {!q.isLoading && architectures.length > 0 && filtered.length === 0 && (
          <div className="rounded-xl border border-dashed bg-white px-6 py-8 text-center text-sm text-gray-500">No architectures match these filters.</div>
        )}

        {/* Grouped by category, or a flat grid when a single category is selected */}
        {grouped ? (
          <div className="space-y-5">
            {sections.map((s) => (
              <div key={s.id || "uncat"}>
                <button onClick={() => toggleSection(s.id)} className="mb-2 flex w-full items-center gap-2 text-left">
                  <span className="text-xs text-gray-400">{collapsed.has(s.id) ? "▸" : "▾"}</span>
                  <span className="h-2.5 w-2.5 rounded-full" style={{ background: s.color }} />
                  <span className="text-sm font-semibold text-gray-700">{s.icon} {s.label}</span>
                  <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">{s.items.length}</span>
                </button>
                {!collapsed.has(s.id) && (
                  s.items.length === 0
                    ? <p className="pl-5 text-xs text-gray-400">No architectures in this category.</p>
                    : <div className="grid grid-cols-1 gap-3 md:grid-cols-2">{s.items.map(renderCard)}</div>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">{filtered.map(renderCard)}</div>
        )}
      </div>
    </div>
  );
}

function FromWorkloadModal({ onClose, onQueued }: { onClose: () => void; onQueued: (n: number) => void }) {
  const qc = useQueryClient();
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const workloads = (wlQ.data?.workloads ?? []) as Workload[];
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const shown = workloads.filter((w) => w.name.toLowerCase().includes(filter.trim().toLowerCase()));
  const allShownSelected = shown.length > 0 && shown.every((w) => selected.has(w.id));

  function toggle(id: string) {
    setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  }
  function toggleAllShown() {
    setSelected((s) => {
      const n = new Set(s);
      if (allShownSelected) shown.forEach((w) => n.delete(w.id));
      else shown.forEach((w) => n.add(w.id));
      return n;
    });
  }

  async function build() {
    if (selected.size === 0) return;
    setBusy(true); setError("");
    try {
      const res = await api.createArchitectureJobs([...selected]);
      qc.invalidateQueries({ queryKey: ["architectureJobs"] });
      onQueued(res.queued);
    } catch (e) { setError(formatError(e)); setBusy(false); }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => !busy && onClose()}>
      <div className="flex max-h-[85vh] w-full max-w-lg flex-col rounded-2xl bg-white p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <h2 className="text-lg font-semibold text-gray-800">✨ Reverse-engineer architecture</h2>
        <p className="mt-1 text-sm text-gray-500">Pick one or more workloads. Each runs as a background job that pulls every member resource with its full Azure Resource Graph configuration and asks the AI to infer the application architecture — you can keep working while they build.</p>

        <div className="mt-3 flex items-center gap-2">
          <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter workloads…"
            className="w-full rounded-lg border px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand" />
          {shown.length > 0 && (
            <button onClick={toggleAllShown} className="shrink-0 rounded-lg border px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-50">
              {allShownSelected ? "Clear" : "All"}
            </button>
          )}
        </div>

        <div className="mt-2 min-h-0 flex-1 overflow-y-auto rounded-lg border">
          {wlQ.isLoading && <div className="p-3 text-sm text-gray-400">Loading workloads…</div>}
          {!wlQ.isLoading && shown.length === 0 && <div className="p-3 text-sm text-gray-400">No workloads match.</div>}
          {shown.map((w) => (
            <label key={w.id} className="flex cursor-pointer items-center gap-2 border-b px-3 py-2 text-sm last:border-b-0 hover:bg-gray-50">
              <input type="checkbox" checked={selected.has(w.id)} onChange={() => toggle(w.id)} className="shrink-0" />
              <span className="truncate text-gray-700">{w.name}</span>
              <span className="ml-auto shrink-0 text-[11px] text-gray-400">{w.nodes.length} members</span>
            </label>
          ))}
        </div>

        {error && <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>}

        <div className="mt-4 flex items-center justify-between gap-2">
          <span className="text-xs text-gray-500">{selected.size} selected</span>
          <div className="flex gap-2">
            <button onClick={onClose} disabled={busy} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50 disabled:opacity-50">Cancel</button>
            <button onClick={() => void build()} disabled={busy || selected.size === 0}
              className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">
              {busy ? "Queueing…" : `Build ${selected.size || ""} architecture${selected.size === 1 ? "" : "s"}`.replace(/\s+/g, " ").trim()}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------- Background generation jobs ----------------
function GenerationJobs() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const seenDone = useRef<Set<string>>(new Set());
  const q = useQuery({
    queryKey: ["architectureJobs"],
    queryFn: api.architectureJobs,
    refetchInterval: (query) => {
      const data = query.state.data as { jobs?: ArchitectureJob[] } | undefined;
      const active = (data?.jobs ?? []).some((j) => j.status === "queued" || j.status === "running");
      return active ? 1500 : false;
    },
  });
  const jobs = q.data?.jobs ?? [];

  useEffect(() => {
    let newlyDone = false;
    for (const j of jobs) {
      if (j.status === "done" && j.architecture_id && !seenDone.current.has(j.id)) {
        seenDone.current.add(j.id);
        newlyDone = true;
      }
    }
    if (newlyDone) qc.invalidateQueries({ queryKey: ["architectures"] });
  }, [jobs, qc]);

  async function cancel(id: string) {
    try { await api.cancelArchitectureJob(id); } catch { /* ignore */ }
    qc.invalidateQueries({ queryKey: ["architectureJobs"] });
  }
  async function dismiss(id: string) {
    try { await api.dismissArchitectureJob(id); } catch { /* ignore */ }
    qc.invalidateQueries({ queryKey: ["architectureJobs"] });
  }

  if (jobs.length === 0) return null;
  const active = jobs.filter((j) => j.status === "queued" || j.status === "running");
  const finished = jobs.filter((j) => j.status !== "queued" && j.status !== "running");

  return (
    <div className="space-y-2 rounded-xl border bg-white p-3 shadow-sm">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-700">AI generation{active.length > 0 ? ` · ${active.length} running` : ""}</h2>
        {finished.length > 0 && (
          <button onClick={() => finished.forEach((j) => void dismiss(j.id))} className="text-xs text-gray-400 hover:text-gray-700">Clear finished</button>
        )}
      </div>
      <div className="space-y-2">
        {jobs.map((j) => (
          <JobRow key={j.id} job={j}
            onCancel={() => void cancel(j.id)}
            onDismiss={() => void dismiss(j.id)}
            onOpen={() => navigate(`/architectures/${j.architecture_id}`)} />
        ))}
      </div>
    </div>
  );
}

function JobRow({ job, onCancel, onDismiss, onOpen }: { job: ArchitectureJob; onCancel: () => void; onDismiss: () => void; onOpen: () => void }) {
  const active = job.status === "queued" || job.status === "running";
  const barColor = job.status === "error" ? "bg-red-400" : job.status === "canceled" ? "bg-gray-300" : job.status === "done" ? "bg-green-500" : "bg-brand";
  const pct = job.status === "done" ? 100 : job.progress;
  return (
    <div className="rounded-lg border px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="truncate text-sm font-medium text-gray-700">{job.workload_name}</span>
        <StatusPill status={job.status} />
        <div className="ml-auto flex shrink-0 items-center gap-1.5">
          {job.status === "done" && job.architecture_id && (
            <button onClick={onOpen} className="rounded-lg bg-brand px-2.5 py-1 text-xs font-medium text-white hover:bg-brand/90">Open</button>
          )}
          {active && (
            <button onClick={onCancel} className="rounded-lg border border-red-200 px-2.5 py-1 text-xs text-red-600 hover:bg-red-50">Cancel</button>
          )}
          {!active && (
            <button onClick={onDismiss} className="rounded-lg border px-2.5 py-1 text-xs text-gray-500 hover:bg-gray-50">Dismiss</button>
          )}
        </div>
      </div>
      <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-gray-100">
        <div className={`h-full rounded-full transition-all ${barColor} ${job.status === "running" ? "animate-pulse" : ""}`} style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 flex items-center gap-2 text-[11px] text-gray-500">
        <span className="truncate">{job.status === "error" ? job.error : job.message}</span>
        {job.resource_count > 0 && <span className="shrink-0 text-gray-400">· {job.resource_count} resources</span>}
      </div>
    </div>
  );
}

function StatusPill({ status }: { status: ArchitectureJob["status"] }) {
  const map: Record<string, string> = {
    queued: "bg-gray-100 text-gray-500",
    running: "bg-brand/10 text-brand",
    done: "bg-green-100 text-green-700",
    error: "bg-red-100 text-red-700",
    canceled: "bg-gray-100 text-gray-500",
  };
  return <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${map[status]}`}>{status}</span>;
}

// ---------------- Editor ----------------
function ArchitectureEditor({ id }: { id: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["architecture", id], queryFn: () => api.architecture(id) });
  const collQ = useQuery({ queryKey: ["architectureCollections"], queryFn: api.architectureCollections });
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const arch = q.data?.architecture;
  const collections = collQ.data?.collections ?? [];
  const workloads = wlQ.data?.workloads ?? [];
  const [showHistory, setShowHistory] = useState(false);
  const [showActivity, setShowActivity] = useState(false);
  const [restoreKey, setRestoreKey] = useState(0);
  const [previewRev, setPreviewRev] = useState<ArchitectureRevision | null>(null);
  // Active in-place rebuild job (re-reverse-engineer from the linked workload).
  const [rebuildJobId, setRebuildJobId] = useState<string | null>(null);
  const [rebuildMsg, setRebuildMsg] = useState("");

  // Read-only content of the revision being previewed (fetched on demand).
  const previewQ = useQuery({
    queryKey: ["architectureRevision", id, previewRev?.id],
    queryFn: () => api.architectureRevision(id, previewRev!.id),
    enabled: !!previewRev,
  });
  const previewArch: Architecture | null = previewRev && previewQ.data
    ? ({ ...arch, ...previewQ.data.revision, id } as Architecture)
    : null;

  async function changeState(s: ArchitectureState) {
    try { await api.setArchitectureState(id, s); qc.invalidateQueries({ queryKey: ["architecture", id] }); qc.invalidateQueries({ queryKey: ["architectures"] }); }
    catch { /* ignore */ }
  }
  async function changeCategory(c: string) {
    try { await api.setArchitectureCategory(id, c); qc.invalidateQueries({ queryKey: ["architecture", id] }); qc.invalidateQueries({ queryKey: ["architectures"] }); }
    catch { /* ignore */ }
  }
  async function changeWorkload(w: string) {
    try { await api.setArchitectureWorkload(id, w); qc.invalidateQueries({ queryKey: ["architecture", id] }); qc.invalidateQueries({ queryKey: ["architectures"] }); }
    catch { /* ignore */ }
  }
  async function rebuildFromWorkload(workloadId: string) {
    if (!workloadId) { setRebuildMsg("Link a workload first, then rebuild."); return; }
    setRebuildMsg("");
    try {
      const { job } = await api.rebuildArchitecture(id, workloadId);
      setRebuildJobId(job.id);
    } catch (e) {
      setRebuildMsg(e instanceof Error ? e.message : "Failed to start rebuild.");
    }
  }
  // Poll the rebuild job until it finishes, then refresh the canvas in place.
  useEffect(() => {
    if (!rebuildJobId) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const { jobs } = await api.architectureJobs();
        const job = jobs.find((j) => j.id === rebuildJobId);
        if (!job) return;
        if (job.status === "done") {
          setRebuildJobId(null);
          setRebuildMsg("");
          await qc.invalidateQueries({ queryKey: ["architecture", id] });
          qc.invalidateQueries({ queryKey: ["architectures"] });
          qc.invalidateQueries({ queryKey: ["architectureRevisions", id] });
          setRestoreKey((k) => k + 1);
        } else if (job.status === "error" || job.status === "canceled") {
          setRebuildJobId(null);
          setRebuildMsg(job.error || "Rebuild did not complete.");
        } else {
          setRebuildMsg(job.message || "Rebuilding…");
        }
      } catch { /* keep polling */ }
    };
    void tick();
    const t = setInterval(() => { if (!cancelled) void tick(); }, 1500);
    return () => { cancelled = true; clearInterval(t); };
  }, [rebuildJobId, id, qc]);
  async function handleRestored() {
    // Refetch the architecture (await active refetch) before re-mounting the canvas so it
    // initializes from the restored content; bump restoreKey to force a fresh mount. The
    // history panel stays open (Word-style) and refreshes to show the new revision on top.
    setPreviewRev(null);
    await qc.invalidateQueries({ queryKey: ["architecture", id] });
    qc.invalidateQueries({ queryKey: ["architectures"] });
    qc.invalidateQueries({ queryKey: ["architectureRevisions", id] });
    setRestoreKey((k) => k + 1);
  }
  async function restorePreviewed() {
    if (!previewRev) return;
    if (!window.confirm("Restore this version? The current version is saved to history first, so you won't lose it.")) return;
    try { await api.restoreArchitectureRevision(id, previewRev.id); await handleRestored(); }
    catch { /* ignore */ }
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-white">
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <button onClick={() => navigate("/architectures")} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">← Architectures</button>
        {arch && <StateSelect value={arch.state ?? "draft"} onChange={(s) => void changeState(s)} />}
        {arch && <CategorySelect value={arch.category_id || ""} collections={collections} onChange={(c) => void changeCategory(c)} />}
        {arch && <WorkloadSelect value={arch.workload_id || ""} workloads={workloads} onChange={(w) => void changeWorkload(w)} disabled={!!rebuildJobId} />}
        {arch && (
          <button
            onClick={() => void rebuildFromWorkload(arch.workload_id || "")}
            disabled={!arch.workload_id || !!rebuildJobId}
            title={arch.workload_id ? "Re-reverse-engineer this diagram from the workload's current Azure resources" : "Link a workload to enable rebuild"}
            className="flex items-center gap-1 rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40"
          >
            {rebuildJobId ? <><span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-brand border-t-transparent" /> Rebuilding…</> : "🔄 Rebuild from workload"}
          </button>
        )}
        {arch && (
          <button
            onClick={() => navigate(`/architectures/${id}/memory`)}
            title="Open this architecture's Memory — a knowledge base used to inform deep investigations"
            className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50"
          >
            🧠 Memory
          </button>
        )}
        <button onClick={() => setShowActivity(true)} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">📋 Activity</button>
        <button onClick={() => { setShowHistory((v) => { if (v) setPreviewRev(null); return !v; }); }}
          className={`rounded-lg border px-2.5 py-1 text-xs ${showHistory ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}>🕘 History</button>
        {arch?.ai?.rationale && <RationaleChip text={arch.ai.rationale} confidence={arch.ai.confidence} count={arch.ai.resource_count} />}
        {arch && (arch.created_by || arch.updated_by) && <AuthorInfo arch={arch} />}
        {rebuildMsg && <span className="text-[11px] text-amber-600">{rebuildMsg}</span>}
      </div>
      <div className="flex min-h-0 flex-1">
        <div className="flex min-h-0 flex-1 flex-col">
          {q.isLoading && <div className="p-6 text-sm text-gray-500">Loading…</div>}
          {q.isError && <div className="p-6 text-sm text-red-600">Architecture not found.</div>}
          {arch && previewRev && (
            <>
              <div className="flex flex-wrap items-center gap-2 border-b border-amber-200 bg-amber-50 px-3 py-1.5 text-xs text-amber-800">
                <span>👁 Viewing version{previewRev.created_at ? ` from ${formatTimestamp(previewRev.created_at)}` : ""} ({previewRev.reason}) — read-only</span>
                <div className="ml-auto flex gap-1.5">
                  <button onClick={() => void restorePreviewed()} className="rounded-md border border-amber-300 bg-white px-2.5 py-1 text-[11px] font-medium text-amber-800 hover:bg-amber-100">↩️ Restore this version</button>
                  <button onClick={() => setPreviewRev(null)} className="rounded-md border px-2.5 py-1 text-[11px] text-gray-600 hover:bg-white">Back to current</button>
                </div>
              </div>
              <div className="min-h-0 flex-1">
                {previewQ.isLoading || !previewArch
                  ? <div className="flex h-full items-center justify-center text-sm text-gray-400">Loading version…</div>
                  : <ArchitecturePreview key={previewRev.id} arch={previewArch} />}
              </div>
            </>
          )}
          {arch && !previewRev && (
            <ArchitectureCanvas
              key={`${arch.id}:${restoreKey}`}
              arch={arch}
              onSaved={() => { qc.invalidateQueries({ queryKey: ["architectures"] }); qc.invalidateQueries({ queryKey: ["architecture", id] }); }}
            />
          )}
        </div>
        {showHistory && (
          <RevisionsPanel
            architectureId={id}
            previewingId={previewRev?.id ?? null}
            onClose={() => { setShowHistory(false); setPreviewRev(null); }}
            onPreview={(r) => setPreviewRev(r)}
            onExitPreview={() => setPreviewRev(null)}
            onRestored={handleRestored}
          />
        )}
      </div>
      {showActivity && <ActivityModal architectureId={id} onClose={() => setShowActivity(false)} />}
    </div>
  );
}

// ---------------- Revision history (docked side panel, Word-style) ----------------
function RevisionsPanel({ architectureId, previewingId, onClose, onPreview, onExitPreview, onRestored }: {
  architectureId: string;
  previewingId: string | null;
  onClose: () => void;
  onPreview: (rev: ArchitectureRevision) => void;
  onExitPreview: () => void;
  onRestored: () => void;
}) {
  const q = useQuery({ queryKey: ["architectureRevisions", architectureId], queryFn: () => api.architectureRevisions(architectureId), refetchOnMount: "always" });
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const revisions = q.data?.revisions ?? [];

  async function restore(revId: string) {
    if (!window.confirm("Restore this version? The current version is saved to history first, so you won't lose it.")) return;
    setBusy(revId); setError("");
    try { await api.restoreArchitectureRevision(architectureId, revId); onRestored(); }
    catch (e) { setError(formatError(e)); setBusy(""); }
  }

  const REASON_ICON: Record<string, string> = {
    "Created": "✨", "Edited": "✏️", "AI enhanced": "🤖", "Generated by AI": "🤖",
    "Cloned": "📑", "Category changed": "🗂️", "Restored from history": "↩️",
  };
  const reasonIcon = (r: string) => REASON_ICON[r] ?? (r.startsWith("State") ? "🚦" : "•");

  return (
    <aside className="flex w-80 shrink-0 flex-col border-l bg-white">
      <div className="flex items-center justify-between border-b px-3 py-2">
        <h2 className="text-sm font-semibold text-gray-800">🕘 Version history</h2>
        <button onClick={onClose} title="Close" className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700">✕</button>
      </div>
      <p className="border-b px-3 py-2 text-[11px] text-gray-500">Click a version to view it (read-only); restoring snapshots the current version first, so nothing is lost.</p>
      {error && <div className="m-2 rounded-md border border-red-200 bg-red-50 px-2 py-1.5 text-[11px] text-red-700">{error}</div>}

      <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto p-2">
        {q.isLoading && <div className="p-3 text-xs text-gray-400">Loading…</div>}
        {!q.isLoading && revisions.length === 0 && <div className="rounded-lg border border-dashed p-5 text-center text-xs text-gray-400">No revisions yet. Edits will appear here automatically.</div>}
        {revisions.map((r, idx) => {
          const current = idx === 0;
          const viewing = previewingId === r.id;
          return (
            <button key={r.id} type="button"
              onClick={() => (current ? onExitPreview() : onPreview(r))}
              className={`group block w-full rounded-lg border px-2.5 py-2 text-left transition ${viewing ? "border-amber-300 bg-amber-50 ring-1 ring-amber-300" : current ? "border-brand/30 bg-brand/5" : "hover:bg-gray-50"}`}>
              <div className="flex items-center gap-2">
                <span className="text-base">{reasonIcon(r.reason)}</span>
                <span className="min-w-0 flex-1 truncate text-xs font-medium text-gray-700">{r.reason}</span>
                {viewing && <span className="shrink-0 rounded-full bg-amber-100 px-1.5 py-0.5 text-[9px] font-medium text-amber-700">viewing</span>}
                {current && !viewing && <span className="shrink-0 rounded-full bg-green-100 px-1.5 py-0.5 text-[9px] font-medium text-green-700">current</span>}
              </div>
              <div className="mt-1 flex items-center gap-1.5">
                <StateBadge state={r.state} />
                <span className="text-[10px] text-gray-400">{r.node_count} res · {r.edge_count} links</span>
              </div>
              <div className="mt-0.5 text-[10px] text-gray-400">{r.by ? `${r.by} · ` : ""}{r.created_at ? formatTimestamp(r.created_at) : ""}</div>
              {!current && (
                <div className="mt-1.5 flex justify-end gap-1.5">
                  <span className="rounded-md border px-2 py-0.5 text-[11px] text-gray-500 opacity-0 transition group-hover:opacity-100">👁 View</span>
                  <button type="button" onClick={(e) => { e.stopPropagation(); void restore(r.id); }} disabled={!!busy}
                    className="rounded-md border px-2 py-0.5 text-[11px] text-gray-600 hover:bg-white disabled:opacity-50">
                    {busy === r.id ? "Restoring…" : "↩️ Restore"}
                  </button>
                </div>
              )}
            </button>
          );
        })}
      </div>
    </aside>
  );
}

// ---------------- Management activity log (audit trail) ----------------
const ACTIVITY_ICON: Record<string, string> = {
  created: "✨",
  ai_generated: "🤖",
  ai_enhanced: "🤖",
  renamed: "✏️",
  edited: "🔧",
  state_changed: "🚦",
  category_changed: "🗂️",
  cloned: "📑",
  cloned_to: "📋",
  restored: "↩️",
};

function ActivityModal({ architectureId, onClose }: { architectureId: string; onClose: () => void }) {
  const q = useQuery({ queryKey: ["architectureActivity", architectureId], queryFn: () => api.architectureActivity(architectureId), refetchOnMount: "always" });
  const entries = q.data?.activity ?? [];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="flex max-h-[85vh] w-full max-w-lg flex-col rounded-2xl bg-white p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <h2 className="text-lg font-semibold text-gray-800">📋 Activity log</h2>
        <p className="mt-1 text-sm text-gray-500">A full record of management changes to this architecture — status and category changes, edits, AI generation, clones, and restores.</p>

        <div className="mt-3 min-h-0 flex-1 overflow-y-auto">
          {q.isLoading && <div className="text-sm text-gray-400">Loading…</div>}
          {!q.isLoading && entries.length === 0 && <div className="rounded-lg border border-dashed p-6 text-center text-sm text-gray-400">No activity recorded yet.</div>}
          {entries.length > 0 && (
            <ol className="relative space-y-3 border-l border-gray-200 pl-4">
              {entries.map((e) => (
                <li key={e.id} className="relative">
                  <span className="absolute -left-[1.42rem] flex h-6 w-6 items-center justify-center rounded-full bg-white text-sm ring-1 ring-gray-200">{ACTIVITY_ICON[e.event] ?? "•"}</span>
                  <div className="text-sm text-gray-700">{e.detail}</div>
                  <div className="mt-0.5 text-[11px] text-gray-400">{e.by ? `${e.by} · ` : ""}{e.at ? formatTimestamp(e.at) : ""}</div>
                </li>
              ))}
            </ol>
          )}
        </div>

        <div className="mt-3 flex justify-end">
          <button onClick={onClose} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Done</button>
        </div>
      </div>
    </div>
  );
}

// ---------------- Manage categories / solutions ----------------
const ICON_CHOICES = ["📁", "🗂️", "🧩", "🏗️", "☁️", "🛒", "💳", "📊", "🔐", "⚙️", "🌐", "🚀"];

function ManageCategoriesModal({ collections, onClose }: { collections: ArchitectureCollection[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [error, setError] = useState("");
  const [newName, setNewName] = useState("");
  const [newIcon, setNewIcon] = useState("📁");
  const [newColor, setNewColor] = useState("#2563eb");

  function invalidate() {
    qc.invalidateQueries({ queryKey: ["architectureCollections"] });
    qc.invalidateQueries({ queryKey: ["architectures"] });
  }
  async function add() {
    if (!newName.trim()) return;
    try { await api.upsertArchitectureCollection({ name: newName.trim(), icon: newIcon, color: newColor }); setNewName(""); invalidate(); }
    catch (e) { setError(formatError(e)); }
  }
  async function save(c: ArchitectureCollection, patch: Partial<ArchitectureCollection>) {
    try { await api.upsertArchitectureCollection({ id: c.id, name: c.name, icon: c.icon, color: c.color, ...patch }); invalidate(); }
    catch (e) { setError(formatError(e)); }
  }
  async function remove(c: ArchitectureCollection) {
    if (!window.confirm(`Delete category “${c.name}”? Its architectures move to Uncategorized.`)) return;
    try { await api.deleteArchitectureCollection(c.id); invalidate(); }
    catch (e) { setError(formatError(e)); }
  }
  async function move(idx: number, dir: -1 | 1) {
    const next = [...collections];
    const j = idx + dir;
    if (j < 0 || j >= next.length) return;
    [next[idx], next[j]] = [next[j], next[idx]];
    try { await api.reorderArchitectureCollections(next.map((c) => c.id)); invalidate(); }
    catch (e) { setError(formatError(e)); }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="flex max-h-[85vh] w-full max-w-lg flex-col rounded-2xl bg-white p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <h2 className="text-lg font-semibold text-gray-800">🗂️ Categories &amp; solutions</h2>
        <p className="mt-1 text-sm text-gray-500">Group architectures into solutions. Each architecture belongs to one category; deleting a category moves its architectures to Uncategorized.</p>
        {error && <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>}

        <div className="mt-3 min-h-0 flex-1 space-y-2 overflow-y-auto">
          {collections.length === 0 && <p className="text-sm text-gray-400">No categories yet. Create one below.</p>}
          {collections.map((c, idx) => (
            <div key={c.id} className="flex items-center gap-2 rounded-lg border px-2.5 py-2">
              <input type="color" value={c.color} onChange={(e) => void save(c, { color: e.target.value })} className="h-7 w-7 shrink-0 cursor-pointer rounded border p-0.5" title="Color" />
              <select value={c.icon} onChange={(e) => void save(c, { icon: e.target.value })} className="shrink-0 rounded-md border px-1 py-1 text-sm" title="Icon">
                {ICON_CHOICES.map((i) => <option key={i} value={i}>{i}</option>)}
              </select>
              <input defaultValue={c.name} onBlur={(e) => { if (e.target.value.trim() && e.target.value !== c.name) void save(c, { name: e.target.value.trim() }); }}
                className="min-w-0 flex-1 rounded-md border px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-brand" />
              <div className="flex shrink-0 items-center gap-0.5">
                <button onClick={() => void move(idx, -1)} disabled={idx === 0} className="rounded border px-1.5 py-1 text-xs text-gray-500 hover:bg-gray-50 disabled:opacity-30" title="Move up">↑</button>
                <button onClick={() => void move(idx, 1)} disabled={idx === collections.length - 1} className="rounded border px-1.5 py-1 text-xs text-gray-500 hover:bg-gray-50 disabled:opacity-30" title="Move down">↓</button>
                <button onClick={() => void remove(c)} className="rounded border border-red-200 px-1.5 py-1 text-xs text-red-600 hover:bg-red-50" title="Delete">✕</button>
              </div>
            </div>
          ))}
        </div>

        <div className="mt-3 flex items-center gap-2 border-t pt-3">
          <input type="color" value={newColor} onChange={(e) => setNewColor(e.target.value)} className="h-8 w-8 shrink-0 cursor-pointer rounded border p-0.5" title="Color" />
          <select value={newIcon} onChange={(e) => setNewIcon(e.target.value)} className="shrink-0 rounded-md border px-1 py-1.5 text-sm">
            {ICON_CHOICES.map((i) => <option key={i} value={i}>{i}</option>)}
          </select>
          <input value={newName} onChange={(e) => setNewName(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void add(); }}
            placeholder="New category name…" className="min-w-0 flex-1 rounded-md border px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-brand" />
          <button onClick={() => void add()} disabled={!newName.trim()} className="shrink-0 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50">Add</button>
        </div>
        <div className="mt-3 flex justify-end">
          <button onClick={onClose} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Done</button>
        </div>
      </div>
    </div>
  );
}

function RationaleChip({ text, confidence, count }: { text: string; confidence?: number | null; count?: number }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button onClick={() => setOpen((v) => !v)} className="flex items-center gap-1.5 rounded-full bg-violet-50 px-2.5 py-1 text-[11px] text-violet-700 hover:bg-violet-100">
        ✨ AI rationale{count != null ? ` · ${count} resources` : ""}{confidence != null ? ` · ${Math.round(confidence * 100)}%` : ""}
      </button>
      {open && (
        <div className="absolute left-0 top-full z-20 mt-1 w-96 rounded-lg border bg-white p-3 text-xs text-gray-600 shadow-lg">
          {text}
        </div>
      )}
    </div>
  );
}

/** Compact "who created / last modified" indicator with a hover detail popover. */
function AuthorInfo({ arch }: { arch: Architecture }) {
  const [open, setOpen] = useState(false);
  const modifier = arch.updated_by || arch.created_by;
  return (
    <div className="relative ml-auto">
      <button onClick={() => setOpen((v) => !v)} className="flex items-center gap-1 rounded-full border px-2 py-1 text-[11px] text-gray-500 hover:bg-gray-50" title="Author info">
        👤 {modifier}
      </button>
      {open && (
        <div className="absolute right-0 top-full z-20 mt-1 w-64 rounded-lg border bg-white p-3 text-[11px] text-gray-600 shadow-lg">
          <div className="flex justify-between gap-3"><span className="text-gray-400">Created by</span><span className="font-medium text-gray-700">{arch.created_by || "—"}</span></div>
          {arch.created_at && <div className="flex justify-between gap-3"><span className="text-gray-400">Created</span><span>{formatTimestamp(arch.created_at)}</span></div>}
          <div className="mt-1.5 flex justify-between gap-3 border-t pt-1.5"><span className="text-gray-400">Last modified by</span><span className="font-medium text-gray-700">{arch.updated_by || arch.created_by || "—"}</span></div>
          {arch.updated_at && <div className="flex justify-between gap-3"><span className="text-gray-400">Modified</span><span>{formatTimestamp(arch.updated_at)}</span></div>}
        </div>
      )}
    </div>
  );
}

// ---------------- Panel ----------------
export function ArchitecturesPanel() {
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  useEffect(() => { /* re-mount on id change handled by key */ }, [id]);
  // Parse the path directly so /architectures/memory (standalone index) and
  // /architectures/:id/memory (editor) don't collide with the /:id editor route.
  const segs = location.pathname.split("/").filter(Boolean); // ["architectures", ...]
  const second = segs[1];
  const third = segs[2];
  if (second === "memory" && !third) return <MemoryIndex />;
  if (second && third === "memory") return <MemoryEditor architectureId={second} />;
  if (second) return <ArchitectureEditor id={second} />;
  return <ArchitecturesList />;
}
