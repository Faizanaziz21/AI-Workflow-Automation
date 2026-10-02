from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PostgresConfig(BaseModel):
    host: str
    port: int = Field(default=5432, ge=1, le=65535)
    database: str
    ssl_mode: Literal["disable", "prefer", "require", "verify-full"] = "prefer"
    statement_timeout_ms: int = Field(default=30000, ge=100, le=600000)
    read_only: bool = Field(default=False, description="Reject write statements and run in READ ONLY transactions")


class PostgresCredentials(BaseModel):
    username: str
    password: str


class QueryInput(BaseModel):
    sql: str = Field(min_length=1, max_length=100_000, description="SQL with :named parameters")
    params: dict[str, Any] = Field(default_factory=dict, description="Values bound to :named parameters")
    max_rows: int = Field(default=1000, ge=1, le=100_000)


class QueryOutput(BaseModel):
    rows: list[dict[str, Any]]
    row_count: int
    columns: list[str]
    truncated: bool
    status: str | None = None


class NewRowsConfig(BaseModel):
    table: str = Field(description="Table (optionally schema-qualified) to watch")
    cursor_column: str = Field(description="Monotonic column such as id or updated_at")
    batch_size: int = Field(default=100, ge=1, le=1000)
    start_from: str | None = Field(default=None, description="Initial cursor value; default = only new rows")
