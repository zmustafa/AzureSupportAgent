"""Application configuration loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve the repo-root .env by absolute path so running from backend/ (alembic,
# uvicorn) or the repo root both work. Real environment variables still take
# precedence (used by Docker Compose).
_REPO_ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"
_BACKEND_ENV = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT_ENV), str(_BACKEND_ENV)),
        extra="ignore",
    )

    environment: str = "local"
    log_level: str = "INFO"

    # Auth. When dev_auth is true, a fake admin identity is injected (no login). With
    # real authentication enabled (default), users log in (local password or SSO).
    dev_auth: bool = False
    dev_auth_role: str = "admin"
    dev_auth_tenant: str = "default"
    dev_auth_email: str = "dev@example.com"
    # Seeded bootstrap admin (created on first run only). Override the password in any
    # real deployment via SEED_ADMIN_PASSWORD.
    seed_admin_username: str = "admin"
    seed_admin_password: str = "admin"
    # Session cookie security. In production set cookie_secure=true and serve over HTTPS;
    # for cross-site (different domains) deployments set cookie_samesite="none".
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    # Public base URL of this API (used to build OIDC/SAML redirect URIs).
    public_base_url: str = "http://localhost:8000"
    # Trusted reverse-proxy IPs (comma-separated). When set, the app honors the
    # ``X-Forwarded-For`` header only for requests whose direct client IP is in this
    # allowlist; otherwise it falls back to ``request.client.host`` to prevent IP
    # spoofing in audit logs and brute-force counters. Leave empty (default) to
    # trust no proxy header at all — the safest default for direct/local deployments.
    trusted_proxies: str = ""

    # LLM
    llm_provider: str = "openai"
    llm_model: str = "gpt-4.1"
    llm_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_deployment: str = ""

    # Datastores
    database_url: str = "postgresql+asyncpg://azsup:azsup@localhost:5432/azsup"
    redis_url: str = "redis://localhost:6379/0"

    # MCP (Azure MCP server spawned over stdio via npx)
    mcp_command: str = "npx"
    mcp_args: str = "-y @azure/mcp@latest server start --transport stdio"
    mcp_read_only: bool = False
    azure_subscription_id: str = ""

    # EntraID (Microsoft Graph) MCP server — a vendored Python FastMCP server spawned
    # over stdio. Its heavy msgraph-sdk deps live in a dedicated venv (long-path safe),
    # so the command points at that venv's python and the args at the stdio launcher.
    entra_mcp_command: str = r"C:\eidmcp\Scripts\python.exe"
    entra_mcp_args: str = str(
        Path(__file__).resolve().parents[3] / "third_party" / "entraid-mcp-server" / "run_server.py"
    )
    # Which credential DefaultAzureCredential should use. On dev machines the default
    # chain can resolve a broker/VS identity with no subscriptions; pinning to the
    # Azure CLI credential matches your `az login`. Set empty to use the full chain.
    azure_token_credentials: str = "AzureCliCredential"

    # Agent safety
    agent_write_policy: str = "gated"  # gated | read_only

    # Azure Activity Log pagination ceiling (Change Explorer / change collectors). The REST
    # reader pages through `nextLink` up to this many pages as a safety bound; raise it to
    # read deeper on very large tenants (at the cost of slower scans), lower it to cap latency.
    azure_activity_log_max_pages: int = 50

    # Secrets at rest: Fernet key for encrypting Azure connection credentials. Leave
    # empty for local dev (a key is generated under backend/.data/secret.key). In
    # production, mount a stable key from Key Vault / a secret store.
    secrets_encryption_key: str = ""

    # CORS
    frontend_origin: str = "http://localhost:5173"

    @property
    def resolved_database_url(self) -> str:
        """For SQLite, anchor a relative path to the backend dir so the same DB is
        used no matter which directory the process is launched from."""
        url = self.database_url
        prefix = "sqlite+aiosqlite:///"
        if url.startswith(prefix):
            raw = url[len(prefix):]
            # Only treat as relative if it isn't an absolute path or drive-letter path.
            is_absolute = raw.startswith("/") or (len(raw) > 1 and raw[1] == ":")
            if not is_absolute:
                if raw.startswith("./"):
                    raw = raw[2:]
                backend_dir = Path(__file__).resolve().parents[2]
                abs_path = (backend_dir / raw).resolve()
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                return prefix + abs_path.as_posix()
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
