FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Node.js (for `npx @azure/mcp`) and the Azure CLI (for local DefaultAzureCredential
# via mounted ~/.azure). Kept in one layer to slim the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
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

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
