import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import ChatView from "./components/ChatView";
import { useAuth } from "./components/AuthContext";
import LoginPage, { ForcePasswordChange } from "./components/LoginPage";
import { HelpMenu } from "./components/HelpMenu";
import { CommandPalette } from "./components/CommandPalette";
import { WelcomeModal } from "./components/WelcomeModal";
import { ContextDocumentationHelp } from "./components/ContextDocumentationHelp";
import { APP_VERSION, APP_VERSION_DISPLAY } from "./version";
import { api } from "./api";
import { NoAccessScreen } from "./components/NoAccessScreen";
import { UserMenu } from "./components/UserMenu";
import { adminRequirement } from "./components/routeAccess";
import { canAccess, hasEffectiveAccess } from "./utils/accessControl";

// Friendly labels for the active AI provider shown in the top bar.
const PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  openai_eu: "OpenAI (EU)",
  azure_openai: "Azure OpenAI",
  azure_foundry: "Azure Foundry",
  github: "GitHub Models",
  github_copilot: "GitHub Copilot",
  chatgpt: "ChatGPT OAuth",
  claude: "Claude API",
  claude_oauth: "Claude OAuth",
  gemini: "Google Gemini",
  grok: "Grok (xAI)",
  mistral: "Mistral",
  openrouter: "OpenRouter",
  ollama: "Ollama (local)",
  lmstudio: "LM Studio (local)",
};

/** Redirect a legacy `/rbac[/tab]` URL to its `/iam` equivalent.
 *
 *  A bare `<Navigate to="/iam" />` would be wrong twice over: it drops the tab segment, so
 *  `/rbac/insights` lands on the overview, and it drops `location.search`, so the Estate Graph
 *  handoff (`?workload_id=`) and any shared filter URL silently lose their scope. */
function LegacyIamRedirect() {
  const location = useLocation();
  const rest = location.pathname.replace(/^\/rbac/, "");
  return <Navigate to={`/iam${rest}${location.search}${location.hash}`} replace />;
}

export default function App() {
  const { user, loading, logout, refresh, has } = useAuth();
  const location = useLocation();
  const activeLlmQ = useQuery({
    queryKey: ["activeLlm"],
    queryFn: api.activeLlm,
    enabled: hasEffectiveAccess(user),
    staleTime: 60_000,
    retry: false,
  });

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-slate-500">
        Loading…
      </div>
    );
  }

  if (!user) {
    return <LoginPage />;
  }

  if (user.must_change_password) {
    return <ForcePasswordChange />;
  }

  if (!hasEffectiveAccess(user)) {
    return (
      <NoAccessScreen
        user={user}
        onLogout={() => void logout()}
        onRefresh={refresh}
      />
    );
  }

  const aiProvider = activeLlmQ.data?.provider ?? "";
  const aiModel = activeLlmQ.data?.model ?? "";
  const aiProviderLabel = PROVIDER_LABELS[aiProvider] ?? aiProvider;

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b bg-brand-dark px-4 py-3 text-white">
        <div className="flex items-center gap-2">
          <span className="text-lg">🤖</span>
          <Link to="/dashboard" className="font-semibold">
            Azure Support Agent
          </Link>
        </div>
        <div className="min-w-0 flex items-center gap-3 text-sm">
          <button
            onClick={() => {
              // Synthesize the palette hotkey so the button mirrors Ctrl/⌘+K.
              window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true }));
            }}
            title="Search (Ctrl/⌘ + K)"
            className="hidden items-center gap-2 rounded-lg border border-white/20 bg-white/10 px-2.5 py-1 text-xs text-white/80 hover:bg-white/20 sm:flex"
          >
            <span>⌕ Search</span>
            <kbd className="rounded bg-white/20 px-1 text-[10px]">⌘K</kbd>
          </button>
          <HelpMenu />
          <ContextDocumentationHelp pathname={location.pathname} />
          {aiProvider ? (
            has("settings.read") ? (
              <Link
                to="/admin/providers"
                title="Change AI provider"
                className="hidden items-center gap-1.5 rounded-lg border border-white/20 bg-white/10 px-2.5 py-1 text-xs text-white/80 hover:bg-white/20 md:flex"
              >
                <span>🧠</span>
                <span className="font-medium text-white/90">{aiProviderLabel}</span>
                {aiModel && <span className="text-white/50">· {aiModel}</span>}
                <span className="text-white/60">⚙︎</span>
              </Link>
            ) : (
              <span
                title="Active AI model"
                className="hidden items-center gap-1.5 rounded-lg border border-white/20 bg-white/10 px-2.5 py-1 text-xs text-white/80 md:flex"
              >
                <span>🧠</span>
                <span className="font-medium text-white/90">{aiProviderLabel}</span>
                {aiModel && <span className="text-white/50">· {aiModel}</span>}
              </span>
            )
          ) : null}
          <span
            className="rounded bg-white/10 px-1.5 py-0.5 text-xs font-medium text-white/70"
            title={`Azure Support Agent ${APP_VERSION}`}
          >
            {APP_VERSION_DISPLAY}
          </span>
          <Link to="/dashboard" className="hidden rounded px-2 py-1 hover:bg-white/10 lg:inline-block">
            Dashboard
          </Link>
          {canAccess(user, adminRequirement(undefined)) && (
            <Link to="/admin" className="hidden rounded px-2 py-1 hover:bg-white/10 lg:inline-block">
              Settings
            </Link>
          )}
          <UserMenu user={user} onLogout={() => void logout()} onRefresh={refresh} />
        </div>
      </header>

      <CommandPalette />
      <WelcomeModal />

      <div className="min-h-0 flex-1">
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<ChatView />} />
          <Route path="/chat" element={<ChatView />} />
          <Route path="/c/:chatId" element={<ChatView />} />
          <Route path="/automations" element={<ChatView />} />
          <Route path="/automations/:section" element={<ChatView />} />
          <Route path="/workloads" element={<ChatView />} />
          <Route path="/workloads/overlaps" element={<ChatView />} />
          <Route path="/workloads/groups" element={<ChatView />} />
          <Route path="/workloads/groups/:id" element={<ChatView />} />
          <Route path="/workloads/:id" element={<ChatView />} />
          <Route path="/mission-control" element={<ChatView />} />
          <Route path="/mission-control/:id" element={<ChatView />} />
          <Route path="/inventory" element={<ChatView />} />
          <Route path="/inventory/:tab" element={<ChatView />} />
          <Route path="/ownership" element={<ChatView />} />
          <Route path="/ownership/:tab" element={<ChatView />} />
          <Route path="/graph" element={<ChatView />} />
          <Route path="/graph/:focusId" element={<ChatView />} />
          <Route path="/tagintel" element={<ChatView />} />
          <Route path="/tagintel/:tab" element={<ChatView />} />
          <Route path="/change-explorer" element={<ChatView />} />
          <Route path="/change-explorer/:tab" element={<ChatView />} />
          <Route path="/insights" element={<ChatView />} />
          <Route path="/insights/:section" element={<ChatView />} />
          <Route path="/iam" element={<ChatView />} />
          <Route path="/iam/:tab" element={<ChatView />} />
          {/* /rbac was renamed to /iam (the screen covers access models that are not RBAC).
              Keep old links, bookmarks and shared URLs working — tab segment, query string
              and hash all have to survive, or the Graph handoff (?workload_id=) and any
              shared filter URL land on a bare overview. */}
          <Route path="/rbac" element={<LegacyIamRedirect />} />
          <Route path="/rbac/:tab" element={<LegacyIamRedirect />} />
          <Route path="/assessments" element={<ChatView />} />
          <Route path="/assessments/:id" element={<ChatView />} />
          <Route path="/architectures" element={<ChatView />} />
          <Route path="/architectures/memory" element={<ChatView />} />
          <Route path="/architectures/:id" element={<ChatView />} />
          <Route path="/architectures/:id/memory" element={<ChatView />} />
          <Route path="/knowme" element={<ChatView />} />
          <Route path="/knowme/:id" element={<ChatView />} />
          <Route path="/fmea" element={<ChatView />} />
          <Route path="/fmea/:id" element={<ChatView />} />
          <Route path="/policy" element={<ChatView />} />
          <Route path="/policy/:tab" element={<ChatView />} />
          {/* The Identity screen was absorbed by Entra ID. Keep its three URLs working — each
              lands on the tab that now hosts the panel it used to open. */}
          <Route path="/identity" element={<Navigate to="/entra/findings?sub=hygiene" replace />} />
          <Route path="/identity/pim" element={<Navigate to="/entra/privileged?sub=jit-hygiene" replace />} />
          <Route path="/identity/app-registrations" element={<Navigate to="/entra/applications?sub=registrations" replace />} />
          <Route path="/identity/*" element={<Navigate to="/entra" replace />} />
          <Route path="/entra" element={<ChatView />} />
          <Route path="/entra/:tab" element={<ChatView />} />
          {/* Sub-tabs are the third segment (/entra/privileged/pim) so a reload, a shared
              link or the back button all land on the screen the reader was actually on. */}
          <Route path="/entra/:tab/:sub" element={<ChatView />} />
          <Route path="/coverage" element={<ChatView />} />
          <Route path="/alerts-manager" element={<ChatView />} />
          <Route path="/alerts-manager/simulator" element={<Navigate to="/alerts-manager/visualize" replace />} />
          <Route path="/alerts-manager/:tab" element={<ChatView />} />
          <Route path="/alert-analysis" element={<Navigate to="/alerts-manager" replace />} />
          <Route path="/telemetry" element={<ChatView />} />
          <Route path="/backupdr" element={<ChatView />} />
          <Route path="/backup-manager" element={<ChatView />} />
          <Route path="/backup-manager/:tab" element={<ChatView />} />
          <Route path="/capability" element={<ChatView />} />
          <Route path="/evidence" element={<ChatView />} />
          <Route path="/cases" element={<ChatView />} />
          <Route path="/cases/:id" element={<ChatView />} />
          <Route path="/radar" element={<ChatView />} />
          <Route path="/reservations" element={<ChatView />} />
          <Route path="/quota" element={<ChatView />} />
          <Route path="/telemetry-intel" element={<ChatView />} />
          <Route path="/performance" element={<ChatView />} />
          <Route path="/notifications" element={<ChatView />} />
          <Route path="/monitor" element={<ChatView />} />
          <Route path="/stats" element={<ChatView />} />
          <Route path="/proactive" element={<ChatView />} />
          <Route path="/admin" element={<ChatView />} />
          <Route path="/admin/:section" element={<ChatView />} />
        </Routes>
      </div>
    </div>
  );
}
