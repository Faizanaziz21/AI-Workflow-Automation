from __future__ import annotations

from pydantic import BaseModel, Field

from app.connectors.builtin.postgres.schemas import NewRowsConfig, QueryInput, QueryOutput


class MySqlConfig(BaseModel):
    host: str
    port: int = Field(default=3306, ge=1, le=65535)
    database: str
    use_ssl: bool = False
    query_timeout_seconds: int = Field(default=30, ge=1, le=600)
    read_only: bool = False


class MySqlCredentials(BaseModel):
    username: str
    password: str


__all__ = ["MySqlConfig", "MySqlCredentials", "NewRowsConfig", "QueryInput", "QueryOutput"]
