# Digest-pinned; bumped by the `docker` ecosystem in .github/dependabot.yml.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Node.js (for `npx @azure/mcp`) and the Azure CLI (for local DefaultAzureCredential
# via mounted ~/.azure). Kept in one layer to slim the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg libicu76 \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -sL https://aka.ms/InstallAzureCLIDeb | bash \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the project metadata and package source needed to build/install the
# wheel (pyproject declares packages = ["app"]), then install. Done before the
# full COPY so the dependency layer is cached unless these inputs change.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --upgrade pip && pip install .

COPY . .

EXPOSE 8000

# Drop root. Everything above needs it (apt, pip); nothing at runtime does.
# HOME must be writable: the Azure CLI and npx write caches there, and docker-compose
# mounts the developer's ~/.azure to /home/azsup/.azure (NOT /root/.azure - that path
# moved with this change; keep compose in sync).
RUN groupadd --system --gid 1000 azsup \
    && useradd --system --uid 1000 --gid 1000 --home-dir /home/azsup --shell /usr/sbin/nologin azsup \
    && mkdir -p /home/azsup \
    && chown -R azsup:azsup /app /home/azsup
ENV HOME=/home/azsup \
    NPM_CONFIG_CACHE=/home/azsup/.npm
USER azsup

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
