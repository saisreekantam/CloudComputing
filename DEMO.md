# Demo run sheet

A 10–12 minute walkthrough of every graded behaviour, plus the numbers we measured. Setup details are in
[README.md](README.md).

## Pre-flight (5 minutes before)

1. **LLM endpoint.** Pick one:
   - **Hosted (Gemini).** Set `LLM_BASE_URL`, `LLM_API_KEY` (free key: <https://aistudio.google.com/apikey>),
     `LLM_MODEL=gemini-3.5-flash-lite` and `LLM_RPM=10` in `.env`. It needs no GPU or tunnel.
   - **Self-hosted GPU.** Run `export GPU_HOST=user@gpu-server`, then:
     - `scripts/gpu_tunnel.sh gpus` and choose the quietest GPU (latency is mostly LLM time, so a busy shared GPU makes jobs 2–3× slower);
     - `scripts/gpu_tunnel.sh serve <gpu>`;
     - `scripts/gpu_tunnel.sh up`;
     - `scripts/gpu_tunnel.sh check`, which should print `OK … serves nt-qwen3vl-30b` and `100% on GPU`.
2. **Fast backoff.** Put `RETRY_BACKOFF_SECONDS=2,4,8,16` in `.env` so the DLQ demo finishes in about 40 s.
3. **Start the stack.** Run `docker compose up -d`, then `make health`. Expect `"status": "ok"` and `alive: 2`.
4. **Open these tabs:**
   - the dashboard <http://localhost:8000/dashboard> (token `dev-admin`);
   - the RabbitMQ UI <http://localhost:15672> (notetaker / notetaker);
   - the Notion destination page.
5. **Extension.** Reload it at `chrome://extensions` (↻). In Options, confirm Notion shows as connected (OAuth).
6. **Clear leftovers.** In the dashboard, clear any armed faults.
7. **Pre-warm the demo videos (the day before).** Submit each video you plan to show once. Transcripts are cached
   per video for 7 days, so on the day no caption request goes to YouTube. YouTube rate-limits captions per
   public IP, and during our testing the whole campus IP was blocked. If a video was not pre-warmed, the job
   still works through the speech-to-text fallback; it just adds about a minute.

## Demo flow

| # | Spec | Do | Point out |
|---|---|---|---|
| 1 | §8 health | `make health` | Redis, broker and 2 live workers are reported **before** any job is sent |
| 2 | §2, §3 happy path | On YouTube, click **Take Notes** on a video of ≤ 20 min (the held-out one, if graders give one) | The popup steps through Queued → Transcribing → Summarizing → Extracting Frames → Publishing → Done, then shows **View in Notion** |
| 3 | §7B, extras | Open the page | Frames are slides and diagrams placed inside their section. Each caption has a timestamp link and says *why it was flagged*. Point out the **Concept map** (switch to *Preview*), the concept index, *Related videos in your notes*, and the review questions |
| 4 | §6 rate limits | Dashboard → force the next 2 LLM calls (and/or Notion, YouTube) to return 429 → submit a video | The popup shows *waiting / retrying in Ns* rather than an error. The job completes |
| 5 | §6 crash | `make chaos URL=<video>` | The worker holding the job is `docker kill`ed during EXTRACTING_FRAMES; another worker resumes in about 6 s; there is still **one** Notion page. Afterwards run `make scale N=2` |
| 6 | §6 degradation | Dashboard → frame failure rate 0.5 (or all frames fail) → submit | Status is `COMPLETED_WITH_WARNINGS`. The page lists the omitted timestamps and never substitutes an image |
| 7 | §6 DLQ | Submit `https://www.youtube.com/watch?v=xxxxxxxxxx0` | Retries at 2/4/8/16 s, then `DEAD_LETTERED` after about 37 s. The message is in `nt.dlq` in the RabbitMQ UI. Use **Replay** in the dashboard |
| 8 | §9 burst | Click **Take Notes** on 5–6 different videos in a row | The dashboard shows the queues draining across both workers. `curl -s localhost:8000/metrics \| grep provider_429` should show 0 |
| 9 | §8 scaling | `make scale N=4` | New workers join and pick up queued stages with no restart |
| 10 | §7 A/B | `make verify JOB=<id> NOTION_TOKEN=<token>` | Every image hash-matches the captured frame **and** a fresh re-capture from YouTube. The LLM judge scores the captions |

Videos we tested (all ≤ 20 min, with captions): `aircAruvnKk`, `Ilg3gGewQ5U`, `tIeHLnjs5U8` (neural nets);
`fNk_zzaMoSs`, `k7RM-ot2NWY`, `kYB8IZa5AuE`, `Ip3X9LOh2dk`, `PFDu9oVAE-g` (linear algebra); `XPk2CzOyXzQ`
(a talking-head video with picture-in-picture slides); `XV-lIaO00H8` (Hindi captions, English notes). `IHZwWFHWa-w`
is 20:33 long, so use it to show the Constraint A rejection.

## Measured results

### Hosted: Gemini 3.5 Flash-Lite (2026-11-22)

YouTube was blocking caption requests from our IP, so both runs also exercised the speech-to-text fallback.

| Video | Transcript | Total | Frames | Gemini judge (caption accuracy) | Re-capture |
|---|---|---|---|---|---|
| Talking head + slides, 17:18 (`XPk2CzOyXzQ`) | Whisper, 58 s | **157 s** | 10 / 10; triage rated 11 / 16 informative | **5 on all 10**, 10 / 10 authentic | distance 0 on all 10 |
| Animation, 16:46 (`TgKwz5Ikpc8`) | Whisper, 95 s | 191 s | 10 / 10 | 5 on 7, 3 on 3*, 10 / 10 authentic | distance 0 on all 10 |

\* The three captions scored 3 were in a caption batch that hit a Gemini 503 before in-call retries were added,
so they fell back to transcript-based text. Vision-grounded captions scored 5 every time.

### Self-hosted: Qwen3-VL-30B-A3B (2026-11-17)

Measured on the self-hosted Qwen3-VL-30B-A3B. The shared GPU was also running other users' jobs at
80–100 % load, so treat the latencies as an upper bound.

| Criterion | Result |
|---|---|
| End-to-end latency, held-out 17:33 video (`S0_qX4VJhMQ`) | **168 s**: transcribe 3 s, summarise 109 s, frames 39 s, publish 18 s. The same pipeline on an idle GPU earlier took 41–58 s per video, so LLM time dominates |
| Burst of 6 videos | 6/6 `COMPLETED`, 60/60 frames, **0 provider 429s**, 649 s to drain on the contended GPU (136 s on 2026-11-15 with an idle GPU and the earlier, lighter pipeline) |
| Worker killed mid-frames | Resumed on the other worker after **6.3 s**. The job completed with one page |
| Crash right after the Notion page is created | The partial page was archived (reconcile-before-create), leaving exactly **one** live page |
| 429 on YouTube + LLM + Notion | Each was retried with backoff (2 / 3 / 2 injected). The job completed |
| All frames fail | `COMPLETED_WITH_WARNINGS`, a text-only page and 10 `FRAME_EXTRACTION_FAILED` warnings |
| DLQ | Non-existent video → `DEAD_LETTERED` after 5 attempts in 37 s. An LLM outage → DLQ → replay → completed |
| Constraint A | The 20:33 video was rejected with `VIDEO_TOO_LONG` and no retries |
| Constraint B (`tools/verify.py`) | 70 / 70 images re-capture to an identical frame (pHash distance 0). LLM judge: **70 / 70 authentic**, mean caption accuracy **4.72 / 5** over the 60 burst images, and 4–5 on the held-out video |
| Frame relevance (talking-head video) | Visual-first selection changed the published frames from 4 / 5 presenter shots to **8 / 8 slides and diagrams** |
| Tests | 50 passing (`make test`, with Redis running) |

## Likely questions (§10)

- **Why RabbitMQ?** Quorum queues, publisher confirms, broker-side delayed retry via TTL tiers, and immediate
  redelivery of unacked messages when a worker dies.
- **How are no-duplicate pages guaranteed?** Acks happen only after an atomic Redis commit and a confirmed publish of
  the next stage. Stage leases carry fencing tokens. Before creating a page, the publisher archives any page marked
  with this job's id.
- **How is the global LLM rate limit coordinated?** A GCRA limiter in a Redis Lua script checks requests/min and
  tokens/min atomically for all workers. A real 429 triggers a shared cooldown.
- **Why not have the LLM pick frames from the transcript?** It can only guess when a slide is on screen. Scanning
  the video shows what is actually there; the transcript is then used to explain it (README §5).
- **What if YouTube blocks transcripts?** Captions API → caption tracks → local Whisper on the audio stream. The
  audio stream comes from YouTube's media CDN and was not blocked when the caption endpoints were. Transcripts
  are also cached per video.
- **Is the concept map an image?** No. It is a Mermaid code block that Notion draws, so every image on the page
  is still a real frame.

## Known limitations

- Latency depends mostly on the LLM, and a shared GPU can make it 2–3× slower.
- In our self-hosted runs the LLM judge and the caption writer are the same model, so the 4.72 score is
  self-assessed. The Gemini runs give a second judge, and the pHash re-capture check is the independent evidence.
- On Gemini's free tier, only the Flash-Lite models answered reliably when tested: full Flash models returned
  503 "high demand" or had no free quota. Brief 503s are now retried in-call (2 s, 5 s).
- The speech-to-text fallback adds about 1 minute per video on CPU, and auto-transcribed text is less accurate
  than human captions.
- On cloud IPs, YouTube may ask for a bot check. Set `YTDLP_COOKIES_FILE` (README §7).
