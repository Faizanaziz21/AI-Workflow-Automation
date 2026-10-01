"""Sandboxed expression language for workflow configuration.

Syntax: Python-like expressions inside ``{{ ... }}``::

    {{ nodes.classify.output.priority == "high" and trigger.body.amount > 5000 }}
    Hello {{ default(trigger.body.name, "there") }}
    {{ [i.email for i in nodes.fetch.output.rows if i.score >= 80] }}

Values are plain JSON data (dict/list/str/number/bool/None). The interpreter walks the AST and only
supports a whitelisted subset of nodes; dunder names, imports, lambdas, attribute access on non-data
objects and arbitrary calls are impossible. Work is bounded by an operation budget and size limits so a
hostile expression cannot hang a worker or exhaust memory.

Missing keys evaluate to ``None`` (like optional chaining), which keeps templates readable when optional
fields are absent.
"""

from __future__ import annotations

import ast
import json
import math
import operator
import re
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

MAX_EXPRESSION_LENGTH = 4000
MAX_OPERATIONS = 100_000
MAX_SEQUENCE = 100_000
MAX_STRING = 1_000_000

TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.S)


class ExpressionError(Exception):
    def __init__(self, message: str, expression: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.expression = expression


# ----------------------------------------------------------------------------- built-in functions


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    raise ExpressionError(f"Cannot interpret {value!r} as a date")


def _date_add(value: Any, days: float = 0, hours: float = 0, minutes: float = 0, seconds: float = 0) -> str:
    return (_parse_dt(value) + timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)).isoformat()


def _date_diff(a: Any, b: Any, unit: str = "seconds") -> float:
    delta = (_parse_dt(a) - _parse_dt(b)).total_seconds()
    return {"seconds": delta, "minutes": delta / 60, "hours": delta / 3600, "days": delta / 86400}[unit]


def _format_date(value: Any, fmt: str = "%Y-%m-%d") -> str:
    return _parse_dt(value).strftime(fmt)


def _get(obj: Any, path: str, default: Any = None) -> Any:
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur


def _default(value: Any, fallback: Any) -> Any:
    return fallback if value in (None, "", [], {}) else value


def _coalesce(*values: Any) -> Any:
    return next((v for v in values if v is not None), None)


def _regex_match(value: Any, pattern: str) -> bool:
    if len(pattern) > 500:
        raise ExpressionError("Regex too long")
    return re.search(pattern, str(value or "")) is not None


def _regex_extract(value: Any, pattern: str, group: int = 0) -> str | None:
    if len(pattern) > 500:
        raise ExpressionError("Regex too long")
    m = re.search(pattern, str(value or ""))
    return m.group(group) if m else None


def _pluck(items: Iterable[Any], key: str) -> list[Any]:
    return [_get(i, key) for i in items or []]


def _to_number(value: Any, default: float | None = None) -> float | int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        s = str(value).strip().replace(",", "")
        return int(s) if re.fullmatch(r"-?\d+", s) else float(s)
    except (TypeError, ValueError):
        return default


def _unique(items: Iterable[Any] | None) -> list[Any]:
    seen: dict[str, Any] = {}
    for item in items or []:
        seen.setdefault(json.dumps(item, sort_keys=True, default=str), item)
    return list(seen.values())


def _range(*args: int) -> list[int]:
    r = range(*args)
    if len(r) > MAX_SEQUENCE:
        raise ExpressionError("range() too large")
    return list(r)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _parse_json(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise ExpressionError(f"Invalid JSON: {exc.msg}") from exc


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "round": round,
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "any": any,
    "all": all,
    "sorted": sorted,
    "list": list,
    "dict": dict,
    "range": lambda *a: _range(*a),
    "lower": lambda s: str(s or "").lower(),
    "upper": lambda s: str(s or "").upper(),
    "trim": lambda s: str(s or "").strip(),
    "title": lambda s: str(s or "").title(),
    "contains": lambda h, n: (n in h) if h is not None else False,
    "startswith": lambda s, p: str(s or "").startswith(p),
    "endswith": lambda s, p: str(s or "").endswith(p),
    "split": lambda s, sep=None: str(s or "").split(sep),
    "join": lambda items, sep=",": sep.join(str(i) for i in items),
    "replace": lambda s, a, b: str(s or "").replace(a, b),
    "truncate": lambda s, n: str(s or "")[: int(n)],
    "keys": lambda d: list((d or {}).keys()),
    "values": lambda d: list((d or {}).values()),
    "items": lambda d: [[k, v] for k, v in (d or {}).items()],
    "unique": lambda items: _unique(items),
    "pluck": _pluck,
    "get": _get,
    "default": _default,
    "coalesce": _coalesce,
    "now": lambda: datetime.now(UTC).isoformat(),
    "today": lambda: date.today().isoformat(),
    "date_add": _date_add,
    "date_diff": _date_diff,
    "format_date": _format_date,
    "timestamp": lambda v=None: _parse_dt(v).timestamp() if v is not None else datetime.now(UTC).timestamp(),
    "regex_match": _regex_match,
    "regex_extract": _regex_extract,
    "number": _to_number,
    "json": _json,
    "parse_json": _parse_json,
    "floor": math.floor,
    "ceil": math.ceil,
    "is_empty": lambda v: v in (None, "", [], {}),
    "type_of": lambda v: type(v).__name__,
}

# Methods callable on data values (receiver type -> allowed method names).
SAFE_METHODS: dict[type, frozenset[str]] = {
    str: frozenset(
        {
            "lower",
            "upper",
            "strip",
            "lstrip",
            "rstrip",
            "startswith",
            "endswith",
            "split",
            "replace",
            "title",
            "capitalize",
            "count",
            "find",
            "isdigit",
            "isalpha",
            "join",
            "splitlines",
            "zfill",
        }
    ),
    dict: frozenset({"get", "keys", "values", "items"}),
    list: frozenset({"count", "index"}),
}

_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_CMPOPS: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b if b is not None else False,
    ast.NotIn: lambda a, b: a not in b if b is not None else True,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}


class _Interpreter:
    def __init__(self, scope: dict[str, Any], source: str) -> None:
        self.scope = scope
        self.source = source
        self.ops = 0

    def tick(self, n: int = 1) -> None:
        self.ops += n
        if self.ops > MAX_OPERATIONS:
            raise ExpressionError("Expression exceeded its operation budget", self.source)

    def eval(self, node: ast.AST, local: dict[str, Any] | None = None) -> Any:
        self.tick()
        local = local or {}
        if isinstance(node, ast.Expression):
            return self.eval(node.body, local)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (str, int, float, bool, type(None))):
                return node.value
            raise ExpressionError("Unsupported constant", self.source)
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise ExpressionError(f"Name '{node.id}' is not allowed", self.source)
            if node.id in local:
                return local[node.id]
            if node.id in self.scope:
                return self.scope[node.id]
            if node.id in ("true", "false", "null", "none", "None", "True", "False"):
                return {"true": True, "True": True, "false": False, "False": False}.get(node.id)
            raise ExpressionError(f"Unknown name '{node.id}'", self.source)
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ExpressionError(f"Attribute '{node.attr}' is not allowed", self.source)
            obj = self.eval(node.value, local)
            if isinstance(obj, dict):
                return obj.get(node.attr)
            if obj is None:
                return None
            if isinstance(obj, list) and node.attr == "length":
                return len(obj)
            raise ExpressionError(f"Cannot read '{node.attr}' of {type(obj).__name__}", self.source)
        if isinstance(node, ast.Subscript):
            obj = self.eval(node.value, local)
            if isinstance(node.slice, ast.Slice):
                lo = self.eval(node.slice.lower, local) if node.slice.lower else None
                hi = self.eval(node.slice.upper, local) if node.slice.upper else None
                step = self.eval(node.slice.step, local) if node.slice.step else None
                if not isinstance(obj, (list, str)):
                    raise ExpressionError("Slicing requires a list or string", self.source)
                return obj[lo:hi:step]
            key = self.eval(node.slice, local)
            if obj is None:
                return None
            if isinstance(obj, dict):
                return obj.get(key) if not isinstance(key, (dict, list)) else None
            if isinstance(obj, (list, str)) and isinstance(key, int) and not isinstance(key, bool):
                return obj[key] if -len(obj) <= key < len(obj) else None
            raise ExpressionError(f"Cannot index {type(obj).__name__} with {type(key).__name__}", self.source)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result: Any = True
                for v in node.values:
                    result = self.eval(v, local)
                    if not result:
                        return result
                return result
            result = False
            for v in node.values:
                result = self.eval(v, local)
                if result:
                    return result
            return result
        if isinstance(node, ast.UnaryOp):
            operand = self.eval(node.operand, local)
            if isinstance(node.op, ast.Not):
                return not operand
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return +operand
            raise ExpressionError("Unsupported unary operator", self.source)
        if isinstance(node, ast.BinOp):
            op = _BINOPS.get(type(node.op))
            if op is None:
                raise ExpressionError("Unsupported operator", self.source)
            left, right = self.eval(node.left, local), self.eval(node.right, local)
            if isinstance(node.op, ast.Pow) and (abs(right) > 100 if isinstance(right, (int, float)) else True):
                raise ExpressionError("Exponent too large", self.source)
            if isinstance(node.op, ast.Mult) and (isinstance(left, (str, list)) or isinstance(right, (str, list))):
                n = right if isinstance(left, (str, list)) else left
                seq = left if isinstance(left, (str, list)) else right
                if not isinstance(n, int) or len(seq) * max(n, 0) > MAX_STRING:
                    raise ExpressionError("Sequence repetition too large", self.source)
            try:
                result = op(left, right)
            except ZeroDivisionError as exc:
                raise ExpressionError("Division by zero", self.source) from exc
            except TypeError as exc:
                raise ExpressionError(f"Type error: {exc}", self.source) from exc
            if isinstance(result, (str, list)) and len(result) > MAX_STRING:
                raise ExpressionError("Result too large", self.source)
            return result
        if isinstance(node, ast.Compare):
            left = self.eval(node.left, local)
            for op_node, comparator in zip(node.ops, node.comparators, strict=True):
                right = self.eval(comparator, local)
                fn = _CMPOPS[type(op_node)]
                try:
                    ok = fn(left, right)
                except TypeError:
                    ok = False  # e.g. None > 5 -> False rather than crashing the workflow
                if not ok:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self.eval(node.body, local) if self.eval(node.test, local) else self.eval(node.orelse, local)
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.eval(e, local) for e in node.elts]
        if isinstance(node, ast.Dict):
            out: dict[str, Any] = {}
            for k, v in zip(node.keys, node.values, strict=True):
                if k is None:
                    raise ExpressionError("Dict unpacking is not supported", self.source)
                out[str(self.eval(k, local))] = self.eval(v, local)
            return out
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.FormattedValue):
                    parts.append(stringify(self.eval(v.value, local)))
                else:
                    parts.append(self.eval(v, local))
            return "".join(parts)
        if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
            if len(node.generators) != 1:
                raise ExpressionError("Only single-loop comprehensions are supported", self.source)
            gen = node.generators[0]
            if not isinstance(gen.target, ast.Name) or gen.is_async:
                raise ExpressionError("Comprehension target must be a simple name", self.source)
            iterable = self.eval(gen.iter, local)
            if iterable is None:
                return []
            if isinstance(iterable, dict):
                iterable = list(iterable.keys())
            if not isinstance(iterable, (list, str)):
                raise ExpressionError("Comprehension requires a list", self.source)
            results = []
            for item in iterable[:MAX_SEQUENCE]:
                self.tick()
                inner = {**local, gen.target.id: item}
                if all(self.eval(cond, inner) for cond in gen.ifs):
                    results.append(self.eval(node.elt, inner))
            return results
        if isinstance(node, ast.Call):
            if node.keywords and any(k.arg is None for k in node.keywords):
                raise ExpressionError("**kwargs not supported", self.source)
            args = [self.eval(a, local) for a in node.args if not isinstance(a, ast.Starred)]
            if any(isinstance(a, ast.Starred) for a in node.args):
                raise ExpressionError("*args not supported", self.source)
            kwargs = {k.arg: self.eval(k.value, local) for k in node.keywords if k.arg}
            if isinstance(node.func, ast.Name):
                fn = FUNCTIONS.get(node.func.id)
                if fn is None:
                    raise ExpressionError(f"Unknown function '{node.func.id}'", self.source)
            elif isinstance(node.func, ast.Attribute):
                receiver = self.eval(node.func.value, local)
                allowed = SAFE_METHODS.get(type(receiver), frozenset())
                if node.func.attr not in allowed:
                    raise ExpressionError(
                        f"Method '{node.func.attr}' not allowed on {type(receiver).__name__}", self.source
                    )
                fn = getattr(receiver, node.func.attr)
            else:
                raise ExpressionError("Unsupported call", self.source)
            try:
                result = fn(*args, **kwargs)
            except ExpressionError:
                raise
            except Exception as exc:
                raise ExpressionError(f"Function error: {type(exc).__name__}: {exc}", self.source) from exc
            if isinstance(result, (str, list)) and len(result) > MAX_STRING:
                raise ExpressionError("Result too large", self.source)
            return result
        raise ExpressionError(f"Unsupported syntax: {type(node).__name__}", self.source)


_parse_cache: dict[str, ast.Expression] = {}


def parse(expression: str) -> ast.Expression:
    expr = expression.strip()
    if len(expr) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError("Expression too long", expr[:100])
    cached = _parse_cache.get(expr)
    if cached is not None:
        return cached
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Syntax error: {exc.msg}", expr) from exc
    if len(_parse_cache) > 5000:
        _parse_cache.clear()
    _parse_cache[expr] = tree
    return tree


def evaluate(expression: str, scope: dict[str, Any]) -> Any:
    return _Interpreter(scope, expression).eval(parse(expression))


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def is_template(value: Any) -> bool:
    return isinstance(value, str) and "{{" in value and "}}" in value


def render(value: Any, scope: dict[str, Any]) -> Any:
    """Render templates recursively. A string that is exactly one ``{{ expr }}`` keeps the result's type."""
    if isinstance(value, str):
        if "{{" not in value:
            return value
        stripped = value.strip()
        match = TEMPLATE_RE.fullmatch(stripped)
        if match and stripped.count("{{") == 1:
            return evaluate(match.group(1), scope)
        return TEMPLATE_RE.sub(lambda m: stringify(evaluate(m.group(1), scope)), value)
    if isinstance(value, dict):
        return {k: render(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, scope) for v in value]
    return value


def extract_expressions(value: Any) -> list[str]:
    """All ``{{ }}`` expressions inside a (nested) config value."""
    out: list[str] = []
    if isinstance(value, str):
        out.extend(m.group(1).strip() for m in TEMPLATE_RE.finditer(value))
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(extract_expressions(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(extract_expressions(v))
    return out


def referenced_nodes(expression: str) -> set[str]:
    """Node ids referenced as ``nodes.<id>`` or ``nodes["<id>"]`` (used by the validator)."""
    refs: set[str] = set()
    for n in ast.walk(parse(expression)):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "nodes":
            refs.add(n.attr)
        elif (
            isinstance(n, ast.Subscript)
            and isinstance(n.value, ast.Name)
            and n.value.id == "nodes"
            and isinstance(n.slice, ast.Constant)
            and isinstance(n.slice.value, str)
        ):
            refs.add(n.slice.value)
    return refs


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "none", "null")
    return bool(value)
