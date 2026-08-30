targetScope = 'resourceGroup'

@description('Azure region for all resources. Defaults to westus3, which has been validated for PostgreSQL Flexible Server B1ms and Azure Container Apps.')
param location string = 'westus3'

@description('Base name for the Azure Support Agent deployment. Use lowercase letters, numbers, and hyphens.')
@minLength(3)
@maxLength(40)
param appName string = 'azure-support-agent'

@description('Immutable container image reference. The default is a published linux/amd64 manifest digest; override only with another reviewed tag or, preferably, registry/repository@sha256:digest reference.')
param containerImage string = 'docker.io/zmustafa/azure-support-agent@sha256:bc1ff8f1e29425ccc9c0804aa4fe20f27377dd919f04b3ead8b73c7b8dc9e421'

@description('Optional Azure Container Registry login server, for example contoso.azurecr.io. Leave empty for public images. When set, containerRegistryIdentityResourceId must name an existing user-assigned identity with AcrPull on that registry.')
param containerRegistryServer string = ''

@description('Optional existing user-assigned identity resource ID used for private ACR pulls and Key Vault references. Grant AcrPull and/or Key Vault Secrets User before deployment. The app also always receives its own system-assigned runtime identity.')
param containerRegistryIdentityResourceId string = ''

@description('Shared tags merged onto all taggable resources. Supplied values override the built-in Application, Environment, and ManagedBy tags.')
param tags object = {}

@description('Value for the built-in Environment resource tag.')
param deploymentEnvironment string = 'Production'

@description('Bootstrap local admin username for first login.')
@minLength(3)
param adminUsername string = 'admin'

@description('Bootstrap local admin password for first login. User is forced to change it after first sign-in.')
@secure()
@minLength(12)
param adminPassword string

@description('PostgreSQL administrator username.')
@minLength(3)
@maxLength(63)
param postgresAdminLogin string = 'azsupadmin'

// SECURITY: this must NOT have a default. It previously defaulted to a uniqueString()
// expression, which is a documented deterministic hash — not a random value. Its inputs
// (subscription id, resource group id, app name) are all discoverable from resource ids,
// portal URLs, ARM exports, and Resource Graph, so the password could be recomputed offline.
@description('PostgreSQL administrator password. Supply a strong, random value — this is a real credential and there is deliberately no default.')
@secure()
@minLength(16)
param postgresAdminPassword string

@description('PostgreSQL database name used by the app.')
@minLength(1)
@maxLength(63)
param postgresDatabaseName string = 'azsup'

@description('PostgreSQL compute tier. Burstable keeps one-click cost low; use GeneralPurpose or MemoryOptimized before enabling high availability.')
@allowed([
  'Burstable'
  'GeneralPurpose'
  'MemoryOptimized'
])
param postgresSkuTier string = 'Burstable'

@description('PostgreSQL Flexible Server SKU name. It must belong to postgresSkuTier and be offered in the selected region. B1ms is the balanced low-cost default; the production preset uses a General Purpose SKU.')
param postgresSkuName string = 'Standard_B1ms'

@description('PostgreSQL Premium SSD storage size in GiB. Storage can grow but cannot be reduced after deployment.')
@minValue(32)
@maxValue(32767)
param postgresStorageSizeGB int = 32

@description('Enable PostgreSQL storage autogrow. This prevents disk-full outages but has no configurable cost ceiling, so the one-click default is Disabled and the production preset opts in.')
@allowed([
  'Disabled'
  'Enabled'
])
param postgresStorageAutoGrow string = 'Disabled'

@description('PostgreSQL point-in-time backup retention in days (7-35). Fourteen days balances recoverability and retained-backup cost.')
@minValue(7)
@maxValue(35)
param postgresBackupRetentionDays int = 14

@description('Enable geo-redundant PostgreSQL backups. This is a create-time, region-dependent, higher-cost choice and is disabled by default.')
@allowed([
  'Disabled'
  'Enabled'
])
param postgresGeoRedundantBackup string = 'Disabled'

@description('PostgreSQL high-availability mode. SameZone and ZoneRedundant require a supported General Purpose or Memory Optimized SKU and approximately double compute cost.')
@allowed([
  'Disabled'
  'SameZone'
  'ZoneRedundant'
])
param postgresHighAvailabilityMode string = 'Disabled'

@description('Optional primary PostgreSQL availability zone. Leave empty for Azure placement; availability varies by region.')
param postgresAvailabilityZone string = ''

@description('Optional PostgreSQL standby availability zone used only with high availability. Leave empty for Azure placement.')
param postgresStandbyAvailabilityZone string = ''

@description('Use a custom PostgreSQL maintenance window. Disabled uses the Azure-managed schedule; Enabled uses the day/hour/minute parameters below.')
@allowed([
  'Disabled'
  'Enabled'
])
param postgresCustomMaintenanceWindow string = 'Disabled'

@description('Custom maintenance day, where 0 is Sunday and 6 is Saturday. Used only when postgresCustomMaintenanceWindow is Enabled.')
@minValue(0)
@maxValue(6)
param postgresMaintenanceDayOfWeek int = 0

@description('Custom maintenance start hour in the server region local time. Used only when postgresCustomMaintenanceWindow is Enabled.')
@minValue(0)
@maxValue(23)
param postgresMaintenanceStartHour int = 2

@description('Custom maintenance start minute. PostgreSQL Flexible Server currently supports the top of the hour.')
@allowed([0])
param postgresMaintenanceStartMinute int = 0

@description('Container CPU cores. Keep this paired with a supported memory value.')
@allowed([
  '0.5'
  '1.0'
  '2.0'
  '4.0'
])
param containerCpu string = '1.0'

@description('Container memory allocation. Chromium and Azure CLI need headroom, so 2Gi is the default.')
@allowed([
  '1Gi'
  '2Gi'
  '4Gi'
  '8Gi'
])
param containerMemory string = '2Gi'

@description('Minimum always-running Container App replicas. One controls idle cost; the production preset uses two for replica-level resilience.')
@minValue(1)
@maxValue(10)
param appMinReplicas int = 1

@description('Maximum Container App replicas. The balanced default allows one additional replica while limiting database connections and cost.')
@minValue(1)
@maxValue(10)
param appMaxReplicas int = 2

@description('Concurrent HTTP requests per replica before the Container Apps HTTP scaler adds capacity.')
@minValue(1)
@maxValue(1000)
param appHttpConcurrency int = 20

@description('Make the Container Apps environment zone redundant. Disabled preserves regional compatibility and cost; enable only in a supported availability-zone region.')
param containerEnvironmentZoneRedundant bool = false

@description('Storage account redundancy for Azure Files. Standard_LRS is the low-cost default; production commonly chooses Standard_ZRS after confirming regional support.')
@allowed([
  'Standard_LRS'
  'Standard_GRS'
  'Standard_RAGRS'
  'Standard_ZRS'
  'Standard_GZRS'
  'Standard_RAGZRS'
])
param storageRedundancy string = 'Standard_LRS'

@description('Azure Files share quota in GiB.')
@minValue(1)
@maxValue(102400)
param fileShareQuotaGB int = 32

@description('Deleted Azure Files shares are recoverable for this many days (1-365).')
@minValue(1)
@maxValue(365)
param fileShareSoftDeleteRetentionDays int = 14

@description('Optional direct Fernet key for stored application secrets. Prefer secretsEncryptionKeySecretUri; leave both empty to retain the existing generated key on Azure Files.')
@secure()
param secretsEncryptionKey string = ''

@description('Optional Key Vault secret URI containing DATABASE_URL. The selected managed identity needs Key Vault Secrets User; leave empty to use the template-created PostgreSQL password secret.')
param databaseUrlSecretUri string = ''

@description('Optional Key Vault secret URI containing the bootstrap admin password. The selected managed identity needs Key Vault Secrets User; leave empty to use adminPassword.')
param adminPasswordSecretUri string = ''

@description('Optional Key Vault secret URI containing SECRETS_ENCRYPTION_KEY. The selected managed identity needs Key Vault Secrets User.')
param secretsEncryptionKeySecretUri string = ''

@description('Log Analytics and diagnostic data retention in days (30-730).')
@minValue(30)
@maxValue(730)
param logRetentionDays int = 30

@description('Deploy PostgreSQL and Azure Files diagnostic settings to Log Analytics. Container App console/system logs are already connected through the managed environment and are not duplicated.')
param enableDiagnosticSettings bool = true

@description('Deploy the action group and supported platform metric alerts. Disabled by default to avoid notification and alert-rule costs in one-click/dev deployments.')
param enableAlerts bool = false

@description('Deploy scheduled-query alerts for event-loop stalls, database-pool exhaustion, probe failures, and platform restart logs. Enable on a later deployment after Container Apps log tables have received data.')
param enableLogAlerts bool = false

@description('Optional email receiver for the generated action group. Leave empty to create an action group without an email receiver and attach receivers later.')
param alertEmailReceiver string = ''

@description('PostgreSQL active-connection count that raises an alert. Set below the server max_connections value with room for maintenance and administration.')
@minValue(1)
param postgresConnectionsAlertThreshold int = 80

@description('Container App average response-time alert threshold in milliseconds.')
@minValue(1)
param appResponseTimeAlertThresholdMs int = 3000

@description('Optional deletion locks on PostgreSQL, storage, and the Container App. CanNotDelete protects data while still allowing updates; None keeps teardown one-click.')
@allowed([
  'None'
  'CanNotDelete'
])
param resourceLock string = 'None'

// ---------------------------------------------------------------------------------------------
// Private networking (optional). Choosing "Yes" injects the Container Apps Environment into a
// VNet and puts BOTH the storage account and the PostgreSQL Flexible Server behind Private
// Endpoints (no public access to either). The app reaches them only over the VNet via their
// private IPs. NOTE: this is a CREATE-TIME choice — a Container Apps Environment's VNet config and
// the database's connectivity are set at create time, so an existing "No" deployment cannot be
// flipped to "Yes" in place; it must be redeployed.
@description('Deploy backing storage AND PostgreSQL behind Private Endpoints inside a VNet (Yes) or use the simple public deployment (No). This is a create-time choice and cannot be toggled on an existing deployment.')
@allowed([
  'No'
  'Yes'
])
param privateNetworking string = 'No'

// SECURITY: the 'AllowAzureServices' firewall rule below is Azure's 0.0.0.0-0.0.0.0 switch.
// Despite the name it is NOT scoped to your subscription or tenant — it admits traffic from
// ANY resource in ANY Azure tenant. It is nonetheless required in public mode, because a
// Container Apps consumption environment egresses from addresses that are not stable, so
// there is no narrower rule to write. That makes it a trade-off the operator must accept
// knowingly rather than inherit silently, so this parameter has no default: a deployment
// cannot proceed without answering it.
@description('PUBLIC MODE ONLY — acknowledge that the PostgreSQL server will accept connections from ANY Azure tenant, via the AllowAzureServices 0.0.0.0 rule, because Container Apps egress IPs are not stable. Choose No only when Private networking = Yes, or when you will add your own scoped firewall rules; otherwise the app cannot reach its database. Ignored when Private networking = Yes.')
@allowed([
  'Yes'
  'No'
])
param acknowledgePublicDatabaseAccess string

@description('VNet address space (CIDR) used only when Private networking = Yes. Pick a range that does not overlap your existing networks.')
param vnetAddressSpace string = '10.42.0.0/22'

// ---------------------------------------------------------------------------------------------
// Network access control. TWO INDEPENDENT LAYERS — they are not alternatives:
//
//   1. ipSecurityRestrictions (below) is enforced by the Azure Container Apps INGRESS. A refused
//      caller never completes a TLS handshake with the app, never reaches Python, and therefore
//      cannot attempt authentication at all. This is the layer that actually stops brute force.
//   2. allowlistSeed seeds the IN-APPLICATION allowlist, which an administrator then manages from
//      the Network Access screen without redeploying. It runs inside the container, so a blocked
//      request has already been accepted by the ingress. It is a management capability, NOT an
//      edge defence, and must not be relied on as one.
//
// SAFETY: an Allow list that omits your own address makes the application unreachable from your
// browser. It does NOT lock you out of Azure — recovery is a control-plane call that is entirely
// unaffected by the block:
//   az containerapp ingress access-restriction remove -n <app> -g <rg> --rule-name <name>
@description('Optional. Client IP ranges (CIDR) permitted to reach the application, enforced at the Container Apps ingress before any traffic reaches the container. Leave empty to allow the whole internet. Each entry: { name, description, ipAddressRange }.')
param allowedClientIpRanges array = []

@description('How allowedClientIpRanges is applied. Allow = ONLY the listed ranges may connect (everything else is refused). Deny = the listed ranges are refused and everything else is permitted. All entries share this action; they cannot be mixed.')
@allowed([
  'Allow'
  'Deny'
])
param ipRestrictionMode string = 'Allow'

@description('Optional. Comma-separated IPs/CIDRs used to seed the IN-APP allowlist on first boot only, so a new deployment is not open to the internet while waiting for an administrator to configure it. Never overwrites what an admin later saves in the app. Example: 203.0.113.0/24,198.51.100.7')
param allowlistSeed string = ''

@description('Mode applied to the seeded in-app allowlist. Use monitor to observe what WOULD be blocked without blocking anything, then switch to enforce in the app once the ranges are proven correct. Ignored when allowlistSeed is empty.')
@allowed([
  'monitor'
  'enforce'
])
param allowlistSeedMode string = 'enforce'

// Ingress visibility. 'Internal' removes the public endpoint entirely — the app is then only
// reachable from inside the VNet, which is the strongest possible answer to "restrict who can
// reach it" because there is no public address to attack.
//
// REQUIREMENTS AND CONSEQUENCES — read before choosing 'Internal':
//   * It requires a VNet-injected environment, i.e. Private networking = Yes. Choosing
//     'Internal' without it fails the deployment with an Azure error rather than silently
//     falling back, because a template that quietly ignored this would leave an operator
//     believing they were private when they were not.
//   * You must provide your own way in: a VPN, a Bastion/jump host, a private endpoint, or a
//     subnet router (e.g. Tailscale) deployed inside the VNet. Nothing in this template does
//     that for you, and after deployment the one-click URL will NOT be reachable from the
//     internet — including from the machine that ran the deployment.
@description('Whether the application has a public endpoint. External = reachable from the internet (default). Internal = no public endpoint; reachable only from inside the VNet, which REQUIRES Private networking = Yes and your own connectivity into that VNet (VPN, Bastion, private endpoint or a subnet router).')
@allowed([
  'External'
  'Internal'
])
param ingressVisibility string = 'External'

@description('Infrastructure subnet (CIDR) for the Container Apps Environment. Must be at least a /23 (Container Apps requirement) and inside the VNet address space. Used only when Private networking = Yes.')
param infraSubnetPrefix string = '10.42.0.0/23'

@description('Private Endpoint subnet (CIDR) for the storage and PostgreSQL private endpoints. Must be inside the VNet address space and not overlap the infrastructure subnet. Used only when Private networking = Yes.')
param privateEndpointSubnetPrefix string = '10.42.2.0/27'

var isPrivate = privateNetworking == 'Yes'
var hasUserAssignedIdentity = !empty(containerRegistryIdentityResourceId)
var keyVaultIdentity = hasUserAssignedIdentity ? containerRegistryIdentityResourceId : 'system'
var resourceTags = union({
  Application: appName
  Environment: deploymentEnvironment
  ManagedBy: 'Bicep'
}, tags)

// The public 0.0.0.0 firewall rule is deployed only in public mode AND only when the
// operator has explicitly acknowledged the cross-tenant exposure it creates.
var allowPublicDatabaseAccess = !isPrivate && acknowledgePublicDatabaseAccess == 'Yes'

var normalizedAppName = toLower(appName)
var unique = uniqueString(resourceGroup().id, normalizedAppName)
var compactAppName = replace(normalizedAppName, '-', '')
var namePrefix = substring(compactAppName, 0, min(length(compactAppName), 14))
var workspaceName = '${namePrefix}-law-${unique}'
var environmentName = '${namePrefix}-env-${unique}'
var containerAppName = '${namePrefix}-app-${unique}'
var storageAccountName = toLower(replace('azsup${unique}', '-', ''))
var fileShareName = 'appdata'
var managedEnvStorageName = 'appdata'
var postgresServerName = '${namePrefix}-pg-${unique}'
// SECURITY/CORRECTNESS: the login and password MUST be percent-encoded before being embedded
// in the connection URL. A generated password containing '@', '#', '/' or ':' otherwise breaks
// URL parsing — SQLAlchemy splits the userinfo at the FIRST '@', so the rest of the password is
// absorbed into the host name and the app dies at startup with
// "socket.gaierror: [Errno -2] Name or service not known".
var databaseUrl = 'postgresql+asyncpg://${uriComponent(postgresAdminLogin)}:${uriComponent(postgresAdminPassword)}@${postgres.properties.fullyQualifiedDomainName}:5432/${postgresDatabaseName}?ssl=require'
var databaseUrlSecret = empty(databaseUrlSecretUri) ? {
  name: 'database-url'
  value: databaseUrl
} : {
  name: 'database-url'
  keyVaultUrl: databaseUrlSecretUri
  identity: keyVaultIdentity
}
var adminPasswordSecret = empty(adminPasswordSecretUri) ? {
  name: 'admin-password'
  value: adminPassword
} : {
  name: 'admin-password'
  keyVaultUrl: adminPasswordSecretUri
  identity: keyVaultIdentity
}
var hasSecretsEncryptionKey = !empty(secretsEncryptionKeySecretUri) || !empty(secretsEncryptionKey)
var secretsEncryptionKeySecret = !empty(secretsEncryptionKeySecretUri) ? {
  name: 'secrets-encryption-key'
  keyVaultUrl: secretsEncryptionKeySecretUri
  identity: keyVaultIdentity
} : {
  name: 'secrets-encryption-key'
  value: secretsEncryptionKey
}
var containerSecrets = concat([
  databaseUrlSecret
  adminPasswordSecret
], hasSecretsEncryptionKey ? [secretsEncryptionKeySecret] : [])
var registryCredentials = empty(containerRegistryServer) ? [] : [
  {
    server: containerRegistryServer
    identity: containerRegistryIdentityResourceId
  }
]

// Ingress-level client IP restrictions. Hoisted into a variable because Bicep does not allow a
// for-expression inside a function call such as union().
var ipSecurityRestrictions = [
  for r in allowedClientIpRanges: {
    name: r.name
    description: r.?description ?? ''
    ipAddressRange: r.ipAddressRange
    action: ipRestrictionMode
  }
]

// Private-networking resource names + subnet resource IDs (only materialised when isPrivate).
var vnetName = '${namePrefix}-vnet-${unique}'
var infraSubnetName = 'snet-infra'
var peSubnetName = 'snet-pe'
var infraSubnetId = resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, infraSubnetName)
var peSubnetId = resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, peSubnetName)
var filePrivateDnsZoneName = 'privatelink.file.${environment().suffixes.storage}'
var storageFilePeName = '${storageAccountName}-file-pe'
// Postgres private-networking names. The Flexible Server privatelink DNS zone is fixed for Azure
// public cloud; sovereign clouds use a different zone name (documented limitation).
var postgresPrivateDnsZoneName = 'privatelink.postgres.database.azure.com'
var postgresPeName = '${postgresServerName}-pe'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  tags: resourceTags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

// VNet for private networking (only when Private networking = Yes). Two subnets:
//  - snet-infra: delegated to Microsoft.App/environments, hosts the VNet-injected Container Apps
//    Environment. Container Apps requires this subnet to be at least a /23.
//  - snet-pe: holds the storage Private Endpoint NIC; PE network policies disabled so the PE can
//    be created.
resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = if (isPrivate) {
  name: vnetName
  location: location
  tags: resourceTags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressSpace
      ]
    }
    subnets: [
      {
        name: infraSubnetName
        properties: {
          addressPrefix: infraSubnetPrefix
          delegations: [
            {
              name: 'aca-delegation'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: peSubnetName
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  tags: resourceTags
  properties: {
    zoneRedundant: containerEnvironmentZoneRedundant
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    // VNet injection only in private mode. resourceId() doesn't create an implicit dependency,
    // so the env's dependsOn (below) explicitly waits for the VNet when private.
    vnetConfiguration: isPrivate ? {
      infrastructureSubnetId: infraSubnetId
    } : null
  }
  dependsOn: isPrivate ? [
    vnet
  ] : []
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: resourceTags
  sku: {
    name: storageRedundancy
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    // The Container Apps Azure Files CSI driver authenticates with the account key, so shared-key
    // access must stay enabled even in private mode.
    allowSharedKeyAccess: true
    supportsHttpsTrafficOnly: true
    // In private mode the account is reachable ONLY through its Private Endpoint: public network
    // access is disabled and the default network rule denies everything (AzureServices bypass lets
    // the platform's trusted control-plane operations through).
    publicNetworkAccess: isPrivate ? 'Disabled' : 'Enabled'
    networkAcls: isPrivate ? {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    } : {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    shareDeleteRetentionPolicy: {
      enabled: true
      days: fileShareSoftDeleteRetentionDays
    }
  }
}

resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: fileShareName
  properties: {
    shareQuota: fileShareQuotaGB
    enabledProtocols: 'SMB'
  }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: containerEnv
  name: managedEnvStorageName
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: fileShare.name
      accessMode: 'ReadWrite'
    }
  }
}

// ----- Private storage path (only when Private networking = Yes) -----------------------------
// Private DNS zone for Azure Files, linked to the VNet so the VNet-injected app resolves the
// storage account's privatelink.file.* name to the Private Endpoint's private IP.
resource fileDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = if (isPrivate) {
  name: filePrivateDnsZoneName
  location: 'global'
  tags: resourceTags
}

resource fileDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = if (isPrivate) {
  parent: fileDnsZone
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

// Private Endpoint for the storage account's "file" sub-resource, in the PE subnet.
resource storageFilePe 'Microsoft.Network/privateEndpoints@2023-11-01' = if (isPrivate) {
  name: storageFilePeName
  location: location
  tags: resourceTags
  properties: {
    subnet: {
      id: peSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'file'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: [
            'file'
          ]
        }
      }
    ]
  }
  dependsOn: [
    vnet
  ]
}

resource storageFilePeDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = if (isPrivate) {
  parent: storageFilePe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'file'
        properties: {
          privateDnsZoneId: fileDnsZone.id
        }
      }
    ]
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresServerName
  location: location
  tags: resourceTags
  sku: {
    name: postgresSkuName
    tier: postgresSkuTier
  }
  properties: union({
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: {
      storageSizeGB: postgresStorageSizeGB
      autoGrow: postgresStorageAutoGrow
    }
    backup: {
      backupRetentionDays: postgresBackupRetentionDays
      geoRedundantBackup: postgresGeoRedundantBackup
    }
    highAvailability: union({
      mode: postgresHighAvailabilityMode
    }, empty(postgresStandbyAvailabilityZone) ? {} : {
      standbyAvailabilityZone: postgresStandbyAvailabilityZone
    })
    maintenanceWindow: union({
      customWindow: postgresCustomMaintenanceWindow
    }, postgresCustomMaintenanceWindow == 'Enabled' ? {
      dayOfWeek: postgresMaintenanceDayOfWeek
      startHour: postgresMaintenanceStartHour
      startMinute: postgresMaintenanceStartMinute
    } : {})
    // In private mode the server is reachable ONLY through its Private Endpoint (public access
    // disabled); in public mode it keeps public/TLS access guarded by the AllowAzureServices rule.
    network: {
      publicNetworkAccess: isPrivate ? 'Disabled' : 'Enabled'
    }
  }, empty(postgresAvailabilityZone) ? {} : {
    availabilityZone: postgresAvailabilityZone
  })
}

resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: postgresDatabaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// Firewall rules are a public-access construct — only meaningful in public mode. In private mode
// the server has public access disabled and is reached solely via its Private Endpoint.
// SECURITY: 0.0.0.0-0.0.0.0 is "any Azure service in any tenant", not "my subscription". Gated
// behind acknowledgePublicDatabaseAccess so it can never be created without a deliberate choice.
resource allowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (allowPublicDatabaseAccess) {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// ----- Private PostgreSQL path (only when Private networking = Yes) --------------------------
// Private DNS zone for PostgreSQL Flexible Server, linked to the VNet so the app resolves the
// server's public FQDN (CNAME -> privatelink zone) to the Private Endpoint's private IP.
resource postgresDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = if (isPrivate) {
  name: postgresPrivateDnsZoneName
  location: 'global'
  tags: resourceTags
}

resource postgresDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = if (isPrivate) {
  parent: postgresDnsZone
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

// Private Endpoint for the PostgreSQL server, in the same PE subnet as the storage PE.
resource postgresPe 'Microsoft.Network/privateEndpoints@2023-11-01' = if (isPrivate) {
  name: postgresPeName
  location: location
  tags: resourceTags
  properties: {
    subnet: {
      id: peSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'postgres'
        properties: {
          privateLinkServiceId: postgres.id
          groupIds: [
            'postgresqlServer'
          ]
        }
      }
    ]
  }
  dependsOn: [
    vnet
  ]
}

resource postgresPeDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = if (isPrivate) {
  parent: postgresPe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'postgres'
        properties: {
          privateDnsZoneId: postgresDnsZone.id
        }
      }
    ]
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: resourceTags
  identity: hasUserAssignedIdentity ? {
    type: 'SystemAssigned,UserAssigned'
    userAssignedIdentities: {
      '${containerRegistryIdentityResourceId}': {}
    }
  } : {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registryCredentials
      ingress: union(
        {
          external: ingressVisibility == 'External'
          targetPort: 8000
          transport: 'auto'
          allowInsecure: false
          traffic: [
            {
              latestRevision: true
              weight: 100
            }
          ]
        },
        // Only emit the property when ranges were supplied: an EMPTY ipSecurityRestrictions
        // array is not the same as an absent one, and leaving the default deployment byte-identical
        // to before matters more than template tidiness.
        empty(allowedClientIpRanges) ? {} : { ipSecurityRestrictions: ipSecurityRestrictions }
      )
      secrets: containerSecrets
    }
    template: {
      containers: [
        {
          name: 'azsupagent'
          image: containerImage
          env: [
            {
              name: 'SEED_ADMIN_USERNAME'
              value: adminUsername
            }
            {
              name: 'SEED_ADMIN_PASSWORD'
              secretRef: 'admin-password'
            }
            {
              name: 'DATABASE_URL'
              secretRef: 'database-url'
            }
            ...hasSecretsEncryptionKey ? [
              {
                name: 'SECRETS_ENCRYPTION_KEY'
                secretRef: 'secrets-encryption-key'
              }
            ] : []
            {
              name: 'COOKIE_SECURE'
              value: 'true'
            }
            {
              // ACA ingress APPENDS the address it observed to X-Forwarded-For (measured
              // 2026-08-04 — it does NOT replace the header, contrary to what this comment
              // previously claimed). Trusting it restores the real client IP for the global
              // login throttle, the audit records and the network access allowlist. The app
              // reads the header RIGHT-to-LEFT precisely because of that append behaviour:
              // anything a caller injects sits to the LEFT of the address ACA vouches for.
              // See backend/app/core/clientip.py.
              name: 'TRUST_FORWARDED_HEADERS'
              value: 'true'
            }
            {
              // First-boot seed for the in-app allowlist (see the allowlistSeed parameter).
              // Empty string = no seed, and the app starts unrestricted.
              name: 'IP_ALLOWLIST_SEED'
              value: allowlistSeed
            }
            {
              name: 'IP_ALLOWLIST_SEED_MODE'
              value: allowlistSeedMode
            }
            {
              name: 'DOTNET_SYSTEM_GLOBALIZATION_INVARIANT'
              value: '1'
            }
            {
              name: 'ENTRA_MCP_COMMAND'
              value: '/opt/eidmcp/bin/python'
            }
            {
              name: 'ENTRA_MCP_ARGS'
              value: '/app/third_party/entraid-mcp-server/run_server.py'
            }
            {
              name: 'BROWSER_PROFILE_DIR'
              value: '/tmp/browser-profiles'
            }
            {
              name: 'AZURE_EXTENSION_DIR'
              value: '/opt/az-extensions'
            }
            // Public base URL of THIS API + the front-end origin. Both are the app's own
            // external ingress FQDN (single container serves the SPA + API same-origin).
            // The backend builds OIDC/SAML redirect URIs from PUBLIC_BASE_URL, and sends
            // the post-login redirect to FRONTEND_ORIGIN — without these the defaults fall
            // back to http://localhost:* and cloud SSO redirects point at localhost.
            // Constructed from the environment's defaultDomain (no circular self-reference).
            {
              name: 'PUBLIC_BASE_URL'
              value: 'https://${containerAppName}.${containerEnv.properties.defaultDomain}'
            }
            {
              name: 'FRONTEND_ORIGIN'
              value: 'https://${containerAppName}.${containerEnv.properties.defaultDomain}'
            }
          ]
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 10
              successThreshold: 1
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 30
              timeoutSeconds: 3
              failureThreshold: 3
              successThreshold: 1
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/readyz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
              successThreshold: 1
            }
          ]
          volumeMounts: [
            {
              volumeName: 'appdata'
              mountPath: '/app/.data'
            }
          ]
        }
      ]
      scale: {
        minReplicas: appMinReplicas
        maxReplicas: appMaxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: string(appHttpConcurrency)
              }
            }
          }
        ]
      }
      volumes: [
        {
          name: 'appdata'
          storageType: 'AzureFile'
          storageName: envStorage.name
        }
      ]
    }
  }
  // In private mode the app must wait for the Postgres PE's DNS to be ready (so its first DB
  // connection resolves to the private IP) and there is no public firewall rule. In public mode
  // it waits for the AllowAzureServices firewall rule instead.
  dependsOn: isPrivate ? [
    postgresDatabase
    envStorage
    storageFilePeDnsGroup
    postgresPeDnsGroup
  ] : [
    postgresDatabase
    allowAzureServices
    envStorage
  ]
}

// Container App console and system logs already flow through containerEnv.appLogsConfiguration.
// Adding a second diagnostic setting on the app would be unsupported duplication, so only the
// data services receive diagnostic settings here.
resource postgresDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (enableDiagnosticSettings) {
  name: 'send-to-${workspaceName}'
  scope: postgres
  properties: {
    workspaceId: logAnalytics.id
    logs: [
      {
        category: 'PostgreSQLLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

resource storageFileDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (enableDiagnosticSettings) {
  name: 'send-to-${workspaceName}'
  scope: fileService
  properties: {
    workspaceId: logAnalytics.id
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'StorageRead'
        enabled: true
      }
      {
        category: 'StorageWrite'
        enabled: true
      }
      {
        category: 'StorageDelete'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (enableAlerts || enableLogAlerts) {
  name: '${namePrefix}-ag-${unique}'
  location: 'global'
  tags: resourceTags
  properties: {
    groupShortName: substring('${namePrefix}alerts', 0, min(length('${namePrefix}alerts'), 12))
    enabled: true
    emailReceivers: empty(alertEmailReceiver) ? [] : [
      {
        name: 'operations-email'
        emailAddress: alertEmailReceiver
        useCommonAlertSchema: true
      }
    ]
  }
}

var platformMetricAlerts = [
  {
    name: 'PostgreSQL CPU high'
    description: 'PostgreSQL average CPU exceeded 80 percent for five minutes.'
    scope: postgres.id
    metricNamespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
    metricName: 'cpu_percent'
    operator: 'GreaterThan'
    threshold: 80
    timeAggregation: 'Average'
    dimensions: []
    severity: 2
  }
  {
    name: 'PostgreSQL memory high'
    description: 'PostgreSQL average memory exceeded 85 percent for five minutes.'
    scope: postgres.id
    metricNamespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
    metricName: 'memory_percent'
    operator: 'GreaterThan'
    threshold: 85
    timeAggregation: 'Average'
    dimensions: []
    severity: 2
  }
  {
    name: 'PostgreSQL connections high'
    description: 'PostgreSQL active connections exceeded the configured threshold.'
    scope: postgres.id
    metricNamespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
    metricName: 'active_connections'
    operator: 'GreaterThan'
    threshold: postgresConnectionsAlertThreshold
    timeAggregation: 'Maximum'
    dimensions: []
    severity: 2
  }
  {
    name: 'PostgreSQL storage high'
    description: 'PostgreSQL average storage use exceeded 80 percent for five minutes.'
    scope: postgres.id
    metricNamespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
    metricName: 'storage_percent'
    operator: 'GreaterThan'
    threshold: 80
    timeAggregation: 'Average'
    dimensions: []
    severity: 1
  }
  {
    name: 'PostgreSQL failed connections'
    description: 'PostgreSQL reported more than five failed connections in five minutes.'
    scope: postgres.id
    metricNamespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
    metricName: 'connections_failed'
    operator: 'GreaterThan'
    threshold: 5
    timeAggregation: 'Total'
    dimensions: []
    severity: 1
  }
  {
    name: 'Container App HTTP 5xx'
    description: 'The Container App returned one or more server-error responses in five minutes.'
    scope: containerApp.id
    metricNamespace: 'Microsoft.App/containerapps'
    metricName: 'Requests'
    operator: 'GreaterThan'
    threshold: 0
    timeAggregation: 'Total'
    dimensions: [
      {
        name: 'statusCodeCategory'
        operator: 'Include'
        values: [
          '5xx'
        ]
      }
    ]
    severity: 1
  }
  {
    name: 'Container App replica restarts'
    description: 'A replica cumulative restart counter exceeded three; investigate revision health.'
    scope: containerApp.id
    metricNamespace: 'Microsoft.App/containerapps'
    metricName: 'RestartCount'
    operator: 'GreaterThan'
    threshold: 3
    timeAggregation: 'Maximum'
    dimensions: []
    severity: 1
  }
  {
    name: 'Container App replicas below minimum'
    description: 'Available replicas fell below the configured minimum.'
    scope: containerApp.id
    metricNamespace: 'Microsoft.App/containerapps'
    metricName: 'Replicas'
    operator: 'LessThan'
    threshold: appMinReplicas
    timeAggregation: 'Maximum'
    dimensions: []
    severity: 1
  }
  {
    name: 'Container App response time high'
    description: 'Average ingress response time exceeded the configured threshold for five minutes.'
    scope: containerApp.id
    metricNamespace: 'Microsoft.App/containerapps'
    metricName: 'ResponseTime'
    operator: 'GreaterThan'
    threshold: appResponseTimeAlertThresholdMs
    timeAggregation: 'Average'
    dimensions: []
    severity: 2
  }
  {
    name: 'Azure Files availability low'
    description: 'Azure Files average availability fell below 99 percent for five minutes.'
    scope: fileService.id
    metricNamespace: 'Microsoft.Storage/storageAccounts/fileServices'
    metricName: 'Availability'
    operator: 'LessThan'
    threshold: 99
    timeAggregation: 'Average'
    dimensions: []
    severity: 1
  }
  {
    name: 'Azure Files capacity high'
    description: 'Azure Files capacity exceeded 80 percent of the configured share quota.'
    scope: fileService.id
    metricNamespace: 'Microsoft.Storage/storageAccounts/fileServices'
    metricName: 'FileCapacity'
    operator: 'GreaterThan'
    threshold: fileShareQuotaGB * 1073741824 * 80 / 100
    timeAggregation: 'Average'
    dimensions: [
      {
        name: 'FileShare'
        operator: 'Include'
        values: [
          fileShareName
        ]
      }
    ]
    severity: 2
  }
]

resource platformMetricAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = [for alert in platformMetricAlerts: if (enableAlerts) {
  name: '${containerAppName}-${uniqueString(alert.name)}'
  location: 'global'
  tags: resourceTags
  properties: {
    description: alert.description
    severity: alert.severity
    enabled: true
    scopes: [
      alert.scope
    ]
    evaluationFrequency: alert.metricName == 'FileCapacity' ? 'PT15M' : 'PT1M'
    windowSize: alert.metricName == 'FileCapacity' ? 'PT1H' : 'PT5M'
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'condition'
          criterionType: 'StaticThresholdCriterion'
          metricNamespace: alert.metricNamespace
          metricName: alert.metricName
          operator: alert.operator
          threshold: alert.threshold
          timeAggregation: alert.timeAggregation
          dimensions: alert.dimensions
          skipMetricValidation: false
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
  }
}]

var logAlertDefinitions = [
  {
    name: 'Application readiness and loop health'
    description: 'Application logs reported an event-loop stall, database-pool exhaustion, or readiness failure.'
    query: 'ContainerAppConsoleLogs | where ContainerAppName == \'${containerAppName}\' | where Log has_any (\'event loop blocked for\', \'QueuePool limit\', \'readiness probe failed\')'
  }
  {
    name: 'Container App platform restart'
    description: 'Container Apps system logs reported a restart event for this application.'
    query: 'ContainerAppSystemLogs | where ContainerAppName == \'${containerAppName}\' | where Reason has \'Restart\' or Log has \'restart\''
  }
]

resource logAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [for alert in logAlertDefinitions: if (enableLogAlerts) {
  name: '${containerAppName}-log-${uniqueString(alert.name)}'
  location: location
  kind: 'LogAlert'
  tags: resourceTags
  properties: {
    displayName: alert.name
    description: alert.description
    enabled: true
    severity: 1
    scopes: [
      logAnalytics.id
    ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    skipQueryValidation: true
    autoMitigate: true
    criteria: {
      allOf: [
        {
          query: alert.query
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
  }
}]

resource postgresLock 'Microsoft.Authorization/locks@2016-09-01' = if (resourceLock == 'CanNotDelete') {
  name: 'prevent-accidental-delete'
  scope: postgres
  properties: {
    level: 'CanNotDelete'
    notes: 'Optional deployment lock. Remove deliberately before deleting PostgreSQL.'
  }
}

resource storageLock 'Microsoft.Authorization/locks@2016-09-01' = if (resourceLock == 'CanNotDelete') {
  name: 'prevent-accidental-delete'
  scope: storage
  properties: {
    level: 'CanNotDelete'
    notes: 'Optional deployment lock. Remove deliberately before deleting persistent Azure Files data.'
  }
}

resource containerAppLock 'Microsoft.Authorization/locks@2016-09-01' = if (resourceLock == 'CanNotDelete') {
  name: 'prevent-accidental-delete'
  scope: containerApp
  properties: {
    level: 'CanNotDelete'
    notes: 'Optional deployment lock. Remove deliberately before deleting the application.'
  }
}

output applicationUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output containerAppName string = containerApp.name
output containerAppPrincipalId string = containerApp.identity.principalId
output postgresServerName string = postgres.name
output storageAccountName string = storage.name
output privateNetworking string = privateNetworking
output vnetName string = isPrivate ? vnetName : ''
output postgresPrivateEndpoint string = isPrivate ? postgresPeName : ''
