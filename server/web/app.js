/* Ai-Voice-Agent — the unified application. Phase 24; redesigned in the UI/UX phase.
 *
 * One page, a hash router, and three backends on the same origin:
 *   /dashboard/api/...     the read side (Phase 10/20): dashboard, calls, call detail, search, me, login
 *   /automation/api/v1/... every write (Phase 17/18/19): prospects, imports, campaigns, actions, dnc, status, audit
 *   /api/app/...           Phase 24/25: session, config, health, knowledge base, engine, progress, stream
 * No business logic lives here: the pages ask, render, and confirm. Every figure
 * shown comes from those routes; nothing is computed that the server does not
 * already say. The vocabulary is fixed: a *contact* (the API says prospect), a
 * *campaign*, a *call*, the *AI agent*, the *knowledge base*.
 */
(() => {
  "use strict";

  const DASH = "/dashboard/api";
  const API = "/automation/api/v1";
  const APP = "/api/app";
  const LOGIN = "/dashboard/login";
  const LOGOUT = "/dashboard/logout";
  const REGISTER = `${APP}/register`;

  // `live`: a campaign is running, so the dashboard, the calls list and the
  // campaign page refresh themselves (see the timer at the end). `tabs`: the
  // tab each campaign page was on, so a refresh does not bounce to Contacts.
  // `registration`: whether the Register page is open (GET /api/app/register),
  // read once; `loginNotice`: a line the login shows once, after a sign-up.
  const state = { principal: null, config: null, route: null, live: false, tabs: {}, engine: null, stream: null, progress: {}, registration: undefined, loginNotice: null };
  const REFRESH_MS = 15000;

  // ---------------------------------------------------------------- helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—");
  const fmtDay = (iso) => (iso ? new Date(iso).toLocaleDateString([], { dateStyle: "medium" }) : "—");
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { timeStyle: "short" }) : "—");
  const fmtDur = (s) => (s == null ? "—" : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);
  const fmtNum = (n) => (n == null || n === "" ? "—" : typeof n === "number" ? n.toLocaleString() : String(n));
  const pct = (n, d) => (d ? Math.round((n / d) * 100) : 0);
  const human = (v) => String(v ?? "—").replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
  const plural = (n, one, many = `${one}s`) => `${fmtNum(n)} ${n === 1 ? one : many}`;
  const can = (perm) => !!(state.principal && state.principal.permissions.includes(perm));
  const icon = (name, cls = "ic") => `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const initials = (name) => String(name || "?").split(/[\s._-]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
  const ago = (iso) => {
    if (!iso) return "";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return fmtDay(iso);
  };

  class ApiError extends Error {
    constructor(status, body) {
      const message = typeof body === "string" ? body : body?.error?.message || body?.error || body?.detail || `HTTP ${status}`;
      super(typeof message === "string" ? message : JSON.stringify(message));
      this.status = status;
      this.body = body;
    }
  }

  async function request(method, url, body, opts = {}) {
    const headers = { "X-Requested-With": "fetch", Accept: "application/json" };
    let payload = body;
    if (body != null && !(body instanceof FormData) && !opts.raw) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    if (opts.contentType) headers["Content-Type"] = opts.contentType;
    let res;
    try {
      res = await fetch(url, { method, headers, body: payload, credentials: "same-origin" });
    } catch (e) {
      throw new ApiError(0, "The server could not be reached.");
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (res.status === 401) {
      state.principal = null;
      renderShell();
      throw new ApiError(401, data || "sign in first");
    }
    if (!res.ok) throw new ApiError(res.status, data);
    return data;
  }
  const api = {
    get: (u) => request("GET", u),
    post: (u, b, o) => request("POST", u, b, o),
    put: (u, b) => request("PUT", u, b),
    del: (u) => request("DELETE", u),
  };

  // A person never reads a status code. `friendly` turns any error into a
  // sentence, and keeps the server's own words as the technical detail.
  function friendly(e, doing = "") {
    const status = e?.status;
    const raw = e instanceof Error ? e.message : String(e);
    const code = e?.body?.error?.code || e?.body?.code || "";
    const verb = doing ? `Unable to ${doing}.` : "Something went wrong.";
    let message;
    if (status === 0) message = `${verb} The server could not be reached — check that the application is running and try again.`;
    else if (status === 403 && /csrf/i.test(code + raw)) message = `${verb} Your session needs to be refreshed — reload the page and try again.`;
    else if (status === 403) message = `${verb} Your role does not have permission for this.`;
    else if (status === 404) message = `${verb} It no longer exists or was never created.`;
    else if (status === 409) message = `${verb} ${raw.replace(/^cannot/i, "It cannot").replace(/campaign '([^']+)'/i, "the campaign")}`;
    else if (status === 422 || status === 400) message = `${verb} Some of the details are not valid: ${raw}`;
    else if (status === 429) message = `${verb} Too many requests in a short time — wait a moment and try again.`;
    else if (status === 503) message = `${verb} A service the application depends on is not available right now — try again shortly.`;
    else if (status >= 500) message = `${verb} The server reported an error. Try again; if it keeps happening, check the application logs.`;
    else message = doing ? `${verb} ${raw}` : raw;
    return { message, detail: status ? `HTTP ${status}${code ? ` · ${code}` : ""} · ${raw}` : raw };
  }

  // ------------------------------------------------------------------ motion
  // One motion system: pages fade up on entry, counters count up once, the
  // sidebar's indicator slides to the active item, progress bars morph from
  // their previous width. All of it is skipped when the person asked for
  // reduced motion, and none of it runs on the quiet 15 s refresh.
  const reducedMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function countUp(root) {
    if (reducedMotion()) return;
    $$("[data-count]", root).forEach((el) => {
      const target = Number(el.dataset.count);
      if (!Number.isFinite(target) || target === 0 || el.dataset.counted) return;
      el.dataset.counted = "1";
      const start = performance.now(), dur = 650;
      const step = (t) => { const p = Math.min(1, (t - start) / dur); const e = 1 - Math.pow(1 - p, 3); el.textContent = Math.round(target * e).toLocaleString(); if (p < 1) requestAnimationFrame(step); };
      requestAnimationFrame(step);
    });
  }
  function animateIn(view) {
    view.classList.remove("page-enter");
    void view.offsetWidth;
    view.classList.add("page-enter");
    countUp(view);
  }
  function moveNavIndicator() {
    const ind = $("#nav-indicator");
    if (!ind) return;
    const a = $("#nav a.active");
    if (!a) { ind.style.opacity = "0"; return; }
    ind.style.opacity = "1";
    ind.style.height = `${a.offsetHeight}px`;
    ind.style.transform = `translateY(${a.offsetTop}px)`;
  }
  window.addEventListener("resize", moveNavIndicator);
  // Replace a box's HTML while every progress bar inside it animates from
  // its previous width to the new one instead of refilling from zero.
  function morphHtml(box, html) {
    const before = $$(".progress i", box).map((i) => i.style.width);
    box.innerHTML = html;
    if (!before.length || reducedMotion()) return;
    const bars = $$(".progress i", box);
    const targets = bars.map((i) => i.style.width);
    bars.forEach((i, k) => { i.style.transition = "none"; i.style.width = before[k] ?? "0%"; });
    void box.offsetWidth;
    bars.forEach((i, k) => { i.style.transition = ""; i.style.width = targets[k]; });
  }

  // Theme: the system's choice unless the person toggled one; remembered per browser.
  const themeKey = "aiva.theme";
  function applyTheme(t) {
    const root = document.documentElement;
    if (t === "dark" || t === "light") root.dataset.theme = t; else delete root.dataset.theme;
    const dark = t === "dark" || (t !== "light" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    const btn = $("#theme-toggle");
    if (btn) { btn.innerHTML = icon(dark ? "sun" : "moon"); btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme"); btn.dataset.tip = dark ? "Light theme" : "Dark theme"; }
    // The embedded voice client (the bot's own page) follows: it listens for this message.
    $$("iframe.frame").forEach((f) => { try { f.contentWindow.postMessage({ type: "aiva:theme", theme: dark ? "dark" : "light" }, "*"); } catch { /* not loaded yet */ } });
    try { if (t) localStorage.setItem(themeKey, t); else localStorage.removeItem(themeKey); } catch { /* private mode */ }
  }
  const currentTheme = () => (document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light");
  let savedTheme = null;
  try { savedTheme = localStorage.getItem(themeKey); } catch { /* private mode */ }
  applyTheme(savedTheme);
  $("#theme-toggle").onclick = () => { const dark = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && window.matchMedia("(prefers-color-scheme: dark)").matches); applyTheme(dark ? "light" : "dark"); };

  // ------------------------------------------------------------- components
  function toast(message, kind = "") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.setAttribute("role", kind === "bad" ? "alert" : "status");
    el.innerHTML = `${icon(kind === "good" ? "check" : kind === "bad" ? "error" : kind === "warn" ? "alert" : "info")}<span></span>`;
    $("span", el).textContent = message;
    $("#toasts").appendChild(el);
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 260); }, kind === "bad" ? 6500 : 4200);
  }
  const ok = (m) => toast(m, "good");
  const fail = (e, doing = "") => toast(friendly(e, doing).message, "bad");

  let lastFocus = null;
  function modal(html, { wide = false, narrow = false, title = "", closable = true } = {}) {
    const root = $("#modal-root");
    lastFocus = document.activeElement;
    root.innerHTML = `<div class="modal-back"><div class="modal card ${wide ? "wide" : ""} ${narrow ? "narrow" : ""}" role="dialog" aria-modal="true" ${title ? `aria-label="${esc(title)}"` : ""}>${html}</div></div>`;
    const back = $(".modal-back", root);
    if (closable) back.addEventListener("click", (e) => { if (e.target === back) closeModal(); });
    const closeBtn = $(".close", root);
    if (closeBtn) closeBtn.onclick = closeModal;
    root.onkeydown = (e) => {
      if (e.key === "Escape" && closable) { e.preventDefault(); closeModal(); }
      if (e.key === "Tab") {
        const f = $$('a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])', root).filter((el) => el.offsetParent !== null);
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    setTimeout(() => { const f = $('[autofocus], input:not([type=hidden]), select, textarea, button.primary, button', root); if (f) f.focus(); }, 0);
    return root;
  }
  function closeModal() {
    const root = $("#modal-root");
    const back = $(".modal-back", root);
    root.onkeydown = null;
    if (back && !reducedMotion()) {
      // Fade out, then remove only this dialog — a new one may open meanwhile.
      back.classList.add("closing");
      back.style.pointerEvents = "none";
      setTimeout(() => { if (back.parentNode === root) back.remove(); }, 150);
    } else root.innerHTML = "";
    if (lastFocus && document.contains(lastFocus)) { try { lastFocus.focus(); } catch {} }
    lastFocus = null;
  }
  const modalHead = (title, sub = "") => `<div class="card-head"><div><h2 id="modal-title">${esc(title)}</h2>${sub ? `<div class="sub">${sub}</div>` : ""}</div><button class="icon-btn close" type="button" aria-label="Close dialog">${icon("close")}</button></div>`;

  // A confirmation says what will happen — never "are you sure?".
  function confirm(title, body, { danger = false, label = "Confirm", cancel = "Cancel" } = {}) {
    return new Promise((resolve) => {
      const root = modal(`${modalHead(title)}
        <div class="card-body"><div class="lead">${body}</div>
          <div class="form-actions">
            <button class="btn" type="button" data-x="no">${esc(cancel)}</button>
            <button class="btn ${danger ? "danger solid" : "primary"}" type="button" data-x="yes" autofocus>${esc(label)}</button>
          </div></div>`, { narrow: true, title });
      const done = (v) => { closeModal(); resolve(v); };
      $('[data-x="no"]', root).onclick = () => done(false);
      $('[data-x="yes"]', root).onclick = () => done(true);
      root.addEventListener("click", (e) => { if (e.target.classList.contains("modal-back") || e.target.closest(".close")) resolve(false); }, { once: true });
    });
  }

  // One meaning per status, everywhere: the same word and the same colour on
  // the dashboard, in a table, on a campaign page and in a call's detail.
  const STATUS = {
    // campaigns
    DRAFT: ["Draft", "neutral"], ACTIVE: ["Running", "good"], RUNNING: ["Running", "good"], PAUSED: ["Paused", "warn"],
    COMPLETED: ["Completed", "info"], CANCELLED: ["Cancelled", "neutral"], FAILED: ["Failed", "bad"],
    // call attempts
    PENDING: ["Pending", "neutral"], QUEUED: ["Queued", "neutral"], RESERVED: ["Queued", "neutral"], CALLING: ["Calling", "info"],
    CONNECTED: ["Connected", "good"], IN_PROGRESS: ["In progress", "info"], UNRESOLVED: ["Unresolved", "bad"],
    NO_ANSWER: ["No answer", "warn"], BUSY: ["Busy", "warn"], VOICEMAIL: ["Voicemail", "warn"],
    // dispositions and results
    NOT_INTERESTED: ["Not interested", "bad"], DO_NOT_CALL: ["Do not call", "bad"], DO_NOT_CONTACT: ["Do not contact", "bad"], OPTED_OUT: ["Opted out", "bad"],
    CALLBACK_REQUESTED: ["Callback requested", "warn"], CALLBACK_SCHEDULED: ["Callback scheduled", "warn"], SCHEDULED: ["Scheduled", "warn"], REQUESTED: ["Requested", "warn"],
    MEETING_BOOKED: ["Meeting booked", "good"], MEETING_REQUESTED: ["Meeting requested", "info"], MEETING_AGREED: ["Meeting agreed", "info"], BOOKED: ["Booked", "good"], AGREED: ["Agreed", "info"], PROPOSED: ["Proposed", "neutral"], DECLINED: ["Declined", "bad"],
    TRANSFERRED: ["Transferred", "good"], HUMAN_FOLLOW_UP: ["Human follow-up", "warn"], SEND_INFORMATION: ["Send information", "info"],
    QUALIFIED: ["Qualified", "good"], PARTIALLY_QUALIFIED: ["Partly qualified", "warn"], DISQUALIFIED: ["Not qualified", "bad"], UNQUALIFIED: ["Not qualified", "bad"],
    INTERESTED: ["Interested", "good"], CURIOUS: ["Curious", "info"], NEUTRAL: ["Neutral", "neutral"], RELUCTANT: ["Reluctant", "warn"],
    UNKNOWN: ["Unknown", "neutral"], NONE: ["None", "neutral"],
    // contacts and memberships
    NEW: ["New", "neutral"], CONTACTED: ["Contacted", "info"], UNREACHABLE: ["Unreachable", "warn"], EXHAUSTED: ["Attempts exhausted", "warn"], SKIPPED: ["Skipped", "neutral"],
    PLACED: ["Placed", "info"], ANSWERED: ["Answered", "good"],
    // health and misc
    ok: ["OK", "good"], failed: ["Failed", "bad"], degraded: ["Degraded", "warn"], skipped: ["Skipped", "neutral"],
    yes: ["Yes", "good"], no: ["No", "neutral"], configured: ["Configured", "good"], "not configured": ["Not configured", "warn"], indexed: ["Indexed", "good"], processing: ["Processing", "info"],
  };
  const statusOf = (s) => STATUS[String(s ?? "")] || STATUS[String(s ?? "").toUpperCase()] || [human(s), "neutral"];
  const statusLabel = (s) => statusOf(s)[0];
  const statusTone = (s) => statusOf(s)[1];
  const tone = (t) => ({ good: "good", bad: "bad", warn: "warn", info: "info", accent: "info" }[t] || "");
  function badge(text, kind, extra = "") {
    if (text == null || text === "") return `<span class="muted">—</span>`;
    const [label, t] = statusOf(text);
    return `<span class="badge ${kind ? tone(kind) || kind : t} ${extra}">${esc(label)}</span>`;
  }
  const liveBadge = (text) => badge(text, "", "live");

  // A KPI card: label, a value that counts up on first render, its context,
  // and an icon chosen by what the label says. Tone colours the value.
  const STAT_ICONS = [[/contact/i, "contacts"], [/campaign/i, "campaigns"], [/meeting/i, "calendar"], [/qualif/i, "star"], [/fail|problem|error/i, "error"], [/answer/i, "phone-out"], [/call/i, "calls"], [/queue|remain|pending|scheduled/i, "clock"], [/valid|new|import/i, "check"], [/invalid|skip/i, "ban"], [/known|duplicate/i, "inbox"]];
  function stat(label, value, detail = "", t = "", extra = "") {
    const raw = value ?? "—";
    const n = typeof raw === "number" ? raw : typeof raw === "string" && /^[\d,]+$/.test(raw) ? Number(raw.replace(/,/g, "")) : null;
    const ic = (STAT_ICONS.find(([re]) => re.test(label)) || [null, "activity"])[1];
    return `<div class="card stat ${tone(t)} ${extra}"><span class="stat-icon" aria-hidden="true">${icon(ic)}</span><div class="label">${esc(label)}</div><div class="value" ${n != null && n > 0 ? `data-count="${n}"` : ""}>${esc(raw)}</div>${detail ? `<div class="detail">${esc(detail)}</div>` : ""}</div>`;
  }
  // A donut of shares (real rows only), with a legend. Colours are the status tones.
  function donut(rows, { centre = "", label = "" } = {}) {
    const total = rows.reduce((n, r) => n + r[1], 0);
    if (!total) return "";
    const r = 42, c = 2 * Math.PI * r;
    let offset = 0;
    const arcs = rows.map(([, n, , t]) => { const len = (n / total) * c; const s = `<circle class="${t || "neutral"}" cx="50" cy="50" r="${r}" stroke-dasharray="${len.toFixed(2)} ${(c - len).toFixed(2)}" stroke-dashoffset="${(-offset).toFixed(2)}"></circle>`; offset += len; return s; }).join("");
    return `<div class="donut-wrap"><div class="donut" role="img" aria-label="${esc(label)}"><svg viewBox="0 0 100 100"><circle class="track" cx="50" cy="50" r="${r}"></circle>${arcs}</svg><div class="center"><b>${esc(centre || fmtNum(total))}</b><span>${esc(label)}</span></div></div>
      <div class="donut-legend">${rows.map(([name, n, share, t]) => `<div class="row"><span class="dot ${t || ""}"></span><span class="truncate" title="${esc(name)}">${esc(name)}</span><b>${fmtNum(n)}</b><span class="pct">${esc(share)}%</span></div>`).join("")}</div></div>`;
  }
  const metricStat = (m, label) => (m ? stat(label || m.label, m.available === false ? "n/a" : m.value, m.available === false ? "not available" : m.detail, m.tone) : "");
  const metric = (list, key) => (list || []).find((m) => m.key === key);

  function table(columns, rows, { empty = "Nothing here yet.", emptyHtml = "", rowAttr, cls = "" } = {}) {
    if (!rows || !rows.length) return emptyHtml || emptyState("inbox", empty);
    const head = columns.map((c) => `<th${c.num ? ' class="num"' : c.actions ? ' class="actions"' : ""}${c.sort ? ` data-sort="${c.sort}"` : ""}>${esc(c.label)}</th>`).join("");
    const body = rows.map((r) => `<tr ${rowAttr ? rowAttr(r) : ""}>${columns.map((c) => `<td${c.num ? ' class="num"' : c.actions ? ' class="actions"' : c.primary ? ' class="primary-cell"' : ""}>${c.render ? c.render(r) : esc(r[c.key] ?? "—")}</td>`).join("")}</tr>`).join("");
    return `<div class="tbl-wrap"><table class="tbl ${cls}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }
  const emptyState = (ic, title, text = "", action = "", small = false) => `<div class="empty ${small ? "sm" : ""}"><span class="empty-icon" aria-hidden="true">${icon(ic)}</span><h3>${esc(title)}</h3>${text ? `<p>${text}</p>` : ""}${action}</div>`;
  const loading = (text = "Loading…") => `<div class="loading" role="status"><span class="spinner"></span> ${esc(text)}</div>`;
  const skelLines = (n = 3) => Array.from({ length: n }, (_, i) => `<div class="skeleton sk-line ${["w80", "w60", "w40", "w25"][i % 4]}"></div>`).join("");
  const skelStats = (n = 5) => `<div class="grid stats">${Array.from({ length: n }, () => `<div class="card stat"><div class="skeleton sk-line w40"></div><div class="skeleton sk-kpi"></div><div class="skeleton sk-line w60"></div></div>`).join("")}</div>`;
  const skelTable = (n = 5) => `<div class="card">${Array.from({ length: n }, () => `<div class="sk-row">${Array.from({ length: 5 }, () => `<div class="skeleton sk-line"></div>`).join("")}</div>`).join("")}</div>`;
  const skelPage = (stats = true) => `<div class="page-head"><div class="titles"><div class="skeleton sk-title"></div><div class="skeleton sk-line w40" style="width:200px"></div></div></div>${stats ? skelStats() : ""}${skelTable()}`;
  const errorBox = (e, { retry = false, back = "", doing = "" } = {}) => {
    const f = friendly(e, doing);
    return `<div class="error-box" role="alert"><h3>${esc(doing ? `Unable to ${doing}` : "Something went wrong")}</h3><p>${esc(f.message.replace(/^Unable to [^.]+\.\s*/, ""))}</p>
      <div class="actions">${retry ? `<button class="btn" type="button" data-retry>${icon("refresh")} Retry</button>` : ""}${back ? `<a class="btn ghost" href="${back}">${icon("back")} Go back</a>` : ""}</div>
      <details class="tech"><summary>Technical details</summary><pre>${esc(f.detail)}</pre></details></div>`;
  };
  const alert = (kind, body, ic) => `<div class="alert ${kind}">${icon(ic || { good: "check", warn: "alert", bad: "error" }[kind] || "info")}<div class="body">${body}</div></div>`;
  const head = (title, sub, actions = "", { eyebrow = "" } = {}) => `<div class="page-head"><div class="titles">${eyebrow ? `<div class="eyebrow">${esc(eyebrow)}</div>` : ""}<h1>${title}</h1>${sub ? `<p>${sub}</p>` : ""}</div>${actions ? `<div class="actions">${actions}</div>` : ""}</div>`;
  // The dashboard's header: a gradient hero with the greeting and the primary actions.
  const hero = (title, sub, actions = "", eyebrow = "") => `<div class="hero"><span class="orb" aria-hidden="true"></span><span class="orb two" aria-hidden="true"></span><div class="titles">${eyebrow ? `<div class="eyebrow">${esc(eyebrow)}</div>` : ""}<h1>${title}</h1>${sub ? `<p>${sub}</p>` : ""}</div>${actions ? `<div class="actions">${actions}</div>` : ""}</div>`;
  const card = (title, body, actions = "", { sub = "", flush = false, id = "" } = {}) => `<div class="card" ${id ? `id="${id}"` : ""}>${title || actions ? `<div class="card-head"><div><h2>${esc(title)}</h2>${sub ? `<div class="sub">${sub}</div>` : ""}</div>${actions ? `<div class="actions">${actions}</div>` : ""}</div>` : ""}<div class="card-body ${flush ? "flush" : ""}">${body}</div></div>`;
  const field = (label, input, hint = "", { required = false } = {}) => `<label class="field"><span class="lbl ${required ? "req" : ""}">${esc(label)}</span>${input}${hint ? `<div class="hint">${hint}</div>` : ""}</label>`;
  const inp = (name, value = "", attrs = "") => `<input name="${name}" value="${esc(value)}" ${attrs}>`;
  const area = (name, value = "", attrs = "") => `<textarea name="${name}" ${attrs}>${esc(Array.isArray(value) ? value.join("\n") : value)}</textarea>`;
  const select = (name, options, value = "", attrs = "") => `<select name="${name}" ${attrs}>${options.map(([v, label]) => `<option value="${esc(v)}" ${String(v) === String(value ?? "") ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>`;
  const lines = (text) => String(text || "").split("\n").map((s) => s.trim()).filter(Boolean);
  const formData = (form) => Object.fromEntries(new FormData(form).entries());
  const kv = (pairs) => `<dl class="kv">${pairs.filter((p) => p).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v == null || v === "" ? '<span class="muted">—</span>' : v}</dd>`).join("")}</dl>`;
  const facts = (items) => `<div class="facts">${items.filter((f) => f).map(([k, v]) => `<div class="fact"><div class="k">${esc(k)}</div><div class="v">${v == null || v === "" ? '<span class="muted">—</span>' : v}</div></div>`).join("")}</div>`;
  const progressBar = (segments, { lg = false, title = "" } = {}) => `<div class="progress ${lg ? "lg" : ""}" role="progressbar" ${title ? `title="${esc(title)}" aria-label="${esc(title)}"` : ""}>${segments.filter((s) => s[0] > 0).map(([w, t]) => `<i class="${t}" style="width:${Math.max(0, Math.min(100, w))}%"></i>`).join("")}</div>`;
  const bars = (rows) => rows.length ? `<div class="bars">${rows.map(([label, n, share, t]) => `<div class="bar-row"><span class="truncate" title="${esc(label)}">${esc(label)}</span><div class="track"><i class="${t || ""}" style="width:${Math.max(1, Math.min(100, share))}%"></i></div><span class="n">${fmtNum(n)} · ${share}%</span></div>`).join("")}</div>` : "";

  async function withButton(btn, fn, label = "") {
    if (!btn) return fn();
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.innerHTML = `<span class="spinner"></span> ${label || original}`;
    try { return await fn(); } finally { if (document.contains(btn)) { btn.disabled = false; btn.removeAttribute("aria-busy"); btn.innerHTML = original; } }
  }

  // Sorting a rendered table on the client: the page in view, not the database.
  function sortable(root, rows, draw) {
    let key = null, dir = 1;
    $$("th[data-sort]", root).forEach((th) => {
      th.classList.add("sortable");
      th.setAttribute("role", "button"); th.tabIndex = 0;
      const go = () => { if (key === th.dataset.sort) dir = -dir; else { key = th.dataset.sort; dir = 1; } draw([...rows].sort((a, b) => { const x = a[key] ?? "", y = b[key] ?? ""; return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * dir; }), key, dir); };
      th.onclick = go; th.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } };
      if (key === th.dataset.sort) th.classList.add("sorted", dir < 0 ? "desc" : "");
    });
  }

  // ------------------------------------------------------------------ shell
  function renderShell() {
    const foot = $("#sidebar-foot");
    const right = $("#topbar-right");
    $("#app").classList.toggle("signed-out", !state.principal);
    if (!state.principal) {
      foot.innerHTML = "";
      right.innerHTML = "";
      renderSignedOut();
      return;
    }
    const p = state.principal;
    foot.innerHTML = `<div class="user-card"><span class="avatar" aria-hidden="true">${esc(initials(p.name))}</span><div class="who"><b title="${esc(p.name)}">${esc(p.name)}</b><span>${esc(p.role)}</span></div><button class="icon-btn" id="logout" type="button" aria-label="Sign out" data-tip="Sign out">${icon("logout")}</button></div>`;
    setTimeout(moveNavIndicator, 0);
    right.innerHTML = `<span id="engine-slot">${enginePill(state.engine)}</span>`;
    api.get(`${APP}/engine`).then(renderEngine).catch(() => {});
    $("#logout").onclick = async () => {
      try { await fetch(LOGOUT, { method: "POST", credentials: "same-origin", headers: { "X-Requested-With": "fetch" } }); } catch {}
      state.principal = null;
      toast("Signed out");
      renderShell();
    };
    route();
  }

  // Signed out, the page is the login — or, at #/register, the sign-up.
  function renderSignedOut() {
    return location.hash === "#/register" ? renderRegister() : renderLogin();
  }

  // Whether sign-up is open, and its rules; asked once, before a login.
  async function loadRegistration() {
    if (state.registration !== undefined) return state.registration;
    try { state.registration = await api.get(REGISTER); } catch { state.registration = null; }
    return state.registration;
  }

  // Per-field problems under a form's inputs (the server's `fields` map, or
  // the page's own checks), in the design system's `.field.invalid` + `.err`.
  function showFieldErrors(form, fields) {
    $$(".field.invalid", form).forEach((el) => { el.classList.remove("invalid"); $$(".err", el).forEach((x) => x.remove()); });
    let first = null;
    for (const [name, message] of Object.entries(fields || {})) {
      const input = $(`[name="${name}"]`, form);
      const wrap = input?.closest(".field");
      if (!wrap) continue;
      wrap.classList.add("invalid");
      const err = document.createElement("div");
      err.className = "err";
      err.textContent = message;
      wrap.appendChild(err);
      first = first || input;
    }
    if (first) first.focus();
    return !!first;
  }

  // The signed-out shell: a brand panel (what the product does — nothing
  // invented) beside the form. Under 1100 px the panel folds away and the
  // card carries the brand itself.
  const authShell = (card) => `<div class="auth">
      <aside class="auth-brand" aria-hidden="true">
        <span class="orb a"></span><span class="orb b"></span><span class="orb c"></span><span class="grid-lines"></span>
        <div class="brand"><span class="brand-mark">${icon("mic", "")}</span><div><div class="brand-name">Ai-Voice-Agent</div><div class="brand-sub">AI sales calling</div></div></div>
        <div>
          <h2>An AI agent that makes the calls, <em>so your team takes the meetings.</em></h2>
          <p class="lede">Import your contacts, launch a campaign, and the agent phones each contact, qualifies them from a real conversation and books the meeting.</p>
          <ul class="features">
            <li>${icon("phone-out")}<span><b>Calls every contact in a campaign</b> within its calling hours, retrying the unanswered and stopping when asked.</span></li>
            <li>${icon("sparkle")}<span><b>Qualifies and books meetings</b> — pain points, objections, decision role and the next step, recorded for every call.</span></li>
            <li>${icon("knowledge")}<span><b>Answers from your knowledge base</b>, so every reply is grounded in your own documents.</span></li>
          </ul>
          <div class="call-motif"><span class="wave"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></span><div class="txt"><b>Live conversation</b><span>Listens, answers and hands over in real time</span></div></div>
        </div>
        <div class="foot">Outbound campaigns · Live transcripts · Calendar and CRM integrations</div>
      </aside>
      <div class="auth-main"><div class="auth-card">${card}</div></div>
    </div>`;
  const mobileBrand = () => `<div class="mobile-brand"><span class="brand-mark" aria-hidden="true">${icon("mic", "")}</span><div><div class="brand-name">Ai-Voice-Agent</div><div class="brand-sub">AI sales calling</div></div></div>`;
  // A password field with a show/hide control.
  const passwordField = (label, name, attrs, hint = "", opts = {}) => `<label class="field with-btn"><span class="lbl ${opts.required ? "req" : ""}">${esc(label)}</span><input name="${name}" type="password" ${attrs}><button class="field-btn" type="button" data-reveal aria-label="Show password" data-tip="Show">${icon("eye")}</button>${hint ? `<div class="hint">${hint}</div>` : ""}</label>`;
  const bindReveal = (root) => $$("[data-reveal]", root).forEach((b) => { b.onclick = () => { const i = b.parentElement.querySelector("input"); const show = i.type === "password"; i.type = show ? "text" : "password"; b.innerHTML = icon(show ? "eye-off" : "eye"); b.setAttribute("aria-label", show ? "Hide password" : "Show password"); b.dataset.tip = show ? "Hide" : "Show"; i.focus(); }; });

  function renderLogin(error = "") {
    const notice = state.loginNotice;
    state.loginNotice = null;
    $("#crumbs").innerHTML = "<b>Sign in</b>";
    $$("#nav a").forEach((a) => { a.classList.remove("active"); a.removeAttribute("aria-current"); });
    $("#view").innerHTML = authShell(`${mobileBrand()}
      <div class="eyebrow">Welcome back</div><h1>Sign in to your workspace</h1><p>Your campaigns, calls and the AI agent, in one place.</p>
      ${error ? alert("bad", esc(error)) : ""}
      ${notice ? alert(notice.kind || "good", esc(notice.text)) : ""}
      <form id="login-form" novalidate>
        ${field("Username", inp("username", notice?.username || "", 'autocomplete="username" required autofocus placeholder="Your name or email"'))}
        ${passwordField("Password", "password", 'autocomplete="current-password" required placeholder="••••••••"')}
        <div class="form-actions"><button class="btn primary lg" type="submit" style="width:100%">Sign in ${icon("arrow")}</button></div>
      </form>
      <p class="foot" id="login-foot" hidden>Don't have an account? <a href="#/register">Create one</a></p>`);
    bindReveal($("#view"));
    loadRegistration().then((r) => { const foot = $("#login-foot"); if (foot && r?.enabled) foot.hidden = false; });
    $("#login-form").onsubmit = async (e) => {
      e.preventDefault();
      const btn = $("button", e.target);
      const f = formData(e.target);
      if (!f.username || !f.password) return renderLogin("Enter your username and password.");
      await withButton(btn, async () => {
        let res;
        try {
          const body = new URLSearchParams({ ...f, next: "/app/" });
          res = await fetch(LOGIN, { method: "POST", body, credentials: "same-origin", redirect: "manual" });
        } catch { return renderLogin("The server could not be reached. Check that the application is running."); }
        // A 303 (opaque under redirect: manual) means signed in; a 200 is the page again, with the error.
        if (res.type === "opaqueredirect" || res.status === 303 || res.status === 0) {
          await loadSession();
          if (state.principal) { renderShell(); return; }
        }
        if (res.status === 403 && res.headers.get("x-aiva-login") === "pending") return renderLogin("Your account is awaiting an administrator's approval. You will be able to sign in once it is approved.");
        renderLogin(res.status === 429 ? "Too many attempts. Wait a minute and try again." : "That username or password is not right.");
      }, "Signing in…");
    };
  }

  // The Register page (Phase 27): name, email, password, the password again.
  // The rules are the server's (`validate_registration`); the page repeats
  // them so a slip is pointed out before a request, and shows the server's
  // own `fields` map or its 409 when the server disagrees.
  function renderRegister() {
    $("#crumbs").innerHTML = "<b>Create account</b>";
    $$("#nav a").forEach((a) => { a.classList.remove("active"); a.removeAttribute("aria-current"); });
    const view = $("#view");
    view.innerHTML = authShell(`${mobileBrand()}
      <div class="eyebrow">Get set up</div><h1>Create your account</h1><p>Register to see your campaigns and calls.</p>
      <div id="register-alert"></div>
      <form id="register-form" novalidate>
        ${field("Name", inp("name", "", 'autocomplete="username" required autofocus maxlength="64"'), "Your sign-in name: letters, digits, dots, underscores or dashes.", { required: true })}
        ${field("Email", inp("email", "", 'type="email" autocomplete="email" required maxlength="254" inputmode="email"'), "", { required: true })}
        ${field("Role", `<select name="role"><option value="viewer" selected>Viewer — see campaigns and results</option><option value="admin">Admin — full control (needs approval)</option></select>`, "Operator and Admin accounts need an administrator's approval before they can sign in.", { required: true })}
        ${passwordField("Password", "password", 'autocomplete="new-password" required', "At least 8 characters.", { required: true })}
        ${passwordField("Confirm password", "confirm_password", 'autocomplete="new-password" required', "", { required: true })}
        <div class="form-actions"><button class="btn primary lg" type="submit" style="width:100%">Create account ${icon("arrow")}</button></div>
      </form>
      <p class="foot">Already have an account? <a href="#/dashboard">Sign in</a></p>`);
    $(".auth-card", view).classList.add("wide");
    bindReveal(view);
    loadRegistration().then((r) => {
      const form = $("#register-form");
      if (!form) return;
      if (r && r.enabled === false) {
        $("#register-alert").innerHTML = alert("warn", "Sign-up is closed on this deployment. Ask an administrator for an account.");
        $$("input, button", form).forEach((el) => { el.disabled = true; });
        return;
      }
      const min = r?.rules?.password_min_length;
      if (min && min !== 8) $$(".hint", form)[2].textContent = `At least ${min} characters.`;
    });
    $("#register-form").onsubmit = async (e) => {
      e.preventDefault();
      const form = e.target;
      const btn = $("button", form);
      const f = formData(form);
      const name = (f.name || "").trim();
      const email = (f.email || "").trim();
      const min = state.registration?.rules?.password_min_length || 8;
      const problems = {};
      if (!name) problems.name = "Enter a name.";
      else if (!/^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$/.test(name)) problems.name = "Use letters, digits, dots, underscores, @ or dashes (up to 64 characters), starting with a letter or digit.";
      if (!email) problems.email = "Enter an email address.";
      else if (!/^[^\s@]{1,64}@[^\s@]+\.[^\s@]{2,}$/.test(email)) problems.email = "That does not look like an email address.";
      const role = f.role || "viewer";
      if (!["viewer", "admin"].includes(role)) problems.role = "Choose viewer, admin.";
      if (!f.password) problems.password = "Enter a password.";
      else if (f.password.length < min) problems.password = `The password needs at least ${min} characters.`;
      if (!f.confirm_password) problems.confirm_password = "Enter the password again.";
      else if (f.password && f.confirm_password !== f.password) problems.confirm_password = "The two passwords do not match.";
      $("#register-alert").innerHTML = "";
      if (showFieldErrors(form, problems)) return;
      await withButton(btn, async () => {
        try {
          const made = await api.post(REGISTER, { name, email, role, password: f.password, confirm_password: f.confirm_password });
          state.loginNotice = made.status === "pending"
            ? { kind: "info", text: `Your request for ${made.role === "admin" ? "an Admin" : "an Operator"} account was sent, ${made.name}. An administrator has to approve it before you can sign in.`, username: made.name }
            : { text: `Your account is ready, ${made.name}. Sign in to continue.`, username: made.name };
          history.replaceState(null, "", "#/dashboard");
          renderLogin();
          toast(made.status === "pending" ? "Request sent for approval" : "Account created", "good");
        } catch (err) {
          const body = err?.body || {};
          if (err.status === 422 && body.fields) { showFieldErrors(form, body.fields); return; }
          if (err.status === 409 && body.field) { showFieldErrors(form, { [body.field]: `${String(body.error).replace(/^a user/, "A user")}.` }); return; }
          const message = err.status === 429 ? "Too many attempts. Wait a minute and try again."
            : err.status === 403 ? (typeof body.error === "string" ? body.error.replace(/^\w/, (c) => c.toUpperCase()) + "." : "Sign-up is not allowed from here.")
            : err.status === 503 ? "The application cannot reach its database right now. Try again shortly."
            : friendly(err, "create your account").message;
          $("#register-alert").innerHTML = alert("bad", esc(message));
        }
      }, "Creating…");
    };
  }

  async function loadSession() {
    try {
      state.principal = await request("GET", `${APP}/session`);
      if (!state.config) state.config = await api.get(`${APP}/config`).catch(() => null);
    } catch (e) {
      if (e.status !== 401) fail(e, "load your session");
      state.principal = null;
    }
  }

  // ----------------------------------------------------------------- router
  const routes = [
    [/^#\/dashboard$/, "dashboard", pageDashboard, ["Dashboard"]],
    [/^#\/campaigns$/, "campaigns", pageCampaigns, ["Campaigns"]],
    [/^#\/campaigns\/new$/, "campaigns", pageCreateCampaign, [["Campaigns", "#/campaigns"], "New campaign"]],
    [/^#\/campaigns\/(\d+)$/, "campaigns", pageCampaign, [["Campaigns", "#/campaigns"], "Campaign"]],
    [/^#\/contacts$/, "contacts", pageContacts, ["Contacts"]],
    [/^#\/contacts\/import$/, "contacts", pageImport, [["Contacts", "#/contacts"], "Import contacts"]],
    [/^#\/calls$/, "calls", pageCalls, ["Calls"]],
    [/^#\/calls\/(\d+)$/, "calls", pageCall, [["Calls", "#/calls"], "Call details"]],
    [/^#\/live$/, "live", pageLive, ["Live AI Agent"]],
    [/^#\/knowledge$/, "knowledge", pageKnowledge, ["Knowledge Base"]],
    [/^#\/analytics$/, "analytics", pageAnalytics, ["Analytics"]],
    [/^#\/settings$/, "settings", pageSettings, ["Settings"]],
  ];
  const setCrumbs = (crumbs) => { $("#crumbs").innerHTML = crumbs.map((c, i) => { const [label, href] = Array.isArray(c) ? c : [c, null]; return i === crumbs.length - 1 ? `<b>${esc(label)}</b>` : `${href ? `<a href="${href}">${esc(label)}</a>` : esc(label)}<span class="sep" aria-hidden="true">/</span>`; }).join(""); document.title = `${crumbs.map((c) => (Array.isArray(c) ? c[0] : c)).slice(-1)[0]} · Ai-Voice-Agent`; };

  // Phase 25: server-sent events from /api/app/stream. One subscription per
  // view; the router closes it when the view changes. Falls back to the
  // 15 s poll when EventSource is unavailable or the stream errors.
  function subscribe(url, handlers) {
    unsubscribe();
    if (typeof EventSource === "undefined") return null;
    let source;
    try { source = new EventSource(url); } catch { return null; }
    for (const [name, fn] of Object.entries(handlers)) source.addEventListener(name, (e) => { try { fn(JSON.parse(e.data)); } catch (err) { console.warn("stream event", name, err); } });
    source.addEventListener("bye", () => { source.close(); if (state.stream === source) { state.stream = null; subscribe(url, handlers); } });
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false); /* the browser reconnects; the poll covers the gap */
    state.stream = source;
    return source;
  }
  function unsubscribe() { if (state.stream) { try { state.stream.close(); } catch {} state.stream = null; } setLive(false); }
  function setLive(on) { $$("[data-live-pill]").forEach((el) => { el.innerHTML = on ? `<span class="dot good"></span> Live` : `<span class="dot"></span> Updating every 15 s`; }); }
  const livePill = () => `<span class="pill-live" data-live-pill><span class="dot"></span> Updating every 15 s</span>`;

  // The engine is the scheduler inside this process: the thing that dials.
  const ENGINE = {
    running: ["good", "Dialling", (e) => (e.in_flight?.length ? `${plural(e.in_flight.length, "call")} in progress` : "Ready to place calls")],
    idle: ["warn", "Not dialling", (e) => e.reason || "No outbound carrier is configured"],
    off: ["neutral", "Dialling disabled", (e) => e.reason || "Calls are placed by a separate scheduler process"],
    starting: ["info", "Starting", () => "The dialler is starting"],
    stopping: ["warn", "Stopping", () => "Finishing calls in progress"],
    stopped: ["neutral", "Stopped", () => "The dialler has stopped"],
    failed: ["bad", "Dialler failed", (e) => e.reason || "Restart the application"],
  };
  function enginePill(engine) {
    if (!engine) return "";
    const [t, word, why] = ENGINE[engine.state] || ["neutral", human(engine.state), () => ""];
    return `<span class="engine-pill ${esc(engine.state)}" title="${esc(why(engine))}"><span class="dot ${t}"></span>${esc(word)}${engine.state === "running" && engine.in_flight?.length ? ` · ${engine.in_flight.length}` : ""}</span>`;
  }
  function renderEngine(engine) {
    state.engine = engine;
    const slot = $("#engine-slot");
    if (slot) slot.innerHTML = enginePill(engine);
  }

  async function route(quiet = false) {
    if (!quiet) unsubscribe();
    if (!state.principal) return renderSignedOut();
    const hash = location.hash || "#/dashboard";
    // Filters and pagers live in the hash's query string (#/calls?campaign=3);
    // the route is the part before it.
    const path = hash.split("?")[0];
    const match = routes.map((r) => [r, path.match(r[0])]).find(([, m]) => m);
    if (!match) { location.hash = "#/dashboard"; return; }
    const [[, nav, page, crumbs], m] = match;
    $$("#nav a").forEach((a) => { const on = a.dataset.route === nav; a.classList.toggle("active", on); if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    setCrumbs(crumbs);
    closeSidebar();
    const view = $("#view");
    if (!quiet) { view.innerHTML = skelPage(); window.scrollTo({ top: 0 }); }
    const token = (state.route = {});
    moveNavIndicator();
    try {
      await page(view, m.slice(1), token, quiet);
      if (state.route === token && !quiet) animateIn(view);
    } catch (e) {
      if (state.route !== token) return;
      if (e.status === 401) return;
      const doing = { dashboard: "load the dashboard", campaigns: "load the campaigns", contacts: "load the contacts", calls: "load the calls", live: "load the live agent", knowledge: "load the knowledge base", analytics: "load the analytics", settings: "load the settings" }[nav] || "load this page";
      view.innerHTML = head(crumbs.slice(-1)[0]) + errorBox(e, { retry: true, back: e.status === 404 ? `#/${nav}` : "", doing });
      $("[data-retry]", view).onclick = () => route();
    }
  }
  window.addEventListener("hashchange", () => route());
  // Real-time enough for a calling floor: while a campaign is running, the
  // dashboard, the calls list and a campaign page (on its Contacts or Calls
  // tab) re-read the rows every REFRESH_MS — never over an open dialog, an
  // edit form or a hidden tab. Nothing is pushed from the server; the rows are
  // written by the scheduler and the bot, and a poll is the honest source.
  setInterval(() => {
    if (!state.principal || !state.live || document.hidden || $("#modal-root").children.length || $("#view button[disabled]")) return;
    const path = location.hash.split("?")[0] || "#/dashboard";
    const campaign = path.match(/^#\/campaigns\/(\d+)$/);
    if (campaign && !["contacts", "calls", undefined].includes(state.tabs[campaign[1]])) return;
    if (campaign || /^#\/(dashboard|calls)$/.test(path)) route(true);
  }, REFRESH_MS);
  const openSidebar = () => { $("#sidebar").classList.add("open"); $("#sidebar-backdrop").classList.add("show"); $("#menu-toggle").setAttribute("aria-expanded", "true"); };
  function closeSidebar() { $("#sidebar").classList.remove("open"); $("#sidebar-backdrop").classList.remove("show"); $("#menu-toggle").setAttribute("aria-expanded", "false"); }
  $("#menu-toggle").onclick = () => ($("#sidebar").classList.contains("open") ? closeSidebar() : openSidebar());
  $("#sidebar-backdrop").onclick = closeSidebar;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#sidebar").classList.contains("open")) closeSidebar(); });

  // Collapsible sidebar (desktop): icons-only rail, remembered per browser.
  const railKey = "aiva.rail";
  const setRail = (on) => {
    $("#app").classList.toggle("rail", on);
    const btn = $("#rail-toggle");
    if (btn) { btn.setAttribute("aria-expanded", String(!on)); btn.setAttribute("aria-label", on ? "Expand sidebar" : "Collapse sidebar"); }
    try { localStorage.setItem(railKey, on ? "1" : "0"); } catch { /* private mode */ }
  };
  try { if (localStorage.getItem(railKey) === "1") setRail(true); } catch { /* private mode */ }
  const railBtn = $("#rail-toggle");
  if (railBtn) railBtn.onclick = () => { setRail(!$("#app").classList.contains("rail")); setTimeout(moveNavIndicator, 240); };

  // ------------------------------------------------------------ dashboard
  // The dashboard answers: what is happening, how many contacts, which
  // campaigns run, how many calls, how well they went, anything failing.
  function activityOf(r) {
    const d = String(r.disposition || r.status || "").toUpperCase();
    const who = `<b>${esc(r.prospect)}</b>${r.company ? ` <span class="muted">· ${esc(r.company)}</span>` : ""}`;
    const map = {
      MEETING_BOOKED: ["good", "calendar", `Meeting booked with ${who}`],
      QUALIFIED: ["good", "star", `Lead qualified: ${who}`],
      TRANSFERRED: ["good", "calls", `Call transferred to a person: ${who}`],
      CALLBACK_REQUESTED: ["warn", "clock", `Callback scheduled with ${who}`],
      COMPLETED: ["info", "check", `Call completed with ${who}`],
      NOT_INTERESTED: ["bad", "ban", `${who} was not interested`],
      DO_NOT_CALL: ["bad", "ban", `${who} asked not to be called`],
      NO_ANSWER: ["warn", "calls", `No answer from ${who}`],
      BUSY: ["warn", "calls", `${who} was busy`],
      VOICEMAIL: ["warn", "calls", `Voicemail reached for ${who}`],
      FAILED: ["bad", "error", `Call to ${who} failed`],
      CONNECTED: ["good", "calls", `On a call with ${who}`],
      CALLING: ["info", "calls", `Calling ${who}`],
    };
    const [t, ic, text] = map[d] || ["neutral", "calls", `${statusLabel(d)}: ${who}`];
    const meta = [r.campaign, r.qualification_label && r.qualification_label !== "Unknown" ? r.qualification_label : "", r.meeting_status && r.meeting_status !== "NONE" && r.meeting_status !== "UNKNOWN" ? statusLabel(r.meeting_status) : "", r.duration].filter(Boolean).join(" · ");
    return `<a class="feed-item" href="#/calls/${r.attempt_id}" style="color:inherit;text-decoration:none"><span class="mark ${t}">${icon(ic)}</span><div class="what">${text}<span class="meta">${esc(meta)}</span></div><span class="when">${esc(r.at_label || "")}</span></a>`;
  }

  async function pageDashboard(view, _m, _token, quietRender = false) {
    const [snap, campaigns, status, engine] = await Promise.all([
      api.get(`${DASH}/dashboard`),
      api.get(`${API}/campaigns?limit=200`),
      api.get(`${API}/status`).catch(() => null),
      api.get(`${APP}/engine`).catch(() => null),
    ]);
    if (engine) renderEngine(engine);
    const rows = campaigns.campaigns || [];
    state.live = rows.some((c) => c.status === "ACTIVE");
    const byId = Object.fromEntries((snap.campaigns || []).map((c) => [String(c.id), c]));
    const running = rows.filter((c) => c.status === "ACTIVE" || c.status === "PAUSED");
    const active = rows.filter((c) => c.status === "ACTIVE").length;
    const totals = snap.totals || [];
    const contacts = metric(totals, "contacts");
    const completed = metric(totals, "completed");
    const answered = metric(totals, "answered");
    const failed = metric(totals, "failed");
    const calls = metric(totals, "calls");
    const qualified = metric(totals, "qualified")?.value ?? (snap.campaigns || []).reduce((n, c) => n + (c.qualified || 0), 0);
    const meetings = metric(totals, "meetings")?.value ?? (snap.campaigns || []).reduce((n, c) => n + (c.meetings || 0), 0);
    const scheduler = status?.scheduler?.workers;
    // Phase 25: the engine inside this process is the normal way calls are
    // placed; a separate `campaign.py run` shows up in the fleet count.
    let banner = "";
    if (engine?.state === "failed") banner = alert("bad", `<b>The dialler has stopped working.</b> ${esc(engine.reason || "")} Restart the application to resume calling.`);
    else if (engine?.state === "idle" && active) banner = alert("warn", `<b>${plural(active, "campaign is", "campaigns are")} running but nothing is being dialled.</b> ${esc(engine.reason || "No outbound carrier is configured.")} <div class="actions"><a class="btn sm" href="#/settings">Check calling settings</a></div>`);
    else if (engine?.state !== "running" && scheduler && scheduler.alive === 0 && active) banner = alert("warn", `<b>${plural(active, "campaign is", "campaigns are")} running but no dialler is active.</b> Calls are placed once the application's dialler or a separate scheduler is running. <div class="actions"><a class="btn sm" href="#/settings">Open settings</a></div>`);
    const attention = (snap.attention || []).filter((m) => m.tone === "bad" || m.tone === "warn");
    if (attention.length) banner += alert("warn", `<b>Needs attention:</b> ${attention.map((m) => `${esc(m.label)} ${esc(m.value)} <span class="muted">(${esc(m.detail)})</span>`).join(" · ")}`);
    if (!quietRender) subscribe(`${APP}/stream`, { engine: renderEngine, campaign: () => route(true) });

    const noData = !rows.length && !(contacts?.value > 0);
    const conv = snap.conversion || [];
    const perf = snap.performance || [];
    const rateTile = (m, label, t) => (m ? `<div class="rate ${m.available === false || m.value == null ? "" : t}"><div class="v">${esc(m.available === false || m.value == null ? "—" : m.value)}</div><div class="k">${esc(label)}</div><div class="d" title="${esc(m.detail || "")}">${esc(m.detail || "")}</div></div>` : "");
    const outcomeRows = (snap.outcomes || []).slice(0, 8).map((o) => [statusLabel(o.key), o.count, o.share, tone(o.tone) || "neutral"]);

    const hour = new Date().getHours();
    const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
    const who = state.principal?.name && state.principal.name !== "anonymous" ? `, ${esc(state.principal.name)}` : "";
    const ENGINE_WORD = { running: "The dialler is placing calls.", idle: "The dialler is idle.", off: "Dialling is handled by a separate scheduler.", failed: "The dialler needs attention.", starting: "The dialler is starting.", stopping: "The dialler is stopping.", stopped: "The dialler has stopped." };
    view.innerHTML = hero(`${greeting}${who}`, `${active ? `<b>${plural(active, "campaign is", "campaigns are")} running.</b> ` : rows.length ? "No campaign is running right now. " : "Import contacts and create your first campaign to begin. "}${esc(engine ? ENGINE_WORD[engine.state] || "" : "")} <span style="white-space:nowrap">${livePill()}</span>`,
      can("write") ? `<a class="btn" href="#/contacts/import">${icon("upload")} Import contacts</a><a class="btn primary" href="#/campaigns/new">${icon("plus")} New campaign</a>` : "", `Dashboard · ${snap.filters?.label || "All campaigns"}`) +
      banner +
      (noData ? card("", emptyState("sparkle", "Welcome. Let's make your first calls.", "Import your contacts, create a campaign, and the AI agent will call each contact, qualify them and book meetings.", can("write") ? `<div class="btn-group" style="margin-top:14px"><a class="btn primary" href="#/contacts/import">${icon("upload")} Import contacts</a><a class="btn" href="#/campaigns/new">Create a campaign</a></div>` : "")) : "") +
      `<div class="grid stats">
        ${stat("Total contacts", fmtNum(contacts?.value ?? 0), contacts?.detail || "")}
        ${stat("Active campaigns", active, running.length > active ? `${running.length - active} paused` : rows.length ? `${rows.length} in total` : "", active ? "good" : "")}
        ${stat("Calls completed", fmtNum(completed?.value ?? 0), calls ? `${fmtNum(calls.value)} placed · ${fmtNum(answered?.value ?? 0)} answered` : "")}
        ${stat("Qualified leads", fmtNum(qualified), "from completed calls", qualified ? "good" : "")}
        ${stat("Meetings booked", fmtNum(meetings), "by the AI agent", meetings ? "good" : "")}
        ${failed && failed.value ? stat("Failed calls", fmtNum(failed.value), failed.detail, "bad") : ""}
      </div>
      <div class="section-title">Active campaigns</div>
      ${running.length ? `<div class="grid cards">${running.map((c) => campaignCard(c, byId[c.id])).join("")}</div>` : card("", emptyState("campaigns", rows.length ? "No campaign is running" : "No campaigns yet", rows.length ? "Start a draft campaign to begin calling, or create a new one." : "Create your first campaign to start reaching your contacts.", can("write") ? `<a class="btn primary" href="${rows.length ? "#/campaigns" : "#/campaigns/new"}">${rows.length ? "View campaigns" : "Create campaign"}</a>` : "", true))}
      <div class="section-title">Results</div>
      <div class="grid two">
        ${card("Call outcomes", outcomeRows.length ? donut(outcomeRows, { label: "finished calls" }) : emptyState("analytics", "No outcomes yet", "Outcomes appear once calls are completed.", "", true), "", { sub: snap.outcomes?.length ? `${fmtNum(snap.outcomes.reduce((n, o) => n + o.count, 0))} finished calls` : "" })}
        ${card("Conversion", conv.length || perf.length ? `<div class="rates">${rateTile(metric(perf, "answer_rate"), "Answer rate", "")}${rateTile(metric(conv, "qualification_rate"), "Qualification rate", "good")}${rateTile(metric(conv, "meeting_rate"), "Meeting rate", "good")}${rateTile(metric(perf, "average_duration"), "Avg. call length", "")}</div>` : emptyState("analytics", "No results yet", "Rates appear after the first answered call.", "", true), `<a class="btn sm ghost" href="#/analytics">Analytics</a>`)}
      </div>
      <div class="section-title">Recent activity</div>
      ${card("", (snap.recent_calls || []).length ? `<div class="feed">${snap.recent_calls.slice(0, 12).map(activityOf).join("")}</div>` : emptyState("calls", "No calls yet", "Calls will appear here once a campaign is running.", "", true), (snap.recent_calls || []).length ? `<a class="btn sm ghost" href="#/calls">All calls</a>` : "", { flush: true })}
      ${snap.notes?.length ? `<p class="muted small" style="margin-top:12px">${snap.notes.map(esc).join(" · ")}</p>` : ""}`;
    $$("[data-action]", view).forEach((b) => (b.onclick = () => runAction(b, () => route(true))));
  }

  // ------------------------------------------------------------ campaigns
  // Only the actions valid for the state are shown; the API refuses the rest anyway.
  const ACTIONS = {
    DRAFT: [["start", "Start", "primary", "play"]],
    ACTIVE: [["pause", "Pause", "", "pause"], ["complete", "Stop", "danger", "stop"]],
    PAUSED: [["resume", "Resume", "primary", "play"], ["complete", "Stop", "danger", "stop"]],
    COMPLETED: [],
    CANCELLED: [],
  };
  // Stopping (complete / cancel) needs the manage permission — an admin — as the API has required since Phase 18.
  const actionButtons = (c, size = "sm") => (can("write") ? (ACTIONS[c.status] || []).filter(([a]) => (a === "complete" || a === "cancel" ? can("manage") : true)).map(([a, label, cls, ic]) => `<button class="btn ${size} ${cls}" type="button" data-action="${a}" data-id="${c.id}" data-name="${esc(c.name)}" data-pending="${c.counts?.pending ?? ""}" data-total="${c.counts?.total ?? ""}">${icon(ic)} ${label}</button>`).join(" ") : "");

  // A campaign as a card: status, progress, the four figures that matter, the valid actions.
  function campaignCard(c, snap) {
    const done = c.counts?.completed ?? snap?.completed_members ?? 0, total = c.counts?.total ?? snap?.prospects ?? 0;
    const t = statusTone(c.status);
    const live = c.status === "ACTIVE";
    return `<div class="card campaign-card hover tone-${t} ${live ? "is-live" : ""}">
      <div class="cc-head"><div class="name"><a href="#/campaigns/${c.id}">${esc(c.name)}</a>${c.description ? `<div class="desc" title="${esc(c.description)}">${esc(c.description)}</div>` : ""}</div>${live ? liveBadge("RUNNING") : badge(c.status)}</div>
      <div class="cc-progress"><div class="row-flex"><span class="muted">${fmtNum(done)} of ${fmtNum(total)} contacts</span><b>${pct(done, total)}%</b></div>${progressBar([[pct(done, total), c.status === "COMPLETED" ? "info" : live ? "" : t === "warn" ? "warn" : "neutral"]], { title: `${done} of ${total} contacts processed` })}</div>
      <div class="cc-stats"><div><b>${fmtNum(total)}</b><span>Contacts</span></div><div class="${c.counts?.in_progress ? "info" : ""}"><b>${fmtNum(c.counts?.in_progress ?? 0)}</b><span>Calling</span></div><div class="${snap?.qualified ? "good" : ""}"><b>${fmtNum(snap?.qualified ?? 0)}</b><span>Qualified</span></div><div class="${snap?.meetings ? "good" : ""}"><b>${fmtNum(snap?.meetings ?? 0)}</b><span>Meetings</span></div></div>
      <div class="cc-foot"><span class="when">Created ${esc(fmtDay(c.created_at))}</span>${actionButtons(c)}<a class="btn sm ghost" href="#/campaigns/${c.id}">Open ${icon("chevron")}</a></div>
    </div>`;
  }

  async function runAction(btn, after) {
    const { action, id, name, pending, total } = btn.dataset;
    const n = `<b>${esc(name)}</b>`;
    const texts = {
      start: [`Start ${esc(name)}?`, `The AI agent will begin calling every pending contact in ${n}${total !== "" ? ` (${fmtNum(Number(total))} contacts)` : ""}, within the campaign's calling hours and limits.${Number(total) === 0 ? " <br><br><b>This campaign has no contacts yet</b> — add some first, or it will complete immediately." : ""}`, "Start campaign", false],
      pause: [`Pause ${esc(name)}?`, `No new calls are placed until you resume. Calls already in progress finish normally.`, "Pause campaign", false],
      resume: [`Resume ${esc(name)}?`, `Calling continues from where it left off, with the remaining contacts.`, "Resume campaign", false],
      complete: [`Stop ${esc(name)}?`, `The campaign is marked completed and cannot be resumed.${pending !== "" && Number(pending) > 0 ? ` <b>${plural(Number(pending), "contact")} that ${Number(pending) === 1 ? "has" : "have"} not been reached will not be called.</b>` : ""} Calls already in progress finish normally. You can retry individual contacts later from a new campaign.`, "Stop campaign", true],
      cancel: [`Cancel ${esc(name)}?`, `The campaign is cancelled and its remaining contacts are not called.`, "Cancel campaign", true],
    };
    const [title, body, label, danger] = texts[action] || [`${human(action)} campaign`, `${human(action)} ${n}?`, human(action), false];
    if (!(await confirm(title, body, { danger, label }))) return;
    const said = { start: "started", pause: "paused", resume: "resumed", complete: "stopped", cancel: "cancelled" }[action] || action;
    await withButton(btn, async () => {
      try {
        const r = await api.post(`${API}/campaigns/${id}/${action}`);
        ok(`Campaign "${r.campaign.name}" ${said}.`);
        await after();
      } catch (e) { fail(e, `${action === "complete" ? "stop" : action} the campaign`); }
    }, { start: "Starting…", pause: "Pausing…", resume: "Resuming…", complete: "Stopping…", cancel: "Cancelling…" }[action]);
  }

  async function pageCampaigns(view) {
    const [data, status, snap] = await Promise.all([api.get(`${API}/campaigns?limit=200`), api.get(`${API}/status`).catch(() => null), api.get(`${DASH}/dashboard`).catch(() => null)]);
    state.live = (data.campaigns || []).some((c) => c.status === "ACTIVE");
    const all = data.campaigns || [];
    const byId = Object.fromEntries(((snap && snap.campaigns) || []).map((c) => [String(c.id), c]));
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const filter = params.get("status") || "";
    const rows = filter ? all.filter((c) => (filter === "ACTIVE" ? c.status === "ACTIVE" : c.status === filter)) : all;
    const alive = status?.scheduler?.workers?.alive ?? null;
    const counts = (s) => all.filter((c) => c.status === s).length;
    view.innerHTML = head("Campaigns", `${plural(all.length, "campaign")} · ${counts("ACTIVE")} running · ${counts("PAUSED")} paused · ${counts("DRAFT")} draft`, can("write") ? `<a class="btn primary" href="#/campaigns/new">${icon("plus")} New campaign</a>` : "", { eyebrow: "Outbound" }) +
      (alive === 0 && state.engine?.state !== "running" && counts("ACTIVE") ? alert("warn", `<b>No dialler is active.</b> Running campaigns are not placing calls until the application's dialler or a separate scheduler is up. <div class="actions"><a class="btn sm" href="#/settings">Open settings</a></div>`) : "") +
      `<div class="tabs" role="tablist">${[["", "All"], ["ACTIVE", "Running"], ["PAUSED", "Paused"], ["DRAFT", "Draft"], ["COMPLETED", "Completed"], ["CANCELLED", "Cancelled"]].map(([v, l]) => `<button role="tab" aria-selected="${v === filter}" class="${v === filter ? "active" : ""}" data-filter="${v}">${l}${v ? `<span class="count">${counts(v)}</span>` : ""}</button>`).join("")}</div>` +
      (rows.length ? `<div class="grid cards">${rows.map((c) => campaignCard(c, byId[c.id])).join("")}</div>` :
        card("", emptyState("campaigns", filter ? `No ${statusLabel(filter).toLowerCase()} campaigns` : "No campaigns yet", filter ? "Try another filter." : "Create your first campaign to start reaching your contacts.", can("write") && !filter ? `<a class="btn primary" href="#/campaigns/new">${icon("plus")} Create campaign</a>` : "")));
    $$("[data-filter]", view).forEach((b) => (b.onclick = () => { location.hash = b.dataset.filter ? `#/campaigns?status=${b.dataset.filter}` : "#/campaigns"; }));
    $$("[data-action]", view).forEach((b) => (b.onclick = () => runAction(b, () => route(true))));
  }

  // The campaign page is built for watching a campaign run: the state, the
  // controls valid for it, the progress, then the contacts and calls.
  async function pageCampaign(view, [id], _token, quietRender = false) {
    const data = await api.get(`${API}/campaigns/${id}`);
    const c = data.campaign;
    state.live = c.status === "ACTIVE";
    let cfg = c.configuration || {};
    let comp = cfg.compliance || {};
    const defaults = state.config?.sales || {};
    // After a save, re-read the campaign before re-showing the tab: the form
    // must show what the server holds, not what this page loaded with.
    const refresh = async (name) => {
      const fresh = (await api.get(`${API}/campaigns/${id}`)).campaign;
      cfg = fresh.configuration || {};
      comp = cfg.compliance || {};
      await show(name);
    };
    // Phase 25: the progress strip is read from /api/app/campaigns/{id}/progress
    // and kept current by the event stream; every figure is the rows' own.
    const progressPanel = (p) => {
      if (!p) return `<div class="grid stats compact">${stat("Contacts", fmtNum(c.counts?.total ?? 0), "", "", "compact")}${stat("Queued", fmtNum(c.counts?.pending ?? 0), "", "", "compact")}${stat("Calling now", fmtNum(c.counts?.in_progress ?? 0), "", "", "compact")}${stat("Processed", fmtNum(c.counts?.completed ?? 0), "", "", "compact")}</div>`;
      const calling = (p.reserved || 0) + (p.calling || 0) + (p.connected || 0);
      const unreached = (p.no_answer || 0) + (p.busy || 0) + (p.voicemail || 0);
      const total = p.contacts || 0;
      const done = p.done || 0;
      const segs = [[pct(p.members_completed || 0, total), "good"], [pct(p.exhausted || 0, total), "warn"], [pct(p.skipped || 0, total), "neutral"], [pct(p.in_progress || 0, total), "info"]];
      return `<div class="progress-title"><span class="pct">${esc(p.progress_pct ?? 0)}%</span><span class="muted">${fmtNum(done)} of ${fmtNum(total)} contacts processed · ${plural(p.attempts || 0, "call")} placed</span><span class="spacer"></span>${livePill()}</div>
      ${progressBar(segs, { lg: true, title: `${done} of ${total} contacts processed` })}
      <div class="legend"><span><span class="dot good"></span> Reached <b>${fmtNum(p.members_completed || 0)}</b></span><span><span class="dot warn"></span> Attempts exhausted <b>${fmtNum(p.exhausted || 0)}</b></span><span><span class="dot info"></span> Calling now <b>${fmtNum(p.in_progress || 0)}</b></span><span><span class="dot"></span> ${p.finished ? "Never reached" : "Remaining"} <b>${fmtNum(p.finished ? p.cancelled : p.remaining)}</b></span></div>
      <hr>
      <div class="grid stats compact">
        ${stat("Contacts", fmtNum(total), p.scheduled ? `${fmtNum(p.scheduled)} scheduled for later` : "", "", "compact")}
        ${stat("Calls placed", fmtNum(p.attempts || 0), `${fmtNum(p.answered || 0)} answered`, "", "compact")}
        ${stat("Calling now", fmtNum(calling), `${fmtNum(p.connected || 0)} connected`, calling ? "info" : "", "compact")}
        ${stat("Qualified", p.qualified ?? "—", "", p.qualified ? "good" : "", "compact")}
        ${stat("Meetings", p.meetings ?? "—", "", p.meetings ? "good" : "", "compact")}
        ${stat("No answer", fmtNum(unreached), `${fmtNum(p.no_answer || 0)} no answer · ${fmtNum(p.busy || 0)} busy · ${fmtNum(p.voicemail || 0)} voicemail`, unreached ? "warn" : "", "compact")}
        ${stat("Failed", fmtNum(p.failed || 0), p.unresolved ? `${fmtNum(p.unresolved)} unresolved` : "", p.failed ? "bad" : "", "compact")}
        ${stat("Remaining", fmtNum(p.remaining || 0), p.queued && !p.finished ? `${fmtNum(p.queued)} due now` : "", "", "compact")}
      </div>
      ${p.not_interested || p.do_not_call || p.callback_requested ? `<p class="muted small" style="margin-top:10px">${[p.not_interested ? `${fmtNum(p.not_interested)} not interested` : "", p.do_not_call ? `${fmtNum(p.do_not_call)} asked not to be called` : "", p.callback_requested ? `${fmtNum(p.callback_requested)} asked for a callback` : ""].filter(Boolean).join(" · ")}</p>` : ""}`;
    };
    const stateLine = { DRAFT: "Not started yet. Review the contacts and settings, then start the campaign.", ACTIVE: "Running — the AI agent is calling contacts within the calling hours.", PAUSED: `Paused${c.paused_at ? ` ${ago(c.paused_at)}` : ""}. No new calls until you resume.`, COMPLETED: `Completed${c.completed_at ? ` on ${fmtDate(c.completed_at)}` : ""}.`, CANCELLED: "Cancelled. No further calls are placed." }[c.status] || "";
    const heroTone = statusTone(c.status);
    const heroIcon = { ACTIVE: "phone-out", PAUSED: "pause", DRAFT: "file", COMPLETED: "check", CANCELLED: "ban" }[c.status] || "campaigns";
    view.innerHTML = `<div class="card campaign-hero ${c.status === "ACTIVE" ? "glow live" : ""}" style="margin-bottom:16px"><span class="big ${heroTone}" aria-hidden="true">${icon(heroIcon)}</span><div class="titles"><h1>${esc(c.name)} ${c.status === "ACTIVE" ? liveBadge("RUNNING") : badge(c.status, "", "lg")}</h1><p>${esc(stateLine)}${c.description ? `<br><span>${esc(c.description)}</span>` : ""}</p></div><div class="actions">${actionButtons(c, "")} <a class="btn ghost" href="#/calls?campaign=${c.id}">${icon("calls")} Call history</a></div></div>` +
      `<div class="card"><div class="card-body" id="progress">${progressPanel(state.progress[id] || null)}</div></div>
      <div class="tabs" id="tabs" role="tablist" style="margin-top:18px"><button role="tab" data-tab="contacts" class="active">Contacts<span class="count">${fmtNum(c.counts?.total ?? 0)}</span></button><button role="tab" data-tab="calls">Calls</button><button role="tab" data-tab="agent">AI agent</button><button role="tab" data-tab="calling">Calling settings</button></div>
      <div id="tab"></div>`;
    $$("[data-action]", view).forEach((b) => (b.onclick = () => runAction(b, () => route(true))));
    const renderProgress = (p) => {
      const before = state.progress[id];
      state.progress[id] = p;
      const box = $("#progress", view);
      if (box) { morphHtml(box, progressPanel(p)); if (before && before.progress_pct !== p.progress_pct) { box.classList.remove("flash"); void box.offsetWidth; box.classList.add("flash"); } }
      setLive(!!state.stream);
      if (before && before.status !== p.status) { route(true); return; }
      // Counters moved while a call ended: the visible list of contacts or
      // calls is stale, so re-read it — never an edit form.
      if (before && ["contacts", "calls"].includes(state.tabs[id]) && JSON.stringify([before.attempts, before.answered, before.failed, before.queued, before.in_progress]) !== JSON.stringify([p.attempts, p.answered, p.failed, p.queued, p.in_progress])) show(state.tabs[id]);
    };
    api.get(`${APP}/campaigns/${id}/progress`).then(renderProgress).catch(() => {});
    if (!quietRender) subscribe(`${APP}/stream?campaign=${id}`, { engine: renderEngine, campaign: (p) => { if (String(p.campaign_id) === String(id)) renderProgress(p); } });
    const tab = $("#tab", view);
    const show = async (name) => {
      state.tabs[id] = name;
      $$("#tabs button", view).forEach((b) => { const on = b.dataset.tab === name; b.classList.toggle("active", on); b.setAttribute("aria-selected", on); });
      tab.innerHTML = skelTable(4);
      try {
        if (name === "contacts") {
          const r = await api.get(`${API}/campaigns/${id}/prospects?limit=500`);
          const rows = r.members || [];
          const open = c.status !== "COMPLETED" && c.status !== "CANCELLED";
          tab.innerHTML = card("Contacts in this campaign", table([
            { label: "Name", primary: true, render: (m) => `<a href="#/contacts?open=${m.prospect.id}">${esc(m.prospect.full_name)}</a>${m.prospect.job_title ? `<div class="muted">${esc(m.prospect.job_title)}</div>` : ""}` },
            { label: "Company", render: (m) => esc(m.prospect.company || "—") },
            { label: "Phone", render: (m) => `<span class="nowrap">${esc(m.prospect.phone_normalized || m.prospect.phone || "—")}</span>` },
            { label: "In this campaign", render: (m) => badge(m.membership.status) },
            { label: "Attempts", num: true, render: (m) => fmtNum(m.membership.attempt_count ?? 0) },
            { label: "Next call", render: (m) => `<span class="nowrap">${esc(m.membership.status === "PENDING" ? (m.membership.next_attempt_at ? fmtDate(m.membership.next_attempt_at) : "When the line is free") : "—")}</span>` },
            // Phase 25: an explicit retry — the only way a contact the campaign is done with is dialled again.
            { label: "", actions: true, render: (m) => (can("write") && ["COMPLETED", "EXHAUSTED"].includes(m.membership.status) && m.prospect.status !== "DO_NOT_CALL" && open ? `<button class="btn sm" type="button" data-retry="${m.prospect.id}" data-name="${esc(m.prospect.full_name)}">${icon("refresh")} Retry</button>` : "") },
          ], rows, { emptyHtml: emptyState("contacts", "No contacts in this campaign", open && can("write") ? "Add contacts from your list or import a CSV file, then start the campaign." : "", open && can("write") ? `<div class="btn-group" style="margin-top:14px"><button class="btn primary" type="button" id="add-contacts">${icon("plus")} Add contacts</button><a class="btn" href="#/contacts/import">${icon("upload")} Import CSV</a></div>` : "", true) }),
            can("write") && open && rows.length ? `<button class="btn sm" type="button" id="add-contacts">${icon("plus")} Add contacts</button>` : "", { flush: true, sub: `${plural(rows.length, "contact")}` });
          const add = $("#add-contacts", tab);
          if (add) add.onclick = () => pickContacts(async (ids) => { if (!ids.length) return; try { await api.post(`${API}/campaigns/${id}/prospects`, { prospect_ids: ids }); ok(`${plural(ids.length, "contact")} added to the campaign.`); route(true); } catch (e) { fail(e, "add the contacts"); } }, [], rows.map((m) => m.prospect.id));
          $$("[data-retry]", tab).forEach((b) => (b.onclick = async () => {
            if (!(await confirm(`Call ${b.dataset.name} again?`, `<b>${esc(b.dataset.name)}</b> is queued for one more call, ahead of the other contacts and past the attempt limit. It is placed as soon as the campaign is running and a line is free.`, { label: "Queue the retry" }))) return;
            try { await api.post(`${API}/campaigns/${id}/prospects/${b.dataset.retry}/retry`); ok(`Retry scheduled for ${b.dataset.name}.`); show("contacts"); } catch (e) { fail(e, "schedule the retry"); }
          }));
        } else if (name === "agent") {
          tab.innerHTML = card("AI agent profile", `<p class="muted" style="margin-bottom:14px">What the agent says it is, who it represents and what it offers. Leave a field blank to use the deployment's default shown as the placeholder. A running campaign's next call uses what you save here.</p>` + agentForm(cfg, defaults));
          bindAgentForm(tab, id, () => refresh("agent"));
        } else if (name === "calling") {
          const t = state.config?.telephony || {};
          tab.innerHTML = card("Calling number", facts([["Provider", esc(t.provider || "—")], ["Caller ID", esc(t.from_number || "Not set")], ["Status", t.configured ? badge("configured") : badge("not configured")]]) + `<p class="muted small" style="margin-top:12px">The carrier and the number are set for the whole deployment and shared by every campaign.</p>`) +
            card("Limits and schedule", callingForm(cfg, comp));
          bindCallingForm(tab, id, () => refresh("calling"));
        } else if (name === "calls") {
          const r = await api.get(`${DASH}/calls?campaign=${id}&limit=50`);
          tab.innerHTML = card("Calls", callsTable(r.calls, { campaign: false }), (r.calls || []).length ? `<a class="btn sm ghost" href="#/calls?campaign=${id}">All calls</a>` : "", { flush: true, sub: `${fmtNum(r.count ?? (r.calls || []).length)} most recent` });
          bindCallRows(tab);
        }
      } catch (e) { tab.innerHTML = errorBox(e, { doing: `load the ${name === "agent" ? "AI agent profile" : name}` }); }
    };
    $$("#tabs button", view).forEach((b) => (b.onclick = () => show(b.dataset.tab)));
    show(state.tabs[id] || "contacts");
  }

  function agentForm(cfg, d) {
    return `<form id="agent-form">
      <div class="row">${field("Agent name", inp("agent_name", cfg.agent_name || "", `placeholder="${esc(d.agent_name || "")}"`), "How the agent introduces itself.")}${field("Company", inp("company_name", cfg.company_name || "", `placeholder="${esc(d.company_name || "")}"`), "The company the agent is calling on behalf of.")}</div>
      <div class="row">${field("Offer", area("offer", cfg.offer || "", `placeholder="${esc(d.offer || "")}"`), "What is being offered, in one or two sentences.")}${field("Meeting ask", inp("meeting_ask", cfg.meeting_ask || "", `placeholder="${esc(d.meeting_ask || "")}"`), "How the agent proposes a meeting.")}</div>
      <div class="row">${field("Value points", area("value_points", cfg.value_points || [], `placeholder="${esc((d.value_points || []).join("\n"))}"`), "One per line.")}${field("Qualification criteria", area("qualification_criteria", cfg.qualification_criteria || [], `placeholder="${esc((d.qualification_criteria || []).join("\n"))}"`), "One per line. A contact is qualified when these are met.")}</div>
      <div class="row">${field("Notes for the agent", area("notes", cfg.notes || []), "One per line. Anything else the agent should keep in mind.")}</div>
      ${can("write") ? `<div class="form-actions"><button class="btn primary" type="submit">Save agent profile</button></div>` : ""}</form>`;
  }
  const agentBody = (f) => ({ agent_name: f.agent_name || "", company_name: f.company_name || "", offer: f.offer || "", meeting_ask: f.meeting_ask || "", value_points: lines(f.value_points), qualification_criteria: lines(f.qualification_criteria), notes: lines(f.notes) });
  function bindAgentForm(root, id, after) {
    const form = $("#agent-form", root);
    if (!form) return;
    form.onsubmit = (e) => { e.preventDefault(); withButton($("button[type=submit]", form), async () => { try { await api.put(`${API}/campaigns/${id}/configuration`, agentBody(formData(form))); ok("Agent profile saved."); await after(); } catch (err) { fail(err, "save the agent profile"); } }, "Saving…"); };
  }
  function callingForm(cfg, comp) {
    const cal = state.config?.calling || {};
    return `<form id="calling-form">
      <div class="fieldset"><div class="fieldset-title">Pace and limits</div><div class="desc">How many calls run at once and how often a contact is retried.</div>
      <div class="row">${field("Simultaneous calls", inp("max_concurrent_calls", cfg.max_concurrent_calls ?? "", 'type="number" min="0" max="100" step="1" placeholder="Deployment limit"'), `Leave blank or 0 to use the deployment's limit${cal.describe ? ` (${esc(cal.describe)})` : ""}.`)}${field("Seconds between calls", inp("pacing_secs", cfg.pacing_secs ?? "", 'type="number" min="0" max="3600" step="1" placeholder="0"'), "Extra pause after each call ends. 0 = none.")}${field("Max attempts per contact", inp("max_attempts", comp.max_attempts ?? "", `type="number" min="1" max="20" placeholder="${esc(cal.max_attempts ?? "")}"`), "Unanswered contacts are retried up to this many times.")}${field("Retry after (minutes)", inp("retry_minutes", comp.retry_minutes ?? "", `type="number" min="0" placeholder="${esc(cal.retry_minutes ?? "")}"`), "Wait before calling an unanswered contact again.")}</div></div>
      <div class="fieldset"><div class="fieldset-title">Calling hours</div><div class="desc">Calls are only placed inside this window.</div>
      <div class="row">${field("Hours", inp("calling_hours", comp.calling_hours || "", 'placeholder="09:00-18:00"'), "Start and end, 24-hour clock.")}${field("Days", inp("calling_days", comp.calling_days || "", 'placeholder="mon-fri"'))}${field("Timezone", inp("timezone", comp.timezone || "", 'placeholder="Asia/Karachi"'), "The contacts' local timezone.")}</div>
      <div class="row"><label class="chk"><input type="checkbox" name="enforce_calling_hours" ${comp.enforce_calling_hours === false ? "" : "checked"}> Enforce calling hours</label><label class="chk"><input type="checkbox" name="ai_disclosure_required" ${comp.ai_disclosure_required ? "checked" : ""}> The agent must say it is an AI in its first sentence</label></div></div>
      ${can("write") ? `<div class="form-actions"><button class="btn primary" type="submit">Save calling settings</button></div>` : ""}</form>`;
  }
  function bindCallingForm(root, id, after) {
    const form = $("#calling-form", root);
    if (!form) return;
    form.onsubmit = (e) => { e.preventDefault(); withButton($("button[type=submit]", form), async () => { try {
      const f = formData(form);
      await api.put(`${API}/campaigns/${id}/configuration`, { pacing_secs: f.pacing_secs === "" ? 0 : Number(f.pacing_secs), max_concurrent_calls: f.max_concurrent_calls === "" ? 0 : Number(f.max_concurrent_calls) });
      const comp = {};
      if (f.max_attempts) comp.max_attempts = Number(f.max_attempts);
      if (f.retry_minutes) comp.retry_minutes = Number(f.retry_minutes);
      if (f.calling_hours) comp.calling_hours = f.calling_hours;
      if (f.calling_days) comp.calling_days = f.calling_days;
      if (f.timezone) comp.timezone = f.timezone;
      comp.enforce_calling_hours = form.enforce_calling_hours.checked;
      comp.ai_disclosure_required = form.ai_disclosure_required.checked;
      await api.put(`${API}/campaigns/${id}/compliance`, comp);
      ok("Calling settings saved."); await after();
    } catch (err) { fail(err, "save the calling settings"); } }, "Saving…"); };
  }

  // ------------------------------------------------------- create campaign
  // Details → Contacts → AI agent → Calling → Review & launch. Nothing is
  // written until the last step; a campaign is created as a draft and may be
  // started from the review in the same breath.
  async function pageCreateCampaign(view) {
    if (!can("write")) { view.innerHTML = head("New campaign") + alert("warn", `Your role (${esc(state.principal.role)}) can view campaigns but not create them.`); return; }
    const d = state.config?.sales || {};
    const t = state.config?.telephony || {};
    const cal = state.config?.calling || {};
    const draft = { name: "", description: "", contacts: [], csv: null, agent: null, calling: null };
    let step = 0;
    const steps = ["Details", "Contacts", "AI agent", "Calling", "Review & launch"];
    const nav = (back, next, nextLabel = "Continue") => `<div class="form-actions">${back ? `<button class="btn ghost left" type="button" id="back">${icon("back")} Back</button>` : `<a class="btn ghost left" href="#/campaigns">Cancel</a>`}${next ? `<button class="btn primary" type="${next === "submit" ? "submit" : "button"}" id="next">${nextLabel} ${icon("arrow")}</button>` : ""}</div>`;
    const render = () => {
      view.innerHTML = head("New campaign", "A campaign is created as a draft. Nothing is dialled until you start it.", "", { eyebrow: `Step ${step + 1} of ${steps.length}` }) +
        `<div class="stepper" aria-label="Steps">${steps.map((s, i) => `<div class="step ${i === step ? "active" : i < step ? "done clickable" : ""}" data-step="${i}" ${i === step ? 'aria-current="step"' : ""}><span class="n">${i < step ? icon("check") : i + 1}</span><span class="t">${s}</span></div>`).join("")}</div><div id="step"></div>`;
      $$(".step.clickable", view).forEach((s) => (s.onclick = () => { step = Number(s.dataset.step); render(); }));
      const box = $("#step", view);
      if (step === 0) {
        box.innerHTML = card("Campaign details", `<form id="f" novalidate><div class="row one">${field("Campaign name", inp("name", draft.name, 'required maxlength="200" autofocus placeholder="e.g. Q4 fleet outreach"'), "", { required: true })}</div><div class="row one">${field("Description", area("description", draft.description, 'placeholder="What this campaign is for (optional)"'))}</div>${nav(false, "submit")}</form>`);
        $("#f", box).onsubmit = (e) => { e.preventDefault(); const f = formData(e.target); if (!f.name.trim()) { $('[name="name"]', box).focus(); return toast("Give the campaign a name.", "warn"); } Object.assign(draft, f); step = 1; render(); };
      } else if (step === 1) {
        const n = draft.contacts.length + (draft.csv ? draft.csv.valid : 0);
        box.innerHTML = card("Contacts", `<p class="muted">Choose contacts already in your list, upload a CSV file, or both. Uploaded files are validated and previewed before anything is saved.</p>
          <div class="grid two" style="margin-top:14px">
            <div class="fieldset" style="margin:0"><div class="fieldset-title">From your contact list</div><div class="desc">${draft.contacts.length ? `<b>${plural(draft.contacts.length, "contact")}</b> selected` : "None selected yet"}</div><button class="btn" type="button" id="pick">${icon("contacts")} ${draft.contacts.length ? "Change selection" : "Choose contacts"}</button></div>
            <div class="fieldset" style="margin:0"><div class="fieldset-title">From a CSV file</div><div class="desc">${draft.csv ? `<b>${esc(draft.csv.name)}</b> · ${plural(draft.csv.valid, "valid row")}` : "No file uploaded yet"}</div><button class="btn" type="button" id="csv">${icon("upload")} ${draft.csv ? "Replace file" : "Upload CSV"}</button>${draft.csv ? ` <button class="btn ghost" type="button" id="csv-clear">Remove</button>` : ""}</div>
          </div>
          ${n ? alert("good", `<b>${plural(n, "contact")}</b> will be added to the campaign.`) : ""}
          ${nav(true, true)}`.replace(/<div class="form-actions">/, '<div class="form-actions" style="margin-top:18px">'));
        $("#pick", box).onclick = () => pickContacts((ids) => { draft.contacts = ids; render(); }, draft.contacts);
        $("#csv", box).onclick = () => csvWizard((result) => { draft.csv = result; render(); });
        const clear = $("#csv-clear", box); if (clear) clear.onclick = () => { draft.csv = null; render(); };
        $("#back", box).onclick = () => { step = 0; render(); };
        $("#next", box).onclick = () => { if (!draft.contacts.length && !draft.csv) return toast("Choose at least one contact or upload a CSV file.", "warn"); step = 2; render(); };
      } else if (step === 2) {
        box.innerHTML = card("AI agent", `<p class="muted" style="margin-bottom:14px">Who the agent is and what it offers on this campaign. Blank fields use the deployment's defaults shown as placeholders.</p>` + agentForm(draft.agent || {}, d) + nav(true, true));
        $("#agent-form .form-actions", box)?.remove();
        $("#back", box).onclick = () => { draft.agent = agentBody(formData($("#agent-form", box))); step = 1; render(); };
        $("#next", box).onclick = () => { draft.agent = agentBody(formData($("#agent-form", box))); step = 3; render(); };
      } else if (step === 3) {
        box.innerHTML = card("Calling number", facts([["Provider", esc(t.provider || "—")], ["Caller ID", esc(t.from_number || "Not set")], ["Status", t.configured ? badge("configured") : badge("not configured")]]) + (t.configured ? "" : alert("warn", `<b>No outbound carrier is configured</b>, so the campaign can be created but nothing is dialled until one is set up in the deployment.`).replace('class="alert warn"', 'class="alert warn" style="margin:14px 0 0"'))) +
          card("Limits and schedule", callingForm(draft.calling?.cfg || {}, draft.calling?.comp || {}) + nav(true, true));
        $("#calling-form .form-actions", box)?.remove();
        const read = () => { const form = $("#calling-form", box); const f = formData(form); draft.calling = { cfg: { pacing_secs: f.pacing_secs, max_concurrent_calls: f.max_concurrent_calls }, comp: { ...f, enforce_calling_hours: form.enforce_calling_hours.checked, ai_disclosure_required: form.ai_disclosure_required.checked } }; };
        $("#back", box).onclick = () => { read(); step = 2; render(); };
        $("#next", box).onclick = () => { read(); step = 4; render(); };
      } else {
        const a = draft.agent || {};
        const cc = draft.calling?.comp || {};
        const cfgc = draft.calling?.cfg || {};
        const n = draft.contacts.length + (draft.csv ? draft.csv.valid : 0);
        const attempts = Number(cc.max_attempts || cal.max_attempts || 1);
        const schedule = [cc.calling_hours || "deployment default hours", cc.calling_days || "", cc.timezone || ""].filter(Boolean).join(", ") + (cc.enforce_calling_hours === false ? " (not enforced)" : "");
        box.innerHTML = card("Review", `<p class="muted">Check everything before launching. You can change the agent and calling settings later from the campaign page.</p>
          <dl class="kv" style="margin-top:14px">
            <dt>Campaign</dt><dd><b>${esc(draft.name)}</b>${draft.description ? `<div class="muted">${esc(draft.description)}</div>` : ""}</dd>
            <dt>Contacts</dt><dd><b>${plural(n, "contact")}</b><div class="muted">${[draft.contacts.length ? `${draft.contacts.length} from your list` : "", draft.csv ? `${draft.csv.valid} from ${esc(draft.csv.name)}` : ""].filter(Boolean).join(" + ")}</div></dd>
            <dt>Calling number</dt><dd>${esc(t.from_number || "Not set")} <span class="muted">via ${esc(t.provider || "—")}</span> ${t.configured ? "" : badge("not configured")}</dd>
            <dt>AI agent</dt><dd>${esc(a.agent_name || d.agent_name || "—")} <span class="muted">for</span> ${esc(a.company_name || d.company_name || "—")}${a.offer || d.offer ? `<div class="muted">${esc(a.offer || d.offer)}</div>` : ""}</dd>
            <dt>Concurrency</dt><dd>${cfgc.max_concurrent_calls && Number(cfgc.max_concurrent_calls) > 0 ? `Up to ${plural(Number(cfgc.max_concurrent_calls), "simultaneous call")}` : `Deployment limit${cal.describe ? ` (${esc(cal.describe)})` : ""}`}${cfgc.pacing_secs && Number(cfgc.pacing_secs) > 0 ? ` · ${cfgc.pacing_secs}s between calls` : ""}</dd>
            <dt>Schedule</dt><dd>${esc(schedule)}</dd>
            <dt>Retries</dt><dd>Up to ${plural(attempts, "attempt")} per contact${cc.retry_minutes ? `, ${cc.retry_minutes} min apart` : ""}</dd>
            <dt>Estimated scope</dt><dd>${fmtNum(n)} contacts · up to ${fmtNum(n * attempts)} calls</dd>
          </dl>
          ${cc.ai_disclosure_required ? `<p class="muted small" style="margin-top:10px">The agent will disclose that it is an AI in its first sentence.</p>` : ""}
          <div class="form-actions" style="margin-top:18px"><button class="btn ghost left" type="button" id="back">${icon("back")} Back</button><button class="btn" type="button" id="create">Save as draft</button><button class="btn primary" type="button" id="create-start">${icon("play")} Create and start campaign</button></div>`);
        $("#back", box).onclick = () => { step = 3; render(); };
        const create = async (btn, start) => {
          if (start && !(await confirm(`Start "${esc(draft.name)}" now?`, `The campaign is created and the AI agent begins calling its ${plural(n, "contact")} straight away, within the calling hours.${t.configured ? "" : " <br><br><b>No outbound carrier is configured</b>, so nothing is dialled until one is set up."}`, { label: "Create and start" }))) return;
          await withButton(btn, async () => {
            let id = null;
            try {
              const created = (await api.post(`${API}/campaigns`, { name: draft.name, description: draft.description || null })).campaign;
              id = created.id;
              if (a && Object.values(a).some((v) => (Array.isArray(v) ? v.length : v))) await api.put(`${API}/campaigns/${id}/configuration`, a);
              if (draft.calling) {
                const pacing = Number(draft.calling.cfg.pacing_secs || 0);
                const cap = Number(draft.calling.cfg.max_concurrent_calls || 0); if (pacing || cap) await api.put(`${API}/campaigns/${id}/configuration`, { pacing_secs: pacing, max_concurrent_calls: cap });
                const comp = {}; const c = draft.calling.comp;
                if (c.max_attempts) comp.max_attempts = Number(c.max_attempts);
                if (c.retry_minutes) comp.retry_minutes = Number(c.retry_minutes);
                if (c.calling_hours) comp.calling_hours = c.calling_hours;
                if (c.calling_days) comp.calling_days = c.calling_days;
                if (c.timezone) comp.timezone = c.timezone;
                comp.enforce_calling_hours = !!c.enforce_calling_hours;
                comp.ai_disclosure_required = !!c.ai_disclosure_required;
                await api.put(`${API}/campaigns/${id}/compliance`, comp);
              }
              if (draft.contacts.length) await api.post(`${API}/campaigns/${id}/prospects`, { prospect_ids: draft.contacts });
              if (draft.csv) await api.post(`${API}/prospects/import?campaign=${id}`, draft.csv.text, { raw: true, contentType: "text/csv" });
              if (start) {
                await api.post(`${API}/campaigns/${id}/start`);
                ok(`Campaign "${created.name}" started.`);
              } else ok(`Campaign "${created.name}" saved as a draft.`);
              location.hash = `#/campaigns/${id}`;
            } catch (err) {
              if (id) { fail(err, start ? "finish setting up the campaign" : "finish setting up the campaign"); toast("The campaign was created as a draft; check its settings.", "warn"); location.hash = `#/campaigns/${id}`; }
              else fail(err, "create the campaign");
            }
          }, start ? "Creating and starting…" : "Creating…");
        };
        $("#create", box).onclick = (e) => create(e.currentTarget, false);
        $("#create-start", box).onclick = (e) => create(e.currentTarget, true);
      }
    };
    render();
  }

  function pickContacts(done, preselected = [], exclude = []) {
    const selected = new Set(preselected);
    const skip = new Set(exclude);
    const root = modal(`${modalHead("Choose contacts", "Only contacts that can be called are listed.")}<div class="card-body"><div class="toolbar"><div class="search" style="flex:1">${icon("search")}<input class="input" id="q" placeholder="Filter by name, company or number" aria-label="Filter contacts"></div><button class="btn sm" type="button" id="all">Select all shown</button><button class="btn sm ghost" type="button" id="none">Clear</button></div><div id="list">${loading()}</div><div class="form-actions"><span class="muted left" id="count"></span><button class="btn" type="button" id="cancel">Cancel</button><button class="btn primary" type="button" id="use">Use selected</button></div></div>`, { wide: true, title: "Choose contacts" });
    let rows = [];
    const draw = () => {
      const q = $("#q", root).value.toLowerCase();
      const shown = rows.filter((p) => p.dialable && !skip.has(p.id) && (!q || `${p.full_name} ${p.company || ""} ${p.phone_normalized || p.phone || ""}`.toLowerCase().includes(q)));
      $("#list", root).innerHTML = table([
        { label: "", render: (p) => `<input type="checkbox" data-id="${p.id}" aria-label="Select ${esc(p.full_name)}" ${selected.has(p.id) ? "checked" : ""}>` },
        { label: "Name", primary: true, render: (p) => `<b>${esc(p.full_name)}</b>${p.job_title ? `<div class="muted">${esc(p.job_title)}</div>` : ""}` }, { label: "Company", render: (p) => esc(p.company || "—") }, { label: "Phone", render: (p) => esc(p.phone_normalized || p.phone) }, { label: "Status", render: (p) => badge(p.status) },
      ], shown, { emptyHtml: emptyState("contacts", rows.length ? "No contacts match" : "No contacts to choose from", rows.length ? "" : "Import a CSV file first.", "", true), cls: "compact" });
      $$("input[type=checkbox]", root).forEach((c) => (c.onchange = () => { c.checked ? selected.add(Number(c.dataset.id)) : selected.delete(Number(c.dataset.id)); $("#count", root).textContent = `${selected.size} selected`; }));
      $("#count", root).textContent = `${selected.size} selected`;
      $("#all", root).onclick = () => { shown.forEach((p) => selected.add(p.id)); draw(); };
      $("#none", root).onclick = () => { selected.clear(); draw(); };
    };
    api.get(`${API}/prospects?limit=500`).then((r) => { rows = r.prospects || []; draw(); }).catch((e) => ($("#list", root).innerHTML = errorBox(e, { doing: "load the contacts" })));
    $("#q", root).oninput = draw;
    $("#cancel", root).onclick = closeModal;
    $("#use", root).onclick = () => { closeModal(); done(Array.from(selected)); };
  }

  // ---------------------------------------------------------------- contacts
  const CONTACT_STATUSES = [["", "Any status"], ["NEW", "New"], ["CONTACTED", "Contacted"], ["DO_NOT_CALL", "Do not call"], ["UNREACHABLE", "Unreachable"]];
  async function pageContacts(view) {
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const offset = Number(params.get("offset") || 0);
    const status = params.get("status") || "";
    const q = (params.get("q") || "").trim();
    const openId = params.get("open");
    const limit = 50;
    // A search asks the server (name, company, email, and the number for a
    // role that may see it) and shows up to ten matches; otherwise the list is paged.
    let rows, searched = false, total = null;
    if (q) {
      const r = await api.get(`${DASH}/search?q=${encodeURIComponent(q)}`);
      rows = (r.prospects || []).map((p) => ({ id: p.prospect_id, full_name: p.name, company: p.company, phone_normalized: p.phone, email: p.email, status: p.status, dialable: p.status !== "DO_NOT_CALL" }));
      searched = true;
    } else {
      const data = await api.get(`${API}/prospects?limit=${limit}&offset=${offset}${status ? `&status=${status}` : ""}`);
      rows = data.prospects || [];
      total = data.total ?? data.count ?? null;
    }
    const page = Math.floor(offset / limit) + 1;
    const draw = (list, sortKey, dir) => {
      const box = $("#contacts-table", view);
      box.innerHTML = table([
        { label: "Name", primary: true, sort: "full_name", render: (p) => `<a href="#/contacts?open=${p.id}${status ? `&status=${status}` : ""}${q ? `&q=${encodeURIComponent(q)}` : ""}" data-open="${p.id}">${esc(p.full_name)}</a>${p.job_title ? `<div class="muted">${esc(p.job_title)}</div>` : ""}` },
        { label: "Company", sort: "company", render: (p) => esc(p.company || "—") },
        { label: "Phone", render: (p) => `<span class="nowrap">${esc(p.phone_normalized || p.phone || "—")}</span>` },
        { label: "Email", render: (p) => esc(p.email || "—") },
        { label: "Status", sort: "status", render: (p) => badge(p.status) },
        { label: "Added", sort: "created_at", render: (p) => `<span class="nowrap">${esc(p.created_at ? fmtDay(p.created_at) : "—")}</span>` },
        { label: "", actions: true, render: (p) => `<button class="btn sm ghost" type="button" data-open="${p.id}">View</button>${can("write") && p.status !== "DO_NOT_CALL" ? ` <button class="btn sm ghost danger" type="button" data-dnc="${p.id}" data-name="${esc(p.full_name)}" data-tip="Do not call">${icon("ban")}<span class="visually-hidden">Do not call</span></button>` : ""}` },
      ], list, { emptyHtml: emptyState("contacts", searched ? "No contacts match" : status ? `No ${statusLabel(status).toLowerCase()} contacts` : "No contacts yet", searched ? `Nothing matched "${esc(q)}". Try a name, a company or an email.` : status ? "Try another status." : "Import a CSV file to add contacts, or add one by hand.", !searched && !status && can("write") ? `<a class="btn primary" href="#/contacts/import">${icon("upload")} Import contacts</a>` : "") });
      $$("th[data-sort]", box).forEach((th) => { if (th.dataset.sort === sortKey) th.classList.add("sorted", dir < 0 ? "desc" : ""); });
      sortable(box, list, draw);
      $$("[data-open]", box).forEach((b) => (b.onclick = (e) => { e.preventDefault(); openContact(b.dataset.open); }));
      $$("[data-dnc]", box).forEach((b) => (b.onclick = () => markDnc(b.dataset.dnc, b.dataset.name, () => route(true))));
    };
    view.innerHTML = head("Contacts", searched ? `${plural(rows.length, "match", "matches")} for "${esc(q)}"${rows.length >= 10 ? " (first 10 shown)" : ""}` : `${total != null ? plural(total, "contact") : plural(rows.length, "contact")}${rows.length === limit || offset ? ` · page ${page}` : ""}${status ? ` · ${statusLabel(status)}` : ""}`,
      can("write") ? `<button class="btn" type="button" id="add">${icon("plus")} Add contact</button><a class="btn primary" href="#/contacts/import">${icon("upload")} Import contacts</a>` : "") +
      `<form class="filters" id="filters"><div class="field wide"><span class="lbl">Search</span><div class="search">${icon("search")}<input name="q" value="${esc(q)}" placeholder="Name, company, email${can("read_pii") ? " or phone number" : ""}" aria-label="Search contacts"></div></div>${field("Status", select("status", CONTACT_STATUSES, status))}<button class="btn" type="submit">Apply</button>${q || status ? `<a class="btn ghost" href="#/contacts">Clear</a>` : ""}</form>` +
      card("", `<div id="contacts-table"></div>` + (searched ? "" : `<div class="pager"><span class="info">Page ${page}${rows.length ? ` · ${offset + 1}–${offset + rows.length}` : ""}</span><button class="btn sm" type="button" id="prev" ${offset ? "" : "disabled"}>Previous</button><button class="btn sm" type="button" id="next" ${rows.length < limit ? "disabled" : ""}>Next</button></div>`), "", { flush: true });
    draw(rows);
    const go = (o) => { location.hash = `#/contacts?offset=${o}${status ? `&status=${status}` : ""}`; };
    if (!searched) { $("#prev", view).onclick = () => go(Math.max(0, offset - limit)); $("#next", view).onclick = () => go(offset + limit); }
    $("#filters", view).onsubmit = (e) => {
      e.preventDefault();
      const f = formData(e.target);
      const p = new URLSearchParams();
      if (f.q.trim()) p.set("q", f.q.trim());
      if (f.status) p.set("status", f.status);
      location.hash = `#/contacts${p.toString() ? `?${p}` : ""}`;
    };
    const add = $("#add", view);
    if (add) add.onclick = () => {
      const root = modal(`${modalHead("Add contact")}<div class="card-body"><form id="f" novalidate><div class="row">${field("First name", inp("first_name", "", "required autofocus"), "", { required: true })}${field("Last name", inp("last_name", "", "required"), "", { required: true })}</div><div class="row">${field("Phone", inp("phone", "", 'required type="tel" placeholder="+92 300 1234567"'), "Include the country code.", { required: true })}${field("Email", inp("email", "", 'type="email"'))}</div><div class="row">${field("Company", inp("company"))}${field("Job title", inp("job_title"))}</div><div class="form-actions"><button class="btn" type="button" id="cancel">Cancel</button><button class="btn primary" type="submit">Save contact</button></div></form></div>`, { title: "Add contact" });
      $("#cancel", root).onclick = closeModal;
      $("#f", root).onsubmit = (e) => { e.preventDefault(); const f = formData(e.target); if (!f.first_name || !f.last_name || !f.phone) return toast("First name, last name and phone are required.", "warn"); withButton($("button.primary", root), async () => { try { const body = Object.fromEntries(Object.entries(f).filter(([, v]) => v)); const r = await api.post(`${API}/prospects`, body); closeModal(); ok(r.created ? `${r.prospect.full_name} added.` : `${r.prospect.full_name} was already in your contacts.`); if (r.warnings?.length) toast(r.warnings.join(" "), "warn"); route(true); } catch (err) { fail(err, "save the contact"); } }, "Saving…"); };
    };
    if (openId) openContact(openId);
  }

  async function markDnc(id, name, after) {
    if (!(await confirm(`Stop calling ${name}?`, `<b>${esc(name)}</b> is marked <b>do not call</b>: the number goes on the do-not-call list, every open campaign membership is closed, and no campaign will dial it again.`, { danger: true, label: "Mark do not call" }))) return;
    try { await api.post(`${API}/prospects/${id}/do-not-call`, { reason: "marked in the application" }); ok(`${name} marked do not call.`); await after(); } catch (e) { fail(e, "update the contact"); }
  }

  // A contact's detail: who they are, their calls, their callbacks.
  async function openContact(id) {
    const root = modal(`${modalHead("Contact")}<div class="card-body" id="body">${skelLines(5)}</div>`, { wide: true, title: "Contact details" });
    try {
      const r = await api.get(`${API}/prospects/${id}`);
      const p = r.prospect;
      const custom = Object.entries(p.custom_data || {}).filter(([, v]) => v != null && v !== "");
      $("#modal-title", root).textContent = p.full_name;
      $("#body", root).innerHTML = `
        <div class="row-flex" style="margin-bottom:14px">${badge(p.status, "", "lg")}${p.dialable ? "" : `<span class="muted small">This contact cannot be called.</span>`}<span class="spacer"></span>${can("write") && p.status !== "DO_NOT_CALL" ? `<button class="btn sm danger" type="button" id="dnc">${icon("ban")} Do not call</button>` : ""}</div>
        ${facts([["Phone", `<span class="nowrap">${esc(p.phone_normalized || p.phone || "—")}</span>`], ["Email", esc(p.email || "")], ["Company", esc(p.company || "")], ["Job title", esc(p.job_title || "")], ["Industry", esc(p.industry || "")], ["Location", esc(p.location || "")], ["Website", p.website ? esc(p.website) : ""], ["Added", esc(fmtDate(p.created_at))]])}
        ${custom.length ? `<details class="panel" style="margin-top:14px"><summary>Custom fields (${custom.length})</summary><div class="card-body">${kv(custom.map(([k, v]) => [k, esc(String(v))]))}</div></details>` : ""}
        <div class="section-title">Calls</div>
        ${(r.calls || []).length ? table([
          { label: "When", render: (a) => `<a href="#/calls/${a.id}">${esc(fmtDate(a.started_at || a.placement_started_at || a.created_at))}</a>` },
          { label: "Status", render: (a) => badge(a.status) },
          { label: "Attempt", num: true, key: "attempt_number" },
          { label: "Duration", render: (a) => esc(fmtDur(a.duration_seconds)) },
          { label: "", actions: true, render: (a) => `<a class="btn sm ghost" href="#/calls/${a.id}">Details</a>` },
        ], r.calls, { cls: "compact" }) : `<p class="muted">No calls yet.</p>`}
        ${(r.callbacks || []).length ? `<div class="section-title">Callbacks</div><ul class="list-clean">${r.callbacks.map((cb) => `<li>${badge(cb.status)}<span>${esc(fmtDate(cb.scheduled_for))}${cb.note ? ` <span class="muted">· ${esc(cb.note)}</span>` : ""}</span></li>`).join("")}</ul>` : ""}
        <div class="form-actions"><button class="btn" type="button" id="close">Close</button></div>`;
      $("#close", root).onclick = closeModal;
      $$("a[href^='#/calls/']", root).forEach((a) => (a.onclick = () => closeModal()));
      const dnc = $("#dnc", root);
      if (dnc) dnc.onclick = () => { closeModal(); markDnc(p.id, p.full_name, () => route(true)); };
    } catch (e) { $("#body", root).innerHTML = errorBox(e, { doing: "load the contact" }) + `<div class="form-actions"><button class="btn" type="button" id="close">Close</button></div>`; $("#close", root).onclick = closeModal; }
  }

  // ------------------------------------------------------------ CSV import
  function parseCsvPreview(text, max = 20) {
    const rows0 = text.split(/\r?\n/).filter((l) => l.trim());
    const split = (line) => { const out = []; let cur = ""; let q = false; for (const ch of line) { if (ch === '"') q = !q; else if (ch === "," && !q) { out.push(cur); cur = ""; } else cur += ch; } out.push(cur); return out.map((s) => s.trim()); };
    const header = rows0.length ? split(rows0[0]) : [];
    const rows = rows0.slice(1, 1 + max).map(split);
    return { header, rows, total: Math.max(0, rows0.length - 1) };
  }

  // Upload → validating → preview and results → use. Never writes: the
  // dry run only parses; the caller decides what to do with the rows.
  function csvWizard(done) {
    const dropzone = `<label class="dropzone" id="drop" for="file"><span class="drop-icon" aria-hidden="true">${icon("upload")}</span><b>Drop a CSV file here, or choose one</b><span class="muted small">Needs first name, last name and phone columns. Phone numbers need a country code (+92…) unless a default region is configured.</span><span class="btn primary" style="margin-top:12px">${icon("file")} Choose file</span><input type="file" id="file" accept=".csv,text/csv" aria-label="CSV file"></label>`;
    const root = modal(`${modalHead("Import from CSV", "Rows are validated before anything is saved.")}<div class="card-body" id="body">${dropzone}</div>`, { wide: true, title: "Import from CSV" });
    const handle = async (file) => {
      if (!file) return;
      const body = $("#body", root);
      body.innerHTML = `<div class="loading"><span class="spinner"></span> Validating ${esc(file.name)}…</div>`;
      let text;
      try { text = await file.text(); } catch (e) { body.innerHTML = errorBox(e, { doing: "read the file" }); return; }
      try {
        const report = await api.post(`${API}/prospects/import?dry_run=true`, text, { raw: true, contentType: "text/csv" });
        const preview = parseCsvPreview(text);
        const rejected = report.rejected || [];
        const duplicates = report.duplicates ?? 0;
        const valid = Math.max(0, preview.total - rejected.length);
        const fresh = Math.max(0, valid - duplicates);
        body.innerHTML = `
          <div class="row-flex" style="margin-bottom:14px">${icon("file")}<b>${esc(file.name)}</b><span class="muted">${plural(preview.total, "row")}</span><span class="spacer"></span><button class="btn sm ghost" type="button" id="again">Choose another file</button></div>
          <div class="grid stats compact" style="margin-bottom:14px">${stat("Valid rows", fmtNum(valid), "will be imported", valid ? "good" : "", "compact")}${stat("Invalid rows", fmtNum(rejected.length), "will be skipped", rejected.length ? "bad" : "", "compact")}${stat("Already known", fmtNum(duplicates), "matched by number, kept as they are", duplicates ? "warn" : "", "compact")}${stat("New contacts", fmtNum(fresh), "", fresh ? "good" : "", "compact")}</div>
          ${valid ? "" : alert("bad", `<b>No row can be imported.</b> Check the column names and the phone numbers, then try again.`)}
          ${report.mapping ? `<p class="muted small">Columns recognised: ${Object.entries(report.mapping.columns || {}).map(([k, v]) => `<b>${esc(v)}</b> as ${esc(human(k)).toLowerCase()}`).join(", ")}${(report.mapping.extras || []).length ? `. Kept as custom fields: ${report.mapping.extras.map(esc).join(", ")}` : ""}.</p>` : ""}
          ${rejected.length ? `<details class="panel" open style="margin-top:12px"><summary>${plural(rejected.length, "row")} will be skipped</summary><div class="card-body flush">${table([{ label: "Line", num: true, key: "line" }, { label: "Problem", render: (r) => esc((r.errors || []).join("; ")) }, { label: "Values", render: (r) => `<span class="muted">${esc(Object.values(r.values || {}).join(", "))}</span>` }], rejected.slice(0, 50), { cls: "dense" })}${rejected.length > 50 ? `<p class="muted small" style="padding:8px 12px">…and ${rejected.length - 50} more.</p>` : ""}</div></details>` : ""}
          <details class="panel" ${rejected.length ? "" : "open"} style="margin-top:12px"><summary>Preview (first ${preview.rows.length} of ${preview.total} rows)</summary><div class="card-body flush"><div class="tbl-wrap"><table class="tbl dense"><thead><tr>${preview.header.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${preview.rows.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div></details>
          <div class="form-actions"><button class="btn" type="button" id="cancel">Cancel</button><button class="btn primary" type="button" id="use" ${valid ? "" : "disabled"}>${icon("check")} Use ${plural(valid, "valid row")}</button></div>`;
        $("#cancel", body).onclick = closeModal;
        $("#again", body).onclick = () => { body.innerHTML = dropzone; bind(); };
        $("#use", body).onclick = () => { closeModal(); done({ name: file.name, text, valid, report, duplicates, rejected: rejected.length }); };
      } catch (err) { body.innerHTML = errorBox(err, { doing: "validate the file" }) + `<div class="form-actions"><button class="btn" type="button" id="again">Try another file</button><button class="btn" type="button" id="cancel">Close</button></div>`; $("#cancel", body).onclick = closeModal; $("#again", body).onclick = () => { body.innerHTML = dropzone; bind(); }; }
    };
    const bind = () => {
      const drop = $("#drop", root);
      $("#file", root).onchange = (e) => handle(e.target.files[0]);
      drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
      drop.ondragleave = () => drop.classList.remove("over");
      drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); handle(e.dataTransfer.files[0]); };
    };
    bind();
  }

  // Upload → validate → preview → confirm → done. Importing never places a call.
  async function pageImport(view) {
    if (!can("write")) { view.innerHTML = head("Import contacts") + alert("warn", "Your role can view contacts but not import them."); return; }
    const campaigns = (await api.get(`${API}/campaigns?limit=200`)).campaigns || [];
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    let chosen = null;
    let outcome = null;
    let campaignId = params.get("campaign") || "";
    const steps = ["Upload", "Validate", "Confirm", "Done"];
    const stepper = (at) => `<div class="stepper" aria-label="Import steps">${steps.map((s, i) => `<div class="step ${i === at ? "active" : i < at ? "done" : ""}" ${i === at ? 'aria-current="step"' : ""}><span class="n">${i < at ? icon("check") : i + 1}</span><span class="t">${s}</span></div>`).join("")}</div>`;
    const render = () => {
      const at = outcome ? 3 : chosen ? 2 : 0;
      view.innerHTML = head("Import contacts", "Upload a CSV file, check the validation results, then confirm. Nothing is dialled by importing.", "", { eyebrow: "Contacts" }) + stepper(at) + `<div id="body"></div>`;
      const body = $("#body", view);
      if (outcome) {
        const r = outcome;
        body.innerHTML = card("", `<div class="status-hero"><span class="big good">${icon("check")}</span><div><h3>${plural(r.created, "contact")} imported</h3><p>${[r.duplicates ? `${plural(r.duplicates, "contact")} already existed and ${r.duplicates === 1 ? "was" : "were"} kept` : "", r.rejected_count ? `${plural(r.rejected_count, "row")} skipped` : "", r.added_to_campaign != null && campaignId ? `${plural(r.added_to_campaign, "contact")} added to the campaign` : ""].filter(Boolean).join(" · ") || "Everything went in."}</p></div></div>` +
          ((r.rejected || []).length ? `<div class="card-body" style="padding-top:0"><details class="panel"><summary>Skipped rows (${r.rejected.length})</summary><div class="card-body flush">${table([{ label: "Line", num: true, key: "line" }, { label: "Problem", render: (x) => esc((x.errors || []).join("; ")) }], r.rejected, { cls: "dense" })}</div></details></div>` : "") +
          `<div class="card-foot"><a class="btn primary" href="#/contacts">View contacts</a>${campaignId ? `<a class="btn" href="#/campaigns/${esc(campaignId)}">Open the campaign</a>` : `<a class="btn" href="#/campaigns/new">Create a campaign</a>`}<button class="btn ghost" type="button" id="more">Import another file</button></div>`, "", { flush: true });
        $("#more", view).onclick = () => { outcome = null; chosen = null; render(); };
        return;
      }
      if (!chosen) {
        body.innerHTML = card("Upload a file", `<label class="dropzone" id="drop" for="file"><span class="drop-icon" aria-hidden="true">${icon("upload")}</span><b>Drop a CSV file here, or choose one</b><span class="muted small">Needs first name, last name and phone columns. Other columns are kept as custom fields.</span><span class="btn primary" style="margin-top:12px">${icon("file")} Choose file</span><input type="file" id="file" accept=".csv,text/csv" aria-label="CSV file"></label>
          <p class="muted small" style="margin-top:12px">Phone numbers need a country code (for example +92 300 1234567) unless a default region is configured. Numbers already in your contacts are matched and kept, not duplicated.</p>`);
        const pick = (file) => { if (!file) return; chosen = { pendingFile: file }; render(); };
        $("#file", body).onchange = (e) => pick(e.target.files[0]);
        const drop = $("#drop", body);
        drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
        drop.ondragleave = () => drop.classList.remove("over");
        drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); pick(e.dataTransfer.files[0]); };
        return;
      }
      if (chosen.pendingFile) {
        // Validating: the dry run parses the file and reports every row.
        view.innerHTML = head("Import contacts", "Upload a CSV file, check the validation results, then confirm. Nothing is dialled by importing.") + stepper(1) + card("", `<div class="loading"><span class="spinner"></span> Validating ${esc(chosen.pendingFile.name)}…</div>`);
        const file = chosen.pendingFile;
        (async () => {
          try {
            const text = await file.text();
            const report = await api.post(`${API}/prospects/import?dry_run=true`, text, { raw: true, contentType: "text/csv" });
            const preview = parseCsvPreview(text);
            const rejected = report.rejected || [];
            chosen = { name: file.name, text, report, preview, rejected, valid: Math.max(0, preview.total - rejected.length), duplicates: report.duplicates ?? 0 };
          } catch (e) {
            chosen = null;
            render();
            $("#body", view).insertAdjacentHTML("afterbegin", errorBox(e, { doing: "validate the file" }));
            return;
          }
          render();
        })();
        return;
      }
      const c = chosen;
      const fresh = Math.max(0, c.valid - c.duplicates);
      body.innerHTML = card("Validation results", `<div class="row-flex" style="margin-bottom:14px">${icon("file")}<b>${esc(c.name)}</b><span class="muted">${plural(c.preview.total, "row")}</span><span class="spacer"></span><button class="btn sm ghost" type="button" id="again">Choose another file</button></div>
        <div class="grid stats compact" style="margin-bottom:14px">${stat("Valid rows", fmtNum(c.valid), "will be imported", c.valid ? "good" : "", "compact")}${stat("Invalid rows", fmtNum(c.rejected.length), "will be skipped", c.rejected.length ? "bad" : "", "compact")}${stat("Already known", fmtNum(c.duplicates), "matched by number, kept as they are", c.duplicates ? "warn" : "", "compact")}${stat("New contacts", fmtNum(fresh), "", fresh ? "good" : "", "compact")}</div>
        ${c.valid ? "" : alert("bad", `<b>No row can be imported.</b> Check the column names and the phone numbers, then choose the file again.`)}
        ${c.report.mapping ? `<p class="muted small">Columns recognised: ${Object.entries(c.report.mapping.columns || {}).map(([k, v]) => `<b>${esc(v)}</b> as ${esc(human(k)).toLowerCase()}`).join(", ")}${(c.report.mapping.extras || []).length ? `. Kept as custom fields: ${c.report.mapping.extras.map(esc).join(", ")}` : ""}.</p>` : ""}
        ${c.rejected.length ? `<details class="panel" open style="margin-top:12px"><summary>${plural(c.rejected.length, "row")} will be skipped</summary><div class="card-body flush">${table([{ label: "Line", num: true, key: "line" }, { label: "Problem", render: (r) => esc((r.errors || []).join("; ")) }, { label: "Values", render: (r) => `<span class="muted">${esc(Object.values(r.values || {}).join(", "))}</span>` }], c.rejected.slice(0, 50), { cls: "dense" })}</div></details>` : ""}
        <details class="panel" ${c.rejected.length ? "" : "open"} style="margin-top:12px"><summary>Preview (first ${c.preview.rows.length} of ${c.preview.total} rows)</summary><div class="card-body flush"><div class="tbl-wrap"><table class="tbl dense"><thead><tr>${c.preview.header.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${c.preview.rows.map((r) => `<tr>${r.map((x) => `<td>${esc(x)}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div></details>`) +
        card("Confirm import", `<form id="f"><div class="row one">${field("Add these contacts to a campaign", select("campaign", [["", "No campaign — contacts only"], ...campaigns.filter((x) => x.status !== "COMPLETED" && x.status !== "CANCELLED").map((x) => [x.id, `${x.name} (${statusLabel(x.status)})`])], campaignId), "Optional. Contacts added to a running campaign are queued for calling; a draft campaign keeps them until it runs.")}</div>
          <div class="form-actions"><a class="btn ghost left" href="#/contacts">Cancel</a><button class="btn primary" type="submit" ${c.valid ? "" : "disabled"}>${icon("check")} Import ${plural(c.valid, "contact")}</button></div></form>`);
      $("#again", body).onclick = () => { chosen = null; render(); };
      $("#f", body).onsubmit = (e) => { e.preventDefault(); campaignId = e.target.campaign.value; withButton($("button[type=submit]", e.target), async () => {
        try {
          const r = await api.post(`${API}/prospects/import${campaignId ? `?campaign=${campaignId}` : ""}`, c.text, { raw: true, contentType: "text/csv" });
          outcome = r;
          ok(`${plural(r.created, "contact")} imported.`);
          render();
        } catch (err) { fail(err, "import the contacts"); }
      }, "Importing…"); };
    };
    render();
  }

  // ------------------------------------------------------------------- calls
  const CALL_STATUSES = [["", "Any status"], ["COMPLETED", "Completed"], ["CALLBACK_REQUESTED", "Callback requested"], ["NOT_INTERESTED", "Not interested"], ["DO_NOT_CALL", "Do not call"], ["NO_ANSWER", "No answer"], ["BUSY", "Busy"], ["VOICEMAIL", "Voicemail"], ["FAILED", "Failed"], ["CONNECTED", "Connected"], ["CALLING", "Calling"], ["QUEUED", "Queued"]];
  const qualBadge = (r) => (r.qualification && r.qualification !== "UNKNOWN" ? badge(r.qualification) : `<span class="muted">—</span>`);
  const meetingBadge = (s) => (s && s !== "NONE" && s !== "UNKNOWN" ? badge(s === "BOOKED" ? "MEETING_BOOKED" : s === "AGREED" ? "MEETING_AGREED" : s) : `<span class="muted">—</span>`);
  function callsTable(rows, { campaign = true } = {}) {
    return table([
      { label: "Contact", primary: true, render: (r) => `<a href="#/calls/${r.attempt_id}">${esc(r.prospect)}</a>${r.company ? `<div class="muted">${esc(r.company)}</div>` : ""}` },
      ...(campaign ? [{ label: "Campaign", render: (r) => (r.campaign_id ? `<a href="#/campaigns/${r.campaign_id}" class="muted">${esc(r.campaign)}</a>` : `<span class="muted">${esc(r.campaign || "—")}</span>`) }] : []),
      { label: "Status", render: (r) => (["CALLING", "CONNECTED", "QUEUED"].includes(r.status) && !r.has_result ? liveBadge(r.disposition || r.status) : badge(r.disposition || r.status)) },
      { label: "Duration", render: (r) => `<span class="nowrap">${esc(r.duration || "—")}</span>` },
      { label: "Qualification", render: qualBadge },
      { label: "Meeting", render: (r) => meetingBadge(r.meeting_status) },
      { label: "Date", render: (r) => `<span class="nowrap">${esc(r.at_label || fmtDate(r.at))}</span>` },
      { label: "", actions: true, render: (r) => `<a class="btn sm ghost" href="#/calls/${r.attempt_id}">View</a>` },
    ], rows, { emptyHtml: emptyState("calls", "No calls yet", "Calls will appear here once a campaign is running.", "", true), rowAttr: (r) => `class="click" data-href="#/calls/${r.attempt_id}" tabindex="0"` });
  }
  const bindCallRows = (root) => $$("tr.click", root).forEach((tr) => { tr.onclick = (e) => { if (!e.target.closest("a, button")) location.hash = tr.dataset.href; }; tr.onkeydown = (e) => { if (e.key === "Enter") location.hash = tr.dataset.href; }; });

  async function pageCalls(view) {
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const q = Object.fromEntries(params.entries());
    const server = { campaign: q.campaign, status: q.status, q: q.q, from: q.from, to: q.to, before_id: q.before_id };
    const [data, campaigns] = await Promise.all([
      api.get(`${DASH}/calls?${new URLSearchParams({ limit: 50, ...Object.fromEntries(Object.entries(server).filter(([, v]) => v)) }).toString()}`),
      api.get(`${API}/campaigns?limit=200`),
    ]);
    // Qualification and meeting are not server filters; they narrow the page in view.
    let rows = data.calls || [];
    if (q.qualification) rows = rows.filter((r) => (q.qualification === "ANY" ? r.qualification && r.qualification !== "UNKNOWN" : r.qualification === q.qualification));
    if (q.meeting) rows = rows.filter((r) => (q.meeting === "ANY" ? r.meeting_status && !["NONE", "UNKNOWN"].includes(r.meeting_status) : r.meeting_status === q.meeting));
    const narrowed = q.qualification || q.meeting;
    const hasFilter = Object.values(q).some(Boolean);
    view.innerHTML = head("Calls", `${fmtNum(rows.length)} shown${narrowed ? " on this page" : ""}${data.filters?.label ? ` · ${esc(data.filters.label)}` : ""}`, "", { eyebrow: "Call history" }) +
      `<form class="filters" id="f"><div class="field wide"><span class="lbl">Search</span><div class="search">${icon("search")}<input name="q" value="${esc(q.q || "")}" placeholder="Name, company, email…" aria-label="Search calls"></div></div>
        ${field("Campaign", select("campaign", [["", "All campaigns"], ...(campaigns.campaigns || []).map((c) => [c.id, c.name])], q.campaign))}
        ${field("Status", select("status", CALL_STATUSES, q.status))}
        ${field("Qualification", select("qualification", [["", "Any"], ["ANY", "Has a result"], ["QUALIFIED", "Qualified"], ["PARTIALLY_QUALIFIED", "Partly qualified"], ["DISQUALIFIED", "Not qualified"]], q.qualification))}
        ${field("Meeting", select("meeting", [["", "Any"], ["ANY", "Any meeting outcome"], ["BOOKED", "Booked"], ["AGREED", "Agreed"], ["PROPOSED", "Proposed"], ["DECLINED", "Declined"]], q.meeting))}
        ${field("From", inp("from", q.from || "", 'type="date"'))}${field("To", inp("to", q.to || "", 'type="date"'))}
        <button class="btn" type="submit">Apply</button>${hasFilter ? `<a class="btn ghost" href="#/calls">Clear</a>` : ""}</form>` +
      card("", callsTable(rows) + `<div class="pager"><span class="info">${data.next_before_id ? "More calls available" : "End of the list"}</span><button class="btn sm" type="button" id="more" ${data.next_before_id ? "" : "disabled"}>Older calls</button></div>`, "", { flush: true });
    $("#f", view).onsubmit = (e) => { e.preventDefault(); const f = Object.fromEntries(Object.entries(formData(e.target)).filter(([, v]) => v)); location.hash = `#/calls${Object.keys(f).length ? `?${new URLSearchParams(f)}` : ""}`; };
    $("#more", view).onclick = () => { location.hash = `#/calls?${new URLSearchParams({ ...q, before_id: data.next_before_id })}`; };
    bindCallRows(view);
  }

  // The call page: the outcome first, then what was said, then how it went.
  async function pageCall(view, [id]) {
    const d = await api.get(`${DASH}/calls/${id}`);
    const r = d.result || {};
    const rl = d.result_labels || {};
    const cl = d.call_labels || {};
    const c = d.call || {};
    const summary = r.summary || {};
    const transcript = r.transcript || (d.conversation && d.conversation.transcript) || [];
    const who = d.prospect ? d.prospect.full_name || [d.prospect.first_name, d.prospect.last_name].filter(Boolean).join(" ") : ""; // the detail JSON carries first/last, not full_name
    const disposition = r.disposition || c.status;
    const items = (list, empty) => (list && list.length ? `<ul class="list-clean">${list.map((i) => { const text = typeof i === "string" ? i : i.detail || i.text || ""; const kind = typeof i === "object" && i.kind ? i.kind : ""; return `<li>${kind ? `<span class="badge plain">${esc(human(kind))}</span>` : ""}<span>${esc(text || human(kind))}${typeof i === "object" && i.handled === false ? ` <span class="muted small">· not addressed</span>` : ""}</span></li>`; }).join("")}</ul>` : `<p class="muted">${esc(empty)}</p>`);
    const known = (v) => v && !["UNKNOWN", "NONE"].includes(String(v).toUpperCase());
    const nextAction = known(r.next_action) ? statusLabel(r.next_action) : "";
    const meetings = d.meetings || [], callbacks = d.callbacks || [], transfers = d.transfers || [];
    const hasMeeting = known(r.meeting_status) || meetings.length;
    const hasCallback = known(r.callback_status) || callbacks.length;
    view.innerHTML = head(`${esc(who || "Unknown contact")} ${badge(disposition, tone(cl.tone), "lg")}`, `${esc(cl.at || fmtDate(c.started_at))}${cl.duration ? ` · ${esc(cl.duration)}` : ""}${d.campaign ? ` · <a href="#/campaigns/${d.campaign.id}">${esc(d.campaign.name)}</a>` : ""}`, `<a class="btn ghost" href="#/calls">${icon("back")} All calls</a>`, { eyebrow: "Call review" }) +
      card("", facts([
        ["Contact", `<b>${esc(who || "—")}</b>${d.prospect?.job_title ? `<div class="muted">${esc(d.prospect.job_title)}</div>` : ""}`],
        ["Company", esc(d.prospect?.company || "")],
        ["Phone", d.prospect ? `<span class="nowrap">${esc(d.prospect.phone_normalized || d.prospect.phone || "—")}</span>` : ""],
        ["Campaign", d.campaign ? `<a href="#/campaigns/${d.campaign.id}">${esc(d.campaign.name)}</a>` : ""],
        ["Call status", badge(disposition, tone(cl.tone))],
        ["Duration", esc(cl.duration || fmtDur(c.duration_seconds))],
        ["Date and time", esc(cl.at || fmtDate(c.started_at))],
        ["Qualification", known(r.qualification_status) ? badge(r.qualification_status) : `<span class="muted">Not assessed</span>`],
        ["Meeting", known(r.meeting_status) ? meetingBadge(r.meeting_status) : `<span class="muted">None</span>`],
      ])) +
      `<div class="grid main-side" style="margin-top:14px">
        <div class="stack">
          ${card("Conversation summary", summary.what_happened || summary.text ? `<p style="font-size:14.5px">${esc(summary.what_happened || summary.text)}</p>${summary.prospect_needs ? `<p><span class="muted">What they need:</span> ${esc(Array.isArray(summary.prospect_needs) ? summary.prospect_needs.join("; ") : summary.prospect_needs)}</p>` : ""}${summary.objections && (Array.isArray(summary.objections) ? summary.objections.length : summary.objections) ? `<p><span class="muted">Objections raised:</span> ${esc(Array.isArray(summary.objections) ? summary.objections.join("; ") : summary.objections)}</p>` : ""}${summary.interest ? `<p><span class="muted">Interest:</span> ${esc(summary.interest)}</p>` : ""}${summary.next_step ? `<p><span class="muted">Next step:</span> ${esc(summary.next_step)}</p>` : ""}${r.notes?.length ? `<p class="muted small">${r.notes.map(esc).join(" · ")}</p>` : ""}` : `<p class="muted">${c.failure_reason ? `The call did not produce a conversation: ${esc(c.failure_reason)}` : d.result ? "No summary was produced for this call." : "This call has no result yet."}</p>`)}
          ${card("Qualification", d.result ? `${facts([["Status", known(r.qualification_status) ? badge(r.qualification_status) : "Not assessed"], ["Interest", known(r.interest_level) ? badge(r.interest_level) : ""], ["Decision role", known(r.decision_role) ? human(r.decision_role) : ""], ["Buying timeline", known(r.buying_timeline) ? human(r.buying_timeline) : ""], ["Existing provider", esc(r.existing_provider || "")], ["Current process", esc(r.current_process || "")], ["Impact", esc(r.impact || "")], ["Desired outcome", esc(r.desired_outcome || "")]])}` : `<p class="muted">No qualification data — the call did not reach a conversation.</p>`)}
          <div class="grid two">
            ${card("Pain points", items(r.pain_points, "None recorded."))}
            ${card("Objections", items(r.objections, "None recorded."))}
          </div>
          ${r.questions?.length ? card("Questions the contact asked", items(r.questions, "")) : ""}
          ${card("Transcript", d.transcript_included ? (transcript.length ? `<div class="transcript">${transcript.map((t) => { const agent = t.role === "assistant"; return `<div class="turn ${agent ? "assistant" : "user"}"><span class="av" aria-hidden="true">${agent ? icon("sparkle") : esc(initials(who))}</span><div><div class="who"><span>${agent ? "AI agent" : esc(who || "Customer")}</span>${t.at != null ? `<span class="t">${esc(fmtDur(Number(t.at)))}</span>` : ""}</div><div class="bubble">${esc(t.text)}</div>${t.interrupted ? `<div class="flag">Interrupted</div>` : ""}</div></div>`; }).join("")}</div>` : `<p class="muted">The call produced no transcript.</p>`) : `<p class="muted">${d.transcript_available ? "Transcripts are available to roles with permission to read personal data; your role sees the outcome only." : "No transcript was recorded for this call."}</p>`, "", { sub: transcript.length ? `${plural(transcript.length, "turn")}` : "" })}
        </div>
        <div class="stack">
          ${nextAction ? `<div class="next-action">${icon("arrow")}<div><div class="muted small">Next action</div><b>${esc(nextAction)}</b>${summary.next_step ? `<div class="muted small">${esc(summary.next_step)}</div>` : ""}</div></div>` : ""}
          ${card("Meeting", hasMeeting ? `${known(r.meeting_status) ? `<p>${meetingBadge(r.meeting_status)}${rl.meeting_start ? ` <b>${esc(rl.meeting_start)}</b>` : ""}${r.meeting_when ? ` <span class="muted">(${esc(r.meeting_when)})</span>` : ""}</p>` : ""}${meetings.map((m) => `<p>${badge(m.status)} ${esc(fmtDate(m.start_at))}${m.provider ? ` <span class="muted">via ${esc(m.provider)}</span>` : ""}${m.attendee_email ? `<div class="muted small">${esc(m.attendee_email)}</div>` : ""}</p>`).join("")}` : `<p class="muted">No meeting was booked on this call.</p>`)}
          ${card("Callback", hasCallback ? `${known(r.callback_status) ? `<p>${badge(r.callback_status)}${rl.callback_for ? ` <b>${esc(rl.callback_for)}</b>` : ""}${r.callback_when ? ` <span class="muted">(${esc(r.callback_when)})</span>` : ""}</p>` : ""}${callbacks.map((cb) => `<p>${badge(cb.status)} ${esc(fmtDate(cb.scheduled_for))}${cb.note ? `<div class="muted small">${esc(cb.note)}</div>` : ""}</p>`).join("")}` : `<p class="muted">No callback was requested.</p>`)}
          ${transfers.length ? card("Transfer", transfers.map((t) => `<p>${badge(t.status)} to ${esc(t.to_number)}${t.error ? `<div class="muted small">${esc(t.error)}</div>` : ""}</p>`).join("")) : ""}
          ${card("Call metadata", kv([["Started", esc(fmtDate(c.started_at))], c.connected_at ? ["Connected", esc(fmtDate(c.connected_at))] : null, ["Ended", esc(fmtDate(c.ended_at))], ["Attempt", `${esc(c.attempt_number ?? "—")}${d.campaign ? " in this campaign" : ""}`], ["Ended by", d.result ? (r.agent_ended_call ? "The AI agent" : "The contact or the carrier") : "—"], r.human_requested ? ["Asked for a person", "Yes"] : null, c.failure_reason ? ["Failure reason", esc(c.failure_reason)] : null]))}
          <details class="panel"><summary>Technical details</summary><div class="card-body">${kv([["Call reference", `<code>${esc(c.telephony_call_id || "—")}</code>`], ["Carrier", esc(c.telephony_provider || "—")], ["Trace id", `<code>${esc(c.trace_id || "—")}</code>`], ["Attempt id", `<code>${esc(c.id ?? id)}</code>`], d.usage ? ["LLM requests", esc(d.usage.llm_requests ?? "—")] : null, d.usage ? ["Tokens", `${esc(d.usage.prompt_tokens ?? "—")} in / ${esc(d.usage.completion_tokens ?? "—")} out`] : null, d.usage ? ["Speech characters", esc(d.usage.tts_characters ?? "—")] : null, d.usage ? ["Estimated cost", d.usage.cost_usd != null ? `$${Number(d.usage.cost_usd).toFixed(4)}` : "no rates configured"] : null, d.quality ? ["Response latency", `p50 ${esc(d.quality.p50_ms ?? "—")} ms · p95 ${esc(d.quality.p95_ms ?? "—")} ms`] : null, d.quality ? ["Interruptions", esc(d.quality.barge_ins ?? "—")] : null, d.quality ? ["Failed turns", esc(d.quality.failed_turns ?? "—")] : null, r.final_state ? ["Final conversation state", esc(human(r.final_state))] : null, r.issues?.length ? ["Issues", esc(r.issues.map((i) => (typeof i === "string" ? i : i.detail || i.kind || JSON.stringify(i))).join("; "))] : null])}</div></details>
        </div>
      </div>`;
  }

  // -------------------------------------------------------------- live agent
  // The voice client itself is the bot's own page, embedded: connecting, the
  // microphone and the conversation happen inside it. This page frames it,
  // says what the agent is running with, and offers a test call to a phone.
  async function pageLive(view) {
    // Behind the application (APP_PROXY_BOT) the bot's client is on this origin.
    const bot = state.config?.bot_proxied ? location.origin : (state.config?.bot_url || "http://127.0.0.1:7860");
    const campaigns = (await api.get(`${API}/campaigns?limit=200`).catch(() => ({ campaigns: [] }))).campaigns || [];
    const s = state.config?.sales || {};
    const pr = state.config?.providers || {};
    const t = state.config?.telephony || {};
    const src = `${bot}/client`;
    view.innerHTML = head(`<span class="ai-ring" aria-hidden="true">${icon("sparkle")}</span> Live AI Agent`, "Talk to the agent from your browser, exactly as a contact hears it on the phone.", `<a class="btn ghost" href="${esc(src)}" target="_blank" rel="noopener">${icon("external")} Open in a new tab</a>`, { eyebrow: "AI agent" }) +
      `<div class="grid main-side">
        <div class="stack">
          ${card("Voice client", `<iframe class="frame" id="frame" src="${esc(src)}/?theme=${currentTheme()}" allow="microphone; autoplay" title="Live AI agent voice client"></iframe>`, `<span class="pill-live"><span class="dot accent"></span> Embedded from the agent</span><button class="btn sm ghost" type="button" id="reload">${icon("refresh")} Reload</button>`, { flush: true }).replace('class="card"', 'class="card glow"')}
          ${alert("info", `Press <b>Connect</b> in the client and allow the microphone. The client shows its own connection and speaking state. If it does not load, the agent process is not running.`)}
        </div>
        <div class="stack">
          ${card("Agent profile in use", `<p class="muted small" style="margin-bottom:10px">A browser session uses the deployment's default profile, not a campaign's.</p>` + kv([["Agent", esc(s.agent_name || "—")], ["Company", esc(s.company_name || "—")], ["Offer", esc(s.offer || "—")], ["Meeting ask", esc(s.meeting_ask || "—")], s.value_points?.length ? ["Value points", `<ul class="list-plain">${s.value_points.map((v) => `<li>${esc(v)}</li>`).join("")}</ul>`] : null]))}
          ${card("Voice pipeline", `<div class="pipeline">${[["mic", "Speech to text", pr.stt], ["cpu", "Language model", pr.llm], ["volume", "Text to speech", pr.tts]].map(([ic, k, v], i) => `${i ? `<div class="arrow" aria-hidden="true">↓</div>` : ""}<div class="node"><span class="n">${icon(ic)}</span><div><div class="k">${esc(k)}</div><div class="v">${esc(v || "—")}</div></div></div>`).join("")}<div class="arrow" aria-hidden="true">↓</div><div class="node"><span class="n">${icon("knowledge")}</span><div><div class="k">Knowledge base</div><div class="v">${state.config?.knowledge_base ? badge("yes") : badge("no")}</div></div></div></div>`)}
          ${can("write") ? card("Test call to a phone", `<form id="f"><div class="row one">${field("Phone number", inp("phone", "", 'required type="tel" placeholder="+92 300 1234567"'), "Include the country code.", { required: true })}</div><div class="row one">${field("Campaign", select("campaign_id", campaigns.filter((c) => c.status === "ACTIVE" || c.status === "DRAFT" || c.status === "PAUSED").map((c) => [c.id, `${c.name} (${statusLabel(c.status)})`])), "The call uses this campaign's agent profile and is placed by the dialler while the campaign is running.")}</div><div class="form-actions"><button class="btn primary" type="submit" ${campaigns.length && t.configured ? "" : "disabled"}>${icon("calls")} Queue test call</button></div>${t.configured ? "" : `<p class="muted small">No outbound carrier is configured, so a phone call cannot be placed.</p>`}${campaigns.length ? "" : `<p class="muted small">Create a campaign first.</p>`}</form>`) : ""}
          <details class="panel"><summary>Diagnostics</summary><div class="card-body">${kv([["Client address", `<code>${esc(src)}</code>`], ["Carrier", esc(t.provider || "—")], ["Caller ID", esc(t.from_number || "Not set")]])}</div></details>
        </div>
      </div>`;
    $("#reload", view).onclick = () => { const f = $("#frame", view); f.src = `${src}/?theme=${currentTheme()}`; };
    const f = $("#f", view);
    if (f) f.onsubmit = (e) => { e.preventDefault(); withButton($("button[type=submit]", f), async () => { try { const r = await api.post(`${API}/calls`, { phone: f.phone.value, campaign_id: Number(f.campaign_id.value) }); ok(r.callback ? `Test call queued for ${fmtDate(r.callback.scheduled_for)}.` : "Test call queued; the dialler places it when the campaign is running."); f.reset(); } catch (err) { fail(err, "queue the test call"); } }, "Queuing…"); };
  }

  // ------------------------------------------------------------- knowledge
  // What the agent knows: the documents indexed for it, and a way to ask.
  async function pageKnowledge(view) {
    let data;
    try { data = await api.get(`${APP}/knowledge`); } catch (e) {
      if (e.status === 503) { view.innerHTML = head("Knowledge Base") + card("", emptyState("knowledge", "The knowledge base is turned off", esc(e.message))); return; }
      throw e;
    }
    const docs = data.documents || [];
    const types = (data.supported || []).map((s) => s.replace(/^\./, "").toUpperCase()).join(", ");
    const uploadZone = can("write") ? `<label class="dropzone" id="drop" for="up"><span class="drop-icon" aria-hidden="true">${icon("upload")}</span><b>Drop a document here, or choose one</b><span class="muted small">${types ? `${types} files.` : ""} The text is split into passages and indexed so the agent can answer from it.</span><span class="btn primary" style="margin-top:12px">${icon("plus")} Add document</span><input type="file" id="up" accept="${esc(data.supported.join(","))}" aria-label="Document"></label>` : "";
    view.innerHTML = head("Knowledge Base", `${plural(data.counts.documents, "document")} · ${plural(data.counts.chunks, "passage")} the agent can answer from`, "", { eyebrow: "AI agent" }) +
      `<div class="grid main-side">
        <div class="stack">
          ${card("Documents", docs.length ? table([
            { label: "Document", primary: true, render: (d) => `<b>${esc(d.title)}</b><div class="muted">${esc(d.source)}</div>` },
            { label: "Status", render: () => badge("indexed") },
            { label: "Passages", num: true, key: "chunk_count" },
            { label: "Size", num: true, render: (d) => esc(`${Math.max(1, Math.round((d.byte_size || 0) / 1024))} KB`) },
            { label: "Added", render: (d) => `<span class="nowrap">${esc(fmtDate(d.ingested_at))}</span>` },
            { label: "", actions: true, render: (d) => (can("write") ? `<button class="btn sm ghost danger" type="button" data-del="${esc(d.source)}" data-title="${esc(d.title)}">${icon("trash")} Remove</button>` : "") },
          ], docs) : emptyState("knowledge", "The agent has no documents yet", "Add pricing sheets, product descriptions or FAQs, and the agent answers questions from them on every call.", "", true), "", { flush: true, sub: docs.length ? `Indexed with ${esc(data.embedding_model)}` : "" })}
          ${uploadZone ? card("Add knowledge", uploadZone + `<div id="upload-state"></div>`) : ""}
        </div>
        <div class="stack">
          ${card("Ask a test question", `<p class="muted small" style="margin-bottom:10px">See what the agent would find for a question a contact might ask.</p><form id="q"><div class="row one">${field("Question", inp("query", "", 'placeholder="How much does it cost per vehicle?" required'))}</div><div class="form-actions"><button class="btn primary" type="submit" ${docs.length ? "" : "disabled"}>${icon("search")} Search</button></div></form><div id="hits"></div>`)}
        </div>
      </div>`;
    const up = $("#up", view);
    if (up) {
      const handle = async (file) => {
        if (!file) return;
        const box = $("#upload-state", view);
        box.innerHTML = `<div class="loading" style="padding:14px 0 0"><span class="spinner"></span> Processing ${esc(file.name)} — reading, splitting into passages and indexing…</div>`;
        const body = new FormData();
        body.append("file", file);
        try { const r = await api.post(`${APP}/knowledge/documents`, body); ok(`${r.title} added: ${plural(r.chunks, "passage")} indexed and available to the agent.`); route(true); } catch (err) { box.innerHTML = errorBox(err, { doing: "add the document" }); }
      };
      up.onchange = (e) => handle(e.target.files[0]);
      const drop = $("#drop", view);
      drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
      drop.ondragleave = () => drop.classList.remove("over");
      drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); handle(e.dataTransfer.files[0]); };
    }
    $$("[data-del]", view).forEach((b) => (b.onclick = async () => { if (!(await confirm(`Remove "${b.dataset.title}"?`, `Every passage from <b>${esc(b.dataset.title)}</b> is removed from the index. The agent will no longer answer from it, from the next call on. The original file is not deleted from your computer.`, { danger: true, label: "Remove document" }))) return; try { await api.del(`${APP}/knowledge/documents/${encodeURIComponent(b.dataset.del)}`); ok(`${b.dataset.title} removed.`); route(true); } catch (e) { fail(e, "remove the document"); } }));
    $("#q", view).onsubmit = (e) => { e.preventDefault(); const hits = $("#hits", view); hits.innerHTML = loading("Searching…"); withButton($("button[type=submit]", e.target), async () => { try { const r = await api.post(`${APP}/knowledge/search`, { query: e.target.query.value }); hits.innerHTML = r.matches.length ? `<div class="section-title">Passages the agent would use</div>` + r.matches.map((m) => `<div class="callout" style="margin-bottom:10px"><div class="muted small">${esc(m.title || m.source)} · relevance ${esc(Math.round(Number(m.score) * 100))}%</div>${esc(m.content)}</div>`).join("") : emptyState("search", "Nothing relevant found", "The agent would say it does not have that information. Add a document that covers it.", "", true); } catch (err) { hits.innerHTML = errorBox(err, { doing: "search the knowledge base" }); } }, "Searching…"); };
  }

  // -------------------------------------------------------------- analytics
  async function pageAnalytics(view) {
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const q = Object.fromEntries(params.entries());
    const [snap, campaigns] = await Promise.all([api.get(`${DASH}/dashboard?${new URLSearchParams(q)}`), api.get(`${API}/campaigns?limit=200`)]);
    const totals = snap.totals || [], conv = snap.conversion || [], perf = snap.performance || [], errs = snap.errors || [];
    const calls = metric(totals, "calls")?.value ?? 0;
    const outcomes = snap.outcomes || [];
    const finished = outcomes.reduce((n, o) => n + o.count, 0);
    const share = (key) => { const o = outcomes.find((x) => x.key === key); return o ? `${o.share}%` : finished ? "0%" : null; };
    const rate = (label, value, detail, t = "") => `<div class="rate ${value == null ? "" : t}"><div class="v">${esc(value ?? "—")}</div><div class="k">${esc(label)}</div>${detail ? `<div class="d" title="${esc(detail)}">${esc(detail)}</div>` : ""}</div>`;
    const m = (list, key) => metric(list, key);
    const val = (x) => (x && x.available !== false ? x.value : null);
    const hasFilter = !!(q.campaign || q.from || q.to);
    view.innerHTML = head("Analytics", esc(snap.filters?.label || "All campaigns · all time"), "", { eyebrow: "Insights" }) +
      `<form class="filters" id="f">${field("Campaign", select("campaign", [["", "All campaigns"], ...(campaigns.campaigns || []).map((c) => [c.id, c.name])], q.campaign))}${field("From", inp("from", q.from || "", 'type="date"'))}${field("To", inp("to", q.to || "", 'type="date"'))}<button class="btn" type="submit">Apply</button>${hasFilter ? `<a class="btn ghost" href="#/analytics">Clear</a>` : ""}</form>` +
      (!calls ? card("", emptyState("analytics", "No analytics yet", hasFilter ? "No calls match these filters." : "Analytics will appear after your first campaign places calls.")) :
      `<div class="grid stats">
        ${metricStat(m(totals, "calls"), "Total calls")}
        ${metricStat(m(totals, "completed"), "Completed calls")}
        ${metricStat(m(totals, "answered"), "Answered")}
        ${metricStat(m(totals, "qualified"), "Qualified leads")}
        ${metricStat(m(totals, "meetings"), "Meetings booked")}
        ${metricStat(m(totals, "failed"), "Failed calls")}
      </div>
      <div class="section-title">Rates</div>
      ${card("", `<div class="rates">
        ${rate("Answer rate", val(m(perf, "answer_rate")), m(perf, "answer_rate")?.detail)}
        ${rate("Qualification rate", val(m(conv, "qualification_rate")), m(conv, "qualification_rate")?.detail, "good")}
        ${rate("Meeting rate", val(m(conv, "meeting_rate")), m(conv, "meeting_rate")?.detail, "good")}
        ${rate("Callback rate", val(m(conv, "callback_rate")), m(conv, "callback_rate")?.detail)}
        ${rate("No-answer rate", share("NO_ANSWER"), finished ? `of ${finished} finished calls` : "", "warn")}
        ${rate("Busy rate", share("BUSY"), finished ? `of ${finished} finished calls` : "", "warn")}
        ${rate("Voicemail rate", val(m(perf, "voicemail_rate")), m(perf, "voicemail_rate")?.detail, "warn")}
        ${rate("Failure rate", share("FAILED"), finished ? `of ${finished} finished calls` : "", "bad")}
        ${rate("Not interested", val(m(conv, "not_interested_rate")), m(conv, "not_interested_rate")?.detail, "bad")}
        ${rate("Average call length", val(m(perf, "average_duration")), m(perf, "average_duration")?.detail)}
        ${m(perf, "response_latency") ? rate("Agent response time", val(m(perf, "response_latency")), m(perf, "response_latency")?.detail) : ""}
      </div>`)}
      <div class="section-title">Outcomes and campaigns</div>
      <div class="grid two">
        ${card("Call outcomes", outcomes.length ? donut(outcomes.map((o) => [statusLabel(o.key), o.count, o.share, tone(o.tone) || "neutral"]), { label: "finished calls" }) : emptyState("analytics", "No outcomes yet", "", "", true), "", { sub: finished ? `${plural(finished, "finished call")}` : "" })}
        ${card("Problems", errs.length ? `<div class="rates">${errs.map((x) => rate(x.label, x.available === false ? null : x.value, x.detail, x.tone === "bad" ? "bad" : x.tone === "warn" ? "warn" : "")).join("")}</div>` : `<p class="muted">Nothing to report.</p>`)}
      </div>
      <div class="section-title">Campaign performance</div>
      ${card("", table([
        { label: "Campaign", primary: true, render: (c) => `<a href="#/campaigns/${c.id}">${esc(c.name)}</a>` }, { label: "Status", render: (c) => badge(c.status === "ACTIVE" ? "RUNNING" : c.status) },
        { label: "Contacts", num: true, key: "prospects" }, { label: "Calls", num: true, key: "attempts" }, { label: "Answered", num: true, key: "answered" },
        { label: "Answer rate", num: true, render: (c) => (c.answer_rate != null ? `${esc(c.answer_rate)}%` : "—") },
        { label: "Qualified", num: true, key: "qualified" }, { label: "Meetings", num: true, key: "meetings" }, { label: "Voicemail", num: true, key: "voicemail" }, { label: "Failed", num: true, key: "failed" },
        { label: "Progress", render: (c) => `${progressBar([[c.progress_pct || 0, ""]], { title: `${c.progress_pct || 0}%` })}<div class="muted">${esc(c.progress_pct ?? 0)}%</div>` },
      ], snap.campaigns, { emptyHtml: emptyState("campaigns", "No campaigns in this view", "", "", true) }), "", { flush: true })}
      ${(snap.compliance || []).length || (snap.progress || []).length ? `<details class="panel" style="margin-top:14px"><summary>More metrics</summary><div class="card-body"><div class="grid stats compact">${[...(snap.compliance || []), ...(snap.progress || [])].map((x) => stat(x.label, x.available === false ? "n/a" : x.value, x.detail, x.tone, "compact")).join("")}</div></div></details>` : ""}`);
    $("#f", view).onsubmit = (e) => { e.preventDefault(); const f = Object.fromEntries(Object.entries(formData(e.target)).filter(([, v]) => v)); location.hash = `#/analytics${Object.keys(f).length ? `?${new URLSearchParams(f)}` : ""}`; };
  }

  // --------------------------------------------------------------- settings
  // Grouped for a person: profile, calling, the AI agent, automation, system.
  // Values come from the environment; secrets are never sent to the page.
  async function pageSettings(view) {
    const [cfg, status] = await Promise.all([api.get(`${APP}/config`), api.get(`${API}/status`).catch(() => null)]);
    state.config = cfg;
    const p = state.principal;
    const workers = status?.scheduler?.workers;
    const queue = status?.scheduler?.queue;
    const yn = (v) => (typeof v === "boolean" ? badge(v ? "yes" : "no") : v == null || v === "" ? '<span class="muted">—</span>' : esc(String(v)));
    const listOrDash = (v) => (Array.isArray(v) ? (v.length ? v.map(esc).join(", ") : '<span class="muted">—</span>') : yn(v));
    const auto = (obj) => (obj && typeof obj === "object" ? kv(Object.entries(obj).map(([k, v]) => [human(k), listOrDash(v)])) : `<p>${yn(obj)}</p>`);
    const engine = state.engine;
    const [et, eword, ewhy] = engine ? ENGINE[engine.state] || ["neutral", human(engine.state), () => ""] : ["neutral", "Unknown", () => ""];
    view.innerHTML = head("Settings", "How this deployment is configured. Values are read from the environment when the application starts; secrets are never shown here.", "", { eyebrow: "System" }) +
      `<div class="section-title">Profile</div>
      ${card("Your account", facts([["Name", `<b>${esc(p.name)}</b>`], ["Role", badge(p.role, "info")], ["Signed in via", esc(p.via)], ["Permissions", `<div class="chips">${p.permissions.map((x) => `<span class="chip">${esc(human(x))}</span>`).join("")}</div>`]]), `<button class="btn sm" type="button" id="signout">${icon("logout")} Sign out</button>`)}
      <div class="section-title">Calling</div>
      <div class="grid two">
        ${card("Telephony", facts([["Provider", esc(cfg.telephony.provider || "—")], ["Caller ID", esc(cfg.telephony.from_number || "Not set")], ["Status", cfg.telephony.configured ? badge("configured") : badge("not configured")], ["Credentials", yn(cfg.telephony.has_credentials)], ["Public address", esc(cfg.telephony.public_url || "—")], ["Transfer number", yn(cfg.telephony.transfer_number_set)], ["Machine detection", yn(cfg.telephony.machine_detection)], ["Webhooks", esc(cfg.telephony.webhooks || "—")]]))}
        ${card("Calling limits", facts([["Concurrency", esc(cfg.calling.describe || "—")], ["Max attempts per contact", esc(cfg.calling.max_attempts)], ["Retry after", cfg.calling.retry_minutes != null ? `${esc(cfg.calling.retry_minutes)} min` : ""], ["Default phone region", esc(cfg.calling.default_phone_region || "—")], ["Dialler", esc(cfg.calling.worker || "—")]]))}
      </div>
      <div class="section-title">AI agent</div>
      <div class="grid two">
        ${card("Default agent profile", `<p class="muted small" style="margin-bottom:10px">Used by every campaign that does not set its own.</p>` + kv([["Enabled", yn(cfg.sales.enabled)], ["Agent name", esc(cfg.sales.agent_name)], ["Company", esc(cfg.sales.company_name)], ["Offer", esc(cfg.sales.offer)], ["Meeting ask", esc(cfg.sales.meeting_ask)], ["Value points", listOrDash(cfg.sales.value_points)], ["Qualification criteria", listOrDash(cfg.sales.qualification_criteria)], ["Notes", listOrDash(cfg.sales.notes)]]))}
        ${card("Voice pipeline", kv([["Speech to text", esc(cfg.providers.stt)], ["Language model", esc(cfg.providers.llm)], ["Text to speech", esc(cfg.providers.tts)], ["Embeddings", esc(cfg.providers.embedding)], ["Knowledge base", yn(cfg.knowledge_base)]]))}
      </div>
      <div class="section-title">Automation</div>
      <div class="grid two">
        ${card("Calendar and CRM", kv([["Calendar", auto(cfg.calendar)], ["CRM", auto(cfg.crm)]]))}
        ${card("Workflow delivery", kv([["Delivery", yn(cfg.automation.delivery)], ["Targets", listOrDash(cfg.automation.targets)], ["Signed", yn(cfg.automation.signed)], ["API keys configured", yn(cfg.automation.api_keys)]]))}
      </div>
      <div class="section-title">System</div>
      <div class="grid two">
        ${card("Dialler", `<div class="status-hero" style="padding:0 0 12px"><span class="big ${et}">${icon("bolt")}</span><div><h3>${esc(eword)}</h3><p>${esc(engine ? ewhy(engine) : "Status not loaded")}</p></div></div>` + (workers ? facts([["Schedulers alive", esc(workers.alive)], ["Calls in progress", esc(workers.in_flight)], ["Due now", esc(queue?.due_now)], ["Scheduled later", esc(queue?.scheduled)], ["Callbacks due", esc(queue?.callbacks_due)], ["Active campaigns", esc(queue?.active_campaigns)]]) : `<p class="muted">Scheduler information is not available.</p>`))}
        ${card("Security and compliance", kv([["Security", auto(cfg.security)], ["Compliance", auto(cfg.compliance)], ["Monitoring", auto(cfg.monitoring)]]))}
      </div>
      <div class="section-title">Advanced</div>
      ${card("Health check", `<p class="muted">Checks the database, the AI providers' keys, the carrier, the calendar and the CRM with cheap authenticated reads. Takes a few seconds and places no call.</p><div id="health"></div>`, can("write") ? `<button class="btn" type="button" id="run-health">${icon("refresh")} Run health check</button>` : "")}
      ${can("manage") ? card("Sign-up requests", `<div id="signups">${skelLines(3)}</div>`, "", { flush: true, sub: "Operator and Admin accounts requested on the Register page. Approving gives exactly the role that was asked for.", id: "signups-card" }) : ""}
      ${can("manage") ? card("Audit log", `<div id="audit">${skelLines(4)}</div>`, "", { flush: true, sub: "The latest 30 actions" }) : ""}`;
    $("#signout", view).onclick = () => $("#logout").click();
    if (can("manage")) {
      const box = $("#signups", view);
      const loadSignups = async () => {
        try {
          const r = await api.get(`${APP}/users/pending`);
          box.innerHTML = table([
            { label: "Name", primary: true, render: (u) => `<b>${esc(u.name)}</b>` },
            { label: "Email", key: "email" },
            { label: "Requested role", render: (u) => badge(u.role, "info") },
            { label: "Requested", render: (u) => `<span class="nowrap">${esc(fmtDate(u.created_at))}</span>` },
            { label: "", actions: true, render: (u) => `<div class="btn-group"><button class="btn sm primary" type="button" data-approve="${u.id}">Approve as ${esc(human(u.role))}</button><button class="btn sm danger" type="button" data-reject="${u.id}">Reject</button></div>` },
          ], r.users, { emptyHtml: emptyState("user", "No requests waiting", "Operator and Admin sign-ups appear here until you decide on them.", "", true), cls: "compact" });
          $$("[data-approve]", box).forEach((b) => { b.onclick = async () => {
            const u = r.users.find((x) => String(x.id) === b.dataset.approve);
            if (!(await confirm("Approve this request?", `<b>${esc(u.name)}</b> (${esc(u.email)}) will become an active <b>${esc(human(u.role))}</b> and can sign in at once.`, { label: `Approve as ${human(u.role)}` }))) return;
            await withButton(b, async () => { try { await api.post(`${APP}/users/${u.id}/approve`); ok(`${u.name} is now ${human(u.role) === "Admin" ? "an" : "a"} ${human(u.role)}`); await loadSignups(); } catch (e) { fail(e, "approve the request"); } }, "Approving…");
          }; });
          $$("[data-reject]", box).forEach((b) => { b.onclick = async () => {
            const u = r.users.find((x) => String(x.id) === b.dataset.reject);
            if (!(await confirm("Reject this request?", `The request from <b>${esc(u.name)}</b> (${esc(u.email)}) will be removed. They can sign up again as a Viewer.`, { danger: true, label: "Reject" }))) return;
            await withButton(b, async () => { try { await api.post(`${APP}/users/${u.id}/reject`); ok(`Request from ${u.name} rejected`); await loadSignups(); } catch (e) { fail(e, "reject the request"); } }, "Rejecting…");
          }; });
        } catch (e) { box.innerHTML = errorBox(e, { doing: "load the sign-up requests" }); }
      };
      loadSignups();
    }
    const hb = $("#run-health", view);
    if (hb) hb.onclick = () => withButton(hb, async () => { const box = $("#health", view); box.innerHTML = loading("Checking every component…"); try { const r = await api.post(`${APP}/health`); const bad = (r.components || []).filter((c) => c.status === "failed").length; box.innerHTML = (bad ? alert("bad", `<b>${plural(bad, "component")} failed.</b> The details below say what to fix.`) : alert("good", `<b>Everything checks out.</b>`)) + table([{ label: "Component", primary: true, render: (c) => `<b>${esc(human(c.name))}</b>` }, { label: "Status", render: (c) => badge(c.status) }, { label: "Detail", key: "detail" }, { label: "Time", num: true, render: (c) => (c.latency_ms != null ? `${esc(c.latency_ms)} ms` : "—") }], r.components, { cls: "compact" }); } catch (e) { box.innerHTML = errorBox(e, { doing: "run the health check" }); } }, "Checking…");
    if (can("manage")) api.get(`${API}/audit?limit=30`).then((r) => { $("#audit", view).innerHTML = table([{ label: "When", render: (e) => `<span class="nowrap">${esc(fmtDate(e.created_at))}</span>` }, { label: "Action", render: (e) => `<code>${esc(e.action)}</code>` }, { label: "Who", render: (e) => `${esc(e.actor)} <span class="muted">(${esc(e.role)})</span>` }, { label: "Outcome", render: (e) => esc(e.outcome || "—") }, { label: "Target", render: (e) => `<span class="muted">${esc(e.target_kind ? `${human(e.target_kind)} ${e.target_id}` : "")}</span>` }], r.entries, { emptyHtml: emptyState("shield", "Nothing audited yet", "", "", true), cls: "compact" }); }).catch((e) => ($("#audit", view).innerHTML = errorBox(e, { doing: "load the audit log" })));
  }

  // ------------------------------------------------------------------- boot
  (async () => {
    await loadSession();
    renderShell();
  })();
})();
