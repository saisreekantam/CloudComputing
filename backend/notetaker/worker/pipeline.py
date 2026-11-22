"""The four pipeline stages. Each stage:

  1. moves the job into its status (idempotent, lease-fenced),
  2. does its work, checkpointing partial progress (per LLM chunk, per frame) so a crash or a
     429 does not redo finished sub-steps,
  3. commits its output + 'stage done' + the next status in ONE atomic Redis step.

The runtime (main.py) acks the broker message only after step 3 and after the next stage's
message is confirmed by the broker.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import youtube
from ..config import settings
from ..errors import LeaseLost, Permanent, RateLimited, Retryable, StaleMessage
from ..frames import FrameError, StreamExpired, capture_best, phash_distance, vision_jpeg_b64
from ..llm import chunk_lines, merge_parts
from ..models import STAGE_STATUS, Status, Warn
from ..notion import NotionClient, build_blocks, marker_block
from ..ratelimit import youtube_buckets
from ..store import CommitResult

if TYPE_CHECKING:
    from .main import Worker

log = logging.getLogger(__name__)


@dataclass
class Ctx:
    w: "Worker"
    job_id: str
    stage: str
    token: str
    attempt: int
    rl_retries: int

    @property
    def lease(self) -> tuple[str, str]:
        return (f"lock:{self.job_id}:{self.stage}", self.token)

    @property
    def store(self):
        return self.w.store

    def check(self, res: CommitResult) -> None:
        if res.ok:
            return
        if res.code == "LOST_LEASE":
            raise LeaseLost(self.job_id)
        raise StaleMessage(f"{self.job_id}: commit refused ({res.code}, status={res.previous})")

    async def enter(self) -> None:
        """Move into this stage's status and record who is working on it."""
        res = await self.store.commit(self.job_id, target=STAGE_STATUS[self.stage], lease=self.lease,
                                      worker=self.w.id, fields={"stage": self.stage, "worker": self.w.id,
                                                                "retry": None, "waiting": None})
        self.check(res)

    async def progress(self, pct: float) -> None:
        self.check(await self.store.set_fields(self.job_id, lease=self.lease, progress=int(pct), waiting=None))

    async def on_wait(self, seconds: float, service: str = "llm") -> None:
        await self.store.set_fields(self.job_id, lease=self.lease, waiting={
            "reason": "RATE_LIMIT_SLOT", "service": service, "until": time.time() + seconds})

    def waiter(self, service: str):
        async def _w(seconds: float) -> None:
            await self.on_wait(seconds, service)
        return _w

    async def maybe_crash(self) -> None:
        """Injected fault: hard-exit mid-stage like SIGKILL (no ack, no lease release)."""
        if await self.w.faults.take_crash(self.stage):
            log.error("💥 injected crash in stage %s for job %s — exiting with 137", self.stage, self.job_id)
            await self.store.add_event(self.job_id, "crash_injected", f"Worker {self.w.id} crashed mid-'{self.stage}' (injected)")
            os._exit(137)

    async def finish(self, next_status: Status, *, fields: dict | None = None, outputs: dict | None = None) -> None:
        res = await self.store.commit(self.job_id, target=next_status, fields={**(fields or {}), "waiting": None},
                                      outputs=outputs, done_stage=self.stage, lease=self.lease, worker=self.w.id)
        self.check(res)


# =========================================================================== transcribe
async def transcribe(ctx: Ctx) -> None:
    await ctx.enter()
    raw = await ctx.store.get_raw(ctx.job_id)
    video_id = raw["video_id"]
    warnings: list[dict] = []

    if await ctx.w.faults.consume("youtube_429"):
        await ctx.store.incr("youtube_injected_429")
        raise RateLimited("youtube", None, injected=True, message="YouTube returned 429 (injected fault)")

    await ctx.w.limiter.acquire("youtube", youtube_buckets(), on_wait=ctx.waiter("youtube"))
    try:
        meta = await asyncio.to_thread(youtube.fetch_metadata, video_id)
    except Retryable as e:
        if isinstance(e, RateLimited) or e.code != "YOUTUBE_ERROR":
            raise
        # yt-dlp broke for a non-fatal reason: carry on with the transcript, frames will degrade.
        log.warning("metadata degraded for %s: %s", video_id, e.message)
        meta = {"title": await asyncio.to_thread(youtube.fetch_oembed_title, video_id), "duration": 0,
                "channel": "", "live_status": "not_live", "stream": None, "caption_tracks": []}
        warnings.append({"code": Warn.METADATA_DEGRADED, "message": "Video metadata partially unavailable"})

    if meta["live_status"] in ("is_live", "is_upcoming"):
        raise Permanent("Live streams and premieres are not supported", code="LIVE_NOT_SUPPORTED")
    duration = int(meta["duration"] or 0)
    if duration > settings.max_video_seconds:  # Constraint A — deterministic rejection, no further work
        raise Permanent(f"Video is {youtube.fmt_ts(duration)} long; the limit is "
                        f"{youtube.fmt_ts(settings.max_video_seconds)}", code="VIDEO_TOO_LONG",
                        duration_sec=duration)
    await ctx.store.set_fields(ctx.job_id, lease=ctx.lease, video_title=meta["title"],
                               duration_sec=duration, channel=meta["channel"], progress=12)
    await ctx.maybe_crash()

    cached = await ctx.store.get_cached_transcript(video_id)
    if cached:
        segments, tinfo = cached["segments"], {**cached["info"], "cached": True}
    else:
        await ctx.w.limiter.acquire("youtube", youtube_buckets(), on_wait=ctx.waiter("youtube"))
        segments, tinfo = await asyncio.to_thread(youtube.fetch_transcript, video_id, meta.get("caption_tracks"),
                                                  meta.get("audio_stream"))
        await ctx.store.cache_transcript(video_id, {"segments": segments, "info": tinfo})
    if not duration:
        duration = youtube.transcript_duration(segments)
        if duration > settings.max_video_seconds:
            raise Permanent(f"Video is about {youtube.fmt_ts(duration)} long; the limit is "
                            f"{youtube.fmt_ts(settings.max_video_seconds)}", code="VIDEO_TOO_LONG")
    if tinfo.get("cached"):
        await ctx.store.add_event(ctx.job_id, "info", "Transcript reused from cache (no YouTube caption request)")
    elif tinfo.get("asr"):
        await ctx.store.add_event(ctx.job_id, "info",
                                  f"Captions unavailable ({tinfo.get('captions_error')}); transcribed the audio with "
                                  f"{tinfo['source']} in {tinfo.get('asr_seconds')}s")
    elif tinfo.get("fallback"):
        await ctx.store.add_event(ctx.job_id, "info", "Transcript fetched via caption-track fallback")
    if tinfo.get("translated_from"):
        await ctx.store.add_event(ctx.job_id, "info", f"Transcript translated from '{tinfo['translated_from']}'")

    await ctx.finish(Status.SUMMARIZING, fields={
        "video_title": meta["title"] or f"YouTube video {video_id}", "duration_sec": duration,
        "channel": meta["channel"], "progress": 25, "warnings:transcribe": warnings,
        "meta": {"transcript_source": tinfo.get("source"), "language": tinfo.get("language"),
                 "generated_captions": tinfo.get("generated"), "segments": len(segments)},
    }, outputs={"transcript": {"segments": segments, "info": tinfo}, "stream": meta.get("stream"),
                "scan_stream": meta.get("scan_stream")})


# =========================================================================== summarize
def _context_for(segments: list[dict], t: float, before: float = 10, after: float = 15) -> str:
    return " ".join(s["text"] for s in segments if t - before <= s["start"] <= t + after)[:600]


async def summarize(ctx: Ctx) -> None:
    await ctx.enter()
    raw = await ctx.store.get_raw(ctx.job_id)
    tr = await ctx.store.load_ckpt(ctx.job_id, "transcript")
    if not tr:
        raise Retryable("transcript checkpoint missing", code="CHECKPOINT_MISSING")
    title = raw.get("video_title") or ""
    duration = int(float(raw.get("duration_sec") or 0)) or youtube.transcript_duration(tr["segments"])
    lines = youtube.to_lines(tr["segments"])
    chunks = chunk_lines(lines, settings.llm_chunk_chars)
    n = len(chunks)
    # Map step in parallel: chunks are independent (the global rate limiter still spaces every call,
    # and each chunk is checkpointed on its own, so a crash only redoes unfinished chunks).
    sem = asyncio.Semaphore(max(1, settings.llm_concurrency))
    done = {"n": 0}

    async def one(i: int, chunk: list[dict]) -> dict:
        key = f"chunk:{i}of{n}"
        part = await ctx.store.load_ckpt(ctx.job_id, key)
        if part is None:
            end_sec = int(chunks[i + 1][0]["start"]) if i + 1 < n else duration
            async with sem:
                part = await ctx.w.llm.synthesize_chunk(title, duration, chunk, i, n, end_sec, want_global=(n == 1),
                                                        on_wait=ctx.waiter("llm"))
            await ctx.store.save_ckpt(ctx.job_id, key, part)
        done["n"] += 1
        await ctx.progress(25 + 25 * done["n"] / n)
        return part

    tasks = [asyncio.create_task(one(i, c)) for i, c in enumerate(chunks)]
    try:
        parts = list(await asyncio.gather(*tasks))
    except BaseException:
        for t in tasks:
            t.cancel()
        raise
    await ctx.maybe_crash()

    synthesis, warnings = merge_parts(parts, duration, settings.max_frames, min_frames=settings.min_frames)
    for m in synthesis["visual_moments"]:
        m["context"] = _context_for(tr["segments"], m["timestamp_sec"])

    # Reduce (TL;DR + review questions) and the concept graph only need the merged sections: run both at once.
    async def do_reduce() -> dict | None:
        if not (n > 1 or not synthesis.get("tldr")):  # also when a single chunk had to be split
            return None
        red = await ctx.store.load_ckpt(ctx.job_id, "reduce")
        if red is None:
            red = await ctx.w.llm.reduce(title, synthesis["sections"], on_wait=ctx.waiter("llm"))
            await ctx.store.save_ckpt(ctx.job_id, "reduce", red)
        return red

    async def do_graph() -> dict | None:  # beyond spec; a failure only drops the concept map
        if not settings.concept_graph:
            return None
        graph = await ctx.store.load_ckpt(ctx.job_id, "graph")
        if graph is None:
            try:
                graph = await ctx.w.llm.concept_graph(title, synthesis, duration, on_wait=ctx.waiter("llm"))
            except RateLimited:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("concept graph skipped: %r", e)
                graph = {"concepts": [], "relations": []}
            await ctx.store.save_ckpt(ctx.job_id, "graph", graph)
        return graph

    red, graph = await asyncio.gather(do_reduce(), do_graph())
    if red:
        synthesis.update(red)
    if graph is not None:
        synthesis["graph"] = graph
        if graph.get("concepts"):
            await ctx.store.add_event(ctx.job_id, "info",
                                      f"Concept graph: {len(graph['concepts'])} concepts, "
                                      f"{len(graph['relations'])} relations"
                                      + (f" ({graph['dropped_by_teaching_order']} contradicting teaching order dropped)"
                                         if graph.get("dropped_by_teaching_order") else ""))

    await ctx.finish(Status.EXTRACTING_FRAMES, fields={
        "progress": 60, "warnings:summarize": warnings,
        "frames_requested": len(synthesis["visual_moments"]),
    }, outputs={"synthesis": synthesis})


# =========================================================================== frames
async def _fresh_stream(ctx: Ctx, video_id: str) -> dict | None:
    await ctx.w.limiter.acquire("youtube", youtube_buckets(), on_wait=ctx.waiter("youtube"))
    meta = await asyncio.to_thread(youtube.fetch_metadata, video_id)
    if meta.get("stream"):
        await ctx.store.save_ckpt(ctx.job_id, "stream", meta["stream"])
    if meta.get("scan_stream"):
        await ctx.store.save_ckpt(ctx.job_id, "scan_stream", meta["scan_stream"])
    return meta.get("stream")


async def _visual_candidates(ctx: Ctx, video_id: str, title: str, duration: int,
                             rl_budget_left: bool) -> list[dict] | None:
    """Scan the video, score visual states, and let the vision model rate the best 16.
    Returns None when the scan is unavailable (callers fall back to transcript-only moments)."""
    from .. import visual
    cached = await ctx.store.load_ckpt(ctx.job_id, "visual_candidates")
    if cached is not None:
        return cached.get("cands")
    if not settings.visual_scan:
        return None
    t0 = time.time()
    try:
        scan_stream = await ctx.store.load_ckpt(ctx.job_id, "scan_stream")
        if not scan_stream:
            await _fresh_stream(ctx, video_id)
            scan_stream = await ctx.store.load_ckpt(ctx.job_id, "scan_stream")
        if not scan_stream:
            raise FrameError("no low-resolution stream to scan")
        try:
            thumbs = await asyncio.to_thread(visual.scan, scan_stream, settings.frame_timeout_sec * 3)
        except StreamExpired:
            await _fresh_stream(ctx, video_id)
            thumbs = await asyncio.to_thread(visual.scan, await ctx.store.load_ckpt(ctx.job_id, "scan_stream"),
                                             settings.frame_timeout_sec * 3)
        shots = visual.build_shots(thumbs, duration)
        baseline = visual.score_shots(shots, thumbs)
        top = visual.top_candidates(shots, thumbs, settings.visual_candidates)
    except RateLimited:
        if rl_budget_left:
            raise
        top, thumbs, baseline = None, [], None
    except Exception as e:  # noqa: BLE001 — the scan improves frame choice; it must never fail the job
        log.warning("visual scan failed: %r", e)
        top, thumbs, baseline = None, [], None
    if not top:
        await ctx.store.add_event(ctx.job_id, "info", "Visual scan unavailable; choosing frames from the transcript")
        await ctx.store.save_ckpt(ctx.job_id, "visual_candidates", {"cands": None})
        return None

    cands = [{**s.to_dict(thumbs), "rating": None, "kind": "", "shows": ""} for s in top]
    rated = False
    if settings.vision_verify:
        try:
            sheet = visual.contact_sheet([thumbs[s.rep].img for s in top])
            ratings = await ctx.w.llm.rate_contact_sheet(vision_jpeg_b64(sheet, max_side=1600), len(top), title,
                                                         on_wait=ctx.waiter("llm"))
            for r in ratings:
                cands[r["index"]].update(rating=r["score"], kind=r["kind"], shows=r["shows"])
            rated = bool(ratings)
        except RateLimited:
            if rl_budget_left:
                raise
        except Exception as e:  # noqa: BLE001
            log.warning("visual triage failed: %r", e)
    for c in cands:  # unrated (no vision / failed triage): fall back to the heuristic score
        if c["rating"] is None:
            c["rating"] = 3 if c["score"] >= 0.7 else 2 if c["score"] >= 0.5 else 1
    good = sum(1 for c in cands if c["rating"] >= 2)
    await ctx.store.add_event(
        ctx.job_id, "info",
        f"Visual scan: {len(thumbs)} keyframes → {len(cands)} candidate visuals, {good} rated informative"
        f"{' by the vision model' if rated else ''} ({time.time() - t0:.1f}s"
        f"{', presenter shot detected' if baseline is not None else ''})")
    await ctx.store.incr("visual_scans")
    await ctx.store.save_ckpt(ctx.job_id, "visual_candidates", {"cands": cands})
    return cands


async def _plan_frames(ctx: Ctx, raw: dict, synthesis: dict, duration: int, rl_budget_left: bool) -> list[dict]:
    from ..visual import select_frames
    plan = await ctx.store.load_ckpt(ctx.job_id, "frame_plan")
    if plan is not None:
        return plan
    cands = await _visual_candidates(ctx, raw["video_id"], raw.get("video_title") or "", duration, rl_budget_left)
    plan = select_frames(synthesis["visual_moments"], cands, synthesis["sections"], settings.max_frames,
                         settings.min_frames)
    tr = await ctx.store.load_ckpt(ctx.job_id, "transcript")
    if tr:
        for m in plan:
            m["context"] = _context_for(tr["segments"], m["timestamp_sec"])
    await ctx.store.save_ckpt(ctx.job_id, "frame_plan", plan)
    return plan


async def frames(ctx: Ctx) -> None:
    await ctx.enter()
    raw = await ctx.store.get_raw(ctx.job_id)
    video_id = raw["video_id"]
    duration = int(float(raw.get("duration_sec") or 0))
    synthesis = await ctx.store.load_ckpt(ctx.job_id, "synthesis")
    if not synthesis:
        raise Retryable("synthesis checkpoint missing", code="CHECKPOINT_MISSING")
    warnings: list[dict] = []
    rl_budget_left = ctx.rl_retries < settings.max_rate_limit_retries - 1
    moments = await _plan_frames(ctx, raw, synthesis, duration, rl_budget_left)
    await ctx.progress(64)

    stream = await ctx.store.load_ckpt(ctx.job_id, "stream")
    stream_error = None
    if moments and (not stream or stream.get("expire", 0) - time.time() < 900):
        try:
            stream = await _fresh_stream(ctx, video_id)
        except RateLimited:
            if rl_budget_left:
                raise
            stream_error = "YouTube kept rate-limiting the video stream"
        except Exception as e:  # noqa: BLE001 — degrade, never fail the job for frames
            stream_error = f"could not resolve video stream: {getattr(e, 'message', e)}"
            stream = None
        if not stream and not stream_error:
            stream_error = "no downloadable video stream"

    # Capture frames concurrently (bounded); each frame is checkpointed on its own, so a crash
    # mid-stage only re-captures the frames that hadn't finished.
    shared = {"stream": stream, "refreshed": False, "done": 0}
    refresh_lock = asyncio.Lock()
    sem = asyncio.Semaphore(settings.frame_concurrency)

    async def capture_one(i: int, m: dict) -> dict:
        meta = await ctx.store.load_ckpt(ctx.job_id, f"frame_meta:{i}")
        if meta is not None:
            return meta
        meta = {"idx": i, "requested_ts": m["timestamp_sec"], "caption": m["caption"], "reason": m["reason"],
                "kind": m["kind"], "section_index": m.get("section_index", 0), "context": m.get("context", ""),
                "source": m.get("source", "llm"), "visual_shows": m.get("visual_shows", ""),
                "dwell_sec": m.get("dwell_sec"), "ok": False}
        async with sem:
            if stream_error:
                meta["error"] = stream_error
            elif await ctx.w.faults.frame_should_fail():
                meta["error"] = "frame capture failed (injected fault)"
            else:
                for _try in range(2):
                    used = shared["stream"]
                    try:
                        f = await asyncio.to_thread(capture_best, used, float(m["timestamp_sec"]), duration or 10**6,
                                                    m.get("offsets") or settings.frame_candidate_offsets,
                                                    settings.frame_timeout_sec)
                        await ctx.store.save_frame(ctx.job_id, i, f.jpeg)
                        meta.update(ok=True, timestamp=f.timestamp, width=f.width, height=f.height,
                                    sharpness=round(f.sharpness, 1), phash=f.phash, bytes=len(f.jpeg))
                        break
                    except StreamExpired as e:
                        async with refresh_lock:
                            if shared["stream"] is used:  # nobody refreshed it yet
                                if shared["refreshed"]:
                                    meta["error"] = str(e)
                                    break
                                shared["refreshed"] = True
                                shared["stream"] = await _fresh_stream(ctx, video_id)
                        if not shared["stream"]:
                            meta["error"] = "video stream unavailable"
                            break
                    except RateLimited:
                        if rl_budget_left:
                            raise  # finished frames are checkpointed; the stage retries later
                        meta["error"] = "YouTube rate-limited the video stream"
                        break
                    except FrameError as e:
                        meta["error"] = str(e)[:300]
                        break
        await ctx.store.save_ckpt(ctx.job_id, f"frame_meta:{i}", meta)
        shared["done"] += 1
        await ctx.progress(64 + 16 * shared["done"] / max(len(moments), 1))
        if shared["done"] == 1:
            await ctx.maybe_crash()
        return meta

    tasks = [asyncio.create_task(capture_one(i, m)) for i, m in enumerate(moments)]
    try:
        results: list[dict] = list(await asyncio.gather(*tasks))
    except BaseException:
        for t in tasks:
            t.cancel()
        raise

    # Near-duplicate slides (same slide flagged twice) → keep the first.
    kept: list[dict] = []
    for meta in results:
        if not meta["ok"]:
            continue
        dup = next((k for k in kept if phash_distance(k["phash"], meta["phash"]) <= settings.phash_dup_threshold), None)
        if dup:
            meta["ok"] = False
            meta["duplicate_of"] = dup["idx"]
            warnings.append({"code": Warn.FRAME_DUPLICATE_DROPPED, "timestamp_sec": meta["requested_ts"],
                             "message": f"Same visual as the frame at {youtube.fmt_ts(dup['timestamp'])}"})
        else:
            kept.append(meta)

    for meta in results:
        if not meta["ok"] and "duplicate_of" not in meta:
            warnings.append({"code": Warn.FRAME_EXTRACTION_FAILED, "timestamp_sec": meta["requested_ts"],
                             "message": meta.get("error", "capture failed")})

    # Vision-grounded captions: one batched multimodal call over the real frames.
    if kept and settings.vision_verify:
        vision = await ctx.store.load_ckpt(ctx.job_id, "vision")
        if vision is None:
            try:
                images = [vision_jpeg_b64(await ctx.store.load_frame(ctx.job_id, k["idx"])) for k in kept]
                items = [{"timestamp": k["timestamp"], "caption": k["caption"], "reason": k["reason"],
                          "context": k.get("context", "")} for k in kept]
                vision = await ctx.w.llm.caption_frames(items, images, on_wait=ctx.waiter("llm"))
                await ctx.store.save_ckpt(ctx.job_id, "vision", vision)
            except RateLimited:
                if rl_budget_left:
                    raise
                vision = []
            except Exception as e:  # noqa: BLE001 — captions from synthesis are still LLM-written
                log.warning("vision captioning skipped: %r", e)
                await ctx.store.add_event(ctx.job_id, "info", "Vision caption check skipped; using transcript-based captions")
                vision = []
        for v in vision:
            k = kept[v["index"]]
            k["caption_source"] = "vision"
            k["transcript_caption"] = k["caption"]
            k["caption"] = v["caption"]
            if v.get("reason"):
                k["reason"] = v["reason"]
            k["shows_expected_visual"] = v.get("shows_expected_visual", True)

    # Section-anchor frames only carry a placeholder caption until the vision model describes them;
    # without a grounded caption they would violate Constraint B, so drop them instead.
    for k in kept:
        if k.get("source") == "section_anchor" and k.get("caption_source") != "vision":
            k["ok"] = False
            k["anchor_dropped"] = True

    frame_list = [{k: v for k, v in r.items() if k != "context"} for r in results]
    await ctx.finish(Status.PUBLISHING, fields={"progress": 85, "warnings:frames": warnings},
                     outputs={"frames": frame_list})


# =========================================================================== publish
async def _resolve_parent(ctx: Ctx, client: NotionClient, parent_id: str, token: str) -> dict:
    cached = await ctx.store.load_ckpt(ctx.job_id, "parent")
    if cached:
        return cached
    if parent_id or settings.notion_default_parent_id:
        parent = await client.resolve_parent(parent_id)
    else:
        # Default hub page: serialize creation per Notion workspace so concurrent jobs don't create two hubs.
        th = hashlib.sha256(token.encode()).hexdigest()[:16]
        hub_key = f"notion:hub:{th}"
        hub = await ctx.store.r.get(hub_key)
        if hub:
            parent = {"type": "page_id", "page_id": hub}
        else:
            lock = f"lock:hub:{th}"
            for _ in range(30):
                if await ctx.store.r.set(lock, ctx.w.id, nx=True, ex=30):
                    break
                await asyncio.sleep(1)
            try:
                hub = await ctx.store.r.get(hub_key)
                parent = {"type": "page_id", "page_id": hub} if hub else await client.resolve_parent(None)
                await ctx.store.r.set(hub_key, parent["page_id"], ex=30 * 86400)
            finally:
                await ctx.store.r.delete(lock)
    await ctx.store.save_ckpt(ctx.job_id, "parent", parent)
    return parent


async def _related_pages(ctx: Ctx, client: NotionClient, token: str, video_id: str, graph: dict) -> list[dict]:
    """Earlier pages in this workspace sharing concepts; pages since archived/deleted are forgotten."""
    from ..graph import forget_page, related_pages
    if not graph.get("concepts"):
        return []
    try:
        out = []
        for e in await related_pages(ctx.store.r, token, video_id, graph, limit=8):
            try:
                p = await client.request("GET", f"/pages/{e['page_id']}")
                alive = not (p.get("archived") or p.get("in_trash"))
            except (Permanent, Retryable):
                alive = False
            if alive:
                out.append(e)
            else:
                await forget_page(ctx.store.r, token, e["video_id"])
            if len(out) >= 5:
                break
        return out
    except RateLimited:
        raise
    except Exception as e:  # noqa: BLE001 — cross-links are a nicety
        log.warning("related pages lookup failed: %r", e)
        return []


async def publish(ctx: Ctx) -> None:
    await ctx.enter()
    raw = await ctx.store.get_raw(ctx.job_id)
    token = await ctx.store.get_token(ctx.job_id)
    if not token:
        raise Permanent("Notion token missing or unreadable — reconnect Notion and resubmit", code="NOTION_TOKEN_MISSING")
    synthesis = await ctx.store.load_ckpt(ctx.job_id, "synthesis")
    frame_list = await ctx.store.load_ckpt(ctx.job_id, "frames")
    if synthesis is None or frame_list is None:
        raise Retryable("checkpoint missing", code="CHECKPOINT_MISSING")
    meta = {"channel": raw.get("channel")}
    job = {"job_id": ctx.job_id, "video_id": raw["video_id"], "video_title": raw.get("video_title"),
           "duration_sec": raw.get("duration_sec")}
    title = raw.get("video_title") or f"YouTube video {raw['video_id']}"
    warnings: list[dict] = []

    client = NotionClient(token, ctx.w.limiter, ctx.w.faults)
    try:
        parent = await _resolve_parent(ctx, client, raw.get("notion_parent_id", ""), token)
        await ctx.progress(87)

        # Reconcile: archive any page an earlier (crashed) attempt of THIS job created.
        stale = await client.find_marked_pages(parent, title, ctx.job_id)
        for pid in stale:
            await client.archive(pid)
        if stale:
            await ctx.store.add_event(ctx.job_id, "reconciled",
                                      f"Archived {len(stale)} partial page(s) from an interrupted attempt")
        page = await client.create_page(parent, title, [marker_block(job, meta)])
        await ctx.store.set_fields(ctx.job_id, lease=ctx.lease, notion_page_id=page["id"], progress=90)
        await ctx.maybe_crash()

        upload_ids: dict[int, str] = {}
        ok_frames = [f for f in frame_list if f.get("ok")]
        for n, f in enumerate(ok_frames):
            data = await ctx.store.load_frame(ctx.job_id, f["idx"])
            if not data:
                warnings.append({"code": Warn.FRAME_UPLOAD_FAILED, "timestamp_sec": f["requested_ts"],
                                 "message": "captured frame expired before publishing"})
                continue
            try:
                upload_ids[f["idx"]] = await client.upload_image(data, f"frame_{int(f['timestamp'])}s.jpg")
            except (RateLimited, LeaseLost):
                raise
            except (Permanent, Retryable) as e:
                if e.code == "NOTION_UNAUTHORIZED":
                    raise
                warnings.append({"code": Warn.FRAME_UPLOAD_FAILED, "timestamp_sec": f["requested_ts"],
                                 "message": e.message[:200]})
            await ctx.progress(90 + 6 * (n + 1) / max(len(ok_frames), 1))

        omitted = [{"timestamp_sec": f["requested_ts"]} for f in frame_list
                   if (not f.get("ok") and "duplicate_of" not in f and not f.get("anchor_dropped"))
                   or (f.get("ok") and f["idx"] not in upload_ids)]
        related = await _related_pages(ctx, client, token, raw["video_id"], synthesis.get("graph") or {})
        blocks = build_blocks(job, synthesis, frame_list, upload_ids, omitted, related)
        await client.append(page["id"], blocks)
        if (synthesis.get("graph") or {}).get("concepts"):
            from ..graph import register_page
            await register_page(ctx.store.r, token, raw["video_id"], page, title, synthesis["graph"])
    finally:
        await client.close()

    prior = await ctx.store.warnings(ctx.job_id)
    final = Status.COMPLETED_WITH_WARNINGS if (prior or warnings) else Status.COMPLETED
    await ctx.finish(final, fields={
        "progress": 100, "warnings:publish": warnings,
        "result": {"notion_page_url": page.get("url") or f"https://www.notion.so/{page['id'].replace('-', '')}",
                   "notion_page_id": page["id"],
                   "frames_requested": len(frame_list),
                   "frames_published": len(upload_ids)},
        "frames_published": len(upload_ids),
    })


HANDLERS = {"transcribe": transcribe, "summarize": summarize, "frames": frames, "publish": publish}
