const PORTAL_HOSTS = new Set(["portal.azure.com", "portal.azure.us", "portal.azure.cn"]);
const RESOURCE_ID = /^\/subscriptions\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:\/.*)?$/i;

export function azurePortalResourceUrl(resourceId: unknown, portalHost = "portal.azure.com"): string {
  const value = String(resourceId ?? "").trim().replace(/\/+$/, "");
  const host = String(portalHost || "").toLowerCase();
  if (
    !PORTAL_HOSTS.has(host)
    || !RESOURCE_ID.test(value)
    || value.includes("://")
    || /[?#\r\n\t\\]/.test(value)
  ) return "";
  return `https://${host}/#@/resource${encodeURI(value)}/overview`;
}

export function azurePortalSubscriptionUrl(subscriptionId: unknown, portalHost = "portal.azure.com"): string {
  return azurePortalResourceUrl(`/subscriptions/${String(subscriptionId ?? "").trim()}`, portalHost);
}