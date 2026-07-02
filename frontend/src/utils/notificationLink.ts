import type { AppNotification } from "../api";

/** Resolve the in-app destination a notification points at, from its source + links.
 * Returns null when there's nothing meaningful to open. Shared by the notification
 * dropdown and the full Notifications page. */
export function notificationLink(n: AppNotification): string | null {
  const links = (n.links ?? {}) as Record<string, unknown>;
  const s = (v: unknown) => (typeof v === "string" && v ? v : "");
  const runId = s(links.run_id);
  const threadId = s(links.thread_id);
  const kind = s(links.kind);
  const refId = s(links.id);

  // Deep-link to the produced artifact (works for direct events AND scheduled-task
  // events, whose result_ref keys {kind,id} are merged into links).
  if (n.source === "assessment" || kind === "assessment_run") {
    const id = runId || (kind === "assessment_run" ? refId : "");
    return id ? `/assessments/${id}` : "/assessments";
  }
  if (n.source === "workbook" || kind === "workbook_run" || s(links.workbook_id)) {
    return "/automations/workbooks";
  }
  if (n.source === "playbook" || kind === "playbook_run" || s(links.playbook_id)) {
    return "/automations/playbooks";
  }
  // Insight-pack digests open the AI Insight Packs runs tab.
  if (n.source === "insight_pack" || s(links.insight_run) || s(links.pack_id)) {
    return "/insights/runs";
  }
  // Agent runs open their chat thread; other scheduled tasks open the Schedules list.
  if (threadId) return `/c/${threadId}`;
  if (n.source === "task" || s(links.task_id)) return "/automations/tasks";
  return null;
}

export const SEVERITY_DOT: Record<string, string> = {
  info: "bg-gray-400",
  warning: "bg-amber-500",
  error: "bg-orange-500",
  critical: "bg-red-500",
};
