import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  api,
  downloadBlob,
  HttpError,
  type AcGroup,
  type AcIdp,
  type AcRole,
  type AcUser,
  type AuthPolicies,
  type FirewallMode,
  type FirewallImportContext,
  type FirewallImportFormat,
  type FirewallImportPreview,
  type FirewallImportStrategy,
  type FirewallResolution,
  type FirewallRule,
  type IdpTestResult,
} from "../api";
import { apiBase } from "../api";
import { useAuth } from "./AuthContext";
import { roleLabel } from "../utils/roleLabel";
import {
  ACCESS_NAV,
  ACCESS_SUB_IDS,
  SECURITY_NAV,
  type SecuritySection,
} from "./navConfig";

export type { SecuritySection };
export { SECURITY_NAV, ACCESS_NAV, ACCESS_SUB_IDS };

// ------------------------------------------------------------------ shared bits
function Card({ title, children, actions }: { title: string; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <section className="rounded-lg border bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="font-medium">{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

function Btn({
  children,
  onClick,
  variant = "default",
  disabled,
  type = "button",
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: "default" | "primary" | "danger" | "ghost";
  disabled?: boolean;
  type?: "button" | "submit";
}) {
  const cls =
    variant === "primary"
      ? "bg-brand-dark text-white hover:bg-brand-dark/90"
      : variant === "danger"
      ? "border border-red-300 text-red-700 hover:bg-red-50"
      : variant === "ghost"
      ? "text-slate-600 hover:bg-slate-100"
      : "border border-slate-300 text-slate-700 hover:bg-slate-50";
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${cls}`}
    >
      {children}
    </button>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium text-slate-700">{label}</span>
      {children}
    </label>
  );
}

const inputCls =
  "w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-brand-dark focus:outline-none";

function errMsg(e: unknown): string {
  return e instanceof HttpError ? e.detail : "Something went wrong.";
}

// ================================================================= Users
function UsersCard() {
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["ac-users"], queryFn: api.acUsers });
  const roles = useQuery({ queryKey: ["ac-roles"], queryFn: api.acRoles });
  const groups = useQuery({ queryKey: ["ac-groups"], queryFn: api.acGroups });
  const [editing, setEditing] = useState<AcUser | null>(null);
  const [creating, setCreating] = useState(false);
  const [resetFor, setResetFor] = useState<AcUser | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["ac-users"] });
  };

  return (
    <Card
      title="Users"
      actions={
        <Btn variant="primary" onClick={() => { setCreating(true); setErr(null); }}>
          + New user
        </Btn>
      }
    >
      {err && <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}
      {creating && (
        <UserForm
          roles={roles.data ?? []}
          groups={groups.data ?? []}
          onClose={() => setCreating(false)}
          onSaved={() => { setCreating(false); invalidate(); }}
          onError={setErr}
        />
      )}
      {editing && (
        <UserEditForm
          user={editing}
          roles={roles.data ?? []}
          groups={groups.data ?? []}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); invalidate(); }}
          onError={setErr}
        />
      )}
      {resetFor && (
        <ResetPasswordForm
          user={resetFor}
          onClose={() => setResetFor(null)}
          onError={setErr}
        />
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs uppercase tracking-wide text-slate-500">
              <th className="py-2 pr-3">User</th>
              <th className="py-2 pr-3">Roles</th>
              <th className="py-2 pr-3">Source</th>
              <th className="py-2 pr-3">Status</th>
              <th className="py-2 pr-3">Last login</th>
              <th className="py-2" />
            </tr>
          </thead>
          <tbody>
            {users.data?.map((u) => (
              <tr key={u.id} className="border-b last:border-0">
                <td className="py-2 pr-3">
                  <div className="font-medium text-slate-800">{u.display_name || u.username}</div>
                  <div className="text-xs text-slate-500">{u.email}</div>
                </td>
                <td className="py-2 pr-3">
                  <div className="flex flex-wrap gap-1">
                    {u.role_names.length === 0 && <span className="text-xs text-slate-400">—</span>}
                    {u.role_names.map((n) => (
                      <span key={n} className="rounded bg-slate-100 px-1.5 py-0.5 text-xs">{roleLabel(n)}</span>
                    ))}
                  </div>
                </td>
                <td className="py-2 pr-3 text-xs text-slate-500">{u.auth_source}</td>
                <td className="py-2 pr-3">
                  <span
                    className={`rounded px-1.5 py-0.5 text-xs ${
                      u.status === "active"
                        ? "bg-emerald-100 text-emerald-700"
                        : "bg-slate-200 text-slate-600"
                    }`}
                  >
                    {u.status}
                  </span>
                  {u.locked && (
                    <span className="ml-1 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-700">locked</span>
                  )}
                </td>
                <td className="py-2 pr-3 text-xs text-slate-500">
                  {u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "never"}
                </td>
                <td className="py-2">
                  <div className="flex justify-end gap-1">
                    <Btn variant="ghost" onClick={() => { setEditing(u); setErr(null); }}>Edit</Btn>
                    {u.auth_source === "local" && (
                      <Btn variant="ghost" onClick={() => { setResetFor(u); setErr(null); }}>Reset PW</Btn>
                    )}
                    <Btn
                      variant="ghost"
                      onClick={async () => {
                        try { await api.acRevokeUserSessions(u.id); }
                        catch (e) { setErr(errMsg(e)); }
                      }}
                    >
                      Sign out
                    </Btn>
                    <Btn
                      variant="danger"
                      onClick={async () => {
                        if (!confirm(`Delete user ${u.username}? This cannot be undone.`)) return;
                        try { await api.acDeleteUser(u.id); invalidate(); }
                        catch (e) { setErr(errMsg(e)); }
                      }}
                    >
                      Delete
                    </Btn>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {users.data?.length === 0 && (
          <p className="py-4 text-sm text-slate-500">No users yet.</p>
        )}
      </div>
      <p className="mt-3 text-xs text-slate-400">
        Roles shown combine directly-assigned roles and roles inherited from groups.
      </p>
    </Card>
  );
}

function RoleGroupPickers({
  roles,
  groups,
  roleIds,
  groupIds,
  setRoleIds,
  setGroupIds,
}: {
  roles: AcRole[];
  groups: AcGroup[];
  roleIds: string[];
  groupIds: string[];
  setRoleIds: (v: string[]) => void;
  setGroupIds: (v: string[]) => void;
}) {
  const toggle = (arr: string[], id: string, set: (v: string[]) => void) =>
    set(arr.includes(id) ? arr.filter((x) => x !== id) : [...arr, id]);
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <div>
        <span className="mb-1 block text-sm font-medium text-slate-700">Roles</span>
        <div className="flex flex-wrap gap-1.5">
          {roles.map((r) => (
            <button
              key={r.id}
              type="button"
              onClick={() => toggle(roleIds, r.id, setRoleIds)}
              className={`rounded-full border px-2.5 py-1 text-xs ${
                roleIds.includes(r.id)
                  ? "border-brand-dark bg-brand-dark/10 text-brand-dark"
                  : "border-slate-300 text-slate-600"
              }`}
            >
              {roleLabel(r.name)}
            </button>
          ))}
        </div>
      </div>
      <div>
        <span className="mb-1 block text-sm font-medium text-slate-700">Groups</span>
        <div className="flex flex-wrap gap-1.5">
          {groups.length === 0 && <span className="text-xs text-slate-400">No groups defined</span>}
          {groups.map((g) => (
            <button
              key={g.id}
              type="button"
              onClick={() => toggle(groupIds, g.id, setGroupIds)}
              className={`rounded-full border px-2.5 py-1 text-xs ${
                groupIds.includes(g.id)
                  ? "border-brand-dark bg-brand-dark/10 text-brand-dark"
                  : "border-slate-300 text-slate-600"
              }`}
            >
              {g.name}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function UserForm({
  roles,
  groups,
  onClose,
  onSaved,
  onError,
}: {
  roles: AcRole[];
  groups: AcGroup[];
  onClose: () => void;
  onSaved: () => void;
  onError: (m: string) => void;
}) {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [roleIds, setRoleIds] = useState<string[]>([]);
  const [groupIds, setGroupIds] = useState<string[]>([]);
  const [mustChange, setMustChange] = useState(true);
  const save = useMutation({
    mutationFn: () =>
      api.acCreateUser({
        username,
        email,
        display_name: displayName,
        password: password || null,
        role_ids: roleIds,
        group_ids: groupIds,
        must_change_password: mustChange,
      }),
    onSuccess: onSaved,
    onError: (e) => onError(errMsg(e)),
  });
  return (
    <div className="mb-4 rounded-lg border border-brand-dark/30 bg-slate-50 p-4">
      <h3 className="mb-3 text-sm font-semibold">New user</h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Username"><input className={inputCls} value={username} onChange={(e) => setUsername(e.target.value)} /></Field>
        <Field label="Email"><input className={inputCls} value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
        <Field label="Display name"><input className={inputCls} value={displayName} onChange={(e) => setDisplayName(e.target.value)} /></Field>
        <Field label="Initial password (optional for SSO-only)"><input type="password" name="new-user-password" autoComplete="new-password" data-1p-ignore data-lpignore="true" className={inputCls} value={password} onChange={(e) => setPassword(e.target.value)} /></Field>
      </div>
      <div className="mt-3">
        <RoleGroupPickers roles={roles} groups={groups} roleIds={roleIds} groupIds={groupIds} setRoleIds={setRoleIds} setGroupIds={setGroupIds} />
      </div>
      <label className="mt-3 flex items-center gap-2 text-sm text-slate-600">
        <input type="checkbox" checked={mustChange} onChange={(e) => setMustChange(e.target.checked)} />
        Require password change on first sign-in
      </label>
      <div className="mt-4 flex gap-2">
        <Btn variant="primary" disabled={!username || !email || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Creating…" : "Create user"}
        </Btn>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
      </div>
    </div>
  );
}

function UserEditForm({
  user,
  roles,
  groups,
  onClose,
  onSaved,
  onError,
}: {
  user: AcUser;
  roles: AcRole[];
  groups: AcGroup[];
  onClose: () => void;
  onSaved: () => void;
  onError: (m: string) => void;
}) {
  const [email, setEmail] = useState(user.email);
  const [displayName, setDisplayName] = useState(user.display_name);
  const [status, setStatus] = useState(user.status);
  const [roleIds, setRoleIds] = useState<string[]>(user.role_ids);
  const [groupIds, setGroupIds] = useState<string[]>(user.group_ids);
  const save = useMutation({
    mutationFn: () =>
      api.acUpdateUser(user.id, {
        email,
        display_name: displayName,
        status,
        role_ids: roleIds,
        group_ids: groupIds,
      }),
    onSuccess: onSaved,
    onError: (e) => onError(errMsg(e)),
  });
  return (
    <div className="mb-4 rounded-lg border border-brand-dark/30 bg-slate-50 p-4">
      <h3 className="mb-3 text-sm font-semibold">Edit {user.username}</h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Email"><input className={inputCls} value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
        <Field label="Display name"><input className={inputCls} value={displayName} onChange={(e) => setDisplayName(e.target.value)} /></Field>
        <Field label="Status">
          <select className={inputCls} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="active">Active</option>
            <option value="disabled">Disabled</option>
          </select>
        </Field>
      </div>
      <div className="mt-3">
        <RoleGroupPickers roles={roles} groups={groups} roleIds={roleIds} groupIds={groupIds} setRoleIds={setRoleIds} setGroupIds={setGroupIds} />
      </div>
      <div className="mt-4 flex gap-2">
        <Btn variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save changes"}
        </Btn>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
      </div>
    </div>
  );
}

function ResetPasswordForm({
  user,
  onClose,
  onError,
}: {
  user: AcUser;
  onClose: () => void;
  onError: (m: string) => void;
}) {
  const [pw, setPw] = useState("");
  const [mustChange, setMustChange] = useState(true);
  const [done, setDone] = useState(false);
  const save = useMutation({
    mutationFn: () => api.acResetPassword(user.id, pw, mustChange),
    onSuccess: () => setDone(true),
    onError: (e) => onError(errMsg(e)),
  });
  return (
    <div className="mb-4 rounded-lg border border-amber-300 bg-amber-50 p-4">
      <h3 className="mb-3 text-sm font-semibold">Reset password — {user.username}</h3>
      {done ? (
        <div className="flex items-center gap-3">
          <span className="text-sm text-emerald-700">Password updated. Active sessions were signed out.</span>
          <Btn variant="ghost" onClick={onClose}>Close</Btn>
        </div>
      ) : (
        <>
          <Field label="New password">
            <input type="password" name="reset-user-password" autoComplete="new-password" data-1p-ignore data-lpignore="true" className={inputCls} value={pw} onChange={(e) => setPw(e.target.value)} />
          </Field>
          <label className="mt-3 flex items-center gap-2 text-sm text-slate-600">
            <input type="checkbox" checked={mustChange} onChange={(e) => setMustChange(e.target.checked)} />
            Require change on next sign-in
          </label>
          <div className="mt-4 flex gap-2">
            <Btn variant="primary" disabled={!pw || save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? "Saving…" : "Set password"}
            </Btn>
            <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
          </div>
        </>
      )}
    </div>
  );
}

// ================================================================= Roles
function RolesCard() {
  const qc = useQueryClient();
  const roles = useQuery({ queryKey: ["ac-roles"], queryFn: api.acRoles });
  const perms = useQuery({ queryKey: ["ac-permissions"], queryFn: api.acPermissions });
  const [editing, setEditing] = useState<AcRole | null>(null);
  const [creating, setCreating] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const invalidate = () => void qc.invalidateQueries({ queryKey: ["ac-roles"] });

  return (
    <Card
      title="Roles"
      actions={<Btn variant="primary" onClick={() => { setCreating(true); setEditing(null); setErr(null); }}>+ New role</Btn>}
    >
      {err && <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}
      {(creating || editing) && (
        <RoleForm
          role={editing}
          permissions={perms.data ?? []}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={() => { setCreating(false); setEditing(null); invalidate(); }}
          onError={setErr}
        />
      )}
      <div className="space-y-2">
        {roles.data?.map((r) => (
          <div key={r.id} className="rounded border p-3">
            <div className="flex items-start justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-medium text-slate-800">{roleLabel(r.name)}</span>
                  {r.is_system && <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">system</span>}
                </div>
                <div className="text-xs text-slate-500">{r.description}</div>
              </div>
              {!r.is_system && (
                <div className="flex gap-1">
                  <Btn variant="ghost" onClick={() => { setEditing(r); setCreating(false); setErr(null); }}>Edit</Btn>
                  <Btn
                    variant="danger"
                    onClick={async () => {
                      if (!confirm(`Delete role ${r.name}?`)) return;
                      try { await api.acDeleteRole(r.id); invalidate(); }
                      catch (e) { setErr(errMsg(e)); }
                    }}
                  >
                    Delete
                  </Btn>
                </div>
              )}
            </div>
            <div className="mt-2 flex flex-wrap gap-1">
              {r.permissions.map((p) => (
                <span key={p} className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">{p}</span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

function RoleForm({
  role,
  permissions,
  onClose,
  onSaved,
  onError,
}: {
  role: AcRole | null;
  permissions: { key: string; label: string; group?: string }[];
  onClose: () => void;
  onSaved: () => void;
  onError: (m: string) => void;
}) {
  const [name, setName] = useState(role?.name ?? "");
  const [description, setDescription] = useState(role?.description ?? "");
  const [selected, setSelected] = useState<string[]>(role?.permissions ?? []);
  const toggle = (k: string) => setSelected((s) => (s.includes(k) ? s.filter((x) => x !== k) : [...s, k]));
  // Group the catalog into ordered sections (mirrors the product nav) so the editor is
  // readable as the permission set grows. Permissions without a group fall under "Other".
  const groups = useMemo(() => {
    const order: string[] = [];
    const byGroup = new Map<string, { key: string; label: string }[]>();
    for (const p of permissions) {
      const g = p.group || "Other";
      if (!byGroup.has(g)) {
        byGroup.set(g, []);
        order.push(g);
      }
      byGroup.get(g)!.push(p);
    }
    return order.map((g) => ({ group: g, items: byGroup.get(g)! }));
  }, [permissions]);
  const setGroup = (keys: string[], on: boolean) =>
    setSelected((s) => (on ? Array.from(new Set([...s, ...keys])) : s.filter((x) => !keys.includes(x))));
  const save = useMutation({
    mutationFn: () =>
      role
        ? api.acUpdateRole(role.id, { name, description, permissions: selected })
        : api.acCreateRole({ name, description, permissions: selected }),
    onSuccess: onSaved,
    onError: (e) => onError(errMsg(e)),
  });
  return (
    <div className="mb-4 rounded-lg border border-brand-dark/30 bg-slate-50 p-4">
      <h3 className="mb-3 text-sm font-semibold">{role ? `Edit role — ${role.name}` : "New role"}</h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Description"><input className={inputCls} value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
      </div>
      <div className="mt-3">
        <span className="mb-1 block text-sm font-medium text-slate-700">Permissions</span>
        <div className="space-y-3">
          {groups.map(({ group, items }) => {
            const keys = items.map((p) => p.key);
            const allOn = keys.every((k) => selected.includes(k));
            const someOn = keys.some((k) => selected.includes(k));
            return (
              <div key={group} className="rounded border bg-white">
                <div className="flex items-center justify-between border-b bg-slate-50 px-3 py-1.5">
                  <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">{group}</span>
                  <button
                    type="button"
                    className="text-xs font-medium text-brand-dark hover:underline"
                    onClick={() => setGroup(keys, !allOn)}
                  >
                    {allOn ? "Clear" : someOn ? "Select all" : "Select all"}
                  </button>
                </div>
                <div className="grid grid-cols-1 gap-1.5 p-2 sm:grid-cols-2">
                  {items.map((p) => (
                    <label key={p.key} className="flex items-start gap-2 rounded border bg-white px-2 py-1.5 text-sm">
                      <input type="checkbox" checked={selected.includes(p.key)} onChange={() => toggle(p.key)} className="mt-0.5" />
                      <span>
                        <span className="font-medium text-slate-700">{p.label}</span>
                        <span className="block font-mono text-xs text-slate-400">{p.key}</span>
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>
      <div className="mt-4 flex gap-2">
        <Btn variant="primary" disabled={!name || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : role ? "Save role" : "Create role"}
        </Btn>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
      </div>
    </div>
  );
}

// ================================================================= Groups
function GroupsCard() {
  const qc = useQueryClient();
  const groups = useQuery({ queryKey: ["ac-groups"], queryFn: api.acGroups });
  const roles = useQuery({ queryKey: ["ac-roles"], queryFn: api.acRoles });
  const [editing, setEditing] = useState<AcGroup | null>(null);
  const [creating, setCreating] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const invalidate = () => void qc.invalidateQueries({ queryKey: ["ac-groups"] });

  return (
    <Card
      title="Groups"
      actions={<Btn variant="primary" onClick={() => { setCreating(true); setEditing(null); setErr(null); }}>+ New group</Btn>}
    >
      {err && <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}
      {(creating || editing) && (
        <GroupForm
          group={editing}
          roles={roles.data ?? []}
          onClose={() => { setCreating(false); setEditing(null); }}
          onSaved={() => { setCreating(false); setEditing(null); invalidate(); }}
          onError={setErr}
        />
      )}
      <div className="space-y-2">
        {groups.data?.length === 0 && <p className="text-sm text-slate-500">No groups yet.</p>}
        {groups.data?.map((g) => (
          <div key={g.id} className="flex items-start justify-between rounded border p-3">
            <div>
              <div className="font-medium text-slate-800">{g.name}</div>
              <div className="text-xs text-slate-500">{g.description}</div>
              <div className="mt-1 flex flex-wrap gap-1">
                {g.role_ids.map((rid) => (
                  <span key={rid} className="rounded bg-slate-100 px-1.5 py-0.5 text-xs">
                    {roles.data?.find((r) => r.id === rid)?.name ?? rid}
                  </span>
                ))}
                <span className="text-xs text-slate-400">· {g.member_count ?? 0} members</span>
              </div>
            </div>
            <div className="flex gap-1">
              <Btn variant="ghost" onClick={() => { setEditing(g); setCreating(false); setErr(null); }}>Edit</Btn>
              <Btn
                variant="danger"
                onClick={async () => {
                  if (!confirm(`Delete group ${g.name}?`)) return;
                  try { await api.acDeleteGroup(g.id); invalidate(); }
                  catch (e) { setErr(errMsg(e)); }
                }}
              >
                Delete
              </Btn>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

function GroupForm({
  group,
  roles,
  onClose,
  onSaved,
  onError,
}: {
  group: AcGroup | null;
  roles: AcRole[];
  onClose: () => void;
  onSaved: () => void;
  onError: (m: string) => void;
}) {
  const [name, setName] = useState(group?.name ?? "");
  const [description, setDescription] = useState(group?.description ?? "");
  const [roleIds, setRoleIds] = useState<string[]>(group?.role_ids ?? []);
  const toggle = (id: string) => setRoleIds((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
  const save = useMutation({
    mutationFn: () =>
      group
        ? api.acUpdateGroup(group.id, { name, description, role_ids: roleIds })
        : api.acCreateGroup({ name, description, role_ids: roleIds }),
    onSuccess: onSaved,
    onError: (e) => onError(errMsg(e)),
  });
  return (
    <div className="mb-4 rounded-lg border border-brand-dark/30 bg-slate-50 p-4">
      <h3 className="mb-3 text-sm font-semibold">{group ? `Edit group — ${group.name}` : "New group"}</h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Description"><input className={inputCls} value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
      </div>
      <div className="mt-3">
        <span className="mb-1 block text-sm font-medium text-slate-700">Roles granted to members</span>
        <div className="flex flex-wrap gap-1.5">
          {roles.map((r) => (
            <button
              key={r.id}
              type="button"
              onClick={() => toggle(r.id)}
              className={`rounded-full border px-2.5 py-1 text-xs ${
                roleIds.includes(r.id) ? "border-brand-dark bg-brand-dark/10 text-brand-dark" : "border-slate-300 text-slate-600"
              }`}
            >
              {roleLabel(r.name)}
            </button>
          ))}
        </div>
      </div>
      <div className="mt-4 flex gap-2">
        <Btn variant="primary" disabled={!name || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : group ? "Save group" : "Create group"}
        </Btn>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
      </div>
    </div>
  );
}

// ================================================================= Identity Providers
const OIDC_FIELDS: { key: string; label: string; secret?: boolean; placeholder?: string; default?: string }[] = [
  { key: "issuer", label: "Issuer URL", placeholder: "https://login.microsoftonline.com/<tenant>/v2.0" },
  { key: "discovery_url", label: "Discovery URL (optional)", placeholder: "Defaults to <issuer>/.well-known/openid-configuration" },
  { key: "client_id", label: "Client ID" },
  { key: "client_secret", label: "Client secret", secret: true },
  { key: "scopes", label: "Scopes", placeholder: "openid email profile", default: "openid email profile" },
  { key: "group_claim", label: "Group claim", placeholder: "groups", default: "groups" },
];
const SAML_FIELDS: { key: string; label: string; placeholder?: string }[] = [
  { key: "entity_id", label: "IdP Entity ID (Issuer)" },
  { key: "sso_url", label: "IdP SSO URL" },
  { key: "certificate", label: "IdP signing certificate (PEM or base64)" },
  { key: "email_attr", label: "Email attribute (optional)" },
  { key: "name_attr", label: "Name attribute (optional)" },
  { key: "group_attr", label: "Group attribute (optional)" },
];

function IdentityProvidersCard() {
  const qc = useQueryClient();
  const idps = useQuery({ queryKey: ["ac-idps"], queryFn: api.acIdps });
  const [editing, setEditing] = useState<AcIdp | null>(null);
  const [creating, setCreating] = useState<"oidc" | "saml" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const invalidate = () => void qc.invalidateQueries({ queryKey: ["ac-idps"] });

  return (
    <Card
      title="Sign-in & Single Sign-On"
      actions={
        <div className="flex gap-2">
          <Btn variant="primary" onClick={() => { setCreating("oidc"); setEditing(null); setErr(null); }}>+ OIDC</Btn>
          <Btn variant="primary" onClick={() => { setCreating("saml"); setEditing(null); setErr(null); }}>+ SAML</Btn>
        </div>
      }
    >
      {err && <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}
      <p className="mb-3 text-sm text-slate-500">
        Connect Microsoft Entra ID, Okta, Auth0, Google, ADFS, PingFederate, or any
        OIDC/SAML 2.0 provider. Local password sign-in is configured under{" "}
        <Link to="/admin/policies" className="font-semibold text-brand-dark hover:underline">
          Security Policy
        </Link>
        .
      </p>
      {(creating || editing) && (
        <IdpForm
          idp={editing}
          type={editing?.type ?? creating ?? "oidc"}
          onClose={() => { setCreating(null); setEditing(null); }}
          onSaved={() => { setCreating(null); setEditing(null); invalidate(); }}
          onError={setErr}
        />
      )}
      <div className="space-y-2">
        {idps.data?.length === 0 && <p className="text-sm text-slate-500">No identity providers configured.</p>}
        {idps.data?.map((p) => (
          <div key={p.id} className="flex items-center justify-between rounded border p-3">
            <div>
              <div className="flex items-center gap-2">
                <span className="font-medium text-slate-800">{p.name}</span>
                <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs uppercase text-slate-500">{p.type}</span>
                <span className={`rounded px-1.5 py-0.5 text-xs ${p.enabled ? "bg-emerald-100 text-emerald-700" : "bg-slate-200 text-slate-600"}`}>
                  {p.enabled ? "enabled" : "disabled"}
                </span>
              </div>
              <div className="text-xs text-slate-500">Button: {p.button_label || p.name}</div>
              <IdpPreview idp={p} />
            </div>
            <div className="flex gap-1">
              <Btn variant="ghost" onClick={() => { setEditing(p); setCreating(null); setErr(null); }}>Edit</Btn>
              <Btn
                variant="danger"
                onClick={async () => {
                  if (!confirm(`Delete identity provider ${p.name}?`)) return;
                  try { await api.acDeleteIdp(p.id); invalidate(); }
                  catch (e) { setErr(errMsg(e)); }
                }}
              >
                Delete
              </Btn>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

// Compact read-only preview of an IdP's key config, shown under each provider in the list
// (issuer + client id + claims for OIDC; entity id + SSO URL for SAML). Secrets are masked
// server-side (config[client_secret]="" + client_secret_set=true), so nothing sensitive leaks.
function IdpPreview({ idp }: { idp: AcIdp }) {
  const cfg = (idp.config ?? {}) as Record<string, unknown>;
  const s = (k: string) => {
    const v = cfg[k];
    return typeof v === "string" ? v.trim() : "";
  };
  const rows: { label: string; value: string; mono?: boolean }[] = [];
  if (idp.type === "saml") {
    if (s("entity_id")) rows.push({ label: "Entity ID", value: s("entity_id"), mono: true });
    if (s("sso_url")) rows.push({ label: "SSO URL", value: s("sso_url"), mono: true });
    rows.push({ label: "Certificate", value: cfg["certificate_set"] ? "configured" : (s("certificate") ? "set" : "not set") });
    const attrs = [s("email_attr") && `email=${s("email_attr")}`, s("group_attr") && `groups=${s("group_attr")}`].filter(Boolean).join(", ");
    if (attrs) rows.push({ label: "Attributes", value: attrs });
  } else {
    if (s("issuer")) rows.push({ label: "Issuer", value: s("issuer"), mono: true });
    if (s("client_id")) rows.push({ label: "Client ID", value: s("client_id"), mono: true });
    rows.push({ label: "Client secret", value: cfg["client_secret_set"] ? "configured" : "not set" });
    if (s("scopes")) rows.push({ label: "Scopes", value: s("scopes"), mono: true });
    if (s("group_claim")) rows.push({ label: "Group claim", value: s("group_claim"), mono: true });
  }
  if (rows.length === 0) return null;
  return (
    <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-slate-500">
      {rows.map((r) => (
        <span key={r.label} className="inline-flex max-w-full items-baseline gap-1">
          <span className="text-slate-400">{r.label}:</span>
          <span className={`truncate ${r.mono ? "font-mono" : ""} text-slate-600`} title={r.value}>{r.value}</span>
        </span>
      ))}
    </div>
  );
}

// Small copy-to-clipboard button used in the SSO setup guide.
function CopyBtn({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch { /* clipboard blocked */ }
      }}
      className="shrink-0 rounded border border-slate-300 px-1.5 py-0.5 text-[11px] font-medium text-slate-600 hover:bg-slate-100"
      title="Copy"
    >
      {copied ? "✓ Copied" : "Copy"}
    </button>
  );
}

// Collapsible, step-by-step guide for registering an app in Microsoft Entra ID (and the
// equivalent SAML steps). Shows the EXACT Redirect URI/ACS URL for this provider so the user
// can paste it straight into the Entra "Authentication → Add a platform → Web" screen.
function SsoSetupGuide({ type, redirectUri, metadataUrl, hasId }: { type: string; redirectUri: string; metadataUrl: string; hasId: boolean }) {
  const [open, setOpen] = useState(false);
  const isSaml = type === "saml";
  return (
    <div className="mt-3 rounded-lg border border-indigo-200 bg-indigo-50/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-sm font-medium text-indigo-800"
      >
        <span className="flex items-center gap-2">
          <span>📘</span> Setup Guide — register this app in Microsoft Entra ID
        </span>
        <span className="text-indigo-400">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-indigo-100 px-4 py-3 text-xs text-slate-600">
          {!hasId && (
            <div className="rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-amber-700">
              Tip: <b>Create the provider first</b> (button below), then re-open this guide — the
              Redirect URI will then contain the real provider ID to paste into Entra.
            </div>
          )}

          <Step n={1} title="Open App registrations">
            In the <a className="text-indigo-600 underline" href="https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade" target="_blank" rel="noreferrer">Microsoft Entra admin center</a> →
            <b> Identity → Applications → App registrations</b> → <b>New registration</b>.
          </Step>

          <Step n={2} title="Register the application">
            Give it a name (e.g. <span className="font-mono">AzSupAgent SignIn</span>). For
            <b> Supported account types</b> choose <i>“Accounts in this organizational directory only”</i>
            (single tenant) unless you need multi-tenant. Leave the Redirect URI blank here — you add it
            in the next step. Click <b>Register</b>.
          </Step>

          {isSaml ? (
            <Step n={3} title="Set up SAML SSO">
              In the app → <b>Single sign-on</b> (or expose the SAML endpoints), set the
              <b> Reply (ACS) URL</b> and <b>Identifier (Entity ID)</b> to the values below, and enable
              <b> “Sign assertions”</b>. Then download the IdP <b>signing certificate</b> + note the
              <b> Login URL</b> / <b>Entra Identifier</b> to paste into this form.
              <CopyRow label="ACS (Reply) URL" value={redirectUri} />
              <CopyRow label="SP Metadata" value={metadataUrl} />
            </Step>
          ) : (
            <Step n={3} title="Add the Web redirect URI">
              In the app → <b>Manage → Authentication</b> → <b>Add a platform (or Add Redirect URI)</b> → <b>Web </b> 
              (not “Single-page application” — this app uses a confidential client with a secret).
              Paste this exact <b>Redirect URI</b> and click <b>Configure</b>:
              <CopyRow label="Redirect URI" value={redirectUri} />
            </Step>
          )}

          {!isSaml && (
            <>
              <Step n={4} title="Create a client secret">
                App → <b>Certificates &amp; secrets</b> → <b>Client secrets</b> → <b>New client secret</b>.
                Copy the secret’s <b>Value</b> immediately (you can’t see it again) and paste it into the
                <b> Client secret</b> field in this form.
              </Step>
              <Step n={5} title="Copy the IDs">
                App → <b>Overview</b>. Copy <b>Application (client) ID</b> → paste into <b>Client ID</b> here.
                Copy <b>Directory (tenant) ID</b> → use it in the <b>Issuer URL</b>:
                <CopyRow label="Issuer URL" value="https://login.microsoftonline.com/<tenant-id>/v2.0" />
                (replace <span className="font-mono">&lt;tenant-id&gt;</span> with your Directory ID).
              </Step>
              <Step n={6} title="(Optional) Group claims">
                To map directory groups to roles, App → <b>Token configuration</b> → <b>Add groups claim</b>,
                then set this form’s <b>Group claim</b> to <span className="font-mono">groups</span>.
              </Step>
            </>
          )}

          <Step n={isSaml ? 4 : 7} title="Finish here">
            Fill the fields above ({isSaml ? "Entity ID, SSO URL, certificate" : "Issuer URL, Client ID, Client secret, Scopes"}),
            tick <b>Enabled</b>, and click <b>{hasId ? "Save provider" : "Create provider"}</b>. A sign-in
            button then appears on the login page.
          </Step>
        </div>
      )}
    </div>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2.5">
      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-indigo-600 text-[11px] font-semibold text-white">{n}</span>
      <div className="min-w-0">
        <div className="font-semibold text-slate-700">{title}</div>
        <div className="mt-0.5 leading-relaxed">{children}</div>
      </div>
    </div>
  );
}

function CopyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="mt-1.5 flex items-center gap-2 rounded border border-slate-200 bg-white px-2 py-1">
      <span className="shrink-0 text-[11px] font-medium text-slate-400">{label}</span>
      <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-slate-700" title={value}>{value}</span>
      <CopyBtn value={value} />
    </div>
  );
}

function IdpForm({
  idp,
  type,
  onClose,
  onSaved,
  onError,
}: {
  idp: AcIdp | null;
  type: string;
  onClose: () => void;
  onSaved: () => void;
  onError: (m: string) => void;
}) {
  const [name, setName] = useState(idp?.name ?? "");
  const [enabled, setEnabled] = useState(idp?.enabled ?? false);
  const [buttonLabel, setButtonLabel] = useState(idp?.button_label ?? "");
  const initialCfg = (idp?.config ?? {}) as Record<string, unknown>;
  const [cfg, setCfg] = useState<Record<string, string>>(() => {
    const out: Record<string, string> = {};
    // For a NEW provider, pre-fill sensible defaults (e.g. OIDC scopes + group claim) so
    // they're real saved values, not just greyed-out placeholder hints.
    if (!idp) {
      (type === "saml" ? SAML_FIELDS : OIDC_FIELDS).forEach((f) => {
        const def = (f as { default?: string }).default;
        if (def) out[f.key] = def;
      });
    }
    Object.entries(initialCfg).forEach(([k, v]) => {
      if (typeof v === "string") out[k] = v;
    });
    return out;
  });
  const fields = type === "saml" ? SAML_FIELDS : OIDC_FIELDS;
  const setF = (k: string, v: string) => setCfg((c) => ({ ...c, [k]: v }));
  // The redirect/ACS/metadata URLs the admin pastes into the IdP must be ABSOLUTE. On a
  // same-origin prod build apiBase is just "/api" (relative), so resolve it against the page
  // origin — which equals the backend's PUBLIC_BASE_URL, the value the backend actually sends
  // as redirect_uri. In dev apiBase is already absolute (http://localhost:8000/api) and is
  // returned unchanged.
  const absApiBase = (() => {
    try { return new URL(apiBase, window.location.origin).href.replace(/\/$/, ""); }
    catch { return apiBase; }
  })();
  const redirectUri = `${absApiBase}/auth/${type === "saml" ? "saml" : "oidc"}/${idp?.id ?? "<id>"}/${type === "saml" ? "acs" : "callback"}`;
  const metadataUrl = `${absApiBase}/auth/saml/${idp?.id ?? "<id>"}/metadata`;

  const save = useMutation({
    mutationFn: () =>
      idp
        ? api.acUpdateIdp(idp.id, { name, type, enabled, button_label: buttonLabel, config: cfg })
        : api.acCreateIdp({ name, type, enabled, button_label: buttonLabel, config: cfg }),
    onSuccess: onSaved,
    onError: (e) => onError(errMsg(e)),
  });

  // Best-effort connection test (OIDC discovery+JWKS / SAML cert parse). Does NOT save.
  const [testResult, setTestResult] = useState<IdpTestResult | null>(null);
  const test = useMutation({
    mutationFn: () => api.acTestIdp({ name, type, enabled, button_label: buttonLabel, config: cfg }, idp?.id),
    onSuccess: (r) => setTestResult(r),
    onError: (e) => setTestResult({ ok: false, summary: errMsg(e), checks: [] }),
  });

  return (
    <div className="mb-4 rounded-lg border border-brand-dark/30 bg-slate-50 p-4">
      <h3 className="mb-3 text-sm font-semibold">
        {idp ? `Edit ${type.toUpperCase()} provider` : `New ${type.toUpperCase()} provider`}
      </h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Display name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Sign-in button label"><input className={inputCls} value={buttonLabel} onChange={(e) => setButtonLabel(e.target.value)} placeholder={`Sign in with ${name || type.toUpperCase()}`} /></Field>
      </div>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
        {fields.map((f) => {
          const isSecret = "secret" in f && f.secret;
          const secretSet = isSecret && (initialCfg[`${f.key}_set`] as boolean);
          return (
            <Field key={f.key} label={f.label}>
              {f.key === "certificate" ? (
                <textarea className={`${inputCls} h-24 font-mono text-xs`} value={cfg[f.key] ?? ""} onChange={(e) => setF(f.key, e.target.value)} placeholder={f.placeholder} autoComplete="off" />
              ) : (
                <input
                  type={isSecret ? "password" : "text"}
                  // Block the browser's username/password autofill from clobbering these
                  // fields (Client ID was getting "admin", the secret a saved password). A
                  // non-credential field name + autoComplete="new-password" is the most
                  // reliable combo in Chrome; the data-* attrs silence 1Password/LastPass.
                  name={`idp-${type}-${f.key}`}
                  autoComplete="new-password"
                  data-1p-ignore
                  data-lpignore="true"
                  data-form-type="other"
                  className={inputCls}
                  value={cfg[f.key] ?? ""}
                  onChange={(e) => setF(f.key, e.target.value)}
                  placeholder={secretSet ? "•••••• (leave blank to keep)" : f.placeholder}
                />
              )}
            </Field>
          );
        })}
      </div>
      {type !== "saml" && (
        <label className="mt-3 flex items-start gap-2 text-sm text-slate-600">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={(cfg.login_prompt ?? "") === "select_account"}
            onChange={(e) => setF("login_prompt", e.target.checked ? "select_account" : "")}
          />
          <span>
            Select account upon sign in
            <span className="mt-0.5 block text-xs text-slate-400">
              Always show the account picker (OIDC <span className="font-mono">prompt=select_account</span>)
              instead of silently reusing an existing IdP session — useful on shared machines.
            </span>
          </span>
        </label>
      )}
      <div className="mt-3 rounded border border-slate-200 bg-white p-3 text-xs text-slate-500">
        <div className="font-medium text-slate-600">Configure at your IdP:</div>
        {type === "saml" ? (
          <>
            <div>ACS (Reply) URL: <span className="font-mono">{redirectUri}</span></div>
            <div>SP Metadata: <span className="font-mono">{metadataUrl}</span></div>
          </>
        ) : (
          <div>Redirect URI: <span className="font-mono">{redirectUri}</span></div>
        )}
        {!idp && <div className="mt-1 text-amber-600">Save first to get the real provider ID in these URLs.</div>}
      </div>
      <SsoSetupGuide type={type} redirectUri={redirectUri} metadataUrl={metadataUrl} hasId={!!idp} />
      <label className="mt-3 flex items-center gap-2 text-sm text-slate-600">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        Enabled (shows a sign-in button on the login page)
      </label>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Btn variant="primary" disabled={!name || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : idp ? "Save provider" : "Create provider"}
        </Btn>
        <Btn variant="default" disabled={test.isPending} onClick={() => { setTestResult(null); test.mutate(); }}>
          {test.isPending ? "Testing…" : "🔌 Test connection"}
        </Btn>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
      </div>

      {testResult && (
        <div className={`mt-3 rounded-lg border p-3 text-xs ${testResult.ok ? "border-emerald-200 bg-emerald-50" : "border-amber-300 bg-amber-50"}`}>
          <div className="mb-2 flex items-center justify-between">
            <span className={`font-semibold ${testResult.ok ? "text-emerald-700" : "text-amber-700"}`}>
              {testResult.ok ? "✓ Configuration looks valid" : "⚠ Configuration needs attention"} — {testResult.summary}
            </span>
            <button type="button" onClick={() => setTestResult(null)} className="text-slate-400 hover:text-slate-600">✕</button>
          </div>
          <ul className="space-y-1">
            {testResult.checks.map((c, i) => (
              <li key={i} className="flex items-start gap-2">
                <span className={c.ok ? "text-emerald-600" : c.critical ? "text-rose-600" : "text-amber-500"}>
                  {c.ok ? "✓" : c.critical ? "✗" : "⚠"}
                </span>
                <span className="min-w-0">
                  <span className="font-medium text-slate-700">{c.name}</span>
                  {c.detail && <span className="ml-1 break-all text-slate-500">— {c.detail}</span>}
                  {!c.ok && !c.critical && <span className="ml-1 text-slate-400">(optional)</span>}
                </span>
              </li>
            ))}
          </ul>
          <div className="mt-2 text-[11px] text-slate-400">
            Best-effort check — a full sign-in still requires a real user to complete the
            {type === "saml" ? " SAML" : " OAuth"} round-trip.
          </div>
        </div>
      )}
    </div>
  );
}

// ================================================================= Sessions
function SessionsCard() {
  const qc = useQueryClient();
  const [showExpired, setShowExpired] = useState(false);
  const sessions = useQuery({
    queryKey: ["ac-sessions", showExpired],
    queryFn: () => api.acSessions(showExpired),
  });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const rows = sessions.data?.sessions ?? [];
  const expiredCount = sessions.data?.expired_count ?? 0;
  return (
    <Card
      title="Active Sessions"
      actions={
        <div className="flex items-center gap-2">
          {expiredCount > 0 && (
            <Btn
              variant="ghost"
              onClick={async () => {
                setBusy(true);
                setErr(null);
                try {
                  await api.acRevokeExpiredSessions();
                  void qc.invalidateQueries({ queryKey: ["ac-sessions"] });
                } catch (e) {
                  setErr(errMsg(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? "Revoking…" : `Revoke ${expiredCount} expired`}
            </Btn>
          )}
          <Btn variant="ghost" onClick={() => void qc.invalidateQueries({ queryKey: ["ac-sessions"] })}>Refresh</Btn>
        </div>
      }
    >
      {err && <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}
      <label className="mb-3 flex items-center gap-2 text-xs text-slate-500">
        <input type="checkbox" checked={showExpired} onChange={(e) => setShowExpired(e.target.checked)} />
        Show expired sessions{expiredCount > 0 ? ` (${expiredCount})` : ""}
      </label>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs uppercase tracking-wide text-slate-500">
              <th className="py-2 pr-3">User</th>
              <th className="py-2 pr-3">Status</th>
              <th className="py-2 pr-3">IP</th>
              <th className="py-2 pr-3">Client</th>
              <th className="py-2 pr-3">Last seen</th>
              <th className="py-2" />
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr key={s.id} className={`border-b last:border-0 ${s.expired ? "opacity-60" : ""}`}>
                <td className="py-2 pr-3">
                  <div className="font-medium text-slate-800">{s.display_name || s.username}</div>
                  <div className="text-xs text-slate-500">{s.username}</div>
                </td>
                <td className="py-2 pr-3">
                  {s.expired ? (
                    <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-500">Expired</span>
                  ) : (
                    <span className="rounded-full bg-green-100 px-2 py-0.5 text-[11px] font-medium text-green-700">Active</span>
                  )}
                </td>
                <td className="py-2 pr-3 text-xs text-slate-500">{s.ip ?? "—"}</td>
                <td className="py-2 pr-3 max-w-xs truncate text-xs text-slate-500" title={s.user_agent ?? ""}>{s.user_agent ?? "—"}</td>
                <td className="py-2 pr-3 text-xs text-slate-500">{s.last_seen_at ? new Date(s.last_seen_at).toLocaleString() : "—"}</td>
                <td className="py-2 text-right">
                  <Btn
                    variant="danger"
                    onClick={async () => {
                      try { await api.acRevokeSession(s.id); void qc.invalidateQueries({ queryKey: ["ac-sessions"] }); }
                      catch (e) { setErr(errMsg(e)); }
                    }}
                  >
                    Revoke
                  </Btn>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p className="py-4 text-sm text-slate-500">
            {showExpired ? "No sessions." : "No active sessions."}
          </p>
        )}
      </div>
    </Card>
  );
}

// ================================================================= Policies
function PoliciesCard() {
  const qc = useQueryClient();
  const policies = useQuery({ queryKey: ["ac-policies"], queryFn: api.acPolicies });
  const roles = useQuery({ queryKey: ["ac-roles"], queryFn: api.acRoles });
  const [draft, setDraft] = useState<AuthPolicies | null>(null);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const values = draft ?? policies.data?.values ?? null;

  const set = <K extends keyof AuthPolicies>(k: K, v: AuthPolicies[K]) => {
    if (!values) return;
    setDraft({ ...values, [k]: v });
    setSaved(false);
  };

  const save = useMutation({
    mutationFn: () => api.acUpdatePolicies(values!),
    onSuccess: () => { setSaved(true); void qc.invalidateQueries({ queryKey: ["ac-policies"] }); },
    onError: (e) => setErr(errMsg(e)),
  });

  if (!values) return <Card title="Security Policy"><p className="text-sm text-slate-500">Loading…</p></Card>;

  const numField = (k: keyof AuthPolicies, label: string, hint?: string) => (
    <Field label={label}>
      <input
        type="number"
        className={inputCls}
        value={values[k] as number}
        onChange={(e) => set(k, Number(e.target.value) as never)}
      />
      {hint && <span className="mt-1 block text-xs text-slate-400">{hint}</span>}
    </Field>
  );
  const boolRow = (k: keyof AuthPolicies, label: string, hint?: string) => (
    <label className="flex items-start gap-2 rounded border bg-white px-3 py-2 text-sm">
      <input type="checkbox" checked={values[k] as boolean} onChange={(e) => set(k, e.target.checked as never)} className="mt-0.5" />
      <span>
        <span className="font-medium text-slate-700">{label}</span>
        {hint && <span className="block text-xs text-slate-400">{hint}</span>}
      </span>
    </label>
  );

  return (
    <Card
      title="Security Policy"
      actions={
        <Btn variant="primary" disabled={save.isPending || !draft} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save policy"}
        </Btn>
      }
    >
      {err && <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}
      {saved && <div className="mb-3 rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700">Policy saved.</div>}

      <h3 className="mb-2 text-sm font-semibold text-slate-700">Sign-in methods</h3>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {boolRow("local_login_enabled", "Local password sign-in", "Allow username/password login. Disable to enforce SSO only.")}
        {boolRow("allow_self_registration", "Self-registration", "Let users create their own local accounts (off by default).")}
      </div>

      <h3 className="mb-2 mt-5 text-sm font-semibold text-slate-700">Password policy</h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {numField("password_min_length", "Minimum length")}
        {boolRow("password_require_complexity", "Require complexity", "Upper + lower + digit. (No MFA for local accounts.)")}
      </div>

      <h3 className="mb-2 mt-5 text-sm font-semibold text-slate-700">Brute-force protection</h3>
      <p className="mb-2 text-xs text-slate-400">
        These react <em>after</em> failed sign-ins. To stop unknown addresses reaching the sign-in
        page at all, see{" "}
        <Link to="/admin/firewall" className="font-semibold text-brand-dark hover:underline">
          Network Access
        </Link>
        .
      </p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {numField("max_failed_attempts", "Max failed attempts (per account)", "After this many wrong passwords for the same user, the account is auto-locked.")}
        {numField("lockout_minutes", "Account lockout duration (minutes)", "Account auto-unlocks after this many minutes.")}
      </div>
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
        {boolRow(
          "ip_rate_limit_enabled",
          "Per-IP rate limit",
          "Also block a client IP that fails too many sign-ins across any usernames (auto-unlocks).",
        )}
        {numField(
          "ip_rate_limit_max_attempts",
          "Max failed attempts (per IP)",
          "Failures counted across the sliding window below.",
        )}
        {numField(
          "ip_rate_limit_window_seconds",
          "IP window (seconds)",
          "Sliding window in which failures are counted.",
        )}
        {numField(
          "ip_rate_limit_lockout_seconds",
          "IP lockout duration (seconds)",
          "How long a tripped IP stays blocked before auto-unlock.",
        )}
      </div>

      <h3 className="mb-2 mt-5 text-sm font-semibold text-slate-700">Sessions</h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {numField("session_idle_minutes", "Idle timeout (minutes)", "Sliding window of inactivity.")}
        {numField("session_absolute_minutes", "Absolute lifetime (minutes)", "Hard cap regardless of activity.")}
      </div>

      <h3 className="mb-2 mt-5 text-sm font-semibold text-slate-700">Single sign-on (JIT)</h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {boolRow("sso_auto_provision", "Auto-provision SSO users", "Create accounts on first successful SSO login.")}
        <Field label="Default role for new SSO users">
          <select className={inputCls} value={values.sso_default_role} onChange={(e) => set("sso_default_role", e.target.value as never)}>
            {(roles.data ?? []).map((r) => (
              <option key={r.id} value={r.name}>{roleLabel(r.name)}</option>
            ))}
          </select>
        </Field>
      </div>
    </Card>
  );
}

// ================================================================= Network Access (firewall)
//
// Restricts which source addresses may reach the application AT ALL. The Security Policy screen
// above handles the reactive control (per-IP lockout after failed sign-ins); this one stops the
// attempt from ever arriving.
//
// The screen is designed around one hazard: it is possible to lock yourself out of the very UI
// you would use to undo the mistake. Hence monitor mode, the self-IP guard, the typed
// confirmation, and the commit-confirm countdown.

const MODE_HELP: Record<FirewallMode, { label: string; hint: string }> = {
  off: { label: "Off", hint: "Anyone on the internet can reach this application." },
  monitor: {
    label: "Monitor",
    hint: "Record what would be blocked, but block nothing. Start here.",
  },
  enforce: { label: "Enforce", hint: "Only the listed sources can reach this application." },
};

function useCountdown(deadline: string | null): string | null {
  const [, force] = useState(0);
  useEffect(() => {
    if (!deadline) return;
    const t = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [deadline]);
  if (!deadline) return null;
  const ms = new Date(deadline).getTime() - Date.now();
  if (ms <= 0) return null;
  const total = Math.floor(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

const FIREWALL_IMPORT_MAX_BYTES = 1024 * 1024;
const FIREWALL_IMPORT_PAGE_SIZE = 50;
const FIREWALL_RULE_PAGE_SIZE = 100;

function FirewallExportMenu({ dirty, onError }: { dirty: boolean; onError: (message: string) => void }) {
  const [busy, setBusy] = useState<"txt" | "csv" | "">("");

  async function download(format: "txt" | "csv") {
    setBusy(format);
    onError("");
    try {
      const blob = await api.firewallExport(format);
      const stamp = new Date().toISOString().slice(0, 10).replaceAll("-", "");
      downloadBlob(
        blob,
        format === "txt"
          ? `firewall-active-ranges-${stamp}.txt`
          : `firewall-all-rules-${stamp}.csv`,
      );
    } catch (error) {
      onError(errMsg(error));
    } finally {
      setBusy("");
    }
  }

  return (
    <details className="relative">
      <summary className="cursor-pointer list-none rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
        {busy ? "Exporting…" : "Export ▾"}
      </summary>
      <div className="absolute right-0 z-20 mt-1 w-72 rounded-lg border bg-white p-2 shadow-lg">
        <button
          type="button"
          disabled={!!busy}
          onClick={() => void download("txt")}
          className="block w-full rounded px-2 py-2 text-left text-sm hover:bg-slate-50 disabled:opacity-50"
        >
          <span className="block font-medium text-slate-700">Active ranges — TXT</span>
          <span className="block text-xs text-slate-500">One active CIDR per line.</span>
        </button>
        <button
          type="button"
          disabled={!!busy}
          onClick={() => void download("csv")}
          className="block w-full rounded px-2 py-2 text-left text-sm hover:bg-slate-50 disabled:opacity-50"
        >
          <span className="block font-medium text-slate-700">All rules — CSV</span>
          <span className="block text-xs text-slate-500">Includes labels and disabled rules.</span>
        </button>
        {dirty && (
          <p className="mt-1 border-t px-2 pt-2 text-[11px] text-amber-700">
            Export uses the saved policy. Save draft changes first to include them.
          </p>
        )}
      </div>
    </details>
  );
}

function FirewallImportPanel({
  mode,
  rules,
  onApply,
  onClose,
}: {
  mode: FirewallMode;
  rules: FirewallRule[];
  onApply: (rules: FirewallRule[], context: FirewallImportContext) => void;
  onClose: () => void;
}) {
  const [sourceKind, setSourceKind] = useState<"paste" | "file">("paste");
  const [text, setText] = useState("");
  const [sourceName, setSourceName] = useState("pasted-ranges.txt");
  const [format, setFormat] = useState<FirewallImportFormat>("auto");
  const [defaultLabel, setDefaultLabel] = useState("Imported range");
  const [strategy, setStrategy] = useState<FirewallImportStrategy>("merge");
  const [preview, setPreview] = useState<FirewallImportPreview | null>(null);
  const [previewPage, setPreviewPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function chooseFile(file: File | undefined) {
    if (!file) return;
    setError("");
    setPreview(null);
    if (!/\.(txt|csv)$/i.test(file.name)) {
      setError("Choose a UTF-8 .txt or .csv file.");
      return;
    }
    if (file.size > FIREWALL_IMPORT_MAX_BYTES) {
      setError("The file is larger than 1 MiB.");
      return;
    }
    try {
      const decoded = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      setText(decoded);
      setSourceName(file.name);
      setFormat(file.name.toLowerCase().endsWith(".csv") ? "csv" : "txt");
    } catch {
      setError("The file is not valid UTF-8 text.");
    }
  }

  async function runPreview() {
    setBusy(true);
    setError("");
    setPreview(null);
    try {
      const result = await api.firewallImportPreview({
        text,
        source_name: sourceKind === "paste" ? "pasted-ranges.txt" : sourceName,
        format: sourceKind === "paste" ? "auto" : format,
        default_label: defaultLabel,
        strategy,
        mode,
        existing_rules: rules.map((rule) => ({
          cidr: rule.cidr,
          label: rule.label,
          enabled: rule.enabled,
        })),
      });
      setPreview(result);
      setPreviewPage(0);
    } catch (cause) {
      setError(errMsg(cause));
    } finally {
      setBusy(false);
    }
  }

  const diagnostics = preview?.diagnostics ?? [];
  const pageCount = Math.max(1, Math.ceil(diagnostics.length / FIREWALL_IMPORT_PAGE_SIZE));
  const safePage = Math.min(previewPage, pageCount - 1);
  const visibleDiagnostics = diagnostics.slice(
    safePage * FIREWALL_IMPORT_PAGE_SIZE,
    (safePage + 1) * FIREWALL_IMPORT_PAGE_SIZE,
  );

  const statusStyle: Record<string, string> = {
    invalid: "bg-rose-100 text-rose-800",
    existing: "bg-slate-100 text-slate-700",
    retained: "bg-sky-100 text-sky-800",
    add: "bg-emerald-100 text-emerald-800",
    valid: "bg-emerald-100 text-emerald-800",
  };

  return (
    <div data-testid="firewall-import-panel" className="mb-4 rounded-lg border border-brand-dark/30 bg-slate-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-slate-800">Import allowed sources</h3>
          <p className="mt-0.5 text-xs text-slate-500">
            Preview first. Applying changes only updates the draft; Save activates it.
          </p>
        </div>
        <Btn variant="ghost" onClick={onClose}>Close</Btn>
      </div>

      <div className="mt-3 inline-flex rounded-lg border bg-white p-0.5 text-xs">
        {(["paste", "file"] as const).map((kind) => (
          <button
            key={kind}
            type="button"
            onClick={() => {
              setSourceKind(kind);
              setText("");
              setSourceName(kind === "paste" ? "pasted-ranges.txt" : "");
              setFormat("auto");
              setPreview(null);
              setError("");
            }}
            className={`rounded-md px-3 py-1.5 ${sourceKind === kind ? "bg-brand-dark text-white" : "text-slate-600 hover:bg-slate-50"}`}
          >
            {kind === "paste" ? "Paste list" : "Upload file"}
          </button>
        ))}
      </div>

      {sourceKind === "paste" ? (
        <Field label="IP addresses or CIDR ranges">
          <textarea
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setPreview(null);
            }}
            rows={9}
            spellCheck={false}
            placeholder={"20.118.190.135/32\n156.20.174.0/24\n2001:db8:100::/48"}
            className={`${inputCls} mt-3 font-mono text-xs`}
          />
          <span className="mt-1 block text-xs text-slate-500">
            One address or CIDR per line. Blank lines and lines beginning with # are ignored.
          </span>
        </Field>
      ) : (
        <div className="mt-3 rounded-lg border-2 border-dashed bg-white p-4">
          <input
            type="file"
            accept=".txt,.csv,text/plain,text/csv"
            aria-label="Choose firewall import file"
            onChange={(event) => void chooseFile(event.target.files?.[0])}
            className="block w-full text-sm text-slate-600 file:mr-3 file:rounded-md file:border file:border-slate-300 file:bg-white file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-slate-700"
          />
          {!!sourceName && sourceName !== "pasted-ranges.txt" && (
            <div className="mt-3">
              <p className="text-xs font-medium text-slate-700">{sourceName}</p>
              <pre className="mt-1 max-h-32 overflow-auto rounded bg-slate-950 p-2 text-[10px] text-slate-100">
                {text.slice(0, 8_000)}{text.length > 8_000 ? "\n…" : ""}
              </pre>
            </div>
          )}
        </div>
      )}

      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
        <Field label="Default label">
          <input
            className={inputCls}
            maxLength={128}
            value={defaultLabel}
            onChange={(event) => {
              setDefaultLabel(event.target.value);
              setPreview(null);
            }}
            placeholder="Corporate egress"
          />
          <span className="mt-1 block text-xs text-slate-500">Used for TXT rows and blank CSV labels.</span>
        </Field>
        <fieldset>
          <legend className="mb-1 text-sm font-medium text-slate-700">Import strategy</legend>
          <label className="flex items-start gap-2 text-sm text-slate-700">
            <input type="radio" checked={strategy === "merge"} onChange={() => { setStrategy("merge"); setPreview(null); }} />
            <span><strong>Merge</strong> — keep existing rules and add new ranges.</span>
          </label>
          <label className="mt-1 flex items-start gap-2 text-sm text-rose-700">
            <input type="radio" checked={strategy === "replace"} onChange={() => { setStrategy("replace"); setPreview(null); }} />
            <span><strong>Replace</strong> — remove saved/draft ranges not in this list.</span>
          </label>
        </fieldset>
      </div>

      {error && <div className="mt-3 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">{error}</div>}

      <div className="mt-3 flex flex-wrap gap-2">
        <Btn variant="primary" disabled={busy || !text.trim()} onClick={() => void runPreview()}>
          {busy ? "Previewing…" : "Preview import"}
        </Btn>
        <Btn variant="ghost" onClick={() => {
          setText("");
          setSourceName(sourceKind === "paste" ? "pasted-ranges.txt" : "");
          setFormat("auto");
          setPreview(null);
          setError("");
        }}>
          Clear
        </Btn>
      </div>

      {preview && (
        <div className="mt-4 space-y-3 border-t pt-4">
          <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4 lg:grid-cols-8">
            {([
              ["Input", preview.summary.input_rows],
              ["Add", preview.summary.added],
              ["Retain", preview.summary.retained],
              ["Remove", preview.summary.removed],
              ["Skipped", preview.summary.skipped_existing],
              ["Invalid", preview.summary.invalid_rows],
              ["Result", preview.summary.result_total],
              ["Active", preview.summary.enabled_total],
            ] as const).map(([labelText, value]) => (
              <div key={labelText} className="rounded border bg-white px-2 py-1.5">
                <div className="text-slate-400">{labelText}</div>
                <div className="font-semibold tabular-nums text-slate-800">{value.toLocaleString()}</div>
              </div>
            ))}
          </div>

          {preview.errors.length > 0 && (
            <ul className="list-disc space-y-0.5 rounded border border-rose-200 bg-rose-50 p-3 pl-7 text-xs text-rose-800">
              {preview.errors.map((message) => <li key={message}>{message}</li>)}
            </ul>
          )}

          <div className={`rounded border px-3 py-2 text-xs ${preview.your_ip_covered ? "border-emerald-200 bg-emerald-50 text-emerald-800" : "border-amber-300 bg-amber-50 text-amber-900"}`}>
            {preview.your_ip_covered
              ? `The resulting list covers your current address (${preview.your_ip ?? "unknown"}).`
              : `The resulting list does not cover your current address (${preview.your_ip ?? "unknown"}). Enforce-mode Save remains blocked until you add it.`}
          </div>

          {preview.overlap_count > 0 && (
            <div className="rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
              <div className="font-medium">{preview.overlap_count.toLocaleString()} overlapping range pair(s) — retained as entered.</div>
              <ul className="mt-1 list-disc pl-5">
                {preview.overlaps.slice(0, 10).map((item) => <li key={`${item.cidr}-${item.overlaps}`}>{item.message}</li>)}
              </ul>
              {preview.overlap_count > 10 && <p className="mt-1">Showing the first 10 overlap warnings.</p>}
            </div>
          )}

          {visibleDiagnostics.length > 0 && (
            <div className="overflow-x-auto rounded border bg-white">
              <table className="w-full text-xs">
                <thead className="bg-slate-50 text-left text-slate-500">
                  <tr><th className="px-2 py-1.5">Line</th><th className="px-2 py-1.5">Input</th><th className="px-2 py-1.5">Normalized</th><th className="px-2 py-1.5">Status</th><th className="px-2 py-1.5">Detail</th></tr>
                </thead>
                <tbody>
                  {visibleDiagnostics.map((item) => (
                    <tr key={`${item.line}-${item.input}`} className="border-t align-top">
                      <td className="px-2 py-1.5 tabular-nums text-slate-500">{item.line}</td>
                      <td className="max-w-56 break-all px-2 py-1.5 font-mono text-slate-700">{item.input || "—"}</td>
                      <td className="px-2 py-1.5 font-mono text-slate-700">{item.cidr ?? "—"}</td>
                      <td className="px-2 py-1.5"><span className={`rounded px-1.5 py-0.5 ${statusStyle[item.status] ?? statusStyle.valid}`}>{item.status}</span></td>
                      <td className="max-w-80 px-2 py-1.5 text-slate-600">{item.message || item.label}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {pageCount > 1 && (
            <div className="flex items-center justify-between text-xs text-slate-500">
              <span>Rows {safePage * FIREWALL_IMPORT_PAGE_SIZE + 1}–{Math.min((safePage + 1) * FIREWALL_IMPORT_PAGE_SIZE, diagnostics.length)} of {diagnostics.length}</span>
              <div className="flex gap-1"><Btn variant="ghost" disabled={safePage === 0} onClick={() => setPreviewPage(safePage - 1)}>Previous</Btn><Btn variant="ghost" disabled={safePage >= pageCount - 1} onClick={() => setPreviewPage(safePage + 1)}>Next</Btn></div>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Btn
              variant="primary"
              disabled={!preview.can_apply}
              onClick={() => onApply(preview.result_rules, {
                source_name: preview.source_name,
                strategy: preview.strategy,
                skipped_existing: preview.summary.skipped_existing,
              })}
            >
              Apply to draft
            </Btn>
            {!preview.can_apply && <span className="text-xs text-rose-700">Resolve preview errors before applying.</span>}
          </div>
        </div>
      )}
    </div>
  );
}

function FirewallCard() {
  const qc = useQueryClient();
  const { has, isAdmin } = useAuth();
  // Read is enough to LOAD this screen (an auditor must be able to evidence the network
  // policy). Changing it needs firewall.manage. Render read-only rather than hiding the
  // screen, and disable rather than silently 403 on save.
  const canManage = isAdmin || has("firewall.manage");
  const cfg = useQuery({ queryKey: ["firewall"], queryFn: api.firewallConfig });
  const [draft, setDraft] = useState<{ mode: FirewallMode; rules: FirewallRule[] } | null>(null);
  const [err, setErr] = useState("");
  const [saved, setSaved] = useState(false);
  const [adding, setAdding] = useState(false);
  const [newCidr, setNewCidr] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [confirmText, setConfirmText] = useState("");
  const [importing, setImporting] = useState(false);
  const [importContext, setImportContext] = useState<FirewallImportContext | null>(null);
  const [importApplied, setImportApplied] = useState(false);
  const [ruleSearch, setRuleSearch] = useState("");
  const [rulePage, setRulePage] = useState(0);

  const server = cfg.data;
  const mode = draft?.mode ?? server?.mode ?? "off";
  const rules = draft?.rules ?? server?.rules ?? [];
  const dirty = draft !== null;
  const countdown = useCountdown(server?.confirm_by ?? null);

  const mutate = (next: Partial<{ mode: FirewallMode; rules: FirewallRule[] }>) => {
    setDraft({ mode, rules, ...next });
    setSaved(false);
  };

  // The server is the authority on whether the caller is covered (it sees the real address),
  // but while editing an unsaved draft only the client knows the pending rules — so the guard
  // is recomputed locally against the draft to keep the warning honest before saving.
  const coveredByDraft = useMemo(() => {
    if (!dirty) return server?.your_ip_covered ?? false;
    const ip = server?.your_ip;
    if (!ip) return false;
    return rules.some((r) => r.enabled && cidrCovers(r.cidr, ip));
  }, [dirty, rules, server]);

  const save = useMutation({
    mutationFn: () =>
      api.updateFirewall({
        mode,
        rules: rules.map((r) => ({ cidr: r.cidr, label: r.label, enabled: r.enabled })),
        ...(importContext ? { import_context: importContext } : {}),
      }),
    onSuccess: (fresh) => {
      setDraft(null);
      setImportContext(null);
      setImportApplied(false);
      setConfirmText("");
      setSaved(true);
      setTimeout(() => setSaved(false), 1800);
      // Seed the cache from the mutation's own response BEFORE invalidating. Without this the
      // card renders the previous server state for the duration of the refetch — which on this
      // screen means briefly announcing "Off / no ranges", i.e. telling the operator they are
      // unprotected at the exact moment they just protected themselves.
      qc.setQueryData(["firewall"], fresh);
      void qc.invalidateQueries({ queryKey: ["firewall"] });
      void qc.invalidateQueries({ queryKey: ["firewall-blocks"] });
    },
    onError: (e) => setErr(errMsg(e)),
  });

  const confirmEnforcement = useMutation({
    mutationFn: api.confirmFirewall,
    onSuccess: (fresh) => {
      qc.setQueryData(["firewall"], fresh);
      void qc.invalidateQueries({ queryKey: ["firewall"] });
    },
    onError: (e) => setErr(errMsg(e)),
  });

  const addRule = (cidr: string, label: string) => {
    const trimmed = cidr.trim();
    if (!trimmed || !label.trim()) return;
    mutate({
      rules: [
        ...rules,
        { cidr: trimmed, label: label.trim(), enabled: true, scope: describeCidr(trimmed), valid: true },
      ],
    });
    setNewCidr("");
    setNewLabel("");
    setAdding(false);
  };

  const filteredRules = useMemo(() => {
    const needle = ruleSearch.trim().toLowerCase();
    return rules
      .map((rule, index) => ({ rule, index }))
      .filter(({ rule }) =>
        !needle || [rule.cidr, rule.label, rule.scope, rule.enabled ? "active" : "disabled"]
          .some((value) => String(value ?? "").toLowerCase().includes(needle)),
      );
  }, [ruleSearch, rules]);

  if (cfg.isLoading) {
    return <Card title="Network access"><p className="text-sm text-slate-500">Loading…</p></Card>;
  }

  const enforceBlocked = mode === "enforce" && !coveredByDraft;
  const needsTypedConfirm = mode === "enforce" && server?.mode !== "enforce";
  const preview = newCidr.trim() ? describeCidr(newCidr) : "";
  const previewCoversMe =
    !!server?.your_ip && !!newCidr.trim() && cidrCovers(newCidr.trim(), server.your_ip);
  const rulePageCount = Math.max(1, Math.ceil(filteredRules.length / FIREWALL_RULE_PAGE_SIZE));
  const safeRulePage = Math.min(rulePage, rulePageCount - 1);
  const visibleRules = filteredRules.slice(
    safeRulePage * FIREWALL_RULE_PAGE_SIZE,
    (safeRulePage + 1) * FIREWALL_RULE_PAGE_SIZE,
  );

  return (
    <div className="space-y-6">
      <Card
        title="Network access"
        actions={
          canManage ? (
            <Btn
              variant="primary"
              disabled={!dirty || save.isPending || enforceBlocked || (needsTypedConfirm && confirmText !== "ENFORCE")}
              onClick={() => save.mutate()}
            >
              {save.isPending ? "Saving…" : "Save"}
            </Btn>
          ) : (
            <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-500">Read-only</span>
          )
        }
      >
        {err && <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}
        {saved && <div className="mb-3 rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700">Network access saved.</div>}
        {!canManage && (
          <div className="mb-3 rounded border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600">
            You can view the network access policy but not change it. Changing it requires the
            <strong> Change network access control</strong> permission.
          </div>
        )}

        {server?.break_glass_active && (
          <div className="mb-3 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
            <strong>Break-glass is active.</strong> <code>IP_ALLOWLIST_DISABLED</code> is set on the
            container, so no address is being blocked regardless of the settings below. Remove that
            environment variable to resume enforcement.
          </div>
        )}

        {countdown && (
          <div className="mb-3 flex items-center justify-between gap-3 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
            <span>
              <strong>Enforcing provisionally</strong> — reverts to Monitor in {countdown} unless you
              confirm you still have access.
            </span>
            <Btn variant="primary" disabled={confirmEnforcement.isPending || !canManage} onClick={() => confirmEnforcement.mutate()}>
              Keep enforcing
            </Btn>
          </div>
        )}

        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          {(Object.keys(MODE_HELP) as FirewallMode[]).map((m) => (
            <label
              key={m}
              className={`flex cursor-pointer items-start gap-2 rounded border px-3 py-2 text-sm ${
                mode === m ? "border-brand-dark bg-slate-50" : "bg-white"
              }`}
            >
              <input
                type="radio"
                className="mt-0.5"
                checked={mode === m}
                disabled={!canManage}
                onChange={() => mutate({ mode: m })}
              />
              <span>
                <span className="font-medium text-slate-700">{MODE_HELP[m].label}</span>
                <span className="block text-xs text-slate-400">{MODE_HELP[m].hint}</span>
              </span>
            </label>
          ))}
        </div>

        <div
          className={`mt-4 rounded border px-3 py-2 text-sm ${
            coveredByDraft
              ? "border-emerald-200 bg-emerald-50 text-emerald-700"
              : "border-amber-300 bg-amber-50 text-amber-800"
          }`}
        >
          {coveredByDraft ? (
            <>
              You are connecting from <code>{server?.your_ip}</code>
              {server?.your_ip_rule && !dirty ? <> — covered by “{server.your_ip_rule}”.</> : " — covered by a rule below."}
            </>
          ) : (
            <>
              You are connecting from <code>{server?.your_ip ?? "an unknown address"}</code>.{" "}
              <strong>No enabled rule covers this address.</strong> Enforcing now would lock you out.
            </>
          )}
        </div>

        {needsTypedConfirm && !enforceBlocked && (
          <div className="mt-4 rounded border border-red-300 bg-red-50 p-3 text-sm text-red-800">
            <p className="font-semibold">Restrict access to this application?</p>
            <p className="mt-1">
              Everyone outside the {rules.filter((r) => r.enabled).length} enabled range(s) will get a
              403 — <strong>including the sign-in page</strong>. Enforcement reverts to Monitor after{" "}
              {server?.confirm_window_minutes ?? 15} minutes unless you confirm it is working.
            </p>
            <p className="mt-1">
              If you lose access, run:{" "}
              <code className="break-all">
                az containerapp update -n &lt;app&gt; -g &lt;rg&gt; --set-env-vars IP_ALLOWLIST_DISABLED=true
              </code>
            </p>
            <label className="mt-2 block">
              <span className="mb-1 block font-medium">Type ENFORCE to confirm</span>
              <input
                className={inputCls}
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                placeholder="ENFORCE"
              />
            </label>
          </div>
        )}

        {enforceBlocked && (
          <p className="mt-3 text-xs text-amber-700">
            Add a range covering your address before enforcing.
          </p>
        )}

        <p className="mt-4 text-xs text-slate-400">
          This controls the application. Your Azure Container App may also restrict access at the
          ingress, which this screen cannot see or change.
        </p>

        <FirewallResolutionDetail resolution={server?.resolution ?? null} />
      </Card>

      <Card
        title="Allowed sources"
        actions={
          <div className="flex flex-wrap justify-end gap-2">
            {canManage && (
              <>
              {server?.your_ip && (
                <Btn onClick={() => addRule(`${server.your_ip}/32`, "My current address")}>
                  + Add my IP
                </Btn>
              )}
              <Btn onClick={() => { setImporting((value) => !value); setAdding(false); }}>
                ⇩ Import list
              </Btn>
              <Btn variant="primary" onClick={() => { setAdding((value) => !value); setImporting(false); }}>+ Add range</Btn>
              </>
            )}
            <FirewallExportMenu dirty={dirty} onError={setErr} />
          </div>
        }
      >
        {importApplied && (
          <div className="mb-4 rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
            Imported into the draft — review the resulting list and press <strong>Save</strong> to activate it.
          </div>
        )}

        {importing && (
          <FirewallImportPanel
            mode={mode}
            rules={rules}
            onClose={() => setImporting(false)}
            onApply={(importedRules, context) => {
              mutate({ rules: importedRules });
              setImportContext(context);
              setImportApplied(true);
              setImporting(false);
              setRulePage(0);
              setRuleSearch("");
            }}
          />
        )}

        {adding && (
          <div className="mb-4 rounded-lg border border-brand-dark/30 bg-slate-50 p-4">
            <h3 className="mb-3 text-sm font-semibold">New allowed source</h3>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label="IP address or CIDR range">
                <input
                  className={inputCls}
                  value={newCidr}
                  placeholder="203.0.113.0/24"
                  onChange={(e) => setNewCidr(e.target.value)}
                />
                {preview && (
                  <span className="mt-1 block text-xs text-slate-500">
                    {preview}
                    {previewCoversMe && (
                      <span className="text-emerald-600"> · includes your current address</span>
                    )}
                  </span>
                )}
              </Field>
              <Field label="Label">
                <input
                  className={inputCls}
                  value={newLabel}
                  placeholder="Office VPN"
                  onChange={(e) => setNewLabel(e.target.value)}
                />
              </Field>
            </div>
            <div className="mt-4 flex gap-2">
              <Btn
                variant="primary"
                disabled={!newCidr.trim() || !newLabel.trim() || preview === "Not a valid range"}
                onClick={() => addRule(newCidr, newLabel)}
              >
                Add
              </Btn>
              <Btn variant="ghost" onClick={() => setAdding(false)}>Cancel</Btn>
            </div>
          </div>
        )}

        {rules.length === 0 ? (
          <p className="py-4 text-sm text-slate-500">
            No ranges yet. In Enforce mode with no ranges, nobody can reach this application — add
            your own address first.
          </p>
        ) : (
          <div className="space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <input
                value={ruleSearch}
                onChange={(event) => { setRuleSearch(event.target.value); setRulePage(0); }}
                aria-label="Search allowed sources"
                placeholder="Search range, label, scope, or status…"
                className="w-full max-w-md rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-dark focus:outline-none"
              />
              <span className="text-xs text-slate-500">
                {filteredRules.length.toLocaleString()} of {rules.length.toLocaleString()} rule(s)
              </span>
            </div>

            {visibleRules.length === 0 ? (
              <p className="rounded border bg-slate-50 px-3 py-4 text-sm text-slate-500">No rules match this search.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="text-left text-slate-500">
                    <tr className="border-b">
                      <th className="py-1.5 pr-3 font-medium">Range</th>
                      <th className="py-1.5 pr-3 font-medium">Label</th>
                      <th className="py-1.5 pr-3 font-medium">Scope</th>
                      <th className="py-1.5 pr-3 font-medium">Status</th>
                      <th className="py-1.5 font-medium" />
                    </tr>
                  </thead>
                  <tbody>
                    {visibleRules.map(({ rule: r, index: i }) => (
                      <tr key={`${r.cidr}-${i}`} className="border-b last:border-0 hover:bg-gray-50">
                        <td className="py-1.5 pr-3 font-mono text-slate-700">{r.cidr}</td>
                        <td className="py-1.5 pr-3 text-slate-600">{r.label}</td>
                        <td className="py-1.5 pr-3 text-slate-500">{r.scope}</td>
                        <td className="py-1.5 pr-3">
                          <span
                            className={`rounded px-1.5 py-0.5 text-xs ${
                              r.enabled ? "bg-emerald-100 text-emerald-700" : "bg-slate-200 text-slate-600"
                            }`}
                          >
                            {r.enabled ? "Active" : "Disabled"}
                          </span>
                        </td>
                        <td className="py-1.5 text-right">
                          {canManage && (
                            <div className="flex justify-end gap-1">
                              <Btn
                                variant="ghost"
                                onClick={() =>
                                  mutate({
                                    rules: rules.map((item, index) => (index === i ? { ...item, enabled: !item.enabled } : item)),
                                  })
                                }
                              >
                                {r.enabled ? "Disable" : "Enable"}
                              </Btn>
                              <Btn
                                variant="danger"
                                onClick={() => {
                                  if (!confirm(`Remove ${r.cidr} (${r.label})?`)) return;
                                  mutate({ rules: rules.filter((_, index) => index !== i) });
                                }}
                              >
                                Delete
                              </Btn>
                            </div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {rulePageCount > 1 && (
              <div className="flex items-center justify-between text-xs text-slate-500">
                <span>
                  Rules {safeRulePage * FIREWALL_RULE_PAGE_SIZE + 1}–{Math.min((safeRulePage + 1) * FIREWALL_RULE_PAGE_SIZE, filteredRules.length)}
                </span>
                <div className="flex gap-1">
                  <Btn variant="ghost" disabled={safeRulePage === 0} onClick={() => setRulePage(safeRulePage - 1)}>Previous</Btn>
                  <Btn variant="ghost" disabled={safeRulePage >= rulePageCount - 1} onClick={() => setRulePage(safeRulePage + 1)}>Next</Btn>
                </div>
              </div>
            )}
          </div>
        )}
        {dirty && (
          <p className="mt-3 text-xs text-amber-700">Unsaved changes — press Save above to apply.</p>
        )}
      </Card>

      <FirewallBlocksCard
        effectiveMode={server?.effective_mode ?? "off"}
        canManage={canManage}
        onAllowRange={(ip) => {
          // Closes the loop from observation to action: the operator reads an address off the
          // blocks table and allows it without retyping a CIDR (the retyping is where the /24
          // vs /32 slips happen).
          setAdding(true);
          setNewCidr(`${ip}/32`);
          setNewLabel(`Allowed from blocks list`);
        }}
      />
    </div>
  );
}

/** Shows how the server arrived at your address.
 *
 * "Which address does the server think I am, and how did it decide?" is the first question
 * anyone debugging an allowlist asks. Without this, a mis-attributed address is only
 * discoverable by noticing the number looks wrong — which is exactly how a CGNAT/tailnet
 * address went unexplained until someone screenshotted it. */
function FirewallResolutionDetail({ resolution }: { resolution: FirewallResolution | null }) {
  const [open, setOpen] = useState(false);
  if (!resolution) return null;
  return (
    <div className="mt-3">
      <button
        type="button"
        className="text-xs text-slate-500 underline hover:text-slate-700"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "Hide" : "How was my address determined?"}
      </button>
      {open && (
        <div className="mt-2 rounded border bg-slate-50 p-3 text-xs text-slate-600">
          <p className="mb-2">{resolution.reason}</p>
          <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1">
            <dt className="text-slate-500">Resolved as</dt>
            <dd className="font-mono">{resolution.resolved_ip ?? "— (caller not identifiable)"}</dd>
            <dt className="text-slate-500">Connection peer</dt>
            <dd className="font-mono">{resolution.socket_peer ?? "—"}</dd>
            <dt className="text-slate-500">X-Forwarded-For</dt>
            <dd className="break-all font-mono">
              {resolution.forwarded_header ?? "(not sent)"}
              {resolution.forwarded_header && !resolution.forwarded_honoured && (
                <span className="ml-1 text-amber-700">(not trusted here, ignored)</span>
              )}
            </dd>
          </dl>
          {resolution.entries.length > 0 && (
            <table className="mt-2 w-full">
              <thead className="text-left text-slate-500">
                <tr className="border-b">
                  <th className="py-1 pr-3 font-medium">Entry</th>
                  <th className="py-1 pr-3 font-medium">Classified as</th>
                  <th className="py-1 font-medium">Used</th>
                </tr>
              </thead>
              <tbody>
                {resolution.entries.map((e, i) => (
                  <tr key={`${e.value}-${i}`} className="border-b last:border-0">
                    <td className="py-1 pr-3 font-mono">{e.value}</td>
                    <td className="py-1 pr-3">{e.classification}</td>
                    <td className="py-1">{e.selected ? "✓" : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="mt-2 text-slate-400">
            The header is read right-to-left because a caller can only prepend to it. Addresses in
            carrier-grade NAT space (100.64.0.0/10), such as a Tailscale address, are treated as a
            real client, not as infrastructure. “Connection peer” is what the server reports and
            may itself be derived from the header when the app runs behind a managed ingress.
          </p>
        </div>
      )}
    </div>
  );
}

/** Parse a CIDR/IP the same way the backend does, for live feedback before saving. */
function parseCidr(value: string): { base: bigint; bits: number; version: 4 | 6 } | null {
  const [addr, prefixRaw] = value.trim().split("/");
  const v6 = addr.includes(":");
  const max = v6 ? 128 : 32;
  const prefix = prefixRaw === undefined ? max : Number(prefixRaw);
  if (!Number.isInteger(prefix) || prefix < 0 || prefix > max) return null;
  let base: bigint;
  if (v6) {
    // Expand "::" and pad each hextet.
    const [head, tail] = addr.split("::");
    const h = head ? head.split(":") : [];
    const t = tail ? tail.split(":") : [];
    if (addr.split("::").length > 2) return null;
    const missing = 8 - h.length - t.length;
    if (missing < 0 || (missing > 0 && !addr.includes("::"))) return null;
    const parts = [...h, ...Array(Math.max(0, missing)).fill("0"), ...t];
    if (parts.length !== 8) return null;
    base = 0n;
    for (const p of parts) {
      const n = parseInt(p || "0", 16);
      if (Number.isNaN(n) || n < 0 || n > 0xffff) return null;
      base = (base << 16n) | BigInt(n);
    }
  } else {
    const octets = addr.split(".");
    if (octets.length !== 4) return null;
    base = 0n;
    for (const o of octets) {
      const n = Number(o);
      if (!Number.isInteger(n) || n < 0 || n > 255 || o === "") return null;
      base = (base << 8n) | BigInt(n);
    }
  }
  const mask = ((1n << BigInt(prefix)) - 1n) << BigInt(max - prefix);
  return { base: base & mask, bits: prefix, version: v6 ? 6 : 4 };
}

function describeCidr(value: string): string {
  const parsed = parseCidr(value);
  if (!parsed) return "Not a valid range";
  if (parsed.version === 6) {
    return parsed.bits === 128 ? "Single IPv6 address" : `IPv6 /${parsed.bits}`;
  }
  if (parsed.bits === 32) return "Single IP address";
  const count = 2 ** (32 - parsed.bits);
  return `${count.toLocaleString()} addresses`;
}

function cidrCovers(cidr: string, ip: string): boolean {
  const net = parseCidr(cidr);
  const addr = parseCidr(ip);
  if (!net || !addr || net.version !== addr.version) return false;
  const max = net.version === 6 ? 128 : 32;
  const mask = ((1n << BigInt(net.bits)) - 1n) << BigInt(max - net.bits);
  return (addr.base & mask) === net.base;
}

function FirewallBlocksCard({
  effectiveMode,
  canManage,
  onAllowRange,
}: {
  effectiveMode: FirewallMode;
  canManage: boolean;
  onAllowRange: (ip: string) => void;
}) {
  const qc = useQueryClient();
  const [page, setPage] = useState(0);
  const pageSize = 25;
  const q = useQuery({
    queryKey: ["firewall-blocks", page],
    queryFn: () => api.firewallBlocks(pageSize, page * pageSize),
    refetchInterval: 15000,
  });
  const items = q.data?.items ?? [];
  const total = q.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  return (
    <Card
      title="Recent blocks"
      actions={
        <div className="flex items-center gap-2">
          {effectiveMode === "monitor" && (
            <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700">
              MONITOR — WOULD HAVE BEEN BLOCKED
            </span>
          )}
          {effectiveMode === "enforce" && (
            <span className="rounded bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700">
              BLOCKED
            </span>
          )}
          {total > 0 && canManage && (
            <Btn
              variant="ghost"
              onClick={() => {
                if (!confirm("Clear the recorded block history?")) return;
                void api.clearFirewallBlocks().then(() => {
                  void qc.invalidateQueries({ queryKey: ["firewall-blocks"] });
                });
              }}
            >
              Clear
            </Btn>
          )}
        </div>
      }
    >
      {items.length === 0 ? (
        <p className="py-4 text-sm text-slate-500">
          {effectiveMode === "off"
            ? "Nothing recorded — network access is Off, so no requests are being evaluated."
            : "Nothing recorded yet."}
        </p>
      ) : (
        <>
          {effectiveMode === "enforce" && items.some((b) => b.mode === "monitor") && (
            // Rows keep the mode they were recorded under, so a "Would block" row can legitimately
            // appear while enforcing. Saying so is cheaper than letting someone conclude the
            // policy is not being applied.
            <p className="mb-2 text-xs text-slate-500">
              Rows keep the mode they were recorded under. “Would block” entries are historical,
              from a period when this was in Monitor — they are not requests being allowed now.
            </p>
          )}
          <table className="w-full text-xs">
            <thead className="text-left text-gray-500">
              <tr className="border-b">
                <th className="py-1.5 pr-3 font-medium">Last seen</th>
                <th className="py-1.5 pr-3 font-medium">Source IP</th>
                <th className="py-1.5 pr-3 font-medium">Hits</th>
                <th className="py-1.5 pr-3 font-medium">Last path</th>
                <th className="py-1.5 pr-3 font-medium">Result</th>
                <th className="py-1.5 font-medium" />
              </tr>
            </thead>
            <tbody>
              {items.map((b) => (
                <tr key={b.id} className="border-b last:border-0 hover:bg-gray-50">
                  <td className="whitespace-nowrap py-1.5 pr-3 text-gray-400">
                    {b.last_seen ? new Date(b.last_seen).toLocaleString() : "—"}
                  </td>
                  <td className="py-1.5 pr-3 font-mono text-gray-700">{b.ip}</td>
                  <td className="py-1.5 pr-3 text-gray-600">{b.hits.toLocaleString()}</td>
                  <td className="max-w-[240px] truncate py-1.5 pr-3 text-gray-600" title={b.last_path}>
                    {b.last_path || "—"}
                  </td>
                  <td className="py-1.5 pr-3">
                    <span
                      className={`rounded px-1.5 py-0.5 ${
                        b.mode === "enforce"
                          ? "bg-red-100 text-red-700"
                          : "bg-amber-100 text-amber-700"
                      }`}
                    >
                      {b.mode === "enforce" ? "Blocked" : "Would block"}
                    </span>
                  </td>
                  <td className="py-1.5 text-right">
                    {canManage && (
                      <Btn variant="ghost" onClick={() => onAllowRange(b.ip)}>
                        + Allow
                      </Btn>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-3 flex items-center justify-between text-xs text-gray-500">
            <span>
              {page * pageSize + 1}–{Math.min((page + 1) * pageSize, total)} of {total.toLocaleString()}
            </span>
            <div className="flex items-center gap-1">
              <Btn variant="ghost" disabled={page === 0} onClick={() => setPage(0)}>« First</Btn>
              <Btn variant="ghost" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>‹ Prev</Btn>
              <span>Page {page + 1} / {pageCount}</span>
              <Btn variant="ghost" disabled={page >= pageCount - 1} onClick={() => setPage((p) => p + 1)}>Next ›</Btn>
              <Btn variant="ghost" disabled={page >= pageCount - 1} onClick={() => setPage(pageCount - 1)}>Last »</Btn>
            </div>
          </div>
        </>
      )}
    </Card>
  );
}

// ================================================================= Panel
export function SecurityPanel({ section }: { section: SecuritySection }) {
  const body = useMemo(() => {
    switch (section) {
      case "users": return <UsersCard />;
      case "roles": return <RolesCard />;
      case "groups": return <GroupsCard />;
      case "identity": return <IdentityProvidersCard />;
      case "sessions": return <SessionsCard />;
      case "policies": return <PoliciesCard />;
      case "firewall": return <FirewallCard />;
      default: return <UsersCard />;
    }
  }, [section]);
  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="mx-auto max-w-6xl 2xl:max-w-screen-2xl space-y-6 p-6">{body}</div>
    </div>
  );
}

/** Access Control: a sub-tabbed page grouping Users, Roles, Groups, and Sign-in & SSO.
 *  Each tab is a real route (/admin/users, /admin/roles, …) so sub-tabs are deep-linkable
 *  and the main Settings menu keeps "Access Control" highlighted. */
export function AccessControlPanel({ section }: { section: string }) {
  const active: SecuritySection = ACCESS_SUB_IDS.has(section)
    ? (section as SecuritySection)
    : "users";
  return (
    <div className="flex h-full flex-col bg-gray-50">
      <div className="shrink-0 border-b border-gray-200 bg-white px-6 pt-5">
        <h2 className="text-lg font-semibold text-gray-800">Access Control</h2>
        <p className="mt-0.5 text-sm text-gray-500">
          Manage users, roles, groups, and single sign-on for this workspace.
        </p>
        <div className="mt-3 flex flex-wrap gap-1">
          {ACCESS_NAV.map((t) => {
            const on = t.id === active;
            return (
              <Link
                key={t.id}
                to={`/admin/${t.id}`}
                className={`flex items-center gap-1.5 rounded-t-lg border-b-2 px-3.5 py-2 text-sm transition ${
                  on
                    ? "border-brand font-medium text-brand"
                    : "border-transparent text-gray-500 hover:bg-gray-50 hover:text-gray-800"
                }`}
              >
                <span className="text-base">{t.icon}</span>
                {t.label}
              </Link>
            );
          })}
        </div>
      </div>
      <div className="min-h-0 flex-1">
        <SecurityPanel section={active} />
      </div>
    </div>
  );
}
