---
layout: default
title: Design & Ownership
parent: User guide
nav_order: 1
description: Design, document, map, and assign accountability across an Azure estate.
permalink: /user-guide/design-ownership/
has_children: true
---

# Design & Ownership

Design & Ownership brings architecture context, operational knowledge, accountability, and estate relationships into one workflow. Use these guides to move from a workload diagram to support-ready documentation and named owners, then explore dependencies and risk across the estate.

## In this section

| Guide | Use it to |
|---|---|
| [AI Insight Packs]({{ site.baseurl }}/user-guide/design-ownership/ai-insight-packs/) | Build scheduled or on-demand AI digests from operational evidence. |
| [Architectures]({{ site.baseurl }}/user-guide/design-ownership/architectures/) | Draw architecture diagrams or reverse-engineer them from workload resources. |
| [Know-Me]({{ site.baseurl }}/user-guide/design-ownership/know-me/) | Maintain support-ready workload knowledge derived from architecture memory. |
| [Ownership]({{ site.baseurl }}/user-guide/design-ownership/ownership/) | Maintain owners and teams, assignments, coverage, suggestions, and attestations. |
| [Estate Graph]({{ site.baseurl }}/user-guide/design-ownership/estate-graph/) | Explore relationships, paths, blast radius, and cached operational overlays. |
| [Network and DNS Diagnostics]({{ site.baseurl }}/user-guide/design-ownership/network-dns-diagnostics/) | Prove reachability and name resolution from inside the network, with Azure evidence beside the result. |
| [Sandbox VM Diagnostics]({{ site.baseurl }}/user-guide/design-ownership/sandbox-diagnostics/) | Run bounded in-guest commands, and understand the approval and audit boundaries around them. |

## Recommended sequence

1. Register or select a workload and confirm its inventory is current.
2. Build an [architecture]({{ site.baseurl }}/user-guide/design-ownership/architectures/) and review every generated relationship.
3. Add [Know-Me]({{ site.baseurl }}/user-guide/design-ownership/know-me/) context for support and investigations.
4. Assign accountable people or teams in [Ownership]({{ site.baseurl }}/user-guide/design-ownership/ownership/).
5. Use [Estate Graph]({{ site.baseurl }}/user-guide/design-ownership/estate-graph/) to inspect cross-workload dependencies and cached risk signals.
6. Add [AI Insight Packs]({{ site.baseurl }}/user-guide/design-ownership/ai-insight-packs/) for recurring review of material changes.
7. When a diagram's expected flow disagrees with reality, prove it with [Network and DNS Diagnostics]({{ site.baseurl }}/user-guide/design-ownership/network-dns-diagnostics/) rather than inferring it from configuration.

## Shared safety model

These views combine stored application records, cached Azure observations, and optional AI output. Generated diagrams, narratives, suggestions, and runbooks are decision support—not authoritative Azure state. Confirm freshness, scope, and source evidence before operational use. Actions that can change Azure, such as applying ownership tags, require an explicit preview and appropriate write access.
