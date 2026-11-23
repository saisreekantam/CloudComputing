import {
  STAGES, TERMINAL, STATUS_TEXT, MAX_VIDEO_SEC, errorText, warningText, retryText, waitingText, recoveryText,
  stageIndex, failedStageIndex, thumb, fmtDuration, timeAgo,
} from "./shared.js";

const $ = (s, el = document) => el.querySelector(s);
const send = (type, extra = {}) => chrome.runtime.sendMessage({ type, ...extra });

let jobs = {};
let auth = null;
let currentTab = null;

// ------------------------------------------------------------------ connection pill
function renderConn() {
  const el = $("#conn");
  if (auth?.accessToken) {
    el.className = "pill on";
    el.innerHTML = `<i></i><span></span>`;
    el.querySelector("span").textContent = auth.workspaceName || "Notion connected";
    el.title = "Connected to Notion — click to manage";
  } else {
    el.className = "pill off";
    el.textContent = "Connect Notion";
    el.title = "One-time Notion authorization";
  }
}

$("#conn").addEventListener("click", async () => {
  if (auth?.accessToken) return chrome.runtime.openOptionsPage();
  const el = $("#conn");
  el.textContent = "Connecting…";
  const res = await send("CONNECT_NOTION");
  if (!res.ok) {
    el.textContent = "Connect Notion";
    if (res.error.code === "OAUTH_NOT_CONFIGURED") chrome.runtime.openOptionsPage();
    else alertBox(errorText(res.error));
  }
});
$("#opts").addEventListener("click", () => chrome.runtime.openOptionsPage());
$("#clear").addEventListener("click", () => send("CLEAR_FINISHED"));

function alertBox(text) {
  const box = document.createElement("div");
  box.className = "notice err";
  box.style.margin = "10px 12px 0";
  box.textContent = text;
  document.body.insertBefore(box, $("#jobs"));
  setTimeout(() => box.remove(), 6000);
}

// ------------------------------------------------------------------ current tab card
async function loadCurrentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  try {
    const u = new URL(tab?.url || "");
    const v = u.hostname.endsWith("youtube.com") && u.pathname === "/watch" ? u.searchParams.get("v") : null;
    currentTab = v ? { videoId: v, title: (tab.title || "").replace(/^\(\d+\)\s*/, "").replace(/ - YouTube$/, "") } : null;
  } catch { currentTab = null; }
}

function renderCurrent() {
  const el = $("#current");
  if (!currentTab) { el.hidden = true; return; }
  const existing = Object.values(jobs).filter((j) => j.video_id === currentTab.videoId)
    .sort((a, b) => b.submitted_at - a.submitted_at)[0];
  if (existing && !TERMINAL.has(existing.status)) { el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = "";
  const img = document.createElement("img");
  img.src = thumb(currentTab.videoId);
  const mid = document.createElement("div");
  mid.className = "grow";
  mid.innerHTML = `<div class="c-title"></div><div class="c-sub">This tab</div>`;
  mid.querySelector(".c-title").textContent = currentTab.title || "YouTube video";
  const btn = document.createElement("button");
  btn.className = "btn primary";
  btn.textContent = existing?.status?.startsWith("COMPLETED") ? "Notes again" : "Take notes";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Sending…";
    const res = await send("SUBMIT_JOB", {
      videoId: currentTab.videoId, url: `https://www.youtube.com/watch?v=${currentTab.videoId}`, title: currentTab.title,
    });
    if (!res.ok) { alertBox(errorText(res.error)); btn.disabled = false; btn.textContent = "Take notes"; }
  });
  el.append(img, mid, btn);
}

// ------------------------------------------------------------------ job cards
function kindOf(job) {
  if (job.status === "COMPLETED") return "ok";
  if (job.status === "COMPLETED_WITH_WARNINGS") return "warn";
  if (job.status === "FAILED" || job.status === "DEAD_LETTERED") return "err";
  return "run";
}

function renderStepper(ol, job) {
  ol.innerHTML = "";
  const kind = kindOf(job);
  const cur = kind === "err" ? failedStageIndex(job) : stageIndex(job.status);
  STAGES.forEach((s, i) => {
    const li = document.createElement("li");
    li.textContent = s.label;
    if (kind === "err") {
      if (i < cur) li.className = "done";
      else if (i === cur) li.className = "fail";
    } else if (kind === "ok" || kind === "warn") {
      li.className = i === 5 && kind === "warn" ? "done warnd" : "done";
    } else if (i < cur) li.className = "done";
    else if (i === cur) li.className = "now";
    ol.appendChild(li);
  });
}

function statusLine(job) {
  if (job.status === "COMPLETED_WITH_WARNINGS" && job.result) {
    return `Done — ${job.result.frames_published} of ${job.result.frames_requested} key frames published`;
  }
  if (job.status === "COMPLETED" && job.result) {
    const n = job.result.frames_published;
    return `Done — notes with ${n} key frame${n === 1 ? "" : "s"} are in Notion`;
  }
  if (!TERMINAL.has(job.status) && job.server_ok === false) return "Reconnecting to the notes server…";
  const base = STATUS_TEXT[job.status] || job.status;
  return TERMINAL.has(job.status) ? base : `${base}… ${job.progress ? job.progress + "%" : ""}`;
}

function renderJob(job) {
  const node = $("#job-tpl").content.firstElementChild.cloneNode(true);
  const kind = kindOf(job);
  node.dataset.kind = kind;
  $(".thumb", node).src = thumb(job.video_id);
  $(".job-title", node).textContent = job.video_title || `YouTube video ${job.video_id}`;
  $(".job-sub", node).textContent = [fmtDuration(job.duration_sec), `submitted ${timeAgo(job.submitted_at)}`].filter(Boolean).join(" · ");
  renderStepper($(".stepper", node), job);
  $(".bar span", node).style.width = `${kind === "err" ? 100 : job.progress || 0}%`;
  $(".status-line", node).textContent = statusLine(job);

  if (!TERMINAL.has(job.status)) {
    const wait = retryText(job) || waitingText(job);
    if (wait) { $(".notice.wait", node).hidden = false; $(".notice.wait", node).textContent = "⏳ " + wait; }
    const rec = recoveryText(job);
    if (rec) { $(".notice.recover", node).hidden = false; $(".notice.recover", node).textContent = "🔄 " + rec; }
  }
  if (job.warnings?.length) {
    const box = $(".notice.warn", node);
    box.hidden = false;
    box.innerHTML = "<b>Heads up</b><ul></ul>";
    const ul = box.querySelector("ul");
    job.warnings.slice(0, 6).forEach((w) => { const li = document.createElement("li"); li.textContent = warningText(w); ul.appendChild(li); });
    if (job.warnings.length > 6) { const li = document.createElement("li"); li.textContent = `…and ${job.warnings.length - 6} more`; ul.appendChild(li); }
  }
  if (kind === "err") {
    const box = $(".notice.err", node);
    box.hidden = false;
    const prefix = job.status === "DEAD_LETTERED"
      ? "Gave up after repeated attempts and set it aside for inspection. "
      : "";
    box.textContent = prefix + errorText(job.error);
  }

  const actions = $(".actions", node);
  if (job.result?.notion_page_url) {
    const a = document.createElement("a");
    a.className = "btn primary";
    a.href = job.result.notion_page_url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "View in Notion ↗";
    actions.appendChild(a);
  }
  if (kind === "err" && !["VIDEO_TOO_LONG", "NO_TRANSCRIPT", "AGE_RESTRICTED"].includes(job.error?.code)) {
    const b = document.createElement("button");
    b.className = "btn";
    b.textContent = job.error?.code === "NOTION_UNAUTHORIZED" ? "Reconnect & retry" : "Try again";
    b.addEventListener("click", async () => {
      b.disabled = true;
      const res = await send("RETRY", { jobId: job.job_id });
      if (!res.ok) { alertBox(errorText(res.error)); b.disabled = false; }
    });
    actions.appendChild(b);
  }
  $(".dismiss", node).addEventListener("click", () => send("DISMISS", { jobId: job.job_id }));
  return node;
}

function renderJobs() {
  const list = Object.values(jobs).sort((a, b) => b.submitted_at - a.submitted_at);
  const main = $("#jobs");
  main.innerHTML = "";
  if (!list.length) {
    main.innerHTML = `<div class="empty"><b>No notes yet</b>Open a YouTube video (up to ${MAX_VIDEO_SEC / 60} min) and click
      <i>Take Notes</i> in the player toolbar.</div>`;
  } else {
    list.forEach((j) => main.appendChild(renderJob(j)));
  }
  $("#clear").hidden = !list.some((j) => TERMINAL.has(j.status));
  renderCurrent();
}

async function renderServer() {
  const el = $("#server");
  const res = await send("HEALTH");
  const status = res?.status || "down";
  el.className = `server ${status}`;
  const w = res?.checks?.workers?.alive;
  el.querySelector("span").textContent =
    status === "ok" ? `Server online · ${w} worker${w === 1 ? "" : "s"}` :
    status === "degraded" ? "Server online · no workers running" : "Server unreachable";
}

// ------------------------------------------------------------------ boot
async function boot() {
  ({ jobs = {}, auth = null } = await chrome.storage.local.get(["jobs", "auth"]));
  await loadCurrentTab();
  renderConn();
  renderJobs();
  renderServer();
  send("POLL");
  // While open: fast polling + 1s re-render for countdowns.
  setInterval(() => send("POLL"), 1500);
  setInterval(() => { if (Object.values(jobs).some((j) => j.retry || j.waiting)) renderJobs(); }, 1000);
  setInterval(renderServer, 10000);
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.jobs) { jobs = changes.jobs.newValue || {}; renderJobs(); }
  if (changes.auth) { auth = changes.auth.newValue || null; renderConn(); }
});

boot();
