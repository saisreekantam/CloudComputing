# Roadmap: open modules

The backend core is in place:
- the gateway REST API and the Notion OAuth exchange;
- RabbitMQ topology, retries and the DLQ;
- the Redis state machine and the global rate limiter;
- the four pipeline stages, with Notion publishing and fault injection.

This document specifies the modules that are still open. Each one has a fixed interface, and most are already
wired into the pipeline behind a feature flag. Implementing a module therefore means filling in its stub and
switching the flag on; no changes to the core are needed.

| # | Module | Where | Flag | Spec link |
|---|---|---|---|---|
| 1 | Chrome extension | `extension/` (new) | — | §2 client, one-time OAuth |
| 2 | Ops dashboard | `backend/notetaker/gateway/static/dashboard.html` (new) | — | §6 demo, §8 observability |
| 3 | Visual-first frame selection | `backend/notetaker/visual.py` | `VISUAL_SCAN` | §7 Constraint B (frame relevance) |
| 4 | Concept graph + cross-video links | `backend/notetaker/graph.py` | `CONCEPT_GRAPH` | beyond spec |
| 5 | Speech-to-text transcript fallback | `backend/notetaker/asr.py` | `ASR_FALLBACK` | §6 graceful degradation |
| 6 | Constraint A/B verifier | `backend/notetaker/tools/verify.py` (new) | — | §7 A/B evidence |

General rules:
- Rebuild images after code changes with `docker compose build worker gateway`. There is no bind mount.
- `make test` must stay green. Add tests under `backend/tests/` for every module: pure logic in `test_<module>.py`,
  and anything that touches Redis in `test_redis.py`.
- Keep the existing guarantees:
  - every published image is a real frame captured from the video;
  - a module failure must degrade the job, never fail it;
  - every LLM call goes through `LLM` in `llm.py`, so it passes the global rate limiter.

---

## 1. Chrome extension (Manifest V3, no build step)

**Goal.** Add a **Take Notes** button to the YouTube player. It runs Notion OAuth once, submits the job, shows
live progress, and finishes with a link to the Notion page.

**Files:**
- `manifest.json`
- `background.js`: service worker, `type: module`
- `content.js` and `content.css`: the button in the player toolbar
- `popup.html`, `popup.js` and `popup.css`: the status UI
- `options.html` and `options.js`: settings
- `shared.js`
- `icons/`

**Manifest essentials:**
- `"minimum_chrome_version": "116"`
- `"permissions": ["storage", "identity", "alarms", "notifications"]`
- `"host_permissions": ["http://localhost:8000/*", "http://127.0.0.1:8000/*", "https://www.youtube.com/*"]`
- Content script on `https://www.youtube.com/*`.
- Pin the extension ID with this public key, so the OAuth redirect URI is
  `https://affbfbcfcpadfbncfklibpgnpoahjlff.chromiumapp.org/notion` on every machine:

```
"key": "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAxo49MBw3qyf229zz4luRlJVyLYmBlocr4K2+VfKgcdV71xTr6xs3pVBEEt7GRdmMhN6wv4hAM2+MJ6LCKmsqnugTZY2S5GG0xPXPjymd5tL2twd55C3KNXuJD1qp6+WFSfCtjfxrJ+GA0uTaeFdg2jHh6ULA88tyC/BtbuzsqfEPqE9ug2vwTPmk5eFzzOI33TRD7GWhec1EKsPmPmfk0Be8mZb63m7tPVrGGixQtz9GZMpmHbBs7UqWi5FGSHuxXaJkg5D7YhMhpxqAuFsTvWz1bLoBQujm6qsZLVm1KVkkbKVNs0LlNXwBnc6g79O2A7JOzKEswJmSYqzv8l65PQIDAQAB"
```

**Gateway contract.** The endpoints already exist. CORS allows `chrome-extension://*`.

| Call | Use |
|---|---|
| `GET /oauth/notion/config` | `{configured, client_id, authorize_url}`. If `configured` is false, open the Options page and ask for an internal `ntn_…` token instead |
| `POST /oauth/notion/exchange` `{code, redirect_uri}` | Returns `{access_token, workspace_name, …}`. The client secret never reaches the extension |
| `GET /notion/pages?q=` (header `Authorization: Bearer <token>`) | Pages and databases the token can see, for the destination picker in Options |
| `POST /jobs` `{youtube_url, notion_token, notion_parent_id?, duration_sec, video_title}` | Returns 202 with `Location`. Send an `Idempotency-Key` header (for example `videoId + click time`, rounded to a few seconds) so a double click does not create two jobs |
| `GET /jobs/{id}` | Poll about every 1.5 s. Shows `status`, `progress`, `retry`/`waiting` ("retrying in Ns"), `events` and `warnings` |
| `GET /jobs/{id}/results` | Returns 409 until the job is terminal, then `result.notion_page_url` |

**Behaviour:**
- **OAuth flow:**
  1. Call `chrome.identity.launchWebAuthFlow` with
     `authorize_url?client_id=…&response_type=code&owner=user&redirect_uri=<chromiumapp URL>`.
  2. Exchange the returned code through the gateway.
  3. Store the token in `chrome.storage.local` and reuse it for every later job.

  The flow runs only once per install.
- **Button:** read `document.querySelector("video").duration`. A video longer than 20 minutes is rejected in the
  UI immediately; the gateway also returns 422 `VIDEO_TOO_LONG`. Disable the button while that video has a job
  in flight.
- **Popup:** show the nine statuses as steps:

  > QUEUED → TRANSCRIBING → SUMMARIZING → EXTRACTING_FRAMES → PUBLISHING → COMPLETED / COMPLETED_WITH_WARNINGS / FAILED / DEAD_LETTERED

  - While the job is running, show the progress bar.
  - When the gateway reports `waiting`, show it as a rate-limit notice, not an error.
  - When the job finishes, show warnings and a **View in Notion** button.
- **Service worker restarts:** MV3 workers sleep after about 30 s. Keep active job ids in `chrome.storage` and
  resume polling with `chrome.alarms`. Send a notification when a job finishes.
- **Options page:**
  - gateway URL (default `http://localhost:8000`);
  - connect or disconnect Notion;
  - the internal-token fallback;
  - the destination page picker.

**Done when:**
- a fresh Chrome profile can load the extension unpacked;
- the user connects Notion once;
- clicking **Take Notes** on a video of 20 minutes or less ends on a Notion page;
- the popup survives closing and reopening during a job.

---

## 2. Ops dashboard (`/dashboard`)

**Goal.** A single static HTML page with no build step, served by the existing `GET /dashboard` route, for
running the failure-mode demos live.

**Data:**
- `GET /admin/overview` (header `X-Admin-Token`, default `dev-admin`) returns
  `{workers, metrics, faults, queues, dlq, config}`. Poll it every 2 s.
- `GET /jobs` lists recent jobs, and `GET /jobs/{id}` returns each job's `timeline` and `events`.

**Panels:**
1. **Workers.** Id, the stage each one is running, and the age of its heartbeat. Mark a worker dead when its
   heartbeat is older than 3 × `WORKER_HEARTBEAT_SEC`.
2. **Queues.** Ready, unacked and consumer counts per `nt.*` queue, including the retry tiers and `nt.dlq`.
3. **Fault injection.** Use `POST /admin/faults` to force the next N LLM, Notion or YouTube calls to return 429,
   set `frame_fail_rate` from 0 to 1, set `frame_fail_all`, and set `crash_stage`. `DELETE /admin/faults`
   clears them.
4. **DLQ.** Each entry with its last error, and a **Replay** button that calls `POST /admin/dlq/{job_id}/replay`.
5. **Jobs and metrics.**
   - Recent jobs with status and latency, plus a per-stage timeline for the selected job.
   - Counters: completed, failed, `provider_429`, recoveries and LLM calls.

**Done when:** each of the four §6 scenarios can be triggered and watched from this page alone, and the token is
stored in `localStorage`.

---

## 3. Visual-first frame selection (`visual.py`, `VISUAL_SCAN=true`)

**Problem.** The LLM reads only the transcript, so it has to *guess* when a slide is on screen. On talking-head,
picture-in-picture and screen-recording videos, most guesses land on the presenter's face.

**Idea.** Scan the video itself for stable, distinctive visual states, have the vision model rate them, and then
match the transcript moments to them. This approach follows lecture-video work on slide-transition detection and
keyframe selection with LLM validation.

**Already provided:**
- `pick_scan_stream(formats)` chooses a 240p video-only stream; `youtube.fetch_metadata` stores it as
  `scan_stream`.
- The `Thumb` and `Shot` dataclasses.
- The transcript-only path of `select_frames(…)`, used when `cands is None`.
- `LLM.rate_contact_sheet(sheet_b64, n, title)` returns `[{index, score 0-3, kind, shows}]`.
- The whole orchestration in `worker/pipeline.py::_visual_candidates` and `_plan_frames`, including checkpoints
  and degradation if the scan fails.

**To implement:**
- **`scan(stream, timeout, width=320, every=2.0) -> list[Thumb]`.**
  - Run one ffmpeg pass:
    `-i <url> -an -vf fps=1/2,scale=320:-2,showinfo -f image2pipe -vcodec mjpeg pipe:1`
    (pass `http_headers` with `-headers`).
  - Split stdout on the JPEG start-of-image marker `FF D8 FF`.
  - Parse `pts_time:` from stderr to get exact timestamps.
  - Per thumbnail, compute the pHash, the edge density (share of pixels with a strong gradient), the grey
    standard deviation, and a coarse 96×54 binary edge map.
  - Raise `RateLimited("youtube", 60)` if stderr shows a 429, and `StreamExpired` on 403 or 410.
  - Do **not** use `-skip_frame nokey`. It is faster, but ffmpeg then stamps each keyframe with the *next*
    keyframe's pts, so timestamps come out about 7 s late.
  - Expect about 2 s for a 20-minute video.
- **`edge_iou(a, b)` and `same_state(a, b)`.**
  - pHash alone thinks two different white slides are the same, so require pHash distance ≤ 10 **and** edge-map
    IoU ≥ 0.55.
  - Dilate the edge maps by 1 px before intersecting them.
- **`build_shots(thumbs, duration)`.**
  - Group consecutive thumbnails that are in the same state into one `Shot`.
  - Skip near-blank frames (standard deviation < 12).
  - Dwell time = start of the next shot − start of this one.
  - Representative frame: the thumbnail about 70 % of the way through the shot, because slides build up.
- **`score_shots(shots, thumbs)`.**
  - Cluster shots by pHash (distance ≤ 14).
  - The cluster with the largest total dwell is the *baseline* (presenter), but only if it holds ≥ 25 % of the
    video's dwell time. Otherwise return None (slides-only or animation videos).
  - Score each shot as `0.45·distinct + 0.35·edges/max_edges + 0.20·log-dwell`. Multiply by 0.35 for baseline
    shots and by 0.6 for one-frame flashes.
- **`top_candidates(shots, thumbs, k)`.** Take the best k shots that are ≥ 10 s apart and not duplicates of each
  other, then sort them by time.
- **`contact_sheet(images)`.** A numbered 4-column JPEG grid, so that **one** vision call rates all 16
  candidates.
- **The visual branch of `select_frames`.**
  1. Snap each transcript moment, most important first, to the best-rated candidate (rating ≥ 2) that overlaps
     `[t − 12 s, t + 25 s]`. Drop moments that do not snap.
  2. Fill up with well-rated candidates that nobody mentioned, keeping at least 12 s between frames.
  3. If there are still fewer than `min_frames`, add rating-1 candidates, then section anchors.
  4. Each plan entry needs `timestamp_sec`, `kind`, `caption`, `reason`, `source`, `section_index` and
     `offsets: [0, -1, 1]`. Use `KIND_MAP` for the kind.

**Done when:**
- On a talking-head video with slides, at least 7 of the 10 published frames are slides or diagrams, where the
  transcript-only selection mostly picked the presenter.
- The scan takes under 5 s for a 20-minute video.
- Tests cover scan parsing, state grouping (including two different white slides), baseline detection, and the
  snapping window.

---

## 4. Concept graph + cross-video links (`graph.py`, `CONCEPT_GRAPH=true`)

**Goal.**
- A small knowledge graph per video, rendered in Notion as a **Mermaid** diagram. It is a code block, not an
  image, so Constraint B is unaffected.
- A concept index with "first explained at mm:ss" deep links.
- On each new page, a list of **"Related videos in your notes"**: earlier pages in the same workspace that share
  concepts.

**Already provided:**
- `LLM.concept_graph` calls `graph_prompt` and then `normalize_graph`.
- The pipeline runs it in parallel with the reduce step, checkpoints it, and drops it if it fails.
- `notion.concept_blocks` renders the index and `to_mermaid`.
- `pipeline._related_pages` and `register_page` are called during publish.

**To implement:**
- **`graph_prompt(...)`.** Give the LLM the section outline in teaching order plus the key terms. Ask for JSON in
  this shape:
  - `{"concepts": [{id, name, definition, first_sec}], "relations": [{from, to, type, label}]}`
  - 6 to 14 concepts, 6 to 20 relations.
  - `type` is one of `REL_TYPES`.
  - Everything is written in `NOTES_LANGUAGE`.
- **`normalize_graph(raw, sections, duration)`.** Transcript-only extraction is noisy, so ground it:
  - Dedupe concepts by normalised name.
  - Anchor each concept to the first section that mentions it. Prefer the LLM's `first_sec` when it falls inside
    that section, or a section whose title matches.
  - Drop `prerequisite_of` edges that contradict teaching order (A taught well after B), and count them in
    `dropped_by_teaching_order`.
  - Remove self-loops, unknown ids and duplicate edges. Drop concepts that end up with no edges.
  - Cap at `MAX_CONCEPTS` concepts and `MAX_RELATIONS` relations.
- **`to_mermaid(graph)`.**
  - Start with `flowchart LR`, and write nodes as `c1["Name"]`.
  - Use `==>` for prerequisites, `-.->` for examples and contrasts, and `-->` for everything else, with the edge
    label as `|label|`.
  - Escape quotes.
- **Cross-video index.** Store it in one Redis hash per Notion workspace, `kg:<sha256(token)[:16]>`, with one
  field per video id.
  - Replays overwrite the video's field, so they never duplicate it.
  - `register_page` stores `{page_id, url, title, names, text}`.
  - `related_pages` returns earlier pages that share concepts. Match a concept when its normalised name appears
    as a phrase in the other video's names or definitions, so that "cost function" matches "…minimise the cost
    functions". It must never return the page's own video.
  - `forget_page` removes an entry. The pipeline calls it when a related page has been archived in Notion.

**Done when:**
- Two related videos (for example two linear-algebra lectures) each get a concept map.
- The second page links to the first.
- Prerequisite edges agree with teaching order.

---

## 5. Speech-to-text transcript fallback (`asr.py`, `ASR_FALLBACK=true`)

**Problem.** YouTube rate-limits its caption endpoints per public IP, so a whole campus network can be blocked
at once (`IpBlocked` or 429 on timedtext). Some videos also have no captions at all. The audio stream comes from
the media CDN and keeps working when captions are blocked.

**Already provided:**
- `pick_audio_stream(formats)` picks the lowest-bitrate audio-only stream; `youtube.fetch_metadata` stores it as
  `audio_stream`.
- `youtube.fetch_transcript` already calls `asr.transcribe(audio_stream)` after both caption paths fail. If ASR
  also fails, it reports the original caption error.
- `pipeline.transcribe` already logs an "ASR" event and caches transcripts per video.

**To implement:**
- **`transcribe(stream) -> (segments, info)`:**
  1. Download the audio with ffmpeg as 16 kHz mono `s16le` on stdout, using the stream's headers. A 20-minute
     video takes about 3 s.
  2. Raise `RateLimited("youtube", 60)` on a 429, and `Retryable(code="TRANSCRIPT_ERROR")` on a timeout or empty
     audio.
  3. Transcribe with `faster-whisper` on CPU (`int8`, `vad_filter=True`, `beam_size=1`).
  4. Load the model once per process, behind a lock.
  5. Return segments as `[{start, dur, text}]` and `info` as
     `{"source": "whisper-<model>", "language", "generated": True, "asr": True, "asr_seconds"}`.
     Raise `Permanent(code="NO_TRANSCRIPT")` if no speech was found.
- **Config:** add `ASR_MODEL` (default `base`) and `ASR_THREADS` (default 8) to `config.py`, and add
  `faster-whisper>=1.0` to `requirements-worker.txt`.
- **Image:**
  - Bake the model in at build time:
    `RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')"`
  - Set `ENV HF_HUB_OFFLINE=1` so jobs never call Hugging Face.

**Done when:**
- With captions blocked (or forced to fail), a 17-minute video completes, and the job events say it was
  transcribed with whisper-base.
- The fallback adds about 1–1.5 minutes on CPU and stays within the 20-minute limit.

---

## 6. Constraint A/B verifier (`tools/verify.py`, `make verify`)

**Goal.** Produce evidence for the grader that every image on a Notion page is a real frame from the source
video, at the stated timestamp, with an accurate caption.

**Usage:** `docker compose exec -e NOTION_TOKEN=… worker python -m notetaker.tools.verify <job_id> [--no-llm]`.
Add a `verify` target to the Makefile.

**Steps:**
1. Load the job (`JobStore`) and its frame checkpoints.
2. List the page's image blocks through the Notion API. Read each timestamp from the caption's `[mm:ss]`.
3. Download each image and compute its pHash. Compare it with the captured frame (distance 0 expected).
4. **Re-capture** the same timestamp from YouTube with `frames.py`, and compare that pHash too. Distance ≤ 4
   means the frame came from that point in the video.
5. Unless `--no-llm` is given, have the vision model judge each image and caption, in batches of
   `VISION_BATCH`. It returns `{authentic: bool, caption_accuracy: 1-5}`.
6. Print a table and a JSON summary:
   - Constraint A: total latency ≤ 20 min, taken from the job timeline;
   - Constraint B: number of images re-captured, mean caption accuracy.

**Done when:** running it on a finished job prints a pass or fail for A and B, with per-image rows.

---

## Suggested order

1. Extension.
2. Dashboard.

   Together these make the full demo possible.
3. Visual selection. This is the biggest gain in note quality.
4. Verifier.
5. ASR fallback.
6. Concept graph.
