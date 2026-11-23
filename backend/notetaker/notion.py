"""Notion API client + page assembly.

Idempotent publishing ("reconcile-before-create"):
  1. The first block of every page we create is a marker callout containing the job id.
  2. Before creating a page, we list the parent's child pages with the same title and check
     each one's first block for our job id. Any match comes from an earlier attempt that crashed
     between creating the page and recording it, so we archive it.
  3. Then we create the page, save its id in Redis immediately, upload the frames and append
     the blocks.
Redelivering the publish stage therefore always ends with exactly one live page per job.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from .config import settings
from .errors import Permanent, RateLimited, Retryable
from .faults import Faults
from .ratelimit import RateLimiter, notion_buckets
from .youtube import fmt_ts, watch_url

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
MAX_TEXT = 1900
HUB_TITLE = "📚 Video Notes"


def normalize_notion_id(value: str | None) -> str | None:
    """Accepts a 32-hex id (with or without dashes) or a notion.so URL; returns dashed UUID form."""
    if not value:
        return None
    s = value.strip().split("?")[0].split("#")[0].rstrip("/")
    tail = s.rsplit("/", 1)[-1].replace("-", "").lower()
    m = re.search(r"([0-9a-f]{32})$", tail)
    if not m:
        return None
    h = m.group(1)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# ----------------------------------------------------------------------- blocks
def rt(text: str, *, link: str | None = None, bold: bool = False, italic: bool = False,
       code: bool = False, color: str = "default") -> list[dict]:
    out = []
    text = text or ""
    for i in range(0, max(len(text), 1), MAX_TEXT):
        piece = text[i:i + MAX_TEXT]
        item: dict[str, Any] = {"type": "text", "text": {"content": piece}}
        if link:
            item["text"]["link"] = {"url": link}
        ann = {k: v for k, v in (("bold", bold), ("italic", italic), ("code", code)) if v}
        if color != "default":
            ann["color"] = color
        if ann:
            item["annotations"] = ann
        out.append(item)
    return out


def _block(kind: str, rich: list[dict] | None = None, **extra) -> dict:
    body: dict[str, Any] = {}
    if rich is not None:
        body["rich_text"] = rich[:100]
    body.update(extra)
    return {"object": "block", "type": kind, kind: body}


def heading(level: int, rich: list[dict]) -> dict:
    return _block(f"heading_{level}", rich)


def bullet(rich: list[dict]) -> dict:
    return _block("bulleted_list_item", rich)


def paragraph(rich: list[dict]) -> dict:
    return _block("paragraph", rich)


def callout(rich: list[dict], emoji: str, color: str = "gray_background") -> dict:
    return _block("callout", rich, icon={"type": "emoji", "emoji": emoji}, color=color)


def toggle(rich: list[dict], children: list[dict]) -> dict:
    return _block("toggle", rich, children=children)


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def code(text: str, language: str) -> dict:
    return _block("code", rt(text), language=language)


def page_mention(page_id: str) -> list[dict]:
    return [{"type": "mention", "mention": {"type": "page", "page": {"id": page_id}}}]


def concept_blocks(graph: dict, video_id: str) -> list[dict]:
    """Concept map (Mermaid diagram, drawn natively by Notion — not an image) + concept index."""
    from .graph import to_mermaid
    concepts = graph.get("concepts") or []
    if not concepts:
        return []
    names = {c["id"]: c["name"] for c in concepts}
    prereq: dict[str, list[str]] = {}
    for r in graph.get("relations", []):
        if r["type"] == "prerequisite_of":
            prereq.setdefault(r["to"], []).append(names[r["from"]])
    out = [heading(1, rt("Concept map"))]
    if graph.get("relations"):
        out.append(paragraph(rt("How the ideas in this video connect (⇒ prerequisite, → relation, ⇢ example / contrast). "
                                "If Notion shows the code, switch the block to Preview.", italic=True, color="gray")))
        out.append(code(to_mermaid(graph), "mermaid"))
    out.append(heading(2, rt("Concept index")))
    for c in concepts:
        item = rt(c["name"], bold=True)
        if c.get("definition"):
            item += rt(f" — {c['definition'].rstrip('.')}. ")
        item += rt("First explained at ", italic=True) + rt(fmt_ts(c["first_sec"]), link=watch_url(video_id, c["first_sec"]))
        if prereq.get(c["id"]):
            item += rt(" · builds on: " + ", ".join(prereq[c["id"]]), italic=True)
        out.append(bullet(item))
    return out


def related_blocks(related: list[dict]) -> list[dict]:
    if not related:
        return []
    out = [heading(1, rt("Related videos in your notes"))]
    for e in related:
        out.append(bullet(page_mention(e["page_id"]) + rt(" — shared concepts: " + ", ".join(e["shared"][:6]), italic=True)))
    return out


def toc() -> dict:
    return {"object": "block", "type": "table_of_contents", "table_of_contents": {}}


def video_embed(url: str) -> dict:
    return {"object": "block", "type": "video", "video": {"type": "external", "external": {"url": url}}}


def image_upload(upload_id: str, caption: list[dict]) -> dict:
    return {"object": "block", "type": "image",
            "image": {"type": "file_upload", "file_upload": {"id": upload_id}, "caption": caption[:100]}}


def marker_text(job_id: str) -> str:
    return f"nt-job:{job_id}"


def frame_caption(frame: dict, video_id: str) -> list[dict]:
    ts = frame["timestamp"]
    return (rt(f"[{fmt_ts(ts)}] ", link=watch_url(video_id, int(ts)), bold=True)
            + rt(frame["caption"].rstrip(".") + ". ")
            + rt("Why it was flagged: ", italic=True, bold=True)
            + rt(frame["reason"] or "Key visual referenced in the lecture.", italic=True))


def build_blocks(job: dict, synthesis: dict, frames: list[dict], upload_ids: dict[int, str],
                 omitted: list[dict], related: list[dict] | None = None) -> list[dict]:
    """Full page body (after the marker block). `frames[i]['moment_index']` links to synthesis moments."""
    vid = job["video_id"]
    blocks: list[dict] = [video_embed(watch_url(vid)), toc()]

    if synthesis.get("tldr"):
        blocks.append(heading(1, rt("TL;DR")))
        blocks += [bullet(rt(t)) for t in synthesis["tldr"]]

    blocks.append(heading(1, rt("Notes")))
    by_section: dict[int, list[dict]] = {}
    for f in frames:
        if f["idx"] in upload_ids:
            by_section.setdefault(f.get("section_index", 0), []).append(f)
    for i, sec in enumerate(synthesis["sections"]):
        blocks.append(heading(2, rt(f"{fmt_ts(sec['start_sec'])} · ", link=watch_url(vid, sec["start_sec"]))
                              + rt(sec["title"])))
        if sec.get("overview"):
            blocks.append(paragraph(rt(sec["overview"], italic=True)))
        blocks += [bullet(rt(b)) for b in sec["bullets"]]
        for f in sorted(by_section.get(i, []), key=lambda x: x["timestamp"]):
            blocks.append(image_upload(upload_ids[f["idx"]], frame_caption(f, vid)))

    blocks += concept_blocks(synthesis.get("graph") or {}, vid)

    if synthesis.get("key_terms"):
        blocks.append(heading(1, rt("Key terms")))
        blocks += [bullet(rt(t["term"], bold=True) + rt(f" — {t['definition']}")) for t in synthesis["key_terms"]]

    if synthesis.get("review_questions"):
        blocks.append(heading(1, rt("Review questions")))
        blocks += [toggle(rt(q["question"]), [paragraph(rt(q["answer"]))]) for q in synthesis["review_questions"]]

    blocks += related_blocks(related or [])

    if omitted:
        blocks.append(divider())
        lines = ", ".join(fmt_ts(o["timestamp_sec"]) for o in omitted if o.get("timestamp_sec", -1) >= 0)
        blocks.append(callout(rt(f"{len(omitted)} flagged frame(s) could not be captured"
                                 + (f" (at {lines})" if lines else "")
                                 + " and were omitted. No image was substituted."), "⚠️", "yellow_background"))
    return blocks


def marker_block(job: dict, meta: dict) -> dict:
    parts = rt("Study notes generated from ") + rt(job.get("video_title") or "this video",
                                                     link=watch_url(job["video_id"]), bold=True)
    if meta.get("channel"):
        parts += rt(f" by {meta['channel']}")
    if job.get("duration_sec"):
        parts += rt(f" · {fmt_ts(int(job['duration_sec']))}")
    parts += rt(" · every image is a real frame from the video, captioned by the model. ")
    parts += rt(marker_text(job["job_id"]), code=True, color="gray")
    return callout(parts, "🎓", "blue_background")


# ----------------------------------------------------------------------- client
class NotionClient:
    def __init__(self, token: str, limiter: RateLimiter, faults: Faults | None = None):
        self.token = token
        self.limiter = limiter
        self.faults = faults
        self.http = httpx.AsyncClient(base_url=API, timeout=httpx.Timeout(30.0, connect=10.0), headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": settings.notion_version,
        })

    async def close(self) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, *, json: Any = None, files: Any = None,
                      params: dict | None = None, on_wait=None) -> dict:
        await self.limiter.acquire("notion", notion_buckets(self.token), on_wait=on_wait)
        if self.faults and await self.faults.consume("notion_429"):
            raise RateLimited("notion", 3, injected=True, message="Notion returned 429 (injected fault)")
        try:
            resp = await self.http.request(method, path, json=json, files=files, params=params)
        except httpx.HTTPError as e:
            raise Retryable(f"Notion unreachable: {type(e).__name__}", code="NOTION_UNAVAILABLE") from None
        if resp.status_code < 300:
            return resp.json() if resp.content else {}
        try:
            body = resp.json()
        except ValueError:
            body = {}
        code, msg = body.get("code", ""), body.get("message", resp.text[:200])
        if resp.status_code == 429:
            ra = float(resp.headers.get("retry-after", "3") or 3)
            await self.limiter.set_cooldown("notion", ra)
            raise RateLimited("notion", ra, message="Notion returned 429")
        if resp.status_code == 401:
            raise Permanent("Notion rejected the access token — reconnect Notion in the extension",
                            code="NOTION_UNAUTHORIZED")
        if resp.status_code == 403:
            raise Permanent(f"Notion integration lacks permission: {msg}", code="NOTION_FORBIDDEN")
        if resp.status_code == 404:
            raise Permanent(f"Notion page not found or not shared with the integration: {msg}",
                            code="NOTION_PARENT_NOT_FOUND")
        if resp.status_code in (409,) or resp.status_code >= 500:
            raise Retryable(f"Notion temporary error {resp.status_code} {code}", code="NOTION_UNAVAILABLE")
        raise Permanent(f"Notion rejected the request ({code}): {msg}", code="NOTION_BAD_REQUEST")

    # ---- discovery ------------------------------------------------------
    async def search_pages(self, query: str = "", limit: int = 50) -> list[dict]:
        body: dict[str, Any] = {"filter": {"property": "object", "value": "page"}, "page_size": min(limit, 100),
                                "sort": {"direction": "descending", "timestamp": "last_edited_time"}}
        if query:
            body["query"] = query
        res = await self.request("POST", "/search", json=body)
        return res.get("results", [])

    @staticmethod
    def page_title(page: dict) -> str:
        for prop in (page.get("properties") or {}).values():
            if prop.get("type") == "title":
                return "".join(t.get("plain_text", "") for t in prop.get("title", []))
        return ""

    async def children(self, block_id: str, limit: int = 1000) -> list[dict]:
        out, cursor = [], None
        while len(out) < limit:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            res = await self.request("GET", f"/blocks/{block_id}/children", params=params)
            out += res.get("results", [])
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
        return out

    async def resolve_parent(self, parent_id: str | None) -> dict:
        """Returns a Notion `parent` object. Default destination (documented in README):
        NOTION_DEFAULT_PARENT_ID if set; otherwise a '📚 Video Notes' page inside the most recently
        edited top-level page the integration can access (created once, then reused)."""
        explicit = bool((parent_id or "").strip())
        raw_pid = (parent_id or settings.notion_default_parent_id or "").strip()
        pid = normalize_notion_id(raw_pid) if raw_pid else None
        if raw_pid and not pid:
            if explicit:
                raise Permanent(f"'{raw_pid}' is not a valid Notion page id", code="NOTION_PARENT_NOT_FOUND")
            log.warning("NOTION_DEFAULT_PARENT_ID is not a valid id; using the default hub page")
        if pid:
            try:
                await self.request("GET", f"/pages/{pid}")
                return {"type": "page_id", "page_id": pid}
            except Permanent as e:
                if e.code not in ("NOTION_PARENT_NOT_FOUND", "NOTION_BAD_REQUEST"):
                    raise
                try:  # maybe it is a database id
                    db = await self.request("GET", f"/databases/{pid}")
                    title_prop = next((k for k, v in db.get("properties", {}).items() if v.get("type") == "title"), "Name")
                    return {"type": "database_id", "database_id": pid, "_title_prop": title_prop}
                except Permanent:
                    if explicit:
                        raise e from None
                    # The server-wide default is not shared with THIS token (e.g. a different OAuth workspace):
                    # fall back to the pages the user granted, instead of failing the job.
                    log.info("default parent not accessible with this token; using the default hub page")
        pages = await self.search_pages()
        hub = next((p for p in pages if self.page_title(p) == HUB_TITLE and not p.get("archived")), None)
        if hub:
            return {"type": "page_id", "page_id": hub["id"]}
        top = [p for p in pages if (p.get("parent") or {}).get("type") == "workspace" and not p.get("archived")]
        anchor = (top or [p for p in pages if not p.get("archived")] or [None])[0]
        if anchor is None:
            raise Permanent("The Notion integration has no pages shared with it. Share a page during "
                            "'Connect Notion' or set a destination page in the extension options.",
                            code="NOTION_NO_DESTINATION")
        hub = await self.request("POST", "/pages", json={
            "parent": {"type": "page_id", "page_id": anchor["id"]},
            "icon": {"type": "emoji", "emoji": "📚"},
            "properties": {"title": {"title": rt(HUB_TITLE)}},
            "children": [paragraph(rt("Study notes created by the Video Note-Taking Agent appear here."))],
        })
        return {"type": "page_id", "page_id": hub["id"]}

    # ---- idempotency ----------------------------------------------------
    async def find_marked_pages(self, parent: dict, title: str, job_id: str) -> list[str]:
        marker = marker_text(job_id)
        candidates: list[str] = []
        if parent["type"] == "page_id":
            for b in await self.children(parent["page_id"]):
                if b.get("type") == "child_page" and b["child_page"].get("title") == title:
                    candidates.append(b["id"])
        else:
            res = await self.request("POST", f"/databases/{parent['database_id']}/query", json={
                "filter": {"property": parent.get("_title_prop", "Name"), "title": {"equals": title}},
                "page_size": 50})
            candidates = [p["id"] for p in res.get("results", [])]
        matches = []
        for pid in candidates:
            res = await self.request("GET", f"/blocks/{pid}/children", params={"page_size": 1})
            first = (res.get("results") or [{}])[0]
            rich = (first.get(first.get("type", ""), {}) or {}).get("rich_text", [])
            if any(marker in (t.get("plain_text") or t.get("text", {}).get("content", "")) for t in rich):
                matches.append(pid)
        return matches

    async def archive(self, page_id: str) -> None:
        await self.request("PATCH", f"/pages/{page_id}", json={"archived": True})

    async def create_page(self, parent: dict, title: str, first_blocks: list[dict]) -> dict:
        p = {k: v for k, v in parent.items() if not k.startswith("_")}
        prop = parent.get("_title_prop", "title")
        return await self.request("POST", "/pages", json={
            "parent": p,
            "icon": {"type": "emoji", "emoji": "📝"},
            "properties": {prop: {"title": rt(title[:1900])}},
            "children": first_blocks,
        })

    async def append(self, block_id: str, blocks: list[dict]) -> None:
        for i in range(0, len(blocks), 90):
            await self.request("PATCH", f"/blocks/{block_id}/children", json={"children": blocks[i:i + 90]})

    # ---- uploads -----------------------------------------------------------
    async def upload_image(self, data: bytes, filename: str) -> str:
        created = await self.request("POST", "/file_uploads", json={
            "mode": "single_part", "filename": filename, "content_type": "image/jpeg"})
        upload_id = created["id"]
        await self.request("POST", f"/file_uploads/{upload_id}/send",
                           files={"file": (filename, data, "image/jpeg")})
        return upload_id


async def exchange_oauth_code(code: str, redirect_uri: str) -> dict:
    if not (settings.notion_client_id and settings.notion_client_secret):
        raise Permanent("Notion OAuth is not configured on the server", code="OAUTH_NOT_CONFIGURED")
    async with httpx.AsyncClient(timeout=20) as http:
        resp = await http.post(f"{API}/oauth/token", auth=(settings.notion_client_id, settings.notion_client_secret),
                               headers={"Notion-Version": settings.notion_version},
                               json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error_description") or resp.json().get("error") or resp.text
        except ValueError:
            msg = resp.text
        raise Permanent(f"Notion OAuth exchange failed: {msg[:200]}", code="OAUTH_EXCHANGE_FAILED")
    return resp.json()


async def gather_limited(coros, limit: int = 2):
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c
    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)
