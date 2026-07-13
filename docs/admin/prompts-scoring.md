---
layout: default
title: System Prompts & Assessments
parent: Administration
nav_order: 6
description: Govern system prompts, assessment weights and bands, execution tuning, and architecture colors.
permalink: /admin/prompts-scoring/
---

# System Prompts and Assessments & Architecture

**Permission:** `settings.write`

## Purpose

**App routes:** `/admin/prompts`, `/admin/scoring`

## Prerequisites and data sources



## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### System Prompts

The prompt catalog groups application and specialist instructions. Edit one prompt at a time, compare with its built-in seed where shown, save reviewed changes, or reset an individual prompt. Changes affect future AI work; in-flight operations retain their existing context.

Treat prompt changes as executable policy: prohibit secret disclosure, preserve approval gates, avoid tenant-specific identifiers, and test representative benign and adversarial requests. Reset removes the customization for that prompt; it does not restore earlier custom versions.

### Assessments & Architecture

The screen exposes assessment severity weights, good/warning score bands, execution concurrency, per-check timeout, run budget, high-confidence percentage, and category color overrides for the architecture designer. Known color values must be valid hex; empty restores the built-in palette.

Changing weights or bands changes interpretation of new calculations and dashboards. Record effective dates and avoid comparing runs across scoring regimes without annotation. Increasing concurrency/budgets can increase Azure and model load; lower values can produce timeouts or incomplete confidence.

## Interpretation of results

### Validation

Save related fields together, confirm backend-normalized values, run one bounded assessment, and inspect scoring, confidence, timeout behavior, and Audit Log before wider use.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations



## Troubleshooting


Use the checks below when results differ from expectations.

## Related pages

### Related docs

- [Assessments]({{ site.baseurl }}/user-guide/assessment-performance/assessments/)
- [Architectures]({{ site.baseurl }}/user-guide/design-ownership/architectures/)
- [Auditing]({{ site.baseurl }}/security/auditing/)
