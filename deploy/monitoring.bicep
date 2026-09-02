// =============================================================================
// Observability dashboards + alerts (mirrors the AWS 7-alarm fleet set).
//
// Provisions:
//   * An Action Group (email/webhook) for alert notifications.
//   * A workspace/App-Insights backed Workbook with the key operational tiles.
//   * Metric + log-query alerts on latency, errors, restarts, token spend, and
//     Function failures.
//
// Called from main.bicep with the App Insights + Log Analytics resource IDs.
// =============================================================================

@description('Location for all resources')
param location string = resourceGroup().location

@description('Short name prefix for resources')
param prefix string = 'mdbmaf'

@description('App Insights component resource ID')
param appInsightsId string

@description('Log Analytics workspace resource ID (alert query scope)')
param logAnalyticsId string

@description('API Container App resource ID (for restart/replica alerts)')
param apiAppId string

@description('Email address for alert notifications (optional)')
param alertEmail string = ''

@description('p95 chat latency alert threshold (ms)')
param latencyThresholdMs int = 15000

@description('LLM tokens-per-hour budget before alerting')
param tokenBudgetPerHour int = 500000

// ---------------------------------------------------------------------------
// Action Group
// ---------------------------------------------------------------------------
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${prefix}-ag'
  location: 'global'
  properties: {
    groupShortName: take('${prefix}ag', 12)
    enabled: true
    emailReceivers: empty(alertEmail) ? [] : [
      {
        name: 'primary'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

var alertActions = [
  {
    actionGroupId: actionGroup.id
  }
]

// ---------------------------------------------------------------------------
// 1. API server 5xx failures (App Insights requests)
// ---------------------------------------------------------------------------
resource alertServerErrors 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${prefix}-alert-5xx'
  location: 'global'
  properties: {
    severity: 1
    enabled: true
    scopes: [ appInsightsId ]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'failedRequests'
          metricNamespace: 'microsoft.insights/components'
          metricName: 'requests/failed'
          operator: 'GreaterThan'
          threshold: 5
          timeAggregation: 'Count'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: alertActions
  }
}

// ---------------------------------------------------------------------------
// 2. p95 server response latency
// ---------------------------------------------------------------------------
resource alertLatency 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${prefix}-alert-latency'
  location: 'global'
  properties: {
    severity: 2
    enabled: true
    scopes: [ appInsightsId ]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'serverResponseTime'
          metricNamespace: 'microsoft.insights/components'
          metricName: 'requests/duration'
          operator: 'GreaterThan'
          threshold: latencyThresholdMs
          timeAggregation: 'Average'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: alertActions
  }
}

// ---------------------------------------------------------------------------
// 3. Chat error events (custom metric chat.errors) — log query alert
// 4. Specialist failure rate — log query alert
// 5. LLM token spend/hour over budget — log query alert
// (custom OTel metrics land in the AppTraces/customMetrics tables)
// ---------------------------------------------------------------------------
resource alertChatErrors 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${prefix}-alert-chat-errors'
  location: location
  properties: {
    severity: 2
    enabled: true
    scopes: [ appInsightsId ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      allOf: [
        {
          query: 'customMetrics | where name == "chat.errors" | summarize total = sum(value)'
          timeAggregation: 'Total'
          metricMeasureColumn: 'total'
          operator: 'GreaterThan'
          threshold: 10
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    actions: { actionGroups: [ actionGroup.id ] }
  }
}

resource alertTokenBudget 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${prefix}-alert-token-budget'
  location: location
  properties: {
    severity: 3
    enabled: true
    scopes: [ appInsightsId ]
    evaluationFrequency: 'PT15M'
    windowSize: 'PT1H'
    criteria: {
      allOf: [
        {
          query: 'customMetrics | where name == "llm.tokens" | summarize total = sum(value)'
          timeAggregation: 'Total'
          metricMeasureColumn: 'total'
          operator: 'GreaterThan'
          threshold: tokenBudgetPerHour
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    actions: { actionGroups: [ actionGroup.id ] }
  }
}

// ---------------------------------------------------------------------------
// 6. Embedding Function failures (log query over the workspace)
// ---------------------------------------------------------------------------
resource alertFunctionFailures 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${prefix}-alert-func-failures'
  location: location
  properties: {
    severity: 2
    enabled: true
    scopes: [ logAnalyticsId ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      allOf: [
        {
          query: 'FunctionAppLogs | where Level == "Error" | summarize c = count()'
          timeAggregation: 'Total'
          metricMeasureColumn: 'c'
          operator: 'GreaterThan'
          threshold: 5
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    actions: { actionGroups: [ actionGroup.id ] }
  }
}

// ---------------------------------------------------------------------------
// 7. Container App replica restarts / unhealthy
// ---------------------------------------------------------------------------
resource alertRestarts 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${prefix}-alert-restarts'
  location: 'global'
  properties: {
    severity: 2
    enabled: true
    scopes: [ apiAppId ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'restarts'
          metricNamespace: 'Microsoft.App/containerApps'
          metricName: 'RestartCount'
          operator: 'GreaterThan'
          threshold: 3
          timeAggregation: 'Total'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: alertActions
  }
}

// ---------------------------------------------------------------------------
// Workbook — operational overview
// ---------------------------------------------------------------------------
var workbookContent = {
  version: 'Notebook/1.0'
  items: [
    {
      type: 1
      content: {
        json: '## Career Assistant — Operations\nRequest rate, latency, errors, specialist mix, tool calls, and token spend.'
      }
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'requests | summarize count() by bin(timestamp, 5m) | render timechart'
        size: 0
        title: 'Request rate'
        queryType: 0
      }
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'requests | summarize p50=percentile(duration,50), p95=percentile(duration,95) by bin(timestamp, 5m) | render timechart'
        size: 0
        title: 'Latency p50/p95'
        queryType: 0
      }
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'customMetrics | where name == "specialist.invocations" | summarize sum(value) by tostring(customDimensions.agent) | render piechart'
        size: 0
        title: 'Specialist mix'
        queryType: 0
      }
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'customMetrics | where name == "llm.tokens" | summarize sum(value) by bin(timestamp, 15m) | render columnchart'
        size: 0
        title: 'LLM token spend'
        queryType: 0
      }
    }
  ]
}

resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, '${prefix}-workbook')
  location: location
  kind: 'shared'
  properties: {
    displayName: '${prefix} — Career Assistant Ops'
    serializedData: string(workbookContent)
    category: 'workbook'
    sourceId: appInsightsId
  }
}

output actionGroupId string = actionGroup.id
output workbookId string = workbook.id
