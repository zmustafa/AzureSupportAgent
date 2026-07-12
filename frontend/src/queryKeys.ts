export type AlertsManagerScopeParams = {
  connection_id?: string;
  workload_id?: string;
  subscription_id?: string;
  management_group_id?: string;
};

function alertsManagerScope(params: AlertsManagerScopeParams) {
  return {
    connection_id: params.connection_id ?? "",
    workload_id: params.workload_id ?? "",
    subscription_id: params.subscription_id ?? "",
    management_group_id: params.management_group_id ?? "",
  } as const;
}

export const queryKeys = {
  azureConnections: ["azureConnections"] as const,
  alertsManager: {
    rulesRoot: ["alerts-manager-rules"] as const,
    rules: (params: AlertsManagerScopeParams) => ["alerts-manager-rules", alertsManagerScope(params)] as const,
    actionGroupsRoot: ["alerts-manager-action-groups"] as const,
    actionGroups: (params: AlertsManagerScopeParams) => ["alerts-manager-action-groups", alertsManagerScope(params)] as const,
    activityLogCoverageRoot: ["alerts-manager-activity-log-coverage"] as const,
    activityLogCoverage: (params: AlertsManagerScopeParams) => ["alerts-manager-activity-log-coverage", alertsManagerScope(params)] as const,
    inboxRoot: ["alerts-manager-inbox"] as const,
    inbox: (params: AlertsManagerScopeParams, days: number) => ["alerts-manager-inbox", alertsManagerScope(params), days] as const,
    changesRoot: ["alerts-manager-changes"] as const,
    changes: (connectionId: string, page: number, pageSize: number) => ["alerts-manager-changes", connectionId, page, pageSize] as const,
    summaryRoot: ["alerts-manager-summary"] as const,
    summary: (connectionId: string) => ["alerts-manager-summary", connectionId] as const,
  },
} as const;
