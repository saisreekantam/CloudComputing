"""Cluster-wide rate limiting shared by every worker, built on Redis.

GCRA (Generic Cell Rate Algorithm) runs in Lua using the *Redis server clock*, so every
worker sees the same time and the check-and-reserve step is atomic. One call can check
several buckets at once (e.g. LLM requests/min AND tokens/min): either every bucket admits
the request and all are charged, or none is charged and the caller learns how long to wait.

There is also a shared *cooldown* key. When any worker gets a real 429 from a provider it
records Retry-After there, and every worker pauses that service. This is the adaptive part
on top of the proactive limiter.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass

import redis.asyncio as aioredis

from .config import settings
from .errors import RateLimited

log = logging.getLogger(__name__)

GCRA_LUA = r"""
-- KEYS: bucket keys.  ARGV: per bucket (emission_interval_ms, tolerance_ms, cost) triples.
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local n = #KEYS
local max_wait = 0
local new_tats = {}
for i = 1, n do
  local ei = tonumber(ARGV[(i - 1) * 3 + 1])
  local tol = tonumber(ARGV[(i - 1) * 3 + 2])
  local cost = tonumber(ARGV[(i - 1) * 3 + 3])
  local tat = tonumber(redis.call('GET', KEYS[i]) or now)
  if tat < now then tat = now end
  local new_tat = tat + ei * cost
  -- capacity = tolerance + one emission interval (i.e. `burst` units)
  local allow_at = new_tat - tol - ei
  if allow_at > now then
    local w = allow_at - now
    if w > max_wait then max_wait = w end
  end
  new_tats[i] = new_tat
end
-- ceil: Redis truncates Lua numbers to integers, and a sub-ms wait must not read as "admitted"
if max_wait > 0 then return math.ceil(max_wait) end
for i = 1, n do
  redis.call('SET', KEYS[i], string.format('%.3f', new_tats[i]), 'PX', math.ceil(new_tats[i] - now) + 1000)
end
return 0
"""


@dataclass(frozen=True)
class Bucket:
    key: str
    rate_per_sec: float     # sustained rate (units/sec)
    burst: float            # how many units may be spent instantly

    @property
    def emission_ms(self) -> float:
        return 1000.0 / self.rate_per_sec

    @property
    def tolerance_ms(self) -> float:
        return self.emission_ms * max(self.burst - 1, 0)


def llm_buckets() -> list[Bucket]:
    # RPM: strictly spaced (burst 1) so no sliding 60s window can exceed LLM_RPM.
    return [
        Bucket("rl:llm:rpm", settings.llm_rpm / 60.0, 1),
        Bucket("rl:llm:tpm", settings.llm_tpm / 60.0, settings.llm_tpm),
    ]


def notion_buckets(token: str) -> list[Bucket]:
    # Notion's limit is per integration token (avg 3 req/s) — key by a hash, never the token.
    h = hashlib.sha256(token.encode()).hexdigest()[:16]
    return [Bucket(f"rl:notion:{h}", settings.notion_rps, 3)]


def youtube_buckets() -> list[Bucket]:
    return [Bucket("rl:youtube", settings.youtube_rpm / 60.0, 3)]


class RateLimiter:
    def __init__(self, r: aioredis.Redis):
        self.r = r
        self._script = r.register_script(GCRA_LUA)

    async def cooldown_remaining(self, service: str) -> float:
        ms = await self.r.pttl(f"rl:cooldown:{service}")
        return ms / 1000.0 if ms and ms > 0 else 0.0

    async def set_cooldown(self, service: str, seconds: float) -> None:
        seconds = max(1.0, min(seconds, 600.0))
        # Only extend, never shorten, an existing cooldown.
        cur = await self.cooldown_remaining(service)
        if seconds > cur:
            await self.r.set(f"rl:cooldown:{service}", "1", px=int(seconds * 1000))

    async def try_acquire(self, buckets: list[Bucket], costs: list[float]) -> float:
        """Returns 0 if admitted (and charged), else seconds to wait (nothing charged)."""
        args: list[float] = []
        for b, c in zip(buckets, costs):
            # A single request larger than the burst can never fit; clamp so it waits a full window instead.
            cost = min(c, max(b.burst, 1))
            args.extend([b.emission_ms, b.tolerance_ms, cost])
        wait_ms = await self._script(keys=[b.key for b in buckets], args=args)
        return float(wait_ms) / 1000.0

    async def acquire(self, service: str, buckets: list[Bucket], costs: list[float] | None = None,
                      max_wait: float | None = None, on_wait=None) -> float:
        """Block (async) until admitted. If the required wait exceeds `max_wait`, raise
        RateLimited(self_imposed=True) so the job is re-queued with backoff instead of
        holding a worker slot. Returns total seconds waited."""
        costs = costs or [1.0] * len(buckets)
        max_wait = settings.max_local_wait_sec if max_wait is None else max_wait
        waited = 0.0
        while True:
            cd = await self.cooldown_remaining(service)
            wait = cd if cd > 0 else await self.try_acquire(buckets, costs)
            if wait <= 0:
                if waited > 0:
                    await self.r.hincrbyfloat("metrics", f"ratelimit_wait_sec:{service}", round(waited, 3))
                return waited
            if waited + wait > max_wait:
                raise RateLimited(service, retry_after=wait, self_imposed=True,
                                  message=f"{service} budget exhausted, waiting {wait:.0f}s")
            if on_wait:
                await on_wait(wait)
            await asyncio.sleep(min(wait, 5.0) + 0.01)
            waited += min(wait, 5.0)
