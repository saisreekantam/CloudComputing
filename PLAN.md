# Implementation Plan — Distributed Multimodal Video Note-Taking Agent

## 1. Architecture (decisions for spec §10)

```
 Chrome extension ──HTTP──▶ Gateway (FastAPI) ──publish──▶ RabbitMQ ──consume──▶ Worker × N
   content / bg / popup        │  (no pipeline work)        quorum queues          (stage consumers)
                               ▼                            + retry tiers               │
                             Redis  ◀──── job state, checkpoints, rate-limit buckets ───┘
                                                                                        │
                                       YouTube (yt-dlp + transcript-api), LLM, Notion ◀─┘
```

| Concern | Decision | Why |
|---|---|---|
| Broker | **RabbitMQ 4 quorum queues**, manual ack, publisher confirms | Durable and replicated; unacked messages are redelivered when a consumer dies; `x-delivery-limit` acts as a poison-message backstop |
| Topology | **One queue per stage** (`transcribe → summarize → frames → publish`). Each message is one stage of one job. Every worker consumes all stages by default; `WORKER_STAGES` lets you run specialised pools | After a crash only the current stage runs again. The ack is sent only after that stage's output is committed and the next stage's message is confirmed |
| Backoff | Per-tier delay queues (`nt.retry.10000ms` …) with TTL, dead-lettered back to the work exchange through a headers exchange | Exponential backoff happens inside the broker, so no worker sleeps on a timer. The tiers can be configured |
| DLQ | `nt.dlx` → `nt.dlq` plus a Redis index; the dashboard/API can **replay** | Jobs can be inspected and re-run |
| State | Redis hash per job, with transitions done by a **Lua compare-and-set state machine** (rejects illegal transitions) and fencing on the stage lease | Stage commits are atomic, and a stale (zombie) worker can't overwrite newer state |
| Idempotency | Stage checkpoints (`job:{id}:done`) + stage lease lock + Notion **reconcile-before-create** (find our marker block → archive → recreate) | A redelivered stage is safe to repeat and never produces a duplicate Notion page |
| Global rate limit | **Redis GCRA token bucket in Lua**, multi-bucket (RPM **and** TPM) using the Redis server clock, plus a shared cooldown when a provider returns 429 | Spaces calls across every worker so the LLM provider never returns 429 during a burst |
| LLM | Any OpenAI-compatible endpoint (Gemini / OpenAI / Groq / Anthropic-compat / Ollama), JSON mode, map-reduce over chunks with checkpoints per chunk | Not tied to one provider |
| Transcript | `youtube-transcript-api` → fallback to yt-dlp caption tracks (json3) | Still works when one source is blocked |
| Frames | yt-dlp resolves the stream URL → `ffmpeg -ss` seeks **directly in the remote stream** (no full download) | Fast; produces real frames only |

## 2. Beyond the spec (kept small and easy to defend)

1. **Frame quality gate**: grab 3 real frames near each flagged timestamp (t, t+2s, t+4s), reject blank or black transition frames, keep the sharpest (Laplacian variance), and drop duplicate slides with a perceptual hash. The page still shows only real frames, with the exact timestamp used.
2. **Vision-grounded captions**: one batched multimodal LLM call looks at the captured frames and rewrites each caption to describe what is *actually visible*, plus why it was flagged. This fixes caption/frame mismatch, which is what an LLM judge checks for Constraint B.
3. **Study-ready Notion page**: TL;DR, table of contents, timestamped section headings that deep-link into the video, frames placed inline in their section, key terms, review-question toggles, and a transparency note listing omitted frames.
4. **Ops dashboard** (`/dashboard`): live workers (heartbeats), queue depths, job timelines, DLQ with one-click replay, and **fault-injection toggles** (forced 429 on LLM/Notion/YouTube, frame failures, a crash in a chosen stage) so each failure mode in §6 can be shown on demand.
5. **Client UX**: per-video button state inside the YouTube player, a toolbar badge counting active jobs, a desktop notification on completion, "waiting for rate limit — retry in 12s" messaging, a 20-minute check in the browser before submit, and Idempotency-Key handling so a double click doesn't create a duplicate job.
6. **Security**: the Notion token is encrypted at rest in Redis (Fernet) and wiped when the job ends; the gateway never persists it.
7. **Evaluation harness**: a burst load script (drain time, p50/p95 latency, provider-429 count = 0), a chaos script (kill the exact worker holding a job and measure recovery time), and an LLM-as-judge script for Constraints A and B.

## 3. Timeline

| Phase | Work | Est. |
|---|---|---|
| P0 | Repo skeleton, docker-compose (RabbitMQ, Redis, gateway, 2 workers), config | 0.5 d |
| P1 | Gateway REST API (`/jobs`, `/jobs/{id}`, `/results`, `/healthz`), Redis store + Lua state machine, broker topology | 1 d |
| P2 | Worker runtime: stage consumers, leases, checkpoints, retry/DLQ routing, heartbeats | 1 d |
| P3 | Pipeline stages: transcript, LLM map-reduce, ffmpeg frames + quality gate, Notion publish (uploads, reconcile) | 1.5 d |
| P4 | Rate limiter (GCRA), 429 handling for all three APIs, fault injection | 0.5 d |
| P5 | Chrome extension: content script (SPA-safe), background (OAuth, polling, notifications), popup, options | 1.5 d |
| P6 | Dashboard, scripts (burst, chaos, judge), unit tests | 1 d |
| P7 | README, end-to-end runs on real videos, demo rehearsal of the 4 failure modes | 1 d |

## 4. Demo script (spec §6 failure modes)

1. **429**: dashboard → "Force 2× LLM 429" → submit → popup shows *Waiting — rate limited, retrying in 10s* → completes.
2. **Worker crash**: `make chaos` (or `docker kill` the worker named in the job's `worker` field during EXTRACTING_FRAMES) → another worker resumes that stage → one Notion page.
3. **Degradation**: "Frame failure rate 50%" → COMPLETED_WITH_WARNINGS; "all frames fail" → text-only page.
4. **DLQ**: submit a private video with demo backoff (`2,4,8,16`) → DEAD_LETTERED in about 30s → replay from the dashboard.
