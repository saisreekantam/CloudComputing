"""Self-check for Constraints A and B on published jobs (an LLM-as-judge harness).

    docker compose exec worker python -m notetaker.tools.verify <job_id> [<job_id> ...] --token <notion token>

For each completed job:
  A  duration_sec <= MAX_VIDEO_SECONDS
  B1 every image block on the Notion page has a non-empty caption containing a timestamp and a reason
  B2 provenance: the Notion image's perceptual hash matches the frame we captured
  B3 independent re-capture: grab the same timestamp from the source video again → hashes agree
  B4 LLM judge (vision): does the caption accurately describe the image? (score 1-5)
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re

import httpx
from PIL import Image

from .. import youtube
from ..config import settings
from ..faults import Faults
from ..frames import grab, phash, phash_distance, vision_jpeg_b64
from ..llm import LLM
from ..ratelimit import RateLimiter
from ..store import JobStore

TS_RE = re.compile(r"\[(\d+:)?(\d{1,2}):(\d{2})\]")


def _ts(caption: str) -> int | None:
    m = TS_RE.search(caption)
    if not m:
        return None
    h = int(m.group(1)[:-1]) if m.group(1) else 0
    return h * 3600 + int(m.group(2)) * 60 + int(m.group(3))


async def notion_images(token: str, page_id: str) -> list[dict]:
    out, cursor = [], None
    async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {token}",
                                                      "Notion-Version": settings.notion_version}) as h:
        while True:
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            r = (await h.get(f"https://api.notion.com/v1/blocks/{page_id}/children", params=params)).json()
            for b in r.get("results", []):
                if b["type"] == "image":
                    img = b["image"]
                    url = (img.get("file") or img.get("external") or {}).get("url")
                    caption = "".join(t.get("plain_text", "") for t in img.get("caption", []))
                    data = (await h.get(url, headers={"Authorization": ""})).content if url else b""
                    out.append({"caption": caption, "bytes": data, "source": img.get("type")})
            if not r.get("has_more"):
                return out
            cursor = r["next_cursor"]


async def _judge_batch(llm: LLM, items: list[dict], offset: int) -> list[dict]:
    content = [{"type": "text", "text": (
        "You are grading study notes. For each image (a frame from an educational video) and its caption, "
        "rate 1-5 how accurately the caption describes what is visible, and say whether the image looks like an "
        "authentic video frame (not AI-generated or redrawn). Return JSON {\"items\": [{\"index\": int, "
        "\"caption_accuracy\": int, \"authentic_frame\": bool, \"comment\": string}]}.\n" +
        "\n".join(f"Image {i}: caption = {it['caption']!r}" for i, it in enumerate(items)))}]
    for it in items:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{vision_jpeg_b64(it['bytes'])}"}})
    raw = await llm.chat_json([{"role": "user", "content": content}], model=settings.vision_model, max_tokens=2048)
    out = []
    for v in raw.get("items", []):
        try:
            i = int(v.get("index", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(items):
            out.append({**v, "index": offset + i})
    return out


async def judge(llm: LLM, items: list[dict]) -> list[dict]:
    """LLM-as-judge in VISION_BATCH-sized batches (one call with every image overflows small contexts)."""
    size = max(1, settings.vision_batch)
    parts = await asyncio.gather(*(_judge_batch(llm, items[s:s + size], s) for s in range(0, len(items), size)))
    return [v for p in parts for v in p]


async def verify(job_id: str, token: str, use_llm: bool) -> dict:
    store = JobStore()
    try:
        view = await store.view(job_id)
        if not view:
            return {"job_id": job_id, "error": "unknown job"}
        rep: dict = {"job_id": job_id, "status": view["status"], "title": view["video_title"],
                     "duration_sec": view["duration_sec"],
                     "A_length_ok": (view["duration_sec"] or 0) <= settings.max_video_seconds}
        if not view["result"]:
            rep["note"] = "job has no published page"
            return rep
        frames = {int(round(f["timestamp"])): f for f in (await store.load_ckpt(job_id, "frames") or []) if f.get("ok")}
        images = await notion_images(token, view["result"]["notion_page_id"])
        stream = None
        try:
            stream = (await asyncio.to_thread(youtube.fetch_metadata, view["video_id"]))["stream"]
        except Exception as e:  # noqa: BLE001
            rep["recapture_error"] = str(e)[:200]
        checks = []
        for img in images:
            c: dict = {"caption": img["caption"][:160], "upload_type": img["source"]}
            t = _ts(img["caption"])
            c["B1_caption_ok"] = bool(t is not None and "Why it was flagged" in img["caption"] and len(img["caption"]) > 30)
            try:
                ph = phash(Image.open(io.BytesIO(img["bytes"])))
            except Exception:  # noqa: BLE001
                c["error"] = "image not decodable"
                checks.append(c)
                continue
            meta = frames.get(t) if t is not None else None
            c["B2_matches_captured"] = bool(meta and phash_distance(ph, meta["phash"]) <= 6)
            if stream and meta:
                try:
                    fresh = await asyncio.to_thread(grab, stream, float(meta["timestamp"]), 40)
                    d = int(phash_distance(ph, phash(Image.open(io.BytesIO(fresh)))))
                    c["B3_recapture_distance"] = d
                    c["B3_matches_source_video"] = d <= 12
                except Exception as e:  # noqa: BLE001
                    c["B3_error"] = str(e)[:120]
            checks.append(c)
        if use_llm and images:
            r = RateLimiter(store.r)
            llm = LLM(r, Faults(store.r), store.incr)
            try:
                for v in await judge(llm, images):
                    i = int(v.get("index", -1))
                    if 0 <= i < len(checks):
                        checks[i]["B4_caption_accuracy"] = v.get("caption_accuracy")
                        checks[i]["B4_authentic"] = v.get("authentic_frame")
                        checks[i]["B4_comment"] = v.get("comment")
            except Exception as e:  # noqa: BLE001
                rep["judge_error"] = str(e)[:200]
        rep["images"] = checks
        rep["B_all_pass"] = all(c.get("B1_caption_ok") and c.get("B2_matches_captured")
                                and c.get("B3_matches_source_video", True) for c in checks)
        return rep
    finally:
        await store.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("job_ids", nargs="+")
    ap.add_argument("--token", default=os.getenv("NOTION_TOKEN", ""))
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    if not a.token:
        raise SystemExit("--token (Notion) required to read the published pages")
    for jid in a.job_ids:
        print(json.dumps(asyncio.run(verify(jid, a.token, not a.no_llm)), indent=2))


if __name__ == "__main__":
    main()
