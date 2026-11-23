"""Speech-to-text fallback for the transcript stage (beyond spec).

YouTube's caption endpoints are rate-limited per public IP (a whole campus NAT can be blocked at
once) and some videos have no captions at all. The audio stream, however, comes from the media CDN
and keeps working. When both caption paths fail we download the lowest-bitrate audio track with
ffmpeg (~3 s for 20 minutes) and transcribe it locally with faster-whisper on CPU, producing the
same timestamped segments the rest of the pipeline expects.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time

import numpy as np

from .config import settings
from .errors import Permanent, RateLimited, Retryable

log = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()


def pick_audio_stream(formats: list[dict]) -> dict | None:
    """Lowest-bitrate audio-only HTTP format (speech needs very little)."""
    fs = [f for f in formats or [] if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
          and f.get("url") and str(f.get("protocol", "")).startswith("http")]
    if not fs:
        return None
    f = min(fs, key=lambda f: f.get("abr") or f.get("tbr") or 1e9)
    return {"url": f["url"], "http_headers": f.get("http_headers") or {}, "abr": f.get("abr")}


def _load_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(settings.asr_model, device="cpu", compute_type="int8",
                                  cpu_threads=settings.asr_threads)
        return _model


def download_audio(stream: dict, timeout: int = 180) -> np.ndarray:
    headers = "".join(f"{k}: {v}\r\n" for k, v in (stream.get("http_headers") or {}).items())
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-i", stream["url"], "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise Retryable(f"audio download timed out after {timeout}s", code="TRANSCRIPT_ERROR") from e
    err = proc.stderr.decode(errors="replace")[-300:]
    if "429" in err:
        raise RateLimited("youtube", 60, message="YouTube rate-limited the audio stream")
    if proc.returncode != 0 or len(proc.stdout) < 32000:
        raise Retryable(f"audio download failed: {err.strip() or 'no audio'}", code="TRANSCRIPT_ERROR")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(stream: dict) -> tuple[list[dict], dict]:
    t0 = time.time()
    audio = download_audio(stream)
    t1 = time.time()
    model = _load_model()
    segments, info = model.transcribe(audio, vad_filter=True, beam_size=1, condition_on_previous_text=False)
    segs = [{"start": round(s.start, 2), "dur": round(s.end - s.start, 2), "text": s.text.strip()}
            for s in segments if s.text.strip()]
    if not segs:
        raise Permanent("No speech could be transcribed from the audio", code="NO_TRANSCRIPT")
    log.info("ASR %s: %.0f min audio, download %.1fs, transcribe %.1fs, %d segments, lang=%s",
             settings.asr_model, len(audio) / 16000 / 60, t1 - t0, time.time() - t1, len(segs), info.language)
    return segs, {"source": f"whisper-{settings.asr_model}", "language": info.language, "generated": True,
                  "asr": True, "asr_seconds": round(time.time() - t0, 1)}
