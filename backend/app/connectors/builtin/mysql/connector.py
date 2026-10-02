from __future__ import annotations

import asyncio

import aiomysql

from app.connectors.builtin.mysql.client import connect
from app.connectors.builtin.mysql.schemas import MySqlConfig, MySqlCredentials, NewRowsConfig, QueryInput, QueryOutput
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
from app.connectors.sqlutil import looks_like_write, to_jsonable, to_pyformat, validate_identifier

_RETRYABLE_ERRNOS = {1205, 1213, 2006, 2013, 1040}


class MySqlConnector(Connector):
    key = "mysql"
    name = "MySQL"
    description = "Run parameterised SQL against MySQL/MariaDB and trigger on new rows."
    category = "data"
    icon = "database"
    auth = AuthSpec(AuthType.BASIC, credentials_model=MySqlCredentials, config_model=MySqlConfig)

    @action("query", "Execute query", input=QueryInput, output=QueryOutput)
    async def query(self, ctx: ConnectorContext, data: QueryInput) -> QueryOutput:
        cfg: MySqlConfig = ctx.config
        if cfg.read_only and looks_like_write(data.sql):
            raise ConnectorError("This connection is read-only; write statements are not allowed")
        try:
            sql = to_pyformat(data.sql, data.params)
        except ValueError as exc:
            raise ConnectorError(str(exc)) from exc
        async with connect(cfg, ctx.credentials) as conn:
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    if cfg.read_only:
                        await cur.execute("START TRANSACTION READ ONLY")
                    await asyncio.wait_for(cur.execute(sql, data.params or None), timeout=cfg.query_timeout_seconds)
                    records = await cur.fetchmany(data.max_rows + 1) if cur.description else []
                    columns = [d[0] for d in cur.description] if cur.description else []
                    affected = cur.rowcount
                await conn.commit()
            except aiomysql.Error as exc:
                await conn.rollback()
                errno = exc.args[0] if exc.args else None
                raise ConnectorError(f"MySQL error {errno}: {exc}", retryable=errno in _RETRYABLE_ERRNOS) from exc
            except TimeoutError as exc:
                raise ConnectorError("MySQL query timed out", retryable=True) from exc
        truncated = len(records) > data.max_rows
        rows = [to_jsonable(dict(r)) for r in records[: data.max_rows]]
        return QueryOutput(
            rows=rows, row_count=len(rows) if columns else max(affected, 0), columns=columns, truncated=truncated
        )

    @trigger("new_rows", "New rows", config=NewRowsConfig, interval=30)
    async def new_rows(self, ctx: ConnectorContext, cfg: NewRowsConfig, state: dict) -> PollResult:
        table = validate_identifier(cfg.table)
        column = validate_identifier(cfg.cursor_column)
        async with connect(ctx.config, ctx.credentials) as conn, conn.cursor(aiomysql.DictCursor) as cur:
            if "cursor" not in state:
                if cfg.start_from is None:
                    await cur.execute(f"SELECT MAX({column}) AS c FROM {table}")
                    row = await cur.fetchone()
                    return PollResult(items=[], state={"cursor": to_jsonable(row["c"]) if row else None})
                state["cursor"] = cfg.start_from
            if state["cursor"] is None:
                await cur.execute(f"SELECT * FROM {table} ORDER BY {column} LIMIT %s", (cfg.batch_size,))
            else:
                await cur.execute(
                    f"SELECT * FROM {table} WHERE {column} > %s ORDER BY {column} LIMIT %s",
                    (state["cursor"], cfg.batch_size),
                )
            records = await cur.fetchall()
            await conn.commit()
        items = [to_jsonable(dict(r)) for r in records]
        if items:
            state["cursor"] = items[-1][column.split(".")[-1]]
        return PollResult(items=items, state=state)

    async def test_connection(self, ctx: ConnectorContext) -> TestResult:
        try:
            async with connect(ctx.config, ctx.credentials) as conn, conn.cursor() as cur:
                await cur.execute("SELECT VERSION()")
                (version,) = await cur.fetchone()
        except (ConnectorError, aiomysql.Error) as exc:
            return TestResult(False, str(exc))
        return TestResult(True, f"Connected to MySQL {version}", {"server_version": version})
