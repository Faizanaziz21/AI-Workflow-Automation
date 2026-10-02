"""SQL helpers shared by database connectors.

Workflow authors write portable named parameters (``WHERE email = :email``); values are *always* bound by the
driver, never interpolated. Identifiers used by polling triggers are validated against a strict pattern.
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import re
import uuid
from typing import Any

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}(\.[A-Za-z_][A-Za-z0-9_]{0,62})?$")
_WRITE_RE = re.compile(r"^\s*(insert|update|delete|merge|create|alter|drop|truncate|grant|revoke|copy|call|do)\b", re.I)


def validate_identifier(name: str) -> str:
    if not IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def looks_like_write(sql: str) -> bool:
    stripped = re.sub(r"(--[^\n]*\n)|(/\*.*?\*/)", " ", sql, flags=re.S)
    return bool(_WRITE_RE.match(stripped)) or bool(re.search(r";\s*\S", stripped.strip().rstrip(";")))


def _scan_named(sql: str) -> list[tuple[int, int, str]]:
    """Find ``:name`` placeholders outside quotes/comments, skipping ``::type`` casts."""
    out: list[tuple[int, int, str]] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`"):
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if c == ":" and i + 1 < n and sql[i + 1] == ":":
            i += 2
            continue
        if c == ":" and i + 1 < n and (sql[i + 1].isalpha() or sql[i + 1] == "_") and (i == 0 or sql[i - 1] != ":"):
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            out.append((i, j, sql[i + 1 : j]))
            i = j
            continue
        i += 1
    return out


def to_positional(sql: str, params: dict[str, Any]) -> tuple[str, list[Any]]:
    """``:name`` -> ``$n`` (PostgreSQL/asyncpg)."""
    order: list[str] = []
    pieces: list[str] = []
    last = 0
    for start, end, name in _scan_named(sql):
        if name not in params:
            raise ValueError(f"Missing SQL parameter: {name}")
        if name not in order:
            order.append(name)
        pieces.append(sql[last:start])
        pieces.append(f"${order.index(name) + 1}")
        last = end
    pieces.append(sql[last:])
    return "".join(pieces), [params[name] for name in order]


def to_pyformat(sql: str, params: dict[str, Any]) -> str:
    """``:name`` -> ``%(name)s`` (MySQL drivers); literal ``%`` escaped."""
    pieces: list[str] = []
    last = 0
    for start, end, name in _scan_named(sql):
        if name not in params:
            raise ValueError(f"Missing SQL parameter: {name}")
        pieces.append(sql[last:start].replace("%", "%%"))
        pieces.append(f"%({name})s")
        last = end
    pieces.append(sql[last:].replace("%", "%%"))
    return "".join(pieces)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() and abs(value) < 2**53 else float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode()
    return value
