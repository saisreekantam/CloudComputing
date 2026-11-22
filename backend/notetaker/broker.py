"""RabbitMQ topology and publishing.

  nt.work   (direct)   --stage-->  nt.stage.{transcribe|summarize|frames|publish}   quorum, durable
                                    x-delivery-limit → nt.dlx (backstop against poison messages)
  nt.retry  (headers)  --delay_ms-> nt.retry.{N}ms   classic, x-message-ttl=N,
                                    dead-letters back to nt.work keeping the original routing key
  nt.dlx    (fanout)   -->          nt.dlq           durable, kept for inspection / replay

Messages are persistent and published with publisher confirms. A stage's message is acked
only after the stage's output is committed to Redis and the next stage's message is confirmed,
so a crash at any point leads to redelivery and never to loss.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message

from .config import settings
from .models import STAGES

log = logging.getLogger(__name__)

WORK_EX = "nt.work"
RETRY_EX = "nt.retry"
DLX = "nt.dlx"
DLQ = "nt.dlq"


def stage_queue(stage: str) -> str:
    return f"nt.stage.{stage}"


def retry_queue(seconds: int) -> str:
    return f"nt.retry.{seconds * 1000}ms"


class Broker:
    def __init__(self, url: str | None = None):
        self.url = url or settings.amqp_url
        self.conn: aio_pika.abc.AbstractRobustConnection | None = None
        self.channel: aio_pika.abc.AbstractChannel | None = None
        self.work_ex = self.retry_ex = self.dlx = None

    async def connect(self, prefetch: int = 1, client_name: str = "notetaker") -> None:
        self.conn = await aio_pika.connect_robust(
            self.url, heartbeat=30, client_properties={"connection_name": client_name})
        self.channel = await self.conn.channel(publisher_confirms=True)
        await self.channel.set_qos(prefetch_count=prefetch)
        await self.declare()

    async def declare(self) -> None:
        ch = self.channel
        self.work_ex = await ch.declare_exchange(WORK_EX, ExchangeType.DIRECT, durable=True)
        self.retry_ex = await ch.declare_exchange(RETRY_EX, ExchangeType.HEADERS, durable=True)
        self.dlx = await ch.declare_exchange(DLX, ExchangeType.FANOUT, durable=True)

        dlq = await ch.declare_queue(DLQ, durable=True)
        await dlq.bind(self.dlx)

        for stage in STAGES:
            q = await ch.declare_queue(stage_queue(stage), durable=True, arguments={
                "x-queue-type": "quorum",
                "x-dead-letter-exchange": DLX,
                "x-delivery-limit": settings.broker_delivery_limit,
            })
            await q.bind(self.work_ex, routing_key=stage)

        for seconds in settings.delay_tiers:
            ms = seconds * 1000
            q = await ch.declare_queue(retry_queue(seconds), durable=True, arguments={
                "x-message-ttl": ms,
                "x-dead-letter-exchange": WORK_EX,
            })
            await q.bind(self.retry_ex, arguments={"x-match": "all", "delay_ms": str(ms)})

    async def close(self) -> None:
        if self.conn and not self.conn.is_closed:
            await self.conn.close()

    @property
    def is_open(self) -> bool:
        return bool(self.conn and not self.conn.is_closed and self.channel and not self.channel.is_closed)

    @staticmethod
    def _message(body: dict, headers: dict | None = None) -> Message:
        return Message(
            json.dumps(body).encode(),
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
            message_id=f"{body.get('job_id')}:{body.get('stage')}:{body.get('attempt', 0)}",
            headers=headers or {},
        )

    async def publish_stage(self, job_id: str, stage: str, *, attempt: int = 0,
                            rl_retries: int = 0, delay: int = 0, extra: dict[str, Any] | None = None) -> None:
        body = {"job_id": job_id, "stage": stage, "attempt": attempt, "rl_retries": rl_retries, **(extra or {})}
        if delay > 0:
            tier = settings.tier_at_least(delay)
            await self.retry_ex.publish(self._message(body, {"delay_ms": str(tier * 1000)}), routing_key=stage)
        else:
            await self.work_ex.publish(self._message(body), routing_key=stage)

    async def publish_dlq(self, body: dict, reason: dict) -> None:
        await self.dlx.publish(self._message({**body, "dead_letter_reason": reason}), routing_key="dlq")

    async def queue_depths(self) -> dict[str, int]:
        """Ready-message counts via passive declare (works without the management plugin)."""
        out = {}
        # Separate channel: a failed passive declare closes the channel it ran on.
        ch = await self.conn.channel()
        try:
            for name in [stage_queue(s) for s in STAGES] + [DLQ] + [retry_queue(s) for s in settings.delay_tiers]:
                try:
                    q = await ch.declare_queue(name, passive=True)
                    out[name] = q.declaration_result.message_count
                except Exception:  # noqa: BLE001
                    out[name] = -1
                    if ch.is_closed:
                        ch = await self.conn.channel()
        finally:
            if not ch.is_closed:
                await ch.close()
        return out

    async def drain_dlq_for(self, job_id: str, max_scan: int = 500) -> int:
        """Remove DLQ messages belonging to `job_id` (used by replay). Others are requeued."""
        ch = await self.conn.channel()
        try:
            q = await ch.declare_queue(DLQ, passive=True)
            removed, held = 0, []
            for _ in range(max_scan):
                msg = await q.get(no_ack=False, fail=False)
                if msg is None:
                    break
                try:
                    body = json.loads(msg.body)
                except ValueError:
                    body = {}
                if body.get("job_id") == job_id:
                    await msg.ack()
                    removed += 1
                else:
                    held.append(msg)
            for msg in held:
                await msg.nack(requeue=True)
            return removed
        finally:
            await ch.close()
