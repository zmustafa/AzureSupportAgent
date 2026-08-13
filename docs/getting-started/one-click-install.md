---
layout: default
title: One-Click Installation
parent: Getting Started
nav_order: 2
description: Deploy Azure Support Agent through the Azure portal with the supplied ARM template.
permalink: /getting-started/one-click-install/
redirect_from:
  - /INSTALLATION/
---

# One-click installation

Use the one-click path when you want a complete Azure-hosted installation without building an image or running deployment commands locally. The supplied template deploys into your subscription and returns the application's HTTPS URL. Expect about ten minutes end to end.

Prefer the CLI, or want full control over the resources? Use [manual deployment]({{ site.baseurl }}/getting-started/manual-deployment/) instead.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fzmustafa%2FAzureSupportAgent%2Fmain%2Fdeploy%2Fmain.json)

## Prerequisites

- Resource-creation access in an Azure subscription and resource group.
- Permission to assign Reader later, or assistance from an Azure RBAC administrator.
- A strong temporary bootstrap administrator password of at least 12 characters.
- A model provider credential or OAuth account for first-run configuration: an API key (OpenAI, Azure OpenAI, Anthropic, Gemini and others), a GitHub Copilot or ChatGPT sign-in, or a local Ollama / LM Studio endpoint.

### Cost footprint

The template provisions a small, low-cost footprint: an Azure Container App, a PostgreSQL Flexible Server (Burstable B1ms), a Standard_LRS storage account for Azure Files, and a Log Analytics workspace. PostgreSQL is the main ongoing cost. Everything can be removed in one step; see [Teardown](#teardown).

## Deploy

1. Select the **Deploy to Azure** button above. It opens the portal's **Custom deployment** blade pre-loaded with the template. Every resource is created in your own subscription.
2. Select the subscription, resource group, and region. Confirm that the selected region supports the template's Container Apps and PostgreSQL choices.
3. Enter the bootstrap administrator password. Do not reuse a personal or production credential. You are forced to change it at first sign-in, so treat it as temporary.
4. Review the remaining parameters, including application name, container image, database settings, and private-networking choice.
5. Select **Review + create**, resolve validation errors, and then select **Create**.
6. Wait for the deployment to finish. PostgreSQL commonly takes longer than the other resources.
7. Open the deployment's **Outputs** tab and copy `applicationUrl`.
8. Open that HTTPS URL. A cold container may take several seconds to answer the first request.

### Template parameters

The bootstrap administrator password is the only value you must supply. The rest have working defaults.

| Parameter | Default |
| --- | --- |
| Location | `westus3`, validated for Container Apps and PostgreSQL B1ms |
| Container image | the published public image, `docker.io/zmustafa/azure-support-agent:latest` |
| Admin username | `admin` |
| PostgreSQL administrator password | generated automatically |

Verify the current defaults in [`deploy/main.bicep`](https://github.com/zmustafa/AzureSupportAgent/blob/main/deploy/main.bicep); the template is authoritative when it differs from this table.

## What the template creates

The deployment includes the application Container App, its environment, PostgreSQL Flexible Server, persistent storage, and Log Analytics. It also assigns a system-managed identity to the app, but that identity does not automatically receive Reader over your subscriptions.

## After deployment

1. Sign in as the bootstrap administrator.
2. Complete the forced password change. The default policy requires upper case, lower case, and a digit, with a minimum length.
3. Configure an AI provider under **Settings → AI Providers**. Providers ship disabled until one is configured.
4. Add a tenant connection under **Settings → Azure tenants** using **Host identity (managed identity)**.
5. Grant the Container App identity Reader at the smallest useful subscription or management-group scope.
6. Test the connection before running discovery.

### Grant the managed identity read access

In the portal: **Subscription** (or **Management group**) → **Access control (IAM)** → **Add role assignment** → role **Reader** → assign to **Managed identity** → select the Container App. Or with the CLI:

```bash
# Get the app's managed identity principal id
PRINCIPAL_ID=$(az containerapp show -g <resource-group> -n <app-name> \
  --query identity.principalId -o tsv)

# Grant Reader at the subscription scope
az role assignment create --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role Reader \
  --scope /subscriptions/<subscription-id>
```

Back in the application, select **Test** on the connection; it should list your subscriptions.

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
| No models appear in the chat model picker | A provider is configured but not enabled under **Settings → AI Providers** |
| A pasted Azure CLI token expires immediately | Generate a fresh token with `az account get-access-token` and paste the full JSON |
| The administrator password is lost | Another administrator can reset it under **Settings → Access Control → Users**; otherwise reset it against the database as described in [manual deployment]({{ site.baseurl }}/getting-started/manual-deployment/) |

## Teardown

Deleting the resource group removes everything the template created.

```bash
az group delete --name <resource-group> --yes --no-wait
```

In the portal, open the resource group and select **Delete resource group**. See [upgrades and uninstall]({{ site.baseurl }}/getting-started/upgrades-uninstall/) for the full removal checklist.

## Related pages

- [Overview and prerequisites]({{ site.baseurl }}/getting-started/overview/)
- [First-run setup]({{ site.baseurl }}/getting-started/first-run/)
- [Manual deployment]({{ site.baseurl }}/getting-started/manual-deployment/)
- [Upgrades and uninstall]({{ site.baseurl }}/getting-started/upgrades-uninstall/)
