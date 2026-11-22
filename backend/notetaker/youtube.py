"""YouTube access: URL parsing, metadata + stream resolution (yt-dlp), timestamped transcripts.

Transcript strategy:
  1. youtube-transcript-api (prefer manual English, then generated English, then any language)
  2. fallback: caption tracks advertised by yt-dlp (json3), when (1) is blocked or empty
Everything here is synchronous and runs in a worker thread.
"""
from __future__ import annotations

import html
import logging
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx

from .config import settings
from .errors import Permanent, RateLimited, Retryable

log = logging.getLogger(__name__)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com",
            "www.youtube-nocookie.com"}
FORMAT = "bv*[height<=720][vcodec^=avc1]/bv*[height<=720]/b[height<=720]/bv*/b"
NOISE_RE = re.compile(r"^\s*[\[(](music|applause|laughter|silence|inaudible|__)[\])]\s*$", re.I)


# --------------------------------------------------------------------------- parsing
def parse_video_id(url: str) -> str | None:
    """Accepts watch / youtu.be / shorts / embed / live URLs. Returns the 11-char id or None."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if VIDEO_ID_RE.match(url):
        return None  # a bare id is not a URL; the API contract requires youtube_url
    if "://" not in url:
        url = "https://" + url
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    candidate = None
    if host in ("youtu.be", "www.youtu.be"):
        candidate = p.path.lstrip("/").split("/")[0]
    elif host in YT_HOSTS:
        if p.path == "/watch":
            candidate = (parse_qs(p.query).get("v") or [None])[0]
        else:
            parts = [x for x in p.path.split("/") if x]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v", "e"):
                candidate = parts[1]
    if candidate and VIDEO_ID_RE.match(candidate):
        return candidate
    return None


def watch_url(video_id: str, t: int | None = None) -> str:
    return f"https://www.youtube.com/watch?v={video_id}" + (f"&t={int(t)}s" if t is not None else "")


def fmt_ts(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- errors
def classify_youtube_error(msg: str) -> Exception:
    low = msg.lower()
    if "429" in low or "too many requests" in low:
        return RateLimited("youtube", 60, message="YouTube is rate-limiting requests (HTTP 429)")
    if "not a bot" in low or "confirm you" in low and "bot" in low:
        return RateLimited("youtube", 90, message="YouTube bot-check challenge (treated as rate limit)")
    if "sign in to confirm your age" in low or "age-restricted" in low or "age restricted" in low:
        return Permanent("This video is age-restricted and cannot be processed", code="AGE_RESTRICTED")
    if any(s in low for s in ("private video", "video unavailable", "has been removed", "not available",
                              "members-only", "join this channel", "account associated", "terminated",
                              "does not exist", "unavailable")):
        # Deliberately retryable: YouTube sometimes reports transient blocks as "unavailable".
        # The bounded retry budget then routes genuinely private/removed videos to the DLQ.
        return Retryable("Video is private, removed or unavailable", code="VIDEO_UNAVAILABLE")
    if "incomplete youtube id" in low or "is not a valid url" in low:
        return Permanent("Not a valid YouTube video", code="INVALID_VIDEO")
    return Retryable(f"YouTube request failed: {msg[:300]}", code="YOUTUBE_ERROR")


# --------------------------------------------------------------------------- metadata
def _ydl_opts(with_format: bool = True) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "extractor_retries": 1,
        "ignore_no_formats_error": True,
        "cachedir": "/tmp/yt-dlp-cache",
    }
    if with_format:
        opts["format"] = FORMAT
    if settings.ytdlp_cookies_file:
        opts["cookiefile"] = settings.ytdlp_cookies_file
    return opts


def _pick_stream(info: dict) -> dict | None:
    fmt = None
    for f in info.get("requested_formats") or []:
        if f.get("vcodec") not in (None, "none"):
            fmt = f
            break
    if fmt is None and info.get("url") and info.get("vcodec") not in (None, "none"):
        fmt = info
    if not fmt or not fmt.get("url"):
        return None
    url = fmt["url"]
    expire = None
    try:
        expire = int(parse_qs(urlparse(url).query).get("expire", [0])[0]) or None
    except (ValueError, TypeError):
        pass
    return {
        "url": url,
        "http_headers": fmt.get("http_headers") or info.get("http_headers") or {},
        "width": fmt.get("width"),
        "height": fmt.get("height"),
        "vcodec": fmt.get("vcodec"),
        "expire": expire or int(time.time()) + 3 * 3600,
    }


def _pick_audio_stream(info: dict) -> dict | None:
    from .asr import pick_audio_stream
    try:
        return pick_audio_stream(info.get("formats") or [])
    except Exception:  # noqa: BLE001
        return None


def _pick_scan_stream(info: dict) -> dict | None:
    from .visual import pick_scan_stream
    try:
        return pick_scan_stream(info.get("formats") or [])
    except Exception:  # noqa: BLE001 — the scan is an optimisation, never a reason to fail
        return None


def _caption_tracks(info: dict, langs: list[str]) -> list[dict]:
    """Ordered list of json3 caption tracks (manual first, then automatic)."""
    tracks: list[dict] = []
    for kind, source in (("manual", info.get("subtitles") or {}), ("auto", info.get("automatic_captions") or {})):
        keys = list(source.keys())
        ordered = [k for k in langs if k in source]
        ordered += [k for k in keys if k.startswith("en") and k not in ordered]
        ordered += [k for k in keys if k.endswith("-orig") and k not in ordered]
        if kind == "manual":
            ordered += [k for k in keys if k not in ordered]
        for lang in ordered:
            for f in source[lang]:
                if f.get("ext") == "json3" and f.get("url"):
                    tracks.append({"lang": lang, "kind": kind, "url": f["url"]})
                    break
    return tracks


def fetch_metadata(video_id: str) -> dict:
    """Title / duration / channel / live status + a direct video stream URL for frame capture."""
    import yt_dlp  # worker-only dependency

    url = watch_url(video_id)
    info = None
    for with_format in (True, False):
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(with_format)) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            if with_format and "requested format" in msg.lower():
                continue
            raise classify_youtube_error(msg) from None
    if not info:
        raise Retryable("YouTube returned no metadata", code="YOUTUBE_ERROR")
    return {
        "title": info.get("title") or "",
        "duration": int(info.get("duration") or 0),
        "channel": info.get("channel") or info.get("uploader") or "",
        "live_status": info.get("live_status") or ("is_live" if info.get("is_live") else "not_live"),
        "thumbnail": info.get("thumbnail"),
        "language": info.get("language"),
        "stream": _pick_stream(info),
        "scan_stream": _pick_scan_stream(info),
        "audio_stream": _pick_audio_stream(info),
        "caption_tracks": _caption_tracks(info, settings.transcript_languages),
    }


def fetch_oembed_title(video_id: str) -> str:
    try:
        r = httpx.get("https://www.youtube.com/oembed", params={"url": watch_url(video_id), "format": "json"},
                      timeout=10)
        if r.status_code == 200:
            return r.json().get("title", "")
    except httpx.HTTPError:
        pass
    return ""


# --------------------------------------------------------------------------- transcript
def _clean(segments: list[dict]) -> list[dict]:
    out = []
    for s in segments:
        text = html.unescape(str(s.get("text", ""))).replace("\n", " ").strip()
        if not text or NOISE_RE.match(text):
            continue
        out.append({"start": round(float(s["start"]), 2), "dur": round(float(s.get("dur") or 0), 2), "text": text})
    out.sort(key=lambda x: x["start"])
    return out


def _yte():
    import youtube_transcript_api._errors as yte  # noqa: PLC0415
    return yte


def _exc(*names):
    yte = _yte()
    return tuple(getattr(yte, n) for n in names if hasattr(yte, n))


def _via_transcript_api(video_id: str, langs: list[str]) -> tuple[list[dict], dict]:
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    tlist = api.list(video_id)
    info = {"source": "youtube-transcript-api"}
    try:
        tr = tlist.find_transcript(langs)
    except _exc("NoTranscriptFound"):
        tr = next(iter(tlist), None)
        if tr is None:
            raise
    info.update(language=tr.language_code, generated=bool(tr.is_generated))
    if not tr.language_code.startswith("en") and getattr(tr, "is_translatable", False):
        try:
            fetched = tr.translate("en").fetch()
            info["translated_from"] = tr.language_code
            info["language"] = "en"
        except Exception:  # noqa: BLE001 — translation is best-effort
            fetched = tr.fetch()
    else:
        fetched = tr.fetch()
    segs = [{"start": s.start, "dur": s.duration, "text": s.text} for s in fetched]
    return segs, info


def _via_caption_track(tracks: list[dict]) -> tuple[list[dict], dict]:
    last_err: Exception | None = None
    for t in tracks[:4]:
        try:
            r = httpx.get(t["url"], timeout=20)
        except httpx.HTTPError as e:
            last_err = Retryable(f"caption download failed: {e}", code="TRANSCRIPT_ERROR")
            continue
        if r.status_code == 429:
            raise RateLimited("youtube", float(r.headers.get("retry-after", 60) or 60))
        if r.status_code != 200:
            last_err = Retryable(f"caption track HTTP {r.status_code}", code="TRANSCRIPT_ERROR")
            continue
        segs = []
        for ev in r.json().get("events", []):
            if "segs" not in ev:
                continue
            text = "".join(s.get("utf8", "") for s in ev["segs"]).strip()
            if text:
                segs.append({"start": ev.get("tStartMs", 0) / 1000, "dur": ev.get("dDurationMs", 0) / 1000,
                             "text": text})
        if segs:
            return segs, {"source": "yt-dlp-captions", "language": t["lang"], "generated": t["kind"] == "auto"}
    if last_err:
        raise last_err
    raise Permanent("This video has no captions/transcript available", code="NO_TRANSCRIPT")


def fetch_transcript(video_id: str, caption_tracks: list[dict] | None = None,
                     audio_stream: dict | None = None) -> tuple[list[dict], dict]:
    """Captions via youtube-transcript-api → yt-dlp caption tracks → local speech-to-text on the audio.
    The last step covers caption endpoints that are IP-blocked and videos that have no captions."""
    langs = settings.transcript_languages
    blocked = _exc("RequestBlocked", "IpBlocked", "PoTokenRequired")
    missing = _exc("TranscriptsDisabled", "NoTranscriptFound")
    unavailable = _exc("VideoUnavailable", "VideoUnplayable", "InvalidVideoId")
    age = _exc("AgeRestricted")
    primary_error: Exception | None = None
    try:
        segs, info = _via_transcript_api(video_id, langs)
        segs = _clean(segs)
        if segs:
            return segs, info
    except age:
        raise Permanent("This video is age-restricted and cannot be processed", code="AGE_RESTRICTED") from None
    except unavailable as e:
        raise classify_youtube_error(f"unavailable: {e}") from None
    except blocked as e:
        primary_error = RateLimited("youtube", 120, message="YouTube blocked the transcript request")
        log.warning("transcript-api blocked for %s: %s", video_id, type(e).__name__)
    except missing:
        primary_error = Permanent("This video has no captions/transcript available", code="NO_TRANSCRIPT")
    except Exception as e:  # noqa: BLE001
        primary_error = classify_youtube_error(str(e))
        log.warning("transcript-api failed for %s: %r", video_id, e)

    if caption_tracks:
        try:
            segs, info = _via_caption_track(caption_tracks)
            segs = _clean(segs)
            if segs:
                info["fallback"] = True
                return segs, info
        except RateLimited as e:
            primary_error = e
            log.warning("caption-track fallback rate-limited for %s", video_id)
        except Exception as e:  # noqa: BLE001
            log.warning("caption-track fallback failed for %s: %r", video_id, e)

    if settings.asr_fallback and audio_stream:
        from . import asr
        try:
            segs, info = asr.transcribe(audio_stream)
            segs = _clean(segs)
            if segs:
                info["fallback"] = True
                info["captions_error"] = getattr(primary_error, "code", None) or type(primary_error).__name__
                return segs, info
        except RateLimited:
            raise
        except Exception as e:  # noqa: BLE001 — report the original caption error, not the ASR one
            log.warning("speech-to-text fallback failed for %s: %r", video_id, e)
    raise primary_error or Permanent("This video has no captions/transcript available", code="NO_TRANSCRIPT")


def transcript_duration(segments: list[dict]) -> int:
    if not segments:
        return 0
    return int(max(s["start"] + s.get("dur", 0) for s in segments))


def to_lines(segments: list[dict], window: float = 20.0) -> list[dict]:
    """Merge caption fragments into ~20s timestamped paragraphs (cheaper + clearer for the LLM)."""
    lines: list[dict] = []
    cur: dict | None = None
    for s in segments:
        if cur is None or s["start"] - cur["start"] >= window:
            if cur:
                lines.append(cur)
            cur = {"start": s["start"], "text": s["text"]}
        else:
            cur["text"] += " " + s["text"]
    if cur:
        lines.append(cur)
    return lines
