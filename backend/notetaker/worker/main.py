"""Worker process: consumes stage messages, runs them under a lease, routes failures.

Delivery guarantees
  * prefetch=1 per stage consumer, manual ack. The ack is sent only after the stage's output
    is committed and the next stage's message is confirmed by the broker.
  * If the worker is SIGKILLed, the broker sees the connection drop and requeues the unacked
    message straight away. Another worker picks it up, sees the stale lease, defers for a few
    seconds until the lease expires, then resumes. Finished sub-steps are skipped because they
    are checkpointed.
  * A duplicate delivery of a stage that already finished (crash between commit and ack) only
    re-publishes the next stage. Duplicate deliveries of a running stage are deferred.
  * Messages that keep crashing workers (x-delivery-count >= CRASH_REDELIVERY_LIMIT) are
    dead-lettered so they can't crash-loop the pool.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import time
import uuid
from functools import partial

from aio_pika.abc import AbstractIncomingMessage

from ..broker import Broker, stage_queue
from ..config import settings
from ..errors import LeaseLost, Permanent, PipelineError, RateLimited, Retryable, StaleMessage
from ..faults import Faults
from ..llm import LLM
from ..models import NEXT_STAGE, TERMINAL, Status
from ..ratelimit import RateLimiter
from ..store import JobStore, now
from .pipeline import HANDLERS, Ctx

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
for noisy in ("httpx", "aiormq", "aio_pika", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("worker")


class Worker:
    def __init__(self) -> None:
        # In Docker the hostname is the container id, so `docker kill <hostname>` targets this worker.
        self.id = f"{socket.gethostname()}:{os.getpid()}"
        self.store = JobStore()
        self.broker = Broker()
        self.limiter = RateLimiter(self.store.r)
        self.faults = Faults(self.store.r)
        self.llm = LLM(self.limiter, self.faults, self.store.incr)
        self.inflight: dict[str, dict] = {}
        self.processed = 0
        self.started = time.time()
        self.stopping = asyncio.Event()
        self._consumers: list[tuple] = []

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        for attempt in range(60):
            try:
                await self.store.ping()
                await self.broker.connect(prefetch=1, client_name=f"worker-{self.id}")
                break
            except Exception as e:  # noqa: BLE001
                log.warning("waiting for broker/redis (%s): %s", attempt, e)
                await asyncio.sleep(2)
        else:
            raise SystemExit("could not connect to broker/redis")

        for stage in settings.worker_stages:
            ch = await self.broker.conn.channel()
            await ch.set_qos(prefetch_count=1)
            q = await ch.get_queue(stage_queue(stage), ensure=False)
            tag = await q.consume(partial(self.on_message, stage))
            self._consumers.append((q, tag))
        log.info("worker %s consuming stages %s", self.id, settings.worker_stages)

        hb = asyncio.create_task(self.heartbeat_loop())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)
        await self.stopping.wait()

        log.info("graceful shutdown: stop consuming, finish %d in-flight", len(self.inflight))
        for q, tag in self._consumers:
            try:
                await q.cancel(tag)
            except Exception:  # noqa: BLE001
                pass
        deadline = time.time() + 30
        while self.inflight and time.time() < deadline:
            await asyncio.sleep(0.5)
        hb.cancel()
        await self.store.r.delete(f"worker:{self.id}")
        await self.broker.close()
        await self.store.close()

    async def heartbeat_loop(self) -> None:
        while True:
            try:
                await self.store.heartbeat(self.id, {
                    "id": self.id, "host": socket.gethostname(), "pid": os.getpid(),
                    "stages": settings.worker_stages, "processed": self.processed,
                    "started_at": self.started, "seen_at": now(),
                    "inflight": [{"job_id": j, **v} for j, v in self.inflight.items()],
                }, ttl=settings.worker_heartbeat_sec * 2 + 1)
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat failed: %s", e)
            await asyncio.sleep(settings.worker_heartbeat_sec)

    async def renew_loop(self, job_id: str, stage: str, token: str) -> None:
        while True:
            await asyncio.sleep(settings.lease_seconds / 3)
            try:
                if not await self.store.renew_lease(job_id, stage, token):
                    log.warning("lease lost for %s/%s", job_id, stage)
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("lease renew error: %s", e)

    # ------------------------------------------------------------------ messages
    async def on_message(self, stage: str, message: AbstractIncomingMessage) -> None:
        try:
            body = json.loads(message.body)
            job_id = body["job_id"]
        except (ValueError, KeyError):
            log.error("malformed message on %s — rejecting to DLQ", stage)
            await message.reject(requeue=False)
            return
        try:
            await self.process(stage, body, message)
        except Exception:  # noqa: BLE001 — never let the consumer die; redeliver
            log.exception("unexpected error handling %s/%s — requeueing", job_id, stage)
            try:
                await asyncio.sleep(1)
                await message.nack(requeue=True)
            except Exception:  # noqa: BLE001
                pass

    async def process(self, stage: str, body: dict, message: AbstractIncomingMessage) -> None:
        job_id = body["job_id"]
        attempt = int(body.get("attempt", 0))
        rl_retries = int(body.get("rl_retries", 0))

        status = await self.store.get_status(job_id)
        if status is None or status in TERMINAL:
            await message.ack()
            return
        if stage in await self.store.done_stages(job_id):
            # Finished earlier; we crashed before ack. Make sure the next stage exists, then ack.
            await self.advance(job_id, stage)
            await message.ack()
            return

        token = f"{self.id}:{uuid.uuid4().hex[:8]}"
        acquired = await self.store.acquire_lease(job_id, stage, token)
        if not acquired:
            # Fast failover: if the lease holder's heartbeat is gone, it died (SIGKILL, OOM, node loss).
            holder = await self.store.lease_holder(job_id, stage)
            if holder and not await self.store.worker_alive(holder.rsplit(":", 1)[0]):
                acquired = await self.store.steal_lease(job_id, stage, holder, token)
                if acquired:
                    await self.store.add_event(job_id, "failover",
                                               f"Worker {holder.split(':')[0]} is dead (no heartbeat); "
                                               f"took over '{stage}' immediately")
        if not acquired:
            # Lease held: either a live duplicate is running, or a crashed worker's lease hasn't expired yet.
            holder = await self.store.lease_holder(job_id, stage)
            await self.broker.publish_stage(job_id, stage, attempt=attempt, rl_retries=rl_retries,
                                            delay=settings.defer_seconds, extra={"deferred": True})
            if message.redelivered:
                await self.store.add_event(job_id, "recovering",
                                           f"Previous worker ({(holder or '?').rsplit(':', 1)[0]}) stopped mid-'{stage}'; "
                                           f"waiting for its lease to expire before resuming")
            await message.ack()
            return

        # Crash accounting lives in Redis (not the broker's delivery count, which resets when we defer).
        # A `running:{stage}` marker that is still set means the previous run never reached `finally`.
        crashes = await self.store.mark_running(job_id, stage, token)
        if crashes:
            await self.store.incr("recoveries")
            await self.store.add_event(job_id, "resumed",
                                       f"Recovered from a worker crash: '{stage}' resumed on worker "
                                       f"{self.id.rsplit(':', 1)[0]} (crash #{crashes})")
            if crashes >= settings.crash_redelivery_limit:
                await self.dead_letter(job_id, stage, body, Ctx(self, job_id, stage, token, attempt, rl_retries), {
                    "code": "CRASH_LOOP", "message": f"Stage '{stage}' crashed workers {crashes} times; "
                                                     f"quarantined to avoid a crash loop"})
                await self.store.clear_running(job_id, stage)
                await self.store.release_lease(job_id, stage, token)
                await message.ack()
                return

        renew = asyncio.create_task(self.renew_loop(job_id, stage, token))
        self.inflight[job_id] = {"stage": stage, "since": now()}
        ctx = Ctx(self, job_id, stage, token, attempt, rl_retries)
        t0 = time.time()
        try:
            await HANDLERS[stage](ctx)
            await self.store.incr(f"stage_ok:{stage}")
            await self.store.r.hincrbyfloat("metrics", f"stage_seconds:{stage}", round(time.time() - t0, 3))
            log.info("✓ %s %s in %.1fs", job_id[:8], stage, time.time() - t0)
            await self.advance(job_id, stage)
            await message.ack()
        except (LeaseLost, StaleMessage) as e:
            log.warning("dropping %s/%s: %s", job_id[:8], stage, e)
            await message.ack()
        except RateLimited as e:
            await self.retry(job_id, stage, body, e, ctx)
            await message.ack()
        except Permanent as e:
            await self.fail(job_id, stage, e, ctx)
            await message.ack()
        except Retryable as e:
            await self.retry(job_id, stage, body, e, ctx)
            await message.ack()
        except Exception as e:  # noqa: BLE001 — unknown bugs are treated as transient (bounded)
            log.exception("stage %s crashed for %s", stage, job_id)
            await self.retry(job_id, stage, body, Retryable(f"{type(e).__name__}: {e}"[:300], code="INTERNAL_ERROR"), ctx)
            await message.ack()
        finally:
            renew.cancel()
            self.inflight.pop(job_id, None)
            self.processed += 1
            try:
                await self.store.clear_running(job_id, stage)
                await self.store.release_lease(job_id, stage, token)
            except Exception:  # noqa: BLE001
                pass

    async def advance(self, job_id: str, stage: str) -> None:
        nxt = NEXT_STAGE[stage]
        if nxt:
            await self.broker.publish_stage(job_id, nxt)

    # ------------------------------------------------------------------ outcomes
    async def retry(self, job_id: str, stage: str, body: dict, err: Retryable, ctx: Ctx) -> None:
        attempt = int(body.get("attempt", 0))
        rl = int(body.get("rl_retries", 0))
        if isinstance(err, RateLimited):
            rl += 1
            budget, used = settings.max_rate_limit_retries, rl
            delay = max(settings.backoff_for(rl), err.retry_after or 0)
            await self.store.incr(f"rate_limited:{err.service}")
        else:
            attempt += 1
            budget, used = settings.max_attempts - 1, attempt
            delay = settings.backoff_for(attempt)
        if used > budget:
            await self.dead_letter(job_id, stage, body, ctx, {
                **err.to_dict(), "message": f"Retry budget exhausted after {used} attempts: {err.message}"})
            return
        delay = settings.tier_at_least(delay)
        info = {"code": err.code, "message": err.message, "service": getattr(err, "service", None),
                "retry_at": time.time() + delay, "delay_sec": delay, "attempt": used, "max_attempts": budget,
                "kind": "rate_limit" if isinstance(err, RateLimited) else "error"}
        res = await self.store.set_fields(job_id, lease=ctx.lease, retry=info, attempt=attempt, waiting=None)
        if not res.ok:
            return
        await self.store.add_event(job_id, "retry_scheduled",
                                   f"{err.message} — retry {used}/{budget} of '{stage}' in {delay}s")
        await self.broker.publish_stage(job_id, stage, attempt=attempt, rl_retries=rl, delay=delay)
        log.info("↻ %s %s retry %d/%d in %ss (%s)", job_id[:8], stage, used, budget, delay, err.code)

    async def fail(self, job_id: str, stage: str, err: PipelineError, ctx: Ctx) -> None:
        res = await self.store.commit(job_id, target=Status.FAILED, lease=ctx.lease, worker=self.id,
                                      fields={"error": {**err.to_dict(), "stage": stage}, "retry": None, "waiting": None})
        log.info("✗ %s FAILED in %s: %s", job_id[:8], stage, err.code)
        if res.ok:
            await self.store.add_event(job_id, "failed", err.message)

    async def dead_letter(self, job_id: str, stage: str, body: dict, ctx: Ctx | None, reason: dict) -> None:
        reason = {**reason, "stage": stage}
        res = await self.store.commit(job_id, target=Status.DEAD_LETTERED, worker=self.id,
                                      lease=ctx.lease if ctx else None,
                                      fields={"error": reason, "retry": None, "waiting": None})
        if not res.ok:
            return
        await self.broker.publish_dlq(body, reason)
        await self.store.dlq_add(job_id, reason)
        await self.store.add_event(job_id, "dead_lettered", reason["message"])
        log.info("☠ %s DEAD_LETTERED in %s: %s", job_id[:8], stage, reason.get("code"))


def main() -> None:
    asyncio.run(Worker().run())


if __name__ == "__main__":
    main()
