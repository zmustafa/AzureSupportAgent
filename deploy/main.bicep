targetScope = 'resourceGroup'

@description('Azure region for all resources. Defaults to westus3, which has been validated for PostgreSQL Flexible Server B1ms and Azure Container Apps.')
param location string = 'westus3'

@description('Base name for the Azure Support Agent deployment. Use lowercase letters, numbers, and hyphens.')
@minLength(3)
@maxLength(40)
param appName string = 'azure-support-agent'

@description('Container image to deploy. Defaults to the public Docker Hub image. Switch to GHCR after the GitHub package is made public.')
param containerImage string = 'docker.io/zmustafa/azure-support-agent:v34'

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
  }
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
    allowSharedKeyAccess: true
    supportsHttpsTrafficOnly: true
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
    network: {
      publicNetworkAccess: 'Enabled'
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

resource allowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
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
  dependsOn: [
    postgresDatabase
    allowAzureServices
    envStorage
  ]
}

output applicationUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output containerAppName string = containerApp.name
output postgresServerName string = postgres.name
output storageAccountName string = storage.name
