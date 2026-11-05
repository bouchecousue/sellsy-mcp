from __future__ import annotations

import re
from pathlib import Path

from pydantic import AliasChoices, Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SELLSY_MCP_",
        extra="ignore",
        populate_by_name=True,
    )

    host: str = "127.0.0.1"
    port: int = Field(default=8766, ge=1024, le=65535)
    public_base_url: HttpUrl = "http://127.0.0.1:8766"
    database_path: Path = Path("var/sellsy-mcp.sqlite3")
    encryption_key_file: Path = Path("var/fernet.key")
    audit_log_path: Path = Path("var/audit.jsonl")

    sellsy_client_id: str = Field(
        validation_alias=AliasChoices("SELLSY_MCP_SELLSY_CLIENT_ID", "SELLSY_CLIENT_ID")
    )
    sellsy_client_secret: str = Field(
        validation_alias=AliasChoices("SELLSY_MCP_SELLSY_CLIENT_SECRET", "SELLSY_CLIENT_SECRET")
    )
    sellsy_api_base_url: HttpUrl = Field(
        default="https://api.sellsy.com/v2",
        validation_alias=AliasChoices("SELLSY_MCP_SELLSY_API_BASE_URL", "SELLSY_API_BASE_URL"),
    )
    sellsy_authorize_url: HttpUrl = Field(
        default="https://login.sellsy.com/oauth2/authorization",
        validation_alias=AliasChoices("SELLSY_MCP_SELLSY_AUTHORIZE_URL", "SELLSY_OAUTH_AUTHORIZE_URL"),
    )
    sellsy_token_url: HttpUrl = Field(
        default="https://login.sellsy.com/oauth2/access-tokens",
        validation_alias=AliasChoices("SELLSY_MCP_SELLSY_TOKEN_URL", "SELLSY_OAUTH_TOKEN_URL"),
    )
    sellsy_scopes: str = (
        "estimates.read estimates.write companies.read contacts.read items.read subscriptions.read "
        "staffs.read opportunities.read opportunities.write invoices.read comments.read files.read "
        "payments.read search.read credit-notes.read taxes.read orders.read deliveries.read "
        "custom-fields.read smart-tags.read"
    )
    allowed_client_redirect_hosts: str = (
        "chatgpt.com chat.openai.com connectors.api.openai.com localhost 127.0.0.1"
    )

    manifest_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    mcp_access_token_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    mcp_refresh_token_ttl_seconds: int = Field(default=2592000, ge=3600, le=7776000)
    search_page_size: int = Field(default=100, ge=10, le=100)
    search_max_pages: int = Field(default=50, ge=1, le=100)

    search_timeout_seconds: int = Field(default=90, ge=5, le=300)
    request_timeout_seconds: int = Field(default=30, ge=1, le=60)
    read_attempts: int = Field(default=3, ge=1, le=5)
    read_queue_limit: int = Field(default=64, ge=1, le=256)
    request_concurrency: int = Field(default=4, ge=1, le=10)
    maintenance_interval_seconds: int = Field(default=3600, ge=60)
    enable_writes: bool = False
    version: str = "0.5.0b2"
    file_download_hosts: list[str] = Field(default_factory=list)

    @field_validator("public_base_url")
    @classmethod
    def require_https_outside_localhost(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https" and value.host not in {"localhost", "127.0.0.1"}:
            raise ValueError("public_base_url must use HTTPS outside localhost")
        return value

    @field_validator("sellsy_client_id")
    @classmethod
    def require_plain_uuid_client_id(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value, re.I):
            raise ValueError("sellsy_client_id must be the plain UUID shown by Sellsy")
        return value

    @property
    def public_base(self) -> str:
        return str(self.public_base_url).rstrip("/")

    @property
    def resource_url(self) -> str:
        return f"{self.public_base}/mcp"

    @property
    def callback_url(self) -> str:
        return f"{self.public_base}/oauth/callback"

    @property
    def scope_list(self) -> list[str]:
        return [scope for scope in self.sellsy_scopes.split() if scope]

    @property
    def redirect_host_list(self) -> list[str]:
        return [host.casefold().rstrip(".") for host in self.allowed_client_redirect_hosts.split() if host]

    def prepare_paths(self) -> None:
        for path in (self.database_path, self.encryption_key_file, self.audit_log_path):
            path.parent.mkdir(parents=True, exist_ok=True)
