// =============================================================================
// Self-hosted MongoDB MCP server on Azure Container Apps
//
// Runs the official `mongodb/mongodb-mcp-server` image over Streamable HTTP at `<fqdn>/mcp`.
// The container holds the MongoDB connection string (from Key Vault) so the agents never handle
// DB credentials — they only reach the MCP endpoint. Read-only by default.
//
// Auth: external ingress is hardened with a shared API-KEY HEADER. The server validates an inbound
// header (name = `mcpApiKeyHeader`, value = `mcpApiKey`) via MDB_MCP_HTTP_HEADERS. The header name
// is intentionally NOT `Authorization` so the same tool can be registered on Foundry agents (the
// Foundry runtime rejects a stored MCP tool that carries an `Authorization` header).
// =============================================================================

@description('Location for all resources')
param location string = resourceGroup().location

@description('Short name prefix for resources')
param prefix string = 'mdbmaf'

@description('Container Apps managed environment resource ID')
param managedEnvironmentId string

@description('Key Vault name holding the MongoDB URI secret')
param keyVaultName string

@description('Key Vault secret name for the MongoDB connection string')
param mongoUriSecretName string = 'mongodb-uri'

@description('Resource ID of the user-assigned managed identity used to read Key Vault secrets')
param workloadIdentityId string

@description('Header NAME the MCP server validates for the API key (non-Authorization)')
param mcpApiKeyHeader string = 'X-MCP-Api-Key'

@description('API-key VALUE the MCP server requires on the above header (empty = no auth)')
@secure()
param mcpApiKey string = ''

@description('Read-only MCP (recommended). Set false only if agents must write.')
param readOnly bool = true

@description('Container image for the MongoDB MCP server')
param mcpImage string = 'mongodb/mongodb-mcp-server:latest'

var mcpAppName = '${prefix}-mcp'
var mongoUriSecretUri = '${reference(resourceId('Microsoft.KeyVault/vaults', keyVaultName), '2023-07-01').vaultUri}secrets/${mongoUriSecretName}'

// The server validates request headers via MDB_MCP_HTTP_HEADERS (JSON object). When an API key is
// supplied it requires an exact header match; when empty the endpoint is unauthenticated.
var httpHeadersJson = empty(mcpApiKey) ? '{}' : '{"${mcpApiKeyHeader}":"${mcpApiKey}"}'

resource mcpApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: mcpAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${workloadIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: managedEnvironmentId
    configuration: {
      // External ingress so BOTH the in-cluster API and out-of-environment Foundry hosted agents
      // can reach it. The API-key header is the access control (see MDB_MCP_HTTP_HEADERS).
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
      }
      secrets: [
        { name: 'mongo-uri', keyVaultUrl: mongoUriSecretUri, identity: workloadIdentityId }
        { name: 'mcp-http-headers', value: httpHeadersJson }
      ]
    }
    template: {
      containers: [
        {
          name: 'mcp'
          image: mcpImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            // Preconfigured MongoDB connection — agents never handle DB credentials.
            { name: 'MDB_MCP_CONNECTION_STRING', secretRef: 'mongo-uri' }
            // Streamable HTTP transport, bound to all interfaces on the ingress target port.
            { name: 'MDB_MCP_TRANSPORT', value: 'http' }
            { name: 'MDB_MCP_HTTP_HOST', value: '0.0.0.0' }
            { name: 'MDB_MCP_HTTP_PORT', value: '3000' }
            // Safety: read-only + no server-side JS.
            { name: 'MDB_MCP_READ_ONLY', value: string(readOnly) }
            { name: 'MDB_MCP_DISABLE_SERVER_SIDE_JS', value: 'true' }
            // API-key header validation (empty object = unauthenticated).
            { name: 'MDB_MCP_HTTP_HEADERS', secretRef: 'mcp-http-headers' }
            // Per-session connection scope: multiple unauthenticated HTTP clients stay isolated.
            { name: 'MDB_MCP_CONNECTION_SCOPE', value: 'session' }
          ]
        }
      ]
      // MCP holds in-memory session state; keep a single replica to avoid cross-replica session loss.
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

@description('Full MCP endpoint URL (Streamable HTTP) for MONGODB_MCP_URL')
output mcpUrl string = 'https://${mcpApp.properties.configuration.ingress.fqdn}/mcp'
output mcpAppName string = mcpApp.name
output mcpFqdn string = mcpApp.properties.configuration.ingress.fqdn
