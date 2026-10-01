"""Shared Redis client and GCRA rate limiter."""

from __future__ import annotations

import time
from dataclasses import dataclass

import redis.asyncio as aioredis

from app.core.config import get_settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True, health_check_interval=30)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


# Generic Cell Rate Algorithm: smooth limiting without per-request sorted sets.
_GCRA_LUA = """
local key = KEYS[1]
local emission = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local tat = tonumber(redis.call('GET', key) or now)
if tat < now then tat = now end
local allow_at = tat - burst * emission
if now < allow_at then
  return {0, math.ceil((allow_at - now))}
end
local new_tat = tat + emission
redis.call('SET', key, new_tat, 'PX', math.ceil(new_tat - now + emission))
return {1, 0}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_ms: int


class RateLimiter:
    def __init__(self, client: aioredis.Redis | None = None) -> None:
        self._client = client
        self._script = None

    @property
    def client(self) -> aioredis.Redis:
        return self._client or get_redis()

    async def hit(self, key: str, per_minute: int, burst: int | None = None) -> RateLimitResult:
        if per_minute <= 0:
            return RateLimitResult(True, 0)
        if self._script is None:
            self._script = self.client.register_script(_GCRA_LUA)
        emission_ms = 60_000 / per_minute
        burst = burst if burst is not None else max(1, per_minute // 6)
        now_ms = int(time.time() * 1000)
        allowed, retry = await self._script(keys=[f"rl:{key}"], args=[emission_ms, burst, now_ms])
        return RateLimitResult(bool(allowed), int(retry))
