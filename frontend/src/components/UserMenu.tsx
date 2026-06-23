// Top-right signed-in-user menu: a popover with an "Active Role" switcher (act as one of the
// roles assigned to you, for the session), a Profile editor, and Sign out. Switching the
// active role or saving the profile refreshes the shared identity + invalidates cached
// queries so permission-gated UI (e.g. the left sidebar) re-renders without a manual refresh.
import { useEffect, useRef, useState } from "react";
import { api, type Me } from "../api";
import { queryClient } from "../queryClient";
import { roleLabel } from "../utils/roleLabel";
import { formatError } from "../utils/format";

export function UserMenu({ user, onLogout, onRefresh }: { user: Me; onLogout: () => void; onRefresh: () => void }) {
  const [open, setOpen] = useState(false);
  const [showProfile, setShowProfile] = useState(false);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const roles = user.assigned_roles ?? (user.role ? [user.role] : []);
  const activeRole = user.active_role || user.role;
  const name = user.display_name || user.email;

  const switchRole = async (role: string) => {
    if (role === activeRole) return;
    setBusy(true);
    try {
      await api.setActiveRole(role);
      // Refresh the shared identity, then drop every cached query so all permission-gated
      // UI (sidebar, dashboards, panels) refetches and re-renders under the new role.
      onRefresh();
      await queryClient.invalidateQueries();
      setOpen(false);
      setBusy(false);
    } catch (e) {
      alert(formatError(e));
      setBusy(false);
    }
  };

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-lg bg-white/10 px-2 py-1 text-sm hover:bg-white/20"
        title="Account"
      >
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-white/20 text-[11px] font-semibold uppercase">
          {(name[0] || "?").toUpperCase()}
        </span>
        <span className="hidden max-w-[160px] truncate sm:inline">{user.email}</span>
        <span className="text-white/60">▾</span>
      </button>

      {open && (
        <div className="absolute right-0 z-50 mt-2 w-72 rounded-xl border border-slate-200 bg-white text-slate-700 shadow-xl">
          <div className="border-b px-4 py-3">
            <div className="font-semibold text-slate-900">{name}</div>
            <div className="truncate text-xs text-slate-500">{user.email}</div>
          </div>
          <div className="px-4 py-3">
            <label className="mb-1 block text-xs font-medium text-slate-500">Active Role</label>
            <select
              value={activeRole}
              disabled={busy || roles.length <= 1}
              onChange={(e) => void switchRole(e.target.value)}
              className="w-full rounded-lg border border-slate-300 px-2.5 py-1.5 text-sm text-brand-dark focus:border-brand-dark focus:outline-none disabled:opacity-60"
            >
              {roles.map((r) => (
                <option key={r} value={r}>{roleLabel(r)}</option>
              ))}
            </select>
            <p className="mt-1 text-[11px] text-slate-400">
              {roles.length <= 1
                ? "You have a single role assigned."
                : "Selecting a different role applies it for this session."}
            </p>
          </div>
          <div className="border-t py-1">
            <button
              onClick={() => { setOpen(false); setShowProfile(true); }}
              className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm hover:bg-slate-50"
            >
              <span>👤</span> Profile
            </button>
            <button
              onClick={onLogout}
              className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm text-rose-600 hover:bg-rose-50"
            >
              <span>⏻</span> Sign out
            </button>
          </div>
        </div>
      )}

      {showProfile && <ProfileModal user={user} roles={roles} onClose={() => setShowProfile(false)} onSaved={onRefresh} />}
    </div>
  );
}

function ProfileModal({ user, roles, onClose, onSaved }: { user: Me; roles: string[]; onClose: () => void; onSaved: () => void }) {
  const [firstName, setFirstName] = useState(user.first_name ?? (user.display_name ?? "").split(" ")[0] ?? "");
  const [lastName, setLastName] = useState(user.last_name ?? (user.display_name ?? "").split(" ").slice(1).join(" "));
  const [activeRole, setActiveRole] = useState(user.active_role || user.role);
  const [defaultRole, setDefaultRole] = useState(user.default_role || user.role);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const initialActive = user.active_role || user.role;

  const save = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.updateProfile({ first_name: firstName, last_name: lastName, default_role: defaultRole });
      if (activeRole !== initialActive) {
        await api.setActiveRole(activeRole);
        await queryClient.invalidateQueries();
      }
      onSaved();
      onClose();
    } catch (e) {
      setErr(formatError(e));
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-md rounded-2xl bg-white text-slate-700 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-5 py-3">
          <h3 className="font-semibold text-slate-900">Update Profile</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">✕</button>
        </div>
        <div className="space-y-3 px-5 py-4">
          <Field label="Email">
            <input value={user.email} disabled className="w-full rounded-lg border border-slate-200 bg-slate-100 px-3 py-2 text-sm text-slate-500" />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="First Name">
              <input value={firstName} onChange={(e) => setFirstName(e.target.value)} className={inputCls} />
            </Field>
            <Field label="Last Name">
              <input value={lastName} onChange={(e) => setLastName(e.target.value)} className={inputCls} />
            </Field>
          </div>
          <Field label="Active Role">
            <select value={activeRole} onChange={(e) => setActiveRole(e.target.value)} disabled={roles.length <= 1} className={inputCls}>
              {roles.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
            </select>
          </Field>
          <Field label="Active Permission Group">
            <input value={roleLabel(activeRole)} disabled className="w-full rounded-lg border border-slate-200 bg-slate-100 px-3 py-2 text-sm text-slate-500" />
          </Field>
          <Field label="Default Role">
            <select value={defaultRole} onChange={(e) => setDefaultRole(e.target.value)} disabled={roles.length <= 1} className={inputCls}>
              {roles.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
            </select>
            <p className="mt-0.5 text-xs text-slate-400">The role new sessions start with.</p>
          </Field>
          {err && <div className="text-sm text-rose-600">{err}</div>}
        </div>
        <div className="flex justify-end gap-2 border-t bg-slate-50 px-5 py-3">
          <button onClick={onClose} className="rounded-lg border px-3 py-1.5 text-sm text-slate-600 hover:bg-white">Cancel</button>
          <button onClick={() => void save()} disabled={busy} className="rounded-lg bg-brand-dark px-4 py-1.5 text-sm font-medium text-white hover:bg-brand-dark/90 disabled:opacity-50">
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

const inputCls =
  "w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-brand-dark focus:outline-none";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium text-slate-700">{label}</span>
      {children}
    </label>
  );
}
