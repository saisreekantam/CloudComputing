"""Visual-first frame selection ("what is actually on screen", not only what the transcript implies).

Why: an LLM that only reads the transcript has to *guess* when a slide is visible. On talking-head,
picture-in-picture and screen-recording videos those guesses mostly land on the presenter. Lecture-video
literature (slide-transition detection, keyframe selection with LLM validation) instead mines the video
itself for stable, distinctive visual states, then aligns them with the transcript.

Pipeline (all analysis images are throwaway; published images are still captured from the full-quality
stream at the chosen timestamp, so Constraint B is unaffected):

  1. scan      — decode a low-res (<=240p) stream keeping one frame every 2 s: a 20-min video takes ~2 s
                 and yields ~600 thumbnails with exact timestamps.
  2. shots     — group consecutive near-identical thumbnails (pHash) into visual states; dwell time =
                 how long a state stays on screen (slides stay; transitions don't).
  3. baseline  — the visual state with the largest total dwell is usually the presenter shot. States that
                 look unlike it are "content" (slides, overlays, code, diagrams).
  4. score     — distinctiveness from the baseline + edge/text density + dwell.
  5. triage    — the top candidates go to the vision model as ONE numbered contact sheet; it rates how
                 informative each one is (see llm.LLM.rate_contact_sheet).
"""
from __future__ import annotations

import io
import math
import re
import subprocess
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw

from .errors import RateLimited
from .frames import FrameError, StreamExpired

PTS_RE = re.compile(rb"pts_time:\s*([0-9.]+)")
JPEG_SOI = b"\xff\xd8\xff"


@dataclass
class Thumb:
    t: float
    img: Image.Image
    phash: str
    edges: float  # fraction of strong-gradient pixels (text, lines, diagrams)
    std: float
    emap: np.ndarray | None = None  # coarse binary edge map: tells apart white slides that pHash confuses


@dataclass
class Shot:
    start: float
    end: float
    thumbs: list[int] = field(default_factory=list)
    rep: int = 0          # index (into thumbs) of the representative thumbnail
    edges: float = 0.0
    distinct: float = 0.0  # 0..1 distance from the baseline (presenter) look
    score: float = 0.0
    cluster: int = -1

    @property
    def dwell(self) -> float:
        return max(self.end - self.start, 0.0)

    def to_dict(self, thumbs: list[Thumb]) -> dict:
        return {"start": round(self.start, 2), "end": round(self.end, 2), "t": round(thumbs[self.rep].t, 2),
                "edges": round(self.edges, 4), "distinct": round(self.distinct, 3), "score": round(self.score, 3),
                "dwell": round(self.dwell, 1), "phash": thumbs[self.rep].phash}


# --------------------------------------------------------------------------- 1. scan
def pick_scan_stream(formats: list[dict]) -> dict | None:
    """Smallest video-only HTTP format between 144p and 360p (prefer 240p H.264: fast to decode)."""
    fs = [f for f in formats or []
          if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")
          and f.get("url") and str(f.get("protocol", "")).startswith("http")
          and 140 <= (f.get("height") or 0) <= 360]
    if not fs:
        return None
    fs.sort(key=lambda f: (abs((f.get("height") or 0) - 240), 0 if str(f.get("vcodec", "")).startswith("avc1") else 1))
    f = fs[0]
    return {"url": f["url"], "http_headers": f.get("http_headers") or {}, "height": f.get("height")}



def scan(stream: dict, timeout: int = 120, width: int = 320, every: float = 2.0) -> list[Thumb]:
    """Decode `stream` at <=240p and return one Thumb every `every` seconds with exact timestamps."""
    raise NotImplementedError("visual scan")


def edge_iou(a: Thumb, b: Thumb) -> float:
    """Overlap (0..1) of the two coarse edge maps; tells apart white slides that pHash confuses."""
    raise NotImplementedError("edge_iou")


def same_state(a: Thumb, b: Thumb, phash_max: int = 10, iou_min: float = 0.55) -> bool:
    raise NotImplementedError("same_state")


# --------------------------------------------------------------------------- 2-4. shots + scoring
def build_shots(thumbs: list[Thumb], duration: float, same: int = 10) -> list[Shot]:
    """Group consecutive near-identical thumbnails into visual states (Shot) with dwell time."""
    raise NotImplementedError("build_shots")


def score_shots(shots: list[Shot], thumbs: list[Thumb], cluster_dist: int = 14) -> int | None:
    """Cluster states by look, find the baseline (presenter) cluster, score every shot in place.
    Returns the baseline cluster id, or None when no state dominates."""
    raise NotImplementedError("score_shots")


def top_candidates(shots: list[Shot], thumbs: list[Thumb], k: int, min_gap: float = 10.0,
                   dup: int = 8) -> list[Shot]:
    """Highest-scoring visually distinct states, at least `min_gap` s apart, sorted by time."""
    raise NotImplementedError("top_candidates")


# --------------------------------------------------------------------------- 5. contact sheet
def contact_sheet(images: list[Image.Image], cols: int = 4, cell_w: int = 320) -> bytes:
    """Numbered JPEG grid used only for vision triage (never published)."""
    raise NotImplementedError("contact_sheet")


# --------------------------------------------------------------------------- 6. selection
KIND_MAP = {"slide": "slide", "diagram": "diagram", "chart": "chart", "code": "code", "equation": "equation",
            "table": "table", "screenshot": "demo", "demo": "demo", "text": "slide"}


def _section_index(sections: list[dict], t: float) -> int:
    idx = 0
    for i, s in enumerate(sections):
        if s["start_sec"] <= t:
            idx = i
    return idx


def select_frames(moments: list[dict], cands: list[dict] | None, sections: list[dict], max_frames: int,
                  min_frames: int, *, good: int = 2, before: float = 12, after: float = 25,
                  min_gap: float = 12) -> list[dict]:
    """Final frame plan from transcript moments (LLM) + visual candidates (scan + vision triage).

    cands: [{t, start, end, rating, kind, shows, score}] or None when the scan was unavailable.
      1. every transcript moment is snapped to the best well-rated visual state overlapping
         [t - before, t + after]; unsnapped moments are dropped (they would land on the presenter)
         unless the scan was unavailable, in which case the old behaviour is kept;
      2. well-rated visual states nobody mentioned are added (best first, spaced >= min_gap);
      3. still short of min_frames → weaker states (rating 1), then section anchors.
    """
    llm_moments = [m for m in moments if m.get("source") != "section_anchor"]
    anchors = [m for m in moments if m.get("source") == "section_anchor"]
    if cands is None:  # no scan: transcript-only behaviour
        plan = list(llm_moments)
        for a in anchors:
            if len(plan) >= max(min_frames, 0):
                break
            plan.append(a)
        return sorted(plan[:max_frames], key=lambda m: m["timestamp_sec"])

    raise NotImplementedError("snap transcript moments onto visual candidates")
