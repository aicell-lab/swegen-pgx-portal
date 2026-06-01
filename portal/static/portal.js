// Shared portal frontend logic — Hypha login, API client, page helpers.

const HYPHA_SDK = "https://cdn.jsdelivr.net/npm/hypha-rpc@0.20.69/dist/hypha-rpc-websocket.min.js";

let _cfg = null;
let _hyphaRPC = null;

async function loadHypha() {
  if (_hyphaRPC) return _hyphaRPC;
  await new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[src="${HYPHA_SDK}"]`);
    if (existing) { existing.addEventListener("load", resolve); existing.addEventListener("error", reject); return; }
    const s = document.createElement("script");
    s.src = HYPHA_SDK;
    s.onload = resolve;
    s.onerror = reject;
    document.head.appendChild(s);
  });
  _hyphaRPC = window.hyphaWebsocketClient || window.hypha;
  if (!_hyphaRPC) throw new Error("hypha-rpc SDK failed to load");
  return _hyphaRPC;
}

async function getConfig() {
  if (_cfg) return _cfg;
  const r = await fetch("/api/config");
  _cfg = await r.json();
  return _cfg;
}

function getToken() {
  return localStorage.getItem("portal_hypha_token");
}

function setToken(t) {
  if (t) localStorage.setItem("portal_hypha_token", t);
  else localStorage.removeItem("portal_hypha_token");
}

async function hyphaLogin() {
  const cfg = await getConfig();
  const rpc = await loadHypha();
  let popup = null;
  const token = await rpc.login({
    server_url: cfg.hypha_server_url,
    login_callback: async (context) => {
      const w = 520, h = 700;
      const left = Math.max(0, Math.round((window.screen.width - w) / 2));
      const top = Math.max(0, Math.round((window.screen.height - h) / 2));
      popup = window.open(
        context.login_url,
        "hypha-login",
        `width=${w},height=${h},left=${left},top=${top}`,
      );
      if (!popup) {
        toast("Popup blocked — allow popups for this site and try again.", true);
        throw new Error("Popup blocked by browser");
      }
      popup.focus();
    },
  });
  try { if (popup && !popup.closed) popup.close(); } catch (_) {}
  if (!token) throw new Error("Login was cancelled or failed.");
  setToken(token);
  return token;
}

function logout() {
  setToken(null);
  window.location.href = "/";
}

async function api(path, opts = {}) {
  const token = getToken();
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401) {
    setToken(null);
    if (window.location.pathname !== "/") {
      window.location.href = "/?reason=session_expired";
      throw new Error("session expired");
    }
  }
  let body;
  try { body = await r.json(); }
  catch { body = { detail: await r.text() }; }
  if (!r.ok) {
    const e = new Error(body.detail || `HTTP ${r.status}`);
    e.status = r.status;
    e.body = body;
    throw e;
  }
  return body;
}

async function getMe() {
  const token = getToken();
  if (!token) return null;
  try { return await api("/api/me"); }
  catch (e) { if (e.status === 401) return null; throw e; }
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toISOString().replace("T", " ").substring(0, 19) + "Z";
}

function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (v != null) e.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}

function copyToClipboard(text) {
  navigator.clipboard.writeText(text).then(
    () => toast("Copied to clipboard"),
    () => toast("Copy failed", true),
  );
}

function toast(msg, error = false) {
  const t = el("div", {
    class: "toast" + (error ? " toast-error" : ""),
    style: "position:fixed;bottom:24px;right:24px;background:" + (error ? "#b91c1c" : "#0f766e") +
           ";color:white;padding:10px 16px;border-radius:6px;font-size:14px;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,0.2);transition:opacity .3s",
  }, msg);
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 350); }, 2200);
}

window.PortalAPI = {
  api, getMe, getConfig, hyphaLogin, logout, getToken, setToken,
  fmtTime, el, copyToClipboard, toast,
};
