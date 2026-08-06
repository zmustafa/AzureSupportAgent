/** Display helpers for the Disabled Access tab.
 *
 * Kept out of the component so the shortening rules can be reasoned about (and unit-reasoned)
 * on their own. Every function here is DISPLAY ONLY — the exports always carry the full value,
 * because a spreadsheet is where somebody goes precisely when the screen abbreviated something
 * they needed.
 */
import type { IamLeaverGrant, IamLeaverIdentity, IamLeaverResource } from "../../api";

/** Collapse a Key Vault access-policy role name to something a table cell can hold.
 *
 * The backend builds these as `Access Policy: keys(get,list,update,create,import,delete,...)
 * secrets(...) certificates(...)` — which is the correct value to STORE, because it is the
 * actual grant, and an impossible value to render: on a real tenant it is several hundred
 * characters and it broke the row layout.
 *
 * Counts, not verbs: "which permissions exactly" is a question for the expanded panel and the
 * export, and neither of those has a width limit.
 */
export function shortRole(role: string): string {
  if (!role.startsWith("Access Policy")) return role;
  const families = [...role.matchAll(/(\w+)\(([^)]*)\)/g)].map(([, name, verbs]) => {
    const n = verbs.split(",").map((v) => v.trim()).filter(Boolean).length;
    return `${name} ${n}`;
  });
  return families.length ? `Access Policy (${families.join(", ")})` : "Access Policy";
}

/** A human label for one scope, without the ARM path. */
export function resourceLabel(r: IamLeaverResource): string {
  if (r.resourceName) return r.resourceName;
  if (r.scopeType === "resourceGroup") return r.resourceGroup || r.scopeDisplayName || r.scope;
  if (r.scopeType === "subscription") return r.subscriptionName || r.scopeDisplayName || r.scope;
  if (r.scopeType === "managementGroup") return r.managementGroupName || r.scopeDisplayName || r.scope;
  if (r.scopeType === "tenantRoot") return r.scopeDisplayName || "Tenant root";
  if (r.scopeType === "directory") return r.scopeDisplayName || "Directory";
  return r.scopeDisplayName || r.scope || "—";
}

/** `Microsoft.Storage/storageAccounts` -> `storageAccounts`. The provider prefix is the same on
 *  every row in a group and eats the width the resource name needs. */
export function shortResourceType(t: string): string {
  if (!t) return "";
  const leaf = t.split("/").slice(1).join("/");
  return leaf || t;
}

/** The heading a scope is filed under in the Where panel. */
export function resourceGroupKey(r: IamLeaverResource): string {
  if (r.resourceType) return shortResourceType(r.resourceType);
  switch (r.scopeType) {
    case "subscription":
      return "Subscription";
    case "resourceGroup":
      return "Resource group";
    case "managementGroup":
      return "Management group";
    case "tenantRoot":
      return "Tenant root";
    case "directory":
      return "Directory";
    default:
      return "Other";
  }
}

/** ISO timestamp -> `2024-03-05`, or an em dash. Never a fabricated "never". */
export function dayOf(iso: string): string {
  return iso ? iso.slice(0, 10) : "—";
}

/** A readable label for the scope a grant lands on.
 *
 * `scopeDisplayName` is empty for resource-group scopes on a real tenant, so falling straight
 * through to `scope` printed a 120-character ARM path in a table cell — which is unreadable and
 * is also the same string on every row of a section, so it carries no information where it is
 * shown. The full id stays in the cell's `title` and in the export. */
export function grantScopeLabel(g: IamLeaverGrant): string {
  if (g.scopeType === "resource") return g.resourceName || g.scopeDisplayName || g.scope;
  if (g.scopeType === "resourceGroup") return g.resourceGroup || g.scopeDisplayName || g.scope;
  if (g.scopeType === "subscription") return g.subscriptionName || g.scopeDisplayName || g.scope;
  if (g.scopeType === "managementGroup") return g.scopeDisplayName || g.scope;
  if (g.scopeType === "tenantRoot") return g.scopeDisplayName || "Tenant root";
  if (g.scopeType === "directory") return g.scopeDisplayName || "Directory";
  return g.scopeDisplayName || g.resourceName || g.scope || "—";
}

/** The subscription a grant belongs to, for the second line of the scope cell. */
export function grantScopeContext(g: IamLeaverGrant): string {
  const bits = [g.subscriptionName, g.scopeType === "resource" ? g.resourceGroup : ""].filter(Boolean);
  return bits.join(" · ");
}

/** A short, honest age string. `null` days means it was not measured. */
export function ageLabel(days: number | null): string {
  if (days === null || days === undefined) return "";
  if (days < 1) return "today";
  if (days < 30) return `${days}d`;
  if (days < 365) return `${Math.floor(days / 30)}mo`;
  return `${(days / 365).toFixed(1)}y`;
}

/** Markdown for a change record / ticket. Everything an approver needs, nothing they have to
 *  come back to the tool for. */
export function identityAsMarkdown(
  i: IamLeaverIdentity,
  tierLabel: string,
  dormancyLabel: string,
): string {
  const lines: string[] = [
    `### Remove access — ${i.displayName || i.principalId}`,
    "",
    `- **Object id:** \`${i.principalId}\``,
    `- **UPN:** ${i.userPrincipalName || "—"}`,
    `- **Type:** ${i.principalType}${i.userType ? ` (${i.userType})` : ""}`,
    `- **Account state:** disabled${i.softDeleted ? ", in the Entra recycle bin (restorable)" : ""}`,
    `- **Exposure:** ${tierLabel}`,
    `- **Directory:** ${
      i.onPremSynced === "true"
        ? "synced from on-premises AD — account state must be changed in AD"
        : i.onPremSynced === "false"
          ? "cloud-only"
          : "sync state unknown"
    }`,
    `- **Last sign-in:** ${i.lastSignIn || "none recorded"}${dormancyLabel ? ` (${dormancyLabel})` : ""}`,
    `- **Last used (Activity Log):** ${
      i.activityMeasured ? i.lastActivity || "no recorded operations" : "not measured"
    }`,
    `- **Grants:** ${i.grants} (${i.privilegedGrants} privileged), oldest ${dayOf(i.oldestGrantAt)}`,
    "",
  ];
  if (i.groupsGrantingAccess.length) {
    lines.push(
      `> Held through ${i.groupGrants} group grant(s): ${i.groupsGrantingAccess.join(", ")}.`,
      "> Remove the member from the group — do NOT delete the group's role assignment.",
      "",
    );
  }
  if (i.ownedServicePrincipals.length) {
    lines.push(
      `> Owns ${i.ownedServicePrincipals.join(", ")}. Reassign ownership and roll the credential;`,
      "> removing the owner alone leaves the existing secret valid.",
      "",
    );
  }
  lines.push("| Role | Scope | Held via | Granted |", "| --- | --- | --- | --- |");
  for (const g of i.grantDetail.slice(0, 50)) {
    const via = g.accessPath === "GroupTransitive" ? g.sourceGroupName || "group" : "direct";
    lines.push(
      `| ${shortRole(g.roleName)}${g.roleIsPrivileged ? " ⚠" : ""} | ${g.scope} | ${via} | ${dayOf(
        g.assignmentCreatedOn,
      )} |`,
    );
  }
  if (i.grantDetail.length > 50) lines.push(`| … ${i.grantDetail.length - 50} more | | | |`);
  return lines.join("\n");
}
