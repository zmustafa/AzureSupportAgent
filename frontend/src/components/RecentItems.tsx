import { useEffect, useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import { api, type RecentItemTouch } from "../api";
import { queryKeys } from "../queryKeys";
import { CONNECTION_KEY } from "../utils/persistedState";
import { useAuth } from "./AuthContext";

const CHANNEL = "azsup-recent-items";
const CHANGE_KEY = "azsup.recentItems.changed";
const TOUCH_INTERVAL_MS = 10 * 60_000;
const touchedAt = new Map<string, number>();
// This is only an in-browser message origin, not a security token. Embedded/older browsers
// may expose Web Crypto without randomUUID, so keep navigation history functional there.
const TAB_ORIGIN = globalThis.crypto?.randomUUID?.()
  ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

const GENERIC: Array<[prefix: string, kind: string, title: string]> = [
  ["/telemetry-intel", "telemetry_intelligence", "Telemetry Intelligence"],
  ["/change-explorer", "change_explorer", "Change Explorer"],
  ["/alerts-manager", "alerts_manager", "Alerts Manager"],
  ["/backup-manager", "backup_manager", "Backup Manager"],
  ["/architectures", "architecture", "Architectures"],
  ["/reservations", "reservations", "Reservations Monitor"],
  ["/performance", "performance", "Performance Profiler"],
  ["/resiliency", "resiliency", "Recovery Readiness"],
  ["/inventory", "inventory", "Inventory"],
  ["/ownership", "ownership", "Ownership"],
  ["/tagintel", "tag_intelligence", "Tag Intelligence"],
  ["/workloads", "workload", "Azure Workloads"],
  ["/insights", "insight", "Daily Intelligence"],
  ["/evidence", "evidence", "Evidence Locker"],
  ["/coverage", "coverage", "Monitoring Coverage"],
  ["/telemetry", "telemetry", "Telemetry Coverage"],
  ["/backupdr", "backup_dr", "Backup & DR Coverage"],
  ["/capability", "capability", "Capability Matrix"],
  ["/policy", "policy", "Azure Policy"],
  ["/entra", "entra", "Entra ID"],
  ["/iam", "iam", "Access (IAM)"],
  ["/radar", "radar", "Retirement Radar"],
  ["/quota", "quota", "Quota Monitor"],
  ["/graph", "graph", "Estate Graph"],
  ["/monitor", "monitor", "Monitor"],
  ["/stats", "stats", "Usage Stats"],
];

const ICONS: Record<string, string> = {
  chat: "💬", workload: "🧩", workload_group: "🧩", mission: "🚀",
  assessment: "🛡️", architecture: "🗺️", know_me: "🧠", fmea: "🧮",
  case: "📁", insight: "🔭", evidence: "📎", graph: "🕸️", inventory: "📦",
  ownership: "👤", tag_intelligence: "🏷️", change_explorer: "🕒", iam: "🛂",
  entra: "🔑", policy: "📐", coverage: "📈", alerts_manager: "🚨",
  telemetry: "🛰️", backup_dr: "💾", backup_manager: "🛟", resiliency: "♻️",
  capability: "🧭", radar: "📡", reservations: "🏷️", quota: "📊",
  telemetry_intelligence: "🔬", performance: "⚡", monitor: "📊", stats: "📉",
};

export function recentItemIcon(kind: string): string {
  return ICONS[kind] ?? "↗";
}

function storedConnectionId(): string | undefined {
  try {
    const value = JSON.parse(localStorage.getItem(CONNECTION_KEY) || "null");
    return typeof value === "string" && value ? value : undefined;
  } catch {
    return undefined;
  }
}

function matchRoute(pathname: string, search: string): RecentItemTouch | null {
  if (pathname === "/" || pathname === "/dashboard" || pathname === "/chat"
      || pathname === "/proactive" || pathname.startsWith("/admin")
      || pathname === "/automations" || pathname === "/notifications") return null;
  const segments = pathname.split("/").filter(Boolean);
  if (segments[0] === "c") return null; // ChatView enriches this with the persisted chat title.
  let kind = "";
  let key = "";
  let title = "";

  if (segments[0] === "workloads" && segments[1] === "groups" && segments[2]) [kind, key, title] = ["workload_group", segments[2], "Workload group"];
  else if (segments[0] === "workloads" && segments[1] && !["groups", "overlaps"].includes(segments[1])) [kind, key, title] = ["workload", segments[1], "Azure workload"];
  else if (segments[0] === "mission-control" && segments[1]) [kind, key, title] = ["mission", segments[1], "Mission Control"];
  else if (segments[0] === "assessments" && segments[1]) [kind, key, title] = ["assessment", segments[1], "Assessment"];
  else if (segments[0] === "architectures" && segments[1] && segments[1] !== "memory") [kind, key, title] = ["architecture", segments[1], "Architecture"];
  else if (segments[0] === "knowme" && segments[1]) [kind, key, title] = ["know_me", segments[1], "Know-Me"];
  else if (segments[0] === "fmea" && segments[1]) [kind, key, title] = ["fmea", segments[1], "FMEA"];
  else if (segments[0] === "cases" && segments[1]) [kind, key, title] = ["case", segments[1], "Case"];
  else {
    const generic = GENERIC.find(([prefix]) => pathname === prefix || pathname.startsWith(`${prefix}/`));
    if (!generic) return null;
    [, kind, title] = generic;
    key = kind;
  }

  const params = new URLSearchParams(search);
  const connectionId = params.get("connection_id") || storedConnectionId() || null;
  const workloadId = params.get("workload_id") || (kind === "workload" || kind === "mission" ? key : null);
  return {
    kind,
    item_key: key === kind ? [kind, connectionId || "default", workloadId || "all"].join(":") : key,
    title,
    subtitle: "",
    route: `${pathname}${search}`,
    connection_id: connectionId,
    workload_id: workloadId,
  };
}

function headingTitle(fallback: string): string {
  const heading = document.querySelector<HTMLElement>("main h1");
  const text = heading?.textContent?.replace(/\s+/g, " ").trim() || "";
  if (!text || text.startsWith("Welcome back")) return fallback;
  return text.slice(0, 256);
}

function notifyRecentChanged(): void {
  try {
    localStorage.setItem(CHANGE_KEY, String(Date.now()));
  } catch {
    // Server persistence still succeeds when browser storage is unavailable.
  }
  try {
    const channel = new BroadcastChannel(CHANNEL);
    // The caller already invalidates its own query. Tag the broadcast so this tab does not
    // process the cross-tab echo and issue a third GET for the same navigation.
    channel.postMessage({ type: "changed", origin: TAB_ORIGIN });
    channel.close();
  } catch {
    // BroadcastChannel may be unavailable in an embedded/private browser.
  }
}

export function useRecentItems(limit = 8) {
  const queryClient = useQueryClient();
  useEffect(() => {
    let channel: BroadcastChannel | undefined;
    try {
      channel = new BroadcastChannel(CHANNEL);
      channel.onmessage = (event) => {
        if (event.data?.origin !== TAB_ORIGIN) {
          void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard.recentItems });
        }
      };
    } catch {
      // The local query cache still updates in this tab.
    }
    const onStorage = (event: StorageEvent) => {
      if (event.key === CHANGE_KEY) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard.recentItems });
      }
    };
    window.addEventListener("storage", onStorage);
    return () => {
      channel?.close();
      window.removeEventListener("storage", onStorage);
    };
  }, [queryClient]);
  return useQuery({
    queryKey: queryKeys.dashboard.recentItems,
    queryFn: () => api.recentItems(limit),
    staleTime: 30_000,
    retry: false,
  });
}

export function useRecentItem(item: RecentItemTouch | null): void {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const stable = useMemo(() => item && JSON.stringify(item), [item]);
  useEffect(() => {
    if (!user || !stable) return;
    const value = JSON.parse(stable) as RecentItemTouch;
    const touchKey = `${user.tenant_id}:${user.subject}:${value.kind}:${value.item_key}:${value.route}`;
    const now = Date.now();
    if (touchedAt.size > 200) {
      for (const [key, touched] of touchedAt) {
        if (now - touched >= TOUCH_INTERVAL_MS) touchedAt.delete(key);
      }
    }
    if (now - (touchedAt.get(touchKey) ?? 0) < TOUCH_INTERVAL_MS) return;
    const timer = window.setTimeout(() => {
      const enriched = { ...value, title: headingTitle(value.title) };
      void api.touchRecentItem(enriched).then(() => {
        touchedAt.set(touchKey, Date.now());
        void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard.recentItems });
        notifyRecentChanged();
      }).catch(() => {
        // Navigation history is best-effort and must never break the destination page.
      });
    }, 2_000);
    return () => window.clearTimeout(timer);
  }, [queryClient, stable, user]);
}

/** Track approved entity/scoped routes after they remain open long enough to count as a visit. */
export function RecentItemTracker() {
  const location = useLocation();
  const item = useMemo(
    () => matchRoute(location.pathname, location.search),
    [location.pathname, location.search],
  );
  useRecentItem(item);
  return null;
}

export function announceRecentChanged(): void {
  notifyRecentChanged();
}
