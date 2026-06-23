// Ownership — owners/teams directory, owner↔subject assignments, federated people-picker,
// and (in later phases) coverage, suggestions, my-estate and attestation. URL-driven tabs
// (/ownership/:tab) so a refresh restores the view. User-level (ownership.read to view,
// ownership.write to mutate).
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type AttestationItem,
  type DirectoryHit,
  type Owner,
  type OwnerEstate,
  type OwnershipAssignment,
  type OwnershipScope,
  type OwnershipSubject,
  type OwnershipSuggestion,
} from "../api";
import { formatError } from "../utils/format";
import { usePersistedState } from "../utils/persistedState";
import { OWNERSHIP_NAV, type OwnershipTab } from "./navConfig";
import { type ScopeKind } from "./ScopePicker";
import { SubscriptionScopePicker } from "./SubscriptionScopePicker";
import { ConnectionScopePicker } from "./ConnectionScopePicker";
import { AzureIcon } from "./AzureIcon";
import { OwnerExportButtons, OwnerImportModal, OwnerTagApplyModal, TagRevisionsPanel } from "./ownership/OwnerImportTags";
import { TrendChart } from "./TrendChart";

const ROLE_LABELS: Record<string, string> = {
  technical: "Technical",
  business: "Business",
  security: "Security",
  cost: "Cost",
  operations: "Operations",
  escalation: "Escalation",
};

const KIND_ICON: Record<string, string> = { person: "🧑", team: "👥", service: "🤖" };
const SUBJECT_ICON: Record<string, string> = {
  mg: "🏢",
  subscription: "🔑",
  resource_group: "📁",
  resource: "🧱",
  workload: "🧩",
  architecture: "🗺️",
};
const SOURCE_BADGE: Record<string, { label: string; cls: string }> = {
  direct: { label: "Direct", cls: "bg-emerald-100 text-emerald-700" },
  tag: { label: "Tag", cls: "bg-sky-100 text-sky-700" },
  workload: { label: "Workload", cls: "bg-indigo-100 text-indigo-700" },
  inherited: { label: "Inherited", cls: "bg-amber-100 text-amber-700" },
  none: { label: "Unowned", cls: "bg-rose-100 text-rose-700" },
};

export function OwnershipPanel({ tab }: { tab: OwnershipTab }) {
  const qc = useQueryClient();
  const [scope, setScope] = usePersistedState<OwnershipScope>("azsup.ownership.scope", {
    kind: "tenant", workloadId: "", subId: "", subName: "",
  });
  // The connection (Azure-tenant) picker scopes the Azure SCANS — coverage/suggestions/the
  // subjects overview and the subscription tree — to the chosen tenant. The owner/assignment
  // directory is shared across connections (Option A), so Directory + My Estate ignore it.
  const [connectionId, setConnectionId] = usePersistedState("azsup.ownership.connectionId", "");
  // Tabs that respect the section scope show the scope bar; owner-centric tabs (Directory,
  // My Estate) are a tenant-wide directory, so they don't.
  const scopeAware = tab === "assignments" || tab === "coverage" || tab === "suggestions" || tab === "attestation";
  // Deep link: /ownership/<tab>?workload_id=… (e.g. from the workload detail page) opens this
  // section already scoped to that workload.
  useEffect(() => {
    const wid = new URLSearchParams(window.location.search).get("workload_id");
    if (wid) setScope({ kind: "workload", workloadId: wid, subId: "", subName: "" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // Switching connection: a previously picked subscription/workload may not belong to the new
  // connection, so reset to Tenant and refetch every scope-aware query.
  const onConnectionChange = (id: string) => {
    setConnectionId(id);
    setScope({ kind: "tenant", workloadId: "", subId: "", subName: "" });
    qc.invalidateQueries({ queryKey: ["ownership"] });
  };
  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="border-b bg-white px-6 py-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <span className="text-xl">🪪</span>
            <h1 className="text-lg font-semibold text-gray-900">Ownership</h1>
          </div>
          {scopeAware && (
            <div className="flex items-center gap-2">
              <ConnectionScopePicker value={connectionId} onChange={onConnectionChange} align="right" />
              <OwnershipScopeBar scope={scope} onChange={setScope} connectionId={connectionId} />
            </div>
          )}
        </div>
        <p className="mt-1 text-sm text-gray-500">
          Assign accountable owners and teams to subscriptions, resource groups, resources,
          workloads and architectures — then manage and group your estate by owner.
        </p>
        <nav className="mt-3 flex flex-wrap gap-1">
          {OWNERSHIP_NAV.map((n) => (
            <Link
              key={n.id}
              to={`/ownership/${n.id}`}
              className={`rounded-lg px-3 py-1.5 text-sm transition ${
                tab === n.id
                  ? "bg-indigo-600 text-white"
                  : "text-gray-600 hover:bg-gray-100"
              }`}
            >
              {n.label}
            </Link>
          ))}
        </nav>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto bg-gray-50 p-6">
        {tab === "directory" && <DirectoryTab />}
        {tab === "assignments" && <AssignmentsTab scope={scope} connectionId={connectionId} />}
        {tab === "coverage" && <CoverageTab scope={scope} onScopeChange={setScope} connectionId={connectionId} />}
        {tab === "suggestions" && <SuggestionsTab scope={scope} connectionId={connectionId} />}
        {tab === "estate" && <EstateTab />}
        {tab === "attestation" && <AttestationTab scope={scope} />}
      </div>
    </div>
  );
}

// Section-wide scope selector (Tenant / Subscription / Workload), consistent with the
// Proactive Support modules. Tenant = the whole directory (no filter).
function OwnershipScopeBar({ scope, onChange, connectionId }: { scope: OwnershipScope; onChange: (s: OwnershipScope) => void; connectionId: string }) {
  const workloadsQ = useQuery({ queryKey: ["workloads", "list"], queryFn: api.workloads });
  const workloads = workloadsQ.data?.workloads ?? [];
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs font-medium text-gray-500">Scope</span>
      <div className="flex items-center rounded-lg border bg-gray-50 p-0.5 text-xs">
        {(["tenant", "subscription", "workload"] as const).map((k) => (
          <button
            key={k}
            onClick={() => onChange({ ...scope, kind: k })}
            className={`flex items-center gap-1 rounded-md px-2.5 py-1 capitalize ${
              scope.kind === k ? "bg-white font-medium text-gray-900 shadow-sm" : "text-gray-500"
            }`}
          >
            <AzureIcon kind={k === "tenant" ? "tenant" : k === "subscription" ? "subscription" : "workload"} className="h-3.5 w-3.5" />
            {k}
          </button>
        ))}
      </div>
      {scope.kind === "workload" && (
        <div className="relative">
          <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2">
            <AzureIcon kind="workload" className="h-3.5 w-3.5" />
          </span>
          <select
            value={scope.workloadId}
            onChange={(e) => onChange({ ...scope, workloadId: e.target.value })}
            className="max-w-[220px] rounded-lg border py-1.5 pl-7 pr-2 text-xs"
          >
            <option value="">All workloads…</option>
            {workloads.map((w) => (
              <option key={w.id} value={w.id}>{w.name}</option>
            ))}
          </select>
        </div>
      )}
      {scope.kind === "subscription" && (
        <SubscriptionScopePicker
          value={scope.subId}
          valueName={scope.subName}
          connectionId={connectionId}
          onPick={(id, name) => onChange({ ...scope, subId: id, subName: name })}
        />
      )}
    </div>
  );
}

// ============================================================ Directory (owners & teams)
function DirectoryTab() {
  const qc = useQueryClient();
  const [showTrash, setShowTrash] = useState(false);
  const [picking, setPicking] = useState(false);
  const [editing, setEditing] = useState<Owner | null>(null);
  const [msg, setMsg] = useState("");
  const [importing, setImporting] = useState(false);
  const [tagApply, setTagApply] = useState(false);
  const [showRevisions, setShowRevisions] = useState(false);

  const ownersQ = useQuery({ queryKey: ["ownership", "owners"], queryFn: api.ownershipOwners });
  const trashQ = useQuery({ queryKey: ["ownership", "owners", "trash"], queryFn: api.ownersTrash, enabled: showTrash });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["ownership", "owners"] });
    qc.invalidateQueries({ queryKey: ["ownership", "subjects"] });
  };

  const del = useMutation({
    mutationFn: (id: string) => api.deleteOwner(id),
    onSuccess: () => { setMsg("Owner moved to Trash."); invalidate(); },
    onError: (e) => setMsg(formatError(e)),
  });
  const restore = useMutation({
    mutationFn: (id: string) => api.restoreOwner(id),
    onSuccess: () => { invalidate(); qc.invalidateQueries({ queryKey: ["ownership", "owners", "trash"] }); },
  });
  const purge = useMutation({
    mutationFn: (id: string) => api.purgeOwner(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ownership", "owners", "trash"] }),
  });

  const owners = ownersQ.data?.owners ?? [];

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm text-gray-500">{owners.length} owner{owners.length === 1 ? "" : "s"}</div>
        <div className="flex flex-wrap items-center gap-2">
          <OwnerExportButtons />
          <button onClick={() => setImporting(true)} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">📥 Import</button>
          <button onClick={() => setTagApply(true)} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50">🏷️ Apply as tags</button>
          <button
            onClick={() => setShowRevisions((v) => !v)}
            className={`rounded-lg border px-3 py-1.5 text-sm ${showRevisions ? "border-indigo-300 bg-indigo-50 text-indigo-700" : "bg-white text-gray-600 hover:bg-gray-50"}`}
          >
            ↩ Revisions
          </button>
          <button
            onClick={() => setShowTrash((v) => !v)}
            className={`rounded-lg border px-3 py-1.5 text-sm ${showTrash ? "border-indigo-300 bg-indigo-50 text-indigo-700" : "bg-white text-gray-600 hover:bg-gray-50"}`}
          >
            🗑 Trash{trashQ.data?.owners.length ? ` (${trashQ.data.owners.length})` : ""}
          </button>
          <button onClick={() => { setEditing(null); setPicking(true); }} className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700">
            + Add owner
          </button>
        </div>
      </div>

      {importing && <OwnerImportModal onClose={() => setImporting(false)} onImported={invalidate} />}
      {tagApply && <OwnerTagApplyModal onClose={() => setTagApply(false)} onApplied={() => qc.invalidateQueries({ queryKey: ["tag-revisions", "ownership"] })} />}
      {showRevisions && <div className="mb-4"><TagRevisionsPanel mode="ownership" /></div>}

      {msg && <div className="mb-3 rounded-lg border bg-white px-3 py-2 text-sm text-gray-600">{msg}</div>}

      {showTrash ? (
        <div className="rounded-xl border bg-white p-4">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-gray-700">Trash</h3>
            {!!trashQ.data?.owners.length && (
              <button
                onClick={() => { if (confirm("Permanently delete all trashed owners?")) api.emptyOwnersTrash().then(() => qc.invalidateQueries({ queryKey: ["ownership", "owners", "trash"] })); }}
                className="text-xs text-rose-600 hover:underline"
              >
                Empty trash
              </button>
            )}
          </div>
          {(trashQ.data?.owners ?? []).length === 0 ? (
            <p className="py-6 text-center text-sm text-gray-400">Trash is empty.</p>
          ) : (
            <ul className="divide-y">
              {trashQ.data!.owners.map((o) => (
                <li key={o.id} className="flex items-center justify-between py-2 text-sm">
                  <span>{KIND_ICON[o.kind]} {o.display_name}</span>
                  <span className="flex gap-2">
                    <button onClick={() => restore.mutate(o.id)} className="text-indigo-600 hover:underline">↩ Restore</button>
                    <button onClick={() => purge.mutate(o.id)} className="text-rose-600 hover:underline">Delete forever</button>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : owners.length === 0 ? (
        <div className="rounded-xl border border-dashed bg-white p-10 text-center">
          <p className="text-sm text-gray-500">No owners yet. Add a person or team — pick from your directory (SSO / Entra) or type one in.</p>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {owners.map((o) => (
            <OwnerCard key={o.id} owner={o} onEdit={() => { setEditing(o); setPicking(true); }} onDelete={() => del.mutate(o.id)} />
          ))}
        </div>
      )}

      {picking && (
        <OwnerEditorModal
          owner={editing}
          onClose={() => setPicking(false)}
          onSaved={() => { setPicking(false); invalidate(); }}
        />
      )}
    </div>
  );
}

function OwnerCard({ owner, onEdit, onDelete }: { owner: Owner; onEdit: () => void; onDelete: () => void }) {
  const linked = owner.source !== "manual";
  return (
    <div className="rounded-xl border bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-lg">{KIND_ICON[owner.kind]}</span>
            <span className="truncate font-medium text-gray-900">{owner.display_name}</span>
          </div>
          {owner.email && <div className="truncate text-xs text-gray-500">{owner.email}</div>}
        </div>
        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] uppercase tracking-wide text-gray-500">{owner.kind}</span>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-1.5 text-[11px]">
        {linked && (
          <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-emerald-700" title="Linked to a directory identity">
            🔗 {owner.source === "app_user" ? "SSO user" : owner.source === "entra" ? "Entra" : owner.source === "oidc_group" ? "OIDC group" : "RBAC"}
          </span>
        )}
        <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-indigo-700">{owner.assignment_count ?? 0} assigned</span>
      </div>
      <div className="mt-3 flex justify-end gap-3 text-xs">
        <button onClick={onEdit} className="text-gray-600 hover:underline">Edit</button>
        <button onClick={onDelete} className="text-rose-600 hover:underline">Delete</button>
      </div>
    </div>
  );
}

// ============================================================ People-picker + owner editor
function OwnerEditorModal({ owner, onClose, onSaved }: { owner: Owner | null; onClose: () => void; onSaved: () => void }) {
  const [tab, setTab] = useState<"picker" | "manual">(owner ? "manual" : "picker");
  const [kind, setKind] = useState<Owner["kind"]>(owner?.kind ?? "person");
  const [displayName, setDisplayName] = useState(owner?.display_name ?? "");
  const [email, setEmail] = useState(owner?.email ?? "");
  const [notes, setNotes] = useState(owner?.notes ?? "");
  const [delegateTo, setDelegateTo] = useState(owner?.delegate?.owner_id ?? "");
  const [delegateUntil, setDelegateUntil] = useState(owner?.delegate?.until ?? "");
  const [err, setErr] = useState("");

  // Other owners that this one can delegate accountability to (only when editing).
  const ownersQ = useQuery({ queryKey: ["ownership", "owners"], queryFn: api.ownershipOwners, enabled: !!owner });

  const save = useMutation({
    mutationFn: () =>
      api.upsertOwner({
        id: owner?.id, kind, display_name: displayName, email, notes,
        source: owner?.source ?? "manual", link: owner?.link ?? {},
        delegate: delegateTo ? { owner_id: delegateTo, until: delegateUntil, reason: "" } : {},
      }),
    onSuccess: onSaved,
    onError: (e) => setErr(formatError(e)),
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-lg rounded-2xl bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-5 py-3">
          <h3 className="font-semibold text-gray-900">{owner ? "Edit owner" : "Add owner"}</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">✕</button>
        </div>

        {!owner && (
          <div className="flex gap-1 border-b px-5 pt-3">
            <button onClick={() => setTab("picker")} className={`rounded-t-lg px-3 py-1.5 text-sm ${tab === "picker" ? "bg-indigo-50 text-indigo-700" : "text-gray-500"}`}>From directory</button>
            <button onClick={() => setTab("manual")} className={`rounded-t-lg px-3 py-1.5 text-sm ${tab === "manual" ? "bg-indigo-50 text-indigo-700" : "text-gray-500"}`}>Manual</button>
          </div>
        )}

        {tab === "picker" && !owner ? (
          <PeoplePicker onPicked={onSaved} />
        ) : (
          <div className="space-y-3 p-5">
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-600">Kind</label>
              <div className="flex gap-2">
                {(["person", "team", "service"] as const).map((k) => (
                  <button key={k} onClick={() => setKind(k)} className={`rounded-lg border px-3 py-1.5 text-sm ${kind === k ? "border-indigo-400 bg-indigo-50 text-indigo-700" : "bg-white text-gray-600"}`}>
                    {KIND_ICON[k]} {k}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-600">Display name</label>
              <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="w-full rounded-lg border px-3 py-2 text-sm" placeholder="John Doe / Platform Team" />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-600">Email / DL</label>
              <input value={email} onChange={(e) => setEmail(e.target.value)} className="w-full rounded-lg border px-3 py-2 text-sm" placeholder="john@contoso.com" />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-600">Notes</label>
              <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} className="w-full rounded-lg border px-3 py-2 text-sm" />
            </div>
            {owner && (
              <div className="rounded-lg border border-amber-200 bg-amber-50/50 p-3">
                <label className="mb-1 block text-xs font-medium text-amber-700">Delegate accountability (RACI cover)</label>
                <div className="flex gap-2">
                  <select value={delegateTo} onChange={(e) => setDelegateTo(e.target.value)} className="flex-1 rounded-lg border px-2 py-1.5 text-sm">
                    <option value="">No delegation</option>
                    {(ownersQ.data?.owners ?? []).filter((o) => o.id !== owner.id).map((o) => (
                      <option key={o.id} value={o.id}>{KIND_ICON[o.kind]} {o.display_name}</option>
                    ))}
                  </select>
                  <input type="date" value={delegateUntil} onChange={(e) => setDelegateUntil(e.target.value)} disabled={!delegateTo} className="rounded-lg border px-2 py-1.5 text-sm disabled:opacity-50" title="Delegate until" />
                </div>
                <p className="mt-1 text-[11px] text-amber-600">While active, this owner's estate shows the delegate as cover (e.g. vacation / on-call).</p>
              </div>
            )}
            {err && <div className="text-sm text-rose-600">{err}</div>}
            <div className="flex justify-end gap-2 pt-1">
              <button onClick={onClose} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600">Cancel</button>
              <button onClick={() => save.mutate()} disabled={save.isPending} className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
                {save.isPending ? "Saving…" : "Save owner"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function PeoplePicker({ onPicked }: { onPicked: () => void }) {
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [err, setErr] = useState("");

  // debounce the query so each keystroke doesn't fire a directory search
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q), 300);
    return () => clearTimeout(t);
  }, [q]);

  const searchQ = useQuery({
    queryKey: ["ownership", "directory", debounced],
    queryFn: () => api.directorySearch(debounced),
    enabled: debounced.trim().length >= 2,
  });

  const pick = useMutation({
    mutationFn: (hit: DirectoryHit) => api.ownerFromDirectory(hit),
    onSuccess: onPicked,
    onError: (e) => setErr(formatError(e)),
  });

  const results = searchQ.data?.results ?? [];
  const notes = searchQ.data?.notes ?? {};

  return (
    <div className="p-5">
      <input
        autoFocus
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search your directory — name, email or UPN (SSO users + live Entra)…"
        className="w-full rounded-lg border px-3 py-2 text-sm"
      />
      {notes.entra && (
        <p className="mt-2 rounded bg-amber-50 px-2 py-1 text-xs text-amber-700">
          Live Entra search unavailable: {notes.entra}. SSO users + manual entry still work.
        </p>
      )}
      {err && <p className="mt-2 text-sm text-rose-600">{err}</p>}
      <div className="mt-3 max-h-72 overflow-y-auto">
        {debounced.trim().length < 2 ? (
          <p className="py-8 text-center text-sm text-gray-400">Type at least 2 characters to search.</p>
        ) : searchQ.isFetching ? (
          <p className="py-8 text-center text-sm text-gray-400">Searching…</p>
        ) : results.length === 0 ? (
          <p className="py-8 text-center text-sm text-gray-400">No directory matches. Switch to “Manual” to type one in.</p>
        ) : (
          <ul className="divide-y">
            {results.map((h, i) => (
              <li key={`${h.email}-${i}`} className="flex items-center justify-between py-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium text-gray-800">{KIND_ICON[h.kind]} {h.display_name}</div>
                  <div className="truncate text-xs text-gray-500">{h.email}</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] uppercase text-gray-500">{h.source === "app_user" ? "SSO" : h.source}</span>
                  <button onClick={() => pick.mutate(h)} disabled={pick.isPending} className="rounded-lg bg-indigo-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
                    Add
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// ============================================================ Assignments
function AssignmentsTab({ scope, connectionId }: { scope: OwnershipScope; connectionId: string }) {
  const qc = useQueryClient();
  const [assigning, setAssigning] = useState<OwnershipSubject | null>(null);
  const scopeKey = `${scope.kind}:${scope.workloadId}:${scope.subId}:${connectionId}`;
  const subjectsQ = useQuery({ queryKey: ["ownership", "subjects", scopeKey], queryFn: () => api.ownershipSubjects(scope, connectionId) });

  const subjects = subjectsQ.data?.subjects ?? [];
  const owned = subjectsQ.data?.owned ?? 0;
  const total = subjectsQ.data?.total ?? 0;

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-4 grid grid-cols-3 gap-3">
        <Stat label="Ownable subjects" value={total} />
        <Stat label="Owned" value={owned} tone="emerald" />
        <Stat label="Unowned" value={total - owned} tone={total - owned > 0 ? "rose" : "gray"} />
      </div>

      <div className="rounded-xl border bg-white">
        <div className="border-b px-4 py-2 text-sm font-semibold text-gray-700">Workloads & architectures</div>
        {subjects.length === 0 ? (
          <p className="px-4 py-8 text-center text-sm text-gray-400">No workloads or architectures yet.</p>
        ) : (
          <ul className="divide-y">
            {subjects.map((s) => (
              <li key={`${s.subject_kind}:${s.subject_id}`} className="flex items-center justify-between px-4 py-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium text-gray-800">
                    {SUBJECT_ICON[s.subject_kind]} {s.subject_name || s.subject_id}
                  </div>
                  <div className="mt-0.5 flex items-center gap-2 text-xs">
                    {s.unowned ? (
                      <span className="rounded bg-rose-100 px-1.5 py-0.5 text-rose-700">Unowned</span>
                    ) : (
                      <>
                        <span className={`rounded px-1.5 py-0.5 ${SOURCE_BADGE[s.source]?.cls ?? "bg-gray-100 text-gray-600"}`}>{SOURCE_BADGE[s.source]?.label ?? s.source}</span>
                        <span className="text-gray-600">
                          {s.owners.map((o) => o.display_name).filter(Boolean).join(", ")}
                        </span>
                      </>
                    )}
                  </div>
                </div>
                <button onClick={() => setAssigning(s)} className="rounded-lg border px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50">
                  {s.unowned ? "Assign owner" : "Manage"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {assigning && (
        <AssignModal
          subject={assigning}
          onClose={() => setAssigning(null)}
          onSaved={() => {
            setAssigning(null);
            qc.invalidateQueries({ queryKey: ["ownership", "subjects"] });
            qc.invalidateQueries({ queryKey: ["ownership", "owners"] });
          }}
        />
      )}
    </div>
  );
}

function Stat({ label, value, tone = "gray" }: { label: string; value: number; tone?: "gray" | "emerald" | "rose" | "amber" }) {
  const toneCls = tone === "emerald" ? "text-emerald-600" : tone === "rose" ? "text-rose-600" : tone === "amber" ? "text-amber-600" : "text-gray-900";
  return (
    <div className="rounded-xl border bg-white p-4">
      <div className={`text-2xl font-semibold ${toneCls}`}>{value}</div>
      <div className="text-xs text-gray-500">{label}</div>
    </div>
  );
}

function AssignModal({ subject, onClose, onSaved }: { subject: OwnershipSubject; onClose: () => void; onSaved: () => void }) {
  const qc = useQueryClient();
  const [ownerId, setOwnerId] = useState("");
  const [role, setRole] = useState("technical");
  const [primary, setPrimary] = useState(true);
  const [err, setErr] = useState("");

  const ownersQ = useQuery({ queryKey: ["ownership", "owners"], queryFn: api.ownershipOwners });
  const existingQ = useQuery({
    queryKey: ["ownership", "assignments", subject.subject_kind, subject.subject_id],
    queryFn: () => api.ownershipAssignments({ subject_kind: subject.subject_kind, subject_id: subject.subject_id }),
  });

  const assign = useMutation({
    mutationFn: () =>
      api.upsertAssignment({
        owner_id: ownerId, subject_kind: subject.subject_kind as OwnershipAssignment["subject_kind"],
        subject_id: subject.subject_id, subject_name: subject.subject_name,
        role: role as OwnershipAssignment["role"], primary,
      }),
    onSuccess: onSaved,
    onError: (e) => setErr(formatError(e)),
  });
  const unassign = useMutation({
    mutationFn: (id: string) => api.deleteAssignment(id),
    onSuccess: () => {
      existingQ.refetch();
      qc.invalidateQueries({ queryKey: ["ownership", "subjects"] });
    },
  });

  const owners = ownersQ.data?.owners ?? [];
  const existing = existingQ.data?.assignments ?? [];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-lg rounded-2xl bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-5 py-3">
          <h3 className="truncate font-semibold text-gray-900">{SUBJECT_ICON[subject.subject_kind]} {subject.subject_name || subject.subject_id}</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">✕</button>
        </div>
        <div className="space-y-4 p-5">
          {existing.length > 0 && (
            <div>
              <div className="mb-1 text-xs font-medium text-gray-600">Current owners</div>
              <ul className="divide-y rounded-lg border">
                {existing.map((a) => (
                  <li key={a.id} className="flex items-center justify-between px-3 py-2 text-sm">
                    <span>{a.owner?.display_name ?? a.owner_id} <span className="text-xs text-gray-400">· {ROLE_LABELS[a.role]}{a.primary ? " · primary" : ""}</span></span>
                    <button onClick={() => unassign.mutate(a.id)} className="text-xs text-rose-600 hover:underline">Remove</button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {owners.length === 0 ? (
            <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-700">No owners yet — add one in the “Owners & Teams” tab first.</p>
          ) : (
            <>
              <div>
                <label className="mb-1 block text-xs font-medium text-gray-600">Owner</label>
                <select value={ownerId} onChange={(e) => setOwnerId(e.target.value)} className="w-full rounded-lg border px-3 py-2 text-sm">
                  <option value="">Select an owner…</option>
                  {owners.map((o) => (
                    <option key={o.id} value={o.id}>{KIND_ICON[o.kind]} {o.display_name}</option>
                  ))}
                </select>
              </div>
              <div className="flex gap-3">
                <div className="flex-1">
                  <label className="mb-1 block text-xs font-medium text-gray-600">Role</label>
                  <select value={role} onChange={(e) => setRole(e.target.value)} className="w-full rounded-lg border px-3 py-2 text-sm">
                    {Object.entries(ROLE_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                  </select>
                </div>
                <label className="mt-6 flex items-center gap-2 text-sm text-gray-600">
                  <input type="checkbox" checked={primary} onChange={(e) => setPrimary(e.target.checked)} /> Primary
                </label>
              </div>
              {err && <div className="text-sm text-rose-600">{err}</div>}
              <div className="flex justify-end gap-2">
                <button onClick={onClose} className="rounded-lg border px-3 py-1.5 text-sm text-gray-600">Close</button>
                <button onClick={() => assign.mutate()} disabled={!ownerId || assign.isPending} className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
                  {assign.isPending ? "Assigning…" : "Assign"}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ============================================================ Coverage + policy
const SOURCE_COLOR: Record<string, string> = {
  direct: "#10b981",
  tag: "#0ea5e9",
  workload: "#6366f1",
  inherited: "#f59e0b",
  none: "#f43f5e",
};
const SEV_CLS: Record<string, string> = {
  error: "border-rose-200 bg-rose-50 text-rose-700",
  warning: "border-amber-200 bg-amber-50 text-amber-700",
  info: "border-sky-200 bg-sky-50 text-sky-700",
};

function Donut({ pct }: { pct: number | null }) {
  const v = pct ?? 0;
  const r = 42;
  const c = 2 * Math.PI * r;
  const tone = v >= 80 ? "#10b981" : v >= 50 ? "#f59e0b" : "#f43f5e";
  return (
    <svg viewBox="0 0 100 100" className="h-28 w-28">
      <circle cx="50" cy="50" r={r} fill="none" stroke="#e5e7eb" strokeWidth="10" />
      <circle
        cx="50" cy="50" r={r} fill="none" stroke={tone} strokeWidth="10" strokeLinecap="round"
        strokeDasharray={`${(v / 100) * c} ${c}`} transform="rotate(-90 50 50)"
      />
      <text x="50" y="50" textAnchor="middle" dominantBaseline="central" className="fill-gray-900" style={{ fontSize: 20, fontWeight: 700 }}>
        {pct === null ? "—" : `${v}%`}
      </text>
    </svg>
  );
}

function CoverageTab({ scope, onScopeChange, connectionId }: { scope: OwnershipScope; onScopeChange: (s: OwnershipScope) => void; connectionId: string }) {
  const qc = useQueryClient();
  const [loadedKey, setLoadedKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  // Owner coverage scans a concrete scope. The section scope's Tenant mode can't be scanned
  // (whole-tenant is expensive); the user narrows to a Subscription or Workload via the scope
  // bar in the header. covKind maps the section scope onto the coverage API's two scan kinds.
  const covKind: ScopeKind = scope.kind === "subscription" ? "subscription" : "workload";
  const sid = scope.kind === "subscription" ? scope.subId : scope.workloadId;
  const scopeReady = scope.kind !== "tenant" && !!sid;
  const scopeKey = `${covKind}:${sid}:${connectionId}`;
  const loaded = scopeReady && loadedKey === scopeKey;

  const covQ = useQuery({
    queryKey: ["ownership", "coverage", covKind, sid, connectionId],
    queryFn: () => api.ownershipCoverage(covKind, scope.workloadId, scope.subId, connectionId),
    enabled: loaded,
  });
  const trendQ = useQuery({
    queryKey: ["ownership", "trend", covKind, sid, connectionId],
    queryFn: () => api.ownershipTrend(covKind, scope.workloadId, scope.subId, connectionId),
    enabled: loaded,
  });

  const load = async () => {
    setLoadedKey(scopeKey);
    const snap = await api.ownershipCoverage(covKind, scope.workloadId, scope.subId, connectionId);
    qc.setQueryData(["ownership", "coverage", covKind, sid, connectionId], snap);
  };
  const refresh = async () => {
    setBusy(true);
    setErr("");
    try {
      const snap = await api.refreshOwnershipCoverage(covKind, scope.workloadId, scope.subId, connectionId);
      setLoadedKey(scopeKey);
      qc.setQueryData(["ownership", "coverage", covKind, sid, connectionId], snap);
      qc.invalidateQueries({ queryKey: ["ownership", "trend", covKind, sid, connectionId] });
    } catch (e) {
      setErr(formatError(e));
    } finally {
      setBusy(false);
    }
  };

  const data = covQ.data;
  const showData = data && !data.never_loaded;

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border bg-white px-4 py-3">
        <div className="text-sm text-gray-600">
          {scope.kind === "tenant"
            ? "Owner coverage scans a subscription or workload — pick one in the Scope selector above."
            : scope.kind === "workload"
            ? "Owner coverage for the selected workload."
            : "Owner coverage for the selected subscription."}
        </div>
        <div className="flex items-center gap-2">
          {scope.kind === "tenant" && (
            <button onClick={() => onScopeChange({ ...scope, kind: "workload" })} className="rounded-lg border px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50">Pick a workload</button>
          )}
          {scopeReady && !loaded && (
            <button onClick={load} className="rounded-lg border px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50">Load coverage</button>
          )}
          <button onClick={refresh} disabled={!scopeReady || busy} className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
            {busy ? "Scanning…" : "↻ Refresh"}
          </button>
        </div>
      </div>

      {err && <div className="mb-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{err}</div>}

      {scope.kind === "tenant" ? (
        <Empty hint="Use the Scope selector above — choose a Subscription or Workload — to compute owner coverage." />
      ) : !scopeReady ? (
        <Empty hint="Pick a specific workload or subscription in the Scope selector above." />
      ) : !loaded ? (
        <Empty hint="Press “Load coverage” for the last result, or “Refresh” to scan now." />
      ) : covQ.isLoading ? (
        <Empty hint="Loading…" />
      ) : !showData ? (
        <Empty hint="No coverage computed yet — press “Refresh” to scan this scope." />
      ) : data!.error ? (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700">{data!.error}</div>
      ) : (
        <div className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-[auto,1fr]">
            <div className="flex items-center gap-4 rounded-xl border bg-white p-4">
              <Donut pct={data!.coverage_pct} />
              <div>
                <div className="text-sm font-medium text-gray-700">Owner coverage</div>
                <div className="text-xs text-gray-500">{data!.kpis.owned} of {data!.kpis.total} resources owned</div>
                {trendQ.data && trendQ.data.points.length >= 2 && (
                  <div className="mt-2"><TrendChart points={trendQ.data.points} current={trendQ.data.current} previous={trendQ.data.previous} delta={trendQ.data.delta} unit="%" /></div>
                )}
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <Stat label="Resources" value={data!.kpis.total} />
              <Stat label="Owned" value={data!.kpis.owned} tone="emerald" />
              <Stat label="Unowned" value={data!.kpis.unowned} tone={data!.kpis.unowned ? "rose" : "gray"} />
              <Stat label="Owners" value={data!.kpis.owners} />
              <Stat label="Prod unowned" value={data!.kpis.prod_unowned} tone={data!.kpis.prod_unowned ? "rose" : "gray"} />
              <Stat label="Tag-only owners" value={data!.kpis.orphan_owners} tone={data!.kpis.orphan_owners ? "amber" : "gray"} />
            </div>
          </div>

          {/* source breakdown */}
          <div className="rounded-xl border bg-white p-4">
            <div className="mb-2 text-sm font-semibold text-gray-700">How ownership is established</div>
            <div className="flex h-3 overflow-hidden rounded-full bg-gray-100">
              {(["direct", "tag", "workload", "inherited", "none"] as const).map((s) =>
                data!.by_source[s] ? (
                  <div key={s} title={`${s}: ${data!.by_source[s]}`} style={{ width: `${(data!.by_source[s] / data!.kpis.total) * 100}%`, background: SOURCE_COLOR[s] }} />
                ) : null
              )}
            </div>
            <div className="mt-2 flex flex-wrap gap-3 text-xs text-gray-600">
              {(["direct", "tag", "workload", "inherited", "none"] as const).map((s) => (
                <span key={s} className="flex items-center gap-1">
                  <span className="h-2.5 w-2.5 rounded-full" style={{ background: SOURCE_COLOR[s] }} />
                  {s === "none" ? "unowned" : s} ({data!.by_source[s] ?? 0})
                </span>
              ))}
            </div>
          </div>

          {/* policy findings */}
          {data!.findings.length > 0 && (
            <div className="space-y-2">
              {data!.findings.map((f) => (
                <div key={f.id} className={`rounded-xl border px-4 py-3 ${SEV_CLS[f.severity]}`}>
                  <div className="text-sm font-medium">{f.title}</div>
                  <div className="mt-0.5 text-xs opacity-80">{f.detail}</div>
                </div>
              ))}
            </div>
          )}

          <div className="grid gap-4 lg:grid-cols-2">
            <div className="rounded-xl border bg-white p-4">
              <div className="mb-2 text-sm font-semibold text-gray-700">By owner</div>
              {data!.by_owner.length === 0 ? (
                <p className="py-4 text-center text-xs text-gray-400">No owners in scope.</p>
              ) : (
                <ul className="divide-y text-sm">
                  {data!.by_owner.slice(0, 12).map((b) => (
                    <li key={b.owner_id || b.label} className="flex items-center justify-between py-1.5">
                      <span className="truncate">{b.label}</span>
                      <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-xs text-indigo-700">{b.count}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="rounded-xl border bg-white p-4">
              <div className="mb-2 text-sm font-semibold text-gray-700">Unowned resources ({data!.kpis.unowned})</div>
              {data!.unowned.length === 0 ? (
                <p className="py-4 text-center text-xs text-emerald-600">Everything in scope has an owner. 🎉</p>
              ) : (
                <ul className="max-h-64 divide-y overflow-y-auto text-sm">
                  {data!.unowned.slice(0, 50).map((r) => (
                    <li key={r.id} className="py-1.5">
                      <div className="truncate font-medium text-gray-700">{r.name}</div>
                      <div className="truncate text-xs text-gray-400">{r.type} · {r.resource_group}</div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Empty({ hint }: { hint: string }) {
  return (
    <div className="rounded-xl border border-dashed bg-white p-10 text-center">
      <p className="text-sm text-gray-500">{hint}</p>
    </div>
  );
}

// ============================================================ My Estate (owner cockpit)
function EstateTab() {
  const meQ = useQuery({ queryKey: ["ownership", "estate", "me"], queryFn: () => api.ownershipEstate() });
  const ownersQ = useQuery({ queryKey: ["ownership", "owners"], queryFn: api.ownershipOwners });
  const [ownerId, setOwnerId] = useState("");
  const ownerQ = useQuery({
    queryKey: ["ownership", "estate", "owner", ownerId],
    queryFn: () => api.ownershipEstate(ownerId),
    enabled: !!ownerId,
  });

  const me = meQ.data;
  const browse = ownerQ.data?.estates ?? [];

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <h2 className="mb-1 text-sm font-semibold text-gray-700">My estate</h2>
        <p className="mb-3 text-xs text-gray-500">
          {me?.principal?.email ? `Owners linked to ${me.principal.email}` : "Owners linked to you"} ·
          {" "}{me?.matched_owners ?? 0} owner record(s), {me?.total_subjects ?? 0} subject(s).
        </p>
        {(me?.estates.length ?? 0) === 0 ? (
          <Empty hint="No owner records are linked to your account yet. Add yourself via the people-picker (Owners & Teams → Add owner → From directory) and assign your estate." />
        ) : (
          <div className="space-y-3">{me!.estates.map((e) => <EstateCard key={e.owner.id} estate={e} />)}</div>
        )}
      </div>

      <div>
        <h2 className="mb-2 text-sm font-semibold text-gray-700">Browse an owner's estate</h2>
        <select value={ownerId} onChange={(e) => setOwnerId(e.target.value)} className="mb-3 w-full max-w-sm rounded-lg border px-3 py-2 text-sm">
          <option value="">Select an owner…</option>
          {(ownersQ.data?.owners ?? []).map((o) => (
            <option key={o.id} value={o.id}>{KIND_ICON[o.kind]} {o.display_name}</option>
          ))}
        </select>
        {ownerId && browse.length > 0 && <div className="space-y-3">{browse.map((e) => <EstateCard key={e.owner.id} estate={e} />)}</div>}
      </div>
    </div>
  );
}

function EstateCard({ estate }: { estate: OwnerEstate }) {
  const o = estate.owner;
  return (
    <div className="rounded-xl border bg-white p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-lg">{KIND_ICON[o.kind]}</span>
          <div>
            <div className="font-medium text-gray-900">{o.display_name}</div>
            {o.email && <div className="text-xs text-gray-500">{o.email}</div>}
          </div>
          {estate.linked && <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[11px] text-emerald-700">🔗 linked</span>}
        </div>
        <span className="rounded-full bg-indigo-50 px-2.5 py-1 text-sm font-semibold text-indigo-700">{estate.total}</span>
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5 text-[11px]">
        {Object.entries(estate.by_kind).map(([k, n]) => (
          <span key={k} className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-600">{SUBJECT_ICON[k] ?? "•"} {k}: {n}</span>
        ))}
        {Object.entries(estate.by_role).map(([r, n]) => (
          <span key={r} className="rounded bg-sky-50 px-1.5 py-0.5 text-sky-700">{ROLE_LABELS[r] ?? r}: {n}</span>
        ))}
      </div>
      {estate.assignments.length > 0 && (
        <ul className="mt-3 max-h-48 divide-y overflow-y-auto text-sm">
          {estate.assignments.slice(0, 50).map((a) => (
            <li key={a.id} className="flex items-center justify-between py-1.5">
              <span className="truncate">{SUBJECT_ICON[a.subject_kind] ?? "•"} {a.subject_name || a.subject_id}</span>
              <span className="text-xs text-gray-400">{ROLE_LABELS[a.role] ?? a.role}{a.primary ? " · primary" : ""}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ============================================================ Suggestions (AI/heuristic)
function SuggestionsTab({ scope, connectionId }: { scope: OwnershipScope; connectionId: string }) {
  const qc = useQueryClient();
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [msg, setMsg] = useState("");
  const scopeKey = `${scope.kind}:${scope.workloadId}:${scope.subId}:${connectionId}`;
  const suggQ = useQuery({ queryKey: ["ownership", "suggestions", scopeKey], queryFn: () => api.ownershipSuggestions(scope, connectionId) });

  const accept = useMutation({
    mutationFn: (s: OwnershipSuggestion) => api.acceptSuggestion(s),
    onSuccess: (_r, s) => {
      setMsg(`Assigned ${s.candidate.display_name} to ${s.subject_name || s.subject_id}.`);
      setDismissed((d) => new Set(d).add(s.id));
      qc.invalidateQueries({ queryKey: ["ownership"] });
    },
    onError: (e) => setMsg(formatError(e)),
  });

  const items = (suggQ.data?.suggestions ?? []).filter((s) => !dismissed.has(s.id));

  return (
    <div className="mx-auto max-w-3xl">
      {suggQ.data?.note && (
        <div className="mb-3 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-sm text-sky-700">{suggQ.data.note}</div>
      )}
      {msg && <div className="mb-3 rounded-lg border bg-white px-3 py-2 text-sm text-gray-600">{msg}</div>}
      {suggQ.isLoading ? (
        <Empty hint="Loading suggestions…" />
      ) : items.length === 0 ? (
        <Empty hint="No suggestions right now. Suggestions come from RBAC owners on unowned workloads — run an RBAC scan and assign the obvious owners first." />
      ) : (
        <div className="space-y-3">
          {items.map((s) => (
            <div key={s.id} className="rounded-xl border bg-white p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-sm">
                    <span className="font-medium text-gray-900">{s.candidate.display_name}</span>
                    <span className="text-gray-500"> should own </span>
                    <span className="font-medium text-gray-900">{SUBJECT_ICON[s.subject_kind] ?? "•"} {s.subject_name || s.subject_id}</span>
                  </div>
                  {s.candidate.email && <div className="text-xs text-gray-400">{s.candidate.email}</div>}
                  <ul className="mt-2 space-y-0.5 text-xs text-gray-500">
                    {s.evidence.map((e, i) => (
                      <li key={i}>• {e.replace(/\*\*/g, "")}</li>
                    ))}
                  </ul>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-2">
                  <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${s.confidence >= 0.8 ? "bg-emerald-100 text-emerald-700" : s.confidence >= 0.6 ? "bg-amber-100 text-amber-700" : "bg-gray-100 text-gray-600"}`}>
                    {Math.round(s.confidence * 100)}% match
                  </span>
                  <div className="flex gap-2">
                    <button onClick={() => setDismissed((d) => new Set(d).add(s.id))} className="text-xs text-gray-400 hover:underline">Dismiss</button>
                    <button onClick={() => accept.mutate(s)} disabled={accept.isPending} className="rounded-lg bg-indigo-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
                      Accept
                    </button>
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ============================================================ Attestation + leaver risk
const ATT_CLS: Record<string, string> = {
  never: "bg-gray-100 text-gray-600",
  stale: "bg-amber-100 text-amber-700",
  fresh: "bg-emerald-100 text-emerald-700",
};

function AttestationTab({ scope }: { scope: OwnershipScope }) {
  const qc = useQueryClient();
  const scopeKey = `${scope.kind}:${scope.workloadId}:${scope.subId}`;
  const attQ = useQuery({ queryKey: ["ownership", "attestation", scopeKey], queryFn: () => api.ownershipAttestation(scope) });
  const leaverQ = useQuery({ queryKey: ["ownership", "leavers"], queryFn: api.ownershipLeavers });

  const attest = useMutation({
    mutationFn: (id: string) => api.attestAssignment(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ownership", "attestation"] }),
  });

  const s = attQ.data?.summary;
  const items = attQ.data?.items ?? [];
  const leavers = leaverQ.data?.at_risk ?? [];

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      {leavers.length > 0 && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-4">
          <div className="mb-2 text-sm font-semibold text-rose-700">⚠️ {leavers.length} owner(s) at risk (joiner-mover-leaver)</div>
          <ul className="space-y-1 text-sm text-rose-700">
            {leavers.map((l) => (
              <li key={l.owner.id}>
                <span className="font-medium">{l.owner.display_name}</span> — {l.reason} · {l.orphaned_subjects} subject(s) need reassignment.
              </li>
            ))}
          </ul>
        </div>
      )}

      {s && (
        <div className="grid grid-cols-4 gap-3">
          <Stat label="Assignments" value={s.total} />
          <Stat label="Never attested" value={s.never} tone={s.never ? "amber" : "gray"} />
          <Stat label={`Stale (>${s.stale_days}d)`} value={s.stale} tone={s.stale ? "rose" : "gray"} />
          <Stat label="Fresh" value={s.fresh} tone="emerald" />
        </div>
      )}

      <div className="rounded-xl border bg-white">
        <div className="border-b px-4 py-2 text-sm font-semibold text-gray-700">Recertification</div>
        {attQ.isLoading ? (
          <p className="px-4 py-8 text-center text-sm text-gray-400">Loading…</p>
        ) : items.length === 0 ? (
          <p className="px-4 py-8 text-center text-sm text-gray-400">No assignments to attest yet.</p>
        ) : (
          <ul className="divide-y">
            {items.map((it: AttestationItem) => (
              <li key={it.id} className="flex items-center justify-between px-4 py-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium text-gray-800">
                    {SUBJECT_ICON[it.subject_kind] ?? "•"} {it.subject_name || it.subject_id}
                  </div>
                  <div className="mt-0.5 text-xs text-gray-500">
                    {it.owner?.display_name ?? it.owner_id} · {ROLE_LABELS[it.role] ?? it.role}
                    {it.days_since !== null && <span> · last confirmed {it.days_since}d ago</span>}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span className={`rounded-full px-2 py-0.5 text-xs ${ATT_CLS[it.attestation_status]}`}>{it.attestation_status}</span>
                  <button onClick={() => attest.mutate(it.id)} disabled={attest.isPending} className="rounded-lg border px-2.5 py-1 text-xs text-gray-700 hover:bg-gray-50">
                    ✓ Confirm
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
