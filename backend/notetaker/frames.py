"""Frame capture straight from the remote video stream with ffmpeg (no full download).

Quality gate (an addition beyond the spec): for each flagged timestamp t we capture real
frames at t+offset for a few small offsets (default 0, 2, 4 s). We reject near-uniform frames
(black or white transition frames) and keep the sharpest one, measured by Laplacian variance.
That avoids publishing a motion-blurred or mid-fade frame. Every image is still a genuine
frame from the source video, and the exact timestamp used is recorded and shown in Notion.
"""
from __future__ import annotations

import base64
import io
import logging
import subprocess
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .errors import RateLimited

log = logging.getLogger(__name__)


class FrameError(Exception):
    pass


class StreamExpired(FrameError):
    pass


@dataclass
class Frame:
    timestamp: float
    jpeg: bytes
    width: int
    height: int
    sharpness: float
    phash: str


def grab(stream: dict, t: float, timeout: int) -> bytes:
    headers = "".join(f"{k}: {v}\r\n" for k, v in (stream.get("http_headers") or {}).items())
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-ss", f"{max(t, 0):.3f}", "-i", stream["url"], "-frames:v", "1", "-an",
            "-q:v", "2", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise FrameError(f"ffmpeg timed out after {timeout}s at t={t:.1f}") from e
    err = proc.stderr.decode(errors="replace")[-400:]
    if "429" in err:
        raise RateLimited("youtube", 60, message="YouTube rate-limited the video stream")
    if "403" in err or "410" in err:
        raise StreamExpired(f"stream URL rejected: {err.strip()}")
    if proc.returncode != 0 or len(proc.stdout) < 1000:
        raise FrameError(f"ffmpeg failed at t={t:.1f}: {err.strip() or 'no output'}")
    return proc.stdout


def analyze(jpeg: bytes) -> tuple[Image.Image, float, float]:
    img = Image.open(io.BytesIO(jpeg))
    img.load()
    g = img.convert("L")
    g.thumbnail((480, 480))
    a = np.asarray(g, dtype=np.float32)
    std = float(a.std())
    lap = a[1:-1, 2:] + a[1:-1, :-2] + a[2:, 1:-1] + a[:-2, 1:-1] - 4.0 * a[1:-1, 1:-1]
    return img, std, float(lap.var())


def phash(img: Image.Image) -> str:
    import imagehash
    return str(imagehash.phash(img))


def phash_distance(a: str, b: str) -> int:
    import imagehash
    return int(imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b))  # numpy int64 → JSON-safe int


def capture_best(stream: dict, t: float, duration: float, offsets: list[int], timeout: int) -> Frame:
    """Capture candidates around t and return the best non-blank one."""
    candidates: list[Frame] = []
    errors: list[str] = []
    upper = max(duration - 1.0, 0.0)
    seen: set[float] = set()
    for off in offsets:
        ts = round(min(max(t + off, 0.0), upper), 2)
        if ts in seen:
            continue
        seen.add(ts)
        try:
            data = grab(stream, ts, timeout)
        except (RateLimited, StreamExpired):
            raise
        except FrameError as e:
            errors.append(str(e))
            continue
        img, std, sharp = analyze(data)
        if std < 12.0:  # near-uniform → black/white transition frame
            errors.append(f"blank frame at {ts:.1f}s (std={std:.1f})")
            continue
        candidates.append(Frame(ts, data, img.width, img.height, sharp, phash(img)))
        if off == offsets[0] and sharp > 400:  # already crisp at the exact timestamp — don't over-fetch
            break
    if not candidates:
        raise FrameError("; ".join(errors) or "no usable frame")
    # Prefer the sharpest; ties go to the earliest (closest to the LLM's timestamp).
    return max(candidates, key=lambda f: (round(f.sharpness, -1), -f.timestamp))


def vision_jpeg_b64(jpeg: bytes, max_side: int = 768) -> str:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()
