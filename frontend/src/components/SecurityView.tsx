import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  api,
  HttpError,
  type AcGroup,
  type AcIdp,
  type AcRole,
  type AcUser,
  type AuthPolicies,
} from "../api";
import { apiBase } from "../api";
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
                      <span key={n} className="rounded bg-slate-100 px-1.5 py-0.5 text-xs">{n}</span>
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
              {r.name}
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
                  <span className="font-medium text-slate-800">{r.name}</span>
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
  permissions: { key: string; label: string }[];
  onClose: () => void;
  onSaved: () => void;
  onError: (m: string) => void;
}) {
  const [name, setName] = useState(role?.name ?? "");
  const [description, setDescription] = useState(role?.description ?? "");
  const [selected, setSelected] = useState<string[]>(role?.permissions ?? []);
  const toggle = (k: string) => setSelected((s) => (s.includes(k) ? s.filter((x) => x !== k) : [...s, k]));
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
        <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
          {permissions.map((p) => (
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
              {r.name}
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
const OIDC_FIELDS: { key: string; label: string; secret?: boolean; placeholder?: string }[] = [
  { key: "issuer", label: "Issuer URL", placeholder: "https://login.microsoftonline.com/<tenant>/v2.0" },
  { key: "discovery_url", label: "Discovery URL (optional)", placeholder: "Defaults to <issuer>/.well-known/openid-configuration" },
  { key: "client_id", label: "Client ID" },
  { key: "client_secret", label: "Client secret", secret: true },
  { key: "scopes", label: "Scopes", placeholder: "openid email profile" },
  { key: "group_claim", label: "Group claim", placeholder: "groups" },
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
        OIDC/SAML 2.0 provider. Local password sign-in is configured under Security Policy.
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
    Object.entries(initialCfg).forEach(([k, v]) => {
      if (typeof v === "string") out[k] = v;
    });
    return out;
  });
  const fields = type === "saml" ? SAML_FIELDS : OIDC_FIELDS;
  const setF = (k: string, v: string) => setCfg((c) => ({ ...c, [k]: v }));
  const redirectUri = `${apiBase}/auth/${type === "saml" ? "saml" : "oidc"}/${idp?.id ?? "<id>"}/${type === "saml" ? "acs" : "callback"}`;
  const metadataUrl = `${apiBase}/auth/saml/${idp?.id ?? "<id>"}/metadata`;

  const save = useMutation({
    mutationFn: () =>
      idp
        ? api.acUpdateIdp(idp.id, { name, type, enabled, button_label: buttonLabel, config: cfg })
        : api.acCreateIdp({ name, type, enabled, button_label: buttonLabel, config: cfg }),
    onSuccess: onSaved,
    onError: (e) => onError(errMsg(e)),
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
                <textarea className={`${inputCls} h-24 font-mono text-xs`} value={cfg[f.key] ?? ""} onChange={(e) => setF(f.key, e.target.value)} placeholder={f.placeholder} />
              ) : (
                <input
                  type={isSecret ? "password" : "text"}
                  // Block autofill from clobbering a saved secret (blank = keep on save).
                  {...(isSecret ? { name: `idp-${f.key}`, autoComplete: "off", "data-1p-ignore": true, "data-lpignore": "true", "data-form-type": "other" } : {})}
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
      <label className="mt-3 flex items-center gap-2 text-sm text-slate-600">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        Enabled (shows a sign-in button on the login page)
      </label>
      <div className="mt-4 flex gap-2">
        <Btn variant="primary" disabled={!name || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : idp ? "Save provider" : "Create provider"}
        </Btn>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
      </div>
    </div>
  );
}

// ================================================================= Sessions
function SessionsCard() {
  const qc = useQueryClient();
  const sessions = useQuery({ queryKey: ["ac-sessions"], queryFn: api.acSessions });
  const [err, setErr] = useState<string | null>(null);
  return (
    <Card
      title="Active Sessions"
      actions={<Btn variant="ghost" onClick={() => void qc.invalidateQueries({ queryKey: ["ac-sessions"] })}>Refresh</Btn>}
    >
      {err && <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{err}</div>}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs uppercase tracking-wide text-slate-500">
              <th className="py-2 pr-3">User</th>
              <th className="py-2 pr-3">IP</th>
              <th className="py-2 pr-3">Client</th>
              <th className="py-2 pr-3">Last seen</th>
              <th className="py-2" />
            </tr>
          </thead>
          <tbody>
            {sessions.data?.map((s) => (
              <tr key={s.id} className="border-b last:border-0">
                <td className="py-2 pr-3">
                  <div className="font-medium text-slate-800">{s.display_name || s.username}</div>
                  <div className="text-xs text-slate-500">{s.username}</div>
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
        {sessions.data?.length === 0 && <p className="py-4 text-sm text-slate-500">No active sessions.</p>}
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
              <option key={r.id} value={r.name}>{r.name}</option>
            ))}
          </select>
        </Field>
      </div>
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
      default: return <UsersCard />;
    }
  }, [section]);
  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <div className="mx-auto max-w-4xl space-y-6 p-8">{body}</div>
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
      <div className="shrink-0 border-b border-gray-200 bg-white px-8 pt-5">
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
