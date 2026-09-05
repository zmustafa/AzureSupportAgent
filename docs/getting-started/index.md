---
layout: default
title: Getting Started
nav_order: 2
description: Deploy Azure Support Agent, complete first-run configuration, and plan lifecycle operations.
permalink: /getting-started/
has_children: true
---

# Getting started

Use this section to move from an empty Azure subscription to a working Azure Support Agent environment. The safest sequence is to deploy the application, secure the bootstrap account, configure an AI provider, connect Azure with read-only access, and then discover the first workload.

## Choose a path

| Goal | Start here |
| --- | --- |
| Check access, identity, model, and browser requirements | [Overview and prerequisites]({{ site.baseurl }}/getting-started/overview/) |
| Deploy from the Azure portal with the supplied template | [One-click installation]({{ site.baseurl }}/getting-started/one-click-install/) |
| Build and deploy the container yourself | [Manual deployment]({{ site.baseurl }}/getting-started/manual-deployment/) |
| Configure a new installation | [First-run setup]({{ site.baseurl }}/getting-started/first-run/) |
| Enable Microsoft Graph-backed Entra features | [Microsoft Entra setup]({{ site.baseurl }}/getting-started/entra-setup/) |
| Replace a release or remove the installation | [Upgrades and uninstall]({{ site.baseurl }}/getting-started/upgrades-uninstall/) |

## Recommended order

1. Confirm the prerequisites and decide which Azure identity the application will use.
2. Choose one-click or manual deployment.
3. Sign in with the bootstrap administrator and change its password.
4. Configure and test an AI provider.
5. Add an Azure tenant connection and grant only the access it needs.
6. Run Workload Autopilot and review candidates before saving them.
7. Add Graph permissions only if Entra-backed features are required.

> Azure Support Agent starts from a read-oriented posture. Keep connections read-only while evaluating the product. Enable write permissions only for a defined workflow, and retain approval gates.

## First connection: keep the boundary explicit

The connection form is the application-side step after deployment and sign-in. Choose the identity and tenant deliberately; creating this record does not grant that identity Azure access.

{% include screenshot.html file="admin-tenant-host-identity.png" title="Onboarding — host identity with read-only access selected" caption="Settings → Azure Tenants shows the authentication method, optional default subscription, and Read-only for this tenant control. This synthetic draft was not saved or tested; it does not demonstrate a completed deployment, Reader assignment, or successful Azure connection." %}

## After onboarding

With the first workload saved, continue to the [User guide]({{ site.baseurl }}/user-guide/) for what each application area does, the [How-to guides]({{ site.baseurl }}/how-to/) for numbered procedures, and [Administration]({{ site.baseurl }}/admin/) to configure providers, access, and integrations.
