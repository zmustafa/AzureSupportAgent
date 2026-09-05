---
layout: default
title: Technical documentation
nav_order: 30
description: Architecture, implementation, APIs, and contribution guidance.
permalink: /technical/
has_children: true
---

# Technical documentation

| Guide | Audience |
| --- | --- |
| [Architecture]({{ site.baseurl }}/technical/architecture/) | Contributors mapping backend modules, APIs, and frontend views |
| [Technical specification]({{ site.baseurl }}/technical/specification/) | Engineers reviewing the stack, persistence, runtime, and integrations |
| [Manual deployment]({{ site.baseurl }}/getting-started/manual-deployment/) | Operators building and configuring the application manually |
| [Documentation regeneration]({{ site.baseurl }}/technical/documentation-regeneration/) | Maintainers rebuilding feature references and how-to guides from source |
| [Contributing on GitHub](https://github.com/zmustafa/AzureSupportAgent/blob/main/CONTRIBUTING.md) | Developers preparing local changes and pull requests |

The source repository is the authority for implementation behavior. Feature guides describe supported user workflows; technical pages explain how those workflows are implemented.

## Start with a concrete implementation surface

Monitor is one example to trace from a routed view through its typed API client to backend aggregation and stored records. Use the architecture guide for module locations and the specification for the widget/data-source model; use the operational guides for actions in the UI.

{% include screenshot.html file="core-monitor-activity.png" title="Technical reading example — Monitor widgets backed by application data" caption="The built-in overview separates aggregate counters from live-operation status and exposes the dashboard selector. All values come from an offline fixture. The image is a frontend orientation aid, not a deployment topology, benchmark, or verification of external telemetry or multi-replica coordination." %}
