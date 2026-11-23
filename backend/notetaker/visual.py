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


def _split_jpegs(data: bytes) -> list[bytes]:
    idx = [m.start() for m in re.finditer(re.escape(JPEG_SOI), data)]
    return [data[a:b] for a, b in zip(idx, idx[1:] + [len(data)])]


def scan(stream: dict, timeout: int = 120, width: int = 320, every: float = 2.0) -> list[Thumb]:
    """One thumbnail every `every` seconds with exact timestamps.

    Note: decoding only keyframes (-skip_frame nokey) is faster but ffmpeg then stamps each keyframe with
    the *next* keyframe's pts (seconds late), so thumbnails would not line up with the full-quality
    capture. A full decode of a <=240p stream with the fps filter is still ~2 s for a 20-minute video."""
    headers = "".join(f"{k}: {v}\r\n" for k, v in (stream.get("http_headers") or {}).items())
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-i", stream["url"], "-an", "-vf", f"fps=1/{every:g},scale={width}:-2,showinfo",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "pipe:1"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise FrameError(f"visual scan timed out after {timeout}s") from e
    err = proc.stderr
    if b"429" in err[-2000:]:
        raise RateLimited("youtube", 60, message="YouTube rate-limited the scan stream")
    if b"403 Forbidden" in err or b"410 Gone" in err:
        raise StreamExpired("scan stream URL rejected")
    times = [float(x) for x in PTS_RE.findall(err)]
    jpegs = _split_jpegs(proc.stdout)
    if not jpegs:
        raise FrameError(f"visual scan produced no frames: {err[-300:].decode(errors='replace')}")
    n = min(len(times), len(jpegs)) if times else 0
    if n < len(jpegs) * 0.9:  # timestamps missing → cannot align; treat as a failed scan
        raise FrameError("visual scan returned frames without timestamps")
    return [_thumb(times[i], jpegs[i]) for i in range(n)]


def _thumb(t: float, jpeg: bytes) -> Thumb:
    import imagehash
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    img.load()
    g = np.asarray(img.convert("L"), dtype=np.float32)
    gx = np.abs(np.diff(g, axis=1))[:-1, :]
    gy = np.abs(np.diff(g, axis=0))[:, :-1]
    edges = float(((gx + gy) > 60).mean())
    return Thumb(t=t, img=img, phash=str(imagehash.phash(img)), edges=edges, std=float(g.std()),
                 emap=_edge_map(img))


def _edge_map(img: Image.Image, size: tuple[int, int] = (96, 54)) -> np.ndarray:
    g = np.asarray(img.convert("L").resize(size, Image.BILINEAR), dtype=np.float32)
    m = np.zeros(g.shape, dtype=bool)
    m[:, 1:] |= np.abs(np.diff(g, axis=1)) > 40
    m[1:, :] |= np.abs(np.diff(g, axis=0)) > 40
    return m


def edge_iou(a: Thumb, b: Thumb) -> float:
    if a.emap is None or b.emap is None:
        return 1.0
    # Dilate by one pixel so tiny jitter/compression does not count as change.
    def dil(m: np.ndarray) -> np.ndarray:
        d = m.copy()
        d[1:, :] |= m[:-1, :]; d[:-1, :] |= m[1:, :]; d[:, 1:] |= m[:, :-1]; d[:, :-1] |= m[:, 1:]
        return d
    inter = (dil(a.emap) & b.emap).sum() + (a.emap & dil(b.emap)).sum()
    tot = a.emap.sum() + b.emap.sum()
    return float(inter / tot) if tot else 1.0


def same_state(a: Thumb, b: Thumb, phash_max: int = 10, iou_min: float = 0.55) -> bool:
    return _dist(a.phash, b.phash) <= phash_max and edge_iou(a, b) >= iou_min


# --------------------------------------------------------------------------- 2-4. shots + scoring
def _dist(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def build_shots(thumbs: list[Thumb], duration: float, same: int = 10) -> list[Shot]:
    shots: list[Shot] = []
    for i, th in enumerate(thumbs):
        if th.std < 12:  # black / white transition frames never start or represent a state
            continue
        if shots and same_state(thumbs[shots[-1].thumbs[-1]], th, phash_max=same):
            shots[-1].thumbs.append(i)
        else:
            shots.append(Shot(start=th.t, end=th.t, thumbs=[i]))
    for k, s in enumerate(shots):
        nxt = shots[k + 1].start if k + 1 < len(shots) else (duration or thumbs[s.thumbs[-1]].t + 5)
        s.end = max(nxt, thumbs[s.thumbs[-1]].t)
        # Slides build up; the most complete view is late in the state (but not the transition frame).
        s.rep = s.thumbs[max(0, math.ceil(len(s.thumbs) * 0.7) - 1)]
        s.edges = float(np.median([thumbs[j].edges for j in s.thumbs]))
    return shots


def score_shots(shots: list[Shot], thumbs: list[Thumb], cluster_dist: int = 14) -> int | None:
    """Cluster visual states by look, find the baseline (largest dwell), and score every shot.
    Returns the baseline cluster id (or None when no state dominates, e.g. full-screen animation)."""
    reps: list[str] = []
    dwell: list[float] = []
    for s in shots:
        h = thumbs[s.rep].phash
        best = next((c for c, r in enumerate(reps) if _dist(r, h) <= cluster_dist), None)
        if best is None:
            reps.append(h)
            dwell.append(0.0)
            best = len(reps) - 1
        s.cluster = best
        dwell[best] += s.dwell
    total = sum(dwell) or 1.0
    base = max(range(len(dwell)), key=lambda c: dwell[c]) if dwell else None
    if base is not None and dwell[base] / total < 0.25:
        base = None  # no dominant presenter shot (animation / slides-only video)
    emax = max((s.edges for s in shots), default=0.0) or 1.0
    for s in shots:
        d = 1.0 if base is None else min(_dist(thumbs[s.rep].phash, reps[base]) / 28.0, 1.0)
        s.distinct = 0.0 if (base is not None and s.cluster == base) else d
        dw = min(math.log1p(s.dwell) / math.log1p(60), 1.0)
        s.score = 0.45 * s.distinct + 0.35 * (s.edges / emax) + 0.20 * dw
        if base is not None and s.cluster == base:
            s.score *= 0.35  # the presenter shot: only chosen when nothing better exists
        if s.dwell < 1.5 and len(s.thumbs) == 1:
            s.score *= 0.6  # flash / transition
    return base


def top_candidates(shots: list[Shot], thumbs: list[Thumb], k: int, min_gap: float = 10.0,
                   dup: int = 8) -> list[Shot]:
    """Highest-scoring visually distinct states, at least `min_gap` s apart."""
    out: list[Shot] = []
    for s in sorted(shots, key=lambda s: -s.score):
        if len(out) >= k:
            break
        t = thumbs[s.rep].t
        if any(abs(t - thumbs[o.rep].t) < min_gap
               or (_dist(thumbs[o.rep].phash, thumbs[s.rep].phash) <= dup and edge_iou(thumbs[o.rep], thumbs[s.rep]) >= 0.6)
               for o in out):
            continue
        out.append(s)
    return sorted(out, key=lambda s: thumbs[s.rep].t)


# --------------------------------------------------------------------------- 5. contact sheet
def contact_sheet(images: list[Image.Image], cols: int = 4, cell_w: int = 320) -> bytes:
    """Numbered grid used only for vision triage (never published)."""
    n = len(images)
    rows = max(1, math.ceil(n / cols))
    cell_h = int(cell_w * 9 / 16)
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), "white")
    draw = ImageDraw.Draw(sheet)
    for i, im in enumerate(images):
        x, y = (i % cols) * cell_w, (i // cols) * cell_h
        tile = im.copy()
        tile.thumbnail((cell_w - 4, cell_h - 4))
        sheet.paste(tile, (x + 2, y + 2))
        draw.rectangle([x + 2, y + 2, x + 34, y + 26], fill="black")
        draw.text((x + 8, y + 6), str(i), fill="yellow")
    buf = io.BytesIO()
    sheet.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


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

    plan: list[dict] = []
    used: set[int] = set()

    def spaced(t: float) -> bool:
        return all(abs(t - p["timestamp_sec"]) >= min_gap for p in plan)

    def from_cand(i: int, c: dict, base: dict | None, source: str) -> dict:
        kind = KIND_MAP.get(c.get("kind", ""), (base or {}).get("kind", "other"))
        m = {"timestamp_sec": int(round(c["t"])), "kind": kind, "importance": (base or {}).get("importance", 3),
             "caption": (base or {}).get("caption") or c.get("shows") or "On-screen visual",
             "reason": (base or {}).get("reason") or "Informative visual shown on screen at this point",
             "source": source, "visual_rating": c.get("rating"), "visual_shows": c.get("shows", ""),
             "dwell_sec": round(c["end"] - c["start"], 1), "offsets": [0, -1, 1]}
        m["section_index"] = _section_index(sections, m["timestamp_sec"])
        return m

    ranked = sorted(llm_moments, key=lambda m: (-m.get("importance", 3), m["timestamp_sec"]))
    for m in ranked:
        if len(plan) >= max_frames:
            break
        t = m["timestamp_sec"]
        near = [(i, c) for i, c in enumerate(cands) if i not in used and c.get("rating", 0) >= good
                and c["end"] >= t - before and c["start"] <= t + after]
        if not near:
            continue
        i, c = max(near, key=lambda ic: (ic[1]["rating"], -abs(ic[1]["t"] - t)))
        if not spaced(c["t"]):
            continue
        used.add(i)
        plan.append(from_cand(i, c, m, "llm+visual"))

    for want in (good, 1):
        for i, c in sorted(enumerate(cands), key=lambda ic: (-ic[1].get("rating", 0), -ic[1].get("score", 0))):
            if want == 1 and len(plan) >= min_frames:
                break
            if len(plan) >= max_frames:
                break
            if i in used or c.get("rating", 0) < want or not spaced(c["t"]):
                continue
            used.add(i)
            plan.append(from_cand(i, c, None, "visual"))
        if len(plan) >= min_frames:
            break

    for a in anchors:
        if len(plan) >= min(min_frames, max_frames):
            break
        if spaced(a["timestamp_sec"]):
            plan.append(dict(a))
    return sorted(plan, key=lambda m: m["timestamp_sec"])
