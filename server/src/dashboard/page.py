"""The dashboard pages: HTML documents, no build step, no network. Phase 10; 18; 20.

**Why a single static shell rendered by JavaScript, rather than server-rendered
HTML.** The JSON API has to exist anyway — it is the reusable half, and the
thing any other tool would read — so rendering the page from it means there is
exactly *one* renderer. Server-rendering the same numbers as well would be two
descriptions of the same data to keep in step, which is the duplication this
phase was told to avoid.

**No CDN, no framework, no bundler.** Everything is inline. The project already
runs offline apart from its vendors, `uv sync` is the only install step there
is, and a dashboard that needs a network round trip to a CDN before it can draw
is a dashboard that fails in exactly the situation you opened it for. The cost
is that this file contains CSS and JavaScript; the benefit is that
`uv run dashboard.py` is the whole setup.

**It reads and nothing else.** The only forms are the login and the logout —
see `web.py`. Pointing a browser at a live calling system cannot change it.

**Phase 20.** The page grew a filter bar (campaign, date range, search),
five more strips (progress, conversion, performance, errors, compliance),
a calls list that pages and searches, and a second document — the call
detail page — that shows one call in full, with the transcript only when
the JSON route sent one (a viewer's did not, and the page says so).
"""

from __future__ import annotations

_STYLE = """
  :root {{
    color-scheme: light dark;
    --bg: #f6f7f9;
    --card: #ffffff;
    --ink: #16181d;
    --muted: #626875;
    --line: #e3e6ec;
    --good: #0f7b4f;
    --bad: #b3261e;
    --warn: #8a5a00;
    --accent: #2b4a8b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #14161a;
      --card: #1c1f25;
      --ink: #e9ebef;
      --muted: #9aa1ae;
      --line: #2c313a;
      --good: #5fd2a0;
      --bad: #ff8a80;
      --warn: #e8b962;
      --accent: #9db8ec;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px;
    background: var(--bg); color: var(--ink);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  header {{
    display: flex; flex-wrap: wrap; align-items: baseline;
    gap: 12px; margin-bottom: 20px;
  }}
  h1 {{ font-size: 20px; margin: 0; font-weight: 650; }}
  h1 a {{ color: inherit; text-decoration: none; }}
  h2 {{ font-size: 14px; margin: 28px 0 10px; font-weight: 650;
       text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }}
  .stamp {{ color: var(--muted); font-size: 13px; }}
  .stamp b {{ color: var(--ink); font-weight: 600; }}
  .grid {{
    display: grid; gap: 12px;
    grid-template-columns: repeat(auto-fill, minmax(215px, 1fr));
  }}
  .card {{
    background: var(--card); border: 1px solid var(--line);
    border-radius: 10px; padding: 14px 16px;
  }}
  .card .label {{ font-size: 13px; color: var(--muted); }}
  .card .value {{ font-size: 30px; font-weight: 640; margin: 4px 0 2px;
                  font-variant-numeric: tabular-nums; letter-spacing: -.02em; }}
  .card .detail {{ font-size: 12px; color: var(--muted); }}
  .value.good {{ color: var(--good); }}
  .value.bad  {{ color: var(--bad); }}
  .value.warn {{ color: var(--warn); }}
  .value.na   {{ color: var(--muted); font-size: 20px; font-weight: 500; }}
  .attention {{ margin-top: 12px; }}
  .attention .card {{ border-left: 3px solid var(--warn); }}
  table {{ width: 100%; border-collapse: collapse; background: var(--card);
           border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line);
            font-size: 13px; vertical-align: top; }}
  th {{ font-weight: 600; color: var(--muted); font-size: 12px;
        text-transform: uppercase; letter-spacing: .04em; }}
  tr:last-child td {{ border-bottom: none; }}
  tr.row {{ cursor: pointer; }}
  tr.row:hover td {{ background: color-mix(in srgb, var(--accent) 8%, transparent); }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  th.num {{ text-align: right; }}
  .pill {{ display: inline-block; padding: 1px 8px; border-radius: 999px;
           font-size: 12px; font-weight: 600; border: 1px solid var(--line); }}
  .pill.good {{ color: var(--good); border-color: color-mix(in srgb, var(--good) 40%, transparent); }}
  .pill.bad  {{ color: var(--bad);  border-color: color-mix(in srgb, var(--bad) 40%, transparent); }}
  .pill.warn {{ color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, transparent); }}
  .muted {{ color: var(--muted); }}
  .headline {{ color: var(--muted); font-size: 12px; margin-top: 2px;
               max-width: 62ch; overflow-wrap: anywhere; }}
  .bar {{ height: 6px; border-radius: 3px; background: var(--line);
          overflow: hidden; margin-top: 6px; }}
  .bar span {{ display: block; height: 100%; background: var(--accent); }}
  .bar span.good {{ background: var(--good); }}
  .bar span.bad  {{ background: var(--bad); }}
  .bar span.warn {{ background: var(--warn); }}
  .note {{ background: var(--card); border: 1px solid var(--line);
           border-left: 3px solid var(--warn); border-radius: 8px;
           padding: 10px 14px; margin-bottom: 10px; font-size: 13px; }}
  .empty {{ color: var(--muted); padding: 14px 0; font-size: 13px; }}
  code {{ font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12px; }}
  footer {{ margin-top: 28px; color: var(--muted); font-size: 12px; }}
  .who {{ margin-left: auto; color: var(--muted); font-size: 13px;
          display: flex; align-items: baseline; gap: 10px; }}
  .who b {{ color: var(--ink); font-weight: 600; }}
  .who form {{ display: inline; margin: 0; }}
  .who button, .filters button, .more button {{
    font: inherit; font-size: 12px; padding: 3px 10px; border-radius: 999px;
    border: 1px solid var(--line); background: var(--card); color: var(--ink); cursor: pointer; }}
  .who button:hover, .filters button:hover, .more button:hover {{ border-color: var(--accent); }}
  .filters {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
              background: var(--card); border: 1px solid var(--line); border-radius: 10px;
              padding: 10px 12px; margin-bottom: 16px; font-size: 13px; }}
  .filters label {{ color: var(--muted); }}
  .filters select, .filters input {{ font: inherit; font-size: 13px; padding: 4px 8px;
      border-radius: 6px; border: 1px solid var(--line); background: var(--bg); color: var(--ink); }}
  .filters input[type=search] {{ min-width: 220px; }}
  .filters .scope {{ margin-left: auto; color: var(--muted); }}
  .more {{ margin-top: 8px; }}
  .kv {{ display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; font-size: 13px; }}
  .kv dt {{ color: var(--muted); }}
  .kv dd {{ margin: 0; overflow-wrap: anywhere; }}
  .transcript {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px;
                 padding: 8px 14px; max-height: 60vh; overflow: auto; }}
  .turn {{ padding: 6px 0; border-bottom: 1px solid var(--line); font-size: 14px; }}
  .turn:last-child {{ border-bottom: none; }}
  .turn .role {{ font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
                 color: var(--muted); margin-right: 8px; }}
  .turn.agent .role {{ color: var(--accent); }}
  .turn .at {{ float: right; color: var(--muted); font-size: 11px; }}
  .turn.interrupted {{ opacity: .75; }}
  .two {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }}
"""

#: The dashboard, as one document. `{api}` and friends are filled in with the
#: routes so the page and the app cannot disagree about where the data is.
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Calling dashboard</title>
<style>""" + _STYLE + """</style>
</head>
<body>
<header>
  <h1><a href="/">Calling dashboard</a></h1>
  <span class="stamp" id="stamp">loading…</span>
  <span class="who">{who}</span>
</header>

<noscript>
  <div class="note">
    This page renders in the browser from <code>{api}</code>, so it needs
    JavaScript. Without it, read that endpoint directly, or use
    <code>uv run campaign.py status</code> and
    <code>uv run campaign.py results</code>.
  </div>
</noscript>

<form class="filters" id="filters" onsubmit="return false">
  <label for="campaign">Campaign</label>
  <select id="campaign" name="campaign"><option value="">All campaigns</option></select>
  <label for="from">From</label>
  <input id="from" name="from" type="date">
  <label for="to">To</label>
  <input id="to" name="to" type="date">
  <input id="q" name="q" type="search" placeholder="Search name, company, email, call id{search_hint}">
  <button type="button" id="apply">Apply</button>
  <button type="button" id="clear">Clear</button>
  <span class="scope" id="scope"></span>
</form>

<div id="notes"></div>
<div class="grid" id="totals"></div>
<div class="grid attention" id="attention"></div>

<h2>Progress</h2>
<div class="grid" id="progress"></div>

<h2>Conversion</h2>
<div class="grid" id="conversion"></div>

<h2>Performance</h2>
<div class="grid" id="performance"></div>

<h2>Usage and cost</h2>
<div class="grid" id="usage"></div>

<h2>Errors and failures</h2>
<div class="grid" id="errors"></div>

<h2>Do-not-call and opt-outs</h2>
<div class="grid" id="compliance"></div>

<h2>Workers and queue</h2>
<div class="grid" id="scheduler"></div>

<h2>Call outcomes</h2>
<div id="outcomes"></div>

<h2>Campaigns</h2>
<div id="campaigns"></div>

<h2 id="calls-heading">Recent calls</h2>
<div id="recent"></div>
<div class="more" id="more"></div>

<footer>
  Read-only. Refreshes every {refresh} seconds; the data is
  <code>{api}</code> and <code>{calls_api}</code>. Times are in <span id="zone">the campaign timezone</span>.
</footer>

<script>
const API = "{api}";
const CALLS_API = "{calls_api}";
const CAMPAIGNS_API = "{campaigns_api}";
const CALL_PAGE = "{call_page}";
const REFRESH_MS = {refresh} * 1000;

// One escape for every string that reaches the page. Prospect names, company
// names and carrier error messages are all data from outside this system, and
// a dashboard is not the place to find out that one of them contained a tag.
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => (
  {{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }}[c]
));

const el = (id) => document.getElementById(id);

// The view: read from the URL on load, written back on apply, so a filtered
// dashboard is a link somebody can send.
const state = {{ campaign: "", from: "", to: "", q: "", before: null }};
function readState() {{
  const p = new URLSearchParams(location.search);
  state.campaign = p.get("campaign") || ""; state.from = p.get("from") || "";
  state.to = p.get("to") || ""; state.q = p.get("q") || "";
  el("campaign").value = state.campaign; el("from").value = state.from;
  el("to").value = state.to; el("q").value = state.q;
}}
function writeState() {{
  const p = new URLSearchParams();
  for (const k of ["campaign", "from", "to", "q"]) if (state[k]) p.set(k, state[k]);
  const qs = p.toString();
  history.replaceState(null, "", qs ? "?" + qs : location.pathname);
}}
function query(extra) {{
  const p = new URLSearchParams();
  for (const k of ["campaign", "from", "to"]) if (state[k]) p.set(k, state[k]);
  for (const [k, v] of Object.entries(extra || {{}})) if (v !== null && v !== undefined && v !== "") p.set(k, v);
  const qs = p.toString();
  return qs ? "?" + qs : "";
}}

function card(metric) {{
  const shown = metric.available ? esc(metric.value ?? "—") : "unavailable";
  const tone = metric.available ? esc(metric.tone) : "na";
  return `<div class="card">
    <div class="label">${{esc(metric.label)}}</div>
    <div class="value ${{tone}}">${{shown}}</div>
    <div class="detail">${{esc(metric.detail)}}</div>
  </div>`;
}}

function outcomes(rows) {{
  if (!rows.length) return `<div class="empty">No finished calls yet.</div>`;
  const source = rows[0].source === "disposition"
    ? "" : `<div class="empty">From attempt statuses: this database has no call results.</div>`;
  return source + `<table><thead><tr>
      <th>Outcome</th><th class="num">Calls</th><th class="num">Share</th><th></th>
    </tr></thead><tbody>` + rows.map((row) => `<tr>
      <td><span class="pill ${{esc(row.tone)}}">${{esc(row.label)}}</span></td>
      <td class="num">${{esc(row.count)}}</td>
      <td class="num">${{esc(row.share)}}%</td>
      <td style="width:40%"><div class="bar">
        <span class="${{esc(row.tone)}}" style="width:${{Number(row.share) || 0}}%"></span>
      </div></td>
    </tr>`).join("") + `</tbody></table>`;
}}

function campaigns(rows) {{
  if (!rows.length) return `<div class="empty">No campaigns yet. Create one with
    <code>uv run campaign.py create "Q1 Outreach"</code>.</div>`;
  const results = rows.some((row) => row.results_available);
  return `<table><thead><tr>
      <th>Campaign</th><th>Status</th><th>Progress</th>
      <th class="num">Contacts</th><th class="num">Remaining</th>
      <th class="num">Calls</th><th class="num">Answered</th>
      <th class="num">Answer rate</th><th class="num">Avg duration</th>
      ${{results ? `<th class="num">Qualified</th><th class="num">Meetings</th><th class="num">Opted out</th>` : ``}}
    </tr></thead><tbody>` + rows.map((row) => `<tr>
      <td><b>${{esc(row.name)}}</b><div class="headline">created ${{esc(row.created_at_label)}}</div></td>
      <td><span class="pill">${{esc(row.status)}}</span></td>
      <td style="min-width:120px">${{row.progress_pct === null ? '<span class="muted">—</span>' : esc(row.progress_pct) + "%"}}
        <div class="bar"><span class="${{row.progress_pct === 100 ? "good" : ""}}" style="width:${{Number(row.progress_pct) || 0}}%"></span></div>
        <div class="headline">${{esc(row.completed_members)}} reached · ${{esc(row.exhausted)}} exhausted · ${{esc(row.skipped)}} skipped${{row.live ? " · " + esc(row.live) + " live" : ""}}</div></td>
      <td class="num">${{esc(row.prospects)}}</td>
      <td class="num">${{esc(row.remaining)}}</td>
      <td class="num">${{esc(row.attempts)}}</td>
      <td class="num">${{esc(row.answered)}}</td>
      <td class="num">${{row.answer_rate === null ? '<span class="muted">—</span>' : esc(row.answer_rate) + "%"}}</td>
      <td class="num">${{row.average_duration ? esc(row.average_duration) : '<span class="muted">—</span>'}}</td>
      ${{results ? `<td class="num">${{esc(row.qualified ?? "—")}}</td>
                    <td class="num">${{esc(row.meetings ?? "—")}}</td>
                    <td class="num">${{esc(row.opted_out ?? "—")}}</td>` : ``}}
    </tr>`).join("") + `</tbody></table>`;
}}

function calls(rows) {{
  if (!rows.length) return `<div class="empty">No calls${{state.q ? " match" : " yet"}}. Place one with
    <code>uv run campaign.py call "Q1 Outreach"</code>.</div>`;
  return `<table><thead><tr>
      <th>When</th><th>Prospect</th><th>Campaign</th>
      <th>Outcome</th><th>Qualification</th><th>Meeting</th><th>Callback</th>
      <th class="num">Duration</th><th class="num">Attempt</th>
    </tr></thead><tbody>` + rows.map((row) => `<tr class="row" data-id="${{esc(row.attempt_id)}}">
      <td class="muted">${{esc(row.at_label)}}</td>
      <td>
        <b>${{esc(row.prospect)}}</b>
        ${{row.company ? ` <span class="muted">· ${{esc(row.company)}}</span>` : ``}}
        <div class="headline">${{esc(row.phone)}}${{row.headline ? " — " + esc(row.headline) : ""}}</div>
      </td>
      <td>${{row.campaign ? esc(row.campaign) : '<span class="muted">no campaign</span>'}}</td>
      <td><span class="pill ${{esc(row.tone)}}">${{esc(row.disposition_label)}}</span></td>
      <td>${{row.qualification_label ? esc(row.qualification_label) : '<span class="muted">—</span>'}}</td>
      <td>${{row.meeting_status && row.meeting_status !== "UNKNOWN" ? esc(row.meeting_status) : '<span class="muted">—</span>'}}</td>
      <td>${{row.callback_status && row.callback_status !== "UNKNOWN" ? esc(row.callback_status) : '<span class="muted">—</span>'}}</td>
      <td class="num">${{row.duration ? esc(row.duration) : '<span class="muted">—</span>'}}</td>
      <td class="num">#${{esc(row.attempt_number)}}</td>
    </tr>`).join("") + `</tbody></table>`;
}}

let callRows = [];
function render(data) {{
  // The read time is shown because it is the number Phase 11 optimised, and a
  // dashboard that hides its own cost is the wrong place to learn that lesson.
  const read = data.read_ms === undefined ? "" : ` · read in ${{esc(data.read_ms)}} ms`;
  el("stamp").innerHTML =
    `updated <b>${{esc(data.generated_at_label)}}</b> · ${{esc(data.timezone)}}${{read}}${{data.masked ? " · numbers masked" : ""}}`;
  el("zone").textContent = data.timezone;
  el("scope").textContent = data.filters ? data.filters.label : "";
  el("notes").innerHTML = data.notes.map((n) => `<div class="note">${{esc(n)}}</div>`).join("");
  el("totals").innerHTML = data.totals.map(card).join("");
  el("attention").innerHTML = data.attention.map(card).join("");
  el("progress").innerHTML = (data.progress || []).map(card).join("");
  el("conversion").innerHTML = (data.conversion || []).map(card).join("");
  el("performance").innerHTML = (data.performance || []).map(card).join("");
  el("usage").innerHTML = (data.usage || []).map(card).join("");
  el("errors").innerHTML = (data.errors || []).map(card).join("");
  el("compliance").innerHTML = (data.compliance || []).map(card).join("");
  el("scheduler").innerHTML = (data.scheduler || []).map(card).join("");
  el("outcomes").innerHTML = outcomes(data.outcomes);
  el("campaigns").innerHTML = campaigns(data.campaigns);
}}

function renderCalls(data, append) {{
  callRows = append ? callRows.concat(data.calls) : data.calls;
  el("calls-heading").textContent = state.q ? `Calls matching “${{state.q}}”` : "Recent calls";
  el("recent").innerHTML = calls(callRows);
  el("more").innerHTML = data.next_before_id
    ? `<button type="button" id="more-btn">Show older calls</button>` : "";
  const btn = el("more-btn");
  if (btn) btn.addEventListener("click", () => loadCalls(data.next_before_id));
  for (const row of document.querySelectorAll("tr.row")) {{
    row.addEventListener("click", () => {{ location.href = `${{CALL_PAGE}}/${{row.dataset.id}}`; }});
  }}
}}

async function fetchJson(url) {{
  const response = await fetch(url, {{ cache: "no-store", credentials: "same-origin" }});
  if (response.status === 401) {{
    // The session ended (it expires on its own). Back to the login page,
    // and back here afterwards.
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    throw new Error("signed out");
  }}
  if (!response.ok) {{
    let message = `HTTP ${{response.status}}`;
    try {{ const body = await response.json(); if (body.error) message = body.error; }} catch (e) {{}}
    throw new Error(message);
  }}
  return response.json();
}}

async function load() {{
  try {{
    render(await fetchJson(API + query()));
  }} catch (error) {{
    // A dashboard that silently shows stale numbers is worse than one that
    // says it is stale, so the failure goes where the timestamp was.
    if (error.message !== "signed out") el("stamp").innerHTML =
      `<span style="color:var(--bad)">could not refresh: ${{esc(error.message)}}</span>`;
  }}
}}

async function loadCalls(before) {{
  try {{
    const data = await fetchJson(CALLS_API + query({{ q: state.q, before_id: before, limit: 25 }}));
    renderCalls(data, Boolean(before));
  }} catch (error) {{
    if (error.message !== "signed out") el("recent").innerHTML =
      `<div class="empty" style="color:var(--bad)">could not load calls: ${{esc(error.message)}}</div>`;
  }}
}}

async function loadCampaigns() {{
  try {{
    const data = await fetchJson(CAMPAIGNS_API);
    const select = el("campaign");
    const current = state.campaign;
    select.innerHTML = `<option value="">All campaigns</option>` + data.campaigns.map((c) =>
      `<option value="${{esc(c.id)}}">${{esc(c.name)}} (${{esc(c.status)}})</option>`).join("");
    select.value = current;
  }} catch (error) {{ /* the filter still works by typing the id into the URL */ }}
}}

function apply() {{
  state.campaign = el("campaign").value; state.from = el("from").value;
  state.to = el("to").value; state.q = el("q").value.trim();
  writeState(); load(); loadCalls(null);
}}
el("apply").addEventListener("click", apply);
el("q").addEventListener("keydown", (e) => {{ if (e.key === "Enter") apply(); }});
el("clear").addEventListener("click", () => {{
  for (const k of ["campaign", "from", "to", "q"]) {{ state[k] = ""; el(k).value = ""; }}
  writeState(); load(); loadCalls(null);
}});

readState();
loadCampaigns();
load();
loadCalls(null);
setInterval(() => {{ load(); if (!state.q) loadCalls(null); }}, REFRESH_MS);
</script>
</body>
</html>
"""


#: One call in full. Rendered from the call's JSON route; the transcript
#: section reads what the route sent, and says so when it sent none.
CALL_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Call {attempt_id}</title>
<style>""" + _STYLE + """</style>
</head>
<body>
<header>
  <h1><a href="/">Calling dashboard</a> · call #{attempt_id}</h1>
  <span class="stamp" id="stamp">loading…</span>
  <span class="who">{who}</span>
</header>

<noscript><div class="note">This page renders in the browser from <code>{api}</code>. Without JavaScript, read that endpoint, or use <code>uv run campaign.py result {attempt_id}</code>.</div></noscript>

<div id="notes"></div>
<div class="grid" id="summary"></div>

<div class="two">
  <div>
    <h2>Who and what</h2>
    <div class="card"><dl class="kv" id="who"></dl></div>
    <h2>Outcome</h2>
    <div class="card"><dl class="kv" id="outcome"></dl></div>
    <h2>What they said</h2>
    <div class="card" id="findings"></div>
  </div>
  <div>
    <h2>Transcript</h2>
    <div id="transcript"></div>
    <h2>How the call went</h2>
    <div class="card"><dl class="kv" id="quality"></dl></div>
    <h2>Usage and cost</h2>
    <div class="card"><dl class="kv" id="usage"></dl></div>
  </div>
</div>

<h2>Transfers, callbacks and meetings</h2>
<div id="related"></div>

<footer>Read-only. The data is <code>{api}</code>.</footer>

<script>
const API = "{api}";
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => (
  {{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }}[c]
));
const el = (id) => document.getElementById(id);
const dash = (v) => (v === null || v === undefined || v === "" ? '<span class="muted">—</span>' : esc(v));
const kv = (pairs) => pairs.filter(([k, v]) => v !== undefined).map(([k, v]) => `<dt>${{esc(k)}}</dt><dd>${{v}}</dd>`).join("");
const tile = (label, value, detail, tone) => `<div class="card"><div class="label">${{esc(label)}}</div>
  <div class="value ${{esc(tone || "neutral")}}">${{dash(value)}}</div><div class="detail">${{esc(detail || "")}}</div></div>`;
const human = (v) => (v ? String(v).replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()) : null);

function render(d) {{
  const call = d.call, r = d.result, rl = d.result_labels || {{}}, cl = d.call_labels || {{}};
  el("stamp").innerHTML = `${{esc(cl.at)}} · ${{esc(cl.status)}}${{d.masked ? " · numbers masked" : ""}}`;
  el("summary").innerHTML = [
    tile("Outcome", r ? rl.disposition : cl.status, r ? "the call's disposition" : "the attempt status; no result yet", cl.tone),
    tile("Qualification", r ? rl.qualification : null, r ? human(r.interest_level) + " interest" : "no result yet"),
    tile("Meeting", r ? rl.meeting : null, r && rl.meeting_start ? "for " + rl.meeting_start : (r && r.meeting_when ? r.meeting_when : "")),
    tile("Callback", r ? rl.callback : null, r && rl.callback_for ? "for " + rl.callback_for : (r && r.callback_when ? r.callback_when : "")),
    tile("Duration", cl.duration, call.connected_at ? "connected " + esc(cl.connected_at) : "never connected"),
    tile("Next action", r ? rl.next_action : null, r && r.transferred ? "handed to a person" : (r && r.human_requested ? "asked for a person" : "")),
  ].join("");

  const p = d.prospect || {{}}, c = d.campaign || {{}};
  el("who").innerHTML = kv([
    ["Prospect", p.full_name ? `<b>${{esc(p.full_name)}}</b>${{p.company ? " · " + esc(p.company) : ""}}` : dash(null)],
    ["Phone", dash(p.phone_normalized || p.phone)],
    ["Email", dash(p.email)],
    ["Title", dash(p.job_title)],
    ["Prospect status", dash(p.status)],
    ["Campaign", c.name ? `${{esc(c.name)}} <span class="pill">${{esc(c.status)}}</span>` : dash(null)],
    ["Attempt", `#${{esc(call.attempt_number)}} · ${{esc(call.telephony_provider || "no carrier")}} · <code>${{esc(call.telephony_call_id || "no call id")}}</code>`],
    ["Timeline", `${{esc(cl.at)}}${{cl.connected_at ? " → connected " + esc(cl.connected_at) : ""}}${{cl.ended_at ? " → ended " + esc(cl.ended_at) : ""}}`],
    ["Failure", call.failure_reason ? `<span style="color:var(--bad)">${{esc(call.failure_reason)}}</span>` : undefined],
    ["Do-not-call", d.dnc ? `<span class="pill bad">on the list</span> ${{esc(d.dnc.source)}}${{d.dnc.reason ? " · " + esc(d.dnc.reason) : ""}}${{d.dnc.created_at ? " · since " + esc(d.dnc.created_at.slice(0, 10)) : ""}}` : undefined],
  ]);

  el("outcome").innerHTML = r ? kv([
    ["Disposition", `<span class="pill ${{esc(cl.tone)}}">${{esc(rl.disposition)}}</span>`],
    ["Call status", dash(human(r.call_status))],
    ["Qualification", dash(rl.qualification)],
    ["Interest", dash(rl.interest)],
    ["Timeline", dash(human(r.buying_timeline))],
    ["Decision role", dash(human(r.decision_role))],
    ["Meeting", `${{esc(rl.meeting)}}${{rl.meeting_start ? " · " + esc(rl.meeting_start) : ""}}${{r.meeting_reference ? " · " + esc(r.meeting_reference) : ""}}`],
    ["Callback", `${{esc(rl.callback)}}${{rl.callback_for ? " · " + esc(rl.callback_for) : ""}}`],
    ["Next action", dash(rl.next_action)],
    ["Transferred", r.transferred === null ? dash(null) : (r.transferred ? "yes" : "no")],
    ["Source", dash(r.source)],
    ["Summary", r.summary_text ? `<div class="headline" style="max-width:none">${{esc(r.summary_text).replace(/\\n/g, "<br>")}}</div>` : dash(null)],
  ]) : `<dt>Result</dt><dd class="muted">no result has been written for this attempt yet</dd>`;

  const list = (items) => items && items.length ? `<ul style="margin:4px 0 8px 18px;padding:0">${{items.map((i) => `<li>${{esc(typeof i === "string" ? i : (i.text || i.kind || JSON.stringify(i)))}}</li>`).join("")}}</ul>` : `<div class="muted">none recorded</div>`;
  el("findings").innerHTML = r ? `
    <div class="label">Pain points</div>${{list(r.pain_points)}}
    <div class="label">Objections</div>${{list(r.objections)}}
    <div class="label">Questions</div>${{list(r.questions)}}
    <dl class="kv">${{kv([["Existing provider", dash(r.existing_provider)], ["Current process", dash(r.current_process)], ["Impact", dash(r.impact)], ["Desired outcome", dash(r.desired_outcome)]])}}</dl>` : `<div class="muted">no result yet</div>`;

  if (d.transcript_included && r && r.transcript && r.transcript.length) {{
    el("transcript").innerHTML = `<div class="transcript">` + r.transcript.map((t) => `<div class="turn ${{esc(t.role)}}${{t.interrupted ? " interrupted" : ""}}">
      <span class="at">${{t.at !== undefined && t.at !== null ? esc(Number(t.at).toFixed(1)) + "s" : ""}}</span>
      <span class="role">${{esc(t.role)}}</span>${{esc(t.text)}}${{t.interrupted ? ' <span class="muted">(interrupted)</span>' : ""}}</div>`).join("") + `</div>`;
  }} else if (d.transcript_available) {{
    el("transcript").innerHTML = `<div class="note">The transcript is withheld for the ${{esc((d.viewer || {{}}).role || "viewer")}} role. An operator or admin can read it here or with <code>uv run campaign.py result ${{esc(call.id)}}</code>; every read is on the audit log.</div>`;
  }} else {{
    el("transcript").innerHTML = `<div class="empty">No transcript: nobody answered, or the call predates the results table.</div>`;
  }}

  const q = d.quality || {{}};
  const ms = (v) => (v === null || v === undefined ? dash(null) : (v / 1000).toFixed(2) + "s");
  el("quality").innerHTML = Object.keys(q).length ? kv([
    ["Responses", dash(q.responses)],
    ["Greeting", ms(q.greeting_ms)],
    ["Response latency", `median ${{ms(q.p50_ms)}} · p95 ${{ms(q.p95_ms)}} · max ${{ms(q.max_ms)}}`],
    ["Failed turns", dash(q.failed_turns)],
    ["Late turns", dash(q.late_turns)],
    ["Barge-ins", `${{dash(q.barge_ins)}}${{q.spurious_interruptions ? " (" + esc(q.spurious_interruptions) + " spurious)" : ""}}`],
    ["Service errors", dash(q.errors)],
    ["Voicemail", q.report && q.report.voicemail ? esc(q.report.voicemail.detected ? "detected" : "not detected") : undefined],
  ]) : `<dt>Quality</dt><dd class="muted">not measured for this call (recorded from Phase 12 onwards)</dd>`;

  const u = d.usage;
  el("usage").innerHTML = u ? kv([
    ["LLM requests", dash(u.llm_requests)],
    ["Tokens", `${{dash(u.prompt_tokens)}} prompt · ${{dash(u.completion_tokens)}} completion`],
    ["Models", u.llm_models && u.llm_models.length ? esc(u.llm_models.join(", ")) : dash(null)],
    ["TTS characters", u.tts_reported ? dash(u.tts_characters) : "not reported by this TTS"],
    ["STT audio", u.stt_reported ? dash(u.stt_audio_seconds) + "s" : "not reported by this STT"],
    ["Cost", u.cost_usd === null || u.cost_usd === undefined ? "no rates configured" : "$" + Number(u.cost_usd).toFixed(4)],
  ]) : `<dt>Usage</dt><dd class="muted">not measured for this call (recorded from Phase 11 onwards)</dd>`;

  const rows = [];
  for (const t of d.transfers || []) rows.push(["Transfer", `${{esc(t.status)}}${{t.to_number ? " to " + esc(t.to_number) : ""}}${{t.duration_seconds ? " · " + esc(t.duration_seconds) + "s" : ""}}${{t.error ? " · " + esc(t.error) : ""}}`]);
  for (const cb of d.callbacks || []) rows.push(["Callback", `${{esc(cb.status)}} for ${{esc(cb.scheduled_for)}}${{cb.note ? " · " + esc(cb.note) : ""}}`]);
  for (const m of d.meetings || []) rows.push(["Meeting", `${{esc(m.status)}} ${{esc(m.start_at || "")}}${{m.external_reference ? " · " + esc(m.external_reference) : ""}}`]);
  el("related").innerHTML = rows.length
    ? `<table><tbody>${{rows.map(([k, v]) => `<tr><td style="width:120px" class="muted">${{esc(k)}}</td><td>${{v}}</td></tr>`).join("")}}</tbody></table>`
    : `<div class="empty">Nothing else is attached to this call.</div>`;
}}

(async () => {{
  try {{
    const response = await fetch(API, {{ cache: "no-store", credentials: "same-origin" }});
    if (response.status === 401) {{ location.href = "/login?next=" + encodeURIComponent(location.pathname); return; }}
    if (response.status === 404) {{ el("stamp").innerHTML = `<span style="color:var(--bad)">no such call</span>`; return; }}
    if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
    render(await response.json());
  }} catch (error) {{
    el("stamp").innerHTML = `<span style="color:var(--bad)">could not load: ${{esc(error.message)}}</span>`;
  }}
}})();
</script>
</body>
</html>
"""


def _esc(value: str) -> str:
    """HTML-escape a value rendered server-side (a user name comes from the environment, but still)."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _who(user: str | None, role: str | None, logout_path: str | None, masked: bool) -> str:
    who = ""
    if user:
        who = f"<b>{_esc(user)}</b> · {_esc(role or '')}"
        if masked:
            who += ' · <span title="phone numbers are masked for this role">numbers masked</span>'
        if logout_path:
            who += f'<form method="post" action="{_esc(logout_path)}"><button type="submit">Sign out</button></form>'
    return who


def render_page(
    *,
    api_path: str,
    refresh_secs: int,
    user: str | None = None,
    role: str | None = None,
    logout_path: str | None = None,
    masked: bool = False,
    calls_api_path: str = "/api/calls",
    campaigns_api_path: str = "/api/campaigns",
    search_api_path: str = "/api/search",
    call_page_path: str = "/calls",
) -> str:
    """The dashboard document, pointed at its own JSON endpoints.

    Args:
        api_path: Where the page fetches its numbers from. Passed in rather
            than hard-coded so the route and the page cannot drift apart.
        refresh_secs: How often the page re-fetches.
        user / role: Who is signed in, for the header. Phase 18.
        logout_path: Where the sign-out button posts; None for a principal
            with no session to end (an API key, or the anonymous dashboard).
        masked: Whether the numbers are being shown masked (a viewer).
        calls_api_path / campaigns_api_path / search_api_path / call_page_path:
            The other routes the page uses. Phase 20.
    """
    return PAGE.format(
        api=api_path,
        refresh=refresh_secs,
        who=_who(user, role, logout_path, masked),
        calls_api=calls_api_path,
        campaigns_api=campaigns_api_path,
        search_api=search_api_path,
        call_page=call_page_path,
        search_hint="" if masked else " or phone",
    )


def render_call_page(
    *,
    attempt_id: int,
    api_path: str,
    user: str | None = None,
    role: str | None = None,
    logout_path: str | None = None,
    masked: bool = False,
) -> str:
    """The call detail document, pointed at the call's JSON route. Phase 20."""
    return CALL_PAGE.format(
        attempt_id=int(attempt_id),
        api=api_path,
        who=_who(user, role, logout_path, masked),
    )


#: The login page: one form, no script, the same palette as the dashboard.
LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Calling dashboard — sign in</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #f6f7f9; --card: #ffffff; --ink: #16181d; --muted: #626875;
    --line: #e3e6ec; --bad: #b3261e; --accent: #2b4a8b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #14161a; --card: #1c1f25; --ink: #e9ebef; --muted: #9aa1ae;
             --line: #2c313a; --bad: #ff8a80; --accent: #9db8ec; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
          background: var(--bg); color: var(--ink);
          font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  form {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px;
          padding: 26px 28px; width: min(360px, 92vw); }}
  h1 {{ font-size: 18px; margin: 0 0 4px; font-weight: 650; }}
  p {{ margin: 0 0 18px; color: var(--muted); font-size: 13px; }}
  label {{ display: block; font-size: 13px; color: var(--muted); margin: 12px 0 4px; }}
  input {{ width: 100%; font: inherit; padding: 8px 10px; border-radius: 8px;
           border: 1px solid var(--line); background: var(--bg); color: var(--ink); }}
  input:focus {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
  button {{ margin-top: 18px; width: 100%; font: inherit; font-weight: 600; padding: 9px;
            border-radius: 8px; border: 1px solid var(--accent); background: var(--accent);
            color: #fff; cursor: pointer; }}
  .error {{ color: var(--bad); font-size: 13px; margin: 0 0 6px; }}
</style>
</head>
<body>
<form method="post" action="/login" autocomplete="on">
  <h1>Calling dashboard</h1>
  <p>Sign in to see campaigns, calls and outcomes.</p>
  {error}
  <label for="username">Name</label>
  <input id="username" name="username" type="text" autocomplete="username" required maxlength="64" value="{username}" autofocus>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required maxlength="1024">
  <input type="hidden" name="next" value="{next}">
  <button type="submit">Sign in</button>
</form>
</body>
</html>
"""


def render_login(*, error: str | None, next_path: str = "/", username: str = "") -> str:
    """The login page, with an error line when there is one."""
    return LOGIN_PAGE.format(
        error=f'<p class="error">{_esc(error)}</p>' if error else "",
        username=_esc(username),
        next=_esc(next_path),
    )


__all__ = ["render_call_page", "render_login", "render_page"]
