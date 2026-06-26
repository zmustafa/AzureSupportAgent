targetScope = 'resourceGroup'

@description('Azure region for all resources. Defaults to westus3, which has been validated for PostgreSQL Flexible Server B1ms and Azure Container Apps.')
param location string = 'westus3'

@description('Base name for the Azure Support Agent deployment. Use lowercase letters, numbers, and hyphens.')
@minLength(3)
@maxLength(40)
param appName string = 'azure-support-agent'

@description('Container image to deploy. Defaults to the public Docker Hub image, :latest tag, so the one-click button always provisions the newest published release. Switch to GHCR after the GitHub package is made public.')
param containerImage string = 'docker.io/zmustafa/azure-support-agent:latest'

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

@description('Auto-generated PostgreSQL administrator password. Leave unchanged unless you need to supply your own.')
@secure()
@minLength(16)
param postgresAdminPassword string = 'Azsup!${uniqueString(subscription().id, resourceGroup().id, appName)}2026'

@description('PostgreSQL database name used by the app.')
@minLength(1)
@maxLength(63)
param postgresDatabaseName string = 'azsup'

@description('PostgreSQL Flexible Server SKU. B1ms is the lowest-cost managed option for this template.')
param postgresSkuName string = 'Standard_B1ms'

@description('Container CPU cores.')
param containerCpu string = '1.0'

@description('Container memory allocation. Chromium and Azure CLI need headroom, so 2Gi is the default.')
@allowed([
  '1Gi'
  '2Gi'
  '4Gi'
])
param containerMemory string = '2Gi'

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

@description('VNet address space (CIDR) used only when Private networking = Yes. Pick a range that does not overlap your existing networks.')
param vnetAddressSpace string = '10.42.0.0/22'

@description('Infrastructure subnet (CIDR) for the Container Apps Environment. Must be at least a /23 (Container Apps requirement) and inside the VNet address space. Used only when Private networking = Yes.')
param infraSubnetPrefix string = '10.42.0.0/23'

@description('Private Endpoint subnet (CIDR) for the storage and PostgreSQL private endpoints. Must be inside the VNet address space and not overlap the infrastructure subnet. Used only when Private networking = Yes.')
param privateEndpointSubnetPrefix string = '10.42.2.0/27'

var isPrivate = privateNetworking == 'Yes'

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
var databaseUrl = 'postgresql+asyncpg://${postgresAdminLogin}:${postgresAdminPassword}@${postgres.properties.fullyQualifiedDomainName}:5432/${postgresDatabaseName}?ssl=require'

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
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
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
  properties: {
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
  sku: {
    name: 'Standard_LRS'
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
}

resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: fileShareName
  properties: {
    shareQuota: 20
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
  sku: {
    name: postgresSkuName
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    // In private mode the server is reachable ONLY through its Private Endpoint (public access
    // disabled); in public mode it keeps public/TLS access guarded by the AllowAzureServices rule.
    network: {
      publicNetworkAccess: isPrivate ? 'Disabled' : 'Enabled'
    }
  }
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
resource allowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (!isPrivate) {
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
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      secrets: [
        {
          name: 'database-url'
          value: databaseUrl
        }
        {
          name: 'admin-password'
          value: adminPassword
        }
      ]
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
            {
              name: 'COOKIE_SECURE'
              value: 'true'
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
          volumeMounts: [
            {
              volumeName: 'appdata'
              mountPath: '/app/.data'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
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

output applicationUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output containerAppName string = containerApp.name
output postgresServerName string = postgres.name
output storageAccountName string = storage.name
output privateNetworking string = privateNetworking
output vnetName string = isPrivate ? vnetName : ''
output postgresPrivateEndpoint string = isPrivate ? postgresPeName : ''
