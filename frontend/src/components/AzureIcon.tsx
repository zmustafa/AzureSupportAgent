import type { ReactNode } from "react";
import type { WorkloadNodeKind } from "../api";

/**
 * Azure-style resource & scope icons. These are original, simplified glyphs drawn in the
 * Azure visual language (cube/box silhouettes, the standard service hues) — not copied
 * Microsoft assets. A generic resource cube is the fallback for unmapped types.
 */

function S({ children, vb = "0 0 18 18" }: { children: ReactNode; vb?: string }) {
  return (
    <svg viewBox={vb} className="h-full w-full" xmlns="http://www.w3.org/2000/svg">
      {children}
    </svg>
  );
}

// ---- Scope containers ------------------------------------------------------
const ManagementGroup = (
  <S>
    <rect x="6.5" y="1.5" width="5" height="4" rx="0.5" fill="#5E9624" />
    <rect x="1.5" y="9.5" width="5" height="4" rx="0.5" fill="#76BC2D" />
    <rect x="11.5" y="9.5" width="5" height="4" rx="0.5" fill="#76BC2D" />
    <path d="M9 5.5v2M9 7.5H4v2M9 7.5h5v2" stroke="#A0A0A0" strokeWidth="0.8" fill="none" />
  </S>
);

// Azure Subscriptions — the official service icon is a golden/yellow key (Microsoft
// gold), not the blue magnifying-glass we had before. Flat fills (no gradient) so many
// instances can render without duplicate-id clashes.
const Subscription = (
  <S>
    {/* bow (ring head) — hole stays transparent via stroke */}
    <circle cx="8.7" cy="5.2" r="2.55" fill="none" stroke="#FFB900" strokeWidth="2.1" />
    {/* subtle lighter highlight on the bow */}
    <path d="M7.2 3.5a2.55 2.55 0 0 1 2.5-.4" fill="none" stroke="#FFD23F" strokeWidth="1" strokeLinecap="round" />
    {/* shaft */}
    <rect x="7.75" y="7.3" width="1.9" height="8.4" rx="0.95" fill="#FFB900" />
    {/* teeth */}
    <rect x="9.65" y="10.9" width="2.9" height="1.5" rx="0.4" fill="#F5A300" />
    <rect x="9.65" y="13.2" width="2.1" height="1.45" rx="0.4" fill="#F5A300" />
  </S>
);

const ResourceGroup = (
  <S>
    <rect x="1.6" y="1.6" width="14.8" height="14.8" rx="1.3" fill="none" stroke="#0078D4" strokeWidth="1" strokeDasharray="2 1.4" />
    <path d="M9 4.2l3.6 2.1v4.2L9 12.6 5.4 10.5V6.3z" fill="#0078D4" />
    <path d="M9 4.2l3.6 2.1L9 8.4 5.4 6.3z" fill="#50E6FF" />
    <path d="M9 8.4v4.2L5.4 10.5V6.3z" fill="#198AB3" />
  </S>
);

// ---- Generic resource (fallback) ------------------------------------------
const GenericResource = (
  <S>
    <path d="M9 1.8l6.2 3.6v7.2L9 16.2 2.8 12.6V5.4z" fill="#0078D4" />
    <path d="M9 1.8l6.2 3.6L9 9 2.8 5.4z" fill="#50E6FF" />
    <path d="M9 9v7.2L2.8 12.6V5.4z" fill="#198AB3" />
  </S>
);

// ---- Common resource-type icons -------------------------------------------
const VM = (
  <S>
    <rect x="2" y="3.5" width="14" height="9" rx="1" fill="#0078D4" />
    <rect x="3.4" y="5" width="11.2" height="6" rx="0.5" fill="#50E6FF" />
    <rect x="6.5" y="13" width="5" height="1.6" fill="#0078D4" />
    <rect x="5" y="14.6" width="8" height="1" rx="0.5" fill="#0078D4" />
  </S>
);

const Storage = (
  <S>
    {/* Azure Storage = teal box with horizontal "table" lines */}
    <rect x="2" y="3.4" width="14" height="11.2" rx="1.2" fill="#37C2B1" />
    <rect x="2" y="3.4" width="14" height="3" rx="1.2" fill="#50E6C8" />
    <rect x="3.6" y="8" width="10.8" height="1.4" rx="0.4" fill="#fff" opacity="0.95" />
    <rect x="3.6" y="10.6" width="10.8" height="1.4" rx="0.4" fill="#fff" opacity="0.75" />
  </S>
);

const Sql = (
  <S>
    {/* Azure SQL = blue cylinder/database */}
    <path d="M3.4 4.6c0-1.25 2.5-2 5.6-2s5.6.75 5.6 2v8.8c0 1.25-2.5 2-5.6 2s-5.6-.75-5.6-2z" fill="#0078D4" />
    <path d="M3.4 4.6c0 1.25 2.5 2 5.6 2s5.6-.75 5.6-2" fill="none" stroke="#fff" strokeWidth="0.7" opacity="0.55" />
    <ellipse cx="9" cy="4.6" rx="5.6" ry="2" fill="#50E6FF" />
    <ellipse cx="9" cy="4.6" rx="3.1" ry="1" fill="#9CEBFF" />
  </S>
);

const AppService = (
  <S>
    <circle cx="9" cy="9" r="7" fill="#0078D4" />
    <path d="M9 2.2A6.8 6.8 0 0 0 9 15.8M2.4 7h13.2M2.4 11h13.2" stroke="#50E6FF" strokeWidth="0.9" fill="none" />
    <ellipse cx="9" cy="9" rx="3" ry="6.8" fill="none" stroke="#50E6FF" strokeWidth="0.9" />
  </S>
);

const KeyVault = (
  <S>
    <path d="M9 1.5l6 2.3v4.7c0 4-2.6 6.5-6 7.9-3.4-1.4-6-3.9-6-7.9V3.8z" fill="#0078D4" />
    <path d="M9 1.5l6 2.3v4.7c0 4-2.6 6.5-6 7.9z" fill="#005BA1" />
    <circle cx="9" cy="7.4" r="2" fill="#FFD400" />
    <rect x="8.3" y="8.4" width="1.4" height="3.2" fill="#FFD400" />
  </S>
);

const Network = (
  <S>
    <circle cx="9" cy="3.5" r="2" fill="#0078D4" />
    <circle cx="3.5" cy="13" r="2" fill="#0078D4" />
    <circle cx="14.5" cy="13" r="2" fill="#0078D4" />
    <path d="M9 5.5L4 11.5M9 5.5l5 6M5 13h8" stroke="#50E6FF" strokeWidth="1" fill="none" />
  </S>
);

const Nsg = (
  <S>
    <path d="M9 1.5l6 2.3v4.7c0 4-2.6 6.5-6 7.9-3.4-1.4-6-3.9-6-7.9V3.8z" fill="#0078D4" />
    <path d="M6.2 9l2 2 3.6-3.8" stroke="#fff" strokeWidth="1.3" fill="none" strokeLinecap="round" strokeLinejoin="round" />
  </S>
);

const Cognitive = (
  <S>
    <path d="M11 2.5a4 4 0 0 1 3.4 6 3.3 3.3 0 0 1-1.6 5.8A3.8 3.8 0 0 1 9 15.5a3.8 3.8 0 0 1-3.8-1.2 3.3 3.3 0 0 1-1.6-5.8A4 4 0 0 1 7 2.5a3.4 3.4 0 0 1 4 0z" fill="#0078D4" />
    <path d="M9 4.5v9M6.5 6.5h5M6.5 11h5" stroke="#50E6FF" strokeWidth="0.9" fill="none" />
  </S>
);

const ServiceBus = (
  <S>
    <rect x="2" y="6" width="14" height="6" rx="1" fill="#0078D4" />
    <path d="M2 9h14" stroke="#50E6FF" strokeWidth="1" />
    <circle cx="5" cy="9" r="1" fill="#fff" />
    <circle cx="9" cy="9" r="1" fill="#fff" />
    <circle cx="13" cy="9" r="1" fill="#fff" />
  </S>
);

const Dns = (
  <S>
    <circle cx="9" cy="9" r="6.5" fill="#0078D4" />
    <path d="M9 2.5v13M2.5 9h13" stroke="#50E6FF" strokeWidth="0.8" />
    <ellipse cx="9" cy="9" rx="2.8" ry="6.5" fill="none" stroke="#50E6FF" strokeWidth="0.8" />
  </S>
);

const Dashboard = (
  <S>
    <rect x="2" y="2.5" width="6.5" height="6.5" rx="0.6" fill="#0078D4" />
    <rect x="9.5" y="2.5" width="6.5" height="4" rx="0.6" fill="#50E6FF" />
    <rect x="9.5" y="7.5" width="6.5" height="8" rx="0.6" fill="#198AB3" />
    <rect x="2" y="10" width="6.5" height="5.5" rx="0.6" fill="#50E6FF" />
  </S>
);

// ARM type (lowercase) -> icon. Substring fallbacks below catch families.
const TYPE_ICONS: Record<string, ReactNode> = {
  "microsoft.compute/virtualmachines": VM,
  "microsoft.compute/virtualmachinescalesets": VM,
  "microsoft.compute/disks": Storage,
  "microsoft.storage/storageaccounts": Storage,
  "microsoft.sql/servers": Sql,
  "microsoft.sql/servers/databases": Sql,
  "microsoft.dbforpostgresql/servers": Sql,
  "microsoft.dbforpostgresql/flexibleservers": Sql,
  "microsoft.dbformysql/servers": Sql,
  "microsoft.dbformysql/flexibleservers": Sql,
  "microsoft.documentdb/databaseaccounts": Sql,
  "microsoft.web/sites": AppService,
  "microsoft.web/serverfarms": AppService,
  "microsoft.web/staticsites": AppService,
  "microsoft.keyvault/vaults": KeyVault,
  "microsoft.network/networksecuritygroups": Nsg,
  "microsoft.network/virtualnetworks": Network,
  "microsoft.network/applicationgateways": Network,
  "microsoft.network/loadbalancers": Network,
  "microsoft.network/publicipaddresses": Network,
  "microsoft.network/networkinterfaces": Network,
  "microsoft.network/privateendpoints": Network,
  "microsoft.network/azurefirewalls": Nsg,
  "microsoft.network/dnszones": Dns,
  "microsoft.network/privatednszones": Dns,
  "microsoft.cognitiveservices/accounts": Cognitive,
  "microsoft.machinelearningservices/workspaces": Cognitive,
  "microsoft.servicebus/namespaces": ServiceBus,
  "microsoft.eventhub/namespaces": ServiceBus,
  "microsoft.eventgrid/topics": ServiceBus,
  "microsoft.portal/dashboards": Dashboard,
  "microsoft.insights/components": Dashboard,
};

function resourceIcon(armType?: string | null): ReactNode {
  const t = (armType || "").toLowerCase();
  if (!t) return GenericResource;
  if (TYPE_ICONS[t]) return TYPE_ICONS[t];
  // Family fallbacks.
  if (t.includes("microsoft.storage")) return Storage;
  if (t.includes("microsoft.sql") || t.includes("dbfor") || t.includes("documentdb")) return Sql;
  if (t.includes("microsoft.compute")) return VM;
  if (t.includes("microsoft.web")) return AppService;
  if (t.includes("microsoft.keyvault")) return KeyVault;
  if (t.includes("networksecuritygroup")) return Nsg;
  if (t.includes("microsoft.network")) return Network;
  if (t.includes("cognitive") || t.includes("machinelearning") || t.includes("openai")) return Cognitive;
  if (t.includes("servicebus") || t.includes("eventhub") || t.includes("eventgrid")) return ServiceBus;
  if (t.includes("dnszone")) return Dns;
  if (t.includes("dashboard") || t.includes("insights")) return Dashboard;
  return GenericResource;
}

/** Azure resource/scope icon. For resources, pass the ARM ``type``. */
export function AzureIcon({
  kind,
  type,
  className = "h-4 w-4",
}: {
  kind: WorkloadNodeKind;
  type?: string | null;
  className?: string;
}) {
  let glyph: ReactNode;
  if (kind === "mg") glyph = ManagementGroup;
  else if (kind === "subscription") glyph = Subscription;
  else if (kind === "resource_group") glyph = ResourceGroup;
  else glyph = resourceIcon(type);
  return <span className={`inline-block shrink-0 ${className}`}>{glyph}</span>;
}

// Full ARM type (lowercase) -> friendly display name. Mirrors the backend friendly map.
const FRIENDLY_TYPE: Record<string, string> = {
  "microsoft.compute/virtualmachines": "Virtual Machines",
  "microsoft.compute/virtualmachinescalesets": "VM Scale Sets",
  "microsoft.compute/disks": "Managed Disks",
  "microsoft.compute/availabilitysets": "Availability Sets",
  "microsoft.compute/images": "VM Images",
  "microsoft.compute/snapshots": "Disk Snapshots",
  "microsoft.compute/sshpublickeys": "SSH Keys",
  "microsoft.storage/storageaccounts": "Storage Accounts",
  "microsoft.sql/servers": "SQL Servers",
  "microsoft.sql/servers/databases": "SQL Databases",
  "microsoft.sql/managedinstances": "SQL Managed Instances",
  "microsoft.dbforpostgresql/servers": "PostgreSQL Servers",
  "microsoft.dbforpostgresql/flexibleservers": "PostgreSQL Servers",
  "microsoft.dbformysql/servers": "MySQL Servers",
  "microsoft.dbformysql/flexibleservers": "MySQL Servers",
  "microsoft.documentdb/databaseaccounts": "Cosmos DB Accounts",
  "microsoft.network/applicationgateways": "Application Gateways",
  "microsoft.network/loadbalancers": "Load Balancers",
  "microsoft.network/virtualnetworks": "Virtual Networks",
  "microsoft.network/networksecuritygroups": "Network Security Groups",
  "microsoft.network/applicationsecuritygroups": "Application Security Groups",
  "microsoft.network/publicipaddresses": "Public IP Addresses",
  "microsoft.network/publicipprefixes": "Public IP Prefixes",
  "microsoft.network/networkinterfaces": "Network Interfaces",
  "microsoft.network/privateendpoints": "Private Endpoints",
  "microsoft.network/privatelinkservices": "Private Link Services",
  "microsoft.network/localnetworkgateways": "Local Network Gateways",
  "microsoft.network/virtualnetworkgateways": "VPN Gateways",
  "microsoft.network/connections": "VPN Connections",
  "microsoft.network/azurefirewalls": "Azure Firewalls",
  "microsoft.network/bastionhosts": "Bastion Hosts",
  "microsoft.network/natgateways": "NAT Gateways",
  "microsoft.network/dnszones": "DNS Zones",
  "microsoft.network/privatednszones": "Private DNS Zones",
  "microsoft.network/frontdoors": "Front Doors",
  "microsoft.network/trafficmanagerprofiles": "Traffic Manager Profiles",
  "microsoft.network/networkwatchers": "Network Watchers",
  "microsoft.network/routetables": "Route Tables",
  "microsoft.cdn/profiles": "CDN / Front Door",
  "microsoft.keyvault/vaults": "Key Vaults",
  "microsoft.web/sites": "App Services",
  "microsoft.web/serverfarms": "App Service Plans",
  "microsoft.web/staticsites": "Static Web Apps",
  "microsoft.web/connections": "API Connections",
  "microsoft.containerservice/managedclusters": "AKS Clusters",
  "microsoft.containerregistry/registries": "Container Registries",
  "microsoft.app/containerapps": "Container Apps",
  "microsoft.app/managedenvironments": "Container Apps Environments",
  "microsoft.cache/redis": "Redis Caches",
  "microsoft.servicebus/namespaces": "Service Bus Namespaces",
  "microsoft.eventhub/namespaces": "Event Hubs Namespaces",
  "microsoft.eventgrid/topics": "Event Grid Topics",
  "microsoft.eventgrid/systemtopics": "Event Grid System Topics",
  "microsoft.insights/components": "Application Insights",
  "microsoft.insights/actiongroups": "Action Groups",
  "microsoft.insights/metricalerts": "Metric Alerts",
  "microsoft.insights/webtests": "Availability Tests",
  "microsoft.insights/workbooks": "Azure Workbooks",
  "microsoft.insights/scheduledqueryrules": "Log Alert Rules",
  "microsoft.insights/datacollectionrules": "Data Collection Rules",
  "microsoft.alertsmanagement/smartdetectoralertrules": "Smart Detector Alert Rules",
  "microsoft.operationalinsights/workspaces": "Log Analytics Workspaces",
  "microsoft.operationsmanagement/solutions": "Monitoring Solutions",
  "microsoft.recoveryservices/vaults": "Recovery Services Vaults",
  "microsoft.apimanagement/service": "API Management",
  "microsoft.logic/workflows": "Logic Apps",
  "microsoft.datafactory/factories": "Data Factories",
  "microsoft.cognitiveservices/accounts": "Cognitive Services",
  "microsoft.machinelearningservices/workspaces": "ML Workspaces",
  "microsoft.search/searchservices": "Cognitive Search",
  "microsoft.signalrservice/signalr": "SignalR",
  "microsoft.managedidentity/userassignedidentities": "Managed Identities",
  "microsoft.portal/dashboards": "Shared Dashboards",
  "microsoft.automation/automationaccounts": "Automation Accounts",
  "microsoft.compute/restorepointcollections": "Restore Point Collections",
  "microsoft.resources/templatespecs": "Template Specs",
};

/** Map a full ARM resource type to a friendly display name. */
export function friendlyResourceType(armType?: string | null): string {
  const t = (armType || "").toLowerCase().trim();
  if (!t) return "Other";
  if (FRIENDLY_TYPE[t]) return FRIENDLY_TYPE[t];
  // Fallback: take the last path segment, split camelCase + separators, title-case.
  const seg = t.split("/").pop() ?? t;
  const spaced = seg
    .replace(/[-_]/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/\s+/g, " ")
    .trim();
  const label = spaced.replace(/\b\w/g, (c) => c.toUpperCase());
  return label || "Other";
}

// Azure region code (lowercase, no spaces) -> friendly display name.
const FRIENDLY_LOCATION: Record<string, string> = {
  eastus: "East US",
  eastus2: "East US 2",
  eastus3: "East US 3",
  eastus2euap: "East US 2 EUAP",
  centraluseuap: "Central US EUAP",
  southcentralus: "South Central US",
  northcentralus: "North Central US",
  westcentralus: "West Central US",
  centralus: "Central US",
  westus: "West US",
  westus2: "West US 2",
  westus3: "West US 3",
  canadacentral: "Canada Central",
  canadaeast: "Canada East",
  brazilsouth: "Brazil South",
  brazilsoutheast: "Brazil Southeast",
  mexicocentral: "Mexico Central",
  northeurope: "North Europe",
  westeurope: "West Europe",
  uksouth: "UK South",
  ukwest: "UK West",
  francecentral: "France Central",
  francesouth: "France South",
  germanywestcentral: "Germany West Central",
  germanynorth: "Germany North",
  switzerlandnorth: "Switzerland North",
  switzerlandwest: "Switzerland West",
  norwayeast: "Norway East",
  norwaywest: "Norway West",
  swedencentral: "Sweden Central",
  swedensouth: "Sweden South",
  polandcentral: "Poland Central",
  italynorth: "Italy North",
  spaincentral: "Spain Central",
  austriaeast: "Austria East",
  eastasia: "East Asia",
  southeastasia: "Southeast Asia",
  japaneast: "Japan East",
  japanwest: "Japan West",
  australiaeast: "Australia East",
  australiasoutheast: "Australia Southeast",
  australiacentral: "Australia Central",
  australiacentral2: "Australia Central 2",
  centralindia: "Central India",
  southindia: "South India",
  westindia: "West India",
  jioindiawest: "Jio India West",
  jioindiacentral: "Jio India Central",
  koreacentral: "Korea Central",
  koreasouth: "Korea South",
  uaenorth: "UAE North",
  uaecentral: "UAE Central",
  qatarcentral: "Qatar Central",
  israelcentral: "Israel Central",
  southafricanorth: "South Africa North",
  southafricawest: "South Africa West",
  indonesiacentral: "Indonesia Central",
  malaysiawest: "Malaysia West",
  newzealandnorth: "New Zealand North",
  global: "Global",
};

/** Map an Azure region code to a friendly display name. */
export function friendlyLocation(loc?: string | null): string {
  const t = (loc || "").toLowerCase().replace(/\s+/g, "").trim();
  if (!t) return "—";
  if (FRIENDLY_LOCATION[t]) return FRIENDLY_LOCATION[t];
  // Fallback: split trailing digits + title-case (e.g. "foobarregion3").
  const m = t.match(/^([a-z]+?)(\d+)?$/);
  const base = (m?.[1] ?? t).replace(/([a-z])([A-Z])/g, "$1 $2");
  const words = base.replace(/(us|uk|uae)$/i, " $1").trim();
  const label = words.replace(/\b\w/g, (c) => c.toUpperCase());
  return (label + (m?.[2] ? ` ${m[2]}` : "")).trim() || (loc as string);
}
