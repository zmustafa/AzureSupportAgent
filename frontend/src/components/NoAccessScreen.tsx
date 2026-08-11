import type { Me } from "../api";
import { roleLabel } from "../utils/roleLabel";
import { UserMenu } from "./UserMenu";

export function NoAccessScreen({
  user,
  onLogout,
  onRefresh,
}: {
  user: Me;
  onLogout: () => void;
  onRefresh: () => Promise<void>;
}) {
  const roles = user.assigned_roles ?? [];
  const canSwitch = roles.some((role) => role !== (user.active_role || user.role));

  return (
    <div className="flex h-full flex-col bg-slate-50">
      <header className="flex items-center justify-between border-b bg-brand-dark px-4 py-3 text-white">
        <div className="flex items-center gap-2 font-semibold">
          <span className="text-lg">🤖</span>
          Azure Support Agent
        </div>
        <UserMenu user={user} onLogout={onLogout} onRefresh={onRefresh} />
      </header>
      <main className="flex flex-1 items-center justify-center p-6">
        <section className="w-full max-w-lg rounded-2xl border border-amber-200 bg-white p-8 text-center shadow-sm">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-amber-100 text-2xl" aria-hidden>
            🔒
          </div>
          <h1 className="mt-4 text-xl font-semibold text-slate-900">Your account has no access</h1>
          <p className="mt-2 text-sm leading-6 text-slate-600">
            Contact an administrator to be granted a role with application permissions.
            Authentication succeeded, but this active role cannot read application data.
          </p>
          <div className="mt-5 rounded-xl bg-slate-50 px-4 py-3 text-left text-sm text-slate-600">
            <div className="flex justify-between gap-4">
              <span>Signed in as</span>
              <span className="truncate font-medium text-slate-800">{user.email}</span>
            </div>
            <div className="mt-1 flex justify-between gap-4">
              <span>Active role</span>
              <span className="font-medium text-slate-800">{roleLabel(user.active_role || user.role) || "No role"}</span>
            </div>
          </div>
          {canSwitch && (
            <p className="mt-4 text-xs text-slate-500">
              Another role is assigned. Use the account menu above to switch the active role and restore access.
            </p>
          )}
          <button
            type="button"
            onClick={onLogout}
            className="mt-5 rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            Sign out
          </button>
        </section>
      </main>
    </div>
  );
}
