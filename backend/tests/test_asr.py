"""Transcript fallback chain: captions API → caption tracks → speech-to-text (no network)."""
import pytest

from notetaker import asr, youtube
from notetaker.errors import Permanent, RateLimited


def test_pick_audio_stream_prefers_lowest_bitrate_audio_only():
    fmts = [{"vcodec": "avc1", "acodec": "none", "url": "v", "protocol": "https"},
            {"vcodec": "none", "acodec": "opus", "abr": 130, "url": "hi", "protocol": "https"},
            {"vcodec": "none", "acodec": "opus", "abr": 48, "url": "lo", "protocol": "https"},
            {"vcodec": "none", "acodec": "opus", "abr": 30, "url": "m3u8", "protocol": "m3u8_native"}]
    assert asr.pick_audio_stream(fmts)["url"] == "lo"
    assert asr.pick_audio_stream([]) is None


def _blocked(*a, **k):
    raise youtube._exc("IpBlocked")[0]("dQw4w9WgXcQ")  # what youtube-transcript-api raises on an IP block


def test_blocked_captions_fall_back_to_speech_to_text(monkeypatch):
    monkeypatch.setattr(youtube, "_via_transcript_api", _blocked)
    monkeypatch.setattr(youtube, "_via_caption_track",
                        lambda tracks: (_ for _ in ()).throw(RateLimited("youtube", 60)))
    monkeypatch.setattr(asr, "transcribe", lambda stream: (
        [{"start": 0.0, "dur": 2.0, "text": "hello world"}], {"source": "whisper-base", "asr": True}))
    segs, info = youtube.fetch_transcript("dQw4w9WgXcQ", [{"lang": "en", "kind": "manual", "url": "u"}], {"url": "a"})
    assert segs[0]["text"] == "hello world" and info["asr"] and info["fallback"]


def test_asr_failure_reports_the_original_caption_error(monkeypatch):
    monkeypatch.setattr(youtube, "_via_transcript_api", _blocked)
    monkeypatch.setattr(asr, "transcribe", lambda stream: (_ for _ in ()).throw(Permanent("no speech")))
    with pytest.raises(RateLimited):  # retried with backoff, not a misleading ASR error
        youtube.fetch_transcript("dQw4w9WgXcQ", [], {"url": "a"})


def test_no_audio_stream_keeps_old_behaviour(monkeypatch):
    monkeypatch.setattr(youtube, "_via_transcript_api", _blocked)
    with pytest.raises(RateLimited):
        youtube.fetch_transcript("dQw4w9WgXcQ", [], None)
