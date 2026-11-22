"""Error taxonomy that drives retry routing in the worker.

  RateLimited  -> re-queued with exponential backoff (separate, larger budget)
  Retryable    -> re-queued with backoff until MAX_ATTEMPTS, then dead-lettered
  Permanent    -> job FAILED immediately (no point retrying)
"""
from __future__ import annotations


class PipelineError(Exception):
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, *, code: str | None = None, **details):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.details:
            d["details"] = {k: v for k, v in self.details.items() if v is not None}
        return d


class Retryable(PipelineError):
    code = "TRANSIENT_ERROR"


class RateLimited(Retryable):
    code = "RATE_LIMITED"

    def __init__(self, service: str, retry_after: float | None = None, *, injected: bool = False,
                 self_imposed: bool = False, message: str | None = None):
        msg = message or f"{service} rate limit hit"
        super().__init__(msg, code="RATE_LIMITED", service=service, retry_after=retry_after)
        self.service = service
        self.retry_after = retry_after
        self.injected = injected
        self.self_imposed = self_imposed


class Permanent(PipelineError):
    code = "PERMANENT_ERROR"


class LeaseLost(Exception):
    """Another worker took over this stage (our lease expired). Abandon quietly."""


class StaleMessage(Exception):
    """Job already moved past this stage / is terminal. Ack and drop."""
