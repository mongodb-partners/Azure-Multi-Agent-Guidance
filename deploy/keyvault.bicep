// =============================================================================
// Key Vault + secrets + a user-assigned managed identity for the workload.
//
// Why a USER-assigned identity (not system-assigned)?
//   Container App Key Vault *references* need the identity to already have the
//   "Key Vault Secrets User" role at deploy time. A user-assigned identity can be
//   created and granted access BEFORE the apps are created, avoiding the
//   chicken-and-egg ordering you get with system-assigned identities.
//
// Secret VALUES are pushed by deploy.sh (az keyvault secret set) BEFORE this
// template runs, so no secret ever transits a Bicep parameter. Bicep only
// references them by URI.
// =============================================================================

@description('Location for all resources')
param location string = resourceGroup().location

@description('Short name prefix for resources')
param prefix string = 'mdbmaf'

@description('Object IDs (principals) that should be able to manage secrets (e.g. the deploying user). Optional.')
param adminObjectIds array = []

var kvName = toLower('${prefix}-kv-${uniqueString(resourceGroup().id)}')
var uamiName = '${prefix}-id'

// Built-in role definition IDs
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'    // Key Vault Secrets User (read-only)
var kvSecretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7' // Key Vault Secrets Officer (read/write)

// ---------------------------------------------------------------------------
// User-assigned managed identity used by the API + Function to read secrets
// ---------------------------------------------------------------------------
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: uamiName
  location: location
}

// ---------------------------------------------------------------------------
// Key Vault (RBAC authorization; soft-delete on by default)
// ---------------------------------------------------------------------------
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled' // tighten to Private Endpoint in the networking workstream
  }
}

// The workload identity gets READ-ONLY secret access (Key Vault Secrets User). It only needs to
// READ the app config secrets (mongo-uri, aoai-key, mcp-api-key) via Container App KV references —
// least privilege. (The prior write grant existed only for the removed Atlas MCP token broker.)
resource secretsUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, uami.id, kvSecretsUserRoleId)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Optional: named admins can MANAGE secrets (so deploy.sh `secret set` works via the running user)
resource adminAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for oid in adminObjectIds: {
    name: guid(kv.id, oid, kvSecretsOfficerRoleId)
    scope: kv
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsOfficerRoleId)
      principalId: oid
      principalType: 'User'
    }
  }
]

output keyVaultName string = kv.name
output keyVaultUri string = kv.properties.vaultUri
output uamiId string = uami.id
output uamiClientId string = uami.properties.clientId
output uamiPrincipalId string = uami.properties.principalId
