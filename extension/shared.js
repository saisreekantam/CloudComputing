// Shared helpers + human-readable text for statuses, warnings and errors.
// Raw JSON is never shown to the user — everything goes through these maps.

export const DEFAULT_GATEWAY = "http://localhost:8000";
export const MAX_VIDEO_SEC = 1200;

export const STAGES = [
  { status: "QUEUED", label: "Queued" },
  { status: "TRANSCRIBING", label: "Transcribing" },
  { status: "SUMMARIZING", label: "Summarizing" },
  { status: "EXTRACTING_FRAMES", label: "Extracting Frames" },
  { status: "PUBLISHING", label: "Publishing" },
  { status: "DONE", label: "Done" },
];

export const TERMINAL = new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"]);

export const STATUS_TEXT = {
  SUBMITTING: "Sending to the notes server…",
  QUEUED: "Queued — waiting for a free worker",
  TRANSCRIBING: "Fetching the transcript",
  SUMMARIZING: "Writing the study notes",
  EXTRACTING_FRAMES: "Capturing key frames from the video",
  PUBLISHING: "Publishing to Notion",
  COMPLETED: "Done — notes are in Notion",
  COMPLETED_WITH_WARNINGS: "Done, with warnings",
  FAILED: "Failed",
  DEAD_LETTERED: "Gave up after repeated attempts",
};

const SERVICE = { llm: "the AI model", notion: "Notion", youtube: "YouTube" };

export const ERROR_TEXT = {
  VIDEO_TOO_LONG: "This video is longer than 20 minutes. Only videos up to 20 minutes are supported.",
  NO_TRANSCRIPT: "This video has no captions or transcript, so notes can’t be generated.",
  VIDEO_UNAVAILABLE: "The video is private, removed or unavailable.",
  AGE_RESTRICTED: "Age-restricted videos can’t be processed.",
  LIVE_NOT_SUPPORTED: "Live streams and premieres aren’t supported. Try again once the video has finished.",
  INVALID_VIDEO: "That doesn’t look like a valid YouTube video.",
  INVALID_YOUTUBE_URL: "That doesn’t look like a YouTube video page.",
  NOTION_UNAUTHORIZED: "Notion access was revoked or expired. Reconnect Notion and try again.",
  NOTION_TOKEN_MISSING: "Notion isn’t connected. Connect Notion and try again.",
  MISSING_NOTION_TOKEN: "Connect Notion first (click the extension icon).",
  NOTION_PARENT_NOT_FOUND: "The destination Notion page isn’t shared with the integration. Pick another page in Options.",
  NOTION_NO_DESTINATION: "No Notion page is shared with the integration. Reconnect Notion and select a page to share.",
  NOTION_FORBIDDEN: "The Notion integration doesn’t have permission to write to that page.",
  NOTION_BAD_REQUEST: "Notion rejected the page content.",
  LLM_AUTH_FAILED: "The server’s AI key was rejected (server configuration problem).",
  LLM_NOT_CONFIGURED: "The server has no AI key configured.",
  LLM_MODEL_NOT_FOUND: "The server’s AI model setting is invalid.",
  LLM_BAD_OUTPUT: "The AI model kept returning unusable output.",
  RATE_LIMITED: "External services stayed rate-limited for too long.",
  CRASH_LOOP: "Workers kept crashing on this video, so it was quarantined for inspection.",
  GATEWAY_UNREACHABLE: "Can’t reach the notes server. Is it running? (Options → server URL)",
  STATE_STORE_UNAVAILABLE: "The notes server is starting up or unhealthy. Try again in a moment.",
  BROKER_UNAVAILABLE: "The notes server’s job queue is unavailable. Try again in a moment.",
  OAUTH_NOT_CONFIGURED: "The server has no Notion OAuth app configured. Paste an internal integration token in Options instead.",
  OAUTH_CANCELLED: "Notion connection was cancelled.",
  INTERNAL_ERROR: "An unexpected server error occurred.",
};

export function fmtTs(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(h ? 2 : 2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function fmtDuration(sec) {
  if (!sec) return "";
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return m ? `${m} min ${s ? s + " s" : ""}`.trim() : `${s} s`;
}

export function timeAgo(ms) {
  const d = Math.max(0, (Date.now() - ms) / 1000);
  if (d < 45) return "just now";
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return `${Math.round(d / 86400)} d ago`;
}

export function warningText(w) {
  const at = w.timestamp_sec != null && w.timestamp_sec >= 0 ? ` at ${fmtTs(w.timestamp_sec)}` : "";
  switch (w.code) {
    case "FRAME_EXTRACTION_FAILED": return `The frame${at} couldn’t be captured, so it was left out (no substitute image).`;
    case "FRAME_UPLOAD_FAILED": return `The frame${at} couldn’t be uploaded to Notion, so it was left out.`;
    case "FRAME_DUPLICATE_DROPPED": return `The frame${at} showed the same slide as an earlier one, so it was skipped.`;
    case "FRAME_TIMESTAMP_INVALID": return `The AI flagged a moment${at} that isn’t in the video. It was ignored.`;
    case "STREAM_UNAVAILABLE": return "The video stream couldn’t be opened, so the page is text-only.";
    case "METADATA_DEGRADED": return "Some video details couldn’t be read. The notes were still generated.";
    default: return w.message || w.code;
  }
}

export function errorText(e) {
  if (!e) return "";
  return ERROR_TEXT[e.code] || e.message || "Something went wrong.";
}

export function retryText(job) {
  const r = job.retry;
  if (!r) return "";
  const secs = Math.max(0, Math.round(r.retry_at - Date.now() / 1000));
  const when = secs > 0 ? `retrying in ${secs}s` : "retrying now";
  if (r.kind === "rate_limit") {
    return `Waiting — ${SERVICE[r.service] || "an external service"} is rate-limiting us, ${when} (retry ${r.attempt}/${r.max_attempts}).`;
  }
  return `Temporary problem (${r.message || r.code}); ${when} (attempt ${r.attempt + 1}/${r.max_attempts + 1}).`;
}

export function waitingText(job) {
  const w = job.waiting;
  if (!w) return "";
  const secs = Math.max(0, Math.round(w.until - Date.now() / 1000));
  return `Pacing requests to ${SERVICE[w.service] || "an external API"} to stay under its rate limit${secs ? ` (~${secs}s)` : ""}…`;
}

export function recoveryText(job) {
  const ev = (job.events || []).slice().reverse().find((e) => ["resumed", "recovering", "reconciled"].includes(e.kind));
  if (!ev) return "";
  if (ev.kind === "recovering") return "A worker stopped mid-job. Handing the job to a healthy worker…";
  if (ev.kind === "reconciled") return "Cleaned up a partial page left by an interrupted attempt.";
  return "A worker crashed and the job resumed automatically on another worker.";
}

export function stageIndex(status) {
  if (status === "COMPLETED" || status === "COMPLETED_WITH_WARNINGS") return 5;
  const i = STAGES.findIndex((s) => s.status === status);
  return i < 0 ? 0 : i;
}

export function failedStageIndex(job) {
  const stage = job.error?.stage;
  const map = { transcribe: 1, summarize: 2, frames: 3, publish: 4 };
  return map[stage] ?? Math.max(0, (job.timeline || []).filter((t) => t.status !== "FAILED" && t.status !== "DEAD_LETTERED").length - 1);
}

export function thumb(videoId) {
  return `https://i.ytimg.com/vi/${videoId}/mqdefault.jpg`;
}
