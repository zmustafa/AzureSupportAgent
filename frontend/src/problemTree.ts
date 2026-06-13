// Azure problem taxonomy: 3 levels — Service family -> Resource type -> Problem.
// Level 1 (family) and level 2 (resource type) mirror the Azure support portal;
// level 3 lists the most common real-world problems for each resource, ordered
// roughly most-frequent first. `count` is optional and unused for display.

export type ProblemNode = {
  label: string;
  count?: number;
  children?: ProblemNode[];
};

export const PROBLEM_TREE: ProblemNode[] = [
  {
    label: "Compute — Virtual Machines",
    children: [
      {
        label: "VM running Windows",
        children: [
          { label: "Cannot connect via RDP (port 3389) — failed or timed out" },
          { label: "RDP connects but shows black screen or freezes after login" },
          { label: "CredSSP / authentication error when connecting via RDP" },
          { label: "VM won't boot — stuck on \"Getting ready\" or boot loop" },
          { label: "VM stopped or restarted unexpectedly (need an RCA)" },
          { label: "Allocation failure — not enough capacity to start or resize" },
          { label: "Cannot resize VM — desired size unavailable in region/zone" },
          { label: "High CPU, memory, or disk latency / slow performance" },
          { label: "OS disk or data disk full / out of space" },
          { label: "Reset the local administrator password / locked out" },
          { label: "VM agent not ready or extensions failing to provision" },
          { label: "Config change (NIC/NSG/route) broke connectivity" },
          { label: "Windows Update fails to download or install on the VM" },
          { label: "BSOD or serial console shows OS errors at boot" },
          { label: "VM stuck in Updating or failed provisioning state" },
        ],
      },
      {
        label: "VM running Linux",
        children: [
          { label: "Cannot connect via SSH (port 22) — refused or timed out" },
          { label: "SSH permission denied (publickey) / lost private key" },
          { label: "VM not booting — kernel panic, GRUB, or bad fstab" },
          { label: "Reset SSH key or root/sudo password" },
          { label: "VM stopped or restarted unexpectedly (need an RCA)" },
          { label: "Allocation failure — not enough capacity in region" },
          { label: "Cannot resize VM — desired size unavailable" },
          { label: "Root filesystem full / disk at 100%" },
          { label: "High CPU, memory, IOPS/throughput, or disk latency" },
          { label: "NIC/NSG/route change broke SSH connectivity" },
          { label: "Package manager (apt/yum/dnf/zypper) failing or repo unreachable" },
          { label: "cloud-init or custom script extension failed" },
          { label: "Linux agent (waagent) not responding / extensions stuck" },
          { label: "Attaching or mounting a data disk fails / disk not visible" },
        ],
      },
      {
        label: "VM Scale Sets",
        children: [
          { label: "Allocation failure creating or scaling out instances" },
          { label: "Autoscale not scaling out or in as expected" },
          { label: "Instances in Failed or NotReady state" },
          { label: "Cannot connect (RDP/SSH) to a scale set instance" },
          { label: "Rolling upgrade or reimage stuck or failing" },
          { label: "Application Health extension marking instances unhealthy" },
          { label: "Custom script / extension provisioning failure across instances" },
          { label: "Capacity stuck below desired instance count" },
          { label: "Spot instances being evicted frequently" },
          { label: "Load balancer not distributing traffic to instances" },
        ],
      },
      {
        label: "VM (RedHat / SUSE / Ubuntu)",
        children: [
          { label: "RHUI / repository connectivity failure (can't reach update infra)" },
          { label: "RedHat subscription / entitlement not recognized" },
          { label: "SUSE — zypper or SCC registration not working" },
          { label: "Ubuntu — won't boot after kernel update or config change" },
          { label: "In-place distro upgrade failed or broke the OS" },
          { label: "cloud-init failed on first boot" },
          { label: "SSH/RDP connect failure after image customization" },
          { label: "Time sync / chrony / NTP drift issues" },
        ],
      },
    ],
  },
  {
    label: "Networking",
    children: [
      {
        label: "Application Gateway",
        children: [
          { label: "502 Bad Gateway errors from the backend" },
          { label: "Backend health shows Unhealthy or Unknown" },
          { label: "Upload, bind, or renew an SSL/TLS listener certificate" },
          { label: "Connection timed out reaching the application" },
          { label: "504 Gateway Timeout" },
          { label: "WAF blocking legitimate requests (false positives)" },
          { label: "Create / update / delete operation failed" },
          { label: "Path-based routing or redirect rules not working" },
          { label: "End-to-end TLS / backend certificate trust issues" },
          { label: "Capacity, autoscaling, or performance issues" },
          { label: "Header rewrite or custom host name issues" },
        ],
      },
      {
        label: "Virtual Network",
        children: [
          { label: "Cannot reach a VM or port across VNet peering" },
          { label: "NSG rule blocking expected traffic" },
          { label: "Connection failure between subnets or to on-prem" },
          { label: "Modify or expand VNet / subnet address space" },
          { label: "VNet peering won't connect or shows Disconnected" },
          { label: "Cannot delete a subnet or VNet — resource in use" },
          { label: "Service endpoints not working" },
          { label: "User-defined route / forced tunneling breaks connectivity" },
          { label: "DNS resolution failing inside the VNet" },
          { label: "IP address exhaustion in a subnet" },
        ],
      },
      {
        label: "VPN Gateway",
        children: [
          { label: "Site-to-site VPN tunnel down or not connecting" },
          { label: "Point-to-site VPN client can't connect" },
          { label: "VPN connects but no traffic passes" },
          { label: "Frequent disconnects / unstable tunnel" },
          { label: "IKE/IPsec phase 1 or phase 2 negotiation failure" },
          { label: "BGP routes not learned or advertised" },
          { label: "Gateway stuck in Failed or Updating state" },
          { label: "Throughput lower than expected over the tunnel" },
          { label: "P2S certificate / authentication issues" },
        ],
      },
      {
        label: "Virtual WAN",
        children: [
          { label: "Point-to-site client connectivity issues" },
          { label: "Site-to-site VPN to hub connectivity" },
          { label: "Branch / VPN site not connecting to the hub" },
          { label: "Routing between spokes or hubs not working" },
          { label: "ExpressRoute association in vWAN issues" },
          { label: "Hub creation or update stuck or failed" },
          { label: "Traffic not flowing through the secured hub (firewall)" },
        ],
      },
      {
        label: "Front Door (Standard / Premium)",
        children: [
          { label: "Origin or origin group returning 5xx errors" },
          { label: "Configure or validate a custom domain certificate" },
          { label: "4xx errors (403 / 404) from Front Door" },
          { label: "Origin shows Unhealthy in health probes" },
          { label: "Custom domain not validating / CNAME issues" },
          { label: "Caching not working as expected" },
          { label: "WAF blocking legitimate traffic" },
          { label: "Routing rule or route not matching" },
          { label: "Latency or performance issues" },
        ],
      },
      {
        label: "ExpressRoute",
        children: [
          { label: "Circuit provisioning or de-provisioning" },
          { label: "BGP session down or not learning routes" },
          { label: "Connectivity or packet loss over the circuit" },
          { label: "Private peering not working" },
          { label: "Microsoft peering / route filter issues" },
          { label: "Circuit bandwidth or performance issues" },
          { label: "ExpressRoute gateway in failed state" },
        ],
      },
      {
        label: "Azure Firewall",
        children: [
          { label: "Traffic being blocked unexpectedly" },
          { label: "Application or network rule not allowing traffic" },
          { label: "DNAT rule not forwarding inbound traffic" },
          { label: "SNAT port exhaustion" },
          { label: "Run a packet capture / trace dropped traffic" },
          { label: "Firewall in Failed provisioning state" },
          { label: "FQDN filtering or DNS proxy issues" },
          { label: "Throughput or performance issues" },
        ],
      },
      {
        label: "Load Balancer",
        children: [
          { label: "Cannot connect to the frontend IP" },
          { label: "Backend pool members unhealthy (probe failing)" },
          { label: "Sudden loss of connectivity to the backend pool" },
          { label: "Outbound connectivity / SNAT port exhaustion" },
          { label: "Inbound NAT rule not forwarding" },
          { label: "Session persistence or distribution issues" },
          { label: "Cross-zone or cross-region load balancing issues" },
        ],
      },
      {
        label: "Private Link, DNS & Bastion",
        children: [
          { label: "Private Endpoint connectivity failing" },
          { label: "Private DNS zone not resolving the private endpoint" },
          { label: "Approve or reject a Private Endpoint connection" },
          { label: "Azure DNS not resolving public records" },
          { label: "Private DNS auto-registration not working" },
          { label: "Bastion — cannot connect to a VM via Bastion" },
          { label: "Bastion deployment or configuration issues" },
          { label: "Conditional forwarding to on-prem DNS" },
        ],
      },
    ],
  },
  {
    label: "Containers & Kubernetes",
    children: [
      {
        label: "Kubernetes Service (AKS)",
        children: [
          { label: "Creating or deploying a new AKS cluster fails" },
          { label: "Cluster or node pool upgrade fails or is stuck" },
          { label: "Node in NotReady state" },
          { label: "Pods stuck in Pending / CrashLoopBackOff / ImagePullBackOff" },
          { label: "Cannot reach the app via Ingress or LoadBalancer service" },
          { label: "DNS resolution failing inside the cluster (CoreDNS)" },
          { label: "Scaling a node pool fails (allocation or quota)" },
          { label: "Cluster autoscaler not adding nodes" },
          { label: "PersistentVolume mount or attach failing" },
          { label: "kubectl / API server connectivity issues" },
          { label: "Node out of disk or memory pressure (pod evictions)" },
          { label: "Managed identity / RBAC / authorization errors" },
          { label: "Outbound connectivity from pods failing" },
          { label: "Cluster certificate expired / rotation needed" },
        ],
      },
      {
        label: "Container Apps",
        children: [
          { label: "App returning 5xx or unavailable" },
          { label: "Outbound connectivity to a private network / VNet" },
          { label: "Creating a Container App or environment fails" },
          { label: "Ingress, custom domain, or certificate issues" },
          { label: "Scaling (KEDA) not triggering" },
          { label: "Revision not activating / activation failed" },
          { label: "Image pull from registry failing" },
          { label: "Cold start or performance issues" },
        ],
      },
      {
        label: "Container Registry",
        children: [
          { label: "Unauthorized (401) error pulling images" },
          { label: "Pull images from an AKS cluster fails" },
          { label: "Push to the registry failing" },
          { label: "Connectivity after enabling firewall or private link" },
          { label: "Replication or webhook issues" },
          { label: "Image or tag not found" },
          { label: "Authentication with managed identity or token" },
        ],
      },
    ],
  },
  {
    label: "Web, App Service & Serverless",
    children: [
      {
        label: "Web App (Windows)",
        children: [
          { label: "Web app down — HTTP 500 / 502 / 503 errors" },
          { label: "App slow or high response time" },
          { label: "Deployment failed or app broken after deploy" },
          { label: "App restarting or recycling unexpectedly" },
          { label: "Outbound connections from the app are failing" },
          { label: "Custom domain or SSL certificate binding issues" },
          { label: "Scaling or quota limits reached" },
          { label: "High CPU / memory causing restarts" },
          { label: "App settings or connection strings not applied" },
          { label: "Authentication (Easy Auth) / 403 issues" },
          { label: "Backup and restore issues" },
        ],
      },
      {
        label: "Web App (Linux)",
        children: [
          { label: "Web app down — HTTP 500 / 502 / 503 errors" },
          { label: "App or container fails to start" },
          { label: "Outbound connections from the app are failing" },
          { label: "Application issues after deployment" },
          { label: "Custom container image not pulling or starting" },
          { label: "Slow performance or high latency" },
          { label: "SSL or custom domain binding issues" },
          { label: "Startup command or app settings not working" },
          { label: "VNet integration / private endpoint connectivity" },
          { label: "Logs or diagnostics not appearing" },
        ],
      },
      {
        label: "Function App",
        children: [
          { label: "Function App down or reporting 500 errors" },
          { label: "Functions not triggering (timer / queue / event)" },
          { label: "Outbound connectivity to a private network" },
          { label: "Cold start or slow execution" },
          { label: "Deployment failed or functions disappeared" },
          { label: "Scaling issues on the Consumption plan" },
          { label: "Host or runtime version mismatch errors" },
          { label: "Binding or connection string errors" },
          { label: "Durable Functions orchestration stuck" },
        ],
      },
      {
        label: "Logic App (Standard)",
        children: [
          { label: "Workflow down or not running" },
          { label: "HTTP connector or API call failing" },
          { label: "Trigger not firing" },
          { label: "Run failed with an action error" },
          { label: "Managed connector authentication issues" },
          { label: "VNet integration / private connectivity" },
          { label: "Performance or throughput issues" },
        ],
      },
      {
        label: "App Service Environment (ASE)",
        children: [
          { label: "ASE down or unhealthy" },
          { label: "Connectivity (VNet or on-prem) issues" },
          { label: "Scaling or capacity issues" },
          { label: "ASE upgrade or migration (v2 to v3)" },
          { label: "Certificate or ILB configuration issues" },
          { label: "NSG or route requirements blocking the ASE" },
        ],
      },
    ],
  },
  {
    label: "Databases & Data",
    children: [
      {
        label: "SQL Managed Instance",
        children: [
          { label: "Connection timeouts" },
          { label: "Database not currently available (Error 40613)" },
          { label: "High CPU utilization" },
          { label: "Slow queries / query tuning" },
          { label: "Instance scaling or service tier change" },
          { label: "Login or authentication failures" },
          { label: "Storage full / running out of space" },
          { label: "Backup, restore, or point-in-time restore" },
          { label: "Failover group or availability issues" },
          { label: "Transaction log full" },
        ],
      },
      {
        label: "Azure DB for PostgreSQL (Flexible)",
        children: [
          { label: "Connection timed out to the server" },
          { label: "Connections failing from all clients" },
          { label: "Modify server parameters" },
          { label: "Major version upgrade fails" },
          { label: "High CPU, memory, or IOPS" },
          { label: "Slow queries / performance tuning" },
          { label: "Out of storage / storage full" },
          { label: "High-availability failover issues" },
          { label: "Too many connections / connection limit reached" },
          { label: "Read replica replication lag" },
        ],
      },
      {
        label: "SQL Database",
        children: [
          { label: "Connection timeouts / cannot connect" },
          { label: "Create or drop databases" },
          { label: "High DTU / CPU / performance issues" },
          { label: "Blocking and deadlocks" },
          { label: "Slow queries / query tuning" },
          { label: "Database at max size / storage full" },
          { label: "Geo-replication or failover issues" },
          { label: "Login, firewall, or authentication failures" },
          { label: "Serverless auto-pause waking slowly" },
          { label: "Transient connection errors (40197 / 40501)" },
        ],
      },
      {
        label: "SQL Server in VM (Windows)",
        children: [
          { label: "Database corrupt / recovery pending / suspect" },
          { label: "All operations on SQL are slow" },
          { label: "Database space issues (shrink, truncate, log full)" },
          { label: "Connection or login failures" },
          { label: "Always On availability group issues" },
          { label: "Backup or restore failures" },
          { label: "High CPU or memory pressure" },
          { label: "Disk performance affecting SQL" },
        ],
      },
      {
        label: "Cache for Redis",
        children: [
          { label: "Unable to connect to the cache" },
          { label: "High latency / slow responses" },
          { label: "Timeouts (RedisTimeoutException)" },
          { label: "Scale up / down / in / out issues" },
          { label: "High memory usage / eviction / maxmemory" },
          { label: "Connection drops or disconnects" },
          { label: "Clustering or sharding issues" },
          { label: "TLS / certificate connection issues" },
        ],
      },
      {
        label: "Cosmos DB",
        children: [
          { label: "Request rate too large (429 throttling)" },
          { label: "Partitioning / hot partition issues" },
          { label: "Firewall and virtual network connectivity" },
          { label: "High RU consumption or cost" },
          { label: "High latency on reads or writes" },
          { label: "Indexing policy or query performance" },
          { label: "Private endpoint / connectivity issues" },
          { label: "Consistency or replication lag" },
        ],
      },
      {
        label: "Data Factory",
        children: [
          { label: "Copy activity source or sink errors" },
          { label: "Copy — errors or unexpected results" },
          { label: "Pipeline run failed" },
          { label: "Self-hosted Integration Runtime offline / connectivity" },
          { label: "Authoring — datasets, linked service, or IR setup" },
          { label: "Mapping data flow debugging or preview" },
          { label: "Trigger not firing / schedule issues" },
          { label: "Slow copy throughput / performance" },
          { label: "Authentication to source or sink (managed identity)" },
        ],
      },
      {
        label: "Databricks",
        children: [
          { label: "Classic compute cluster launch failure" },
          { label: "Serverless compute launch failure" },
          { label: "Unity Catalog metastore setup" },
          { label: "Job run failed or cancelled" },
          { label: "Workspace deployment / VNet injection issues" },
          { label: "Cluster terminated unexpectedly" },
          { label: "Notebook or library installation failures" },
          { label: "Connectivity to storage (ADLS) / mount issues" },
        ],
      },
      {
        label: "Event Hubs",
        children: [
          { label: "Receiving events or messages issues" },
          { label: "Send or receive performance issues" },
          { label: "Throttling / quota exceeded" },
          { label: "Consumer group or checkpoint issues" },
          { label: "Connectivity or authentication (SAS, Entra)" },
          { label: "Capture to storage not working" },
        ],
      },
      {
        label: "Synapse Analytics (Spark)",
        children: [
          { label: "Spark job failure" },
          { label: "Livy error / session won't start" },
          { label: "Dedicated SQL pool connectivity or performance" },
          { label: "Pipeline / integration runtime issues" },
          { label: "Spark pool autoscale issues" },
          { label: "Out of memory in a Spark job" },
        ],
      },
    ],
  },
  {
    label: "AI & OpenAI",
    children: [
      {
        label: "Azure OpenAI / OpenAI",
        children: [
          { label: "HTTP 500 / 503 server errors" },
          { label: "HTTP 429 rate limit / quota (TPM) exceeded" },
          { label: "HTTP 400 invalid request" },
          { label: "Poor or slow performance (high latency)" },
          { label: "Service unavailable" },
          { label: "Accuracy or quality of responses" },
          { label: "Content filter blocking legitimate content" },
          { label: "Cannot find or deploy a model" },
          { label: "Quota increase for a deployment" },
          { label: "Authentication, key, or endpoint issues" },
          { label: "Fine-tuning job failures" },
        ],
      },
      {
        label: "Microsoft Foundry",
        children: [
          { label: "Deployment — HTTP 500 internal server error" },
          { label: "Creating, accessing, or managing agents" },
          { label: "Problem connecting to Azure OpenAI" },
          { label: "Project or hub creation issues" },
          { label: "Model deployment failures" },
          { label: "Connection or authentication issues" },
        ],
      },
      {
        label: "Azure AI Search",
        children: [
          { label: "Issue creating a Search service" },
          { label: "Connection timeouts" },
          { label: "Index-related service limits" },
          { label: "Indexer failing or not running" },
          { label: "Query performance / slow searches" },
          { label: "Skillset or enrichment errors" },
          { label: "Data source connectivity issues" },
        ],
      },
      {
        label: "Cognitive Services & ML",
        children: [
          { label: "Document Intelligence / Form Recognizer 429 rate limit" },
          { label: "ML — managed online endpoint create or update" },
          { label: "ML — training job or compute failures" },
          { label: "Cognitive Services key, endpoint, or quota issues" },
          { label: "Model deployment or scoring errors" },
          { label: "Vision / Speech / Language service errors" },
        ],
      },
    ],
  },
  {
    label: "Storage & Backup",
    children: [
      {
        label: "Azure NetApp Files",
        children: [
          { label: "NFS volume mount issues" },
          { label: "Connect from an Azure VM / VNet — network issue" },
          { label: "Active Directory authentication (SMB)" },
          { label: "Capacity pool or volume resize" },
          { label: "Performance or throughput tier issues" },
          { label: "Snapshot, backup, or restore" },
          { label: "Cross-region replication issues" },
        ],
      },
      {
        label: "Blob Storage",
        children: [
          { label: "Throttling (503 server busy)" },
          { label: "Connectivity after enabling the firewall" },
          { label: "Approve or reject Private Endpoint connections" },
          { label: "403 authorization / SAS / access issues" },
          { label: "Slow upload or download performance" },
          { label: "Lifecycle management or tiering not working" },
          { label: "Soft delete, versioning, or restore" },
          { label: "CORS configuration issues" },
        ],
      },
      {
        label: "Files Storage",
        children: [
          { label: "Unable to mount a file share (Windows/Linux)" },
          { label: "Connectivity issues (SMB port 445 blocked)" },
          { label: "Unable to delete a file or share" },
          { label: "File Sync not syncing or slow performance" },
          { label: "Permissions / NTFS ACL issues" },
          { label: "Snapshot or restore issues" },
          { label: "Throttling / IOPS limits" },
        ],
      },
      {
        label: "Storage Account Management",
        children: [
          { label: "Connectivity after enabling the firewall" },
          { label: "Migrate to ZRS / GZRS / RA-GZRS" },
          { label: "Access keys or SAS issues" },
          { label: "Private endpoint or network rule issues" },
          { label: "Account failover (geo) issues" },
          { label: "Cannot delete the storage account" },
          { label: "Access tier or replication changes" },
        ],
      },
      {
        label: "Azure Backup",
        children: [
          { label: "VM backup or restore failing (Windows/Linux)" },
          { label: "Configuring, enabling, or disabling backups" },
          { label: "Backup job failing with an error" },
          { label: "Restore taking too long or failing" },
          { label: "SQL / SAP HANA in-VM backup issues" },
          { label: "MARS agent backup issues" },
          { label: "Soft delete / vault recovery" },
        ],
      },
      {
        label: "Azure Site Recovery",
        children: [
          { label: "Test failover for Azure VMs failing" },
          { label: "Initial replication not progressing" },
          { label: "Recovery Services Vault issues" },
          { label: "Failover or failback failing" },
          { label: "Replication health critical" },
          { label: "Agent / mobility service issues" },
        ],
      },
    ],
  },
  {
    label: "Identity & Access",
    children: [
      {
        label: "Microsoft Entra — Apps",
        children: [
          { label: "API permissions and admin consent" },
          { label: "Certificates, secrets, and federated credentials" },
          { label: "Redirect URI mismatch (AADSTS error)" },
          { label: "Enterprise apps — SSO configuration" },
          { label: "Service principal or app registration issues" },
          { label: "Token or optional claims configuration" },
          { label: "Consent errors (AADSTS65001)" },
        ],
      },
      {
        label: "Microsoft Entra — Sign-in & MFA",
        children: [
          { label: "Conditional Access blocking sign-in" },
          { label: "MFA not prompting or failing" },
          { label: "Grant or block access policies" },
          { label: "Sign-in errors (AADSTS50xxx)" },
          { label: "Self-service password reset (SSPR) issues" },
          { label: "Account locked out" },
        ],
      },
      {
        label: "Microsoft Entra — Directory & Governance",
        children: [
          { label: "B2B guest invite failing" },
          { label: "Privileged Identity Management (PIM) elevation" },
          { label: "Entra Connect Sync — unexpected results" },
          { label: "Group membership / dynamic group issues" },
          { label: "Access reviews or entitlement management" },
          { label: "Directory role assignment issues" },
        ],
      },
      {
        label: "Key Vault",
        children: [
          { label: "Troubleshoot access to Key Vault (403)" },
          { label: "Creating and managing a Key Vault" },
          { label: "Key Vault firewall / private link connectivity" },
          { label: "Secret, key, or certificate access denied" },
          { label: "Access policy vs RBAC confusion" },
          { label: "Certificate renewal or auto-rotation" },
          { label: "Soft delete / purge / recovery" },
        ],
      },
      {
        label: "RBAC & Automation",
        children: [
          { label: "RBAC role assignment problems" },
          { label: "Managed Identity configuration errors" },
          { label: "Custom role definition issues" },
          { label: "\"Insufficient privileges\" errors" },
          { label: "Automation Account runbook failures" },
          { label: "Scope or inheritance issues" },
        ],
      },
      {
        label: "Azure DevOps Services",
        children: [
          { label: "Sign-in not authorized (401 / 403)" },
          { label: "Inviting or removing users" },
          { label: "Self-hosted pipeline agents offline" },
          { label: "Pipeline build or release failures" },
          { label: "Repos or permissions issues" },
          { label: "Parallel jobs / concurrency limits" },
          { label: "Service connection authentication" },
        ],
      },
    ],
  },
  {
    label: "Security & Defender",
    children: [
      {
        label: "Defender for IoT",
        children: [
          { label: "Sensor disconnected from Azure" },
          { label: "Sensor is not accessible" },
          { label: "Devices missing or misclassified" },
          { label: "Onboarding sensors" },
          { label: "Sensor software update issues" },
          { label: "Alerts not generating" },
        ],
      },
      {
        label: "Defender for Cloud",
        children: [
          { label: "Integration with Defender for Endpoint (MDE)" },
          { label: "Storage / posture recommendations" },
          { label: "Security policy assignments" },
          { label: "Secure score not updating" },
          { label: "Agent or extension onboarding" },
          { label: "Vulnerability assessment issues" },
          { label: "Regulatory compliance dashboard" },
        ],
      },
      {
        label: "Sentinel & Intune",
        children: [
          { label: "Sentinel — analytics rule configuration" },
          { label: "Sentinel — viewing and handling incidents" },
          { label: "Sentinel — data connector not ingesting" },
          { label: "Intune — Win32 app deployment" },
          { label: "Intune — device enrollment or compliance" },
          { label: "Intune — policy not applying" },
        ],
      },
    ],
  },
  {
    label: "Monitoring & Management",
    children: [
      {
        label: "Power BI",
        children: [
          { label: "Semantic model (dataset) refresh failing" },
          { label: "Set up or configure the Gateway" },
          { label: "Gateway offline or unreachable" },
          { label: "Refresh timeout or performance" },
          { label: "DirectQuery / connectivity issues" },
          { label: "Capacity or Premium issues" },
          { label: "Report rendering or visual errors" },
        ],
      },
      {
        label: "Log Analytics",
        children: [
          { label: "Query returns no results" },
          { label: "Query returns an error" },
          { label: "Query slow or timing out" },
          { label: "Data not ingesting / missing logs" },
          { label: "High ingestion cost or data volume" },
          { label: "Workspace permissions or access" },
          { label: "Custom logs or DCR issues" },
        ],
      },
      {
        label: "Alerts & Action Groups",
        children: [
          { label: "Alert fired when it shouldn't have (metric)" },
          { label: "Alert not fired when it should have" },
          { label: "Log search alert rule issue" },
          { label: "Action group not notifying (email / SMS / webhook)" },
          { label: "Alert processing rules / suppression" },
          { label: "Dynamic threshold issues" },
        ],
      },
      {
        label: "Azure Update Manager",
        children: [
          { label: "Update failed or not installed" },
          { label: "ARG query / reporting questions" },
          { label: "Machines not showing as compliant" },
          { label: "Scheduled patching not running" },
          { label: "Pre or post scripts failing" },
          { label: "Assessment not completing" },
        ],
      },
      {
        label: "Azure Monitor Agent (AMA)",
        children: [
          { label: "Migrating to AMA (Windows / Linux)" },
          { label: "No heartbeat / data not in workspace" },
          { label: "Data Collection Rule (DCR) not applying" },
          { label: "Agent extension install / provisioning failure" },
          { label: "Duplicate data / both agents running" },
          { label: "Performance counters not collecting" },
        ],
      },
    ],
  },
  {
    label: "Desktop, Billing & Quotas",
    children: [
      {
        label: "Azure Virtual Desktop",
        children: [
          { label: "Users unable to connect" },
          { label: "FSLogix service crash / profile issues" },
          { label: "Profile VHD failed to attach or detach" },
          { label: "Session host unavailable or not registering" },
          { label: "Black screen or disconnects after login" },
          { label: "Host pool or scaling issues" },
          { label: "App attach / MSIX issues" },
          { label: "Slow performance or latency" },
        ],
      },
      {
        label: "Service & subscription limits (quotas)",
        children: [
          { label: "Compute (vCPU / cores) quota increase" },
          { label: "Machine Learning VM quota" },
          { label: "Networking quota (public IPs, NICs)" },
          { label: "Storage quota (Managed Disks, NetApp Files)" },
          { label: "Azure DB quota increase" },
          { label: "Regional or SKU capacity restriction" },
          { label: "Spot vCPU quota increase" },
          { label: "Quota increase request was rejected" },
        ],
      },
      {
        label: "Billing",
        children: [
          { label: "Credit request for a scenario not listed" },
          { label: "Credit request for an outage / service impact" },
          { label: "Still being charged for a cancelled service" },
          { label: "Understand my cost recommendations" },
          { label: "Unexpected charges or cost spike" },
          { label: "Invoice or payment issues" },
          { label: "Reservation or savings plan billing" },
          { label: "Refund request" },
        ],
      },
      {
        label: "Subscription management",
        children: [
          { label: "Reserved instance purchase failing" },
          { label: "Transfer ownership / subscription transfer error" },
          { label: "Unable to cancel a subscription" },
          { label: "Subscription disabled or suspended" },
          { label: "Move resources between subscriptions" },
          { label: "Change directory / tenant for a subscription" },
          { label: "Convert offer type (PAYG / EA / CSP)" },
        ],
      },
    ],
  },
];

/** Flatten the tree into "Family > Resource > Problem" leaf paths. Used as the
 *  candidate catalog sent to the "propose problems" matcher. Computed once. */
function flattenCatalog(nodes: ProblemNode[], trail: string[] = []): string[] {
  const out: string[] = [];
  for (const node of nodes) {
    const path = [...trail, node.label];
    if (node.children?.length) {
      out.push(...flattenCatalog(node.children, path));
    } else {
      out.push(path.join(" > "));
    }
  }
  return out;
}

export const PROBLEM_CATALOG: string[] = flattenCatalog(PROBLEM_TREE);
