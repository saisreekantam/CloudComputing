"""Redis-backed job store.

Key layout
  job:{id}                hash   — job record (status, progress, title, result, ...)
  job:{id}:timeline       list   — status transitions [{status, at, worker}]
  job:{id}:events         list   — notable events (retry scheduled, recovered after crash, ...)
  job:{id}:done           set    — stages whose output is durably committed (checkpoints)
  job:{id}:ckpt:{name}    string — stage outputs (transcript, synthesis, chunk:N, frames)
  job:{id}:frame:{n}      bytes  — captured JPEG frames
  lock:{id}:{stage}       string — stage lease (holder token, PX lease, renewed while working)
  jobs:recent             zset   — job ids by creation time
  idem:{key}              string — Idempotency-Key -> job id
  dlq:index               zset   — dead-lettered job ids
  worker:{id}             string — worker heartbeat (TTL)
  metrics                 hash   — counters

All status changes go through COMMIT_LUA: an atomic compare-and-set that (a) enforces the
legal transition graph, (b) verifies the caller still holds the stage lease (fencing), and
(c) writes stage outputs + the "stage done" checkpoint in the same step. A worker that lost its
lease (e.g. was partitioned and replaced) can therefore never overwrite newer state.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

import redis.asyncio as aioredis
from cryptography.fernet import Fernet, InvalidToken

from .config import settings
from .models import STAGES, TERMINAL, Status, predecessors

COMMIT_LUA = r"""
local job = KEYS[1]
local cur = redis.call('HGET', job, 'status')
if not cur then return {'MISSING', ''} end
local target = ARGV[1]
local lock_key = ARGV[3]
if lock_key ~= '' then
  if redis.call('GET', lock_key) ~= ARGV[4] then return {'LOST_LEASE', cur} end
end
local terminal = {COMPLETED=true, COMPLETED_WITH_WARNINGS=true, FAILED=true, DEAD_LETTERED=true}
if target == '' then
  if terminal[cur] then return {'TERMINAL', cur} end
elseif target ~= cur then
  if not string.find(ARGV[2], ',' .. cur .. ',', 1, true) then return {'INVALID', cur} end
end
local now = ARGV[5]
local i = 6
local nf = tonumber(ARGV[i]); i = i + 1
for _ = 1, nf do
  redis.call('HSET', job, ARGV[i], ARGV[i + 1]); i = i + 2
end
local no = tonumber(ARGV[i]); i = i + 1
for _ = 1, no do
  redis.call('SET', ARGV[i], ARGV[i + 1], 'EX', tonumber(ARGV[i + 2])); i = i + 3
end
local done = ARGV[i]; i = i + 1
local worker = ARGV[i]; i = i + 1
if done ~= '' then redis.call('SADD', job .. ':done', done) end
if target ~= '' and target ~= cur then
  redis.call('HSET', job, 'status', target)
  redis.call('RPUSH', job .. ':timeline', cjson.encode({status = target, at = tonumber(now), worker = worker}))
end
redis.call('HSET', job, 'updated_at', now)
return {'OK', cur}
"""

RENEW_LUA = r"""
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

RELEASE_LUA = r"""
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""

STEAL_LUA = r"""
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3]); return 1
end
return 0
"""

JSON_FIELDS = {"error", "result", "retry", "meta", "waiting"}
INT_FIELDS = {"progress", "duration_sec", "attempt", "frames_requested", "frames_published"}
CKPT_TTL = 3 * 24 * 3600
FRAME_TTL = 24 * 3600


class CommitResult:
    def __init__(self, code: str, previous: str):
        self.code = code
        self.previous = previous

    @property
    def ok(self) -> bool:
        return self.code == "OK"

    def __repr__(self) -> str:  # pragma: no cover
        return f"CommitResult({self.code}, prev={self.previous})"


def _allowed_csv(target: Status | None) -> str:
    if target is None:
        return ","
    return "," + ",".join(s.value for s in predecessors(target)) + ","


def now() -> float:
    return round(time.time(), 3)


class JobStore:
    def __init__(self, url: str | None = None):
        url = url or settings.redis_url
        self.r = aioredis.from_url(url, decode_responses=True, health_check_interval=15)
        self.rb = aioredis.from_url(url, decode_responses=False, health_check_interval=15)
        self._commit = self.r.register_script(COMMIT_LUA)
        self._renew = self.r.register_script(RENEW_LUA)
        self._release = self.r.register_script(RELEASE_LUA)
        self._steal = self.r.register_script(STEAL_LUA)
        self._fernet = Fernet(settings.fernet_key)

    async def close(self) -> None:
        await self.r.aclose()
        await self.rb.aclose()

    async def ping(self) -> bool:
        return bool(await self.r.ping())

    # ------------------------------------------------------------------ keys
    @staticmethod
    def k(job_id: str, suffix: str = "") -> str:
        return f"job:{job_id}" + (f":{suffix}" if suffix else "")

    # --------------------------------------------------------------- secrets
    def encrypt(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode()).decode()

    def decrypt(self, blob: str) -> str | None:
        try:
            return self._fernet.decrypt(blob.encode()).decode()
        except (InvalidToken, ValueError):
            return None

    # -------------------------------------------------------------- creation
    async def create_job(self, *, video_id: str, youtube_url: str, notion_token: str,
                         notion_parent_id: str | None, idempotency_key: str | None,
                         client_hint: dict | None = None) -> tuple[str, bool]:
        """Create a QUEUED job. Returns (job_id, created). Honors Idempotency-Key for 10 min."""
        job_id = str(uuid.uuid4())
        if idempotency_key:
            existing = await self.r.set(f"idem:{idempotency_key}", job_id, nx=True, ex=600, get=True)
            if existing:
                return existing, False
        ts = now()
        fields = {
            "job_id": job_id,
            "video_id": video_id,
            "youtube_url": youtube_url,
            "status": Status.QUEUED.value,
            "progress": 0,
            "video_title": (client_hint or {}).get("title") or "",
            "duration_sec": int((client_hint or {}).get("duration_sec") or 0),
            "created_at": ts,
            "updated_at": ts,
            "attempt": 0,
            "token_enc": self.encrypt(notion_token),
            "notion_parent_id": notion_parent_id or "",
            "stage": "transcribe",
        }
        pipe = self.r.pipeline(transaction=True)
        pipe.hset(self.k(job_id), mapping={k: str(v) for k, v in fields.items()})
        pipe.rpush(self.k(job_id, "timeline"), json.dumps({"status": "QUEUED", "at": ts, "worker": "gateway"}))
        pipe.zadd("jobs:recent", {job_id: ts})
        pipe.hincrby("metrics", "jobs_submitted", 1)
        await pipe.execute()
        return job_id, True

    async def delete_job(self, job_id: str) -> None:
        await self.r.delete(self.k(job_id), self.k(job_id, "timeline"), self.k(job_id, "events"))
        await self.r.zrem("jobs:recent", job_id)

    # ----------------------------------------------------------------- reads
    async def exists(self, job_id: str) -> bool:
        return bool(await self.r.exists(self.k(job_id)))

    async def get_raw(self, job_id: str) -> dict[str, str] | None:
        data = await self.r.hgetall(self.k(job_id))
        return data or None

    async def get_status(self, job_id: str) -> Status | None:
        s = await self.r.hget(self.k(job_id), "status")
        return Status(s) if s else None

    async def done_stages(self, job_id: str) -> set[str]:
        return set(await self.r.smembers(self.k(job_id, "done")))

    async def get_token(self, job_id: str) -> str | None:
        blob = await self.r.hget(self.k(job_id), "token_enc")
        return self.decrypt(blob) if blob else None

    async def warnings(self, job_id: str, raw: dict | None = None) -> list[dict]:
        raw = raw if raw is not None else (await self.get_raw(job_id) or {})
        out: list[dict] = []
        for stage in STAGES:
            blob = raw.get(f"warnings:{stage}")
            if blob:
                out.extend(json.loads(blob))
        return out

    async def view(self, job_id: str) -> dict | None:
        """Public JSON view used by GET /jobs/{id}. Pure read."""
        pipe = self.r.pipeline(transaction=False)
        pipe.hgetall(self.k(job_id))
        pipe.lrange(self.k(job_id, "timeline"), 0, -1)
        pipe.lrange(self.k(job_id, "events"), -15, -1)
        raw, timeline, events = await pipe.execute()
        if not raw:
            return None
        parsed: dict[str, Any] = {}
        for key, val in raw.items():
            if key in JSON_FIELDS:
                parsed[key] = json.loads(val) if val else None
            elif key in INT_FIELDS:
                try:
                    parsed[key] = int(float(val))
                except ValueError:
                    parsed[key] = 0
            else:
                parsed[key] = val
        status = parsed.get("status")
        retry = parsed.get("retry")
        waiting = parsed.get("waiting")
        if status in {s.value for s in TERMINAL}:
            retry = waiting = None
        return {
            "job_id": job_id,
            "status": status,
            "progress": parsed.get("progress", 0),
            "video_id": parsed.get("video_id"),
            "video_title": parsed.get("video_title") or None,
            "duration_sec": parsed.get("duration_sec") or None,
            "warnings": await self.warnings(job_id, raw),
            "error": parsed.get("error"),
            "result": parsed.get("result"),
            # --- extras beyond the spec (used by popup + dashboard) ---
            "youtube_url": parsed.get("youtube_url"),
            "channel": parsed.get("channel") or None,
            "stage": parsed.get("stage"),
            "attempt": parsed.get("attempt", 0),
            "retry": retry,
            "waiting": waiting,
            "frames_requested": parsed.get("frames_requested"),
            "worker": parsed.get("worker") or None,
            "created_at": float(parsed.get("created_at", 0)),
            "updated_at": float(parsed.get("updated_at", 0)),
            "timeline": [json.loads(x) for x in timeline],
            "events": [json.loads(x) for x in events],
        }

    async def recent(self, limit: int = 50) -> list[str]:
        return await self.r.zrevrange("jobs:recent", 0, limit - 1)

    # ---------------------------------------------------------------- writes
    async def commit(self, job_id: str, *, target: Status | None = None,
                     fields: dict[str, Any] | None = None,
                     outputs: dict[str, Any] | None = None,
                     done_stage: str | None = None,
                     lease: tuple[str, str] | None = None,
                     worker: str = "") -> CommitResult:
        """Atomic: [lease check] + [transition check] + fields + checkpoint outputs + done flag."""
        argv: list[Any] = [
            target.value if target else "",
            _allowed_csv(target),
            lease[0] if lease else "",
            lease[1] if lease else "",
            str(now()),
        ]
        fields = fields or {}
        argv.append(len(fields))
        for key, val in fields.items():
            argv.extend([key, json.dumps(val) if key in JSON_FIELDS or key.startswith("warnings:") else str(val)])
        outputs = outputs or {}
        argv.append(len(outputs))
        for name, val in outputs.items():
            argv.extend([self.k(job_id, f"ckpt:{name}"), json.dumps(val), CKPT_TTL])
        argv.extend([done_stage or "", worker])
        code, prev = await self._commit(keys=[self.k(job_id)], args=argv)
        res = CommitResult(code, prev)
        if res.ok and target in TERMINAL:
            await self._on_terminal(job_id, target)
        return res

    async def _on_terminal(self, job_id: str, status: Status) -> None:
        ttl = settings.job_ttl_days * 86400
        pipe = self.r.pipeline(transaction=False)
        if status != Status.DEAD_LETTERED:
            pipe.hdel(self.k(job_id), "token_enc")  # wipe the user's Notion token
        for suffix in ("", "timeline", "events", "done"):
            pipe.expire(self.k(job_id, suffix), ttl)
        pipe.hincrby("metrics", f"jobs_{status.value.lower()}", 1)
        await pipe.execute()

    async def set_fields(self, job_id: str, lease: tuple[str, str] | None = None, **fields) -> CommitResult:
        """Non-transition update (progress, title...). Refused once the job is terminal."""
        return await self.commit(job_id, fields=fields, lease=lease)

    async def add_event(self, job_id: str, kind: str, message: str, **extra) -> None:
        entry = {"at": now(), "kind": kind, "message": message, **extra}
        pipe = self.r.pipeline(transaction=False)
        pipe.rpush(self.k(job_id, "events"), json.dumps(entry))
        pipe.ltrim(self.k(job_id, "events"), -100, -1)
        await pipe.execute()

    # ----------------------------------------------------------- checkpoints
    async def load_ckpt(self, job_id: str, name: str) -> Any | None:
        blob = await self.r.get(self.k(job_id, f"ckpt:{name}"))
        return json.loads(blob) if blob else None

    async def save_ckpt(self, job_id: str, name: str, value: Any) -> None:
        await self.r.set(self.k(job_id, f"ckpt:{name}"), json.dumps(value), ex=CKPT_TTL)

    async def save_frame(self, job_id: str, idx: int, data: bytes) -> None:
        await self.rb.set(self.k(job_id, f"frame:{idx}").encode(), data, ex=FRAME_TTL)

    async def load_frame(self, job_id: str, idx: int) -> bytes | None:
        return await self.rb.get(self.k(job_id, f"frame:{idx}").encode())

    # ----------------------------------------------------------------- lease
    async def acquire_lease(self, job_id: str, stage: str, token: str) -> bool:
        key = f"lock:{job_id}:{stage}"
        ok = await self.r.set(key, token, nx=True, px=settings.lease_seconds * 1000)
        if ok:
            return True
        return (await self.r.get(key)) == token

    async def renew_lease(self, job_id: str, stage: str, token: str) -> bool:
        return bool(await self._renew(keys=[f"lock:{job_id}:{stage}"], args=[token, settings.lease_seconds * 1000]))

    async def release_lease(self, job_id: str, stage: str, token: str) -> None:
        await self._release(keys=[f"lock:{job_id}:{stage}"], args=[token])

    async def steal_lease(self, job_id: str, stage: str, expected: str, token: str) -> bool:
        """Compare-and-swap the lease from a holder known to be dead. Fencing (COMMIT_LUA) makes a
        wrongly-judged steal harmless: the old holder's next commit fails with LOST_LEASE."""
        return bool(await self._steal(keys=[f"lock:{job_id}:{stage}"],
                                      args=[expected, token, settings.lease_seconds * 1000]))

    async def worker_alive(self, worker_id: str) -> bool:
        return bool(await self.r.exists(f"worker:{worker_id}"))

    async def mark_running(self, job_id: str, stage: str, token: str) -> int:
        """Set the running marker. Returns the crash count for this stage (incremented if a previous
        run left its marker behind, i.e. died without cleanup)."""
        key = self.k(job_id)
        prev = await self.r.hget(key, f"running:{stage}")
        await self.r.hset(key, f"running:{stage}", token)
        if prev and prev != token:
            return int(await self.r.hincrby(key, f"crashes:{stage}", 1))
        return 0

    async def clear_running(self, job_id: str, stage: str) -> None:
        await self.r.hdel(self.k(job_id), f"running:{stage}")

    async def lease_holder(self, job_id: str, stage: str) -> str | None:
        return await self.r.get(f"lock:{job_id}:{stage}")

    # ------------------------------------------------------------------- dlq
    async def dlq_add(self, job_id: str, reason: dict) -> None:
        pipe = self.r.pipeline(transaction=False)
        pipe.zadd("dlq:index", {job_id: now()})
        pipe.hset(self.k(job_id), "dlq_reason", json.dumps(reason))
        await pipe.execute()

    async def dlq_list(self) -> list[str]:
        return await self.r.zrevrange("dlq:index", 0, 200)

    async def dlq_remove(self, job_id: str) -> None:
        await self.r.zrem("dlq:index", job_id)

    # ------------------------------------------------------------- telemetry
    async def incr(self, name: str, by: int = 1) -> None:
        await self.r.hincrby("metrics", name, by)

    # ---- transcript cache (by video id; avoids repeat YouTube caption requests) ----
    async def get_cached_transcript(self, video_id: str) -> dict | None:
        if settings.transcript_cache_days <= 0:
            return None
        raw = await self.r.get(f"transcript:{video_id}")
        return json.loads(raw) if raw else None

    async def cache_transcript(self, video_id: str, value: dict) -> None:
        if settings.transcript_cache_days > 0 and value.get("segments"):
            await self.r.set(f"transcript:{video_id}", json.dumps(value), ex=settings.transcript_cache_days * 86400)

    async def metrics(self) -> dict[str, float]:
        # Counters are HINCRBY ints; durations (stage_seconds:*, ratelimit_wait_sec:*) are HINCRBYFLOAT.
        out: dict[str, float] = {}
        for k, v in (await self.r.hgetall("metrics")).items():
            f = float(v)
            out[k] = int(f) if f.is_integer() else round(f, 3)
        return out

    async def heartbeat(self, worker_id: str, info: dict, ttl: int) -> None:
        await self.r.set(f"worker:{worker_id}", json.dumps(info), ex=ttl)

    async def workers(self) -> list[dict]:
        out = []
        async for key in self.r.scan_iter("worker:*", count=100):
            blob = await self.r.get(key)
            if blob:
                out.append(json.loads(blob))
        return sorted(out, key=lambda w: w.get("id", ""))
