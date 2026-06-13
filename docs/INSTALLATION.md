# Installation Guide

Deploy **Azure Support Agent** to your own Azure subscription in about 10 minutes — no
CLI and no local setup required. This guide walks the one-click path from clicking the
button to onboarding your first workload.

> Prefer the CLI or want full control over the resources? See the
> **[manual deployment guide](DEPLOYMENT.md)** instead.

## Prerequisites

- An **Azure subscription** and permission to create resources in a resource group.
- Permission to **assign an RBAC role** later (so the app's identity can read your Azure
  resources) — or a service principal you can use instead.
- An **LLM** to power the agent: an API key (OpenAI, Azure OpenAI, Anthropic, Gemini,
  etc.), a GitHub Copilot/ChatGPT sign-in, or a local Ollama / LM Studio endpoint.

### Roughly what it costs

The template provisions a small, low-cost footprint: an Azure Container App
(scale-to-one), a **PostgreSQL Flexible Server B1ms** (Burstable), a Standard_LRS storage
account (Azure Files), and a Log Analytics workspace. The PostgreSQL server is the main
ongoing cost. You can delete everything in one click when you're done (see
[Teardown](#teardown)).

---

## Step 1 — Click "Deploy to Azure"

From the [README](../README.md#-deploy-to-azure-one-click), click the button:

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fzmustafa%2FAzureSupportAgent%2Fmain%2Fdeploy%2Fmain.json)

This opens the **Custom deployment** blade in the Azure Portal, pre-loaded with the
template. Everything is created **in your own subscription** — your data never leaves it.

- Select (or create) a **Resource group** — e.g. `rg-azure-support-agent`.
- Leave **Region** as the default unless you have a preference.

## Step 2 — Enter the admin password

The only field you **must** fill in is:

- **Admin Password** — the bootstrap password for the first sign-in (minimum 12
  characters). You'll be **forced to change it immediately on first login**, so treat it
  as a temporary value.

Everything else has a sensible default and can be left as-is:

| Parameter | Default |
| --- | --- |
| Location | **West US 3** (validated for Container Apps + PostgreSQL B1ms) |
| Container image | the published public image (`v25`) |
| Admin username | `admin` |
| PostgreSQL admin password | **auto-generated** (you don't need to touch it) |

## Step 3 — Run the template

Click **Review + create**, wait for validation to pass, then click **Create**.

Azure provisions the full stack (Container App, PostgreSQL Flexible Server, storage +
Azure Files share, Log Analytics, and the Container Apps environment). This typically
takes a **few minutes** — the PostgreSQL server is the slowest part.

## Step 4 — Get the `applicationUrl` output

When the deployment finishes:

1. Open the deployment (the notification, or **Resource group → Deployments → `main`**).
2. Go to the **Outputs** tab.
3. Copy the value of **`applicationUrl`** — this is your app's HTTPS address.

It looks like:

```text
https://azuresupportag-app-xxxxx.<region>.azurecontainerapps.io
```

## Step 5 — Open it in the browser

Paste the **`applicationUrl`** into your browser. The app loads to a **sign-in** page.

> First load can take a few extra seconds while the container warms up.

## Step 6 — Change the initial password

1. Sign in with username **`admin`** and the **Admin Password** you set in Step 2.
2. You're immediately prompted to **set a new password**. Choose a strong one — the
   default policy requires a mix of **upper case, lower case, and a digit**, with a
   minimum length.

After this you land on the dashboard.

## Step 7 — Connect an LLM

The agent needs a model provider. **Providers ship disabled until you configure one.**

1. Go to **Settings → AI Providers**.
2. Pick a provider and add its credential:
   - **API-key providers** (OpenAI, Azure OpenAI, Claude, Gemini, Grok, Mistral,
     OpenRouter): paste the key.
   - **GitHub Copilot / ChatGPT**: click **Sign in** and complete the OAuth flow.
   - **Local (Ollama / LM Studio)**: enter the base URL of your local server.
3. Click **Save** — the provider auto-enables. Optionally click **Test connection**, pick
   a model, and **Set as default**.

> Tip: you can enable several providers and switch models per chat.

## Step 8 — Connect your Azure tenant

1. Go to **Settings → Azure tenants → Connect a tenant**.
2. Choose an **authentication method**:
   - **Host identity (managed identity)** — *recommended for the one-click deploy.* The
     Container App already has a system-assigned managed identity; you just need to grant
     it read access (next).
   - **Service principal (client secret / certificate)** — for CI or when you can't use a
     managed identity.
   - **Paste Azure CLI token (short-lived)** — quick testing or a tenant you can't sign
     into on the host. Paste the full JSON from `az account get-access-token`.
3. Save the connection.

### Grant the managed identity read access (required for "Host identity")

The app's identity needs at least **Reader** on the scope you want to explore (a
subscription or management group). In the Azure Portal:

> **Subscription** (or **Management group**) → **Access control (IAM)** → **Add role
> assignment** → role **Reader** → assign to **Managed identity** → pick the Container App
> named `azuresupportag-app-…`.

Or with the CLI:

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

Back in the app, click **Test** on the connection — it should list your subscriptions.

## Step 9 — Onboard a workload

1. Go to **Workloads → Azure Workload Autopilot**.
2. Pick a **scope** (a subscription or management group) and start discovery.
3. The agent uses **Azure Resource Graph** to enumerate your resources, then proposes
   logical **workloads** with reasoning and confidence.
4. Review the candidates and **Save** the ones you want.

> Resource discovery works with the managed identity (no LLM needed). The AI **grouping**
> step uses the provider you configured in Step 7 — make sure that's done first.

---

## What's next

- Start a **chat** and ask about your environment.
- Toggle **Deep investigation** to run a War Room of specialist agents.
- Generate an **Architecture** diagram from a workload.
- Run an **Assessment** or check **Monitoring / Backup coverage**.

See the [feature overview](../README.md#-features) for the full picture.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Connection test says *"please run az login"* / *"setup account"* | The managed identity has no role yet — grant it **Reader** (Step 8). |
| **Workload discovery shows zero resources** | Same cause — the identity needs **Reader** on the subscription/management group. |
| **Pasted token "expired"** immediately | Generate a fresh token with `az account get-access-token` and paste the full JSON again. |
| **No models** in the chat model picker | Enable a provider under **Settings → AI Providers** (Step 7). |
| Forgot the admin password | An admin can reset it under **Settings → Access Control → Users**; otherwise reset it against the database (see [DEPLOYMENT.md](DEPLOYMENT.md)). |
| App is slow on the very first request | Cold start — the container is warming up; subsequent requests are fast. |

## Teardown

To remove everything the template created, delete the resource group:

```bash
az group delete --name <resource-group> --yes --no-wait
```

Or in the Portal: open the resource group → **Delete resource group**.

---

Need the manual / CLI deployment path, production env vars, or cost-tuning notes? See
**[docs/DEPLOYMENT.md](DEPLOYMENT.md)**.
