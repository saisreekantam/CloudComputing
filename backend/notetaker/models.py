"""Job lifecycle: statuses, stages, and the legal state-transition graph (spec §4)."""
from __future__ import annotations

from enum import Enum


class Status(str, Enum):
    QUEUED = "QUEUED"
    TRANSCRIBING = "TRANSCRIBING"
    SUMMARIZING = "SUMMARIZING"
    EXTRACTING_FRAMES = "EXTRACTING_FRAMES"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"


TERMINAL = {Status.COMPLETED, Status.COMPLETED_WITH_WARNINGS, Status.FAILED, Status.DEAD_LETTERED}
NON_TERMINAL = [s for s in Status if s not in TERMINAL]

# Pipeline stages, in order. Each stage is a separate message on its own queue.
STAGES = ["transcribe", "summarize", "frames", "publish"]

# Status a job shows while a stage is running.
STAGE_STATUS = {
    "transcribe": Status.TRANSCRIBING,
    "summarize": Status.SUMMARIZING,
    "frames": Status.EXTRACTING_FRAMES,
    "publish": Status.PUBLISHING,
}

NEXT_STAGE = {"transcribe": "summarize", "summarize": "frames", "frames": "publish", "publish": None}

# Progress (%) at the *start* of each status; stages interpolate inside their band.
PROGRESS_BASE = {
    Status.QUEUED: 0,
    Status.TRANSCRIBING: 5,
    Status.SUMMARIZING: 25,
    Status.EXTRACTING_FRAMES: 60,
    Status.PUBLISHING: 85,
    Status.COMPLETED: 100,
    Status.COMPLETED_WITH_WARNINGS: 100,
}
PROGRESS_BAND_END = {
    Status.TRANSCRIBING: 24,
    Status.SUMMARIZING: 58,
    Status.EXTRACTING_FRAMES: 84,
    Status.PUBLISHING: 99,
}

_FAIL = {Status.FAILED, Status.DEAD_LETTERED}

# Legal transitions (from -> set of to). Same-state "transitions" are always allowed (idempotent).
ALLOWED: dict[Status, set[Status]] = {
    Status.QUEUED: {Status.TRANSCRIBING} | _FAIL,
    Status.TRANSCRIBING: {Status.SUMMARIZING} | _FAIL,
    Status.SUMMARIZING: {Status.EXTRACTING_FRAMES} | _FAIL,
    Status.EXTRACTING_FRAMES: {Status.PUBLISHING} | _FAIL,
    Status.PUBLISHING: {Status.COMPLETED, Status.COMPLETED_WITH_WARNINGS} | _FAIL,
    Status.COMPLETED: set(),
    Status.COMPLETED_WITH_WARNINGS: set(),
    Status.FAILED: {Status.QUEUED},          # manual replay only
    Status.DEAD_LETTERED: {Status.QUEUED},   # manual replay from the DLQ
}


def predecessors(target: Status) -> set[Status]:
    return {src for src, dsts in ALLOWED.items() if target in dsts}


def can_transition(src: Status | None, dst: Status) -> bool:
    if src is None:
        return dst == Status.QUEUED
    return src == dst or dst in ALLOWED[src]


# Human-facing warning codes (popup/README map these to sentences).
class Warn:
    FRAME_EXTRACTION_FAILED = "FRAME_EXTRACTION_FAILED"
    FRAME_UPLOAD_FAILED = "FRAME_UPLOAD_FAILED"
    FRAME_DUPLICATE_DROPPED = "FRAME_DUPLICATE_DROPPED"
    FRAME_TIMESTAMP_INVALID = "FRAME_TIMESTAMP_INVALID"
    STREAM_UNAVAILABLE = "STREAM_UNAVAILABLE"
    VISION_CHECK_SKIPPED = "VISION_CHECK_SKIPPED"
    TRANSCRIPT_FALLBACK = "TRANSCRIPT_FALLBACK"
    TRANSCRIPT_TRANSLATED = "TRANSCRIPT_NOT_ENGLISH"
    METADATA_DEGRADED = "METADATA_DEGRADED"
