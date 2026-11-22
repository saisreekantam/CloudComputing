"""API Gateway — accepts jobs, exposes status/results. Never runs pipeline work.

Spec endpoints:   POST /jobs · GET /jobs/{id} · GET /jobs/{id}/results · GET /healthz
Extras:           GET /jobs (recent) · POST /oauth/notion/exchange · GET /oauth/notion/config
                  GET /notion/pages (destination picker) · GET /metrics (Prometheus)
                  GET /dashboard · /admin/* (faults, DLQ inspect + replay; X-Admin-Token)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..broker import Broker
from ..config import settings
from ..errors import PipelineError
from ..faults import Faults
from ..models import STAGES, TERMINAL, Status
from ..notion import NotionClient, exchange_oauth_code, normalize_notion_id
from ..ratelimit import RateLimiter
from ..store import JobStore
from ..youtube import parse_video_id

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s [gateway] %(message)s")
log = logging.getLogger("gateway")
STATIC = Path(__file__).parent / "static"


class Deps:
    store: JobStore
    broker: Broker
    faults: Faults
    limiter: RateLimiter


deps = Deps()


@asynccontextmanager
async def lifespan(app: FastAPI):
    deps.store = JobStore()
    deps.broker = Broker()
    deps.faults = Faults(deps.store.r)
    deps.limiter = RateLimiter(deps.store.r)
    for attempt in range(60):
        try:
            await deps.store.ping()
            await deps.broker.connect(client_name="gateway")
            break
        except Exception as e:  # noqa: BLE001
            log.warning("waiting for broker/redis (%s): %s", attempt, e)
            await asyncio.sleep(2)
    yield
    await deps.broker.close()
    await deps.store.close()


app = FastAPI(title="Video Note-Taking Agent — Gateway", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origin_regex=r"^(chrome-extension://.*|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?)$",
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["Location"])


def err(http_status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=http_status, content={"error": {"code": code, "message": message, **extra}})


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    return err(422, "INVALID_REQUEST", f"{loc or 'body'}: {first.get('msg', 'invalid request')}")


@app.exception_handler(json.JSONDecodeError)
async def json_handler(_: Request, __):
    return err(400, "INVALID_JSON", "Request body must be valid JSON")


# ---------------------------------------------------------------------------- jobs
class JobRequest(BaseModel):
    youtube_url: str = Field(..., max_length=500)
    notion_token: str | None = Field(None, max_length=500)
    notion_parent_id: str | None = Field(None, max_length=200)
    # Optional client hints (the extension reads these from the <video> element) — enable
    # deterministic rejection of over-long videos at submission time (Constraint A).
    duration_sec: float | None = Field(None, ge=0)
    video_title: str | None = Field(None, max_length=500)


@app.post("/jobs", status_code=202)
async def submit_job(req: JobRequest, idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    video_id = parse_video_id(req.youtube_url)
    if not video_id:
        return err(400, "INVALID_YOUTUBE_URL", "youtube_url must be a YouTube video URL (watch, youtu.be, shorts or embed)")
    token = (req.notion_token or "").strip() or settings.notion_fallback_token
    if not token:
        return err(400, "MISSING_NOTION_TOKEN", "notion_token is required — connect Notion in the extension first")
    if not re.match(r"^[A-Za-z0-9_\-.]{20,}$", token):
        return err(400, "INVALID_NOTION_TOKEN", "notion_token is malformed")
    if req.duration_sec and req.duration_sec > settings.max_video_seconds + 1:
        return err(422, "VIDEO_TOO_LONG",
                   f"Video is {int(req.duration_sec) // 60} min long; videos up to "
                   f"{settings.max_video_seconds // 60} min are supported", duration_sec=int(req.duration_sec))
    parent = None
    if (req.notion_parent_id or "").strip():
        parent = normalize_notion_id(req.notion_parent_id)
        if not parent:
            return err(400, "INVALID_NOTION_PARENT", "notion_parent_id must be a Notion page/database id or URL")

    try:
        job_id, created = await deps.store.create_job(
            video_id=video_id, youtube_url=req.youtube_url, notion_token=token, notion_parent_id=parent,
            idempotency_key=f"{idempotency_key}:{video_id}" if idempotency_key else None,
            client_hint={"title": req.video_title, "duration_sec": req.duration_sec})
    except Exception as e:  # noqa: BLE001
        log.exception("state store unavailable")
        return err(503, "STATE_STORE_UNAVAILABLE", f"Job store unavailable: {type(e).__name__}")

    if created:
        try:
            await deps.broker.publish_stage(job_id, "transcribe")
        except Exception as e:  # noqa: BLE001
            await deps.store.delete_job(job_id)
            log.exception("broker publish failed")
            return err(503, "BROKER_UNAVAILABLE", f"Could not enqueue job: {type(e).__name__}")
    status = await deps.store.get_status(job_id)
    return JSONResponse(status_code=202, headers={"Location": f"/jobs/{job_id}"},
                        content={"job_id": job_id, "status": (status or Status.QUEUED).value, "video_id": video_id,
                                 "deduplicated": not created})


@app.get("/jobs")
async def list_jobs(limit: int = 30):
    ids = await deps.store.recent(min(max(limit, 1), 200))
    views = await asyncio.gather(*(deps.store.view(j) for j in ids))
    return {"jobs": [v for v in views if v]}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    view = await deps.store.view(job_id)
    if not view:
        return err(404, "JOB_NOT_FOUND", f"No job with id {job_id}")
    return view


@app.get("/jobs/{job_id}/results")
async def get_results(job_id: str):
    view = await deps.store.view(job_id)
    if not view:
        return err(404, "JOB_NOT_FOUND", f"No job with id {job_id}")
    if Status(view["status"]) not in TERMINAL:
        return err(409, "JOB_NOT_FINISHED", f"Job is still {view['status']}; poll GET /jobs/{job_id}",
                   status=view["status"], progress=view["progress"])
    return {"status": view["status"], "result": view["result"], "warnings": view["warnings"], "error": view["error"]}


# ---------------------------------------------------------------------------- health
@app.get("/healthz")
async def healthz():
    checks: dict[str, dict] = {}
    t = time.perf_counter()
    try:
        await asyncio.wait_for(deps.store.ping(), 2)
        checks["redis"] = {"ok": True, "ms": round((time.perf_counter() - t) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        checks["redis"] = {"ok": False, "error": type(e).__name__}
    t = time.perf_counter()
    try:
        depths = await asyncio.wait_for(deps.broker.queue_depths(), 3)
        checks["broker"] = {"ok": deps.broker.is_open and all(v >= 0 for v in depths.values()),
                            "ms": round((time.perf_counter() - t) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        checks["broker"] = {"ok": False, "error": type(e).__name__}
    try:
        workers = await deps.store.workers()
        checks["workers"] = {"ok": len(workers) > 0, "alive": len(workers),
                             "ids": [w["id"].split(":")[0] for w in workers]}
    except Exception:  # noqa: BLE001
        checks["workers"] = {"ok": False, "alive": 0}
    checks["config"] = {"ok": bool(settings.llm_api_key) or "localhost" in settings.llm_base_url,
                        "llm_model": settings.llm_model, "llm_rpm": settings.llm_rpm,
                        "notion_oauth": bool(settings.notion_client_id and settings.notion_client_secret)}
    core_ok = checks["redis"]["ok"] and checks["broker"]["ok"]
    return JSONResponse(status_code=200 if core_ok else 503,
                        content={"status": "ok" if core_ok and checks["workers"]["ok"] else ("degraded" if core_ok else "down"),
                                 "checks": checks})


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    lines = []
    m = await deps.store.metrics()
    for k, v in sorted(m.items()):
        name, _, label = k.partition(":")
        lbl = f'{{key="{label}"}}' if label else ""
        lines.append(f"notetaker_{name}{lbl} {v}")
    try:
        for q, n in (await deps.broker.queue_depths()).items():
            lines.append(f'notetaker_queue_ready{{queue="{q}"}} {n}')
    except Exception:  # noqa: BLE001
        pass
    lines.append(f"notetaker_workers_alive {len(await deps.store.workers())}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------- notion oauth
class OAuthExchange(BaseModel):
    code: str
    redirect_uri: str


@app.get("/oauth/notion/config")
async def oauth_config():
    return {"configured": bool(settings.notion_client_id and settings.notion_client_secret),
            "client_id": settings.notion_client_id or None,
            "authorize_url": "https://api.notion.com/v1/oauth/authorize"}


@app.post("/oauth/notion/exchange")
async def oauth_exchange(body: OAuthExchange):
    """Swap an OAuth code for a token. The client secret stays server-side; the token is returned
    to the extension and NOT stored by the gateway."""
    if not re.match(r"^https://[a-p]{32}\.chromiumapp\.org/", body.redirect_uri) and not body.redirect_uri.startswith("http://localhost"):
        return err(400, "INVALID_REDIRECT_URI", "redirect_uri must be the extension's chromiumapp.org URL")
    try:
        tok = await exchange_oauth_code(body.code, body.redirect_uri)
    except PipelineError as e:
        return err(400, e.code, e.message)
    return {k: tok.get(k) for k in ("access_token", "refresh_token", "bot_id", "workspace_id", "workspace_name",
                                    "workspace_icon", "duplicated_template_id")}


@app.get("/notion/pages")
async def notion_pages(authorization: str = Header(""), q: str = ""):
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return err(401, "MISSING_NOTION_TOKEN", "Authorization: Bearer <notion token> required")
    client = NotionClient(token, deps.limiter)
    try:
        pages = await client.search_pages(q, limit=50)
    except PipelineError as e:
        return err(400 if e.code != "NOTION_UNAUTHORIZED" else 401, e.code, e.message)
    finally:
        await client.close()
    return {"pages": [{"id": p["id"], "title": NotionClient.page_title(p) or "Untitled", "url": p.get("url"),
                       "icon": (p.get("icon") or {}).get("emoji")} for p in pages if not p.get("archived")]}


# ---------------------------------------------------------------------------- admin / dashboard
def _admin(token: str | None) -> JSONResponse | None:
    if token != settings.admin_token:
        return err(401, "ADMIN_TOKEN_REQUIRED", "X-Admin-Token header missing or wrong")
    return None


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/admin/overview")
async def admin_overview(x_admin_token: str | None = Header(None)):
    if (e := _admin(x_admin_token)):
        return e
    workers, metrics_, faults, dlq = await asyncio.gather(
        deps.store.workers(), deps.store.metrics(), deps.faults.get_all(), deps.store.dlq_list())
    queues = await _queue_stats()
    return {"workers": workers, "metrics": metrics_, "faults": faults, "queues": queues, "dlq": dlq,
            "config": {"llm_model": settings.llm_model, "llm_rpm": settings.llm_rpm, "llm_tpm": settings.llm_tpm,
                       "max_attempts": settings.max_attempts, "backoff": settings.retry_backoff_seconds,
                       "max_rate_limit_retries": settings.max_rate_limit_retries,
                       "lease_seconds": settings.lease_seconds, "max_video_seconds": settings.max_video_seconds,
                       "vision_verify": settings.vision_verify, "stages": STAGES}}


async def _queue_stats() -> list[dict]:
    """Ready + unacked per queue from the management API, falling back to passive declares."""
    try:
        async with httpx.AsyncClient(timeout=3) as http:
            r = await http.get(f"{settings.rabbit_mgmt_url}/api/queues/%2F")
            r.raise_for_status()
            return [{"name": q["name"], "ready": q.get("messages_ready", 0), "unacked": q.get("messages_unacknowledged", 0),
                     "consumers": q.get("consumers", 0)} for q in r.json() if q["name"].startswith("nt.")]
    except Exception:  # noqa: BLE001
        depths = await deps.broker.queue_depths()
        return [{"name": k, "ready": v, "unacked": None, "consumers": None} for k, v in depths.items()]


class FaultUpdate(BaseModel):
    llm_429: int | None = None
    notion_429: int | None = None
    youtube_429: int | None = None
    frame_fail_rate: float | None = Field(None, ge=0, le=1)
    frame_fail_all: bool | None = None
    crash_stage: str | None = None


@app.post("/admin/faults")
async def set_faults(body: FaultUpdate, x_admin_token: str | None = Header(None)):
    if (e := _admin(x_admin_token)):
        return e
    if body.crash_stage and body.crash_stage not in STAGES:
        return err(400, "INVALID_STAGE", f"crash_stage must be one of {STAGES}")
    return {"faults": await deps.faults.set(body.model_dump(exclude_unset=True))}


@app.delete("/admin/faults")
async def clear_faults(x_admin_token: str | None = Header(None)):
    if (e := _admin(x_admin_token)):
        return e
    await deps.faults.clear()
    return {"faults": {}}


@app.post("/admin/dlq/{job_id}/replay")
async def replay(job_id: str, x_admin_token: str | None = Header(None)):
    if (e := _admin(x_admin_token)):
        return e
    raw = await deps.store.get_raw(job_id)
    if not raw:
        return err(404, "JOB_NOT_FOUND", f"No job with id {job_id}")
    if raw["status"] not in (Status.DEAD_LETTERED.value, Status.FAILED.value):
        return err(409, "NOT_REPLAYABLE", f"Job is {raw['status']}; only DEAD_LETTERED/FAILED jobs can be replayed")
    if not raw.get("token_enc"):
        return err(409, "TOKEN_GONE", "The job's Notion token was wiped; resubmit from the extension instead")
    # Replay walks the state machine from the start. Expensive LLM chunk checkpoints are kept, so
    # finished synthesis is reused; frame checkpoints are cleared so failed captures get retried.
    res = await deps.store.commit(job_id, target=Status.QUEUED, worker="admin-replay",
                                  fields={"error": None, "retry": None, "attempt": 0, "stage": "transcribe",
                                          "progress": 0})
    if not res.ok:
        return err(409, "NOT_REPLAYABLE", res.code)
    r = deps.store.r
    await r.delete(deps.store.k(job_id, "done"), deps.store.k(job_id, "ckpt:vision"), deps.store.k(job_id, "ckpt:frames"))
    async for key in r.scan_iter(deps.store.k(job_id, "ckpt:frame_meta:*")):
        await r.delete(key)
    fields = [f"{p}:{s}" for s in STAGES for p in ("crashes", "running", "warnings")]
    await r.hdel(deps.store.k(job_id), *fields, "dlq_reason")
    removed = await deps.broker.drain_dlq_for(job_id)
    await deps.store.dlq_remove(job_id)
    await deps.store.add_event(job_id, "replayed", "Replayed from the DLQ by an operator")
    await deps.broker.publish_stage(job_id, "transcribe")
    return {"job_id": job_id, "status": "QUEUED", "dlq_messages_removed": removed}
