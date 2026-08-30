export type AlertsManagerScopeParams = {
  connection_id?: string;
  workload_id?: string;
  subscription_id?: string;
  management_group_id?: string;
  all_visible?: boolean;
};

function alertsManagerScope(params: AlertsManagerScopeParams) {
  return {
    connection_id: params.connection_id ?? "",
    workload_id: params.workload_id ?? "",
    subscription_id: params.subscription_id ?? "",
    management_group_id: params.management_group_id ?? "",
    all_visible: params.all_visible ?? false,
  } as const;
}

export type BackupManagerScopeParams = {
  connection_id?: string;
  workload_id?: string;
  subscription_id?: string;
  management_group_id?: string;
};

function backupManagerScope(params: BackupManagerScopeParams) {
  return {
    connection_id: params.connection_id ?? "",
    workload_id: params.workload_id ?? "",
    subscription_id: params.subscription_id ?? "",
    management_group_id: params.management_group_id ?? "",
  } as const;
}

export const queryKeys = {
  azureConnections: ["azureConnections"] as const,
  dashboard: {
    recentItems: ["dashboard", "recent-items"] as const,
    missionReadiness: (workloadId: string) => ["dashboard", "mission-readiness", workloadId] as const,
  },
  backupManager: {
    root: ["backup-manager"] as const,
    capabilities: (p: BackupManagerScopeParams) => ["backup-manager-capabilities", backupManagerScope(p)] as const,
    /** The one analysis every tab reads. Held indefinitely; only an explicit Analyze replaces it. */
    snapshotRoot: ["backup-manager-snapshot"] as const,
    snapshot: (p: BackupManagerScopeParams) => ["backup-manager-snapshot", backupManagerScope(p)] as const,
    analyzeJobRoot: ["backup-manager-analyze-job"] as const,
    analyzeJob: (p: BackupManagerScopeParams) => ["backup-manager-analyze-job", backupManagerScope(p)] as const,
    summaryRoot: ["backup-manager-summary"] as const,
    summary: (p: BackupManagerScopeParams) => ["backup-manager-summary", backupManagerScope(p)] as const,
    inventoryRoot: ["backup-manager-inventory"] as const,
    inventory: (p: BackupManagerScopeParams, filters: Record<string, unknown>) =>
      ["backup-manager-inventory", backupManagerScope(p), filters] as const,
    vaultsRoot: ["backup-manager-vaults"] as const,
    vaults: (p: BackupManagerScopeParams) => ["backup-manager-vaults", backupManagerScope(p)] as const,
    postureRoot: ["backup-manager-posture"] as const,
    posture: (p: BackupManagerScopeParams) => ["backup-manager-posture", backupManagerScope(p)] as const,
    jobsRoot: ["backup-manager-jobs"] as const,
    jobs: (p: BackupManagerScopeParams, filters: Record<string, unknown>) =>
      ["backup-manager-jobs", backupManagerScope(p), filters] as const,
    jobAnalysis: (p: BackupManagerScopeParams) => ["backup-manager-job-analysis", backupManagerScope(p)] as const,
    policiesRoot: ["backup-manager-policies"] as const,
    policies: (p: BackupManagerScopeParams) => ["backup-manager-policies", backupManagerScope(p)] as const,
    gapsRoot: ["backup-manager-gaps"] as const,
    gaps: (p: BackupManagerScopeParams) => ["backup-manager-gaps", backupManagerScope(p)] as const,
    drRoot: ["backup-manager-dr"] as const,
    dr: (p: BackupManagerScopeParams) => ["backup-manager-dr", backupManagerScope(p)] as const,
    drillsRoot: ["backup-manager-drills"] as const,
    drills: (p: BackupManagerScopeParams) => ["backup-manager-drills", backupManagerScope(p)] as const,
    costRoot: ["backup-manager-cost"] as const,
    cost: (p: BackupManagerScopeParams, opts: Record<string, unknown> = {}) =>
      ["backup-manager-cost", backupManagerScope(p), opts] as const,
    costActuals: (p: BackupManagerScopeParams, opts: Record<string, unknown> = {}) =>
      ["backup-manager-cost-actuals", backupManagerScope(p), opts] as const,
    prices: (p: BackupManagerScopeParams) => ["backup-manager-prices", backupManagerScope(p)] as const,
    reportsRoot: ["backup-manager-reports"] as const,
    reports: (p: BackupManagerScopeParams, days: number) => ["backup-manager-reports", backupManagerScope(p), days] as const,
    changesRoot: ["backup-manager-changes"] as const,
    changes: (connectionId: string, page: number, pageSize: number, view = "all", status = "") =>
      ["backup-manager-changes", connectionId, page, pageSize, view, status] as const,
    /** Cached-only headline per workload; refreshed when an analysis finishes. */
    fleet: ["backup-manager-fleet"] as const,
    /** Batched status of every in-flight analysis — polled while the Fleet grid has work. */
    analyzeJobs: ["backup-manager-analyze-jobs"] as const,
    cleanup: ["backup-manager-cleanup"] as const,
    snapshotStore: ["backup-manager-snapshot-store"] as const,
  },
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
    changes: (connectionId: string, page: number, pageSize: number, view = "action_required", sort = "newest") => ["alerts-manager-changes", connectionId, page, pageSize, view, sort] as const,
    summaryRoot: ["alerts-manager-summary"] as const,
    summary: (connectionId: string) => ["alerts-manager-summary", connectionId] as const,
  },
} as const;
