# Distributed Multimodal Video Note-Taking Agent

Click **Take Notes** on a YouTube video and get study notes in Notion: summary, timestamped sections, real key
frames with vision-grounded captions, key terms and review questions. The work runs on a queue-backed worker pool that survives
worker crashes, rate limits and partial failures.

```
Chrome extension ──HTTP──▶ Gateway :8000 ──▶ RabbitMQ (quorum queues, retry tiers, DLQ) ──▶ Worker × N
 (content/bg/popup)          (no pipeline work)                                              │
                               └──────────▶ Redis (job state machine, checkpoints, rate limiter) ◀─┘
                                                     YouTube (yt-dlp, transcripts) · LLM · Notion API
```

| | |
|---|---|
| **Base URL** | `http://localhost:8000` (RabbitMQ UI: `http://localhost:15672` notetaker/notetaker) |
| **Health check** | `GET /healthz` reports Redis, broker, live workers and config (HTTP 503 if Redis or the broker is down) |
| **Open modules / roadmap** | [ROADMAP.md](ROADMAP.md) |
| **Default Notion destination** | If `notion_parent_id` is omitted: `NOTION_DEFAULT_PARENT_ID` when set **and shared with the token's integration**. Otherwise (or if that page is not visible to this user's OAuth workspace) the worker uses a page named **“📚 Video Notes”**, created once inside the most recently edited top-level page that the integration can access and then reused. A page or database can also be picked in the extension's Options. |

---

## 1. Quick start (about 5 minutes)

**Prerequisites:** Docker (Compose v2) and Chrome 116+.

```bash
cp .env.example .env          # then fill in LLM_API_KEY, NOTION_CLIENT_ID, NOTION_CLIENT_SECRET
docker compose up -d --build  # rabbitmq + redis + gateway + 2 workers
curl -s localhost:8000/healthz | python3 -m json.tool   # expect "status": "ok", workers.alive = 2
```

**Choose an LLM** (any OpenAI-compatible endpoint; set these in `.env`). The default is Gemini 3.5 Flash-Lite with a
free API key from <https://aistudio.google.com/apikey>: set `LLM_API_KEY` and keep `LLM_RPM=10` for the free tier.
Use a key from a project **without** prepaid billing (otherwise: 402 "credits depleted").

**Notion OAuth app (one time):** at <https://www.notion.so/profile/integrations>, create a **public** integration
(capabilities: read, update and insert content). Add this redirect URI exactly:

```
https://affbfbcfcpadfbncfklibpgnpoahjlff.chromiumapp.org/notion
```

The extension ID is pinned by the `key` in the extension manifest (see ROADMAP.md), so this URI is the same on every machine.
Put the client ID and secret in `.env`, then run `docker compose up -d` again.

**Without the extension**, submit jobs from the CLI or curl:

```bash
python3 scripts/nt.py submit "https://www.youtube.com/watch?v=aircAruvnKk" --token ntn_...   # internal integration token
```

> No public integration? Create an *internal* integration, share a page with it, and pass its `ntn_…` token as
> `notion_token`. Everything else works the same way.

---

## 2. Environment variables

All variables are set in `.env`. Only the first three are required.

| Variable | Default | Meaning |
|---|---|---|
| `LLM_API_KEY` | — **required** | API key for the LLM provider |
| `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` | — **required for OAuth** | Notion public-integration credentials (the secret never leaves the gateway) |
| `LLM_BASE_URL` | Gemini OpenAI-compatible URL | Any OpenAI-compatible endpoint (Gemini, OpenAI, Anthropic `/v1/`, Groq, Ollama) |
| `LLM_MODEL` | `gemini-3.5-flash-lite` | Model name used for synthesis |
| `LLM_VISION_MODEL` | = `LLM_MODEL` | Model used to caption frames (must accept images when `VISION_VERIFY=true`) |
| `LLM_RPM` | `10` | **Configured global LLM rate limit**: requests/min shared by *all* workers |
| `LLM_TPM` | `250000` | Global tokens/min shared by all workers |
| `LLM_5XX_RETRIES` | `2` | In-call retries (2 s, 5 s) for brief provider 5xx such as 503 "high demand"; persistent 5xx → stage retry / DLQ |
| `LLM_CONCURRENCY` | `3` | Parallel LLM calls inside one job (transcript chunks; reduce). Every call still passes the global limiter |
| `VISION_VERIFY` | `true` | Ground frame captions in the actual image (batched multimodal calls) |
| `VISION_BATCH` | `4` | Frames per vision call — keep ≤4 for 8k-context self-hosted models; a failed batch only loses its own frames |
| `LLM_CHUNK_CHARS` | `24000` | Transcript chunk size for map-reduce, in English-equivalent chars (non-Latin scripts weigh 3x). Use ~12000 for 8k-context self-hosted models; an over-full chunk is split and retried automatically |
| `ASR_FALLBACK` | `false` | Speech-to-text fallback when captions are blocked or missing (module in progress, see ROADMAP.md) |
| `TRANSCRIPT_CACHE_DAYS` | `7` | Reuse a video's transcript across jobs; repeat videos make no YouTube caption request |
| `NOTES_LANGUAGE` | `English` | Language of the notes; non-English transcripts are translated (technical terms kept) |
| `MIN_FRAMES` | `3` | Minimum frames per page; topped up from weaker on-screen visuals, then one real frame per section (captioned by the vision model, dropped if no grounded caption) |
| `VISUAL_SCAN` | `false` | Visual-first frame selection (module in progress, see ROADMAP.md); off = frames chosen from the transcript |
| `CONCEPT_GRAPH` | `false` | Per-video concept map + cross-video links (module in progress, see ROADMAP.md) |
| `NOTION_DEFAULT_PARENT_ID` | empty | Fixed default destination (page or database id/URL) |
| `NOTION_RPS` | `2.5` | Notion requests/second per token (Notion allows about 3) |
| `AMQP_URL` | `amqp://notetaker:notetaker@rabbitmq:5672/` | **Broker connection string** (set in compose) |
| `REDIS_URL` | `redis://redis:6379/0` | State store (set in compose; AOF persistence on) |
| `MAX_ATTEMPTS` | `5` | **Retry budget**: runs per stage for ordinary errors before DEAD_LETTERED |
| `RETRY_BACKOFF_SECONDS` | `10,30,90,270` | **Backoff schedule** for attempts 1, 2, 3, 4+ (use `2,4,8,16` for a fast demo) |
| `MAX_RATE_LIMIT_RETRIES` | `8` | Separate retry budget for HTTP 429s |
| `LEASE_SECONDS` | `12` | Stage lease (upper bound on takeover time if a heartbeat is still fresh) |
| `CRASH_REDELIVERY_LIMIT` | `4` | Crashes of one stage before it is quarantined in the DLQ |
| `MAX_VIDEO_SECONDS` | `1200` | Constraint A limit (20 min) |
| `MAX_FRAMES` | `10` | Maximum frames per page |
| `YOUTUBE_RPM` | `30` | Global YouTube metadata/transcript request rate |
| `ADMIN_TOKEN` | `dev-admin` | Token for `/admin/*` |
| `TOKEN_ENC_KEY` | derived | Fernet key used to encrypt Notion tokens at rest in Redis |
| `WORKER_STAGES` | all four | Make a worker serve only some stages (specialised pools) |

---

## 3. REST API

| Method & path | Behaviour |
|---|---|
| `POST /jobs` | Body `{youtube_url, notion_token, notion_parent_id?}`. Returns **202** with a `Location: /jobs/{id}` header and `{job_id, status:"QUEUED", video_id}`. It only validates and enqueues. Errors are 4xx `{"error":{"code","message"}}`. Optional `duration_sec` (sent by the extension) rejects videos over 20 minutes immediately with **422 VIDEO_TOO_LONG**. An `Idempotency-Key` header de-duplicates double clicks. |
| `GET /jobs/{id}` | `{job_id, status, progress, video_title, duration_sec, warnings, error, result}` plus `retry`, `waiting`, `timeline` and `events` for the UI. A pure read. Unknown id → **404**. |
| `GET /jobs/{id}/results` | **409** until the job is terminal. Then `{status, result:{notion_page_url, frames_requested, frames_published}, warnings, error}`. |
| `GET /healthz` | Checks Redis, the broker and live workers. |
| `GET /metrics` | Prometheus text: jobs by outcome, queue depths, LLM calls, provider 429s, recoveries. |
| `POST /oauth/notion/exchange` | Exchanges an OAuth code for a token, keeping the client secret server-side. The token is returned to the extension and not stored. |

```bash
curl -i -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"youtube_url":"https://www.youtube.com/watch?v=aircAruvnKk","notion_token":"ntn_..."}'
```

---

## 4. Demonstrating the required failure modes (§6)

All four can be triggered with the CLI (`python3 scripts/nt.py faults '{"llm_429": 2}'`, standard library only)
or the admin API (`POST /admin/faults` with `X-Admin-Token`). Set `RETRY_BACKOFF_SECONDS=2,4,8,16` for quick runs.

| Failure | How to trigger | What you'll see |
|---|---|---|
| **429 from an external API** | `nt.py faults '{"llm_429": 2}'` (also `notion_429`, `youtube_429`), then submit a video | `GET /jobs/{id}` shows *“Waiting — the AI model is rate-limiting us, retrying in 10s (retry 1/8)”*. The job stays in its stage and then completes. The message goes to a TTL retry queue, so no worker is held. |
| **Worker crash** | `python3 scripts/nt.py chaos <url>` runs `docker kill` on **the exact worker holding the job** during EXTRACTING_FRAMES. Alternatively, arm `crash_stage` | RabbitMQ requeues the unacked message. The surviving worker sees the dead holder's missing heartbeat, takes over the lease (about 5s), and resumes. Frames already captured are reused from per-frame checkpoints. The result is one Notion page. |
| **Graceful degradation** | `frame_fail_rate: 0.5`, or `frame_fail_all: true` | COMPLETED_WITH_WARNINGS with the text and whichever frames succeeded. If every frame fails, the page is text-only. The page lists omitted timestamps and **never substitutes an image**. |
| **Dead-letter queue** | Submit a private or non-existent video, e.g. `https://www.youtube.com/watch?v=xxxxxxxxxx0` | Retries at 2s, 4s, 8s, 16s, then DEAD_LETTERED. The message sits in `nt.dlq` (visible in the RabbitMQ UI), and `POST /admin/dlq/{job_id}/replay` re-queues it. |

Scaling: `docker compose up -d --scale worker=4`. Burst test: `python3 scripts/nt.py burst <url1> <url2> …` prints
drain time, p50 and max latency, and the **provider-429 count, which should be 0**.

---

## 5. Design decisions (spec §10)

**Broker: RabbitMQ 4 quorum queues.** Durable, replicated and acknowledged. When a consumer's connection drops, its
unacked messages are redelivered straight away. `x-delivery-limit` acts as a backstop against poison messages.
Backoff uses **per-tier TTL queues** (`nt.retry.10000ms`, …) that dead-letter back to the work exchange through a
headers exchange, so delays are handled by the broker and no worker sleeps on a timer.

**Topology: one queue per stage; generic workers.** Every message is *(job, stage)*. A worker acks a stage only
after (1) the stage output, the "stage done" checkpoint and the next status are committed atomically in Redis, and
(2) the broker has **confirmed** the next stage's message. A crash at any point causes redelivery, never loss, and
only the interrupted stage runs again. Each worker runs one consumer per stage (prefetch 1), so it can pipeline
different jobs. `WORKER_STAGES` lets you run specialised pools (e.g. frame-only workers) without code changes.

**State: Redis hash per job with a Lua state machine.** Every status change is one compare-and-set script that
enforces the legal transition graph from §4, checks that the caller still holds the stage **lease** (fencing token),
and writes outputs and checkpoints in the same step. A partitioned "zombie" worker therefore cannot overwrite newer
state. Polling reads never mutate.

**Idempotency, so redelivery is *safe to repeat*.**
- Stages that already finished are skipped, and their next message is re-published.
- Long stages checkpoint at a finer grain: each LLM chunk and each captured frame.
- Duplicate deliveries of a running stage are deferred until its lease is released.
- Notion uses **reconcile-before-create**: every page starts with a marker block containing the job id. Before
  creating a page, the worker archives any page an earlier, interrupted attempt of the same job left behind, so each
  job always ends with exactly one live page.

**Global LLM rate limit: Redis GCRA in Lua.** One atomic script checks **both** requests/min and tokens/min using the
Redis server clock: either every bucket admits the call or nothing is charged. RPM is strictly spaced, so no sliding
60-second window can exceed `LLM_RPM` and a burst drains *without* provider 429s. If a provider still returns 429,
a **shared cooldown** pauses that API for every worker. Notion (per token) and YouTube have their own buckets. Waits
longer than 45s are turned into a broker-delayed retry instead of holding a worker.

**Pipeline choices.**
- Transcripts: `youtube-transcript-api`, falling back to yt-dlp caption tracks.
- Metadata and true duration: yt-dlp, which enforces Constraint A authoritatively. The extension also rejects
  over-length videos up front using the `<video>` duration.
- LLM: map-reduce over transcript chunks in order, with strict JSON output and validation. Timestamps outside the
  video are dropped and reported.
- Frames: chosen from the transcript-flagged moments, then captured at full quality with `ffmpeg -ss` seeking **directly in
  the remote stream URL**, without downloading the video.

**Beyond the spec.**
1. **Robust long and non-English transcripts.** Chunk size is token-aware (non-Latin scripts weigh 3×), an empty
   or truncated chunk result is split in half and retried instead of being silently dropped, and notes are written
   in `NOTES_LANGUAGE` (e.g. a Hindi-captioned video yields English notes). Transcripts are cached per video id.
2. **Frame quality gate**: grab t, t+2s and t+4s, reject black or blank transition frames, keep the sharpest
   (Laplacian variance), and drop duplicate slides using a perceptual hash.
3. **Vision-grounded captions**: batched multimodal calls rewrite each caption to describe what is *actually
   visible*. Captions written from the transcript alone can mismatch the frame.
4. **Study-ready page**: TL;DR, table of contents, timestamp deep links, frames placed inside their section, key
   terms, review-question toggles, and a disclosure of omitted frames.
5. **Notion tokens encrypted at rest** (Fernet) and wiped when the job finishes.

Further modules (Chrome extension, ops dashboard, visual-first frame selection, concept graph, speech-to-text
fallback, Constraint A/B verifier) are specified in [ROADMAP.md](ROADMAP.md).

---

## 6. Repository layout

```
backend/notetaker/
  gateway/app.py        REST API, OAuth exchange, health, metrics, admin API
  worker/main.py        consumer runtime: leases, failover, retries, DLQ, heartbeats, graceful drain
  worker/pipeline.py    transcribe → summarize → frames → publish
  store.py              Redis job store + Lua state machine
  broker.py             RabbitMQ topology and publishing
  ratelimit.py          GCRA multi-bucket limiter + shared cooldown
  youtube.py · llm.py · frames.py · notion.py · faults.py
  visual.py · graph.py · asr.py   interfaces for the modules in ROADMAP.md (wired in, disabled by default)
backend/tests/          pure logic + Redis-backed tests (state machine, fencing, limiter, chunking, blocks)  → make test
scripts/nt.py           submit / watch / burst / chaos / faults CLI
```

## 7. Troubleshooting

- **`workers.alive: 0`**: run `docker compose up -d --scale worker=2` (a worker killed in a chaos demo stays down on purpose).
- **IpBlocked / 429 on transcripts**: YouTube rate-limits caption requests per public IP. Try another network or set `YTDLP_COOKIES_FILE`.
- **NO_TRANSCRIPT**: the video has no captions.
- **YouTube “confirm you're not a bot”**: this is treated as a rate limit and retried. On cloud IPs, set
  `YTDLP_COOKIES_FILE` (mount a cookies.txt into the worker).
- **NOTION_PARENT_NOT_FOUND**: share the destination page with the integration (page ••• → Connections).
