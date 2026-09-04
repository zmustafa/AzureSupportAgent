using './main.bicep'

// Required secrets come from the caller's environment and are never committed to this file.
// Set AZSUP_ADMIN_PASSWORD and AZSUP_POSTGRES_PASSWORD before running `az deployment group`.
param adminPassword = readEnvironmentVariable('AZSUP_ADMIN_PASSWORD')
param postgresAdminPassword = readEnvironmentVariable('AZSUP_POSTGRES_PASSWORD')
param containerImage = 'docker.io/zmustafa/azure-support-agent:latest'

// Private data services, zone-aware platform placement, and bounded horizontal capacity.
param privateNetworking = 'Yes'
param acknowledgePublicDatabaseAccess = 'No'
param containerEnvironmentZoneRedundant = true
param appMinReplicas = 2
param appMaxReplicas = 4
param appHttpConcurrency = 20
param containerCpu = '1.0'
param containerMemory = '2Gi'

// General Purpose supports HA. SameZone avoids making cross-zone support a hidden regional
// prerequisite; choose ZoneRedundant explicitly after confirming the target region and SKU.
param postgresSkuTier = 'GeneralPurpose'
param postgresSkuName = 'Standard_D2s_v3'
param postgresStorageSizeGB = 128
param postgresStorageAutoGrow = 'Enabled'
param postgresBackupRetentionDays = 14
param postgresGeoRedundantBackup = 'Disabled'
param postgresHighAvailabilityMode = 'SameZone'
param postgresCustomMaintenanceWindow = 'Enabled'
param postgresMaintenanceDayOfWeek = 0
param postgresMaintenanceStartHour = 2
param postgresMaintenanceStartMinute = 0

param storageRedundancy = 'Standard_ZRS'
param fileShareQuotaGB = 100
param fileShareSoftDeleteRetentionDays = 30
param logRetentionDays = 90
param enableDiagnosticSettings = true
param enableAlerts = true
// Enable after the ContainerAppConsoleLogs and ContainerAppSystemLogs tables receive data.
param enableLogAlerts = false
param alertEmailReceiver = readEnvironmentVariable('AZSUP_ALERT_EMAIL', '')
param resourceLock = 'CanNotDelete'

param deploymentEnvironment = 'Production'
param tags = {
  DataClassification: 'Confidential'
  Criticality: 'High'
}
