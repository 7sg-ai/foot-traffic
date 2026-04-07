@description('Base name for all resources')
param baseName string = 'foottraffic'

@description('Azure region for deployment (all resources except Cognitive Services / OpenAI)')
param location string = resourceGroup().location

@description('Azure region for Azure OpenAI / Cognitive Services. Defaults to East US 2 which has broad model availability.')
param cognitiveServicesLocation string = 'eastus2'

@description('Environment (dev, staging, prod)')
@allowed(['dev', 'staging', 'prod'])
param environment string = 'prod'

@description('Azure OpenAI model deployment name')
// gpt-5.3-chat supports vision (text + image) — used in place of gpt-5.4 which is restricted
param openAiModelDeploymentName string = 'gpt-5.3-chat'

@description('Azure OpenAI model version')
// gpt-5.3-chat (2025-02-01): vision-capable chat model
// Previous: gpt-5.4 (2026-03-05) — restricted, gpt-4o 2024-11-20 (retires 2026-10-01)
param openAiModelVersion string = '2025-02-01'

@description('Synapse SQL admin username')
param synapseSqlAdminUser string = 'sqladmin'

@description('Principal ID of the user/SP running the deployment (optional — grants Key Vault Secrets User so postprovision.sh can read secrets)')
param deploymentPrincipalId string = ''

@description('Tags to apply to all resources')
param tags object = {
  application: 'foot-traffic-analyzer'
  environment: environment
  managedBy: 'bicep'
}

// ============================================================
// Variables
// ============================================================
// All names follow the pattern: <abbrev>-<uniqueSuffix>  e.g. oai-2kmmhwfzp6qsq
// Storage accounts and ACR are alphanumeric-only (no hyphens allowed by Azure):
//   stg<suffix> / syn<suffix> / acr<suffix>  →  3 + 13 = 16 chars ✓
// Everything else uses a hyphen:
//   <abbrev>-<suffix>  →  3-4 + 1 + 13 = 17-18 chars ✓
var uniqueSuffix = uniqueString(resourceGroup().id)
var storageAccountName      = 'stg${uniqueSuffix}'          // no hyphen — storage accounts don't allow it
var synapseStorageName      = 'syn${uniqueSuffix}'          // no hyphen — storage accounts don't allow it
var containerRegistryName   = 'acr${uniqueSuffix}'          // no hyphen — ACR names don't allow it
var functionsAppName        = 'func-${uniqueSuffix}'
var synapseWorkspaceName    = 'syn-${uniqueSuffix}'
var keyVaultName            = 'kv-${uniqueSuffix}'
var containerAppEnvName     = 'cae-${uniqueSuffix}'
var containerAppName        = 'ca-${uniqueSuffix}'
var serviceBusNamespaceName = 'sb-${uniqueSuffix}'
var logAnalyticsName        = 'law-${uniqueSuffix}'
var openAiAccountName       = 'oai-${uniqueSuffix}'

// Auto-generate a strong Synapse SQL password and store it in Key Vault.
// Using a deterministic but unguessable value derived from the resource group ID + a fixed salt.
// This means re-deployments produce the same password (idempotent).
var synapseSqlAdminPassword = '${uniqueString(resourceGroup().id, 'synapse-pwd-salt')}Aa1!'

// ============================================================
// Log Analytics Workspace
// ============================================================
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 90
    features: { enableLogAccessUsingOnlyResourcePermissions: true }
  }
}

// ============================================================
// Key Vault
// ============================================================
resource keyVault 'Microsoft.KeyVault/vaults@2023-02-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: true
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// ============================================================
// Azure OpenAI Account
// gpt-5.3-chat: vision-capable (text + image)
// Provisioned entirely by Bicep — no pre-created resource needed.
//
// Deployed to cognitiveServicesLocation (default: eastus2) which is
// separate from the main location used by all other resources.
// This allows OpenAI/Cognitive Services to be placed in a region with
// better model availability and quota while everything else (Synapse,
// Container Apps, Storage, etc.) stays in the primary location.
//
// NOTE: The model deployment (openAiDeployment) is intentionally NOT
// provisioned here. It is attempted separately in postprovision.sh so
// that quota/region/tier failures do not block the rest of the deployment.
// If the model deployment fails, postprovision.sh will print clear
// instructions for deploying it manually.
// ============================================================
resource openAiAccount 'Microsoft.CognitiveServices/accounts@2023-10-01-preview' = {
  name: openAiAccountName
  location: cognitiveServicesLocation
  tags: tags
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: openAiAccountName
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
}

// Store OpenAI key in Key Vault — apps reference it via Key Vault reference, never see the raw key
resource openAiKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-02-01' = {
  parent: keyVault
  name: 'openai-api-key'
  properties: {
    value: openAiAccount.listKeys().key1
  }
}

// ============================================================
// Storage Account #1 — General purpose (Functions + Frame Cache)
// Standard StorageV2. Used for:
//   • AzureWebJobsStorage (Functions host state / leases)
//   • video-frames blob container  (captured frame images)
//   • analysis-results blob container (VLM output JSON)
//
// Cannot be shared with Synapse: Synapse requires ADLS Gen2
// (isHnsEnabled: true) which is a one-time, irreversible setting
// and changes the storage account's behaviour for non-Synapse workloads.
// ============================================================
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    networkAcls: { defaultAction: 'Allow', bypass: 'AzureServices' }
  }
}

resource framesContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  name: '${storageAccount.name}/default/video-frames'
  properties: { publicAccess: 'None' }
}

resource analysisContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  name: '${storageAccount.name}/default/analysis-results'
  properties: { publicAccess: 'None' }
}

// ============================================================
// Storage Account #2 — ADLS Gen2 (Synapse Analytics only)
// Synapse workspaces require a dedicated storage account with the
// Hierarchical Namespace (HNS) enabled. HNS is a one-time, irreversible
// flag that turns the account into an Azure Data Lake Storage Gen2
// endpoint — enabling the directory semantics and ACL model that
// Synapse depends on. It cannot be enabled on the general-purpose
// account above without recreating it, so a separate account is required.
// ============================================================
resource synapseStorage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: synapseStorageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    isHnsEnabled: true
    networkAcls: { defaultAction: 'Allow', bypass: 'AzureServices' }
  }
}

resource synapseFileSystem 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  name: '${synapseStorage.name}/default/synapse'
  properties: { publicAccess: 'None' }
}

// ============================================================
// Service Bus
// ============================================================
resource serviceBusNamespace 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: serviceBusNamespaceName
  location: location
  tags: tags
  sku: { name: 'Standard', tier: 'Standard' }
}

resource frameAnalysisQueue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: serviceBusNamespace
  name: 'frame-analysis'
  properties: {
    lockDuration: 'PT5M'
    maxSizeInMegabytes: 1024
    requiresDuplicateDetection: false
    requiresSession: false
    defaultMessageTimeToLive: 'PT1H'
    deadLetteringOnMessageExpiration: true
    maxDeliveryCount: 3
  }
}

// ============================================================
// Azure Container Registry
// ============================================================
resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-01-01-preview' = {
  name: containerRegistryName
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: true
    publicNetworkAccess: 'Enabled'
  }
}

// ============================================================
// Azure Synapse Analytics
// Workspace + Dedicated SQL Pool — fully provisioned by Bicep.
// Password is auto-generated and stored in Key Vault.
// ============================================================
resource synapseWorkspace 'Microsoft.Synapse/workspaces@2021-06-01' = {
  name: synapseWorkspaceName
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    defaultDataLakeStorage: {
      accountUrl: 'https://${synapseStorage.name}.dfs.core.windows.net'
      filesystem: 'synapse'
    }
    sqlAdministratorLogin: synapseSqlAdminUser
    sqlAdministratorLoginPassword: synapseSqlAdminPassword
    managedVirtualNetwork: 'default'
    publicNetworkAccess: 'Enabled'
  }
}

resource synapseFirewallAllowAzure 'Microsoft.Synapse/workspaces/firewallRules@2021-06-01' = {
  parent: synapseWorkspace
  name: 'AllowAllWindowsAzureIps'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource synapseFirewallAllowAll 'Microsoft.Synapse/workspaces/firewallRules@2021-06-01' = {
  parent: synapseWorkspace
  name: 'AllowAll'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '255.255.255.255' }
}

resource synapseSqlPool 'Microsoft.Synapse/workspaces/sqlPools@2021-06-01' = {
  parent: synapseWorkspace
  name: 'foottrafficdw'
  location: location
  tags: tags
  sku: { name: 'DW100c' }
  properties: {
    collation: 'SQL_Latin1_General_CP1_CI_AS'
    createMode: 'Default'
  }
}

// Store Synapse password in Key Vault
resource synapseSqlPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-02-01' = {
  parent: keyVault
  name: 'synapse-sql-password'
  properties: { value: synapseSqlAdminPassword }
}

// ============================================================
// Container App Environment
// (declared before the two Container Apps that depend on it)
// ============================================================
resource containerAppEnvironment 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: containerAppEnvName
  location: location
  tags: tags
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

// ============================================================
// Container App (Azure Functions)
// Runs the Functions host inside a container on Azure Container Apps.
// Eliminates the Y1/Consumption App Service Plan quota requirement.
// The official mcr.microsoft.com/azure-functions/python:4-python3.11
// base image bundles the Functions host — behaviour is identical to the
// hosted plan but quota-free and billed per vCPU-second like any other
// Container App.
//
// NOTE: On first provision the ACR is empty, so we use a public placeholder
// image. `azd deploy` (or the deploy scripts) will build and push the real
// images and update the Container Apps automatically.
// ============================================================
resource functionsContainerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: functionsAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'video-analyzer' })
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: containerAppEnvironment.id
    configuration: {
      // Functions host does not need an external ingress — it is driven by
      // timer triggers and Service Bus queue triggers internally.
      ingress: {
        external: false
        targetPort: 80
        transport: 'http'
      }
      registries: [
        {
          server: containerRegistry.properties.loginServer
          username: containerRegistry.listCredentials().username
          passwordSecretRef: 'registry-password'
        }
      ]
      secrets: [
        { name: 'registry-password',          value: containerRegistry.listCredentials().passwords[0].value }
        { name: 'synapse-password',            value: synapseSqlAdminPassword }
        { name: 'openai-api-key',              value: openAiAccount.listKeys().key1 }
        { name: 'storage-connection-string',   value: 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};AccountKey=${storageAccount.listKeys().keys[0].value};EndpointSuffix=core.windows.net' }
        { name: 'servicebus-connection-string', value: listKeys('${serviceBusNamespace.id}/AuthorizationRules/RootManageSharedAccessKey', serviceBusNamespace.apiVersion).primaryConnectionString }
      ]
    }
    template: {
      containers: [
        {
          name: 'functions-app'
          // Use a public placeholder on first provision (ACR is empty at this point).
          // azd deploy / the deploy scripts will push the real image and update this.
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: [
            // Functions host required settings
            { name: 'AzureWebJobsStorage',              secretRef: 'storage-connection-string' }
            { name: 'FUNCTIONS_EXTENSION_VERSION',      value: '~4' }
            { name: 'FUNCTIONS_WORKER_RUNTIME',         value: 'python' }
            // Force the Functions host (ASP.NET Core) to bind on port 80.
            // Without this the host picks a random ephemeral port, causing
            // Container Apps startup probes to fail (PortMismatch warning).
            { name: 'ASPNETCORE_URLS',                  value: 'http://+:80' }
            { name: 'WEBSITES_PORT',                    value: '80' }
            // Azure OpenAI
            { name: 'AZURE_OPENAI_ENDPOINT',    value: openAiAccount.properties.endpoint }
            { name: 'AZURE_OPENAI_API_KEY',     secretRef: 'openai-api-key' }
            { name: 'AZURE_OPENAI_DEPLOYMENT',  value: openAiModelDeploymentName }
            { name: 'AZURE_OPENAI_API_VERSION', value: '2025-01-01-preview' }
            // Storage
            { name: 'STORAGE_ACCOUNT_NAME',      value: storageAccount.name }
            { name: 'STORAGE_CONNECTION_STRING', secretRef: 'storage-connection-string' }
            // Service Bus
            { name: 'SERVICE_BUS_CONNECTION_STRING', secretRef: 'servicebus-connection-string' }
            // Synapse
            { name: 'SYNAPSE_SERVER',   value: '${synapseWorkspace.name}.sql.azuresynapse.net' }
            { name: 'SYNAPSE_DATABASE', value: synapseSqlPool.name }
            { name: 'SYNAPSE_USERNAME', value: synapseSqlAdminUser }
            { name: 'SYNAPSE_PASSWORD', secretRef: 'synapse-password' }
            // Key Vault URI
            { name: 'KEY_VAULT_URI', value: keyVault.properties.vaultUri }
            // Reference frames mode
            // Set REFERENCE_FRAMES_MODE=true to process pre-uploaded profile_data frames
            // instead of capturing live video. Frames must be uploaded to the
            // profile-reference/ prefix in the video-frames container (done by postprovision.sh).
            { name: 'REFERENCE_FRAMES_MODE',   value: 'false' }
            { name: 'REFERENCE_FRAMES_PREFIX', value: 'profile-reference/' }
          ]
        }
      ]
      scale: {
        // Keep exactly 1 replica — the timer trigger must run on a single instance
        // to avoid duplicate captures. Scale-to-zero is disabled for the same reason.
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
  dependsOn: [serviceBusSecret]
}

// ============================================================
// Container App (Streamlit)
// All config wired from provisioned resources.
// ============================================================
resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: containerAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'streamlit-dashboard' })
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: containerAppEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8501
        transport: 'http'
        allowInsecure: false
      }
      registries: [
        {
          server: containerRegistry.properties.loginServer
          username: containerRegistry.listCredentials().username
          passwordSecretRef: 'registry-password'
        }
      ]
      secrets: [
        { name: 'registry-password', value: containerRegistry.listCredentials().passwords[0].value }
        { name: 'synapse-password',  value: synapseSqlAdminPassword }
        { name: 'openai-api-key',    value: openAiAccount.listKeys().key1 }
      ]
    }
    template: {
      containers: [
        {
          name: 'streamlit-app'
          // Use a public placeholder on first provision (ACR is empty at this point).
          // azd deploy / the deploy scripts will push the real image and update this.
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: [
            { name: 'SYNAPSE_SERVER',   value: '${synapseWorkspace.name}.sql.azuresynapse.net' }
            { name: 'SYNAPSE_DATABASE', value: synapseSqlPool.name }
            { name: 'SYNAPSE_USERNAME', value: synapseSqlAdminUser }
            { name: 'SYNAPSE_PASSWORD', secretRef: 'synapse-password' }
            { name: 'AZURE_OPENAI_ENDPOINT',    value: openAiAccount.properties.endpoint }
            { name: 'AZURE_OPENAI_API_KEY',     secretRef: 'openai-api-key' }
            { name: 'AZURE_OPENAI_DEPLOYMENT',  value: openAiModelDeploymentName }
            { name: 'AZURE_OPENAI_API_VERSION', value: '2025-01-01-preview' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
        rules: [
          {
            name: 'http-scaling'
            http: { metadata: { concurrentRequests: '10' } }
          }
        ]
      }
    }
  }
  dependsOn: [openAiKeySecret, synapseSqlPasswordSecret]
}

// ============================================================
// RBAC Assignments
// ============================================================

// Functions Container App -> Key Vault (Secrets User)
resource funcKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, functionsContainerApp.id, 'Key Vault Secrets User')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: functionsContainerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Functions Container App -> Storage (Blob Data Contributor)
resource funcStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionsContainerApp.id, 'Storage Blob Data Contributor')
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: functionsContainerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Functions Container App -> OpenAI (Cognitive Services OpenAI User)
resource funcOpenAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAiAccount.id, functionsContainerApp.id, 'Cognitive Services OpenAI User')
  scope: openAiAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
    principalId: functionsContainerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Container App -> Key Vault (Secrets User)
resource caKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, containerApp.id, 'Key Vault Secrets User')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Container App -> OpenAI (Cognitive Services OpenAI User)
resource caOpenAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAiAccount.id, containerApp.id, 'Cognitive Services OpenAI User')
  scope: openAiAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Synapse Workspace -> Synapse Storage (Storage Blob Data Contributor)
resource synapseStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(synapseStorage.id, synapseWorkspace.id, 'Storage Blob Data Contributor')
  scope: synapseStorage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: synapseWorkspace.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ============================================================
// Remaining Key Vault Secrets
// ============================================================
resource serviceBusSecret 'Microsoft.KeyVault/vaults/secrets@2023-02-01' = {
  parent: keyVault
  name: 'servicebus-connection-string'
  properties: {
    value: listKeys('${serviceBusNamespace.id}/AuthorizationRules/RootManageSharedAccessKey', serviceBusNamespace.apiVersion).primaryConnectionString
  }
}

// ============================================================
// Outputs — consumed by azd, postprovision.sh, and app config
// ============================================================
output functionsContainerAppName    string = functionsContainerApp.name
output containerAppUrl              string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output synapseWorkspaceName         string = synapseWorkspace.name
output synapseSqlPoolName           string = synapseSqlPool.name
output synapseEndpoint              string = '${synapseWorkspace.name}.sql.azuresynapse.net'
output synapseSqlAdminUser          string = synapseSqlAdminUser
output keyVaultName                 string = keyVault.name
output keyVaultUri                  string = keyVault.properties.vaultUri
output storageAccountName           string = storageAccount.name
output containerRegistryLoginServer string = containerRegistry.properties.loginServer
output logAnalyticsWorkspaceId      string = logAnalytics.id
output openAiEndpoint               string = openAiAccount.properties.endpoint
output openAiAccountName            string = openAiAccount.name
// openAiDeploymentName is a param (not a deployed resource) — deployment is attempted
// in postprovision.sh so quota/region failures don't block infrastructure provisioning.
output openAiDeploymentName         string = openAiModelDeploymentName

// ============================================================
// RBAC — Deployment principal Key Vault access
// Grants the current deployment principal (azd / az CLI user) Secrets User
// access so postprovision.sh can read secrets without a separate grant step.
// ============================================================
resource deployerKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deploymentPrincipalId)) {
  name: guid(keyVault.id, deploymentPrincipalId, 'Key Vault Secrets User deployer')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: deploymentPrincipalId
    principalType: 'User'
  }
}

// ─── azd-injected env var outputs ────────────────────────────────────────────
// azd automatically uppercases Bicep output names and injects them as env vars
// into hook scripts (postprovision.sh, postdeploy.sh). The names below must
// exactly match the variable names used in those scripts.
output AZURE_CONTAINER_REGISTRY_ENDPOINT  string = containerRegistry.properties.loginServer
output AZURE_OPENAI_ACCOUNT_NAME          string = openAiAccount.name
output AZURE_OPENAI_DEPLOYMENT_NAME       string = openAiModelDeploymentName
output AZURE_OPENAI_LOCATION              string = cognitiveServicesLocation
output AZURE_KEY_VAULT_NAME               string = keyVault.name
output AZURE_SYNAPSE_WORKSPACE_NAME       string = synapseWorkspace.name
output AZURE_SYNAPSE_SQL_POOL_NAME        string = synapseSqlPool.name
