# Distributed Multimodal Video Note-Taking Agent

Click **Take Notes** on a YouTube video and get study notes in Notion: summary, timestamped sections, real key
frames (chosen by scanning what is actually on screen) with captions, a concept map linked to your other video
notes, key terms and review questions. The work runs on a queue-backed worker pool that survives
worker crashes, rate limits and partial failures.

```
Chrome extension ──HTTP──▶ Gateway :8000 ──▶ RabbitMQ (quorum queues, retry tiers, DLQ) ──▶ Worker × N
 (content/bg/popup)          (no pipeline work)                                              │
                               └──────────▶ Redis (job state machine, checkpoints, rate limiter) ◀─┘
                                                     YouTube (yt-dlp, transcripts) · LLM · Notion API
```

| | |
|---|---|
| **Base URL** | `http://localhost:8000` (dashboard: `http://localhost:8000/dashboard`, RabbitMQ UI: `http://localhost:15672` notetaker/notetaker) |
| **Health check** | `GET /healthz` reports Redis, broker, live workers and config (HTTP 503 if Redis or the broker is down) |
| **Demo run sheet & measured results** | [DEMO.md](DEMO.md) |
| **Default Notion destination** | If `notion_parent_id` is omitted: `NOTION_DEFAULT_PARENT_ID` when set **and shared with the token's integration**. Otherwise (or if that page is not visible to this user's OAuth workspace) the worker uses a page named **“📚 Video Notes”**, created once inside the most recently edited top-level page that the integration can access and then reused. A page or database can also be picked in the extension's Options. |

---

## 1. Quick start (about 5 minutes)

**Prerequisites:** Docker (Compose v2) and Chrome 116+.

```bash
cp .env.example .env          # then fill in LLM_API_KEY, NOTION_CLIENT_ID, NOTION_CLIENT_SECRET
docker compose up -d --build  # rabbitmq + redis + gateway + 2 workers
curl -s localhost:8000/healthz | python3 -m json.tool   # expect "status": "ok", workers.alive = 2
```

**Choose an LLM** (any OpenAI-compatible endpoint; set these in `.env`):

| | Option A: hosted (simplest, recommended for grading) | Option B: self-hosted on a GPU (what we developed with) |
|---|---|---|
| Model | Gemini 3.5 Flash-Lite (free API key at <https://aistudio.google.com/apikey>) | Qwen3-VL-30B-A3B via Ollama (text + vision, open weights) |
| `LLM_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` | `http://host.docker.internal:11435/v1` |
| `LLM_API_KEY` | your key | `ollama` (any non-empty value) |
| `LLM_MODEL` | `gemini-3.5-flash-lite` | `nt-qwen3vl-30b` (see `deploy/ollama/Modelfile`) |
| Also set | `LLM_RPM=10` (free tier). Use a key from a project **without** prepaid billing (otherwise: 402 "credits depleted"); full Flash models were overloaded/unavailable on the free tier when tested | `LLM_RPM=30`, `LLM_CHUNK_CHARS=12000`, `LLM_TIMEOUT_SEC=300` |

Option B on a remote GPU server reached over SSH:

```bash
export GPU_HOST=user@gpu-server            # key-based SSH
scripts/gpu_tunnel.sh serve 2              # optional: private Ollama pinned to GPU 2 (useful on shared, busy servers)
scripts/gpu_tunnel.sh up                   # forwards localhost:11435 and reconnects automatically
scripts/gpu_tunnel.sh check                # "OK … serves nt-qwen3vl-30b", "100% on GPU"
```

If the tunnel or server is down, jobs wait in retries and then dead-letter; after `up`, replay them from the dashboard.

**Notion OAuth app (one time):** at <https://www.notion.so/profile/integrations>, create a **public** integration
(capabilities: read, update and insert content). Add this redirect URI exactly:

```
https://affbfbcfcpadfbncfklibpgnpoahjlff.chromiumapp.org/notion
```

The extension ID is pinned by the `key` in `extension/manifest.json`, so this URI is the same on every machine.
Put the client ID and secret in `.env`, then run `docker compose up -d` again.

> No public integration? Create an *internal* integration, share a page with it, and paste its `ntn_…` token in
> the extension's **Options → “Use an internal integration token instead”**. Everything else works the same way.

**Load the extension (no build step):** go to `chrome://extensions`, turn on Developer mode, click **Load unpacked**
and select the `extension/` folder.

**Use it:** open a YouTube video of 20 minutes or less and click **Take Notes** in the player toolbar. The first click
starts the one-time Notion OAuth flow; the token is stored in `chrome.storage.local` and reused for later jobs. Follow
progress from the toolbar icon (Queued → Transcribing → Summarizing → Extracting Frames → Publishing → Done),
then click **View in Notion**.

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
| `LLM_CONCURRENCY` | `3` | Parallel LLM calls inside one job (transcript chunks; reduce ∥ concept graph). Every call still passes the global limiter |
| `VISION_VERIFY` | `true` | Ground frame captions in the actual image (batched multimodal calls) |
| `VISION_BATCH` | `4` | Frames per vision call — keep ≤4 for 8k-context self-hosted models; a failed batch only loses its own frames |
| `LLM_CHUNK_CHARS` | `24000` | Transcript chunk size for map-reduce, in English-equivalent chars (non-Latin scripts weigh 3x). Use ~12000 for 8k-context self-hosted models; an over-full chunk is split and retried automatically |
| `ASR_FALLBACK` | `true` | If YouTube captions are blocked (IP rate limit) or missing, transcribe the audio locally with faster-whisper |
| `ASR_MODEL` / `ASR_THREADS` | `base` / `8` | Whisper model baked into the worker image (`docker build --build-arg ASR_MODEL=small` for higher accuracy) and CPU threads |
| `TRANSCRIPT_CACHE_DAYS` | `7` | Reuse a video's transcript across jobs; repeat videos make no YouTube caption request |
| `NOTES_LANGUAGE` | `English` | Language of the notes; non-English transcripts are translated (technical terms kept) |
| `MIN_FRAMES` | `3` | Minimum frames per page; topped up from weaker on-screen visuals, then one real frame per section (captioned by the vision model, dropped if no grounded caption) |
| `VISUAL_SCAN` | `true` | Visual-first frame selection: scan the whole video at 240p (~2 s) and pick informative on-screen states (see §5) |
| `VISUAL_CANDIDATES` | `16` | Candidate visuals sent to the vision model for triage (one contact-sheet image) |
| `CONCEPT_GRAPH` | `true` | Build a per-video concept map and link pages that share concepts |
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
| `ADMIN_TOKEN` | `dev-admin` | Token for `/dashboard` and `/admin/*` |
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

All four can be triggered from the **dashboard** (`/dashboard` → *Fault injection*) or with the CLI
(`python3 scripts/nt.py …`, standard library only). Set `RETRY_BACKOFF_SECONDS=2,4,8,16` for quick runs.

| Failure | How to trigger | What you'll see |
|---|---|---|
| **429 from an external API** | Dashboard: *Force next N LLM / Notion / YouTube calls → 429*, then submit a video | The popup shows *“Waiting — the AI model is rate-limiting us, retrying in 10s (retry 1/8)”*. The job stays in its stage and then completes. The message goes to a TTL retry queue, so no worker is held. |
| **Worker crash** | `python3 scripts/nt.py chaos <url>` runs `docker kill` on **the exact worker holding the job** during EXTRACTING_FRAMES. Alternatively, arm *Crash worker mid-stage* | RabbitMQ requeues the unacked message. The surviving worker sees the dead holder's missing heartbeat, takes over the lease (about 5s), and resumes. Frames already captured are reused from per-frame checkpoints. The result is one Notion page. |
| **Graceful degradation** | *Frame failure rate 0.5*, or *All frames fail* | COMPLETED_WITH_WARNINGS with the text and whichever frames succeeded. If every frame fails, the page is text-only. The page lists omitted timestamps and **never substitutes an image**. |
| **Dead-letter queue** | Submit a private or non-existent video, e.g. `https://www.youtube.com/watch?v=xxxxxxxxxx0` | Retries at 2s, 4s, 8s, 16s, then DEAD_LETTERED. The message sits in `nt.dlq` (visible in the RabbitMQ UI), and the dashboard offers **Replay**. |

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
- Frames: chosen **visual-first** (below), then captured at full quality with `ffmpeg -ss` seeking **directly in
  the remote stream URL**, without downloading the video.

**Beyond the spec.**
1. **Visual-first frame selection.** A transcript-only LLM has to *guess* when a slide is visible; on talking-head,
   picture-in-picture and screen-recording videos most guesses land on the presenter. Following lecture-video
   keyframe work (slide-transition detection; keyframe selection with LLM validation), the frames stage:
   (a) decodes a ≤240p rendition at one frame every 2 s (~2 s for a 20-minute video, exact timestamps);
   (b) groups near-identical frames into *visual states* using a perceptual hash **and** an edge-map IoU (white
   slides look alike to pHash alone) and measures how long each stays on screen; (c) finds the dominant
   "presenter" look and scores states by distinctiveness from it, text/edge density and dwell time; (d) sends the
   best 16 to the vision model as **one numbered contact sheet** to rate 0–3; (e) snaps each transcript-flagged
   moment to the best-rated state within −12/+25 s and adds well-rated states the transcript never mentioned.
   On a 17-minute talking-head video this changed the published frames from 4/5 presenter shots to 8/8 slides
   and diagrams. Analysis images are throwaway; every published image is still a full-quality frame captured at
   its timestamp. If the scan fails, selection falls back to the transcript-only behaviour.
2. **Concept map + cross-video links (a small knowledge graph).** After summarising, the LLM proposes concepts
   and typed relations (`prerequisite_of`, `part_of`, `uses`, …). Because transcript-only extraction is noisy,
   the graph is grounded: each concept is anchored to the section that explains it, prerequisite edges that
   contradict the video's teaching order are dropped, and ids/duplicates/self-loops are cleaned. Notion renders
   it as a **Mermaid diagram** (a code block, not an image, so Constraint B is unaffected) with a concept index
   that deep-links to "first explained at mm:ss". Every page registers its concepts per Notion workspace, and new
   pages get **“Related videos in your notes”** links to earlier pages that cover the same ideas.
3. **Transcripts that survive YouTube blocks.** YouTube rate-limits caption requests per public IP (a whole
   campus NAT was blocked during our testing) and some videos have no captions. The chain is now: captions API →
   yt-dlp caption tracks → **local speech-to-text** (faster-whisper `base`, baked into the image) on the audio-only
   stream, which comes from the media CDN and kept working (16.8 min of audio: 3 s download, ~60–95 s transcription
   on CPU). Transcripts are cached per video id for 7 days, so demos and bursts that repeat a video make no caption
   request at all.
4. **Robust long and non-English transcripts.** Chunk size is token-aware (non-Latin scripts weigh 3×), an empty
   or truncated chunk result is split in half and retried instead of being silently dropped, and notes are written
   in `NOTES_LANGUAGE` (e.g. a Hindi-captioned video yields English notes).
5. **Frame quality gate**: grab t, t+2s and t+4s, reject black or blank transition frames, keep the sharpest
   (Laplacian variance), and drop duplicate slides using a perceptual hash.
6. **Vision-grounded captions**: batched multimodal calls rewrite each caption to describe what is *actually
   visible*. Captions written from the transcript alone can mismatch the frame.
7. **Study-ready page**: TL;DR, table of contents, timestamp deep links, frames placed inside their section, key
   terms, review-question toggles, and a disclosure of omitted frames.
8. **Ops console** with live workers, queues, job timelines, DLQ replay and fault injection.
9. **Notion tokens encrypted at rest** (Fernet) and wiped when the job finishes.
10. **Self-verification of Constraints A and B**: `make verify JOB=<id> NOTION_TOKEN=…` re-downloads each published
   image, checks it hash-matches the captured frame, **re-captures the same timestamp from the source video** to prove
   where it came from, and asks a vision LLM to grade caption accuracy.

---

## 6. Repository layout

```
backend/notetaker/
  gateway/app.py        REST API, OAuth exchange, health, metrics, admin (+ static/dashboard.html)
  worker/main.py        consumer runtime: leases, failover, retries, DLQ, heartbeats, graceful drain
  worker/pipeline.py    transcribe → summarize → frames → publish
  store.py              Redis job store + Lua state machine
  broker.py             RabbitMQ topology and publishing
  ratelimit.py          GCRA multi-bucket limiter + shared cooldown
  visual.py             visual-first frame selection (scan, visual states, contact-sheet triage, snapping)
  graph.py              concept graph: grounding, Mermaid rendering, cross-video concept index
  asr.py                speech-to-text fallback (audio-only stream → faster-whisper)
  youtube.py · llm.py · frames.py · notion.py · faults.py · tools/verify.py
backend/tests/          50 tests (pure logic + Redis-backed: state machine, fencing, limiter, selection, graph)  → make test
extension/              MV3, no build step: content.js, background.js, popup.*, options.*
scripts/nt.py           submit / watch / burst / chaos / faults CLI
scripts/gpu_tunnel.sh   self-hosted LLM: private Ollama on a chosen GPU + auto-reconnecting SSH tunnel
deploy/ollama/Modelfile the self-hosted model definition (Qwen3-VL-30B-A3B, 8k context)
```

## 7. Troubleshooting

- **`workers.alive: 0`**: run `docker compose up -d --scale worker=2` (a worker killed in a chaos demo stays down on purpose).
- **“transcribed the audio with whisper-base”** in the job events: YouTube blocked or lacks captions; the
  speech-to-text fallback handled it (adds ~1 min). Other networks or `YTDLP_COOKIES_FILE` avoid the block.
- **NO_TRANSCRIPT**: no captions and no intelligible speech in the audio.
- **YouTube “confirm you're not a bot”**: this is treated as a rate limit and retried. On cloud IPs, set
  `YTDLP_COOKIES_FILE` (mount a cookies.txt into the worker).
- **LLM_UNAVAILABLE / APITimeoutError with a self-hosted model**: run `scripts/gpu_tunnel.sh check`. On a shared GPU
  server the model may have been pushed onto the CPU by other jobs; start a private instance on a free GPU with
  `scripts/gpu_tunnel.sh serve <gpu>` (it reuses the model files via `OLLAMA_MODELS_DIR`).
- **Concept map shows as code**: in Notion, switch the Mermaid block to *Preview*.
- **NOTION_PARENT_NOT_FOUND**: share the destination page with the integration (page ••• → Connections).
