# Architecture (for contributors)

How Azure Support Agent is put together, so you can find your way around and extend it. For
*using* the product, see [USER_GUIDE.md](USER_GUIDE.md); for the deep spec, see
[TECHNICAL_SPEC.md](TECHNICAL_SPEC.md).

## High level

```
┌──────────────────────────┐        ┌───────────────────────────────────────────┐
│  React + TS + Vite SPA   │  HTTPS │  FastAPI app (uvicorn)                     │
│  frontend/src            │ ─────▶ │  backend/app                              │
│  • ChatView shell + nav  │  REST  │  • api/        REST routers               │
│  • feature views         │  +SSE  │  • agent/      orchestrator + providers   │
│  • TanStack Query cache  │        │  • mcp/        Azure + Graph MCP tools     │
└──────────────────────────┘        │  • <feature>/  collectors, demo, cache    │
                                     │  • core/       db, security, settings     │
                                     └───────────────────────────────────────────┘
                                          │ PostgreSQL   │ Redis   │ Azure Files
```

- **Frontend** — a single-page app. `components/ChatView.tsx` is the shell (sidebar nav +
  routed panels); `components/navConfig.ts` holds nav data; feature views are lazy-loaded
  and code-split. Server state is cached with **TanStack Query**; live agent output streams
  over **SSE**.
- **Backend** — **FastAPI**. The app is constructed exactly once in `app/main.py` (so the
  `/docs` gate works), which mounts every router under `/api`. Health probes (`/healthz`,
  `/readyz`) and `/api/meta` stay outside auth.
- **State** — **PostgreSQL** (relational data via SQLAlchemy + Alembic), **Redis** (caching
  / coordination), and **Azure Files** mounted at `/app/.data` for JSON registries, caches,
  and the encryption key.

## Backend module map

| Area | Where | Notes |
| --- | --- | --- |
| REST routers | `app/api/*.py` | One module per feature; registered in `app/main.py`. |
| Agent loop | `app/agent/` | Orchestrator + one module per LLM provider. |
| MCP tools | `app/mcp/` | Azure (~65) + Microsoft Graph (~43) tools, read/write classified. |
| Coverage detectors | `app/amba/`, `app/telemetry/`, `app/backupdr/` | Each: `collector.py`, `cache.py`, `reference.py`, `demo.py`, `iac.py`. |
| Other proactive | `app/radar/`, `app/perfprofile/`, `app/teleintel/`, `app/reservations/`, `app/identity/`, `app/rbac/` | Same shape (collector + demo + cache). |
| Assessments | `app/assessments/` | Catalog, runner, `pdf_report.py`. |
| Shared PDF engine | `app/core/pdf_common.py` | Primitives shared by assessment + coverage PDFs. |
| Evidence Locker | `app/evidence/` | Write-once, SHA-256-stamped snapshots. |
| Automations | `app/automations/`, `app/workbooks/`, `app/playbooks/` | Scheduler, sub-agents, connectors. |
| Cross-cutting | `app/core/` | `db.py`, `security.py`, `app_settings.py`, `azure_connections.py`, coverage trend/run stores. |

### Conventions worth knowing

- **Demo vs live** — every feature has a `demo.py` that seeds a synthetic snapshot; demo
  scopes never touch Azure. The central catalog is `app/demo_catalog.py`.
- **Cached-only GETs** — coverage `GET /coverage` returns the *latest cached* snapshot (or a
  `report_exists:false` sentinel); a live scan runs only on an explicit `POST /refresh`.
- **Reference baselines** — coverage detectors score against editable, versioned reference
  sets with a change-request inbox (admin).
- **Read/write/audit** — write-classified tools are approval-gated; privileged actions write
  to `AuditLog`.

## Frontend view map

`App.tsx` mounts the global header (version, Help menu, Command Palette, Welcome) and routes
everything else into `ChatView.tsx`, which renders the sidebar and the active panel. Key
shared UI:

| Component | Purpose |
| --- | --- |
| `CommandPalette.tsx` | <kbd>Ctrl/⌘+K</kbd> navigation + quick actions. |
| `HelpMenu.tsx` | Header "?" — Glossary, shortcuts, Trust & Security, About, docs. |
| `WelcomeModal.tsx` | First-run: explore demo data vs connect Azure. |
| `PageIntro.tsx` | Consistent per-page title + blurb + "Learn more". |
| `help/glossary.ts` | In-app glossary + page-intro copy (mirrors CONCEPTS.md). |

## Running locally

```pwsh
# Backend (from backend/, venv active)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Frontend (from frontend/)
npm run dev          # Vite dev server on :5173

# Tests / checks
python -m pytest -q  # backend
npx tsc --noEmit     # frontend types
```

Backend defaults to `environment=local` (which enables `/docs`). The SPA talks to
`VITE_API_BASE` (default `http://localhost:8000/api`).

## Build & deploy

A multi-arch Docker image bundles the built SPA and the API. `deploy/main.bicep`
(+ compiled `main.json`) provisions a Container App, PostgreSQL Flexible Server, and an Azure
Files share. `APP_VERSION` / `VITE_APP_VERSION` build args stamp the running version (shown
in the header and **Help → About**). See [DEPLOYMENT.md](DEPLOYMENT.md).

## Repo hygiene

`docs/` is also where the slide decks and reference spreadsheets live. Generated build
artifacts (`docs/_*.py`, `docs/__pycache__/`, `docs/usecase-render/`, working `*.pptx`
copies) are **not** product source — prefer keeping them out of commits (see `.gitignore`).
