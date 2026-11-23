"""LLM synthesis: transcript → structured notes + flagged visual moments with captions.

* Works with any OpenAI-compatible endpoint (Gemini, OpenAI, Groq, Anthropic compat, Ollama).
* Every call first passes the cluster-wide GCRA limiter (RPM + TPM), so a burst of jobs drains
  without the provider ever returning 429. If a 429 still arrives, a shared cooldown is set.
* Long transcripts are chunked (map). Each chunk's result is checkpointed, so a crash or 429
  mid-synthesis does not redo finished chunks. Results are merged in order (reduce) and a
  final call produces the global TL;DR and review questions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable

import openai
from openai import AsyncOpenAI

from .config import settings
from .errors import Permanent, RateLimited, Retryable
from .faults import Faults
from .ratelimit import RateLimiter, llm_buckets
from .youtube import fmt_ts

log = logging.getLogger(__name__)

KINDS = {"slide", "diagram", "chart", "code", "equation", "demo", "table", "other"}

SYSTEM_NOTES = (
    "You are an expert teaching assistant. You turn timestamped lecture or tutorial transcripts into "
    "accurate, well-organised study notes. Use only information present in the transcript; never invent "
    "facts. Reply with a single JSON object and nothing else."
)

SYSTEM_VISION = (
    "You caption frames captured from an educational video. Each image is a real, unedited frame. "
    "Describe only what is actually visible in the image. Reply with a single JSON object and nothing else."
)


def text_weight(text: str) -> int:
    """Size in English-equivalent characters. Non-Latin scripts (Devanagari, CJK, Arabic, …) cost roughly
    3-4x more tokens per character, so a char-based budget would overflow small context windows."""
    return len(text) + 2 * sum(1 for ch in text if ord(ch) > 0x24F)


def chunk_lines(lines: list[dict], max_chars: int) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for ln in lines:
        n = text_weight(ln["text"]) + 12
        if cur and size + n > max_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def _chunk_prompt(title: str, duration: int, chunk: list[dict], idx: int, total: int, end_sec: int,
                  frames_budget: int, want_global: bool) -> str:
    start = int(chunk[0]["start"])
    body = "\n".join(f"[{int(ln['start'])}] {ln['text']}" for ln in chunk)
    # Depth scales with the span covered: roughly one section per ~2 minutes of video.
    span = max(end_sec - start, 60)
    sec_lo = max(2, round(span / 180))
    sec_hi = max(sec_lo + 1, round(span / 100))
    global_fields = ""
    global_rules = ""
    if want_global:
        global_fields = (',\n  "tldr": [string, ...],\n'
                         '  "review_questions": [{"question": string, "answer": string}, ...]')
        global_rules = ("- tldr: 3-5 bullets capturing the core ideas of the whole video.\n"
                        "- review_questions: 3-5 questions that test understanding (not trivia), each with a "
                        "concise answer grounded in the transcript.\n")
    lang = settings.notes_language
    return f"""Video title: "{title}" (duration {fmt_ts(duration)}).
Write every string in the JSON in {lang}. If the transcript is in another language or script, translate
faithfully (keep technical terms, code, product and proper names as they are).
This is part {idx + 1} of {total}, covering seconds {start}-{end_sec}.
Each transcript line is "[seconds-from-start] text".

TRANSCRIPT
{body}

Return JSON with exactly this shape:
{{
  "sections": [{{"title": string, "start_sec": integer, "overview": string, "bullets": [string, ...]}}, ...],
  "visual_moments": [{{"timestamp_sec": integer, "kind": "slide|diagram|chart|code|equation|demo|table|other",
                      "caption": string, "reason": string, "importance": integer 1-5}}, ...],
  "key_terms": [{{"term": string, "definition": string}}, ...]{global_fields}
}}

Rules:
- sections: {sec_lo}-{sec_hi} for this part, chronological, covering the WHOLE span from {start} to {end_sec}
  with no gaps (if the speaker enumerates steps/stages/points, give every one its own section, including the
  first). start_sec within [{start}, {end_sec}].
- overview: 1-2 sentences saying what the section is about and why it matters.
- bullets: 4-8 per section. Each bullet is a complete, specific, self-contained sentence a student could revise
  from: concepts, definitions, steps, concrete numbers, tools/names mentioned, examples, caveats and advice.
  Prefer specifics over generalities. No filler such as "the speaker says" or "this section discusses".
- visual_moments: moments where something informative is likely on screen: a slide, diagram, chart, code,
  equation, table, on-screen text or list, screenshot or demo. Evidence: the speaker points at something
  ("as you can see", "this diagram", "on this slide", "here we have", "let me show you", "look at this code",
  "this graph shows"), walks through a figure, reads a formula, list or code aloud, or names a tool/website
  they are showing. Use a timestamp 2-4 seconds AFTER the cue, when the visual is fully on screen. Up to
  {frames_budget}, most informative first; for mostly talking-head videos still flag the moments most likely
  to show on-screen text, titles or lists. timestamp_sec must be within [{start}, {end_sec}].
- caption: one sentence describing what the frame should show. reason: one sentence on why it matters for study.
- key_terms: up to 6 important technical terms with one-line definitions.
{global_rules}"""


def _reduce_prompt(title: str, sections: list[dict]) -> str:
    outline = "\n".join(
        f"[{fmt_ts(s['start_sec'])}] {s['title']}\n" + "\n".join(f"  - {b}" for b in s["bullets"])
        for s in sections)
    return f"""These are the section notes of the video "{title}", in order:

{outline}

Return JSON: {{"tldr": [string, ...], "review_questions": [{{"question": string, "answer": string}}, ...]}}
- Write in {settings.notes_language}.
- tldr: 3-5 bullets capturing the core ideas of the whole video.
- review_questions: 3-5 questions that test understanding, each with a concise answer grounded in the notes."""


def _vision_prompt(items: list[dict]) -> str:
    lines = "\n".join(
        f'Frame {i}: captured at {fmt_ts(it["timestamp"])}. Transcript-based expectation: "{it["caption"]}". '
        f'Why flagged: "{it["reason"]}". Nearby transcript: "{it.get("context", "")[:300]}"'
        for i, it in enumerate(items))
    return f"""The images below are real frames captured from the video, in this order:
{lines}

For each frame write an accurate caption of what is visible (titles, labels, diagram structure, axes, code,
equations) and why it matters for a student, using the transcript context to explain significance. Never
describe content that is not visible. If the frame does not show the expected visual (e.g. only the presenter),
say plainly what it shows instead.
Write captions and reasons in {settings.notes_language}.
Return JSON: {{"frames": [{{"index": integer, "shows_expected_visual": boolean, "caption": string,
"reason": string}}, ...]}} with one entry per frame. Keep caption <= 40 words and reason <= 25 words."""


def extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        val = json.loads(text)
        if isinstance(val, dict):
            return val
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            val = json.loads(text[start:end + 1])
            if isinstance(val, dict):
                return val
        except ValueError:
            pass
    raise ValueError("model output is not a JSON object")


def _as_int(v: Any, default: int = -1) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def normalize_part(raw: dict, start: int, end: int) -> dict:
    """Coerce one chunk's raw LLM output into a clean, typed structure."""
    sections = []
    for s in raw.get("sections") or []:
        if not isinstance(s, dict):
            continue
        bullets = [str(b).strip() for b in (s.get("bullets") or []) if str(b).strip()]
        title = str(s.get("title") or "").strip()
        if not (title or bullets):
            continue
        st = _as_int(s.get("start_sec"), start)
        sections.append({"title": title or "Notes", "start_sec": min(max(st, start), end), "bullets": bullets,
                         "overview": str(s.get("overview") or "").strip()})
    sections.sort(key=lambda x: x["start_sec"])
    moments = []
    for m in raw.get("visual_moments") or []:
        if not isinstance(m, dict):
            continue
        moments.append({
            "timestamp_sec": _as_int(m.get("timestamp_sec")),
            "kind": str(m.get("kind") or "other").lower() if str(m.get("kind") or "").lower() in KINDS else "other",
            "caption": str(m.get("caption") or "").strip(),
            "reason": str(m.get("reason") or "").strip(),
            "importance": min(max(_as_int(m.get("importance"), 3), 1), 5),
        })
    terms = [{"term": str(t.get("term")).strip(), "definition": str(t.get("definition") or "").strip()}
             for t in raw.get("key_terms") or [] if isinstance(t, dict) and t.get("term")]
    out = {"sections": sections, "visual_moments": moments, "key_terms": terms}
    if "tldr" in raw:
        out["tldr"] = [str(x).strip() for x in raw.get("tldr") or [] if str(x).strip()]
    if "review_questions" in raw:
        out["review_questions"] = [
            {"question": str(q.get("question")).strip(), "answer": str(q.get("answer") or "").strip()}
            for q in raw.get("review_questions") or [] if isinstance(q, dict) and q.get("question")]
    return out


def merge_parts(parts: list[dict], duration: int, max_frames: int, min_gap: int = 8,
                min_frames: int = 0) -> tuple[dict, list[dict]]:
    """Order-preserving merge of chunk results. Returns (synthesis, warnings).

    If the LLM flagged fewer than `min_frames` visual moments (common for talking-head videos), the gap is
    filled with one "section anchor" frame per section. Those are still real frames: they pass the same
    quality gate, and the vision model writes their caption from what is actually visible."""
    warnings: list[dict] = []
    sections: list[dict] = []
    for part in parts:
        for s in part["sections"]:
            if sections and sections[-1]["title"].lower() == s["title"].lower():
                sections[-1]["bullets"].extend(s["bullets"])  # a section split across a chunk boundary
            else:
                sections.append({**s, "bullets": list(s["bullets"])})
    if not sections:
        raise Retryable("LLM produced no notes", code="LLM_BAD_OUTPUT")

    valid = []
    for m in (m for p in parts for m in p["visual_moments"]):
        t = m["timestamp_sec"]
        if t < 0 or (duration and t >= duration) or not m["caption"]:
            warnings.append({"code": "FRAME_TIMESTAMP_INVALID", "timestamp_sec": t,
                             "message": "LLM flagged a timestamp outside the video or without a caption"})
            continue
        valid.append(m)
    # Keep the most important moments, then enforce a minimum spacing, then restore time order.
    valid.sort(key=lambda m: (-m["importance"], m["timestamp_sec"]))
    chosen: list[dict] = []
    for m in valid:
        if len(chosen) >= max_frames:
            break
        if all(abs(m["timestamp_sec"] - c["timestamp_sec"]) >= min_gap for c in chosen):
            chosen.append(m)
    want = min(min_frames, max_frames)
    if len(chosen) < want:
        anchors = []
        for s in sections:
            # A few seconds into the section, once any title card / slide change has settled.
            t = s["start_sec"] + 6
            if duration and t >= duration - 2:
                continue
            anchors.append({"timestamp_sec": t, "kind": "other", "importance": 1, "source": "section_anchor",
                            "caption": f"Frame from the section \"{s['title']}\".",
                            "reason": f"Shows what was on screen while covering: {s['title']}."})
        # Spread picks over the video rather than taking the first few sections.
        if len(anchors) > want - len(chosen):
            step = len(anchors) / (want - len(chosen))
            anchors = [anchors[int(i * step)] for i in range(want - len(chosen))]
        for a in anchors:
            if len(chosen) >= want:
                break
            if all(abs(a["timestamp_sec"] - c["timestamp_sec"]) >= min_gap for c in chosen):
                chosen.append(a)
    chosen.sort(key=lambda m: m["timestamp_sec"])
    for m in chosen:
        idx = 0
        for i, s in enumerate(sections):
            if s["start_sec"] <= m["timestamp_sec"]:
                idx = i
        m["section_index"] = idx

    seen, terms = set(), []
    for t in (t for p in parts for t in p["key_terms"]):
        key = t["term"].lower()
        if key not in seen:
            seen.add(key)
            terms.append(t)
    return {
        "sections": sections,
        "visual_moments": chosen,
        "key_terms": terms[:12],
        "tldr": parts[-1].get("tldr", []) if len(parts) == 1 else [],
        "review_questions": parts[-1].get("review_questions", []) if len(parts) == 1 else [],
    }, warnings


def _estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            total += len(c) // 4
        else:
            for part in c:
                total += len(part.get("text", "")) // 4 if part.get("type") == "text" else 1100
    return total + 50


def _retry_after(headers) -> float | None:
    if not headers:
        return None
    for key in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        val = headers.get(key)
        if val:
            m = re.match(r"^\s*([\d.]+)\s*(ms|s)?", str(val))
            if m:
                n = float(m.group(1))
                return n / 1000 if m.group(2) == "ms" else n
    return None


class LLM:
    def __init__(self, limiter: RateLimiter, faults: Faults, metrics: Callable[[str], Awaitable[None]]):
        self.limiter = limiter
        self.faults = faults
        self.metric = metrics
        self.client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key or "missing",
                                  max_retries=0, timeout=settings.llm_timeout_sec)
        self._json_mode = settings.llm_json_mode

    async def chat_json(self, messages: list[dict], *, model: str, max_tokens: int,
                        on_wait: Callable[[float], Awaitable[None]] | None = None) -> dict:
        if not settings.llm_api_key and "localhost" not in settings.llm_base_url and "ollama" not in settings.llm_base_url:
            raise Permanent("LLM_API_KEY is not configured on the server", code="LLM_NOT_CONFIGURED")
        for attempt in range(2):
            text = await self._call(messages, model=model, max_tokens=max_tokens, on_wait=on_wait)
            try:
                return extract_json(text)
            except ValueError:
                await self.metric("llm_bad_json")
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": text[:4000]},
                        {"role": "user", "content": "That was not a valid JSON object. Reply again with ONLY the JSON object."},
                    ]
        raise Retryable("LLM returned malformed JSON twice", code="LLM_BAD_OUTPUT")

    async def _call(self, messages: list[dict], *, model: str, max_tokens: int, on_wait) -> str:
        est = _estimate_tokens(messages) + max_tokens
        await self.limiter.acquire("llm", llm_buckets(), [1, est], on_wait=on_wait)
        if await self.faults.consume("llm_429"):
            await self.metric("llm_injected_429")
            raise RateLimited("llm", None, injected=True, message="LLM provider returned 429 (injected fault)")
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        await self.metric("llm_calls")
        try:
            resp = await self._create_with_5xx_retry(kwargs)
        except openai.RateLimitError as e:
            ra = _retry_after(getattr(e.response, "headers", None))
            await self.limiter.set_cooldown("llm", ra or settings.backoff_for(1))
            await self.metric("llm_provider_429")
            raise RateLimited("llm", ra, message="LLM provider returned 429") from None
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            raise Retryable(f"LLM unreachable: {type(e).__name__}", code="LLM_UNAVAILABLE") from None
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise Permanent("LLM API key rejected by provider", code="LLM_AUTH_FAILED") from None
        except openai.BadRequestError as e:
            if self._json_mode and "response_format" in str(e):
                self._json_mode = False  # provider without JSON mode: fall back to prompt-only JSON
                return await self._call(messages, model=model, max_tokens=max_tokens, on_wait=on_wait)
            raise Permanent(f"LLM rejected the request: {str(e)[:300]}", code="LLM_BAD_REQUEST") from None
        except openai.NotFoundError:
            raise Permanent(f"LLM model '{model}' not found", code="LLM_MODEL_NOT_FOUND") from None
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                raise Retryable(f"LLM provider error {e.status_code}", code="LLM_UNAVAILABLE") from None
            raise Permanent(f"LLM error {e.status_code}: {str(e)[:200]}", code="LLM_ERROR") from None
        choice = resp.choices[0]
        if usage := getattr(resp, "usage", None):
            await self.metric_tokens(usage)
        if choice.finish_reason == "length":
            log.warning("LLM output truncated (max_tokens=%s)", max_tokens)
        return choice.message.content or ""

    async def _create_with_5xx_retry(self, kwargs: dict):
        """Hosted providers return brief 503 "high demand" spikes; retry those in-call (2 s, 5 s) so optional
        steps (triage, captions) don't silently degrade. Persistent 5xx still surfaces as LLM_UNAVAILABLE and
        connection errors are not retried here (the stage-level backoff / DLQ handles real outages)."""
        delays = [2, 5][:max(settings.llm_5xx_retries, 0)]
        for delay in delays + [None]:
            try:
                return await self.client.chat.completions.create(**kwargs)
            except openai.APIStatusError as e:
                if delay is None or e.status_code < 500:
                    raise
                await self.metric("llm_5xx_retried")
                log.info("LLM %s; retrying in %ss", e.status_code, delay)
                await asyncio.sleep(delay)

    async def metric_tokens(self, usage) -> None:
        try:
            await self.metric("llm_tokens", int(getattr(usage, "total_tokens", 0) or 0))  # type: ignore[call-arg]
        except TypeError:
            pass

    # ------------------------------------------------------------------ tasks
    async def synthesize_chunk(self, title: str, duration: int, chunk: list[dict], idx: int, total: int,
                               end_sec: int, want_global: bool, on_wait=None) -> dict:
        budget = max(2, -(-settings.max_frames // total) + 1)
        prompt = _chunk_prompt(title, duration, chunk, idx, total, end_sec, budget, want_global)
        try:
            raw = await self.chat_json([{"role": "system", "content": SYSTEM_NOTES}, {"role": "user", "content": prompt}],
                                       model=settings.llm_model, max_tokens=settings.llm_max_output_tokens,
                                       on_wait=on_wait)
            part = normalize_part(raw, int(chunk[0]["start"]), end_sec)
        except Retryable as e:
            if e.code != "LLM_BAD_OUTPUT" or len(chunk) < 2:
                raise
            part = {"sections": []}
        if part["sections"]:
            return part
        # Empty / truncated output usually means the chunk overflowed the model's context window.
        # Adaptive re-chunking: split in half and synthesize each half, instead of silently losing a span.
        if len(chunk) < 2:
            raise Retryable("LLM produced no notes for a transcript chunk", code="LLM_BAD_OUTPUT")
        log.warning("chunk %d/%d produced no sections; splitting it in half and retrying", idx + 1, total)
        await self.metric("llm_chunk_splits")
        mid = len(chunk) // 2
        left = await self.synthesize_chunk(title, duration, chunk[:mid], idx, total, int(chunk[mid]["start"]),
                                           False, on_wait)
        right = await self.synthesize_chunk(title, duration, chunk[mid:], idx, total, end_sec, False, on_wait)
        return {k: left.get(k, []) + right.get(k, []) for k in ("sections", "visual_moments", "key_terms")}

    async def concept_graph(self, title: str, synthesis: dict, duration: int, on_wait=None) -> dict:
        from .graph import graph_prompt, normalize_graph
        prompt = graph_prompt(title, synthesis["sections"], synthesis.get("key_terms", []), settings.notes_language)
        raw = await self.chat_json([{"role": "system", "content": SYSTEM_NOTES}, {"role": "user", "content": prompt}],
                                   model=settings.llm_model, max_tokens=3000, on_wait=on_wait)
        return normalize_graph(raw, synthesis["sections"], duration)

    async def rate_contact_sheet(self, sheet_b64: str, n: int, title: str, on_wait=None) -> list[dict]:
        """Vision triage: rate N numbered thumbnails (one grid image) for how useful each is as a
        study-note illustration. One call regardless of N."""
        prompt = (
            f'The image is a grid of {n} numbered, low-resolution thumbnails (tiles 0..{n - 1}, number in the '
            f'top-left corner) taken from the video "{title}". Rate each tile for how useful it would be as an '
            "illustration in study notes:\n"
            "3 = slide, diagram, chart, code, equation, table, screenshot or demo with informative content\n"
            "2 = some informative on-screen text or visual (keyword overlay, labelled illustration, list)\n"
            "1 = mostly the presenter with a minor overlay, or decorative b-roll\n"
            "0 = presenter only, logo / intro / end card, blank or transition\n"
            'Return JSON {"tiles": [{"index": int, "score": int, "kind": "slide|diagram|chart|code|equation|'
            'table|screenshot|text|demo|presenter|other", "shows": "what is visible, <= 15 words"}]} with one '
            f"entry per tile. Write in {settings.notes_language}.")
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{sheet_b64}"}}]
        raw = await self.chat_json([{"role": "system", "content": SYSTEM_VISION}, {"role": "user", "content": content}],
                                   model=settings.vision_model, max_tokens=2048, on_wait=on_wait)
        out = []
        for t in raw.get("tiles") or []:
            if isinstance(t, dict) and _as_int(t.get("index")) in range(n):
                kind = str(t.get("kind") or "other").lower()
                out.append({"index": _as_int(t["index"]), "score": min(max(_as_int(t.get("score"), 0), 0), 3),
                            "kind": kind, "shows": str(t.get("shows") or "").strip()[:200]})
        return out

    async def reduce(self, title: str, sections: list[dict], on_wait=None) -> dict:
        raw = await self.chat_json([{"role": "system", "content": SYSTEM_NOTES},
                                    {"role": "user", "content": _reduce_prompt(title, sections)}],
                                   model=settings.llm_model, max_tokens=2048, on_wait=on_wait)
        part = normalize_part({"tldr": raw.get("tldr", []), "review_questions": raw.get("review_questions", [])}, 0, 0)
        return {"tldr": part.get("tldr", []), "review_questions": part.get("review_questions", [])}

    async def caption_frames(self, items: list[dict], images_b64: list[str], on_wait=None) -> list[dict]:
        """Vision-grounded captions, in batches of VISION_BATCH frames so the request fits small-context
        (self-hosted) models. A failed batch only loses its own frames; rate limits still propagate."""
        size = max(1, settings.vision_batch)
        sem = asyncio.Semaphore(max(1, settings.llm_concurrency))

        async def run(start: int) -> list[dict]:
            async with sem:
                return await self._caption_batch(items[start:start + size], images_b64[start:start + size],
                                                 start, on_wait)

        # Batches are independent: run them concurrently (each still passes the global limiter).
        results = await asyncio.gather(*(run(s) for s in range(0, len(items), size)), return_exceptions=True)
        out: list[dict] = []
        last_err: BaseException | None = None
        for r in results:
            if isinstance(r, RateLimited):
                raise r
            if isinstance(r, BaseException):
                last_err = r
            else:
                out += r
        if not out and last_err is not None:
            raise last_err
        return sorted(out, key=lambda f: f["index"])

    async def _caption_batch(self, items: list[dict], images_b64: list[str], offset: int, on_wait) -> list[dict]:
        content: list[dict] = [{"type": "text", "text": _vision_prompt(items)}]
        for b64 in images_b64:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        raw = await self.chat_json([{"role": "system", "content": SYSTEM_VISION}, {"role": "user", "content": content}],
                                   model=settings.vision_model, max_tokens=2048, on_wait=on_wait)
        out = []
        for f in raw.get("frames") or []:
            if isinstance(f, dict) and _as_int(f.get("index")) in range(len(items)) and str(f.get("caption") or "").strip():
                out.append({"index": offset + _as_int(f["index"]), "caption": str(f["caption"]).strip(),
                            "reason": str(f.get("reason") or "").strip(),
                            "shows_expected_visual": bool(f.get("shows_expected_visual", True))})
        return out
