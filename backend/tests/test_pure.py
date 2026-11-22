"""Pure-logic tests (no network, no Redis)."""
import pytest

from notetaker.config import Settings
from notetaker.llm import chunk_lines, extract_json, merge_parts, normalize_part
from notetaker.models import ALLOWED, TERMINAL, Status, can_transition, predecessors
from notetaker.notion import build_blocks, normalize_notion_id, rt
from notetaker.youtube import classify_youtube_error, fmt_ts, parse_video_id, to_lines
from notetaker.errors import Permanent, RateLimited, Retryable


@pytest.mark.parametrize("url,vid", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=x", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://vimeo.com/123456", None),
    ("https://www.youtube.com/watch?v=short", None),
    ("https://evil.com/watch?v=dQw4w9WgXcQ", None),
    ("dQw4w9WgXcQ", None),
    ("", None),
])
def test_parse_video_id(url, vid):
    assert parse_video_id(url) == vid


def test_state_machine_is_linear_and_terminal_states_are_sinks():
    order = [Status.QUEUED, Status.TRANSCRIBING, Status.SUMMARIZING, Status.EXTRACTING_FRAMES, Status.PUBLISHING]
    for a, b in zip(order, order[1:]):
        assert can_transition(a, b)
        assert not can_transition(b, a), f"{b}->{a} must be illegal"
    assert not can_transition(Status.QUEUED, Status.SUMMARIZING)  # no skipping stages
    for s in order:
        assert can_transition(s, Status.FAILED) and can_transition(s, Status.DEAD_LETTERED)
    assert ALLOWED[Status.COMPLETED] == set() and ALLOWED[Status.COMPLETED_WITH_WARNINGS] == set()
    assert predecessors(Status.COMPLETED) == {Status.PUBLISHING}
    assert can_transition(None, Status.QUEUED) and not can_transition(None, Status.TRANSCRIBING)
    assert TERMINAL == {Status.COMPLETED, Status.COMPLETED_WITH_WARNINGS, Status.FAILED, Status.DEAD_LETTERED}


def test_backoff_schedule(monkeypatch):
    monkeypatch.setenv("RETRY_BACKOFF_SECONDS", "10,30,90")
    monkeypatch.setenv("DEFER_SECONDS", "5")
    s = Settings()
    assert [s.backoff_for(i) for i in (1, 2, 3, 4, 9)] == [10, 30, 90, 90, 90]
    assert s.tier_at_least(12) == 30 and s.tier_at_least(1000) == 90 and s.tier_at_least(3) == 5


def test_youtube_error_classification():
    assert isinstance(classify_youtube_error("HTTP Error 429: Too Many Requests"), RateLimited)
    e = classify_youtube_error("ERROR: [youtube] abc: Private video. Sign in if you've been granted access")
    assert isinstance(e, Retryable) and e.code == "VIDEO_UNAVAILABLE"  # bounded retries → DLQ
    assert isinstance(classify_youtube_error("Sign in to confirm your age"), Permanent)
    assert isinstance(classify_youtube_error("Sign in to confirm you’re not a bot"), RateLimited)


def test_to_lines_and_chunking_preserve_order():
    segs = [{"start": float(i * 4), "dur": 4.0, "text": f"w{i}"} for i in range(300)]
    lines = to_lines(segs, window=20)
    assert lines[0]["start"] == 0 and all(a["start"] < b["start"] for a, b in zip(lines, lines[1:]))
    chunks = chunk_lines(lines, max_chars=200)
    assert len(chunks) > 1
    flat = [ln for c in chunks for ln in c]
    assert flat == lines


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": {"b": 2}} hope it helps') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("no json here")


def _part(start, end, sections, moments, **kw):
    raw = {"sections": sections, "visual_moments": moments, "key_terms": kw.get("terms", [])}
    raw.update({k: v for k, v in kw.items() if k != "terms"})
    return normalize_part(raw, start, end)


def test_merge_parts_orders_validates_and_caps_frames():
    p1 = _part(0, 300, [{"title": "Intro", "start_sec": 0, "bullets": ["a", "b"]},
                        {"title": "Neurons", "start_sec": 120, "bullets": ["c"]}],
               [{"timestamp_sec": 130, "caption": "diagram of a neuron", "reason": "core", "importance": 5, "kind": "diagram"},
                {"timestamp_sec": 133, "caption": "same diagram", "reason": "dup", "importance": 2, "kind": "diagram"}],
               terms=[{"term": "Neuron", "definition": "holds a number"}])
    p2 = _part(300, 600, [{"title": "Neurons", "start_sec": 300, "bullets": ["d"]},  # continues across boundary
                          {"title": "Layers", "start_sec": 400, "bullets": ["e"]}],
               [{"timestamp_sec": 450, "caption": "layer chart", "reason": "structure", "importance": 4, "kind": "chart"},
                {"timestamp_sec": 9999, "caption": "hallucinated", "reason": "x", "importance": 5}],
               terms=[{"term": "neuron", "definition": "dup"}, {"term": "Layer", "definition": "group"}])
    syn, warns = merge_parts([p1, p2], duration=600, max_frames=8, min_gap=8)
    assert [s["title"] for s in syn["sections"]] == ["Intro", "Neurons", "Layers"]
    assert syn["sections"][1]["bullets"] == ["c", "d"]
    ts = [m["timestamp_sec"] for m in syn["visual_moments"]]
    assert ts == [130, 450]  # 133 dropped (min gap), 9999 dropped (outside video)
    assert [m["section_index"] for m in syn["visual_moments"]] == [1, 2]
    assert any(w["code"] == "FRAME_TIMESTAMP_INVALID" and w["timestamp_sec"] == 9999 for w in warns)
    assert [t["term"] for t in syn["key_terms"]] == ["Neuron", "Layer"]
    capped, _ = merge_parts([p1, p2], duration=600, max_frames=1)
    assert [m["timestamp_sec"] for m in capped["visual_moments"]] == [130]  # highest importance kept


def test_normalize_notion_id():
    assert normalize_notion_id("1429989fe8ac4effbc8f57f56486db54") == "1429989f-e8ac-4eff-bc8f-57f56486db54"
    assert normalize_notion_id("https://www.notion.so/team/My-Page-1429989fe8ac4effbc8f57f56486db54?pvs=4") == \
        "1429989f-e8ac-4eff-bc8f-57f56486db54"
    assert normalize_notion_id("1429989f-e8ac-4eff-bc8f-57f56486db54") == "1429989f-e8ac-4eff-bc8f-57f56486db54"
    assert normalize_notion_id("not-an-id") is None


def test_rich_text_splits_long_text():
    parts = rt("x" * 4500)
    assert len(parts) == 3 and all(len(p["text"]["content"]) <= 2000 for p in parts)


def test_every_published_image_has_a_caption_and_real_upload():
    syn = {"sections": [{"title": "A", "start_sec": 0, "bullets": ["one"]}, {"title": "B", "start_sec": 60, "bullets": ["two"]}],
           "tldr": ["t"], "key_terms": [{"term": "k", "definition": "d"}],
           "review_questions": [{"question": "q?", "answer": "a"}]}
    frames = [
        {"idx": 0, "ok": True, "timestamp": 12.0, "requested_ts": 12, "caption": "A slide", "reason": "key", "section_index": 0},
        {"idx": 1, "ok": True, "timestamp": 70.0, "requested_ts": 70, "caption": "A chart", "reason": "data", "section_index": 1},
        {"idx": 2, "ok": False, "requested_ts": 90, "caption": "x", "reason": "y", "section_index": 1},
    ]
    blocks = build_blocks({"video_id": "dQw4w9WgXcQ"}, syn, frames, {0: "up-0", 1: "up-1"}, [{"timestamp_sec": 90}])
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 2
    for img in images:
        assert img["image"]["type"] == "file_upload"  # uploaded frame bytes, never an external/generated URL
        caption = "".join(t["text"]["content"] for t in img["image"]["caption"])
        assert "Why it was flagged" in caption and len(caption) > 20
    assert fmt_ts(70) in "".join(t["text"]["content"] for t in images[1]["image"]["caption"])
    # Omitted frame is disclosed, not substituted.
    assert any(b["type"] == "callout" and "omitted" in b["callout"]["rich_text"][0]["text"]["content"] for b in blocks)
    # Frames land inside their own section (image for section B appears after heading B).
    kinds = [(b["type"], b.get("image", {}).get("file_upload", {}).get("id")) for b in blocks]
    hb = next(i for i, b in enumerate(blocks) if b["type"] == "heading_2" and "B" in b["heading_2"]["rich_text"][-1]["text"]["content"])
    assert kinds.index(("image", "up-1")) > hb > kinds.index(("image", "up-0"))


async def test_vision_batching_maps_indices_and_isolates_failures(monkeypatch):
    """8 frames → two batches of 4; indices are remapped to the global list; a failing batch
    loses only its own frames, while a rate limit still propagates (so the stage is retried)."""
    from notetaker import llm as llm_mod
    monkeypatch.setattr(llm_mod.settings.__class__, "vision_batch", property(lambda s: 4), raising=False)
    calls = []

    async def fake_chat_json(self, messages, **kw):
        n = sum(1 for c in messages[1]["content"] if c["type"] == "image_url")
        calls.append(n)
        if len(calls) == 2 and fail_second:
            raise Permanent("context exceeded", code="LLM_BAD_REQUEST")
        return {"frames": [{"index": i, "caption": f"cap{len(calls)}-{i}", "reason": "r"} for i in range(n)]}

    monkeypatch.setattr(llm_mod.LLM, "chat_json", fake_chat_json)
    inst = llm_mod.LLM.__new__(llm_mod.LLM)
    items = [{"timestamp": t, "caption": "c", "reason": "r", "context": ""} for t in range(8)]
    fail_second = False
    out = await inst.caption_frames(items, ["x"] * 8)
    assert calls == [4, 4] and [o["index"] for o in out] == list(range(8)) and out[5]["caption"] == "cap2-1"

    calls.clear(); fail_second = True
    out = await inst.caption_frames(items, ["x"] * 8)
    assert [o["index"] for o in out] == [0, 1, 2, 3]

    async def rl(self, messages, **kw):
        raise RateLimited("llm", 5)
    monkeypatch.setattr(llm_mod.LLM, "chat_json", rl)
    with pytest.raises(RateLimited):
        await inst.caption_frames(items, ["x"] * 8)


def _anchor_part(sections, moments=()):
    return {"sections": [{"title": t, "start_sec": s, "bullets": ["b"], "overview": ""} for t, s in sections],
            "visual_moments": list(moments), "key_terms": []}


def test_section_anchor_frames_fill_talking_head_videos():
    secs = [(f"Stage {i}", i * 100) for i in range(8)]
    syn, _ = merge_parts([_anchor_part(secs)], 823, max_frames=8, min_frames=3)
    ms = syn["visual_moments"]
    assert len(ms) == 3 and all(m["source"] == "section_anchor" for m in ms)
    assert [m["timestamp_sec"] for m in ms] == sorted(m["timestamp_sec"] for m in ms)
    assert ms[0]["timestamp_sec"] == 6 and ms[-1]["timestamp_sec"] > 400  # spread across the video
    # LLM moments are kept and only topped up; anchors respect the min gap to them.
    llm_m = {"timestamp_sec": 104, "kind": "slide", "caption": "c", "reason": "r", "importance": 5}
    syn, _ = merge_parts([_anchor_part(secs, [llm_m])], 823, max_frames=8, min_frames=3)
    ts = [m["timestamp_sec"] for m in syn["visual_moments"]]
    assert 104 in ts and len(ts) == 3 and 106 not in ts
    # Enough LLM moments → no anchors; min_frames=0 → old behaviour.
    syn, _ = merge_parts([_anchor_part(secs)], 823, max_frames=8, min_frames=0)
    assert syn["visual_moments"] == []


def test_section_overview_is_rendered():
    syn = {"sections": [{"title": "Intro", "start_sec": 0, "bullets": ["x"], "overview": "Why this matters."}],
           "tldr": [], "key_terms": [], "review_questions": [], "visual_moments": []}
    blocks = build_blocks({"video_id": "dQw4w9WgXcQ", "job_id": "j"}, syn, [], {}, [])
    paras = [b for b in blocks if b["type"] == "paragraph"]
    assert paras and paras[0]["paragraph"]["rich_text"][0]["text"]["content"] == "Why this matters."


def test_chunking_weights_non_latin_scripts():
    from notetaker.llm import text_weight
    assert text_weight("hello") == 5 and text_weight("नमस्ते") == 3 * len("नमस्ते")
    eng = [{"start": i * 20, "text": "a" * 100} for i in range(40)]
    hin = [{"start": i * 20, "text": "क" * 100} for i in range(40)]
    assert len(chunk_lines(hin, 1200)) >= 3 * len(chunk_lines(eng, 1200)) - 1


async def test_empty_chunk_output_is_split_not_silently_dropped(monkeypatch):
    from notetaker import llm as llm_mod
    calls = []

    async def fake(self, messages, **kw):
        body = messages[1]["content"].split("TRANSCRIPT\n", 1)[1].split("\n\nReturn JSON", 1)[0]
        n = len(body.splitlines())
        calls.append(n)
        if n > 2:  # "context overflow": too big → empty object
            return {"sections": []}
        first = int(body.split("]", 1)[0][1:])
        return {"sections": [{"title": f"S{first}", "start_sec": first, "bullets": ["b"]}]}

    async def noop(self, *a, **k):
        return None
    monkeypatch.setattr(llm_mod.LLM, "chat_json", fake)
    monkeypatch.setattr(llm_mod.LLM, "metric", noop, raising=False)
    inst = llm_mod.LLM.__new__(llm_mod.LLM)
    chunk = [{"start": i * 20, "text": f"line {i}"} for i in range(8)]
    part = await inst.synthesize_chunk("t", 200, chunk, 0, 1, 160, True)
    assert [s["start_sec"] for s in part["sections"]] == [0, 40, 80, 120]  # whole span covered
    assert calls[0] == 8


async def test_default_parent_not_shared_falls_back_but_explicit_parent_fails(monkeypatch):
    from notetaker import notion as N
    calls = []

    async def fake_request(self, method, path, **kw):
        calls.append((method, path))
        if path.startswith("/pages/") or path.startswith("/databases/"):
            raise Permanent("not found", code="NOTION_PARENT_NOT_FOUND")
        return {}

    async def fake_search(self, query="", limit=50):
        return [{"id": "hub-1", "object": "page", "parent": {"type": "workspace"},
                 "properties": {"title": {"type": "title", "title": [{"plain_text": N.HUB_TITLE}]}}}]

    monkeypatch.setattr(N.NotionClient, "request", fake_request)
    monkeypatch.setattr(N.NotionClient, "search_pages", fake_search)
    monkeypatch.setattr(N.settings.__class__, "notion_default_parent_id",
                        property(lambda s: "4190db350ccb80df988de34170bbe5f3"), raising=False)
    c = N.NotionClient.__new__(N.NotionClient)
    # server default not visible to this OAuth workspace → the user's own hub page
    assert await c.resolve_parent(None) == {"type": "page_id", "page_id": "hub-1"}
    # a destination the user explicitly chose must not be silently replaced
    with pytest.raises(Permanent):
        await c.resolve_parent("11111111222233334444555555555555")


async def test_llm_retries_brief_5xx_but_not_4xx(monkeypatch):
    import httpx
    import openai
    from notetaker import llm as llm_mod

    async def nosleep(_):
        return None
    monkeypatch.setattr(llm_mod.asyncio, "sleep", nosleep)

    def err(code):
        req = httpx.Request("POST", "https://x/v1/chat/completions")
        return openai.APIStatusError("e", response=httpx.Response(code, request=req), body=None)

    calls = []

    class FakeCompletions:
        async def create(self, **kw):
            calls.append(1)
            if len(calls) < 3:
                raise err(503)
            return "ok"

    inst = llm_mod.LLM.__new__(llm_mod.LLM)
    inst.client = type("C", (), {"chat": type("Ch", (), {"completions": FakeCompletions()})()})()

    async def noop(*a, **k):
        return None
    inst.metric = noop
    assert await inst._create_with_5xx_retry({}) == "ok" and len(calls) == 3   # 503, 503, then success

    class Always400:
        async def create(self, **kw):
            calls.append(1)
            raise err(400)
    calls.clear()
    inst.client.chat.completions = Always400()
    with pytest.raises(openai.APIStatusError):
        await inst._create_with_5xx_retry({})
    assert len(calls) == 1   # 4xx is never retried in-call
