import { Link, Navigate, Route, Routes } from "react-router-dom";
import ChatView from "./components/ChatView";
import { useAuth } from "./components/AuthContext";
import LoginPage, { ForcePasswordChange } from "./components/LoginPage";

export default function App() {
  const { user, loading, logout } = useAuth();

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

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b bg-brand-dark px-4 py-3 text-white">
        <div className="flex items-center gap-2">
          <span className="text-lg">🤖</span>
          <Link to="/dashboard" className="font-semibold">
            Azure Support Agent
          </Link>
        </div>
        <div className="flex items-center gap-4 text-sm">
          <Link to="/dashboard" className="rounded px-2 py-1 hover:bg-white/10">
            Dashboard
          </Link>
          {user.role === "admin" && (
            <Link to="/admin" className="rounded px-2 py-1 hover:bg-white/10">
              Settings
            </Link>
          )}
          <span className="rounded bg-white/10 px-2 py-1">
            {`${user.email} (${user.role})`}
          </span>
          <button
            onClick={() => void logout()}
            className="rounded px-2 py-1 hover:bg-white/10"
            title="Sign out"
          >
            Sign out
          </button>
        </div>
      </header>

      <div className="min-h-0 flex-1">
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<ChatView />} />
          <Route path="/chat" element={<ChatView />} />
          <Route path="/c/:chatId" element={<ChatView />} />
          <Route path="/automations" element={<ChatView />} />
          <Route path="/automations/:section" element={<ChatView />} />
          <Route path="/workloads" element={<ChatView />} />
          <Route path="/workloads/:id" element={<ChatView />} />
          <Route path="/inventory" element={<ChatView />} />
          <Route path="/inventory/:tab" element={<ChatView />} />
          <Route path="/assessments" element={<ChatView />} />
          <Route path="/assessments/:id" element={<ChatView />} />
          <Route path="/architectures" element={<ChatView />} />
          <Route path="/architectures/memory" element={<ChatView />} />
          <Route path="/architectures/:id" element={<ChatView />} />
          <Route path="/architectures/:id/memory" element={<ChatView />} />
          <Route path="/policy" element={<ChatView />} />
          <Route path="/policy/:tab" element={<ChatView />} />
          <Route path="/identity" element={<ChatView />} />
          <Route path="/identity/:tab" element={<ChatView />} />
          <Route path="/coverage" element={<ChatView />} />
          <Route path="/telemetry" element={<ChatView />} />
          <Route path="/backupdr" element={<ChatView />} />
          <Route path="/evidence" element={<ChatView />} />
          <Route path="/radar" element={<ChatView />} />
          <Route path="/telemetry-intel" element={<ChatView />} />
          <Route path="/performance" element={<ChatView />} />
          <Route path="/notifications" element={<ChatView />} />
          <Route path="/monitor" element={<ChatView />} />
          <Route path="/stats" element={<ChatView />} />
          <Route path="/admin" element={<ChatView />} />
          <Route path="/admin/:section" element={<ChatView />} />
        </Routes>
      </div>
    </div>
  );
}
