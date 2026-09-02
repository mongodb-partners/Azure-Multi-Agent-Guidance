// =============================================================================
// Multi-Agent Career Assistant — supporting Azure infra (runnable-demo scope)
//
// Provisions: Log Analytics, Container Apps environment, ACR, the embedding Azure
// Function, and the API + UI Container Apps. Secrets (Azure OpenAI key, Mongo URI)
// are read from Azure Key Vault via a user-assigned managed identity — the values
// are pushed into Key Vault by deploy.sh and never transit Bicep parameters.
//
// The MongoDB MCP server is SELF-HOSTED here (mcp.bicep module → mongodb/mongodb-mcp-server on
// Container Apps). Foundry project + model deployments are assumed pre-provisioned (see README
// prerequisites).
// =============================================================================

@description('Location for all resources')
param location string = resourceGroup().location

@description('Short name prefix for resources')
param prefix string = 'mdbmaf'

// ---- App configuration (passed through to the API/UI container apps) ----
@description('Azure AI Foundry project endpoint')
param projectEndpoint string

@description('Chat model deployment name (specialists + utility LLM)')
param chatDeployment string = 'gpt-5.6-sol'

@description('Embedding model deployment name')
param embeddingModel string = 'text-embedding-3-large'

@description('Embedding dimensions (3072 for text-embedding-3-large)')
param embeddingDimensions int = 3072

@description('Azure OpenAI endpoint (for the embedding function)')
param azureOpenAIEndpoint string

@description('Key Vault name holding the secrets (created by keyvault.bicep, populated by deploy.sh)')
param keyVaultName string

@description('Name of the Key Vault secret holding the Azure OpenAI API key')
param aoaiKeySecretName string = 'azure-openai-api-key'

@description('Name of the Key Vault secret holding the MongoDB Atlas connection string')
param mongoUriSecretName string = 'mongodb-uri'

@description('Name of the Key Vault secret holding the self-hosted MCP API key')
param mcpApiKeySecretName string = 'mcp-api-key'

@description('Resource ID of the user-assigned managed identity used to read Key Vault secrets')
param workloadIdentityId string

@description('MongoDB database name')
param mongoDb string = 'multi-agent'

@description('Header NAME the self-hosted MCP validates for its API key (non-Authorization)')
param mcpApiKeyHeader string = 'X-MCP-Api-Key'

@description('API-key VALUE the self-hosted MCP requires (empty = unauthenticated MCP)')
@secure()
param mcpApiKey string = ''

@description('Run the self-hosted MongoDB MCP read-only (recommended)')
param mcpReadOnly bool = true

@description('Specialist execution mode: registered-or-inline | registered | inline')
param specialistMode string = 'registered-or-inline'

@description('Container image tag (built + pushed by deploy.sh)')
param imageTag string = 'latest'

@description('Name of the pre-created ACR (deploy.sh creates it and builds images before this deploy)')
param acrName string

@description('Email for alert notifications (optional; leave empty to skip email receivers)')
param alertEmail string = ''

var envName = '${prefix}-cae'
var apiAppName = '${prefix}-api'
var uiAppName = '${prefix}-ui'
// Function App names must be GLOBALLY unique (…​.azurewebsites.net). Suffix with a
// per-resource-group hash so multiple RGs/regions don't collide on the same name.
var functionAppName = '${prefix}-embed-func-${uniqueString(resourceGroup().id)}'
var storageAccountName = toLower('st${uniqueString(resourceGroup().id)}')

// ---------------------------------------------------------------------------
// Observability + Container Apps environment
// ---------------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
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

// Application Insights (workspace-based) — target for OpenTelemetry from the API + Function.
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-appi'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
  }
}

// ---------------------------------------------------------------------------
// Azure Container Registry (pre-created by deploy.sh; images already built+pushed)
// ---------------------------------------------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: acrName
}

// ---------------------------------------------------------------------------
// Key Vault (created by keyvault.bicep) — referenced for secret URIs
// ---------------------------------------------------------------------------
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

var aoaiKeySecretUri = '${kv.properties.vaultUri}secrets/${aoaiKeySecretName}'
var mongoUriSecretUri = '${kv.properties.vaultUri}secrets/${mongoUriSecretName}'
var mcpApiKeySecretUri = '${kv.properties.vaultUri}secrets/${mcpApiKeySecretName}'

// ---------------------------------------------------------------------------
// Embedding Azure Function (reused pattern from mongodb-foundry-agent)
// ---------------------------------------------------------------------------
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
}

resource hostingPlan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: '${functionAppName}-plan'
  location: location
  sku: { name: 'Y1', tier: 'Dynamic' }
  properties: { reserved: true }
}

resource functionApp 'Microsoft.Web/sites@2023-01-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${workloadIdentityId}': {}
    }
  }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    keyVaultReferenceIdentity: workloadIdentityId
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.11'
      appSettings: [
        { name: 'AzureWebJobsStorage', value: 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};AccountKey=${storageAccount.listKeys().keys[0].value}' }
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAIEndpoint }
        // Key Vault reference — resolved at runtime via the user-assigned identity.
        { name: 'AZURE_OPENAI_API_KEY', value: '@Microsoft.KeyVault(SecretUri=${aoaiKeySecretUri})' }
        { name: 'EMBEDDING_MODEL', value: embeddingModel }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
      ]
    }
  }
}

var embeddingFunctionUrl = 'https://${functionApp.properties.defaultHostName}/api/embed'

// ---------------------------------------------------------------------------
// Self-hosted MongoDB MCP server (Azure Container Apps) — replaces the remote managed MCP.
// The API + Foundry agents point MONGODB_MCP_URL at this endpoint; the container holds the Mongo
// connection string (from Key Vault) so agents never handle DB credentials.
// ---------------------------------------------------------------------------
// Serialize the Container Apps behind the environment to avoid the well-known
// 'ManagedEnvironmentNotProvisioned' race: apps created while the managed environment is still
// 'Updating' fail. The explicit dependsOn forces this module to wait for the environment resource.
module mcp 'mcp.bicep' = {
  name: 'mcp'
  dependsOn: [ containerEnv ]
  params: {
    location: location
    prefix: prefix
    managedEnvironmentId: containerEnv.id
    keyVaultName: keyVaultName
    mongoUriSecretName: mongoUriSecretName
    workloadIdentityId: workloadIdentityId
    mcpApiKeyHeader: mcpApiKeyHeader
    mcpApiKey: mcpApiKey
    readOnly: mcpReadOnly
  }
}

// ---------------------------------------------------------------------------
// API Container App (FastAPI + SSE)
// ---------------------------------------------------------------------------
resource apiApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: apiAppName
  location: location
  // Serialize after the MCP app (which itself waits for the env) so all three Container Apps are
  // created sequentially into a fully-provisioned environment — avoids ManagedEnvironmentNotProvisioned.
  dependsOn: [ mcp ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${workloadIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: { external: true, targetPort: 3000, transport: 'auto' }
      registries: [ { server: acr.properties.loginServer, username: acr.name, passwordSecretRef: 'acr-pwd' } ]
      secrets: [
        { name: 'acr-pwd', value: acr.listCredentials().passwords[0].value }
        // Key Vault references — resolved by the user-assigned identity (no secret values in Bicep).
        { name: 'mongo-uri', keyVaultUrl: mongoUriSecretUri, identity: workloadIdentityId }
        { name: 'aoai-key', keyVaultUrl: aoaiKeySecretUri, identity: workloadIdentityId }
        { name: 'mcp-api-key', keyVaultUrl: mcpApiKeySecretUri, identity: workloadIdentityId }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: '${acr.properties.loginServer}/career-api:${imageTag}'
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: [
            { name: 'AZURE_AI_PROJECT_ENDPOINT', value: projectEndpoint }
            { name: 'AZURE_AI_MODEL_DEPLOYMENT_NAME', value: chatDeployment }
            { name: 'CLASSIFIER_MODEL_DEPLOYMENT', value: chatDeployment }
            { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAIEndpoint }
            { name: 'AZURE_OPENAI_API_KEY', secretRef: 'aoai-key' }
            { name: 'EMBEDDING_MODEL', value: embeddingModel }
            { name: 'EMBEDDING_DIMENSIONS', value: string(embeddingDimensions) }
            { name: 'EMBEDDING_FUNCTION_URL', value: embeddingFunctionUrl }
            { name: 'MONGODB_URI', secretRef: 'mongo-uri' }
            { name: 'MONGODB_DB', value: mongoDb }
            // Self-hosted MongoDB MCP endpoint (deployed by the mcp.bicep module) + its API-key header.
            { name: 'MONGODB_MCP_URL', value: mcp.outputs.mcpUrl }
            { name: 'MCP_API_KEY_HEADER', value: mcpApiKeyHeader }
            { name: 'MCP_API_KEY', secretRef: 'mcp-api-key' }
            { name: 'AZURE_KEY_VAULT_URL', value: kv.properties.vaultUri }
            { name: 'SPECIALIST_MODE', value: specialistMode }
            { name: 'CLASSIFIER_BACKEND', value: 'auto' }
            { name: 'PORT', value: '3000' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
            { name: 'OTEL_ENABLED', value: 'true' }
            { name: 'OTEL_SERVICE_NAME', value: 'career-assistant-api' }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 2 }
    }
  }
}

// ---------------------------------------------------------------------------
// UI Container App (Streamlit)
// ---------------------------------------------------------------------------
resource uiApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: uiAppName
  location: location
  // Serialize behind the API app + force an explicit env dependency. Container Apps created
  // right after a managed environment finishes provisioning can intermittently get a spurious
  // 'ManagedEnvironmentNotProvisioned' error; this ordering avoids the race.
  dependsOn: [ apiApp ]
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: { external: true, targetPort: 8501, transport: 'auto' }
      registries: [ { server: acr.properties.loginServer, username: acr.name, passwordSecretRef: 'acr-pwd' } ]
      secrets: [ { name: 'acr-pwd', value: acr.listCredentials().passwords[0].value } ]
    }
    template: {
      containers: [
        {
          name: 'ui'
          image: '${acr.properties.loginServer}/career-ui:${imageTag}'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'API_URL', value: 'https://${apiApp.properties.configuration.ingress.fqdn}' }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

// ---------------------------------------------------------------------------
// Observability dashboards + alerts
// ---------------------------------------------------------------------------
module monitoring 'monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    prefix: prefix
    appInsightsId: appInsights.id
    logAnalyticsId: logAnalytics.id
    apiAppId: apiApp.id
    alertEmail: alertEmail
  }
}

output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output apiUrl string = 'https://${apiApp.properties.configuration.ingress.fqdn}'
output uiUrl string = 'https://${uiApp.properties.configuration.ingress.fqdn}'
output embeddingFunctionUrl string = embeddingFunctionUrl
output functionAppName string = functionApp.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output mcpUrl string = mcp.outputs.mcpUrl
output mcpAppName string = mcp.outputs.mcpAppName
