"""Visual-first frame selection (pure logic; no network)."""
import io

import numpy as np
from PIL import Image, ImageDraw

from notetaker import visual as V


def _thumb(t, kind):
    img = Image.new("RGB", (320, 180), "white" if kind.startswith("slide") else (90, 70, 50))
    d = ImageDraw.Draw(img)
    if kind == "presenter":
        d.ellipse([130, 30, 190, 100], fill=(230, 200, 170))
        d.rectangle([110, 100, 210, 180], fill=(250, 250, 250))
    elif kind == "slideA":
        for y in range(20, 160, 20):
            d.line([20, y, 200, y], fill="black", width=2)
    elif kind == "slideB":
        d.rectangle([40, 40, 120, 120], outline="black", width=3)
        d.ellipse([180, 40, 260, 120], outline="black", width=3)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return V._thumb(t, buf.getvalue())


def test_scan_pipeline_prefers_slides_over_presenter_and_separates_white_slides():
    seq = ["presenter"] * 12 + ["slideA"] * 4 + ["presenter"] * 10 + ["slideB"] * 4 + ["presenter"] * 8
    thumbs = [_thumb(i * 5.0, k) for i, k in enumerate(seq)]
    shots = V.build_shots(thumbs, len(seq) * 5.0)
    assert len(shots) == 5  # presenter / A / presenter / B / presenter
    base = V.score_shots(shots, thumbs)
    assert base is not None
    top = V.top_candidates(shots, thumbs, 2)
    assert sorted(round(thumbs[s.rep].t) for s in top) == [70, 140]  # late in each slide state (A: 60-75s, B: 130-145s)
    sheet = Image.open(io.BytesIO(V.contact_sheet([thumbs[s.rep].img for s in top])))
    assert sheet.size[0] == 4 * 320


def _cand(t, rating, start=None, end=None, kind="slide"):
    return {"t": t, "start": start if start is not None else t - 3, "end": end if end is not None else t + 6,
            "rating": rating, "kind": kind, "shows": f"visual@{t}", "score": rating / 3}


SECTIONS = [{"title": "A", "start_sec": 0}, {"title": "B", "start_sec": 300}]


def test_select_snaps_transcript_moments_to_nearby_visuals_and_drops_presenter_guesses():
    moments = [{"timestamp_sec": 100, "kind": "diagram", "caption": "c1", "reason": "r1", "importance": 5},
               {"timestamp_sec": 500, "kind": "slide", "caption": "c2", "reason": "r2", "importance": 4}]
    cands = [_cand(110, 3), _cand(320, 2), _cand(700, 0)]
    plan = V.select_frames(moments, cands, SECTIONS, max_frames=10, min_frames=3)
    ts = [p["timestamp_sec"] for p in plan]
    # 110 snapped (kept the LLM caption); 500 had no visual nearby → dropped (would be the presenter);
    # 700 is rated 0 (presenter / end card) → never used, even to reach min_frames.
    assert ts == [110, 320]
    assert plan[0]["source"] == "llm+visual" and plan[0]["caption"] == "c1"
    assert plan[1]["source"] == "visual" and plan[1]["section_index"] == 1


def test_select_falls_back_to_transcript_when_scan_unavailable():
    moments = [{"timestamp_sec": 50, "kind": "slide", "caption": "c", "reason": "r", "importance": 3},
               {"timestamp_sec": 400, "kind": "other", "caption": "a", "reason": "a", "importance": 1,
                "source": "section_anchor"}]
    plan = V.select_frames(moments, None, SECTIONS, max_frames=10, min_frames=2)
    assert [p["timestamp_sec"] for p in plan] == [50, 400]


def test_select_respects_max_frames_and_spacing():
    cands = [_cand(t, 3) for t in range(0, 200, 5)]
    plan = V.select_frames([], cands, SECTIONS, max_frames=6, min_frames=3, min_gap=12)
    ts = [p["timestamp_sec"] for p in plan]
    assert len(ts) == 6 and all(b - a >= 12 for a, b in zip(ts, ts[1:]))
