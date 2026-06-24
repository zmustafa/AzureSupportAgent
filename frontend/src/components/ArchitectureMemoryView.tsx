import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Markdown } from "./LazyMarkdown";
import {
  api,
  streamGenerateMemory,
  type MemorySection,
  type MemorySectionMeta,
  type ArchitectureMemory,
  type MemoryRevision,
} from "../api";

// Format an elapsed-seconds value as m:ss (or s if under a minute) for the live timer.
function fmtElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

// Starter templates: each seeds a tailored ordered set of section keys so manual
// authoring (or a focused AI draft) begins from the right shape for the workload kind.
const MEMORY_TEMPLATES: { id: string; label: string; icon: string; keys: string[] }[] = [
  {
    id: "web",
    label: "Internet-facing web app",
    icon: "🌐",
    keys: ["overview", "pattern", "expected_flow", "components", "network_topology", "security_model", "identity_access", "data_storage", "resiliency_targets", "scaling_performance", "critical_thresholds", "observability", "known_gaps", "diagnostic_hints"],
  },
  {
    id: "event",
    label: "Event-driven / async",
    icon: "⚡",
    keys: ["overview", "pattern", "expected_flow", "components", "dependencies", "data_storage", "scaling_performance", "critical_thresholds", "observability", "runbook", "known_gaps", "diagnostic_hints"],
  },
  {
    id: "data",
    label: "Data platform / analytics",
    icon: "🗄️",
    keys: ["overview", "pattern", "expected_flow", "components", "data_storage", "identity_access", "security_model", "compliance", "scaling_performance", "cost_sizing", "observability", "known_gaps", "diagnostic_hints"],
  },
  {
    id: "full",
    label: "Comprehensive (all sections)",
    icon: "📚",
    keys: [],
  },
];

/** A coverage warning: a section that probably should mention something visible in the
 *  architecture diagram (e.g. a Key Vault node) but currently doesn't. */
interface CoverageWarning {
  sectionKey: string;
  sectionLabel: string;
  message: string;
}

/** Cross-check section content against the architecture's node types and flag likely
 *  gaps. Heuristic and read-only — purely to nudge the author toward completeness. */
function computeCoverageWarnings(
  sections: MemorySection[],
  nodeTypes: string[],
): CoverageWarning[] {
  const byKey = new Map(sections.map((s) => [s.key, (s.content || "").toLowerCase()]));
  const typesBlob = nodeTypes.join(" ").toLowerCase();
  const has = (k: string) => byKey.has(k);
  const mentions = (k: string, ...terms: string[]) => {
    const c = byKey.get(k) || "";
    return terms.some((t) => c.includes(t.toLowerCase()));
  };
  const present = (...terms: string[]) => terms.some((t) => typesBlob.includes(t.toLowerCase()));
  const warns: CoverageWarning[] = [];
  const push = (sectionKey: string, message: string) => {
    const lbl = sections.find((s) => s.key === sectionKey)?.label || sectionKey;
    warns.push({ sectionKey, sectionLabel: lbl, message });
  };

  if (present("keyvault", "key vault") && has("security_model") && !mentions("security_model", "key vault", "keyvault", "secret"))
    push("security_model", "Diagram has a Key Vault but the security model doesn't mention secret management.");
  if (present("privateendpoint", "private endpoint") && has("network_topology") && !mentions("network_topology", "private endpoint", "private link"))
    push("network_topology", "Private endpoints are in the diagram but absent from the network topology section.");
  if (present("frontdoor", "applicationgateway", "application gateway", "/waf") && has("security_model") && !mentions("security_model", "waf", "front door", "application gateway"))
    push("security_model", "An edge/WAF resource is present but not described in the security model.");
  if (present("sql", "cosmosdb", "cosmos", "storageaccount", "storage account", "postgres", "mysql") && has("data_storage") && !mentions("data_storage", "encrypt", "backup", "retention", "store"))
    push("data_storage", "Data stores are in the diagram but encryption/backup details are missing.");
  if (present("managedidentity", "managed identity", "userassignedidentit") && has("identity_access") && !mentions("identity_access", "managed identity", "identity", "rbac"))
    push("identity_access", "Managed identities exist but identity & access doesn't cover them.");
  return warns;
}

// Build the combined "Memory.md" document from the current (unsaved) editor state, so
// the preview pane updates live as the user types — mirrors the backend renderer.
function renderMemoryMarkdown(title: string, workloadName: string, sections: MemorySection[]): string {
  const t = (title || "").trim() || "Architecture Memory";
  const lines: string[] = [`# ${t}`, ""];
  if (workloadName) lines.push(`> **Linked workload:** ${workloadName}`, "");
  for (const s of sections) {
    const content = (s.content || "").trim();
    if (!content) continue;
    lines.push(`## ${s.label || s.key}`, "", content, "");
  }
  return lines.join("\n").trim() + "\n";
}

/** A single editable memory section card (label + auto-growing textarea + controls). */
function SectionCard({
  section,
  hint,
  onChange,
  onRemove,
  onMoveUp,
  onMoveDown,
  isFirst,
  isLast,
  onRegenerate,
  regenerating,
  canRegenerate,
  onToggleReview,
}: {
  section: MemorySection;
  hint?: string;
  onChange: (content: string) => void;
  onRemove: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  isFirst: boolean;
  isLast: boolean;
  onRegenerate: () => void;
  regenerating: boolean;
  canRegenerate: boolean;
  onToggleReview: () => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  // Auto-grow the textarea to fit its content.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.max(64, el.scrollHeight)}px`;
  }, [section.content]);
  const isEmpty = !(section.content || "").trim();
  const needsReview = !!section.needs_review;
  return (
    <div
      className={`rounded-xl border bg-white p-3 shadow-sm ${
        needsReview ? "border-amber-300 ring-1 ring-amber-200" : isEmpty ? "border-dashed border-gray-300" : "border-gray-200"
      }`}
    >
      <div className="mb-1 flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-sm font-semibold text-gray-800">
            {section.label || section.key}
            {isEmpty && <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-gray-400">empty</span>}
            {needsReview && <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-amber-700">needs review</span>}
          </div>
          {hint && <div className="text-[11px] text-gray-400">{hint}</div>}
        </div>
        <div className="flex shrink-0 items-center gap-0.5">
          <button
            onClick={onRegenerate}
            disabled={regenerating || !canRegenerate}
            title={canRegenerate ? "Regenerate just this section with AI" : "Save the memory first to regenerate"}
            className="rounded p-1 text-brand hover:bg-brand/10 disabled:opacity-30"
          >
            {regenerating ? <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-brand border-t-transparent align-middle" /> : "✨"}
          </button>
          <button
            onClick={onToggleReview}
            title={needsReview ? "Clear the “needs review” flag" : "Flag this section for review"}
            className={`rounded p-1 hover:bg-amber-50 ${needsReview ? "text-amber-600" : "text-gray-400 hover:text-amber-600"}`}
          >
            ⚑
          </button>
          <button onClick={onMoveUp} disabled={isFirst} title="Move up" className="rounded p-1 text-gray-400 hover:bg-gray-100 disabled:opacity-30">▲</button>
          <button onClick={onMoveDown} disabled={isLast} title="Move down" className="rounded p-1 text-gray-400 hover:bg-gray-100 disabled:opacity-30">▼</button>
          <button onClick={onRemove} title="Remove section" className="rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-600">✕</button>
        </div>
      </div>
      <textarea
        ref={ref}
        value={section.content}
        onChange={(e) => onChange(e.target.value)}
        placeholder={hint || "Write this section in Markdown…"}
        spellCheck
        className="w-full resize-none rounded-lg border border-gray-200 bg-gray-50/50 px-3 py-2 text-[13px] leading-relaxed text-gray-800 focus:border-brand focus:bg-white focus:outline-none focus:ring-1 focus:ring-brand"
      />
    </div>
  );
}

/** Popover to add a catalog section (grouped) or a custom one. */
function AddSectionMenu({
  catalog,
  presentKeys,
  onAdd,
}: {
  catalog: MemorySectionMeta[];
  presentKeys: Set<string>;
  onAdd: (key: string, label: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [custom, setCustom] = useState("");
  const groups = useMemo(() => {
    const m = new Map<string, MemorySectionMeta[]>();
    for (const s of catalog) {
      if (presentKeys.has(s.key)) continue;
      if (!m.has(s.group)) m.set(s.group, []);
      m.get(s.group)!.push(s);
    }
    return [...m.entries()];
  }, [catalog, presentKeys]);

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full rounded-lg border border-dashed border-gray-300 px-3 py-2 text-sm text-gray-500 transition hover:border-brand hover:text-brand"
      >
        ➕ Add section
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute left-0 z-20 mt-1 max-h-96 w-72 overflow-y-auto rounded-xl border border-gray-200 bg-white p-2 shadow-lg">
            {groups.length === 0 && <div className="px-2 py-2 text-xs text-gray-400">All catalog sections added.</div>}
            {groups.map(([group, items]) => (
              <div key={group} className="mb-1.5">
                <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">{group}</div>
                {items.map((s) => (
                  <button
                    key={s.key}
                    onClick={() => { onAdd(s.key, s.label); setOpen(false); }}
                    title={s.hint}
                    className="block w-full truncate rounded-md px-2 py-1.5 text-left text-[13px] text-gray-700 hover:bg-brand/5 hover:text-brand"
                  >
                    {s.label}
                  </button>
                ))}
              </div>
            ))}
            <div className="mt-1 border-t pt-2">
              <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">Custom section</div>
              <div className="flex gap-1 px-1">
                <input
                  value={custom}
                  onChange={(e) => setCustom(e.target.value)}
                  placeholder="Section title…"
                  className="min-w-0 flex-1 rounded-md border border-gray-200 px-2 py-1 text-[13px] focus:border-brand focus:outline-none"
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && custom.trim()) {
                      const key = `custom_${custom.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "")}`;
                      onAdd(key, custom.trim());
                      setCustom("");
                      setOpen(false);
                    }
                  }}
                />
                <button
                  onClick={() => {
                    if (!custom.trim()) return;
                    const key = `custom_${custom.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "")}`;
                    onAdd(key, custom.trim());
                    setCustom("");
                    setOpen(false);
                  }}
                  className="shrink-0 rounded-md bg-brand px-2 py-1 text-[12px] font-medium text-white hover:bg-brand/90"
                >
                  Add
                </button>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/** Two-pane Architecture Memory editor: section fields (left) + live markdown preview (right). */
export function MemoryEditor({ architectureId }: { architectureId: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const catalogQ = useQuery({ queryKey: ["memoryCatalog"], queryFn: api.memoryCatalog });
  const memQ = useQuery({ queryKey: ["architectureMemory", architectureId], queryFn: () => api.architectureMemory(architectureId) });
  // Full architecture (nodes/edges) — used for coverage warnings cross-checks.
  const archQ = useQuery({ queryKey: ["architecture", architectureId], queryFn: () => api.architecture(architectureId) });

  const [title, setTitle] = useState("");
  const [sections, setSections] = useState<MemorySection[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [archName, setArchName] = useState("");
  const [workloadName, setWorkloadName] = useState("");
  const [workloadId, setWorkloadId] = useState("");
  const [archUpdatedAt, setArchUpdatedAt] = useState("");
  const [aiInfo, setAiInfo] = useState<ArchitectureMemory["ai"] | undefined>(undefined);
  const [loaded, setLoaded] = useState(false);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [dirty, setDirty] = useState(false);
  const [genState, setGenState] = useState<"idle" | "running">("idle");
  const [genMsg, setGenMsg] = useState("");
  // Live progress: a timer + the ordered status steps received from the server.
  const [genSteps, setGenSteps] = useState<{ phase: string; message: string; at: number }[]>([]);
  const [genElapsed, setGenElapsed] = useState(0);
  const genStartRef = useRef<number>(0);
  const genTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const genAbort = useRef<AbortController | null>(null);
  // Auto-scroll the live progress list to the newest step as it streams in.
  const genStepsRef = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const el = genStepsRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [genSteps.length]);
  // Per-section AI regenerate (which section key is currently regenerating).
  const [regenKey, setRegenKey] = useState<string>("");
  // Import grounding notes (folded into the AI draft as authoritative context).
  const [showImport, setShowImport] = useState(false);
  const [extraContext, setExtraContext] = useState("");
  // Show only sections flagged "needs review".
  const [reviewOnly, setReviewOnly] = useState(false);
  // Raw single-document Markdown edit mode (vs. the per-section card editor).
  const [rawMode, setRawMode] = useState(false);
  const [rawText, setRawText] = useState("");
  // Templates popover.
  const [showTemplates, setShowTemplates] = useState(false);
  // Revision history: a side panel listing snapshots + a read-only preview of one.
  const [showHistory, setShowHistory] = useState(false);
  const [previewRev, setPreviewRev] = useState<MemoryRevision | null>(null);
  // Show a diff (vs. the live memory) while previewing a revision.
  const [showDiff, setShowDiff] = useState(false);
  const memoryExists = !!memQ.data?.memory;
  const revQ = useQuery({
    queryKey: ["memoryRevisions", architectureId],
    queryFn: () => api.memoryRevisions(architectureId),
    enabled: showHistory && memoryExists,
  });
  const previewQ = useQuery({
    queryKey: ["memoryRevision", architectureId, previewRev?.id],
    queryFn: () => api.memoryRevision(architectureId, previewRev!.id),
    enabled: !!previewRev,
  });

  // Initialize editor state once the memory (or empty default) loads.
  useEffect(() => {
    if (!memQ.data || loaded) return;
    const m = memQ.data.memory;
    const arch = memQ.data.architecture;
    setArchName(arch.name);
    setWorkloadName(arch.workload_name);
    setWorkloadId(arch.workload_id);
    setArchUpdatedAt(arch.updated_at || "");
    if (m) {
      setTitle(m.title || "");
      setSections(m.sections || []);
      setEnabled(m.enabled_for_investigations);
      setAiInfo(m.ai);
    } else {
      // No memory yet — seed from the catalog defaults so the user can start typing.
      const defaults = catalogQ.data;
      if (defaults) {
        const byKey = new Map(defaults.sections.map((s) => [s.key, s]));
        setSections(defaults.default_keys.map((k) => ({ key: k, label: byKey.get(k)?.label || k, content: "" })));
      }
      setTitle(arch.name ? `${arch.name} — Memory` : "Architecture Memory");
    }
    setLoaded(true);
  }, [memQ.data, catalogQ.data, loaded]);

  const hintByKey = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of catalogQ.data?.sections || []) m.set(s.key, s.hint);
    return m;
  }, [catalogQ.data]);

  const markdown = useMemo(() => renderMemoryMarkdown(title, workloadName, sections), [title, workloadName, sections]);

  // Completeness: how many sections have content.
  const filledCount = useMemo(() => sections.filter((s) => (s.content || "").trim()).length, [sections]);
  const reviewCount = useMemo(() => sections.filter((s) => s.needs_review).length, [sections]);
  const completeness = sections.length ? Math.round((filledCount / sections.length) * 100) : 0;

  // Staleness: the memory was AI-generated before the architecture diagram last changed.
  const staleness = useMemo(() => {
    const gen = aiInfo?.generated_at;
    if (!gen || !archUpdatedAt) return null;
    const g = Date.parse(gen);
    const u = Date.parse(archUpdatedAt);
    if (!Number.isFinite(g) || !Number.isFinite(u) || u <= g) return null;
    const days = Math.floor((u - g) / 86_400_000);
    return { generatedAt: gen, days };
  }, [aiInfo?.generated_at, archUpdatedAt]);

  // Coverage warnings: section content vs. architecture node types.
  const nodeTypes = useMemo(
    () => (archQ.data?.architecture?.nodes || []).map((n) => `${n.type || ""} ${n.name || ""}`),
    [archQ.data],
  );
  const coverageWarnings = useMemo(
    () => (sections.length && nodeTypes.length ? computeCoverageWarnings(sections, nodeTypes) : []),
    [sections, nodeTypes],
  );

  // Diff (previewed revision vs. current live sections), section by section.
  const diffRows = useMemo(() => {
    if (!showDiff || !previewQ.data?.revision) return [];
    const cur = new Map(sections.map((s) => [s.key, (s.content || "").trim()]));
    const old = new Map((previewQ.data.revision.sections || []).map((s) => [s.key, (s.content || "").trim()]));
    const keys = Array.from(new Set([...cur.keys(), ...old.keys()]));
    return keys.map((k) => {
      const label = sections.find((s) => s.key === k)?.label
        || (previewQ.data!.revision.sections || []).find((s) => s.key === k)?.label || k;
      const c = cur.get(k) ?? "";
      const o = old.get(k) ?? "";
      let status: "same" | "changed" | "added" | "removed" = "same";
      if (!old.has(k)) status = "added";
      else if (!cur.has(k)) status = "removed";
      else if (c !== o) status = "changed";
      return { key: k, label, status, current: c, old: o };
    });
  }, [showDiff, previewQ.data, sections]);

  // Parse a raw single-document Markdown edit back into sections (reverse of the
  // renderer): each `## Heading` becomes/updates a section (matched by label, else new).
  function applyRawText(text: string) {
    const blocks = text.split(/\n(?=## )/g);
    const labelToKey = new Map(sections.map((s) => [(s.label || s.key).toLowerCase(), s.key]));
    const newSections: MemorySection[] = [];
    for (const block of blocks) {
      const m = block.match(/^##\s+(.+?)\n([\s\S]*)$/);
      if (!m) continue;
      const label = m[1].trim();
      const content = m[2].trim();
      const key = labelToKey.get(label.toLowerCase())
        || `custom_${label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "")}`;
      if (newSections.some((s) => s.key === key)) continue;
      newSections.push({ key, label, content });
    }
    if (newSections.length) update({ sections: newSections });
  }

  function toggleRawMode() {
    if (!rawMode) {
      // entering raw mode: seed from the current section-derived document (sans title/quote)
      setRawText(sections.map((s) => `## ${s.label || s.key}\n\n${s.content || ""}`).join("\n\n").trim());
    } else {
      applyRawText(rawText);
    }
    setRawMode((v) => !v);
  }

  // Debounced autosave whenever the editable state changes (after initial load).
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const save = useCallback(async (next: { title: string; sections: MemorySection[]; enabled: boolean }) => {
    setSaveState("saving");
    try {
      await api.upsertArchitectureMemory(architectureId, {
        title: next.title,
        sections: next.sections,
        enabled_for_investigations: next.enabled,
      });
      setSaveState("saved");
      setDirty(false);
      // A save may have created a new revision — refresh the history list if open.
      qc.invalidateQueries({ queryKey: ["memoryRevisions", architectureId] });
    } catch {
      setSaveState("error");
    }
  }, [architectureId, qc]);

  const scheduleSave = useCallback((next: { title: string; sections: MemorySection[]; enabled: boolean }) => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => void save(next), 800);
  }, [save]);

  function update(next: Partial<{ title: string; sections: MemorySection[]; enabled: boolean }>) {
    const nt = next.title ?? title;
    const ns = next.sections ?? sections;
    const ne = next.enabled ?? enabled;
    if (next.title !== undefined) setTitle(next.title);
    if (next.sections !== undefined) setSections(next.sections);
    if (next.enabled !== undefined) setEnabled(next.enabled);
    if (loaded) { setDirty(true); scheduleSave({ title: nt, sections: ns, enabled: ne }); }
  }

  function setSectionContent(idx: number, content: string) {
    update({ sections: sections.map((s, i) => (i === idx ? { ...s, content } : s)) });
  }
  function removeSection(idx: number) {
    update({ sections: sections.filter((_, i) => i !== idx) });
  }
  function moveSection(idx: number, dir: -1 | 1) {
    const j = idx + dir;
    if (j < 0 || j >= sections.length) return;
    const copy = [...sections];
    [copy[idx], copy[j]] = [copy[j], copy[idx]];
    update({ sections: copy });
  }
  function addSection(key: string, label: string) {
    if (sections.some((s) => s.key === key)) return;
    update({ sections: [...sections, { key, label, content: "" }] });
  }

  async function generate() {
    if (genState === "running") return;
    setGenState("running");
    setShowImport(false);
    setGenMsg("Starting…");
    setGenSteps([{ phase: "start", message: "Starting…", at: 0 }]);
    genStartRef.current = Date.now();
    setGenElapsed(0);
    if (genTimerRef.current) clearInterval(genTimerRef.current);
    genTimerRef.current = setInterval(() => {
      setGenElapsed((Date.now() - genStartRef.current) / 1000);
    }, 250);
    genAbort.current = new AbortController();
    await streamGenerateMemory(architectureId, {
      onStatus: (s) => {
        setGenMsg(s.message);
        setGenSteps((prev) => [
          ...prev,
          { phase: s.phase, message: s.message, at: (Date.now() - genStartRef.current) / 1000 },
        ]);
      },
      onDone: (r) => {
        if (r.memory) {
          setTitle(r.memory.title || title);
          setSections(r.memory.sections);
          setEnabled(r.memory.enabled_for_investigations);
          setAiInfo(r.memory.ai);
        }
        stopGenTimer();
        setGenState("idle");
        setGenMsg("");
        setGenSteps([]);
        setSaveState("saved");
        setDirty(false);
        qc.invalidateQueries({ queryKey: ["memoryRevisions", architectureId] });
      },
      onError: (msg) => { stopGenTimer(); setGenState("idle"); setGenMsg(`Error: ${msg}`); },
    }, genAbort.current.signal, extraContext);
  }

  // Regenerate a single section with AI, leaving the rest untouched.
  async function regenerateSection(key: string) {
    if (regenKey) return;
    setRegenKey(key);
    try {
      const r = await api.regenerateMemorySection(architectureId, key, extraContext);
      if (r.memory) {
        const fresh = r.memory.sections.find((s) => s.key === key);
        if (fresh) {
          setSections((prev) => prev.map((s) => (s.key === key ? { ...s, content: fresh.content, needs_review: false } : s)));
        }
        setAiInfo(r.memory.ai);
        setSaveState("saved");
        setDirty(false);
        qc.invalidateQueries({ queryKey: ["memoryRevisions", architectureId] });
      }
    } catch {
      setSaveState("error");
    } finally {
      setRegenKey("");
    }
  }

  function toggleReview(idx: number) {
    update({ sections: sections.map((s, i) => (i === idx ? { ...s, needs_review: !s.needs_review } : s)) });
  }

  // Apply a starter template: add any of the template's sections that aren't present yet
  // (preserving existing content), in the template's order for new ones.
  function applyTemplate(tmpl: typeof MEMORY_TEMPLATES[number]) {
    setShowTemplates(false);
    const keys = tmpl.keys.length ? tmpl.keys : (catalogQ.data?.sections || []).map((s) => s.key);
    const byKey = new Map((catalogQ.data?.sections || []).map((s) => [s.key, s]));
    const present = new Set(sections.map((s) => s.key));
    const additions = keys
      .filter((k) => !present.has(k))
      .map((k) => ({ key: k, label: byKey.get(k)?.label || k, content: "" }));
    if (additions.length === 0) return;
    update({ sections: [...sections, ...additions] });
  }

  // Open a deep-investigation chat scoped to this workload + this architecture's memory.
  function investigate() {
    try {
      sessionStorage.setItem(
        "azsup.memoryHandoff",
        JSON.stringify({ workloadId, memoryArchId: architectureId }),
      );
    } catch { /* ignore */ }
    navigate("/chat");
  }

  // Print / save-as-PDF the rendered memory document.
  function printDoc() {
    const w = window.open("", "_blank", "width=860,height=1000");
    if (!w) return;
    const esc = (s: string) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c] || c));
    w.document.write(
      `<!doctype html><html><head><title>${esc(title || archName || "Architecture Memory")}</title>` +
      `<style>body{font:14px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;max-width:760px;margin:40px auto;padding:0 24px;color:#1f2937}h1{font-size:24px}h2{font-size:17px;margin-top:1.6em;border-bottom:1px solid #e5e7eb;padding-bottom:4px}code{background:#f3f4f6;padding:1px 4px;border-radius:4px}blockquote{color:#6b7280;border-left:3px solid #e5e7eb;margin:0;padding-left:12px}</style>` +
      `</head><body><pre style="white-space:pre-wrap;font:inherit">${esc(markdown)}</pre></body></html>`,
    );
    w.document.close();
    w.focus();
    setTimeout(() => { w.print(); }, 250);
  }

  function stopGenTimer() {
    if (genTimerRef.current) {
      clearInterval(genTimerRef.current);
      genTimerRef.current = null;
    }
  }

  function cancelGenerate() {
    genAbort.current?.abort();
    stopGenTimer();
    setGenState("idle");
    setGenMsg("");
    setGenSteps([]);
  }

  // Stop the ticking timer if the component unmounts mid-generation.
  useEffect(() => () => stopGenTimer(), []);

  function copyMarkdown() {
    void navigator.clipboard.writeText(markdown).catch(() => {});
  }
  function downloadMarkdown() {
    const blob = new Blob([markdown], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${(archName || "architecture").replace(/[^a-z0-9]+/gi, "-").toLowerCase()}-memory.md`;
    a.click();
    URL.revokeObjectURL(url);
  }
  async function deleteMemory() {
    if (!window.confirm("Delete this memory? This cannot be undone.")) return;
    try { await api.deleteArchitectureMemory(architectureId); } catch { /* ignore */ }
    navigate(`/architectures/${architectureId}`);
  }

  // Restore the currently-previewed revision onto the live memory, then reload it into
  // the editor. The current version is snapshotted first (server-side), so nothing is lost.
  async function restorePreviewed() {
    if (!previewRev) return;
    if (!window.confirm("Restore this version? The current version is saved to history first, so you won't lose it.")) return;
    try {
      const r = await api.restoreMemoryRevision(architectureId, previewRev.id);
      if (r.memory) {
        setTitle(r.memory.title || "");
        setSections(r.memory.sections);
        setEnabled(r.memory.enabled_for_investigations);
        setAiInfo(r.memory.ai);
      }
      setPreviewRev(null);
      setSaveState("saved");
      qc.invalidateQueries({ queryKey: ["architectureMemory", architectureId] });
      qc.invalidateQueries({ queryKey: ["memoryRevisions", architectureId] });
      qc.invalidateQueries({ queryKey: ["architectureMemories"] });
    } catch { /* ignore */ }
  }

  useEffect(() => () => { genAbort.current?.abort(); }, []);

  const presentKeys = useMemo(() => new Set(sections.map((s) => s.key)), [sections]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-white">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <button onClick={() => navigate(`/architectures/${architectureId}`)} className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50">← Architecture</button>
        <span className="text-gray-300">/</span>
        <span className="text-sm font-semibold text-gray-700">🧠 Memory</span>
        <input
          value={title}
          onChange={(e) => update({ title: e.target.value })}
          placeholder="Memory title…"
          className="min-w-[12rem] flex-1 rounded-md border border-transparent px-2 py-1 text-sm text-gray-800 hover:border-gray-200 focus:border-brand focus:outline-none"
        />
        <button
          onClick={() => navigate("/knowme")}
          title="Open Workload Know-Me (support-facing references transformed from memories)"
          className="rounded-lg border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-xs font-medium text-indigo-700 hover:bg-indigo-100"
        >
          📄 Know-Me
        </button>
        <button
          onClick={() => void generate()}
          disabled={genState === "running"}
          title="Draft the memory from the architecture + live resources + known weaknesses"
          className="flex items-center gap-1 rounded-lg border border-brand/30 bg-brand/5 px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/10 disabled:opacity-50"
        >
          {genState === "running" ? <><span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-brand border-t-transparent" /> Generating… <span className="font-mono tabular-nums">{fmtElapsed(genElapsed)}</span></> : "✨ Generate with AI"}
        </button>
        <button
          onClick={() => setShowImport((v) => !v)}
          title="Add operator notes (runbook, RCA) to ground the AI draft"
          className={`rounded-lg border px-2 py-1 text-xs ${showImport || extraContext ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}
        >
          📎 Notes{extraContext ? " •" : ""}
        </button>
        <div className="relative">
          <button onClick={() => setShowTemplates((v) => !v)} title="Seed sections from a template" className="rounded-lg border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50">🧱 Template</button>
          {showTemplates && (
            <>
              <div className="fixed inset-0 z-10" onClick={() => setShowTemplates(false)} />
              <div className="absolute left-0 z-20 mt-1 w-60 rounded-xl border border-gray-200 bg-white p-1.5 shadow-lg">
                {MEMORY_TEMPLATES.map((t) => (
                  <button key={t.id} onClick={() => applyTemplate(t)} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] text-gray-700 hover:bg-brand/5 hover:text-brand">
                    <span>{t.icon}</span>{t.label}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
        <button onClick={investigate} disabled={!workloadId} title={workloadId ? "Start a deep investigation grounded in this memory" : "Link a workload to investigate"} className="rounded-lg border border-violet-300 bg-violet-50 px-2 py-1 text-xs font-medium text-violet-700 hover:bg-violet-100 disabled:opacity-40">🔎 Investigate</button>
        <label className="flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs text-gray-600" title="Inject this memory into deep investigations on the linked workload">
          <input type="checkbox" checked={enabled} onChange={(e) => update({ enabled: e.target.checked })} className="accent-brand" />
          Use in investigations
        </label>
        <button
          onClick={() => void save({ title, sections, enabled })}
          disabled={!dirty || saveState === "saving"}
          title="Save now"
          className={`rounded-lg border px-2 py-1 text-xs ${dirty ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-400"} disabled:opacity-50`}
        >
          {saveState === "saving" ? "Saving…" : dirty ? "● Save" : "Saved"}
        </button>
        <div className="ml-auto flex items-center gap-1">
          <button onClick={toggleRawMode} title="Toggle raw Markdown editing" className={`rounded-lg border px-2 py-1 text-xs ${rawMode ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"}`}>{rawMode ? "▦ Sections" : "</> Raw"}</button>
          <button
            onClick={() => { setShowHistory((v) => { if (v) { setPreviewRev(null); setShowDiff(false); } return !v; }); }}
            disabled={!memoryExists}
            title="Version history"
            className={`rounded-lg border px-2 py-1 text-xs ${showHistory ? "border-brand/40 bg-brand/5 text-brand" : "text-gray-600 hover:bg-gray-50"} disabled:opacity-40`}
          >
            🕘 History
          </button>
          <button onClick={copyMarkdown} title="Copy as Markdown" className="rounded-lg border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50">Copy</button>
          <button onClick={printDoc} title="Print / save as PDF" className="rounded-lg border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50">🖨️ PDF</button>
          <button onClick={downloadMarkdown} title="Download .md" className="rounded-lg border px-2 py-1 text-xs text-gray-600 hover:bg-gray-50">⬇︎ .md</button>
          <button onClick={() => void deleteMemory()} title="Delete memory" className="rounded-lg border px-2 py-1 text-xs text-red-600 hover:bg-red-50">Delete</button>
        </div>
      </div>

      {/* Read-only revision preview banner (Word-style) */}
      {previewRev && (
        <div className="flex flex-wrap items-center gap-2 border-b border-amber-200 bg-amber-50 px-3 py-1.5 text-xs text-amber-800">
          <span>👁 Viewing version{previewRev.created_at ? ` from ${new Date(previewRev.created_at).toLocaleString()}` : ""} ({previewRev.reason}) — read-only</span>
          <div className="ml-auto flex gap-1.5">
            <button onClick={() => setShowDiff((v) => !v)} className={`rounded-md border px-2.5 py-1 text-[11px] font-medium ${showDiff ? "border-brand/40 bg-brand/5 text-brand" : "border-amber-300 bg-white text-amber-800 hover:bg-amber-100"}`}>⇄ {showDiff ? "Hide diff" : "Diff vs current"}</button>
            <button onClick={() => void restorePreviewed()} className="rounded-md border border-amber-300 bg-white px-2.5 py-1 text-[11px] font-medium text-amber-800 hover:bg-amber-100">↩️ Restore this version</button>
            <button onClick={() => { setPreviewRev(null); setShowDiff(false); }} className="rounded-md border px-2.5 py-1 text-[11px] text-gray-600 hover:bg-white">Back to current</button>
          </div>
        </div>
      )}

      {genState === "running" ? (
        <div className="border-b border-brand/20 bg-brand/5 px-3 py-2 text-[12px] text-brand">
          <div className="flex items-center gap-2">
            <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-brand border-t-transparent" />
            <span className="font-medium">Generating memory…</span>
            <span className="font-mono tabular-nums text-brand/80">{fmtElapsed(genElapsed)}</span>
            <span className="text-[11px] text-brand/60">large drafts can take a few minutes</span>
            <button
              onClick={cancelGenerate}
              className="ml-auto rounded-md border border-brand/30 bg-white px-2 py-0.5 text-[11px] font-medium text-brand hover:bg-brand/10"
            >
              Cancel
            </button>
          </div>
          {genSteps.length > 0 && (
            <ol ref={genStepsRef} className="mt-1.5 max-h-44 space-y-0.5 overflow-y-auto border-t border-brand/10 pt-1.5">
              {genSteps.map((s, i) => {
                const isLast = i === genSteps.length - 1;
                return (
                  <li key={i} className="flex items-center gap-2 text-[11px]">
                    {isLast ? (
                      <span className="inline-block h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-2 border-brand border-t-transparent" />
                    ) : (
                      <span className="shrink-0 text-emerald-600">✓</span>
                    )}
                    <span className={isLast ? "text-brand" : "text-gray-500"}>{s.message}</span>
                    <span className="ml-auto font-mono tabular-nums text-gray-400">{fmtElapsed(s.at)}</span>
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      ) : genMsg ? (
        <div className="border-b border-red-200 bg-red-50 px-3 py-1.5 text-[12px] text-red-700">{genMsg}</div>
      ) : null}
      {!workloadId && (
        <div className="border-b border-amber-200 bg-amber-50 px-3 py-1.5 text-[12px] text-amber-700">
          Not linked to a workload — AI generation drafts from the diagram only. Link a workload on the architecture to ground it in live resources.
        </div>
      )}

      {/* Import grounding notes */}
      {showImport && (
        <div className="border-b border-brand/20 bg-brand/[0.03] px-3 py-2">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">📎 Grounding notes (folded into AI drafts)</span>
            <button onClick={() => setExtraContext("")} className="text-[11px] text-gray-400 hover:text-gray-600">Clear</button>
          </div>
          <textarea
            value={extraContext}
            onChange={(e) => setExtraContext(e.target.value)}
            placeholder="Paste a runbook, incident RCA, design doc, or operator notes here — the AI treats these as authoritative when drafting or regenerating sections."
            rows={4}
            className="w-full resize-y rounded-lg border border-gray-200 bg-white px-3 py-2 text-[12px] text-gray-800 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
          />
        </div>
      )}

      {/* Completeness meter + review filter */}
      {!previewRev && sections.length > 0 && (
        <div className="flex flex-wrap items-center gap-3 border-b bg-white px-3 py-1.5">
          <span className="text-[11px] font-medium text-gray-500">Completeness</span>
          <div className="h-2 w-40 overflow-hidden rounded-full bg-gray-100">
            <div className={`h-full ${completeness >= 80 ? "bg-emerald-500" : completeness >= 40 ? "bg-amber-400" : "bg-red-400"}`} style={{ width: `${completeness}%` }} />
          </div>
          <span className="text-[11px] tabular-nums text-gray-500">{filledCount}/{sections.length} filled · {completeness}%</span>
          {reviewCount > 0 && (
            <button
              onClick={() => setReviewOnly((v) => !v)}
              className={`ml-auto rounded-full border px-2 py-0.5 text-[11px] font-medium ${reviewOnly ? "border-amber-400 bg-amber-100 text-amber-800" : "border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100"}`}
            >
              ⚑ {reviewCount} needs review{reviewOnly ? " · showing" : ""}
            </button>
          )}
        </div>
      )}

      {/* Staleness banner */}
      {!previewRev && staleness && (
        <div className="flex flex-wrap items-center gap-2 border-b border-amber-200 bg-amber-50 px-3 py-1.5 text-[12px] text-amber-800">
          <span>⏳ This memory was AI-generated {staleness.days > 0 ? `${staleness.days} day(s) ` : ""}before the architecture last changed — it may be stale.</span>
          <button onClick={() => void generate()} disabled={genState === "running"} className="ml-auto rounded-md border border-amber-300 bg-white px-2 py-0.5 text-[11px] font-medium text-amber-800 hover:bg-amber-100 disabled:opacity-50">Refresh with AI</button>
        </div>
      )}

      {/* Coverage warnings */}
      {!previewRev && coverageWarnings.length > 0 && (
        <div className="border-b border-sky-200 bg-sky-50 px-3 py-1.5 text-[12px] text-sky-800">
          <div className="mb-0.5 font-medium">🧭 {coverageWarnings.length} coverage suggestion{coverageWarnings.length === 1 ? "" : "s"} from the diagram</div>
          <ul className="space-y-0.5">
            {coverageWarnings.map((w, i) => (
              <li key={i} className="flex items-start gap-1.5">
                <span className="mt-0.5 shrink-0 text-sky-400">•</span>
                <span><span className="font-medium">{w.sectionLabel}:</span> {w.message}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Two-pane body */}
      <div className="flex min-h-0 flex-1">
        {/* Left: editable sections (dimmed + locked while previewing an old version) */}
        <div className={`min-h-0 w-1/2 overflow-y-auto border-r p-3 ${previewRev ? "pointer-events-none opacity-40" : ""}`}>
          {rawMode ? (
            <div className="flex h-full flex-col">
              <div className="mb-1.5 text-[11px] text-gray-400">Editing the whole document as Markdown — <code>## Heading</code> maps to a section. Switch back to “Sections” to apply.</div>
              <textarea
                value={rawText}
                onChange={(e) => setRawText(e.target.value)}
                spellCheck
                className="min-h-0 flex-1 resize-none rounded-lg border border-gray-200 bg-gray-50/50 px-3 py-2 font-mono text-[12px] leading-relaxed text-gray-800 focus:border-brand focus:bg-white focus:outline-none focus:ring-1 focus:ring-brand"
              />
            </div>
          ) : (
            <div className="space-y-2.5">
              {sections.filter((s) => !reviewOnly || s.needs_review).length === 0 && reviewOnly && (
                <div className="rounded-lg border border-dashed p-4 text-center text-xs text-gray-400">No sections flagged for review.</div>
              )}
              {sections.map((s, i) => ({ s, i })).filter(({ s }) => !reviewOnly || s.needs_review).map(({ s, i }) => (
                <SectionCard
                  key={s.key}
                  section={s}
                  hint={hintByKey.get(s.key)}
                  onChange={(c) => setSectionContent(i, c)}
                  onRemove={() => removeSection(i)}
                  onMoveUp={() => moveSection(i, -1)}
                  onMoveDown={() => moveSection(i, 1)}
                  isFirst={i === 0}
                  isLast={i === sections.length - 1}
                  onRegenerate={() => void regenerateSection(s.key)}
                  regenerating={regenKey === s.key}
                  canRegenerate={memoryExists && !regenKey}
                  onToggleReview={() => toggleReview(i)}
                />
              ))}
              {!reviewOnly && <AddSectionMenu catalog={catalogQ.data?.sections || []} presentKeys={presentKeys} onAdd={addSection} />}
            </div>
          )}
        </div>

        {/* Right: history list, a previewed revision, or the live markdown preview */}
        {showHistory && !previewRev ? (
          <div className="min-h-0 w-1/2 overflow-y-auto bg-gray-50/40 p-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">Version history</span>
              <button onClick={() => setShowHistory(false)} className="text-[11px] text-gray-400 hover:text-gray-600">Close</button>
            </div>
            {revQ.isLoading && <div className="py-4 text-center text-xs text-gray-400">Loading…</div>}
            {!revQ.isLoading && (revQ.data?.revisions.length ?? 0) === 0 && (
              <div className="rounded-lg border bg-white p-4 text-center text-xs text-gray-400">No revisions yet.</div>
            )}
            <div className="space-y-1.5">
              {revQ.data?.revisions.map((r, i) => (
                <button
                  key={r.id}
                  onClick={() => setPreviewRev(r)}
                  className="flex w-full items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-2 text-left transition hover:border-brand/30 hover:bg-brand/[0.03]"
                >
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="truncate text-[13px] font-medium text-gray-800">{r.reason || "Edited"}</span>
                      {i === 0 && <span className="shrink-0 rounded-full bg-green-100 px-1.5 py-0.5 text-[9px] font-medium text-green-700">current</span>}
                    </span>
                    <span className="mt-0.5 block truncate text-[11px] text-gray-400">
                      {r.created_at ? new Date(r.created_at).toLocaleString() : ""}{r.by ? ` · ${r.by}` : ""} · {r.filled_count}/{r.section_count} filled
                    </span>
                  </span>
                  <span className="shrink-0 text-[10px] text-gray-400">View →</span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="min-h-0 w-1/2 overflow-y-auto bg-gray-50/40 p-4">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
                {previewRev ? (showDiff ? "Diff vs current" : "Version preview") : "Preview"}
              </span>
              {!previewRev && aiInfo?.confidence != null && (
                <span className="text-[10px] text-gray-400">AI confidence {Math.round((aiInfo.confidence || 0) * 100)}%</span>
              )}
            </div>
            {previewRev && showDiff ? (
              <div className="space-y-2">
                {diffRows.filter((d) => d.status !== "same").length === 0 && (
                  <div className="rounded-lg border bg-white p-4 text-center text-xs text-gray-400">No differences between this version and the current memory.</div>
                )}
                {diffRows.filter((d) => d.status !== "same").map((d) => (
                  <div key={d.key} className="overflow-hidden rounded-lg border border-gray-200 bg-white">
                    <div className="flex items-center gap-2 border-b bg-gray-50 px-3 py-1.5 text-[12px] font-medium text-gray-700">
                      {d.label}
                      <span className={`rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${
                        d.status === "added" ? "bg-emerald-100 text-emerald-700" : d.status === "removed" ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700"
                      }`}>{d.status === "added" ? "in current only" : d.status === "removed" ? "in this version only" : "changed"}</span>
                    </div>
                    <div className="grid grid-cols-2 divide-x text-[11px]">
                      <div className="bg-red-50/40 p-2">
                        <div className="mb-1 text-[9px] font-semibold uppercase tracking-wide text-red-400">This version</div>
                        <pre className="whitespace-pre-wrap font-sans text-gray-700">{d.old || "—"}</pre>
                      </div>
                      <div className="bg-emerald-50/40 p-2">
                        <div className="mb-1 text-[9px] font-semibold uppercase tracking-wide text-emerald-500">Current</div>
                        <pre className="whitespace-pre-wrap font-sans text-gray-700">{d.current || "—"}</pre>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="prose prose-sm max-w-none rounded-xl border border-gray-200 bg-white p-5 [&_h1]:text-2xl [&_h1]:font-bold [&_h2]:mb-1 [&_h2]:mt-5 [&_h2]:text-lg [&_h2]:font-bold [&_h2]:text-gray-900 [&_h3]:mt-3 [&_h3]:text-base [&_h3]:font-semibold [&_h3]:text-gray-800">
                <Markdown>
                  {previewRev ? (previewQ.data?.markdown ?? "Loading…") : markdown}
                </Markdown>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** Standalone index of all architecture memories (managed separately, linked back). */
export function MemoryIndex() {
  const navigate = useNavigate();
  const q = useQuery({ queryKey: ["architectureMemories"], queryFn: api.architectureMemories });
  const memories = q.data?.memories ?? [];
  const sourceBadge = (s: string) =>
    s === "ai" ? "bg-violet-100 text-violet-700" : s === "hybrid" ? "bg-sky-100 text-sky-700" : "bg-gray-100 text-gray-600";

  return (
    <div className="h-full overflow-y-auto bg-gray-50/40">
      <div className="space-y-4 p-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-bold text-gray-900">🧠 Architecture Memory</h1>
            <p className="mt-0.5 text-sm text-gray-500">
              Knowledge bases that inform deep investigations. Each is owned by an architecture.
            </p>
          </div>
          <button onClick={() => navigate("/architectures")} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-white">← Architectures</button>
        </div>

        {q.isLoading && <div className="py-10 text-center text-sm text-gray-400">Loading…</div>}
        {!q.isLoading && memories.length === 0 && (
          <div className="rounded-xl border bg-white p-8 text-center">
            <div className="text-sm font-medium text-gray-700">No memories yet</div>
            <p className="mt-1 text-xs text-gray-500">
              Open an architecture and click <b>🧠 Memory</b> to create one (or generate it with AI).
            </p>
          </div>
        )}

        <div className="space-y-2">
          {memories.map((m) => (
            <button
              key={m.id}
              onClick={() => navigate(`/architectures/${m.architecture_id}/memory`)}
              className="flex w-full items-center gap-3 rounded-xl border border-gray-200 bg-white px-4 py-3 text-left transition hover:border-brand/30 hover:shadow-sm"
            >
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2">
                  <span className="truncate text-sm font-semibold text-gray-800">{m.title || m.architecture_name}</span>
                  <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${sourceBadge(m.source)}`}>{m.source}</span>
                  {!m.enabled_for_investigations && <span className="shrink-0 rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">not in investigations</span>}
                  {!m.architecture_exists && <span className="shrink-0 rounded-full bg-red-100 px-1.5 py-0.5 text-[10px] text-red-600">orphaned</span>}
                </span>
                <span className="mt-0.5 block truncate text-[12px] text-gray-500">
                  {m.architecture_name}{m.workload_name ? ` · 🧩 ${m.workload_name}` : ""}
                </span>
              </span>
              <span className="shrink-0 text-right">
                <span className="block text-[12px] font-medium text-gray-600">{m.filled_count}/{m.section_count} sections</span>
                <span className="block text-[10px] text-gray-400">updated {m.updated_at ? new Date(m.updated_at).toLocaleDateString() : "—"}</span>
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
