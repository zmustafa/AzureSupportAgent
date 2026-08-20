import type { AccessRequirement } from "../utils/accessControl";
import {
  ADMIN_NAV,
  AUTOMATIONS_NAV,
  PROACTIVE_NAV,
  type AdminSection,
  type AutomationsSection,
} from "./navConfig";

export const CHAT_PERMISSION = "chat.use";
export const DASHBOARD_REQUIREMENT: AccessRequirement = { anyPermission: true };

export const PROACTIVE_PERMISSIONS = Array.from(new Set(PROACTIVE_NAV.map((item) => item.permission)));
export const ADMIN_PERMISSIONS = Array.from(new Set(ADMIN_NAV.map((item) => item.permission)));
export const AUTOMATION_PERMISSIONS = Array.from(new Set([
  "agents.read",
  "tasks.read",
  "workbooks.read",
  "playbooks.read",
  "notifications.manage",
]));

const TOP_LEVEL: [prefix: string, permission: string][] = [
  ["/mission-control", "missions.read"],
  ["/workloads", "workloads.read"],
  ["/ownership", "ownership.read"],
  ["/inventory", "inventory.read"],
  ["/tagintel", "tagintel.read"],
  ["/change-explorer", "changeexplorer.read"],
  ["/insights", "insights.read"],
  ["/iam", "iam.read"],
  ["/rbac", "iam.read"],
  ["/assessments", "assessments.read"],
  ["/architectures", "architectures.read"],
  ["/knowme", "architectures.read"],
  ["/fmea", "architectures.read"],
  ["/policy", "policy.read"],
  ["/entra", "entra.read"],
  ["/coverage", "coverage.read"],
  ["/alerts-manager", "alerts_manager.read"],
  ["/telemetry-intel", "teleintel.read"],
  ["/telemetry", "coverage.read"],
  ["/backup-manager", "backup_manager.read"],
  ["/resiliency", "resiliency.read"],
  ["/backupdr", "coverage.read"],
  ["/capability", "connections.read"],
  ["/evidence", "evidence.read"],
  ["/cases", "cases.read"],
  ["/radar", "radar.read"],
  ["/reservations", "reservations.read"],
  ["/quota", "quota.read"],
  ["/performance", "perfprofile.read"],
  ["/notifications", "notifications.read"],
  ["/monitor", "monitor.view"],
  ["/stats", "monitor.view"],
];

export function adminNavItem(section: AdminSection | string | undefined) {
  if (!section || section === "overview") return undefined;
  if (["users", "roles", "groups", "identity"].includes(section)) {
    return ADMIN_NAV.find((item) => item.id === "access");
  }
  return ADMIN_NAV.find((item) => item.id === section);
}

export function adminRequirement(section: AdminSection | string | undefined): AccessRequirement {
  const item = adminNavItem(section);
  return item ? { permission: item.permission } : { anyOf: ADMIN_PERMISSIONS };
}

export function adminWritePermission(section: AdminSection | string | undefined): string | undefined {
  return adminNavItem(section)?.writePermission;
}

export function automationNavItem(section: AutomationsSection | string | undefined) {
  return AUTOMATIONS_NAV.find((item) => item.id === section);
}

export function automationRequirement(section: AutomationsSection | string | undefined): AccessRequirement {
  if (section === "agents") return { permission: "agents.read" };
  if (section === "connectors") return { permission: "connectors.manage" };
  const item = automationNavItem(section);
  return item ? { permission: item.permission } : { anyOf: AUTOMATION_PERMISSIONS };
}

export function automationWritePermission(section: AutomationsSection | string | undefined): string | undefined {
  if (section === "agents") return "agents.write";
  if (section === "connectors") return "connectors.manage";
  return automationNavItem(section)?.writePermission;
}

export function routeRequirement(pathnameWithQuery: string): AccessRequirement {
  const pathname = pathnameWithQuery.split("?", 1)[0].replace(/\/$/, "") || "/";
  if (pathname === "/dashboard" || pathname === "/") return DASHBOARD_REQUIREMENT;
  if (pathname === "/chat" || pathname.startsWith("/c/")) return { permission: CHAT_PERMISSION };
  if (pathname === "/proactive") return { anyOf: PROACTIVE_PERMISSIONS };

  if (pathname === "/admin" || pathname.startsWith("/admin/")) {
    const section = pathname.split("/")[2] as AdminSection | undefined;
    return adminRequirement(section);
  }
  if (pathname === "/automations" || pathname.startsWith("/automations/")) {
    const section = pathname.split("/")[2] as AutomationsSection | undefined;
    return automationRequirement(section);
  }

  const match = TOP_LEVEL.find(([prefix]) => pathname === prefix || pathname.startsWith(`${prefix}/`));
  return match ? { permission: match[1] } : DASHBOARD_REQUIREMENT;
}
