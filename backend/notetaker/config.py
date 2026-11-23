"""Central configuration — every tunable comes from an environment variable.

The README documents each variable; defaults are chosen so `docker compose up`
works with only an LLM key and Notion credentials supplied.
"""
from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_list(name: str, default: str) -> list[int]:
    raw = os.getenv(name) or default
    return [int(x) for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    # --- infrastructure -------------------------------------------------
    redis_url: str = field(default_factory=lambda: _str("REDIS_URL", "redis://localhost:6379/0"))
    amqp_url: str = field(default_factory=lambda: _str("AMQP_URL", "amqp://notetaker:notetaker@localhost:5672/"))
    rabbit_mgmt_url: str = field(default_factory=lambda: _str("RABBITMQ_MGMT_URL", "http://notetaker:notetaker@localhost:15672"))
    public_base_url: str = field(default_factory=lambda: _str("PUBLIC_BASE_URL", "http://localhost:8000"))
    admin_token: str = field(default_factory=lambda: _str("ADMIN_TOKEN", "dev-admin"))
    token_enc_key: str = field(default_factory=lambda: _str("TOKEN_ENC_KEY", ""))

    # --- LLM --------------------------------------------------------------
    llm_base_url: str = field(default_factory=lambda: _str("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"))
    llm_api_key: str = field(default_factory=lambda: _str("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _str("LLM_MODEL", "gemini-3.5-flash-lite"))
    llm_vision_model: str = field(default_factory=lambda: _str("LLM_VISION_MODEL", ""))
    llm_rpm: int = field(default_factory=lambda: _int("LLM_RPM", 10))
    llm_tpm: int = field(default_factory=lambda: _int("LLM_TPM", 250_000))
    llm_max_output_tokens: int = field(default_factory=lambda: _int("LLM_MAX_OUTPUT_TOKENS", 8192))
    llm_chunk_chars: int = field(default_factory=lambda: _int("LLM_CHUNK_CHARS", 24_000))
    llm_json_mode: bool = field(default_factory=lambda: _bool("LLM_JSON_MODE", True))
    llm_timeout_sec: float = field(default_factory=lambda: _float("LLM_TIMEOUT_SEC", 120.0))
    vision_verify: bool = field(default_factory=lambda: _bool("VISION_VERIFY", True))
    vision_batch: int = field(default_factory=lambda: _int("VISION_BATCH", 4))
    notes_language: str = field(default_factory=lambda: _str("NOTES_LANGUAGE", "English"))

    # --- Notion -----------------------------------------------------------
    notion_client_id: str = field(default_factory=lambda: _str("NOTION_CLIENT_ID"))
    notion_client_secret: str = field(default_factory=lambda: _str("NOTION_CLIENT_SECRET"))
    notion_default_parent_id: str = field(default_factory=lambda: _str("NOTION_DEFAULT_PARENT_ID"))
    notion_fallback_token: str = field(default_factory=lambda: _str("NOTION_FALLBACK_TOKEN"))
    notion_version: str = field(default_factory=lambda: _str("NOTION_VERSION", "2022-06-28"))
    notion_rps: float = field(default_factory=lambda: _float("NOTION_RPS", 2.5))

    # --- YouTube ----------------------------------------------------------
    youtube_rpm: int = field(default_factory=lambda: _int("YOUTUBE_RPM", 30))
    ytdlp_cookies_file: str = field(default_factory=lambda: _str("YTDLP_COOKIES_FILE"))
    asr_fallback: bool = field(default_factory=lambda: _bool("ASR_FALLBACK", True))
    asr_model: str = field(default_factory=lambda: _str("ASR_MODEL", "base"))
    asr_threads: int = field(default_factory=lambda: _int("ASR_THREADS", 8))
    transcript_cache_days: int = field(default_factory=lambda: _int("TRANSCRIPT_CACHE_DAYS", 7))
    transcript_languages: list[str] = field(default_factory=lambda: [x.strip() for x in _str("TRANSCRIPT_LANGUAGES", "en,en-US,en-GB").split(",") if x.strip()])

    # --- pipeline limits ---------------------------------------------------
    max_video_seconds: int = field(default_factory=lambda: _int("MAX_VIDEO_SECONDS", 1200))
    max_frames: int = field(default_factory=lambda: _int("MAX_FRAMES", 10))
    min_frames: int = field(default_factory=lambda: _int("MIN_FRAMES", 3))
    visual_scan: bool = field(default_factory=lambda: _bool("VISUAL_SCAN", True))
    visual_candidates: int = field(default_factory=lambda: _int("VISUAL_CANDIDATES", 16))
    concept_graph: bool = field(default_factory=lambda: _bool("CONCEPT_GRAPH", True))
    llm_concurrency: int = field(default_factory=lambda: _int("LLM_CONCURRENCY", 3))
    llm_5xx_retries: int = field(default_factory=lambda: _int("LLM_5XX_RETRIES", 2))
    frame_candidate_offsets: list[int] = field(default_factory=lambda: _int_list("FRAME_CANDIDATE_OFFSETS", "0,2,4"))
    frame_timeout_sec: int = field(default_factory=lambda: _int("FRAME_TIMEOUT_SEC", 40))
    frame_concurrency: int = field(default_factory=lambda: _int("FRAME_CONCURRENCY", 3))
    phash_dup_threshold: int = field(default_factory=lambda: _int("PHASH_DUP_THRESHOLD", 4))

    # --- retries / reliability -------------------------------------------
    max_attempts: int = field(default_factory=lambda: _int("MAX_ATTEMPTS", 5))
    max_rate_limit_retries: int = field(default_factory=lambda: _int("MAX_RATE_LIMIT_RETRIES", 8))
    retry_backoff_seconds: list[int] = field(default_factory=lambda: _int_list("RETRY_BACKOFF_SECONDS", "10,30,90,270"))
    defer_seconds: int = field(default_factory=lambda: _int("DEFER_SECONDS", 2))
    lease_seconds: int = field(default_factory=lambda: _int("LEASE_SECONDS", 12))
    max_local_wait_sec: float = field(default_factory=lambda: _float("MAX_LOCAL_WAIT_SEC", 45.0))
    broker_delivery_limit: int = field(default_factory=lambda: _int("BROKER_DELIVERY_LIMIT", 10))
    crash_redelivery_limit: int = field(default_factory=lambda: _int("CRASH_REDELIVERY_LIMIT", 4))
    job_ttl_days: int = field(default_factory=lambda: _int("JOB_TTL_DAYS", 7))

    # --- worker -----------------------------------------------------------
    worker_stages: list[str] = field(default_factory=lambda: [s.strip() for s in _str("WORKER_STAGES", "transcribe,summarize,frames,publish").split(",") if s.strip()])
    worker_heartbeat_sec: int = field(default_factory=lambda: _int("WORKER_HEARTBEAT_SEC", 2))

    @property
    def vision_model(self) -> str:
        return self.llm_vision_model or self.llm_model

    @property
    def fernet_key(self) -> bytes:
        """Fernet key: explicit TOKEN_ENC_KEY, else derived from ADMIN_TOKEN (dev only)."""
        if self.token_enc_key:
            return self.token_enc_key.encode()
        digest = hashlib.sha256(("notetaker::" + self.admin_token).encode()).digest()
        return base64.urlsafe_b64encode(digest)

    def backoff_for(self, attempt: int) -> int:
        """Delay (seconds) before retry number `attempt` (1-based)."""
        tiers = self.retry_backoff_seconds or [10]
        return tiers[min(max(attempt, 1), len(tiers)) - 1]

    def tier_at_least(self, seconds: float) -> int:
        """Smallest configured delay tier >= seconds (used to honour Retry-After)."""
        tiers = sorted(set(self.retry_backoff_seconds + [self.defer_seconds]))
        for t in tiers:
            if t >= seconds:
                return t
        return tiers[-1]

    @property
    def delay_tiers(self) -> list[int]:
        return sorted(set(self.retry_backoff_seconds + [self.defer_seconds]))


settings = Settings()
