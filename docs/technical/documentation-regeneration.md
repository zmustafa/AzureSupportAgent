---
layout: default
title: Documentation regeneration
parent: Technical documentation
nav_order: 2
description: How the repository discovers features and deterministically rebuilds its public documentation.
permalink: /technical/documentation-regeneration/
---

# Documentation regeneration

Azure Support Agent changes frequently, so its documentation is maintained by a workspace custom agent that starts from the implementation rather than from an old page list.

## Invoke the agent

In VS Code Chat, choose **Documentation Regenerator** from the agent picker and ask it to regenerate the documentation. An optional prompt can highlight a recent feature, but the agent always performs a complete scan.

The agent definition is stored at `.github/agents/documentation-regenerator.agent.md`. It is committed with the repository so every contributor uses the same workflow and page formats.

## What every run scans

The run uses the following authorities, in order:

1. `RELEASE` and `frontend/src/version.ts` for the current release.
2. `frontend/src/App.tsx` for application routes.
3. `frontend/src/components/navConfig.ts` for sidebar groups, items, URL-driven tabs, Admin areas, security areas, and Automations.
4. The relevant components under `frontend/src/components/` for actual buttons, dialogs, filters, bulk actions, failure states, and handoffs.
5. Every FastAPI router under `backend/app/api/` plus its feature modules for endpoints, request schemas, limits, caching, exports, history, streaming, and write behavior.
6. `backend/app/auth/permissions.py` for exact product permission keys.
7. Connector, provider, MCP-tool, reference-set, assessment, mission, notification, workbook, and playbook registries for currently supported catalogs.
8. Existing reference pages, how-to recipes, indexes, links, and public screenshot assets.

A label is never treated as proof that an operation exists. The run confirms the frontend action, backend endpoint, permission guard, and approval/apply behavior before documenting it.

## Discovery output

During regeneration, the agent writes temporary files under `.docs-work/`:

- `current-inventory.json` — sorted routes, navigation, tabs, features, actions, endpoints, permissions, connectors, safety classification, and mapped pages.
- `coverage-report.md` — missing, stale, removed, ambiguous, and documented features.

The directory is ignored by Git because it is a reproducible local audit artifact. The public Markdown pages remain tracked.

## Documentation layers

The site has two complementary layers:

- **Feature reference** under `docs/user-guide/`, `docs/admin/`, `docs/connectors/`, `docs/security/`, and `docs/reference/` explains purpose, concepts, tabs, permissions, outputs, and limitations.
- **How-to guides** under `docs/how-to/` provide numbered procedures with expected results, verification, safety/rollback, troubleshooting, and related links.

Every visible application area must have both. Every material action must be represented by a `How to` recipe.

## Deterministic update rules

- New routes, navigation items, tabs, permissions, connectors, or actions produce new or expanded documentation.
- Changed workflows update the reference, recipe, indexes, permission references, troubleshooting, and related links together.
- Removed behavior is removed from active pages and navigation; obsolete procedures are not left appearing current.
- Existing explanations are preserved when accurate.
- Titles, category placement, and permalinks remain stable unless the feature was renamed.
- Public examples never contain live identities, IDs, endpoints, or secrets.

## Required validation

A run is complete only when all checks pass:

1. `git diff --check` reports no whitespace errors.
2. Frontmatter, parent hierarchy, titles, and permalinks are valid and unique.
3. Every current route, navigation item, permission area, and connector maps to documentation.
4. Every internal link and image resolves.
5. The full Just the Docs Jekyll site builds locally.
6. A generated HTML crawl reports zero broken `/AzureSupportAgent/` links or assets.
7. Generated `docs/_site/` output is removed.
8. Application source files remain untouched by the documentation run.

The agent does not commit, push, publish, deploy, or modify Azure unless a user explicitly requests a separate operation.
