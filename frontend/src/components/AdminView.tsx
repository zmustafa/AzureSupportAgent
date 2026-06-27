import { useEffect, useRef, useState, lazy, Suspense } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import JSZip from "jszip";
import { api, streamRefreshLlmModels, streamTestLlmProvider, type AiPrompt, type AppSettings, type ArchitectureCategory, type AuditEntry, type BackupConflictMode, type BackupImportPreview, type LlmTestStep, type SandboxVm, type SandboxVmRun, type SiemDestination, type Workload } from "../api";
import { ensureUtc, formatError } from "../utils/format";
import { ConnectorsSection } from "./AutomationsView";
// Heavy admin sub-sections — lazy so visiting /admin only pulls the small General/
// Providers/etc. cards, not every reference-set editor in the bundle.
const SecurityPanel = lazy(() => import("./SecurityView").then((m) => ({ default: m.SecurityPanel })));
const AccessControlPanel = lazy(() => import("./SecurityView").then((m) => ({ default: m.AccessControlPanel })));
const AmbaReferenceEditor = lazy(() => import("./AmbaReferenceEditor").then((m) => ({ default: m.AmbaReferenceEditor })));
const TelemetryReferenceEditor = lazy(() => import("./TelemetryReferenceEditor").then((m) => ({ default: m.TelemetryReferenceEditor })));
const BackupDrReferenceEditor = lazy(() => import("./BackupDrReferenceEditor").then((m) => ({ default: m.BackupDrReferenceEditor })));

const AdminSubLoading = () => <div className="p-6 text-sm text-gray-500">Loading…</div>;
const wrap = (node: React.ReactNode) => <Suspense fallback={<AdminSubLoading />}>{node}</Suspense>;
import {
  ADMIN_NAV,
  ADMIN_SECTION_IDS,
  ACCESS_SUB_IDS,
  type AdminSection,
  type SecuritySection,
} from "./navConfig";

export { ADMIN_NAV, ADMIN_SECTION_IDS, ACCESS_SUB_IDS };
export type { AdminSection };

const PROVIDERS: {
  id: string;
  label: string;
  keyLabel: string;
  keyHint: string;
  auth: "key" | "none" | "oauth";
}[] = [
  {
    id: "openai",
    label: "OpenAI",
    keyLabel: "OpenAI API key",
    keyHint: "Starts with sk-… (platform.openai.com/api-keys)",
    auth: "key",
  },
  {
    id: "openai_eu",
    label: "OpenAI (EU)",
    keyLabel: "OpenAI API key",
    keyHint: "EU data residency — routes to eu.api.openai.com (use an EU-enabled OpenAI key)",
    auth: "key",
  },
  {
    id: "azure_openai",
    label: "Azure OpenAI",
    keyLabel: "Azure OpenAI API key",
    keyHint: "From your Azure OpenAI resource → Keys and Endpoint",
    auth: "key",
  },
  {
    id: "azure_foundry",
    label: "Azure Foundry",
    keyLabel: "Azure AI Foundry key",
    keyHint: "From your Azure AI Foundry resource → Keys (…services.ai.azure.com)",
    auth: "key",
  },
  {
    id: "github",
    label: "GitHub Models",
    keyLabel: "GitHub token (PAT)",
    keyHint: "GitHub PAT with models access — open-model catalog (Phi, Llama, Mistral)",
    auth: "key",
  },
  {
    id: "github_copilot",
    label: "GitHub Copilot",
    keyLabel: "GitHub Copilot sign-in",
    keyHint: "Sign in once in a browser; the token is captured & refreshed automatically",
    auth: "oauth",
  },
  {
    id: "ollama",
    label: "Ollama (local)",
    keyLabel: "Base URL",
    keyHint: "Local Ollama server — no API key needed",
    auth: "none",
  },
  {
    id: "chatgpt",
    label: "ChatGPT OAuth",
    keyLabel: "ChatGPT OAuth token",
    keyHint: "Sign in with your ChatGPT account (browser or paste-URL OAuth)",
    auth: "oauth",
  },
  {
    id: "claude",
    label: "Claude API",
    keyLabel: "Anthropic API key",
    keyHint: "Starts with sk-ant-… (console.anthropic.com)",
    auth: "key",
  },
  {
    id: "claude_oauth",
    label: "Claude OAuth",
    keyLabel: "Claude sign-in",
    keyHint: "Sign in with your Claude Pro/Max subscription (browser or paste-code OAuth)",
    auth: "oauth",
  },
  {
    id: "gemini",
    label: "Google Gemini",
    keyLabel: "Google AI API key",
    keyHint: "From aistudio.google.com/apikey (OpenAI-compatible endpoint)",
    auth: "key",
  },
  {
    id: "grok",
    label: "Grok (xAI)",
    keyLabel: "xAI API key",
    keyHint: "From console.x.ai (starts with xai-…)",
    auth: "key",
  },
  {
    id: "mistral",
    label: "Mistral",
    keyLabel: "Mistral API key",
    keyHint: "From console.mistral.ai",
    auth: "key",
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    keyLabel: "OpenRouter API key",
    keyHint: "From openrouter.ai/keys (starts with sk-or-…) — many models via one key",
    auth: "key",
  },
  {
    id: "lmstudio",
    label: "LM Studio (local)",
    keyLabel: "Base URL",
    keyHint: "Local LM Studio server — no API key needed",
    auth: "none",
  },
];

// Provider list grouping for the AI Providers screen. Grayed all-caps subheadings,
// mirroring the Settings sidebar clusters.
const PROVIDER_GROUPS: { label: string; ids: string[] }[] = [
  { label: "Microsoft / OpenAI", ids: ["openai", "openai_eu", "azure_openai", "azure_foundry", "github", "github_copilot", "chatgpt"] },
  { label: "Other providers", ids: ["claude_oauth", "claude", "gemini", "grok", "mistral", "openrouter"] },
  { label: "Local", ids: ["ollama", "lmstudio"] },
];

/** Format a USD amount, keeping sub-cent precision visible for small estimates. */
function fmtUsd(v: number): string {
  if (!v) return "$0.00";
  if (v < 0.01) return `$${v.toFixed(4)}`;
  if (v < 1) return `$${v.toFixed(3)}`;
  return `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Settings landing page shown at bare /admin: grouped cards linking to every section.
 *  Maps over ADMIN_NAV so a new section appears here + in the sidebar automatically. A quick
 *  filter keeps it usable as the section count grows (enterprise apps have many settings). */
function SettingsOverview() {
  const [q, setQ] = useState("");
  const term = q.trim().toLowerCase();

  // Preserve the ADMIN_NAV order but split into the same groups the sidebar uses. Each item
  // carries the group of the nearest preceding item that declared one.
  const groups: { name: string; items: typeof ADMIN_NAV }[] = [];
  for (const item of ADMIN_NAV) {
    if (item.group || groups.length === 0) {
      groups.push({ name: item.group ?? "Settings", items: [] });
    }
    groups[groups.length - 1].items.push(item);
  }

  const matches = (i: (typeof ADMIN_NAV)[number]) =>
    !term || i.label.toLowerCase().includes(term) || (i.desc ?? "").toLowerCase().includes(term);
  const visibleGroups = groups
    .map((g) => ({ ...g, items: g.items.filter(matches) }))
    .filter((g) => g.items.length > 0);

  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="mx-auto max-w-5xl space-y-6 p-8">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-gray-900">Settings</h1>
            <p className="mt-1 text-sm text-gray-500">
              Configure providers and connections, tune the agent's reference data, manage access,
              and review usage &amp; audit history.
            </p>
          </div>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search settings…"
            className="w-56 rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand-dark focus:outline-none"
          />
        </div>

        {visibleGroups.length === 0 ? (
          <p className="rounded-xl border border-dashed bg-white p-10 text-center text-sm text-gray-400">
            No settings match “{q}”.
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
                    to={`/admin/${i.id}`}
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

/** Settings content panel. Rendered inside the chat shell (the chat sidebar stays
 *  visible); section is driven by the /admin/:section URL. */
export function AdminPanel({ section }: { section: AdminSection }) {
  const tools = useQuery({ queryKey: ["tools"], queryFn: api.listTools });
  const usage = useQuery({ queryKey: ["usage"], queryFn: api.usage });

  // Bare /admin → a Settings overview landing page with links to every section.
  if (section === "overview") {
    return wrap(<SettingsOverview />);
  }

  // Access Control: a sub-tabbed page for Users / Roles / Groups / Sign-in & SSO.
  if (section === "access" || ACCESS_SUB_IDS.has(section)) {
    return wrap(<AccessControlPanel section={section} />);
  }
  // Standalone security sections (Active Sessions, Security Policy) keep their own panel.
  if (section === "sessions" || section === "policies") {
    return wrap(<SecurityPanel section={section as SecuritySection} />);
  }
  // AMBA Reference Set gets a dedicated full-page two-pane rich editor.
  if (section === "amba") {
    return wrap(<AmbaReferenceEditor />);
  }
  if (section === "telemetry") {
    return wrap(<TelemetryReferenceEditor />);
  }
  if (section === "backupdr") {
    return wrap(<BackupDrReferenceEditor />);
  }

  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="space-y-6 p-8">
        {section === "providers" && <AIProviderCard />}
        {section === "tenants" && <ConnectionsCard />}
        {section === "sandboxvms" && <SandboxVmsCard />}
        {section === "connectors" && <ConnectorsSection />}
        {section === "settings" && <AppSettingsCard />}
        {section === "prompts" && <AiPromptsCard />}
        {section === "scoring" && <ScoringTaxonomyCard />}
        {section === "ambachanges" && <AmbaChangeRequestsCard />}
        {section === "telemetrychanges" && <TelemetryChangeRequestsCard />}
        {section === "backupdrchanges" && <BackupDrChangeRequestsCard />}
        {section === "radar" && <RadarReferenceCard />}
        {section === "tools" && (
          <Card title="Azure MCP Tools">
            {tools.isError && (
              <p className="text-sm text-red-600">
                MCP server unavailable. Check the connection and Azure sign-in.
              </p>
            )}
            {tools.isLoading && (
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {Array.from({ length: 6 }).map((_, i) => (
                  <div key={i} className="h-16 animate-pulse rounded border bg-gray-100" />
                ))}
              </div>
            )}
            {!tools.isLoading && !tools.isError && tools.data?.length === 0 && (
              <p className="text-sm text-gray-500">No tools are currently exposed.</p>
            )}
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {tools.data?.map((t) => (
                <div key={t.name} className="rounded border bg-white p-3 text-sm">
                  <div className="flex items-center justify-between">
                    <span className="font-mono font-medium">{t.name}</span>
                    <span
                      className={`rounded px-2 py-0.5 text-xs ${
                        t.kind === "write"
                          ? "bg-amber-100 text-amber-800"
                          : "bg-green-100 text-green-800"
                      }`}
                    >
                      {t.kind}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-gray-500">{t.description}</p>
                </div>
              ))}
            </div>
          </Card>
        )}

        {section === "tools" && <BuiltinToolsCard />}

        {section === "entratools" && <EntraToolsCard />}

        {section === "usage" && (
          <Card title="Usage by Model">
            <table className="w-full text-sm">
              <thead className="text-left text-gray-500">
                <tr>
                  <th className="py-1">Provider</th>
                  <th className="py-1">Model</th>
                  <th>Requests</th>
                  <th>Prompt</th>
                  <th>Completion</th>
                  <th className="text-right">Est. cost</th>
                </tr>
              </thead>
              <tbody>
                {usage.data?.map((u) => (
                  <tr key={`${u.provider}|${u.model}`} className="border-t">
                    <td className="py-1">{u.provider ? (PROVIDER_DISPLAY[u.provider] ?? u.provider) : "—"}</td>
                    <td className="py-1 font-mono">{u.model}</td>
                    <td>{u.requests}</td>
                    <td>{u.prompt_tokens}</td>
                    <td>{u.completion_tokens}</td>
                    <td className="text-right tabular-nums" title={u.estimated ? "Estimated with a default rate (model price not in table)" : "Estimated from per-model rates"}>
                      {fmtUsd(u.cost_usd)}
                      {u.estimated && <span className="ml-1 text-[10px] text-amber-500">~</span>}
                    </td>
                  </tr>
                ))}
                {usage.data && usage.data.length > 0 && (
                  <tr className="border-t-2 border-gray-300 font-semibold">
                    <td className="py-1" colSpan={2}>Total</td>
                    <td>{usage.data.reduce((a, u) => a + u.requests, 0)}</td>
                    <td>{usage.data.reduce((a, u) => a + u.prompt_tokens, 0)}</td>
                    <td>{usage.data.reduce((a, u) => a + u.completion_tokens, 0)}</td>
                    <td className="text-right tabular-nums">{fmtUsd(usage.data.reduce((a, u) => a + u.cost_usd, 0))}</td>
                  </tr>
                )}
                {usage.data && usage.data.length === 0 && (
                  <tr className="border-t">
                    <td colSpan={6} className="py-3 text-center text-gray-400">No usage recorded yet.</td>
                  </tr>
                )}
              </tbody>
            </table>
            <p className="mt-3 text-[11px] text-gray-400">
              Costs are estimated from token counts using standard per-model rates and are for
              governance visibility only — not a billing source. Rows marked “~” use a default
              rate because the model isn’t in the price table.
            </p>
          </Card>
        )}

        {section === "audit" && <AuditCard />}
        {section === "backup" && <BackupRestoreCard />}
        {section === "demodata" && <DemoDataCard />}
      </div>
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border bg-white p-4 shadow-sm">
      <h2 className="mb-3 font-medium">{title}</h2>
      {children}
    </section>
  );
}

// ===========================================================================
// Backup & Restore
// ===========================================================================
const BACKUP_TIER_LABEL: Record<string, string> = {
  config: "Configuration",
  reference: "Reference sets",
  secrets: "Connections & credentials",
  data: "Operational data",
};
const BACKUP_TIER_ORDER = ["config", "data", "reference", "secrets"];

// A checkbox that can render the tri-state "indeterminate" look (set via a ref, since React
// has no `indeterminate` prop). Used for the backup category headers.
function IndeterminateCheckbox({
  checked,
  indeterminate,
  onChange,
}: {
  checked: boolean;
  indeterminate: boolean;
  onChange: () => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate && !checked;
  }, [indeterminate, checked]);
  return <input ref={ref} type="checkbox" checked={checked} onChange={onChange} />;
}
const BACKUP_MODE_HELP: Record<BackupConflictMode, string> = {
  merge: "Add new items and update matching ones; keep everything else.",
  overwrite: "Replace matching items with the backup's version (new items added).",
  skip: "Only add items that don't already exist; never touch existing ones.",
};

function BackupRestoreCard() {
  const qc = useQueryClient();
  const sectionsQ = useQuery({ queryKey: ["backupSections"], queryFn: api.backupSections });
  const sections = sectionsQ.data?.sections ?? [];

  // ---- Export state ----
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [exporting, setExporting] = useState(false);
  const [exportErr, setExportErr] = useState("");
  const [includeChats, setIncludeChats] = useState(false);

  // Default-select every section once the catalog loads.
  useEffect(() => {
    if (sections.length) setSelected((cur) => (cur.size ? cur : new Set(sections.map((s) => s.id))));
  }, [sections]);

  const byTier = BACKUP_TIER_ORDER.map((tier) => ({
    tier,
    items: sections.filter((s) => s.tier === tier),
  })).filter((g) => g.items.length > 0);

  const toggle = (id: string) =>
    setSelected((cur) => {
      const next = new Set(cur);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  // Select / deselect every section in a category at once. If all of the category's items
  // are already selected, clicking clears them; otherwise it selects them all.
  const toggleTier = (ids: string[]) =>
    setSelected((cur) => {
      const next = new Set(cur);
      const allOn = ids.every((id) => next.has(id));
      for (const id of ids) {
        if (allOn) next.delete(id);
        else next.add(id);
      }
      return next;
    });

  async function doExport() {
    setExporting(true);
    setExportErr("");
    try {
      const blob = await api.backupExport([...selected], includeChats);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const ts = new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-");
      a.href = url;
      a.download = `azsupagent-backup-${ts}.zip`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setExportErr(formatError(e));
    } finally {
      setExporting(false);
    }
  }

  // ---- Import state ----
  const [parsed, setParsed] = useState<unknown>(null);
  const [fileName, setFileName] = useState("");
  const [mode, setMode] = useState<BackupConflictMode>("merge");
  const [preview, setPreview] = useState<BackupImportPreview | null>(null);
  const [importErr, setImportErr] = useState("");
  const [importBusy, setImportBusy] = useState(false);
  const [done, setDone] = useState<{ sections: number; secrets: string[] } | null>(null);
  const [importArchiveHasChats, setImportArchiveHasChats] = useState(false);

  const parseImportedFile = async (file: File) => {
    const lowerName = file.name.toLowerCase();
    if (lowerName.endsWith(".zip")) {
      const zip = await JSZip.loadAsync(await file.arrayBuffer());
      const manifestFile = zip.file("backup.json");
      if (!manifestFile) throw new Error("That ZIP archive does not contain backup.json.");
      const text = await manifestFile.async("text");
      return { data: JSON.parse(text), hasChats: Boolean(zip.file("chats.zip")) };
    }
    return { data: JSON.parse(await file.text()), hasChats: false };
  };

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setImportErr("");
    setPreview(null);
    setDone(null);
    setFileName(file.name);
    setImportArchiveHasChats(false);
    try {
      const parsedFile = await parseImportedFile(file);
      setParsed(parsedFile.data);
      setImportArchiveHasChats(parsedFile.hasChats);
    } catch {
      setParsed(null);
      setImportErr(file.name.toLowerCase().endsWith(".zip") ? "That ZIP archive isn't a valid backup export." : "That file isn't valid JSON.");
    }
  }

  async function doPreview() {
    if (!parsed) return;
    setImportBusy(true);
    setImportErr("");
    setDone(null);
    try {
      setPreview(await api.backupImportPreview(parsed, mode));
    } catch (e) {
      setImportErr(formatError(e));
    } finally {
      setImportBusy(false);
    }
  }

  async function doImport() {
    if (!parsed) return;
    if (!confirm(`Restore this backup using "${mode}"? This changes configuration and data on this instance.`)) return;
    setImportBusy(true);
    setImportErr("");
    try {
      const res = await api.backupImport(parsed, mode, []);
      setDone({ sections: res.sections.length, secrets: res.secrets_required });
      setPreview(null);
      qc.invalidateQueries({ queryKey: ["backupSections"] });
    } catch (e) {
      setImportErr(formatError(e));
    } finally {
      setImportBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <Card title="Export backup">
        <p className="mb-3 text-xs text-gray-500">
          Download a portable ZIP backup of this tenant's configuration and operational data.
          Secrets (API keys, client secrets, tokens) are <strong>never</strong> exported — you'll
          re-enter them after restoring. Chats can be included as an export-only HTML archive.
        </p>
        {sectionsQ.isLoading && <div className="h-16 animate-pulse rounded-lg border bg-gray-100" />}
        <div className="space-y-4">
          {byTier.map((g) => {
            const ids = g.items.map((s) => s.id);
            const selectedCount = ids.filter((id) => selected.has(id)).length;
            const allOn = selectedCount === ids.length;
            const someOn = selectedCount > 0 && !allOn;
            return (
            <div key={g.tier}>
              <label className="mb-1.5 flex cursor-pointer items-center gap-2 select-none">
                <IndeterminateCheckbox
                  checked={allOn}
                  indeterminate={someOn}
                  onChange={() => toggleTier(ids)}
                />
                <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400 hover:text-gray-600">
                  {BACKUP_TIER_LABEL[g.tier] ?? g.tier}
                </span>
                <span className="text-[10px] text-gray-300">{selectedCount}/{ids.length}</span>
                {g.tier === "secrets" && (
                  <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700">
                    secrets redacted
                  </span>
                )}
              </label>
              <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                {g.items.map((s) => (
                  <label
                    key={s.id}
                    className="flex cursor-pointer items-center justify-between rounded-lg border px-2.5 py-1.5 text-sm hover:bg-gray-50"
                  >
                    <span className="flex items-center gap-2">
                      <input type="checkbox" checked={selected.has(s.id)} onChange={() => toggle(s.id)} />
                      <span className="text-gray-700">{s.label}</span>
                    </span>
                    <span className="text-[11px] text-gray-400">{s.count}</span>
                  </label>
                ))}
              </div>
            </div>
            );
          })}
        </div>
        <label className="mt-4 flex items-center gap-2 text-sm text-gray-700">
          <input type="checkbox" checked={includeChats} onChange={(e) => setIncludeChats(e.target.checked)} />
          Include chats as a nested HTML ZIP export
        </label>
        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={doExport}
            disabled={exporting || selected.size === 0}
            className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50"
          >
            {exporting ? "Preparing…" : `Download ZIP backup (${selected.size})`}
          </button>
          <button onClick={() => setSelected(new Set(sections.map((s) => s.id)))} className="text-xs text-gray-500 hover:underline">
            Select all
          </button>
          <button onClick={() => setSelected(new Set())} className="text-xs text-gray-500 hover:underline">
            Clear
          </button>
          {exportErr && <span className="text-xs text-red-600">{exportErr}</span>}
        </div>
      </Card>

      <Card title="Restore backup">
        <p className="mb-3 text-xs text-gray-500">
          Upload a backup file, preview the changes, then apply. Existing local data is never
          deleted; a restored secret blank never overwrites a working credential. ZIP exports
          can include chats, which remain export-only and are skipped on restore.
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <label className="cursor-pointer rounded-lg border px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50">
            <input type="file" accept="application/json,application/zip,.json,.zip" onChange={onFile} className="hidden" />
            {fileName || "Choose backup file…"}
          </label>
          <select
            value={mode}
            onChange={(e) => {
              setMode(e.target.value as BackupConflictMode);
              setPreview(null);
            }}
            className="rounded-lg border px-2 py-1.5 text-sm"
          >
            <option value="merge">Merge</option>
            <option value="overwrite">Overwrite</option>
            <option value="skip">Skip existing</option>
          </select>
          <button
            onClick={doPreview}
            disabled={!parsed || importBusy}
            className="rounded-lg border px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            Preview
          </button>
          <button
            onClick={doImport}
            disabled={!parsed || importBusy}
            className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50"
          >
            {importBusy ? "Working…" : "Restore"}
          </button>
        </div>
        <p className="mt-1.5 text-[11px] text-gray-400">{BACKUP_MODE_HELP[mode]}</p>
        {importErr && <div className="mt-2 text-xs text-red-600">{importErr}</div>}
        {importArchiveHasChats && (
          <div className="mt-2 rounded-lg border border-sky-200 bg-sky-50 p-2 text-xs text-sky-800">
            This export includes chats. They are preserved as HTML in the archive and are not restored into the app.
          </div>
        )}

        {preview && (
          <div className="mt-4">
            {preview.source_tenant && (
              <div className="mb-2 text-[11px] text-gray-500">
                Backup from tenant <span className="font-mono">{preview.source_tenant}</span>
                {preview.exported_at && <> · exported {new Date(preview.exported_at).toLocaleString()}</>}
              </div>
            )}
            <div className="overflow-x-auto rounded-lg border">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 text-left text-xs text-gray-500">
                  <tr>
                    <th className="px-3 py-1.5 font-medium">Section</th>
                    <th className="px-3 py-1.5 font-medium">Incoming</th>
                    <th className="px-3 py-1.5 font-medium text-green-600">Create</th>
                    <th className="px-3 py-1.5 font-medium text-blue-600">Update</th>
                    <th className="px-3 py-1.5 font-medium text-gray-500">Skip</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.sections.map((s) => (
                    <tr key={s.id} className="border-t">
                      <td className="px-3 py-1.5 text-gray-700">{s.label}</td>
                      <td className="px-3 py-1.5 text-gray-500">{s.incoming}</td>
                      <td className="px-3 py-1.5 text-green-700">{s.create || ""}</td>
                      <td className="px-3 py-1.5 text-blue-700">{s.update || ""}</td>
                      <td className="px-3 py-1.5 text-gray-400">{s.skip || ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {preview.secrets_required.length > 0 && (
              <BackupSecretsNotice secrets={preview.secrets_required} />
            )}
          </div>
        )}

        {done && (
          <div className="mt-4 rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-800">
            ✓ Restore complete — {done.sections} section{done.sections === 1 ? "" : "s"} applied.
            {done.secrets.length > 0 && <BackupSecretsNotice secrets={done.secrets} />}
          </div>
        )}
      </Card>
    </div>
  );
}

function BackupSecretsNotice({ secrets }: { secrets: string[] }) {
  return (
    <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
      <div className="mb-1 font-medium">Re-enter these secrets after restoring:</div>
      <ul className="ml-4 list-disc space-y-0.5">
        {secrets.map((s) => (
          <li key={s}>{s}</li>
        ))}
      </ul>
      <div className="mt-2 flex gap-3">
        <Link to="/admin/connectors" className="text-amber-900 underline">Connectors</Link>
        <Link to="/admin/tenants" className="text-amber-900 underline">Azure Tenants</Link>
        <Link to="/admin/providers" className="text-amber-900 underline">AI Providers</Link>
      </div>
    </div>
  );
}

const AUTH_METHOD_LABELS: Record<string, string> = {
  service_principal: "Service principal (client secret)",
  service_principal_cert: "Service principal (certificate)",
  default_chain: "Host identity (managed identity / az login)",
  az_cli_token: "Paste Azure CLI token (short-lived)",
};

const BLANK_FORM: import("../api").ConnectionUpsert = {
  display_name: "",
  tenant_id: "",
  auth_method: "default_chain",
  default_subscription: "",
  read_only: true,
  auto_execute_writes: false,
  is_default: false,
  client_id: "",
  client_secret: "",
  certificate_pem: "",
  access_token: "",
  access_token_json: "",
  graph_access_token_json: "",
};

function EntraValidationResult({
  result,
}: {
  result: { ok: boolean; detail?: string; report?: import("../api").EntraValidation };
}) {
  const r = result.report;
  if (!result.ok && !r) {
    return (
      <div className="mt-1.5 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-700">
        ✗ EntraID check failed: {result.detail ?? "Unknown error"}
      </div>
    );
  }
  if (!r) return null;
  return (
    <div
      className={`mt-1.5 rounded-md border px-3 py-2 text-[11px] ${
        r.satisfied ? "border-green-200 bg-green-50" : "border-amber-200 bg-amber-50"
      }`}
    >
      <div className={`font-medium ${r.satisfied ? "text-green-700" : "text-amber-700"}`}>
        {r.satisfied ? "✓ " : "⚠ "}
        {r.summary}
      </div>
      {r.displayName && (
        <div className="mt-0.5 text-gray-500">
          App: <span className="font-mono">{r.displayName}</span> ({r.appId})
        </div>
      )}
      <div className="mt-1.5 flex flex-wrap gap-1">
        {r.required.map((p) => {
          const granted = r.granted.includes(p);
          return (
            <span
              key={p}
              title={granted ? "Granted" : "Missing — grant + admin-consent this permission"}
              className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${
                granted ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"
              }`}
            >
              {granted ? "✓" : "✗"} {p}
            </span>
          );
        })}
      </div>
      {r.extra.length > 0 && (
        <div className="mt-1 text-[10px] text-gray-400">
          Also granted: {r.extra.join(", ")}
        </div>
      )}
    </div>
  );
}

function ConnectionsCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["adminConnections"], queryFn: api.adminConnections });
  const [editing, setEditing] = useState<import("../api").ConnectionUpsert | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ id: string; ok: boolean; text: string } | null>(null);
  const [entra, setEntra] = useState<{
    id: string;
    ok: boolean;
    detail?: string;
    report?: import("../api").EntraValidation;
  } | null>(null);
  const [error, setError] = useState("");

  const connections = q.data?.connections ?? [];

  function startAdd() {
    setError("");
    setEditing({ ...BLANK_FORM });
  }

  function startEdit(c: import("../api").AzureConnection) {
    setError("");
    setEditing({
      id: c.id,
      display_name: c.display_name,
      tenant_id: c.tenant_id,
      auth_method: c.auth_method,
      default_subscription: c.default_subscription,
      read_only: c.read_only,
      auto_execute_writes: c.auto_execute_writes,
      is_default: c.is_default,
      client_id: c.client_id,
      // Secrets are write-only; leave blank to keep existing.
      client_secret: "",
      certificate_pem: "",
      access_token: "",
      access_token_json: "",
      graph_access_token_json: "",
    });
  }

  async function save() {
    if (!editing) return;
    if (!editing.display_name.trim()) {
      setError("Give the connection a name.");
      return;
    }
    if (editing.auth_method !== "az_cli_token" && !editing.tenant_id.trim()) {
      setError("Tenant ID is required.");
      return;
    }
    setError("");
    try {
      await api.upsertConnection(editing);
      setEditing(null);
      qc.invalidateQueries({ queryKey: ["adminConnections"] });
      qc.invalidateQueries({ queryKey: ["azureConnections"] });
    } catch (e) {
      setError(formatError(e));
    }
  }

  async function remove(id: string) {
    if (!confirm("Delete this Azure connection?")) return;
    setBusyId(id);
    try {
      await api.deleteConnection(id);
      qc.invalidateQueries({ queryKey: ["adminConnections"] });
      qc.invalidateQueries({ queryKey: ["azureConnections"] });
    } finally {
      setBusyId(null);
    }
  }

  async function makeDefault(id: string) {
    setBusyId(id);
    try {
      await api.setDefaultConnection(id);
      qc.invalidateQueries({ queryKey: ["adminConnections"] });
      qc.invalidateQueries({ queryKey: ["azureConnections"] });
    } finally {
      setBusyId(null);
    }
  }

  async function toggleDisabled(c: import("../api").AzureConnection) {
    setBusyId(c.id);
    try {
      // Minimal upsert: the server merges with the stored connection, so only the
      // identity fields + the flipped flag are needed (secrets are kept untouched).
      await api.upsertConnection({
        id: c.id,
        display_name: c.display_name,
        tenant_id: c.tenant_id,
        auth_method: c.auth_method,
        disabled: !c.disabled,
      });
      qc.invalidateQueries({ queryKey: ["adminConnections"] });
      qc.invalidateQueries({ queryKey: ["azureConnections"] });
    } finally {
      setBusyId(null);
    }
  }

  async function test(id: string) {
    setBusyId(id);
    setMsg(null);
    try {
      const r = await api.testConnection(id);
      setMsg({
        id,
        ok: r.ok,
        text: r.ok
          ? `✓ Connected — ${r.subscription_count} subscription(s) visible`
          : `✗ ${r.detail ?? "Test failed"}`,
      });
      qc.invalidateQueries({ queryKey: ["adminConnections"] });
    } catch (e) {
      setMsg({ id, ok: false, text: formatError(e) });
    } finally {
      setBusyId(null);
    }
  }

  async function validateEntra(id: string) {
    setBusyId(`entra-${id}`);
    setEntra(null);
    try {
      const r = await api.validateEntra(id);
      setEntra({ id, ok: r.ok, detail: r.detail, report: r.report });
    } catch (e) {
      setEntra({ id, ok: false, detail: formatError(e) });
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-5">
      <Card title="Azure tenants (connections)">
        <p className="mb-3 text-xs text-gray-500">
          Connect one or more Azure tenants. Each connection has its own identity and
          governance (read-only / write policy). Users pick a tenant per chat from the
          composer. Credentials are encrypted at rest and never shown again.
        </p>

        {q.isLoading && (
          <div className="space-y-2">
            {Array.from({ length: 2 }).map((_, i) => (
              <div key={i} className="h-16 animate-pulse rounded-lg border bg-gray-100" />
            ))}
          </div>
        )}

        {!q.isLoading && connections.length === 0 && !editing && (
          <div className="rounded-lg border border-dashed border-gray-300 bg-gray-50 p-6 text-center text-sm text-gray-500">
            No Azure tenants connected yet.
          </div>
        )}

        <div className="space-y-2">
          {connections.map((c) => (
            <div key={c.id} className="rounded-lg border bg-white p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className={`font-medium ${c.disabled ? "text-gray-400" : ""}`}>{c.display_name}</span>
                    {c.is_default && (
                      <span className="rounded bg-brand/10 px-1.5 py-0.5 text-[10px] font-medium text-brand">
                        default
                      </span>
                    )}
                    {c.disabled ? (
                      <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] font-medium text-gray-600">
                        disabled
                      </span>
                    ) : (
                      <StatusDot status={c.status} />
                    )}
                    {c.read_only ? (
                      <span className="rounded bg-green-100 px-1.5 py-0.5 text-[10px] text-green-800">
                        read-only
                      </span>
                    ) : (
                      <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-800">
                        writes enabled
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 truncate text-xs text-gray-500">
                    {AUTH_METHOD_LABELS[c.auth_method] ?? c.auth_method} · tenant{" "}
                    <span className="font-mono">{c.tenant_id || "—"}</span>
                  </div>
                  {c.status_detail && (
                    <div className="mt-0.5 text-[11px] text-gray-400">{c.status_detail}</div>
                  )}
                  {msg && msg.id === c.id && (
                    <div
                      className={`mt-1 text-[11px] ${msg.ok ? "text-green-600" : "text-red-600"}`}
                    >
                      {msg.text}
                    </div>
                  )}
                  {entra && entra.id === c.id && (
                    <EntraValidationResult result={entra} />
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-1.5 text-xs">
                  <button
                    onClick={() => void test(c.id)}
                    disabled={busyId === c.id}
                    className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50 disabled:opacity-50"
                  >
                    Test
                  </button>
                  <button
                    onClick={() => void validateEntra(c.id)}
                    disabled={busyId === `entra-${c.id}`}
                    title="Check the app's Microsoft Graph permissions for the EntraID MCP server"
                    className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50 disabled:opacity-50"
                  >
                    {busyId === `entra-${c.id}` ? "Checking…" : "Test EntraID"}
                  </button>
                  {!c.is_default && (
                    <button
                      onClick={() => void makeDefault(c.id)}
                      disabled={busyId === c.id || c.disabled}
                      title={c.disabled ? "Enable this tenant first" : undefined}
                      className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50 disabled:opacity-50"
                    >
                      Set default
                    </button>
                  )}
                  <button
                    onClick={() => void toggleDisabled(c)}
                    disabled={busyId === c.id}
                    title={c.disabled ? "Enable this tenant" : "Disable — hide from the chat tenant picker"}
                    className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50 disabled:opacity-50"
                  >
                    {c.disabled ? "Enable" : "Disable"}
                  </button>
                  <button
                    onClick={() => startEdit(c)}
                    className="rounded border px-2 py-1 text-gray-600 hover:bg-gray-50"
                  >
                    Edit
                  </button>
                  <button
                    onClick={() => void remove(c.id)}
                    disabled={busyId === c.id}
                    className="rounded border border-red-200 px-2 py-1 text-red-600 hover:bg-red-50 disabled:opacity-50"
                  >
                    Delete
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>

        {!editing && (
          <button
            onClick={startAdd}
            className="mt-3 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90"
          >
            + Connect a tenant
          </button>
        )}

        {editing && (
          <ConnectionForm
            form={editing}
            setForm={setEditing}
            authMethods={q.data?.auth_methods ?? Object.keys(AUTH_METHOD_LABELS)}
            error={error}
            onCancel={() => {
              setEditing(null);
              setError("");
            }}
            onSave={() => void save()}
          />
        )}
      </Card>
    </div>
  );
}

function StatusDot({ status }: { status: string }) {
  const color =
    status === "ok" ? "bg-green-500" : status === "error" ? "bg-red-500" : "bg-gray-300";
  const title = status === "ok" ? "Healthy" : status === "error" ? "Error" : "Untested";
  return <span className={`inline-block h-2 w-2 rounded-full ${color}`} title={title} />;
}

function ConnectionForm({
  form,
  setForm,
  authMethods,
  error,
  onCancel,
  onSave,
}: {
  form: import("../api").ConnectionUpsert;
  setForm: (f: import("../api").ConnectionUpsert) => void;
  authMethods: string[];
  error: string;
  onCancel: () => void;
  onSave: () => void;
}) {
  const set = (patch: Partial<import("../api").ConnectionUpsert>) =>
    setForm({ ...form, ...patch });
  const input =
    "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand";
  const label = "mb-1 block text-xs font-medium text-gray-600";

  // Live subscription discovery using the saved connection's credentials.
  const [subs, setSubs] = useState<
    { id: string; name: string; state?: string; is_default?: boolean }[]
  >([]);
  const [pulling, setPulling] = useState(false);
  const [pullMsg, setPullMsg] = useState("");

  async function pullSubscriptions() {
    if (!form.id) {
      setPullMsg("Save the connection first, then pull subscriptions.");
      return;
    }
    setPulling(true);
    setPullMsg("");
    try {
      const r = await api.discoverConnection(form.id);
      if (!r.ok) {
        setPullMsg(r.detail || "Could not reach Azure with this connection.");
        setSubs([]);
        return;
      }
      setSubs(r.subscriptions);
      if (r.subscriptions.length === 0) {
        setPullMsg("No subscriptions are visible to this connection.");
        return;
      }
      // Default to the connection's marked default sub if present, else the first.
      if (!form.default_subscription) {
        const def = r.subscriptions.find((s) => s.is_default) ?? r.subscriptions[0];
        set({ default_subscription: def.id });
      }
      setPullMsg(`Found ${r.subscriptions.length} subscription(s).`);
    } catch (e) {
      setPullMsg(formatError(e));
      setSubs([]);
    } finally {
      setPulling(false);
    }
  }

  return (
    <div className="mt-4 space-y-3 rounded-lg border border-brand/30 bg-brand/5 p-4">
      <div className="text-sm font-medium text-gray-800">
        {form.id ? "Edit connection" : "Connect an Azure tenant"}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label className={label}>Display name</label>
          <input
            className={input}
            value={form.display_name}
            onChange={(e) => set({ display_name: e.target.value })}
            placeholder="e.g. Contoso Production"
          />
        </div>
        <div>
          <label className={label}>Tenant ID</label>
          <input
            className={input}
            value={form.tenant_id}
            onChange={(e) => set({ tenant_id: e.target.value })}
            placeholder="00000000-0000-0000-0000-000000000000"
          />
        </div>
      </div>

      <div>
        <label className={label}>Authentication method</label>
        <select
          className={input}
          value={form.auth_method}
          onChange={(e) => set({ auth_method: e.target.value })}
        >
          {authMethods.map((m) => (
            <option key={m} value={m}>
              {AUTH_METHOD_LABELS[m] ?? m}
            </option>
          ))}
        </select>
      </div>

      {form.auth_method === "service_principal" && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <label className={label}>Client (application) ID</label>
            <input
              className={input}
              value={form.client_id ?? ""}
              onChange={(e) => set({ client_id: e.target.value })}
              placeholder="App registration client id"
            />
          </div>
          <div>
            <label className={label}>Client secret</label>
            <input
              // Masked TEXT input (not type=password) so the browser won't autofill the
              // saved admin login over the real secret. Mirrors the AI-provider key guard.
              type="text"
              style={{ WebkitTextSecurity: "disc" } as React.CSSProperties}
              name="conn-client-secret"
              autoComplete="off"
              data-1p-ignore
              data-lpignore="true"
              data-form-type="other"
              className={input}
              value={form.client_secret ?? ""}
              onChange={(e) => set({ client_secret: e.target.value })}
              placeholder={form.id ? "•••• (leave blank to keep)" : "Secret value"}
            />
          </div>
        </div>
      )}

      {form.auth_method === "service_principal_cert" && (
        <div className="space-y-3">
          <div>
            <label className={label}>Client (application) ID</label>
            <input
              className={input}
              value={form.client_id ?? ""}
              onChange={(e) => set({ client_id: e.target.value })}
              placeholder="App registration client id"
            />
          </div>
          <div>
            <label className={label}>Certificate (PEM: private key + certificate)</label>
            <textarea
              rows={4}
              className={`${input} font-mono text-xs`}
              value={form.certificate_pem ?? ""}
              onChange={(e) => set({ certificate_pem: e.target.value })}
              placeholder={
                form.id
                  ? "•••• (leave blank to keep)"
                  : "-----BEGIN PRIVATE KEY-----\n…\n-----BEGIN CERTIFICATE-----\n…"
              }
            />
          </div>
        </div>
      )}

      {form.auth_method === "azure_cli" && <AzureCliFields tenantId={form.tenant_id} />}

      {form.auth_method === "az_cli_token" && <AzCliTokenFields form={form} set={set} />}

      {form.auth_method === "default_chain" && (
        <div className="rounded-lg border border-gray-200 bg-white px-3 py-2.5 text-xs text-gray-600">
          Uses the host's <code className="rounded bg-gray-100 px-1">az login</code> /
          managed identity (DefaultAzureCredential). Best for a single tenant the server
          is already signed into.
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <div className="mb-1 flex items-center justify-between">
            <label className={label.replace("mb-1 ", "")}>
              Default subscription (optional)
            </label>
            <button
              type="button"
              onClick={() => void pullSubscriptions()}
              disabled={pulling}
              title={
                form.id
                  ? "Fetch subscriptions live using this connection"
                  : "Save the connection first to pull subscriptions"
              }
              className="inline-flex items-center gap-1 rounded-md border border-brand/40 px-2 py-0.5 text-[11px] font-medium text-brand transition hover:bg-brand/10 disabled:opacity-50"
            >
              {pulling ? "Pulling…" : "↻ Pull live"}
            </button>
          </div>
          {subs.length > 0 ? (
            <select
              className={input}
              value={form.default_subscription ?? ""}
              onChange={(e) => set({ default_subscription: e.target.value })}
            >
              <option value="">— None —</option>
              {subs.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.id}){s.is_default ? " · default" : ""}
                </option>
              ))}
            </select>
          ) : (
            <input
              className={input}
              value={form.default_subscription ?? ""}
              onChange={(e) => set({ default_subscription: e.target.value })}
              placeholder="Subscription id to default to"
            />
          )}
          {pullMsg && (
            <p className="mt-1 text-[11px] text-gray-500">{pullMsg}</p>
          )}
        </div>
      </div>

      <div className="divide-y rounded-lg border bg-white px-3">
        <Toggle
          label="Read-only for this tenant"
          hint="Only investigation tools are exposed; the agent can never modify this tenant."
          checked={!!form.read_only}
          onChange={(v) => set({ read_only: v })}
        />
        {!form.read_only && (
          <Toggle
            label="Auto-execute writes"
            hint="Run mutating operations immediately (no approval pause) for this tenant."
            checked={!!form.auto_execute_writes}
            onChange={(v) => set({ auto_execute_writes: v })}
          />
        )}
        <Toggle
          label="Make default tenant"
          hint="New chats use this tenant unless the user picks another."
          checked={!!form.is_default}
          onChange={(v) => set({ is_default: v })}
        />
      </div>

      {error && <div className="text-xs text-red-600">{error}</div>}

      <div className="flex gap-2">
        <button
          onClick={onSave}
          className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90"
        >
          Save connection
        </button>
        <button
          onClick={onCancel}
          className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

function AzureCliFields({ tenantId }: { tenantId: string }) {
  return (
    <div className="rounded-lg border border-green-200 bg-green-50 p-3 text-xs text-green-900">
      <div className="mb-1 font-semibold">Sign in once — stays connected</div>
      <p className="mb-2">
        Uses the host's Azure CLI session for this tenant. The CLI keeps the session
        refreshed automatically (~90 days, rolling), so you never paste tokens and it
        won't expire after an hour — just like a normal{" "}
        <code className="rounded bg-white px-1">az login</code> connection.
      </p>
      <ol className="list-decimal space-y-1 pl-4">
        <li>
          On the machine running this app, sign in to the tenant:
          <pre className="mt-1 overflow-x-auto rounded bg-white px-2 py-1 font-mono text-[11px] text-gray-800">
            az login --tenant {tenantId || "<TENANT_ID>"}
          </pre>
        </li>
        <li>
          Enter the Tenant ID above, then <strong>Save</strong> and <strong>Test</strong>.
          That's it — no token to paste.
        </li>
      </ol>
      <div className="mt-2 text-[11px] text-green-800">
        Re-authentication is only needed if you sign out of the CLI or the session is
        revoked. For a fully unattended service, use a service principal instead.
      </div>
    </div>
  );
}

function CmdBlock({ cmd }: { cmd: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="mt-1 flex items-stretch gap-1">
      <button
        type="button"
        onClick={() => { void navigator.clipboard?.writeText(cmd); setCopied(true); setTimeout(() => setCopied(false), 1200); }}
        title="Copy command"
        className="shrink-0 rounded border border-gray-200 bg-white px-1.5 text-[10px] text-gray-500 hover:bg-gray-50 hover:text-gray-700"
      >
        {copied ? "✓ Copied" : "⧉ Copy"}
      </button>
      <pre className="flex-1 overflow-x-auto rounded bg-white px-2 py-1 font-mono text-[11px] text-gray-800">{cmd}</pre>
    </div>
  );
}

function AzCliTokenFields({
  form,
  set,
}: {
  form: import("../api").ConnectionUpsert;
  set: (patch: Partial<import("../api").ConnectionUpsert>) => void;
}) {
  const input =
    "w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand";
  const label = "mb-1 block text-xs font-medium text-gray-600";
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-xs text-blue-900">
        <div className="mb-1 font-semibold">How to get your token</div>
        <ol className="list-decimal space-y-1 pl-4">
          <li>
            On your own computer, sign in to the tenant:
            <CmdBlock cmd="az login --tenant <TENANT_ID>" />
          </li>
          <li>
            Get an ARM access token (valid ~1 hour):
            <CmdBlock cmd="az account get-access-token --resource https://management.azure.com --output json" />
          </li>
          <li>Paste the entire JSON output below. We extract the token, expiry, tenant and subscription automatically.</li>
        </ol>
        <div className="mt-2 text-[11px] text-blue-800">
          The token is short-lived (~1 hour) and cannot be refreshed — the Azure CLI
          does not expose refresh tokens. For an always-on connection that auto-refreshes,
          switch to <strong>“Azure CLI sign-in”</strong> above, or use a service principal.
        </div>
      </div>
      <div>
        <label className={label}>Paste `az account get-access-token` JSON</label>
        <textarea
          rows={4}
          className={`${input} font-mono text-xs`}
          value={form.access_token_json ?? ""}
          onChange={(e) => set({ access_token_json: e.target.value })}
          placeholder={'{\n  "accessToken": "eyJ0…",\n  "expiresOn": "2026-06-06 12:00:00.000000",\n  "subscription": "…",\n  "tenant": "…"\n}'}
        />
      </div>
      <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
        <div className="mb-1 font-semibold">Microsoft Graph token (optional — resolves names)</div>
        <p className="mb-1">
          An ARM token can’t query Microsoft Graph, so user / group / service-principal <strong>names</strong>{" "}
          (e.g. in RBAC Access Review) can’t be resolved from GUIDs without a Graph token. To enable name
          resolution, also paste a Graph token:
        </p>
        <div className="mb-2"><CmdBlock cmd="az account get-access-token --resource-type ms-graph --output json" /></div>
        <label className={label}>Paste Microsoft Graph token JSON (optional)</label>
        <textarea
          rows={3}
          className={`${input} font-mono text-xs`}
          value={form.graph_access_token_json ?? ""}
          onChange={(e) => set({ graph_access_token_json: e.target.value })}
          placeholder={'{\n  "accessToken": "eyJ0…",\n  "expiresOn": "2026-06-06 12:00:00.000000"\n}'}
        />
        <div className="mt-1 text-[11px] text-amber-800">
          Needs Directory.Read.All (or User/Group/Application read) on your account. Without it, names
          stay as GUIDs. Like the ARM token, it’s short-lived (~1 hour).
        </div>
      </div>
    </div>
  );
}

const PROVIDER_DISPLAY: Record<string, string> = {
  openai: "OpenAI",
  github: "GitHub Models",
  github_copilot: "GitHub Copilot",
  ollama: "Ollama",
  chatgpt: "ChatGPT Codex",
  azure_openai: "Azure OpenAI",
  azure_foundry: "Azure Foundry",
  claude: "Claude",
  claude_oauth: "Claude OAuth",
  gemini: "Google Gemini",
  grok: "Grok",
  mistral: "Mistral",
  openrouter: "OpenRouter",
  lmstudio: "LM Studio",
};

// Required Microsoft Graph application permissions for the EntraID MCP server.
const ENTRA_GRAPH_PERMISSIONS: { name: string; desc: string }[] = [
  { name: "AuditLog.Read.All", desc: "Read all audit log data" },
  { name: "AuthenticationContext.Read.All", desc: "Read all authentication context information" },
  { name: "DeviceManagementManagedDevices.Read.All", desc: "Read Microsoft Intune devices" },
  { name: "Directory.Read.All", desc: "Read directory data" },
  { name: "Group.Read.All", desc: "Read all groups" },
  { name: "GroupMember.Read.All", desc: "Read all group memberships" },
  { name: "Group.ReadWrite.All", desc: "Create, update, delete groups; manage group members and owners" },
  { name: "Policy.Read.All", desc: "Read your organization's policies" },
  { name: "RoleManagement.Read.Directory", desc: "Read all directory RBAC settings" },
  { name: "User.Read.All", desc: "Read all users' full profiles" },
  { name: "User-PasswordProfile.ReadWrite.All", desc: "Least privileged permission to update the passwordProfile property" },
  { name: "UserAuthenticationMethod.Read.All", desc: "Read all users' authentication methods" },
  { name: "Application.ReadWrite.All", desc: "Create, update, and delete applications (app registrations) and service principals" },
];

/** Built-in first-party utility tools (web fetch + network diagnostics) with an admin
 *  kill-switch and per-tool enable/disable. All tools are read-only + SSRF-guarded. */
function BuiltinToolsCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["builtinTools"], queryFn: api.listBuiltinTools, retry: false });
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const enabled = q.data?.enabled ?? true;
  const disabled = new Set(q.data?.disabled ?? []);
  const allTools = q.data?.tools ?? [];

  async function save(body: Partial<AppSettings>) {
    setSaving(true);
    setError("");
    try {
      await api.updateAppSettings(body);
      qc.invalidateQueries({ queryKey: ["builtinTools"] });
      qc.invalidateQueries({ queryKey: ["appSettings"] });
    } catch (e) {
      setError(formatError(e));
    } finally {
      setSaving(false);
    }
  }

  function toggleTool(name: string, on: boolean) {
    const next = new Set(disabled);
    if (on) next.delete(name);
    else next.add(name);
    void save({ builtin_tools_disabled: [...next] });
  }

  return (
    <Card title="Built-in Utility Tools">
      <p className="mb-3 text-sm text-gray-500">
        First-party, read-only tools the agent can call directly — web page fetch, HTTP
        probe, DNS lookup, TCP port check, ping, and traceroute. All outbound requests are
        SSRF-guarded (private ranges and the cloud metadata endpoint are always blocked).
      </p>

      <div className="mb-4 flex items-start justify-between gap-4 rounded-lg border bg-gray-50 px-4 py-3">
        <div>
          <div className="text-sm font-medium text-gray-800">
            Enable built-in utility tools
          </div>
          <div className="text-xs text-gray-500">
            Kill-switch. When off, none of these tools are exposed to any agent (default or
            custom). On by default since they&apos;re read-only.
          </div>
        </div>
        <Toggle label="" checked={enabled} onChange={(v) => void save({ builtin_tools_enabled: v })} />
      </div>

      {saving && <p className="mb-2 text-xs text-gray-400">Saving…</p>}
      {error && (
        <div className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </div>
      )}
      {q.isLoading && (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-16 animate-pulse rounded border bg-gray-100" />
          ))}
        </div>
      )}

      <div className={`grid grid-cols-1 gap-2 sm:grid-cols-2 ${enabled ? "" : "opacity-50"}`}>
        {allTools.map((t) => {
          const off = disabled.has(t.name);
          return (
            <div key={t.name} className="rounded border bg-white p-3 text-sm">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono font-medium">{t.name}</span>
                <div className="flex items-center gap-2">
                  <span className="rounded bg-green-100 px-2 py-0.5 text-xs text-green-800">read</span>
                  <Toggle
                    label=""
                    checked={enabled && !off}
                    onChange={(v) => toggleTool(t.name, v)}
                  />
                </div>
              </div>
              <p className="mt-1 text-xs text-gray-500">{t.description}</p>
            </div>
          );
        })}
      </div>
      <p className="mt-3 text-[11px] text-gray-400">
        Egress allow/deny lists and the per-call timeout are configurable in app settings
        (network_egress_allowlist / network_egress_denylist / network_tool_timeout_seconds).
      </p>
    </Card>
  );
}

function EntraToolsCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["entraTools"], queryFn: api.listEntraTools, retry: false });
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const enabled = q.data?.enabled ?? false;
  const connectionConfigured = q.data?.connection_configured ?? false;
  const allTools = q.data?.tools ?? [];

  async function toggleEnabled(v: boolean) {
    setSaving(true);
    setError("");
    try {
      await api.updateAppSettings({ entra_mcp_enabled: v });
      qc.invalidateQueries({ queryKey: ["entraTools"] });
      qc.invalidateQueries({ queryKey: ["appSettings"] });
    } catch (e) {
      setError(formatError(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-5">
      <Card title="EntraID MCP Tools">
        <p className="mb-3 text-sm text-gray-500">
          Microsoft Graph tools for Entra ID (Azure AD): users, groups, app registrations
          &amp; service principals, secret/cert expiry, MFA, sign-in &amp; audit logs, and
          conditional-access policies. Runs as a local MCP server authenticated with the
          default Azure connection&apos;s service principal.
        </p>

        <div className="mb-4 flex items-start justify-between gap-4 rounded-lg border bg-gray-50 px-4 py-3">
          <div>
            <div className="text-sm font-medium text-gray-800">
              Expose EntraID tools to the default assistant
            </div>
            <div className="text-xs text-gray-500">
              When on, normal chats can answer directory questions live. Sub agents opt
              in separately via their &ldquo;EntraID&rdquo; tool checkbox.
            </div>
          </div>
          <Toggle label="" checked={enabled} onChange={(v) => void toggleEnabled(v)} />
        </div>
        {saving && <p className="mb-2 text-xs text-gray-400">Saving…</p>}
        {error && (
          <div className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            {error}
          </div>
        )}

        {!connectionConfigured && !q.isLoading && (
          <div className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
            No default Azure connection is configured. Add a service-principal connection
            (with the Graph permissions below) under Azure Tenants and set it as default.
          </div>
        )}

        {q.isError && (
          <p className="text-sm text-red-600">
            EntraID MCP server unavailable: {formatError(q.error)}
          </p>
        )}
        {q.isLoading && (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="h-16 animate-pulse rounded border bg-gray-100" />
            ))}
          </div>
        )}
        {!q.isLoading && !q.isError && allTools.length === 0 && (
          <p className="text-sm text-gray-500">No tools are currently exposed.</p>
        )}
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {allTools.map((t) => (
            <div key={t.name} className="rounded border bg-white p-3 text-sm">
              <div className="flex items-center justify-between">
                <span className="font-mono font-medium">{t.name}</span>
                <span
                  className={`rounded px-2 py-0.5 text-xs ${
                    t.kind === "write"
                      ? "bg-amber-100 text-amber-800"
                      : "bg-green-100 text-green-800"
                  }`}
                >
                  {t.kind}
                </span>
              </div>
              <p className="mt-1 text-xs text-gray-500">{t.description}</p>
            </div>
          ))}
        </div>
      </Card>

      <Card title="Required Microsoft Graph permissions">
        <p className="mb-3 text-sm text-gray-500">
          Grant these <span className="font-medium">Application</span> permissions to the
          app registration used by the connection, then grant admin consent. Read-only
          permissions are enough for most queries; the ReadWrite ones enable group,
          password, and application management.
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-gray-500">
              <tr className="border-b">
                <th className="py-1.5 pr-3 font-medium">Permission</th>
                <th className="py-1.5 pr-3 font-medium">Type</th>
                <th className="py-1.5 font-medium">Description</th>
              </tr>
            </thead>
            <tbody>
              {ENTRA_GRAPH_PERMISSIONS.map((p) => (
                <tr key={p.name} className="border-b last:border-0">
                  <td className="py-1.5 pr-3 font-mono text-gray-800">{p.name}</td>
                  <td className="py-1.5 pr-3 text-gray-500">Application</td>
                  <td className="py-1.5 text-gray-600">{p.desc}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function SiemExportCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["siemExport"], queryFn: api.siemExport });
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);

  const destinations = q.data?.destinations ?? [];

  async function addDestination() {
    setError("");
    setAdding(true);
    try {
      await api.addSiemDestination({
        name: `Destination ${destinations.length + 1}`,
        type: "splunk_hec",
        enabled: false,
      });
      qc.invalidateQueries({ queryKey: ["siemExport"] });
    } catch (e) {
      setError(formatError(e));
    } finally {
      setAdding(false);
    }
  }

  return (
    <section className="rounded-lg border bg-white p-4 shadow-sm">
      <div className="mb-1 flex items-center justify-between gap-2">
        <h2 className="font-medium">Continuous SIEM export</h2>
        <button
          onClick={() => void addDestination()}
          disabled={adding}
          className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-50"
        >
          {adding ? "Adding…" : "+ Add destination"}
        </button>
      </div>
      <p className="mb-3 text-xs text-gray-500">
        Continuously stream every audit-log entry to one or more SIEMs as it happens. Each
        destination forwards independently with its own durable cursor — exactly-once
        delivery and automatic catch-up after any outage.
      </p>

      {error && (
        <div className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </div>
      )}

      {q.isLoading ? (
        <p className="text-sm text-gray-400">Loading…</p>
      ) : destinations.length === 0 ? (
        <div className="rounded-lg border border-dashed bg-gray-50 px-4 py-6 text-center text-sm text-gray-500">
          No SIEM destinations yet. Add one to start streaming the audit log.
        </div>
      ) : (
        <div className="space-y-3">
          {destinations.map((d) => (
            <SiemDestinationRow key={d.id} dest={d} />
          ))}
        </div>
      )}
    </section>
  );
}

function SiemDestinationRow({ dest }: { dest: SiemDestination }) {
  const qc = useQueryClient();
  const [form, setForm] = useState<SiemDestination | null>(null);
  const [token, setToken] = useState("");
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"" | "save" | "test" | "flush" | "reset" | "delete" | "toggle">("");
  const [testResult, setTestResult] = useState<{ ok: boolean; error: string | null } | null>(null);
  const [flushMsg, setFlushMsg] = useState("");

  const cfg = form ?? dest;
  const dirty = form !== null || token.trim() !== "";

  function patch(p: Partial<SiemDestination>) {
    setForm({ ...cfg, ...p });
    setSaved(false);
    setTestResult(null);
    setFlushMsg("");
  }

  function body(): Record<string, unknown> {
    const b: Record<string, unknown> = {
      name: cfg.name,
      type: cfg.type,
      endpoint: cfg.endpoint,
      auth_header: cfg.auth_header,
      auth_scheme: cfg.auth_scheme,
      splunk_index: cfg.splunk_index,
      splunk_sourcetype: cfg.splunk_sourcetype,
      verify_tls: cfg.verify_tls,
      batch_size: cfg.batch_size,
    };
    if (token.trim()) b.token = token.trim();
    return b;
  }

  async function save() {
    setBusy("save");
    setError("");
    try {
      await api.updateSiemDestination(dest.id, body());
      setForm(null);
      setToken("");
      setSaved(true);
      setTimeout(() => setSaved(false), 1800);
      qc.invalidateQueries({ queryKey: ["siemExport"] });
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy("");
    }
  }

  async function toggleEnabled(v: boolean) {
    setBusy("toggle");
    setError("");
    try {
      await api.updateSiemDestination(dest.id, { enabled: v });
      qc.invalidateQueries({ queryKey: ["siemExport"] });
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy("");
    }
  }

  async function test() {
    setBusy("test");
    setTestResult(null);
    setError("");
    try {
      if (dirty) await api.updateSiemDestination(dest.id, body());
      const res = await api.testSiemDestination(dest.id);
      setTestResult(res);
      if (dirty) {
        setForm(null);
        setToken("");
        qc.invalidateQueries({ queryKey: ["siemExport"] });
      }
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy("");
    }
  }

  async function flush() {
    setBusy("flush");
    setFlushMsg("");
    setError("");
    try {
      const res = await api.flushSiemDestination(dest.id);
      setFlushMsg(
        res.error
          ? `Error: ${res.error}`
          : `Forwarded ${res.forwarded} event${res.forwarded === 1 ? "" : "s"}.`,
      );
      qc.invalidateQueries({ queryKey: ["siemExport"] });
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy("");
    }
  }

  async function resetCursor() {
    if (!window.confirm("Re-send the entire audit log to this destination from the beginning?")) return;
    setBusy("reset");
    setError("");
    try {
      await api.resetSiemCursor(dest.id);
      qc.invalidateQueries({ queryKey: ["siemExport"] });
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy("");
    }
  }

  async function remove() {
    if (!window.confirm(`Delete SIEM destination “${dest.name}”?`)) return;
    setBusy("delete");
    setError("");
    try {
      await api.deleteSiemDestination(dest.id);
      qc.invalidateQueries({ queryKey: ["siemExport"] });
    } catch (e) {
      setError(formatError(e));
    } finally {
      setBusy("");
    }
  }

  const isSplunk = cfg.type === "splunk_hec";
  const st = cfg.status;

  return (
    <div className="overflow-hidden rounded-lg border">
      {/* Header row */}
      <div className="flex items-center gap-3 px-3 py-2.5">
        <span className="text-base">{isSplunk ? "🟢" : "🌐"}</span>
        <button onClick={() => setOpen((o) => !o)} className="min-w-0 flex-1 text-left">
          <span className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-gray-800">{cfg.name}</span>
            <span
              className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                cfg.enabled ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500"
              }`}
            >
              {cfg.enabled ? "Streaming" : "Off"}
            </span>
            {st.last_error && (
              <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-medium text-red-700">
                error
              </span>
            )}
          </span>
          <span className="mt-0.5 block truncate text-[11px] text-gray-400">
            {isSplunk ? "Splunk HEC" : "HTTP / webhook"} · {cfg.endpoint || "not configured"} ·{" "}
            {st.forwarded_total.toLocaleString()} delivered
          </span>
        </button>
        <Toggle label="" checked={cfg.enabled} onChange={(v) => void toggleEnabled(v)} />
        <button
          onClick={() => setOpen((o) => !o)}
          className={`text-gray-400 transition-transform ${open ? "rotate-90" : ""}`}
          aria-hidden
        >
          ▸
        </button>
      </div>

      {open && (
        <div className="border-t bg-gray-50/40 px-3 py-3">
          {error && (
            <div className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </div>
          )}

          {/* Status strip */}
          <div className="mb-3 grid grid-cols-2 gap-2 rounded-lg bg-white p-3 text-xs sm:grid-cols-4">
            <div>
              <div className="text-gray-400">Delivered</div>
              <div className="font-medium text-gray-800">{st.forwarded_total.toLocaleString()}</div>
            </div>
            <div>
              <div className="text-gray-400">Last success</div>
              <div className="font-medium text-gray-800">
                {st.last_success_at ? new Date(ensureUtc(st.last_success_at)).toLocaleString() : "—"}
              </div>
            </div>
            <div>
              <div className="text-gray-400">Cursor</div>
              <div className="font-medium text-gray-800">
                {st.cursor_ts ? new Date(ensureUtc(st.cursor_ts)).toLocaleString() : "Start"}
              </div>
            </div>
            <div>
              <div className="text-gray-400">Health</div>
              <div className={`font-medium ${st.last_error ? "text-red-600" : "text-green-600"}`}>
                {st.last_error ? "Error" : "Healthy"}
              </div>
            </div>
          </div>
          {st.last_error && (
            <div className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
              Last delivery error: {st.last_error}
            </div>
          )}

          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="text-xs font-medium text-gray-600">Name</span>
                <input
                  value={cfg.name}
                  onChange={(e) => patch({ name: e.target.value })}
                  className="mt-1 w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                />
              </label>
              <label className="block">
                <span className="text-xs font-medium text-gray-600">Type</span>
                <select
                  value={cfg.type}
                  onChange={(e) => patch({ type: e.target.value })}
                  className="mt-1 w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                >
                  <option value="splunk_hec">Splunk (HTTP Event Collector)</option>
                  <option value="http">Generic HTTP / webhook (Sentinel, Elastic, Datadog…)</option>
                </select>
              </label>
            </div>

            <label className="block">
              <span className="text-xs font-medium text-gray-600">
                {isSplunk ? "HEC URL" : "Endpoint URL"}
              </span>
              <input
                value={cfg.endpoint}
                onChange={(e) => patch({ endpoint: e.target.value })}
                placeholder={isSplunk ? "https://splunk.contoso.com:8088" : "https://siem.example.com/ingest"}
                className="mt-1 w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
              />
            </label>

            <label className="block">
              <span className="text-xs font-medium text-gray-600">
                {isSplunk ? "HEC token" : "Secret / API key"}{" "}
                {cfg.token_set && <span className="text-gray-400">(saved — leave blank to keep)</span>}
              </span>
              <input
                // Masked TEXT input (not type=password) to block autofill clobbering the
                // saved token (blank = keep).
                type="text"
                style={{ WebkitTextSecurity: "disc" } as React.CSSProperties}
                name="siem-token"
                autoComplete="off"
                data-1p-ignore
                data-lpignore="true"
                data-form-type="other"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={cfg.token_set ? "••••••••" : isSplunk ? "HEC token" : "Bearer token or API key"}
                className="mt-1 w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
              />
            </label>

            {isSplunk ? (
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block">
                  <span className="text-xs font-medium text-gray-600">Index (optional)</span>
                  <input
                    value={cfg.splunk_index}
                    onChange={(e) => patch({ splunk_index: e.target.value })}
                    placeholder="main"
                    className="mt-1 w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                  />
                </label>
                <label className="block">
                  <span className="text-xs font-medium text-gray-600">Sourcetype</span>
                  <input
                    value={cfg.splunk_sourcetype}
                    onChange={(e) => patch({ splunk_sourcetype: e.target.value })}
                    placeholder="azsupagent:audit"
                    className="mt-1 w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                  />
                </label>
              </div>
            ) : (
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block">
                  <span className="text-xs font-medium text-gray-600">Auth header</span>
                  <input
                    value={cfg.auth_header}
                    onChange={(e) => patch({ auth_header: e.target.value })}
                    placeholder="Authorization"
                    className="mt-1 w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                  />
                </label>
                <label className="block">
                  <span className="text-xs font-medium text-gray-600">Auth scheme (optional)</span>
                  <input
                    value={cfg.auth_scheme}
                    onChange={(e) => patch({ auth_scheme: e.target.value })}
                    placeholder="Bearer"
                    className="mt-1 w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                  />
                </label>
              </div>
            )}

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="text-xs font-medium text-gray-600">Batch size</span>
                <input
                  type="number"
                  min={1}
                  max={1000}
                  value={cfg.batch_size}
                  onChange={(e) => patch({ batch_size: Number(e.target.value) || 1 })}
                  className="mt-1 w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                />
              </label>
              <div className="flex items-end">
                <Toggle
                  label="Verify TLS"
                  hint="Disable only for self-signed SIEM endpoints."
                  checked={cfg.verify_tls}
                  onChange={(v) => patch({ verify_tls: v })}
                />
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2 pt-1">
              <button
                onClick={() => void save()}
                disabled={busy !== "" || !dirty}
                className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-50"
              >
                {busy === "save" ? "Saving…" : "Save"}
              </button>
              <button
                onClick={() => void test()}
                disabled={busy !== "" || !cfg.endpoint}
                className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 transition hover:bg-gray-50 disabled:opacity-50"
              >
                {busy === "test" ? "Testing…" : "Send test event"}
              </button>
              <button
                onClick={() => void flush()}
                disabled={busy !== "" || !cfg.enabled}
                className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 transition hover:bg-gray-50 disabled:opacity-50"
                title="Deliver any pending audit rows right now"
              >
                {busy === "flush" ? "Flushing…" : "Flush now"}
              </button>
              <button
                onClick={() => void resetCursor()}
                disabled={busy !== ""}
                className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 transition hover:bg-gray-50 disabled:opacity-50"
                title="Re-send the entire audit log from the beginning"
              >
                Reset cursor
              </button>
              <button
                onClick={() => void remove()}
                disabled={busy !== ""}
                className="ml-auto rounded-lg border border-red-200 px-3 py-1.5 text-sm text-red-600 transition hover:bg-red-50 disabled:opacity-50"
              >
                {busy === "delete" ? "Deleting…" : "Delete"}
              </button>
              {saved && <span className="text-xs text-green-600">Saved ✓</span>}
              {testResult && (
                <span className={`text-xs ${testResult.ok ? "text-green-600" : "text-red-600"}`}>
                  {testResult.ok ? "Test event delivered ✓" : `Test failed: ${testResult.error}`}
                </span>
              )}
              {flushMsg && <span className="text-xs text-gray-600">{flushMsg}</span>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function DemoDataCard() {
  const qc = useQueryClient();
  const [busy, setBusy] = useState<"" | "seed" | "purge">("");
  const [confirmPurge, setConfirmPurge] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const statusQ = useQuery({ queryKey: ["demoStatus"], queryFn: api.demoStatus, retry: false });
  const loaded = statusQ.data?.loaded ?? false;

  async function doSeed() {
    setBusy("seed");
    setMsg(null);
    try {
      const r = await api.seedDemoData();
      const errs = Object.keys(r.errors || {});
      setMsg({
        text: `Demo data loaded — ${r.seeded?.length ?? 0} area(s)${errs.length ? `, ${errs.length} with issues: ${errs.join(", ")}` : ""}.`,
        ok: errs.length === 0,
      });
      await qc.invalidateQueries({ queryKey: ["demoStatus"] });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function doPurge() {
    setConfirmPurge(false);
    setBusy("purge");
    setMsg(null);
    try {
      const r = await api.purgeDemoData();
      const removed = Object.entries(r.removed || {}).filter(([, v]) => v).map(([k]) => k);
      setMsg({ text: `Demo data removed — cleared ${removed.length} area(s).`, ok: true });
      await qc.invalidateQueries({ queryKey: ["demoStatus"] });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  return (
    <Card title="🎬 Demo Data">
      <p className="mb-3 text-sm text-gray-600">
        Load a complete dummy dataset across every proactive screen — Monitoring / Telemetry /
        Backup-DR coverage, Performance Profiler, Retirement Radar, Telemetry Intelligence,
        Evidence Locker, DNS &amp; reachability, RBAC access, Reservations and Entra App
        Registrations — anchored to the demo workloads{" "}
        <b>“Contoso Hotels”</b>, <b>“Zava Shoes Website”</b>, and <b>“Zava Shoes CRM”</b>.
        Useful for demos and screenshots without a live Azure tenant. Removing it deletes{" "}
        <b>only</b> demo data — your real workloads, scans, and settings are never touched.
      </p>

      <div className="mb-3 flex items-center gap-2 text-xs">
        <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-medium ${loaded ? "bg-emerald-50 text-emerald-700" : "bg-gray-100 text-gray-500"}`}>
          <span className={`h-1.5 w-1.5 rounded-full ${loaded ? "bg-emerald-500" : "bg-gray-400"}`} />
          {statusQ.isLoading ? "Checking…" : loaded ? "Demo data loaded" : "No demo data"}
        </span>
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => void doSeed()}
          disabled={busy !== ""}
          className="rounded-lg bg-brand px-3.5 py-2 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-50"
        >
          {busy === "seed" ? "Loading…" : "Load demo data"}
        </button>
        <button
          onClick={() => setConfirmPurge(true)}
          disabled={busy !== ""}
          className="rounded-lg border border-red-300 bg-white px-3.5 py-2 text-sm font-medium text-red-700 transition hover:bg-red-50 disabled:opacity-50"
        >
          {busy === "purge" ? "Removing…" : "Remove demo data"}
        </button>
      </div>

      {msg && (
        <div className={`mt-3 rounded-lg border p-2 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-amber-200 bg-amber-50 text-amber-700"}`}>
          {msg.text}
        </div>
      )}

      <p className="mt-3 text-[11px] text-gray-400">
        Note: the demo architectures (DEMO1–DEMO4) are removed by “Remove demo data” and are not
        recreated by “Load”.
      </p>

      {confirmPurge && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" onClick={() => setConfirmPurge(false)}>
          <div className="w-full max-w-md rounded-xl bg-white p-5 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-base font-semibold text-gray-900">Remove all demo data?</h3>
            <p className="mt-2 text-sm text-gray-600">
              This removes only the demo dataset — the demo workload, its coverage/profile/radar
              snapshots, demo evidence &amp; diagnostics runs, and the DEMO architectures. Your
              real workloads, cached scans, and Settings configuration are <b>not</b> affected.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button onClick={() => setConfirmPurge(false)} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">
                Cancel
              </button>
              <button onClick={() => void doPurge()} className="rounded-lg bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700">
                Remove demo data
              </button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

function AuditCard() {
  const PAGE_SIZE = 25;
  const [page, setPage] = useState(0);
  const [exporting, setExporting] = useState<"csv" | "json" | null>(null);
  const [exportError, setExportError] = useState("");
  const q = useQuery({
    queryKey: ["audit", page],
    queryFn: () => api.audit(PAGE_SIZE, page * PAGE_SIZE),
    placeholderData: (prev) => prev,
  });

  const total = q.data?.total ?? 0;
  const items = q.data?.items ?? [];
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const startIdx = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const endIdx = Math.min(total, (page + 1) * PAGE_SIZE);

  async function exportAll(format: "csv" | "json") {
    if (exporting) return;
    setExporting(format);
    setExportError("");
    try {
      // Pull the full log across pages (API max is 200 per call).
      const all: AuditEntry[] = [];
      let offset = 0;
      while (offset < total && all.length < 5000) {
        const res = await api.audit(200, offset);
        all.push(...res.items);
        offset += 200;
        if (res.items.length === 0) break;
      }
      let blob: Blob;
      if (format === "json") {
        blob = new Blob([JSON.stringify(all, null, 2)], { type: "application/json" });
      } else {
        const header = ["timestamp", "actor", "action", "target", "provider", "model"];
        const rows = all.map((a) =>
          [
            new Date(ensureUtc(a.created_at)).toISOString(),
            a.actor_id,
            a.action,
            a.target ?? "",
            a.provider ?? "",
            a.model ?? "",
          ]
            .map((v) => `"${String(v).replace(/"/g, '""')}"`)
            .join(","),
        );
        blob = new Blob([[header.join(","), ...rows].join("\n")], { type: "text/csv" });
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `audit-log.${format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setExportError(`Export failed: ${formatError(e)}`);
    } finally {
      setExporting(null);
    }
  }

  return (
    <div className="space-y-4">
      <SiemExportCard />
      <section className="rounded-lg border bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="font-medium">Audit Log</h2>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-400">
            {total.toLocaleString()} {total === 1 ? "entry" : "entries"}
          </span>
          <button
            onClick={() => void exportAll("csv")}
            disabled={exporting !== null || total === 0}
            className="rounded-md border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50"
          >
            {exporting === "csv" ? "Exporting…" : "⬇ CSV"}
          </button>
          <button
            onClick={() => void exportAll("json")}
            disabled={exporting !== null || total === 0}
            className="rounded-md border px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50"
          >
            {exporting === "json" ? "Exporting…" : "⬇ JSON"}
          </button>
        </div>
      </div>

      {exportError && (
        <div className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {exportError}
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-left text-gray-500">
            <tr className="border-b">
              <th className="py-1.5 pr-3 font-medium">Time</th>
              <th className="py-1.5 pr-3 font-medium">Action</th>
              <th className="py-1.5 pr-3 font-medium">Target</th>
              <th className="py-1.5 pr-3 font-medium">Provider</th>
              <th className="py-1.5 font-medium">Model</th>
            </tr>
          </thead>
          <tbody>
            {items.map((a) => (
              <tr key={a.id} className="border-b last:border-0 hover:bg-gray-50">
                <td className="whitespace-nowrap py-1.5 pr-3 text-gray-400">
                  {new Date(ensureUtc(a.created_at)).toLocaleString()}
                </td>
                <td className="py-1.5 pr-3 font-mono text-gray-700">{a.action}</td>
                <td
                  className="max-w-[200px] truncate py-1.5 pr-3 text-gray-600"
                  title={a.target ?? ""}
                >
                  {a.target ?? "—"}
                </td>
                <td className="py-1.5 pr-3 text-gray-600">
                  {a.provider ? PROVIDER_DISPLAY[a.provider] ?? a.provider : "—"}
                </td>
                <td className="py-1.5 font-mono text-gray-500">{a.model ?? "—"}</td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={5} className="py-6 text-center text-gray-400">
                  {q.isLoading ? "Loading…" : "No audit entries."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="mt-3 flex items-center justify-between text-xs text-gray-500">
        <span>
          {startIdx}–{endIdx} of {total.toLocaleString()}
        </span>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setPage(0)}
            disabled={page === 0}
            className="rounded border px-2 py-1 hover:bg-gray-50 disabled:opacity-40"
          >
            « First
          </button>
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="rounded border px-2 py-1 hover:bg-gray-50 disabled:opacity-40"
          >
            ‹ Prev
          </button>
          <span className="px-2">
            Page {page + 1} / {pageCount}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
            disabled={page >= pageCount - 1}
            className="rounded border px-2 py-1 hover:bg-gray-50 disabled:opacity-40"
          >
            Next ›
          </button>
          <button
            onClick={() => setPage(pageCount - 1)}
            disabled={page >= pageCount - 1}
            className="rounded border px-2 py-1 hover:bg-gray-50 disabled:opacity-40"
          >
            Last »
          </button>
        </div>
      </div>
      </section>
    </div>
  );
}

// ===================================================================== Sandbox Troubleshooting VMs

type VmForm = {
  id?: string;
  display_name: string;
  host: string;
  port: number;
  username: string;
  auth_method: string;
  strict_mode: boolean;
  disabled: boolean;
  allow_sudo: boolean;
  workload_ids: string[];
  vnet_label: string;
  ssh_private_key: string;
  ssh_passphrase: string;
  ssh_password: string;
};

const EMPTY_VM: VmForm = {
  display_name: "",
  host: "",
  port: 22,
  username: "",
  auth_method: "ssh_password",
  strict_mode: false,
  disabled: false,
  allow_sudo: true,
  workload_ids: [],
  vnet_label: "",
  ssh_private_key: "",
  ssh_passphrase: "",
  ssh_password: "",
};

function SandboxVmsCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["sandboxVms"], queryFn: api.sandboxVms });
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const [editing, setEditing] = useState<VmForm | null>(null);
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");
  const [testMsg, setTestMsg] = useState<Record<string, { ok: boolean; text: string; caps?: string[]; pm?: string }>>({});
  const [runFor, setRunFor] = useState<string | null>(null);

  const vms = q.data?.vms ?? [];
  const workloads: Workload[] = wlQ.data?.workloads ?? [];
  const wlName = (id: string) => workloads.find((w) => w.id === id)?.name ?? id;
  // Only keep linked workload ids that still exist — a deleted workload would otherwise
  // render as a raw UUID chip. Wait until the workloads list has loaded before filtering
  // so we don't hide everything during the initial fetch.
  const existingWorkloads = (ids: string[]) =>
    wlQ.isSuccess ? ids.filter((id) => workloads.some((w) => w.id === id)) : ids;

  function startAdd() { setError(""); setEditing({ ...EMPTY_VM }); }
  function startEdit(v: SandboxVm) {
    setError("");
    setEditing({
      id: v.id,
      display_name: v.display_name,
      host: v.host,
      port: v.port,
      username: v.username,
      auth_method: v.auth_method,
      strict_mode: v.strict_mode,
      disabled: v.disabled,
      allow_sudo: v.allow_sudo,
      workload_ids: [...v.workload_ids],
      vnet_label: v.vnet_label,
      ssh_private_key: "",
      ssh_passphrase: "",
      ssh_password: "",
    });
  }

  async function save() {
    if (!editing) return;
    if (!editing.display_name.trim() || !editing.host.trim() || !editing.username.trim()) {
      setError("Name, host, and username are required.");
      return;
    }
    try {
      await api.upsertSandboxVm({
        id: editing.id,
        display_name: editing.display_name.trim(),
        host: editing.host.trim(),
        port: Number(editing.port) || 22,
        username: editing.username.trim(),
        auth_method: editing.auth_method,
        strict_mode: editing.strict_mode,
        disabled: editing.disabled,
        allow_sudo: editing.allow_sudo,
        workload_ids: editing.workload_ids,
        vnet_label: editing.vnet_label.trim(),
        ...(editing.ssh_private_key.trim() ? { ssh_private_key: editing.ssh_private_key } : {}),
        ...(editing.ssh_passphrase.trim() ? { ssh_passphrase: editing.ssh_passphrase } : {}),
        ...(editing.ssh_password.trim() ? { ssh_password: editing.ssh_password } : {}),
      });
      setEditing(null);
      qc.invalidateQueries({ queryKey: ["sandboxVms"] });
    } catch (e) { setError(formatError(e)); }
  }

  async function remove(id: string) {
    if (!window.confirm("Remove this sandbox VM?")) return;
    try { await api.deleteSandboxVm(id); qc.invalidateQueries({ queryKey: ["sandboxVms"] }); }
    catch (e) { setError(formatError(e)); }
  }

  async function test(id: string) {
    setBusyId(id);
    setTestMsg((m) => { const n = { ...m }; delete n[id]; return n; });
    try {
      const r = await api.testSandboxVm(id);
      if (r.ok) setTestMsg((m) => ({ ...m, [id]: { ok: true, text: `✓ ${r.whoami}@${r.os_info}`, caps: r.capabilities, pm: r.pkg_manager ? `${r.pkg_manager}${r.can_sudo ? (r.sudo_mode === "password" ? " + sudo(pw)" : " + sudo") : " (no sudo)"}` : "" } }));
      else setTestMsg((m) => ({ ...m, [id]: { ok: false, text: r.detail || "Connection failed." } }));
      qc.invalidateQueries({ queryKey: ["sandboxVms"] });
    } catch (e) {
      setTestMsg((m) => ({ ...m, [id]: { ok: false, text: formatError(e) } }));
    } finally { setBusyId(""); }
  }

  return (
    <Card title="Sandbox VMs">
      <p className="-mt-2 mb-4 text-sm text-gray-500">
        Onboard dedicated sandbox VMs (SSH) that sit inside a workload's network. The agent runs
        diagnostic commands on them via <code className="rounded bg-gray-100 px-1">vm_exec</code> to reach
        private endpoints — in normal and deep chat. Link a VM to a workload to make it available
        when that workload is selected.
      </p>

      {error && <div className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-600">{error}</div>}

      {!editing && (
        <button onClick={startAdd} className="mb-4 rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90">
          + Onboard a VM
        </button>
      )}

      {editing && (
        <div className="mb-4 space-y-3 rounded-xl border border-gray-200 bg-gray-50 p-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-gray-600">Display name</span>
              <input value={editing.display_name} onChange={(e) => setEditing({ ...editing, display_name: e.target.value })} placeholder="prod-probe-1" className="w-full rounded-lg border px-3 py-2 text-sm" />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-gray-600">VNet / network label</span>
              <input value={editing.vnet_label} onChange={(e) => setEditing({ ...editing, vnet_label: e.target.value })} placeholder="prod-vnet" className="w-full rounded-lg border px-3 py-2 text-sm" />
            </label>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <label className="block sm:col-span-2">
              <span className="mb-1 block text-xs font-medium text-gray-600">Host</span>
              <input value={editing.host} onChange={(e) => setEditing({ ...editing, host: e.target.value })} placeholder="10.0.0.4 or vm.example.com" className="w-full rounded-lg border px-3 py-2 text-sm" />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-gray-600">Port</span>
              <input type="number" value={editing.port} onChange={(e) => setEditing({ ...editing, port: Number(e.target.value) })} className="w-full rounded-lg border px-3 py-2 text-sm" />
            </label>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-gray-600">Username</span>
              <input value={editing.username} onChange={(e) => setEditing({ ...editing, username: e.target.value })} placeholder="ubuntu" className="w-full rounded-lg border px-3 py-2 text-sm" />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-gray-600">Auth method</span>
              <select value={editing.auth_method} onChange={(e) => setEditing({ ...editing, auth_method: e.target.value })} className="w-full rounded-lg border px-3 py-2 text-sm">
                <option value="ssh_password">Password</option>
                <option value="ssh_key">SSH private key</option>
              </select>
            </label>
          </div>
          {editing.auth_method === "ssh_password" ? (
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-gray-600">Password</span>
              <input
                type="text"
                style={{ WebkitTextSecurity: "disc" } as React.CSSProperties}
                name="vm-ssh-password" autoComplete="off" data-1p-ignore data-lpignore="true" data-form-type="other"
                value={editing.ssh_password}
                onChange={(e) => setEditing({ ...editing, ssh_password: e.target.value })}
                placeholder={editing.id ? "•••• (leave blank to keep)" : "SSH password"}
                className="w-full rounded-lg border px-3 py-2 text-sm"
              />
            </label>
          ) : (
            <>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-gray-600">SSH private key (PEM)</span>
                <textarea
                  style={{ WebkitTextSecurity: "disc" } as React.CSSProperties}
                  name="vm-ssh-key" autoComplete="off" data-1p-ignore data-lpignore="true" data-form-type="other"
                  value={editing.ssh_private_key}
                  onChange={(e) => setEditing({ ...editing, ssh_private_key: e.target.value })}
                  rows={4}
                  placeholder={editing.id ? "•••• (leave blank to keep)" : "-----BEGIN OPENSSH PRIVATE KEY-----"}
                  className="w-full rounded-lg border px-3 py-2 font-mono text-xs"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-gray-600">Key passphrase (optional)</span>
                <input
                  type="text"
                  style={{ WebkitTextSecurity: "disc" } as React.CSSProperties}
                  name="vm-ssh-passphrase" autoComplete="off" data-1p-ignore data-lpignore="true" data-form-type="other"
                  value={editing.ssh_passphrase}
                  onChange={(e) => setEditing({ ...editing, ssh_passphrase: e.target.value })}
                  placeholder={editing.id ? "•••• (leave blank to keep)" : "passphrase"}
                  className="w-full rounded-lg border px-3 py-2 text-sm"
                />
              </label>
            </>
          )}
          <div>
            <span className="mb-1 block text-xs font-medium text-gray-600">Linked workloads (VNet reach)</span>
            <div className="flex max-h-32 flex-wrap gap-2 overflow-auto rounded-lg border bg-white p-2">
              {workloads.length === 0 && <span className="text-xs text-gray-400">No workloads yet.</span>}
              {workloads.map((w) => {
                const on = editing.workload_ids.includes(w.id);
                return (
                  <button key={w.id}
                    onClick={() => setEditing({ ...editing, workload_ids: on ? editing.workload_ids.filter((x) => x !== w.id) : [...editing.workload_ids, w.id] })}
                    className={`rounded-full px-2.5 py-1 text-xs font-medium ${on ? "bg-brand text-white" : "bg-gray-100 text-gray-600 hover:bg-gray-200"}`}>
                    {on ? "✓ " : ""}{w.name}
                  </button>
                );
              })}
            </div>
          </div>
          <Toggle label="Strict mode" hint="Require operator approval before mutating commands run (off = autonomous sandbox)." checked={editing.strict_mode} onChange={(v) => setEditing({ ...editing, strict_mode: v })} />
          <Toggle label="Allow sudo" hint="Let the agent use sudo on this VM (e.g. to auto-install missing diagnostic tools). Off = never elevate." checked={editing.allow_sudo} onChange={(v) => setEditing({ ...editing, allow_sudo: v })} />
          <Toggle label="Disabled" hint="Hide this VM from the agent without deleting it." checked={editing.disabled} onChange={(v) => setEditing({ ...editing, disabled: v })} />
          <div className="flex gap-2">
            <button onClick={() => void save()} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white hover:bg-brand/90">Save</button>
            <button onClick={() => setEditing(null)} className="rounded-lg border px-4 py-1.5 text-sm text-gray-600 hover:bg-gray-50">Cancel</button>
          </div>
        </div>
      )}

      <div className="space-y-2">
        {vms.length === 0 && !editing && <p className="text-sm text-gray-400">No sandbox VMs onboarded yet.</p>}
        {vms.map((v) => {
          const tm = testMsg[v.id];
          const dot = v.status === "ok" ? "bg-green-500" : v.status === "error" ? "bg-red-500" : "bg-gray-300";
          return (
            <div key={v.id} className="rounded-xl border border-gray-200 bg-white p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className={`h-2 w-2 rounded-full ${dot}`} title={v.status_detail || v.status} />
                <span className="font-medium text-gray-800">{v.display_name}</span>
                <span className="text-xs text-gray-400">{v.username}@{v.host}:{v.port}</span>
                {v.strict_mode && <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">strict</span>}
                {v.disabled && <span className="rounded-full bg-gray-200 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">disabled</span>}
                {v.os_info && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">{v.os_info}</span>}
                {v.last_tested && (
                  !v.allow_sudo
                    ? <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-500" title="Sudo is disabled for this VM by an operator — the agent will not elevate or auto-install tools.">sudo off</span>
                    : v.can_sudo
                    ? <span className="rounded-full bg-green-100 px-1.5 py-0.5 text-[10px] font-medium text-green-700" title={v.sudo_mode === "password" ? "This VM/user can sudo using its SSH login password — the agent can auto-install missing tools." : "This VM/user has passwordless sudo — the agent can auto-install missing tools."}>🔑 sudo{v.sudo_mode === "password" ? " (pw)" : v.sudo_mode === "passwordless" ? " (nopasswd)" : ""}</span>
                    : <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-500" title="No usable sudo for this VM/user — the agent cannot auto-install missing tools.">no sudo</span>
                )}
                {v.pkg_manager && <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-700" title="Package manager detected on the VM.">pkg: {v.pkg_manager}</span>}
                <span className="ml-auto flex gap-1">
                  <button onClick={() => void test(v.id)} disabled={busyId === v.id} className="rounded-lg border px-2.5 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">{busyId === v.id ? "Testing…" : "Test"}</button>
                  <button onClick={() => setRunFor(runFor === v.id ? null : v.id)} className="rounded-lg border px-2.5 py-1 text-xs text-gray-700 hover:bg-gray-50">Run</button>
                  <button onClick={() => startEdit(v)} className="rounded-lg border px-2.5 py-1 text-xs text-gray-700 hover:bg-gray-50">Edit</button>
                  <button onClick={() => void remove(v.id)} className="rounded-lg border border-red-200 px-2.5 py-1 text-xs text-red-600 hover:bg-red-50">Delete</button>
                </span>
              </div>
              {(() => {
                const linked = existingWorkloads(v.workload_ids);
                return linked.length > 0 ? (
                  <div className="mt-1.5 flex flex-wrap gap-1 text-[11px] text-gray-500">
                    {linked.map((wid) => <span key={wid} className="rounded bg-violet-50 px-1.5 py-0.5 text-violet-700">🧩 {wlName(wid)}</span>)}
                  </div>
                ) : null;
              })()}
              {tm && (
                <div className={`mt-1.5 text-xs ${tm.ok ? "text-green-600" : "text-red-600"}`}>
                  {tm.text}{tm.pm ? <span className="ml-1 rounded bg-blue-50 px-1.5 py-0.5 text-[10px] text-blue-700">pkg: {tm.pm}</span> : null}
                  {tm.caps && tm.caps.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {tm.caps.map((c) => <span key={c} className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">{c}</span>)}
                    </div>
                  )}
                </div>
              )}
              {runFor === v.id && <VmRunConsole vmId={v.id} />}
            </div>
          );
        })}
      </div>
    </Card>
  );
}

/** Inline ad-hoc command console for a sandbox VM (admin debugging) + recent history. */
function VmRunConsole({ vmId }: { vmId: string }) {
  const [cmd, setCmd] = useState("");
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<string>("");
  const runsQ = useQuery({ queryKey: ["sandboxVmRuns", vmId], queryFn: () => api.sandboxVmRuns(vmId) });

  async function run() {
    if (!cmd.trim()) return;
    setBusy(true); setOut("");
    try {
      const r = await api.runSandboxVm(vmId, cmd.trim());
      if (r.needs_approval) setOut("⚠ Blocked — this command needs approval (strict mode).");
      else setOut(`exit ${r.exit_code}\n${r.stdout}${r.stderr ? "\n[stderr]\n" + r.stderr : ""}${r.error ? "\n[error] " + r.error : ""}`);
      runsQ.refetch();
    } catch (e) { setOut(formatError(e)); }
    finally { setBusy(false); }
  }

  const runs: SandboxVmRun[] = runsQ.data?.runs ?? [];

  return (
    <div className="mt-2 rounded-lg border border-gray-200 bg-gray-50 p-2">
      <div className="flex gap-2">
        <input value={cmd} onChange={(e) => setCmd(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void run()} placeholder="e.g. dig +short example.com" className="flex-1 rounded-lg border px-2.5 py-1.5 font-mono text-xs" />
        <button onClick={() => void run()} disabled={busy} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50">{busy ? "Running…" : "Run"}</button>
      </div>
      {out && <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-gray-900 p-2 text-[11px] text-gray-100">{out}</pre>}
      {runs.length > 0 && (
        <div className="mt-2">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">Recent runs</div>
          <div className="max-h-40 space-y-1 overflow-auto">
            {runs.slice(0, 10).map((r) => (
              <div key={r.id} className="flex items-center gap-2 text-[11px]">
                <span className={`rounded px-1 py-0.5 ${r.status === "succeeded" ? "bg-green-100 text-green-700" : r.status === "blocked" ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-700"}`}>{r.status}</span>
                <code className="truncate text-gray-700">{r.command}</code>
                <span className="ml-auto text-gray-400">{r.trigger}{r.duration_ms != null ? ` · ${r.duration_ms}ms` : ""}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Toggle({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-2">
      <div>
        <div className="text-sm font-medium text-gray-800">{label}</div>
        {hint && <div className="text-xs text-gray-500">{hint}</div>}
      </div>
      <button
        onClick={() => onChange(!checked)}
        className={`relative h-6 w-11 shrink-0 rounded-full transition ${
          checked ? "bg-brand" : "bg-gray-300"
        }`}
        role="switch"
        aria-checked={checked}
      >
        <span
          className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition ${
            checked ? "left-[22px]" : "left-0.5"
          }`}
        />
      </button>
    </div>
  );
}

function NumberField({
  label,
  hint,
  value,
  min,
  max,
  step,
  suffix,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  step: number;
  suffix?: string;
  onChange: (v: number) => void;
}) {
  return (
    <div>
      <div className="flex items-center justify-between gap-4">
        <label className="text-sm font-medium text-gray-800">{label}</label>
        <div className="flex items-center gap-1.5">
          <input
            type="number"
            value={value}
            min={min}
            max={max}
            step={step}
            onChange={(e) => onChange(Number(e.target.value))}
            className="w-28 rounded-lg border px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
          />
          {suffix && <span className="text-xs text-gray-400">{suffix}</span>}
        </div>
      </div>
      {hint && <div className="mt-1 text-xs text-gray-500">{hint}</div>}
      <div className="mt-1 text-[11px] text-gray-400">
        Range: {min.toLocaleString()}–{max.toLocaleString()}
        {suffix ? ` ${suffix}` : ""}
      </div>
    </div>
  );
}

// Per-group presentation: an icon, a one-line blurb, and where/when the prompts in
// that group actually run. Keyed by the group name the backend returns.
const PROMPT_GROUP_META: Record<string, { icon: string; blurb: string; when: string }> = {
  "Chat Agent": {
    icon: "💬",
    blurb: "The core assistant used on every chat turn — its persona, tone, and helper prompts.",
    when: "Runs whenever you chat, when a chat is titled, and when follow-up/starter chips are generated.",
  },
  "Deep Investigation": {
    icon: "🔬",
    blurb: "The multi-phase root-cause engine: gather evidence, test hypotheses, then conclude.",
    when: "Runs when you start a Deep Investigation from a chat.",
  },
  "Azure Workloads (Autopilot)": {
    icon: "🧭",
    blurb: "How Autopilot groups raw Azure resources into meaningful workloads.",
    when: "Runs when you discover or refresh workloads under Azure Workloads.",
  },
  "Workbooks (AI'fication)": {
    icon: "📓",
    blurb: "How a workbook's raw command output is summarized, scored, and structured.",
    when: "Runs when a workbook with AI summarization is executed.",
  },
  "AI Agent Builder": {
    icon: "🛠️",
    blurb: "How the 'Generate with AI' wizard interviews you and writes sub agents.",
    when: "Runs in the custom-agent builder when designing or enhancing an agent.",
  },
};

function AiPromptsCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["aiPrompts"], queryFn: api.aiPrompts });
  // Local edits keyed by prompt id; only changed prompts are saved.
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [savedId, setSavedId] = useState("");
  const [error, setError] = useState("");
  // Which group panels are expanded. Everything starts collapsed so the page is short
  // and scannable; the user opens only the area they want to tune.
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});

  const prompts = q.data?.prompts ?? [];
  const groups = Array.from(new Set(prompts.map((p) => p.group)));

  function valueOf(p: AiPrompt): string {
    return edits[p.id] !== undefined ? edits[p.id] : p.current;
  }
  function dirty(p: AiPrompt): boolean {
    return edits[p.id] !== undefined && edits[p.id] !== p.current;
  }

  async function save(p: AiPrompt) {
    setError("");
    try {
      await api.updateAiPrompts({ [p.id]: valueOf(p) });
      setEdits((e) => {
        const n = { ...e };
        delete n[p.id];
        return n;
      });
      setSavedId(p.id);
      setTimeout(() => setSavedId(""), 1800);
      qc.invalidateQueries({ queryKey: ["aiPrompts"] });
    } catch (e) {
      setError(formatError(e));
    }
  }

  async function restore(p: AiPrompt) {
    setError("");
    // Show the shipped default immediately for instant feedback…
    setEdits((e) => ({ ...e, [p.id]: p.default }));
    try {
      // …then clear any persisted override on the server (back to default).
      await api.resetAiPrompt(p.id);
      setEdits((e) => {
        const n = { ...e };
        delete n[p.id];
        return n;
      });
      setSavedId(p.id);
      setTimeout(() => setSavedId(""), 1800);
      qc.invalidateQueries({ queryKey: ["aiPrompts"] });
    } catch (e) {
      setError(formatError(e));
    }
  }

  if (q.isLoading) {
    return (
      <Card title="System Prompts">
        <p className="text-sm text-gray-400">Loading…</p>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-gray-800">System Prompts</h1>
        <p className="mt-1 text-sm text-gray-500">
          These are the instructions sent to the AI behind the scenes, grouped by the
          feature that uses them. Expand a section to tune its guidance. The strict output
          format is locked and appended automatically, so your edits can&apos;t break a
          feature — use <span className="font-medium">Restore original</span> to revert to
          what the app shipped with.
        </p>
        <div className="mt-3 flex items-center gap-2">
          <button
            onClick={() => setOpenGroups(Object.fromEntries(groups.map((g) => [g, true])))}
            className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 transition hover:bg-gray-50"
          >
            Expand all
          </button>
          <button
            onClick={() => setOpenGroups({})}
            className="rounded-lg border px-2.5 py-1 text-xs text-gray-600 transition hover:bg-gray-50"
          >
            Collapse all
          </button>
        </div>
      </div>

      {error && <div className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

      {groups.map((group) => {
        const groupPrompts = prompts.filter((p) => p.group === group);
        const meta = PROMPT_GROUP_META[group];
        const modifiedCount = groupPrompts.filter((p) => p.is_overridden).length;
        const open = !!openGroups[group];
        return (
          <section key={group} className="overflow-hidden rounded-lg border bg-white shadow-sm">
            <button
              onClick={() => setOpenGroups((o) => ({ ...o, [group]: !o[group] }))}
              className="flex w-full items-start gap-3 px-4 py-3 text-left transition hover:bg-gray-50"
            >
              <span className="mt-0.5 text-lg leading-none">{meta?.icon ?? "📝"}</span>
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-gray-800">{group}</span>
                  <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500">
                    {groupPrompts.length} {groupPrompts.length === 1 ? "prompt" : "prompts"}
                  </span>
                  {modifiedCount > 0 && (
                    <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-700">
                      {modifiedCount} modified
                    </span>
                  )}
                </span>
                {meta && <span className="mt-0.5 block text-xs text-gray-500">{meta.blurb}</span>}
              </span>
              <span
                className={`mt-1 shrink-0 text-gray-400 transition-transform ${open ? "rotate-90" : ""}`}
                aria-hidden
              >
                ▸
              </span>
            </button>

            {open && (
              <div className="border-t bg-gray-50/40 px-4 py-4">
                {meta && (
                  <p className="mb-4 rounded-lg bg-blue-50 px-3 py-2 text-xs text-blue-700">
                    <span className="font-medium">When this runs:</span> {meta.when}
                  </p>
                )}
                <div className="space-y-5">
                  {groupPrompts.map((p) => (
                    <div key={p.id} className="rounded-lg border bg-white p-3 shadow-sm">
                      <div className="flex items-center justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-medium text-gray-800">{p.label}</span>
                            {p.is_overridden && (
                              <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
                                modified
                              </span>
                            )}
                            {p.kind === "list" && (
                              <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">list</span>
                            )}
                          </div>
                          <p className="mt-0.5 text-xs text-gray-500">{p.description}</p>
                        </div>
                        {savedId === p.id && <span className="shrink-0 text-xs text-green-600">Saved ✓</span>}
                      </div>
                      <textarea
                        className="mt-2 w-full rounded-lg border px-3 py-2 text-[13px] leading-relaxed focus:outline-none focus:ring-2 focus:ring-brand"
                        rows={p.kind === "list" ? 3 : 7}
                        value={valueOf(p)}
                        onChange={(e) => setEdits((ed) => ({ ...ed, [p.id]: e.target.value }))}
                        spellCheck={false}
                      />
                      {p.contract && p.kind !== "list" && (
                        <details className="mt-1 text-[11px] text-gray-400">
                          <summary className="cursor-pointer hover:text-gray-600">
                            Locked output format (appended automatically)
                          </summary>
                          <pre className="mt-1 whitespace-pre-wrap rounded bg-gray-50 p-2 font-mono text-[11px] text-gray-500">
                            {p.contract}
                          </pre>
                        </details>
                      )}
                      <div className="mt-2 flex items-center gap-2">
                        <button
                          onClick={() => void save(p)}
                          disabled={!dirty(p)}
                          className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-50"
                        >
                          Save
                        </button>
                        <button
                          onClick={() => void restore(p)}
                          title="Reset this prompt to the text the app shipped with"
                          className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 transition hover:bg-gray-50"
                        >
                          Restore original
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

const SEVERITY_ROWS: { id: string; label: string; color: string; hint: string }[] = [
  { id: "critical", label: "Critical", color: "#dc2626", hint: "Most severe — e.g. public exposure, no encryption at rest" },
  { id: "error", label: "Error", color: "#ea580c", hint: "High-impact misconfigurations" },
  { id: "warning", label: "Warning", color: "#d97706", hint: "Recommended best practices" },
  { id: "info", label: "Informational", color: "#0284c7", hint: "Minor / advisory findings" },
];

function ScoringTaxonomyCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["appSettings"], queryFn: api.appSettings });
  const catQ = useQuery({ queryKey: ["architectureCatalog"], queryFn: api.architectureCatalog });
  const [form, setForm] = useState<AppSettings | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (q.data) setForm(q.data.settings);
  }, [q.data]);

  if (!form) {
    return (
      <Card title="Assessments & Architecture">
        <p className="text-sm text-gray-400">Loading…</p>
      </Card>
    );
  }

  const set = (patch: Partial<AppSettings>) => setForm((f) => (f ? { ...f, ...patch } : f));
  const weights = form.assessment_severity_weights ?? {};
  const setWeight = (sev: string, v: number) =>
    set({ assessment_severity_weights: { ...weights, [sev]: Math.max(0, Math.min(100, v || 0)) } });
  const colors = form.architecture_category_colors ?? {};
  const categories: ArchitectureCategory[] = catQ.data?.categories ?? [];
  const setColor = (cid: string, hex: string) =>
    set({ architecture_category_colors: { ...colors, [cid]: hex } });
  const resetColor = (cid: string) => {
    const next = { ...colors };
    delete next[cid];
    set({ architecture_category_colors: next });
  };
  // Workload Health Score weights (the composite blend on the Workloads command center).
  const WL_SIGNALS: { id: string; label: string }[] = [
    { id: "monitoring", label: "Monitoring" }, { id: "telemetry", label: "Telemetry" },
    { id: "backupdr", label: "Backup / DR" }, { id: "performance", label: "Performance" },
    { id: "ownership", label: "Ownership" }, { id: "policy", label: "Policy" }, { id: "tags", label: "Tags" },
  ];
  const wlWeights = form.workload_health_weights ?? {};
  const setWlWeight = (sig: string, v: number) =>
    set({ workload_health_weights: { ...wlWeights, [sig]: Math.max(0, v || 0) } });
  const wlMax = Math.max(0.1, ...WL_SIGNALS.map((s) => Number(wlWeights[s.id] ?? 1)));
  const maxWeight = Math.max(1, ...SEVERITY_ROWS.map((r) => Number(weights[r.id] ?? 0)));
  const good = form.assessment_score_good ?? 80;
  const warn = form.assessment_score_warn ?? 50;

  async function save() {
    if (!form) return;
    setError("");
    try {
      await api.updateAppSettings({
        assessment_severity_weights: form.assessment_severity_weights,
        assessment_score_good: form.assessment_score_good,
        assessment_score_warn: form.assessment_score_warn,
        architecture_category_colors: form.architecture_category_colors,
        workload_health_weights: form.workload_health_weights,
        workload_nightly_refresh: form.workload_nightly_refresh,
        policy_exemption_require_justification: form.policy_exemption_require_justification,
        policy_exemption_max_expiry_days: form.policy_exemption_max_expiry_days,
        policy_exemption_block_never_expires: form.policy_exemption_block_never_expires,
        changeexplorer_resolve_identities: form.changeexplorer_resolve_identities,
        changeexplorer_change_limit: form.changeexplorer_change_limit,
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 1800);
      qc.invalidateQueries({ queryKey: ["appSettings"] });
      qc.invalidateQueries({ queryKey: ["architectureCatalog"] });
      qc.invalidateQueries({ queryKey: ["assessmentCatalog"] });
      qc.invalidateQueries({ queryKey: ["workloadProfiles"] });
      qc.invalidateQueries({ queryKey: ["workloadHealthWeights"] });
    } catch (e) {
      setError(formatError(e));
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-gray-800">Assessments &amp; Architecture</h1>
        <p className="mt-1 text-sm text-gray-500">
          Tune how Well-Architected assessments are scored and how architecture diagrams are
          colored. Scoring changes apply to subsequent assessment runs; color changes apply
          immediately across all diagrams.
        </p>
      </div>

      <Card title="Assessment scoring weights">
        <p className="mb-3 text-xs text-gray-500">
          Each failing control reduces its pillar&rsquo;s 0&ndash;100 score in proportion to its
          severity weight. Raise a weight to make that severity matter more.
        </p>
        <div className="space-y-3">
          {SEVERITY_ROWS.map((r) => (
            <div key={r.id} className="flex items-center gap-3">
              <span className="flex w-28 items-center gap-2 text-sm font-medium text-gray-700">
                <span className="h-2.5 w-2.5 rounded-full" style={{ background: r.color }} />
                {r.label}
              </span>
              <input
                type="number"
                min={0}
                max={100}
                value={weights[r.id] ?? 0}
                onChange={(e) => setWeight(r.id, Number(e.target.value))}
                className="w-20 rounded-lg border px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
              />
              <div className="hidden h-2 flex-1 overflow-hidden rounded-full bg-gray-100 sm:block">
                <div
                  className="h-full rounded-full"
                  style={{ width: `${(Number(weights[r.id] ?? 0) / maxWeight) * 100}%`, background: r.color }}
                />
              </div>
              <span className="hidden w-40 truncate text-xs text-gray-400 lg:block">{r.hint}</span>
            </div>
          ))}
        </div>
      </Card>

      <Card title="Score color bands">
        <p className="mb-3 text-xs text-gray-500">
          Thresholds for the green/amber/red badges shown on scores across the Assessments
          dashboard.
        </p>
        <div className="flex flex-wrap items-end gap-6">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-gray-600">Healthy at or above</span>
            <div className="flex items-center gap-1">
              <input
                type="number"
                min={1}
                max={100}
                value={good}
                onChange={(e) => set({ assessment_score_good: Number(e.target.value) })}
                className="w-24 rounded-lg border px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
              />
              <span className="text-sm text-gray-400">/ 100</span>
            </div>
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-gray-600">At risk at or above</span>
            <div className="flex items-center gap-1">
              <input
                type="number"
                min={0}
                max={99}
                value={warn}
                onChange={(e) => set({ assessment_score_warn: Number(e.target.value) })}
                className="w-24 rounded-lg border px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
              />
              <span className="text-sm text-gray-400">/ 100</span>
            </div>
          </label>
          <div className="flex items-center gap-2 text-xs">
            <span className="rounded px-2 py-0.5 font-semibold bg-green-100 text-green-700">≥ {good} healthy</span>
            <span className="rounded px-2 py-0.5 font-semibold bg-amber-100 text-amber-700">≥ {warn} at risk</span>
            <span className="rounded px-2 py-0.5 font-semibold bg-red-100 text-red-700">&lt; {warn} poor</span>
          </div>
        </div>
        {warn >= good && (
          <p className="mt-2 text-xs text-amber-600">
            &ldquo;At risk&rdquo; should be lower than &ldquo;Healthy&rdquo; — it will be clamped on save.
          </p>
        )}
      </Card>

      <Card title="Workload Health Score">
        <p className="mb-3 text-xs text-gray-500">
          The composite 0&ndash;100 health score on the Workloads command center is a weighted
          blend of these per-signal coverage metrics. Only signals that have been analyzed count;
          the weights below set their relative influence. Raise Backup/DR to make an unprotected
          workload score lower.
        </p>
        <div className="space-y-3">
          {WL_SIGNALS.map((s) => (
            <div key={s.id} className="flex items-center gap-3">
              <span className="w-28 text-sm font-medium text-gray-700">{s.label}</span>
              <input
                type="number"
                min={0}
                step={0.5}
                value={wlWeights[s.id] ?? 1}
                onChange={(e) => setWlWeight(s.id, Number(e.target.value))}
                className="w-20 rounded-lg border px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
              />
              <div className="hidden h-2 flex-1 overflow-hidden rounded-full bg-gray-100 sm:block">
                <div className="h-full rounded-full bg-brand" style={{ width: `${(Number(wlWeights[s.id] ?? 1) / wlMax) * 100}%` }} />
              </div>
            </div>
          ))}
        </div>
        <label className="mt-3 flex items-start gap-2 text-sm text-gray-700">
          <input
            type="checkbox"
            checked={!!form.workload_nightly_refresh}
            onChange={(e) => set({ workload_nightly_refresh: e.target.checked })}
            className="mt-0.5"
          />
          <span>Nightly fleet refresh — warm every workload&rsquo;s analysis caches + record a score-trend point each night (off by default; analysis is on-demand otherwise).</span>
        </label>
      </Card>

      <Card title="Architecture category colors">
        <p className="mb-3 text-xs text-gray-500">
          Accent color used for each resource category on architecture diagram nodes. Leave a
          category at its default or pick a brand color.
        </p>
        {categories.length === 0 ? (
          <p className="text-sm text-gray-400">Loading categories…</p>
        ) : (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {categories.map((c) => {
              const effective = colors[c.id] ?? c.color;
              const overridden = c.id in colors;
              return (
                <div key={c.id} className="flex items-center gap-2 rounded-lg border bg-white px-2.5 py-2">
                  <input
                    type="color"
                    value={effective}
                    onChange={(e) => setColor(c.id, e.target.value)}
                    className="h-7 w-7 shrink-0 cursor-pointer rounded border border-gray-200 bg-white p-0.5"
                    aria-label={`${c.label} color`}
                  />
                  <span className="flex-1 truncate text-sm text-gray-700">{c.label}</span>
                  {overridden && (
                    <button
                      onClick={() => resetColor(c.id)}
                      className="shrink-0 text-xs text-gray-400 hover:text-gray-700"
                      title="Reset to default"
                    >
                      Reset
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>

      <div className="flex items-center gap-3">
        <button
          onClick={save}
          className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90"
        >
          Save changes
        </button>
        {saved && <span className="text-xs text-green-600">Saved ✓</span>}
        {error && <span className="text-xs text-red-600">{error}</span>}
      </div>
    </div>
  );
}

function AppSettingsCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["appSettings"], queryFn: api.appSettings });
  const [form, setForm] = useState<AppSettings | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (q.data) setForm(q.data.settings);
  }, [q.data]);

  if (!form) {
    return (
      <Card title="Settings">
        <p className="text-sm text-gray-400">Loading…</p>
      </Card>
    );
  }

  const styles = q.data?.response_styles ?? ["default"];
  const set = (patch: Partial<AppSettings>) => setForm((f) => (f ? { ...f, ...patch } : f));

  async function save() {
    if (!form) return;
    setError("");
    try {
      await api.updateAppSettings(form);
      setSaved(true);
      setTimeout(() => setSaved(false), 1800);
      qc.invalidateQueries({ queryKey: ["appSettings"] });
    } catch (e) {
      setError(formatError(e));
    }
  }

  return (
    <div className="space-y-5">
      <Card title="Instructions & responses">
        <p className="mb-2 text-xs text-gray-500">
          Persistent guidance prepended to every chat — tone, role, conventions, things the
          agent should always remember (like a ChatGPT custom instruction or Claude project
          instructions).
        </p>
        <textarea
          value={form.custom_instructions}
          onChange={(e) => set({ custom_instructions: e.target.value })}
          rows={5}
          placeholder="e.g. Always investigate the production subscription first. Prefer Azure CLI examples. Format findings as a table followed by next steps."
          className="w-full resize-y rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
        />
        <div className="mt-4">
          <label className="mb-1 block text-xs font-medium text-gray-600">Response style</label>
          <div className="flex flex-wrap gap-2">
            {styles.map((s) => (
              <button
                key={s}
                onClick={() => set({ response_style: s })}
                className={`rounded-lg border px-3 py-1.5 text-sm capitalize transition ${
                  form.response_style === s
                    ? "border-brand bg-brand/5 font-medium text-brand"
                    : "border-gray-200 text-gray-600 hover:bg-gray-50"
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
        <div className="mt-4">
          <label className="mb-1 block text-xs font-medium text-gray-600">
            Max response tokens
          </label>
          <input
            type="number"
            min={256}
            max={32000}
            step={256}
            value={form.max_tokens}
            onChange={(e) => set({ max_tokens: Number(e.target.value) })}
            className="w-40 rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
          />
        </div>
      </Card>

      <Card title="Behavior">
        <div className="divide-y">
          <Toggle
            label="Auto-name new chats"
            hint="Title a new chat from its first message."
            checked={form.auto_title}
            onChange={(v) => set({ auto_title: v })}
          />
          <Toggle
            label="Show follow-up suggestions"
            hint="Suggest next actions after each answer."
            checked={form.suggestions}
            onChange={(v) => set({ suggestions: v })}
          />
          <div className="py-3">
            <div className="text-sm font-medium text-gray-800">Progress detail</div>
            <p className="mb-2 text-xs text-gray-500">
              How much of the agent&rsquo;s work to show in the “Working on your request…”
              feed.
            </p>
            <div className="flex flex-wrap gap-2">
              {(
                [
                  ["compact", "Compact", "High-level phases only"],
                  ["normal", "Normal", "Phases + tool names & results"],
                  ["detailed", "Detailed", "Everything, incl. reasoning & params"],
                ] as const
              ).map(([value, title, desc]) => (
                <button
                  key={value}
                  onClick={() => set({ progress_detail: value })}
                  title={desc}
                  className={`rounded-lg border px-3 py-1.5 text-sm capitalize transition ${
                    form.progress_detail === value
                      ? "border-brand bg-brand/5 font-medium text-brand"
                      : "border-gray-200 text-gray-600 hover:bg-gray-50"
                  }`}
                >
                  {title}
                </button>
              ))}
            </div>
          </div>
        </div>
      </Card>

      <Card title="Scope & clarification">
        <div className="divide-y">
          <Toggle
            label="Ask which subscription to use"
            hint="For ambiguous questions, prompt to pick a subscription before investigating."
            checked={form.scope_clarification}
            onChange={(v) => set({ scope_clarification: v })}
          />
          <Toggle
            label="Ask which management group to use"
            hint="For governance, policy, compliance, and org-wide questions, prompt to pick a management group before investigating."
            checked={form.mgmt_group_clarification}
            onChange={(v) => set({ mgmt_group_clarification: v })}
          />
          <Toggle
            label="Propose problems upon new chats"
            hint="On the first message of a new chat, suggest up to 5 sharper problem statements (matched from the Azure problem catalog) for the user to pick from."
            checked={form.propose_problems}
            onChange={(v) => set({ propose_problems: v })}
          />
        </div>
      </Card>

      <Card title="Deep investigation">
        <div>
          <Toggle
            label="Simultaneous sub-agents"
            hint="Validate multiple hypotheses at once during a deep investigation, then combine their evidence at the conclusion — dramatically faster than one at a time."
            checked={form.deep_parallel_enabled}
            onChange={(v) => set({ deep_parallel_enabled: v })}
          />
          {form.deep_parallel_enabled && (
            <div className="mt-2 flex flex-wrap items-center gap-3 pl-1">
              <span className="text-xs text-gray-500">Max parallel sub-agents</span>
              <div className="flex flex-wrap items-center gap-1.5">
                {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map((n) => (
                  <button
                    key={n}
                    onClick={() => set({ deep_parallel_count: n })}
                    className={`h-7 w-7 rounded-lg border text-sm font-medium transition ${
                      form.deep_parallel_count === n
                        ? "border-brand bg-brand/5 text-brand"
                        : "border-gray-200 text-gray-600 hover:bg-gray-50"
                    }`}
                  >
                    {n}
                  </button>
                ))}
              </div>
              <span className="text-[11px] text-gray-400">
                more = faster, but more concurrent Azure queries
              </span>
            </div>
          )}
        </div>
      </Card>

      <Card title="Tool Safety">
        <div className="space-y-3">
          <Toggle
            label="Read-only Azure tools"
            hint="When on, only investigation (read) tools are exposed to the agent — it can never modify your Azure resources. Turn off to allow mutating tools (create/update/delete)."
            checked={form.mcp_read_only}
            onChange={(v) => set({ mcp_read_only: v })}
          />
          {!form.mcp_read_only && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2.5 text-xs text-amber-800">
              <div className="mb-1 font-semibold">⚠️ Write tools are enabled</div>
              The agent can now invoke mutating Azure operations (create, update,
              delete, restart, role assignments, etc.). Actions are limited by the
              signed-in Azure identity's RBAC. Only enable this if you intend to let the
              agent make changes.
            </div>
          )}

          {!form.mcp_read_only && (
            <div className="border-t pt-3">
              <Toggle
                label="Auto-execute write operations"
                hint="When on, the agent performs write/mutating actions IMMEDIATELY without verbally asking for approval. When off, it pauses and asks before each write."
                checked={form.auto_execute_writes}
                onChange={(v) => set({ auto_execute_writes: v })}
              />
              {form.auto_execute_writes && (
                <div className="mt-2 rounded-lg border border-red-300 bg-red-50 px-3 py-2.5 text-xs text-red-800">
                  <div className="mb-1 font-semibold">🚨 Auto-execute is ON</div>
                  The agent will create, modify, and delete Azure resources on its own
                  with NO confirmation step. There is no undo. Every action is still
                  audited, but nothing will pause for your review. Use with caution.
                </div>
              )}
            </div>
          )}
        </div>
      </Card>

      <Card title="Host command execution">
        <p className="mb-3 text-xs text-gray-500">
          Adds a <span className="font-medium">Run</span> button next to Copy on CLI code
          blocks in chat. Commands run on this host, authenticated with the chat&rsquo;s
          Azure connection, and stream their output back live — for when the MCP tools
          don&rsquo;t cover what you need.
        </p>
        <div className="space-y-3">
          <Toggle
            label="Enable the Run button"
            hint="Master switch. When off, no command can be executed and the endpoint is disabled."
            checked={!!form.command_execution_enabled}
            onChange={(v) => set({ command_execution_enabled: v })}
          />
          {form.command_execution_enabled && (
            <>
              <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2.5 text-xs text-amber-800">
                <div className="mb-1 font-semibold">⚠️ Commands run on the host</div>
                Only allowlisted binaries run, shell operators (pipes, chaining,
                redirection) are rejected, and mutating commands require an explicit
                confirmation click. Mutating commands are blocked on read-only
                connections. Every run is audited.
              </div>
              <div>
                <div className="mb-1.5 text-sm font-medium text-gray-800">Allowed commands</div>
                <div className="flex flex-wrap gap-2">
                  {(q.data?.command_binaries ?? ["az"]).map((bin) => {
                    const on = (form.command_allowlist ?? ["az"]).includes(bin);
                    const isAz = bin === "az";
                    return (
                      <button
                        key={bin}
                        onClick={() => {
                          if (isAz) return; // az is always allowed
                          const cur = form.command_allowlist ?? ["az"];
                          set({
                            command_allowlist: on
                              ? cur.filter((b) => b !== bin)
                              : [...cur, bin],
                          });
                        }}
                        title={isAz ? "az is always allowed" : undefined}
                        className={`rounded-lg border px-3 py-1.5 font-mono text-sm transition ${
                          on
                            ? "border-brand bg-brand/5 font-medium text-brand"
                            : "border-gray-200 text-gray-600 hover:bg-gray-50"
                        } ${isAz ? "cursor-default opacity-90" : ""}`}
                      >
                        {bin}
                      </button>
                    );
                  })}
                </div>
              </div>
              <NumberField
                label="Command timeout (seconds)"
                hint="A running command is killed after this many seconds."
                value={form.command_timeout_seconds}
                min={5}
                max={900}
                step={5}
                onChange={(v) => set({ command_timeout_seconds: v })}
              />
            </>
          )}
        </div>
      </Card>

      <Card title="Advanced (agent tuning)">
        <p className="mb-3 text-xs text-gray-500">
          Low-level limits that control how the agent investigates. Defaults work well;
          change these only if you hit the described problem. Applied immediately — no
          restart.
        </p>
        <div className="space-y-4">
          <NumberField
            label="Max tool steps per turn"
            hint="How many Azure tool calls the agent may chain before it must stop and answer. Raise it for broad investigations that get cut off early (e.g. 'audit every NSG'); lower it to cap cost/latency."
            value={form.max_tool_iterations}
            min={1}
            max={50}
            step={1}
            suffix="steps"
            onChange={(v) => set({ max_tool_iterations: v })}
          />
          <NumberField
            label="Tool result size limit"
            hint="Max characters of a normal tool result fed back to the model. Larger keeps more data in context (better answers) but costs more tokens. Too small can truncate findings."
            value={form.tool_result_limit}
            min={2000}
            max={200000}
            step={1000}
            suffix="chars"
            onChange={(v) => set({ tool_result_limit: v })}
          />
          <NumberField
            label="Tool discovery size limit"
            hint="Max characters of a tool 'learn' result, which lists a service's available sub-commands. These are large (30KB+); if it's too small the agent can't see commands near the end of the list and guesses wrong (e.g. it couldn't find the SQL firewall-rule delete command)."
            value={form.tool_discovery_limit}
            min={2000}
            max={400000}
            step={5000}
            suffix="chars"
            onChange={(v) => set({ tool_discovery_limit: v })}
          />
          <NumberField
            label="LLM request timeout"
            hint="How long to wait for a single model streaming request before giving up. Raise it for slow, high-reasoning models on long turns; lower it to fail faster."
            value={form.request_timeout_seconds}
            min={30}
            max={600}
            step={10}
            suffix="sec"
            onChange={(v) => set({ request_timeout_seconds: v })}
          />
        </div>
      </Card>

      <Card title="Policy exemption guardrails">
        <p className="mb-2 text-xs text-gray-500">
          Enforced whenever someone creates or extends an Azure Policy exemption (Policy → Exemptions).
          These keep exemptions a controlled, hygienic security exception rather than a permanent escape hatch.
        </p>
        <Toggle
          label="Require a justification"
          hint="A description (e.g. ticket #, owner, mitigation) is mandatory on every exemption create/extend."
          checked={form.policy_exemption_require_justification ?? true}
          onChange={(v) => set({ policy_exemption_require_justification: v })}
        />
        <Toggle
          label="Block never-expiring exemptions"
          hint="Forbid creating exemptions with no expiry date — every exemption must eventually lapse and be re-reviewed."
          checked={form.policy_exemption_block_never_expires ?? true}
          onChange={(v) => set({ policy_exemption_block_never_expires: v })}
        />
        <div className="mt-2">
          <NumberField
            label="Maximum expiry window"
            hint="The furthest into the future an exemption may expire. Set to 0 to remove the cap (not recommended)."
            value={form.policy_exemption_max_expiry_days ?? 180}
            min={0}
            max={3650}
            step={30}
            suffix="days"
            onChange={(v) => set({ policy_exemption_max_expiry_days: v })}
          />
        </div>
      </Card>

      <Card title="Change Explorer">
        <p className="mb-2 text-xs text-gray-500">
          Controls how the Workload Change Explorer attributes “who made the change”.
        </p>
        <Toggle
          label="Resolve actor identities via Microsoft Graph"
          hint="Turn raw object-ids (service principals / managed identities / users) into friendly display names. Requires the Azure connection to have directory read access (Directory.Read.All). When off — or when Graph is unavailable — object-ids are shown as-is."
          checked={form.changeexplorer_resolve_identities ?? true}
          onChange={(v) => set({ changeexplorer_resolve_identities: v })}
        />
        <div className="mt-2">
          <NumberField
            label="Max changes per scan"
            hint="Upper bound on changes collected per scan from each source (Resource Graph + Activity Log). A bigger value is more complete on busy estates / wide windows, but slower and heavier. Default 5,000."
            value={form.changeexplorer_change_limit ?? 5000}
            min={100}
            max={50000}
            step={500}
            suffix="changes"
            onChange={(v) => set({ changeexplorer_change_limit: v })}
          />
        </div>
      </Card>

      <div className="flex items-center gap-3">
        <button
          onClick={() => void save()}
          className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90"
        >
          Save settings
        </button>
        {saved && <span className="text-sm text-green-600">✓ Saved</span>}
        {error && <span className="text-sm text-red-600">{error}</span>}
      </div>
    </div>
  );
}

function AIProviderCard() {
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["llmConfig"], queryFn: api.llmConfig });
  const [active, setActive] = useState<string>("openai");
  // Collapse not-yet-configured providers behind a "Show all" expander to keep the
  // rail short — only set-up/active/currently-viewed providers show by default.
  const [showAllProviders, setShowAllProviders] = useState(false);
  // Per-provider local form state.
  const [forms, setForms] = useState<
    Record<
      string,
      { model: string; newKey: string; freeOnly: boolean; endpoint: string; apiVersion: string }
    >
  >({});
  const [models, setModels] = useState<Record<string, string[]>>({});
  const [loadingModels, setLoadingModels] = useState<string | null>(null);
  // Per-provider staged refresh-models diagnostics (live-updated from the SSE stream).
  const [refreshSteps, setRefreshSteps] = useState<Record<string, LlmTestStep[]>>({});
  const [refreshResult, setRefreshResult] = useState<
    Record<string, { ok: boolean; detail: string }>
  >({});
  // Per-provider "Manage visibility" panel open state + filter text.
  const [visibilityOpen, setVisibilityOpen] = useState<Record<string, boolean>>({});
  const [visibilityFilter, setVisibilityFilter] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  // Per-provider connection-test state (null = idle).
  const [testing, setTesting] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<
    Record<string, { ok: boolean; detail: string }>
  >({});
  // Per-provider staged diagnostics (live-updated as the SSE stream arrives).
  const [testSteps, setTestSteps] = useState<Record<string, LlmTestStep[]>>({});
  const [importMsg, setImportMsg] = useState("");
  const [ghBusy, setGhBusy] = useState(false);
  // ChatGPT paste-URL sign-in state (for headless/remote hosts).
  const [chatgptAuthUrl, setChatgptAuthUrl] = useState("");
  const [chatgptCallback, setChatgptCallback] = useState("");
  // Claude (Pro/Max) paste-code sign-in state (for headless/remote hosts).
  const [claudeAuthUrl, setClaudeAuthUrl] = useState("");
  const [claudeCallback, setClaudeCallback] = useState("");
  // GitHub Copilot OAuth device-flow state (headless/remote sign-in).
  const [ghDevice, setGhDevice] = useState<{ user_code: string; verification_uri: string } | null>(null);
  const ghStatus = useQuery({
    queryKey: ["githubCopilotStatus"],
    queryFn: api.githubCopilotStatus,
  });
  const cgStatus = useQuery({
    queryKey: ["chatgptStatus"],
    queryFn: api.chatgptStatus,
  });
  const claudeStatus = useQuery({
    queryKey: ["claudeOauthStatus"],
    queryFn: api.claudeStatus,
  });

  async function chatgptRefresh() {
    setGhBusy(true);
    setImportMsg("Refreshing token…");
    try {
      await api.chatgptRefresh();
      setImportMsg("✓ Token refreshed");
      cgStatus.refetch();
    } catch (e) {
      setImportMsg(formatError(e));
    } finally {
      setGhBusy(false);
    }
  }

  async function chatgptGetLink() {
    setGhBusy(true);
    setImportMsg("Creating sign-in link…");
    try {
      const r = await api.chatgptAuthorizeUrl();
      setChatgptAuthUrl(r.authorize_url);
      setImportMsg("Open the link, sign in, then paste the redirected URL below.");
      try {
        window.open(r.authorize_url, "_blank", "noopener");
      } catch {
        /* popup blocked — the link is still shown for manual copy */
      }
    } catch (e) {
      setImportMsg(formatError(e));
    } finally {
      setGhBusy(false);
    }
  }

  async function chatgptComplete() {
    if (!chatgptCallback.trim()) {
      setImportMsg("Paste the URL you were redirected to (it contains '?code=…').");
      return;
    }
    setGhBusy(true);
    setImportMsg("Completing sign-in…");
    try {
      await api.chatgptComplete(chatgptCallback.trim());
      setImportMsg("✓ Signed in & token captured");
      setChatgptCallback("");
      setChatgptAuthUrl("");
      cgStatus.refetch();
      qc.invalidateQueries({ queryKey: ["activeLlm"] });
    } catch (e) {
      setImportMsg(formatError(e));
    } finally {
      setGhBusy(false);
    }
  }

  async function chatgptSignout() {
    setGhBusy(true);
    try {
      await api.chatgptSignout();
      setImportMsg("Signed out");
      setChatgptAuthUrl("");
      setChatgptCallback("");
      cgStatus.refetch();
    } catch (e) {
      setImportMsg(formatError(e));
    } finally {
      setGhBusy(false);
    }
  }

  async function claudeRefresh() {
    setGhBusy(true);
    setImportMsg("Refreshing token…");
    try {
      await api.claudeRefresh();
      setImportMsg("✓ Token refreshed");
      claudeStatus.refetch();
    } catch (e) {
      setImportMsg(formatError(e));
    } finally {
      setGhBusy(false);
    }
  }

  async function claudeGetLink() {
    setGhBusy(true);
    setImportMsg("Creating sign-in link…");
    try {
      const r = await api.claudeAuthorizeUrl();
      setClaudeAuthUrl(r.authorize_url);
      setImportMsg("Open the link, sign in, then paste the code you're shown below.");
      try {
        window.open(r.authorize_url, "_blank", "noopener");
      } catch {
        /* popup blocked — the link is still shown for manual copy */
      }
    } catch (e) {
      setImportMsg(formatError(e));
    } finally {
      setGhBusy(false);
    }
  }

  async function claudeComplete() {
    if (!claudeCallback.trim()) {
      setImportMsg("Paste the code shown on the Claude callback page (it looks like 'code#state').");
      return;
    }
    setGhBusy(true);
    setImportMsg("Completing sign-in…");
    try {
      await api.claudeComplete(claudeCallback.trim());
      setImportMsg("✓ Signed in & token captured");
      setClaudeCallback("");
      setClaudeAuthUrl("");
      claudeStatus.refetch();
      qc.invalidateQueries({ queryKey: ["activeLlm"] });
    } catch (e) {
      setImportMsg(formatError(e));
    } finally {
      setGhBusy(false);
    }
  }

  async function claudeSignout() {
    setGhBusy(true);
    try {
      await api.claudeSignout();
      setImportMsg("Signed out");
      setClaudeAuthUrl("");
      setClaudeCallback("");
      claudeStatus.refetch();
    } catch (e) {
      setImportMsg(formatError(e));
    } finally {
      setGhBusy(false);
    }
  }

  async function githubCopilotLogin() {
    // Headless/remote-friendly OAuth device flow: show a code + link, then poll until
    // the user authorizes on their own device. No server-side browser is used.
    setGhBusy(true);
    setGhDevice(null);
    setImportMsg("Starting GitHub sign-in…");
    try {
      const d = await api.githubCopilotDeviceStart();
      setGhDevice({ user_code: d.user_code, verification_uri: d.verification_uri });
      setImportMsg("");
      try {
        window.open(d.verification_uri, "_blank", "noopener");
      } catch {
        /* popup blocked — the link + code are shown for manual entry */
      }
      // Poll for completion until authorized, expired, or cancelled.
      const intervalMs = Math.max(3, d.interval || 5) * 1000;
      const deadline = Date.now() + (d.expires_in || 900) * 1000;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        if (Date.now() > deadline) {
          setImportMsg("Sign-in code expired. Click sign in to try again.");
          setGhDevice(null);
          break;
        }
        await new Promise((r) => setTimeout(r, intervalMs));
        let res;
        try {
          res = await api.githubCopilotDevicePoll();
        } catch (e) {
          setImportMsg(formatError(e));
          setGhDevice(null);
          break;
        }
        const st = res.status?.status;
        if (st === "authorized") {
          setImportMsg("✓ Signed in & Copilot token captured");
          setGhDevice(null);
          ghStatus.refetch();
          qc.invalidateQueries({ queryKey: ["activeLlm"] });
          break;
        }
        if (st === "error") {
          setImportMsg(res.status?.detail || "Sign-in failed. Try again.");
          setGhDevice(null);
          break;
        }
        // st === "pending" — keep waiting.
      }
    } catch (e) {
      setImportMsg(formatError(e));
      setGhDevice(null);
    } finally {
      setGhBusy(false);
    }
  }

  async function githubCopilotRefresh() {
    setGhBusy(true);
    setImportMsg("Refreshing token…");
    try {
      await api.githubCopilotRefresh();
      setImportMsg("✓ Token refreshed");
      ghStatus.refetch();
    } catch (e) {
      setImportMsg(formatError(e));
    } finally {
      setGhBusy(false);
    }
  }

  async function githubCopilotSignout() {
    setGhBusy(true);
    try {
      await api.githubCopilotSignout();
      setImportMsg("Signed out");
      ghStatus.refetch();
    } catch (e) {
      setImportMsg(formatError(e));
    } finally {
      setGhBusy(false);
    }
  }

  // Seed local state from server config. Runs whenever cfg.data changes (incl. the
  // optimistic cache write when toggling model visibility), so it must NOT re-seed the
  // viewed provider or clobber in-progress edits each time.
  const seededRef = useRef(false);
  useEffect(() => {
    if (!cfg.data) return;
    // `active` (which provider is being viewed) is seeded only ONCE. Re-seeding on every
    // cfg.data change yanked the admin off the provider they were viewing back to the
    // global default (e.g. when clicking Hide in the model-visibility panel).
    if (!seededRef.current) {
      setActive(cfg.data.active_provider);
      seededRef.current = true;
    }
    // Add a form entry for any provider that doesn't have one yet (first load / a newly
    // added provider), but preserve existing entries so unsaved key/endpoint/model edits
    // aren't wiped when the config object changes for an unrelated reason.
    setForms((prev) => {
      const next = { ...prev };
      let changed = false;
      for (const [name, p] of Object.entries(cfg.data!.providers)) {
        if (next[name]) continue;
        next[name] = {
          model: p.model,
          newKey: "",
          freeOnly: !!p.free_only,
          endpoint: p.base_url || "",
          apiVersion: p.api_version || "",
        };
        changed = true;
      }
      return changed ? next : prev;
    });
  }, [cfg.data]);

  async function refreshModels(provider: string, freeOnly?: boolean) {
    setLoadingModels(provider);
    setRefreshSteps((s) => ({ ...s, [provider]: [] }));
    setRefreshResult((r) => {
      const next = { ...r };
      delete next[provider];
      return next;
    });
    try {
      await streamRefreshLlmModels(
        provider,
        freeOnly,
        {
          onStep: (step) => {
            setRefreshSteps((s) => {
              const existing = s[provider] ?? [];
              const filtered = existing.filter((x) => x.step !== step.step);
              return { ...s, [provider]: [...filtered, step] };
            });
          },
          onDone: (r) => {
            setRefreshResult((rs) => ({ ...rs, [provider]: { ok: r.ok, detail: r.detail } }));
            if (r.models && r.models.length > 0) {
              setModels((m) => ({ ...m, [provider]: r.models }));
            }
          },
          onError: (msg) => {
            setRefreshResult((rs) => ({ ...rs, [provider]: { ok: false, detail: msg } }));
          },
        },
      );
    } catch {
      /* ignore — onError already surfaced the failure */
    } finally {
      setLoadingModels(null);
    }
  }

  // Model lists are fetched only when the user clicks "Fetch model catalogue" / Refresh —
  // never automatically on page load or provider switch (the catalog call can be slow and
  // hits the provider API). The dropdown falls back to the configured/free-text model id.

  // Hide/show a provider from the chat model picker (takes effect immediately).
  async function toggleDisabled(provider: string, disabled: boolean) {
    setError("");
    try {
      await api.updateLlmConfig({ providers: { [provider]: { disabled } } });
      qc.invalidateQueries({ queryKey: ["llmConfig"] });
      qc.invalidateQueries({ queryKey: ["activeLlm"] });
    } catch (e) {
      setError(formatError(e));
    }
  }

  // Hide / unhide a specific model within a provider. The full saved list is
  // persisted so concurrent edits from elsewhere can't drop it accidentally.
  async function toggleModelHidden(provider: string, modelId: string, hide: boolean) {
    setError("");
    const current = new Set(cfg.data?.providers[provider]?.hidden_models ?? []);
    if (hide) current.add(modelId);
    else current.delete(modelId);
    const hidden_models = Array.from(current).sort();
    // Optimistically reflect in the cached config so the UI updates immediately.
    qc.setQueryData<typeof cfg.data>(["llmConfig"], (prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        providers: {
          ...prev.providers,
          [provider]: { ...prev.providers[provider], hidden_models },
        },
      };
    });
    try {
      await api.updateLlmConfig({ providers: { [provider]: { hidden_models } } });
      qc.invalidateQueries({ queryKey: ["llmConfig"] });
      qc.invalidateQueries({ queryKey: ["activeLlm"] });
    } catch (e) {
      setError(formatError(e));
      // Roll back on failure.
      qc.invalidateQueries({ queryKey: ["llmConfig"] });
    }
  }

  async function save(setDefault = false) {
    setError("");
    try {
      const providers: Record<
        string,
        {
          model?: string;
          api_key?: string;
          base_url?: string;
          api_version?: string;
          free_only?: boolean;
        }
      > = {};
      for (const [name, f] of Object.entries(forms)) {
        providers[name] = { model: f.model };
        const meta = PROVIDERS.find((p) => p.id === name);
        if (name === "openrouter") providers[name].free_only = f.freeOnly;
        if (name === "azure_openai" || name === "azure_foundry") {
          // Azure needs the resource endpoint + API version alongside the key.
          if (f.endpoint.trim()) providers[name].base_url = f.endpoint.trim();
          if (f.apiVersion.trim()) providers[name].api_version = f.apiVersion.trim();
        }
        if (f.newKey.trim()) {
          // For Ollama the editable field is the base URL, not a key.
          if (meta?.auth === "none") providers[name].base_url = f.newKey.trim();
          else providers[name].api_key = f.newKey.trim();
        }
      }
      // Only change the global default provider when explicitly requested.
      await api.updateLlmConfig(
        setDefault ? { active_provider: active, providers } : { providers },
      );
      setSaved(true);
      setTimeout(() => setSaved(false), 1800);
      // Clear entered keys and refresh config (masked).
      setForms((prev) => {
        const cleared: typeof prev = {};
        for (const [k, v] of Object.entries(prev)) cleared[k] = { ...v, newKey: "" };
        return cleared;
      });
      qc.invalidateQueries({ queryKey: ["llmConfig"] });
      qc.invalidateQueries({ queryKey: ["activeLlm"] });
      // Auto-refresh the model catalogue on the FIRST successful save of a connection — i.e.
      // when the viewed provider has no models loaded yet — so the admin doesn't have to click
      // "↻ Refresh models" separately. Once a catalogue exists this won't re-fire on later saves.
      if ((models[active]?.length ?? 0) === 0 && loadingModels !== active) {
        void refreshModels(active, active === "openrouter" ? forms[active]?.freeOnly : undefined);
      }
    } catch (e) {
      setError(formatError(e));
    }
  }

  // Send a tiny live request through the provider's SAVED credentials to verify they
  // work. Tests the persisted config, so the admin should Save first, then Test.
  // Uses the SSE diagnostics stream so each phase (config, DNS, connect, auth,
  // request, first-token) shows up as it happens.
  async function testProvider(provider: string) {
    setError("");
    setTesting(provider);
    setTestResult((r) => {
      const next = { ...r };
      delete next[provider];
      return next;
    });
    setTestSteps((s) => ({ ...s, [provider]: [] }));
    try {
      await streamTestLlmProvider(
        provider,
        {
          onStep: (step) => {
            setTestSteps((s) => {
              const existing = s[provider] ?? [];
              // Replace an in-progress entry for the same step, otherwise append.
              const filtered = existing.filter((x) => x.step !== step.step);
              return { ...s, [provider]: [...filtered, step] };
            });
          },
          onDone: (r) => {
            setTestResult((rs) => ({ ...rs, [provider]: r }));
          },
          onError: (msg) => {
            setTestResult((rs) => ({ ...rs, [provider]: { ok: false, detail: msg } }));
          },
        },
      );
    } catch (e) {
      setTestResult((r) => ({ ...r, [provider]: { ok: false, detail: formatError(e) } }));
    } finally {
      setTesting(null);
    }
  }

  if (cfg.isLoading) {
    return (
      <Card title="AI Provider">
        <p className="text-sm text-gray-400">Loading…</p>
      </Card>
    );
  }

  const activeProvider = cfg.data?.active_provider;

  return (
    <section className="rounded-lg border bg-white shadow-sm">
      <div className="border-b px-5 py-4">
        <h2 className="font-medium">AI Provider</h2>
        <p className="mt-0.5 text-xs text-gray-500">
          Pick the provider that powers the agent, add its credentials, and choose a model.
          Changes apply immediately to new messages.
        </p>
      </div>

      <div className="flex">
        {/* Provider list */}
        <div className="w-52 shrink-0 space-y-2 border-r p-2">
          {(() => {
            // A provider is "primary" (always visible) when it's enabled, the global
            // default, or the one being viewed; disabled/not-set-up providers are tucked
            // behind "Show all".
            const isPrimary = (id: string) => {
              const prov = cfg.data?.providers[id];
              return (
                active === id ||
                activeProvider === id ||
                (prov ? !prov.disabled : false)
              );
            };
            const hiddenCount = PROVIDERS.filter(
              (p) => cfg.data?.providers[p.id] && !isPrimary(p.id),
            ).length;
            return (
              <>
                {PROVIDER_GROUPS.map((group) => {
                  const shownIds = group.ids.filter((id) => {
                    if (!cfg.data?.providers[id]) return false;
                    return showAllProviders || isPrimary(id);
                  });
                  if (shownIds.length === 0) return null;
                  return (
                    <div key={group.label} className="space-y-1">
                      <div className="px-3 pt-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                        {group.label}
                      </div>
                      {shownIds.map((id) => {
                        const p = PROVIDERS.find((x) => x.id === id);
                        if (!p) return null;
                        const isViewing = active === p.id;
                        const isActive = activeProvider === p.id;
                        const hasKey = cfg.data?.providers[p.id]?.has_key;
                        const isDisabled = cfg.data?.providers[p.id]?.disabled;
                        const providerModel = cfg.data?.providers[p.id]?.model;
                        return (
                          <button
                            key={p.id}
                            onClick={() => setActive(p.id)}
                            className={`flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-sm transition ${
                              isViewing
                                ? "bg-brand/10 font-medium text-brand"
                                : "text-gray-700 hover:bg-gray-100"
                            }`}
                          >
                            <span className="min-w-0 flex-1">
                              <span className={`block truncate ${isDisabled ? "text-gray-400" : ""}`}>{p.label}</span>
                              {providerModel && (
                                <span
                                  className={`block truncate text-[11px] font-normal ${
                                    isViewing ? "text-brand/70" : "text-gray-400"
                                  }`}
                                  title={`Default model: ${providerModel}`}
                                >
                                  {providerModel}
                                </span>
                              )}
                            </span>
                            <span className="flex shrink-0 items-center gap-1">
                              {isActive && (
                                <span className="rounded-full bg-green-100 px-1.5 py-0.5 text-[10px] font-medium text-green-700">
                                  default
                                </span>
                              )}
                              {isDisabled && (
                                <span className="rounded-full bg-gray-200 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">
                                  hidden
                                </span>
                              )}
                              {hasKey && !isActive && !isDisabled && (
                                <span className="text-green-500" title="Configured">●</span>
                              )}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  );
                })}
                {(hiddenCount > 0 || showAllProviders) && (
                  <button
                    onClick={() => setShowAllProviders((v) => !v)}
                    className="mt-1 flex w-full items-center justify-center gap-1 rounded-lg px-3 py-1.5 text-xs font-medium text-gray-500 transition hover:bg-gray-100 hover:text-gray-700"
                  >
                    {showAllProviders ? "Show less" : `Show all (${hiddenCount} more)`}
                  </button>
                )}
              </>
            );
          })()}
        </div>

        {/* Active provider config */}
        <div className="min-w-0 flex-1 p-5">
      {PROVIDERS.filter((p) => p.id === active).map((p) => {
        const form = forms[p.id] ?? {
          model: "",
          newKey: "",
          freeOnly: false,
          endpoint: "",
          apiVersion: "",
        };
        const serverProv = cfg.data?.providers[p.id];
        const modelList = models[p.id] ?? [];
        return (
          <div key={p.id} className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="min-w-0">
                <h3 className="text-sm font-semibold text-gray-800">{p.label}</h3>
                <p className="mt-0.5 text-xs text-gray-500">
                  Default model:{" "}
                  {serverProv?.model ? (
                    <span className="font-mono font-medium text-gray-700">{serverProv.model}</span>
                  ) : (
                    <span className="italic text-gray-400">none selected</span>
                  )}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {activeProvider === p.id ? (
                  <span className="rounded-full bg-green-100 px-2.5 py-1 text-xs font-medium text-green-700">
                    ✓ Default
                  </span>
                ) : (
                  <span className="text-xs text-gray-400">Not default</span>
                )}
                {serverProv?.disabled ? (
                  <button
                    onClick={() => void toggleDisabled(p.id, false)}
                    className="rounded-lg border border-gray-300 bg-white px-3 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50"
                  >
                    Enable in menu
                  </button>
                ) : (
                  <button
                    onClick={() => void toggleDisabled(p.id, true)}
                    disabled={activeProvider === p.id}
                    title={
                      activeProvider === p.id
                        ? "Can't hide the default provider — set another as default first"
                        : "Hide this provider from the chat model picker"
                    }
                    className="rounded-lg border border-red-200 bg-white px-3 py-1 text-xs font-medium text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    Disable
                  </button>
                )}
              </div>
            </div>
            {p.auth === "key" && (
              <div>
                <label className="mb-1 block text-xs font-medium text-gray-600">
                  {p.keyLabel}
                </label>
                <input
                  // Masked TEXT input (not type=password) so browsers/password managers
                  // never autofill the saved admin login into the API-key field. Chrome
                  // ignores autocomplete=off on real password fields, which previously let
                  // the admin credential overwrite a provider key on the next Save.
                  type="text"
                  style={{ WebkitTextSecurity: "disc" } as React.CSSProperties}
                  name={`llm-key-${p.id}`}
                  autoComplete="off"
                  data-1p-ignore
                  data-lpignore="true"
                  data-form-type="other"
                  value={form.newKey}
                  onChange={(e) =>
                    setForms((m) => ({ ...m, [p.id]: { ...form, newKey: e.target.value } }))
                  }
                  placeholder={
                    serverProv?.has_key
                      ? serverProv.key_hint
                        ? `Key set (${serverProv.key_hint}) — leave blank to keep`
                        : "A key is set but looks unusually short — paste a new key to replace it"
                      : "Paste key to enable this provider"
                  }
                  className="w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                />
                <p className="mt-1 text-[11px] text-gray-400">{p.keyHint}</p>
              </div>
            )}

            {(p.id === "azure_openai" || p.id === "azure_foundry") && (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-600">
                    Endpoint
                  </label>
                  <input
                    type="text"
                    value={form.endpoint}
                    onChange={(e) =>
                      setForms((m) => ({ ...m, [p.id]: { ...form, endpoint: e.target.value } }))
                    }
                    placeholder={
                      p.id === "azure_foundry"
                        ? "https://<resource>.services.ai.azure.com"
                        : "https://<resource>.openai.azure.com"
                    }
                    className="w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                  />
                  <p className="mt-1 text-[11px] text-gray-400">
                    {p.id === "azure_foundry"
                      ? "Your Azure AI Foundry resource endpoint (…services.ai.azure.com)."
                      : "Your Azure OpenAI resource endpoint."}
                  </p>
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-600">
                    API version
                  </label>
                  <input
                    type="text"
                    value={form.apiVersion}
                    onChange={(e) =>
                      setForms((m) => ({ ...m, [p.id]: { ...form, apiVersion: e.target.value } }))
                    }
                    placeholder={p.id === "azure_foundry" ? "2024-05-01-preview" : "2024-10-21"}
                    className="w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                  />
                  <p className="mt-1 text-[11px] text-gray-400">
                    {p.id === "azure_foundry"
                      ? "The Foundry inference API version. Model = a deployed model name."
                      : "The deployment's API version. Model = your deployment name."}
                  </p>
                </div>
              </div>
            )}

            {p.auth === "none" && (
              <div>
                <label className="mb-1 block text-xs font-medium text-gray-600">
                  {p.keyLabel}
                </label>
                <input
                  type="text"
                  value={form.newKey}
                  onChange={(e) =>
                    setForms((m) => ({ ...m, [p.id]: { ...form, newKey: e.target.value } }))
                  }
                  placeholder={serverProv?.base_url || "http://localhost:11434/v1"}
                  className="w-full rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                />
                <p className="mt-1 text-[11px] text-gray-400">{p.keyHint}</p>
              </div>
            )}

            {p.auth === "oauth" && p.id === "github_copilot" && (
              <div className="rounded-lg border border-gray-200 bg-gray-50 p-3">
                <div className="mb-2 text-xs text-gray-600">
                  Sign in with your GitHub Copilot account to use its subscription models
                  (Claude, Gemini, GPT-5.x). A code + link appears below — open it on any
                  device, enter the code, and the token is captured &amp; refreshed
                  automatically. No browser runs on the server.
                </div>
                <div className="mb-2 flex items-center gap-2 text-xs">
                  {ghStatus.data?.has_token && !ghStatus.data?.expired ? (
                    <span className="rounded-full bg-green-100 px-2 py-0.5 font-medium text-green-700">
                      ✓ Connected
                    </span>
                  ) : ghStatus.data?.signed_in ? (
                    <span className="rounded-full bg-amber-100 px-2 py-0.5 font-medium text-amber-700">
                      Signed in — token stale
                    </span>
                  ) : (
                    <span className="rounded-full bg-gray-200 px-2 py-0.5 font-medium text-gray-600">
                      Not signed in
                    </span>
                  )}
                  {ghStatus.data?.api_base_url && (
                    <span className="text-gray-400">{ghStatus.data.api_base_url}</span>
                  )}
                </div>
                {ghDevice && (
                  <div className="mb-2 rounded-lg border border-brand/30 bg-brand/5 p-3 text-sm">
                    <div className="mb-1 text-gray-700">
                      1. Open{" "}
                      <a
                        href={ghDevice.verification_uri}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="font-medium text-brand underline"
                      >
                        {ghDevice.verification_uri}
                      </a>
                    </div>
                    <div className="flex items-center gap-2 text-gray-700">
                      <span>2. Enter code:</span>
                      <code className="rounded bg-white px-2 py-1 text-base font-bold tracking-widest text-gray-900 border">
                        {ghDevice.user_code}
                      </code>
                      <button
                        onClick={() => void navigator.clipboard?.writeText(ghDevice.user_code)}
                        className="rounded border border-gray-300 bg-white px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50"
                      >
                        Copy
                      </button>
                    </div>
                    <div className="mt-1 text-[11px] text-gray-500">Waiting for you to authorize…</div>
                  </div>
                )}
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => void githubCopilotLogin()}
                    disabled={ghBusy}
                    className="rounded-lg border border-brand/40 bg-white px-3 py-1.5 text-sm font-medium text-brand hover:bg-brand/5 disabled:opacity-50"
                  >
                    {ghBusy && ghDevice ? "Waiting…" : ghStatus.data?.signed_in ? "Re-sign in" : "Sign in with GitHub Copilot"}
                  </button>
                  {ghStatus.data?.signed_in && (
                    <>
                      <button
                        onClick={() => void githubCopilotRefresh()}
                        disabled={ghBusy}
                        className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                      >
                        Refresh token
                      </button>
                      <button
                        onClick={() => void githubCopilotSignout()}
                        disabled={ghBusy}
                        className="rounded-lg border border-red-200 bg-white px-3 py-1.5 text-sm text-red-600 hover:bg-red-50 disabled:opacity-50"
                      >
                        Sign out
                      </button>
                    </>
                  )}
                  {importMsg && <span className="text-xs text-gray-500">{importMsg}</span>}
                </div>
                <p className="text-[11px] text-gray-400">
                  Azure MCP tools are available on this provider via guided tool-calling.
                </p>
              </div>
            )}

            {p.auth === "oauth" && p.id === "chatgpt" && (
              <div className="rounded-lg border border-gray-200 bg-gray-50 p-3">
                <div className="mb-2 text-xs text-gray-600">
                  Sign in with your ChatGPT subscription: click <b>Get sign-in link</b>,
                  sign in in the browser tab that opens, then paste the URL you land on
                  below. The token is stored by this app and refreshed automatically.
                </div>

                <div className="mb-2 flex items-center gap-2 text-xs">
                  {cgStatus.data?.has_token && !cgStatus.data?.expired ? (
                    <span className="rounded-full bg-green-100 px-2 py-0.5 font-medium text-green-700">
                      ✓ Connected
                    </span>
                  ) : cgStatus.data?.signed_in ? (
                    <span className="rounded-full bg-amber-100 px-2 py-0.5 font-medium text-amber-700">
                      Signed in — token stale
                    </span>
                  ) : (
                    <span className="rounded-full bg-gray-200 px-2 py-0.5 font-medium text-gray-600">
                      Not signed in
                    </span>
                  )}
                  {cgStatus.data?.account_id && (
                    <span className="text-gray-400">{cgStatus.data.account_id}</span>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => void chatgptGetLink()}
                    disabled={ghBusy}
                    className="rounded-lg border border-brand/40 bg-white px-3 py-1.5 text-sm font-medium text-brand hover:bg-brand/5 disabled:opacity-50"
                  >
                    Get sign-in link
                  </button>
                  {cgStatus.data?.signed_in && (
                    <button
                      onClick={() => void chatgptRefresh()}
                      disabled={ghBusy}
                      className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                    >
                      Force refresh
                    </button>
                  )}
                  {cgStatus.data?.signed_in && (
                    <button
                      onClick={() => void chatgptSignout()}
                      disabled={ghBusy}
                      className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                    >
                      Sign out
                    </button>
                  )}
                  {importMsg && <span className="text-xs text-gray-500">{importMsg}</span>}
                </div>

                {/* Sign-in link + paste (works on any host, including headless/remote) */}
                <div className="mt-3 space-y-2 text-xs text-gray-600">
                    <p className="text-[11px] text-gray-500">
                      Open the link, sign in, then paste the URL you land on
                      (it ends in <code className="rounded bg-white px-1">…/auth/callback?code=…</code>,
                      and won't load — that's expected).
                    </p>
                    {chatgptAuthUrl && (
                      <input
                        readOnly
                        value={chatgptAuthUrl}
                        onFocus={(e) => e.currentTarget.select()}
                        className="w-full rounded border bg-white px-2 py-1 text-[11px] text-gray-600"
                      />
                    )}
                    <div className="flex gap-2">
                      <input
                        type="text"
                        value={chatgptCallback}
                        onChange={(e) => setChatgptCallback(e.target.value)}
                        placeholder="Paste the redirected URL (…/auth/callback?code=…)"
                        className="flex-1 rounded border bg-white px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-brand"
                      />
                      <button
                        onClick={() => void chatgptComplete()}
                        disabled={ghBusy || !chatgptCallback.trim()}
                        className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50"
                      >
                        Complete
                      </button>
                    </div>
                </div>
              </div>
            )}

            {p.auth === "oauth" && p.id === "claude_oauth" && (
              <div className="rounded-lg border border-gray-200 bg-gray-50 p-3">
                <div className="mb-2 text-xs text-gray-600">
                  Sign in with your Claude Pro/Max subscription: click <b>Get sign-in
                  link</b>, sign in in the browser tab that opens, then paste the code
                  you're shown below. The token is stored by this app and refreshed
                  automatically.
                </div>

                <div className="mb-2 flex items-center gap-2 text-xs">
                  {claudeStatus.data?.has_token && !claudeStatus.data?.expired ? (
                    <span className="rounded-full bg-green-100 px-2 py-0.5 font-medium text-green-700">
                      ✓ Connected
                    </span>
                  ) : claudeStatus.data?.signed_in ? (
                    <span className="rounded-full bg-amber-100 px-2 py-0.5 font-medium text-amber-700">
                      Signed in — token stale
                    </span>
                  ) : (
                    <span className="rounded-full bg-gray-200 px-2 py-0.5 font-medium text-gray-600">
                      Not signed in
                    </span>
                  )}
                  {claudeStatus.data?.account_id && (
                    <span className="text-gray-400">{claudeStatus.data.account_id}</span>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => void claudeGetLink()}
                    disabled={ghBusy}
                    className="rounded-lg border border-brand/40 bg-white px-3 py-1.5 text-sm font-medium text-brand hover:bg-brand/5 disabled:opacity-50"
                  >
                    Get sign-in link
                  </button>
                  {claudeStatus.data?.signed_in && (
                    <button
                      onClick={() => void claudeRefresh()}
                      disabled={ghBusy}
                      className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                    >
                      Force refresh
                    </button>
                  )}
                  {claudeStatus.data?.signed_in && (
                    <button
                      onClick={() => void claudeSignout()}
                      disabled={ghBusy}
                      className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                    >
                      Sign out
                    </button>
                  )}
                  {importMsg && <span className="text-xs text-gray-500">{importMsg}</span>}
                </div>

                {/* Sign-in link + paste (works on any host, including headless/remote) */}
                <div className="mt-3 space-y-2 text-xs text-gray-600">
                    <p className="text-[11px] text-gray-500">
                      Open the link, sign in, then paste the code shown on the
                      Claude callback page (it looks like{" "}
                      <code className="rounded bg-white px-1">code#state</code>).
                    </p>
                    {claudeAuthUrl && (
                      <input
                        readOnly
                        value={claudeAuthUrl}
                        onFocus={(e) => e.currentTarget.select()}
                        className="w-full rounded border bg-white px-2 py-1 text-[11px] text-gray-600"
                      />
                    )}
                    <div className="flex gap-2">
                      <input
                        type="text"
                        value={claudeCallback}
                        onChange={(e) => setClaudeCallback(e.target.value)}
                        placeholder="Paste the code shown (code#state)"
                        className="flex-1 rounded border bg-white px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-brand"
                      />
                      <button
                        onClick={() => void claudeComplete()}
                        disabled={ghBusy || !claudeCallback.trim()}
                        className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white hover:bg-brand/90 disabled:opacity-50"
                      >
                        Complete
                      </button>
                    </div>
                </div>
              </div>
            )}

            {p.id === "openrouter" && (
              <Toggle
                label="Show only free models"
                hint="Limit the model list to OpenRouter's free models (ids ending in :free)."
                checked={form.freeOnly}
                onChange={(v) => {
                  setForms((m) => ({ ...m, [p.id]: { ...form, freeOnly: v } }));
                  void refreshModels(p.id, v);
                }}
              />
            )}

            <div>
              <div className="mb-1 flex items-center justify-between">
                <label className="text-xs font-medium text-gray-600">Model</label>
                <div className="flex items-center gap-3">
                  <button
                    onClick={() =>
                      setVisibilityOpen((v) => ({ ...v, [p.id]: !v[p.id] }))
                    }
                    className="text-[11px] text-brand hover:underline"
                    title="Hide individual models from the chat model picker"
                  >
                    {visibilityOpen[p.id]
                      ? "× Close visibility"
                      : `👁 Manage visibility${
                          (serverProv?.hidden_models?.length ?? 0) > 0
                            ? ` (${serverProv?.hidden_models?.length} hidden)`
                            : ""
                        }`}
                  </button>
                  <button
                    onClick={() => void refreshModels(p.id, p.id === "openrouter" ? form.freeOnly : undefined)}
                    className="text-[11px] text-brand hover:underline"
                    disabled={loadingModels === p.id}
                  >
                    {loadingModels === p.id ? "Loading…" : "↻ Refresh models"}
                  </button>
                </div>
              </div>
              <div className="flex gap-2">
                <select
                  value={form.model}
                  onChange={(e) =>
                    setForms((m) => ({ ...m, [p.id]: { ...form, model: e.target.value } }))
                  }
                  className="min-w-0 flex-1 truncate rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                >
                  {form.model && !modelList.includes(form.model) && (
                    <option value={form.model}>{form.model}</option>
                  )}
                  {modelList.map((m) => {
                    const hidden = (serverProv?.hidden_models ?? []).includes(m);
                    return (
                      <option key={m} value={m}>
                        {hidden ? `${m}  (hidden)` : m}
                      </option>
                    );
                  })}
                </select>
                <input
                  value={form.model}
                  onChange={(e) =>
                    setForms((m) => ({ ...m, [p.id]: { ...form, model: e.target.value } }))
                  }
                  placeholder="or type a model id"
                  className="w-48 rounded-lg border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand"
                />
              </div>

              {visibilityOpen[p.id] && (
                <ModelVisibilityPanel
                  provider={p.id}
                  allModels={modelList}
                  hidden={serverProv?.hidden_models ?? []}
                  filter={visibilityFilter[p.id] ?? ""}
                  setFilter={(s) =>
                    setVisibilityFilter((m) => ({ ...m, [p.id]: s }))
                  }
                  onToggle={(modelId, hide) => void toggleModelHidden(p.id, modelId, hide)}
                  onRefresh={() => void refreshModels(p.id, p.id === "openrouter" ? form.freeOnly : undefined)}
                  refreshing={loadingModels === p.id}
                />
              )}
            </div>

            <div className="mt-5 flex flex-wrap items-center gap-3 border-t pt-4">
              <button
                onClick={() => void save(false)}
                className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand/90"
              >
                Save changes
              </button>
              {activeProvider !== p.id && (
                <button
                  onClick={() => void save(true)}
                  className="rounded-lg border border-brand/40 bg-white px-4 py-2 text-sm font-medium text-brand hover:bg-brand/5"
                >
                  Set as default
                </button>
              )}
              <button
                onClick={() => void testProvider(p.id)}
                disabled={testing === p.id}
                title="Run a staged diagnostic against the saved credentials"
                className="rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
              >
                {testing === p.id ? "Testing…" : "Test connection"}
              </button>
              {saved && <span className="text-sm text-green-600">✓ Saved</span>}
              {error && <span className="text-sm text-red-600">{error}</span>}
            </div>

            {(testing === p.id || (testSteps[p.id]?.length ?? 0) > 0 || testResult[p.id]) && (
              <DiagnosticsPanel
                running={testing === p.id}
                steps={testSteps[p.id] ?? []}
                result={testResult[p.id]}
              />
            )}

            {(loadingModels === p.id || (refreshSteps[p.id]?.length ?? 0) > 0 || refreshResult[p.id]) && (
              <DiagnosticsPanel
                title="Model catalogue refresh"
                running={loadingModels === p.id}
                steps={refreshSteps[p.id] ?? []}
                result={refreshResult[p.id]}
                order={[
                  { id: "config", label: "Load configuration" },
                  { id: "endpoint", label: "Resolve endpoint (DNS)" },
                  { id: "connect", label: "Connect (TCP / TLS)" },
                  { id: "fetch", label: "Fetch model catalogue" },
                  { id: "complete", label: "Complete" },
                ]}
              />
            )}
          </div>
        );
      })}
        </div>
      </div>
    </section>
  );
}

// --- Per-provider Manage visibility panel ------------------------------------------
// Lets the admin hide / unhide individual models from the chat model picker. The
// admin's own model dropdown still shows everything (with a "(hidden)" annotation),
// so it's always possible to set a hidden model as the default if needed.
function ModelVisibilityPanel({
  provider,
  allModels,
  hidden,
  filter,
  setFilter,
  onToggle,
  onRefresh,
  refreshing,
}: {
  provider: string;
  allModels: string[];
  hidden: string[];
  filter: string;
  setFilter: (s: string) => void;
  onToggle: (modelId: string, hide: boolean) => void;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const hiddenSet = new Set(hidden);
  const f = filter.trim().toLowerCase();
  const filtered = f ? allModels.filter((m) => m.toLowerCase().includes(f)) : allModels;
  const hiddenCount = allModels.filter((m) => hiddenSet.has(m)).length;
  const visibleCount = allModels.length - hiddenCount;

  function bulkHideAll() {
    for (const m of filtered) {
      if (!hiddenSet.has(m)) onToggle(m, true);
    }
  }
  function bulkShowAll() {
    for (const m of filtered) {
      if (hiddenSet.has(m)) onToggle(m, false);
    }
  }

  return (
    <div className="mt-3 rounded-lg border border-gray-200 bg-gray-50/60 p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">
          Manage model visibility · {provider}
        </div>
        <div className="text-[11px] text-gray-500">
          {allModels.length} total · {visibleCount} visible · {hiddenCount} hidden
        </div>
      </div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter models…"
          className="min-w-0 flex-1 rounded border bg-white px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-brand"
        />
        <button
          onClick={onRefresh}
          disabled={refreshing}
          className="rounded-lg border border-gray-300 bg-white px-2 py-1 text-[11px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          {refreshing ? "Refreshing…" : "↻ Refresh"}
        </button>
        <button
          onClick={bulkShowAll}
          className="rounded-lg border border-gray-300 bg-white px-2 py-1 text-[11px] font-medium text-gray-700 hover:bg-gray-50"
          title="Show all models in the filtered list"
        >
          Show all
        </button>
        <button
          onClick={bulkHideAll}
          className="rounded-lg border border-gray-300 bg-white px-2 py-1 text-[11px] font-medium text-gray-700 hover:bg-gray-50"
          title="Hide all models in the filtered list"
        >
          Hide all
        </button>
      </div>
      {allModels.length === 0 ? (
        <p className="text-xs italic text-gray-500">
          No models loaded yet — click <span className="font-medium">↻ Refresh models</span> to fetch the catalogue.
        </p>
      ) : filtered.length === 0 ? (
        <p className="text-xs italic text-gray-500">No models match "{filter}".</p>
      ) : (
        <ul className="max-h-64 space-y-0.5 overflow-y-auto rounded border border-gray-200 bg-white p-1">
          {filtered.map((m) => {
            const isHidden = hiddenSet.has(m);
            return (
              <li
                key={m}
                className={`flex items-center justify-between gap-2 rounded px-2 py-1 text-xs ${
                  isHidden ? "bg-gray-50 text-gray-400" : "text-gray-700 hover:bg-gray-50"
                }`}
              >
                <span className="min-w-0 truncate font-mono">{m}</span>
                <button
                  onClick={() => onToggle(m, !isHidden)}
                  className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium transition ${
                    isHidden
                      ? "border-gray-300 text-gray-500 hover:bg-gray-100"
                      : "border-brand/40 text-brand hover:bg-brand/5"
                  }`}
                >
                  {isHidden ? "Show" : "Hide"}
                </button>
              </li>
            );
          })}
        </ul>
      )}
      <p className="mt-2 text-[11px] text-gray-500">
        Hidden models are removed from the chat model picker for everyone. You can still
        select a hidden model as the default here in Settings if you need to.
      </p>
    </div>
  );
}

// --- Connection diagnostics panel (renders the staged SSE events) -------------------
function DiagnosticsPanel({
  running,
  steps,
  result,
  title,
  order,
}: {
  running: boolean;
  steps: LlmTestStep[];
  result?: { ok: boolean; detail: string };
  title?: string;
  order?: { id: LlmTestStep["step"]; label: string }[];
}) {
  // Canonical phase order; we render every phase (greyed out until it arrives) so the
  // admin sees the whole pipeline up-front.
  const defaultOrder: { id: LlmTestStep["step"]; label: string }[] = [
    { id: "config", label: "Load configuration" },
    { id: "endpoint", label: "Resolve endpoint (DNS)" },
    { id: "connect", label: "Connect (TCP / TLS)" },
    { id: "auth", label: "Authenticate" },
    { id: "request", label: "Send probe request" },
    { id: "first_token", label: "Receive first token" },
    { id: "complete", label: "Complete" },
  ];
  const phaseOrder = order ?? defaultOrder;
  const byId: Record<string, LlmTestStep | undefined> = {};
  for (const s of steps) byId[s.step] = s;

  // The "currently running" phase is the first one without a recorded result.
  let runningIdx = -1;
  if (running) {
    runningIdx = phaseOrder.findIndex((o) => !byId[o.id]);
  }

  return (
    <div className="mt-4 rounded-lg border border-gray-200 bg-gray-50/60 p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">
          {title ?? "Connection diagnostics"}
        </div>
        {result && (
          <div
            className={`text-xs font-medium ${
              result.ok ? "text-green-700" : "text-red-700"
            }`}
            title={result.detail}
          >
            {result.ok ? "✓ Healthy" : "✗ Failed"} · {result.detail}
          </div>
        )}
      </div>
      <ol className="space-y-1.5">
        {phaseOrder.map((o, i) => {
          const s = byId[o.id];
          const isRunning = i === runningIdx;
          const pending = !s && !isRunning;
          const icon = s
            ? s.status === "ok"
              ? "✓"
              : s.status === "error"
                ? "✗"
                : s.status === "warn"
                  ? "!"
                  : "–"
            : isRunning
              ? "◌"
              : "·";
          const iconClass = s
            ? s.status === "ok"
              ? "bg-green-100 text-green-700"
              : s.status === "error"
                ? "bg-red-100 text-red-700"
                : s.status === "warn"
                  ? "bg-amber-100 text-amber-700"
                  : "bg-gray-100 text-gray-500"
            : isRunning
              ? "bg-blue-100 text-blue-700 animate-pulse"
              : "bg-gray-100 text-gray-400";
          const titleText = s ? s.title : o.label;
          return (
            <li
              key={o.id}
              className={`flex items-start gap-2 rounded px-2 py-1.5 text-sm ${
                pending ? "opacity-50" : ""
              } ${s?.status === "error" ? "bg-red-50/60" : ""}`}
            >
              <span
                className={`mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-bold ${iconClass}`}
                aria-hidden
              >
                {icon}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-gray-800">{titleText}</span>
                  {s?.ms !== undefined && (
                    <span className="text-[11px] text-gray-500">{s.ms} ms</span>
                  )}
                  {isRunning && (
                    <span className="text-[11px] italic text-blue-600">running…</span>
                  )}
                </div>
                {s?.detail && (
                  <div
                    className={`mt-0.5 break-words text-[12px] ${
                      s.status === "error" ? "text-red-600" : "text-gray-500"
                    }`}
                  >
                    {s.detail}
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// AMBA Reference Set is now a dedicated full-page rich editor — see AmbaReferenceEditor.tsx
// (rendered via the early return in AdminPanel for section === "amba").

// ===================== AMBA Change Requests (approval inbox) =====================
function AmbaChangeRequestsCard() {
  const qc = useQueryClient();
  const reqQ = useQuery({ queryKey: ["amba-approvals"], queryFn: () => api.ambaApprovals() });
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  async function decide(id: string, decision: string) {
    setBusy(id);
    setMsg(null);
    try {
      await api.decideAmbaApproval(id, { decision });
      await qc.invalidateQueries({ queryKey: ["amba-approvals"] });
      setMsg({ text: `Request ${decision}.`, ok: true });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  async function remove(id: string) {
    setBusy(id);
    try {
      await api.deleteAmbaApproval(id);
      await qc.invalidateQueries({ queryKey: ["amba-approvals"] });
    } catch (e) {
      setMsg({ text: formatError(e), ok: false });
    } finally {
      setBusy("");
    }
  }

  const STATUS_CLS: Record<string, string> = {
    pending: "bg-amber-100 text-amber-700",
    approved: "bg-green-100 text-green-700",
    rejected: "bg-red-100 text-red-700",
    applied: "bg-sky-100 text-sky-700",
  };

  const requests = reqQ.data?.requests ?? [];

  return (
    <Card title="AMBA Change Requests">
      <p className="mb-3 text-xs text-gray-500">
        Proposed monitoring-alert changes (generated IaC for coverage gaps) awaiting review.
        Approving records human sign-off only — this app never applies changes to Azure; export
        the IaC to your own pipeline.
      </p>
      {msg && (
        <div className={`mb-3 rounded-lg border p-2 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>
          {msg.text}
        </div>
      )}
      {requests.length === 0 ? (
        <p className="text-sm text-gray-400">No change requests yet.</p>
      ) : (
        <div className="space-y-2">
          {requests.map((r) => (
            <div key={r.id} className="rounded-lg border bg-white">
              <div className="flex flex-wrap items-center gap-2 px-3 py-2">
                <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${STATUS_CLS[r.status] ?? "bg-gray-100"}`}>{r.status}</span>
                <span className="text-sm font-medium text-gray-800">{r.scope_name || r.scope_id}</span>
                <span className="text-xs text-gray-500">{r.gap_count} gap(s) · {r.iac_format}</span>
                <span className="text-[11px] text-gray-400">by {r.requested_by}</span>
                <div className="ml-auto flex items-center gap-1.5">
                  <button onClick={() => setOpen(open === r.id ? null : r.id)} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50">{open === r.id ? "Hide IaC" : "View IaC"}</button>
                  {r.status === "pending" && (
                    <>
                      <button onClick={() => void decide(r.id, "approved")} disabled={busy === r.id} className="rounded border border-green-300 px-2 py-0.5 text-[11px] text-green-700 hover:bg-green-50 disabled:opacity-50">Approve</button>
                      <button onClick={() => void decide(r.id, "rejected")} disabled={busy === r.id} className="rounded border border-red-300 px-2 py-0.5 text-[11px] text-red-700 hover:bg-red-50 disabled:opacity-50">Reject</button>
                    </>
                  )}
                  {r.status === "approved" && (
                    <button onClick={() => void decide(r.id, "applied")} disabled={busy === r.id} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50 disabled:opacity-50">Mark applied</button>
                  )}
                  <button onClick={() => void remove(r.id)} disabled={busy === r.id} className="rounded border px-2 py-0.5 text-[11px] text-gray-400 hover:bg-gray-50 disabled:opacity-50">Delete</button>
                </div>
              </div>
              {open === r.id && (
                <pre className="max-h-72 overflow-auto border-t bg-gray-900 p-3 text-[10px] leading-relaxed text-gray-100">{r.iac_text || "(IaC not stored)"}</pre>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// Telemetry Reference Set is now a dedicated full-page rich editor — see
// TelemetryReferenceEditor.tsx (rendered via the early return in AdminPanel).

// ===================== Telemetry Change Requests (approval inbox) =====================
function TelemetryChangeRequestsCard() {
  const qc = useQueryClient();
  const reqQ = useQuery({ queryKey: ["telemetry-approvals"], queryFn: () => api.telemetryApprovals() });
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  async function decide(id: string, decision: string) {
    setBusy(id); setMsg(null);
    try {
      await api.decideTelemetryApproval(id, { decision });
      await qc.invalidateQueries({ queryKey: ["telemetry-approvals"] });
      setMsg({ text: `Request ${decision}.`, ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(""); }
  }

  async function remove(id: string) {
    setBusy(id);
    try {
      await api.deleteTelemetryApproval(id);
      await qc.invalidateQueries({ queryKey: ["telemetry-approvals"] });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(""); }
  }

  const STATUS_CLS: Record<string, string> = {
    pending: "bg-amber-100 text-amber-700", approved: "bg-green-100 text-green-700",
    rejected: "bg-red-100 text-red-700", applied: "bg-sky-100 text-sky-700",
  };
  const requests = reqQ.data?.requests ?? [];

  return (
    <Card title="Telemetry Change Requests">
      <p className="mb-3 text-xs text-gray-500">
        Proposed diagnostic-settings remediations (Bicep / Azure Policy) awaiting review.
        Approving records human sign-off only — the app never applies changes; export the
        artifact to your own pipeline.
      </p>
      {msg && (
        <div className={`mb-3 rounded-lg border p-2 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
      )}
      {requests.length === 0 ? (
        <p className="text-sm text-gray-400">No change requests yet.</p>
      ) : (
        <div className="space-y-2">
          {requests.map((r) => (
            <div key={r.id} className="rounded-lg border bg-white">
              <div className="flex flex-wrap items-center gap-2 px-3 py-2">
                <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${STATUS_CLS[r.status] ?? "bg-gray-100"}`}>{r.status}</span>
                <span className="text-sm font-medium text-gray-800">{r.scope_name || r.scope_id}</span>
                <span className="text-xs text-gray-500">{r.gap_count} gap(s) · {r.iac_format}</span>
                <span className="text-[11px] text-gray-400">by {r.requested_by}</span>
                <div className="ml-auto flex items-center gap-1.5">
                  <button onClick={() => setOpen(open === r.id ? null : r.id)} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50">{open === r.id ? "Hide" : "View"}</button>
                  {r.status === "pending" && (
                    <>
                      <button onClick={() => void decide(r.id, "approved")} disabled={busy === r.id} className="rounded border border-green-300 px-2 py-0.5 text-[11px] text-green-700 hover:bg-green-50 disabled:opacity-50">Approve</button>
                      <button onClick={() => void decide(r.id, "rejected")} disabled={busy === r.id} className="rounded border border-red-300 px-2 py-0.5 text-[11px] text-red-700 hover:bg-red-50 disabled:opacity-50">Reject</button>
                    </>
                  )}
                  {r.status === "approved" && (
                    <button onClick={() => void decide(r.id, "applied")} disabled={busy === r.id} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50 disabled:opacity-50">Mark applied</button>
                  )}
                  <button onClick={() => void remove(r.id)} disabled={busy === r.id} className="rounded border px-2 py-0.5 text-[11px] text-gray-400 hover:bg-gray-50 disabled:opacity-50">Delete</button>
                </div>
              </div>
              {open === r.id && (
                <pre className="max-h-72 overflow-auto border-t bg-gray-900 p-3 text-[10px] leading-relaxed text-gray-100">{r.iac_text || "(not stored)"}</pre>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// Backup/DR Reference Set is now a dedicated full-page rich editor — see
// BackupDrReferenceEditor.tsx (rendered via the early return in AdminPanel).

// ===================== Backup/DR Change Requests (approval inbox) =====================
function BackupDrChangeRequestsCard() {
  const qc = useQueryClient();
  const reqQ = useQuery({ queryKey: ["backupdr-approvals"], queryFn: () => api.backupDrApprovals() });
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  async function decide(id: string, decision: string) {
    setBusy(id); setMsg(null);
    try {
      await api.decideBackupDrApproval(id, { decision });
      await qc.invalidateQueries({ queryKey: ["backupdr-approvals"] });
      setMsg({ text: `Request ${decision}.`, ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(""); }
  }
  async function remove(id: string) {
    setBusy(id);
    try {
      await api.deleteBackupDrApproval(id);
      await qc.invalidateQueries({ queryKey: ["backupdr-approvals"] });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(""); }
  }

  const STATUS_CLS: Record<string, string> = {
    pending: "bg-amber-100 text-amber-700", approved: "bg-green-100 text-green-700",
    rejected: "bg-red-100 text-red-700", applied: "bg-sky-100 text-sky-700",
  };
  const requests = reqQ.data?.requests ?? [];

  return (
    <Card title="Backup/DR Change Requests">
      <p className="mb-3 text-xs text-gray-500">
        Proposed backup/DR remediations (Bicep / runbook) awaiting review. Approving records
        human sign-off only — the app never applies changes; export the artifact to your pipeline.
      </p>
      {msg && (
        <div className={`mb-3 rounded-lg border p-2 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
      )}
      {requests.length === 0 ? (
        <p className="text-sm text-gray-400">No change requests yet.</p>
      ) : (
        <div className="space-y-2">
          {requests.map((r) => (
            <div key={r.id} className="rounded-lg border bg-white">
              <div className="flex flex-wrap items-center gap-2 px-3 py-2">
                <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${STATUS_CLS[r.status] ?? "bg-gray-100"}`}>{r.status}</span>
                <span className="text-sm font-medium text-gray-800">{r.scope_name || r.scope_id}</span>
                <span className="text-xs text-gray-500">{r.gap_count} gap(s) · {r.iac_format}</span>
                <span className="text-[11px] text-gray-400">by {r.requested_by}</span>
                <div className="ml-auto flex items-center gap-1.5">
                  <button onClick={() => setOpen(open === r.id ? null : r.id)} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50">{open === r.id ? "Hide" : "View"}</button>
                  {r.status === "pending" && (
                    <>
                      <button onClick={() => void decide(r.id, "approved")} disabled={busy === r.id} className="rounded border border-green-300 px-2 py-0.5 text-[11px] text-green-700 hover:bg-green-50 disabled:opacity-50">Approve</button>
                      <button onClick={() => void decide(r.id, "rejected")} disabled={busy === r.id} className="rounded border border-red-300 px-2 py-0.5 text-[11px] text-red-700 hover:bg-red-50 disabled:opacity-50">Reject</button>
                    </>
                  )}
                  {r.status === "approved" && (
                    <button onClick={() => void decide(r.id, "applied")} disabled={busy === r.id} className="rounded border px-2 py-0.5 text-[11px] hover:bg-gray-50 disabled:opacity-50">Mark applied</button>
                  )}
                  <button onClick={() => void remove(r.id)} disabled={busy === r.id} className="rounded border px-2 py-0.5 text-[11px] text-gray-400 hover:bg-gray-50 disabled:opacity-50">Delete</button>
                </div>
              </div>
              {open === r.id && (
                <pre className="max-h-72 overflow-auto border-t bg-gray-900 p-3 text-[10px] leading-relaxed text-gray-100">{r.iac_text || "(not stored)"}</pre>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// ===================== Retirement Radar Reference =====================
function RadarReferenceCard() {
  const qc = useQueryClient();
  const refQ = useQuery({ queryKey: ["radar-reference"], queryFn: api.radarReference });
  const revsQ = useQuery({ queryKey: ["radar-reference-revisions"], queryFn: api.radarReferenceRevisions });
  const settingsQ = useQuery({ queryKey: ["app-settings"], queryFn: api.appSettings });
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [lead, setLead] = useState("");
  const [feedOn, setFeedOn] = useState(false);
  const [feedUrl, setFeedUrl] = useState("");

  const ref = refQ.data;
  const ruleCount = ref?.classification_rules?.length ?? 0;
  const modelCount = ref?.model_lifecycle?.length ?? 0;
  const settings = settingsQ.data?.settings;

  function startEdit() {
    setDraft(JSON.stringify({ classification_rules: ref?.classification_rules ?? [], model_lifecycle: ref?.model_lifecycle ?? [] }, null, 2));
    setEditing(true);
    setMsg(null);
  }

  async function save() {
    let parsed: { classification_rules?: unknown; model_lifecycle?: unknown };
    try { parsed = JSON.parse(draft); } catch { setMsg({ text: "Invalid JSON.", ok: false }); return; }
    setBusy(true);
    try {
      await api.updateRadarReference({
        classification_rules: (parsed.classification_rules as never) ?? [],
        model_lifecycle: (parsed.model_lifecycle as never) ?? [],
        reason: "Edited in Admin",
      });
      await qc.invalidateQueries({ queryKey: ["radar-reference"] });
      await qc.invalidateQueries({ queryKey: ["radar-reference-revisions"] });
      setEditing(false);
      setMsg({ text: "Saved a new reference version.", ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(false); }
  }

  async function reset() {
    if (!window.confirm("Reset the radar reference (classification rules + model lifecycle) to the built-in seed?")) return;
    setBusy(true);
    try {
      await api.resetRadarReference();
      await qc.invalidateQueries({ queryKey: ["radar-reference"] });
      await qc.invalidateQueries({ queryKey: ["radar-reference-revisions"] });
      setMsg({ text: "Reset to built-in seed.", ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(false); }
  }

  async function restore(id: string) {
    setBusy(true);
    try {
      await api.restoreRadarReference(id);
      await qc.invalidateQueries({ queryKey: ["radar-reference"] });
      await qc.invalidateQueries({ queryKey: ["radar-reference-revisions"] });
      setMsg({ text: "Restored revision as a new version.", ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(false); }
  }

  async function saveSettings() {
    const leadDays = lead
      .split(/[,\s]+/)
      .map((x) => parseInt(x, 10))
      .filter((n) => Number.isFinite(n) && n > 0);
    setBusy(true);
    try {
      await api.updateAppSettings({
        radar_digest_lead_days: leadDays.length ? leadDays : undefined,
        radar_azure_updates_feed_enabled: feedOn,
        radar_azure_updates_feed_url: feedUrl,
      });
      await qc.invalidateQueries({ queryKey: ["app-settings"] });
      setMsg({ text: "Saved radar settings.", ok: true });
    } catch (e) { setMsg({ text: formatError(e), ok: false }); } finally { setBusy(false); }
  }

  return (
    <>
      <Card title="Retirement Radar Reference">
        <p className="mb-3 text-xs text-gray-500">
          Event classification rules (Retirement vs. Permanent Breaking Change + recommended
          replacement / migration links) and the Azure OpenAI / Foundry model-lifecycle table.
          Versioned; restore or reset to the built-in seed — no redeploy.
        </p>
        {msg && (
          <div className={`mb-3 rounded-lg border p-2 text-xs ${msg.ok ? "border-green-200 bg-green-50 text-green-700" : "border-red-200 bg-red-50 text-red-700"}`}>{msg.text}</div>
        )}
        <div className="mb-3 flex flex-wrap items-center gap-3 text-xs text-gray-600">
          <span>Version <b>{ref?.version ?? 0}</b></span>
          <span>{ruleCount} classification rule(s)</span>
          <span>{modelCount} model(s)</span>
          {ref?.updated_by && <span className="text-gray-400">last edited by {ref.updated_by}</span>}
        </div>
        <div className="mb-3 flex gap-2">
          {!editing ? (
            <>
              <button onClick={startEdit} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium hover:bg-gray-50">Edit JSON</button>
              <button onClick={() => void reset()} disabled={busy} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium hover:bg-gray-50 disabled:opacity-50">Reset to built-in</button>
            </>
          ) : (
            <>
              <button onClick={() => void save()} disabled={busy} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50">{busy ? "Saving…" : "Save new version"}</button>
              <button onClick={() => setEditing(false)} className="rounded-lg border bg-white px-3 py-1.5 text-xs font-medium hover:bg-gray-50">Cancel</button>
            </>
          )}
        </div>
        {editing ? (
          <textarea value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false}
            className="h-96 w-full rounded-lg border bg-gray-900 p-3 font-mono text-[11px] text-gray-100 focus:outline-none" />
        ) : (
          <div className="max-h-72 space-y-2 overflow-auto">
            {(ref?.model_lifecycle ?? []).map((m) => (
              <div key={`${m.model}-${m.version}`} className="flex items-center gap-2 rounded-lg border bg-white p-2 text-xs">
                <span className="font-medium text-gray-800">{m.model} {m.version}</span>
                <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">{m.stage}</span>
                <span className="ml-auto text-gray-400">retires {m.retirement_date || "—"}</span>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="Scheduled digest & external feed">
        <p className="mb-3 text-xs text-gray-500">
          The scheduled Radar digest (a <code>radar</code> automation) pushes only NEW + deadline-
          approaching items via your notification channels. Configure the lead-time thresholds and
          the optional public Azure Updates feed (the only net-new external fetch; off by default
          and may lag announcements by ~2 weeks).
        </p>
        <div className="space-y-3 text-sm">
          <label className="block">
            <span className="text-xs text-gray-600">Digest lead-time thresholds (days, comma-separated)</span>
            <input
              value={lead}
              onChange={(e) => setLead(e.target.value)}
              placeholder={(settings?.radar_digest_lead_days ?? [90, 60, 30]).join(", ")}
              className="mt-1 w-full rounded-lg border px-2 py-1.5 text-sm"
            />
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={feedOn} onChange={(e) => setFeedOn(e.target.checked)} />
            <span className="text-sm text-gray-700">Enable public Azure Updates retirements feed</span>
          </label>
          <label className="block">
            <span className="text-xs text-gray-600">Feed URL (blank = Microsoft default)</span>
            <input
              value={feedUrl}
              onChange={(e) => setFeedUrl(e.target.value)}
              placeholder={settings?.radar_azure_updates_feed_url || "https://www.microsoft.com/releasecommunications/api/v2/azure/rss"}
              className="mt-1 w-full rounded-lg border px-2 py-1.5 text-sm"
            />
          </label>
          <button onClick={() => void saveSettings()} disabled={busy} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50">
            {busy ? "Saving…" : "Save radar settings"}
          </button>
        </div>
      </Card>

      <Card title="Version history">
        <div className="space-y-1 text-xs">
          {(revsQ.data?.revisions ?? []).length === 0 && <p className="text-gray-400">No revisions yet.</p>}
          {(revsQ.data?.revisions ?? []).map((r) => (
            <div key={r.id} className="flex items-center gap-2 rounded border bg-white px-2 py-1.5">
              <span className="font-medium">v{r.version}</span>
              <span className="text-gray-500">{r.reason}</span>
              <span className="text-gray-400">{r.rule_count} rules · {r.model_count} models</span>
              <span className="ml-auto text-gray-400">{r.by}</span>
              <button onClick={() => void restore(r.id)} disabled={busy} className="rounded border px-2 py-0.5 hover:bg-gray-50 disabled:opacity-50">Restore</button>
            </div>
          ))}
        </div>
      </Card>
    </>
  );
}

// --- Settings nav line icons (monochrome, ChatGPT-style) ---------------------
