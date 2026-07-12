import { useEffect, useMemo, useState } from "react";
import {
  api,
  type ActivityLogDiagnosticCategory,
  type ActivityLogDiagnosticDestinationKind,
  type ActivityLogDiagnosticDestinationOptions,
  type ActivityLogDiagnosticInventory,
  type ActivityLogDiagnosticPlan,
  type ActivityLogDiagnosticPlanRequest,
  type AlertsManagerCapabilities,
} from "../api";
import type { AlertsManagerScopeParams } from "../queryKeys";
import { formatError } from "../utils/format";

const REQUIRED_CATEGORIES: ActivityLogDiagnosticCategory[] = ["Administrative", "Alert", "Policy", "Security"];
const STEPS = ["Inventory", "Destination", "Preview & submit"] as const;
const ARM_PATTERNS: Record<ActivityLogDiagnosticDestinationKind, RegExp> = {
  workspace: /^\/subscriptions\/[^/]+\/resourceGroups\/[^/]+\/providers\/Microsoft\.OperationalInsights\/workspaces\/[^/]+$/i,
  event_hub: /^\/subscriptions\/[^/]+\/resourceGroups\/[^/]+\/providers\/Microsoft\.EventHub\/namespaces\/[^/]+\/authorizationRules\/[^/]+$/i,
  storage: /^\/subscriptions\/[^/]+\/resourceGroups\/[^/]+\/providers\/Microsoft\.Storage\/storageAccounts\/[^/]+$/i,
};
const buttonSecondary = "rounded-lg border bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40";
const buttonPrimary = "rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-700 disabled:opacity-40";

function statusTone(status: string) {
  if (status === "covered" || status === "equivalent") return "bg-emerald-50 text-emerald-700";
  if (status === "partial" || status === "update") return "bg-amber-50 text-amber-700";
  if (status === "missing" || status === "create") return "bg-sky-50 text-sky-700";
  if (status === "blocked" || status === "unknown") return "bg-rose-50 text-rose-700";
  return "bg-gray-100 text-gray-600";
}

function shortResource(value: string) {
  return value.split("/").filter(Boolean).at(-1) || value;
}

export type ActivityLogDiagnosticsWizardProps = {
  scopeParams: AlertsManagerScopeParams;
  capabilities?: AlertsManagerCapabilities;
  onBack: () => void;
  onClose: () => void;
  onSubmitted: (pendingCount: number) => void;
};

export function ActivityLogDiagnosticsWizard({ scopeParams, capabilities, onBack, onClose, onSubmitted }: ActivityLogDiagnosticsWizardProps) {
  const [step, setStep] = useState(0);
  const [inventory, setInventory] = useState<ActivityLogDiagnosticInventory | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [destinationKind, setDestinationKind] = useState<ActivityLogDiagnosticDestinationKind>("workspace");
  const [destinationOptions, setDestinationOptions] = useState<ActivityLogDiagnosticDestinationOptions | null>(null);
  const [destinationManagementGroup, setDestinationManagementGroup] = useState(scopeParams.management_group_id ?? "");
  const [destinationSubscription, setDestinationSubscription] = useState(scopeParams.subscription_id ?? "");
  const [destinationResourceGroup, setDestinationResourceGroup] = useState("");
  const [eventHubNamespaceId, setEventHubNamespaceId] = useState("");
  const [useEventHubFallback, setUseEventHubFallback] = useState(false);
  const [resourceId, setResourceId] = useState("");
  const [eventHubName, setEventHubName] = useState("");
  const [settingName, setSettingName] = useState("aznetagent-activity-log");
  const [preview, setPreview] = useState<ActivityLogDiagnosticPlan | null>(null);
  const [validated, setValidated] = useState(false);
  const [reason, setReason] = useState("Export required subscription Activity Log categories to the reviewed destination");
  const [busy, setBusy] = useState<"inventory" | "destination" | "preview" | "validate" | "submit" | "">("inventory");
  const [error, setError] = useState("");
  const canSubmit = !!capabilities?.can_manage_rules && !capabilities.read_only;

  useEffect(() => {
    let active = true;
    setBusy("inventory");
    api.activityLogDiagnosticInventory(scopeParams).then((result) => {
      if (!active) return;
      setInventory(result);
      setSelected(result.subscriptions.filter((row) => row.complete && row.status !== "unknown").map((row) => row.subscription_id));
    }).catch((cause) => { if (active) setError(formatError(cause)); }).finally(() => { if (active) setBusy(""); });
    return () => { active = false; };
  }, [scopeParams.connection_id, scopeParams.management_group_id, scopeParams.subscription_id, scopeParams.workload_id]);

  useEffect(() => {
    if (step !== 1) return;
    let active = true;
    setBusy("destination");
    setError("");
    api.activityLogDiagnosticDestinationOptions({
      connection_id: scopeParams.connection_id,
      management_group_id: destinationManagementGroup || undefined,
      subscription_id: destinationSubscription || undefined,
      resource_group: destinationResourceGroup || undefined,
      kind: destinationKind,
      namespace_id: eventHubNamespaceId || undefined,
    }).then((result) => {
      if (!active) return;
      setDestinationOptions(result);
      if (destinationSubscription && !result.subscriptions.some((item) => item.id.toLowerCase() === destinationSubscription.toLowerCase())) {
        setDestinationSubscription(""); setDestinationResourceGroup(""); setEventHubNamespaceId(""); setEventHubName(""); setResourceId("");
      }
    }).catch((cause) => { if (active) { setDestinationOptions(null); setError(formatError(cause)); } }).finally(() => { if (active) setBusy(""); });
    return () => { active = false; };
  }, [destinationKind, destinationManagementGroup, destinationResourceGroup, destinationSubscription, eventHubNamespaceId, scopeParams.connection_id, step]);

  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [busy, onClose]);

  const resourceError = !resourceId.trim()
    ? "A destination ARM resource ID is required."
    : !ARM_PATTERNS[destinationKind].test(resourceId.trim())
      ? `Enter the full ARM ID for an Azure ${destinationKind.replace("_", " ")} ${destinationKind === "event_hub" ? "authorization rule" : "resource"}.`
      : "";
  const eventHubError = destinationKind === "event_hub" && (!eventHubName.trim() || eventHubName.trim().length > 256) ? "Enter an Event Hub name (maximum 256 characters)." : "";
  const nameError = !/^[A-Za-z0-9_.()-]{1,260}$/.test(settingName.trim()) ? "Use 1–260 letters, numbers, _, -, periods, or parentheses." : "";
  const destinationValid = !resourceError && !eventHubError && !nameError;

  const request = useMemo<ActivityLogDiagnosticPlanRequest>(() => ({
    ...scopeParams,
    subscription_ids: selected,
    categories: REQUIRED_CATEGORIES,
    destination: { kind: destinationKind, resource_id: resourceId.trim(), event_hub_name: destinationKind === "event_hub" ? eventHubName.trim() : "" },
    setting_name: settingName.trim(),
  }), [destinationKind, eventHubName, resourceId, scopeParams, selected, settingName]);

  function resetPlan() { setPreview(null); setValidated(false); setError(""); }
  function resetDestinationFromManagementGroup(value: string) {
    setDestinationManagementGroup(value); setDestinationSubscription(""); setDestinationResourceGroup("");
    setEventHubNamespaceId(""); setEventHubName(""); setResourceId(""); setUseEventHubFallback(false); resetPlan();
  }
  function resetDestinationFromSubscription(value: string) {
    setDestinationSubscription(value); setDestinationResourceGroup(""); setEventHubNamespaceId("");
    setEventHubName(""); setResourceId(""); setUseEventHubFallback(false); resetPlan();
  }
  function resetDestinationFromResourceGroup(value: string) {
    setDestinationResourceGroup(value); setEventHubNamespaceId(""); setEventHubName("");
    setResourceId(""); setUseEventHubFallback(false); resetPlan();
  }
  function toggleSubscription(id: string, checked: boolean) {
    setSelected((current) => checked ? [...new Set([...current, id])] : current.filter((value) => value !== id));
    resetPlan();
  }
  async function buildPreview() {
    setBusy("preview"); setError(""); setValidated(false);
    try { const result = await api.previewActivityLogDiagnosticPlan(request); setPreview(result.plan); setStep(2); }
    catch (cause) { setError(formatError(cause)); }
    finally { setBusy(""); }
  }
  async function validatePlan() {
    if (!preview) return;
    setBusy("validate"); setError("");
    try {
      const result = await api.validateActivityLogDiagnosticPlan({ ...request, plan_token: preview.plan_token });
      setPreview(result.plan); setValidated(result.valid);
      if (!result.valid) setError(result.errors.join(" · ") || "Validation failed.");
    } catch (cause) { setValidated(false); setError(formatError(cause)); }
    finally { setBusy(""); }
  }
  async function submitPlan() {
    if (!preview || !validated) return;
    setBusy("submit"); setError("");
    try {
      const result = await api.submitActivityLogDiagnosticPlan({ ...request, plan_token: preview.plan_token, reason: reason.trim() });
      onSubmitted(result.change_count);
    } catch (cause) { setError(formatError(cause)); }
    finally { setBusy(""); }
  }

  return <div className="fixed inset-0 z-[60] flex justify-end bg-black/40" role="dialog" aria-modal="true" aria-labelledby="activity-diagnostics-title">
    <div className="flex h-full w-full max-w-5xl flex-col bg-white shadow-2xl">
      <header className="border-b px-5 py-4">
        <div className="flex items-start gap-3"><div><h2 id="activity-diagnostics-title" className="text-base font-semibold text-gray-900">Subscription Activity Log diagnostic settings</h2><p className="mt-0.5 text-xs text-gray-500">Inspect live settings, preview exact operations, and create approval-gated pending changes. Nothing here writes to Azure.</p></div><button onClick={onClose} disabled={!!busy} aria-label="Close diagnostic-settings workflow" className="ml-auto text-gray-400 hover:text-gray-700 disabled:opacity-40">✕</button></div>
        <ol className="mt-4 grid grid-cols-3 gap-1" aria-label="Workflow progress">{STEPS.map((label, index) => <li key={label} aria-current={step === index ? "step" : undefined} className={`rounded-md px-2 py-1.5 text-center text-[10px] font-medium ${step === index ? "bg-gray-900 text-white" : index < step ? "bg-emerald-50 text-emerald-700" : "bg-gray-100 text-gray-500"}`}>{index < step ? "✓" : index + 1}. {label}</li>)}</ol>
      </header>
      <main className="min-h-0 flex-1 overflow-auto p-5">
        {error && <div role="alert" className="mb-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700">{error}</div>}
        {busy && busy !== "destination" && <div role="status" aria-live="polite" className="mb-4 flex items-center gap-2 rounded-lg border border-sky-200 bg-sky-50 p-3 text-xs text-sky-700"><span className="h-3 w-3 animate-spin rounded-full border-2 border-sky-200 border-t-sky-700" />{busy === "inventory" ? "Inspecting subscription diagnostic settings…" : busy === "preview" ? "Building a read-only preview…" : busy === "validate" ? "Rechecking inventory, destination, and conflicts…" : "Creating pending managed changes…"}</div>}

        {step === 0 && <section>
          <div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-sm font-semibold text-gray-900">Inspect and select subscriptions</h3><p className="mt-1 text-xs text-gray-500">Every subscription is explicit. Unknown or incomplete inventory fails closed and cannot be selected.</p></div>{inventory && <div className="flex flex-wrap gap-1">{(["covered", "partial", "missing", "unknown"] as const).map((status) => <span key={status} className={`rounded px-2 py-1 text-[10px] font-medium ${statusTone(status)}`}>{status} {inventory.counts[status] ?? 0}</span>)}</div>}</div>
          {inventory?.partial && <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">Inventory is partial. Incomplete subscriptions remain unselected and no change can be planned for them.</div>}
          <div className="mt-3 flex flex-wrap items-center gap-2"><button className={buttonSecondary} disabled={!inventory} onClick={() => { setSelected(inventory?.subscriptions.filter((row) => row.complete && row.status !== "unknown").map((row) => row.subscription_id) ?? []); resetPlan(); }}>Select inspectable ({inventory?.subscriptions.filter((row) => row.complete && row.status !== "unknown").length ?? 0})</button><button className={buttonSecondary} onClick={() => { setSelected([]); resetPlan(); }}>Clear</button><span className="text-[10px] text-gray-500">{selected.length} selected</span></div>
          <div className="mt-3 divide-y overflow-hidden rounded-xl border">{inventory?.subscriptions.map((row) => <label key={row.subscription_id} className={`flex items-start gap-3 p-3 text-xs ${row.complete && row.status !== "unknown" ? "hover:bg-gray-50" : "bg-gray-50 opacity-70"}`}>
            <input type="checkbox" checked={selected.includes(row.subscription_id)} disabled={!row.complete || row.status === "unknown"} onChange={(event) => toggleSubscription(row.subscription_id, event.target.checked)} />
            <span className="min-w-0 flex-1"><span className="flex flex-wrap items-center gap-2"><strong className="break-all text-gray-900">{row.subscription_id}</strong><span className={`rounded px-1.5 py-0.5 text-[9px] capitalize ${statusTone(row.status)}`}>{row.status}</span><span className="text-[9px] text-gray-500">{row.settings.length} setting{row.settings.length === 1 ? "" : "s"}</span></span>
              <span className="mt-2 flex flex-wrap gap-1">{REQUIRED_CATEGORIES.map((category) => <span key={category} className={`rounded px-1.5 py-0.5 text-[9px] ${row.categories[category] === "covered" ? "bg-emerald-50 text-emerald-700" : row.categories[category] === "unknown" ? "bg-gray-100 text-gray-600" : "bg-amber-50 text-amber-700"}`}>{category}: {row.categories[category]}</span>)}</span>
              <span className="mt-1 flex flex-wrap gap-1">{(["workspace", "event_hub", "storage"] as const).map((kind) => <span key={kind} className="rounded bg-gray-100 px-1.5 py-0.5 text-[9px] text-gray-600">{kind.replace("_", " ")}: {row.destinations[kind]}</span>)}</span>
              {!!row.settings.length && <details className="mt-2"><summary className="cursor-pointer text-[10px] font-medium text-indigo-700">Existing settings and destinations</summary><div className="mt-1 space-y-1">{row.settings.map((setting) => <div key={setting.id} className="rounded bg-gray-50 p-2 text-[10px]"><strong>{setting.name}</strong> · {setting.categories.join(", ") || "No required categories"}{setting.destinations.map((destination) => <div key={`${destination.kind}-${destination.resource_id}`} className="break-all text-gray-500">{destination.kind.replace("_", " ")}: {destination.resource_id}{destination.event_hub_name ? ` · hub ${destination.event_hub_name}` : ""}</div>)}</div>)}</div></details>}
              {row.error && <span className="mt-1 block text-[10px] text-rose-700">{row.error}</span>}
            </span>
          </label>) ?? (!busy && <div className="p-8 text-center text-xs text-gray-400">No subscription inventory returned.</div>)}</div>
        </section>}

        {step === 1 && <section className="space-y-5">
          <div><h3 className="text-sm font-semibold text-gray-900">Required categories and destination</h3><p className="mt-1 text-xs text-gray-500">The baseline categories are always enabled. Choose one reviewed destination for every selected subscription.</p></div>
          <fieldset><legend className="text-xs font-semibold text-gray-700">Required Activity Log categories</legend><div className="mt-2 grid gap-2 sm:grid-cols-4">{REQUIRED_CATEGORIES.map((category) => <label key={category} className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-xs font-medium text-emerald-800"><input type="checkbox" checked readOnly />{category}</label>)}</div></fieldset>
          <fieldset><legend className="text-xs font-semibold text-gray-700">Destination kind</legend><div className="mt-2 grid gap-2 sm:grid-cols-3">{(["workspace", "event_hub", "storage"] as const).map((kind) => <label key={kind} className={`rounded-xl border p-3 text-xs ${destinationKind === kind ? "border-indigo-300 bg-indigo-50" : ""}`}><input type="radio" name="diagnostic-destination" checked={destinationKind === kind} onChange={() => { setDestinationKind(kind); setDestinationResourceGroup(""); setEventHubNamespaceId(""); setEventHubName(""); setResourceId(""); setUseEventHubFallback(false); resetPlan(); }} /> <span className="font-medium capitalize">{kind.replace("_", " ")}</span><span className="mt-1 block text-[10px] text-gray-500">{kind === "workspace" ? "Log Analytics workspace" : kind === "event_hub" ? "Event Hub authorization rule plus hub name" : "Storage account"}</span></label>)}</div></fieldset>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-xs font-medium text-gray-700">Management group<select value={destinationManagementGroup} disabled={busy === "destination"} onChange={(event) => resetDestinationFromManagementGroup(event.target.value)} className="mt-1 w-full rounded-lg border px-3 py-2 font-normal disabled:bg-gray-100"><option value="">{busy === "destination" && !destinationSubscription ? "Loading management groups…" : "All visible subscriptions"}</option>{destinationOptions?.management_groups.map((item) => <option key={item.id} value={item.id}>{`${"  ".repeat(item.depth ?? 0)}${item.name} — ${item.id}`}</option>)}</select></label>
            <label className="text-xs font-medium text-gray-700">Subscription<select value={destinationSubscription} disabled={busy === "destination"} onChange={(event) => resetDestinationFromSubscription(event.target.value)} className="mt-1 w-full rounded-lg border px-3 py-2 font-normal disabled:bg-gray-100"><option value="">{busy === "destination" && !destinationSubscription ? "Loading subscriptions…" : "Select subscription"}</option>{destinationOptions?.subscriptions.map((item) => <option key={item.id} value={item.id}>{item.name} — {item.id}</option>)}</select></label>
            <label className="text-xs font-medium text-gray-700">Resource group<select value={destinationResourceGroup} disabled={!destinationSubscription || busy === "destination"} onChange={(event) => resetDestinationFromResourceGroup(event.target.value)} className="mt-1 w-full rounded-lg border px-3 py-2 font-normal disabled:bg-gray-100"><option value="">{busy === "destination" && destinationSubscription && !destinationResourceGroup ? "Loading resource groups…" : "Select resource group"}</option>{destinationOptions?.resource_groups.map((item) => <option key={item.id} value={item.name}>{item.name} — {item.id}</option>)}</select></label>
            {destinationKind !== "event_hub" && <label className="text-xs font-medium text-gray-700">{destinationKind === "workspace" ? "Log Analytics workspace" : "Storage account"}<select value={resourceId} disabled={!destinationResourceGroup || busy === "destination"} onChange={(event) => { setResourceId(event.target.value); resetPlan(); }} className="mt-1 w-full rounded-lg border px-3 py-2 font-normal disabled:bg-gray-100"><option value="">{busy === "destination" && destinationResourceGroup ? destinationKind === "workspace" ? "Loading Log Analytics workspaces…" : "Loading storage accounts…" : "Select resource"}</option>{destinationOptions?.resources.map((item) => <option key={item.id} value={item.id}>{item.name} — {item.id}</option>)}</select></label>}
          </div>
          {destinationResourceGroup && !busy && destinationOptions?.resources.length === 0 && <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">No visible {destinationKind.replace("_", " ")} destinations were found in this resource group.</div>}
          {destinationKind === "event_hub" && <div className="space-y-3 rounded-xl border p-4">
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="text-xs font-medium text-gray-700">Event Hubs namespace<select value={eventHubNamespaceId} disabled={!destinationResourceGroup || busy === "destination"} onChange={(event) => { setEventHubNamespaceId(event.target.value); setEventHubName(""); setResourceId(""); setUseEventHubFallback(false); resetPlan(); }} className="mt-1 w-full rounded-lg border px-3 py-2 font-normal disabled:bg-gray-100"><option value="">{busy === "destination" && destinationResourceGroup && !eventHubNamespaceId ? "Loading Event Hubs namespaces…" : "Select namespace"}</option>{destinationOptions?.resources.map((item) => <option key={item.id} value={item.id}>{item.name} — {item.id}</option>)}</select></label>
              <label className="text-xs font-medium text-gray-700">Event Hub<select value={eventHubName} disabled={!eventHubNamespaceId} onChange={(event) => { setEventHubName(event.target.value); resetPlan(); }} className="mt-1 w-full rounded-lg border px-3 py-2 font-normal disabled:bg-gray-100"><option value="">Select Event Hub</option>{destinationOptions?.event_hubs.map((item) => <option key={item.id} value={item.name}>{item.name} — {item.id}</option>)}</select></label>
              <label className="text-xs font-medium text-gray-700">Namespace authorization rule<select value={useEventHubFallback ? "" : resourceId} disabled={!eventHubNamespaceId || busy === "destination" || !destinationOptions?.authorization_rules_complete} onChange={(event) => { setResourceId(event.target.value); resetPlan(); }} className="mt-1 w-full rounded-lg border px-3 py-2 font-normal disabled:bg-gray-100"><option value="">{busy === "destination" && eventHubNamespaceId ? "Loading authorization rules…" : "Select authorization rule"}</option>{destinationOptions?.authorization_rules.map((item) => <option key={item.id} value={item.id}>{item.name} — {item.id}</option>)}</select></label>
            </div>
            {eventHubNamespaceId && destinationOptions && !destinationOptions.authorization_rules_complete && <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800"><strong>Authorization rules could not be inventoried.</strong> {destinationOptions.authorization_rule_error}<button type="button" className="ml-2 underline" onClick={() => { setUseEventHubFallback(true); setResourceId(""); }}>Enter an authorization-rule ARM ID as fallback</button></div>}
            {useEventHubFallback && <label className="block text-xs font-medium text-gray-700">Fallback Event Hub namespace authorization-rule ARM ID<input value={resourceId} onChange={(event) => { setResourceId(event.target.value); resetPlan(); }} placeholder="/subscriptions/.../resourceGroups/.../providers/Microsoft.EventHub/namespaces/.../authorizationRules/..." className="mt-1 w-full rounded-lg border px-3 py-2 font-mono text-[11px] font-normal" /><span className="mt-1 block text-[10px] text-gray-500">Use only when Azure inventory cannot list the namespace authorization rule.</span></label>}
          </div>}
          {resourceError && <div className="text-[10px] text-rose-700">{resourceError}</div>}{eventHubError && <div className="text-[10px] text-rose-700">{eventHubError}</div>}
          <label className="block text-xs font-medium text-gray-700">Diagnostic-setting name<input value={settingName} onChange={(event) => { setSettingName(event.target.value); resetPlan(); }} className={`mt-1 w-full rounded-lg border px-3 py-2 font-normal ${nameError ? "border-rose-300" : ""}`} />{nameError && <span className="mt-1 block text-[10px] text-rose-700">{nameError}</span>}</label>
          <div className="rounded-lg border border-sky-200 bg-sky-50 p-3 text-xs leading-5 text-sky-800"><strong>Separate control plane:</strong> This diagnostic setting exports subscription Activity Logs. It does not replace Security Activity Log alerts or Action Group notification routing. Submission creates pending records for review; Azure is unchanged until separately approved and applied in Managed Changes.</div>
        </section>}

        {step === 2 && <section>
          <div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-sm font-semibold text-gray-900">Preview exact diagnostic-setting operations</h3><p className="mt-1 text-xs text-gray-500">Equivalent rows are retained for evidence. Blocked rows fail closed and are never submitted.</p></div>{preview && <div className="flex flex-wrap gap-1">{(["create", "update", "equivalent", "blocked"] as const).map((value) => <span key={value} className={`rounded px-2 py-1 text-[10px] font-medium ${statusTone(value)}`}>{value} {preview.counts[value] ?? 0}</span>)}</div>}</div>
          {!preview ? <div className="mt-8 text-center text-xs text-gray-400">Build a preview to continue.</div> : <div className="mt-4 space-y-3">{preview.items.map((item) => <article key={item.target_id} className={`overflow-hidden rounded-xl border ${item.errors.length ? "border-rose-200" : "border-gray-200"}`}><div className="flex flex-wrap items-start gap-3 bg-gray-50 px-4 py-3"><span className={`rounded px-2 py-1 text-[10px] font-semibold uppercase ${statusTone(item.classification)}`}>{item.classification}</span><div className="min-w-0 flex-1"><strong className="text-xs text-gray-900">{item.subscription_id} · {item.setting_name}</strong><div className="mt-1 break-all font-mono text-[9px] text-gray-400">{item.target_id}</div></div><div className="text-right text-[10px] text-gray-500">{item.actionable ? "Pending change required" : "No change submitted"}</div></div><div className="grid gap-3 p-4 text-xs sm:grid-cols-2"><div><div className="text-[10px] font-semibold uppercase text-gray-500">Categories</div><div className="mt-1">{item.categories.join(", ")}</div></div><div><div className="text-[10px] font-semibold uppercase text-gray-500">Destination</div><div className="mt-1 capitalize">{item.destination.kind.replace("_", " ")} · {shortResource(item.destination.resource_id)}</div><div className="break-all font-mono text-[9px] text-gray-400">{item.destination.resource_id}</div>{item.destination.event_hub_name && <div className="text-[10px] text-gray-600">Event Hub: {item.destination.event_hub_name}</div>}</div></div>{item.errors.length > 0 && <div className="border-t border-rose-200 bg-rose-50 px-4 py-3 text-[10px] text-rose-800">{item.errors.map((message, index) => <div key={index}>• {message}</div>)}</div>}{item.blocker && <div className="border-t px-4 py-2 text-[10px] text-rose-700">Blocked by managed change {item.blocker.change_id} ({item.blocker.status}).</div>}</article>)}</div>}
          {preview && <label className="mt-4 block text-xs font-medium text-gray-700">Submission reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={1000} className="mt-1 min-h-20 w-full rounded-lg border px-3 py-2 font-normal" /></label>}
          {validated && <div role="status" className="mt-4 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-700"><strong>Validation passed.</strong> The actionable rows can now be submitted as approval-gated pending changes. No Azure write has occurred.</div>}
        </section>}
      </main>
      <footer className="flex flex-wrap items-center gap-2 border-t px-5 py-3"><span className="text-[10px] text-gray-500">{selected.length} subscriptions · 4 required categories · approval required · no immediate Azure write</span><div className="ml-auto flex gap-2">{step === 0 ? <button onClick={onBack} disabled={!!busy} className={buttonSecondary}>Back to alert setup</button> : <button onClick={() => { setStep((value) => value - 1); setError(""); }} disabled={!!busy} className={buttonSecondary}>Back</button>}<button onClick={onClose} disabled={!!busy} className={buttonSecondary}>Cancel</button>{step === 0 && <button onClick={() => setStep(1)} disabled={!!busy || !selected.length} className={buttonPrimary}>Configure destination</button>}{step === 1 && <button onClick={() => void buildPreview()} disabled={!!busy || !selected.length || !destinationValid || !canSubmit} className={buttonPrimary}>Preview operations</button>}{step === 2 && <><button onClick={() => void buildPreview()} disabled={!!busy || !destinationValid} className={buttonSecondary}>Refresh preview</button><button onClick={() => void validatePlan()} disabled={!!busy || !preview} className={buttonSecondary}>Validate</button><button onClick={() => void submitPlan()} disabled={!!busy || !preview || !validated || !reason.trim() || !canSubmit} className={buttonPrimary}>{busy === "submit" ? "Submitting…" : "Submit pending changes"}</button></>}</div></footer>
    </div>
  </div>;
}
