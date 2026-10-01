from __future__ import annotations

import asyncpg

from app.connectors.builtin.postgres.client import get_pool
from app.connectors.builtin.postgres.schemas import (
    NewRowsConfig,
    PostgresConfig,
    PostgresCredentials,
    QueryInput,
    QueryOutput,
)
from app.connectors.sdk import (
    AuthSpec,
    AuthType,
    Connector,
    ConnectorContext,
    ConnectorError,
    PollResult,
    TestResult,
    action,
    trigger,
)
from app.connectors.sqlutil import looks_like_write, to_jsonable, to_positional, validate_identifier

_RETRYABLE_SQLSTATES = {"40001", "40P01", "55P03", "57014", "53300", "08006", "08001", "08004"}


class PostgresConnector(Connector):
    key = "postgres"
    name = "PostgreSQL"
    description = "Run parameterised SQL against PostgreSQL and trigger on new/changed rows."
    category = "data"
    icon = "database"
    auth = AuthSpec(AuthType.BASIC, credentials_model=PostgresCredentials, config_model=PostgresConfig)

    @action(
        "query",
        "Execute query",
        input=QueryInput,
        output=QueryOutput,
        description="Execute SQL with bound :named parameters. Results are returned as rows.",
    )
    async def query(self, ctx: ConnectorContext, data: QueryInput) -> QueryOutput:
        cfg: PostgresConfig = ctx.config
        if cfg.read_only and looks_like_write(data.sql):
            raise ConnectorError("This connection is read-only; write statements are not allowed")
        try:
            sql, args = to_positional(data.sql, data.params)
        except ValueError as exc:
            raise ConnectorError(str(exc)) from exc
        pool = await get_pool(cfg, ctx.credentials)
        try:
            async with pool.acquire() as conn:
                async with conn.transaction(readonly=cfg.read_only):
                    stmt = await conn.prepare(sql)
                    if stmt.get_attributes():
                        records = await stmt.fetch(*args)
                        status = None
                    else:
                        status = await conn.execute(sql, *args)
                        records = []
        except asyncpg.PostgresError as exc:
            code = getattr(exc, "sqlstate", None)
            raise ConnectorError(f"PostgreSQL error {code}: {exc}", retryable=code in _RETRYABLE_SQLSTATES) from exc
        truncated = len(records) > data.max_rows
        rows = [to_jsonable(dict(r)) for r in records[: data.max_rows]]
        columns = list(records[0].keys()) if records else []
        count = len(records)
        if status and status.split()[-1].isdigit():
            count = int(status.split()[-1])
        return QueryOutput(rows=rows, row_count=count, columns=columns, truncated=truncated, status=status)

    @trigger(
        "new_rows",
        "New or updated rows",
        config=NewRowsConfig,
        interval=30,
        description="Poll a table for rows whose cursor column advanced (database change trigger).",
    )
    async def new_rows(self, ctx: ConnectorContext, cfg: NewRowsConfig, state: dict) -> PollResult:
        table = validate_identifier(cfg.table)
        column = validate_identifier(cfg.cursor_column)
        pool = await get_pool(ctx.config, ctx.credentials)
        async with pool.acquire() as conn:
            if "cursor" not in state:
                if cfg.start_from is not None:
                    state["cursor"] = cfg.start_from
                else:
                    current = await conn.fetchval(f"SELECT max({column}) FROM {table}")
                    return PollResult(items=[], state={"cursor": to_jsonable(current)})
            cursor = state["cursor"]
            if cursor is None:
                records = await conn.fetch(
                    f"SELECT * FROM {table} ORDER BY {column} LIMIT $1",
                    cfg.batch_size,
                )
            else:
                col_type = await conn.fetchval(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = $1::regclass AND attname = $2",
                    table,
                    column.split(".")[-1],
                )
                records = await conn.fetch(
                    f"SELECT * FROM {table} WHERE {column} > $1::text::{col_type or 'text'} ORDER BY {column} LIMIT $2",
                    str(cursor),
                    cfg.batch_size,
                )
        items = [to_jsonable(dict(r)) for r in records]
        if items:
            state["cursor"] = items[-1][column.split(".")[-1]]
        return PollResult(items=items, state=state)

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        try:
            pool = await get_pool(ctx.config, ctx.credentials)
            async with pool.acquire() as conn:
                version = await conn.fetchval("SHOW server_version")
        except (ConnectorError, asyncpg.PostgresError, OSError) as exc:
            return TestResult(False, str(exc))
        return TestResult(True, f"Connected to PostgreSQL {version}", {"server_version": version})
