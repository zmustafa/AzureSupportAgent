# Contributing to Azure Support Agent

Thanks for your interest in contributing! This guide covers how to set up the
project locally, make changes, and submit them.

## Code of Conduct

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md). By participating,
you agree to uphold it.

## Project Layout

```
backend/    Python 3.12 / FastAPI API + agent orchestrator (SQLAlchemy, Alembic)
frontend/   React 18 + TypeScript + Vite + Tailwind SPA
third_party/  Vendored EntraID (Microsoft Graph) MCP server
Dockerfile  Single-container image (builds the SPA, serves it from FastAPI)
deploy/     Infrastructure-as-Code for one-click Azure deploys (where present)
```

## Prerequisites

- **Python 3.12+**
- **Node.js 20+**
- **Docker** (optional — for building/running the container image)
- **Azure CLI** (`az`) — only needed to exercise live Azure features

## Local Setup

### Backend

```pwsh
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

Run the backend (uses SQLite locally; set `DATABASE_URL` for PostgreSQL):

```pwsh
$env:DEV_AUTH = "true"   # local dev: bypass login with an admin principal
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Health check: <http://127.0.0.1:8000/healthz>

### Frontend

```pwsh
npm --prefix frontend install
npm --prefix frontend run dev
```

App: <http://localhost:5173>

### Full stack via Docker Compose

```pwsh
Copy-Item .env.example .env   # then edit .env (set LLM_API_KEY, etc.)
docker compose up --build
```

## Before You Submit

Please make sure these pass locally:

```pwsh
# Backend tests
cd backend; .\.venv\Scripts\python.exe -m pytest tests/ -q

# Backend lint (if configured)
.\.venv\Scripts\python.exe -m ruff check app

# Frontend type-check (must be clean — the project treats TS errors as failures)
cd ..\frontend; npx tsc -p tsconfig.json --noEmit
```

> Note: the frontend build flags unused variables/imports (TS6133). Remove dead
> code rather than suppressing it.

## Making Changes

1. **Fork** the repo and create a feature branch:
   `git checkout -b feature/short-description`
2. Keep changes focused. Match the existing code style and patterns.
3. Add or update tests for behavior changes.
4. Do **not** commit secrets. The `.gitignore` excludes `.env`, `backend/.data/`,
   keys, and local artifacts — keep it that way.
5. Update documentation when you change user-facing behavior.

## Commit & PR Guidelines

- Write clear, imperative commit messages (e.g., "Add backup coverage filter").
- Open a Pull Request against `main` with:
  - A description of **what** changed and **why**.
  - Steps to verify, and confirmation that tests + type-check pass.
  - Linked issue(s), if any.
- Keep PRs reasonably small and reviewable.

## Reporting Bugs & Requesting Features

Use the **Issues** tab. Include your deployment mode (local, Docker Compose, or
Azure), the version/commit, reproduction steps, and relevant logs (secrets
redacted).

## Security Issues

Do **not** file security vulnerabilities as public issues — see
[SECURITY.md](SECURITY.md) for private reporting.

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE).
