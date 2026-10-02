"""Secret masking for logs, execution data and API responses.

Two complementary strategies:

* **Key-based**: values under keys that look sensitive (``password``, ``api_key``, ``authorization`` ...)
  are replaced regardless of content.
* **Value-based**: any string equal to — or containing — a registered secret value (credentials resolved
  for the current execution) is redacted, catching secrets echoed back by third-party APIs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

MASK = "••••••••"

SENSITIVE_KEY_RE = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|apikey|authorization|auth|credential|private[_-]?key|"
    r"client[_-]?secret|refresh[_-]?token|access[_-]?token|cookie|session[_-]?id|signature|ssn|card[_-]?number|cvv)",
    re.IGNORECASE,
)
# Keys that match the regex but are not secrets.
_ALLOWED_KEYS = {
    "token_count",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "tokens",
    "auth_type",
    "max_tokens",
    "author",
    "authority",
    "authored_by",
    "authorized",
}

_INLINE_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"(?i)(postgres(ql)?|mysql|redis|amqp)://([^:/\s]+):([^@\s]+)@"),
]


def is_sensitive_key(key: str) -> bool:
    k = key.lower()
    if k in _ALLOWED_KEYS:
        return False
    return bool(SENSITIVE_KEY_RE.search(k))


def mask_inline(text: str, secret_values: Iterable[str] = ()) -> str:
    out = text
    for value in secret_values:
        if value and len(value) >= 4 and value in out:
            out = out.replace(value, MASK)
    for pattern in _INLINE_PATTERNS:
        if pattern.groups >= 4:  # connection URLs: keep scheme and user, mask password
            out = pattern.sub(lambda m: f"{m.group(1)}://{m.group(3)}:{MASK}@", out)
        elif pattern.groups >= 1:
            out = pattern.sub(lambda m: m.group(1) + MASK, out)
        else:
            out = pattern.sub(MASK, out)
    return out


def mask_data(data: Any, secret_values: Iterable[str] = (), *, mask_keys: bool = True, _depth: int = 0) -> Any:
    """Return a deep copy of ``data`` with secrets masked."""
    values = tuple(v for v in secret_values if v)
    if _depth > 50:
        return data
    if isinstance(data, dict):
        result: dict[str, Any] = {}
        for k, v in data.items():
            if mask_keys and isinstance(k, str) and is_sensitive_key(k) and v not in (None, "", [], {}):
                result[k] = MASK
            else:
                result[k] = mask_data(v, values, mask_keys=mask_keys, _depth=_depth + 1)
        return result
    if isinstance(data, list):
        return [mask_data(v, values, mask_keys=mask_keys, _depth=_depth + 1) for v in data]
    if isinstance(data, str):
        return mask_inline(data, values)
    return data
