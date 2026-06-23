// Display labels for the built-in system roles. The stored role `name` (admin, operator,
// auditor, user, noaccess) is the canonical key used everywhere for access gating and must
// not change — this only prettifies how those keys are shown to humans. Custom roles fall
// through unchanged.
const SYSTEM_ROLE_LABELS: Record<string, string> = {
  admin: "SysAdmin",
  auditor: "Auditor",
  noaccess: "NoAccess",
  operator: "Operator",
  user: "User",
};

/** Human-friendly label for a role name. Unknown (custom) roles are returned as-is. */
export function roleLabel(name: string | null | undefined): string {
  if (!name) return "";
  return SYSTEM_ROLE_LABELS[name.toLowerCase()] ?? name;
}
