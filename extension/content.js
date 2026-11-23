// Content script — injects a "Take Notes" button into the YouTube player toolbar.
// YouTube is a single-page app: the player is reused across videos and the toolbar is
// re-rendered at will. So we (1) never cache the video id — it is read from the URL at click
// time, (2) re-inject on `yt-navigate-finish` and whenever a MutationObserver sees the toolbar
// without our button, and (3) re-derive the button state from storage for the *current* video.
(() => {
  if (window.__vnaLoaded) return;
  window.__vnaLoaded = true;

  const BTN_ID = "vna-take-notes";
  const MAX_SEC = 1200;
  const TERMINAL = new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"]);
  const LABEL = {
    QUEUED: "Queued…", TRANSCRIBING: "Transcribing…", SUMMARIZING: "Summarizing…",
    EXTRACTING_FRAMES: "Extracting frames…", PUBLISHING: "Publishing…",
    COMPLETED: "Open notes", COMPLETED_WITH_WARNINGS: "Open notes", FAILED: "Failed — retry", DEAD_LETTERED: "Failed — retry",
  };
  const ICON = `<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M6 2h9l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zm8 1.5V8h4.5L14 3.5zM7 12h10v1.6H7V12zm0 3.4h10V17H7v-1.6zm0-6.8h5v1.6H7V8.6z"/></svg>`;

  let busy = false;
  let pollTimer = null;

  const alive = () => { try { return Boolean(chrome.runtime?.id); } catch { return false; } };

  function currentVideoId() {
    const u = new URL(location.href);
    if (u.pathname !== "/watch") return null;
    const v = u.searchParams.get("v");
    return v && /^[A-Za-z0-9_-]{11}$/.test(v) ? v : null;
  }

  function videoInfo() {
    const player = document.querySelector("#movie_player");
    const video = player?.querySelector("video.html5-main-video") || player?.querySelector("video");
    const isAd = player?.classList.contains("ad-showing");
    const duration = !isAd && video && Number.isFinite(video.duration) && video.duration > 0 ? video.duration : null;
    const title = document.querySelector("ytd-watch-metadata h1")?.textContent?.trim()
      || document.title.replace(/^\(\d+\)\s*/, "").replace(/ - YouTube$/, "");
    return { duration, title };
  }

  function toast(text, kind = "info") {
    const host = document.querySelector("#movie_player") || document.body;
    let el = host.querySelector(".vna-toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "vna-toast";
      host.appendChild(el);
    }
    el.textContent = text;
    el.dataset.kind = kind;
    el.classList.add("vna-show");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("vna-show"), 4500);
  }

  function setButton(btn, state, label, progress) {
    btn.dataset.state = state;
    btn.querySelector(".vna-label").textContent = label;
    btn.style.setProperty("--vna-progress", `${progress || 0}%`);
    btn.title = state === "idle" ? "Take Notes — send this video to Notion as study notes" : label;
  }

  async function latestJobFor(videoId) {
    if (!alive()) return null;
    const { jobs } = await chrome.storage.local.get("jobs");
    return Object.values(jobs || {}).filter((j) => j.video_id === videoId)
      .sort((a, b) => b.submitted_at - a.submitted_at)[0] || null;
  }

  async function refreshState() {
    const btn = document.getElementById(BTN_ID);
    const vid = currentVideoId();
    if (!btn || !vid) return;
    if (busy) return;
    const job = await latestJobFor(vid);
    if (!job) return setButton(btn, "idle", "Take Notes", 0);
    if (!TERMINAL.has(job.status)) {
      const waiting = job.retry ? "Waiting (rate limit)…" : LABEL[job.status] || "Working…";
      setButton(btn, "active", waiting, job.progress);
      ensurePolling();
    } else if (job.status.startsWith("COMPLETED")) {
      setButton(btn, job.status === "COMPLETED" ? "done" : "warn", LABEL[job.status], 100);
    } else {
      setButton(btn, "error", LABEL[job.status], 0);
    }
  }

  function ensurePolling() {
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      if (!alive()) return clearInterval(pollTimer);
      const job = await latestJobFor(currentVideoId());
      if (!job || TERMINAL.has(job.status) || document.hidden) {
        if (!job || TERMINAL.has(job.status)) { clearInterval(pollTimer); pollTimer = null; }
        return;
      }
      chrome.runtime.sendMessage({ type: "POLL" }).catch(() => {});
    }, 3000);
  }

  async function onClick(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    if (!alive()) return toast("The extension was updated — refresh this page to keep using it.", "error");
    const vid = currentVideoId(); // read at click time: survives SPA navigation
    if (!vid || busy) return;
    const btn = document.getElementById(BTN_ID);
    const job = await latestJobFor(vid);
    if (job && job.status.startsWith("COMPLETED") && job.result?.notion_page_url) {
      window.open(job.result.notion_page_url, "_blank", "noopener");
      return;
    }
    if (job && !TERMINAL.has(job.status)) return toast("Notes for this video are already in progress — open the extension to follow along.");

    const { duration, title } = videoInfo();
    if (duration && duration > MAX_SEC + 1) {
      return toast(`This video is ${Math.round(duration / 60)} min long. Notes support videos up to 20 min.`, "error");
    }
    busy = true;
    setButton(btn, "active", "Sending…", 2);
    try {
      const res = await chrome.runtime.sendMessage({
        type: job ? "RETRY" : "SUBMIT_JOB", jobId: job?.job_id, videoId: vid,
        url: `https://www.youtube.com/watch?v=${vid}`, durationSec: duration, title,
      });
      if (!res?.ok) {
        toast(res?.error?.message || "Couldn’t submit this video.", "error");
      } else {
        toast(res.existing ? "Already taking notes on this video." : "Taking notes — follow progress from the extension icon.", "ok");
        ensurePolling();
      }
    } catch (e) {
      toast("The extension was updated — refresh this page.", "error");
    } finally {
      busy = false;
      refreshState();
    }
  }

  function inject() {
    const vid = currentVideoId();
    const existing = document.getElementById(BTN_ID);
    if (!vid) { existing?.remove(); return; }
    const controls = document.querySelector("#movie_player .ytp-right-controls");
    if (!controls) return;
    if (existing && controls.contains(existing)) return;
    existing?.remove();
    const btn = document.createElement("button");
    btn.id = BTN_ID;
    btn.className = "ytp-button vna-btn";
    btn.setAttribute("aria-label", "Take Notes");
    btn.innerHTML = `<span class="vna-bar"></span>${ICON}<span class="vna-label">Take Notes</span>`;
    btn.addEventListener("click", onClick, true);
    controls.prepend(btn);
    refreshState();
  }

  // SPA navigation hooks + resilient re-injection.
  document.addEventListener("yt-navigate-finish", () => { inject(); refreshState(); });
  window.addEventListener("popstate", () => setTimeout(inject, 300));
  let scheduled = false;
  new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; inject(); });
  }).observe(document.documentElement, { childList: true, subtree: true });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.jobs) refreshState();
  });
  inject();
})();
