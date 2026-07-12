import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  api,
  type NotificationSimulation,
} from "../api";
import { formatError } from "../utils/format";
import { queryKeys, type AlertsManagerScopeParams } from "../queryKeys";
import { BulkPathSimulator } from "./alerts/BulkPathSimulator";

export function NotificationSimulatorPanel({ params }: { params: AlertsManagerScopeParams }) {
  const rulesQ = useQuery({ queryKey: queryKeys.alertsManager.rules(params), queryFn: () => api.managedAlertRules(params), staleTime: 5 * 60_000 });
  const groupsQ = useQuery({ queryKey: queryKeys.alertsManager.actionGroups(params), queryFn: () => api.managedActionGroups(params), staleTime: 5 * 60_000 });
  const [ruleId, setRuleId] = useState("");
  const [resourceId, setResourceId] = useState("");
  const [severity, setSeverity] = useState(3);
  const [timestamp, setTimestamp] = useState(() => new Date().toISOString().slice(0, 16));
  const [monitorCondition, setMonitorCondition] = useState<"Fired" | "Resolved">("Fired");
  const [selectedGroups, setSelectedGroups] = useState<string[]>([]);
  const [useSelectedOnly, setUseSelectedOnly] = useState(false);
  const [result, setResult] = useState<NotificationSimulation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const selected = (rulesQ.data?.rules || []).find((rule) => rule.id === ruleId);
  async function simulate() {
    if (!ruleId && !resourceId) return;
    setBusy(true); setError(""); setResult(null);
    try {
      setResult(await api.simulateNotificationPath({
        connection_id: params.connection_id, rule_id: ruleId, rule_name: selected?.name,
        family: selected?.family || "metric", resource_id: resourceId || selected?.scopes[0] || "",
        severity, timestamp: timestamp ? new Date(timestamp).toISOString() : "", action_group_ids: selected?.action_group_ids || [],
        selected_action_group_ids: selectedGroups, use_selected_only: useSelectedOnly, monitor_condition: monitorCondition,
      }));
    } catch (cause) { setError(formatError(cause)); }
    finally { setBusy(false); }
  }
  return <div className="space-y-4">
    <BulkPathSimulator params={params} />
    <div className="border-t pt-4"><div className="mb-3"><h2 className="font-semibold text-gray-900">Single-rule fidelity simulator</h2><p className="text-xs text-gray-500">Inspect payload schemas, mute windows, resolved behavior, and recent test outcomes for one rule.</p></div></div>
    <section className="rounded-xl border bg-white p-4"><div><h2 className="font-semibold">Notification path simulator</h2><p className="text-xs text-gray-500">Evaluate current rules, Action Groups, schemas, mute windows, and full receiver destinations without firing an alert.</p></div>{error && <div className="mt-3 rounded bg-red-50 p-3 text-sm text-red-700">{error}</div>}<div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-5"><label className="text-xs">Alert rule<select value={ruleId} onChange={(event) => { const rule = (rulesQ.data?.rules || []).find((item) => item.id === event.target.value); setRuleId(event.target.value); setResourceId(rule?.scopes[0] || ""); setSeverity(rule?.severity ?? 3); }} className="mt-1 w-full rounded border px-2 py-2"><option value="">Hypothetical alert…</option>{(rulesQ.data?.rules || []).map((rule) => <option key={rule.id} value={rule.id}>{rule.name} ({rule.family})</option>)}</select></label><label className="text-xs">Affected resource<input value={resourceId} onChange={(event) => setResourceId(event.target.value)} placeholder="Full ARM resource ID" className="mt-1 w-full rounded border px-2 py-2" /></label><label className="text-xs">Severity<select value={severity} onChange={(event) => setSeverity(Number(event.target.value))} className="mt-1 w-full rounded border px-2 py-2">{[0, 1, 2, 3, 4].map((item) => <option key={item} value={item}>Sev {item}</option>)}</select></label><label className="text-xs">Event state<select value={monitorCondition} onChange={(event) => setMonitorCondition(event.target.value as "Fired" | "Resolved")} className="mt-1 w-full rounded border px-2 py-2"><option>Fired</option><option>Resolved</option></select></label><label className="text-xs">Simulation time<input type="datetime-local" value={timestamp} onChange={(event) => setTimestamp(event.target.value)} className="mt-1 w-full rounded border px-2 py-2" /></label></div><div className="mt-3 rounded border p-3"><div className="flex items-center"><div><div className="text-xs font-semibold">Action Groups to simulate</div><div className="text-[10px] text-gray-500">Select additional groups to compare before attaching them to a rule.</div></div><label className="ml-auto text-xs"><input type="checkbox" checked={useSelectedOnly} onChange={(event) => setUseSelectedOnly(event.target.checked)} /> Use selected groups only</label></div><div className="mt-2 grid gap-2 md:grid-cols-2 xl:grid-cols-3">{(groupsQ.data?.action_groups || []).map((group) => <label key={group.id} className="flex items-center gap-2 rounded border px-2 py-1.5 text-xs"><input type="checkbox" checked={selectedGroups.includes(group.id)} onChange={(event) => setSelectedGroups(event.target.checked ? [...selectedGroups, group.id] : selectedGroups.filter((id) => id !== group.id))} /><span>{group.name}</span><span className="ml-auto text-[10px] text-gray-400">{group.receiver_count} receivers</span></label>)}</div></div><button disabled={busy || (!ruleId && !resourceId)} onClick={() => void simulate()} className="mt-3 rounded bg-gray-900 px-4 py-2 text-xs text-white disabled:opacity-50">{busy ? "Simulating…" : "Trace notification path"}</button></section>
    {result && <><div className="grid gap-3 sm:grid-cols-2"><div className="rounded-xl border bg-white p-4"><div className="text-2xl font-semibold">{result.would_run_count}</div><div className="text-xs text-gray-500">receivers would run</div></div><div className="rounded-xl border bg-white p-4"><div className="text-2xl font-semibold">{result.final_action_group_ids.length}</div><div className="text-xs text-gray-500">final Action Groups</div></div></div><section className="overflow-hidden rounded-xl border bg-white"><div className="border-b px-4 py-3"><h3 className="font-semibold">Receiver execution path</h3><p className="text-xs text-gray-500">Destinations are shown in full. “Would run” includes rule, Action Group, and receiver state.</p></div>{result.paths.length ? <div className="divide-y">{result.paths.map((path) => <div key={path.action_group_id} className="p-4"><div className="flex items-center"><strong>{path.name}</strong><span className={`ml-2 rounded px-2 py-0.5 text-[10px] ${path.enabled && !path.missing ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>{path.missing ? "missing" : path.enabled ? "enabled" : "disabled"}</span></div><div className="mt-2 grid gap-2 md:grid-cols-2">{path.receivers.map((receiver, index) => <div key={`${receiver.type}:${receiver.name}:${index}`} className="flex items-center gap-2 rounded border p-2 text-xs"><span className={`h-2 w-2 rounded-full ${receiver.would_run ? "bg-green-500" : "bg-gray-300"}`} /><span className="font-medium capitalize">{receiver.type}</span><span>{receiver.name}</span><span className="text-gray-500">{receiver.destination || receiver.masked}</span><span className={`ml-auto ${receiver.would_run ? "text-green-700" : "text-gray-500"}`}>{receiver.would_run ? "Would run" : receiver.blocked_reason}</span></div>)}</div></div>)}</div> : <div className="p-8 text-center text-sm text-gray-400">No Action Groups are attached.</div>}</section>{result.duplicate_paths.length > 0 && <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">{result.duplicate_paths.length} receiver path(s) appear in multiple Action Groups and may receive duplicate notifications.</div>}</>}
    {result && <SimulationFidelityDetails result={result} />}
  </div>;
}

function SimulationFidelityDetails({ result }: { result: NotificationSimulation }) {
  return <div className="grid gap-4 xl:grid-cols-2">
    <section className="rounded-xl border bg-white p-4"><h3 className="font-semibold">Delivery behavior</h3><div className="mt-2 grid gap-2 text-xs sm:grid-cols-2"><div className="rounded bg-gray-50 p-2"><strong>Event</strong><div>{result.monitor_condition}</div></div><div className="rounded bg-gray-50 p-2"><strong>Resolved notification</strong><div>{result.resolved_notification_expected ? "Expected" : "Not configured"}</div></div><div className="rounded bg-gray-50 p-2"><strong>Mute / throttle</strong><div>{result.mute_or_throttle_duration || "None"}{result.muted_or_throttled ? " · active now" : ""}</div></div><div className="rounded bg-gray-50 p-2"><strong>Historical alerts</strong><div>{result.history.fired_30d} fired · {result.history.resolved_30d} resolved (30d)</div></div></div><div className="mt-3 space-y-2">{result.paths.flatMap((path) => path.receivers.map((receiver, index) => <details key={`${path.action_group_id}:${index}`} className="rounded border p-2 text-xs"><summary className="cursor-pointer font-medium">{path.name} → {receiver.channel} · {receiver.payload_schema}</summary><div className="mt-2 text-gray-600">{receiver.constraints.map((constraint) => <div key={constraint}>• {constraint}</div>)}</div><pre className="mt-2 max-h-48 overflow-auto rounded bg-gray-950 p-2 text-[10px] text-gray-100">{JSON.stringify(receiver.payload_preview, null, 2)}</pre></details>))}</div></section>
    <section className="rounded-xl border bg-white p-4"><h3 className="font-semibold">Recent Action Group test outcomes</h3><p className="text-xs text-gray-500">Sanitized results from explicit test notifications; destinations and signed callbacks are omitted.</p>{result.history.test_deliveries.length ? <div className="mt-2 max-h-80 divide-y overflow-auto rounded border">{result.history.test_deliveries.map((delivery) => <div key={`${delivery.action_group_id}:${delivery.tested_at}`} className="p-3 text-xs"><div className="flex items-center"><strong>{delivery.state}</strong><span className="ml-auto text-gray-400">{new Date(delivery.tested_at).toLocaleString()}</span></div>{delivery.details.map((detail, index) => <div key={index} className="mt-1 text-gray-600">{detail.mechanism} · {detail.name}: {detail.status}{detail.detail ? ` — ${detail.detail}` : ""}</div>)}</div>)}</div> : <div className="mt-3 rounded bg-gray-50 p-6 text-center text-xs text-gray-400">No test-delivery history for the selected Action Groups.</div>}</section>
  </div>;
}

