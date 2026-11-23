// Background service worker:
//  • one-time Notion OAuth (chrome.identity.launchWebAuthFlow) — token stored in chrome.storage.local
//  • submits jobs to the gateway, tracks them, polls status, relays updates (via storage) to popup/content
//  • badge with active-job count + desktop notification when notes are ready
import { DEFAULT_GATEWAY, TERMINAL, MAX_VIDEO_SEC, errorText, fmtDuration } from "./shared.js";

const MAX_JOBS_KEPT = 40;
let pollPromise = null;
let lastPoll = 0;

// ------------------------------------------------------------------ storage helpers
async function getSettings() {
  const { settings } = await chrome.storage.local.get("settings");
  return { gatewayUrl: DEFAULT_GATEWAY, parentId: "", parentTitle: "", ...(settings || {}) };
}
async function getAuth() {
  const { auth } = await chrome.storage.local.get("auth");
  return auth || null;
}
async function getJobs() {
  const { jobs } = await chrome.storage.local.get("jobs");
  return jobs || {};
}
async function saveJobs(jobs) {
  const ids = Object.keys(jobs).sort((a, b) => (jobs[b].submitted_at || 0) - (jobs[a].submitted_at || 0));
  const trimmed = {};
  for (const id of ids.slice(0, MAX_JOBS_KEPT)) trimmed[id] = jobs[id];
  await chrome.storage.local.set({ jobs: trimmed });
  await updateBadge(trimmed);
}
async function patchJob(id, patch) {
  const jobs = await getJobs();
  if (!jobs[id]) return;
  jobs[id] = { ...jobs[id], ...patch };
  await saveJobs(jobs);
}

// ------------------------------------------------------------------ gateway client
class ApiError extends Error {
  constructor(code, message, status) { super(message); this.code = code; this.status = status; }
}

async function api(path, { method = "GET", body, headers = {}, timeoutMs = 15000 } = {}) {
  const { gatewayUrl } = await getSettings();
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let resp;
  try {
    resp = await fetch(gatewayUrl.replace(/\/$/, "") + path, {
      method,
      headers: { "Content-Type": "application/json", ...headers },
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
  } catch (e) {
    throw new ApiError("GATEWAY_UNREACHABLE", `Can’t reach the notes server at ${gatewayUrl}`, 0);
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try { data = await resp.json(); } catch { /* non-JSON */ }
  if (!resp.ok) {
    const e = data?.error || {};
    throw new ApiError(e.code || `HTTP_${resp.status}`, e.message || `Server returned ${resp.status}`, resp.status);
  }
  return data;
}

// ------------------------------------------------------------------ Notion OAuth
async function connectNotion() {
  const cfg = await api("/oauth/notion/config");
  if (!cfg.configured) throw new ApiError("OAUTH_NOT_CONFIGURED", "Server has no Notion OAuth app configured");
  const redirectUri = chrome.identity.getRedirectURL("notion");
  const state = crypto.randomUUID();
  const url = `${cfg.authorize_url}?client_id=${encodeURIComponent(cfg.client_id)}&response_type=code&owner=user`
    + `&redirect_uri=${encodeURIComponent(redirectUri)}&state=${state}`;
  let responseUrl;
  try {
    responseUrl = await chrome.identity.launchWebAuthFlow({ url, interactive: true });
  } catch (e) {
    throw new ApiError("OAUTH_CANCELLED", String(e?.message || e));
  }
  const u = new URL(responseUrl);
  if (u.searchParams.get("error")) throw new ApiError("OAUTH_CANCELLED", u.searchParams.get("error"));
  if (u.searchParams.get("state") !== state) throw new ApiError("OAUTH_STATE_MISMATCH", "OAuth state mismatch");
  const tok = await api("/oauth/notion/exchange", {
    method: "POST", body: { code: u.searchParams.get("code"), redirect_uri: redirectUri },
  });
  const auth = {
    mode: "oauth", accessToken: tok.access_token, refreshToken: tok.refresh_token || null,
    workspaceName: tok.workspace_name || "Notion", workspaceIcon: tok.workspace_icon || null, connectedAt: Date.now(),
  };
  await chrome.storage.local.set({ auth });
  return auth;
}

async function ensureToken({ interactive }) {
  const auth = await getAuth();
  if (auth?.accessToken) return auth.accessToken;
  if (!interactive) throw new ApiError("MISSING_NOTION_TOKEN", "Connect Notion first");
  try {
    const fresh = await connectNotion(); // one-time; stored for every later job
    return fresh.accessToken;
  } catch (e) {
    // No OAuth app on this server: take the user straight to the token form instead of a dead end.
    if (e?.code === "OAUTH_NOT_CONFIGURED") chrome.runtime.openOptionsPage();
    throw e;
  }
}

// ------------------------------------------------------------------ jobs
async function submitJob({ videoId, url, durationSec, title }) {
  if (!videoId) throw new ApiError("INVALID_YOUTUBE_URL", "Not a YouTube video page");
  if (durationSec && durationSec > MAX_VIDEO_SEC + 1) {
    throw new ApiError("VIDEO_TOO_LONG", `This video is ${fmtDuration(durationSec)}; up to 20 min is supported`);
  }
  // Client-side de-dup: one active job per video.
  const jobs = await getJobs();
  const active = Object.values(jobs).find((j) => j.video_id === videoId && !TERMINAL.has(j.status));
  if (active) return { job: active, existing: true };

  const token = await ensureToken({ interactive: true });
  const { parentId } = await getSettings();
  const body = {
    youtube_url: url || `https://www.youtube.com/watch?v=${videoId}`,
    notion_token: token,
    ...(parentId ? { notion_parent_id: parentId } : {}),
    ...(durationSec ? { duration_sec: Math.round(durationSec) } : {}),
    ...(title ? { video_title: title.slice(0, 300) } : {}),
  };
  const idem = `${videoId}-${Math.floor(Date.now() / 15000)}`; // absorbs accidental double clicks
  const res = await api("/jobs", { method: "POST", body, headers: { "Idempotency-Key": idem } });
  const job = {
    job_id: res.job_id, video_id: res.video_id, status: res.status, progress: 0,
    video_title: title || null, duration_sec: durationSec ? Math.round(durationSec) : null,
    submitted_at: Date.now(), warnings: [], error: null, result: null,
  };
  const all = await getJobs();
  all[job.job_id] = { ...(all[job.job_id] || {}), ...job };
  await saveJobs(all);
  schedulePolling();
  return { job, existing: false };
}

async function pollActive({ force = false } = {}) {
  if (pollPromise) return pollPromise;
  if (!force && Date.now() - lastPoll < 1000) return;
  pollPromise = (async () => {
    lastPoll = Date.now();
    const jobs = await getJobs();
    const active = Object.values(jobs).filter((j) => !TERMINAL.has(j.status));
    if (!active.length) return;
    const updates = await Promise.allSettled(active.map((j) => api(`/jobs/${j.job_id}`, { timeoutMs: 8000 })));
    const fresh = await getJobs();
    const finished = [];
    updates.forEach((u, i) => {
      const id = active[i].job_id;
      if (!fresh[id]) return;
      if (u.status === "fulfilled") {
        const v = u.value;
        const wasTerminal = TERMINAL.has(fresh[id].status);
        fresh[id] = {
          ...fresh[id], status: v.status, progress: v.progress, video_title: v.video_title || fresh[id].video_title,
          duration_sec: v.duration_sec || fresh[id].duration_sec, warnings: v.warnings || [], error: v.error,
          result: v.result, retry: v.retry, waiting: v.waiting, events: v.events, timeline: v.timeline,
          frames_requested: v.frames_requested, server_ok: true, updated_at: Date.now(),
        };
        if (!wasTerminal && TERMINAL.has(v.status)) finished.push(fresh[id]);
      } else if (u.reason?.code === "JOB_NOT_FOUND") {
        fresh[id] = { ...fresh[id], status: "FAILED", error: { code: "JOB_NOT_FOUND", message: "The server no longer knows this job (it may have been reset)." } };
      } else {
        fresh[id] = { ...fresh[id], server_ok: false };
      }
    });
    await saveJobs(fresh);
    for (const j of finished) notify(j);
  })().finally(() => { pollPromise = null; });
  return pollPromise;
}

function notify(job) {
  const ok = job.status === "COMPLETED" || job.status === "COMPLETED_WITH_WARNINGS";
  const title = ok ? (job.status === "COMPLETED" ? "Notes ready ✓" : "Notes ready (with warnings)") : "Couldn’t create notes";
  const message = (job.video_title || "Your video") + (ok ? "" : ` — ${errorText(job.error)}`);
  chrome.notifications.create(`job:${job.job_id}`, {
    type: "basic", iconUrl: "icons/icon128.png", title, message, priority: 1,
    buttons: ok && job.result?.notion_page_url ? [{ title: "View in Notion" }] : [],
  });
}

async function openJobResult(notificationId) {
  if (!notificationId.startsWith("job:")) return;
  const jobs = await getJobs();
  const job = jobs[notificationId.slice(4)];
  if (job?.result?.notion_page_url) chrome.tabs.create({ url: job.result.notion_page_url });
  chrome.notifications.clear(notificationId);
}
chrome.notifications.onClicked.addListener(openJobResult);
chrome.notifications.onButtonClicked.addListener((id) => openJobResult(id));

async function updateBadge(jobs) {
  const n = Object.values(jobs || (await getJobs())).filter((j) => !TERMINAL.has(j.status)).length;
  await chrome.action.setBadgeText({ text: n ? String(n) : "" });
  await chrome.action.setBadgeBackgroundColor({ color: "#6d5efc" });
}

// Polling: popup and content scripts ask for refreshes while open; an alarm keeps it going otherwise.
function schedulePolling() {
  chrome.alarms.create("poll", { periodInMinutes: 0.5 });
  // Short burst loop while the worker is alive (fetch activity keeps the service worker awake).
  let ticks = 0;
  const tick = async () => {
    ticks += 1;
    await pollActive();
    const jobs = await getJobs();
    if (ticks < 200 && Object.values(jobs).some((j) => !TERMINAL.has(j.status))) setTimeout(tick, 2500);
  };
  setTimeout(tick, 1500);
}
chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name !== "poll") return;
  await pollActive({ force: true });
  const jobs = await getJobs();
  if (!Object.values(jobs).some((j) => !TERMINAL.has(j.status))) chrome.alarms.clear("poll");
});
chrome.runtime.onStartup.addListener(() => { updateBadge(); schedulePolling(); });
chrome.runtime.onInstalled.addListener(() => { updateBadge(); });

// ------------------------------------------------------------------ messaging
const handlers = {
  async SUBMIT_JOB(msg) { return submitJob(msg); },
  async POLL() { await pollActive(); return { jobs: await getJobs() }; },
  async CONNECT_NOTION() { const a = await connectNotion(); return { workspaceName: a.workspaceName }; },
  async DISCONNECT_NOTION() { await chrome.storage.local.remove("auth"); return {}; },
  async SET_INTERNAL_TOKEN({ token }) {
    const t = (token || "").trim();
    if (!/^(secret_|ntn_)[A-Za-z0-9]{20,}$/.test(t)) throw new ApiError("INVALID_NOTION_TOKEN", "That doesn’t look like a Notion integration token (secret_… or ntn_…)");
    await api("/notion/pages", { headers: { Authorization: `Bearer ${t}` } }); // validates the token
    await chrome.storage.local.set({ auth: { mode: "internal", accessToken: t, workspaceName: "Internal integration", connectedAt: Date.now() } });
    return {};
  },
  async LIST_PAGES() {
    const token = await ensureToken({ interactive: false });
    return api("/notion/pages", { headers: { Authorization: `Bearer ${token}` } });
  },
  async HEALTH() { return api("/healthz", { timeoutMs: 5000 }).catch((e) => ({ status: "down", error: e.code })); },
  async DISMISS({ jobId }) { const jobs = await getJobs(); delete jobs[jobId]; await saveJobs(jobs); return {}; },
  async CLEAR_FINISHED() {
    const jobs = await getJobs();
    for (const [id, j] of Object.entries(jobs)) if (TERMINAL.has(j.status)) delete jobs[id];
    await saveJobs(jobs); return {};
  },
  async RETRY({ jobId }) {
    const jobs = await getJobs();
    const old = jobs[jobId];
    if (!old) throw new ApiError("JOB_NOT_FOUND", "Job not found");
    delete jobs[jobId];
    await saveJobs(jobs);
    if (old.error?.code === "NOTION_UNAUTHORIZED") await chrome.storage.local.remove("auth");
    return submitJob({ videoId: old.video_id, title: old.video_title, durationSec: old.duration_sec });
  },
};

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const h = handlers[msg?.type];
  if (!h) return false;
  h(msg).then(
    (data) => sendResponse({ ok: true, ...data }),
    (e) => sendResponse({ ok: false, error: { code: e.code || "INTERNAL_ERROR", message: e.message || String(e) } }),
  );
  return true; // async response
});
