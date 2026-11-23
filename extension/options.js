import { DEFAULT_GATEWAY, errorText } from "./shared.js";

const $ = (s) => document.querySelector(s);
const send = (type, extra = {}) => chrome.runtime.sendMessage({ type, ...extra });
const msg = (el, text, ok) => { el.textContent = text; el.className = `msg ${ok ? "ok" : "err"}`; };

async function load() {
  const { settings = {}, auth } = await chrome.storage.local.get(["settings", "auth"]);
  $("#gw").value = settings.gatewayUrl || DEFAULT_GATEWAY;
  $("#redirect").textContent = chrome.identity.getRedirectURL("notion");
  renderAuth(auth);
  if (settings.parentId) {
    const o = new Option(settings.parentTitle || settings.parentId, settings.parentId, true, true);
    $("#parent").add(o);
  }
  if (auth?.accessToken) loadPages(settings.parentId);
}

function renderAuth(auth) {
  $("#auth-state").textContent = auth?.accessToken
    ? `Connected: ${auth.workspaceName || "Notion"} (${auth.mode === "internal" ? "internal integration" : "OAuth"})`
    : "Not connected. The first “Take Notes” click will also start the one-time OAuth flow.";
  $("#disconnect").hidden = !auth?.accessToken;
}

$("#save-gw").addEventListener("click", async () => {
  let url = $("#gw").value.trim().replace(/\/$/, "") || DEFAULT_GATEWAY;
  try { url = new URL(url).origin; } catch { return msg($("#gw-msg"), "Not a valid URL", false); }
  // Custom servers need host permission (requested here, during the user's click).
  if (!/^http:\/\/(localhost|127\.0\.0\.1):8000$/.test(url)) {
    const granted = await chrome.permissions.request({ origins: [`${url}/*`] });
    if (!granted) return msg($("#gw-msg"), "Permission to contact that server was declined", false);
  }
  const { settings = {} } = await chrome.storage.local.get("settings");
  await chrome.storage.local.set({ settings: { ...settings, gatewayUrl: url } });
  const h = await send("HEALTH");
  if (h.status === "ok") msg($("#gw-msg"), `Server OK · ${h.checks.workers.alive} worker(s) online`, true);
  else if (h.status === "degraded") msg($("#gw-msg"), "Server reachable, but no workers are running", false);
  else msg($("#gw-msg"), "Can’t reach the server at that URL", false);
});

$("#connect").addEventListener("click", async () => {
  const res = await send("CONNECT_NOTION");
  if (res.ok) { msg($("#auth-msg"), `Connected to ${res.workspaceName}`, true); loadPages(); }
  else msg($("#auth-msg"), errorText(res.error), false);
});
$("#disconnect").addEventListener("click", async () => { await send("DISCONNECT_NOTION"); msg($("#auth-msg"), "Disconnected", true); });
$("#save-tok").addEventListener("click", async () => {
  const res = await send("SET_INTERNAL_TOKEN", { token: $("#tok").value });
  if (res.ok) { $("#tok").value = ""; msg($("#auth-msg"), "Token saved and verified", true); loadPages(); }
  else msg($("#auth-msg"), res.error.message || errorText(res.error), false);
});

async function loadPages(selected) {
  const res = await send("LIST_PAGES");
  if (!res.ok) return msg($("#parent-msg"), errorText(res.error), false);
  const sel = $("#parent");
  const keep = selected ?? sel.value;
  sel.length = 1;
  for (const p of res.pages) sel.add(new Option(`${p.icon ? p.icon + " " : ""}${p.title}`, p.id, false, p.id === keep));
  msg($("#parent-msg"), `${res.pages.length} page(s) shared with the integration`, true);
}
$("#load-pages").addEventListener("click", () => loadPages());
$("#save-parent").addEventListener("click", async () => {
  const sel = $("#parent");
  const { settings = {} } = await chrome.storage.local.get("settings");
  await chrome.storage.local.set({ settings: { ...settings, parentId: sel.value, parentTitle: sel.selectedOptions[0]?.text || "" } });
  msg($("#parent-msg"), sel.value ? "Saved — new notes go under that page" : "Saved — using the default destination", true);
});

chrome.storage.onChanged.addListener((c, area) => { if (area === "local" && c.auth) renderAuth(c.auth.newValue); });
load();
