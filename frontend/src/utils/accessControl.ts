import type { Me } from "../api";

/** A route or navigation item may require one capability, any capability from a set,
 *  or merely a non-empty effective permission set (the Dashboard shell). */
export type AccessRequirement =
  | { permission: string }
  | { anyOf: readonly string[] }
  | { anyPermission: true };

const LEGACY_PERMISSION_ALIASES: Record<string, string> = {
  "rbac.read": "iam.read",
};

function permissionSet(user: Me | null | undefined): Set<string> {
  return new Set((user?.permissions ?? []).map((permission) => LEGACY_PERMISSION_ALIASES[permission] ?? permission));
}

/** Mirrors backend `Principal.is_admin`: users.manage is deliberately an administrator
 *  capability, including when it comes from a custom role. */
export function isEffectiveAdmin(user: Me | null | undefined): boolean {
  if (!user) return false;
  return user.role === "admin" || permissionSet(user).has("users.manage");
}

export function hasEffectivePermission(user: Me | null | undefined, permission: string): boolean {
  if (!user) return false;
  if (isEffectiveAdmin(user)) return true;
  return permissionSet(user).has(LEGACY_PERMISSION_ALIASES[permission] ?? permission);
}

export function hasAnyEffectivePermission(
  user: Me | null | undefined,
  permissions: readonly string[],
): boolean {
  return permissions.some((permission) => hasEffectivePermission(user, permission));
}

export function hasEffectiveAccess(user: Me | null | undefined): boolean {
  return !!user && (isEffectiveAdmin(user) || (user.permissions ?? []).length > 0);
}

export function canAccess(
  user: Me | null | undefined,
  requirement: AccessRequirement | null | undefined,
): boolean {
  if (!user) return false;
  if (!requirement) return true;
  if ("anyPermission" in requirement) return hasEffectiveAccess(user);
  if ("permission" in requirement) return hasEffectivePermission(user, requirement.permission);
  return hasAnyEffectivePermission(user, requirement.anyOf);
}

/** Filter grouped navigation while preserving a heading when the item that originally
 *  introduced the group is hidden by RBAC. */
export function filterPermissionedGroups<
  T extends { group?: string; permission: string },
>(items: readonly T[], user: Me | null | undefined): T[] {
  let currentGroup: string | undefined;
  let emittedGroup: string | undefined;
  const visible: T[] = [];

  for (const item of items) {
    if (item.group) currentGroup = item.group;
    if (!hasEffectivePermission(user, item.permission)) continue;
    const group = currentGroup && currentGroup !== emittedGroup ? currentGroup : undefined;
    visible.push({ ...item, group });
    if (currentGroup) emittedGroup = currentGroup;
  }
  return visible;
}
