---
layout: default
title: One-Click Installation
parent: Getting Started
nav_order: 2
description: Deploy Azure Support Agent through the Azure portal with the supplied ARM template.
permalink: /getting-started/one-click-install/
---

# One-click installation

Use the one-click path when you want a complete Azure-hosted installation without building an image or running deployment commands locally. The supplied template deploys into your subscription and returns the application's HTTPS URL.

For template parameters, current defaults, and portal screenshots, follow the canonical [Installation Guide]({{ site.baseurl }}/INSTALLATION/).

## Prerequisites

- Resource-creation access in an Azure subscription and resource group.
- Permission to assign Reader later, or assistance from an Azure RBAC administrator.
- A strong temporary bootstrap administrator password of at least 12 characters.
- A model provider credential or OAuth account for first-run configuration.

## Deploy

1. Open the **Deploy to Azure** button in the [Installation Guide]({{ site.baseurl }}/INSTALLATION/).
2. Select the subscription, resource group, and region. Confirm that the selected region supports the template's Container Apps and PostgreSQL choices.
3. Enter the bootstrap administrator password. Do not reuse a personal or production credential.
4. Review the remaining parameters, including application name, container image, database settings, and private-networking choice.
5. Select **Review + create**, resolve validation errors, and then select **Create**.
6. Wait for the deployment to finish. PostgreSQL commonly takes longer than the other resources.
7. Open the deployment's **Outputs** tab and copy `applicationUrl`.
8. Open that HTTPS URL. A cold container may take several seconds to answer the first request.

## What the template creates

The deployment includes the application Container App, its environment, PostgreSQL Flexible Server, persistent storage, and Log Analytics. It also assigns a system-managed identity to the app, but that identity does not automatically receive Reader over your subscriptions.

## After deployment

1. Sign in as the bootstrap administrator.
2. Complete the forced password change.
3. Configure an AI provider.
4. Add a tenant connection using **Host identity (managed identity)**.
5. Grant the Container App identity Reader at the smallest useful subscription or management-group scope.
6. Test the connection before running discovery.

Continue with [First-run setup]({{ site.baseurl }}/getting-started/first-run/).

## Safety notes

- Deployment creates billable Azure resources. Confirm the resource group contents and pricing.
- Do not paste IDs or secrets into issue reports or public chat transcripts.
- Reader enables broad metadata visibility at its assigned scope. Use resource-group or subscription scope instead of management-group scope when practical.
- Private networking is a create-time design choice in the supplied template; validate DNS and outbound access requirements before selecting it.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Template validation fails | Provider registration, regional availability, naming constraints, quotas, and your create permissions |
| The app URL does not answer immediately | Container cold start and Container App revision health |
| Sign-in fails | The exact bootstrap username/password entered during deployment |
| Azure test reports that account setup or sign-in is required | The managed identity has not received Reader at the selected scope |
| Discovery returns no resources | Connection scope, Reader assignment propagation, and the selected subscription |

## Related pages

- [Overview and prerequisites]({{ site.baseurl }}/getting-started/overview/)
- [First-run setup]({{ site.baseurl }}/getting-started/first-run/)
- [Upgrades and uninstall]({{ site.baseurl }}/getting-started/upgrades-uninstall/)
- [Full installation guide]({{ site.baseurl }}/INSTALLATION/)
