# syntax=docker/dockerfile:1
# Single-container image: builds the React SPA and serves it from the FastAPI backend
# (API under /api, SPA + assets at every other path). Targets Azure Container Apps.

# ---- Stage 1: build the React SPA --------------------------------------------------
FROM node:20-alpine AS frontend
WORKDIR /web
COPY frontend/package.json ./
RUN npm install
COPY frontend/ ./
# Same-origin API base so the bundle calls /api/... on whatever host serves it.
ENV VITE_API_BASE=/api
# Release/image version shown in the top header. Pass --build-arg APP_VERSION=vNN at build
# time (the image tag); defaults to "dev" for an untagged build.
ARG APP_VERSION=dev
ENV VITE_APP_VERSION=$APP_VERSION
RUN npm run build

# ---- Stage 2: backend + bundled SPA ------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Node.js (for `npx @azure/mcp`), the Azure CLI (DefaultAzureCredential), Xvfb (a
# virtual display so the OAuth sign-in flows can launch a headful Chromium in this
# server container without a real screen), and the networking CLIs the built-in utility
# tools shell out to (ping, traceroute, dig, etc.). One layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg xvfb xauth \
        iputils-ping traceroute dnsutils netcat-openbsd iproute2 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -sL https://aka.ms/InstallAzureCLIDeb | bash \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Resource discovery uses `az graph query`, which needs the resource-graph CLI extension.
# Each query runs in a throwaway AZURE_CONFIG_DIR (fresh SP login), so relying on runtime
# auto-install would re-download the extension every time (slow / unreliable in a locked
# container) and silently yield zero resources. Bake it into a FIXED extension dir that
# every config dir resolves via AZURE_EXTENSION_DIR.
ENV AZURE_EXTENSION_DIR=/opt/az-extensions
RUN az extension add --name resource-graph --only-show-errors

WORKDIR /app

# Backend source (app package, alembic, alembic.ini, pyproject) must be present before
# the install — setuptools packages=["app"] validates the package dir during build.
COPY backend/ ./
# Install the exact dependency set captured from the working dev venv (requirements.txt),
# then the app package itself without re-resolving deps. This guarantees every runtime
# import (argon2, lxml, signxml, PyJWT, …) is present.
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install --no-deps .

# Playwright drives a headless Chromium for the GitHub Copilot / ChatGPT (Codex) OAuth
# sign-in flows. Install the browser + its OS libraries into the image so those flows
# don't fail with "Executable doesn't exist … please run playwright install".
RUN python -m playwright install --with-deps chromium

# Bundled SPA goes into the package's static dir, which main.py serves.
COPY --from=frontend /web/dist ./app/static

# EntraID (Microsoft Graph) MCP server: vendored under third_party, run from a dedicated
# venv so its msgraph-sdk dependency tree stays isolated from the backend's.
COPY third_party/ /app/third_party/
RUN python -m venv /opt/eidmcp \
    && /opt/eidmcp/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/eidmcp/bin/pip install --no-cache-dir \
        azure-core azure-identity "mcp[cli]" msgraph-core msgraph-sdk fastmcp python-dotenv

EXPOSE 8000

# Run DB migrations then start the API + SPA. Wrap in xvfb-run so the OAuth sign-in
# flows (GitHub Copilot / ChatGPT Codex) can launch a headful Chromium against a
# virtual display instead of failing with "Missing X server".
CMD ["sh", "-c", "alembic upgrade head && xvfb-run -a --server-args='-screen 0 1280x1024x24' uvicorn app.main:app --host 0.0.0.0 --port 8000"]
