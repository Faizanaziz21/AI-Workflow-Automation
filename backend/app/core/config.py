"""Application configuration loaded from environment variables (12-factor)."""

from __future__ import annotations

import base64
import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FF_", env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    app_name: str = "FlowForge AI"
    log_level: str = "INFO"
    log_json: bool = True

    # Database / cache
    database_url: str = "postgresql+asyncpg://flowforge:flowforge@localhost:5432/flowforge"
    db_pool_size: int = 30
    db_max_overflow: int = 20
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    jwt_secret: str = Field(default="dev-insecure-jwt-secret-change-me-0123456789", min_length=32)
    jwt_issuer: str = "flowforge"
    jwt_audience: str = "flowforge-api"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 14

    # Envelope encryption: JSON map of key-version -> base64 32-byte key, plus active version.
    # Generate with: python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
    encryption_keys: str = Field(
        default='{"v1": "ZGV2LW9ubHktZW5jcnlwdGlvbi1rZXktMzJieXRlcyE="}',
        description="JSON object mapping KEK version to base64 key",
    )
    active_encryption_key: str = "v1"

    # HTTP
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    public_base_url: str = "http://localhost:8000"
    trusted_proxies: int = 1

    # Rate limits (requests per minute)
    rate_limit_default: int = 600
    rate_limit_auth: int = 20
    rate_limit_webhook: int = 3000

    # Engine
    worker_concurrency: int = 32
    job_lease_seconds: int = 60
    job_poll_interval_seconds: float = 1.0
    max_node_output_bytes: int = 1_000_000
    default_node_timeout_seconds: int = 300
    default_execution_timeout_seconds: int = 60 * 60 * 24 * 30
    webhook_sync_timeout_seconds: int = 30
    webhook_signature_tolerance_seconds: int = 300

    # Egress (SSRF) policy for HTTP-calling nodes
    allow_private_network_egress: bool = False
    egress_allowlist: Annotated[list[str], NoDecode] = []

    # Storage
    storage_dir: str = "./storage"
    max_upload_bytes: int = 25 * 1024 * 1024

    # Observability
    otel_exporter_endpoint: str | None = None
    otel_service_name: str = "flowforge"
    metrics_enabled: bool = True

    # Retention (days; 0 = keep forever). Organizations can override both in their settings.
    execution_retention_days: int = Field(default=90, ge=0)
    audit_retention_days: int = Field(default=365, ge=0)

    # Bootstrap
    seed_templates: bool = True

    @field_validator("cors_origins", "egress_allowlist", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            if v.strip().startswith("["):
                return json.loads(v)
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    def kek_map(self) -> dict[str, bytes]:
        raw = json.loads(self.encryption_keys)
        keys = {k: base64.b64decode(v) for k, v in raw.items()}
        for version, key in keys.items():
            if len(key) != 32:
                raise ValueError(f"Encryption key {version} must be 32 bytes (got {len(key)})")
        if self.active_encryption_key not in keys:
            raise ValueError("FF_ACTIVE_ENCRYPTION_KEY not present in FF_ENCRYPTION_KEYS")
        return keys

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def assert_production_safe(self) -> None:
        """Refuse to boot production with development secrets."""
        if not self.is_production:
            return
        if "dev-insecure" in self.jwt_secret:
            raise RuntimeError("FF_JWT_SECRET must be set in production")
        if "ZGV2LW9ubHkt" in self.encryption_keys:
            raise RuntimeError("FF_ENCRYPTION_KEYS must be set in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
