// =============================================================================
// Azure OpenAI Model Deployment Module
//
// This module is intentionally separate from main.bicep so that quota,
// region-availability, or subscription-tier failures do NOT block the
// rest of the infrastructure deployment.
//
// It is called from postprovision.sh with graceful error handling.
// If it fails, the user is shown clear manual-deployment instructions.
// =============================================================================

@description('Name of the existing Azure OpenAI account')
param openAiAccountName string

@description('Model deployment name (used as the deployment identifier in API calls)')
param deploymentName string = 'gpt-5.3-chat'

@description('Model name in the OpenAI catalog')
param modelName string = 'gpt-5.3-chat'

@description('Model version — gpt-5.3-chat (2025-02-01)')
param modelVersion string = '2025-02-01'

@description('Tokens-per-minute capacity (1 = 1K TPM). Default 10 = 10K TPM.')
param capacityK int = 10

// Reference the existing OpenAI account (already created by main.bicep)
resource openAiAccount 'Microsoft.CognitiveServices/accounts@2023-10-01-preview' existing = {
  name: openAiAccountName
}

resource openAiDeployment 'Microsoft.CognitiveServices/accounts/deployments@2023-10-01-preview' = {
  parent: openAiAccount
  name: deploymentName
  sku: {
    name: 'Standard'
    capacity: capacityK
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
}

output deploymentName string = openAiDeployment.name
output modelVersion   string = modelVersion
output modelRetires   string = 'N/A'
