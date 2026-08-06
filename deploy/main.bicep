// =============================================================================
// Multi-Agent Career Assistant — supporting Azure infra (runnable-demo scope)
//
// Provisions: Log Analytics, Container Apps environment, ACR, the embedding Azure
// Function, and the API + UI Container Apps. The MongoDB MCP server is REUSED
// (mcpUrl param) rather than redeployed. Foundry project + model deployments are
// assumed pre-provisioned by the user (see README prerequisites).
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

@secure()
@description('Azure OpenAI API key (for the embedding function)')
param azureOpenAIKey string

@secure()
@description('MongoDB Atlas connection string (FabricCluster)')
param mongoUri string

@description('MongoDB database name')
param mongoDb string = 'multi-agent'

@description('Reused MongoDB MCP Container App URL')
param mcpUrl string

@description('Orchestrator mode: classifier | maf-handoff')
param orchestratorMode string = 'classifier'

@description('Container image tag (built + pushed by deploy.sh)')
param imageTag string = 'latest'

@description('Name of the pre-created ACR (deploy.sh creates it and builds images before this deploy)')
param acrName string

var envName = '${prefix}-cae'
var apiAppName = '${prefix}-api'
var uiAppName = '${prefix}-ui'
var functionAppName = '${prefix}-embed-func'
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

// ---------------------------------------------------------------------------
// Azure Container Registry (pre-created by deploy.sh; images already built+pushed)
// ---------------------------------------------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: acrName
}

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
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.11'
      appSettings: [
        { name: 'AzureWebJobsStorage', value: 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};AccountKey=${storageAccount.listKeys().keys[0].value}' }
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAIEndpoint }
        { name: 'AZURE_OPENAI_API_KEY', value: azureOpenAIKey }
        { name: 'EMBEDDING_MODEL', value: embeddingModel }
      ]
    }
  }
}

var embeddingFunctionUrl = 'https://${functionApp.properties.defaultHostName}/api/embed'

// ---------------------------------------------------------------------------
// API Container App (FastAPI + SSE)
// ---------------------------------------------------------------------------
resource apiApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: apiAppName
  location: location
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: { external: true, targetPort: 3000, transport: 'auto' }
      registries: [ { server: acr.properties.loginServer, username: acr.name, passwordSecretRef: 'acr-pwd' } ]
      secrets: [
        { name: 'acr-pwd', value: acr.listCredentials().passwords[0].value }
        { name: 'mongo-uri', value: mongoUri }
        { name: 'aoai-key', value: azureOpenAIKey }
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
            { name: 'MONGODB_MCP_URL', value: mcpUrl }
            { name: 'ORCHESTRATOR_MODE', value: orchestratorMode }
            { name: 'CLASSIFIER_BACKEND', value: 'auto' }
            { name: 'PORT', value: '3000' }
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

output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output apiUrl string = 'https://${apiApp.properties.configuration.ingress.fqdn}'
output uiUrl string = 'https://${uiApp.properties.configuration.ingress.fqdn}'
output embeddingFunctionUrl string = embeddingFunctionUrl
output functionAppName string = functionApp.name
