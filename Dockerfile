# syntax=docker/dockerfile:1
# Single-container image: builds the React SPA and serves it from the FastAPI backend
# (API under /api, SPA + assets at every other path). Targets Azure Container Apps.

# ---- Stage 1: build the React SPA --------------------------------------------------
# Base images are pinned BY DIGEST so a build is reproducible and a compromised or
# silently-retagged upstream cannot slip in. Pinning alone would freeze out upstream
# SECURITY patches, so it is paired with the `docker` ecosystem in .github/dependabot.yml,
# which opens a PR when a new digest is published. Do not un-pin; bump via that PR.
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS frontend
WORKDIR /web
# Install exactly the audited lockfile. Copying both manifests into this layer also ensures
# dependency-only security updates invalidate Docker's npm cache before the SPA is bundled.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Same-origin API base so the bundle calls /api/... on whatever host serves it.
ENV VITE_API_BASE=/api
# Release/image version shown in the top header. Pass --build-arg APP_VERSION=vNN at build
# time (the image tag); defaults to "dev" for an untagged build.
ARG APP_VERSION=dev
ENV VITE_APP_VERSION=$APP_VERSION
# Sequential release number (e.g. git commit count) shown as "v1 (rel 1234)". Pass
# --build-arg APP_RELEASE=$(Get-Content RELEASE); empty by default (local/dev).
ARG APP_RELEASE=
ENV VITE_APP_RELEASE=$APP_RELEASE
RUN npm run build

# ---- Stage 2: rebuild Azure Quick Review with a patched Go toolchain ----------------
# azqr v4.0.1's official archive was built with Go 1.26.0. Its embedded standard library
# has multiple fixed CVEs (including CVE-2026-56862). The source itself remains pinned to
# the signed release commit; only the compiler is raised to the first release containing
# every listed fix. This avoids waiting for another azqr release while preserving behavior.
FROM golang:1.26.6-bookworm@sha256:116d58cbd88c1297624acc6e967a060012422bacf9930927e23fb719189c6f36 AS azqr
ARG AZQR_VERSION=4.0.1
ARG AZQR_SOURCE_COMMIT=ffda262cbccc33bf4f472c07f81758839b165b1a
ARG AZQR_SOURCE_SHA256=afef4ba8c09945668145d0a035da87922ec26ba1461077d2c1bf418a12e8321f
ARG AZQR_APRL_COMMIT=60eaddda76541f6adbc1c5ffa686829807e55e29
ARG AZQR_APRL_SHA256=9f5125e2992649057328c0fb8e7430d5eac0db574d07316b4876236a66a10deb
# The release source pins x/crypto v0.54.0, which contains CVE-2026-56854.
# Override only that module to its first fixed release before compiling.
ARG GO_X_CRYPTO_VERSION=v0.55.0
WORKDIR /src
RUN curl -fsSLo /tmp/azqr.tar.gz \
        "https://github.com/Azure/azqr/archive/${AZQR_SOURCE_COMMIT}.tar.gz" \
    && echo "${AZQR_SOURCE_SHA256}  /tmp/azqr.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/azqr.tar.gz --strip-components=1 \
    && curl -fsSLo /tmp/aprl.tar.gz \
        "https://github.com/Azure/Azure-Proactive-Resiliency-Library-v2/archive/${AZQR_APRL_COMMIT}.tar.gz" \
    && echo "${AZQR_APRL_SHA256}  /tmp/aprl.tar.gz" | sha256sum -c - \
    && mkdir -p internal/graph/aprl \
    && tar -xzf /tmp/aprl.tar.gz -C internal/graph/aprl --strip-components=1 \
    && go get "golang.org/x/crypto@${GO_X_CRYPTO_VERSION}" \
    && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath \
        -ldflags "-s -w -X github.com/Azure/azqr/cmd/azqr/commands.version=${AZQR_VERSION}" \
        -o /usr/local/bin/azqr ./cmd/azqr/main.go \
    && /usr/local/bin/azqr --version \
    && rm /tmp/azqr.tar.gz /tmp/aprl.tar.gz

# ---- Stage 3: backend + bundled SPA ------------------------------------------------
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Release/image version, exposed to the backend at runtime (the frontend gets it via the
# builder stage's VITE_APP_VERSION). Shown in Help → About and reported by /api/meta.
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
# Sequential release number, also exposed to the backend for /api/meta parity.
ARG APP_RELEASE=
ENV APP_RELEASE=$APP_RELEASE

# Node.js (to run the Azure MCP server), the Azure CLI (DefaultAzureCredential), and the
# networking CLIs the built-in utility tools shell out to (ping, traceroute, dig, etc.).
# One layer.
# `apt-get upgrade` pulls in the latest Debian security patches for the base image so a
# rebuild closes known OS-package CVEs instead of inheriting whatever was current when
# the base tag was published.
#
# npm is PINNED rather than `@latest`. An unpinned global upgrade makes the image
# non-reproducible — two builds of the same commit can ship different npm trees — and npm
# vendors its own dependency copies under /usr/lib/node_modules/npm/node_modules, so those
# bundled packages are what image CVE scans report. Pinning makes that surface a deliberate,
# reviewable choice; bump it on purpose after checking the scan.
ARG NPM_VERSION=12.0.2
# The Azure MCP server is INSTALLED AT BUILD TIME instead of being fetched by `npx` on every
# cold start. Fetching it at runtime meant the container reached out to the public npm
# registry from production — a supply-chain and availability dependency on a third party,
# and the main path that exercised npm's own HTTP stack at runtime.
#
# Pinned to the exact version `@latest` resolved to when this was introduced, so this change
# is only about WHEN the package is fetched, not WHICH version runs. NOTE: the package's
# `latest` dist-tag currently points at a PRERELEASE (there is no stable tag), which is worth
# a deliberate decision rather than inheriting it silently — see MCP_COMMAND below.
ARG AZURE_MCP_VERSION=3.0.0-beta.31
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
        libicu76 \
        iputils-ping traceroute dnsutils netcat-openbsd iproute2 \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -sL https://aka.ms/InstallAzureCLIDeb | bash \
    && /opt/az/bin/python3 -m pip install --no-cache-dir \
        "cryptography>=50.0.0" "setuptools>=83.0.0" \
    && npm install -g "npm@${NPM_VERSION}" \
    && npm install -g "@azure/mcp@${AZURE_MCP_VERSION}" \
    && npm cache clean --force \
    # npm is a BUILD-TIME tool here, not a runtime one: the only runtime consumer was
    # `npx @azure/mcp`, which the pre-install above replaced with a real binary. Removing it
    # deletes the whole /usr/lib/node_modules/npm/node_modules vendored tree, which is where
    # every open image CVE in this project lives (undici, ip-address, brace-expansion). Those
    # are bundled INSIDE npm, so they cannot be patched downstream — even the newest npm still
    # ships the vulnerable copies. Deleting the surface is the only fix available to us.
    #
    # It also removes the ability to install anything from the public npm registry at runtime,
    # which is a deliberate hardening rather than a side effect.
    #
    # TRADE-OFF: `npx` no longer exists in this image. The application's default
    # `MCP_COMMAND=npx` (still correct for local development) is overridden to `azmcp` below;
    # a deployment that forces MCP_COMMAND back to `npx` would not find it. Node itself stays,
    # because `azmcp` needs it.
    && rm -rf /usr/lib/node_modules/npm /usr/bin/npm /usr/bin/npx \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY --from=azqr /usr/local/bin/azqr /usr/local/bin/azqr
RUN azqr --version

# Run the pre-installed server binary directly. `npx -y @azure/mcp@latest` (the application
# default, which stays correct for local development) would resolve and possibly download the
# package on every cold start; `azmcp` is the bin the package installs and is already on PATH.
ENV MCP_COMMAND=azmcp \
    MCP_ARGS="server start --transport stdio"

# Resource discovery uses `az graph query`, which needs the resource-graph CLI extension.
# Each query runs in a throwaway AZURE_CONFIG_DIR (fresh SP login), so relying on runtime
# auto-install would re-download the extension every time (slow / unreliable in a locked
# container) and silently yield zero resources. Bake it into a FIXED extension dir that
# every config dir resolves via AZURE_EXTENSION_DIR.
ENV AZURE_EXTENSION_DIR=/opt/az-extensions
RUN az extension add --name resource-graph --only-show-errors \
    # pip is required to install the extension, but neither az nor the installed
    # extension needs it at runtime. Its vendored msgpack/setuptools copies carry
    # fixable CVEs that cannot be upgraded independently, so remove the build tool.
    && rm -rf /opt/az/lib/python*/site-packages/pip \
        /opt/az/lib/python*/site-packages/pip-*.dist-info \
        /opt/az/bin/pip /opt/az/bin/pip3

WORKDIR /app

# Backend source (app package, alembic, alembic.ini, pyproject) must be present before
# the install — setuptools packages=["app"] validates the package dir during build.
COPY backend/ ./
# Install the exact dependency set captured from the working dev venv (requirements.txt),
# then the app package itself without re-resolving deps. This guarantees every runtime
# import (argon2, lxml, signxml, PyJWT, …) is present.
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install --no-deps . \
    # Runtime application processes never install packages. Removing pip also
    # removes its private vulnerable vendor tree and reduces post-exploit tooling.
    && rm -rf /usr/local/lib/python*/site-packages/pip \
        /usr/local/lib/python*/site-packages/pip-*.dist-info \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

# Bundled SPA goes into the package's static dir, which main.py serves.
COPY --from=frontend /web/dist ./app/static

# EntraID (Microsoft Graph) MCP server: vendored under third_party, run from a dedicated
# venv so its msgraph-sdk dependency tree stays isolated from the backend's.
COPY third_party/ /app/third_party/
RUN python -m venv /opt/eidmcp \
    && /opt/eidmcp/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/eidmcp/bin/pip install --no-cache-dir \
        "cryptography>=50.0.0" azure-core azure-identity "mcp[cli]>=1.28.1,<2" msgraph-core msgraph-sdk fastmcp python-dotenv \
    && rm -rf /opt/eidmcp/lib/python*/site-packages/pip \
        /opt/eidmcp/lib/python*/site-packages/pip-*.dist-info \
        /opt/eidmcp/bin/pip /opt/eidmcp/bin/pip3 /opt/eidmcp/bin/pip3.*

EXPOSE 8000

# ---- Drop root -------------------------------------------------------------------
# Everything above needs root (apt, pip, npm -g, az extension install). Everything at
# RUNTIME does not, and the agent can execute `az` and spawn MCP subprocesses, so any
# code-execution bug would otherwise be root-in-container.
#
# Safe for the persistent volume: ACA mounts the Azure Files share at /app/.data with
# mode 0777 (verified 2026-07-31 on the live revision: `drwxrwxrwx 2 0 0 /app/.data`),
# so a non-root uid can still read/write it. The app writes secret.key + JSON registries
# there and creates them 0600 under its own uid.
#
# HOME must exist and be writable: the Azure CLI writes throwaway AZURE_CONFIG_DIRs and
# npm/npx (for `npx @azure/mcp`) needs a cache dir.
RUN groupadd --system --gid 1000 azsup \
    && useradd --system --uid 1000 --gid 1000 --home-dir /home/azsup --shell /usr/sbin/nologin azsup \
    && mkdir -p /home/azsup /app/.data \
    && chown -R azsup:azsup /app /home/azsup \
    && chmod 0755 /home/azsup
ENV HOME=/home/azsup \
    NPM_CONFIG_CACHE=/home/azsup/.npm
USER azsup

# Run DB migrations (serialized by a PostgreSQL advisory lock) then start exactly ONE uvicorn
# worker. Background work is process-owned and coordinated across replicas by database leases;
# extra workers inside a replica would create hidden schedulers and duplicate connection pools.
# All AI-provider sign-in flows are headless, so no virtual display / browser is needed.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
