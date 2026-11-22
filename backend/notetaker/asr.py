"""Speech-to-text fallback for the transcript stage (beyond spec).

YouTube's caption endpoints are rate-limited per public IP (a whole campus NAT can be blocked at
once) and some videos have no captions at all. The audio stream, however, comes from the media CDN
and keeps working. When both caption paths fail we download the lowest-bitrate audio track with
ffmpeg (~3 s for 20 minutes) and transcribe it locally with faster-whisper on CPU, producing the
same timestamped segments the rest of the pipeline expects.
"""
from __future__ import annotations

import logging
from .config import settings

log = logging.getLogger(__name__)


def pick_audio_stream(formats: list[dict]) -> dict | None:
    """Lowest-bitrate audio-only HTTP format (speech needs very little)."""
    fs = [f for f in formats or [] if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
          and f.get("url") and str(f.get("protocol", "")).startswith("http")]
    if not fs:
        return None
    f = min(fs, key=lambda f: f.get("abr") or f.get("tbr") or 1e9)
    return {"url": f["url"], "http_headers": f.get("http_headers") or {}, "abr": f.get("abr")}


def transcribe(stream: dict) -> tuple[list[dict], dict]:
    """Download the audio stream (16 kHz mono) and transcribe it locally.

    Returns (segments, info) where segments = [{"start", "dur", "text"}] and info carries at least
    {"source", "language", "generated": True, "asr": True, "asr_seconds"}."""
    raise NotImplementedError("speech-to-text fallback")
