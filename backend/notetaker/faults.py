"""Runtime fault injection so every failure mode in spec §6 can be shown on demand.

Faults live in a Redis hash `faults` that the dashboard / admin API sets:
  llm_429, notion_429, youtube_429   int   — next N calls to that API raise a 429
  frame_fail_rate                    float — probability a single frame capture fails
  frame_fail_all                     0/1   — every frame capture fails (text-only page)
  crash_stage                        str   — next worker to run this stage hard-crashes mid-stage
                                             (os._exit, like SIGKILL: no ack, no cleanup). One-shot.
"""
from __future__ import annotations

import random

import redis.asyncio as aioredis

CONSUME_LUA = r"""
local v = tonumber(redis.call('HGET', KEYS[1], ARGV[1]) or '0')
if v and v > 0 then redis.call('HINCRBY', KEYS[1], ARGV[1], -1); return 1 end
return 0
"""

GETDEL_FIELD_LUA = r"""
local v = redis.call('HGET', KEYS[1], ARGV[1])
if v then redis.call('HDEL', KEYS[1], ARGV[1]) end
return v
"""

VALID_FIELDS = {"llm_429", "notion_429", "youtube_429", "frame_fail_rate", "frame_fail_all", "crash_stage"}


class Faults:
    def __init__(self, r: aioredis.Redis):
        self.r = r
        self._consume = r.register_script(CONSUME_LUA)
        self._getdel = r.register_script(GETDEL_FIELD_LUA)

    async def consume(self, name: str) -> bool:
        return bool(await self._consume(keys=["faults"], args=[name]))

    async def frame_should_fail(self) -> bool:
        vals = await self.r.hmget("faults", "frame_fail_all", "frame_fail_rate")
        if vals[0] in ("1", "true"):
            return True
        try:
            rate = float(vals[1] or 0)
        except ValueError:
            rate = 0.0
        return rate > 0 and random.random() < rate

    async def take_crash(self, stage: str) -> bool:
        cur = await self.r.hget("faults", "crash_stage")
        if cur != stage:
            return False
        return (await self._getdel(keys=["faults"], args=["crash_stage"])) == stage

    async def get_all(self) -> dict:
        return await self.r.hgetall("faults")

    async def set(self, values: dict) -> dict:
        clean: dict[str, str] = {}
        removals: list[str] = []
        for k, v in values.items():
            if k not in VALID_FIELDS:
                continue
            if v in (None, "", 0, "0", False, "false"):
                removals.append(k)
            else:
                clean[k] = "1" if v is True else str(v)
        if clean:
            await self.r.hset("faults", mapping=clean)
        if removals:
            await self.r.hdel("faults", *removals)
        return await self.get_all()

    async def clear(self) -> None:
        await self.r.delete("faults")
