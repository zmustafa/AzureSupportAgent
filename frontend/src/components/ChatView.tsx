import { useEffect, useRef, useState, memo, createContext, useContext, useCallback, useMemo, Children, isValidElement, lazy, Suspense } from "react";
import { flushSync } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams, useLocation, Link } from "react-router-dom";
import { Markdown } from "./LazyMarkdown";
import {
  api,
  streamMessage,
  reconnectStream,
  isTurnActive,
  streamCommand,
  type Chat,
  type Message,
  type MessageScope,
  type StreamHandlers,
  type SubscriptionOption,
  type ManagementGroupOption,
  type TenantOption,
  type Investigation,
  type HypothesisNode,
  type InvestigationConclusion,
  type DeepAgent,
  type CustomAgent,
} from "../api";
import { PROBLEM_TREE, PROBLEM_CATALOG, type ProblemNode } from "../problemTree";
import { CopyButton } from "./CopyButton";
import {
  ADMIN_NAV,
  ADMIN_SECTION_IDS,
  ACCESS_SUB_IDS,
  AUTOMATIONS_NAV,
  POLICY_TAB_IDS,
  INVENTORY_TAB_IDS,
  TAGINTEL_TAB_IDS,
  CHANGEEXPLORER_TAB_IDS,
  RBAC_TAB_IDS,
  OWNERSHIP_TAB_IDS,
  IDENTITY_TAB_IDS,
  type AdminSection,
  type AutomationsSection,
  type PolicyTab,
  type InventoryTab,
  type TagIntelTab,
  type ChangeExplorerTab,
  type RbacTab,
  type OwnershipTab,
  type IdentityTab,
} from "./navConfig";
import { useAuth } from "./AuthContext";
import { formatDuration, formatTimestamp } from "../utils/format";
import {
  ComposeIcon,
  SearchIcon,
  PinIcon,
  PencilIcon,
  TrashIcon,
  SettingsIcon,
  InventoryIcon,
  WorkloadIcon,
  OwnershipIcon,
  TagIcon,
  ChangeIcon,
  DashboardIcon,
  MissionControlIcon,
  AssessmentIcon,
  MonitorIcon,
  StatsIcon,
  ArchitectureIcon,
  GraphIcon,
  PolicyIcon,
  RbacIcon,
  IdentityIcon,
  CoverageIcon,
  TelemetryIcon,
  BackupIcon,
  EvidenceIcon,
  RadarIcon,
  ReservationIcon,
  TelemetryIntelIcon,
  PerformanceIcon,
  ProactiveIcon,
  BoltIcon,
  RobotIcon,
  ChevronRightIcon,
  SparkleIcon,
  WarRoomIcon,
  PanelLeftIcon,
  Spinner,
} from "./chat/icons";
import { PanelErrorBoundary } from "./chat/PanelErrorBoundary";

// Heavy, admin-only panels — lazy-loaded (code-split) so the initial chat bundle stays
// small. They only download when the user first opens Settings/Automations/Monitor.
const AdminPanel = lazy(() => import("./AdminView").then((m) => ({ default: m.AdminPanel })));
const AutomationsPanel = lazy(() =>
  import("./AutomationsView").then((m) => ({ default: m.AutomationsPanel })),
);
const MonitorPanel = lazy(() => import("./MonitorView").then((m) => ({ default: m.MonitorPanel })));
const StatsPanel = lazy(() => import("./MonitorView").then((m) => ({ default: m.StatsPanel })));
const WorkloadsPanel = lazy(() =>
  import("./WorkloadsView").then((m) => ({ default: m.WorkloadsPanel })),
);
const OwnershipPanel = lazy(() =>
  import("./OwnershipView").then((m) => ({ default: m.OwnershipPanel })),
);
const MissionControlPanel = lazy(() =>
  import("./MissionControlView").then((m) => ({ default: m.MissionControlPanel })),
);
const InventoryPanel = lazy(() =>
  import("./InventoryView").then((m) => ({ default: m.InventoryPanel })),
);
const TagIntelligencePanel = lazy(() =>
  import("./TagIntelligenceView").then((m) => ({ default: m.TagIntelligencePanel })),
);
const ChangeExplorerPanel = lazy(() =>
  import("./ChangeExplorerView").then((m) => ({ default: m.ChangeExplorerPanel })),
);
const AssessmentsPanel = lazy(() =>
  import("./AssessmentsView").then((m) => ({ default: m.AssessmentsPanel })),
);
const ArchitecturesPanel = lazy(() =>
  import("./ArchitecturesView").then((m) => ({ default: m.ArchitecturesPanel })),
);
const PolicyPanel = lazy(() =>
  import("./PolicyView").then((m) => ({ default: m.PolicyPanel })),
);
const RbacPanel = lazy(() =>
  import("./RbacView").then((m) => ({ default: m.RbacPanel })),
);
const IdentityPanel = lazy(() =>
  import("./IdentityView").then((m) => ({ default: m.IdentityPanel })),
);
const MonitoringCoveragePanel = lazy(() =>
  import("./MonitoringCoverageView").then((m) => ({ default: m.MonitoringCoveragePanel })),
);
const TelemetryCoveragePanel = lazy(() =>
  import("./TelemetryCoverageView").then((m) => ({ default: m.TelemetryCoveragePanel })),
);
const BackupDrCoveragePanel = lazy(() =>
  import("./BackupDrCoverageView").then((m) => ({ default: m.BackupDrCoveragePanel })),
);
const EvidenceLockerPanel = lazy(() =>
  import("./EvidenceLockerView").then((m) => ({ default: m.EvidenceLockerPanel })),
);
const RetirementRadarPanel = lazy(() =>
  import("./RetirementRadarView").then((m) => ({ default: m.RetirementRadarPanel })),
);
const GraphPanel = lazy(() =>
  import("./GraphView").then((m) => ({ default: m.GraphPanel })),
);
const TelemetryIntelligencePanel = lazy(() =>
  import("./TelemetryIntelligenceView").then((m) => ({ default: m.TelemetryIntelligencePanel })),
);
const ReservationsMonitorPanel = lazy(() =>
  import("./ReservationsMonitorView").then((m) => ({ default: m.ReservationsMonitorPanel })),
);
const PerformancePanel = lazy(() =>
  import("./PerformanceView").then((m) => ({ default: m.PerformancePanel })),
);
const NotificationsPanel = lazy(() =>
  import("./NotificationsView").then((m) => ({ default: m.NotificationsPanel })),
);
const DashboardPanel = lazy(() =>
  import("./DashboardView").then((m) => ({ default: m.DashboardPanel })),
);
// Interactive metric chart rendered inline from a ```azchart fenced block (recharts is
// heavy, so it's code-split out of the initial chat bundle).
const MetricChart = lazy(() =>
  import("./chat/MetricChart").then((m) => ({ default: m.MetricChart })),
);
import { NotificationBell } from "./NotificationBell";

// Fallback shown while a lazy panel chunk loads.
function PanelLoading() {
  return (
    <div className="flex h-full items-center justify-center text-sm text-gray-400">
      Loading…
    </div>
  );
}

// Shown on an empty chat so the user can start with one click instead of typing.
// Grouped into categories of the most common real-world Azure troubleshooting tasks,
// including deeper multi-step investigations the agent can run end to end.
type PromptCategory = { title: string; icon: string; prompts: string[] };

const STARTER_CATEGORIES: PromptCategory[] = [
  {
    title: "Connectivity & Networking",
    icon: "🌐",
    prompts: [
      "Diagnose why a VM can't be reached over RDP/SSH",
      "Find NSGs blocking traffic to a subnet or VM",
      "Trace why an app can't reach a private endpoint",
      "Check private DNS resolution for private endpoints",
      "Audit VPN/ExpressRoute and VNet peering connectivity",
    ],
  },
  {
    title: "Identity & Access",
    icon: "🔑",
    prompts: [
      "Find role assignments granting Owner or Contributor broadly",
      "Check Key Vault access policies and firewall settings",
      "Find service principals or app secrets nearing expiry",
      "Diagnose why a managed identity can't access a resource",
      "Review RBAC for over-privileged users and identities",
    ],
  },
  {
    title: "Security & Exposure",
    icon: "🔐",
    prompts: [
      "Find resources publicly exposed to the internet",
      "Find NSG rules allowing inbound from any source (0.0.0.0/0)",
      "List storage accounts with public network access enabled",
      "Review Microsoft Defender for Cloud recommendations",
      "Find public IPs and what they are attached to",
    ],
  },
  {
    title: "Compute & Apps",
    icon: "⚡",
    prompts: [
      "Investigate why an App Service is returning 5xx errors",
      "Diagnose a VM that won't boot or is stuck",
      "Troubleshoot failing AKS pods or unhealthy nodes",
      "Check why a Function App or Logic App is failing",
      "Review VM scale set health and instance failures",
    ],
  },
  {
    title: "Performance & Reliability",
    icon: "📈",
    prompts: [
      "Check resource health for degraded or unavailable resources",
      "List active Azure Service Health incidents",
      "Diagnose throttling or 429 errors on a resource",
      "Find VMs that are stopped, deallocated, or unhealthy",
      "Review autoscale settings and capacity for a workload",
    ],
  },
  {
    title: "Cost & Governance",
    icon: "💰",
    prompts: [
      "Find orphaned disks, NICs, and unattached public IPs",
      "Identify the most expensive resource types in my subscription",
      "Get Azure Advisor cost and reliability recommendations",
      "Find resources missing required tags",
      "List resources that violate Azure Policy compliance",
    ],
  },
];

type Step =
  | { kind: "reasoning"; text: string }
  | {
      kind: "tool";
      name: string;
      args: unknown;
      status: "running" | "done" | "error";
      summary?: string;
      duration?: number;
    };

// A single line in the live progress feed shown while the agent works.
type LogLine = {
  kind: "info" | "tool" | "result" | "reason";
  text: string;
  ts: number;
  pending?: boolean;
  detail?: string;
  // Transient "thinking" placeholder (Brainstorming…/Formulating…/etc.). Only the
  // newest one is kept while working, and all are removed once the turn finishes.
  thinking?: boolean;
};

// Lets fenced code blocks (rendered deep in markdown) know whether the host "Run"
// button is enabled, which CLI binaries are allowed, and which chat to run against.
// `openEditor` opens the right-side editor panel pre-filled with a command/query.
type ExecMode = "command" | "kql";
type ExecConfig = {
  enabled: boolean;
  allowlist: string[];
  chatId: string | null;
  openEditor: (command: string, mode: ExecMode) => void;
  // Opens the right-side Mermaid editor drawer pre-filled with a diagram's source,
  // with a live preview that updates as the user edits.
  openMermaidEditor: (code: string) => void;
};
const ExecContext = createContext<ExecConfig>({
  enabled: false,
  allowlist: [],
  chatId: null,
  openEditor: () => {},
  openMermaidEditor: () => {},
});

const ACTIVITIES_KEY = "azsup.activities.v1";
// Full progress feed (the exact lines shown under "Working on your request…")
// persisted per assistant message so the detailed work survives after completion.
const PROGRESS_KEY = "azsup.progress.v1";
// Deep-investigation hypothesis trees, persisted per assistant message.
const INVESTIGATIONS_KEY = "azsup.investigations.v1";
// LocalStorage flag: the user has acknowledged the one-time deep-investigation notice.
const DEEP_CONFIRMED_KEY = "azsup.deepConfirmed.v1";
// Persisted width (px) of the resizable deep-investigation side panel.
const INVESTIGATION_WIDTH_KEY = "azsup.investigationWidth.v1";
// LocalStorage flag: keep the deep-investigation panel pinned so it auto-reopens when
// switching to a chat that has a running or completed investigation.
const INVESTIGATION_PINNED_KEY = "azsup.investigationPinned.v1";
// Persisted width (px) of the resizable command/query editor side panel.
const EDITOR_WIDTH_KEY = "azsup.editorWidth.v1";
// Persisted width (px) of the resizable Mermaid diagram editor side panel.
const MERMAID_WIDTH_KEY = "azsup.mermaidEditorWidth.v1";

// Animated deep-investigation icon (served from frontend/public/agent-icons), shown at
// the top while a deep investigation runs.
const DEEP_INVESTIGATION_ICON = "/agent-icons/agent-orbit-repair.svg";

// Playful "the model is thinking" phrases, shown (one picked at random per turn) in
// place of the old fixed "Model is responding…" / "Writing the answer…" lines.
const THINKING_PHRASES = [
  "Brainstorming…",
  "Mining diamonds…",
  "Processing…",
  "Synthesizing…",
  "Reflecting…",
  "Cogitating…",
  "Formulating…",
  "Contemplating…",
  "Pondering…",
  "Reasoning…",
];

function randomThinkingPhrase(): string {
  return THINKING_PHRASES[Math.floor(Math.random() * THINKING_PHRASES.length)];
}

// Host "Run" command results, persisted per (chat, command) so a command's output
// stays part of the conversation when the chat is reopened.
const EXEC_KEY = "azsup.execRuns.v1";

// One persisted command run (output + exit/error), keyed by `${chatId}::${command}`.
type ExecRun = {
  output: { text: string; stream: "stdout" | "stderr" }[];
  exit: { code: number | null; duration_ms: number } | null;
  error: string;
};

function loadExecRuns(): Record<string, ExecRun> {
  try {
    return JSON.parse(localStorage.getItem(EXEC_KEY) || "{}");
  } catch {
    return {};
  }
}

function execRunKey(chatId: string, command: string): string {
  return `${chatId}::${command}`;
}

function getExecRun(chatId: string | null, command: string): ExecRun | undefined {
  if (!chatId) return undefined;
  return loadExecRuns()[execRunKey(chatId, command)];
}

function saveExecRun(chatId: string, command: string, run: ExecRun): void {
  try {
    const all = loadExecRuns();
    all[execRunKey(chatId, command)] = run;
    const capped = capRecord(all, MAX_PERSISTED_ENTRIES);
    localStorage.setItem(EXEC_KEY, JSON.stringify(capped));
  } catch {
    /* ignore quota errors */
  }
}

// Single-line-ish preview of tool arguments for the live feed. Kept generous so the
// full command/parameters are visible (the UI wraps long values); only extreme
// payloads are trimmed to avoid bloating the feed.
function argsPreview(args: unknown): string | undefined {
  if (!args || typeof args !== "object") return undefined;
  const entries = Object.entries(args as Record<string, unknown>);
  if (entries.length === 0) return undefined;
  const s = entries
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
  return s.length > 600 ? s.slice(0, 600) + "…" : s;
}

function loadActivities(): Record<string, Step[]> {
  try {
    return JSON.parse(localStorage.getItem(ACTIVITIES_KEY) || "{}");
  } catch {
    return {};
  }
}

function loadProgress(): Record<string, LogLine[]> {
  try {
    return JSON.parse(localStorage.getItem(PROGRESS_KEY) || "{}");
  } catch {
    return {};
  }
}

function loadInvestigations(): Record<string, Investigation> {
  try {
    return JSON.parse(localStorage.getItem(INVESTIGATIONS_KEY) || "{}");
  } catch {
    return {};
  }
}

// Cap how many per-message activity/progress entries we keep in localStorage so they
// can't grow without bound across many chats (objects preserve insertion order, so we
// drop the oldest keys past the limit).
const MAX_PERSISTED_ENTRIES = 200;

function capRecord<T>(rec: Record<string, T>, max: number): Record<string, T> {
  const keys = Object.keys(rec);
  if (keys.length <= max) return rec;
  const keep = keys.slice(keys.length - max);
  const out: Record<string, T> = {};
  for (const k of keep) out[k] = rec[k];
  return out;
}

type LiveStream = {
  streaming: boolean;
  started: boolean;
  streamText: string;
  steps: Step[];
  // Live status feed shown immediately while the agent works.
  log: LogLine[];
  startedAt: number;
  error?: string;
  // Live deep-investigation tree (deep thinking level only).
  investigation?: Investigation;
  // Randomly-chosen animated icon shown at the top while a deep investigation runs.
  deepIcon?: string;
  // Optimistic user message shown immediately, before the server reload.
  userMessage: Message;
  // Allows the Stop button to cancel the in-flight fetch for this chat.
  abort?: AbortController;
};

export default function ChatView() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { chatId: routeChatId } = useParams<{ chatId?: string }>();
  const location = useLocation();
  // Automations live inside this shell so the chat sidebar stays visible. The URL
  // drives which panel shows, so a browser refresh restores the same view.
  const inAutomations = location.pathname.startsWith("/automations");
  const automationsSection: AutomationsSection = (() => {
    const seg = location.pathname.split("/")[2] as AutomationsSection | undefined;
    return seg === "tasks" ||
      seg === "agents" ||
      seg === "connectors" ||
      seg === "workbooks" ||
      seg === "playbooks" ||
      seg === "notifications"
      ? seg
      : "overview";
  })();
  // The Custom Agents management page lives at /automations/agents but is surfaced as
  // its own top-level sidebar menu, so we track it separately from Automations.
  const inCustomAgents = automationsSection === "agents";
  // Settings (admin) also live inside this shell so the chat sidebar stays visible.
  const inAdmin = location.pathname.startsWith("/admin");
  const adminSection: AdminSection = (() => {
    const seg = location.pathname.split("/")[2] as AdminSection | undefined;
    // Bare /admin (no section) shows the Settings overview landing page; the submenu still
    // auto-expands. A concrete /admin/<section> renders that section.
    return seg && ADMIN_SECTION_IDS.has(seg) ? seg : "overview";
  })();
  // Monitor dashboard lives in the shell too.
  const inMonitor = location.pathname.startsWith("/monitor");
  // Stats: a read-only at-a-glance metrics page.
  const inStats = location.pathname.startsWith("/stats");
  // Azure Workloads section.
  const inWorkloads = location.pathname.startsWith("/workloads");
  // Ownership section. Sub-tab lives in the URL (/ownership/:tab) so a refresh restores it.
  const inOwnership = location.pathname.startsWith("/ownership");
  const ownershipTab: OwnershipTab = (() => {
    const seg = location.pathname.split("/")[2] as OwnershipTab | undefined;
    return seg && OWNERSHIP_TAB_IDS.has(seg) ? seg : "directory";
  })();
  const inMissionControl = location.pathname.startsWith("/mission-control");
  const inInventory = location.pathname.startsWith("/inventory");
  // Inventory sub-tab lives in the URL (/inventory/:tab) so a refresh restores the same view.
  const inventoryTab: InventoryTab = (() => {
    const seg = location.pathname.split("/")[2] as InventoryTab | undefined;
    return seg && INVENTORY_TAB_IDS.has(seg) ? seg : "grid";
  })();
  const inAssessments = location.pathname.startsWith("/assessments");
  const inArchitectures = location.pathname.startsWith("/architectures");
  const inPolicy = location.pathname.startsWith("/policy");
  // Tag Intelligence. Sub-tab lives in the URL (/tagintel/:tab) so a refresh restores the view.
  const inTagIntel = location.pathname.startsWith("/tagintel");
  const tagIntelTab: TagIntelTab = (() => {
    const seg = location.pathname.split("/")[2] as TagIntelTab | undefined;
    return seg && TAGINTEL_TAB_IDS.has(seg) ? seg : "census";
  })();
  // Change Explorer. Sub-tab lives in the URL (/change-explorer/:tab).
  const inChangeExplorer = location.pathname.startsWith("/change-explorer");
  const changeExplorerTab: ChangeExplorerTab = (() => {
    const seg = location.pathname.split("/")[2] as ChangeExplorerTab | undefined;
    return seg && CHANGEEXPLORER_TAB_IDS.has(seg) ? seg : "summary";
  })();
  // RBAC / Access Review. Sub-tab lives in the URL (/rbac/:tab) so a refresh restores the view.
  const inRbac = location.pathname.startsWith("/rbac");
  const rbacTab: RbacTab = (() => {
    const seg = location.pathname.split("/")[2] as RbacTab | undefined;
    return seg && RBAC_TAB_IDS.has(seg) ? seg : "overview";
  })();
  // Identity posture dashboard (admin-only).
  const inIdentity = location.pathname.startsWith("/identity");
  // Identity sub-tab lives in the URL (/identity/:tab) so a refresh restores the same view.
  const identityTab: IdentityTab = (() => {
    const seg = location.pathname.split("/")[2] as IdentityTab | undefined;
    return seg && IDENTITY_TAB_IDS.has(seg) ? seg : "overview";
  })();
  // Monitoring Coverage (AMBA) dashboard (admin-only).
  const inCoverage = location.pathname.startsWith("/coverage");
  // Telemetry Coverage (diagnostic settings) dashboard (admin-only).
  const inTelemetry = location.pathname.startsWith("/telemetry") && !location.pathname.startsWith("/telemetry-intel");
  // Backup & DR Coverage dashboard (admin-only).
  const inBackupDr = location.pathname.startsWith("/backupdr");
  // Evidence Locker (investigation snapshots).
  const inEvidence = location.pathname.startsWith("/evidence");
  // Retirement & Breaking-Change Radar (admin-only).
  const inRadar = location.pathname.startsWith("/radar");
  // Telemetry Intelligence (AI correlation & triage over App Insights, admin-only).
  const inTeleIntel = location.pathname.startsWith("/telemetry-intel");
  // Reservations Monitor (weekly expiry digest, admin-only).
  const inReservations = location.pathname.startsWith("/reservations");
  // Performance Profiler (profile against AMBA, admin-only).
  const inPerformance = location.pathname.startsWith("/performance");
  // Estate Graph: central workload-aware knowledge graph (admin-only).
  const inGraph = location.pathname.startsWith("/graph");
  // Azure Policy sub-tab lives in the URL (/policy/:tab) so a refresh restores the same view.
  const policyTab: PolicyTab = (() => {
    const seg = location.pathname.split("/")[2] as PolicyTab | undefined;
    return seg && POLICY_TAB_IDS.has(seg) ? seg : "overview";
  })();
  const inNotifications = location.pathname.startsWith("/notifications");
  // Getting-started / overview page for newcomers.
  const inDashboard = location.pathname.startsWith("/dashboard");
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  // Per-chat composer drafts. The `input` state is a single global value, so without
  // this a half-typed message would follow the user into whatever chat they switch to.
  // We stash the outgoing chat's draft here (keyed by chat id; "" = the new-chat view)
  // and restore the incoming chat's draft on switch. `inputRef` mirrors `input` so the
  // async URL-sync effect always reads the latest text without a stale closure.
  const draftsRef = useRef<Map<string, string>>(new Map());
  const inputRef = useRef("");
  // Set the composer text and keep the mirror ref in lockstep.
  const setInputSynced = (v: string) => {
    inputRef.current = v;
    setInput(v);
  };
  // Drop a suggested prompt into the composer (instead of sending it) so the user can
  // edit it before sending. Focuses the textarea and places the cursor at the end.
  const fillComposer = (text: string) => {
    setInputSynced(text);
    setTimeout(() => {
      const el = composerRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(text.length, text.length);
    }, 0);
  };
  // Images (base64 data URLs) the user pasted/attached for the next message.
  const [attachments, setAttachments] = useState<string[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Composer textarea ref, used to auto-grow its height with the typed content.
  const composerRef = useRef<HTMLTextAreaElement>(null);
  // Images captured at send time, read by runSend (survives the clarify detour).
  const pendingImagesRef = useRef<string[]>([]);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  // Inline rename state for the sidebar.
  const [renamingId, setRenamingId] = useState<string | null>(null);
  // Sidebar chat search query.
  const [chatSearch, setChatSearch] = useState("");
  // Sidebar: collapse to an icon-only rail (persisted).
  const [railCollapsed, setRailCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem("azsup.railCollapsed.v1") === "1";
    } catch {
      return false;
    }
  });
  function toggleRail() {
    setRailCollapsed((v) => {
      const next = !v;
      try {
        localStorage.setItem("azsup.railCollapsed.v1", next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  }
  // Quick filter for the chat list: all threads or pinned only.
  const [chatFilter, setChatFilter] = useState<"all" | "pinned">("all");
  // Automations sub-menu expansion in the sidebar (auto-open on an automations route).
  const [automationsOpen, setAutomationsOpen] = useState<boolean>(false);
  // Proactive Support sub-menu expansion (groups the posture/forensic dashboards).
  const [proactiveOpen, setProactiveOpen] = useState<boolean>(false);
  // Settings sub-menu expansion in the sidebar (auto-open on an /admin route).
  const [adminOpen, setAdminOpen] = useState<boolean>(false);

  // Auto-open each sidebar sub-menu when you navigate INTO its section, AND collapse it
  // when you leave — matching the behavior of the Settings sub-menu so all expandable
  // groups behave the same. The chevron still toggles per session while you're inside.
  useEffect(() => {
    setAutomationsOpen(inAutomations && !inCustomAgents);
  }, [inAutomations, inCustomAgents]);
  // NOTE: Architectures is a top-level item — NOT a Proactive Support child — so it must
  // not appear in this condition. Including it caused Proactive Support to wrongly auto-
  // expand whenever the user opened Architectures.
  const inAnyProactive = inInventory || inPolicy || inAssessments || inRbac || inIdentity
    || inCoverage || inTelemetry || inBackupDr || inEvidence || inRadar || inReservations
    || inTeleIntel || inPerformance || inTagIntel || inChangeExplorer;
  useEffect(() => {
    setProactiveOpen(inAnyProactive);
  }, [inAnyProactive]);
  // Settings tracks the route: opens on an /admin route and collapses when you navigate
  // away to a section outside Settings (still chevron-collapsible while inside /admin).
  useEffect(() => {
    setAdminOpen(inAdmin);
  }, [inAdmin]);
  // Pending scope clarification: when a question is ambiguous we ask the user to
  // pick a subscription (or skip) before running the agent. For governance/policy
  // questions we instead ask which management group to scope to.
  const [pendingClarify, setPendingClarify] = useState<{
    content: string;
    options: SubscriptionOption[];
    mgOptions?: ManagementGroupOption[];
    mgScope?: { management_group_id: string; management_group_name: string };
    checking?: boolean;
    thinking?: "normal" | "deep";
  } | null>(null);
  // Pending "propose problems": on the first message of a new chat we offer up to 5
  // sharper, catalog-matched problem statements for the user to pick from (or keep
  // their original wording) before the agent runs.
  const [pendingPropose, setPendingPropose] = useState<{
    chatId: string;
    content: string;
    suggestions: string[];
    checking?: boolean;
  } | null>(null);
  // Optimistic activity for the just-finished turn until the server reload carries
  // it on the message itself (historical panes render from message.activity).
  const [activities, setActivities] = useState<Record<string, Step[]>>(loadActivities);
  // Full progress feed per assistant message (persisted; mirrors the live feed).
  const [progressLogs, setProgressLogs] = useState<Record<string, LogLine[]>>(loadProgress);
  // Persisted deep-investigation tree per assistant message (survives reload).
  const [investigations, setInvestigations] = useState<Record<string, Investigation>>(
    loadInvestigations,
  );
  // The assistant message whose deep-investigation panel is open (right drawer).
  const [openInvestigation, setOpenInvestigation] = useState<string | null>(null);
  // When pinned, the deep-investigation panel auto-reopens on switching to a chat that
  // has a running or completed investigation (instead of needing the reopen pill).
  const [investigationPinned, setInvestigationPinned] = useState<boolean>(() => {
    try {
      return localStorage.getItem(INVESTIGATION_PINNED_KEY) === "1";
    } catch {
      return false;
    }
  });
  const toggleInvestigationPinned = useCallback(() => {
    setInvestigationPinned((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(INVESTIGATION_PINNED_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);
  // Pending deep-investigation authorization: when deep mode is on and the user sends,
  // we ask them to confirm (Continue/Cancel) before running the long investigation.
  const [pendingDeepAuth, setPendingDeepAuth] = useState<{
    chatId: string;
    content: string;
    hasImages: boolean;
    // When set, the launch path is a regenerate/resend — propagated to runSend so the
    // backend trims trailing assistant turns instead of appending a duplicate.
    regenerate?: boolean;
  } | null>(null);
  // Specialist agent suggestions for the deep-investigation launch popup, plus the
  // user's current selection. Loaded (AI-pre-selected) when the auth card appears.
  const [deepAgentOptions, setDeepAgentOptions] = useState<DeepAgent[] | null>(null);
  const [deepAgentSel, setDeepAgentSel] = useState<Set<string>>(new Set());
  // "All Hands On Deck": when active, every visible agent is selected; toggling off
  // restores the selection that was in effect before it was engaged.
  const [allHandsActive, setAllHandsActive] = useState(false);
  const [allHandsPrevSel, setAllHandsPrevSel] = useState<Set<string>>(new Set());
  // Architecture Memory to inject into the deep investigation. "" = auto-resolve
  // (backend uses the sole candidate if there is exactly one). A specific architecture
  // id pins that architecture's memory; "none" suppresses memory entirely.
  const [deepMemorySel, setDeepMemorySel] = useState<string>("");
  // Reasoning effort for the active chat: "normal" or "deep" investigation. Synced
  // from the chat's saved level and changed via the composer dropdown.
  const [thinkingLevel, setThinkingLevel] = useState<"normal" | "deep">("normal");
  // Selected custom agent for the active chat (null = default assistant). Synced from
  // the chat's saved agent and changed via the composer's agent selector.
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [selectedWorkloadId, setSelectedWorkloadId] = useState<string | null>(null);
  // First-time deep-investigation notice modal (shown when enabling deep mode).
  const [deepNotice, setDeepNotice] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  // Scroll container for the message list. We only auto-scroll when the user is
  // already pinned to the bottom; otherwise we surface a "New messages" pill so a
  // streaming response never yanks the viewport away from what they're reading.
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const [showNewMessages, setShowNewMessages] = useState(false);
  // Command/query editor panel (right drawer). Open when a code block's "Edit" button
  // is clicked; lets the user modify the command/query before running it.
  const [editorState, setEditorState] = useState<{ command: string; mode: ExecMode } | null>(
    null,
  );
  // Mermaid diagram editor drawer: holds the source being edited (null = closed).
  const [mermaidEdit, setMermaidEdit] = useState<string | null>(null);
  // Tracks the chat the user is currently viewing, used to guard async work.
  const viewRef = useRef<string | null>(null);
  // In-flight streams keyed by chat id. Streams keep running and updating their
  // entry here even when the user navigates away, so returning to the chat shows
  // the live progress again. A tick state forces re-render on mutation.
  const streamsRef = useRef<Map<string, LiveStream>>(new Map());
  // The set of chat ids with an in-flight turn, kept in REAL state (not just the ref)
  // so the sidebar re-renders — and its "Working…" spinners update — whenever ANY
  // chat starts or finishes, regardless of which chat is currently being viewed.
  // (Background streams suppress paint() re-renders, so a ref alone leaves spinners
  // stale for chats you aren't looking at.)
  const [streamingIds, setStreamingIds] = useState<Set<string>>(() => new Set());
  const markStreaming = (chatId: string, on: boolean) => {
    setStreamingIds((prev) => {
      if (on === prev.has(chatId)) return prev; // no change
      const next = new Set(prev);
      if (on) next.add(chatId);
      else next.delete(chatId);
      return next;
    });
  };
  const [, setTick] = useState(0);
  const rerender = () => setTick((t) => t + 1);
  // Coalesce high-frequency repaints (one per streamed token) into at most one per
  // animation frame. Re-rendering on every token forces a full markdown re-parse and
  // is the main cause of streaming jank; rAF batching keeps it smooth at ~60fps.
  const rafRef = useRef<number | null>(null);
  const scheduleRerender = () => {
    if (rafRef.current != null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      setTick((t) => t + 1);
    });
  };
  useEffect(
    () => () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    },
    [],
  );

  const { data: chats = [] } = useQuery({ queryKey: ["chats"], queryFn: api.listChats });
  // Architecture memories, used to offer a memory picker when launching a deep
  // investigation on a chat whose workload has one or more documented architectures.
  const { data: allMemories } = useQuery({
    queryKey: ["architectureMemories"],
    queryFn: api.architectureMemories,
  });
  // Trash (soft-deleted chats). Only fetched while the Trash panel is open.
  const [trashOpen, setTrashOpen] = useState(false);
  const { data: trashedChats = [] } = useQuery({
    queryKey: ["trashChats"],
    queryFn: api.trashChats,
    enabled: trashOpen,
  });
  const { data: activeLlm } = useQuery({
    queryKey: ["activeLlm"],
    queryFn: api.activeLlm,
    refetchOnWindowFocus: true,
    staleTime: 10_000,
  });
  // Server source of truth for which chats are currently running a turn. Polled so the
  // sidebar shows live "Working…" spinners in EVERY tab/window — including ones that
  // didn't start the turn (e.g. a second browser window watching new chats appear).
  const { data: serverActive } = useQuery({
    queryKey: ["activeTurns"],
    queryFn: api.activeTurns,
    refetchInterval: 4000,
    refetchIntervalInBackground: false,
    staleTime: 2000,
  });
  // When the set of server-active turns changes, refresh the chat list so OTHER tabs/
  // windows surface newly-created chats (and pick up final titles when turns finish).
  // Keyed on the sorted id list so it only fires on an actual membership change.
  const serverActiveKey = (serverActive?.active ?? []).slice().sort().join(",");
  const prevActiveKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (prevActiveKeyRef.current === serverActiveKey) return;
    prevActiveKeyRef.current = serverActiveKey;
    qc.invalidateQueries({ queryKey: ["chats"] });
  }, [serverActiveKey, qc]);
  // Azure tenants (connections) available for the composer's tenant selector.
  const { data: tenantData } = useQuery({
    queryKey: ["azureConnections"],
    queryFn: api.azureConnections,
    staleTime: 30_000,
  });
  const tenants = tenantData?.connections ?? [];
  // Custom agents available for the composer's agent selector (persona + tools + model).
  const { data: agentsData } = useQuery({
    queryKey: ["customAgents"],
    queryFn: api.customAgents,
    staleTime: 30_000,
  });
  const customAgents = agentsData?.agents ?? [];
  // Only enabled agents are surfaced in the sidebar quick-launch menu and the composer
  // agent picker (disabled agents are kept but hidden).
  const enabledAgents = customAgents.filter((a) => a.enabled !== false);
  // Identity — used to gate the Admin Dashboard link in the sidebar footer. Read from the
  // shared AuthContext (single source of truth) so an active-role switch re-renders the menu
  // immediately, with no manual page refresh.
  const { user: me } = useAuth();
  // Progress-feed verbosity, configured in the dashboard (compact/normal/detailed).
  const { data: appSettings } = useQuery({
    queryKey: ["appSettings"],
    queryFn: api.appSettings,
    staleTime: 60_000,
  });
  const progressLevel: ProgressLevel =
    appSettings?.settings.progress_detail ?? "detailed";

  // Live stream entry for the chat currently being viewed (if any).
  const live = activeId ? streamsRef.current.get(activeId) : undefined;
  const streaming = !!live?.streaming;
  const streamText = live?.streamText ?? "";
  const errorDetail = live?.error ?? "";
  const liveLog = live?.log ?? [];
  const liveStartedAt = live?.startedAt ?? Date.now();

  function setActive(id: string | null) {
    viewRef.current = id;
    setActiveId(id);
  }

  function persistActivities(next: Record<string, Step[]>) {
    const capped = capRecord(next, MAX_PERSISTED_ENTRIES);
    setActivities(capped);
    try {
      localStorage.setItem(ACTIVITIES_KEY, JSON.stringify(capped));
    } catch {
      /* ignore quota errors */
    }
  }

  function persistProgress(next: Record<string, LogLine[]>) {
    const capped = capRecord(next, MAX_PERSISTED_ENTRIES);
    setProgressLogs(capped);
    try {
      localStorage.setItem(PROGRESS_KEY, JSON.stringify(capped));
    } catch {
      /* ignore quota errors */
    }
  }

  function persistInvestigations(next: Record<string, Investigation>) {
    const capped = capRecord(next, MAX_PERSISTED_ENTRIES);
    setInvestigations(capped);
    try {
      localStorage.setItem(INVESTIGATIONS_KEY, JSON.stringify(capped));
    } catch {
      /* ignore quota errors */
    }
  }

  async function selectChat(id: string) {
    // URL is the source of truth; navigating triggers the sync effect below.
    navigate(`/c/${id}`);
  }

  // Load a chat's messages into view. Returns false if the chat no longer exists.
  async function openChat(id: string): Promise<boolean> {
    // Preserve composer drafts per chat: stash the chat we're leaving, restore the one
    // we're entering. Without this the single global `input` would leak a half-typed
    // message into whatever chat the user switches to.
    const leaving = viewRef.current;
    if (leaving !== id) {
      draftsRef.current.set(leaving ?? "", inputRef.current);
      setInputSynced(draftsRef.current.get(id) ?? "");
    }
    setActive(id);
    setSuggestions([]);
    setPendingClarify(null);
    setPendingPropose(null);
    setPendingDeepAuth(null);
    // Close any Deep Investigation panel left open from the previously viewed chat;
    // the live stream handler re-opens it if this chat has a running investigation.
    setOpenInvestigation(null);
    atBottomRef.current = true;
    setShowNewMessages(false);
    try {
      const msgs = await api.listMessages(id);
      // Only apply if the user hasn't navigated away while loading.
      if (viewRef.current !== id) return true;
      setMessages(msgs);
    } catch {
      // Chat not found (deleted or bad URL) — fall back to a new chat.
      return false;
    }
    // If this chat already has a live stream in memory, keep it. Otherwise the turn
    // may still be running on the server (we navigated away/reloaded) — reconnect to
    // its live event stream so we resume showing progress.
    if (streamsRef.current.get(id)?.streaming) {
      rerender();
    } else {
      void reconnectIfActive(id);
      void loadSuggestions(id);
    }
    return true;
  }

  // If a turn is running on the server for this chat (e.g. after navigating away or
  // reloading), attach to its live SSE stream and resume rendering progress.
  async function reconnectIfActive(chatId: string) {
    if (streamsRef.current.get(chatId)?.streaming) return;
    const active = await isTurnActive(chatId);
    if (!active || viewRef.current !== chatId) return;
    if (streamsRef.current.get(chatId)?.streaming) return;

    const controller = new AbortController();
    const stream: LiveStream = {
      streaming: true,
      started: false,
      streamText: "",
      steps: [],
      log: [{ kind: "info", text: "Reconnecting to the running task…", ts: Date.now() }],
      startedAt: Date.now(),
      // The user message is already persisted; the transcript shows it.
      userMessage: {
        id: crypto.randomUUID(),
        role: "user",
        content: "",
        created_at: new Date().toISOString(),
      },
      abort: controller,
    };
    streamsRef.current.set(chatId, stream);
    markStreaming(chatId, true);
    rerender();
    await consumeStream(chatId, stream, controller, (handlers, signal) =>
      reconnectStream(chatId, handlers, signal),
    );
  }

  function startNewChat() {
    // Clear the viewed transcript synchronously BEFORE navigating. The URL-sync effect
    // only runs after the route commits, so without this the previous chat's messages
    // and its live/persisted status feed flash at /chat for a frame — especially after
    // interrupting a turn, where the just-stopped chat's progress lingers in the new
    // chat. Resetting here (which also sets viewRef to null) makes the effect's guard
    // skip a redundant second reset.
    resetToNewChat();
    navigate("/chat");
  }

  // Reset to the "new chat" empty state (used when the URL has no chat id).
  function resetToNewChat() {
    // Stash the leaving chat's draft and restore the new-chat ("") draft slot, so a
    // half-typed message doesn't leak from the previous chat into the new-chat view.
    const leaving = viewRef.current;
    if (leaving !== null) {
      draftsRef.current.set(leaving, inputRef.current);
      setInputSynced(draftsRef.current.get("") ?? "");
    }
    setActive(null);
    setMessages([]);
    setSuggestions([]);
    setPendingClarify(null);
    setPendingPropose(null);
    setPendingDeepAuth(null);
    setOpenInvestigation(null);
    atBottomRef.current = true;
    setShowNewMessages(false);
  }

  // Keep the viewed chat in sync with the URL (/c/:id). Handles deep links,
  // browser back/forward, and deleted/invalid ids (which redirect to "/").
  useEffect(() => {
    const id = routeChatId ?? null;
    // Already viewing this chat (e.g. send() set it before navigating) — skip reload
    // so we don't clobber optimistic/streaming messages.
    if (id === viewRef.current) return;
    if (!id) {
      resetToNewChat();
      return;
    }
    void (async () => {
      const ok = await openChat(id);
      if (!ok && viewRef.current === id) navigate("/chat", { replace: true });
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeChatId]);

  async function loadSuggestions(id: string) {
    try {
      const res = await api.suggestions(id);
      if (viewRef.current !== id) return;
      setSuggestions(res.suggestions ?? []);
    } catch {
      if (viewRef.current === id) setSuggestions([]);
    }
  }

  // Keep the latest content in view ONLY when the user is pinned to the bottom.
  // If they've scrolled up to read, new streamed content must not yank the viewport;
  // instead we show a "New messages" pill (handled below). Depend on message count
  // and streamed text length so it fires when content actually changes.
  useEffect(() => {
    if (atBottomRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: streaming ? "auto" : "smooth" });
      setShowNewMessages(false);
    } else if (streaming || messages.length) {
      // Content arrived while scrolled up — invite the user to jump down.
      setShowNewMessages(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages.length, streamText.length, streaming, pendingClarify, pendingPropose]);

  // Track whether the message list is scrolled to (near) the bottom.
  function handleMessagesScroll() {
    const el = scrollContainerRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const atBottom = distance < 80; // px tolerance
    atBottomRef.current = atBottom;
    if (atBottom && showNewMessages) setShowNewMessages(false);
  }

  function scrollToBottom() {
    atBottomRef.current = true;
    setShowNewMessages(false);
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }

  // Auto-grow the composer: reset to one line, then expand to fit content up to a
  // max height (after which it scrolls). Runs on every input change.
  useEffect(() => {
    const el = composerRef.current;
    if (!el) return;
    el.style.height = "auto";
    const maxHeight = 200; // ~8 lines, then scroll internally
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
    el.style.overflowY = el.scrollHeight > maxHeight ? "auto" : "hidden";
  }, [input]);

  // Focus the message composer whenever the window/chat gains focus or the viewed chat
  // changes, so the user can start typing immediately. Skip while a tool/composer flow
  // would be disrupted by mobile keyboards isn't a concern here (desktop app).
  useEffect(() => {
    const focusComposer = () => {
      const el = composerRef.current;
      if (el && document.activeElement?.tagName !== "INPUT") el.focus();
    };
    // On mount, on chat switch, and when the tab/window regains focus.
    const id = window.setTimeout(focusComposer, 0);
    window.addEventListener("focus", focusComposer);
    return () => {
      window.clearTimeout(id);
      window.removeEventListener("focus", focusComposer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  async function send(text?: string) {
    // An explicit `text` (a suggestion bubble or starter prompt) is its own message and
    // must NOT consume the user's in-progress composer draft. Only a bare send() (the
    // composer's Send button / Enter) sends the draft and therefore clears it.
    const usingDraft = text === undefined;
    const content = (text ?? input).trim();
    // Block only if THIS chat is already streaming; other chats may stream too.
    if (!content && attachments.length === 0) return;
    if (activeId && streamsRef.current.get(activeId)?.streaming) return;

    // Sending pins the view to the bottom so the user follows their own message.
    atBottomRef.current = true;
    setShowNewMessages(false);

    // Capture attachments for this turn (survives the clarify round-trip). A suggestion/
    // starter doesn't carry the draft's attachments, so leave them on the composer.
    pendingImagesRef.current = usingDraft ? attachments : [];
    const hasImages = pendingImagesRef.current.length > 0;
    if (usingDraft) setAttachments([]);
    const effectiveContent = content || "Please analyze the attached image(s).";
    // Whether this is the opening message of the chat (drives "propose problems").
    const isFirstMessage = messages.length === 0;

    // Ensure a chat exists so clarify can enumerate subscriptions.
    let chatId = activeId;
    if (!chatId) {
      const chat = await api.createChat();
      // Seed the sidebar with this chat already titled from the first message, so it
      // never flashes "New Chat". Also carry the currently-selected agent and thinking
      // level onto the optimistic chat so the picker doesn't flash back to "Default
      // agent" while the turn streams (the sync effect reads chat.agent_id; a brand-new
      // chat has none until the turn persists it server-side).
      qc.setQueryData<Chat[]>(["chats"], (prev) => [
        {
          ...chat,
          title: deriveTitle(effectiveContent),
          agent_id: selectedAgentId,
          thinking_level: thinkingLevel,
        },
        ...(prev ?? []),
      ]);
      chatId = chat.id;
      // Keep the sync effect from clobbering the composer's agent/thinking selection
      // when activeChat flips from null to this new chat.
      syncedChatRef.current = chatId;
      // Set active BEFORE navigating so the URL-sync effect skips a redundant reload
      // (which would otherwise wipe the optimistic message we're about to add).
      setActive(chatId);
      navigate(`/c/${chatId}`, { replace: true });
    }

    // Optimistically title the chat in the sidebar from the first user message so it
    // stops showing "New Chat" immediately (the server auto-title refetch arrives a
    // few seconds later and replaces this with a polished summary).
    qc.setQueryData<Chat[]>(["chats"], (prev) =>
      prev?.map((c) =>
        c.id === chatId && (!c.title || c.title === "New Chat")
          ? { ...c, title: deriveTitle(effectiveContent) }
          : c,
      ),
    );

    // Only clear the composer when we actually sent its draft (a bare send()). A
    // suggestion/starter leaves the user's typed-but-unsent text intact.
    if (usingDraft) {
      setInputSynced("");
      // Drop any stashed draft for this chat (and the new-chat slot, since a brand-new
      // chat's draft just became this chat's first message) so it can't reappear.
      draftsRef.current.delete(chatId);
      draftsRef.current.delete("");
    }

    // Deep investigation: gate behind an authorization card (Continue/Cancel) before
    // running the long, multi-phase investigation. The first-time notice is shown when
    // the user enables deep mode from the dropdown, not here.
    if (thinkingLevel === "deep" && !hasImages) {
      setPendingDeepAuth({ chatId, content: effectiveContent, hasImages });
      // Load the specialist-agent suggestions (AI pre-selects the relevant ones) so the
      // launch popup can show a "war room roster" picker.
      setDeepAgentOptions(null);
      setDeepAgentSel(new Set());
      setAllHandsActive(false);
      setDeepMemorySel("");
      void (async () => {
        try {
          const res = await api.deepSuggestAgents(chatId, effectiveContent);
          if (viewRef.current !== chatId) return;
          setDeepAgentOptions(res.agents);
          setDeepAgentSel(new Set(res.agents.filter((a) => a.recommended).map((a) => a.id)));
        } catch {
          if (viewRef.current === chatId) setDeepAgentOptions([]);
        }
      })();
      return;
    }

    // On the first message of a new chat, optionally propose sharper problem
    // statements from the catalog for the user to pick from. The server returns an
    // empty list (no LLM call) when the feature is disabled, so this is safe to call.
    if (isFirstMessage && !hasImages) {
      setPendingPropose({ chatId, content: effectiveContent, suggestions: [], checking: true });
      try {
        const res = await api.proposeProblems(chatId, effectiveContent, PROBLEM_CATALOG);
        if (viewRef.current !== chatId) {
          setPendingPropose(null);
          return;
        }
        if (res.suggestions.length > 0) {
          setPendingPropose({ chatId, content: effectiveContent, suggestions: res.suggestions });
          return; // wait for the user's selection
        }
      } catch {
        /* proposal failed — just proceed with the original question */
      }
      setPendingPropose(null);
    }

    await clarifyAndSend(chatId, effectiveContent, hasImages);
  }

  // Pre-flight scope check (subscription / management group) then run the turn. Shared
  // by the normal send path and the "propose problems" pick.
  async function clarifyAndSend(
    chatId: string,
    content: string,
    hasImages: boolean,
    thinking: "normal" | "deep" = "normal",
    deepAgents?: string[],
    regenerate?: boolean,
    deepMemoryArchId?: string,
  ) {
    // Always send the resolved thinking level explicitly. Sending an empty scope for
    // "normal" would let the backend fall back to the chat's SAVED level — which is
    // "deep" here — so picking "standard answer" would still run a deep investigation.
    const scopeExtra: MessageScope = { thinking_level: thinking };
    // Dispatch the user-chosen specialist agents for the war room (deep runs only).
    if (thinking === "deep" && deepAgents && deepAgents.length > 0) {
      scopeExtra.deep_agents = deepAgents;
    }
    // Architecture memory selection for a deep investigation: a specific id pins that
    // architecture's memory; "none" suppresses it; "" (auto) lets the backend resolve
    // from the workload's sole documented architecture.
    if (thinking === "deep" && deepMemoryArchId) {
      scopeExtra.architecture_memory_id = deepMemoryArchId === "none" ? "__none__" : deepMemoryArchId;
    }
    // Preserve the regenerate intent so the backend trims trailing assistant turns
    // instead of appending a duplicate user message + reply.
    if (regenerate) {
      scopeExtra.regenerate = true;
    }
    // When an Azure Workload is selected for this chat, it already defines the scope —
    // so skip the subscription/management-group clarification prompt entirely and send.
    if (selectedWorkloadId) {
      setPendingClarify(null);
      await runSend(chatId, content, scopeExtra);
      return;
    }
    setPendingClarify({ content, options: [], checking: true });
    try {
      const res = await api.clarify(chatId, content);
      if (viewRef.current !== chatId) {
        setPendingClarify(null);
        return;
      }
      if (!hasImages && res.needs_subscription && res.options.length > 0) {
        setPendingClarify({ content, options: res.options, thinking });
        return; // wait for the user's selection
      }
      if (!hasImages && res.needs_management_group && res.mg_options.length > 0) {
        setPendingClarify({
          content,
          options: res.options,
          mgOptions: res.mg_options,
          thinking,
        });
        return; // wait for the user's selection
      }
    } catch {
      /* clarify failed — just proceed without scoping */
    }
    setPendingClarify(null);
    await runSend(chatId, content, scopeExtra);
  }

  // Open the deep-investigation authorization card (Continue/Cancel + agent picker)
  // for a re-run path (regenerate/resend). Mirrors the gate inside send() so a user
  // re-launching a deep chat still gets the agent picker + a fresh war room roster,
  // instead of silently re-running with an empty focus list (no war room).
  function openDeepGateForRerun(chatId: string, content: string, hasImages: boolean) {
    setPendingDeepAuth({ chatId, content, hasImages, regenerate: true });
    setDeepAgentOptions(null);
    setDeepAgentSel(new Set());
    setAllHandsActive(false);
    setDeepMemorySel("");
    void (async () => {
      try {
        const res = await api.deepSuggestAgents(chatId, content);
        if (viewRef.current !== chatId) return;
        setDeepAgentOptions(res.agents);
        setDeepAgentSel(new Set(res.agents.filter((a) => a.recommended).map((a) => a.id)));
      } catch {
        if (viewRef.current === chatId) setDeepAgentOptions([]);
      }
    })();
  }

  // Optimistically bump a chat to the top of "Recents" the instant the user
  // sends/retries — instead of waiting for the AI response to finish and refetch.
  // Respects pinned-first ordering (pinned chats stay above unpinned ones).
  function bumpChatToTop(chatId: string) {
    qc.setQueryData<Chat[]>(["chats"], (prev) => {
      if (!prev) return prev;
      const idx = prev.findIndex((c) => c.id === chatId);
      if (idx <= 0) return prev; // already at front (or not present) — nothing to do
      const chat = prev[idx];
      const rest = prev.filter((c) => c.id !== chatId);
      if (chat.pinned) return [chat, ...rest];
      // Unpinned: place at the front of the unpinned group (after any pinned chats).
      const firstUnpinned = rest.findIndex((c) => !c.pinned);
      const at = firstUnpinned < 0 ? rest.length : firstUnpinned;
      return [...rest.slice(0, at), chat, ...rest.slice(at)];
    });
  }

  async function runSend(
    chatId: string,
    content: string,
    scope?: MessageScope,
    modelHint?: { provider?: string; model?: string },
  ) {
    if (streamsRef.current.get(chatId)?.streaming) return;
    const streamChatId = chatId;
    const isRegenerate = !!scope?.regenerate;
    // Consume any images captured at send time for this turn.
    const images = pendingImagesRef.current;
    pendingImagesRef.current = [];

    const userMsg: Message = {
      id: crypto.randomUUID(),
      role: "user",
      content,
      created_at: new Date().toISOString(),
      images: images.length ? images : undefined,
    };

    const controller = new AbortController();
    // Create the per-chat live stream entry. All handlers mutate this object so
    // progress is retained across navigation.
    // Prefer an explicit modelHint (from "Retry with a different model") — the chats
    // query that backs pickerProvider/pickerModel hasn't refetched yet right after a
    // model switch, so reading it here would show the OLD model in the status line.
    const labelProvider = modelHint?.provider ?? pickerProvider;
    const labelModel = modelHint?.model ?? pickerModel;
    const modelLabel = labelModel
      ? `${PROVIDER_LABELS[labelProvider ?? ""] ?? labelProvider ?? ""} · ${labelModel}`
      : "the model";
    const stream: LiveStream = {
      streaming: true,
      started: false,
      streamText: "",
      steps: [],
      log: [
        {
          kind: "info",
          text: isRegenerate
            ? `Regenerating with ${modelLabel}…`
            : `Sending your request to ${modelLabel}…`,
          ts: Date.now(),
          pending: true,
        },
      ],
      startedAt: Date.now(),
      userMessage: userMsg,
      abort: controller,
    };
    // Deep investigation: seed the live tree and auto-open the investigation panel.
    const isDeep =
      scope?.thinking_level === "deep" ||
      (!scope?.thinking_level && activeChat?.thinking_level === "deep");
    if (isDeep) {
      stream.investigation = { phases: [], hypotheses: [], conclusion: null };
      // Animated icon shown at the top while the investigation runs.
      stream.deepIcon = DEEP_INVESTIGATION_ICON;
      setOpenInvestigation("__live__");
      // Auto-pin the side panel so it stays/reopens for the whole investigation and
      // when returning to this chat later (user can unpin via the panel's pin button).
      setInvestigationPinned(true);
      try {
        localStorage.setItem(INVESTIGATION_PINNED_KEY, "1");
      } catch {
        /* ignore */
      }
    }
    streamsRef.current.set(streamChatId, stream);
    markStreaming(streamChatId, true);

    // On regenerate, the user message already exists in the transcript — drop the
    // old assistant reply locally so the new one streams in its place.
    if (isRegenerate) {
      setMessages((m) => {
        const copy = [...m];
        while (copy.length && copy[copy.length - 1].role !== "user") copy.pop();
        return copy;
      });
    } else {
      setMessages((m) => [...m, userMsg]);
    }
    // Move this chat to the top of Recents now (on send/retry), not after the reply.
    bumpChatToTop(streamChatId);
    setInputSynced("");
    setSuggestions([]);
    rerender();

    await consumeStream(streamChatId, stream, controller, (handlers, signal) =>
      streamMessage(
        streamChatId,
        content,
        handlers,
        {
          // Bind every turn to the chat's selected tenant (unless the scope already
          // specifies one) so the agent runs against the right Azure connection.
          ...(activeTenant
            ? { connection_id: activeTenant.id, tenant_name: activeTenant.display_name }
            : {}),
          // Run as the selected custom agent ("" clears it back to the default
          // assistant). Scope may override, but no caller currently sets agent_id.
          agent_id: selectedAgentId ?? "",
          // Scope to the selected Azure Workload ("" clears any saved workload).
          workload_id: selectedWorkloadId ?? "",
          ...(scope ?? {}),
          images: images.length ? images : undefined,
        },
        signal,
      ),
    );
  }

  // Wire a LiveStream's handlers to an SSE source (initial POST or reconnect) and
  // handle post-completion reload. Shared by runSend and reconnect-on-open.
  async function consumeStream(
    streamChatId: string,
    stream: LiveStream,
    controller: AbortController,
    runner: (handlers: StreamHandlers, signal: AbortSignal) => Promise<void>,
  ) {
    const isCurrent = () => viewRef.current === streamChatId;
    let liveBuf = "";
    let savedId = "";
    let firstEvent = true;
    let writingLogged = false;
    const localSteps: Step[] = [...stream.steps];

    const paint = (sync = false) => {
      if (!isCurrent()) return;
      if (sync) flushSync(rerender);
      else scheduleRerender();
    };

    // Append a line to the live progress feed. Transient "thinking" placeholders never
    // accumulate: any prior one is dropped before adding a new line, so while working
    // only the latest thinking phrase is shown.
    const log = (line: Omit<LogLine, "ts">) => {
      for (let i = stream.log.length - 1; i >= 0; i--) {
        if (stream.log[i].thinking) stream.log.splice(i, 1);
      }
      stream.log.forEach((l) => (l.pending = false));
      stream.log.push({ ...line, ts: Date.now() });
    };
    // Mark the first activity so the "Sending…" line resolves to a response.
    const markResponding = () => {
      if (firstEvent) {
        firstEvent = false;
        log({ kind: "info", text: randomThinkingPhrase(), pending: true, thinking: true });
      }
    };

    await runner(
      {
        // Pre-token connection milestones (loading tools, connecting to the model,
        // request sent, response received). Each replaces the prior pending line and is
        // live-only (thinking:true ⇒ cleared from the persisted feed when the turn ends).
        onStatus: (d) => {
          firstEvent = false;
          log({ kind: "info", text: d.message, pending: true, thinking: true });
          paint();
        },
        onToken: (t) => {
          markResponding();
          liveBuf += t;          stream.started = true;
          stream.streamText += t;
          if (!writingLogged && stream.streamText.trim()) {
            writingLogged = true;
            log({ kind: "info", text: randomThinkingPhrase(), pending: true, thinking: true });
          }
          paint();
        },
        onToolStart: (d) => {
          markResponding();
          if (liveBuf.trim()) {
            localSteps.push({ kind: "reasoning", text: liveBuf.trim() });
            // Surface the model's thinking (what it understood + its plan) in the
            // progress feed so it persists at the point it occurred, instead of
            // disappearing when the streamed answer is reset for the tool call.
            log({ kind: "reason", text: liveBuf.trim() });
          }
          liveBuf = "";
          // The answer-so-far was just reasoning leading into a tool call; reset it.
          stream.streamText = "";
          writingLogged = false;
          localSteps.push({
            kind: "tool",
            name: d.tool_name,
            args: d.arguments,
            status: "running",
          });
          log({
            kind: "tool",
            text: d.tool_name,
            pending: true,
            detail: argsPreview(d.arguments),
          });
          // War room: attribute this live tool call to its specialist agent.
          if (d.agent && stream.investigation) {
            const act = (stream.investigation.agentActivity ??= {});
            const a = (act[d.agent] ??= { tools: 0 });
            a.tool = d.tool_name;
            a.busy = true;
            a.startedAt = a.startedAt ?? Date.now();
          }
          stream.started = true;
          stream.steps = [...localSteps];
          paint(true);
        },
        onToolResult: (d) => {
          const isErr = !!d.is_error;
          for (let i = localSteps.length - 1; i >= 0; i--) {
            const s = localSteps[i];
            if (s.kind === "tool" && s.status === "running") {
              localSteps[i] = {
                ...s,
                status: isErr ? "error" : "done",
                summary: d.summary,
                duration: d.duration_ms,
              };
              break;
            }
          }
          log({
            kind: "result",
            text: isErr ? `⚠ ${d.tool_name} failed — ${d.summary || "error"}` : (d.summary || "Done"),
            detail: formatDuration(d.duration_ms),
          });
          // War room: tally the agent's completed tool call.
          if (d.agent && stream.investigation) {
            const act = (stream.investigation.agentActivity ??= {});
            const a = (act[d.agent] ??= { tools: 0 });
            a.tools = (a.tools ?? 0) + 1;
            a.busy = false;
          }
          stream.steps = [...localSteps];
          paint(true);
        },
        onApprovalRequired: (d) => {
          markResponding();
          localSteps.push({
            kind: "tool",
            name: d.tool_name,
            args: d.arguments,
            status: "done",
            summary: "Needs admin approval (write action)",
          });
          log({ kind: "result", text: `${d.tool_name} needs admin approval (write action)` });
          stream.steps = [...localSteps];
          paint(true);
        },
        onPhase: (d) => {
          markResponding();
          const inv = (stream.investigation ??= { phases: [], hypotheses: [], conclusion: null });
          // Replace a same-phase placeholder (no summary) when its summary arrives.
          const phases = inv.phases ?? (inv.phases = []);
          const existing = phases.find((p) => p.phase === d.phase);
          if (existing) existing.summary = d.summary ?? existing.summary;
          else phases.push({ phase: d.phase, label: d.label, summary: d.summary });
          log({ kind: "info", text: `Deep investigation — ${d.label}`, pending: true, thinking: true });
          paint(true);
        },
        onHypothesis: (d) => {
          const inv = (stream.investigation ??= { phases: [], hypotheses: [], conclusion: null });
          (inv.hypotheses ??= []).push(d);
          paint(true);
        },
        onHypothesisStatus: (d) => {
          const inv = stream.investigation;
          const node = inv?.hypotheses?.find((h) => h.id === d.id);
          if (node) {
            node.status = d.status as HypothesisNode["status"];
            node.evidence = d.evidence;
          }
          paint(true);
        },
        onConclusion: (d: InvestigationConclusion) => {
          const inv = (stream.investigation ??= { phases: [], hypotheses: [], conclusion: null });
          inv.conclusion = d;
          paint(true);
        },
        onAgents: (d) => {
          const inv = (stream.investigation ??= { phases: [], hypotheses: [], conclusion: null });
          inv.agents = d.agents;
          paint(true);
        },
        onSaved: (d) => {
          savedId = d.id;
        },
        onDone: () => {
          // The turn is finished — remove the transient "thinking" placeholders so the
          // feed shows only the real work (reasoning, tools, results).
          for (let i = stream.log.length - 1; i >= 0; i--) {
            if (stream.log[i].thinking) stream.log.splice(i, 1);
          }
          stream.log.forEach((l) => (l.pending = false));
        },
        onError: (msg) => {
          stream.error = `Error: ${msg}`;
          // Clear any stuck spinner (pending tool/phase) so a broken turn doesn't keep
          // showing a spinner in the persisted feed.
          stream.log.forEach((l) => (l.pending = false));
          log({ kind: "result", text: `Error: ${msg}` });
          paint();
        },
      },
      controller.signal,
    );

    // Turn finished. Persist activity, drop the live entry, and refresh transcript.
    if (savedId && localSteps.length > 0) {
      persistActivities({ ...activities, [savedId]: localSteps });
    }
    // Persist the full progress feed so the detailed work survives after completion.
    if (savedId && stream.log.length > 0) {
      const frozen = stream.log
        .filter((l) => !l.thinking)
        .map((l) => ({ ...l, pending: false }));
      persistProgress({ ...progressLogs, [savedId]: frozen });
    }
    // Persist the deep-investigation tree so its panel survives reload.
    if (savedId && stream.investigation && (stream.investigation.hypotheses?.length || stream.investigation.conclusion)) {
      persistInvestigations({ ...investigations, [savedId]: stream.investigation });
      // Re-point an open live panel at the now-saved message so it stays open.
      setOpenInvestigation((cur) => (cur === "__live__" ? savedId : cur));
    }
    // Only clear if this is still the active run (a reconnect that lost the race
    // shouldn't wipe a newer run).
    const superseded = streamsRef.current.get(streamChatId) !== stream && streamsRef.current.has(streamChatId);
    if (streamsRef.current.get(streamChatId) === stream) {
      streamsRef.current.delete(streamChatId);
      markStreaming(streamChatId, false);
    }
    qc.invalidateQueries({ queryKey: ["chats"] });

    if (!isCurrent()) {
      rerender();
      return;
    }
    // If a NEWER run has already started for this chat (e.g. the user clicked Stop and
    // immediately sent a new message), do NOT reload the transcript from the server —
    // that stale snapshot wouldn't include the new run's just-sent message and would
    // clobber its optimistic state. Let the newer run own the message list.
    if (superseded) {
      rerender();
      return;
    }
    const refreshed = await api.listMessages(streamChatId);
    if (viewRef.current !== streamChatId || streamsRef.current.get(streamChatId) !== undefined) {
      // View changed, or a new run started during the await — don't overwrite its state.
      rerender();
      return;
    }
    setMessages(refreshed);
    rerender();
    void loadSuggestions(streamChatId);
  }

  // Stop the in-flight response for the active chat. The turn runs in a background
  // task on the server (decoupled from the SSE connection), so we must tell the server
  // to cancel it — aborting only the local fetch would leave it running and the UI
  // would just reconnect to it. The backend persists partial output at checkpoints, so
  // the answer-so-far is kept and the user can continue by sending another message.
  function stopStreaming() {
    if (!activeId) return;
    const chatId = activeId;
    // Ask the server to cancel the background turn first, then drop the local stream.
    void api.stopTurn(chatId).catch(() => {});
    streamsRef.current.get(chatId)?.abort?.abort();
    streamsRef.current.delete(chatId);
    markStreaming(chatId, false);
    // Refresh the transcript so the persisted partial answer shows, and clear the
    // server-active spinner state quickly.
    qc.invalidateQueries({ queryKey: ["activeTurns"] });
    void api.listMessages(chatId).then((msgs) => {
      if (viewRef.current === chatId) setMessages(msgs);
      rerender();
    });
    rerender();
  }

  // Read image files into base64 data URLs and queue them as attachments.
  async function addImageFiles(files: FileList | File[]) {
    const list = Array.from(files).filter((f) => f.type.startsWith("image/"));
    const urls = await Promise.all(
      list.map(
        (f) =>
          new Promise<string>((resolve) => {
            const r = new FileReader();
            r.onload = () => resolve(String(r.result));
            r.readAsDataURL(f);
          }),
      ),
    );
    if (urls.length) setAttachments((a) => [...a, ...urls].slice(0, 6));
  }

  function onComposerPaste(e: React.ClipboardEvent) {
    const files = Array.from(e.clipboardData.files);
    const images = files.filter((f) => f.type.startsWith("image/"));
    if (images.length) {
      e.preventDefault();
      void addImageFiles(images);
    }
  }

  async function commitRename(id: string, title: string) {
    setRenamingId(null);
    const trimmed = title.trim();
    if (!trimmed) return;
    try {
      await api.renameChat(id, trimmed);
      qc.invalidateQueries({ queryKey: ["chats"] });
    } catch {
      /* ignore */
    }
  }

  async function deleteOneChat(id: string) {
    // No confirmation: deletion is non-destructive — the chat moves to Trash, where it
    // can be restored or permanently removed.
    try {
      await api.deleteChat(id);
    } catch {
      /* ignore */
    }
    streamsRef.current.delete(id);
    if (activeId === id) startNewChat();
    qc.invalidateQueries({ queryKey: ["chats"] });
    qc.invalidateQueries({ queryKey: ["trashChats"] });
  }

  // Restore a trashed chat back into the active list.
  async function restoreTrashedChat(id: string) {
    try {
      await api.restoreChat(id);
    } catch {
      /* ignore */
    }
    qc.invalidateQueries({ queryKey: ["chats"] });
    qc.invalidateQueries({ queryKey: ["trashChats"] });
  }

  // Permanently delete a single trashed chat.
  async function purgeTrashedChat(id: string) {
    try {
      await api.purgeChat(id);
    } catch {
      /* ignore */
    }
    streamsRef.current.delete(id);
    qc.invalidateQueries({ queryKey: ["trashChats"] });
  }

  // Permanently delete every trashed chat.
  async function emptyTrash() {
    try {
      await api.emptyTrash();
    } catch {
      /* ignore */
    }
    qc.invalidateQueries({ queryKey: ["trashChats"] });
  }

  async function togglePin(id: string, pinned: boolean) {
    try {
      await api.pinChat(id, pinned);
      qc.invalidateQueries({ queryKey: ["chats"] });
    } catch {
      /* ignore */
    }
  }

  // Re-run the most recent user turn (regenerate the assistant's answer), optionally
  // with a different provider/model. Replaces the previous answer in place.
  async function regenerate(provider?: string, model?: string) {
    if (!activeId || streaming) return;
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (!lastUser) return;
    // Switch the chat's model first if a new one was chosen via "Retry with…".
    if (provider && model) {
      try {
        // Per-chat only — a retry with a different model must not change the global default.
        await api.setChatModel(activeId, provider, model);
        qc.invalidateQueries({ queryKey: ["chats"] });
      } catch {
        /* ignore — fall back to current model */
      }
    }
    pendingImagesRef.current = lastUser.images ?? [];
    // Deep chats: route through the same authorization + agent-picker popup as a
    // fresh send so the user can re-pick the war room roster — otherwise focus=[] on
    // the backend and no agents event would ever fire (stale, empty war room).
    if (activeChat?.thinking_level === "deep" && !(lastUser.images?.length)) {
      // Wipe any stale assistant investigation so the war room rebuilds fresh.
      setInvestigations((inv) => {
        const next = { ...inv };
        for (const m of messages) {
          if (m.role === "assistant" && next[m.id]) delete next[m.id];
        }
        return next;
      });
      openDeepGateForRerun(activeId, lastUser.content, false);
      return;
    }
    await runSend(
      activeId,
      lastUser.content,
      { regenerate: true },
      provider && model ? { provider, model } : undefined,
    );
  }

  // Retry an arbitrary assistant message: truncate the conversation back to the user
  // turn that produced it, then re-run from there (works for any answer, not just last).
  async function regenerateFrom(messageId: string, provider?: string, model?: string) {
    if (!activeId || streaming) return;
    const idx = messages.findIndex((m) => m.id === messageId);
    if (idx < 0) return;
    let userIdx = -1;
    for (let i = idx - 1; i >= 0; i--) {
      if (messages[i].role === "user") {
        userIdx = i;
        break;
      }
    }
    if (userIdx < 0) return;
    const user = messages[userIdx];
    if (provider && model) {
      try {
        // Per-chat only — a retry with a different model must not change the global default.
        await api.setChatModel(activeId, provider, model);
        qc.invalidateQueries({ queryKey: ["chats"] });
      } catch {
        /* ignore */
      }
    }
    try {
      await api.deleteMessagesFrom(activeId, messageId);
    } catch {
      /* ignore — the regenerate path also trims trailing assistant turns */
    }
    // Trim local transcript to end at the preceding user message.
    setMessages((prev) => {
      const i = prev.findIndex((m) => m.id === messageId);
      return i >= 0 ? prev.slice(0, i) : prev;
    });
    pendingImagesRef.current = user.images ?? [];
    // Deep chats: open the agent-picker popup so the war room rebuilds with a fresh
    // roster (otherwise focus=[] and no agents are dispatched).
    if (activeChat?.thinking_level === "deep" && !(user.images?.length)) {
      setInvestigations((inv) => {
        const next = { ...inv };
        // Clear all trailing assistant investigations since we just truncated them.
        for (let i = userIdx + 1; i < messages.length; i++) {
          const m = messages[i];
          if (m.role === "assistant" && next[m.id]) delete next[m.id];
        }
        return next;
      });
      openDeepGateForRerun(activeId, user.content, false);
      return;
    }
    await runSend(
      activeId,
      user.content,
      { regenerate: true },
      provider && model ? { provider, model } : undefined,
    );
  }

  // Resend a user message: re-run that turn FRESH. We remove everything that follows it
  // (the previous answer and its persisted progress feed) and stream a new reply in its
  // place — instead of appending a duplicate user message and leaving the stale "Thinking
  // Process" progress from the old turn visible above the new one.
  async function resendFrom(messageId: string) {
    if (!activeId || streaming) return;
    const idx = messages.findIndex((m) => m.id === messageId);
    if (idx < 0) return;
    const user = messages[idx];
    if (user.role !== "user") return;
    // Drop the old answer (and anything after) from the server and local transcript.
    const next = messages[idx + 1];
    if (next) {
      try {
        await api.deleteMessagesFrom(activeId, next.id);
      } catch {
        /* ignore — runSend's regenerate path also trims trailing assistant turns */
      }
      setMessages((prev) => {
        const i = prev.findIndex((m) => m.id === next.id);
        return i >= 0 ? prev.slice(0, i) : prev;
      });
    }
    pendingImagesRef.current = user.images ?? [];
    // Deep chats: re-open the agent-picker so the war room launches fresh with all
    // recommended agents, instead of silently re-running with an empty roster.
    if (activeChat?.thinking_level === "deep" && !(user.images?.length)) {
      setInvestigations((inv) => {
        const cleaned = { ...inv };
        for (let i = idx + 1; i < messages.length; i++) {
          const m = messages[i];
          if (m.role === "assistant" && cleaned[m.id]) delete cleaned[m.id];
        }
        return cleaned;
      });
      openDeepGateForRerun(activeId, user.content, false);
      return;
    }
    await runSend(activeId, user.content, { regenerate: true });
  }

  // Break out (fork) the current chat into a new thread that copies everything up to
  // and including the given message, then switch focus to it.
  async function breakout(upToMessageId: string) {
    if (!activeId) return;
    try {
      const newChat = await api.breakoutChat(activeId, upToMessageId);
      qc.invalidateQueries({ queryKey: ["chats"] });
      navigate(`/c/${newChat.id}`);
    } catch (e) {
      console.error("Breakout failed", e);
    }
  }

  // Download the current chat transcript as a Markdown file.
  function exportChat() {
    if (!displayMessages.length) return;
    const lines = displayMessages.map((m) => {
      const who = m.role === "user" ? "You" : "Assistant";
      return `## ${who}\n\n${m.content}\n`;
    });
    const title = chats.find((c) => c.id === activeId)?.title ?? "chat";
    const blob = new Blob([`# ${title}\n\n${lines.join("\n")}`], {
      type: "text/markdown",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${title.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }

  // While viewing a streaming chat, ensure the optimistic user message is shown
  // even right after navigating back (before the server reload completes). Match by
  // id OR by trailing role+content so the server-persisted copy (which has a
  // different id) doesn't render as a duplicate of the optimistic one.
  // Also drop a trailing assistant message: while streaming it's the in-progress
  // (possibly partial) row the backend persists incrementally — the live pane
  // renders that, so showing the server copy too would duplicate it.
  let baseMessages = messages;
  if (live && baseMessages.length && baseMessages[baseMessages.length - 1].role === "assistant") {
    baseMessages = baseMessages.slice(0, -1);
  }
  const lastMsg = baseMessages[baseMessages.length - 1];
  const optimisticAlreadyShown =
    !!live &&
    (baseMessages.some((m) => m.id === live.userMessage.id) ||
      (lastMsg?.role === "user" && lastMsg.content === live.userMessage.content));
  // When reconnecting to an in-flight turn we don't have the original user content
  // (it's already persisted in `messages`), so we use an empty-content sentinel for
  // `live.userMessage`. Don't render that as a real bubble — it would look like an
  // empty blue message above the "Reconnecting…" status feed.
  const liveUserIsPlaceholder = !!live && !live.userMessage.content;
  const displayMessages =
    live && !optimisticAlreadyShown && !liveUserIsPlaceholder
      ? [...baseMessages, live.userMessage]
      : baseMessages;

  const showWelcome =
    displayMessages.length === 0 && !streamText && !streaming && !pendingClarify && !pendingPropose && !pendingDeepAuth;

  // Model picker reflects the ACTIVE CHAT's saved provider/model (each chat keeps its
  // own), falling back to the global active provider for a new/unsaved chat.
  // Provider and model MUST be resolved as a pair from the same source — never mix a
  // fallback provider with the chat's model (that produces a mismatched pair like
  // "GitHub Copilot · <an OpenRouter model>" and can get persisted via "Try again").
  const activeChat = chats.find((c) => c.id === activeId);
  const chatHasModel = !!(activeChat?.provider && activeChat?.model);
  const pickerProvider = chatHasModel ? activeChat?.provider : activeLlm?.provider;
  const pickerModel = chatHasModel ? activeChat?.model : activeLlm?.model;

  // Keep the composer's thinking-level and agent selectors in sync with the ACTIVE
  // chat's saved values — but only when the user actually SWITCHES chats (the chat id
  // changes), never on field-level churn. During a turn the chats list gets optimistic
  // updates / refetches; depending on the agent_id/thinking_level fields here would
  // momentarily reset the composer (e.g. the agent picker flashing back to "Default"
  // mid-stream, then restoring on completion). A ref tracks the last-synced chat id.
  const syncedChatRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const id = activeChat?.id ?? null;
    if (syncedChatRef.current === id) return; // same chat — don't clobber local edits
    syncedChatRef.current = id;
    setThinkingLevel(activeChat?.thinking_level === "deep" ? "deep" : "normal");
    setSelectedAgentId(activeChat?.agent_id ?? null);
    setSelectedWorkloadId(activeChat?.workload_id ?? null);
  }, [activeChat?.id, activeChat?.thinking_level, activeChat?.agent_id, activeChat?.workload_id]);

  // Handoff from the Architecture Memory screen: "Investigate with this memory" stores a
  // one-shot scope in sessionStorage and navigates to /chat. We preselect deep mode + the
  // workload + the architecture's memory on the new-chat composer; sending the first
  // message creates the chat (the proven path). We pin syncedChatRef so the chat-sync
  // effect won't reset the level back to normal for the empty new-chat view.
  useEffect(() => {
    let raw: string | null = null;
    try { raw = sessionStorage.getItem("azsup.memoryHandoff"); } catch { /* ignore */ }
    if (!raw) return;
    try { sessionStorage.removeItem("azsup.memoryHandoff"); } catch { /* ignore */ }
    let scope: { workloadId?: string; memoryArchId?: string };
    try { scope = JSON.parse(raw); } catch { return; }
    // Apply the deep scope to the current composer. Pin syncedChatRef so the chat-sync
    // effect won't reset the level back to normal.
    syncedChatRef.current = activeChat?.id ?? null;
    setThinkingLevel("deep");
    if (scope.workloadId) setSelectedWorkloadId(scope.workloadId);
    if (scope.memoryArchId) setDeepMemorySel(scope.memoryArchId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key]);

  // Handoff from the Identity dashboard: "Investigate" stores a one-shot scope (optional
  // owning workload + a prefilled prompt) in sessionStorage and navigates to /chat. We
  // preselect the workload (when resolved) and prefill the composer so the user can review
  // and send. Sending the first message creates the chat (the proven path).
  useEffect(() => {
    let raw: string | null = null;
    try { raw = sessionStorage.getItem("azsup.identityHandoff"); } catch { /* ignore */ }
    if (!raw) return;
    try { sessionStorage.removeItem("azsup.identityHandoff"); } catch { /* ignore */ }
    let scope: { workloadId?: string; prompt?: string };
    try { scope = JSON.parse(raw); } catch { return; }
    syncedChatRef.current = activeChat?.id ?? null;
    if (scope.workloadId) setSelectedWorkloadId(scope.workloadId);
    if (scope.prompt) setInputSynced(scope.prompt);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key]);

  // Handoff from Backup & DR Coverage: "Investigate in War Room" stores deep mode +
  // workload + a gap-preloaded prompt; we open the deep (War Room) composer prefilled.
  useEffect(() => {
    let raw: string | null = null;
    try { raw = sessionStorage.getItem("azsup.warRoomHandoff"); } catch { /* ignore */ }
    if (!raw) return;
    try { sessionStorage.removeItem("azsup.warRoomHandoff"); } catch { /* ignore */ }
    let scope: { workloadId?: string; prompt?: string };
    try { scope = JSON.parse(raw); } catch { return; }
    syncedChatRef.current = activeChat?.id ?? null;
    setThinkingLevel("deep");
    if (scope.workloadId) setSelectedWorkloadId(scope.workloadId);
    if (scope.prompt) setInputSynced(scope.prompt);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key]);

  // Surface the Deep Investigation side panel as soon as a new/empty chat is put into
  // deep mode (before any message streams a hypothesis tree). It shows an idle
  // placeholder; sending a message swaps it for the live run. The effect only auto-
  // opens on the empty state, so the panel's close button is respected thereafter.
  useEffect(() => {
    if (streaming) return;
    if (thinkingLevel === "deep" && messages.length === 0) {
      setOpenInvestigation((cur) => (cur == null ? "__deep_idle__" : cur));
    } else {
      setOpenInvestigation((cur) => (cur === "__deep_idle__" ? null : cur));
    }
  }, [thinkingLevel, messages.length, streaming]);

  // When the panel is pinned, auto-reopen it on switching to a chat that has a running
  // or completed deep investigation — so the user doesn't have to click the reopen pill
  // each time. Closing the panel un-pins it (handled in InvestigationPanel.onClose).
  const liveInvestigation = live?.investigation;
  const lastDeepMessage = [...displayMessages]
    .reverse()
    .find((m) => m.role === "assistant" && (investigations[m.id] || m.investigation));
  useEffect(() => {
    if (!investigationPinned) return;
    if (openInvestigation) return; // already open (incl. live/idle) — don't override
    const target = liveInvestigation ? "__live__" : lastDeepMessage?.id;
    if (target) setOpenInvestigation(target);
  }, [
    investigationPinned,
    openInvestigation,
    liveInvestigation,
    lastDeepMessage?.id,
    activeId,
  ]);

  // Launch a fresh chat with a custom agent pre-selected (from the sidebar quick-launch
  // menu, which navigates to "/?agent=<id>"). We open the new-chat welcome screen with
  // the agent picked, then wait for the user's first message. The agent param is then
  // stripped from the URL so a refresh doesn't re-trigger it.
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const agentId = params.get("agent");
    if (!agentId) return;
    // Reset to a clean new chat and pin the chosen agent (guard the sync effect so it
    // doesn't immediately reset the picker back to "Default").
    syncedChatRef.current = null;
    setActive(null);
    setMessages([]);
    setSuggestions([]);
    setSelectedAgentId(agentId);
    navigate("/chat", { replace: true });
    setTimeout(() => composerRef.current?.focus(), 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.search]);

  // Change the active chat's thinking level. First time deep is enabled, show the
  // one-time notice; persist to the chat when one exists.
  function changeThinking(level: "normal" | "deep") {
    if (level === "deep") {
      let confirmed = true;
      try {
        confirmed = localStorage.getItem(DEEP_CONFIRMED_KEY) === "1";
      } catch {
        /* ignore */
      }
      if (!confirmed) {
        setDeepNotice(true);
        return; // wait for the user to accept the notice
      }
    }
    setThinkingLevel(level);
    if (activeId) {
      void api.setChatThinking(activeId, level).catch(() => {});
      qc.setQueryData<Chat[]>(["chats"], (prev) =>
        prev?.map((c) => (c.id === activeId ? { ...c, thinking_level: level } : c)),
      );
    }
  }

  // Change the active chat's custom agent (null = default assistant). Persists to the
  // chat when one exists so it sticks for subsequent messages, like the model picker.
  function changeAgent(agentId: string | null) {
    setSelectedAgentId(agentId);
    if (activeId) {
      void api.setChatAgent(activeId, agentId).catch(() => {});
      qc.setQueryData<Chat[]>(["chats"], (prev) =>
        prev?.map((c) => (c.id === activeId ? { ...c, agent_id: agentId } : c)),
      );
    }
  }

  // The tenant (Azure connection) bound to the active chat — the chat's saved tenant,
  // else the default connection. Drives the composer tenant selector and turn scope.
  const activeTenant =
    tenants.find((t) => t.id === activeChat?.connection_id) ??
    tenants.find((t) => t.is_default) ??
    (tenants.length === 1 ? tenants[0] : undefined);

  // Sidebar: filter by search, then split into pinned / unpinned groups.
  const q = chatSearch.trim().toLowerCase();
  const { searchedChats, filteredChats, pinnedChats, recentChats } = useMemo(() => {
    const searched = q ? chats.filter((c) => c.title.toLowerCase().includes(q)) : chats;
    const filtered = chatFilter === "pinned" ? searched.filter((c) => c.pinned) : searched;
    return {
      searchedChats: searched,
      filteredChats: filtered,
      pinnedChats: filtered.filter((c) => c.pinned),
      recentChats: filtered.filter((c) => !c.pinned),
    };
  }, [chats, chatFilter, q]);
  void searchedChats; // exported for parity; consumed indirectly via filteredChats

  // Spinner source: local in-flight streams (this tab) ∪ server-reported active turns
  // (any tab/window). A short-lived local stream that just finished is excluded by the
  // server set on the next poll, so spinners don't get stuck.
  const serverActiveSet = new Set(serverActive?.active ?? []);

  const renderChatRow = (c: (typeof chats)[number]) => {
    const isStreaming = streamingIds.has(c.id) || serverActiveSet.has(c.id);
    return (
    <div
      key={c.id}
      className={`group relative mb-px flex items-center rounded-lg text-[13px] transition hover:bg-gray-200/60 ${
        c.id === activeId ? "bg-gray-200/80 text-gray-900" : "text-gray-700"
      }`}
    >
      {renamingId === c.id ? (
        <input
          autoFocus
          defaultValue={c.title}
          onBlur={(e) => void commitRename(c.id, e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void commitRename(c.id, e.currentTarget.value);
            if (e.key === "Escape") setRenamingId(null);
          }}
          className="m-1 w-full rounded border px-2 py-1 text-[13px] focus:outline-none focus:ring-1 focus:ring-gray-300"
        />
      ) : (
        <>
          <button
            onClick={() => selectChat(c.id)}
            className="flex min-w-0 flex-1 items-center gap-2 px-2.5 py-1.5 text-left"
          >
            {c.thinking_level === "deep" && (
              <WarRoomIcon className="h-4 w-4 shrink-0" />
            )}
            {c.pinned && <PinIcon className="h-3 w-3 shrink-0 text-gray-400" />}
            <span className="truncate">{c.title}</span>
          </button>
          {isStreaming && (
            <span className="shrink-0 pr-2 group-hover:hidden" title="Working…">
              <Spinner className="h-3.5 w-3.5 text-brand" />
            </span>
          )}
          <div className="hidden shrink-0 items-center gap-0.5 pr-1 group-hover:flex">
            <button
              onClick={() => void togglePin(c.id, !c.pinned)}
              title={c.pinned ? "Unpin" : "Pin"}
              className={`rounded p-1 hover:bg-gray-300/70 ${
                c.pinned ? "text-gray-600" : "text-gray-400 hover:text-gray-700"
              }`}
            >
              <PinIcon className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setRenamingId(c.id)}
              title="Rename"
              className="rounded p-1 text-gray-400 hover:bg-gray-300/70 hover:text-gray-700"
            >
              <PencilIcon className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => void deleteOneChat(c.id)}
              title="Delete"
              className="rounded p-1 text-gray-400 hover:bg-red-100 hover:text-red-600"
            >
              <TrashIcon className="h-3.5 w-3.5" />
            </button>
          </div>
        </>
      )}
    </div>
    );
  };

  const execConfig: ExecConfig = {
    enabled: me?.role === "admin" && !!appSettings?.settings.command_execution_enabled,
    allowlist: appSettings?.settings.command_allowlist ?? ["az"],
    chatId: activeId,
    openEditor: (command, mode) => setEditorState({ command, mode }),
    openMermaidEditor: (code) => setMermaidEdit(code),
  };

  return (
    <ExecContext.Provider value={execConfig}>
    <div className="relative flex h-full">
      {/* Sidebar */}
      <aside
        className={`flex flex-col border-r border-gray-200 bg-gray-50 transition-[width] duration-200 ${
          railCollapsed ? "w-14" : "w-64"
        }`}
      >
        {/* Header: app label + collapse toggle */}
        <div className="flex items-center justify-between px-2 py-2">
          {!railCollapsed && (
            <span className="pl-1.5 text-sm font-semibold text-gray-800">Azure Agent</span>
          )}
          <div className="ml-auto flex items-center gap-1">
            <NotificationBell collapsed={railCollapsed} />
            <button
              onClick={toggleRail}
              title={railCollapsed ? "Expand sidebar" : "Collapse sidebar"}
              className="rounded-lg p-1.5 text-gray-500 transition hover:bg-gray-200/60 hover:text-gray-700"
            >
              <PanelLeftIcon className="h-[18px] w-[18px]" collapsed={railCollapsed} />
            </button>
          </div>
        </div>

        {/* New chat */}
        <button
          onClick={startNewChat}
          title="New chat"
          className={`mx-2 mb-0.5 flex items-center rounded-lg text-sm text-gray-700 transition hover:bg-gray-200/60 ${
            railCollapsed ? "justify-center p-2" : "gap-2.5 px-2.5 py-2"
          }`}
        >
          <ComposeIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
          {!railCollapsed && <span>New chat</span>}
        </button>

        {railCollapsed && (
          // Collapsed rail: a search affordance that expands back to the full list.
          <button
            onClick={toggleRail}
            title="Search chats"
            className="mx-2 mb-1 flex items-center justify-center rounded-lg p-2 text-gray-500 transition hover:bg-gray-200/60 hover:text-gray-700"
          >
            <SearchIcon className="h-[18px] w-[18px]" />
          </button>
        )}

        {/* Scrollable region: the nav menus (Automations, Custom Agents, …) and the
            chat list live in ONE scroll container, so a long expanded Custom Agents
            list never hides Recents/Pinned — the user just scrolls down to reach them. */}
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">

        {/* Dashboard: a getting-started / overview page for newcomers. Pinned at the
            very top of the nav so first-time users always have a home base. All users. */}
        {railCollapsed ? (
          <Link
            to="/dashboard"
            title="Dashboard"
            className={`mx-2 mb-1 flex items-center justify-center rounded-lg p-2 transition ${
              inDashboard
                ? "bg-gray-200 text-gray-900"
                : "text-gray-500 hover:bg-gray-200/60 hover:text-gray-700"
            }`}
          >
            <DashboardIcon className="h-[18px] w-[18px]" />
          </Link>
        ) : (
          <div className="mb-1 px-2">
            <Link
              to="/dashboard"
              className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition ${
                inDashboard
                  ? "bg-gray-200 font-medium text-gray-900"
                  : "text-gray-700 hover:bg-gray-200/60"
              }`}
            >
              <DashboardIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
              <span>Dashboard</span>
            </Link>
          </div>
        )}

        {/* Mission Control: per-workload "run every analysis" cockpit. Top-level, right
            below Dashboard. Admin-only (the missions API is admin-gated). */}
        {me?.role === "admin" &&
          (railCollapsed ? (
            <Link
              to="/mission-control"
              title="Mission Control"
              className={`mx-2 mb-1 flex items-center justify-center rounded-lg p-2 transition ${
                inMissionControl
                  ? "bg-gray-200 text-gray-900"
                  : "text-gray-500 hover:bg-gray-200/60 hover:text-gray-700"
              }`}
            >
              <MissionControlIcon className="h-[18px] w-[18px]" />
            </Link>
          ) : (
            <div className="mb-1 px-2">
              <Link
                to="/mission-control"
                className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition ${
                  inMissionControl
                    ? "bg-gray-200 font-medium text-gray-900"
                    : "text-gray-700 hover:bg-gray-200/60"
                }`}
              >
                <MissionControlIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
                <span>Mission Control</span>
              </Link>
            </div>
          ))}

        {/* Azure Workloads: hand-picked resource scopes. Available to all users. */}
        {railCollapsed ? (
          <Link
            to="/workloads"
            title="Azure Workloads"
            className={`mx-2 mb-1 flex items-center justify-center rounded-lg p-2 transition ${
              inWorkloads
                ? "bg-gray-200 text-gray-900"
                : "text-gray-500 hover:bg-gray-200/60 hover:text-gray-700"
            }`}
          >
            <WorkloadIcon className="h-[18px] w-[18px]" />
          </Link>
        ) : (
          <div className="mb-1 px-2">
            <Link
              to="/workloads"
              className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition ${
                inWorkloads
                  ? "bg-gray-200 font-medium text-gray-900"
                  : "text-gray-700 hover:bg-gray-200/60"
              }`}
            >
              <WorkloadIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
              <span>Azure Workloads</span>
            </Link>
          </div>
        )}

        {/* Ownership: assign accountable owners/teams across the estate. All users. */}
        {railCollapsed ? (
          <Link
            to="/ownership"
            title="Ownership"
            className={`mx-2 mb-1 flex items-center justify-center rounded-lg p-2 transition ${
              inOwnership
                ? "bg-gray-200 text-gray-900"
                : "text-gray-500 hover:bg-gray-200/60 hover:text-gray-700"
            }`}
          >
            <OwnershipIcon className="h-[18px] w-[18px]" />
          </Link>
        ) : (
          <div className="mb-1 px-2">
            <Link
              to="/ownership"
              className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition ${
                inOwnership
                  ? "bg-gray-200 font-medium text-gray-900"
                  : "text-gray-700 hover:bg-gray-200/60"
              }`}
            >
              <OwnershipIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
              <span>Ownership</span>
            </Link>
          </div>
        )}

        {/* Architectures: visual application architecture diagrams (manual or AI). All users. */}
        {railCollapsed ? (
          <Link
            to="/architectures"
            title="Architectures"
            className={`mx-2 mb-1 flex items-center justify-center rounded-lg p-2 transition ${
              inArchitectures
                ? "bg-gray-200 text-gray-900"
                : "text-gray-500 hover:bg-gray-200/60 hover:text-gray-700"
            }`}
          >
            <ArchitectureIcon className="h-[18px] w-[18px]" />
          </Link>
        ) : (
          <div className="mb-1 px-2">
            <Link
              to="/architectures"
              className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition ${
                inArchitectures
                  ? "bg-gray-200 font-medium text-gray-900"
                  : "text-gray-700 hover:bg-gray-200/60"
              }`}
            >
              <ArchitectureIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
              <span>Architectures</span>
            </Link>
          </div>
        )}

        {/* Estate Graph: central workload-aware knowledge graph of the whole tenant. Admin-only. */}
        {me?.role === "admin" && (railCollapsed ? (
          <Link
            to="/graph"
            title="Estate Graph"
            className={`mx-2 mb-1 flex items-center justify-center rounded-lg p-2 transition ${
              inGraph ? "bg-gray-200 text-gray-900" : "text-gray-500 hover:bg-gray-200/60 hover:text-gray-700"
            }`}
          >
            <GraphIcon className="h-[18px] w-[18px]" />
          </Link>
        ) : (
          <div className="mb-1 px-2">
            <Link
              to="/graph"
              className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition ${
                inGraph ? "bg-gray-200 font-medium text-gray-900" : "text-gray-700 hover:bg-gray-200/60"
              }`}
            >
              <GraphIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
              <span>Estate Graph</span>
            </Link>
          </div>
        ))}

        {/* Proactive Support: posture/forensic dashboards grouped under one expandable
            menu (Monitoring/Telemetry/Backup-DR coverage, Evidence Locker, Retirement
            Radar, Telemetry Intelligence, Performance Profiler). Admin-only. */}
        {me?.role === "admin" && (() => {
          const items = [
            { to: "/assessments", label: "Assessments", Icon: AssessmentIcon, active: inAssessments },
            { to: "/performance", label: "Performance Profiler", Icon: PerformanceIcon, active: inPerformance },
            { to: "/coverage", label: "Monitoring Coverage", Icon: CoverageIcon, active: inCoverage },
            { to: "/telemetry", label: "Telemetry Coverage", Icon: TelemetryIcon, active: inTelemetry },
            { to: "/backupdr", label: "Backup & DR Coverage", Icon: BackupIcon, active: inBackupDr },
            { to: "/radar", label: "Retirement Radar", Icon: RadarIcon, active: inRadar },
            { to: "/reservations", label: "Reservations Monitor", Icon: ReservationIcon, active: inReservations },
            { to: "/inventory", label: "Inventory", Icon: InventoryIcon, active: inInventory },
            { to: "/tagintel", label: "Tag Intelligence", Icon: TagIcon, active: inTagIntel },
            { to: "/change-explorer", label: "Change Explorer", Icon: ChangeIcon, active: inChangeExplorer },
            { to: "/policy", label: "Azure Policy", Icon: PolicyIcon, active: inPolicy },
            { to: "/identity", label: "Identity", Icon: IdentityIcon, active: inIdentity },
            { to: "/rbac", label: "RBAC", Icon: RbacIcon, active: inRbac },
            { to: "/telemetry-intel", label: "Telemetry Intelligence", Icon: TelemetryIntelIcon, active: inTeleIntel },
            { to: "/evidence", label: "Evidence Locker", Icon: EvidenceIcon, active: inEvidence },
          ];
          const anyActive = items.some((i) => i.active);
          return railCollapsed ? (
            <Link
              to="/coverage"
              title="Proactive Support"
              className={`mx-2 mb-1 flex items-center justify-center rounded-lg p-2 transition ${
                anyActive
                  ? "bg-gray-200 text-gray-900"
                  : "text-gray-500 hover:bg-gray-200/60 hover:text-gray-700"
              }`}
            >
              <ProactiveIcon className="h-[18px] w-[18px]" />
            </Link>
          ) : (
            <div className="mb-1 px-2">
              <div className="flex items-center">
                <button
                  onClick={() => setProactiveOpen((v) => !v)}
                  className={`flex flex-1 items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition ${
                    anyActive ? "bg-gray-200 font-medium text-gray-900" : "text-gray-700 hover:bg-gray-200/60"
                  }`}
                >
                  <ProactiveIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
                  <span>Proactive Support</span>
                </button>
                <button
                  onClick={() => setProactiveOpen((v) => !v)}
                  title={proactiveOpen ? "Collapse" : "Expand"}
                  className="ml-1 rounded p-1 text-gray-400 transition hover:bg-gray-200/60 hover:text-gray-700"
                >
                  <ChevronRightIcon className={`h-4 w-4 transition-transform ${proactiveOpen ? "rotate-90" : ""}`} />
                </button>
              </div>
              {proactiveOpen && (
                <div className="mt-0.5 space-y-0.5 pl-3.5">
                  {items.map((it) => (
                    <Link
                      key={it.to}
                      to={it.to}
                      className={`flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px] transition ${
                        it.active
                          ? "bg-gray-200 font-medium text-gray-900"
                          : "text-gray-600 hover:bg-gray-200/60"
                      }`}
                    >
                      <it.Icon className="h-[15px] w-[15px] shrink-0 text-gray-500" />
                      <span>{it.label}</span>
                    </Link>
                  ))}
                </div>
              )}
            </div>
          );
        })()}

        {/* Settings: an expandable menu (mirrors Automations). URL-driven so a refresh
            restores the same panel. Admin-only. */}
        {me?.role === "admin" &&
          (railCollapsed ? (
            <Link
              to="/admin"
              title="Settings"
              className={`mx-2 mb-1 flex items-center justify-center rounded-lg p-2 transition ${
                inAdmin
                  ? "bg-gray-200 text-gray-900"
                  : "text-gray-500 hover:bg-gray-200/60 hover:text-gray-700"
              }`}
            >
              <SettingsIcon className="h-[18px] w-[18px]" />
            </Link>
          ) : (
            <div className="mb-1 px-2">
              <div className="flex items-center">
                <Link
                  to="/admin"
                  className={`flex flex-1 items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition ${
                    inAdmin
                      ? "bg-gray-200 font-medium text-gray-900"
                      : "text-gray-700 hover:bg-gray-200/60"
                  }`}
                >
                  <SettingsIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
                  <span>Settings</span>
                </Link>
                <button
                  onClick={() => setAdminOpen((v) => !v)}
                  title={adminOpen ? "Collapse" : "Expand"}
                  className="ml-1 rounded p-1 text-gray-400 transition hover:bg-gray-200/60 hover:text-gray-700"
                >
                  <ChevronRightIcon
                    className={`h-4 w-4 transition-transform ${
                      adminOpen ? "rotate-90" : ""
                    }`}
                  />
                </button>
              </div>
              {adminOpen && (
                <div className="mt-0.5 space-y-0.5 pl-3.5">
                  {ADMIN_NAV.map((n) => {
                    const active =
                      inAdmin &&
                      (adminSection === n.id ||
                        (n.id === "access" && ACCESS_SUB_IDS.has(adminSection)));
                    return (
                      <div key={n.id}>
                        {n.group && (
                          <div className="px-2.5 pb-0.5 pt-2 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                            {n.group}
                          </div>
                        )}
                        <Link
                          to={`/admin/${n.id}`}
                          className={`flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px] transition ${
                            active
                              ? "bg-gray-200 font-medium text-gray-900"
                              : "text-gray-600 hover:bg-gray-200/60"
                          }`}
                        >
                          <span className="text-xs">{n.icon}</span>
                          {n.label}
                        </Link>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          ))}

        {/* Automations: an expandable menu. URL-driven so a browser refresh restores the
            same panel. Admin-only. Placed below Settings. */}
        {me?.role === "admin" &&
          (railCollapsed ? (
            <Link
              to="/automations"
              title="Automations"
              className={`mx-2 mb-1 flex items-center justify-center rounded-lg p-2 transition ${
                inAutomations && !inCustomAgents
                  ? "bg-gray-200 text-gray-900"
                  : "text-gray-500 hover:bg-gray-200/60 hover:text-gray-700"
              }`}
            >
              <BoltIcon className="h-[18px] w-[18px]" />
            </Link>
          ) : (
            <div className="mb-1 px-2">
              <div className="flex items-center">
                <Link
                  to="/automations"
                  className={`flex flex-1 items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition ${
                    inAutomations && !inCustomAgents
                      ? "bg-gray-200 font-medium text-gray-900"
                      : "text-gray-700 hover:bg-gray-200/60"
                  }`}
                >
                  <BoltIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
                  <span>Automations</span>
                </Link>
                <button
                  onClick={() => setAutomationsOpen((v) => !v)}
                  title={automationsOpen ? "Collapse" : "Expand"}
                  className="ml-1 rounded p-1 text-gray-400 transition hover:bg-gray-200/60 hover:text-gray-700"
                >
                  <ChevronRightIcon
                    className={`h-4 w-4 transition-transform ${
                      automationsOpen ? "rotate-90" : ""
                    }`}
                  />
                </button>
              </div>
              {automationsOpen && (
                <div className="mt-0.5 space-y-0.5 pl-3.5">
                  {AUTOMATIONS_NAV.map((n) => {
                    const active = inAutomations && automationsSection === n.id;
                    return (
                      <Link
                        key={n.id}
                        to={`/automations/${n.id}`}
                        className={`flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px] transition ${
                          active
                            ? "bg-gray-200 font-medium text-gray-900"
                            : "text-gray-600 hover:bg-gray-200/60"
                        }`}
                      >
                        <span className="text-xs">{n.icon}</span>
                        {n.label}
                      </Link>
                    );
                  })}
                </div>
              )}
            </div>
          ))}

        {/* Sub Agents: a top-level link (next to Automations) to the management page.
            Admin-only. Clicking opens the management page; it does NOT expand a submenu. */}
        {me?.role === "admin" &&
          (railCollapsed ? (
            <Link
              to="/automations/agents"
              title="Sub Agents"
              className={`mx-2 mb-1 flex items-center justify-center rounded-lg p-2 transition ${
                inCustomAgents
                  ? "bg-gray-200 text-gray-900"
                  : "text-gray-500 hover:bg-gray-200/60 hover:text-gray-700"
              }`}
            >
              <RobotIcon className="h-[18px] w-[18px]" />
            </Link>
          ) : (
            <div className="mb-1 px-2">
              <Link
                to="/automations/agents"
                className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition ${
                  inCustomAgents
                    ? "bg-gray-200 font-medium text-gray-900"
                    : "text-gray-700 hover:bg-gray-200/60"
                }`}
              >
                <RobotIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
                <span>Sub Agents</span>
              </Link>
            </div>
          ))}

        {/* Monitor: a single link to the central dashboard. Admin-only. */}
        {me?.role === "admin" &&
          (railCollapsed ? (
            <Link
              to="/monitor"
              title="Monitor"
              className={`mx-2 mb-1 flex items-center justify-center rounded-lg p-2 transition ${
                inMonitor
                  ? "bg-gray-200 text-gray-900"
                  : "text-gray-500 hover:bg-gray-200/60 hover:text-gray-700"
              }`}
            >
              <MonitorIcon className="h-[18px] w-[18px]" />
            </Link>
          ) : (
            <div className="mb-1 px-2">
              <Link
                to="/monitor"
                className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition ${
                  inMonitor
                    ? "bg-gray-200 font-medium text-gray-900"
                    : "text-gray-700 hover:bg-gray-200/60"
                }`}
              >
                <MonitorIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
                <span>Monitor</span>
              </Link>
            </div>
          ))}

        {/* Stats: a read-only at-a-glance metrics page. Admin-only. */}
        {me?.role === "admin" &&
          (railCollapsed ? (
            <Link
              to="/stats"
              title="Stats"
              className={`mx-2 mb-1 flex items-center justify-center rounded-lg p-2 transition ${
                inStats
                  ? "bg-gray-200 text-gray-900"
                  : "text-gray-500 hover:bg-gray-200/60 hover:text-gray-700"
              }`}
            >
              <StatsIcon className="h-[18px] w-[18px]" />
            </Link>
          ) : (
            <div className="mb-1 px-2">
              <Link
                to="/stats"
                className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition ${
                  inStats
                    ? "bg-gray-200 font-medium text-gray-900"
                    : "text-gray-700 hover:bg-gray-200/60"
                }`}
              >
                <StatsIcon className="h-[18px] w-[18px] shrink-0 text-gray-500" />
                <span>Stats</span>
              </Link>
            </div>
          ))}

        {/* Search + quick filter — sit just above the chat list (Favorites / Recents). */}
        {!railCollapsed && (
          <>
            <div className="relative mx-2 mb-2 mt-1">
              <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400">
                <SearchIcon className="h-3.5 w-3.5" />
              </span>
              <input
                value={chatSearch}
                onChange={(e) => setChatSearch(e.target.value)}
                placeholder="Search chats…"
                className="w-full rounded-lg border border-gray-200 bg-white py-1.5 pl-8 pr-7 text-sm text-gray-700 placeholder:text-gray-400 focus:border-gray-300 focus:outline-none focus:ring-0"
              />
              {chatSearch && (
                <button
                  onClick={() => setChatSearch("")}
                  title="Clear"
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                >
                  ×
                </button>
              )}
            </div>

            {/* Quick filter chips */}
            <div className="mb-1 flex gap-1.5 px-2">
              {(["all", "pinned"] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setChatFilter(f)}
                  className={`rounded-full px-2.5 py-0.5 text-xs font-medium capitalize transition ${
                    chatFilter === f
                      ? "bg-gray-200 text-gray-800"
                      : "text-gray-500 hover:bg-gray-100"
                  }`}
                >
                  {f === "all" ? "All" : "Pinned"}
                </button>
              ))}
            </div>
          </>
        )}

        {/* Chat list (hidden when collapsed) */}
        {!railCollapsed && (
          <nav className="px-2 pb-2">
            {filteredChats.length === 0 && (
              <div className="px-3 py-4 text-center text-xs text-gray-400">
                {q
                  ? "No threads match your search."
                  : chatFilter === "pinned"
                    ? "No pinned threads."
                    : "No threads yet."}
              </div>
            )}
            {pinnedChats.length > 0 && (
              <>
                <div className="px-2.5 pb-1 pt-2 text-[11px] font-medium text-gray-400">
                  Favorites
                </div>
                {pinnedChats.map(renderChatRow)}
              </>
            )}
            {recentChats.length > 0 && (
              <>
                <div className="px-2.5 pb-1 pt-3 text-[11px] font-medium text-gray-400">
                  {q ? "Results" : "Recents"}
                </div>
                {recentChats.map(renderChatRow)}
              </>
            )}
          </nav>
        )}
        {railCollapsed && <div className="flex-1" />}
        </div>

        {/* Footer: Trash + delete unpinned + Admin Dashboard link */}
        <div className="mt-auto border-t border-gray-200">
          <button
            onClick={() => setTrashOpen(true)}
            title="Trash"
            className={`flex w-full items-center text-[13px] text-gray-500 transition hover:bg-gray-200/60 hover:text-gray-800 ${
              railCollapsed ? "justify-center p-2.5" : "gap-2.5 px-3 py-2.5"
            }`}
          >
            <TrashIcon className="h-[18px] w-[18px] shrink-0" />
            {!railCollapsed && <span>Trash</span>}
          </button>
          {!railCollapsed && chats.length > 0 && (
            <button
              onClick={async () => {
                if (
                  !window.confirm(
                    "Move all unpinned chats to Trash? Pinned chats are kept. You can restore them from Trash.",
                  )
                )
                  return;
                await api.deleteAllChats();
                startNewChat();
                qc.invalidateQueries({ queryKey: ["chats"] });
                qc.invalidateQueries({ queryKey: ["trashChats"] });
              }}
              className="flex w-full items-center gap-2.5 px-3 py-2.5 text-[13px] text-gray-500 transition hover:bg-gray-200/60 hover:text-red-600"
            >
              <TrashIcon className="h-[18px] w-[18px] shrink-0" />
              Delete unpinned
            </button>
          )}
        </div>
      </aside>

      {/* Trash overlay: lists soft-deleted chats with restore / permanent-delete. */}
      {trashOpen && (
        <TrashPanel
          chats={trashedChats}
          onClose={() => setTrashOpen(false)}
          onRestore={(id) => void restoreTrashedChat(id)}
          onPurge={(id) => void purgeTrashedChat(id)}
          onEmpty={() => void emptyTrash()}
        />
      )}

      {/* Main */}
      {inAutomations ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Automations">
            <Suspense fallback={<PanelLoading />}>
              <AutomationsPanel section={automationsSection} />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inAdmin ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Settings">
            <Suspense fallback={<PanelLoading />}>
              <AdminPanel section={adminSection} />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inDashboard ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Dashboard">
            <Suspense fallback={<PanelLoading />}>
              <DashboardPanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inMonitor ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Monitor">
            <Suspense fallback={<PanelLoading />}>
              <MonitorPanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inStats ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Stats">
            <Suspense fallback={<PanelLoading />}>
              <StatsPanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inMissionControl ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Mission Control">
            <Suspense fallback={<PanelLoading />}>
              <MissionControlPanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inWorkloads ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Workloads">
            <Suspense fallback={<PanelLoading />}>
              <WorkloadsPanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inOwnership ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Ownership">
            <Suspense fallback={<PanelLoading />}>
              <OwnershipPanel tab={ownershipTab} />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inInventory ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Inventory">
            <Suspense fallback={<PanelLoading />}>
              <InventoryPanel tab={inventoryTab} />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inTagIntel ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Tag Intelligence">
            <Suspense fallback={<PanelLoading />}>
              <TagIntelligencePanel tab={tagIntelTab} />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inChangeExplorer ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Change Explorer">
            <Suspense fallback={<PanelLoading />}>
              <ChangeExplorerPanel tab={changeExplorerTab} />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inAssessments ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Assessments">
            <Suspense fallback={<PanelLoading />}>
              <AssessmentsPanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inArchitectures ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Architectures">
            <Suspense fallback={<PanelLoading />}>
              <ArchitecturesPanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inPolicy ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Policy">
            <Suspense fallback={<PanelLoading />}>
              <PolicyPanel tab={policyTab} />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inRbac ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="RBAC">
            <Suspense fallback={<PanelLoading />}>
              <RbacPanel tab={rbacTab} />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inIdentity ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Identity">
            <Suspense fallback={<PanelLoading />}>
              <IdentityPanel tab={identityTab} />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inCoverage ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Monitoring Coverage">
            <Suspense fallback={<PanelLoading />}>
              <MonitoringCoveragePanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inTelemetry ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Telemetry Coverage">
            <Suspense fallback={<PanelLoading />}>
              <TelemetryCoveragePanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inBackupDr ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Backup & DR Coverage">
            <Suspense fallback={<PanelLoading />}>
              <BackupDrCoveragePanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inGraph ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Estate Graph">
            <Suspense fallback={<PanelLoading />}>
              <GraphPanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inEvidence ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Evidence Locker">
            <Suspense fallback={<PanelLoading />}>
              <EvidenceLockerPanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inRadar ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Retirement Radar">
            <Suspense fallback={<PanelLoading />}>
              <RetirementRadarPanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inReservations ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Reservations Monitor">
            <Suspense fallback={<PanelLoading />}>
              <ReservationsMonitorPanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inTeleIntel ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Telemetry Intelligence">
            <Suspense fallback={<PanelLoading />}>
              <TelemetryIntelligencePanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inPerformance ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Performance Profiler">
            <Suspense fallback={<PanelLoading />}>
              <PerformancePanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : inNotifications ? (
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PanelErrorBoundary name="Notifications">
            <Suspense fallback={<PanelLoading />}>
              <NotificationsPanel />
            </Suspense>
          </PanelErrorBoundary>
        </main>
      ) : (
      <main className="flex min-w-0 flex-1 flex-col">
        {activeId && displayMessages.length > 0 && (
          <div className="flex items-center gap-2 border-b bg-white px-6 py-1.5">
            <button
              onClick={() => void regenerate()}
              disabled={streaming}
              title="Regenerate the last response"
              className="rounded-md px-2 py-1 text-xs text-gray-500 hover:bg-gray-100 hover:text-gray-700 disabled:opacity-40"
            >
              ↻ Regenerate
            </button>
            <button
              onClick={exportChat}
              title="Export chat as Markdown"
              className="rounded-md px-2 py-1 text-xs text-gray-500 hover:bg-gray-100 hover:text-gray-700"
            >
              ⬇ Export
            </button>
          </div>
        )}
        <div ref={scrollContainerRef} onScroll={handleMessagesScroll} className="flex-1 overflow-y-auto px-6 py-4">
          <div
            className={`mx-auto space-y-4 ${
              showWelcome
                ? "max-w-3xl"
                : "max-w-5xl xl:max-w-6xl 2xl:max-w-screen-2xl"
            }`}
          >
            {showWelcome && (
              <div className="mx-auto mt-10 w-full max-w-3xl">
                <div className="mb-8 text-center text-2xl font-semibold text-gray-800">
                  What are you working on?
                </div>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  {/* Quick checks — collapsible button menus */}
                  <div className="rounded-xl border border-gray-200 bg-white p-2 shadow-sm">
                    <div className="px-1.5 pb-1 pt-0.5 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
                      Quick checks
                    </div>
                    <div className="max-h-[60vh] overflow-y-auto pr-1">
                      {STARTER_CATEGORIES.map((cat) => (
                        <StarterCategory
                          key={cat.title}
                          category={cat}
                          onPick={(prompt) => fillComposer(prompt)}
                        />
                      ))}
                    </div>
                  </div>

                  {/* Browse by resource & problem */}
                  <div className="rounded-xl border border-gray-200 bg-white p-2 shadow-sm">
                    <div className="flex items-center justify-between px-1.5 pb-1 pt-0.5 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
                      <span>Browse by resource &amp; problem</span>
                      <span className="font-normal normal-case text-gray-300">
                        # = sub-items
                      </span>
                    </div>
                    <div className="max-h-[60vh] overflow-y-auto pr-1">
                      {SORTED_PROBLEM_TREE.map((node) => (
                        <TreeNode
                          key={node.label}
                          node={node}
                          depth={0}
                          trail={[]}
                          onPick={(prompt) => fillComposer(prompt)}
                        />
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            )}

            {displayMessages.map((m, idx) => {
              const isLastAssistant =
                m.role === "assistant" && idx === displayMessages.length - 1;
              return m.role === "assistant" ? (
                <div key={m.id} className="space-y-2">
                  {(() => {
                    // Prefer the full persisted progress feed (detailed lines shown
                    // live); fall back to the condensed reasoning+tool step list.
                    const plog = progressLogs[m.id];
                    if (plog && plog.length > 0) {
                      return <ProgressPane log={plog} level={progressLevel} />;
                    }
                    const act = (m.activity as Step[] | undefined) ?? activities[m.id];
                    return act && act.length > 0 ? (
                      <ActivityPane steps={act} live={false} />
                    ) : null;
                  })()}
                  <Bubble
                    role="assistant"
                    content={m.content}
                    timestamp={m.created_at}
                    provider={m.provider}
                    model={m.model}
                    durationMs={m.duration_ms}
                  />
                  {!streaming && m.content.trim() && (
                    <div className="flex items-center gap-1 pl-1">
                      {(investigations[m.id] || m.investigation) && (
                        <button
                          onClick={() => setOpenInvestigation(m.id)}
                          title="View the deep-investigation hypothesis tree"
                          className="flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-brand transition hover:bg-brand/10"
                        >
                          <SparkleIcon className="h-3.5 w-3.5" />
                          Investigation
                        </button>
                      )}
                      <button
                        onClick={() => void regenerateFrom(m.id)}
                        title="Retry this answer"
                        className="flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-gray-500 transition hover:bg-gray-100 hover:text-gray-700"
                      >
                        <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
                          <path d="M15.3 8A6 6 0 104 10" strokeLinecap="round" />
                          <path d="M15.5 4v4h-4" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                        Retry
                      </button>
                      {isLastAssistant ? (
                        <ModelPicker
                          provider={pickerProvider}
                          model={pickerModel}
                          variant="retry"
                          onPick={(prov, mod) => void regenerate(prov, mod)}
                        />
                      ) : (
                        <ModelPicker
                          provider={pickerProvider}
                          model={pickerModel}
                          variant="retry"
                          onPick={(prov, mod) => void regenerateFrom(m.id, prov, mod)}
                        />
                      )}
                      <button
                        onClick={() => void breakout(m.id)}
                        title="Break out into a new chat (copies the conversation up to here)"
                        className="flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-gray-500 transition hover:bg-gray-100 hover:text-gray-700"
                      >
                        <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7">
                          <path d="M11 3h6v6" strokeLinecap="round" strokeLinejoin="round" />
                          <path d="M17 3l-7 7" strokeLinecap="round" strokeLinejoin="round" />
                          <path d="M15 12v3a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h3" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                        Breakout
                      </button>
                      <CopyButton
                        content={m.content}
                        title="Copy this answer as Markdown"
                        label="Copy"
                        className="flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-gray-500 transition hover:bg-gray-100 hover:text-gray-700"
                      />
                    </div>
                  )}
                </div>
              ) : (
                <Bubble
                  key={m.id}
                  role={m.role}
                  content={m.content}
                  images={m.images}
                  timestamp={m.created_at}
                  onResend={
                    m.role === "user" && !streaming
                      ? () => void resendFrom(m.id)
                      : undefined
                  }
                />
              );
            })}

            {/* Live progress for the in-flight turn */}
            {streaming && (
              <div className="space-y-2">
                {/* Deep investigation: a random animated agent icon at the top. */}
                {live?.deepIcon && (
                  <div className="flex items-center gap-3 rounded-lg border border-brand/20 bg-gradient-to-br from-brand/10 to-transparent px-4 py-3">
                    <img
                      src={live.deepIcon}
                      alt=""
                      aria-hidden="true"
                      className="h-12 w-12 shrink-0"
                    />
                    <div className="min-w-0">
                      <div className="text-sm font-semibold text-gray-800">
                        Deep investigation in progress
                      </div>
                      <div className="text-xs text-gray-500">
                        Specialist agents are analyzing your Azure environment…
                      </div>
                    </div>
                  </div>
                )}
                {/* Live progress feed — appears immediately, streams details, and shows
                    the answer-so-far inside the same panel. */}
                <LiveProgress
                  log={liveLog}
                  startedAt={liveStartedAt}
                  answer={streamText}
                  level={progressLevel}
                />
              </div>
            )}

            {errorDetail && (
              <div className="flex justify-start">
                <div className="max-w-full rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
                  <div className="mb-1 font-medium">⚠️ The model failed to respond</div>
                  <div className="whitespace-pre-wrap break-words text-xs text-red-600">
                    {errorDetail}
                  </div>
                  <div className="mt-2 text-xs text-red-500">
                    Try again, or switch models with the picker below.
                  </div>
                </div>
              </div>
            )}

            {/* Deep investigation launcher: pick the specialist agents (AI pre-selected) */}
            {pendingDeepAuth && (
              <div className="space-y-2">
                <Bubble role="user" content={pendingDeepAuth.content} />
                <div className="rounded-xl border border-brand/30 bg-brand/5 p-4">
                  <div className="mb-1 flex items-center gap-2 text-sm font-medium text-gray-800">
                    <WarRoomIcon className="h-5 w-5" />
                    Assemble your investigation team
                  </div>
                  <div className="mb-3 text-xs text-gray-500">
                    Deep investigation dispatches specialist AI agents that work in parallel —
                    each researches its domain, forms hypotheses, and validates them with
                    evidence from your live Azure data. Pick who joins the war room.
                  </div>

                  {deepAgentOptions === null ? (
                    <ThinkingDots label="AI is selecting the right specialists…" />
                  ) : deepAgentOptions.length === 0 ? (
                    <div className="mb-3 text-xs text-amber-600">
                      Couldn’t load suggestions — the investigation will pick agents automatically.
                    </div>
                  ) : (
                    <div className="mb-3">
                      <div className="mb-1.5 flex items-center justify-between">
                        <span className="text-[11px] font-medium text-gray-500">
                          {deepAgentSel.size} of {deepAgentOptions.length} selected
                        </span>
                        <button
                          onClick={() => {
                            if (allHandsActive) {
                              // Restore the selection that was in effect before All Hands.
                              setDeepAgentSel(new Set(allHandsPrevSel));
                              setAllHandsActive(false);
                            } else {
                              setAllHandsPrevSel(new Set(deepAgentSel));
                              setDeepAgentSel(new Set(deepAgentOptions.map((a) => a.id)));
                              setAllHandsActive(true);
                            }
                          }}
                          className={`flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] font-medium transition ${
                            allHandsActive
                              ? "border-brand bg-brand text-white"
                              : "border-brand/40 bg-white text-brand hover:bg-brand/5"
                          }`}
                        >
                          🙌 All Hands On Deck
                        </button>
                      </div>
                      <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                      {deepAgentOptions.map((a) => {
                        const on = deepAgentSel.has(a.id);
                        return (
                          <button
                            key={a.id}
                            onClick={() =>
                              setDeepAgentSel((prev) => {
                                const next = new Set(prev);
                                if (next.has(a.id)) next.delete(a.id);
                                else next.add(a.id);
                                setAllHandsActive(false);
                                return next;
                              })
                            }
                            className={`flex items-start gap-2 rounded-lg border p-2 text-left transition ${
                              on
                                ? "border-brand/50 bg-white shadow-sm"
                                : "border-gray-200 bg-white/40 opacity-70 hover:opacity-100"
                            }`}
                          >
                            <span className="text-lg leading-none">{a.icon}</span>
                            <span className="min-w-0 flex-1">
                              <span className="flex items-center gap-1.5">
                                <span className="truncate text-[13px] font-semibold text-gray-800">{a.name}</span>
                                {a.recommended && (
                                  <span className="shrink-0 rounded-full bg-brand/10 px-1.5 text-[9px] font-medium text-brand">
                                    AI pick
                                  </span>
                                )}
                              </span>
                              <span className="mt-0.5 block text-[11px] leading-snug text-gray-500">
                                {a.reason || a.domain}
                              </span>
                            </span>
                            <span
                              className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[9px] ${
                                on ? "border-brand bg-brand text-white" : "border-gray-300 text-transparent"
                              }`}
                            >
                              ✓
                            </span>
                          </button>
                        );
                      })}
                      </div>
                    </div>
                  )}

                  {/* Architecture memory picker — only when the chat's workload has
                      documented architectures whose memory can inform the investigation. */}
                  {(() => {
                    const candidates = (allMemories?.memories ?? []).filter(
                      (m) =>
                        m.enabled_for_investigations &&
                        m.architecture_exists &&
                        selectedWorkloadId &&
                        m.workload_id === selectedWorkloadId,
                    );
                    if (candidates.length === 0) return null;
                    return (
                      <div className="mb-3 rounded-lg border border-gray-200 bg-white/60 p-2.5">
                        <div className="mb-1 flex items-center gap-1.5 text-[12px] font-medium text-gray-700">
                          🧠 Architecture memory
                          <span className="font-normal text-gray-400">— intended design &amp; known gaps to inform the investigation</span>
                        </div>
                        <select
                          value={deepMemorySel}
                          onChange={(e) => setDeepMemorySel(e.target.value)}
                          className="w-full rounded-md border border-gray-200 px-2 py-1.5 text-[13px] text-gray-700 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                        >
                          <option value="">
                            {candidates.length === 1 ? `Auto · ${candidates[0].architecture_name}` : "Auto (let the investigator decide)"}
                          </option>
                          {candidates.map((m) => (
                            <option key={m.architecture_id} value={m.architecture_id}>
                              {m.architecture_name}{m.title && m.title !== m.architecture_name ? ` — ${m.title}` : ""}
                            </option>
                          ))}
                          <option value="none">Don&rsquo;t use any memory</option>
                        </select>
                      </div>
                    );
                  })()}

                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      onClick={() => {
                        const { chatId, content, hasImages, regenerate } = pendingDeepAuth;
                        const agents = [...deepAgentSel];
                        const mem = deepMemorySel;
                        setPendingDeepAuth(null);
                        setDeepAgentOptions(null);
                        void clarifyAndSend(chatId, content, hasImages, "deep", agents, regenerate, mem);
                      }}
                      disabled={deepAgentOptions !== null && deepAgentOptions.length > 0 && deepAgentSel.size === 0}
                      className="flex items-center gap-1.5 rounded-lg bg-brand px-3.5 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-50"
                    >
                      <SparkleIcon className="h-3.5 w-3.5" />
                      {deepAgentSel.size > 0 ? `Launch ${deepAgentSel.size} agent${deepAgentSel.size === 1 ? "" : "s"}` : "Launch investigation"}
                    </button>
                    <button
                      onClick={() => {
                        const { chatId, content, hasImages, regenerate } = pendingDeepAuth;
                        setPendingDeepAuth(null);
                        setDeepAgentOptions(null);
                        void clarifyAndSend(chatId, content, hasImages, "normal", undefined, regenerate);
                      }}
                      className="rounded-lg border border-gray-300 bg-white px-3.5 py-1.5 text-sm font-medium text-gray-600 transition hover:bg-gray-100"
                    >
                      Cancel — standard answer
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* Propose problems: offer sharper, catalog-matched problem statements */}
            {pendingPropose && (
              <div className="space-y-2">
                <Bubble role="user" content={pendingPropose.content} />
                <div className="rounded-xl border border-brand/30 bg-brand/5 p-4">
                  {pendingPropose.checking ? (
                    <ThinkingDots label="Matching your question to known problems…" />
                  ) : (
                    <>
                      <div className="mb-1 flex items-center gap-2 text-sm font-medium text-gray-800">
                        <SparkleIcon className="h-4 w-4 text-brand" />
                        Did you mean one of these?
                      </div>
                      <div className="mb-3 text-xs text-gray-500">
                        Pick a sharper problem statement to investigate, or keep your own wording.
                      </div>
                      <div className="space-y-1.5">
                        {pendingPropose.suggestions.map((s, i) => (
                          <button
                            key={i}
                            onClick={() => {
                              const id = pendingPropose.chatId;
                              setPendingPropose(null);
                              void clarifyAndSend(id, s, false);
                            }}
                            className="group flex w-full items-start gap-2.5 rounded-lg border border-brand/30 bg-white px-3 py-2.5 text-left text-sm text-gray-700 transition hover:border-brand hover:bg-brand/10 hover:text-brand"
                          >
                            <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-brand/10 text-[11px] font-semibold text-brand">
                              {i + 1}
                            </span>
                            <span className="flex-1">{s}</span>
                            <ChevronRightIcon className="mt-0.5 h-4 w-4 shrink-0 text-gray-300 transition group-hover:text-brand" />
                          </button>
                        ))}
                      </div>
                      <button
                        onClick={() => {
                          const id = pendingPropose.chatId;
                          const c = pendingPropose.content;
                          setPendingPropose(null);
                          void clarifyAndSend(id, c, false);
                        }}
                        className="mt-3 rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-600 transition hover:bg-gray-100"
                      >
                        Use my question as-is
                      </button>
                    </>
                  )}
                </div>
              </div>
            )}

            {/* Scope clarification: show the user's question + subscription choices */}
            {pendingClarify && (
              <div className="space-y-2">
                <Bubble role="user" content={pendingClarify.content} />
                <div className="rounded-xl border border-brand/30 bg-brand/5 p-4">
                  {pendingClarify.checking ? (
                    <ThinkingDots label="Checking scope…" />
                  ) : pendingClarify.mgOptions && pendingClarify.mgOptions.length > 0 ? (
                    <>
                      <div className="mb-1 text-sm font-medium text-gray-800">
                        Which management group should I investigate?
                      </div>
                      <div className="mb-3 text-xs text-gray-500">
                        Pick one to scope governance/policy queries, or skip to search all
                        subscriptions.
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {pendingClarify.mgOptions.map((o) => (
                          <button
                            key={o.id}
                            onClick={() => {
                              const c = pendingClarify.content;
                              const subs = pendingClarify.options;
                              const id = activeId;
                              const mgScope = {
                                management_group_id: o.id,
                                management_group_name: o.name,
                              };
                              // Chain into the subscription pick when available;
                              // otherwise run with just the management-group scope.
                              if (subs && subs.length > 0) {
                                setPendingClarify({ content: c, options: subs, mgScope, thinking: pendingClarify.thinking });
                              } else {
                                setPendingClarify(null);
                                if (id) void runSend(id, c, { ...mgScope, thinking_level: pendingClarify.thinking ?? "normal" });
                              }
                            }}
                            className="rounded-lg border border-brand/40 bg-white px-3 py-1.5 text-sm text-gray-700 transition hover:border-brand hover:bg-brand/10 hover:text-brand"
                          >
                            {o.name}
                          </button>
                        ))}
                        <button
                          onClick={() => {
                            const c = pendingClarify.content;
                            const subs = pendingClarify.options;
                            const id = activeId;
                            // Skipping the management group still drills into the
                            // subscription pick when subscription clarification is on.
                            if (subs && subs.length > 0) {
                              setPendingClarify({ content: c, options: subs, thinking: pendingClarify.thinking });
                            } else {
                              setPendingClarify(null);
                              if (id) void runSend(id, c, { scope_all: true, thinking_level: pendingClarify.thinking ?? "normal" });
                            }
                          }}
                          className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-600 transition hover:bg-gray-100"
                        >
                          Skip — search all
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="mb-1 text-sm font-medium text-gray-800">
                        Which subscription should I investigate?
                      </div>
                      <div className="mb-3 text-xs text-gray-500">
                        Pick one to focus the search, or skip to search all subscriptions.
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {pendingClarify.options.map((o) => (
                          <button
                            key={o.id}
                            onClick={() => {
                              const c = pendingClarify.content;
                              const id = activeId;
                              const mgScope = pendingClarify.mgScope;
                              setPendingClarify(null);
                              if (id)
                                void runSend(id, c, {
                                  ...(mgScope ?? {}),
                                  subscription_id: o.id,
                                  subscription_name: o.name,
                                  thinking_level: pendingClarify.thinking ?? "normal",
                                });
                            }}
                            className="rounded-lg border border-brand/40 bg-white px-3 py-1.5 text-sm text-gray-700 transition hover:border-brand hover:bg-brand/10 hover:text-brand"
                          >
                            {o.name}
                            {o.is_default && (
                              <span className="ml-1.5 text-[10px] text-gray-400">default</span>
                            )}
                          </button>
                        ))}
                        <button
                          onClick={() => {
                            const c = pendingClarify.content;
                            const id = activeId;
                            const mgScope = pendingClarify.mgScope;
                            setPendingClarify(null);
                            // If a management group was chosen, keep that scope; only
                            // fall back to "search all" when there is no MG scope.
                            if (id)
                              void runSend(id, c, { ...(mgScope ?? { scope_all: true }), thinking_level: pendingClarify.thinking ?? "normal" });
                          }}
                          className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-600 transition hover:bg-gray-100"
                        >
                          {pendingClarify.mgScope
                            ? "Skip — all subscriptions in this group"
                            : "Skip — search all"}
                        </button>
                      </div>
                    </>
                  )}
                </div>
              </div>
            )}

            {/* Follow-up suggestions after an answer */}
            {!streaming && !pendingClarify && suggestions.length > 0 && displayMessages.length > 0 && (
              <div className="flex flex-wrap gap-2 pt-1">
                {suggestions.map((s) => (
                  <button
                    key={s}
                    onClick={() => void send(s)}
                    className="rounded-full border border-brand/30 bg-brand/5 px-3 py-1.5 text-xs text-brand transition hover:bg-brand/10"
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}

            <div ref={bottomRef} />
          </div>
        </div>

        <div className="relative bg-white px-6 pb-4 pt-2">
          {showNewMessages && (
            <button
              onClick={scrollToBottom}
              className="absolute -top-5 left-1/2 z-10 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-sky-500 bg-sky-500 px-3 py-1.5 text-xs font-medium text-white shadow-md transition hover:bg-sky-600"
            >
              New messages
              <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
                <path d="M10 4v11M5.5 10.5L10 15l4.5-4.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          )}
          <div className="mx-auto max-w-5xl xl:max-w-6xl 2xl:max-w-screen-2xl">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              multiple
              className="hidden"
              onChange={(e) => {
                if (e.target.files) void addImageFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <div className="rounded-3xl border border-gray-300 bg-white px-4 py-3 shadow-sm transition focus-within:border-gray-400 focus-within:shadow-md">
              {attachments.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-2">
                  {attachments.map((src, i) => (
                    <div key={i} className="relative">
                      <img
                        src={src}
                        alt={`attachment ${i + 1}`}
                        className="h-16 w-16 rounded-lg border object-cover"
                      />
                      <button
                        onClick={() => setAttachments((a) => a.filter((_, j) => j !== i))}
                        className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-gray-700 text-xs text-white hover:bg-gray-900"
                        title="Remove"
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>
              )}

              <textarea
                ref={composerRef}
                value={input}
                onChange={(e) => setInputSynced(e.target.value)}
                onPaste={onComposerPaste}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void send();
                  }
                }}
                rows={1}
                placeholder="Write a message…"
                className="block max-h-[200px] w-full resize-none overflow-y-hidden bg-transparent px-1 text-[15px] leading-6 text-gray-900 placeholder:text-gray-400 focus:outline-none"
              />

              <div className="mt-2 flex flex-wrap items-center justify-between gap-y-2">
                <div className="flex min-w-0 items-center gap-1.5">
                  <button
                    onClick={() => fileInputRef.current?.click()}
                    title="Attach image"
                    className="flex h-9 w-9 items-center justify-center rounded-full text-gray-500 transition hover:bg-gray-100"
                  >
                    <svg className="h-5 w-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
                      <path d="M10 4.5v11M4.5 10h11" strokeLinecap="round" />
                    </svg>
                  </button>
                  <ThinkingPicker level={thinkingLevel} onChange={changeThinking} />
                  {enabledAgents.length > 0 && (
                    <AgentPicker
                      agents={enabledAgents}
                      selectedId={selectedAgentId}
                      onChange={changeAgent}
                    />
                  )}
                </div>

                <div className="flex min-w-0 items-center gap-1.5">
                  {tenants.length > 0 && (
                    <TenantPicker
                      tenants={tenants}
                      activeId={activeTenant?.id}
                      onPick={(id) => {
                        if (activeId) {
                          void api.setChatConnection(activeId, id).then(() => {
                            qc.invalidateQueries({ queryKey: ["chats"] });
                          });
                        }
                      }}
                    />
                  )}
                  <WorkloadPicker
                    selectedId={selectedWorkloadId}
                    onChange={setSelectedWorkloadId}
                  />
                  <ModelPicker
                    provider={pickerProvider}
                    model={pickerModel}
                    dropUp
                    variant="bare"
                    onPick={(prov, m) => {
                      if (activeId) {
                        // Existing chat: set only THIS chat's model. Don't touch the
                        // global default — switching a model mid-conversation must not
                        // re-point what new chats / automations inherit.
                        void api.setChatModel(activeId, prov, m).then(() => {
                          qc.invalidateQueries({ queryKey: ["chats"] });
                        });
                      } else {
                        // No chat yet (welcome screen): choosing a model here is the
                        // explicit way to set the global default for the next new chat.
                        void api
                          .updateLlmConfig({ active_provider: prov, providers: { [prov]: { model: m } } })
                          .then(() => {
                            qc.invalidateQueries({ queryKey: ["activeLlm"] });
                            qc.invalidateQueries({ queryKey: ["llmConfig"] });
                          });
                      }
                    }}
                  />
                  {streaming ? (
                    <button
                      onClick={stopStreaming}
                      title="Stop generating"
                      className="flex h-9 w-9 items-center justify-center rounded-full bg-gray-900 text-white transition hover:bg-black"
                    >
                      <span className="h-3 w-3 rounded-[2px] bg-white" />
                    </button>
                  ) : (
                    <button
                      onClick={() => void send()}
                      disabled={!input.trim() && attachments.length === 0}
                      title="Send"
                      className="flex h-9 w-9 items-center justify-center rounded-full bg-gray-900 text-white transition hover:bg-black disabled:bg-gray-300"
                    >
                      <svg className="h-5 w-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
                        <path d="M10 16V5M5.5 9.5L10 5l4.5 4.5" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      </main>
      )}
      {editorState && activeId && (
        <CommandEditorPanel
          chatId={activeId}
          command={editorState.command}
          mode={editorState.mode}
          onClose={() => setEditorState(null)}
        />
      )}
      {mermaidEdit !== null && (
        <MermaidEditorPanel
          code={mermaidEdit}
          onClose={() => setMermaidEdit(null)}
        />
      )}
      {(() => {
        // Idle placeholder so the Deep Investigation sidebar appears immediately when
        // a new/empty chat is in deep mode — before the first message streams a tree.
        const EMPTY_INV: Investigation = { phases: [], hypotheses: [], conclusion: null };
        const panelInv: Investigation | undefined =
          openInvestigation === "__live__"
            ? live?.investigation
            : openInvestigation === "__deep_idle__"
              ? EMPTY_INV
              : openInvestigation
                ? investigations[openInvestigation] ??
                  messages.find((m) => m.id === openInvestigation)?.investigation ??
                  undefined
                : undefined;
        if (!panelInv) return null;
        return (
          <InvestigationPanel
            investigation={panelInv}
            live={openInvestigation === "__live__"}
            pinned={investigationPinned}
            onTogglePin={toggleInvestigationPinned}
            onClose={() => {
              setOpenInvestigation(null);
              // Closing the panel also clears the pin, so it stays closed until the
              // user explicitly reopens (and optionally re-pins) it.
              if (investigationPinned) toggleInvestigationPinned();
            }}
          />
        );
      })()}
      {/* Floating "reopen investigation" pill — shown when a deep investigation is
          running (or just finished) for the active chat but its panel is closed, so
          an accidentally-dismissed panel can always be brought back. */}
      {(() => {
        const liveInv = live?.investigation;
        const lastDeep = [...displayMessages].reverse().find(
          (m) => m.role === "assistant" && (investigations[m.id] || m.investigation),
        );
        const reopenId = liveInv ? "__live__" : lastDeep?.id;
        if (!reopenId || openInvestigation) return null;
        const running = !!liveInv && streaming;
        return (
          <button
            onClick={() => setOpenInvestigation(reopenId)}
            className="absolute bottom-24 right-4 z-20 flex items-center gap-2 rounded-full border border-brand/30 bg-white px-3.5 py-2 text-sm font-medium text-brand shadow-lg transition hover:bg-brand/5"
            title="Show the deep-investigation panel"
          >
            {running ? (
              <span className="flex h-2 w-2">
                <span className="absolute inline-flex h-2 w-2 animate-ping rounded-full bg-brand/60" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-brand" />
              </span>
            ) : (
              <SparkleIcon className="h-4 w-4" />
            )}
            {running ? "Investigation running" : "Investigation"}
          </button>
        );
      })()}
      {deepNotice && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => setDeepNotice(false)}>
          <div className="w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-2 flex items-center gap-2 text-base font-semibold text-gray-800">
              <SparkleIcon className="h-5 w-5 text-brand" />
              Enable deep investigation?
            </div>
            <p className="mb-4 text-sm text-gray-600">
              Deep investigations give the agent a structured methodology for complex problems:
              it researches context, forms multiple hypotheses, and validates each one with
              evidence. These runs query multiple data sources and can take several minutes.
              Deep mode stays on for subsequent messages until you switch back to Normal.
            </p>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setDeepNotice(false)}
                className="rounded-lg border border-gray-300 px-4 py-1.5 text-sm text-gray-600 transition hover:bg-gray-50"
              >
                Not now
              </button>
              <button
                onClick={() => {
                  try {
                    localStorage.setItem(DEEP_CONFIRMED_KEY, "1");
                  } catch {
                    /* ignore */
                  }
                  setDeepNotice(false);
                  setThinkingLevel("deep");
                  if (activeId) {
                    void api.setChatThinking(activeId, "deep").catch(() => {});
                    qc.setQueryData<Chat[]>(["chats"], (prev) =>
                      prev?.map((c) => (c.id === activeId ? { ...c, thinking_level: "deep" } : c)),
                    );
                  }
                }}
                className="rounded-lg bg-brand px-4 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90"
              >
                Yes, enable
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
    </ExecContext.Provider>
  );
}

/** Trash overlay: lists soft-deleted (archived) chats. Each can be restored to the
 * active list or permanently deleted; "Empty trash" purges everything at once. */
function TrashPanel({
  chats,
  onClose,
  onRestore,
  onPurge,
  onEmpty,
}: {
  chats: Chat[];
  onClose: () => void;
  onRestore: (id: string) => void;
  onPurge: (id: string) => void;
  onEmpty: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/30 p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-[80vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b px-5 py-3.5">
          <div className="flex items-center gap-2 text-sm font-semibold text-gray-800">
            <TrashIcon className="h-[18px] w-[18px]" />
            Trash
            {chats.length > 0 && (
              <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">
                {chats.length}
              </span>
            )}
          </div>
          <button onClick={onClose} className="text-gray-400 transition hover:text-gray-700">
            ✕
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
          {chats.length === 0 ? (
            <div className="px-3 py-10 text-center text-sm text-gray-400">
              Trash is empty. Deleted chats appear here and can be restored.
            </div>
          ) : (
            chats.map((c) => (
              <div
                key={c.id}
                className="group flex items-center justify-between gap-3 rounded-lg px-3 py-2 hover:bg-gray-50"
              >
                <div className="min-w-0">
                  <div className="truncate text-sm text-gray-800">{c.title || "Untitled chat"}</div>
                  <div className="text-[11px] text-gray-400">{formatTimestamp(c.created_at)}</div>
                </div>
                <div className="flex shrink-0 items-center gap-1.5 text-xs">
                  <button
                    onClick={() => onRestore(c.id)}
                    className="rounded-md border border-gray-200 px-2 py-1 text-gray-600 transition hover:border-brand/40 hover:text-brand"
                  >
                    Restore
                  </button>
                  <button
                    onClick={() => onPurge(c.id)}
                    title="Delete permanently"
                    className="rounded-md border border-red-200 px-2 py-1 text-red-600 transition hover:bg-red-50"
                  >
                    Delete
                  </button>
                </div>
              </div>
            ))
          )}
        </div>

        {chats.length > 0 && (
          <div className="flex items-center justify-between border-t px-5 py-3">
            <span className="text-[11px] text-gray-400">
              Permanently deleted items can&rsquo;t be recovered.
            </span>
            <button
              onClick={() => {
                if (window.confirm("Permanently delete everything in Trash? This cannot be undone.")) {
                  onEmpty();
                }
              }}
              className="rounded-lg border border-red-200 px-3 py-1.5 text-sm font-medium text-red-600 transition hover:bg-red-50"
            >
              Empty trash
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/** Reasoning-effort selector: Normal vs Deep Investigation. The choice persists on
 * the chat and stays on for subsequent messages, like Azure SRE Agent's mode. */
function ThinkingPicker({
  level,
  onChange,
}: {
  level: "normal" | "deep";
  onChange: (level: "normal" | "deep") => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);
  const deep = level === "deep";
  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title="Reasoning effort"
        className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1.5 text-xs font-medium transition ${
          deep
            ? "border-brand bg-brand/10 text-brand"
            : "border-gray-200 text-gray-600 hover:bg-gray-100"
        }`}
      >
        {deep ? (
          <SparkleIcon className="h-3.5 w-3.5 text-brand" />
        ) : (
          <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
            <circle cx="10" cy="10" r="6.5" />
            <path d="M10 6.5v4l2.5 1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        )}
        <span>{deep ? "Deep investigation" : "Normal Mode"}</span>
        <svg className="h-3 w-3 text-gray-400" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M6 8l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && (
        <div className="absolute bottom-full left-0 z-30 mb-1.5 w-80 overflow-hidden rounded-xl border border-gray-200 bg-white shadow-lg">
          {([
            [
              "normal",
              "Normal Mode",
              "Fast, single-turn answer. The agent reads your question, calls a few tools if it needs live Azure data, and replies in seconds. Best for quick lookups, how-to questions, and well-scoped tasks.",
              null,
            ],
            [
              "deep",
              "Deep investigation",
              "Multi-agent root-cause analysis. Plans the work, dispatches a war room of specialist agents (network, identity, cost, security, …) in parallel, forms hypotheses, validates each against your live Azure evidence, and writes up a ranked verdict with citations. Best for incidents, audits, and anything that needs proof. Takes several minutes.",
              "✨",
            ],
          ] as const).map(([val, label, desc, badge]) => (
            <button
              key={val}
              onClick={() => {
                setOpen(false);
                onChange(val);
              }}
              className={`flex w-full items-start gap-2 px-3 py-2.5 text-left transition hover:bg-gray-50 ${
                level === val ? "bg-brand/5" : ""
              }`}
            >
              <span className="mt-0.5 w-4 shrink-0 text-brand">{level === val ? "✓" : ""}</span>
              <span className="min-w-0">
                <span className="block text-sm font-medium text-gray-800">
                  {label} {badge && <span>{badge}</span>}
                </span>
                <span className="block text-[11px] text-gray-500">{desc}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Custom-agent selector for the composer: run the chat as a saved custom agent
 * (persona + tools + model) or as the default assistant. Sits next to the reasoning
 * selector; the choice persists on the chat like the model picker. */
function AgentPicker({
  agents,
  selectedId,
  onChange,
}: {
  agents: CustomAgent[];
  selectedId: string | null;
  onChange: (id: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);
  const selected = agents.find((a) => a.id === selectedId) ?? null;
  const active = !!selected;
  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title="Run as a sub agent"
        className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1.5 text-xs font-medium transition ${
          active
            ? "border-brand bg-brand/10 text-brand"
            : "border-gray-200 text-gray-600 hover:bg-gray-100"
        }`}
      >
        <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
          <rect x="4" y="6" width="12" height="9" rx="2" />
          <path d="M10 3v3M7.5 10.5h.01M12.5 10.5h.01" strokeLinecap="round" />
        </svg>
        <span className="max-w-[140px] truncate">{selected ? selected.name : "Use built-in agents"}</span>
        <svg className="h-3 w-3 text-gray-400" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M6 8l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && (
        <div className="absolute bottom-full left-0 z-30 mb-1.5 max-h-80 w-72 overflow-y-auto rounded-xl border border-gray-200 bg-white shadow-lg">
          <button
            onClick={() => {
              setOpen(false);
              onChange(null);
            }}
            className={`flex w-full items-start gap-2 px-3 py-2.5 text-left transition hover:bg-gray-50 ${
              selectedId === null ? "bg-brand/5" : ""
            }`}
          >
            <span className="mt-0.5 w-4 shrink-0 text-brand">{selectedId === null ? "✓" : ""}</span>
            <span className="min-w-0">
              <span className="block text-sm font-medium text-gray-800">Use built-in agents</span>
              <span className="block text-[11px] text-gray-500">
                The standard Azure support assistant (no custom persona).
              </span>
            </span>
          </button>
          {agents.map((a) => (
            <button
              key={a.id}
              onClick={() => {
                setOpen(false);
                onChange(a.id);
              }}
              className={`flex w-full items-start gap-2 px-3 py-2.5 text-left transition hover:bg-gray-50 ${
                selectedId === a.id ? "bg-brand/5" : ""
              }`}
            >
              <span className="mt-0.5 w-4 shrink-0 text-brand">{selectedId === a.id ? "✓" : ""}</span>
              <span className="min-w-0">
                <span className="block text-sm font-medium text-gray-800">{a.name}</span>
                {a.instructions && (
                  <span className="block truncate text-[11px] text-gray-500">{a.instructions}</span>
                )}
                {a.model && (
                  <span className="mt-0.5 block text-[10px] uppercase tracking-wide text-gray-400">
                    {a.model}
                    {a.run_mode === "autonomous" ? " · autonomous" : ""}
                  </span>
                )}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Status pill colors for hypotheses (matches the SRE Agent semantics). */
function hypoPill(status: string): string {
  switch (status) {
    case "validated":
      return "bg-emerald-100 text-emerald-700 border-emerald-200";
    case "invalidated":
      return "bg-red-100 text-red-700 border-red-200";
    case "inconclusive":
      return "bg-amber-100 text-amber-700 border-amber-200";
    case "validating":
    default:
      return "bg-sky-100 text-sky-700 border-sky-200";
  }
}

function hypoDot(status: string): string {
  switch (status) {
    case "validated":
      return "bg-emerald-500";
    case "invalidated":
      return "bg-red-500";
    case "inconclusive":
      return "bg-amber-500";
    default:
      return "bg-sky-500 animate-pulse";
  }
}

/** One node in the interactive hypothesis tree; expands to show its evidence. */
function HypothesisTreeNode({
  node,
  childrenByParent,
  forceOpen,
}: {
  node: HypothesisNode;
  childrenByParent: Record<string, HypothesisNode[]>;
  forceOpen?: boolean;
}) {
  const kids = childrenByParent[node.id] ?? [];
  const hasKids = kids.length > 0;
  // Expand top-level + nodes with children by default; collapse deeper leaves. An
  // explicit forceOpen (from Expand all / Collapse all) overrides the default.
  const [open, setOpen] = useState(
    forceOpen !== undefined ? forceOpen : node.depth === 1 || hasKids,
  );
  const expandable = hasKids || !!node.evidence;
  return (
    <div className="relative">
      <button
        onClick={() => expandable && setOpen((o) => !o)}
        className={`flex w-full items-start gap-1.5 rounded-lg border border-gray-200 bg-white px-2 py-2 text-left transition hover:border-brand/40 hover:bg-gray-50 ${
          expandable ? "" : "cursor-default"
        }`}
      >
        <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center text-gray-400">
          {expandable ? (
            <svg
              className={`h-3 w-3 transition-transform ${open ? "rotate-90" : ""}`}
              viewBox="0 0 20 20"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
            >
              <path d="M7 5l5 5-5 5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          ) : null}
        </span>
        <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${hypoDot(node.status)}`} />
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-gray-800">
              {node.title}
            </span>
            {hasKids && (
              <span className="shrink-0 rounded-full bg-gray-100 px-1.5 text-[10px] text-gray-500">
                {kids.length}
              </span>
            )}
            <span className={`shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-medium capitalize ${hypoPill(node.status)}`}>
              {node.status}
            </span>
          </span>
          {node.description && (
            <span className="mt-0.5 block text-[11px] text-gray-500">{node.description}</span>
          )}
          {open && node.evidence && (
            <span className="mt-1.5 block rounded-md bg-gray-50 px-2 py-1.5 text-[11px] leading-relaxed text-gray-600">
              <span className="font-medium text-gray-500">Evidence: </span>
              {node.evidence}
            </span>
          )}
        </span>
      </button>
      {hasKids && open && (
        <div className="ml-3 mt-1 space-y-1 border-l border-gray-200 pl-3">
          {kids.map((k) => (
            <HypothesisTreeNode
              key={k.id}
              node={k}
              childrenByParent={childrenByParent}
              forceOpen={forceOpen}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** A resizable right-side panel width (px), persisted to localStorage. Returns the
 *  current width, a setter, and a splitter element to render on the panel's LEFT edge.
 *  Because the panel is a flex sibling of the main content (not an overlay), resizing
 *  splits the available space instead of covering the chat. */
function useResizablePanel(storageKey: string, defaultWidth: number) {
  const [width, setWidth] = useState<number>(() => {
    try {
      const v = parseInt(localStorage.getItem(storageKey) || "", 10);
      if (!Number.isNaN(v)) return v;
    } catch {
      /* ignore */
    }
    return defaultWidth;
  });
  const resizingRef = useRef(false);
  const widthRef = useRef(width);
  widthRef.current = width;

  useEffect(() => {
    function onMove(e: MouseEvent) {
      if (!resizingRef.current) return;
      // Handle is on the LEFT edge; width grows as the cursor moves left.
      const next = Math.max(320, Math.min(window.innerWidth - 360, window.innerWidth - e.clientX));
      setWidth(next);
    }
    function onUp() {
      if (!resizingRef.current) return;
      resizingRef.current = false;
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      try {
        localStorage.setItem(storageKey, String(Math.round(widthRef.current)));
      } catch {
        /* ignore */
      }
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [storageKey]);

  const reset = () => {
    setWidth(defaultWidth);
    try {
      localStorage.setItem(storageKey, String(defaultWidth));
    } catch {
      /* ignore */
    }
  };

  const handle = (
    <div
      onMouseDown={(e) => {
        e.preventDefault();
        resizingRef.current = true;
        document.body.style.userSelect = "none";
        document.body.style.cursor = "col-resize";
      }}
      onDoubleClick={reset}
      title="Drag to resize · double-click to reset"
      className="group absolute left-0 top-0 z-30 h-full w-1.5 -translate-x-1/2 cursor-col-resize"
    >
      <div className="mx-auto h-full w-px bg-gray-200 transition group-hover:w-0.5 group-hover:bg-brand/50" />
    </div>
  );

  return { width, handle };
}

/** Live "war room" card for one specialist agent: shows its live tool activity while
 * working and rolls up to a confirmed/ruled-out/inconclusive verdict when done. */
function WarRoomAgentCard({
  agent,
  hyps,
  activity,
  live,
}: {
  agent: DeepAgent;
  hyps: HypothesisNode[];
  activity?: { tool?: string; busy?: boolean; tools?: number; startedAt?: number };
  live: boolean;
}) {
  const validated = hyps.filter((h) => h.status === "validated").length;
  const invalidated = hyps.filter((h) => h.status === "invalidated").length;
  const inconclusive = hyps.filter((h) => h.status === "inconclusive").length;
  const validating = hyps.filter((h) => h.status === "validating").length;
  const busy = live && (validating > 0 || activity?.busy);

  // Overall card accent + verdict.
  let accent = "border-gray-200 bg-white";
  let dot = "bg-gray-300";
  let verdict = "Idle";
  if (busy) {
    accent = "border-sky-200 bg-sky-50/40";
    dot = "bg-sky-500 animate-pulse";
    verdict = "Working";
  } else if (validated > 0) {
    accent = "border-emerald-200 bg-emerald-50/40";
    dot = "bg-emerald-500";
    verdict = "Confirmed";
  } else if (hyps.length > 0 && invalidated === hyps.length) {
    accent = "border-red-200 bg-red-50/30";
    dot = "bg-red-500";
    verdict = "Ruled out";
  } else if (inconclusive > 0) {
    accent = "border-amber-200 bg-amber-50/40";
    dot = "bg-amber-500";
    verdict = "Inconclusive";
  } else if (hyps.length > 0) {
    verdict = "Pending";
  }

  // Activity / summary line.
  let line: string;
  if (busy && activity?.tool) line = `🔧 ${activity.tool}`;
  else if (busy && hyps.length === 0) line = "Researching…";
  else if (busy) line = "Validating hypothesis…";
  else if (hyps.length === 0) line = agent.domain;
  else {
    const bits: string[] = [];
    if (validated) bits.push(`${validated} confirmed`);
    if (invalidated) bits.push(`${invalidated} ruled out`);
    if (inconclusive) bits.push(`${inconclusive} inconclusive`);
    line = bits.join(" · ") || agent.domain;
  }

  return (
    <div className={`flex items-start gap-2 rounded-lg border p-2 transition ${accent}`}>
      <span className="relative mt-0.5 text-base leading-none">
        {agent.icon}
        <span className={`absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full ${dot}`} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-[12px] font-semibold text-gray-800">{agent.name}</span>
          {activity?.tools ? (
            <span className="shrink-0 rounded-full bg-gray-100 px-1.5 text-[9px] text-gray-500">
              {activity.tools} {activity.tools === 1 ? "check" : "checks"}
            </span>
          ) : null}
        </span>
        <span className="mt-0.5 block truncate text-[11px] text-gray-500">{line}</span>
      </span>
      <span
        className={`mt-0.5 shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-medium ${
          verdict === "Confirmed"
            ? "bg-emerald-100 text-emerald-700"
            : verdict === "Ruled out"
              ? "bg-red-100 text-red-700"
              : verdict === "Inconclusive"
                ? "bg-amber-100 text-amber-700"
                : verdict === "Working"
                  ? "bg-sky-100 text-sky-700"
                  : "bg-gray-100 text-gray-500"
        }`}
      >
        {verdict}
      </span>
    </div>
  );
}

/** Confidence (0–100) heuristic for an investigation, mirroring the backend:
 * leans on the strongest validated hypothesis, floored at 50% once a concrete root
 * cause is concluded. Pure — derived from the hypothesis tree already in state. */
function investigationConfidence(
  hyps: HypothesisNode[],
  conclusion?: InvestigationConclusion | null,
): number {
  const weightFor = (s: string): number =>
    s === "validated" ? 1 : s === "inconclusive" ? 0.4 : s === "validating" ? 0.3 : s === "invalidated" ? 0 : 0.2;
  const rootCause = (conclusion?.root_cause || "").trim();
  if (hyps.length === 0) return rootCause ? 55 : 0;
  const weights = hyps.map((h) => weightFor(h.status));
  const best = Math.max(...weights);
  const avg = weights.reduce((a, b) => a + b, 0) / weights.length;
  let score = best * 0.75 + avg * 0.25;
  if (rootCause) score = Math.max(score, 0.5);
  return Math.max(0, Math.min(100, Math.round(score * 100)));
}

/** Right-side drawer rendering a deep-investigation: phase timeline, the branching
 * hypothesis tree with colored status pills, and the structured conclusion. */
function InvestigationPanel({
  investigation,
  live,
  pinned,
  onTogglePin,
  onClose,
}: {
  investigation: Investigation;
  live?: boolean;
  pinned?: boolean;
  onTogglePin?: () => void;
  onClose: () => void;
}) {
  // Expand/collapse-all: bumping `v` remounts the tree with the chosen default state.
  const [treeMode, setTreeMode] = useState<{ open: boolean; v: number }>({ open: true, v: 0 });
  // Resizable, non-overlapping split panel: resizing shrinks the chat instead of covering it.
  const { width, handle } = useResizablePanel(INVESTIGATION_WIDTH_KEY, 420);

  const hyps = investigation.hypotheses ?? [];
  const childrenByParent: Record<string, HypothesisNode[]> = {};
  const roots: HypothesisNode[] = [];
  for (const h of hyps) {
    if (h.parent_id) (childrenByParent[h.parent_id] ??= []).push(h);
    else roots.push(h);
  }
  const PHASES: { key: string; label: string }[] = [
    { key: "research", label: "Incident research" },
    { key: "hypotheses", label: "Forming hypotheses" },
    { key: "validation", label: "Validating hypotheses" },
    { key: "conclusion", label: "Conclusion" },
  ];
  const reached = new Set((investigation.phases ?? []).map((p) => p.phase));
  const conclusion = investigation.conclusion;
  const validatingCount = hyps.filter((h) => h.status === "validating").length;
  const researchSummary =
    investigation.research ||
    (investigation.phases ?? []).find((p) => p.phase === "research")?.summary ||
    "";

  return (
    <div
      className="relative flex h-full shrink-0 flex-col border-l border-gray-200 bg-white"
      style={{ width }}
    >
      {/* Left-edge splitter to resize the panel (splits the chat, no overlap). */}
      {handle}
      <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
        <div className="flex items-center gap-2">
          <SparkleIcon className="h-4 w-4 text-brand" />
          <span className="text-sm font-semibold text-gray-800">Deep investigation</span>
          {live && validatingCount > 1 ? (
            <span className="flex items-center gap-1 rounded-full bg-sky-100 px-2 py-0.5 text-[10px] font-medium text-sky-700" title="Sub-agents validating hypotheses in parallel">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-500" />
              {validatingCount} sub-agents
            </span>
          ) : (
            live && (
              <span className="flex items-center gap-1 rounded-full bg-sky-100 px-2 py-0.5 text-[10px] font-medium text-sky-700">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-500" />
                Running
              </span>
            )
          )}
        </div>
        <div className="flex items-center gap-1">
          {onTogglePin && (
            <button
              onClick={onTogglePin}
              title={
                pinned
                  ? "Unpin — stop auto-opening this panel when switching chats"
                  : "Pin — keep this panel open and auto-reopen it when switching chats"
              }
              aria-label={pinned ? "Unpin panel" : "Pin panel"}
              aria-pressed={pinned}
              className={`rounded-lg p-1.5 transition ${
                pinned
                  ? "bg-brand/10 text-brand hover:bg-brand/20"
                  : "text-gray-400 hover:bg-gray-100 hover:text-gray-700"
              }`}
            >
              <svg
                className="h-4 w-4"
                viewBox="0 0 20 20"
                fill={pinned ? "currentColor" : "none"}
                stroke="currentColor"
                strokeWidth="1.5"
              >
                <path
                  d="M12.5 2.5l5 5-3 1-3.5 3.5-.5 4-2-2-3.5 3.5M7.5 12.5l-4-4 3.5-3.5 4-.5L14.5 1"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          )}
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-gray-400 transition hover:bg-gray-100 hover:text-gray-700"
            aria-label="Close"
          >
            <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
              <path d="M5 5l10 10M15 5L5 15" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        {/* War room — specialist agent roster (when agents were dispatched) */}
        {(investigation.agents?.length ?? 0) > 0 && (
          <div>
            <div className="mb-1.5 flex items-center gap-1.5">
              <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
                War room
              </span>
              <span className="rounded-full bg-gray-100 px-1.5 text-[10px] text-gray-500">
                {investigation.agents!.length} agents
              </span>
            </div>
            <div className="grid grid-cols-1 gap-1.5">
              {investigation.agents!.map((a) => (
                <WarRoomAgentCard
                  key={a.id}
                  agent={a}
                  hyps={hyps.filter((h) => h.agent === a.id)}
                  activity={investigation.agentActivity?.[a.id]}
                  live={!!live}
                />
              ))}
            </div>
          </div>
        )}

        {/* Phase timeline */}
        <ol className="space-y-1.5">
          {PHASES.map((p) => {
            const done = reached.has(p.key);
            const isLast = !reached.has(PHASES[PHASES.indexOf(p) + 1]?.key);
            const active = live && done && isLast;
            return (
              <li key={p.key} className="flex items-center gap-2 text-xs">
                <span
                  className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[9px] ${
                    done ? "bg-brand text-white" : "bg-gray-200 text-gray-400"
                  }`}
                >
                  {active ? "•" : done ? "✓" : ""}
                </span>
                <span className={done ? "font-medium text-gray-700" : "text-gray-400"}>{p.label}</span>
              </li>
            );
          })}
        </ol>

        {/* Research summary */}
        {researchSummary && (
          <details className="rounded-lg border border-gray-200 bg-gray-50/60 p-2.5" open={!conclusion}>
            <summary className="cursor-pointer text-[11px] font-semibold uppercase tracking-wide text-gray-400">
              Research findings
            </summary>
            <p className="mt-1.5 whitespace-pre-wrap text-[12px] leading-relaxed text-gray-600">
              {researchSummary}
            </p>
          </details>
        )}

        {/* Hypothesis tree */}
        {roots.length > 0 && (
          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
                Hypotheses
              </span>
              <div className="flex items-center gap-1 text-[10px]">
                <button
                  onClick={() => setTreeMode({ open: true, v: treeMode.v + 1 })}
                  className="rounded px-1.5 py-0.5 text-gray-500 transition hover:bg-gray-100"
                >
                  Expand all
                </button>
                <button
                  onClick={() => setTreeMode({ open: false, v: treeMode.v + 1 })}
                  className="rounded px-1.5 py-0.5 text-gray-500 transition hover:bg-gray-100"
                >
                  Collapse all
                </button>
              </div>
            </div>
            <div className="space-y-1.5">
              {roots.map((r) => (
                <HypothesisTreeNode
                  key={`${r.id}-${treeMode.v}`}
                  node={r}
                  childrenByParent={childrenByParent}
                  forceOpen={treeMode.v > 0 ? treeMode.open : undefined}
                />
              ))}
            </div>
          </div>
        )}

        {/* Conclusion */}
        {conclusion && (
          <div className="rounded-xl border border-emerald-200 bg-emerald-50/60 p-3">
            <div className="mb-1 flex items-center justify-between">
              <div className="text-[11px] font-semibold uppercase tracking-wide text-emerald-700">
                Conclusion
              </div>
              {(() => {
                const conf = investigationConfidence(hyps, conclusion);
                const band = conf >= 75 ? "bg-emerald-500" : conf >= 50 ? "bg-amber-500" : "bg-orange-500";
                return (
                  <div className="flex items-center gap-1.5" title="Heuristic confidence from how the hypotheses resolved">
                    <span className="h-1.5 w-12 overflow-hidden rounded-full bg-emerald-100">
                      <span className={`block h-full ${band}`} style={{ width: `${conf}%` }} />
                    </span>
                    <span className="text-[11px] font-semibold tabular-nums text-emerald-700">{conf}%</span>
                  </div>
                );
              })()}
            </div>
            <div className="text-sm font-semibold text-gray-800">{conclusion.root_cause}</div>
            {conclusion.summary && (
              <p className="mt-1 text-[12px] leading-relaxed text-gray-600">{conclusion.summary}</p>
            )}
            {conclusion.evidence?.length > 0 && (
              <div className="mt-2">
                <div className="text-[11px] font-medium text-gray-500">Evidence</div>
                <ul className="mt-0.5 list-disc pl-4 text-[12px] text-gray-600">
                  {conclusion.evidence.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </div>
            )}
            {conclusion.actions?.length > 0 && (
              <div className="mt-2">
                <div className="text-[11px] font-medium text-gray-500">Recommended actions</div>
                <ul className="mt-0.5 list-disc pl-4 text-[12px] text-gray-600">
                  {conclusion.actions.map((a, i) => (
                    <li key={i}>{a}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        {roots.length === 0 && !conclusion && live && (
          <div className="flex items-center gap-2 text-xs text-gray-400">
            <Spinner className="h-3.5 w-3.5 text-gray-400" />
            Gathering context and forming hypotheses…
          </div>
        )}
      </div>
    </div>
  );
}

/** Tenant (Azure connection) selector for the composer. Lets the user choose which
 * connected Azure tenant a prompt runs against; the choice persists on the chat. */
function WorkloadPicker({
  selectedId,
  onChange,
}: {
  selectedId: string | null;
  onChange: (id: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const wlQ = useQuery({ queryKey: ["workloads"], queryFn: api.workloads });
  const workloads = wlQ.data?.workloads ?? [];
  const active = workloads.find((w) => w.id === selectedId);

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    if (open) document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  // Hide entirely when there are no workloads to choose from.
  if (workloads.length === 0) return null;

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title="Scope this chat to an Azure Workload"
        className={`flex h-9 items-center gap-1.5 rounded-full px-3 text-xs font-medium transition hover:bg-gray-100 ${
          active ? "text-brand" : "text-gray-600"
        }`}
      >
        <span className="text-[13px]">🧱</span>
        <span className="max-w-[140px] truncate">{active?.name ?? "No workload"}</span>
        <svg className="h-3 w-3 text-gray-400" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M6 8l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && (
        <div className="absolute bottom-11 left-0 z-20 w-64 rounded-xl border bg-white p-1.5 shadow-lg">
          <div className="px-2 py-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">
            Scope to workload
          </div>
          <button
            onClick={() => {
              onChange(null);
              setOpen(false);
            }}
            className={`flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm transition hover:bg-gray-100 ${
              !selectedId ? "bg-brand/5 text-brand" : "text-gray-700"
            }`}
          >
            No workload (full tenant)
          </button>
          {workloads.map((w) => (
            <button
              key={w.id}
              onClick={() => {
                onChange(w.id);
                setOpen(false);
              }}
              className={`flex w-full items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-left text-sm transition hover:bg-gray-100 ${
                w.id === selectedId ? "bg-brand/5 text-brand" : "text-gray-700"
              }`}
            >
              <span className="min-w-0">
                <span className="block truncate font-medium">{w.name}</span>
                <span className="block truncate text-[11px] text-gray-400">
                  {w.nodes.length} scope node{w.nodes.length === 1 ? "" : "s"}
                </span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function TenantPicker({
  tenants,
  activeId,
  onPick,
}: {
  tenants: TenantOption[];
  activeId?: string;
  onPick: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const active = tenants.find((t) => t.id === activeId);

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    if (open) document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title="Azure tenant for this chat"
        className="flex h-9 items-center gap-1.5 rounded-full px-3 text-xs font-medium text-gray-600 transition hover:bg-gray-100"
      >
        <span className="text-[13px]">🏢</span>
        <span className="max-w-[140px] truncate">
          {active?.display_name ?? "Select tenant"}
        </span>
        <svg className="h-3 w-3 text-gray-400" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M6 8l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && (
        <div className="absolute bottom-11 left-0 z-20 w-64 rounded-xl border bg-white p-1.5 shadow-lg">
          <div className="px-2 py-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">
            Azure tenant
          </div>
          {tenants.map((t) => (
            <button
              key={t.id}
              onClick={() => {
                onPick(t.id);
                setOpen(false);
              }}
              className={`flex w-full items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-left text-sm transition hover:bg-gray-100 ${
                t.id === activeId ? "bg-brand/5 text-brand" : "text-gray-700"
              }`}
            >
              <span className="min-w-0">
                <span className="block truncate font-medium">{t.display_name}</span>
                <span className="block truncate text-[11px] text-gray-400">{t.tenant_id}</span>
              </span>
              <span className="flex shrink-0 items-center gap-1">
                {t.read_only && (
                  <span className="rounded bg-green-100 px-1 py-0.5 text-[9px] text-green-700">
                    RO
                  </span>
                )}
                <span
                  className={`inline-block h-2 w-2 rounded-full ${
                    t.status === "ok"
                      ? "bg-green-500"
                      : t.status === "error"
                        ? "bg-red-500"
                        : "bg-gray-300"
                  }`}
                />
              </span>
            </button>
          ))}
          <div className="mt-1 border-t px-2 pt-1.5 text-[11px] text-gray-400">
            Manage tenants in Settings → Azure Tenants
          </div>
        </div>
      )}
    </div>
  );
}

/** Live or collapsed timeline of the agent's reasoning + tool calls. */
const ActivityPane = memo(function ActivityPane({ steps, live }: { steps: Step[]; live: boolean }) {
  // Keep the pane expanded by default — both while live and after completion — so the
  // agent's thinking and tool steps persist instead of vanishing the moment the turn
  // ends. The user can collapse it manually via the header.
  const [open, setOpen] = useState(true);

  const toolCount = steps.filter((s) => s.kind === "tool").length;
  const errorSteps = steps.filter(
    (s): s is Extract<Step, { kind: "tool" }> => s.kind === "tool" && s.status === "error",
  );
  const header = live
    ? "Working…"
    : `Thinking Process · ${toolCount} Step${toolCount === 1 ? "" : "s"}`;

  return (
    <div className="rounded-lg border border-gray-200 bg-gray-50/70">
      {/* Prominent, always-visible failure banner — a tool (e.g. a Sandbox VM) errored, so
          surface it even while the step detail below stays collapsed. */}
      {errorSteps.length > 0 && (
        <div className="m-2 rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-800">
          <div className="flex items-center gap-1.5 font-semibold">
            <span aria-hidden>⚠️</span>
            {errorSteps.length === 1
              ? `Tool failed: ${errorSteps[0].name}`
              : `${errorSteps.length} tools failed`}
          </div>
          {errorSteps.map((s, i) => (
            <div key={i} className="mt-1 break-words text-[11px] leading-snug text-red-700">
              <span className="font-mono font-medium">{s.name}</span>
              {s.summary ? ` — ${s.summary.replace(/^Error:\s*/i, "")}` : ""}
            </div>
          ))}
        </div>
      )}
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-gray-600"
      >
        {live ? (
          <span className="flex gap-0.5">
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand [animation-delay:-0.3s]" />
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand [animation-delay:-0.15s]" />
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand" />
          </span>
        ) : (
          <span className="text-green-600">✓</span>
        )}
        <span>{header}</span>
        <span className="ml-auto text-gray-400">{open ? "▲" : "▼ details"}</span>
      </button>

      {open && (
        <div className="space-y-2 px-3 pb-3">
          {steps.map((s, i) =>
            s.kind === "reasoning" ? (
              <div key={i} className="border-l-2 border-gray-300 pl-3 text-xs text-gray-600">
                <div className="prose-chat">
                  <Markdown>{s.text}</Markdown>
                </div>
              </div>
            ) : (
              <div key={i} className={`rounded border px-3 py-2 text-xs ${s.status === "error" ? "border-red-300 bg-red-50" : "border-gray-200 bg-white"}`}>
                <div className="flex items-center gap-2">
                  {s.status === "running" && live ? (
                    <Spinner />
                  ) : s.status === "running" ? (
                    <span className="text-gray-300">◦</span>
                  ) : s.status === "error" ? (
                    <span className="text-red-600">✗</span>
                  ) : (
                    <span className="text-green-600">✓</span>
                  )}
                  <span className="font-mono font-medium text-gray-800">{s.name}</span>
                  {s.status === "error" && (
                    <span className="rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-700">failed</span>
                  )}
                  {s.duration != null && (
                    <span className="text-gray-400">{formatDuration(s.duration)}</span>
                  )}
                </div>
                {s.args != null && Object.keys(s.args as object).length > 0 && (
                  <pre className="mt-1 overflow-x-auto rounded bg-gray-50 px-2 py-1 text-[11px] text-gray-500">
                    {JSON.stringify(s.args)}
                  </pre>
                )}
                {s.summary && (
                  <div className={`mt-1 ${s.status === "error" ? "text-red-700" : "text-gray-600"}`}>{s.summary}</div>
                )}
              </div>
            ),
          )}
        </div>
      )}
    </div>
  );
});

const PROVIDER_LABELS: Record<string, string> = {
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

// Providers whose credential is a stored API key (so "needs setup" applies if absent).
// OAuth/local providers are usable without a stored key, so they're excluded.
const KEY_BASED_PROVIDERS = new Set([
  "openai",
  "github",
  "azure_openai",
  "azure_foundry",
  "claude",
  "gemini",
  "grok",
  "mistral",
  "openrouter",
]);

const RECENT_MODELS_KEY = "azsup.recentModels.v1";

type RecentModel = { provider: string; model: string };

function loadRecentModels(): RecentModel[] {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_MODELS_KEY) || "[]");
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

function pushRecentModel(provider: string, model: string): RecentModel[] {
  const list = loadRecentModels().filter(
    (r) => !(r.provider === provider && r.model === model),
  );
  list.unshift({ provider, model });
  const capped = list.slice(0, 5);
  try {
    localStorage.setItem(RECENT_MODELS_KEY, JSON.stringify(capped));
  } catch {
    /* ignore quota */
  }
  return capped;
}

/** Claude-style model picker: recently used, providers › models flyout, and Configure…. */
function ModelPicker({
  provider,
  model,
  onPick,
  dropUp,
  variant = "pill",
}: {
  provider?: string | null;
  model?: string | null;
  onPick?: (provider: string, model: string) => void;
  dropUp?: boolean;
  variant?: "pill" | "bare" | "retry";
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const cfg = useQuery({ queryKey: ["llmConfig"], queryFn: api.llmConfig });
  const [open, setOpen] = useState(false);
  const [flyout, setFlyout] = useState<string | null>(null);
  const [models, setModels] = useState<Record<string, string[]>>({});
  const [loading, setLoading] = useState<string | null>(null);
  const [recent, setRecent] = useState<RecentModel[]>(loadRecentModels);
  const [busy, setBusy] = useState(false);

  const label = (provider && PROVIDER_LABELS[provider]) || provider || "Select model";

  // Providers known to the backend, in our preferred display order. Hide providers
  // that still need credential setup or have been disabled in admin — the user can
  // still add/enable them via Configure…
  const orderedIds = Object.keys(PROVIDER_LABELS).filter(
    (id) => cfg.data?.providers[id] && !needsSetup(id) && !cfg.data?.providers[id]?.disabled,
  );

  // Recently used, excluding models whose provider is now hidden (disabled/needs
  // setup). Show only the last 3 most-recently-used.
  const visibleRecent = recent
    .filter((r) => orderedIds.includes(r.provider))
    .slice(0, 3);

  async function loadModels(pid: string) {
    if (models[pid]) return;
    setLoading(pid);
    try {
      const res = await api.llmModels(pid);
      setModels((m) => ({ ...m, [pid]: res.models }));
    } catch {
      /* ignore */
    } finally {
      setLoading(null);
    }
  }

  async function pick(pid: string, m: string) {
    setBusy(true);
    try {
      // Apply the chosen model via the parent. An active chat keeps it as its OWN
      // per-chat model; the welcome screen (no chat yet) sets the global default so the
      // next new chat inherits it. Picking a model never silently changes the global
      // default mid-conversation — that is only set explicitly (on the welcome screen,
      // or in Settings → AI Providers → "Set as default").
      onPick?.(pid, m);
      setRecent(pushRecentModel(pid, m));
      qc.invalidateQueries({ queryKey: ["activeLlm"] });
      qc.invalidateQueries({ queryKey: ["llmConfig"] });
      qc.invalidateQueries({ queryKey: ["chats"] });
    } catch {
      /* ignore */
    } finally {
      setBusy(false);
      setOpen(false);
      setFlyout(null);
    }
  }

  function needsSetup(pid: string): boolean {
    return KEY_BASED_PROVIDERS.has(pid) && !cfg.data?.providers[pid]?.has_key;
  }

  return (
    <div className="relative">
      {variant === "retry" ? (
        <button
          onClick={() => setOpen((o) => !o)}
          className="flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-gray-500 transition hover:bg-gray-100 hover:text-gray-700"
          title="Retry with a different model"
        >
          {busy ? (
            <span className="animate-spin">⟳</span>
          ) : (
            <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M15.3 8A6 6 0 104 10" strokeLinecap="round" />
              <path d="M15.5 4v4h-4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          )}
          <svg className="h-3 w-3 text-gray-400" viewBox="0 0 20 20" fill="currentColor">
            <path
              fillRule="evenodd"
              d="M5.23 7.21a.75.75 0 011.06.02L10 11.17l3.71-3.94a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z"
              clipRule="evenodd"
            />
          </svg>
        </button>
      ) : variant === "bare" ? (
        <button
          onClick={() => setOpen((o) => !o)}
          className="flex max-w-[14rem] items-center gap-1.5 rounded-md px-2 py-1 text-sm text-gray-700 transition hover:bg-gray-100"
          title={`Switch model${model ? ` (current: ${model})` : ""}`}
        >
          <span className="truncate font-medium text-gray-800">{model || label}</span>
          {model && <span className="shrink-0 text-gray-400">{label}</span>}
          {busy ? (
            <span className="shrink-0 animate-spin text-gray-400">⟳</span>
          ) : (
            <svg className="h-4 w-4 shrink-0 text-gray-400" viewBox="0 0 20 20" fill="currentColor">
              <path
                fillRule="evenodd"
                d="M5.23 7.21a.75.75 0 011.06.02L10 11.17l3.71-3.94a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z"
                clipRule="evenodd"
              />
            </svg>
          )}
        </button>
      ) : (
        <button
          onClick={() => setOpen((o) => !o)}
          className="flex items-center gap-1.5 rounded-full border border-gray-200 bg-gray-50 px-3 py-1 text-xs text-gray-600 transition hover:bg-gray-100"
          title="Switch model"
        >
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
          <span className="font-medium text-gray-700">{label}</span>
          {model && <span className="text-gray-400">·</span>}
          {model && <span className="font-mono text-gray-500">{model}</span>}
          {busy ? (
            <span className="ml-0.5 animate-spin text-gray-400">⟳</span>
          ) : (
            <svg className="ml-0.5 h-3 w-3 text-gray-400" viewBox="0 0 20 20" fill="currentColor">
              <path
                fillRule="evenodd"
                d="M5.23 7.21a.75.75 0 011.06.02L10 11.17l3.71-3.94a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z"
                clipRule="evenodd"
              />
            </svg>
          )}
        </button>
      )}

      {open && (
        <>
          <div
            className="fixed inset-0 z-40"
            onClick={() => {
              setOpen(false);
              setFlyout(null);
            }}
          />
          <div
            className={`absolute z-50 w-60 rounded-xl border border-gray-200 bg-white py-1 text-[13px] shadow-xl ${
              variant === "retry" ? "left-0" : "right-0"
            } ${dropUp ? "bottom-full mb-1.5" : "mt-1.5"}`}
            onMouseLeave={() => setFlyout(null)}
          >
            {variant === "retry" && (
              <>
                <button
                  onClick={() => {
                    setOpen(false);
                    setFlyout(null);
                    if (provider && model) void pick(provider, model);
                  }}
                  className="flex w-full items-center gap-2 px-2.5 py-1 text-left hover:bg-gray-100"
                >
                  <svg className="h-3.5 w-3.5 text-gray-500" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
                    <path d="M15.3 8A6 6 0 104 10" strokeLinecap="round" />
                    <path d="M15.5 4v4h-4" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  <span className="min-w-0">
                    <span className="text-gray-800">Try again</span>
                    {model && (
                      <span className="ml-2 text-[11px] text-gray-400">{model}</span>
                    )}
                  </span>
                </button>
                <div className="my-1 border-t border-gray-100" />
              </>
            )}
            {visibleRecent.length > 0 && (
              <>
                <div className="px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                  Recently used
                </div>
                {visibleRecent.map((r) => (
                  <button
                    key={`${r.provider}:${r.model}`}
                    onClick={() => void pick(r.provider, r.model)}
                    className="flex w-full items-center justify-between gap-2 px-2.5 py-1 text-left hover:bg-gray-100"
                  >
                    <span className="flex min-w-0 items-baseline gap-2">
                      <span className="truncate font-mono text-gray-800">{r.model}</span>
                      <span className="shrink-0 text-[11px] text-gray-400">
                        {PROVIDER_LABELS[r.provider] ?? r.provider}
                      </span>
                    </span>
                    {provider === r.provider && model === r.model && (
                      <span className="shrink-0 text-brand">✓</span>
                    )}
                  </button>
                ))}
                <div className="my-1 border-t border-gray-100" />
              </>
            )}

            <div className="px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
              Providers
            </div>
            {orderedIds.map((pid) => (
              <div
                key={pid}
                className="relative"
                onMouseEnter={() => {
                  setFlyout(pid);
                  void loadModels(pid);
                }}
              >
                <button className="flex w-full items-center justify-between gap-2 px-2.5 py-1 text-left hover:bg-gray-100">
                  <span className="flex items-center gap-1.5">
                    <span className="text-gray-800">{PROVIDER_LABELS[pid]}</span>
                  </span>
                  <span className="flex items-center gap-1.5 text-gray-400">
                    {provider === pid && <span className="text-brand">✓</span>}
                    ›
                  </span>
                </button>
                {flyout === pid && (
                  <div className={`absolute top-0 z-50 max-h-80 w-52 overflow-y-auto rounded-xl border border-gray-200 bg-white py-1 shadow-xl ${variant === "retry" ? "left-full" : "right-full"}`}>
                    {loading === pid && (
                      <div className="px-2.5 py-1.5 text-xs text-gray-400">Loading…</div>
                    )}
                    {(models[pid] ?? []).map((m) => (
                      <button
                        key={m}
                        onClick={() => void pick(pid, m)}
                        className="flex w-full items-center justify-between gap-2 px-2.5 py-1 text-left hover:bg-gray-100"
                      >
                        <span className="truncate font-mono text-xs text-gray-800">{m}</span>
                        {provider === pid && model === m && (
                          <span className="shrink-0 text-brand">✓</span>
                        )}
                      </button>
                    ))}
                    {loading !== pid && (models[pid]?.length ?? 0) === 0 && (
                      <div className="px-2.5 py-1.5 text-xs text-gray-400">No models.</div>
                    )}
                  </div>
                )}
              </div>
            ))}

            <div className="my-1 border-t border-gray-100" />
            <button
              onClick={() => {
                setOpen(false);
                navigate("/admin/providers");
              }}
              className="flex w-full items-center gap-2 px-2.5 py-1 text-left text-gray-600 hover:bg-gray-100"
            >
              <span>⚙️</span> Configure…
            </button>
          </div>
        </>
      )}
    </div>
  );
}


// Sidebar line icons, Spinner, and other pure presentational icons live in
// ./chat/icons (imported at the top of this file).

/** Shared renderer for the progress feed lines (used live and in history).
 *
 * `level` controls how much of the agent's work is shown:
 *   compact  — only high-level phases (sending / responding / writing the answer)
 *   normal   — phases + tool names + result summaries (parameters & reasoning hidden)
 *   detailed — everything, including reasoning blocks and full tool parameters
 */
type ProgressLevel = "compact" | "normal" | "detailed";

function filterProgress(log: LogLine[], level: ProgressLevel): LogLine[] {
  if (level === "detailed") return log;
  if (level === "compact") {
    // Only the high-level phase lines; keep any still-pending marker so the spinner
    // shows while work is in flight.
    return log.filter((l) => l.kind === "info");
  }
  // normal: phases + tool calls + results, but drop reasoning blocks and the verbose
  // parameter detail on tool lines.
  return log
    .filter((l) => l.kind !== "reason")
    .map((l) => (l.kind === "tool" && l.detail ? { ...l, detail: undefined } : l));
}

/** Fake-streaming typewriter: reveals `text` a few characters at a time so a line
 *  that arrives all at once still appears to "type" super fast. Only animates while
 *  `live` (the turn is in flight); historical/persisted feeds render instantly.
 *  Continues from its current position if the text grows, so partial lines that get
 *  appended to (e.g. a streaming reason block) keep flowing instead of restarting. */
function useTypewriter(text: string, live?: boolean): string {
  const [shown, setShown] = useState(() => (live ? 0 : text.length));
  const shownRef = useRef(shown);
  shownRef.current = shown;
  useEffect(() => {
    if (!live) {
      shownRef.current = text.length;
      setShown(text.length);
      return;
    }
    // Text shrank or was replaced by something shorter — clamp.
    if (shownRef.current > text.length) {
      shownRef.current = text.length;
      setShown(text.length);
    }
    if (shownRef.current >= text.length) return;
    // Reveal in ~40 small steps so the typing is actually visible (a quick but
    // noticeable stream) rather than an instant flash. Scales with length so short
    // and long lines both read like typing.
    const chunk = Math.max(1, Math.ceil(text.length / 40));
    const id = window.setInterval(() => {
      const next = Math.min(text.length, shownRef.current + chunk);
      shownRef.current = next;
      setShown(next);
      if (next >= text.length) window.clearInterval(id);
    }, 22);
    return () => window.clearInterval(id);
  }, [text, live]);
  return shown >= text.length ? text : text.slice(0, shown);
}

function TypedText({ text, live }: { text: string; live?: boolean }) {
  return <>{useTypewriter(text, live)}</>;
}

function TypedMarkdown({ text, live }: { text: string; live?: boolean }) {
  const shown = useTypewriter(text, live);
  return <Markdown>{shown}</Markdown>;
}

function ProgressLines({
  log,
  level = "detailed",
  live = false,
}: {
  log: LogLine[];
  level?: ProgressLevel;
  live?: boolean;
}) {
  const lines = filterProgress(log, level);
  // Fake-stream the newest content. We animate the last line AND the last "settled"
  // (non-pending) line — because a freshly-arrived tool result is often followed by a
  // pending placeholder (e.g. a spinner/"thinking" line), which would otherwise make
  // the result render instantly. Earlier lines are already complete (their typewriter
  // is at full length), so this stays bounded to ~one active timer.
  const lastIdx = lines.length - 1;
  let lastSettledIdx = lastIdx;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (!lines[i].pending) {
      lastSettledIdx = i;
      break;
    }
  }
  const animates = (i: number) => live && (i === lastIdx || i === lastSettledIdx);
  const icon = (l: LogLine) => {
    // Only spin while a turn is actually live. In a historical/persisted feed a
    // still-"pending" line means the turn ended before that step completed (e.g. it
    // errored) — show a neutral marker instead of an eternal spinner.
    if (l.pending && live) return <Spinner />;
    if (l.pending) return <span className="text-gray-300">◦</span>;
    if (l.kind === "tool") return <span className="text-gray-400">🔧</span>;
    if (l.kind === "result") return <span className="text-green-600">✓</span>;
    if (l.kind === "reason") return <span className="text-gray-400">💭</span>;
    return <span className="text-gray-400">•</span>;
  };
  return (
    <>
      {lines.map((l, i) =>
        l.kind === "reason" ? (
          // The model's thinking (what it understood + its plan). Rendered as a
          // distinct block so it stands out from the tool steps.
          <div key={i} className="flex items-start gap-2 text-xs">
            <span className="mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center">
              {icon(l)}
            </span>
            <div className="min-w-0 flex-1 rounded-md border border-gray-200 bg-white px-2 py-1.5">
              <div className="prose-chat text-xs text-gray-700">
                <TypedMarkdown text={l.text} live={animates(i)} />
              </div>
            </div>
          </div>
        ) : (
          <div key={i} className="flex items-start gap-2 text-xs">
            <span className="mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center">
              {icon(l)}
            </span>
            <div className="min-w-0">
              <span
                className={
                  l.kind === "tool"
                    ? "font-mono text-gray-800"
                    : l.pending
                      ? "text-gray-700"
                      : "text-gray-600"
                }
              >
                <TypedText text={l.text} live={animates(i)} />
              </span>
              {l.detail && (
                <span className="ml-1.5 break-all text-[11px] text-gray-400">
                  <TypedText text={l.detail} live={animates(i)} />
                </span>
              )}
            </div>
          </div>
        ),
      )}
    </>
  );
}

/** Format an elapsed-second count as a compact timer: "12s", "1m 05s", "1h 02m 05s". */
function formatElapsed(totalSec: number): string {
  const s = Math.max(0, Math.floor(totalSec));
  if (s < 60) return `${s}s`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (h > 0) return `${h}h ${pad(m)}m ${pad(sec)}s`;
  return `${m}m ${pad(sec)}s`;
}

/** Live progress feed shown immediately while the agent works. Streams status
 *  lines (sending, responding, tool calls + results, writing) as they happen, and
 *  renders the answer-so-far inside the same panel as it streams. */
function LiveProgress({
  log,
  startedAt,
  answer,
  level = "detailed",
}: {
  log: LogLine[];
  startedAt: number;
  answer?: string;
  level?: ProgressLevel;
}) {
  const [, force] = useState(0);
  const [open, setOpen] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Tick once a second so the elapsed timer updates while waiting.
  useEffect(() => {
    const id = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // Keep the newest line in view.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [log.length]);

  const elapsed = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
  const elapsedLabel = formatElapsed(elapsed);

  return (
    <div className="rounded-lg border border-brand/20 bg-brand/5">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-gray-700"
      >
        <span className="flex gap-0.5">
          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand [animation-delay:-0.3s]" />
          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand [animation-delay:-0.15s]" />
          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand" />
        </span>
        <span>Working on your request…</span>
        <span className="ml-auto flex items-center gap-2">
          <span className="font-mono text-gray-400">{elapsedLabel}</span>
          <span className="text-gray-400">{open ? "▲" : "▼"}</span>
        </span>
      </button>
      {open && (
        <>
          <div
            ref={scrollRef}
            className="max-h-[70vh] space-y-1 overflow-y-auto border-t border-brand/10 px-3 py-2"
          >
            <ProgressLines log={log} level={level} live />
          </div>
          {answer && answer.trim() && (
            <div className="border-t border-brand/10 px-3 py-2">
              <div className="prose-chat text-sm text-gray-900">
                <Markdown components={MARKDOWN_COMPONENTS}>
                  {answer}
                </Markdown>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** Collapsible historical progress feed: the exact detailed lines that were shown
 *  live, persisted so the full work performed for a request stays visible. */
function ProgressPane({ log, level = "detailed" }: { log: LogLine[]; level?: ProgressLevel }) {
  const [open, setOpen] = useState(true);
  const toolCount = log.filter((l) => l.kind === "tool").length;
  return (
    <div className="rounded-lg border border-gray-200 bg-gray-50/70">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-gray-600"
      >
        <span className="text-green-600">✓</span>
        <span>{`Thinking Process · ${toolCount} Step${toolCount === 1 ? "" : "s"}`}</span>
        <span className="ml-auto text-gray-400">{open ? "▲" : "▼ details"}</span>
      </button>
      {open && (
        <div className="space-y-1 px-3 pb-3">
          <ProgressLines log={log} level={level} />
        </div>
      )}
    </div>
  );
}

function ThinkingDots({ label }: { label: string }) {
  return (
    <div className="flex justify-start">
      <div className="flex items-center gap-2 rounded-lg bg-gray-100 px-4 py-2 text-sm text-gray-600">
        <span className="flex gap-1">
          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-gray-400 [animation-delay:-0.3s]" />
          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-gray-400 [animation-delay:-0.15s]" />
          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-gray-400" />
        </span>
        <span>{label}</span>
      </div>
    </div>
  );
}

/** Sum the historical case counts of all descendant leaves. Cached per node since the
 *  problem tree is static — avoids recomputing on every sort comparison and render. */
const _countCache = new WeakMap<ProblemNode, number>();
function totalCount(node: ProblemNode): number {
  const cached = _countCache.get(node);
  if (cached !== undefined) return cached;
  const value = !node.children?.length
    ? node.count ?? 0
    : node.children.reduce((sum, c) => sum + totalCount(c), 0);
  _countCache.set(node, value);
  return value;
}

/** Children sorted by total case volume (most common first). */
function sortedChildren(node: ProblemNode): ProblemNode[] {
  return [...(node.children ?? [])].sort((a, b) => totalCount(b) - totalCount(a));
}

// The top-level tree is static; sort it once instead of on every welcome render.
const SORTED_PROBLEM_TREE = [...PROBLEM_TREE].sort((a, b) => totalCount(b) - totalCount(a));

/** A node in the Azure problem tree (family → service → area → problem), with
 *  case-count badges so the most common issues surface first. */
const TreeNode = memo(function TreeNode({
  node,
  depth,
  trail,
  onPick,
}: {
  node: ProblemNode;
  depth: number;
  trail: string[];
  onPick: (prompt: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const hasChildren = !!node.children?.length;
  const path = [...trail, node.label];
  const count = hasChildren ? node.children!.length : 0;

  const Badge = () =>
    count > 0 ? (
      <span
        className="ml-auto shrink-0 rounded-full bg-gray-100 px-1.5 text-[10px] font-medium text-gray-500"
        title={`${count} sub-item${count === 1 ? "" : "s"}`}
      >
        {count}
      </span>
    ) : null;

  if (!hasChildren) {
    // Leaf = a specific problem. Clicking sends a troubleshooting prompt.
    const prompt = `Help me troubleshoot this Azure issue: ${path.join(" → ")}. Investigate the relevant resources in my subscriptions and tell me the likely cause and next steps.`;
    return (
      <button
        onClick={() => onPick(prompt)}
        style={{ paddingLeft: `${depth * 14 + 24}px` }}
        className="flex w-full items-center gap-2 rounded py-1 pr-2 text-left text-[13px] text-gray-600 transition hover:bg-brand/5 hover:text-brand"
        title={node.label}
      >
        <span className="truncate">{node.label}</span>
        <Badge />
      </button>
    );
  }

  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{ paddingLeft: `${depth * 14 + 6}px` }}
        className={`flex w-full items-center gap-1.5 rounded py-1 pr-2 text-left transition hover:bg-gray-50 ${
          depth === 0 ? "text-sm font-semibold text-gray-800" : "text-[13px] font-medium text-gray-700"
        }`}
      >
        <span className="w-3 shrink-0 text-gray-400">{open ? "▾" : "▸"}</span>
        <span className="truncate">{node.label}</span>
        <Badge />
      </button>
      {open && (
        <div>
          {sortedChildren(node).map((child) => (
            <TreeNode
              key={child.label}
              node={child}
              depth={depth + 1}
              trail={path}
              onPick={onPick}
            />
          ))}
        </div>
      )}
    </div>
  );
});

/** A collapsible "Quick check" category button — expands to reveal one-click starter
 * prompts. Mirrors the TreeNode interaction so both menus feel the same. */
const StarterCategory = memo(function StarterCategory({
  category,
  onPick,
}: {
  category: PromptCategory;
  onPick: (prompt: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 rounded py-1 pl-1.5 pr-2 text-left text-sm font-semibold text-gray-800 transition hover:bg-gray-50"
      >
        <span className="w-3 shrink-0 text-gray-400">{open ? "▾" : "▸"}</span>
        <span className="text-base leading-none">{category.icon}</span>
        <span className="truncate">{category.title}</span>
      </button>
      {open && (
        <div className="pb-1">
          {category.prompts.map((p) => (
            <button
              key={p}
              onClick={() => onPick(p)}
              style={{ paddingLeft: "38px" }}
              className="flex w-full items-center gap-2 rounded py-1 pr-2 text-left text-[13px] text-gray-600 transition hover:bg-brand/5 hover:text-brand"
              title={p}
            >
              <span className="truncate">{p}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
});

/** A short, instant sidebar title from the user's first message — shown immediately
 * while the server computes a polished auto-title in the background. */
function deriveTitle(text: string): string {
  const clean = text.replace(/\s+/g, " ").trim();
  if (!clean) return "New Chat";
  const firstLine = clean.split(/[.!?\n]/)[0].trim() || clean;
  return firstLine.length > 48 ? `${firstLine.slice(0, 48).trim()}…` : firstLine;
}

const Bubble = memo(function Bubble({
  role,
  content,
  images,
  timestamp,
  provider,
  model,
  durationMs,
  onResend,
}: {
  role: string;
  content: string;
  images?: string[];
  timestamp?: string;
  provider?: string | null;
  model?: string | null;
  durationMs?: number | null;
  onResend?: () => void;
}) {
  const isUser = role === "user";
  const ts = formatTimestamp(timestamp);
  if (isUser) {
    return (
      <div className="group flex flex-col items-end gap-1">
        <div className="max-w-[85%] rounded-lg bg-brand px-4 py-2 text-sm text-white">
          {images && images.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-2">
              {images.map((src, i) => (
                <img
                  key={i}
                  src={src}
                  alt={`attachment ${i + 1}`}
                  className="max-h-48 rounded-md border border-white/30 object-contain"
                />
              ))}
            </div>
          )}
          <div className="whitespace-pre-wrap">{content}</div>
        </div>
        <div className="flex items-center gap-2">
          {content.trim() && (
            <CopyButton
              content={content}
              title="Copy your message"
              className="flex items-center text-gray-400 transition hover:text-gray-600"
            />
          )}
          {content.trim() && onResend && (
            <button
              onClick={onResend}
              title="Resend this message"
              className="flex items-center text-gray-400 transition hover:text-gray-600"
            >
              <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
                <path d="M3.5 10a6.5 6.5 0 1011.9-3.6" strokeLinecap="round" />
                <path d="M15.5 3v3.5H12" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          )}
          {ts && <span className="text-[11px] text-gray-400">{ts}</span>}
        </div>
      </div>
    );
  }
  return (
    <div className="flex justify-start">
      <div className="w-full max-w-full">
        <div className="rounded-lg border border-gray-200 bg-white px-4 py-3 text-sm text-gray-900 shadow-sm">
          <div className="prose-chat">
            <Markdown components={MARKDOWN_COMPONENTS}>
              {content}
            </Markdown>
          </div>
        </div>
        {(ts || model || durationMs != null) && (
          <div className="mt-1 flex items-center gap-2 pl-1 text-[11px] text-gray-400">
            {model && (
              <span className="min-w-0 truncate font-medium text-gray-500">
                {(provider && PROVIDER_LABELS[provider]) || provider
                  ? `${PROVIDER_LABELS[provider ?? ""] ?? provider} · ${model}`
                  : model}
              </span>
            )}
            {ts && model && <span className="shrink-0 text-gray-300">•</span>}
            {ts && <span className="shrink-0">{ts}</span>}
            {durationMs != null && durationMs > 0 && (
              <>
                <span className="shrink-0 text-gray-300">•</span>
                <span className="shrink-0" title="Processing time">
                  ⏱ {formatDuration(durationMs)}
                </span>
              </>
            )}
            {durationMs != null && durationMs > 250 && content.length > 4 && (() => {
              // Rough generation speed estimate. Uses the OpenAI-ish heuristic of
              // ~4 chars per token — transparent and provider-agnostic. Hidden for
              // very short answers / sub-second turns where the number is noisy.
              const estTokens = Math.max(1, Math.round(content.length / 4));
              const tps = (estTokens / (durationMs / 1000));
              if (!isFinite(tps) || tps <= 0) return null;
              return (
                <>
                  <span className="shrink-0 text-gray-300">•</span>
                  <span
                    className="shrink-0"
                    title={`Approximate generation speed: ~${estTokens} tokens / ${(durationMs / 1000).toFixed(1)}s`}
                  >
                    ≈ {tps >= 10 ? tps.toFixed(0) : tps.toFixed(1)} tok/s
                  </span>
                </>
              );
            })()}
          </div>
        )}
      </div>
    </div>
  );
});

/** Extract the raw text from a React node tree (for copying code blocks). */
function nodeToText(node: React.ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeToText).join("");
  if (typeof node === "object" && "props" in node) {
    return nodeToText(
      (node as React.ReactElement<{ children?: React.ReactNode }>).props.children,
    );
  }
  return "";
}

/** The `language-xxx` hint react-markdown puts on the inner <code> element, if any. */
function codeLanguage(children?: React.ReactNode): string {
  const arr = Array.isArray(children) ? children : [children];
  for (const node of arr) {
    if (node && typeof node === "object" && "props" in node) {
      const cls = (node as React.ReactElement<{ className?: string }>).props.className ?? "";
      const m = /language-([\w-]+)/.exec(cls);
      if (m) return m[1].toLowerCase();
    }
  }
  return "";
}

// KQL operator keywords that appear after a pipe — used to recognize a Kusto query
// when the code fence isn't explicitly tagged as kql/kusto.
const KQL_OPERATORS =
  /\|\s*(where|project|project-away|project-rename|summarize|extend|order\s+by|sort\s+by|take|top|count|distinct|join|mv-expand|parse|render|limit|where\b)/i;

/** Heuristic: does this code block look like a KQL/Kusto query (vs. a shell command)? */
function looksLikeKql(text: string, lang: string, allowlist: string[]): boolean {
  if (lang === "kql" || lang === "kusto") return true;
  const trimmed = text.trim();
  if (!trimmed) return false;
  const first = trimmed.split(/\s+/)[0]?.toLowerCase() ?? "";
  // Don't treat an allowlisted CLI invocation as KQL.
  if (allowlist.some((b) => b.toLowerCase() === first)) return false;
  // First token must be a bare table identifier (letters/digits/underscore).
  if (!/^[A-Za-z_][\w]*$/.test(trimmed.split(/\s+/)[0] ?? "")) return false;
  return KQL_OPERATORS.test(trimmed);
}

/** A fenced code block rendered with a hover copy icon — and, when the command is an
 * allowlisted CLI invocation and host execution is enabled, a Run button that streams
 * the command's live output below the block. KQL/Kusto blocks get a "Run query" button
 * that executes them via Azure Resource Graph. */
/** Renders a Mermaid diagram from a ```mermaid fenced code block in an AI response.
 * Mermaid is loaded lazily (only when a diagram is actually shown) so it doesn't bloat
 * the initial bundle. On a parse error it falls back to showing the raw source plus the
 * error, and offers a "View source / Copy" affordance for any diagram. */
const MermaidDiagram = memo(function MermaidDiagram({ code }: { code: string }) {
  const { svg, error } = useMermaidRender(code);
  const [showSource, setShowSource] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);

  // A single layout for every state (diagram / source / error / loading) so the
  // toolbar is ALWAYS present — the user can never get stranded in a button-less
  // source/error view after navigating back to the chat.
  const failed = !!error && !svg;
  // When rendering fails we force the source view; otherwise honour the toggle.
  const sourceShown = failed || showSource;

  // Close the fullscreen overlay with Escape.
  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen]);

  // Toolbar buttons: Code (source) · Preview (diagram) · Full screen.
  const tabBtn = (active: boolean) =>
    `inline-flex h-7 w-7 items-center justify-center rounded-md border text-gray-600 transition ${
      active
        ? "border-gray-300 bg-white text-gray-900 shadow-sm"
        : "border-transparent hover:border-gray-200 hover:bg-white/70"
    }`;

  return (
    <div
      className={`my-2 overflow-hidden rounded-lg border bg-white ${
        failed ? "border-amber-300" : "border-gray-200"
      }`}
    >
      <div className="flex items-center justify-between border-b border-gray-200 bg-gray-50 px-3 py-1.5">
        <div className="flex items-center gap-1.5 text-xs font-medium text-gray-600">
          <svg className="h-3.5 w-3.5 text-gray-500" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
            <rect x="3" y="3" width="6" height="5" rx="1" />
            <rect x="11" y="12" width="6" height="5" rx="1" />
            <path d="M6 8v3a1 1 0 001 1h7" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Mermaid
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setShowSource(true)}
            disabled={failed}
            title="Code"
            className={tabBtn(sourceShown)}
          >
            <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
              <path d="M8 6l-4 4 4 4M12 6l4 4-4 4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          <button
            onClick={() => setShowSource(false)}
            disabled={failed}
            title="Preview"
            className={tabBtn(!sourceShown)}
          >
            <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
              <path d="M6 4.5l9 5.5-9 5.5z" />
            </svg>
          </button>
          <button
            onClick={() => setFullscreen(true)}
            disabled={failed || !svg}
            title="Full screen"
            className={tabBtn(false)}
          >
            <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
              <path d="M8 3H3v5M12 3h5v5M8 17H3v-5M12 17h5v-5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </div>
      </div>
      {failed ? (
        <>
          <div className="border-b border-amber-200 px-3 py-1.5 text-[11px] text-amber-700">
            Couldn&rsquo;t render Mermaid diagram — showing source
          </div>
          <pre className="max-h-72 overflow-auto px-3 py-2 font-mono text-[12px] leading-snug text-amber-900">
            {code}
          </pre>
        </>
      ) : sourceShown ? (
        <pre className="max-h-72 overflow-auto px-3 py-2 font-mono text-[12px] leading-snug text-gray-700">
          {code}
        </pre>
      ) : svg ? (
        <div
          className="mermaid-diagram flex justify-center overflow-x-auto px-3 py-4 [&_svg]:h-auto [&_svg]:max-w-full"
          // svg comes from useMermaidRender which (1) renders via Mermaid's
          // securityLevel:"strict" parser and (2) runs the result through
          // DOMPurify's SVG profile as a defense-in-depth second layer.
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      ) : (
        <div className="flex items-center gap-2 px-3 py-6 text-xs text-gray-400">
          <Spinner className="h-3.5 w-3.5 text-gray-400" />
          Rendering diagram…
        </div>
      )}

      {fullscreen && svg && (
        <div
          className="fixed inset-0 z-50 flex flex-col bg-black/60 backdrop-blur-sm"
          onClick={() => setFullscreen(false)}
        >
          <div className="flex items-center justify-between px-4 py-2 text-white">
            <span className="text-sm font-medium">Mermaid</span>
            <button
              onClick={() => setFullscreen(false)}
              title="Close (Esc)"
              className="inline-flex h-8 w-8 items-center justify-center rounded-md text-white/80 transition hover:bg-white/20 hover:text-white"
            >
              <svg className="h-5 w-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
                <path d="M5 5l10 10M15 5L5 15" strokeLinecap="round" />
              </svg>
            </button>
          </div>
          <div
            className="mermaid-diagram m-4 mt-0 flex flex-1 items-center justify-center overflow-auto rounded-lg bg-white p-6 [&_svg]:h-auto [&_svg]:max-h-full [&_svg]:max-w-full"
            onClick={(e) => e.stopPropagation()}
            // svg already sanitized by Mermaid (strict) + DOMPurify (SVG profile)
            // in useMermaidRender; safe to inject.
            dangerouslySetInnerHTML={{ __html: svg }}
          />
        </div>
      )}
    </div>
  );
});

/** Renders Mermaid `code` to an SVG string, returning `{ svg, error }`. Mermaid is
 * imported lazily on first use. Used by both the inline diagram and the editor's live
 * preview so they stay visually identical. */
function useMermaidRender(code: string): { svg: string; error: string } {
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");
  // Keep the latest successfully-rendered source so we can suppress redundant
  // re-renders (e.g. identical code after a parent re-render) without flicker.
  const lastRenderedRef = useRef<string>("");

  useEffect(() => {
    const trimmed = code.trim();
    if (!trimmed) {
      setSvg("");
      setError("");
      lastRenderedRef.current = "";
      return;
    }
    // Same source already rendered — nothing to do (avoids a needless clear/redraw).
    if (trimmed === lastRenderedRef.current && svg) return;

    let cancelled = false;
    // Debounce so streaming (which appends a token at a time) coalesces into one
    // render once the source settles, instead of re-parsing on every keystroke/token.
    const timer = window.setTimeout(() => {
      // A fresh id per render attempt avoids "diagram id already exists" failures when
      // the component remounts (e.g. navigating away from a chat and back).
      const renderId = `mmd-${Math.random().toString(36).slice(2)}`;
      // IMPORTANT: do NOT clear the current svg here. Keeping the last good diagram
      // visible until the new one is ready is what removes the render flicker — the
      // spinner only shows on the very first render (when svg is still empty).
      (async () => {
        try {
          const mermaid = (await import("mermaid")).default;
          mermaid.initialize({
            startOnLoad: false,
            securityLevel: "strict", // sanitize labels/links — never inject raw HTML
            theme: "neutral",
            fontFamily: "inherit",
            // Render node/edge labels as native SVG <text> (not HTML <foreignObject>).
            // We sanitize the output with DOMPurify's SVG profile below, which strips
            // <foreignObject>'s XHTML content — that would leave the shapes but BLANK out
            // every label. SVG <text> labels survive the SVG-profile sanitize intact.
            htmlLabels: false,
            flowchart: { htmlLabels: false },
          });
          // `render` validates + produces SVG without touching the DOM tree we control.
          const { svg: out } = await mermaid.render(renderId, trimmed);
          // Defense-in-depth: even though Mermaid is configured with
          // securityLevel:"strict", we run the output through DOMPurify (SVG
          // profile) before injecting it as innerHTML. If a future Mermaid CVE
          // ever lets an attacker-crafted diagram smuggle a JS handler past
          // Mermaid's sanitizer, DOMPurify still strips it.
          const DOMPurify = (await import("dompurify")).default;
          const safeSvg = DOMPurify.sanitize(out, { USE_PROFILES: { svg: true, svgFilters: true } });
          if (!cancelled) {
            setSvg(safeSvg);
            setError("");
            lastRenderedRef.current = trimmed;
          }
        } catch (e) {
          // Only surface an error if we have nothing good to show; while streaming, a
          // transiently-incomplete diagram shouldn't replace the last valid render.
          if (!cancelled) setError((e as Error)?.message ?? "Failed to render diagram.");
        } finally {
          // mermaid can leave an orphan measurement node in <body> if a render was
          // interrupted mid-flight; clean it up so repeated mounts stay reliable.
          document.getElementById(renderId)?.remove();
          document.getElementById(`d${renderId}`)?.remove();
        }
      })();
    }, 120);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  return { svg, error };
}

/** Right-side drawer to edit a Mermaid diagram's source with a live preview that
 * re-renders as the user types (debounced). The chat's persisted diagram is unchanged;
 * this is a scratchpad for tweaking + copying the result. */
function MermaidEditorPanel({ code, onClose }: { code: string; onClose: () => void }) {
  const [draft, setDraft] = useState(code);
  // Debounce the source fed to the renderer so every keystroke doesn't trigger a parse.
  const [debounced, setDebounced] = useState(code);
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(draft), 250);
    return () => window.clearTimeout(id);
  }, [draft]);
  const { svg, error } = useMermaidRender(debounced.trim());

  // Resizable, non-overlapping split panel (shares the splitter pattern with the chat).
  const { width, handle } = useResizablePanel(MERMAID_WIDTH_KEY, 560);

  return (
    <div
      className="relative flex h-full shrink-0 flex-col border-l border-gray-200 bg-white"
      style={{ width }}
    >
      {handle}
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-gray-800">Edit diagram</div>
          <div className="text-[11px] text-gray-500">
            Modify the Mermaid source — the preview updates live.
          </div>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setDraft(code)}
            title="Reset to the original source"
            className="rounded-md border border-gray-200 px-2 py-1 text-[11px] text-gray-600 transition hover:bg-gray-100"
          >
            Reset
          </button>
          <CopyButton
            content={() => draft}
            title="Copy the edited source"
            label="Copy"
            className="inline-flex items-center gap-1 rounded-md border border-gray-200 px-2 py-1 text-[11px] text-gray-600 transition hover:bg-gray-100"
            checkClassName="h-3.5 w-3.5 text-green-600"
          />
          <button
            onClick={onClose}
            title="Close"
            className="rounded-md p-1.5 text-gray-400 transition hover:bg-gray-100 hover:text-gray-700"
          >
            <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M5 5l10 10M15 5L5 15" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-600">Mermaid source</label>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            rows={12}
            className="w-full resize-y rounded-lg border border-gray-300 bg-gray-900 px-3 py-2 font-mono text-[12px] leading-snug text-gray-100 focus:border-sky-400 focus:outline-none focus:ring-1 focus:ring-sky-400"
          />
        </div>

        <div>
          <div className="mb-1 flex items-center justify-between">
            <label className="block text-xs font-medium text-gray-600">Live preview</label>
            {error && <span className="text-[11px] text-amber-600">Parse error</span>}
          </div>
          <div
            className={`overflow-hidden rounded-lg border bg-white ${
              error && !svg ? "border-amber-300" : "border-gray-200"
            }`}
          >
            {error && !svg ? (
              <pre className="max-h-40 overflow-auto px-3 py-2 font-mono text-[11px] leading-snug text-amber-800">
                {error}
              </pre>
            ) : svg ? (
              <div
                className="mermaid-diagram flex justify-center overflow-auto px-3 py-4 [&_svg]:h-auto [&_svg]:max-w-full"
                // svg already sanitized by Mermaid (strict) + DOMPurify (SVG profile)
                // in useMermaidRender; safe to inject.
                dangerouslySetInnerHTML={{ __html: svg }}
              />
            ) : (
              <div className="flex items-center gap-2 px-3 py-6 text-xs text-gray-400">
                <Spinner className="h-3.5 w-3.5 text-gray-400" />
                Rendering diagram…
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function CodeBlock({ children }: { children?: React.ReactNode }) {
  const exec = useContext(ExecContext);
  const text = nodeToText(children).replace(/\n$/, "");
  const trimmed = text.trim();
  const lang = codeLanguage(children);
  // Mermaid diagrams: render the chart instead of the raw code fence.
  if (lang === "mermaid" && trimmed) {
    return <MermaidDiagram code={trimmed} />;
  }
  // Interactive Azure Monitor metric chart from a ```azchart { ...spec } block.
  if (lang === "azchart" && trimmed) {
    return (
      <Suspense
        fallback={<div className="my-3 h-[340px] w-full animate-pulse rounded-xl bg-gray-100" />}
      >
        <MetricChart spec={trimmed} />
      </Suspense>
    );
  }
  const firstToken = trimmed.split(/\s+/)[0]?.toLowerCase() ?? "";
  const singleCommand = !trimmed.includes("\n");
  const cmdRunnable =
    exec.enabled &&
    !!exec.chatId &&
    singleCommand &&
    exec.allowlist.some((b) => b.toLowerCase() === firstToken);
  const kqlRunnable =
    exec.enabled && !!exec.chatId && !cmdRunnable && looksLikeKql(text, lang, exec.allowlist);
  const runnable = cmdRunnable || kqlRunnable;

  const [started, setStarted] = useState(false);
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState("");
  const [output, setOutput] = useState<{ text: string; stream: "stdout" | "stderr" }[]>([]);
  const [errorMsg, setErrorMsg] = useState("");
  const [exit, setExit] = useState<{ code: number | null; duration_ms: number } | null>(null);
  const [confirmReq, setConfirmReq] = useState<{ message: string } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Distinguishes a deliberate Stop / navigation-away abort from a real failure, so an
  // interrupted run is never persisted (or shown) as a "network error".
  const abortedRef = useRef(false);

  // Abort any in-flight run when this code block unmounts (e.g. switching chats), so the
  // interrupted fetch doesn't surface as a spurious network error.
  useEffect(() => {
    return () => {
      abortedRef.current = true;
      abortRef.current?.abort();
    };
  }, []);

  // Rehydrate a previous run for this (chat, command) so the output stays part of the
  // conversation when the chat is reopened.
  useEffect(() => {
    if (!runnable || !exec.chatId) return;
    const saved = getExecRun(exec.chatId, text);
    if (saved && (saved.output.length || saved.exit || saved.error)) {
      setStarted(true);
      setOutput(saved.output);
      setExit(saved.exit);
      setErrorMsg(saved.error);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exec.chatId, text, runnable]);

  async function doRun(confirm = false) {
    if (!exec.chatId) return;
    const chatId = exec.chatId;
    setStarted(true);
    setRunning(true);
    setErrorMsg("");
    setExit(null);
    setStatus("");
    setConfirmReq(null);
    if (!confirm) setOutput([]);
    const collected: { text: string; stream: "stdout" | "stderr" }[] = [];
    let finalExit: { code: number | null; duration_ms: number } | null = null;
    let finalError = "";
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    abortedRef.current = false;
    try {
      await streamCommand(
        chatId,
        text,
        {
          onStatus: (t) => setStatus(t),
          onOutput: (t, stream) => {
            collected.push({ text: t, stream });
            setOutput((o) => [...o, { text: t, stream }]);
          },
          onApprovalRequired: (d) => {
            setConfirmReq({ message: d.message });
            setRunning(false);
          },
          onExit: (d) => {
            finalExit = d;
            setExit(d);
          },
          onError: (m) => {
            finalError = m;
            setErrorMsg(m);
          },
          onDone: () => setRunning(false),
        },
        { confirm, signal: ctrl.signal, mode: kqlRunnable ? "kql" : "command" },
      );
    } catch (e) {
      // An abort (Stop button or navigating away) is not a failure — ignore it.
      if (!ctrl.signal.aborted && !abortedRef.current) {
        finalError = (e as Error)?.message ?? "Command failed";
        setErrorMsg(finalError);
      }
    } finally {
      setRunning(false);
      // Persist only a genuinely completed run. Never overwrite a stored result with an
      // interrupted/aborted one (otherwise reopening the chat shows a phantom error).
      const interrupted = ctrl.signal.aborted || abortedRef.current;
      if (!interrupted && (collected.length || finalExit || finalError)) {
        saveExecRun(chatId, text, { output: collected, exit: finalExit, error: finalError });
      }
    }
  }

  return (
    <div className="group/code relative">
      <div className="absolute right-2 top-2 z-10 flex items-center gap-1 opacity-0 transition group-hover/code:opacity-100">
        {runnable && (
          <button
            onClick={() => void doRun(false)}
            disabled={running}
            title={
              kqlRunnable
                ? "Run this KQL query via Azure Resource Graph"
                : "Run this command on the host"
            }
            className="inline-flex items-center gap-1 rounded-md border border-sky-400/40 bg-sky-500/20 px-1.5 py-1 text-[11px] text-sky-100 backdrop-blur transition hover:bg-sky-500/30 disabled:opacity-50"
          >
            {running ? (
              <Spinner className="h-3.5 w-3.5 text-sky-200" />
            ) : (
              <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor">
                <path d="M6 4.5v11l9-5.5-9-5.5z" />
              </svg>
            )}
            {kqlRunnable ? "Run query" : "Run"}
          </button>
        )}
        {runnable && (
          <button
            onClick={() => exec.openEditor(text, kqlRunnable ? "kql" : "command")}
            title="Edit before running"
            className="inline-flex items-center gap-1 rounded-md border border-white/15 bg-white/10 px-1.5 py-1 text-[11px] text-gray-200 backdrop-blur transition hover:bg-white/20"
          >
            <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
              <path d="M13.5 4.5l2 2L7 15l-3 .8.8-3 8.7-8.3z" strokeLinejoin="round" />
            </svg>
            Edit
          </button>
        )}
        <CopyButton
          content={() => text}
          title="Copy code"
          label="Copy"
          className="inline-flex items-center gap-1 rounded-md border border-white/15 bg-white/10 px-1.5 py-1 text-[11px] text-gray-200 backdrop-blur transition hover:bg-white/20"
          checkClassName="h-3.5 w-3.5 text-green-400"
        />
      </div>
      <pre>{children}</pre>
      {started && (
        <div className="mt-1.5 overflow-hidden rounded-lg border border-gray-700 bg-gray-900 text-[12px]">
          <div className="flex items-center justify-between border-b border-gray-700 px-3 py-1.5 text-[11px] text-gray-400">
            <span className="flex items-center gap-1.5">
              {running && <Spinner className="h-3 w-3 text-sky-300" />}
              {running ? status || "Running…" : "Output"}
            </span>
            {running ? (
              <button
                onClick={() => abortRef.current?.abort()}
                className="rounded px-1.5 py-0.5 text-gray-300 hover:bg-gray-700"
              >
                Stop
              </button>
            ) : (
              exit && (
                <span className={exit.code === 0 ? "text-green-400" : "text-red-400"}>
                  exit {exit.code} · {(exit.duration_ms / 1000).toFixed(1)}s
                </span>
              )
            )}
          </div>
          <pre className="max-h-80 overflow-auto px-3 py-2 font-mono text-[12px] leading-snug text-gray-100">
            {output.length === 0 && !errorMsg && !running && (
              <span className="text-gray-500">No output.</span>
            )}
            {output.map((l, i) => (
              <span key={i} className={l.stream === "stderr" ? "text-amber-300" : ""}>
                {l.text}
              </span>
            ))}
          </pre>
          {confirmReq && (
            <div className="flex items-center justify-between gap-2 border-t border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-200">
              <span>⚠ {confirmReq.message}</span>
              <div className="flex gap-2">
                <button
                  onClick={() => setConfirmReq(null)}
                  className="rounded border border-gray-600 px-2 py-0.5 text-gray-300 hover:bg-gray-700"
                >
                  Cancel
                </button>
                <button
                  onClick={() => void doRun(true)}
                  className="rounded bg-amber-500 px-2 py-0.5 font-medium text-gray-900 hover:bg-amber-400"
                >
                  Run anyway
                </button>
              </div>
            </div>
          )}
          {errorMsg && (
            <div className="border-t border-red-500/30 bg-red-500/10 px-3 py-2 text-[12px] text-red-300">
              {errorMsg}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Pulls header cells and body rows out of react-markdown's default table tree
 * (thead/tbody/tr/th/td) so we can re-render them with client-side sort + search,
 * preserving each cell's original rich content (bold, code, links). */
function extractTable(children: React.ReactNode): {
  headers: React.ReactNode[];
  rows: React.ReactNode[][];
} {
  let headers: React.ReactNode[] = [];
  const rows: React.ReactNode[][] = [];
  for (const section of Children.toArray(children)) {
    if (!isValidElement(section)) continue;
    const sec = section as React.ReactElement<{ children?: React.ReactNode }>;
    const isHead = sec.type === "thead";
    for (const tr of Children.toArray(sec.props.children)) {
      if (!isValidElement(tr)) continue;
      const trEl = tr as React.ReactElement<{ children?: React.ReactNode }>;
      const cells = Children.toArray(trEl.props.children)
        .filter(isValidElement)
        .map((c) => (c as React.ReactElement<{ children?: React.ReactNode }>).props.children);
      if (isHead) headers = cells;
      else rows.push(cells);
    }
  }
  // GFM tables always have a thead; fall back to first row as header just in case.
  if (headers.length === 0 && rows.length > 0) {
    headers = rows[0];
    rows.shift();
  }
  return { headers, rows };
}

/** Renders a markdown table with client-side column sorting and (for larger tables)
 * a full-text filter. Cell content keeps its original markdown formatting. */
function MarkdownTable({ children }: { children?: React.ReactNode }) {
  const { headers, rows } = useMemo(() => extractTable(children), [children]);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ col: number; dir: "asc" | "desc" } | null>(null);

  // Pre-compute plain text per cell for searching/sorting.
  const rowTexts = useMemo(
    () => rows.map((cells) => cells.map((c) => nodeToText(c).trim())),
    [rows],
  );

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    let idx = rows.map((_, i) => i);
    if (q) idx = idx.filter((i) => rowTexts[i].some((t) => t.toLowerCase().includes(q)));
    if (sort) {
      const { col, dir } = sort;
      idx = [...idx].sort((a, b) => {
        const ta = rowTexts[a][col] ?? "";
        const tb = rowTexts[b][col] ?? "";
        const na = Number(ta.replace(/[^0-9.\-]/g, ""));
        const nb = Number(tb.replace(/[^0-9.\-]/g, ""));
        const numeric = ta !== "" && tb !== "" && !Number.isNaN(na) && !Number.isNaN(nb);
        const cmp = numeric ? na - nb : ta.localeCompare(tb, undefined, { numeric: true });
        return dir === "asc" ? cmp : -cmp;
      });
    }
    return idx;
  }, [rows, rowTexts, query, sort]);

  function toggleSort(col: number) {
    setSort((s) => {
      if (!s || s.col !== col) return { col, dir: "asc" };
      if (s.dir === "asc") return { col, dir: "desc" };
      return null; // third click clears sorting
    });
  }

  if (headers.length === 0) {
    // Not a recognizable table — render markdown's default output untouched.
    return <table>{children}</table>;
  }

  const showSearch = rows.length > 4;

  return (
    <div className="my-2">
      {showSearch && (
        <div className="mb-1.5 flex items-center gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={`Search ${rows.length} rows…`}
            className="w-56 rounded-md border px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-brand"
          />
          {query && (
            <span className="text-[11px] text-gray-400">
              {visible.length} match{visible.length === 1 ? "" : "es"}
            </span>
          )}
        </div>
      )}
      <div className="overflow-x-auto">
        <table>
          <thead>
            <tr>
              {headers.map((h, i) => {
                const sorted = sort?.col === i;
                return (
                  <th
                    key={i}
                    onClick={() => toggleSort(i)}
                    title="Click to sort"
                    className="cursor-pointer select-none whitespace-nowrap"
                  >
                    <span className="inline-flex items-center gap-1">
                      {h}
                      <span className={sorted ? "text-brand" : "text-gray-300"}>
                        {sorted ? (sort!.dir === "asc" ? "▲" : "▼") : "↕"}
                      </span>
                    </span>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {visible.map((ri) => (
              <tr key={ri}>
                {rows[ri].map((cell, ci) => (
                  <td key={ci}>{cell}</td>
                ))}
              </tr>
            ))}
            {visible.length === 0 && (
              <tr>
                <td colSpan={headers.length} className="text-center text-gray-400">
                  No matching rows.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Shared react-markdown component overrides (adds copy buttons to code blocks). */
type MarkdownComponents = import("react-markdown").Components;
const MARKDOWN_COMPONENTS: MarkdownComponents = {
  pre: ({ children }: { children?: React.ReactNode }) => <CodeBlock>{children}</CodeBlock>,
  table: ({ children }: { children?: React.ReactNode }) => <MarkdownTable>{children}</MarkdownTable>,
};

/** Right-side drawer that lets the user edit a command / KQL query before running it.
 * Streams output live in the panel; Run executes the edited text, Discard closes it. */
function CommandEditorPanel({
  chatId,
  command,
  mode,
  onClose,
}: {
  chatId: string;
  command: string;
  mode: ExecMode;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState(command);
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState("");
  const [output, setOutput] = useState<{ text: string; stream: "stdout" | "stderr" }[]>([]);
  const [errorMsg, setErrorMsg] = useState("");
  const [exit, setExit] = useState<{ code: number | null; duration_ms: number } | null>(null);
  const [confirmReq, setConfirmReq] = useState<{ message: string } | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  async function run(confirm = false) {
    const text = draft.trim();
    if (!text) return;
    setRunning(true);
    setErrorMsg("");
    setExit(null);
    setStatus("");
    setConfirmReq(null);
    if (!confirm) setOutput([]);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      await streamCommand(
        chatId,
        text,
        {
          onStatus: (t) => setStatus(t),
          onOutput: (t, stream) => setOutput((o) => [...o, { text: t, stream }]),
          onApprovalRequired: (d) => {
            setConfirmReq({ message: d.message });
            setRunning(false);
          },
          onExit: (d) => setExit(d),
          onError: (m) => setErrorMsg(m),
          onDone: () => setRunning(false),
        },
        { confirm, signal: ctrl.signal, mode },
      );
    } catch (e) {
      if (!ctrl.signal.aborted) setErrorMsg((e as Error)?.message ?? "Command failed");
    } finally {
      setRunning(false);
    }
  }

  const isKql = mode === "kql";
  // Resizable, non-overlapping split panel (shares a splitter with the chat).
  const { width, handle } = useResizablePanel(EDITOR_WIDTH_KEY, 520);

  return (
    <div
      className="relative flex h-full shrink-0 flex-col border-l border-gray-200 bg-white"
      style={{ width }}
    >
      {handle}
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-gray-800">
            Edit {isKql ? "query" : "command"}
          </div>
          <div className="text-[11px] text-gray-500">
            {isKql
              ? "Modify the KQL, then run it via Azure Resource Graph."
              : "Modify the command, then run it on the host."}
          </div>
        </div>
        <button
          onClick={onClose}
          title="Discard"
          className="rounded-md p-1.5 text-gray-400 transition hover:bg-gray-100 hover:text-gray-700"
        >
          <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path d="M5 5l10 10M15 5L5 15" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-600">
            {isKql ? "KQL query" : "Command"}
          </label>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            rows={isKql ? 8 : 4}
            className="w-full resize-y rounded-lg border border-gray-300 bg-gray-900 px-3 py-2 font-mono text-[12px] leading-snug text-gray-100 focus:border-sky-400 focus:outline-none focus:ring-1 focus:ring-sky-400"
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                if (!running) void run(false);
              }
            }}
          />
          <div className="mt-1 text-[11px] text-gray-400">
            {isKql
              ? "Runs read-only via Azure Resource Graph."
              : "Single command only — no shell operators. Mutating commands ask to confirm."}
            {"  ·  ⌘/Ctrl+Enter to run"}
          </div>
        </div>

        {(output.length > 0 || running || exit || errorMsg) && (
          <div className="overflow-hidden rounded-lg border border-gray-700 bg-gray-900 text-[12px]">
            <div className="flex items-center justify-between border-b border-gray-700 px-3 py-1.5 text-[11px] text-gray-400">
              <span className="flex items-center gap-1.5">
                {running && <Spinner className="h-3 w-3 text-sky-300" />}
                {running ? status || "Running…" : "Output"}
              </span>
              {!running && exit && (
                <span className={exit.code === 0 ? "text-green-400" : "text-red-400"}>
                  exit {exit.code} · {(exit.duration_ms / 1000).toFixed(1)}s
                </span>
              )}
            </div>
            <pre className="max-h-96 overflow-auto px-3 py-2 font-mono text-[12px] leading-snug text-gray-100">
              {output.length === 0 && !errorMsg && !running && (
                <span className="text-gray-500">No output.</span>
              )}
              {output.map((l, i) => (
                <span key={i} className={l.stream === "stderr" ? "text-amber-300" : ""}>
                  {l.text}
                </span>
              ))}
            </pre>
            {errorMsg && (
              <div className="border-t border-red-500/30 bg-red-500/10 px-3 py-2 text-[12px] text-red-300">
                {errorMsg}
              </div>
            )}
          </div>
        )}

        {confirmReq && (
          <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2.5 text-xs text-amber-800">
            <div className="mb-2">⚠ {confirmReq.message}</div>
            <div className="flex gap-2">
              <button
                onClick={() => setConfirmReq(null)}
                className="rounded border border-gray-300 bg-white px-2.5 py-1 font-medium text-gray-700 hover:bg-gray-50"
              >
                Cancel
              </button>
              <button
                onClick={() => void run(true)}
                className="rounded bg-amber-500 px-2.5 py-1 font-medium text-gray-900 hover:bg-amber-400"
              >
                Run anyway
              </button>
            </div>
          </div>
        )}
      </div>

      <div className="flex items-center justify-end gap-2 border-t px-4 py-3">
        <button
          onClick={onClose}
          className="rounded-lg border px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50"
        >
          Discard
        </button>
        {running ? (
          <button
            onClick={() => abortRef.current?.abort()}
            className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-black"
          >
            Stop
          </button>
        ) : (
          <button
            onClick={() => void run(false)}
            disabled={!draft.trim()}
            className="inline-flex items-center gap-1.5 rounded-lg bg-sky-500 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-sky-600 disabled:opacity-50"
          >
            <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor">
              <path d="M6 4.5v11l9-5.5-9-5.5z" />
            </svg>
            Run
          </button>
        )}
      </div>
    </div>
  );
}
