# Handoff — Ai-Voice-Agent

**Written for whoever picks this up next, including a fresh Claude session with no memory of how it got here.** Read this before touching code. The section that will save you the most time is [Attempted Approaches That Failed](#attempted-approaches-that-failed) — several of the things below look obviously right and are not.

- **Status:** Phase 26 complete (the UI/UX redesign of the unified application — **frontend only**: `server/web/index.html`, `server/web/styles.css` and `server/web/app.js` were rewritten as a small design system and twelve redesigned pages over the *same* routes and the *same* API calls; no backend, API, database, engine, pipeline, telephony or n8n file changed. Grouped sidebar (Main / AI / Insights / System) with a user area; one status vocabulary with one colour per meaning everywhere (a dot and a word, never colour alone); skeleton loading, empty states with a next action, friendly errors with an expandable technical detail; confirmations that say what will happen; a dashboard built around the questions a user asks; a five-step campaign wizard whose review offers *Save as draft* or *Create and start*; a campaign page built for live monitoring (progress bar with a legend, counters, only the valid controls, the Phase 25 event stream with a Live pill); contacts with server search, client sort, a detail dialog; CSV import as Upload → Validate → Confirm → Done with valid / invalid / already-known counts; call history with qualification and meeting filters; a call page with a summary strip, the sections the brief lists, a readable transcript and a collapsed Technical details panel; the live agent, knowledge base, analytics and settings pages reorganised for a person. Walked end to end in the installed Chrome through Playwright against a private `app.py` instance on 7901 with three test users: 69 steps, all passing, no console errors, no 5xx; see the Phase 26 section.)
- **Phase 26 verified:** 2026-09-10. `uv run validate.py`: **24 scripts, 3,785 checks, 0 findings failed, 11 warnings** (the same posture as Phase 25; the TTS finding passed on this run). `tests/test_app.py` and `tests/test_engine.py` unchanged and passing; the browser walk is in the Phase 26 section. No call was placed.
- **Previous status:** Phase 25 complete (automated outbound campaign execution: the scheduler runs **inside `app.py`** as `src/app/engine.py::CampaignEngine`, built by the new `src/campaigns/runtime.py::build_worker` that `campaign.py run` now uses too — starting a campaign dials it: the engine reserves each contact from the PostgreSQL queue under the Phase 21 advisory lock, places the call through the configured carrier with the contact's ids on the handshake, follows it, writes the outcome, moves to the next, and completes the campaign; per-campaign `configuration.max_concurrent_calls` enforced inside the reservation beside `MAX_CONCURRENT_CALLS`; an explicit retry route (`POST /campaigns/{id}/prospects/{pid}/retry`, a due callback ahead of the queue); a failed call always carries a reason; `CampaignStore.campaign_progress` (every counter in two statements) and `src/campaigns/progress.py`; `GET /api/app/engine`, `GET /api/app/campaigns/{id}/progress`, `GET /api/app/stream` (server-sent events, polled from the rows, only changes sent); the page's campaign strip and dashboard update live from the stream, with a Retry button and a per-campaign concurrency field; `WORKER_EMBEDDED` / `WORKER_SHUTDOWN_SECS`; `app.py --no-engine` and `--with-scheduler` (a child process instead); `/readyz` includes the engine; graceful shutdown (no new calls, a bounded wait, hand-over). **No schema change.** `tests/test_engine.py`, the 24th script (68 checks, all passing, the last section over a throwaway PostgreSQL schema). Live on this machine: a two-contact campaign created from the page with a CSV, started from the page, dialled by the engine without any other command — Ada (interested: pain point, decision maker, partially qualified, 139 s, 9 transcript turns) and, 17 ms after her call ended, Grace (not interested, 69 s) — and marked COMPLETED by the engine itself; the progress route and the event stream showed every step (connected → completed → next contact → 100%), the calls list and the call pages showed both records, and the rows were deleted afterwards.
- **Phase 25 verified:** 2026-09-08. `uv run validate.py`: **24 scripts, 3,785 checks, every script exit 0; the one failed finding is health.py tts (the ElevenLabs credential, unchanged since Phase 23); 13 warnings, the same posture plus two stale scheduler_workers rows from earlier sessions**.
- **Previous status:** Phase 24 complete (the unified application: `uv run app.py` serves a single-page application at `http://127.0.0.1:7900/app/` — Dashboard, Campaigns, Create Campaign (a five-step wizard), the campaign page with start / pause / resume / stop, Contacts, Import CSV with a dry-run preview and an explicit confirm, Calls, the call page with transcript / summary / pain points / objections / qualification / meeting / next action / usage / trace, Live AI Agent (the bot's `/client` framed, plus a queued test call), Knowledge Base (upload / remove / search), Analytics, Settings — and mounts the **existing** dashboard at `/dashboard` and the **existing** automation API at `/automation` on the same origin with one shared session cookie, so one Phase 18 login reaches every part; the API accepts the session with an `X-Requested-With` header as the CSRF check and API keys still work for n8n; one new API write, `PUT /campaigns/{id}/configuration`, stores a campaign's agent profile in the `configuration` JSON the brief has read since Phase 6; **no schema change, no change to the pipeline, the scheduler, the dialer or the carrier**; `app.py --with-scheduler` spawns `campaign.py run`; `tests/test_app.py`, the 23rd check script, walks the whole flow — login, wizard, dry-run import, import, start, a real `CampaignWorker` dialling a fake carrier with the prospect / campaign / attempt / trace ids on the handshake, the brief resolving the contact and the agent profile, pause / resume, stop refused to an operator and allowed to an admin, the knowledge routes, the boundary). **The visual walk through the pages in a browser was not done** (the Chrome tool needed a browser chosen by the user); every route the pages call was exercised over HTTP against the running app and the shapes match what the pages read. The TTS credential is still broken, so the bot still cannot speak.
- **Phase 24 verified:** 2026-09-08. `uv run validate.py`: **23 scripts, 3,715 checks, every script exit 0** (3,647 from before plus `test_app.py`'s 68); two automated findings failed — `health.py tts` (the ElevenLabs credential, as in Phase 23) and `health.py crm` (HubSpot did not answer within 6 s on that run: a network hiccup, not a code change). No real call was placed.
- **Phase 24 integration audit:** 2026-09-08, after the phase. The whole flow was driven end to end on this machine — the real page rendered in jsdom against the running `app.py` and the real PostgreSQL (login with three roles, dashboard, the wizard with a CSV of four rows, preview, create, start / pause / resume, contacts, import, calls, call detail, live, knowledge upload / search / remove, analytics, settings, sign-out), then the **real scheduler worker over the real database dialling the real bot** (Deepgram Flux, Groq, Cartesia, the real brief, the real tools, the local calendar) through a bridge that stands in for SignalWire's HTTP API only — two answered calls with Kokoro as the prospect, one opting out (do-not-call written by the bot's tool, `OPTED_OUT`, `DO_NOT_CONTACT`) and one interested (pain point, decision maker, meeting proposed), every row read back on the calls list and the call page with transcript, summary, objections, usage, latency and trace id. **Six defects found and fixed** (see [Phase 24 audit](#phase-24-integration-audit-this-session)): hash routes with a query string bounced to the dashboard, so every filter and pager was broken; the campaign page read the queue under the wrong key; a saved agent profile re-rendered from stale data; the call page showed no contact name; the CSV preview said 0 already known before an import said 2; and **a real scheduler bug** — a carrier timeout with an empty message crashed the dialer's unresolved hold (`"".splitlines()[0]`), leaving the attempt reserved and the prospect blocked until recovery (the same trap in 40 log lines across 14 files, all hardened; a regression check added). Auto-refresh added for the dashboard, the calls list and the campaign page while a campaign runs. `uv run validate.py` after the fixes: **23 scripts, 3,717 checks, every script exit 0; one automated finding failed (health.py tts, the ElevenLabs credential, unchanged since Phase 23); the extra warning is a stale scheduler_workers row from an earlier session, not the audit's**. The audit rows were removed from the database afterwards. Two observations outside this layer, not fixed: the free Groq tier stalled tool turns for 43–63 s, and on one of those Cartesia rejected an empty sentence three times so the supervisor ended the call mid meeting-request (see [Pending tasks](#4-pending-tasks)).
- **Previous status:** Phase 23 complete (production readiness and end-to-end validation — no new features: `tests/test_production.py`, the 22nd check script, runs the whole story over the real code and a real PostgreSQL in a throwaway schema — CSV import through the real API, campaign created and activated, the scheduler selecting and gating (do-not-call, calling hours), the dial, a conversation with a barge-in, a knowledge-base answer from real pgvector, qualification, an objection, a booking on the real local calendar, a transfer with the carrier's `<Dial action>` report, a callback scheduled and then executed by the worker, a no-answer and a voicemail, the signed completion webhook over HTTP, every row read back, the CRM filing, an n8n delivery over real HTTP to a receiver that verifies the signature, the dashboard and the API with roles, recovery after a dead worker, transient retries, eight concurrent reservations, no secret in any log line, HTTP answer or tracked file, every provider failing gracefully; `uv run validate.py` runs configuration hygiene, `health.py`, `security.py check`, the figures from the rows and all 22 scripts into `server/validation-report.md`, and `validate.py live` runs the controlled real-phone test only with `--dial --yes`; `PRODUCTION_READINESS.md` at the root, which **does not declare the system production-ready** and lists fifteen items still requiring manual verification). Four hardening fixes found by the suite: the importer added a listed number to a campaign before applying the do-not-call list; a CSV "Notes" column never reached the brief; `health.py` probed Cartesia for an ElevenLabs key; duplicated `.env` variables were invisible (`security.py check` now warns). **This machine's `.env` selects ElevenLabs whose key is rejected (401): the bot cannot speak until one `TTS_PROVIDER` line names a provider whose key works** — the first blocker in `PRODUCTION_READINESS.md`.
- **Phase 23 verified:** 2026-09-08. `uv run validate.py`: **22 scripts, 3,647 checks, every script exit 0** (3,489 from before plus `test_production.py`'s 158); one automated finding failed — `health.py tts` (the ElevenLabs credential) — so the runner exits 1 and the report's verdict is red, as it should be while the bot cannot speak. **The eval suites were not re-run this session** (they need a working TTS); the phone drills were not re-run (same). No real call was placed: the controlled real-phone test is the first item of the manual list. See [Phase 23](#phase-23-this-session) and [Pending tasks](#4-pending-tasks).
- **Previous status:** Phase 22 complete (production monitoring and observability, with the call's behaviour untouched: a dependency-free metrics registry — counters, gauges, histograms with Prometheus buckets *and* exact p50/p95/p99 — served at `GET /metrics` (Prometheus text) and `/metrics.json` on every server; `GET /healthz` (liveness) and `GET /readyz` (readiness: the database answers, the deliverer loop is up, the scheduler is not draining; 503 otherwise) on the bot's runner, the dashboard, the automation API and the webhook receiver, and on a small server of the scheduler's own (`MONITORING_PORT`, 7895); every one of the seventeen asked-for figures as a named `aiva_*` metric — attempts, outcomes, results, carrier failures, STT/LLM/TTS errors and the stall, barge-ins, inbound and outbound webhook outcomes, CRM / calendar / callback / transfer / knowledge outcomes, turn and greeting latency by stage, placement / webhook / CRM / tool / store / HTTP latency, tokens per model and per call, cost per call and in total, throughput over a window, worker health, queue depth; a **correlation id** (`trace`, sixteen hex) born at the dialer, written on `call_attempts.trace_id`, sent to the bot on the handshake and read back by the receiver, the recovery poll, the CRM syncer and the event deliverer, so one `grep trace=…` follows a call scheduler → telephony → agent → tools → database → webhook → CRM; `component` and `pid` on every JSON log line; a request id on every API request; the fleet gauges (queue, workers, throughput) read from PostgreSQL into every server every `MONITORING_REFRESH_SECS`; labels a closed list that can never name a person; `MONITORING_TOKEN` in front of `/metrics`; `campaign.py metrics [--json|--prometheus]`; `MONITORING_ENABLED / HOST / PORT / TOKEN / REFRESH_SECS / THROUGHPUT_WINDOW_SECS`, `LOG_COMPONENT`). Phase 23 followed — see [Phase 23](#phase-23-this-session).
- **Phase 22 verified:** 2026-09-08. All **twenty-one** deterministic check scripts pass — **3,489 checks**, the 3,318 from before plus `test_monitoring.py`'s 170 and one in `test_campaigns.py` that had rotted (see below) — with PostgreSQL reachable, so every SQL section ran (the `trace_id` column, the row write and read-back by call id, the throughput aggregate with Phase 11's tokens and cost, `ping()`, the timed store operations under a bound trace, the collector over the real store, all in a throwaway schema). `uv run campaign.py init` was run on this machine's database (the column exists). `uv run bot.py --port 7867` was booted with a token in the shell and probed over HTTP: `/healthz` 200 with the role and sessions active, `/readyz` 200 with the database's answer, `/metrics` 401 without the bearer and Prometheus text with it, `/metrics.json`, and `/client` still served; `uv run campaign.py metrics` and `security.py check` were run against the real rows. The voice pipeline's *behaviour* is untouched: `src/conversation/` did not change at all, `src/telephony/` did not change, and every new line on the call path is a counter increment, a histogram observation or a log field — the supervisor, the diagnostics observer, the latency reporter, the dialer, the action service and the sink record numbers they already had. **Nothing here has been scraped by a real Prometheus or probed by a real orchestrator**: the exposition format is verified by its own parser rules in the checks and by `curl`, not by a Prometheus server; no real call has yet carried a trace end to end. See [Phase 22](#phase-22-this-session) and [Pending tasks](#4-pending-tasks).
- **Previous status:** Phase 21 complete (multi-worker production scaling: any number of `campaign.py run` processes over one queue, coordinated through **PostgreSQL only** — a transaction-level advisory lock around the reservation so the fleet-wide concurrency count is exact and no prospect is ever reserved by two workers; a shared pacing slot in a `scheduler_state` row taken under a lock at placement time, deployment-wide and per campaign (`configuration.pacing_secs`); a `scheduler_workers` heartbeat table with `running` / `draining` / `stopped` and stale detection; `call_attempts.worker_id` naming the worker following each call, so a live worker claims — under one lock — the calls of stale, stopped or unknown workers and releases the reservations a dead one never placed, never touching a live worker's; a clean stop that clears ownership so the next adoption pass anywhere picks the calls up at once; `FAILED` attempts whose reason was the system's (5xx, 429, timeout, unavailable, a worker that died, a recovery close) queued again within the ceiling; duplicate webhook processing proven under concurrent delivery; `campaign.py workers`, a `scheduler` health component, a *Workers and queue* dashboard strip and `scheduler` in `/api/v1/status`; `WORKER_ID / WORKER_HEARTBEAT_SECS / WORKER_STALE_SECS / WORKER_ADOPT_SECS / WORKER_RETRY_TRANSIENT_FAILURES / WORKER_TRANSIENT_RETRY_MINUTES`). Phase 22 followed — see [Phase 22](#phase-22-this-session).
- **Phase 21 verified:** 2026-09-07. All **twenty** deterministic check scripts pass — **3,319 checks**, the 3,166 from before plus `test_scaling.py`'s 151 and one net new check in each of `test_worker.py` and `test_webhooks.py` — with PostgreSQL reachable, so every SQL section ran (six concurrent reservations under a limit of two: exactly two succeed; the pacing slot; the heartbeat table; claim and release; five concurrent deliveries of one webhook applied once, all in a throwaway schema). `uv run campaign.py init` was run on this machine's database (the column and both tables exist; no worker has registered); `campaign.py workers` and `health.py scheduler` were run against it. The voice pipeline is untouched: `bot.py`, `src/conversation/`, `src/telephony/`, `src/actions/` did not change, and `test_scaling.py` asserts `bot.py` and `coordination.py` know nothing of each other. Two earlier expectations changed *because* the behaviour did, on purpose: a second worker no longer adopts a live worker's call (`test_worker.py`), and a carrier `failed` with a SIP 503 is retried rather than exhausted (`test_webhooks.py`). **No fleet has run against a real carrier**: two workers over one database have only ever been the checks' two workers over one in-memory store and one throwaway schema. See [Phase 21](#phase-21-this-session) and [Pending tasks](#4-pending-tasks).
- **Previous status:** Phase 20 complete (the production dashboard and analytics, extending Phase 10's page rather than rebuilding it: campaign and date-range filters applied inside every single-pass aggregate; a calls list that pages and searches name, company, email and call id — the digits of a number only for `read_pii`; a call detail page with the outcome, qualification, meeting, callback and next-action status, the findings, transfers, callbacks, meetings, the do-not-call entry, usage and cost, latency and turn figures, and the transcript only for `read_pii` with every read audited; five new strips — progress, conversion, performance, errors, do-not-call and opt-outs — every rate with its denominator; per-campaign progress and calls remaining; response latency and error figures from a small per-call `usage -> 'quality'` JSON the sink now writes; the snapshot cache keyed by view and bounded). Phase 21 followed — see [Phase 21](#phase-21-this-session).
- **Phase 20 verified:** 2026-09-07. All **nineteen** deterministic check scripts pass — **3,166 checks**, the 3,094 from before plus 72 new ones in `test_dashboard.py` (161, from 89) — with PostgreSQL reachable, so every SQL section ran; the dashboard's route checks now run over the throwaway schema the aggregate checks seed, through a `store_factory`, so they assert on rows for the first time. `uv run dashboard.py --once` against this machine's database produced every section. The voice pipeline is untouched: `bot.py`, `src/conversation/`, `src/telephony/`, `src/actions/`, the dialer, the worker and the gate did not change; `src/campaigns/briefing.py` gained a compact quality summary written beside the usage at teardown (the same write, one more key). **No real call has written a quality summary yet**, so this machine's dashboard shows latency as unavailable until the next call. See [Phase 20](#phase-20-this-session) and [Pending tasks](#4-pending-tasks).
- **Previous status:** Phase 19 complete (outbound calling compliance and safety controls, with every earlier DNC and calling-hours rule kept and strengthened: a persistent do-not-call list (`dnc_numbers`, by number, never deleted) written on every opt-out — verbal, API, CLI, file, registry — and enforced independently in the queue's SQL, at a new pre-dial `ComplianceGate`, on import and create, on adding to a campaign, and on the API's call requests; an opt-out heard mid-call recorded before the call ends, from a campaign call or an anonymous one; a layered `CompliancePolicy` — environment < campaign `configuration["compliance"]` < jurisdiction by the number's country (`COMPLIANCE_JURISDICTIONS`, applied last) — carrying the calling window, the attempt ceiling, per-outcome retry delays, and AI and recording disclosures the agent must speak first; `OPTED_OUT` distinct from `DO_NOT_CALL`; every gate decision, list change and disclosure instruction on the audit log; `campaign.py dnc* / compliance / compliance-log`, `/api/v1/dnc*` and `/campaigns/{id}/compliance`; `COMPLIANCE.md` saying which controls exist and which policies are the operator's, and that none of it is legal advice). Phase 20 not specified — see [Next recommended steps](#12-next-recommended-steps).
- **Phase 19 verified:** 2026-09-07. All **nineteen** deterministic check scripts pass — **3,094 checks**, the 2,945 from before plus `test_compliance.py`'s 146 and three new ones in `test_results.py` — with PostgreSQL reachable, so every SQL section ran (the `dnc_numbers` table, the queue excluding a listed number, the outlook, the configuration merge, all against a throwaway schema). `uv run campaign.py init` was run on this machine's database (the table exists; the list is empty); `campaign.py compliance`, `dnc-list` and `compliance-log` were run against it. The voice pipeline is untouched: `bot.py` gained two lines (the resolver on the briefing, the disclosures on the environment defaults), `src/conversation/` gained one field and two instruction branches and imports nothing from `src/compliance/`; `src/telephony/`, `src/actions/`, `worker.py` did not change. **No real call has exercised the gate**, and no agent has been heard speaking a required disclosure — the instruction is verified in the prompt and on the audit log; the eval suite is where a spoken opening is checked. See [Phase 19](#phase-19-this-session) and [Pending tasks](#4-pending-tasks).
- **Previous status:** Phase 18 complete (authentication, authorisation and security hardening for the three servers that show or change customer data — the dashboard, the automation API and the webhook receiver — with the bot, the pipeline, the dialer and the scheduler untouched: a login on the dashboard from `DASHBOARD_USERS` and a signed session cookie; three roles (viewer / operator / admin) as permission sets, on dashboard users and on API keys (`AUTOMATION_API_KEYS` admin, `AUTOMATION_OPERATOR_API_KEYS`, `AUTOMATION_VIEWER_API_KEYS`); phone numbers, emails, transcripts and custom fields masked out of every answer for a viewer; rate limits per key, per address and per login form; input validation past lengths; a body cap; security headers and a CSP; CORS off unless listed; `SECURITY_REQUIRE_HTTPS` behind trusted proxies; an `audit_log` table written on every login, refusal, write and transcript read; every new secret scrubbed from the log; `server/.env` untracked; `SECURITY.md`; `uv run security.py`). Phase 19 not specified — see [Next recommended steps](#12-next-recommended-steps).
- **Phase 18 verified:** 2026-09-07. All **eighteen** deterministic check scripts pass — **2,945 checks**, the 2,726 from before plus `test_security.py`'s 214 and five new ones in `test_dashboard.py` — with PostgreSQL reachable, so every SQL section ran (the `audit_log` table's SQL runs inside `test_dashboard.py`'s and `test_automation.py`'s real-database sections through the dashboard's login and the API's writes). The voice pipeline is untouched: `bot.py`, `src/conversation/`, `src/telephony/`, `src/actions/`, `src/campaigns/worker.py` and `dialer.py` did not change, and `test_security.py` asserts nothing on the call path imports `src/security/`. **No server has been run behind a real TLS proxy**: `SECURITY_REQUIRE_HTTPS` is verified through FastAPI's test client with a forwarded scheme, and the Caddy / nginx snippets in `SECURITY.md` are the documented shapes, not observed ones. `security.py check` was run against this machine's `.env`. See [Phase 18](#phase-18-this-session) and [Pending tasks](#4-pending-tasks).
- **Previous status:** Phase 17 complete (n8n automation integration, outside the realtime pipeline: an authenticated JSON API — `uv run automation.py` — that creates and imports prospects, creates and starts/pauses/resumes campaigns, queues a call or a callback, and reads calls, results, meetings and the outbox, every write idempotent; and an outbox that delivers `call.completed`, `call.updated`, `lead.qualified`, `meeting.booked`, `callback.scheduled` and `campaign.completed` to n8n's webhook URL, signed, once per row; six importable n8n workflows and a reference in `n8n/README.md`). Phase 18 not specified — see [Next recommended steps](#12-next-recommended-steps).
- **Phase 17 verified:** 2026-09-07. All **seventeen** deterministic check scripts pass — **2,726 checks**, the 2,481 from before plus `test_automation.py`'s 245 — with PostgreSQL reachable, so every SQL section ran. `health.py` unchanged in code (no n8n component: a webhook URL cannot be probed without firing the workflow) and **9 ok** on this machine — `.env` now carries Cal.com (`calcom`, event type 6963977, 30 min) and HubSpot (`crm: hubspot`) credentials, both new since Phase 16's handoff and both green on their cheap reads; no live booking, sync or call was attempted by this session. `uv run automation.py` was booted on port 7891 with a key in the shell: `/api/ping` answered, `/api/v1/status` refused without the key and answered with it, the campaigns list read the real rows, the OpenAPI schema listed 20 paths. The voice pipeline is untouched: `bot.py`, `src/conversation/`, `src/telephony/`, `src/actions/` did not change, and `test_automation.py` asserts nothing on the call path imports `src/automation/`. The API never dials: `test_automation.py` runs the real `CampaignWorker` over a call the API queued and proves the worker — and only the worker — placed it. **No real n8n instance has received a delivery** — the request shape, signature and retries are verified against a recording sender; the six workflow files are valid n8n JSON whose connections name real nodes, and have never been imported into a live n8n. See [Phase 17](#phase-17-this-session) and [Pending tasks](#4-pending-tasks).
- **Phase 16 verified (previous session):** 2026-09-06. All **sixteen** deterministic check scripts pass — **2,481 checks**, the 2,382 from before plus `test_booking_transfer.py`'s 99 — with PostgreSQL reachable, so every SQL section ran. `health.py` gained a `calendar` component. The conversation layer is unchanged: no file in `src/conversation/` was edited, and `test_actions.py`'s 240 checks on the tool boundary pass as they did. **No real booking and no live transfer has been observed** — both are verified against stubs and the vendors' documented shapes. See [Phase 16](#phase-16-this-session) and [Pending tasks](#4-pending-tasks).
- **Phase 15 verified:** 2026-09-06. All **fifteen** deterministic check scripts pass — **2,382 checks**, the 2,203 from before plus `test_crm.py`'s 179 — with PostgreSQL reachable, so every SQL section ran. `health.py`: 7 ok, 1 skipped (the new `crm` component, because no CRM is configured in `.env`). The voice pipeline is untouched: `bot.py`, `src/conversation/`, `src/telephony/` did not change, and `test_crm.py` asserts nothing on the call path imports `src/crm/`. **No call has been filed with a real HubSpot portal** — the adapter is verified against HubSpot's documented endpoints over a stub session. See [Phase 15](#phase-15-this-session) and [Pending tasks](#4-pending-tasks).
- **Phase 14 verified:** 2026-09-06. All **fourteen** deterministic check scripts pass — **2,203 checks**, the 2,012 from before plus `test_webhooks.py`'s 191 — with PostgreSQL reachable, so every SQL section ran. `health.py` is green (7 ok). The phone drills were not re-run: the audio path is untouched (`bot.py` gained one line before `main()`, outside every session). **No live webhook delivery has been received** — the carriers' signing is verified against Twilio's published example and SignalWire's own SDK algorithm, and the first real call is the first real delivery. See [Phase 14](#phase-14-this-session) and [Pending tasks](#4-pending-tasks).
- **Phase 13 found four latent bugs in the Phase 5–9 dialling path, invisible to every earlier check and all fixed:** the last permitted attempt was always released as "attempt limit reached" without dialling (with `CAMPAIGN_MAX_ATTEMPTS=1` nothing ever dialled); a carrier `ringing` event marked the membership `EXHAUSTED` for the length of the ring; the pre-dial safety check read the *stale* prospect copy from the reservation, so a do-not-call landing between reserving and dialling was not caught on the `dial_next` path; and a duplicate placement left its attempt live for ever. See [Failed §33](#33-what-the-scheduler-found-in-the-dialling-path-phase-13).
- **Last verified before that:** 2026-09-06 (Phase 13), thirteen scripts, 2,012 checks. Still true from Phase 12: **no call has been placed through SignalWire**, and **no unattended run has been made against a real carrier** — see [Pending tasks](#4-pending-tasks).
- **Phase 12 found the biggest real-line defect this project has had, and it was invisible to every earlier test.** A phone call's audio streams from the moment it is answered; the bot spent ~8 s setting the session up before reading any of it, so Flux started the call five seconds behind real time and every interruption in the first drill landed on a bot that had already finished speaking. The greeting arrived 15 s after the call connected. Both are fixed (drop the backlog, warm the process once): greeting 4–7 s, turn-start detection under a second, barge-in stop latency 62–141 ms. See [Failed §31](#31-reading-the-carriers-audio-only-once-the-session-was-ready-phase-12) and [Phase 12](#phase-12-this-session).
- **The Phase 7 Groq finding still stands** and dominates the latency numbers below: on the free tier a throttled turn's first token took 9–21 s during the drills, and `turn.late_response` is the log line that says so. Read [Known issues](#5-known-issues-and-limitations) before believing a timeout.
- **Phase 11 measured before it changed anything**, and one measurement is a negative result worth keeping: **six plausible indexes made every dashboard query slower**. Do not add them again — see [Failed §28](#28-indexing-a-full-table-aggregate-phase-11).
- **Two bugs were found by running Phase 9's own code against a real outage**, both now fixed and both worth knowing about: an LLM failure could never accumulate towards its threshold, and a call whose *greeting* failed sat silent until the idle timeout. See [Failed §25](#25-counting-an-llm-failure-and-then-immediately-forgetting-it-phase-9) and [§26](#26-a-threshold-that-cannot-be-reached-because-nothing-tries-again-phase-9).
- **Phase 8 fixed a Phase 6 bug worth knowing about:** the attempt status for a callback, a no, or a do-not-call was only written when the call ended *without* the agent's goodbye. It now reads the state path. See [Failed §23](#23-reading-the-attempt-status-from-the-final-state-phase-6-found-in-phase-8).
- **Stack:** Pipecat 1.8.1, Python 3.12, Deepgram Flux + Groq + Cartesia, SmallWebRTC + Twilio/SignalWire, PostgreSQL (pgvector for the knowledge base, plain tables for campaigns, callbacks, meetings, call results, and — Phase 17 — the automation outbox and the API's idempotency ledger), phonenumbers, tzdata. Optional Cal.com for the calendar, HubSpot for the CRM, n8n (or anything that takes a signed POST) for automation, reached through FastAPI + aiohttp, both already installed. No broker, no lock service, no scheduler daemon — Phase 9's correctness is a transaction and two unique indexes.

> **This file was stale between Phase 2 and Phase 4.** Phase 3 shipped without it
> being updated, so the Phase 3 notes below were reconstructed from the code on
> 2026-09-03 by whoever did Phase 4, and carry none of the "we tried X and it
> failed" detail that makes the rest of this document worth reading. Treat the
> Phase 3 sections as a map, not as testimony. If you are picking this up: update
> this file as part of your phase, not after it.

---

## 1. Project overview and objectives

`D:\Ai-Voice-Agent` is an **AI cold-calling sales agent**. The finished product phones prospects, runs discovery, qualifies them, handles objections, and books meetings with a sales rep. Every call must emit a transcript, summary, pain points, objections, qualification status, meeting status and next action, in a CRM-ready shape.

As of Phase 7 the agent does all of that, including booking the meeting: it phones a prospect it can name, runs discovery, qualifies, handles objections, checks a calendar and books a slot the prospect chose, schedules a callback at an exact time, honours a do-not-call, can hand a live call to a person, and writes a structured record — including every action it took and whether it succeeded — onto the call attempt. As of Phase 8 every finished attempt — answered or not — also has one validated, CRM-ready `CallResult` row: a disposition, the qualification fields, the meeting and callback status, the transcript kept verbatim, and a six-part summary composed from the record rather than by a model. As of Phase 13 `campaign.py run` places the queued calls unattended — recovery, due callbacks, the queue, every call followed to its end, the campaign closed when it is done — in a process of its own beside the bot. What is missing is anything that pushes the result to a CRM.

**The project is built in numbered phases, and the user explicitly does not want later-phase work started early.** This is a hard constraint, not a preference. Do not add a scheduler, CRM sync, auth or business workflows unless the current phase asks for them.

| Phase | Scope | State |
|---|---|---|
| 1 | Browser mic → STT → LLM → TTS, clean provider seams | Done (2026-09-01) |
| 2 | Realtime: streaming, VAD, turn-taking, barge-in, latency measurement, resilience | Done (2026-09-02) |
| 3 | Knowledge base: documents in PostgreSQL/pgvector, retrieval as a pipeline stage, grounded answers | Done (2026-09-02) |
| 4 | Telephony: outbound calls, two swappable carriers, call state and call-level metrics | Done (2026-09-03) |
| 5 | Prospects, campaigns, CSV import, the call queue, do-not-call, call history | Done (2026-09-03) |
| 6 | The sales conversation: state machine, prospect context, objections, DNC, structured qualification | Done (2026-09-03) |
| 7 | Actions: calendar lookup and booking, scheduled callbacks, DNC, end call, live transfer, knowledge search — strict tool schemas, a validating backend, honest results | Done (2026-09-04) |
| 8 | The post-call result: one validated `CallResult` per finished attempt, dispositions, the transcript kept verbatim, a deterministic summary, a CRM-ready shape (no integration yet) | Done (2026-09-04) |
| 9 | Safety and reliability: duplicate-call protection, idempotency, recovery after a restart, bounded retries, campaign guardrails, health checks, structured logs, failure-injection tests | Done (2026-09-04) |
| 10 | The dashboard: a read-only page and JSON endpoint over the existing PostgreSQL — totals, call outcomes, per-campaign statistics, recent calls | Done (2026-09-04) |
| 11 | Performance, scaling and cost: measured first, then a shared connection pool, concurrent dashboard reads, a snapshot cache, concurrency enforced in the reservation, and per-call usage and cost tracking | **Done (2026-09-04)** |
| 12 | Real-line voice quality: turn monitoring, barge-in latency, voicemail detection, carrier AMD, phone drills | Done (2026-09-05) |
| 13 | The autonomous scheduler: `campaign.py run` — recovery, callbacks, the queue, calls followed to their end, campaigns completed, graceful stop, metrics. One process, no new infrastructure | Done (2026-09-06) |
| 14 | Production telephony webhooks: the carrier's lifecycle events pushed to a signed, idempotent receiver, applied through the existing monotonic write; a delivery ledger; polling kept as the fallback; carrier code isolated | Done (2026-09-06) |
| 15 | CRM integration: a provider-agnostic `CrmProvider`, HubSpot first; every finished call filed as a contact plus a call activity carrying outcome, qualification, pain points, objections, summary, meeting, next action and callback; asynchronous (its own process), idempotent, retried with backoff, status on a `crm_sync` row | Done (2026-09-06) |
| 16 | Production booking and transfer: Cal.com hardened (timeout, lost answer looked up, event type verified), the local diary atomic (exclusion constraint), the external booking id stored; a transfer's outcome reported by the carrier's `<Dial action>` through the webhook receiver and recorded on `call_transfers`; the conversation untouched | Done (2026-09-06) |
| 17 | n8n automation: an authenticated JSON API that queues calls as callback rows the scheduler places, and a signed outbox of `call.completed` / `lead.qualified` / `meeting.booked` … events; six importable workflows | Done (2026-09-07) |
| 18 | Authentication, authorisation and security hardening: a dashboard login, three roles on users and API keys, PII masking, rate limits, input validation, security headers, CORS, HTTPS enforcement, an audit log, secrets out of git and out of the log | Done (2026-09-07) |
| 19 | Outbound calling compliance and safety controls: a persistent do-not-call list enforced everywhere, immediate opt-outs, a layered policy (environment < campaign < jurisdiction) for windows, ceilings, retry delays and disclosures, `OPTED_OUT` vs `DO_NOT_CALL`, every decision audited, `COMPLIANCE.md` | Done (2026-09-07) |
| 20 | Production dashboard and analytics: campaign and date filters inside the aggregates, a searchable paged calls list, a call detail page with the transcript behind `read_pii`, conversion / performance / error / compliance / progress strips, per-campaign progress and calls remaining, latency and error figures from a per-call quality summary | Done (2026-09-07) |
| 21 | Multi-worker production scaling: several `campaign.py run` processes over one queue through PostgreSQL alone — an advisory lock around the reservation (exact fleet-wide concurrency, no prospect reserved twice), a shared pacing slot, a heartbeat table, ownership on every attempt with claim and release of a dead worker's work, a clean hand-over at shutdown, transient failures retried, duplicate webhooks proven, worker-health and queue-depth metrics | Done (2026-09-07) |
| 22 | Production monitoring and observability: a metrics registry served as Prometheus text and JSON on every server, `/healthz` and `/readyz` everywhere (the scheduler on its own port), seventeen named figures from attempts to queue depth, a correlation id from the scheduler to the CRM on every log line, `component` on JSON lines, fleet gauges from PostgreSQL, no label that can name a person, `MONITORING_TOKEN`, `campaign.py metrics` | Done (2026-09-08) |
| 23 | Production readiness and end-to-end validation: `tests/test_production.py` (the whole story over real code and a real PostgreSQL), `validate.py` (every automated check into one report; the controlled real-phone test behind `--dial --yes`), `PRODUCTION_READINESS.md` (not declared ready; fifteen manual items), four hardening fixes, no new features | Done (2026-09-08) |
| 24 | Unified application and frontend: `app.py` (port 7900) serves a single-page application and mounts the existing dashboard and automation API on one origin with one login; pages for the dashboard, campaigns, a create-campaign wizard, contacts, CSV import with preview and confirm, calls and call detail, the live agent, the knowledge base, analytics and settings; `PUT /campaigns/{id}/configuration`; no schema change; the pipeline, scheduler, dialer and carrier untouched | Done (2026-09-08) |
| 25 | Automated outbound campaign execution: the scheduler inside `app.py` (`CampaignEngine` over `runtime.build_worker`), per-campaign concurrency, an explicit retry route, a reason on every failed call, `campaign_progress`, `/api/app/engine`, `/api/app/campaigns/{id}/progress`, the `/api/app/stream` event stream, the live campaign page, bounded graceful shutdown; no schema change | Done (2026-09-08) |
| 26 | Not yet specified by the user | Not started |

---

## 2. Current implementation status

A working, measured, tested realtime **cold-calling sales agent** that knows who it is phoning, answers from your documents, records what every call produced, and that you can reach in a browser **or on a phone** — with the failure handling that makes pointing it at real numbers defensible.

```
caller -> Deepgram Flux -> retrieval -> call guidance -> Groq -> Cartesia -> caller
```

Where `caller` is one of three transports over the *same* pipeline:

| Transport | What it is | How you use it |
|---|---|---|
| `webrtc` | A browser at `http://localhost:7860/client` | Development |
| `twilio` (plus telnyx / plivo / exotel on the audio side) | A real phone call, through Twilio or SignalWire | `uv run call.py +92...` |
| `eval` | The headless harness in `evals/` | Regression testing |

**No carrier account? The phone path is still testable.** `tests/fake_carrier.py` speaks a carrier's media-stream protocol at the bot's `/ws` and needs no account, no tunnel and no money. It is how everything below was verified on this machine, which has neither a Twilio nor a SignalWire account.

Everything streams. Turn-taking is decided from speech rather than a silence timer. You can interrupt it mid-sentence — on a phone too — and it answers what you just said. Every answer is grounded in the knowledge base. Every response logs its latency, and every phone call logs its own line and summary.

**Run it:**

```bash
cd server
uv sync
cp .env.example .env    # then add DEEPGRAM_API_KEY, GROQ_API_KEY, CARTESIA_API_KEY
uv run ingest.py init && uv run ingest.py add evals/kb
uv run bot.py
# open http://localhost:7860/client
```

**Simulate a phone call** (no account, no tunnel, no money):

```bash
uv run bot.py                            # terminal 1
uv run python tests/fake_carrier.py      # terminal 2
```

**Phone someone for real** (needs carrier credentials and a tunnel — see [§9](#9-current-architecture-and-workflow)):

```bash
ngrok http 7860                    # put its https URL in TELEPHONY_PUBLIC_URL
uv run call.py +923001234567
```

**Run a campaign unattended** (Phase 13 — the bot must be up, and this dials real numbers):

```bash
uv run campaign.py run                          # every ACTIVE campaign, until Ctrl+C
uv run campaign.py run "Q1 Outreach" --max-calls 1  # the first real run: one call, your own phone
```

**Test it:**

```bash
cd server
uv run health.py                           # Phase 9 — is every dependency actually up?
uv run dashboard.py                        # Phase 10 — the reporting page, http://127.0.0.1:7870
uv run python tests/test_conversation.py   # 291 checks — the sales layer. Run this first
uv run python tests/test_results.py        # 258 checks — Phase 8: the call result, no database
uv run python tests/test_reliability.py    # 141 checks — Phase 9: injected failures
uv run python tests/test_performance.py    # 53 checks — Phase 11: usage, cost, pooling, concurrency
uv run python tests/test_dashboard.py      # Phase 10: the aggregates, the footnotes, the routes
uv run python tests/test_knowledge.py
uv run python tests/test_telephony.py
uv run python tests/test_realtime.py
uv run python tests/test_campaigns.py
uv run python tests/test_worker.py         # 213 checks — Phase 13: the scheduler (SQL half needs PostgreSQL)
uv run python tests/test_security.py       # 214 checks — Phase 18: login, roles, masking, limits, audit, HTTPS
uv run security.py check                   # Phase 18 — the security posture of .env, line by line
uv run python tests/test_compliance.py     # 146 checks — Phase 19: the list, the gate, the policy, the words (SQL half needs PostgreSQL)
uv run campaign.py compliance              # Phase 19 — the effective policy; `compliance-log` for the decisions
uv run python tests/test_dashboard.py      # 161 checks — Phase 10/20: the numbers, the filters, the routes, one call (SQL needs PostgreSQL)
uv run python tests/test_scaling.py        # 151 checks — Phase 21: two workers over one queue, heartbeats, adoption, hand-over, retries, concurrent webhooks, the lock in SQL (needs PostgreSQL)
uv run campaign.py workers                 # Phase 21 — who is alive, what each holds, the queue depth
uv run python tests/test_monitoring.py     # 170 checks — Phase 22: the registry, the trace, every instrumented point, /healthz /readyz /metrics, the fleet gauges, the boundary, the rows (SQL needs PostgreSQL)
uv run campaign.py metrics                 # Phase 22 — throughput, queue and fleet from the rows; --prometheus for a textfile collector
curl -s localhost:7860/readyz              # Phase 22 — any running server; /healthz, /metrics (bearer MONITORING_TOKEN when set)
uv run python tests/test_production.py     # 158 checks — Phase 23: the whole story over PostgreSQL (needs PostgreSQL; ~40 s)
uv run validate.py                         # Phase 23 — config, health, posture, the rows, all 22 scripts -> validation-report.md (exit 1 on any failure)
uv run validate.py measure                 # Phase 23 — answer/success rate, latency, cost, duplicates, from the rows
uv run validate.py live --to +92...        # Phase 23 — the controlled real-phone test; dials only with --dial --yes
uv run app.py                              # Phase 24 — the unified application, http://127.0.0.1:7900/app/ (sign in with a DASHBOARD_USERS account)
uv run app.py --with-scheduler             # Phase 24 — the same, plus `campaign.py run` as a child; dials ACTIVE campaigns
uv run python tests/test_app.py            # 68 checks — Phase 24: serving, one login for every part, CSRF, roles, the whole flow over a fake carrier, knowledge, the boundary
uv run python tests/test_engine.py         # 68 checks — Phase 25: the engine inside the application: automatic execution, pause/resume/stop, failures, concurrency, retry, crash + adoption, shutdown, the stream (SQL section needs PostgreSQL)
uv run app.py                              # Phase 25 — the application AND the engine: a started campaign is dialled from this process (--no-engine to serve without dialling)

SESSION_IDLE_TIMEOUT_SECS=3600 uv run python -m pipecat.evals suite evals/suite.yaml
SESSION_IDLE_TIMEOUT_SECS=3600 USER_IDLE_TIMEOUT_SECS=120 \
  uv run python -m pipecat.evals suite evals/sales/suite.yaml
```

Both eval commands are load-bearing in their exact form, and the second override is new in Phase 6. See [Testing completed](#10-testing-completed) for why neither the env overrides nor the `python -m` form are optional.

---

## 3. Work completed so far

### Phase 1 (prior session, context only)

Cascade pipeline on Pipecat; `src/services.py` factories so a provider swap is a `.env` change plus one branch; `src/config.py` validating env at startup; `src/diagnostics.py` turn-cycle observer; `src/prompts.py`.

### Phase 2 (this session)

**Turn-taking rebuilt around Deepgram Flux.** Flux returns the transcript and the end-of-turn decision on the same websocket, judged from the words and the prosody rather than from silence alone. `STT_PROVIDER=deepgram` keeps the old Silero VAD + Smart Turn v3 path as a fallback for accounts without Flux access, and as an A/B baseline.

**Barge-in, and the repair it requires.** Flux drives interruption via `should_interrupt=True`. Separately — and this is the non-obvious half — an interrupted reply leaves the agent's own half-sentence in the context, and left bare that fragment degrades every reply after it. `mark_interrupted_reply` in `src/turns.py` appends a cut-off marker. See [Attempted Approaches That Failed §2](#2-fixing-the-post-barge-in-truncation-with-the-system-prompt).

**Latency measurement.** `src/metrics.py` wraps Pipecat's `UserBotLatencyObserver` and emits one line per response plus a p50/p95/min/max session summary. The measurement starts from the caller's *real* silence, not from when the VAD reported it (the observer subtracts `stop_secs`).

**Silence and disconnect handling.** `src/resilience.py`: `SilenceHandler` escalates check-ins then closes the call gracefully; `ConnectionGuard` holds the session open through a brief drop and acknowledges a reconnection.

**Error visibility.** A loguru sink counts every ERROR-or-worse record for the session, not just `ErrorFrame`s. This exists because an exception swallowed inside an event handler is exactly how a real bug hid here.

**Eval suite.** `server/evals/` — four audio-mode scenarios plus one text smoke check, a Groq judge supplied through the harness's factory hook, and a suite manifest that spawns a fresh bot per scenario.

**Configuration.** Every tuning knob is an env var, validated at startup, documented in `.env.example` with what it trades away.

**Documentation.** `README.md` and `server/evals/README.md` rewritten for Phase 2.

### Phase 3 (reconstructed from the code, not witnessed)

A knowledge base the agent must answer from. `ingest.py` extracts text from PDFs and text files (`src/documents.py`), chunks it at 60 words with 15 overlapping, embeds it locally with `BAAI/bge-small-en-v1.5` through fastembed (`src/embeddings.py`) and stores it in PostgreSQL with pgvector (`src/knowledge_store.py`).

`src/retrieval.py` is a **pipeline stage**, not a tool the model calls: it sits between the user aggregator and the LLM, embeds what the caller just said, searches, and hands the LLM a copy of the context with the passages appended as a `role: "user"` block. The instruction that travels with the block — "answer from these excerpts only, and say plainly if they do not contain it" — is in `src/prompts.py` and is what makes a refusal possible.

Two eval scenarios (`knowledge_known`, `knowledge_unknown`) and a deterministic script (`tests/test_knowledge.py`) cover it. `KB_ENABLED=false` drops the stage out of the pipeline entirely and gives you the Phase 2 agent.

### Phase 6 (this session)

**The whole layer is one package that imports nothing.** `src/conversation/`
holds the ten states, the qualification record, the prospect brief, every word
the model is told, the deterministic detectors, the tools and the pipeline
stage — and it imports no database, no carrier, and nothing from `bot.py`. Its
only two ways out are Protocols with plain-data signatures (`ProspectSource`,
`ConversationSink`), both implemented in `src/campaigns/briefing.py`, which is
the Phase 6 analogue of `dialer.py`: the one module that knows two worlds. That
is what makes `tests/test_conversation.py` able to drive all fourteen required
scenarios in a second with no keys and no database.

**Conversation state is a table, and two rows of it are promises rather than
preferences.** `NOT_INTERESTED` has no transition back to `VALUE_PROPOSITION` or
`MEETING_REQUEST`, so "never push after a clear rejection" holds whatever the
model decides; `DO_NOT_CALL` ignores the table entirely and is reachable from
anywhere. A refused transition is recorded rather than raising — a live call
must not die because the model asked for something silly — and the refusals end
up in the call's outcome, because a call where the model kept trying to go back
to pitching after a no is a fact about the prompt worth seeing.

**Two mechanisms drive the state, and the asymmetry between them is the
design.** Tools are the mechanism: the model states what it understood as a
structured call, which is what reads "honestly, we're happy where we are and I'd
rather you didn't ring again" correctly. `signals.py` is a floor underneath, and
**only a do-not-call request forces anything** — a false positive there costs one
sale and a false negative costs somebody being phoned after they asked not to
be. Being asked "are you a robot", being asked for a person, and a plain no all
raise guidance for the next turn and force nothing, because "I'm not interested
in switching right now, but tell me more" is a real sentence.

Both halves are tested without the other propping them up:
`evals/sales/do_not_call.yaml` uses a phrasing the detector catches (and
therefore asserts *behaviour*, since the model has nothing left to record), and
`do_not_call_implicit.yaml` uses "I'd appreciate it if you didn't contact me
about this again" — which no phrase list catches — and asserts the tool call.

**Unknown is a value.** Every enum in `qualification.py` has an explicit
`UNKNOWN` member, every free-text field defaults to `None`, and an unparseable
argument from the model leaves its field alone rather than picking the nearest
member. `qualification_status` is the one field the model cannot set: it is
derived from need, interest and authority, so "qualified" means the same thing on
every call.

**The prospect block names its own gaps.** `ProspectBrief.render` does not omit
the fields it does not have — it lists them, by name, under `NOT KNOWN:`,
followed by the rule that nothing outside the block may be claimed. A model
reading a prompt with `company` simply missing cannot tell "not supplied" from
"not applicable" and fills the gap; one told "Not known: their company, their job
title" has been told both that it does not know and that saying so is expected.
The same shape covers the campaign: with no `SALES_COMPANY_NAME` the agent is
told it has not been given one and forbidden to invent one, rather than being
handed a placeholder.

**Guidance is attached per inference, never to the conversation.** The
`ConversationDirector` appends the stage block to a *copy* of the context, the
same mechanism Phase 3 established for the knowledge block and for the same
reason: it is true for one turn and would be stale by the next, and appending it
for real would leave forty obsolete stage notes in a long call. It sits *after*
the retriever because the retriever builds its search query from the last user
message.

**Retrieval is gated now.** `KB_RETRIEVAL_MODE=auto` (the new default) skips
turns that cannot be an information request — "yeah", "we do it by hand", "next
quarter probably" — because a sales conversation is mostly not questions and
searching for each of those attaches confident, on-topic, irrelevant passages to
a turn nobody asked anything in. The gate is written to *skip* rather than to
*allow*, so the default answer is still to search; `always` restores Phase 3
exactly, and `tests/test_knowledge.py` checks both modes.

**The three human-judgement attempt statuses are finally set.** Phase 5 left
`CALLBACK_REQUESTED`, `NOT_INTERESTED` and `DO_NOT_CALL` on `CallAttempt` with
nothing writing them; the sink writes them at the end of the call, and
`dialer.refresh` now refuses to overwrite them with a carrier status. What
somebody *said* outranks the fact that the call completed, and to the carrier
those two calls look identical.

**`call_attempts` gained one nullable column.** `conversation_data jsonb`, added
by `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` inside the existing idempotent
`create_schema`, so an existing database needs `uv run campaign.py init` and no
migration tool. Readers tolerate its absence, so a database that has not been
re-initialised still works — it simply has no conversation records.

**The two stale eval scenarios were rewritten, because Phase 6 answered the
question that left them open.** `conversation` and `voice_quality` asked the
agent for the capital of Spain and the day after Tuesday; Phase 3 told it to
answer only from the knowledge base and they became flaky. Phase 4 deliberately
left the product question open. The answer is that the agent is a sales
representative, so both now test the same mechanisms — multi-turn memory,
speakable output — with turns that belong in the call it is actually on, and
what happens when a prospect genuinely goes off-topic is
`sales/unrelated_question.yaml`.

### Phase 26 (this session)

**The brief:** a professional UI/UX redesign of the existing application —
usability, consistency and visual quality — without rebuilding the backend,
the agent, the engine, telephony, PostgreSQL, n8n or any business logic,
without removing working functionality and without inventing features or
data. Design system; application shell; dashboard around the user's
questions; campaign creation as steps with a review; the campaign page as a
live monitor with only the valid controls; contacts with search, filter,
sort, pagination and a clear CSV import; call history with useful filters;
a strong call page; the live agent as a product feature; an intuitive
knowledge base; useful analytics; organised settings; loading, empty and
error states everywhere; confirmations that explain; responsive;
accessible; no developer UI; a consistency pass; the five user journeys;
performance; nothing broken; the test suite and a manual verification.

**What was built — three files, nothing else.**

- **`server/web/styles.css`** (rewritten, ~430 lines) — the design system:
  tokens (a grey scale, one brand blue, five status tones `good / warn /
  bad / info / neutral`, a type scale, radii, two shadows), and one
  component per idea — `.btn` (primary / danger / ghost / sm / lg),
  `.field` inputs, `.card`, `.tbl` (compact / dense, sortable headers),
  `.badge` (always a dot *and* a word; `.live` pulses), `.alert`,
  `.error-box` with `details.tech`, `.modal` (dialog role, focus trap,
  Escape, focus restored), `.toast`, `.tabs`, `.stepper`, `.progress`
  (segmented), `.bars`, `.rates`, `.facts` / `.kv`, `.feed`, `.transcript`
  (avatars, bubbles, timestamps, an *Interrupted* flag), `.skeleton`,
  `.empty`, `.dropzone`, `details.panel`. The sidebar collapses off-canvas
  under 960 px with a backdrop; tables scroll inside their own wrapper;
  forms stack under 600 px. `prefers-reduced-motion` and print are handled.
- **`server/web/index.html`** — the shell: an inline SVG symbol set (no
  CDN: the CSP allows only `'self'`), the grouped navigation (Main:
  Dashboard, Campaigns, Contacts, Calls · AI: Live AI Agent, Knowledge
  Base · Insights: Analytics · System: Settings), `aria-current` on the
  active item, the user card with sign-out in the sidebar foot, a
  breadcrumb `nav`, a skip link, `aria-live` toasts.
- **`server/web/app.js`** (rewritten, ~1,380 lines) — the same twelve
  routes, the same `request()` (cookie + `X-Requested-With`), the same
  writes in the same order, the same 15 s poll and the same Phase 25
  `EventSource` subscription. New: `friendly(error, doing)` turns every
  status into a sentence and keeps the server's words as *Technical
  details*; one `STATUS` map (label + tone) used by every badge on every
  page; `skelPage / skelTable / skelStats` for page-level loading;
  `withButton(btn, fn, label)` for *Starting… / Saving… / Importing…*;
  `confirm(title, body)` whose body says the consequence (*Stop X? …
  cannot be resumed; N contacts will not be called*); `sortable()` for
  client-side column sorts of the page in view.

**Page by page (all over existing routes and data):**

- *Sign in* — the sidebar is hidden while signed out; a brand card; a
  plain error (*That username or password is not right*, *Too many
  attempts*); no mention of `DASHBOARD_USERS`.
- *Dashboard* — KPIs (Total contacts, Active campaigns, Calls completed,
  Qualified leads, Meetings booked, + Failed calls when non-zero) from the
  Phase 20 `totals`; **Active campaigns** (running + paused, with progress,
  calling now, qualified, meetings and the valid quick actions) or an empty
  state that says what to do; **Results** — outcomes as bars and an
  answer / qualification / meeting rate strip from `conversion` /
  `performance`; **Recent activity** as a feed of human events (*Meeting
  booked with…*, *Lead qualified…*, *Callback scheduled…*, *Call to … failed*)
  derived from `recent_calls`' disposition; the engine state as a pill
  (*Dialling / Not dialling / Dialling disabled / Dialler failed*) and a
  warning banner only when campaigns run and nothing dials.
- *Campaigns* — status tabs with counts, progress, calling now, qualified,
  meetings, the valid actions; empty states per filter.
- *New campaign* — Details → Contacts → AI agent → Calling → Review &
  launch; the review lists Campaign, Contacts, Calling number, AI agent,
  Concurrency, Schedule, Retries, Estimated scope; **Save as draft** or
  **Create and start campaign** (the latter confirms, then calls the
  existing `POST /campaigns/{id}/start` after the same create → configure →
  compliance → add → import sequence). If a later write fails after the
  create, the page says the draft exists and opens it.
- *Campaign page* — title with the status badge and a state sentence;
  **Start** (draft), **Pause / Stop** (running), **Resume / Stop** (paused),
  nothing on completed / cancelled; Stop is red and admin-only as before;
  the progress card: percent, a segmented bar (reached / attempts
  exhausted / calling now / remaining) with a legend, eight compact
  counters, a *Live* pill that turns green when the stream is open; tabs
  Contacts (with count) / Calls / AI agent / Calling settings; the Retry
  button and Add contacts (excluding members already in the campaign).
- *Contacts* — server search through `/dashboard/api/search` (name,
  company, email; the number for `read_pii`), status filter, sortable
  columns, pagination, a detail dialog (`GET /prospects/{id}`: facts,
  custom fields, calls, callbacks, Do not call), Add contact; the do-not-call
  confirmation says what it does.
- *Import contacts* — Upload → Validate → Confirm → Done as a stepper;
  drag-and-drop or choose; a validating state; **Valid rows / Invalid rows
  / Already known / New contacts**; the skipped rows with their reason; a
  preview; the campaign choice; a *Done* card with the counts and the next
  actions. The CSV dialog inside the wizard has the same shape.
- *Calls* — Search, Campaign, Status (human labels), Qualification,
  Meeting, From, To; the last two are client-side over the page in view
  and the heading says *on this page* when they apply; columns Contact,
  Campaign, Status, Duration, Qualification, Meeting, Date, View; rows are
  keyboard-focusable.
- *Call page* — name and disposition, a facts strip (contact, company,
  phone, campaign, status, duration, date, qualification, meeting); main
  column: Conversation summary, Qualification (status, interest, decision
  role, timeline, existing provider, process, impact, desired outcome),
  Pain points, Objections (with *not addressed* when `handled` is false),
  Questions, Transcript (AI agent vs the contact's name, `at` as a time,
  interrupted turns flagged); side column: **Next action**, Meeting,
  Callback, Transfer, Call metadata, and a collapsed **Technical details**
  (carrier id, trace, attempt id, tokens, cost, latency, final state,
  issues).
- *Live AI Agent* — the bot's `/client` framed (with Reload and Open in a
  new tab), the agent profile in use, the voice pipeline, the test-call
  form (disabled with a reason when no carrier or no campaign), a
  Diagnostics panel. Connection, microphone and speaking state live inside
  the bot's own client; the page cannot observe them without a backend
  change, which this phase did not make.
- *Knowledge Base* — documents with an *Indexed* status, passages, size,
  date, Remove (a confirmation that says the agent stops answering from
  it); an Add knowledge dropzone with a *Processing…* state; *Ask a test
  question* with passages as callouts and relevance as a percentage.
- *Analytics* — KPIs, a Rates strip (answer, qualification, meeting,
  callback, no-answer, busy, voicemail, failure, not interested, average
  length, response time), outcomes as bars, Problems, Campaign performance,
  *More metrics* collapsed. No agent filter: the API has none.
- *Settings* — Profile, Calling (telephony, limits), AI agent (default
  profile, voice pipeline), Automation (calendar, CRM, workflow delivery),
  System (the dialler with the scheduler figures, security and compliance),
  Advanced (health check with a summary line, the audit log for admins).

**What was verified (2026-09-10).**

- `node --check web/app.js`; `tests/test_app.py` (its boundary checks —
  same-origin only, every route present, `pageImport` never starting a
  call — still hold) and `tests/test_engine.py` pass.
- **A real browser walk.** The Chrome MCP tool needs a browser choice the
  session could not make, so Playwright (`npm i playwright`, scratch, not
  in the repo) drove the *installed* Chrome (`channel: "chrome"`) against a
  private `app.py --port 7901 --no-engine --no-deliver` with three test
  users injected after `load_dotenv` (the Phase 24 audit's `envrun.py`
  wrapper; `.env` untouched). 69 steps: sign in (wrong password, then
  admin); dashboard; campaigns and a status tab; the wizard with a
  four-row CSV (2 valid, 2 invalid, the duplicate named); Save as draft;
  the campaign page and its four tabs, a profile saved from the tab;
  **Start → Running (Pause, Stop) → Pause → Resume → Stop → Completed (no
  controls)** with every confirmation checked and cancelled once; the
  dashboard listing the running campaign; contacts search, the detail
  dialog, Escape, status filter, Add contact, Do not call; import
  (validate, confirm: 0 new, 2 kept, 2 skipped); calls, a call page, the
  qualification filter, a missing call's error state; live, knowledge
  (search finds passages), analytics, settings (no secret in the DOM, the
  audit log, the health check); mobile 390 px (menu, off-canvas sidebar, no
  horizontal overflow on the dashboard, calls and campaign page) and
  tablet; viewer and operator roles; no console error, no 5xx.
  **No call was placed:** a scheduler from the user's own `app.py` was
  alive on this machine, so the walk's campaign carried a closed calling
  window (`02:00-02:30 Asia/Karachi`, enforced), which the compliance gate
  refuses — the two contacts stayed `PENDING` and were never dialled. The
  campaign, its two contacts, the do-not-call test contact, their
  membership rows and the one automation event were deleted afterwards.
- `uv run validate.py` after the change: 24 scripts, 3,785 checks, 0 findings
  failed, 11 warnings.
- Screenshots of every page were reviewed and four rendering faults fixed
  before the second pass (inline SVGs without a size, the activity feed's
  company line wrapping, KPI tiles wrapping to a second row, the sidebar
  showing on the sign-in screen).

**Not done, on purpose.** No new API route (the live agent's in-call state
and a bot reachability probe would each need one). No agent filter on
analytics. Qualification / meeting filters on the calls page narrow the
page in view, not the database. No dark theme. The old `/dashboard` pages
(`src/dashboard/page.py`) are untouched and still served.

### Phase 25 (previous session)

**The brief:** make the outbound calling system execute campaigns
automatically, reusing every existing implementation: load a started
campaign's eligible contacts into a persistent queue; process it through
the existing telephony; for every contact create the call record, place
the call, hand the contact to the existing agent, let it conduct the
conversation, capture the existing transcript / summary / qualification /
objections / pain points / meeting / next action, update the statuses,
continue with the next contact; reliable call states; no duplicate calls
unless an explicit retry; configurable, bounded concurrency; failures
recorded and the campaign continues; safe recovery after a restart;
Start / Pause / Resume / Stop with pause and stop letting the call in
progress finish; live progress (total, queued, calling, completed, failed,
no-answer, qualified, meetings) and the backend events for the page to
show it without a refresh; state in PostgreSQL, not memory; logging, error
handling, retry protection, graceful shutdown; no new queue system; a
small test campaign end to end.

**What already existed, and was reused unchanged.** Almost all of it, from
Phases 5, 9, 13 and 21: the queue *is* the `campaign_prospects` rows; the
reservation is one transaction under a deployment-wide advisory lock with
an idempotency key per attempt, so a contact is never handed out twice;
`MAX_CONCURRENT_CALLS` is counted inside that transaction; the worker
draws only from `ACTIVE` campaigns (so pause and stop place nothing new)
and follows its calls to their end whatever the campaign's status; every
outcome is written before the next reservation; a dead worker's calls are
adopted and ambiguous placements reconciled with the carrier; transient
failures are retried, a bad number is exhausted; the contact's ids ride the
handshake and the bot writes the transcript and the result. What was
missing was that all of this ran only in a separate command
(`campaign.py run`), that a campaign had no ceiling of its own, that a
retry had no explicit door, that the page had no live figures, and that a
failed call could sit there without a reason.

**What was built.**

- **`src/campaigns/runtime.py`** — `build_service`, `build_guards`,
  `build_gate`, `build_worker(config, store, provider, ...)`: the assembly
  `campaign.py run` did inline, in one place both the CLI and the engine
  call. `campaign.py`'s `_service / _gate / _guards` delegate to it and
  `command_run` calls `build_worker`.
- **`src/app/engine.py`** — `CampaignEngine`: opens its own store pool,
  builds the carrier (`make_provider`, or a `provider_factory` for the
  checks and the audit), builds the worker through `build_worker`, runs
  `worker.run()` as one asyncio task in the application's loop, reports
  (`status()`: state `off / idle / starting / running / stopping / stopped /
  failed`, worker id, provider, limits, the calls in flight, metrics), and
  stops in three bounded steps — `request_stop()` and `WORKER_SHUTDOWN_SECS`
  for the calls in progress, then `request_stop(immediate=True)` (the
  Phase 21 hand-over: rows keep their status, the next worker adopts
  them), then a cancel. No carrier configured → `idle`, the application
  serves. The loop raising → `failed`, logged, `/readyz` not ready, the
  application serves.
- **`src/app/server.py`** — `create_unified_app(..., engine=, provider_factory=,
  worker_id=)`; the engine starts after the store in the lifespan and
  stops first on exit; `/readyz` gains an `engine` check; routes
  `GET /api/app/engine`, `GET /api/app/campaigns/{id}/progress`,
  `GET /api/app/stream?campaign=&max_secs=&poll_secs=` — server-sent events
  (`hello`, `engine`, `campaign`, `error`, `bye`; a comment every 15 s;
  the rows re-read every 2 s and an event sent only when something
  changed; a finished campaign's final figures sent once; ends after an
  hour and the page reconnects).
- **`src/campaigns/store.py`** — `campaign_progress(campaign_id)` (two
  statements: memberships by status with due / scheduled, attempts by
  status with reserved / calling / connected / unresolved / live /
  answered and every ending; keys `PROGRESS_KEYS`, always all present);
  `campaign_concurrency(raw)`; the reservation refuses when the campaign's
  own `configuration.max_concurrent_calls` is reached, counted under the
  same advisory lock. **`src/campaigns/progress.py`** merges the counters
  with the result-derived `qualified` / `meetings`, adds `cancelled`
  (the pending contacts of a finished campaign), `remaining`, `done`,
  `progress_pct`.
- **`src/campaigns/dialer.py`** — an unhappy carrier ending with no
  message gets `the carrier reported <status>` as its reason, so a call
  history never shows a bare `FAILED`.
- **`src/automation/api.py`** — `ConfigurationIn.max_concurrent_calls`;
  `POST /campaigns/{campaign}/prospects/{prospect_id}/retry`, which
  becomes a due callback through the existing `queue_call` (placed ahead
  of the queue, past the attempt ceiling, through the same reservation —
  so never while the contact is on a call, never a do-not-call number,
  held while the campaign is paused).
- **`src/config.py`** — `WorkerConfig.embedded` (`WORKER_EMBEDDED`, on) and
  `shutdown_secs` (`WORKER_SHUTDOWN_SECS`, 30). **`app.py`** — the engine
  by default; `--no-engine`; `--with-scheduler` keeps the child process
  and turns the engine off; the banner says which.
- **`web/app.js`** — the campaign page's progress strip (contacts, queued,
  calling, completed, failed, no answer, qualified, meetings, a bar and a
  line) read from the progress route and kept current by an `EventSource`
  on the stream, which also re-reads the visible Contacts / Calls tab when
  the counters move and re-renders the page when the status changes; the
  dashboard subscribes to the fleet stream and shows the engine's state in
  its banner; an engine badge in the top bar; a *Retry* button on reached
  and exhausted contacts; a per-campaign concurrency field on the Calling
  tab and in the wizard. The 15 s poll from Phase 24 stays as the fallback.
- **`tests/test_engine.py`** — 68 checks: the real application with the
  real engine over the in-memory store and the scripted carrier, nothing
  ticked by hand — automatic execution to completion with each contact
  once and the ids on the handshake; pause (the call in progress ends, no
  new one), resume, stop (the unreached contact counted as cancelled); a
  failed and a no-answer call with reasons and the campaign continuing;
  the deployment's limit and a campaign's ceiling measured at the
  reservation; no duplicates and the explicit retry; a crash mid-call
  (the loop killed without a hand-over) and the next engine adopting the
  call and finishing the campaign; graceful shutdown within the bound with
  the call handed over; no carrier → idle; `engine=False` → off; the event
  stream's events; the boundary; and the ceiling and the counters in SQL
  over a throwaway schema.

**Database changes: none.** The ceiling lives in `campaigns.configuration`
(JSON, since Phase 5); every state the engine keeps is a column that
already existed.

**Queue and concurrency behaviour, in one paragraph.** A started campaign's
contacts are already its queue (the memberships, `PENDING`, ordered by
`next_attempt_at`). Each engine tick reserves the next due contact under
the advisory lock — refusing when the deployment's live calls reach
`MAX_CONCURRENT_CALLS`, when the campaign's live calls reach its own
ceiling, when the contact has a live attempt anywhere, when the number is
on the do-not-call list, outside calling hours, or under pacing — creates
the attempt row (`PENDING`), places the call (`CALLING` → `CONNECTED` as
the carrier reports), and follows it each tick until a final status is
written and the membership moves on. Only then is the next contact
reserved. Pausing or stopping removes the campaign from the draw; the
calls in progress are followed to their end. A no-answer or busy is
queued again after `retry_minutes` until `max_attempts`; a failure is
exhausted; a transient system failure is retried once more. Two engines
(or an engine and `campaign.py run`) over one database coordinate through
the same lock and the same heartbeat table.

### Phase 24 (previous session)

**The brief:** a unified application and frontend around the existing
system, without rebuilding or replacing any of the voice pipeline
(Pipecat, Deepgram Flux, Groq, Cartesia, PostgreSQL, the telephony layer,
RAG). One frontend with routing instead of separate ports; pages for the
dashboard, campaigns, campaign creation, contacts, calls, call detail, the
live agent, the knowledge base, analytics and settings; dashboard totals;
CSV import with validation, errors shown, a preview and a confirm, stored
in PostgreSQL and **never dialling on upload**; campaign management with
name, description, contacts, the AI agent, calling limits and start /
pause / resume / stop; the existing outbound path (CSV → database → queue
→ telephony → Pipecat) connected, with the contact's details reaching the
agent before each call; call history and call detail with transcript,
summary, pain points, objections, qualification, meeting, next action and
metadata; the existing auth and roles; n8n still compatible; a
professional SaaS layout with loading, empty and error states,
confirmations and toasts; the backend modular, no business logic in the
frontend; local development and the voice-agent testing flow unbroken.

**What was built.**

- **`server/app.py`** — the entry point. `--host` / `APP_HOST`, `--port` /
  `APP_PORT` (7900), `--bot-url` / `APP_BOT_URL` (where the bot's
  `/client` is, for the Live AI Agent page), `--with-scheduler` (spawns
  `campaign.py run` as a child and stops it on exit), `--no-deliver` (no
  outbox deliverer). Logs as `component=app`.
- **`server/src/app/server.py`** — `create_unified_app(config, ...)`: one
  FastAPI application that builds the **existing** dashboard app
  (`src/dashboard/web.create_app`) and the **existing** automation app
  (`src/automation/api.create_automation_app`) and mounts them at
  `/dashboard` and `/automation`. The parent's lifespan enters both
  sub-applications' lifespans (so their pools and the deliverer loop run as
  before) and opens a small pool of its own. The session secret is
  generated once and shared with both by `dataclasses.replace`, so one
  cookie is valid everywhere. The parent's own routes: `GET /` (redirect to
  `/app/`), `/app`, `/app/{path}` (the single page — hash routing, so every
  deep link serves the same file), `/static` (the page's files),
  `GET /api/app/session` (who am I, permissions, the login and logout
  URLs), `GET /api/app/config` (the effective configuration with every
  secret scrubbed — providers, telephony, calling, sales, calendar, CRM,
  automation, security, compliance, monitoring, the bot URL),
  `POST /api/app/health` (the Phase 9 probes, `write`),
  `GET /api/app/knowledge`, `POST /api/app/knowledge/documents` (multipart
  upload → extract → chunk → embed → `add_document`, audited),
  `DELETE /api/app/knowledge/documents/{source}`,
  `POST /api/app/knowledge/search` (the same vector search the agent
  runs, with the same score floor). `install_security(..., kind="dashboard")`
  with a content-security policy that additionally allows the bot's origin
  in a frame; the Phase 22 ops routes under role `app`.
- **`server/web/`** — `index.html`, `styles.css`, `app.js`: a vanilla
  single-page application, no build step, no framework, no dependency.
  A sidebar, a top bar with breadcrumbs and the signed-in user, a hash
  router (`#/dashboard`, `#/campaigns`, `#/campaigns/new`,
  `#/campaigns/:id`, `#/contacts`, `#/contacts/import`, `#/calls`,
  `#/calls/:id`, `#/live`, `#/knowledge`, `#/analytics`, `#/settings`),
  loading / empty / error states on every view, confirmation dialogs on
  every irreversible action, toasts, a login view shown on any 401, and
  buttons hidden by the permissions the session reports. Every request
  goes to one of three prefixes on the same origin — `/dashboard/api/…`,
  `/automation/api/v1/…`, `/api/app/…` — with the cookie and an
  `X-Requested-With: fetch` header. The create-campaign wizard is five
  steps (details → contacts, from the list or a CSV with a dry-run preview
  → the AI agent's profile → calling limits and hours → review) and only
  writes on the last step: create the draft, `PUT configuration`,
  `PUT compliance`, add the chosen contacts, import the CSV into the
  campaign. The campaign is created as `DRAFT`; the user starts it.
- **`server/src/automation/api.py`** — two additions. (1) `require_key`
  accepts the dashboard session cookie when no bearer key is presented:
  the principal comes from `read_session` with the shared secret (or the
  anonymous operator when `DASHBOARD_AUTH_DISABLED` on loopback); a write
  without `X-Requested-With` is refused `403 csrf`; the rate limit is
  keyed `session:<name>`. Bearer keys behave exactly as before, so n8n is
  unaffected. (2) `PUT /campaigns/{id}/configuration` (`write`):
  `agent_name`, `company_name`, `offer`, `value_points`,
  `qualification_criteria`, `meeting_ask`, `notes`, `pacing_secs` — written
  through the existing `update_campaign_configuration`, audited as
  `campaign.configure`, read by the existing `CampaignBrief.from_configuration`
  at dial time. A missing or empty field clears it.
- **`server/src/security/http.py`** — `HttpPolicy.csp`, an optional
  override of the dashboard's content-security policy (the application
  needs `frame-src` for the bot's client).
- **`server/tests/test_app.py`** — 68 checks over the unified app with
  `FakeStore`, `FakeKnowledge` and a fake carrier: serving and the CSP;
  one login reaching `/app`, `/dashboard/api`, `/automation/api/v1` and
  `/api/app`; the CSRF refusal; a viewer refused every write; the
  auth-disabled loopback; the configuration route and the brief that
  reads it; the whole flow — dry-run import, import, start, a real
  `CampaignWorker` (`auto_complete=False`) dialling with the prospect,
  campaign, attempt and trace ids on the handshake, the brief resolving
  the contact, company, note and agent profile, pause, resume, stop
  refused to an operator (403) and allowed with an admin key; the
  knowledge routes; the import boundary (`src/app` imports no
  conversation code).
- `validate.py` runs `test_app` as the 23rd script; `.env.example` gained
  the `APP_*` block.

**Database changes: none.** The prospects, campaigns, memberships,
attempts, results, callbacks, meetings and knowledge tables are the
existing ones; the agent profile lives in `campaigns.configuration`, a
JSON column since Phase 5. `campaign.py init` is not needed for this phase.

**The flow, verified in `test_app.py`:** login → dashboard → create
campaign (wizard) → upload CSV → validate / preview (dry run) → create →
start (`ACTIVE`) → the queue (the existing selection) → the existing
worker dials → the carrier (fake) opens the bot's `/ws` handshake with the
ids → the brief carries the contact's name, company, notes and the
campaign's agent profile → the result rows → the calls list and the call
page read them back. The real bot, the real carrier and the real speech
services are the same processes as before and were not exercised (the TTS
credential is still broken — see [Pending tasks](#4-pending-tasks)).

**What was deliberately not done.** No per-campaign calling number or
provider (the carrier and its number are deployment-level configuration,
shown read-only on the campaign's Calling tab and in Settings — adding a
per-campaign number is a telephony-layer change, not a frontend one); no
per-campaign concurrency (the Phase 21 limit is fleet-wide by design and
is shown as such); no `FAILED` campaign state (the backend has
`DRAFT / ACTIVE / PAUSED / COMPLETED / CANCELLED`; the page shows `ACTIVE`
as *Running* and failures at the call level); no scheduling of a
campaign's start time (the backend has none — calling hours are enforced,
a start time is not); no frontend framework or build step.

### Phase 24 integration audit (this session)

**The brief:** after the phase, an end-to-end integration audit of the
whole application — login → dashboard → CSV upload → validation / preview
→ create campaign → start → queue → the existing telephony → the existing
Pipecat agent → completion → transcript / results → PostgreSQL → call
history → call detail — checking connectivity, every endpoint, auth and
roles, CSV validation, contact persistence, campaign state changes, the
queue, the contact reaching the agent, telephony, call status, transcript
persistence, the result fields, history and detail, real-time updates,
error and loading states; fixing what was broken; no new major features.

**How it was tested (all on this machine, nothing in the repository).**

1. *The page, for real.* `web/index.html` + `web/app.js` as served by the
   running `app.py` were rendered in **jsdom** with `fetch` bridged to
   Node's and a cookie jar, and driven as a person would — typing, clicking,
   file inputs, confirm dialogs — against the real backend and the real
   database: sign-in (wrong password, then three roles), the eight stat
   cards, the wizard end to end with a four-row CSV (two valid, one number
   without a country code, one in-file duplicate), the preview's counts and
   reasons, create → `DRAFT`, the campaign page's tabs, start / pause /
   resume with confirmations and cancel, the no-scheduler warning, contacts
   (list, filter, add, look-up, do-not-call), the import page (re-upload →
   "already known"), calls, call detail, the missing-call error state, the
   live page's frame, knowledge (list, search, upload, search again,
   remove), analytics with a filter, settings (scrubbed config, audit log,
   the health probe), sign-out, the viewer's masked numbers and no
   transcript, the operator's missing Stop. Console errors and 5xx were
   counted (none). Three jsdom gaps had to be polyfilled — the form named
   getter (`form.username`), `Blob.text()`, and event-handler rejections —
   all of which browsers have; nothing in the page was changed for jsdom.
2. *The call, for real.* The **real** `CampaignWorker`, `CampaignDialer`,
   `ComplianceGate`, guards, service and store over the **real** database,
   with one substitution: a `TelephonyProvider` whose `place_call` opens
   the fake carrier's media-stream session (`tests/phone_drill.DrillCall`,
   Kokoro speaking the prospect's lines when the agent goes quiet) against
   the **real bot** on port 7861 — real Deepgram Flux, real Groq, real
   Cartesia (the bot was run with `TTS_PROVIDER=cartesia` and
   `CALENDAR_PROVIDER=local` applied *after* `load_dotenv`, so no real
   booking could be made and `.env` was not edited). The handshake carried
   the dialer's own parameters — prospect, campaign, attempt, trace — and
   the bot's system prompt was seen to contain the contact's company, job
   title, the CSV `Notes` column and the campaign's agent profile.
3. *The rows.* Every figure the page showed was checked against the JSON
   and, where it mattered, the SQL: the attempt's status, carrier id,
   trace id, worker id and duration; the result's disposition,
   qualification, interest, decision role, next action, summary, pain
   points, objections, transcript with timings, usage and the quality
   summary; the do-not-call entry the bot's tool wrote (`source=verbal`,
   the attempt and campaign on it); the membership's `EXHAUSTED` /
   `IN_PROGRESS` / `COMPLETED`; the outbox's `call.completed` rows once a
   deliverer ran (`automation.py --once`; n8n is not running here, so
   `RETRY`); the audit log's `campaign.start / pause / resume`,
   `prospect.create / do_not_call / import`, `pii.transcript_read`, the
   logins and logouts.

**What worked first time.** Login and the session cookie across all three
mounted parts; CSRF (a write without the header is 403); the three roles
end to end (viewer: no buttons, masked numbers, no transcript, no audit
log; operator: no Stop); the wizard's five steps and its writes; the CSV
preview's per-row reasons; the import's "nothing dialled"; start / pause /
resume / stop with the audit trail; contact add / look-up / do-not-call;
the queue reserving the contact under the gate (`compliance.allowed`,
region US, policy default); the dial with the ids on the handshake; the
brief; the conversation's tools (`record_objection`, `record_discovery`,
`check_calendar_availability`, the do-not-call tool, `end_call`); the
carrier outcome and the conversation outcome combined correctly (a
`COMPLETED` carrier status did not overwrite `DO_NOT_CALL`); the result
row; the calls list and the call page; the knowledge base's upload,
search and removal; the health probe; secrets scrubbed from Settings.

**Defects found, and fixed.**

1. **Every hash route with a query string bounced to the dashboard**
   (`web/app.js` `route()`): the route regexes were matched against the
   whole hash, so `#/calls?campaign=3`, `#/contacts?status=…`, the pager
   and the analytics filter all went to `#/dashboard`. The route is now
   the part before `?`. Found on the first filter click.
2. **The campaign page read the queue figure under `outlook`**; the API
   sends `queue`. "N due now" never showed. Fixed.
3. **Saving the agent profile or the calling settings re-rendered the
   tab from the configuration the page had loaded with**, so the edit
   looked lost until a reload (the server had it). The tab now re-reads the
   campaign before re-showing.
4. **The call page showed no contact name** ("Call #7 — unknown"): the
   dashboard's detail JSON carries `first_name` / `last_name`, not
   `full_name`. Fixed with a fallback.
5. **The CSV preview said "0 already known" and the import then said
   "0 created, 2 already known"**: the importer's dry run returned before
   the database was consulted. `import_csv(dry_run=True)` now counts the
   numbers `find_prospect_by_phone` knows.
6. **A carrier placement that timed out with an empty message crashed the
   dialer's unresolved hold** (`src/campaigns/dialer.py::_hold_unresolved`:
   `str(exc.cause).splitlines()[0]` on `""` is `IndexError`), so the attempt
   was never marked `UNRESOLVED`, the tick failed, and the prospect stayed
   blocked until `campaign.py recover`. A bare `asyncio.TimeoutError` is
   exactly what a slow carrier produces. Fixed with a fallback to the
   exception's name; the same `str(exc).splitlines()[0]` sat in forty log
   lines across fourteen files (worker, webhooks, CRM sync, the deliverer,
   health, the gate, the API, the dashboard, the app) where an empty
   message would have turned a handled failure into an unhandled one — all
   now `(str(exc).splitlines() or [type(exc).__name__])[0]`. Two checks
   added to `tests/test_worker.py` (216, from 214).

**Added, because the brief asked for real-time updates where required:**
while a campaign is `ACTIVE`, the dashboard, the calls list and a campaign
page on its Contacts or Calls tab re-read the rows every 15 s — never over
an open dialog, a disabled (working) button, an edit tab or a hidden
browser tab, and without the loading placeholder. The campaign page
remembers its tab across refreshes. No WebSocket: the rows are written by
the scheduler and the bot, and a poll is the honest source.

**Observed and not fixed (outside this layer; the brief said not to touch
the pipeline).** On the interested call the free Groq tier stalled the two
tool turns for 43 s and 63 s (the known throttling — see Known issues),
and on the second Cartesia rejected the reply three times ("No valid
transcripts passed": the model produced an empty sentence after the tool
call), so the supervisor ended the call during the meeting request. The
result row recorded all of it honestly (`late_turns=1`, `failed_turns=1`,
the three TTS errors, "the line closed before the agent ended the call").
The greeting once said "this is [name] calling" with the campaign's agent
name set to "Audit Agent" — the model treating an odd name as a
placeholder, not a code path.

**Cleaned up:** the audit's three campaigns, three prospects, three
attempts, three results, two do-not-call entries, two outbox rows and two
scheduler-worker rows were deleted from the database in one transaction
afterwards; the audit-log rows about them were left, as an audit log
should be. The jsdom walker, the bridge carrier and the cleanup script
lived in the session's scratch directory and are not in the repository.

### Phase 23 (previous session)

**The brief:** production readiness and end-to-end validation, the last
phase before real use; no unnecessary features. An end-to-end validation
suite over twenty-five requirements (import, creation, activation,
selection, DNC, calling hours, the outbound call, the conversation,
barge-in, retrieval, qualification, objections, booking, transfer, callback
scheduling and execution, voicemail and no-answer, the completion webhook,
persistence, CRM, n8n, the dashboard, auth, retry and recovery, cost);
run every automated test; controlled real-phone tests; measure latency and
success rate; verify no duplicate calls, no unauthorized access, no exposed
secrets, graceful provider failure; a `PRODUCTION_READINESS.md`; and do not
declare the system ready if any critical requirement is unverified.

**What was built.** Three things and four fixes.

* `tests/test_production.py` — the twenty-second check script, 158 checks,
  one story over one throwaway PostgreSQL schema. A `Deployment` opens the
  real store, service, `ComplianceGate`, a scripted carrier, guards, dialer,
  worker, a real `KnowledgeStore` + embedder + retriever in the same schema
  (search_path `"<schema>", public` so `vector` resolves), and a
  `store_factory` for apps under FastAPI's test client (a pool per loop —
  Phase 17's lesson, met again in step 18). Steps 1–3 go through the real
  automation API (a CSV with a duplicate spelling, an unusable number and a
  number already on the do-not-call list; a prospect marked do-not-call
  through the API; the campaign started through the API). Steps 4–7 tick
  the real worker: a closed window places nothing and spends nothing; the
  open one reserves, gates, dials once with the caller ID, the stream URL,
  the ids and the trace on the handshake and the callback URL for the
  webhooks. Steps 8–13 drive a `Harness` per call: `open_briefing` over the
  store's pool resolves the brief from the rows (the prospect, the company,
  the CSV note), `SalesConversation` with the real tools over the real
  `ActionService` (the real local calendar with `busy=store`, the real
  retriever, `test_actions`' fake carrier for the transfer), a `TurnMonitor`
  fed the frames a barge-in produces, a `LatencyReporter` fed a breakdown;
  discovery, the interruption kept truncated in the stored transcript, a
  pgvector answer for "how much does it cost", a price objection handled, a
  slot offered and booked (a row in `meetings`; the same slot refused twice),
  `end_call`. Step 15 schedules a callback on Bilal's call, step 14
  transfers Danish (the `<Dial action>` report applied through the real
  processor: `ANSWERED`, TwiML `<Hangup />`), step 16 makes the callback due
  *while Erum's call is up* so the tick that closes Erum places the callback
  ahead of the queue (Failed §42), step 17 reads Erum's no-answer and
  Fahad's machine-answered `VOICEMAIL`, both queued for retry with thin
  results. Step 18 posts signed Twilio deliveries over HTTP through the real
  router: `in-progress` connects, a forged and an unsigned `completed` are
  403 and change nothing, the signed one ends the call with the carrier's
  duration, a redelivery is a duplicate, the ledger holds each. Step 19 reads
  every table back and the audit log; step 20 files with `test_crm`'s
  `MockCrm` through the real `CrmSyncer` (reached calls filed, unanswered
  skipped, nothing twice, a 503 a scheduled retry); step 21 serves an n8n
  receiver on a real socket (`serve_ops` from Phase 22 serves any app) that
  does what `n8n/README.md` tells n8n to do — header auth, then
  `verify_signature` over the raw body — and the real `EventDeliverer` with
  the real `AiohttpSender` delivers every settled event to it, the payload
  carrying the result and the trace; a 500 is a retry. Steps 22–23 drive the
  real dashboard and API apps over the schema with an operator, a viewer and
  three keys. Step 24: a `CALLING` attempt owned by a dead worker resolved
  by `AttemptRecovery` from the carrier's answer; another adopted under the
  lock by the live worker and followed to its end; a 503 placement queued
  again, a refused number not; Phase 9's carrier and database failure
  injections re-run. Then eight concurrent reservations under a limit of two
  (exactly two, different people, the right idempotency keys), every number
  dialled at most once and the callback prospect exactly twice, no two
  live attempts per prospect in SQL; the scrubber's real `.env` values plus
  the suite's own credentials searched for in every captured log line, every
  HTTP body and every git-tracked file of both repositories; and a calendar
  unreachable, a calendar crashing, a carrier unreachable for a transfer, a
  retriever raising, an LLM down twice, a database gone — each a plain
  failure the call survives. It prints the timings it measured (the API
  import, a worker tick with a dial, the sink, the webhook apply, the CRM
  pass, the n8n pass, the dashboard snapshot, each tool) and says plainly
  they are the pipeline's own paths, not audio.
* `validate.py` — configuration hygiene (duplicated `.env` variables,
  `.env` tracked, which providers, carrier, webhooks, calendar, CRM, keys,
  users, HTTPS, jurisdictions, rates, monitoring), `health.py` through
  `check_health`, `security.py check` parsed, `measure` (attempts by status,
  answer rate, success rate by disposition, p50/p95 from `usage ->
  'quality'`, priced calls and cost, prospects with two live attempts,
  repeated idempotency keys, the fleet, throughput — read-only), all 22
  scripts in parallel with PASS/FAIL counted, `--evals` for the two suites,
  a `REQUIREMENTS` matrix mapping each go-live item to the sections that
  prove it and the manual step that remains, written to
  `validation-report.md` and `.json`; exit 1 on any failed finding. `live`
  prints the procedure, checks the preconditions (`require_outbound`, the
  number's shape, the four vendor probes, the bot's `/readyz` on loopback)
  and runs `tests/live_call.py` only with `--dial --yes`.
* `PRODUCTION_READINESS.md` — architecture, the 193 variables by group,
  deployment (five processes, ports, probes, the public address, the proxy),
  external services with what is and is not verified live, the database
  (extension, the sixteen tables, connections, correctness, indexes,
  backups), webhook, CRM, calendar and n8n configuration, the security,
  compliance and monitoring checklists, known limitations, what was
  verified on 2026-09-08 and how, the fifteen manual items with their
  commands, and the go-live checklist. It says **not production-ready** on
  the first line and why.

**The fixes.** (1) `service.import_csv` applied the do-not-call list once
at the end, after memberships were opened, so a listed new number was
counted as "added to campaign" and its membership existed for the length
of the import; the list is now applied per row before the add. (2)
`briefing._notes_for` matched `custom_data` keys case-sensitively and the
importer keeps an unmapped column under its own header, so a CSV "Notes"
column never reached the agent; keys are lowered. (3) `health._check_tts`
had no ElevenLabs probe and sent that key to Cartesia; it now probes
`/v1/user` with `xi-api-key`, which is how the real cause on this machine
surfaced. (4) `security.py check` gained `_env_duplicates`: a variable
defined twice is decided by its last line, silently.

**What was deliberately not built.** No new features. No real call placed
by a session: it rings a phone, costs money and needs a person on the line;
`validate.py live` makes it one command with the preconditions checked. No
eval run: the configured TTS credential is rejected. No change to the
user's `.env`: the fix is theirs to make and is spelled out.

### Phase 22 (previous session)

**The brief:** production monitoring and observability, without changing
the call's behaviour. Track call attempts, success and failure, carrier
failures, STT / LLM / TTS errors, barge-ins, webhook, CRM, calendar and
callback failures, average and percentile latency, token usage, estimated
cost per call, campaign throughput, worker health and queue depth; use
structured logs with correlation ids so one call can be traced across
scheduler → telephony → agent → tools → database → webhook → CRM; log no
API key, no full token, no unnecessary customer data; add health and
readiness endpoints for deployment.

**What was already there, and stands.** Phase 9's `call_context` bound the
row ids on every log line and scrubbed every credential; Phase 2's
`LatencyReporter` measured every response; Phase 11 summed tokens and
priced them; Phase 12 wrote a per-call quality report; Phase 13 logged a
`worker.metrics` line a minute; Phase 14 kept `WebhookMetrics`; Phase 21
put a heartbeat row and a queue-depth read in the database. Every one
answered its own question in its own shape — a log line, a JSON file, a
row — and none could be scraped, none shared a name across processes, and
no one id followed a call from the scheduler to the CRM. Nothing was
removed; each now *also* feeds a named metric, and one id joins them.

**The mechanism: a registry, an id, three routes, one collector.** New
package `src/monitoring/`:

* `metrics.py` — `MetricsRegistry` with `Counter`, `Gauge`, `Histogram`;
  no dependency (the Prometheus text format is a few lines per metric, and
  `prometheus_client` is not installed). A histogram keeps Prometheus's
  cumulative buckets *and* a bounded reservoir (1,024 samples) for exact
  p50 / p95 / p99 / mean in `/metrics.json`. `LABEL_NAMES_ALLOWED` is a
  closed list — `campaign` (an id), `stage`, `provider`, `outcome`,
  `status`, `kind`, `operation`, `model`, `transport`, `reason`, `role`,
  `method`, `route`, `bucket`, `state` — and a metric defined with any
  other label raises at definition; a label value is bounded at 80
  characters; `None` reads as `none`, an enum as its value.
* `instruments.py` — every metric, defined once by name, all `aiva_*`,
  with the seventeen-row table of what lands where. `measured()` times and
  counts one awaitable; `outcome_of()` turns an `ActionOutcome` into `ok`
  or its error code; `process_started()` stamps `aiva_up{role}` and the
  start time.
* `tracing.py` — `new_trace_id()` (sixteen hex from `secrets`),
  `PARAM_TRACE_ID` (the media-stream custom parameter; the reader's side
  names it, as `sources.py` does for the row ids), `trace_from_runner_args`,
  `clean_id` (an id from outside is accepted only in `[A-Za-z0-9._-]{4,64}`),
  `REQUEST_HEADER` (`X-Aiva-Request-Id`).
* `http.py` — `create_ops_router` / `install_ops_routes` / `create_ops_app`:
  `/healthz` (always 200: role, pid, uptime, version, `stopping`, plus the
  entry point's `info`), `/readyz` (200 or 503, one scrubbed line per
  check, 503 the moment `stopping()` is true), `/metrics` (text,
  `text/plain; version=0.0.4`) and `/metrics.json`, the last two behind
  `MONITORING_TOKEN` as a bearer compared in constant time. `run_check`
  (a probe under a timeout that never raises past it), `store_ready` (the
  store's `ping()`, or `count_prospects()` on a double without one),
  `always_ready`. `RequestIdMiddleware` (honours a well-formed
  `X-Aiva-Request-Id`, makes one otherwise, echoes it, binds it as
  `request` on the request's lines) and `RequestMetricsMiddleware`
  (`aiva_http_requests_total{role,method,route,status}` by FastAPI's
  route *template* from `scope["route"]`, `unmatched` for a 404; never the
  raw path). `serve_ops` for the scheduler: binds the socket itself with
  `SO_EXCLUSIVEADDRUSE` on Windows (Failed §41), hands it to uvicorn with
  `lifespan="off"`, returns None with one warning when the port is taken.
* `collect.py` — duck-typed over the store: `set_queue_gauges`,
  `set_worker_gauges`, `set_throughput_gauges`, `refresh_from_store` (never
  raises; `partial` when a read fails), `GaugeRefresher` (a background loop
  for the servers; warns once when reads fail, resets when they recover).

**The id.** `CallContext` gained `trace_id`; `CALL_FIELDS` gained `trace`
(first), `worker` and `request`; `call_context(trace_id=…)` and the other
`*_id` keywords land under the short names. `current_trace_id()` reads
loguru's own context var back (the module-level `loguru._logger.context`,
not an attribute on `Core`). The dialer makes the id at the top of
`dial()` — keeping one an attempt already carries — writes it with the
new `store.set_attempt_trace` (tolerant of a schema without the column,
warned once), sends it as `PARAM_TRACE_ID`, binds it. `bot.py` reads it
from the handshake (`trace_from_runner_args`) or makes its own for a
browser, an eval or an inbound call, and puts it on the report identity.
The worker's `_context` adds the row's trace and `worker=`; the receiver
binds it from the attempt it finds by call id; the CRM syncer from the
attempt it loads; the deliverer from `payload["call"]["trace_id"]`
(`serialize.attempt_dict` gained the field, so n8n gets it too). The
store's call-path writes — reserve, placement, the carrier's event, the
conversation record, the result, the usage, the ledger, callbacks,
meetings, CRM and automation rows, the audit — are wrapped by `_timed`,
which counts, times and writes a `store.op` DEBUG line under whatever the
caller bound: the *database* hop. `configure_logging(component=…)` puts
`component` and `pid` on every JSON line (text leaves them out); each
entry point names itself.

**Where each number is recorded.** The dialer: `aiva_call_attempts_total`
on every path out of `dial()`, `aiva_placement_seconds`,
`aiva_carrier_failures_total{kind}` by exception class, `ambiguous`,
`duplicate_placement`, `refresh`. The worker: `aiva_call_outcomes_total`
in `_finish_attempt`, ticks, heartbeats by outcome, in-flight, callback
placements by outcome, and `refresh_fleet_gauges()` on the report tick.
The supervisor: `aiva_service_errors_total{stage,kind=error}` on every
error frame (before it decides anything), `{llm,stall}` from the watchdog,
`aiva_supervisor_terminations_total{reason}`. The diagnostics observer:
`aiva_barge_ins_total`. The reporter: `aiva_turn_latency_seconds{stage}`
per response and `aiva_greeting_latency_seconds`. `bot.py` at teardown
(`_record_session_metrics`, in the same `finally`): sessions and endings,
call duration, tokens per model and per call, characters, audio seconds,
cost per call and per stage from Phase 11's `priced`. The sink:
`aiva_call_results_total{outcome}` by disposition. The action service:
every tool through `_observed` — the public Protocol methods now wrap
private bodies — with `ok` or the error code, and `aiva_tool_seconds`.
The receiver: `aiva_webhook_events_total{provider,outcome}` in `_done`.
The syncer and the deliverer: their outcomes and latency. Every server:
requests by template.

**Where they are served.** The bot: `_install_ops(app)` after the webhook
route, routes only (no middleware in front of the audio websocket),
readiness = `check_health(only=("database",))` with a 3 s timeout, DEGRADED
counted as ready. The dashboard and the API: `install_ops_routes` with a
`GaugeRefresher` started in the lifespan; the API's readiness also checks
the deliverer task is alive; `ApiSettings` gained `monitoring` and
`worker_stale_secs`. The standalone receiver: routes and a database ping.
The scheduler: `serve_ops(create_ops_app("scheduler", …))` in
`command_run`, readiness = the store's ping plus "not draining", `info` =
worker id, in flight, stopping; stopped in the `finally`.

**The store.** `call_attempts.trace_id` (nullable, added by `init`),
`_attempt()` reads it; `ping()`; `set_attempt_trace()`; `throughput
(window_secs)` — one aggregate over `call_attempts`: placed by
`COALESCE(placement_started_at, started_at)`, finished / answered / failed
by `ended_at` and status, `sum(cost_usd)`, tokens from `usage -> 'llm'`,
per campaign; a schema without Phase 9's or 11's columns falls back to
the plain counts. `Throughput` lives in `coordination.py` beside
`QueueDepth`.

**Configuration.** `MonitoringConfig` (`MONITORING_ENABLED` default true,
`MONITORING_HOST` 127.0.0.1, `MONITORING_PORT` 7895 — 0 turns the
scheduler's server off, `MONITORING_TOKEN` at least 16 characters or
unset, `MONITORING_REFRESH_SECS` 30, `MONITORING_THROUGHPUT_WINDOW_SECS`
3600), `Config.monitoring`, `MONITORING_TOKEN` in `SECRET_ENV`,
`LOG_COMPONENT`. `security.py check` warns when `/metrics` is open.
`campaign.py metrics [--json] [--prometheus] [--hours N]`.

**What was deliberately not built.** No OpenTelemetry, no push gateway,
no metrics table: a scrape endpoint per process and the rows for the fleet
are what the project's "PostgreSQL and nothing else" rule allows, and a
metrics table would be a second source of truth for numbers the rows
already hold. No trace on the callback row: an n8n `POST /calls` request's
id becomes a callback row and the worker's dial makes the call's trace
later; joining the two would need a column on `scheduled_callbacks` and is
a small later change. No per-frame token counting: the totals are summed
at teardown from what Phase 11 already keeps. No middleware on the bot's
runner. No vendor probes in `/readyz`: seven network round trips every ten
seconds is a probe somebody turns off; `health.py` keeps them.

### Phase 21 (previous session)

**The brief:** multi-worker production scaling — the scheduler was built
for one process; extend it for several: distributed job locking and
reservation; centralised concurrency limits; centralised campaign pacing;
never the same prospect called by two workers; worker heartbeat and health;
recovery of abandoned jobs; safe shutdown; retry of failed jobs; no
duplicate webhook processing; metrics for worker health and queue depth —
with a shared coordination mechanism fitting the existing architecture
(Redis or PostgreSQL) and the realtime STT → LLM → TTS path untouched.

**What was already there, and stands.** Phase 9's reservation — one
transaction, `FOR UPDATE SKIP LOCKED`, a unique idempotency key, the
live-attempt exclusion, the never-retried placement — was safe across
processes from the day it was written, and Phase 13's worker said so in
its own docstring while declining to build the rest. Phase 11 counted
`max_concurrent` inside the transaction. Phase 14's ledger (`event_key`
unique) and monotonic `apply_call_event` already made a duplicate webhook a
no-op. Phase 13's recovery pass, drain and adoption of live attempts.
None of it was removed; three rules that lived in the process moved into
the database, and two things a fleet needs that one process never did were
added.

**The mechanism is PostgreSQL, and nothing else.** Every process here
already holds a pool to it; Redis or a broker would be a second thing to
run, a second thing to fail, and a second place for the truth to be. What
a fleet needs — serialising the reservation, one shared "last placement"
moment, a table of who is alive, an owner on each call — PostgreSQL does
with an advisory lock, a row, a table and a column. The keys and the
vocabulary are in the new `src/campaigns/coordination.py`; the SQL is in
`store.py`; the loop is in `worker.py`.

**What was built — the store.** `pg_advisory_xact_lock(RESERVE_LOCK_KEY)`
at the top of `_reserve`, before the concurrency count: under READ
COMMITTED two transactions could each count "one below the limit" and
both insert; now the second waits for the first to commit and counts its
row. `worker_id` on the attempt row, written by the reservation.
`take_pacing_slot(min_interval, campaign_id=, campaign_interval_secs=)`
under `PACING_LOCK_KEY`: reads `scheduler_state.last_placement_at` for the
global row and the campaign's, refuses with the longest wait if either
interval has not elapsed, else stamps both — one statement each, one
lock, so two workers cannot both find the interval elapsed. The
`scheduler_workers` table: `register_worker` (upsert), `heartbeat_worker`
(status, in-flight, a metrics JSON), `mark_worker_stopped`,
`list_workers`, `worker_summary(stale_after_secs)` judged against the
*database's* `now()` so clock skew between hosts does not declare a worker
dead, `prune_workers`. `set_attempt_worker`. `claim_abandoned_attempts
(worker_id, stale_after_secs)` under `CLAIM_LOCK_KEY`: one `UPDATE …
RETURNING` over live attempts with a call id whose owner is null, stale,
stopped or unknown to the table, `FOR UPDATE SKIP LOCKED`.
`release_abandoned_reservations`: the never-placed shape only (`PENDING`,
no call id, no placement started) owned by a dead worker, undone through
Phase 13's `unreserve_attempt`; a placement that *started* is left for
recovery to ask the carrier about. `queue_depth(max_attempts)`: due now
(the queue's own eligibility, per active campaign), scheduled later,
callbacks due, reserved, live. All of it `_optional_table`: a database
without the tables gets a `CampaignStoreError` naming `campaign.py init`.

**The worker.** A `worker_id` (`WORKER_ID` plus a six-hex suffix, or
`<host>-<pid>-<suffix>`), passed on every `dial_next` / `dial_membership`
rather than set on the dialer — Failed §40 says why. `start()` registers,
recovers, then runs the adoption pass instead of adopting everything live.
`tick()` beats when due (`draining` from the tick after a stop request —
`request_stop` clears the next-beat time so the status lands at once, not
a heartbeat later), and runs the adoption pass every `adopt_secs`.
`_follow_one` drops an attempt whose row names another worker: that only
happens when this one was taken for dead, and following it too would count
one ending twice. `finish()` clears ownership of everything still in
flight (`_hand_over`) and marks the row stopped (`_deregister`). A pacing
refusal from the shared slot ends the tick's placing loop, the way a guard
refusal does — the slot is global, and asking again would only push every
due prospect back by the wait. Three new counters: `adopted`, `released`,
`handed_over`. A database without the tables: one
`worker.coordination_unavailable` warning, then Phase 13's behaviour.

**The service.** `record_outcome` gained one branch: `FAILED`, the reason
matching `transient_failure()` — the carrier unavailable, a timeout, 429 or
5xx, a rate limit, a lost connection, "never reported an outcome",
"reserved but never placed", "released by recovery", a worker that died
— and the count below the policy's ceiling → the membership is `PENDING`
again after `WORKER_TRANSIENT_RETRY_MINUTES` (or the policy's wait).
Everything else stays what Phase 5 decided. `WORKER_RETRY_TRANSIENT_
FAILURES=false` removes the branch.

**The dialer.** `worker_id=` on `dial_next` and `dial_membership`; in
`dial()`, after the compliance gate and before `mark_placement_started`,
the shared slot is taken — the process-local `PacingLimiter` still runs
first as the cheap check — and a refusal is a `defer()` with the wait in
`blocked_by`. A campaign's `configuration["pacing_secs"]` is the second
scope, capped at an hour.

**Metrics.** `campaign.py workers [--json] [--prune HOURS]`; `health.py`'s
`scheduler` component (degraded on a stale worker, or on work due with no
worker alive; degraded, not failed, without the tables); the dashboard's
*Workers and queue* strip (unfiltered on purpose — whether anyone is
running is a question about the deployment, not the campaign in view);
`scheduler` in `/api/v1/status`.

**Webhooks.** Nothing changed; the checks now deliver one event six times
across two receivers concurrently in memory and five times across five
receivers concurrently against PostgreSQL, and one is applied.

**What was deliberately not built.** No Redis, no broker, no leader
election: nothing here needs a leader. No per-worker share of the
concurrency limit: the limit is one number for the fleet, counted in the
reservation, and a busy worker may hold all of it — `_capacity()` is still
the process's own ceiling, so `MAX_CONCURRENT_CALLS` is both. No automatic
scaling. No fencing token on the carrier call: two workers cannot own one
attempt because the ownership column is written under the same lock the
claim runs under, and the carrier is asked once per attempt, before which
the reservation is the only state.

### Phase 20 (previous session)

**The brief:** the production dashboard and analytics, extending the
existing dashboard rather than rebuilding it: campaign filters; date-range
filters; prospect and call search; a call detail page; the full transcript
with appropriate access control; call outcome and disposition;
qualification, meeting and callback status; conversion metrics; answer
rate; human-transfer rate; voicemail rate; average call duration; average
response latency; AI cost and usage; error and failure metrics; campaign
progress; calls remaining; DNC and opt-out statistics — with the queries
efficient and nothing touching the realtime call pipeline.

**What was already there, and stands.** Phase 10's one page, one JSON
route and one renderer; Phase 11's concurrent aggregates, five-second
snapshot cache, usage strip and the finding that indexes on these tables
make them slower; Phase 18's login, roles, masking and audit; Phase 19's
dispositions and list. Phase 20 adds filters to the aggregates, four more
reads, four more routes and one more document, and changes no number that
was already shown.

**What was built — the rows read** (`store.py`): every reporting aggregate
takes `campaign_id`, `since`, `until` and applies them in the same
single-pass statement (`attempt_counts`, `result_counts`,
`disposition_counts`, `meeting_counts`, `callback_counts`,
`campaign_result_counts`; `prospect_counts(campaign_id=)` through the
memberships; `campaign_overview(campaign_id=)` with the membership
statuses — pending, in progress, completed, exhausted, skipped — and the
voicemail count). `attempt_counts` also reads the per-call quality summary
from the `usage` column (`with_latency`, the average of each call's median
`p50_ms`, `p95_ms`, `greeting_ms`, `failed_turns`, `late_turns`,
`barge_ins`, `calls_with_errors`) and three failure figures
(`with_failure_reason`, `refused_before_dial`, `unreached`);
`result_counts` adds `meetings_agreed`, `callbacks_requested`,
`opted_out`, `do_not_call`, `not_interested`, `answered`.
`recent_call_rows` takes the filters, a prospect, a status, a page
boundary and a search (name, company, email, call id; the number's digits
only when `search_phone`). New: `search_prospects`, `get_attempt_usage`.

**What was built — the summary the sink writes** (`briefing.py`):
`_store_usage` now writes `usage["quality"]` — responses, greeting, the
`total` stage's p50 / p95 / max, failed and late turns, barge-ins,
spurious interruptions, error count — from Phase 12's report, so the
dashboard's latency and error figures are one small JSON field per row
rather than a scan of every transcript. The report itself stays in
`conversation_data`, turns and all.

**What was built — the numbers** (`dashboard/stats.py`): `ReportFilter`
(campaign, since, until; `key()` for the cache, `describe()` for the page);
`collect(filters=)`; `Snapshot` gained `filters`, `conversion`,
`performance`, `errors`, `compliance`, `progress`. *Conversion*: qualified,
meetings booked, callbacks asked for, handed to a person, not interested —
each over answered calls, with the denominator and the neighbouring facts
(agreed-not-booked, at a chosen time, asked for a person) in the footnote.
*Performance*: answer rate, voicemail rate (warns above 30%),
human-transfer rate, average duration with the total, response latency (the
average per-call median, with p95 and the greeting; warns above 3 s;
unavailable, not zero, until a call has measured it). *Errors*: failed
calls with the rate, the refusals before dialling and the recorded
reasons; unresolved; unreached broken down; failed turns and calls with
service errors from the summary. *Compliance*: do-not-call prospects, the
list by source with the revoked, opted out on a call with the dials the
list refused, and the gate's decisions by code from the audit rows.
*Progress*: active campaigns, memberships closed over all, calls remaining
(pending memberships plus due callbacks, with the live ones named). The
campaigns table gained `progress_pct`, `remaining`, the membership
statuses, `voicemail`, `transferred`, `opted_out`. New readers:
`list_calls` (filters, search, status, paging), `search_people`,
`call_detail` (keyed reads only: the attempt, the result — transcript on
request — the transfers, the person, the campaign, their callbacks and
meetings, the list entry, the usage and cost, the quality summary and, with
the transcript, the report; serialized by the dashboard's own `_plain`,
because `src/dashboard/` must import nothing from `src/automation/`).

**What was built — the routes** (`dashboard/web.py`): `/api/dashboard?campaign=&from=&to=`
(a campaign by id or name, `YYYY-MM-DD` days in the campaign zone or ISO
moments, `to` inclusive of the day, a range of at most 400 days; 422 with
a sentence otherwise); `/api/campaigns` for the filter; `/api/calls`
(filters, `q`, `status`, `prospect_id`, `before_id`, `limit` ≤ 100; the
digits of a number searched only for `read_pii`); `/api/search?q=` (people
and calls); `/api/calls/{id}` (the transcript, the conversation record and
the report only for `read_pii`, with a `pii.transcript_read` row; a
viewer's answer says `transcript_included: false` and is masked); and
`/calls/{id}`, the detail page. The snapshot cache is keyed by the view and
bounded to 32 views with the oldest evicted; `get(store)` still works for
the unfiltered view. `dashboard.py --once` is unchanged.

**What was built — the pages** (`dashboard/page.py`): a filter bar
(campaign select from `/api/campaigns`, from and to dates, a search box;
the state is in the URL, so a filtered dashboard is a link); five new
strips; a campaigns table with a progress bar, reached / exhausted /
skipped, remaining and opted out; a calls table that pages ("Show older
calls") and links each row to its detail page; the detail document — six
tiles, who and what, the outcome, the findings, the transcript (or the
note that it is withheld for the role), how the call went, usage and cost,
and what else is attached. No CDN, no framework; the CSP from Phase 18
stands.

**The tests.** `tests/test_dashboard.py` — 161 checks (72 new):
`check_analytics` over fixed inputs (every rate's denominator, the
agreed-not-booked and at-a-chosen-time footnotes, unavailable-not-zero
when there are no results, no answered calls, no usage column or no
measured call; latency as the per-call median with p95 and the greeting;
failed calls with refusals and reasons; the compliance tiles by source and
by code; progress and calls remaining with the live ones; `ReportFilter`'s
description, key and dict); the SQL section seeds a second campaign and
proves a campaign filter narrows every count, a date range narrows and an
empty one is empty, the failure and quality figures aggregate from the
usage column, prospects narrow by membership, the overview narrows and
carries the statuses, search finds by name and — only when asked — by the
digits of a number, by campaign and status together, paging walks
backwards without overlap, one call's usage reads back; the routes — now
over the seeded schema — check every section including the five new ones,
the view's label, a campaign filter by id and by name, an unknown campaign,
a bad date and a backwards range as 422, a future range empty and labelled,
the campaigns list, the calls list paging and walking backwards, search by
name and by number for an operator, a status filter, the search route, one
call in full with its usage and quality, 404, the call page, the two
writing routes, and then a **viewer**: the list masked, a number search
finding nothing, the detail withholding the transcript and the record and
saying so, the page saying the numbers are masked. `check_degraded` proves
the new strips render without the optional tables.

**What was deliberately not built:** indexes for the new filters (Phase
11's measurement stands: these are `count(*) FILTER` scans and an index
would only add write cost); a users-per-campaign or per-team scope (a
viewer sees every campaign, masked — a per-campaign grant is a Phase of
its own); server-side charts or exports (the JSON is the export; the page
draws bars with CSS); a search index (an `ILIKE` scan bounded by `LIMIT`
is milliseconds at this list size, and the number search is gated by role
rather than made faster); verifying the agent's latency figures against a
real call (the summary is written from Phase 12's report by the same code
that stores the transcript; a real call will fill it from the next teardown).

### Phase 19 (previous session)

**The brief:** outbound calling compliance and safety controls, keeping
every existing DNC and calling-hours rule and strengthening it for
production: centralised DNC enforcement before every outbound call;
opt-outs respected immediately and stored persistently; future workers
unable to call opted-out numbers; configured calling windows enforced;
configurable maximum attempts and retry delays; configurable AI and
recording disclosure text; campaign-level compliance settings; clear
dispositions for opt-out, DNC, no-answer, voicemail, failed and completed;
audit logs for compliance decisions; country and jurisdiction rules
configurable rather than hard-coded; no automatic claim of legal
compliance, and documentation of which controls the software implements
and which policies the operator must configure.

**What was already there, and stands.** Phase 5's `ProspectStatus.DO_NOT_CALL`
on the person, closing their open memberships and (Phase 7) pending
callbacks in one transaction; Phase 5's reservation SQL excluding them;
Phase 9's `check_callable` against fresh rows before every dial, the
calling window in the prospect's own timezone, and the concurrency and
pacing guards; Phase 6's detector that forces the state on "take me off
your list" and the tool that does the same, both writing the status
mid-call through the sink; Phase 13's deferral of a reservation whose
window is closed; Phase 8's dispositions. None of it was removed. Phase 19
is what was missing around it: a record of the *number* that outlives any
prospect row, one place every pre-dial rule is called from with the
policy for *that* number, a policy that a campaign and a country can
shape, the words the agent must say first, a disposition that tells "they
told us" from "we knew", and a row per decision.

**What was built — the compliance package** (`src/compliance/`):

| Module | Does |
|---|---|
| `policy.py` | `CompliancePolicy` (jurisdiction label, `calling_hours` / `calling_days` / `timezone` / `enforce_calling_hours`, `max_attempts`, `retry_minutes`, `RetryDelays` per outcome, `Disclosure` for AI and for recording, `recording_enabled`, `sources`; `honor_dnc` is a constant, not a setting), `.overlay(overrides, source=, problems=)` validating every key (`OVERLAY_KEYS`) and reporting rather than raising, `.window()`, `.retry_minutes_for(status)`, `.disclosures()`, `.to_dict()`, `.describe()` (ASCII); `PolicyResolver(base, jurisdictions=, default_region=)` with `for_campaign` (environment < campaign) and `for_call` (… < jurisdiction, by `phonenumbers.region_code_for_number` — the one thing a number *does* determine, unlike a timezone); `parse_jurisdictions` validating `COMPLIANCE_JURISDICTIONS` at startup. Pure; `config.py` imports it, so `CallingWindow` is imported lazily inside `window()` and `resolve_zone` is restated |
| `dnc.py` | `DncEntry` (number, `DncSource` — `verbal`, `api`, `cli`, `import`, `registry`, `manual` — reason, prospect / campaign / attempt ids, who, note, created / expires / revoked at, revoked by and why; `is_active_at`), `parse_source` |
| `gate.py` | `ComplianceGate(service, resolver, audit=, clock=, actor=)`; `check(prospect, campaign, membership, ignore_attempt_id=, ignore_attempt_limit=, attempt_id=, purpose=)` → `ComplianceDecision(allowed, verdict, code, reason, retry_after_secs, policy, region, entry)`; order: the list (`dnc_list`) → the status (`dnc_status`) → `service.check_callable(..., max_attempts=policy.max_attempts)` mapped to `attempt_limit` (EXHAUST) / `retry_wait` (DEFER, until due) / `live_call` (DEFER 30 s) / `campaign_inactive`, `membership_closed`, `not_dialable` (RELEASE) → the policy's window in the prospect's zone (`window_closed`, DEFER until it opens); `Verdict` ALLOW / DNC / DEFER / EXHAUST / RELEASE; a row per decision (`compliance.allowed` / `compliance.blocked`) with the policy line, the region, the code, the reason and the retry; a store without `find_dnc` warns once and decides on the status. Exported lazily from the package (`__getattr__`), because `health.py` imports `config.py` which imports the package |

**What was built — configuration** (`config.py`): `ComplianceConfig`
(`COMPLIANCE_AI_DISCLOSURE` / `_REQUIRED`, `COMPLIANCE_RECORDING_ENABLED` /
`_DISCLOSURE` / `_DISCLOSURE_REQUIRED` (defaults to `_ENABLED`),
`COMPLIANCE_RETRY_MINUTES_NO_ANSWER` / `_BUSY` / `_VOICEMAIL`,
`COMPLIANCE_JURISDICTIONS`, `COMPLIANCE_DEFAULT_JURISDICTION`,
`COMPLIANCE_AUDIT_ALLOWED`; `.policy(reliability, max_attempts=, retry_minutes=)`
composing Phase 9's window and Phase 5's figures into the base;
`.describe()`), `Config.compliance`, `Config.compliance_policy`,
`Config.policy_resolver()`.

**What was built — the rows** (`store.py`): `dnc_numbers` (a partial unique
index on the number `WHERE revoked_at IS NULL`, so one active row per
number and a full history), `find_dnc` (active, unexpired), `add_dnc`
(the first record stands; an expired active row is revoked as `expired`
and replaced), `revoke_dnc` (a stamp, never a DELETE), `list_dnc`,
`dnc_counts`, `prospects_with_number`, `apply_dnc_list(ids | None)` (marks
listed numbers `DO_NOT_CALL`, closing memberships and callbacks through
`set_prospect_status`), `update_campaign_configuration(id, key, value)`
(`configuration || $2::jsonb`, or `- key` for None); `_DNC_EXISTS_SQL`
spliced into `_reserve` and `queue_outlook` **only when the table exists**
(`_dnc_clause`, learned once per store, warning once), so a database that
predates Phase 19 keeps dialling on the status until `campaign.py init`.

**What was built — the service** (`service.py`): `compliance=` on the
constructor; `policy_for(campaign)`, `policy_for_call(campaign, prospect)`,
`max_attempts_for(campaign)`; `check_callable(..., max_attempts=)`;
`next_call` and `reserve_membership` reserve under the campaign's ceiling
(one campaign read); `record_outcome` closes or reschedules under the
policy for the call — `policy.max_attempts` and
`policy.retry_minutes_for(status)` — and a `DO_NOT_CALL` outcome lists the
number as `verbal` naming the campaign and the attempt;
`mark_do_not_call(id, source=, reason=, actor=, campaign_id=, call_attempt_id=)`
(status first, list second, a list that cannot be written logged loudly and
not undoing the status); `add_do_not_call_number` (normalised; every
prospect with the number marked); `remove_do_not_call_number(actor=, reason=,
reinstate_prospects=)` (two decisions, deliberately: the number may be
dialled again; the people are back to NEW only when asked); `is_listed`;
`create_prospect`, `import_csv` and `add_prospects` apply the list, and a
`DO_NOT_CALL` prospect never joins a campaign; `exhaust(queued, reason)`
(unreserve and `EXHAUSTED`, for a jurisdiction's ceiling the SQL could not
apply per number).

**What was built — the dialer** (`dialer.py`): `gate=` on the constructor;
with a gate, `dial()` asks it once and acts on the verdict — DNC →
`record_outcome(DO_NOT_CALL, failure_reason="not dialled: …")` and
`call.blocked`; DEFER → `defer` (Phase 13's path); EXHAUST → `exhaust`;
RELEASE → `release` (Phase 9's path). Without a gate, Phase 9's
`check_callable` and Phase 13's window check run exactly as before.
`campaign.py` always supplies a gate (`_gate`), beside `_guards`.

**What was built — the words** (`conversation/brief.py`, `playbook.py`,
`campaigns/briefing.py`, `bot.py`): `CampaignBrief.disclosures` (sentences,
carried from the defaults, never read from the campaign JSON directly);
`render()` adds a REQUIRED DISCLOSURES block; `opening_instruction` makes
the first sentence include them and drops "do not say you are an AI
unless they ask" when one is required; `CampaignProspectSource.load`
resolves the policy for the call (campaign, then the number's
jurisdiction), puts the disclosures on the brief and writes a
`compliance.disclosure` row; `CampaignConversationSink.on_do_not_call`
records a campaign call's opt-out with the call's ids and an anonymous
caller's by number (the Phase 6 gap in [Known issues](#5-known-issues-and-limitations),
closed); `open_briefing(compliance=)`; `bot.py` passes
`CONFIG.policy_resolver()` and the environment's disclosures on the
defaults. `src/conversation/` imports nothing from `src/compliance/`.

**What was built — dispositions** (`results.py`): `Disposition.OPTED_OUT`
— the conversation heard the request (`final_state` is `DO_NOT_CALL`, or a
conversation result whose status is `DO_NOT_CALL` after a goodbye);
`DO_NOT_CALL` now means the list refused the dial, or a status with no
conversation behind it. Both carry `next_action = DO_NOT_CONTACT`;
`validate_call_result` checks both. `crm/mapping.py` words and the
dashboard's tones know the new one.

**What was built — the tools:** `campaign.py dnc [prospect_id | --number]
--reason --source --note --actor`, `dnc-remove <number> [--reinstate]`,
`dnc-list [--number] [--source] [--all]`, `dnc-import <file> [--source
registry]` (one number per line, or a CSV with a phone column),
`dnc-apply`, `compliance [campaign] [--set key=value …] [--clear key]`
(validated through the overlay before it is written; prints the
environment's, the campaign's and the jurisdictions' policies and says
whose decisions they are), `compliance-log`; every CLI change audited
(`compliance.dnc_added` / `dnc_removed` / `dnc_imported` / `dnc_applied`,
`campaign.compliance_updated`). API: `POST /api/v1/dnc` (write),
`GET /dnc` (read_pii), `GET /dnc/check?phone=` (read; yes/no only),
`DELETE /dnc/{phone}` (manage; `reinstate_prospects`),
`POST /prospects/{id}/do-not-call?reason=` (now lists the number, source
`api`, the key's label as the actor), `GET` / `PUT
/campaigns/{ref}/compliance` (write; 422 with the problems, nothing
written), and `POST /calls` / `/callbacks` refusing a listed number with
409 `do_not_call`, audited. `ApiSettings.compliance`.

**The tests.** `tests/test_compliance.py`, the nineteenth script — 146
checks: the policy (defaults, every overlay key, every refusal, the
precedence with the jurisdiction last, the region from the number, the
default region, stored settings that do not validate reported and the rest
applied, `COMPLIANCE_JURISDICTIONS` parsing); list entries (active,
expiry, revocation, sources); the gate over the in-memory store (allowed
and audited with the policy; the list refused before anything else with
the source, revoked allowed again; the status; the window in the policy's
zone and the prospect's own; a campaign switching the window off; the
campaign's ceiling exhausting; a jurisdiction's lower ceiling by the
number; a callback waiving the ceiling and never the list; the retry wait
deferred for exactly that long; an inactive campaign released; a store
without the list deciding on the status and warning once); the service
(marking writes the list row with the facts and keeps the first record; a
bare number listed and its prospects marked; a prospect created for a
listed number `DO_NOT_CALL` at birth; an import marking the listed and
leaving the rest; an unparseable number refused; removal keeping the row
and the statuses, reinstating when asked; the campaign's ceiling
exhausting; raising it reopening the queue; the voicemail delay; a
callback waiving the wait; at the ceiling a busy exhausting; a
jurisdiction's wait by the number; a `DO_NOT_CALL` outcome listing the
number as verbal with the attempt; `exhaust()`); the dialer with a carrier
stub that fails the check if reached (listed between reserving and
dialling → not dialled, `DO_NOT_CALL` attempt and disposition, prospect
marked, audited, nothing more handed out; a closed window deferred with
the count restored; a ceiling the reservation waived applied at the gate;
without a gate Phase 9's refusal); the briefing (the campaign's disclosure
in its words on the brief, first in the opening, in the system
instruction, audited; a jurisdiction adding its by the number; nothing
required → Phase 6's opening; an anonymous caller's opt-out by number; a
campaign call's with the ids; the queue closed to them); dispositions; the
API (listing normalised and marking, the actor a label, twice is the first
record, a viewer refused, audited, the prospect `DO_NOT_CALL`, a call for
it 409, `/dnc/check` for anybody, the list for `read_pii`, removal for
`manage` with reinstatement, 404, do-not-call on a prospect listing the
number, compliance settings read by a viewer, bad settings 422 with
nothing written, unknown key 422, good settings saved and reflected, a
viewer refused, audited); the boundary; and against PostgreSQL in a
throwaway schema: the row and its facts, one active row per number, the
queue handing a listed number to nobody, the outlook counting it
undialable, `apply_dnc_list`, counts, revocation keeping the row, the
queue open again, an expired entry not found and replaced, the
configuration merging per key and removing on None. `test_results.py`
gained three checks (`OPTED_OUT` from the state, after a goodbye, and
`DO_NOT_CALL` with no conversation); `test_campaigns.py`, `test_crm.py`
follow the disposition; `test_worker.py`'s `MemoryStore` gained the list,
`prospects_with_number`, `apply_dnc_list`, `update_campaign_configuration`
and the list in its eligibility.

**What was deliberately not built:** any encoded law (every rule is the
operator's, in `COMPLIANCE_JURISDICTIONS` and per campaign, and
`COMPLIANCE.md` says so at the top); contacting a registry (a file is
loaded; nothing is fetched); call recording (the flag governs the
disclosure only); verifying that the agent *spoke* the disclosure (the
instruction is recorded; the transcript and the eval suite are where a
spoken opening is checked); deriving a timezone from a number (Phase 9's
rule stands, for the reason it gave); a per-number override of the
attempt ceiling in the reservation SQL (the gate applies a jurisdiction's
lower figure after reserving and exhausts, which costs one reservation
and no dial); an expiry on the prospect's status (the list has one; the
status is the person's); a `SUPPRESSED` attempt status (a list refusal
closes the attempt as `DO_NOT_CALL`, which is what it is).

### Phase 18 (previous session)

**The brief:** authentication, authorisation and security hardening —
the dashboard and the APIs carry customer and lead information, so
protect them before production: a dashboard login; role-based
authorisation (admin / operator at least); the campaign, prospect, call,
transcript and analytics APIs protected; webhook authentication and
signature verification; input validation and sanitisation; rate limits on
public endpoints; no unauthorised access to transcripts and phone numbers;
every secret in an environment variable and never in a log; secure CORS;
production HTTPS requirements documented; audit logging of sensitive
actions; no raw customer information through an unauthenticated endpoint.

**What was already there, and stands.** Phase 17's API already demanded a
bearer key on every route but `/api/ping`, compared in constant time
against a rotatable list. Phase 14's webhook receiver already verified
the carrier's signature against the configured public URL (never the
tunnelled one) and refused anything else with 403 before reading it.
Phase 17's outbox already signed every delivery (`X-Aiva-Signature`, HMAC
over the timestamp and the exact body, five-minute tolerance). Phase 9's
scrubber already replaced every configured credential's value with `***`
in every log record, exception text included. Phase 10's dashboard already
escaped every string it rendered. Phase 18 is the parts that were missing
around those: *who* a key or a person is, *what* they may see, a door on
the one page that had none, a record of what was done, and the transport
and input hardening a production deployment needs.

**What was built — the security package** (`src/security/`, imported by
`config.py`, the dashboard, the API and the webhook receiver; never by the
bot):

| Module | Does |
|---|---|
| `roles.py` | `Role` (viewer / operator / admin), `Permission` (`read`, `read_pii`, `write`, `manage`), `ROLE_PERMISSIONS` as sets, `Principal` (`name`, `role`, `via`; `.can()`), `User`, `UserDirectory.parse("name:role:hash,…")` with every problem reported at startup, `authenticate()` in constant time (an unknown name is verified against a dummy hash) |
| `passwords.py` | scrypt from the standard library (`n=2^14, r=8, p=1`, 16-byte salt, 32-byte key; `scrypt$n$r$p$salt$hash`); parameters read from the hash, bounded, so a hostile `n` cannot ask for gigabytes; a malformed hash verifies False rather than raising; 8–1024 character passwords |
| `sessions.py` | a stateless signed cookie (`aiva_session`): `base64url(JSON claims).base64url(HMAC-SHA256)`, claims = user, role, iat, exp, nonce; signature checked before the body is parsed; `(None, reason)` for anything wrong; a 32-character minimum on the secret |
| `ratelimit.py` | a sliding-window limiter per key with a `Decision` (`allowed`, `remaining`, `retry_after_header`), bounded key count, `reset()`, a peek (`consume=False`), limit 0 = off |
| `pii.py` | `mask_phone` (`+92••••••••67`), `mask_email` (`h•••@example.com`), `redact_pii` — one walk over any JSON, keyed by field *name* (`phone`, `phone_normalized`, `to_number`, `email`, `attendee_email`, `transcript` → None with `transcript_included: false`, `custom_data` → `{}`), so a new serializer field is masked without a call at the route |
| `audit.py` | `AuditEntry`, `AuditLog.record(action, principal=, ip=, target=, **detail)`: an `audit_log` row through a duck-typed `store.record_audit` **and** an `audit.<action>` log line; detail scrubbed (phones masked, `redact`, fields *named* like a credential dropped, 400 chars, 24 keys); a store that cannot write → the action still goes ahead and `audit.unavailable` is logged once a minute, or `AuditUnavailable` in strict mode |
| `http.py` | a pure-ASGI `SecurityMiddleware`: `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, `Cache-Control: no-store`, a dashboard CSP (`default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; …`), HSTS once HTTPS is required; HTTPS enforcement (308 for a dashboard GET, 403 `https_required` otherwise); a body cap from `Content-Length` before buffering and again while streaming; `client_ip` / `effective_scheme` believing `X-Forwarded-*` only from `SECURITY_TRUSTED_PROXIES`, walked from the right; `origin_allowed` for the login/logout forms; `install_security(app, HttpPolicy, kind=)` adding CORS only when origins are listed (wildcard refused at config time) |

**What was built — configuration** (`config.py`): `SecurityConfig`
(`DASHBOARD_USERS`, `DASHBOARD_AUTH_DISABLED`, `DASHBOARD_SESSION_SECRET`,
`DASHBOARD_SESSION_TTL_SECS`, `SECURITY_REQUIRE_HTTPS`,
`SECURITY_TRUSTED_PROXIES`, `SECURITY_CORS_ORIGINS`,
`SECURITY_API_RATE_LIMIT` 300, `SECURITY_ANON_RATE_LIMIT` 30,
`SECURITY_LOGIN_RATE_LIMIT` 5, `SECURITY_MAX_BODY_BYTES` 5 MiB,
`SECURITY_AUDIT_ENABLED`, `SECURITY_AUDIT_STRICT`; `.defaults()` for a
check that builds settings by hand; `describe()` never a secret) as
`Config.security`; `AutomationConfig` gained `operator_api_keys`,
`viewer_api_keys`, `docs_enabled` (`AUTOMATION_DOCS_ENABLED`) with defaults,
`role_for_key()` (constant time across every key of every role, returning
the role and a label such as `operator-key#2`), a refusal of a key in two
lists, and `api_enabled` meaning any role's key.

**What was built — the dashboard** (`src/dashboard/web.py`, `page.py`):
`GET /login` (a form; already signed in → home), `POST /login` (same-site
origin required, `SECURITY_LOGIN_RATE_LIMIT` per address, "Wrong name or
password" whichever it was, the cookie HttpOnly / SameSite=Strict / Secure
when HTTPS is required / `Max-Age` = TTL, a redirect to a same-site `next`
only), `POST /logout`; `/` redirects to the login, `/api/dashboard` and
`/api/me` answer 401 with `{"login": "/login"}`; a session **or** an API
key from the automation lists is a principal; a viewer's JSON goes through
`redact_pii` and carries `masked: true`, and the page says "numbers masked";
the header names who is signed in with a sign-out button (none for a key
holder); the page redirects to the login on a 401 from its fetch; no docs,
no OpenAPI; `create_app(config, store_factory=, clock=)` so the checks run
without a database; a `ConfigError` at build time with nobody in
`DASHBOARD_USERS`; `dashboard.py` refuses a non-loopback `--host` under
`DASHBOARD_AUTH_DISABLED`.

**What was built — the API** (`src/automation/api.py`): `require_key`
resolves a `Principal` (role from the key list) onto `request.state`,
spends the address's anonymous budget *before* auditing a refusal (so
guessing keys cannot fill the table), and the key's own budget after;
`need(Permission)` dependencies on every route (`403 forbidden` naming the
missing permission, audited); a `_PiiRoute` route class that masks every
JSON answer for a principal without `read_pii`; `require_pii` inside the
routes a viewer may read whose *option* is not for them (`include=transcript`,
`include=payload`, a lookup by number); `GET /results/{id}` and
`GET /events/{id}` need `read_pii`; `complete` / `cancel`, `events/{id}/retry`
and the new `GET /api/v1/audit` need `manage`; every write and every
transcript read audited (`prospect.create`, `prospect.import`,
`prospect.do_not_call`, `campaign.create`, `campaign.add_prospects`,
`campaign.<verb>`, `call.queue`, `callback.schedule`, `callback.cancel`,
`event.retry`, `pii.transcript_read`, plus `auth.refused`,
`auth.rate_limited`, `auth.forbidden`); validation past lengths —
control characters refused in every text field (newlines only in a
description or a note), `custom_data` plus extras ≤ 16 KiB and ≤ 100 keys,
import rows ≤ 60 columns and ≤ 2,000 characters a cell, path ids `≥ 1`,
query strings bounded, `Idempotency-Key` `[A-Za-z0-9._:-]{1,200}`, `kind`
a real event kind; `ApiError` carries headers (`Retry-After` on a 429);
`install_security(kind="api")`; docs and schema off under
`AUTOMATION_DOCS_ENABLED=false`; `/status` reports the principal and the
audit counters; `API_VERSION` 18.

**What was built — the webhook receiver** (`src/campaigns/webhooks.py`):
the standalone app gets `install_security(kind="webhook")` (headers, HTTPS,
a 1 MiB cap, no CORS); the router — both the bot-mounted and the standalone
form — counts every 400 / 403 per client address and answers 429 before
computing the next signature once `SECURITY_ANON_RATE_LIMIT` refusals a
minute are reached, and refuses an oversized `Content-Length` outright.
The signature check itself is Phase 14's, unchanged.

**What was built — the rows and the tools:** `audit_log` in
`CampaignStore.create_schema` (`uv run campaign.py init`, idempotent) with
`record_audit`, `list_audit(action= prefix, actor=, since=, before_id=, limit=)`,
`audit_counts`; `campaign.py audit [--action] [--actor] [--since-hours] [--limit]`;
`security.py` (`hash-password`, `make-key --role`, `make-secret`, `check
[--strict]` — the posture line by line, including whether `.env` is
tracked by git); `SECRET_ENV` gained the role-scoped keys, the session
secret and `DASHBOARD_USERS` (each entry scrubbed on its own), and the
patterns gained a password-by-name, a session cookie and a scrypt hash;
`server/.gitignore` and `git rm --cached .env` in the nested repository
(staged, not committed — see [Pending tasks](#4-pending-tasks));
`SECURITY.md`; a SECURITY section in `.env.example`; README and
`n8n/README.md` updated.

**The realtime pipeline.** `bot.py`, `src/conversation/`, `src/actions/`,
`src/telephony/`, `src/campaigns/worker.py`, `dialer.py`, `service.py` did
not change. `test_security.py` greps every module on the call path for an
import of `src.security` and asserts `security.py` imports no pipecat and
no bot.

**The tests.** `tests/test_security.py`, the eighteenth script — 214
checks: passwords (verify, wrong, fresh salt, malformed, hostile `n`,
lengths, the dummy hash); sessions (round trip, expiry, tampered signature,
swapped body, wrong secret, garbage, size, a short secret); roles and the
directory (every permission, parse, a plain password refused, unknown role,
duplicate, bad name, constant-time authenticate); the limiter (window,
`Retry-After`, slide, reset, peek, off, bounded keys); masking (every
shape, nested, the original untouched); the scrubber (every new secret,
hash, cookie and password by shape); the HTTP layer (trusted-proxy walking
for address and scheme, bad networks, wildcard CORS, origin checks, and on
a two-route app: headers, CSP, body cap 413, HTTPS 403 / 308 / forwarded OK
/ HSTS, CORS listed vs unlisted vs unconfigured); the audit writer (row and
line, masked phone, dropped password, nobody, a broken store non-strict
once-a-minute, strict raising, no store); the dashboard (redirect, 401,
form, CSP, ping, no docs, wrong password, unknown user without the typed
name in the row, no password in any log line, the fourth attempt 429,
cross-site 403, the right password → cookie flags, audited by name, the
token in no log line, the page naming the user, `/api/me`, admin unmasked,
POSTs refused, logout, forged and expired cookies, a viewer masked on the
JSON and on the page, viewer / operator keys, an unknown key, anonymous
limit, only two POST routes, no users → `ConfigError`, auth off →
anonymous operator); the API (each key's role, headers, viewer write 403
audited, operator create audited without the number, full vs masked
prospect, lookup by number, every transcript / payload route 403, campaign
create / start / complete by role, retry and audit by role, the log
newest-first with counts and a prefix filter and no key in it, control
characters, newline, phone with a control character, an unparseable phone
still 201, oversized and too-many custom data, negative id, bad
idempotency key, unknown kind, 413 over the body cap, 401 → 429 per
address with refusals audited only within budget, the ping sharing that
budget, per-key 429 with `Retry-After`, another key unaffected, no key in
any log line, HTTPS required 403 / forwarded OK, docs off); the webhook
router (403 then 429 before the next signature, an oversized body); the
boundary. `test_dashboard.py`'s route checks sign in first (five new
checks; the login and logout land in the real `audit_log`). `test_automation.py`
is unchanged and passes: an admin key behaves exactly as Phase 17's key did.

**What was deliberately not built:** multi-factor authentication and a
password-reset flow (users are operators configured by whoever runs the
deployment); a session revocation list (a session ends at expiry or when
the secret is rotated; keep the TTL short if that matters); a distributed
rate limiter (in-process, like Phase 9's pacing and Phase 11's cache);
encryption of transcripts at rest (PostgreSQL's and the disk's business);
a users table (users live in the environment with the other secrets, so a
deployment has one place to manage them and this repository can never
commit one); per-user API keys (keys are per role; the audit log records
the key's label); a `health.py` component (`security.py check` is the
posture tool); hardening of the bot's own dev-runner web server
(`/client`, `/ws` — a development surface; the tunnel exposes only what the
carrier needs); TLS termination in-process (a proxy's job, documented in
`SECURITY.md`); and verification of the *dashboard's* aggregates under a
login (the aggregates are `test_dashboard.py`'s, unchanged).

### Phase 17 (previous session)

**The brief:** n8n automation integration, with n8n kept *outside* the
realtime audio pipeline:

```
CSV / CRM / event → n8n → AI agent API → outbound call → webhook → n8n → CRM / calendar / notifications
```

Documented API endpoints n8n can use to create/import prospects, create
campaigns, start/pause/resume them, trigger a call, schedule a callback and
retrieve call results; authenticated webhook endpoints for call completion
events; every external event idempotent; example n8n workflows for CSV
intake → campaign, campaign → outbound calls, completed call → CRM update,
qualified lead → notification, meeting booked → CRM update, callback due →
call; n8n asynchronous and outside the voice path; the endpoints and the
environment documented. STT, LLM, TTS, VAD and turn detection not moved.

**What was already there, and stands.** Every operation the API exposes
existed as a `CampaignService` / `CampaignStore` call behind `campaign.py`
(Phase 5's docstring said "when a web or n8n front end arrives it will
call the same service, not this file" — it does). The pending-callback row
(Phase 7) was already the mechanism by which "phone this person at this
time" becomes a call the scheduler (Phase 13) places ahead of the queue.
The call result (Phase 8) was already the CRM-ready export shape. Phase 15's
`CrmSyncer` was already the shape of "tell an external system about a row
once, from a separate process, with backoff". Phase 17 is those four things
with an HTTP surface in front and an outbox behind.

**What was built — the API** (`src/automation/api.py`, served by
`automation.py` on `127.0.0.1:7890`, OpenAPI at `/api/v1/docs`):

| Route | Does | Idempotent by |
|---|---|---|
| `POST /api/v1/prospects` | one prospect, number normalised as an import does | phone: the same number is 200 + the existing row |
| `POST /api/v1/prospects/import` | JSON rows *or* a `text/csv` body through the Phase 5 importer (aliases, normalisation, duplicates, report), optionally into a campaign (created on request) | phone per row |
| `GET /prospects`, `/prospects/{id}`, `POST /prospects/{id}/do-not-call` | read; never-call (cancels the pending callback) | `changed` |
| `POST /api/v1/campaigns` | in DRAFT | name (any case): 200 + the existing one |
| `GET /campaigns[/{id or name}[/prospects]]` | with counts and the Phase 13 queue outlook | — |
| `POST /campaigns/{ref}/prospects` | by ids, by numbers, or all | membership `ON CONFLICT` |
| `POST /campaigns/{ref}/start\|pause\|resume\|complete\|cancel` | the transition table `_TRANSITIONS`: target status already → `changed: false`; a status it may not leave → 409 `invalid_transition` with `allowed_from` | the status |
| `POST /api/v1/calls` | **queue a call now**: the prospect (id or number) joins the campaign if absent, a pending callback row due now is written, 202 with `dialled_by` and `warnings` (campaign not ACTIVE, on a call now, membership closed, a pending callback moved) | one pending callback per prospect |
| `POST /api/v1/callbacks` | the same with `scheduled_at` required (not past, within `CALLBACK_MAX_DAYS_AHEAD`) | as above |
| `GET /calls`, `/calls/{attempt}`, `/callbacks`, `DELETE /callbacks/{id}` | attempts, one attempt + result + transfers, the diary, withdraw | `changed` |
| `GET /results`, `/results/{attempt}`, `/meetings` | Phase 8's export, transcript on request, `since` and `before_id` paging | — |
| `GET /events`, `/events/{id}`, `POST /events/{id}/retry`, `GET /status` | the outbox | — |

Plus the `Idempotency-Key` header on any POST: the answer is stored in
`api_requests` (route + key unique, a fingerprint of the body) and
replayed with `Idempotent-Replayed: true`; the same key with a different
body is 422. Auth is `Authorization: Bearer` (or `X-API-Key`) against
`AUTOMATION_API_KEYS`, every configured key compared in constant time so
rotation has no gap; the app refuses to build without a key. Errors are one
JSON shape with a code. A `+05:00` offset arriving as ` 05:00` in a query
string (n8n rarely encodes the plus) is read as the plus it was.

**What was built — the outbox** (`src/automation/events.py`, the
`automation_events` table, `store.claim_automation_events`):

| Kind | Created from | Key |
|---|---|---|
| `call.completed` | a `call_results` row unchanged for `AUTOMATION_SETTLE_SECS` (30 s) | `call.completed:result:<id>` |
| `call.updated` | a result changed after its `call.completed` was closed; one open per result | `…:<id>:<epoch of the change>` |
| `lead.qualified` | a settled result with `qualification_status = QUALIFIED` | `lead.qualified:result:<id>` |
| `meeting.booked` | a `meetings` row, at once | `meeting.booked:meeting:<id>` |
| `callback.scheduled` | a pending `callbacks` row; a moved one is a new event | `…:<id>:<epoch due>` |
| `campaign.completed` | a campaign the scheduler closed | `…:<id>:<epoch>` |

The claim is Phase 15's shape: one transaction, six `INSERT … SELECT …
ON CONFLICT (event_key) DO NOTHING` statements that create what the rows
now justify, then `FOR UPDATE SKIP LOCKED` over `PENDING` / due `RETRY` /
stale `DELIVERING` rows, oldest fact first, `attempts + 1`. The deliverer
builds the payload *at delivery* from the rows (prospect, campaign, call,
result, transfers, meeting, callback — the API's own shapes, from
`serialize.py`), POSTs it with `X-Aiva-Event`, `X-Aiva-Event-Id`,
`X-Aiva-Delivery`, `X-Aiva-Timestamp`, `X-Aiva-Signature: t=…,v1=<HMAC-SHA256
over "t." + body>` when `AUTOMATION_WEBHOOK_SECRET` is set, and a static
header (`X-Aiva-Key` by default) for n8n's native Header Auth. 2xx →
`DELIVERED` with the payload, target, status and the result's version
kept on the row; a timeout, no connection, 408/425/429/5xx and 404 (n8n's
answer for an inactive workflow) → `RETRY` with backoff doubling from
`AUTOMATION_RETRY_SECS` to `AUTOMATION_MAX_RETRY_SECS`, jittered, with
`Retry-After` as a floor *after* the jitter, for `AUTOMATION_MAX_ATTEMPTS`
passes then `FAILED`; any other 4xx → `FAILED` at once with the body's first
line. `campaign.py events` / `events-retry` list and reopen (a reopened
row gets a fresh attempt budget). Per-kind URLs
(`AUTOMATION_WEBHOOK_URL_<KIND>`), `AUTOMATION_EVENTS` to choose kinds,
`AUTOMATION_EVENTS_SINCE` to bound a first run on a database with history.

**Where it landed:** `src/automation/` (`auth.py`, `serialize.py`,
`events.py`, `api.py`, `__init__.py`), `automation.py` (serves the API and
runs the deliverer; `--once`, `--no-deliver`), `campaigns/models.py`
(`AutomationEventState`, `AutomationEvent`, `ApiRequestRecord`),
`campaigns/store.py` (two tables, `_EVENT_CREATE_SQL`,
`claim_automation_events`, `record_automation_event`, `get_/find_/list_`,
`automation_event_counts`, `retry_automation_events`, `get_/save_/purge_api_request`,
`get_meeting`, `list_call_results(since=, before_id=)`,
`list_callbacks(campaign_id=)`), `campaigns/__init__.py`, `config.py`
(`AutomationConfig`, `AUTOMATION_EVENT_KINDS`, `_url`, `_moment`,
`Config.automation`), `reliability/observability.py` (the new secrets
scrubbed, a comma-separated key list scrubbed per key), `campaign.py`
(`events`, `events-retry`), `.env.example` (an AUTOMATION section),
`n8n/README.md` (the reference), `n8n/workflows/01…06.json`.

**The realtime pipeline.** `bot.py`, `src/conversation/`, `src/actions/`,
`src/telephony/`, `src/crm/`, `src/scheduling/` and `src/dashboard/` did
not change. `src/campaigns/` gained rows and reads and no behaviour: the
worker, the dialer, the service and the briefing are untouched.
`test_automation.py` greps every module on the call path for an import of
`src.automation` and asserts `automation.py` imports no pipecat and no bot.

**The tests.** `tests/test_automation.py`, the seventeenth script —
245 checks: keys (bearer, `X-API-Key`, constant-time against
several) and signatures (verified, tampered, wrong secret, stale, malformed,
tolerance off); every configuration knob including the per-kind URLs, the
unknown-kind and short-key problems, and that `describe()` never carries a
secret; the API over the fake store through FastAPI's test client — 401,
ping without a key, prospects (create, same number twice, unusable number,
validation), `Idempotency-Key` (replay, reuse, race), import (JSON rows,
CSV body, re-import, dry run, unusable columns, unknown campaign),
campaigns (create, same name, add by id/number/all, every transition and
its refusal), calls and callbacks (every 422 and 409, the pending callback
moved and the move reported, joining a campaign on the way, do-not-call
cancelling it), the diary (pending, due, all, by campaign, withdraw), then
**the real `CampaignWorker` and `CampaignDialer` over the same store placing
exactly the call the API queued** — as a callback, through the scripted
carrier, followed to its end, the callback `PLACED`, nothing else dialled —
and the result read back through the API (list, one, paging, `since` with
an unencoded plus, a Z suffix, a naive time), the outbox routes, a
database that goes away (503) and comes back; the deliverer — the first
pass before the settle window, the headers, the signature verified against
the exact bytes, the auth header, every payload, oldest first, nothing
twice, `call.updated` once per change and never while one is open, the
transcript on request, a missing transfers table, 503 / 429 with
`Retry-After` / no response → `FAILED` after the budget / 400 at once / 404
retried / a sender that raises / a database that goes away, unsigned when
no secret, `run()` pacing and stop, a stop mid-pass releasing the rest
unspent, one kind creating only that kind, an unknown kind refused; the
boundary; and, against PostgreSQL in a throwaway schema, both tables, the
settle window, every kind's creation, `call.updated`'s three rules, a moved
callback, a finished campaign, retry timing, the stale reclaim, counts,
retry, `since`, four concurrent claims disjoint, the idempotency ledger,
the read extensions, the API over the real store (its own pool: the test
client's loop is not the check's) and the deliverer over real rows.

**What was deliberately not built:** dialling from the API (the scheduler
is the one place that decides *when*; an HTTP handler that reached the
carrier would bypass the calling window, the concurrency limit and the
pacing, and hold a request open across a carrier round trip); n8n verifying
the HMAC in the example workflows (n8n's Code node needs
`NODE_FUNCTION_ALLOW_BUILTIN=crypto` and the Webhook node's raw-body option;
the check is documented in `n8n/README.md` and the examples use the static
header n8n verifies natively); a `health.py` component for n8n (a webhook
URL cannot be probed without firing the workflow, and the `/api/ping` and
`campaign.py events` counts say what a monitor needs); a Data Table of seen
`event_id`s inside the workflows (n8n's schema for it varies by version;
workflow 03 writes the id into the CRM note instead); per-callback control
of the attempt-limit override (a worker-level setting since Phase 13).

### Phase 16 (previous session)

**The brief:** replace or complete the local and stub parts of meeting
booking and human transfer with production-ready integrations, without
recreating the actions: the selected calendar provider integrated, real
availability, atomic booking, no double booking, the external booking id
stored, failures and unavailable slots handled; the existing
`transfer_to_human` verified with the carrier, the caller sent to the
configured number, failure handled, the transfer's status and outcome
recorded, the realtime pipeline not blocked; mocks and tests; the environment
for a real test documented; the conversation logic unchanged.

**What was already there, and stands.** Phase 7 built both actions properly:
a `CalendarProvider` seam with a real local calendar and a Cal.com client
against API v2; `book_meeting` that reports success only after the durable
write and stores Cal.com's uid as `meetings.reference`; `transfer_to_human`
as a blind redirect through the carrier's live-call update with an inline
fallback. Calendly was rejected then (its API cannot create a booking) and
stays rejected. What Phase 16 changed is what those did *when the world did
not cooperate*.

**Calendar — what changed:**

| | Before | Now |
|---|---|---|
| A Cal.com request that hangs | aiohttp's five-minute default, inside a tool turn | `CALCOM_TIMEOUT_SECS` (15) on every request |
| A booking whose answer was lost | The caller was told it failed; Cal.com may have booked it | `find_booking` lists the attendee's bookings around that start (`GET /bookings`, email + event type + a one-minute window) and adopts the one Cal.com made; only a definite miss is reported as unavailable, and the POST is never repeated |
| A slot taken between offer and write, local calendar | Check then write, not transactional (a documented known issue) | An exclusion constraint on `meetings` over `tstzrange(start_at, end_at)` for live local bookings; the write refuses the overlap, `add_meeting` raises `MeetingConflictError`, the service answers `slot_taken`. Five simultaneous bookings of one slot yield one row |
| A slot taken, Cal.com | Read from the error message | Unchanged in mechanism; the markers gained Cal.com's `no_available_users_found_error` and `booking_time_out_of_bounds_error` |
| A wrong key or event type id | Found on the first call, mid-booking | `check_credentials` reads `GET /event-types/{id}` back; `uv run health.py calendar` reports it, and marks the component degraded when the event type's length differs from `CALENDAR_SLOT_MINUTES` |

**Transfer — what changed:**

| | Before | Now |
|---|---|---|
| What the carrier is told | `<Dial>` then an inline `<Say>` fallback and `<Hangup/>` | When a webhook receiver is configured: `<Dial action="<receiver>" method="POST" timeout="…">` and nothing after it — the carrier reports how the colleague's leg ended and asks what to do next. Without one, the Phase 7 markup, unchanged |
| The outcome | Unknown; the bot left the call when the carrier took it | The receiver decodes `DialCallStatus` / `DialCallSid` / `DialCallDuration` as a `transfer` event, records it on `call_transfers` (`ANSWERED`, `NO_ANSWER`, `BUSY`, `FAILED`, `CANCELED`, with the duration), and answers with TwiML: a hang-up if the colleague answered, the fallback sentence and a hang-up if not |
| The record | `transferred=true` on the result | Plus a `call_transfers` row: `REQUESTED` written by the action service the moment the carrier accepts (guarded, five-second ceiling, never holds the turn), completed by the report. `campaign.py transfers` lists them |
| The ring time | 30 s, hard-coded | `TELEPHONY_TRANSFER_TIMEOUT_SECS` |

**Where it landed:** `telephony/base.py` (`WEBHOOK_TRANSFER`,
`WebhookEvent.dial_call_id` / `transfer_answered`, `build_transfer_twiml(action_url=)`,
`transfer_response_twiml`, `transfer_call(action_url=, timeout_secs=)`),
`telephony/twilio.py` (the `<Dial action>` report parsed first, because it
also carries the parent call's status), `scheduling/base.py`
(`find_booking`, `check_credentials`), `scheduling/calcom.py`,
`scheduling/local.py` (docstring: the constraint), `campaigns/models.py`
(`TransferStatus`, `CallTransfer`, `WebhookOutcome.TRANSFER`),
`campaigns/store.py` (the constraint under a savepoint so an older database
with overlapping rows still initialises, with a warning; `MeetingConflictError`;
`call_transfers` and `add_transfer` / `complete_transfer` / `list_transfers`
/ `transfer_counts`), `campaigns/webhooks.py` (`_apply_transfer`, TwiML on
every answer to a transfer report — a duplicate included, because the carrier
acts on the body), `actions/service.py` (`transfer_action_url`,
`transfer_timeout_secs`, `_record_transfer`, `MeetingConflictError` →
`slot_taken`), `actions/__init__.py`, `config.py`, `reliability/health.py`
(`calendar`), `campaign.py transfers`, `.env.example` (a "what a live test
needs" section).

**The realtime pipeline.** Nothing in `bot.py` or `src/conversation/` was
edited. The transfer's REST call was already inside the tool turn (the model
waits for the result) and is bounded by the carrier client's 20 s; the new
row write is `guarded` at 5 s and cannot fail the transfer; the outcome is
processed by the webhook receiver, which is off the call path by Phase 14's
construction.

**The tests.** `tests/test_booking_transfer.py`, the sixteenth script — 99
checks: the transfer TwiML with and without a receiver, and the two answers;
the carrier's live-call update; the `<Dial>` report decoded (kind, leg id,
duration, key, answered); the receiver over the fake store — answered, every
unhappy ending, a duplicate that still answers TwiML, a second report that
does not rewrite, a report with no request row, a report for a call never
placed, an unknown status, a forged report, the HTTP route answering XML
where a status event answers text; the action service — the receiver and
ring time passed through, the `REQUESTED` row with its ids and reason, a
failing and a slow database neither failing nor holding the transfer, refusals
recording nothing, the second transfer refused, a local booking, a conflict
as `slot_taken`; Cal.com over a stub — the timeout on every request, a lost
answer looked up and adopted, not found, a 5xx likewise, cancelled or
other-time bookings ignored, the lookup failing too, four taken-slot messages,
the event type read back and a length mismatch; configuration and the
calendar health component; and, against PostgreSQL, the constraint (overlap
refused, adjacent allowed, a Cal.com mirror allowed, a cancelled booking
freed, five concurrent bookings), the transfers table, and the receiver over
real rows. `test_actions.py`'s fake carrier gained the two arguments and its
fake store the transfer method; `test_scheduling.py`'s stub session records
the timeout. No assertion in either changed.

**What was deliberately not built:** a warm transfer (still a different
carrier API, and still nobody has watched a blind one succeed); Calendly
(cannot book); a Cal.com cancellation on a failed local mirror (the booking
exists and the person will receive it — the log line says so, as Phase 7
decided); a HubSpot meeting engagement (Phase 15's body and `ai_meeting_at`
carry it); the transfer outcome on the call result's own row (the result is
written by the conversation before the outcome exists; `call_transfers` is
the record, and joins on the attempt).

### Phase 15 (previous session)

**The brief:** let completed calls synchronise their final CRM-ready
information to an external CRM — a provider-agnostic adapter, HubSpot first;
prospect/contact mapping; after a completed call, the outcome, qualification,
pain points, objections, summary, meeting status, next action and callback;
asynchronous so it never affects call latency; retries for temporary CRM
failures; idempotent; status and errors in the database; credentials in the
environment; the layer swappable for Salesforce/Pipedrive later; the realtime
pipeline untouched; tests with mocked CRM responses for the successful,
failed, duplicate and retry cases. Architecture: agent → database → CRM
adapter.

**One new package, `src/crm/`, in the layers it belongs to:**

| Module | What |
|---|---|
| `crm/base.py` | The contract. `CrmProvider` (six methods: `find_contact`, `create_contact`, `update_contact`, `find_activity`, `create_activity`, `update_activity`, plus optional `ensure_schema` / `check_credentials`), the neutral `CrmContact`, `CallActivity` (key, title, body, `CallOutcome`, when, duration, numbers, and a `fields` mapping of every structured fact by neutral name), `CallSync`, `SyncReceipt`; `CrmError` / `CrmUnavailableError` (retryable, `retry_after_secs`) / `CrmAuthError` / `CrmRejectedError` (`status`). No vendor |
| `crm/mapping.py` | `build_call_sync(result, prospect, campaign, attempt, from_number)` — the `CallResult` read for a CRM. Pure. The body is headed sections (Outcome, Summary, Qualification, Pain points, Objections, Questions, Discovery, Meeting, Callback, Next action, Actions taken, Notes, Record issues) that restate fields and *name* an unrecorded one; every fact also lands in `fields`. `sync_key(result_id, attempt_id)` = `aiva12x34`, alphanumeric, written into the body |
| `crm/hubspot.py` | The only vendor file. Contacts: search by email (`EQ`), then phone (`EQ`, candidates checked digit-wise with the trunk zero stripped, because HubSpot's phone search is area-code-and-local), create (a 409 "Existing ID" is the answer), `PATCH` of `ai_*` custom properties. Calls: `POST /crm/v3/objects/calls` with `hs_timestamp` (ms), title, body ending `ref <key>`, `OUTBOUND`, `hs_call_status`, `hs_call_duration` (ms), the numbers, and HubSpot's six fixed disposition ids; associated to the contact with the HubSpot-defined type 194; `PATCH` to update; search `hs_call_body CONTAINS_TOKEN <key>` to find a create whose answer was lost. `ensure_schema` creates the thirteen `ai_*` contact properties that are missing. Bearer token; `describe()` shows its tail. 401/403 → auth, 429 (with `Retry-After`) / 5xx / timeout / no connection → unavailable, other 4xx → rejected with HubSpot's words |
| `crm/sync.py` | `CrmSyncer`: `run_once()` claims a batch (`store.claim_results_for_sync`), files each (`find_contact` → `create_contact` → [`find_activity` on a retry] → `create_activity` or `update_activity` → `update_contact`), records the outcome; `run(poll_secs, once)` loops. Reads go through `call_with_retry(READ_POLICY)`; writes through `WRITE_POLICY` (one attempt, 30 s) with `write_classifier`, so a lost answer is `AmbiguousOutcomeError` and the next pass searches before it creates. Transient → `RETRY` with `retry_secs × 2^n` capped and jittered, honouring the CRM's `Retry-After`, up to `max_attempts` then `FAILED`; rejected → `FAILED` at once; a rejected token → the pass stops and the claimed rows are handed back with no attempt spent |
| `crm/__init__.py` | `make_crm_provider(config.crm)` — the one point that knows which CRM |
| `campaigns/models.py` | `CrmSyncState` (`PENDING`, `SYNCING`, `SYNCED`, `RETRY`, `FAILED`, `SKIPPED`), `CrmSyncRecord` |
| `campaigns/store.py` | `crm_sync` (unique `call_result_id`, CASCADE with the result; `sync_key`, both external ids, `attempts`, `last_error`, `next_attempt_at`, `started_at`, `synced_at`, `result_updated_at`); `claim_results_for_sync` (one transaction: insert a `PENDING` row for every result without one, keyed in SQL; then `FOR UPDATE SKIP LOCKED` over `PENDING`/due `RETRY`/stale `SYNCING`/`SYNCED`-but-result-updated rows → `SYNCING`, `attempts + 1`), `record_crm_sync`, `get_crm_sync`, `list_crm_sync`, `crm_sync_counts` (with `UNSEEN`), `retry_crm_sync`; `_phase15` |
| `config.py` | `CrmConfig` (`Config.crm`): `CRM_PROVIDER` (`none` / `hubspot`), `HUBSPOT_ACCESS_TOKEN` (demanded only when selected, with the scopes named), `HUBSPOT_API_BASE`, `CRM_CUSTOM_PROPERTIES`, `CRM_SYNC_UNANSWERED`, `CRM_SYNC_POLL_SECS`, `CRM_SYNC_BATCH`, `CRM_SYNC_MAX_ATTEMPTS`, `CRM_SYNC_RETRY_SECS`, `CRM_SYNC_MAX_RETRY_SECS`, `CRM_SYNC_STALE_SECS`; `describe()` in the startup line as `CRM=` |
| `campaign.py` | `crm-sync [--once] [--limit] [--retry-failed]`, `crm-status [--state] [--limit]`, `crm-retry --result N | --all-failed` |
| `reliability/health.py` / `health.py` | A `crm` component: the token reads one contact; skipped when no CRM is configured |
| `reliability/observability.py` | `HUBSPOT_ACCESS_TOKEN` scrubbed from every log line |

**Architecture: agent → database → CRM adapter, and nothing shorter.** The
bot writes the `CallResult` at the end of a call exactly as since Phase 8 and
knows nothing about a CRM; `campaign.py crm-sync` is a third process beside
the bot and the worker, and it reads the row later. That is what makes the
sync asynchronous in the only sense that matters here — no CRM request can
add a millisecond to a turn — and `test_crm.py` asserts the arrow's direction:
nothing in `bot.py`, `src/conversation/`, `src/campaigns/`, `src/telephony/`
or `src/reliability/` (bar the health check's lazy import) imports `src/crm/`,
and `src/crm/` imports no Pipecat.

**Idempotent at three layers.** One `crm_sync` row per result, unique, claimed
under `SKIP LOCKED` — two syncers never file the same result and a `SYNCED`
row is not claimed again until its *result* changes (the conversation's rich
result replacing the carrier's thin one), when the same activity is updated.
The CRM ids are recorded the moment each is learned, so a crash between the
contact and the activity resumes with the contact. And a create whose answer
was lost is not repeated: the key in the body is searched for first, and only
a miss creates — Phase 9's ambiguous placement, in another vendor's clothes.

**What was sent, on a real result**, in the checks: a booked meeting for Sara
Ali becomes contact `C1` (email-matched or created with name, phone, email,
company, title) and activity `A2` titled `AI call — meeting booked — Sara Ali
(Q1 Outreach)`, Connected, 290 s, with a body that restates the summary, the
qualification (qualified / interested / decision role), each pain point and
objection with whether it was handled, the questions asked, discovery facts,
`Booked for Tue 08 Sep 2026, 15:00 PKT (ref bk_abc123)`, the callback line
(`Not discussed, or not recorded.` when nothing was), the next action, the
actions taken, the notes, the campaign, the ids and `ref aiva12x34`; and the
contact's `ai_qualification_status`, `ai_next_action`,
`ai_last_call_disposition`, `ai_last_call_at`, `ai_meeting_at`,
`ai_pain_points`, `ai_objections`, `ai_last_call_summary`, `ai_campaign`.

**The tests.** `tests/test_crm.py`, the fifteenth script — 179 checks: the
mapping (the key, the contact, the title, every disposition's outcome, every
body section including the ones that name an unknown, the fields, callbacks
scheduled/requested, meetings agreed, a prospect with no identity, an unstored
result); the HubSpot adapter over a stub session (email then phone search with
the digit check, create and the 409 answer, the `ai_*` PATCH with millisecond
dates, the schema created only where missing and never with custom properties
off, the engagement with association 194 and each built-in disposition id, the
update, the key search, 401/429-with-Retry-After/5xx/timeout, the credential
check); the syncer over a fake store with `MockCrm` — successful; duplicate
(a second pass, a fresh syncer); the result changed after filing (updated, not
created); an existing contact; transient failure then retry (the base wait
jittered, the contact kept, the search before the create); a create whose
answer was lost (found by key, no duplicate) and likewise a contact; permanent
failure and `crm-retry`; out of attempts with the doubling and `Retry-After`
honoured; a rejected token stopping the pass and handing rows back unspent;
unanswered calls skipped or filed; no identity / no prospect; the schema
refused; the database gone; a bug in one row; the run loop; the boundary; the
configuration — and the table, the claim, the re-sync on a changed result,
retry timing, stale claims, four concurrent claims disjoint, counts, retry,
and the real syncer over the real rows, against PostgreSQL in a throwaway
schema.

**What was deliberately not built:** a push from the sink at the end of the
call (a CRM outage would then cost the call's own teardown; the poller
survives it — the handoff's own recommendation); HubSpot meeting or task
engagements for a booked meeting or a callback (the body and the contact's
`ai_meeting_at` / `ai_callback_at` carry them; separate objects are a
per-portal workflow decision); `hs_lead_status` or `lifecyclestage` writes
(opinionated on somebody else's contact record; the `ai_*` properties are
clearly ours); a Pipedrive or Salesforce adapter (three edits each, per
`crm/__init__.py`; untested code against no account would be a pretence); the
worker running the sync in its loop (its import boundary is a check).

### Phase 14 (previous session)

**The brief:** receive the carrier's authoritative call events instead of
relying only on polling — HTTP webhook endpoints for the configured carrier;
the lifecycle events (initiated, ringing, answered, completed, failed, busy,
no-answer, canceled); the carrier's official signature validation; idempotent
processing so a duplicate cannot corrupt call state; the existing rows
updated; reconciliation with polling as the fallback; structured logs and
error handling; nothing in the realtime audio path; carrier code isolated so
Twilio and SignalWire stay swappable; tests for valid, invalid, duplicate and
out-of-order events. Built onto the Phase 4 telephony layer and the Phase 9
write, not beside them — `store.apply_call_event` was written for this and
is the entry point it was meant to be.

**What was built, in the layers it belongs to:**

| Layer | What |
|---|---|
| `src/telephony/base.py` | `WebhookRequest` (one delivery, carrier-neutral: the *configured* URL, lower-cased headers, the form), `WebhookEvent` (provider, call id, kind `status`/`amd`, normalised `CallStatus`, the raw word, sequence, timestamp, duration, error, verdict; `key`, `to_snapshot()`), `WebhookError` / `WebhookSignatureError`, `CallRequest.status_callback_url`, `TelephonyProvider.supports_webhooks` / `verify_webhook` / `parse_webhook` (defaults: cannot), `webhook_url()` |
| `src/telephony/twilio.py` | `place_call` sends `StatusCallback`, `StatusCallbackMethod=POST` and four repeated `StatusCallbackEvent` fields when the request carries a URL (plus `AsyncAmdStatusCallback` with async AMD); `compute_signature` — base64 HMAC-SHA1 over the URL then every field's name and value in name order, keyed by the auth token, reproducing Twilio's documented example byte for byte; `verify_webhook` (constant-time, the URL with and without its default port as Twilio's own libraries allow, `AccountSid` must be this account); `parse_webhook` |
| `src/telephony/signalwire.py` | `signing_key`, `signature_headers = ("x-signalwire-signature", "x-twilio-signature")`, secret hint `SIGNALWIRE_SIGNING_KEY`. SignalWire signs with Twilio's algorithm — its SDK's `RequestValidator` delegates to Twilio's for form bodies — but keyed by a *separate* signing key from the dashboard, so the API token is deliberately not accepted as the secret |
| `src/config.py` | `TelephonyConfig`: `TELEPHONY_WEBHOOKS` (default on), `TELEPHONY_WEBHOOK_RECEIVER` (`bot` / `standalone`), `TELEPHONY_WEBHOOK_PATH`, `SIGNALWIRE_SIGNING_KEY`; `webhook_url()` — None whenever events could not be received *or verified*; `can_verify_webhooks`; `describe_webhooks()`, folded into `describe()`. `WorkerConfig.webhook_poll_secs` (`WORKER_WEBHOOK_POLL_SECS`, 30) |
| `src/campaigns/models.py` | `WebhookOutcome` (`applied`, `duplicate`, `stale`, `unmatched`, `ignored`, `noted`), `WebhookDelivery` |
| `src/campaigns/store.py` | `telephony_webhook_events` (unique `event_key`; `attempt_id` SET NULL; no FK on `call_id`), `record_webhook_event` (`INSERT … ON CONFLICT DO NOTHING` → `(row, inserted)`), `set_webhook_outcome`, `last_webhook_at`, `webhook_answered_by`, `list_webhook_events`, `webhook_counts`; `_phase14` names `campaign.py init` on an older database |
| `src/campaigns/webhooks.py` | `WebhookProcessor.receive` (never raises; returns a `WebhookReceipt` with the HTTP status), `WebhookMetrics`, `create_webhook_router`, `install_webhook_receiver` (the bot), `create_webhook_app` (standalone), `build_webhook_processor` |
| `src/campaigns/dialer.py` | `status_callback_url` → `CallRequest`; `map_call_status` made public so a poll and a push read one table |
| `src/campaigns/worker.py` | `webhook_poll_secs`; `_Tracked.pushed` / `last_polled_at`; `_poll_due`; `call.pushed`, `pushed=` on `call.completed`, `worker.webhooks_unavailable` once on an old database |
| `bot.py` | `install_webhook_receiver(app, CONFIG)` before `main()`: one POST route on Pipecat's runner app, the database opened lazily on the first delivery |
| `webhooks.py` | The standalone receiver, port 7880, refuses to start unless `TELEPHONY_WEBHOOK_RECEIVER=standalone` |
| `campaign.py` | `call` and `run` pass the URL, `run` passes the worker setting, both print the webhook state; `webhooks [--call] [--attempt] [--limit]` lists the ledger |
| `call.py` | The URL on the request; `--no-webhooks`; `--dry-run` says where events go; `watch()`'s docstring says why it still polls (a one-off call has no attempt row) |

**How one delivery is handled:** verify (403 on failure; nothing is read) →
decode (400) → ledger insert under the unique key (a redelivery loses the
insert and is answered 200 `duplicate` *before the attempt is read*) →
`find_attempt_by_call_id` (none → 200 `unmatched`, recorded) →
`map_call_status` (unknown word → `ignored`) → Phase 12's machine rule, with
the verdict read back from the ledger because a completion event does not
carry it → `apply_call_event` (not applied → 200 `stale`) →
`service.record_outcome` (membership, prospect, the carrier result — exactly
as after a poll) → the ledger row's outcome. A database that is gone is 503,
so a carrier that retries does; one that predates the ledger applies events
without it and says so once.

**The ordering rule is unchanged, and it is what makes out-of-order delivery
safe.** `may_advance` already refused a regression and never overwrote a
final status: `completed` before `answered` gives `COMPLETED` and the late
`answered` is `stale`; a conversation outcome the bot wrote stays when the
carrier's `completed` arrives after it. What Phase 14 added on top is the
ledger's unique key — which catches a *redelivery of the same event* before
any row is read, so `record_outcome` runs once — and an outcome written per
delivery, so a duplicate says which event it duplicated.

**Reconciliation with polling.** Nothing was removed. The worker's tick still
reads every followed row every `WORKER_POLL_SECS` — that is how a pushed
ending is noticed and its slot freed as fast as a polled one — but the
*carrier request* is made only while no event has ever arrived for that call
(the fallback for a receiver that is down, unmounted or refusing) or when the
last poll is `WORKER_WEBHOOK_POLL_SECS` old (the safety net for an event the
carrier never sent). Recovery, `campaign.py recover` and `call.py` are
unchanged. In `test_webhooks.py` a call followed for 68 seconds costs three
carrier reads with events arriving and one per tick without.

**Where it runs, and why that is not the audio path.** A single tunnel gives
one public address, and `/ws` already has to be on it, so the default mounts
the route on the bot's runner app — one `POST`, added before `main()`. The
handler parses a form, computes one HMAC, and awaits a handful of short
database statements; it never touches a pipeline, a frame or a session, and
every wait yields the loop. `TELEPHONY_WEBHOOK_RECEIVER=standalone` moves the
same router and processor to `uv run webhooks.py` for a deployment that can
route one path to a second process.

**The tests.** `tests/test_webhooks.py`, the fourteenth script — 191 checks:
signatures (Twilio's published example reproduced; the port variants; wrong
secret, no header, tampered field, wrong URL, another account; SignalWire's
header and key, the API token refused, no key refused naming the setting),
decoding (every status word, sequence, timestamp, duration, the SIP code, the
AMD event, the key), placement (the fields sent; none without a URL; the
dialer carries it), configuration, the processor (a valid sequence;
forged / unsigned / malformed / unknown; a duplicate; out of order both live
and final; unmatched; every unhappy ending and the retry it schedules; the
machine verdict before and after completion; a conversation outcome
outranking the carrier; a database that is gone and one without the ledger),
the HTTP route through FastAPI's test client (including that a signature over
the server's *own* URL is refused), mounting on the bot and the standalone
app, the worker with and without events, on an old database, and racing the
receiver on one call; then the ledger and the processor against PostgreSQL in
a throwaway schema.

**What was deliberately not built:** a shared secret in the URL (the
carrier's signature is the authentication, and a URL is logged everywhere);
a worker that stops polling entirely (a receiver can be down); a dashboard
panel for the ledger (`campaign.py webhooks` is the view); webhooks for
Telnyx / Plivo / Exotel (no provider can *place* a call through them either;
`supports_webhooks` is False and a mounted receiver would answer 501); a
relabelling of a `COMPLETED` call when the machine verdict arrives after the
completion (logged as `webhook.late_verdict`; see Known issues).

### Phase 13 (previous session)

**The brief:** after importing prospects and activating a campaign, the system
places the calls itself — selecting the next eligible prospect; respecting the
campaign's status, calling hours, do-not-call, retry limits and prospect
status; reserving before dialling; starting the existing pipeline; persisting
every transition; moving to the next prospect when a call ends; placing
callbacks when they fall due; surviving a crash without losing or duplicating a
job; honouring concurrency and pacing; stopping gracefully; logging and
counting queued, started, completed, failed and skipped calls — without
touching the audio pipeline and without new infrastructure. Reliable for one
process first.

**One new module, and it decides only *when to ask*.** `src/campaigns/worker.py`
— `CampaignWorker` is a loop over five verbs the earlier phases already own:

| Verb | Owned by | Unchanged |
|---|---|---|
| who to call | `service.next_call` → `store.reserve_next_call` (one transaction, `SKIP LOCKED`, the idempotency key, the rules in SQL) | yes |
| whether to call now | `CampaignGuards` — window in the prospect's zone, concurrency, pacing, each refusal with `retry_after_secs` | yes |
| how to call | `CampaignDialer.dial_next` — re-check, stamp, ask the carrier once, hold ambiguity as `UNRESOLVED` | yes |
| what happened | `CampaignDialer.refresh` → `store.apply_call_event` (monotonic) → `service.record_outcome` | yes |
| what a crash left | `AttemptRecovery` — never dials | yes |

Nothing in the worker is a rule about *whether* a call may be placed, and
`tests/test_worker.py` has a check that its imports name nothing outside
`src/campaigns/` and `src/reliability/`.

**Per tick:** follow every call it is watching (`get_attempt`, then `refresh`
when there is a call id; the bot's own final write — a do-not-call, a callback
— is seen and stands); run the periodic recovery pass when due; place due
`PENDING` callbacks *before* the queue, through a targeted reservation; then
draw from every `ACTIVE` campaign in turn while there is capacity; for a
campaign that hands out nothing, ask `queue_outlook` whether it is finished,
waiting on a retry, waiting on a callback, or blocked by a live call, and
sleep accordingly — the guard's `retry_after_secs`, the next due moment, the
poll interval while anything is live, never longer than `WORKER_IDLE_SECS`.
`campaign.py run` is the front end: `[campaign...]`, `--max-calls N`, `--once`,
`--no-auto-complete`; Ctrl+C once drains, twice stops now
(`install_signal_handlers` handles Windows, where the loop has no signal
handlers). `WorkerConfig` (`Config.worker`) is six `WORKER_*` settings in
`.env.example`, none of which decides whether a call may be placed.

**What was added underneath, each because the loop had a question nothing
could answer:**

* `store.reserve_membership` / `service.reserve_membership` /
  `dialer.dial_membership` — the queue's own reservation statement with the
  row pinned, for a callback: the queue orders never-called rows first, and a
  promise for ten o'clock must not wait behind them. Same lock, same key, same
  concurrency count; `ignore_attempt_limit` waives only the cap.
* `store.queue_outlook` → `QueueOutlook` — one query: due now, next due,
  in progress, undialable, pending callbacks. `is_finished` requires at least
  one membership: a campaign started before its list was imported is empty,
  not done.
* `store.sweep_memberships` — closes what the queue would never hand out (no
  number or do-not-call → `SKIPPED`; out of attempts with no pending callback
  → `EXHAUSTED`) so a list with one unusable number can still finish.
* `store.unreserve_attempt` / `service.defer` / `service.unreserve` — hand a
  reservation back *unspent*: the never-placed attempt row is deleted, the
  count restored, the membership scheduled. Used by the dialer when the
  prospect's own window is closed (`DialResult.deferred`), and by recovery for
  a reservation a dead process never dialled. Refused once
  `placement_started_at` is set.
* `list_campaigns(status=)`, `CallingWindow.clock`, `CampaignService.clock`
  and `now()` — so a whole run can be judged against a fixed Monday.

**Four bugs found in code that had passed every check since Phase 5** — the
last permitted attempt never dialled; `record_outcome` on a live status;
the stale prospect copy; the duplicate placement left live. Each is in
[Failed §33](#33-what-the-scheduler-found-in-the-dialling-path-phase-13) with
how it surfaced.

**The tests.** `tests/test_worker.py`, the thirteenth script: an in-memory
store with the queue's rules written out (eligibility, the live-attempt
exclusion, the idempotency key, the concurrency count, the monotonic write), a
carrier whose calls walk a scripted status list, a clock the checks move by
hand, and the real service, dialer, recovery and worker in between. 213 checks
across end to end, duplicate reservation (two workers over one store), calling
hours (the server's zone, a Sunday, the prospect's own zone), DNC (before,
between reserving and dialling, written by the bot mid-call, a due callback for
a DNC prospect), retry limits (the wait, the last attempt, a limit of one,
FAILED not retried, busy retried), callbacks (at the limit, ahead of the queue,
after a bot crash, override off, a paused campaign, one honest try, no
membership), restart (died mid-call, died between reserving and placing, too
young to touch, ambiguous with and without a call), completion (done, a retry
pending, empty, an unusable number, a promised callback, auto-complete off,
paused and draft campaigns, pausing mid-run), concurrency and pacing, graceful
shutdown (drain, twice, the drain ceiling, `--once`, `--max-calls`), an
ambiguous placement holding the slot, a database and a carrier that go away,
a bug in a tick, the metrics, and the import boundary. The last section runs
the new SQL and the worker itself against PostgreSQL in a throwaway schema.

**What was deliberately not built:** carrier status webhooks (polling at
`WORKER_POLL_SECS` is one request per live call per poll; fine at one call,
noted for ten); a second worker (the reservation is already safe across
processes; pacing and the in-flight set are not shared); a table for worker
state (the rows are the state); a health endpoint for the worker; an
hours-aware queue (the configured window is checked before reserving in the
configured zone, the prospect's own after, so a prospect is dialled only when
both are open — see Known issues).

### Phase 12 (previous session)

**The brief:** make the voice conversation reliable and human-like under real
calling conditions — a controlled real-telephony workflow, barge-in that stops
the bot at once, rapid speech, pauses, overlap, noise and short answers,
voicemail detection with a disposition, per-turn latency for every stage,
structured logs for failed turns, interruptions, voicemail and telephony
failures. No provider changes; everything configurable in `.env`.

**Two new modules, and neither touches the audio path.**

* `src/voice_quality.py` — `TurnMonitor`, an observer that records every
  caller turn (transcript, released → LLM started → first token → TTS started →
  first audio), every barge-in (with the interruption-to-bot-stopped latency
  and the words heard before the cut), every failed turn (`turn.failed`: no
  response, no audio after the LLM started, or a service error, after
  `TURN_RESPONSE_TIMEOUT_SECS`; a reply that comes after all is
  `turn.late_response`), and every **spurious interruption** — a turn that cut
  the bot off and closed with no words. `report()` assembles it all with
  `LatencyReporter.summary()` and the voicemail verdict into one JSON record;
  `bot.py` stores it with the conversation (`conversation_data.quality`) and
  writes it to `CALL_REPORT_DIR/<call id>.json` for phone calls.
* `src/voicemail.py` — `VoicemailDetector` (phrases only a recording says; a
  first turn that talks *over the agent's greeting* for longer than
  `VOICEMAIL_MAX_GREETING_SECS`; the carrier's `answered_by`) and
  `VoicemailHandler` (hang up, or wait for the greeting to end, speak
  `VOICEMAIL_MESSAGE` through a `TTSSpeakFrame` with no model involved, and
  hang up). Built only for phone calls.

**What changed around them.** `CallAttemptStatus.VOICEMAIL` and
`Disposition.VOICEMAIL` (final, not reached, retried like a no-answer; the
result keeps the recording's transcript as evidence and forces every
qualification field to unknown, overruling a model that "qualified" a
greeting). `FINAL_STATUS_SQL` in `models.py` replaces the two hand-written
status lists in `store.py`. `CallRequest.machine_detection` and
`CallSnapshot.answered_by` (`TELEPHONY_MACHINE_DETECTION=off|async|sync`;
`TwilioProvider` sends `MachineDetection=Enable` and `AsyncAmd=true`; the
dialer's `refresh` and recovery turn a completed-by-machine call into
`VOICEMAIL`; the bot polls `answered_by` for `TELEPHONY_AMD_WINDOW_SECS` after
connect; `call.py --hang-up-machines`). `LatencyReporter` now keeps per-response
records and `summary()`, and its per-response line is the structured
`LATENCY | turn.latency | response=N total_ms=… turn_end_ms=… stt_ms=…
llm_first_token_ms=… tts_first_audio_ms=…`. `NOISE_RESUME_INSTRUCTION` in
`prompts.py` asks the agent to pick up after a spurious interruption instead
of sitting silent until the idle nudge. `SilenceHandler.closed_call`,
`SalesConversation.note_voicemail` and `finish(quality=…)`, the dashboard's
`VOICEMAIL` row. `.env.example` gained a Phase 12 section.

**Two fixes to session start that came out of the first drill**, both in
`bot.py`: `_drain_stale_audio` reads and drops the carrier audio that queued
during setup, right before the pipeline starts (logged as
`telephony.backlog_dropped | latency_ms=…`); `_preflight` now imports the LLM
service's module and loads the embedding model once per process
(`services.warm_up_llm_module`, `embeddings.shared_embedder`). Measured: the
first import cost 4.3 s and the model 1.1 s, after the person had picked up.

**Three tools.** `tests/phone_drill.py` — seven scripted callers spoken by
Kokoro over the carrier's wire protocol (short answers, barge-in, overlap, a
mid-sentence pause, 1.35× speech, steady noise, a voicemail greeting with a
beep), each checked against the bot's report; it also measures turn-start
detection lag, which is the number that exposed the backlog.
`tests/live_call.py` — one real call to your own phone, a script to follow,
and a PASS/FAIL checklist across the carrier's side and the bot's report.
`tests/test_voice_quality.py` — the twelfth deterministic script.

**Three eval scenarios**, audio mode, added to `evals/suite.yaml`:
`short_answers`, `rapid_speech`, `overlap`. Results in
[Testing completed](#10-testing-completed).

### Phase 11 (previous session)

**It measured first, and the measurements decided what changed.**
`scripts/benchmark_db.py` seeds a throwaway schema with 20,000 prospects and
60,000 attempts and times every query the system issues. At that size:

| Path | Measured | What was done |
|---|---|---|
| The dialer's queries | 0.7–10.7 ms | Nothing. They are fine |
| Retrieval per turn (embed 28 ms + pgvector 1.5 ms) | 29 ms of a ~1,300 ms turn | Nothing. It is 2% of a turn |
| One dashboard load | 249 ms, 7 sequential queries | Made concurrent, then cached |
| Connections per call | 2 idle, up to 6 | One shared pool: 1 idle, up to 4 |
| Prompt per LLM request | **3,394 tokens**, 1,248 of them tool schemas | Left alone, with the reason recorded |

**Three optimisations, each with a before and after.**

* *Concurrent dashboard reads.* `collect()` issued eight independent aggregate
  queries one after another, so a page load cost their sum. `asyncio.gather`:
  **249 ms → 149 ms, 43% faster**, same queries and same numbers.
* *A five-second snapshot cache with a lock.* Every open tab refreshed every 15
  seconds and each refresh was a full pass of aggregates over the database the
  dialer is using. **21 simultaneous viewers now cause 1 database read instead
  of 21**, and the lock stops a slow read being started twice — the stampede
  that turns one slow query into several at the worst moment.
* *One connection pool per call instead of two.* `KnowledgeStore` and
  `CampaignStore` both opened their own, and `DATABASE_URL` defaults to
  `KB_DATABASE_URL`, so the second was usually against the same database.
  `Config.shares_database` decides, and the campaign store borrows the other's
  pool through a new `connect(pool=...)`, closing only what it opened.
  **Measured: 2 idle connections per call → 1, peak 6 → 4**, no leak. Against
  PostgreSQL's default hundred that roughly doubles the concurrent-call
  ceiling.

**The concurrency limit now holds under a real race.** Phase 9 counted live
calls *before* reserving, which two workers could pass at the same instant.
`reserve_next_call` takes `max_concurrent` and counts inside the transaction
that takes the row lock. Six simultaneous reservations against a limit of two
hand out exactly two — checked, and bounded on both sides so it cannot pass by
handing out none.

**Per-call usage and cost, from data that was already there.**
`enable_usage_metrics` has been on since Phase 2 and nothing read it.
`reliability/usage.py` sums what Pipecat reports — tokens, characters, audio
seconds — onto `call_attempts.usage` and `cost_usd`, and the dashboard grew a
usage strip. Two rules carried over from earlier phases:

* *Units are measured, prices are configured, nothing is invented.* No rate
  configured means no cost, never a guessed one.
* *A stage is priced only if the provider reported it.* Pipecat 1.8.1's
  **websocket** Deepgram TTS never records characters (only its HTTP variant
  does), so a call on it lists `tts` under `unmeasured` rather than pricing it
  at zero. Found by reading a live call's usage and noticing a zero that should
  have been a number.

**What was deliberately not changed.** The providers, the models, the pipeline
order, turn detection, retrieval, and the tool list. The dominant latency is
turn-end at 653 ms of ~1,300 ms, which is Deepgram's tuned default and where
cutting people off lives; and the dominant cost is the prompt, whose obvious
fix is blocked by a real framework race (below).

### Phase 10 (previous session)

**A dashboard that adds no data.** `src/dashboard/` plus `dashboard.py`, over
the PostgreSQL the dialer already writes. No analytics database, no warehouse,
no scheduled rollup, no cached table — every figure is a SQL aggregate run when
the page loads, which is why it cannot go stale or disagree with `campaign.py`.
The eight tiles the phase asked for, a call-outcome breakdown, a row per
campaign, and the fifteen most recent calls.

**Counting happens in SQL; wording happens in `stats.py`.** Twelve read-only
aggregate methods went into `campaigns/store.py` — next to the queries they
resemble, because they need the pool and the table names — and `stats.py`
turns them into labelled metrics. It counts nothing itself. A dashboard that
loaded a hundred thousand attempt rows to display "total calls" is one nobody
leaves open.

**Two numbers could mislead, so both carry a footnote in the data, not the
CSS.** *Answered* and *completed* overlap and are not the same — `COMPLETED` is
the carrier's word for a call that ran to its end, *answered* is every status
meaning somebody picked up, including one that ended in a do-not-call. And an
average duration is meaningless without the count it averages, so the tile
always says "over the 40 calls that have a duration". `Metric.detail` is part
of the JSON, so any other reader gets the caveat too.

**A missing table is `available: false`, never zero.** `call_results` and
`meetings` arrive in Phases 8 and 7. On an older database those tiles read
"unavailable", the page says which command fixes it, and the outcome breakdown
falls back to attempt statuses and labels itself as such. A zero next to
"Qualified prospects" is a claim; an absence is not.

**One renderer.** The page is a static shell that renders from
`/api/dashboard` — the endpoint that has to exist anyway — so there is no
second description of the same numbers to keep in step. No build step, no
framework, no CDN: `uv run dashboard.py` is the whole setup, and a dashboard
that needs a CDN round trip before it can draw fails in exactly the situation
you opened it for.

**Its own process, and every route a read.** Served by `dashboard.py`, not by
the bot's runner: the runner answers calls, and a page refreshing every fifteen
seconds has no business in that process. They share the database and nothing
else — the same seam every other CLI uses. There is no POST, no PUT and no
DELETE, and `test_dashboard.py` asserts that the application exposes no method
but GET and HEAD, because "it only reads" is the property that makes it
defensible to point at a system that is dialling.

**Loopback by default.** No login, and it shows names and phone numbers, so
`--host 0.0.0.0` is allowed and prints a warning rather than being silently
convenient.

**Nothing in the voice path changed.** No new dependency either: FastAPI and
uvicorn arrive with Pipecat, and the only edits outside `src/dashboard/` were
the store's new read methods, one SQL constant in `models.py`, and docs.

### Phase 9 (previous session)

**The requirement that shaped everything: never two calls to one person.** Five
independent mechanisms, listed in `src/reliability/__init__.py` and each driven
on its own by a check in `tests/test_reliability.py`:

1. `reserve_next_call` picks and reserves in one transaction with
   `FOR UPDATE SKIP LOCKED` (Phase 5, unchanged).
2. **`call_attempts.idempotency_key`**, unique, derived — `v1:campaign:3:
   membership:12:attempt:2`. Derived rather than random because a random key
   protects one client retrying; a derived one makes two *different* callers
   who mean the same call compute the same string. A collision inside the
   reservation transaction rolls the whole thing back (so the attempt count is
   not spent either) and reports an empty queue.
3. **`place_call` is never retried.** Its policy is `NEVER_RETRY` — one
   attempt, with a timeout. Neither Twilio nor SignalWire offers an idempotency
   key for call creation, so there is no safe repeat.
4. **`CallAttemptStatus.UNRESOLVED`**, new, for network ambiguity — a placement
   that timed out or lost its connection. It is deliberately **live**, so the
   prospect stays blocked. Not knowing costs one uncalled prospect; the other
   direction costs a stranger's phone ringing twice.
5. **`campaigns/recovery.py`** resolves it by asking the carrier which calls
   exist (`find_recent_calls`), never by dialling. What it cannot resolve it
   closes as failed with the reason on the row, so the prospect is freed and the
   campaign's own retry policy — not recovery — decides about trying again.

**A failure is classified before anything is retried.** `reliability/retry.py`
has three verdicts, and the third is the point: `RETRY` (it certainly did not
happen), `FATAL` (it was refused on its merits), `AMBIGUOUS` (unknown).
`AMBIGUOUS` raises `AmbiguousOutcomeError` immediately, whatever attempts
remain, so a caller cannot mistake "it failed" for "it did not happen". Backoff
is exponential with a cap and equal jitter; every policy carries a per-attempt
timeout, because an operation that can hang is not retryable in any useful
sense. An exception can opt into being retryable by setting `retryable = True`
on its class — which is how `ProviderUnavailableError` says a carrier blip is
worth another go without `retry.py` knowing what a carrier is.

**Status updates are monotonic, so a duplicate event is a no-op.**
`models.may_advance` plus `store.apply_call_event` (which selects `FOR UPDATE`)
mean a webhook delivered twice, a poll racing it, or an out-of-order delivery
all land on the same row without any of them undoing another. A final status is
never overwritten — which *generalises* the three special-cased lines in
`dialer.refresh` that stopped the carrier's "completed" flattening a
do-not-call. Those lines are gone; the rule is now a property of the write.

**Campaign safety, with the safe end of every trade as the default.**
`reliability/guardrails.py`: calling hours (09:00–18:00 mon-fri, enforced),
concurrency (one live call), pacing (off), and a call-duration ceiling (ten
minutes). Hours are applied in the *prospect's* timezone when their imported
record supplies one, and never inferred from their phone number — a country
code does not determine a timezone. Guardrails are checked **before** a
reservation is taken, so a closed window does not spend one of a prospect's
attempts.

**The bot supervises itself.** `reliability/supervisor.py` counts *consecutive*
failures per pipeline stage, resets on a success from that stage, and ends the
call deliberately — with a goodbye where the agent can still speak — rather
than leaving the caller in silence. It also catches an inference that starts
and never finishes, which no error path can see because nothing raises, and
enforces the duration ceiling. `services.py` additionally sets the LLM client's
own HTTP timeout to 80% of the stall threshold, through the OpenAI SDK's public
`with_options`, because Pipecat's `create_client` swallows kwargs and the SDK's
default wait is ten minutes.

**Health checks that cost nothing and ring nobody.** `uv run health.py` probes
the database, knowledge base, STT, LLM, TTS and carrier with the cheapest
authenticated read each one offers. The LLM check also verifies the configured
model is still in the provider's catalogue — the Groq-catalogue-churn failure
this project has already met, which otherwise surfaces as a bot that answers
the phone and says nothing.

**Structured logs, credentials scrubbed.** `reliability/observability.py`:
`call_context` binds campaign, prospect, attempt, call and provider onto every
record inside a block, `LOG_FORMAT=json` emits them as fields, and a loguru
patcher replaces every configured credential's *value* with `***` in every
record including exception text. The scrubber is a backstop against a vendor
SDK putting an Authorization header in an exception message — something no
care at the call site prevents.

**No distributed infrastructure.** No broker, no lock service, no daemon.
Correctness lives in PostgreSQL: a transaction, two unique indexes and a status
that blocks. Pacing and the concurrency limiter are in-process and documented
as such — they are about *rate*, and a second dialer could not cause a
duplicate call because none of the five mechanisms depends on being the only
process running.

### Phase 8 (previous session)

**One row per finished attempt, from either of two writers, and the row is a
projection.** `call_results` (`src/campaigns/results.py`, `store.py`) holds
one `CallResult` per call attempt, `UNIQUE (call_attempt_id)`. The bot writes
the rich one at the end of a call it held, through the same
`ConversationSink.on_call_finished` Phase 6 built, from the same outcome dict
it already stored as `conversation_data`; the dialer writes a thin one from the
carrier's report inside `CampaignService.record_outcome`, so a call nobody
answered has a row too. `conversation_data` stays the raw record, transcript
included, and the result is derived from it — which is why
`campaign.py rebuild-results` exists and why the result's shape can change
without losing anything.

**Two writers, one rule, applied in the SQL.** `save_call_result` is an upsert
whose `WHERE` says a conversation result replaces anything and a carrier result
replaces only another carrier result. Whichever order the bot's teardown and
the dialer's reconciliation land in, the row ends up as the conversation's.
Tested both ways in `test_campaigns.py`.

**The disposition is derived by precedence and every branch needs a recorded
fact.** `derive_disposition`: the three unreached statuses; then `DO_NOT_CALL`;
`MEETING_BOOKED`; `TRANSFERRED`; `CALLBACK_REQUESTED`; `NOT_INTERESTED`;
`QUALIFIED`; `UNQUALIFIED`; `COMPLETED`. The vocabulary is
`CallAttemptStatus`'s final values plus the four the attempt status cannot say
(`MEETING_BOOKED`, `TRANSFERRED`, `QUALIFIED`, `UNQUALIFIED`) — `TRANSFERRED`
is the one addition to the list the phase gave, because a call handed to a
person is the most important thing about it. Unknown interest is not a no; an
agreed meeting is not a booking; a callback outranks a no because "not now,
ring me in March" is both.

**Qualification is re-derived from the evidence, and the validator refuses a
result that claims otherwise.** The builder rebuilds a `QualificationRecord`
from interest, pain points, decision role, timeline and next action and reads
`.qualification_status` — the same rule the live call uses — and
`validate_call_result` recomputes both the disposition and the qualification
and refuses a mismatch. The store calls the validator before every write.
Malformed input never raises in the builder: an unreadable field is left
`UNKNOWN`/`None` and named in `issues`, which is stored on the row.

**The transcript is evidence; the summary is a reading of the record.**
`src/conversation/transcript.py` records the prospect's turns from
`note_user_turn` (the director's path — the words the detectors ran over) and
the agent's from `note_agent_turn(text, interrupted=...)` (the assistant
aggregator, after `transport.output()` — what the caller heard, cut where they
cut it). It travels in the outcome dict and lands in `call_results.transcript`
and in `conversation_data`, untouched. The summary — what happened, needs,
objections, interest, qualification, next step — is composed
*deterministically* from the structured fields and never reads the transcript,
which is the only way to guarantee it invents nothing; where a field is unknown
it says so by name. Phase 6's "if a second LLM pass comes back, it belongs at
the end of the call" was considered and not taken: a summary a CRM will trust
must be one you can prove says nothing the record does not.

**`questions` is a filter over the prospect's transcript turns, not an
interpretation.** A sentence is kept when it ends in a question mark (on more
than one word) or opens with an interrogative, verbatim. It misses a question
phrased as a statement and keeps the odd false positive, and both are the
right way to fail because the transcript is beside it.

**The attempt status now comes from the state path.** Phase 6 mapped the
*final* state to `CALLBACK_REQUESTED` / `NOT_INTERESTED` / `DO_NOT_CALL`, and
the final state after the agent's own `end_call` is always `ENDING`. So on a
real database those three were only ever written when the line dropped before
the goodbye. `results.attempt_status_for` reads the last non-`ENDING` state on
the path (a do-not-call anywhere wins) and the sink uses it. Found by
`test_results.py`; see [Failed §23](#23-reading-the-attempt-status-from-the-final-state-phase-6-found-in-phase-8).

**Read back from the CLI, and rebuildable.** `campaign.py results`
(`--campaign`, `--prospect`, `--disposition`, `--json`), `campaign.py result
<attempt> [--transcript] [--json]`, and `campaign.py rebuild-results [--all]`
for attempts that finished before the table existed. `tests/fake_carrier.py`
gained `--prospect/--campaign/--attempt` so a simulated call can be a campaign
call and land its result on a real attempt row — which is how the phase was
verified end to end.

**No CRM integration.** The row is flat and typed, the export is
`CallResult.to_dict()`, and the mapping onto HubSpot, Pipedrive and Salesforce
is a table in the module docstring of `results.py`. Nothing here talks to any
of them, on purpose.

### Phase 7 (previous session)

**Seven action tools, one result shape, and the model never touches anything.**
The chain the phase asked for is the chain that exists::

    LLM -> tool (conversation/tools.py) -> SalesConversation (rules) -> ActionBackend (validation, I/O) -> ToolResult -> LLM

Every one of the twelve tools — the five Phase 6 recorders plus
`search_knowledge_base`, `check_calendar_availability`, `book_meeting`,
`schedule_callback`, `mark_do_not_call`, `transfer_to_human`, `end_call` —
answers with the same `ToolResult` (`conversation/results.py`): `success` is the
only thing the model may read as "it happened"; a failure always has an
`error_code` from a closed vocabulary and a message; every result has
`guidance`, and every failure's guidance says "nothing happened, do not say it
did". The record is held to the same rule: `meeting_booked`,
`callback_scheduled_for` and `transferred` are written only on a backend `ok`.

**The tool boundary is one function.** `conversation/toolkit.py::strict_tool`
takes a Phase 6-style direct function and returns a `FunctionSchema` with the
*same* schema Pipecat derives from the signature and docstring, plus a handler
that validates the model's arguments against it (required, types, unknown keys
dropped and logged), guards the call (an exception becomes `internal_error`; a
tool that reports nothing becomes a failure rather than a hang), and writes one
audit line — tool, session/call/prospect/attempt ids, arguments, verdict,
elapsed, result summary — and one entry in the call's outcome. Pipecat
auto-registers a `FunctionSchema`'s handler exactly as it does a direct
function; verified in the installed source before building on it.

**A third Protocol out of the conversation package.** `ActionBackend`
(`conversation/actions.py`) joins `ProspectSource` and `ConversationSink`: plain
data in, `ActionOutcome` out, never raises. `src/actions/service.py` implements
it and is the one module that knows the campaign store, the calendar, the
retriever and the carrier at once — the Phase 7 analogue of `dialer.py` and
`briefing.py`. `Capabilities` travels with it, and **the prompt is built from
the capabilities**: a browser session is not told about transfers, a bot with no
calendar is told to record intent and say a colleague will confirm.

**Two layers of validation, each about what it can see.** The conversation
refuses what the *state* forbids — no calendar or booking after a "no", nothing
at all after a do-not-call, refused moves reported as `not_authorized` — and
refuses what does not parse (`timeparse.py` is strict: ISO 8601 or a failure
telling the model to work the date out). The service refuses what the *world*
forbids: past times, beyond the horizon, no calendar, no prospect row, not a
phone call, no transfer number, carrier or calendar refused. Both are tested.

**The calendar is an abstraction with two providers.** `src/scheduling/` has a
`CalendarProvider` base with two operations, a `LocalCalendarProvider` (business
hours on a slot grid minus what the `meetings` table holds — the default, real,
and what makes the whole flow testable end to end without an account) and a
`CalComProvider` against Cal.com API v2 (slots and bookings), written against the
documented API and exercised against a stub HTTP session only; **no Cal.com
account exists on this machine**. Calendly was considered and rejected: its API
cannot create a booking, only issue links, which a voice agent cannot hand to
somebody on the phone.

**Booking is a sequence the tools enforce.** `check_calendar_availability`
remembers the slots it returned; `book_meeting` refuses anything else as
`slot_not_offered`. The agent is told the current date, time and timezone in the
system instruction, so "next Tuesday at ten" is an exact moment before any tool
sees it. A provider that needs an email (Cal.com) is surfaced as a capability so
the agent asks *before* trying.

**A callback is a row and a queue entry.** `callbacks` table, one `PENDING` per
prospect (asking again moves it), cancelled by a do-not-call, marked `PLACED` by
the dialer. At call end the sink reopens the campaign membership with
`next_attempt_at` = the callback time, so Phase 5's queue — which already
refuses to hand out anything before its retry time — dials it then. On a
session with no prospect row the tool says so and the agent says a colleague
will arrange it.

**Transfer is a blind redirect through the carrier's live-call update.**
`TelephonyProvider.transfer_call` posts new TwiML (`<Dial>` the destination,
`<Say>` a fallback, `<Hangup/>`) to the call resource; SignalWire inherits it.
The carrier ends the media stream when it applies the TwiML, which is the bot's
signal — so `transfer_to_human` deliberately pushes **no** end frame, because an
`EndFrame` would make the serializer hang up the very call just handed over.
Eligible only on a phone call with credentials and `TELEPHONY_TRANSFER_NUMBER`.
Never exercised against a live call.

**Groq's free tier, measured.** `x-ratelimit-limit-tokens: 8000` per minute for
this organisation and model; a request with the twelve tools advertised is 3,094
prompt tokens after trimming (3,843 before). Two consequences that took most of
the session's debugging: with no output cap, Groq *estimates* the reply at 1,815
tokens and refuses every request against its 1,000 output-tokens-per-minute
limit (`LLM_MAX_OUTPUT_TOKENS`, default 400, fixes that and is now always sent);
and a tool turn is two requests, so the second is held back until the minute
resets. See [Known issues](#5-known-issues-and-limitations) and
[Failed §19–§20](#19-advertising-twelve-tools-with-no-output-cap-on-groq).

### Phase 5 (previous session)

**Four entities, and the boundary between them is the design.** `Prospect` is a
person and carries no campaign state at all; `CampaignProspect` is one person's
membership of one campaign and is where attempts and retry times live; so the
same prospect sits in two campaigns with independent progress and one row.
`CallAttempt` is one dial — a prospect is not a call attempt, they have many —
and it carries the carrier's call id, which is the join between this database
and the telephony provider's records.

**Phone normalisation refuses to guess.** `phonenumbers` (Google's
libphonenumber), not a regex, because deciding whether `0322 1234567` is a valid
Pakistani mobile is a data problem and a hand-rolled version answers it
confidently and sometimes wrongly. With `DEFAULT_PHONE_REGION=PK` the four
spellings in the requirement collapse to one E.164 number; with no region set, a
local-format number is **refused** rather than assigned a country. A rejected row
costs one uncalled prospect; a wrongly normalised one calls a stranger.

**CSV import assumes nothing about the file.** Headers match an alias table after
being reduced to letters and digits, so `First Name` / `first_name` /
`FirstName` / `FIRST-NAME` are one column. Unrecognised columns are kept in
`custom_data` rather than dropped. Parsing is pure — no database, no writes —
which is what makes `--dry-run` one branch rather than a second code path, and
what lets the import logic be tested exhaustively in a second.

**Do-not-call is enforced three times.** Marking closes the person's open
memberships; the queue's SQL excludes them; and the check runs again against a
freshly read row immediately before the call is placed. Any one would usually be
enough. "Usually" is not the standard for phoning somebody who asked not to be
phoned.

**The queue reserves in one transaction.** `reserve_next_call` applies every
eligibility rule inside the statement that takes the row lock, with
`FOR UPDATE SKIP LOCKED`, then marks the membership and writes the attempt row.
A check made *before* a lock can be true when made and false when used, and two
callers can otherwise be handed the same person.

**`campaign.py` is the service layer's front end**, in the shape `ingest.py`
established: logic in `src/campaigns/`, argparse over it. No web framework was
added — this project has no API of its own, and a future web or n8n front end
will call `CampaignService`, not this file.

### Phase 4 (previous session)

**Outbound calling, as a transport rather than a rewrite.** `bot.py` gained a `twilio` (and telnyx / plivo / exotel) entry in `transport_params` and nothing else structural — the pipeline, services, turns, metrics, retrieval and resilience modules are untouched by the phone.

**A carrier abstraction that is honest about its two halves.** Receiving a call's audio needs *no* code from us: Pipecat detects the carrier from the media stream's first message and picks the serializer, which is why four carriers work on the audio side. Placing a call is a per-vendor REST API, and that is what `src/telephony/` abstracts — `TelephonyProvider` with three methods, `TwilioProvider` implementing it, and `make_provider` as the single point that knows which is configured. `bot.py` never imports a carrier.

**`call.py`** places a call, watches it, and exits 0 / 1 / 2 for *answered* / *could not be placed* / *nobody reached*. Busy, no-answer, failed and cancelled are kept apart rather than flattened, because they mean different things to whatever schedules the next call.

**Call state and call-level metrics.** `src/telephony/session.py` turns the media stream's handshake into a `CallSession` — the call id, both numbers, and whether we placed it — logs connect and disconnect, counts turns per side, and prints a summary next to the latency one. A call where the other end never spoke is called out explicitly; voicemail and a wrong number both look like that and neither shows up in the latency numbers.

**Configuration that does not hold the browser hostage.** Telephony settings are validated at the point of use, not at startup, so a bot with no Twilio account still boots, still serves the browser, and still runs the eval suite. `require_outbound()` turns a missing setting into a message naming it.

**Verification without a phone.** `tests/test_telephony.py` covers placement, TwiML, outcome mapping, error translation, handshake parsing and configuration against a stub HTTP session. The audio path was verified separately by simulating a carrier — see [Testing completed](#10-testing-completed).

---

## 4. Pending tasks

Nothing is half-finished. Phase 16 is complete as specified. What follows is *not started*, and most of it is deliberately deferred:

- **Phase 27 scope** — not specified. Do not guess and start building.
- **The application has now been walked in a real browser** (Phase 26, Playwright over the installed Chrome); the remaining UI gaps are listed at the end of the Phase 26 section (no in-call state on the live page without a new route; no analytics agent filter; the calls page's qualification / meeting filters are client-side).
- **The engine dials whenever `app.py` is up and a campaign is `ACTIVE`.** That is the phase's point, and it is also the first thing to remember on a machine with real carrier credentials: `uv run app.py --no-engine` (or `WORKER_EMBEDDED=false`) serves the page without dialling.
- **Seen on the Phase 25 live call, not fixed (the pipeline is out of scope):** the Groq model spoke its own reasoning aloud once ("I need to figure out which day next week refers to. Today is Tuesday…") and then "I've stopped." — a reasoning-mode leak from `qwen/qwen3.8-27b` into the spoken reply; the transcript records it faithfully. Worth a look with the TTS credential and the Groq throttling before real use.
- **Graceful shutdown on Windows:** uvicorn stops on Ctrl+C and runs the lifespan exit, which is where the engine drains and hands over; `taskkill /F` skips it, and the next start adopts the abandoned calls after `WORKER_STALE_SECS`. Verified by the checks (`engine.stop` bounded, hand-over, adoption), not by a signal on this machine.
- **The unified application has been walked end to end in jsdom, not in a real browser.** jsdom rendered the real page against the real backend and every page, form, dialog and role passed (see the Phase 24 audit); the three things jsdom lacks (the form named getter, `Blob.text()`, layout) are things every browser has. A human should still open `http://127.0.0.1:7900/app/` once for the *look* — spacing, the sidebar on a phone, the toasts — which no DOM walk can judge.
- **Two voice-pipeline behaviours seen on the audit's real call, not fixed here:** the free Groq tier stalls tool turns for 40–60 s (known; the prospect hears silence), and once Cartesia refused an empty sentence the model produced after a tool call, three times, so the supervisor ended the call mid meeting-request. The second is worth a look before real use: either the LLM adapter should not send an empty text frame to TTS, or the supervisor should not count three refusals of the *same* empty frame as a dead TTS. Reproduce with `tests/phone_drill.py` or the audit's bridge (the approach is described in the Phase 24 audit); the result row's quality summary records it.
- **The Live AI Agent page frames the bot's `/client`**, so it is blank until `uv run bot.py` is up — and the bot cannot speak until the TTS line is fixed (next bullet).
- **Per-campaign calling number / provider, per-campaign concurrency, a `FAILED` campaign state and a scheduled start time** are shown as not supported rather than invented; each is a backend change if wanted (see Phase 24's *deliberately not done*).
- **The bot cannot speak as this machine is configured.** `.env` sets `TTS_PROVIDER` three times and the last line (`elevenlabs`) wins; the ElevenLabs key is rejected (401) while the Cartesia key on the same file is accepted. One `TTS_PROVIDER` line with a working key, then `uv run health.py tts`. Nothing in code can fix a credential; `security.py check` and `validate.py` both say so.
- **`PRODUCTION_READINESS.md` §15 is the list.** Fifteen items, each with its command, none done on this system; §16 orders them. The first is one real answered call. `validation-report.md` is regenerated by `uv run validate.py` and is the evidence; trust its date over the document's.
- **The eval suites and the phone drills were not re-run in Phase 23** (the TTS credential). Run `uv run validate.py --evals` and `tests/phone_drill.py all` once TTS is fixed, before the real call.
- **`validation-report.md` / `.json` in `server/` are generated** and untracked; keep or ignore them as you prefer (`server/.gitignore` does not list them).
- **`callback.scheduled` is only made while the callback is pending**, so a deliverer that is not running when the worker places the callback never sends it; in production the deliverer runs continuously. Noted in `PRODUCTION_READINESS.md` §13.
- **No real Prometheus has scraped `/metrics`, and no orchestrator has probed `/readyz`.** The text format is checked against its own rules (HELP/TYPE, escaping, cumulative buckets, `+Inf`) and by `curl` against a booted bot; a Prometheus server has not parsed it. The first deployment should point one at every port (7860, 7870, 7880, 7890, 7895) and confirm `aiva_up` is 1 for each `role`.
- **No real call has carried a trace end to end.** The dialer writes it, the handshake carries it and the receiver, syncer and deliverer read it back — every hop verified over the checks' fixtures and the throwaway schema, never over a carrier. The first real call: `grep trace=<id>` across the bot's and the worker's logs (with `LOG_FORMAT=json` and `component` it is one `jq` filter) and confirm the same id on the `call.placed`, `CALL |`, `store.op`, `webhook.applied` and `crm.synced` lines.
- **`campaign.py init` is needed on any existing database** for `call_attempts.trace_id`. Idempotent; **done on this machine's database on 2026-09-08**. Without it the dialer warns once (`TRACE | this database has no trace_id column`) and the id still travels on the handshake and the logs, just not on the row — so the receiver, the syncer and the deliverer log without it.
- **Counters are per process and reset on restart.** That is what Prometheus expects (`rate()` handles it) and what `/metrics.json` shows plainly; the durable figures are the rows, which `campaign.py metrics` and the fleet gauges read. There is no metrics table on purpose.
- **`/healthz` and `/readyz` are open on every server, `/metrics` is open unless `MONITORING_TOKEN` is set.** Fine on loopback, which every server binds by default; `security.py check` warns when the token is unset. The bot's runner has no security middleware at all (Phase 18 left it that way; unchanged), so on a public tunnel only `/ws` and the webhook path should be reachable — the ops routes included.
- **The scheduler's port is one per machine.** Two `campaign.py run` processes on one host: the second logs `ops.port_unavailable` and serves nothing; give it `MONITORING_PORT` of its own in the shell if both should be scraped.
- **The fleet gauges in a server process refresh every 30 s from three aggregates.** Cheap at this size; on a large `call_attempts` the throughput aggregate is a scan over the window (Failed §28 says why nothing is indexed for it). Raise `MONITORING_REFRESH_SECS` before adding an index.
- **A Phase 7 check had rotted with the calendar.** `test_campaigns.py` pinned a callback to 2026-09-07 10:00 and compared it with the real clock; from 2026-09-08 10:00 Karachi "the queue will not hand it out before then" failed on every run (it was red in this session's baseline). It now uses the next Monday from the day it runs. Nothing in the code changed.
- **No fleet has run against a real carrier.** Two workers over one database have only ever been the checks' workers over an in-memory store and a throwaway schema. The first real run should be two `campaign.py run` processes on one machine with `MAX_CONCURRENT_CALLS=1` and `campaign.py workers` open in a third terminal.
- **`WORKER_STALE_SECS` is a judgement, and 60 s is the default, not a measurement.** A worker on a database that stalls for longer than that is declared dead mid-call and its calls adopted; when it comes back it drops them (`worker.handed_over`), so nothing is counted twice — but the adoption is noise. Raise it on a slow database; never below three heartbeats (`config.py` refuses less than two).
- **The concurrency limit is one number for the fleet, and a busy worker may hold all of it.** `_capacity()` is the process's own ceiling and the reservation counts the same number across everyone; with three workers and `MAX_CONCURRENT_CALLS=2`, one of them can be following both calls while the other two skip. Fair sharing is not built.
- **Transient-failure retries spend attempts.** A carrier outage that fails a placement three times exhausts the prospect at `CAMPAIGN_MAX_ATTEMPTS=3`. That is the ceiling doing its job; if an outage should not count, pause the campaign.
- **`scheduler_workers` rows are kept until pruned** (`campaign.py workers --prune 24`). Nothing prunes automatically.
- **No call has written a quality summary yet.** The dashboard's response latency, failed turns and service errors come from `usage -> 'quality'`, which the sink writes at teardown from Phase 20 onwards; on this machine every tile that needs it reads "unavailable" until the next call ends. `campaign.py rebuild-results` does not backfill it (the figures are in `conversation_data`, and a backfill would be a one-off script over that JSON if anyone wants history).
- **The dashboard's `--once` output is the same JSON the page reads, unfiltered.** A filtered export is `GET /api/dashboard?campaign=…&from=…&to=…` with a key, not a CLI flag.
- **A viewer sees every campaign.** Masked, and without transcripts, but every campaign; a per-campaign or per-team grant is not built.
- **Search is a scan.** `ILIKE` over the prospects and a join for the calls, bounded by `LIMIT`; milliseconds at this list size, and not indexed on purpose (Failed §28). A list of a million rows would want a trigram index, and that measurement has not been made.
- **No jurisdiction rules are configured, and none should be invented.** `COMPLIANCE_JURISDICTIONS` is empty in this machine's `.env`; the examples in `COMPLIANCE.md` and `.env.example` are illustrations of the *shape*, labelled as such, not statements of any law. The operator decides the hours, ceilings and disclosures per country with advice from somebody qualified to give it; the software applies whatever is written.
- **The do-not-call list on this machine is empty**, and `campaign.py init` has created the table. A list loaded *after* prospects exist needs `campaign.py dnc-apply` once (imports and creates from then on apply it themselves).
- **No agent has been heard speaking a required disclosure.** The opening instruction and the system instruction carry it, and `compliance.disclosure` records what was instructed; whether a given model actually opens with it is an eval question (`server/evals/`), and the sales suite has no scenario for it yet. Add one before turning `COMPLIANCE_AI_DISCLOSURE_REQUIRED` on for real calls.
- **The gate has never refused a real call.** Every verdict is verified over the in-memory store and the SQL over a throwaway schema; the first real run should be watched with `campaign.py compliance-log` open.
- **Dispositions changed meaning.** A verbal request is now `OPTED_OUT`; `DO_NOT_CALL` means the list refused the dial. A consumer that filtered on `DO_NOT_CALL` for "they asked" (a CRM report, an n8n workflow) needs both values; `n8n/README.md` says so.
- **A jurisdiction's lower attempt ceiling costs one reservation per prospect.** The reservation SQL applies the campaign's figure; the gate applies the jurisdiction's after reserving and exhausts the membership without dialling. Correct, audited, and one wasted transaction per such prospect — a per-number ceiling in the SQL would need the region on the prospect row.
- **`server/.env` is untracked but not committed, and its history stands.** Phase 18 ran `git rm --cached .env` in the nested `server/` repository and added `server/.gitignore`; the removal is *staged* and nothing was committed (the user commits). The file is still in that repository's first commit: if it was ever pushed, every key it carried — Deepgram, Groq, Cartesia, SignalWire, and whatever was added since — is compromised and must be rotated in each vendor's console; a history rewrite is the user's call.
- **No server has run behind a real TLS proxy.** `SECURITY_REQUIRE_HTTPS`, HSTS, the `Secure` cookie and the trusted-proxy rules are verified through FastAPI's test client with a forwarded scheme; the Caddy and nginx snippets in `SECURITY.md` are documented shapes. The first deployment behind a proxy should run `uv run security.py check --strict`, then sign in over HTTPS and confirm the cookie is sent (`Secure`) and a plain-HTTP page is redirected.
- **`DASHBOARD_USERS`, `DASHBOARD_SESSION_SECRET` and the role-scoped keys are not in `.env`.** The dashboard refuses to start without a user (by design); `security.py` prints how to make one. The checks configure their own.
- **The audit table is new: run `uv run campaign.py init` on any existing database.** Idempotent. Until then every write still goes ahead and `audit.unavailable` is logged once a minute (`SECURITY_AUDIT_STRICT=true` refuses instead).
- **Rate limits are per process.** Two API processes each allow their own `SECURITY_API_RATE_LIMIT`. A distributed limiter needs infrastructure the project has none of.
- **The bot's dev-runner web server is not hardened.** `/client` and `/ws` are Pipecat's; the webhook route mounted on it counts refusals per address but the runner's app has no security middleware (adding one would sit in front of the browser client and the carrier's websocket). In production only the tunnel's path to `/ws` and the webhook route should be reachable.
- **No real n8n instance has received a delivery, and no workflow file has been imported into a live n8n.** The request, the signature and the retry rules are verified against a recording sender; the six JSON files are valid n8n workflow JSON whose connections name real nodes and whose node types and versions are the current ones (`httpRequest` 4.2, `webhook` 2, `if` 2, `set` 3.4, `code` 2, `splitInBatches` 3, `scheduleTrigger` 1.2, `slack` 2.2, `readWriteFile` 1, `extractFromFile` 1). The first live test: `AUTOMATION_API_KEYS` and `AUTOMATION_WEBHOOK_URL` in `.env`, `uv run campaign.py init`, import `04-qualified-lead-notification.json`, select the two Header Auth credentials, activate it, `uv run automation.py --once`, then `uv run campaign.py events`. A 404 in `last_error` means the workflow is not active (or the Test URL was used instead of the Production URL). If a node fails to import, the node's `typeVersion` is the first thing to check against that n8n's version.
- **An existing database needs `uv run campaign.py init` again** for `automation_events` and `api_requests`. Idempotent; **done on this machine's database on 2026-09-07**. Without it every automation route answers 503 naming the command, and the deliverer logs `automation.claim_failed` once per pass.
- **Set `AUTOMATION_EVENTS_SINCE` before the first delivery on a database with history.** Unset, the first pass creates a `call.completed` for every result ever written — on this machine the drill rows from Phases 8–16 — and delivers them all. `campaign.py events` shows what a pass would send before any URL is configured only if a URL is configured; the honest check is `AUTOMATION_EVENTS_SINCE` set to now, first.
- **`AUTOMATION_API_KEYS` is not in `.env`.** The API refuses to serve without it (by design); `automation.py` prints how to generate one. The smoke boot on 2026-09-07 passed the key in the shell.
- **The HubSpot nodes in workflows 03, 05 and 06 name the `ai_*` contact properties** that Phase 15's `crm-sync` creates on its first run (`ensure_schema`). On a portal that has never run `crm-sync`, HubSpot rejects the PATCH with an unknown-property error; create them or delete those keys from the node's JSON body.
- **No real Cal.com booking and no live transfer has been observed.** Phase
  16 verified both against stubs and the vendors' documented shapes, and
  `.env.example`'s last section says exactly what a live test needs. For
  Cal.com: the key, the event type id, a matching `CALENDAR_SLOT_MINUTES`,
  then `uv run health.py calendar` (it reads the event type back and flags a
  length mismatch) before any call. For a transfer: `TELEPHONY_TRANSFER_NUMBER`
  on a phone you can answer and a public URL, then a call, then
  `uv run campaign.py transfers` — `REQUESTED` should become `ANSWERED` (or
  `NO_ANSWER` with the fallback spoken) once the leg ends. `.env` currently
  has neither a transfer number nor Cal.com credentials.
- **The transfer outcome needs the webhook receiver.** Without
  `TELEPHONY_PUBLIC_URL` (and on SignalWire the signing key) the transfer
  still works with the inline fallback, but every row stays `REQUESTED`.
- **No call has been filed with a real HubSpot portal.** `HUBSPOT_ACCESS_TOKEN`
  is not in `.env`, so `CRM_PROVIDER` is `none`, `health.py` skips the `crm`
  component, and `campaign.py crm-sync` says what it needs. The adapter is
  verified against HubSpot's documented endpoints and property names over a
  stub session; the first live sync is the first real test. To run it: create
  a private app with the scopes the config error names, set the token and
  `CRM_PROVIDER=hubspot`, `uv run health.py crm` (reads one contact),
  then `uv run campaign.py crm-sync --once` and `crm-status`. Every existing
  result in the database will be filed on that first pass — there are a
  handful of drill rows from Phases 8–13 — so expect them in the portal, or
  mark them `SKIPPED` by hand first.
- **HubSpot's search index lags a create by "a few moments"** (documented).
  A create whose answer was lost is looked up by key on the next pass; with
  `CRM_SYNC_RETRY_SECS=60` that pass is a minute later, which is enough. Set
  it much lower and a duplicate activity becomes possible.
- **No live webhook delivery has been received.** Every check in
  `test_webhooks.py` signs deliveries the way the carriers' own libraries do
  (Twilio's documented example is reproduced byte for byte; SignalWire's SDK
  delegates to the same algorithm keyed by its signing key), but no carrier
  has yet POSTed to this receiver. The first real call is the first real
  delivery: run `uv run call.py <your phone>` with the bot up and a tunnel in
  `TELEPHONY_PUBLIC_URL`, then `uv run campaign.py webhooks` — the ledger
  should show `initiated`, `ringing`, `in-progress`, `completed` for that
  call, each `unmatched` (a one-off call has no attempt row). If it shows
  nothing, the carrier could not reach the URL; if `campaign.py webhooks`
  shows nothing and the bot logs `webhook.refused`, the signature did not
  match — for SignalWire that means the wrong key in `SIGNALWIRE_SIGNING_KEY`.
- **`SIGNALWIRE_SIGNING_KEY` is not set in `.env`.** Until it is, SignalWire
  calls are placed *without* a status callback (deliberately: events that
  could not be verified would only be refused) and the worker polls exactly
  as in Phase 13. The key is on the dashboard's API credentials page.
  `campaign.py run` prints which mode it is in.
- **No unattended run has been made against a real carrier.** `campaign.py run`
  has been driven end to end against the real PostgreSQL with a stub carrier
  (`test_worker.py`'s last section) and against the in-memory store for every
  scenario, but never with SignalWire on the line. The first real run should be
  `uv run campaign.py run <campaign> --max-calls 1` with a campaign whose only
  prospect is your own phone, the bot up, and `health.py` green — it dials
  whatever is `ACTIVE` in the database, so check `campaign.py status` first.
- **No real call has been placed in Phase 12 either.** `tests/live_call.py` is
  the workflow — it needs a tunnel URL in `TELEPHONY_PUBLIC_URL`, the bot up,
  and a phone in your hand — and it has been dry-checked (argument parsing,
  config, the report reader) but never dialled. Everything the phase measured
  was over `tests/fake_carrier.py`'s wire protocol, which is faithful to the
  framing and the μ-law but not to a real line, a real handset or a real
  accent. Run it: `uv run python tests/live_call.py --to +92300XXXXXXX`.
- **Carrier-side answering-machine detection is unverified live.**
  `TELEPHONY_MACHINE_DETECTION=async` sends Twilio's documented
  `MachineDetection=Enable` + `AsyncAmd=true` and reads `answered_by` back from
  the call resource; the stub tests pin the request and the mapping. Whether
  SignalWire's Compatibility API populates `answered_by` for an async
  detection without an `AsyncAmdStatusCallback` is the one thing to confirm on
  the first live call with it on. Default is `off`.
- **`VOICEMAIL_ACTION=message` does not hear the beep.** It waits
  `VOICEMAIL_MESSAGE_DELAY_SECS` after the greeting's turn ends and speaks.
  The drill only exercises `hangup` (the default); message mode is covered by
  `test_voice_quality.py` against fakes.
- **The greeting still takes 4–7 s after the call connects**, and on the free
  Groq tier up to 13 s when the first request is throttled. What remains is
  per-session: two vendor websockets (1.7 s), the Silero and Smart Turn model
  loads (0.6 s), and the greeting inference. A fixed opening line spoken from
  a `TTSSpeakFrame` while the model composes the real one would cut it
  further; not built, because it changes what the agent says first.
- **An existing database needs `uv run campaign.py init` again** for Phase 11's `usage` and `cost_usd` columns. Idempotent; done on this machine. Calls before that recorded no usage, and `attempt_counts` reports `usage_available: false` rather than zeros.
- **Per-call usage is recorded from Phase 11 onwards only.** There is no backfill and there cannot be: the token counts were never captured for earlier calls. The dashboard says how many calls have usage next to every figure derived from it.
- **TTS characters are not measured on the Deepgram TTS path.** Pipecat 1.8.1's websocket Deepgram TTS never reports them (Cartesia does), so a cost total on that path names `tts` as `unmeasured`. `TTS_PROVIDER=cartesia` measures it. See [Failed §29](#29-believing-a-zero-that-nobody-measured-phase-11).
- **The prompt is still 3,394 tokens per request**, 1,248 of them tool schemas. The obvious fix is measured, understood, and blocked — see [Considered and rejected](#18-considered-and-deliberately-rejected).
- **The dashboard has no authentication and no filtering.** It is an operator's local tool: loopback by default, everything or nothing, no date range and no per-campaign drill-down. A login and a campaign filter are the two things anyone will ask for first, and both are small — the JSON endpoint already takes the shape a query string would filter, and `disposition_counts` already accepts a `campaign_id`.
- **The dashboard does not auto-start with anything.** `uv run dashboard.py` is a separate command from `uv run bot.py`. That is deliberate (see [Decisions](#phase-10)), but it does mean somebody has to remember to run it.
- **An existing database needs `uv run campaign.py init` again** for the Phase 9 columns (`idempotency_key`, `placement_started_at`) and the rebuilt live-attempts index. Idempotent; done on this machine. Attempts written before it have no idempotency key, which is safe — the reservation lock still protects them — and every new attempt gets one.
- **There is still no webhook endpoint.** Carrier status is polled. `apply_call_event` is written to be the entry point for a webhook when one arrives — it is idempotent and takes a carrier call id — but nothing serves one, so "duplicate webhook" is covered by the check scripts and not by a live delivery.
- **The concurrency limit and pacing are in-process.** With `MAX_CONCURRENT_CALLS=1` and one dialer they are exact. Two dialers would each allow their own limit; they could not cause a *duplicate call* (that is protected in the database) but they could exceed the intended rate. A shared limit needs either a database counter or the scheduler that Phase 9 deliberately did not build.
- **`campaign.py call --count N` is not a scheduler.** It places N calls with pacing between them and stops on an ambiguous placement. Nothing runs unattended, so a scheduled callback still only happens when somebody runs the command.
- **Results for attempts that finished before 2026-09-04 need `uv run campaign.py rebuild-results`** — once, after `campaign.py init`. Done on this machine's database. New attempts get theirs automatically.
- **Phase 9's drills left rows in the database.** A campaign `Phase 9 drill` (id 2) with one prospect `Drill Subject` (+923009999001, id 4) and attempts 4 and 5, both closed. Kept as a worked example of the ambiguous-placement path; `DELETE FROM campaigns WHERE name = 'Phase 9 drill'; DELETE FROM prospects WHERE phone_normalized = '+923009999001';` removes them. Prospect `Sara Ali` is now `EXHAUSTED` in `Q1 Outreach` after a real SignalWire refusal (21219, an unverified number).
- **The Phase 8 manual test left one row in the database.** Attempt 2 for prospect 1 (`telephony_call_id` `CAmanual-phase8`, provider `fake`), created by hand so a simulated call could land a result on it; its result is the greeting-only `COMPLETED` row. The attempt was then set to `COMPLETED` by hand (a fake provider has no carrier to reconcile from, and a `QUEUED` attempt counts as live and would have blocked prospect 1 in the queue), which also exercised the precedence rule on the real row: `record_carrier_result` logged "the conversation's result stands" and returned None. Harmless and a worked example; `DELETE FROM call_attempts WHERE telephony_call_id = 'CAmanual-phase8';` removes it and its result.
- **The transcript's timestamps are when a turn was *recorded*.** For the agent that is after playout, so the greeting on a phone call shows `at` ≈ 15 s. Honest, and documented in `transcript.py`; a start time would need a different event.
- **The Groq tier.** The code runs; the free tier throttles it — see [Known issues](#5-known-issues-and-limitations). Somebody has to decide: pay Groq, switch `LLM_PROVIDER`, or cut the tool count. That decision gates any live use of Phase 7.
- **Cal.com has never been called.** `CalComProvider` is written against the documented v2 API and tested against a stub. The first live call is the first real test; the two places most likely to need a one-line fix are marked in `scheduling/calcom.py` (the slots response shape, the notes field name).
- **A live transfer has never happened.** `transfer_call` is verified against a stub session; SignalWire's Compatibility API accepts the same live-call update, but nobody has watched a call move. Set `TELEPHONY_TRANSFER_NUMBER` to your own phone and try it — after `tests/fake_carrier.py`, which cannot exercise it (the fake carrier does not implement the REST update).
- **Nothing pushes the call result anywhere.** Phase 8 gave every finished
  attempt a `call_results` row, a summary, and a flat `to_dict()` export that
  `campaign.py result --json` prints — and no webhook, no CRM client, no sync.
  The mapping onto HubSpot / Pipedrive / Salesforce is a table in
  `src/campaigns/results.py`; implementing any of it is a later phase.
- **No scheduler.** `campaign.py call` places calls one at a time when a person
  runs it. The queue is built for a worker to sit on later — the reservation
  already uses `FOR UPDATE SKIP LOCKED`, and a scheduled callback now lands in
  it as a `next_attempt_at` — but there is no worker, no concurrency limit and
  no calling-hours window. This is the piece that makes callbacks *happen*
  rather than merely queue.
- **A callback does not override the attempt cap.** A membership reopened for a
  callback is still subject to `CAMPAIGN_MAX_ATTEMPTS` in the queue's SQL, so a
  prospect on their last permitted attempt who asks to be called back appears in
  `campaign.py callbacks --due` and nowhere else. Deliberately left: changing the
  queue's eligibility rule is a Phase 5 change and a policy question.
- **The local calendar's check-then-write is not transactional.** `book`
  confirms the slot is free, then the service inserts the row. Two bots booking
  the same slot in the same second could both succeed. One bot placing one call
  at a time cannot hit it; a scheduler running bots in parallel can, and the fix
  is an exclusion constraint on `meetings(start_at, end_at)`.
- **The Phase 6 tools were renamed.** `do_not_call` → `mark_do_not_call`,
  `request_callback` → `schedule_callback`. Anything outside this repository
  that asserted on the old names — there should be nothing — will break.
- **The campaign smoke test left rows in the database.** `Q1 Outreach` and three
  prospects from `/tmp/p5/prospects.csv`, created on 2026-09-03 while verifying
  the CLI against the real database. Harmless, and a worked example; remove them
  with `DELETE FROM campaigns WHERE name = 'Q1 Outreach';` and
  `DELETE FROM prospects;` if you want a clean slate.
- **A real phone call has never been placed.** There is no Twilio account on this machine. Everything on the placement side is verified against a stub, and the audio side against a simulated carrier. See [Known issues](#5-known-issues-and-limitations).
- **Inbound calls are not wired to anything.** The `/ws` route accepts them and the pipeline handles them — `CallSession` defaults to `inbound` — but no number is pointed at this bot and nothing was tested that way. An inbound caller is also, correctly, an anonymous prospect: the agent is told it does not know who it is speaking to. Whether an inbound call should look somebody up *by number* is a real question and nobody has asked it.
- **A do-not-call on an anonymous call is honoured but not stored.** The agent stops selling and closes; with no prospect id there is no row to write to, and the log says so loudly. That is correct behaviour for a browser session and a genuine hole for an inbound phone call, which does have a number to look up.
- **`conversation_data` needs a re-init on an existing database.** `uv run campaign.py init` adds the column and is idempotent. Nothing breaks without it — the readers tolerate a missing column and the writer raises a message naming the command — but no conversation is stored.
- **Answering-machine detection.** Twilio's `MachineDetection` is not used, so the agent talks to voicemail as if it were a person. The call summary flags "the other end never spoke", which is the cheap version. Real AMD belongs with the campaign work.
- ~~**The two stale eval scenarios.**~~ Resolved in Phase 6: `conversation` and `voice_quality` were rewritten once the product question had an answer. See [Phase 6](#phase-6-this-session).
- **Live microphone test** — the agent has never been talked to through a real mic. Everything is verified through synthesized audio.
- **Speculative inference on eager end-of-turn** — `FLUX_EAGER_EOT_THRESHOLD` is exposed but nothing consumes it. Acting on it means starting the LLM early and cancelling if the caller keeps talking; not implemented.
- **Turn-detection latency tuning** — `turn-end` is half the total wait. Deliberately left at Deepgram's default; see [Decisions](#6-decisions-made-during-development).
- **Context Hub** — not installed. `uv tool install "pipecat-ai[cli]"` then `pipecat context-hub install` would give future sessions indexed Pipecat docs. Index build is 5–10 min and ~750 MB; it is the user's call.

---

## 5. Known issues and limitations

**The one to read first, as of Phase 7: Groq's free tier cannot keep up with a twelve-tool agent.**

Measured on 2026-09-04 with a direct request carrying the real system prompt and
the real tool schemas (`x-ratelimit-*` headers, `usage.prompt_tokens`):

| | |
|---|---|
| Organisation limit, `qwen/qwen3.8-27b`, on-demand tier | **8,000 tokens per minute**, 1,000 requests per minute, 1,000 *output* tokens per minute, and **200,000 tokens per day** |
| One request, system prompt only | 1,603 prompt tokens |
| One request, system prompt + twelve tool schemas | **3,094** prompt tokens (3,843 before the descriptions were trimmed) |
| One *tool turn* | two requests — the call, then the reply to its result — so ~6,300 tokens |

So a second request inside the same minute is refused with a 429 carrying a
`retry-after`, the OpenAI SDK inside Pipecat's Groq service retries after it
**silently** (its retry logging is on the standard-library logger, which loguru
does not show), and the turn simply takes 20–50 seconds. Observed: greeting in
1.4 s, next turn 18 s, the turn after 49 s, with the pipeline's heartbeat monitor
warning that a processor looked stalled. Nothing in the bot's own log says why;
the `x-ratelimit-reset-tokens` header does.

And the day has a ceiling too. At 13:30 on 2026-09-04, after one afternoon of
Phase 7 verification — roughly sixty requests across the check runs, five
scenario runs, three re-runs and a measurement — the organisation had used
199,843 of its **200,000 tokens per day**, and every request, including the
greeting, was refused with `Rate limit reached … on tokens per day (TPD)…
Please try again in 24m`. That is what ended the eval work for the day, and it
is why two scenarios below are recorded as "could not be re-run" rather than as
a result. At ~3,100 tokens per request, 200,000 a day is about sixty turns:
**one afternoon of evals, or a handful of real calls.**

Three separate things, and only one is fixed:

1. **Output cap.** With no `max_completion_tokens` sent, Groq estimated the reply
   at 1,815 tokens and refused *every* request against the 1,000 OTPM limit
   before the model said a word — the bot answered the phone and said nothing.
   Fixed: `LLM_MAX_OUTPUT_TOKENS` (default 400) is always sent. This is also
   simply correct for a voice agent.
2. **Input budget.** 8,000 a minute is the tier. Trimming the tool descriptions
   and the prompt's tool section took a request from 3,843 to 3,094 and that is
   close to the floor — Groq's chat template costs ~100 tokens per tool before a
   word of description. The remaining options are the user's: a paid Groq tier
   (the developer tier is tens of thousands of TPM), another `LLM_PROVIDER`
   (Cerebras' free tier is larger; Anthropic/OpenAI need credits), or fewer
   tools advertised per turn (an idea, untried and with a catch — see
   [Decisions](#phase-7)). **The eval scenarios that involve a tool carry
   `within_ms: 150000` on the affected turns for exactly this reason**; a live
   call has no such patience.
3. **Daily budget.** Nothing to fix in code. 200,000 tokens a day is the tier,
   and the eval judge (`evals/groq_judge.py`, the same model by default) draws
   on it too. Plan an eval afternoon as ~60 requests, or move the judge — or the
   agent — to a model with its own limits.

The rest of this section is unchanged from Phase 6 and still true.

**Whether the model calls a tool is the thing that varies, and it is what the sales evals mostly measure.**

The conversation layer itself is deterministic and covered exhaustively by `tests/test_conversation.py` — 249 checks, one second, no keys. What is *not* deterministic is whether Groq/Qwen decides to call `record_discovery` on the turn where the prospect describes their problem. Measured on 2026-09-03, that turned out to be almost entirely a prompt-placement question rather than a model-capability one; see [Failed §16](#16-describing-the-tools-only-in-the-system-prompt). The fix (naming the one tool the current stage needs, in the block immediately before generation) took it from "called nothing across three turns" to calling the right tool. It is still the axis on which a sales eval fails, and it is model-dependent: a different `GROQ_MODEL` may need the hints tuned again.

**Run the sales suite with `-r 2` before believing a single result**, for the reason the rest of this section has always given.

---

*Resolved in Phase 6, kept because the shape of it recurs:* `conversation` and `voice_quality` were flaky from Phase 3 to Phase 5. Both are Phase 2 scenarios that asked the agent general-knowledge questions ("what about Spain?", "what day comes after Tuesday?"). Phase 3 told the agent to answer only from the knowledge base and never from its own knowledge, so it sometimes politely refused, or asked the caller to repeat, and the judge — correctly — said it did not answer. Whether it refused varied run to run, which is what made it flaky rather than simply broken. Measured on 2026-09-03, before the rewrite:

| Run | Result | What failed |
|---|---|---|
| Full suite, `KB_ENABLED=true` | 4/6 | `conversation` — "did not state Madrid"; `voice_quality` — "did not say Wednesday" |
| Full suite again, code unchanged | 4/6 | `conversation` **passed**; `voice_quality` failed differently ("asked the user to repeat"); `knowledge_unknown` failed on a *transcription* artefact in the greeting turn |
| Full suite a third time | 4/6 | `voice_quality` **passed**; `conversation` and `knowledge_unknown` failed |
| The affected scenarios, `KB_ENABLED=false` | 2/2 pass | — |

Three runs, three different pairs of failures, and always four out of six. `barge_in` and `silence` passed every time.

Two separate things are in there, and neither is telephony:

1. **The grounding collision.** `conversation` and `voice_quality` ask general-knowledge questions and the grounded agent sometimes refuses them. The `KB_ENABLED=false` row is the proof: with the knowledge stage removed, the whole Phase 2 agent still passes over the same pipeline Phase 4 changed.
2. **Judge and transcription noise.** `knowledge_unknown`'s failure was the judge complaining about how Moonshine transcribed a greeting, not about what the agent said. That is the harness, not the bot.

**Run anything here with `-r 3` before drawing a conclusion from a single result.** Two full passes disagreed about which scenarios fail. This project has been fooled by exactly this before — see [Failed §2](#2-fixing-the-post-barge-in-truncation-with-the-system-prompt), where a real defect was only separable from noise by repeats.

Phase 6 answered the product question that kept those two open: the agent is a sales representative, so both scenarios were rewritten to test the same mechanisms with turns that belong in a sales call, and off-topic behaviour moved to `sales/unrelated_question.yaml` where it belongs.

| Issue | Impact | Notes |
|---|---|---|
| The voicemail length rule needs the recording to talk over the greeting | A machine whose greeting starts *after* the agent finishes speaking is caught only by the phrase list | Phase 12, deliberate: the first version tripped on a fast eight-second human answer. See Failed §32 |
| A spurious interruption still silences the bot for a moment | The caller hears the agent stop, then resume a second or two later with a fresh sentence | Phase 12. Flux opens the turn on the noise; the resume is an instruction, so it is the model's words, not a replay |
| `turn.failed` fires on a throttled turn | A Groq free-tier turn that takes 15 s is logged as failed at 10 s, then `turn.late_response` | Phase 12, deliberate: the caller *did* wait 10 s in silence. Raise `TURN_RESPONSE_TIMEOUT_SECS` on a paid tier only if it is noisy |
| The call report is a file per call | A busy campaign writes many small JSON files to `CALL_REPORT_DIR` | Phase 12. The same record is on the attempt row; the file exists for the two processes that cannot read the database mid-call |
| Whether the model calls a tool is model-dependent | A missed `record_discovery` leaves a pain point out of the CRM record — quietly | The stage block names the stage's tool on every turn, which is what made it reliable on Qwen. Re-verify after changing `GROQ_MODEL` |
| A do-not-call fires a database write inside the turn | A slow database adds that latency to the reply | Deliberate: the request must be recorded before the call can drop. The sink swallows its own errors, so a *failing* database costs nothing |
| An anonymous call's do-not-call is honoured but not stored | A browser or inbound caller who asks not to be called has no row marked | Logged as a warning naming the gap. Inbound number lookup is the fix and nobody has asked for it |
| `conversation_data` is the raw record; `call_results` is the reading of it | Two copies of the transcript per attempt (raw and projected) | Phase 8, deliberate: the raw record is what a rebuild reads. `campaign.py result` shows the projection |
| The user side of the transcript comes from the director, the agent side from the aggregator | A caller turn that never reached an inference (spoken after `end_call`, say) is not in the transcript | Phase 8. The director's path is the one the detectors and the model saw, which is why it was chosen; the aggregator's own `on_user_turn_stopped` would be the other source |
| `questions` is a heuristic | A question phrased as a statement is missed; "do it by hand" opens with an interrogative and may be kept | Phase 8. Verbatim and beside the transcript, so a reader can see. `extract_questions` is one function to tune |
| A result's duration is the bot's view on a phone call | A second or two shorter than the carrier bills; `call_attempts.duration_seconds` takes the carrier's on reconciliation | Phase 8. `source` on the row says who wrote it; both numbers are kept, on their own rows |
| Results are not backfilled automatically | An attempt that finished before `campaign.py init` added the table has no row | `uv run campaign.py rebuild-results`, once. Done on this machine |
| Usage is measured only from Phase 11 onwards | Averages over "calls with usage" cover a subset of the history | No backfill is possible — the counts were never captured. Every derived figure states the count it is over |
| Deepgram's websocket TTS reports no characters | A cost total on that path omits TTS and says so (`unmeasured`) | Pipecat 1.8.1 only instruments its HTTP variant. `TTS_PROVIDER=cartesia` measures it |
| The dashboard cache is in-process | Two dashboard processes each keep their own, so a read every 5 s each | Holds no state that can be wrong, only numbers that were true a moment ago |
| The prompt is 3,394 tokens per request | On the free Groq tier that is ~2.4 requests per minute before throttling | 1,248 tokens are tool schemas; the fix is blocked by a framework race, see Considered and rejected |
| An ambiguous placement blocks its prospect until recovery runs | One prospect uncalled, for as long as nobody runs `campaign.py recover` | Phase 9, deliberate and the safe direction. `campaign.py call` runs recovery first; `health.py` flags live attempts as degraded |
| Recovery cannot resolve an attempt if the carrier cannot list calls | The attempt is closed as failed and the reason says to check the carrier's log by hand | Both supported carriers *can* list calls, and it was exercised live against SignalWire. A future carrier without the endpoint gets the honest dead end rather than a guess |
| Concurrency and pacing are in-process | Two dialers would each allow their own limit | Cannot cause a duplicate call — that is protected in the database. It is a rate limit, not a correctness one |
| No call has been filed with a real CRM (Phase 15) | The HubSpot adapter is verified against a stub session, not a portal | The first `crm-sync` with a token is the test; `crm-status` shows what each row came to. See Pending tasks |
| A create whose answer was lost is found by a search that can lag (Phase 15) | With a retry interval far below HubSpot's index delay, an activity could be filed twice | `CRM_SYNC_RETRY_SECS` defaults to 60; the ambiguity is logged and the next pass searches first. Same shape as Phase 9's ambiguous placement |
| The `ai_*` contact properties need a scope the token may lack (Phase 15) | Without `crm.schemas.contacts.write` the structured facts are not on the contact | Logged once per run as `crm.schema_unavailable`; the call is still filed and its body carries every fact. Create the properties by hand, or grant the scope |
| A result changed after filing is re-sent whole (Phase 15) | The activity is rewritten; a note somebody added to it in the CRM is kept, but the body they may have edited is replaced | Deliberate: the row is the source of truth for the call. Edit the row (`rebuild-results`) rather than the CRM |
| No live webhook delivery has been received (Phase 14) | The receiver is verified against the carriers' documented signing and their SDKs, not against a carrier | The first real call is the test: `campaign.py webhooks` afterwards should list its events. See Pending tasks |
| A machine verdict that arrives after the completion cannot relabel the call (Phase 14) | A short voicemail whose async AMD verdict lands after `completed` reads `COMPLETED`, not `VOICEMAIL` | Logged as `webhook.late_verdict`. The monotonic rule that protects every conversation outcome is the same rule that stops this; polling had the same window. The bot's own detection (Phase 12) is the other half |
| The receiver shares the bot's process by default (Phase 14) | A burst of deliveries is a burst of short database writes on the bot's event loop | One HMAC and a few awaited statements per event; nothing touches a pipeline. `TELEPHONY_WEBHOOK_RECEIVER=standalone` moves it to `webhooks.py` |
| `call.py`'s one-off calls are `unmatched` on the ledger (Phase 14) | Their events are recorded but update nothing — there is no attempt row | Deliberate; `call.py` still watches by polling. The campaign paths are the ones the webhooks update |
| The calling window uses the prospect's timezone only if their record supplies one | A list imported without a `timezone` column is called in `CALLING_TIMEZONE` | Deliberate: a country code does not determine a timezone, and guessing puts the call at the wrong hour invisibly |
| A supervised ending writes a note, not a distinct status | A call cut off by the duration ceiling reads as `COMPLETED` with a note | The note is on the call result and the reason is in the log. A distinct disposition would need a Phase 8 vocabulary change |
| Retrieval gating is a heuristic | A product question phrased with no interrogative and no commercial noun is not searched | Written to skip rather than allow, so the failure needs all three signals absent. `KB_RETRIEVAL_MODE=always` restores Phase 3 |
| The conversation opens a second database pool per session | Two more connections per call, closed at session end | One bot per call, so it is bounded. A long-lived multi-session host would want a shared pool |
| A call nobody follows and no webhook reaches sits in `QUEUED` until the next recovery pass | A worker that died and a receiver that is down together leave an outcome unwritten for up to `WORKER_RECOVERY_INTERVAL_SECS` | Phase 14 halved this: a webhook writes the row with no worker at all. Recovery remains the backstop for the other half |
| The scheduler is one process (Phase 13) | Pacing is in-process, so a second worker would halve the interval; each worker follows only the calls it placed plus what it adopted at start | The reservation, the idempotency key and the concurrency count are in the database, so a second worker cannot cause a duplicate call — it would only pace wrongly. See [Next recommended steps](#12-next-recommended-steps) |
| A prospect is dialled only when *both* the configured window and their own are open (Phase 13) | A prospect whose imported timezone never overlaps `CALLING_HOURS` in `CALLING_TIMEZONE` is deferred every time and never dialled | Deliberate: the configured window is the operator's rule and a prospect's own zone can only narrow it. Set the window for the list, or split lists by zone |
| `DEFAULT_PHONE_REGION` is unset by default | Local-format numbers in a CSV are all rejected | Deliberate — guessing a country dials a stranger. Set it to the country your lists are written in |
| No real phone call has ever been placed | The one thing that cannot be proved here | No carrier account on this machine. Placement is verified against a stub, audio against `tests/fake_carrier.py` |
| Twilio has no trial in Pakistan | The reference carrier is unusable for the person building this | Hence SignalWire, whose trial needs no card. Both are supported and the bot is identical either way |
| SignalWire's REST paths are assumed to accept the `.json` suffix | A 404 on the first real call | Its Compatibility API mirrors Twilio's URL scheme, but this has not been exercised against a live account. If placement 404s, drop `.json` in `TwilioProvider.place_call`/`fetch_call` |
| A bot with no carrier credentials answers a call but cannot hang it up over REST | The agent's own goodbye does not end the call; it ends when the websocket closes | Deliberate — it is what makes `fake_carrier.py` and a first run work. Configure a carrier for the full path; the warning at call setup says so |
| Answering machines are treated as people | The agent talks to voicemail | No AMD; the call summary flags "the other end never spoke" after the fact |
| `call.py` watches by polling, not by webhook | Its outcome is up to `--poll` seconds late | Deliberate since Phase 14: a one-off call has no attempt row for a webhook to update; see the docstring on `call.watch` |
| Local `+92` numbers are not sold by Twilio | Pakistani prospects see a foreign caller ID | A business problem before the campaign phase, not a technical one |
| Without headphones the agent hears itself | It answers its own voice in a loop and the caller is squeezed out | Acoustic echo; browsers' own cancellation is not always enough at speaker volume. `ECHO_SUPPRESSION=always` fixes it and costs barge-in. See [Decisions](#6-decisions-made-during-development) |
| A browser that vanishes is only noticed by a watchdog | Up to `PEER_TIMEOUT_SECS` before the session ends | WebRTC cannot report it and Pipecat does not poll for it; `PeerWatchdog` does. Setting it to 0 brings back "connects once, then never again" |
| Never tested through a real microphone | Unknown real-world feel | All verification is synthesized audio (Kokoro in, Moonshine out) |
| `turn-end` p50 653ms | Half the total latency | Dominant cost. Tuning it trades against cutting callers off |
| Eval audio is one synthetic voice | No accents, noise, or overlap coverage | Clean-room conditions |
| Loguru error sink is process-wide | Several concurrent sessions in one process would cross-count errors | Fine for one bot per process, which is how this runs |
| `SESSION_IDLE_TIMEOUT_SECS` counts from pipeline start | Under `-t eval` that is boot, not connect, so a waiting eval bot self-terminates at 300s | Override to 3600 for eval runs |
| Post-barge-in reply quality is model-dependent | Fixed for Groq/Qwen; unverified on other providers | The marker fix is model-agnostic in principle |
| Groq free tier: 8,000 input tokens/min vs ~3,100 per request | Every tool turn waits 20–50 s for the minute to reset; a live call falls silent | The headline issue above. `LLM_MAX_OUTPUT_TOKENS` fixed the hard refusal; the throttle is the tier |
| Cal.com provider never run against a live account | First real booking may 4xx on a response shape or field name | Two places marked in `scheduling/calcom.py`; the stub tests pin the request shape. Since Phase 16 `health.py calendar` reads the event type back first, which catches the key and the id |
| A Cal.com booking whose answer was lost is looked up by attendee email (Phase 16) | A prospect with no email cannot be looked up; the caller is told the booking failed and Cal.com may hold it | Cal.com requires an email to book at all, so the case is a booking that was refused on that ground anyway. The lookup is one minute either side of the start, per the documented rounding |
| A transfer's outcome is known only through the webhook receiver (Phase 16) | Without a public URL every `call_transfers` row stays `REQUESTED` | Deliberate: the bot has left the call by then, and only the carrier's `<Dial action>` report knows. `campaign.py transfers` says which mode is on |
| The transfer outcome is not on the call result (Phase 16) | `call_results.transferred` says a transfer was requested, not whether the colleague answered | The result is written by the conversation before the outcome exists. `call_transfers` joins on the attempt; a CRM mapping of it is a later phase |
| Transfer never exercised on a live call | Unknown whether SignalWire moves the call cleanly and closes the stream, and whether its `<Dial action>` report matches Twilio's | Verified against a stub only. Try it on your own number first; `campaign.py transfers` shows whether the report arrived |
| A callback the carrier refuses is withdrawn after one try (Phase 13) | The prospect is not phoned back, and only the log says why | Deliberate: a refusal that repeated every tick would write a failed attempt each time. `campaign.py callbacks --all` shows it `CANCELLED`; place it by hand once the number is fixed |
| ~~Local calendar check-then-write is not atomic~~ | Resolved in Phase 16: an exclusion constraint on `meetings` refuses an overlapping live local booking at the write | A database whose existing local bookings already overlap cannot take the constraint; `campaign.py init` says so and carries on with the check-then-write |
| A meeting on a session with no prospect row is recorded with `prospect_id NULL` | The row exists but nobody knows who it is for beyond `attendee_name` | Eval sessions do this; a real call always has a prospect |
| Windows console encoding | Redirecting bot stdout without `PYTHONIOENCODING=utf-8` crashes on the startup banner | cp1252 vs the banner's box-drawing characters |
| Pipecat 1.8.1 has pre-2.0 deprecations | `PipelineTask`, `EndTaskFrame`, `aggregate_sentences`, `InputParams` are all deprecated aliases | Current code uses the new names throughout |
| `load_dotenv(override=True)` means `.env` beats the shell | `KB_ENABLED=false uv run bot.py` silently does nothing | Edit `.env` to A/B. See [Assumptions §9](#7-assumptions) |

---

## 6. Decisions made during development

Each of these has a reason. Reversing one without the reason is how this regresses.

**Deepgram Flux is the default STT.** The choice of STT provider *is* the turn-taking design here, not just who transcribes. Flux was the single biggest lever on how natural the agent feels. `deepgram` remains as a fallback because not every account has `/v2/listen` access.

**On the Flux path, `src/turns.py` deliberately passes no turn strategies.** Flux pushes its own recommendation (`ExternalUserTurnStrategies`) and the context aggregator adopts it *unless you supply your own*. Overriding them silently disables server-side end-of-turn detection and leaves the transcripts working — it looks fine and feels much worse.

**VAD is configured on both paths.** On the Flux path it no longer drives turn-taking, but it still emits the speech-start/stop frames the latency measurement is anchored to. Removing it would cost the measurements Phase 2 exists to produce.

**VAD and Flux thresholds keep the vendors' tuned defaults.** They are exposed as env vars but not changed. `VAD_STOP_SECS` in particular looks like the "make it snappier" dial and is really the "start truncating callers" dial. Unset means "use Deepgram's default", implemented via `NOT_GIVEN` rather than a number invented here.

**TTS aggregates by sentence by default.** Token streaming (`TTS_STREAM_TOKENS=true`) removes most of a sentence of latency but hands the vendor fragments to guess intonation from. The latency it costs is small next to what it buys in how the agent sounds.

**Turn instructions use `role: "user"`, not `developer` or `system`.** Carried forward from Phase 1, where this was verified: `developer` is OpenAI-only and open-weight chat templates (Groq/Qwen) reject both it and `system` in that position because the template requires the conversation to end on a user turn.

**The agent composes its own check-ins and goodbyes.** Nudges are added to the context as instructions and the LLM writes the words, so they sound like the rest of the conversation instead of canned lines — and the agent knows it already asked.

**Session ends on the audio finishing, not on the LLM finishing.** See [Failed §4](#4-ending-the-session-with-stop_when_done-right-after-queueing-the-goodbye).

**Errors are counted with a loguru sink, not by subscribing to `ErrorFrame`.** The things that actually break a voice agent are wider than error frames — a swallowed handler exception leaves the session looking healthy while a piece of it silently stops running.

### Phase 4

**Twilio, chosen over Telnyx and LiveKit/SIP.** All three can carry the audio. What separated them for *this* project:

| | Twilio | Telnyx | LiveKit + SIP |
|---|---|---|---|
| Pipecat support | Serializer, dev-runner route, XML template, auto hang-up | Serializer + route | Transport, but you bring the SIP trunk |
| Outbound | One REST call with inline TwiML; no webhook needed | One REST call, plus a webhook or SIP setup | Provision a trunk, then dial through it |
| Latency | Third-party routing; independent tests put it ~40ms behind Telnyx | Carrier-owned routing, the lowest measured | Depends entirely on your trunk |
| Price | ~2× Telnyx per minute | Cheapest | Trunk cost plus LiveKit |
| Pakistan | Outbound works once geographic permissions are enabled; no local +92 numbers | Same shape | Whatever your trunk provider allows |
| Setup cost | Lowest | Low | Highest — a SIP trunk is a project of its own |

Twilio wins on the axis that matters at this phase, which is **how little has to be true before a call can be placed**. Telnyx is the one to revisit when per-minute cost or the last 40ms starts to matter; the abstraction exists so that is one module. LiveKit/SIP only makes sense once somebody wants to own the carrier relationship, which is a business decision nobody has made.

**Telephony is a transport, and the transport is the only thing that knows.** No branch anywhere in the pipeline asks whether this is a phone call. The three places that legitimately differ are marked in `bot.py` and explained in `src/telephony/session.py`: the greeting's trigger, the disconnect grace window, and the call-level logging.

**The greeting hangs off a different event on a phone.** A browser and the eval harness are RTVI clients, and RTVI's `on_client_ready` is the right signal there — it fires when the client can actually play audio. A carrier is not an RTVI client and never sends that message, so on a phone call the greeting is triggered by the media stream opening (`on_client_connected`) instead. This is the single most dangerous thing to get wrong in the whole phase: waiting for a message that never arrives produces a bot that answers the phone and says nothing, with no error anywhere.

**A dropped phone call gets a zero grace window.** `DISCONNECT_GRACE_SECS` exists because WebRTC blips and recovers into the same session. A phone call does not: the person redials and gets a new call. Holding on buys nothing and keeps a dead pipeline and a live STT websocket around, so `TELEPHONY_DISCONNECT_GRACE_SECS` defaults to 0 and `ConnectionGuard` takes an override rather than growing a transport check.

**Call outcomes are normalised, and the four unhappy ones are kept apart.** `CallStatus` has `BUSY`, `NO_ANSWER`, `FAILED` and `CANCELED` rather than one failure, because a campaign runner treats them differently: the first two mean the number is fine and the person was not, the third means the call could not be made. An unrecognised carrier status maps to `UNKNOWN` and is deliberately **not** final, so a polling loop keeps watching rather than declaring an outcome it never saw.

**Telephony configuration is validated at the point of use, not at startup.** Every other setting in `config.py` stops the process when it is wrong. Telephony cannot, because that would mean nobody without a Twilio account could run the browser agent — which is how this is developed and how the eval suite runs. The cost is that a missing key surfaces one second into `call.py` instead of at boot; the message names the variable either way.

**Outbound calls carry their own identity in TwiML `<Parameter>` elements.** Twilio's media-stream handshake carries the call and stream ids and nothing else — not the number dialled, not even that the call was outbound. Without those parameters the bot answers a call it placed and has no idea who is on the line. Pipecat reads them back as `call_data.body`, and promotes `from_number` / `to_number` onto the typed fields.

**Inline TwiML rather than a webhook URL.** Twilio's `Twiml` parameter takes the markup on the call-creation request, so an outbound call needs no second public endpoint and no round trip back to us before it dials. Only `/ws` has to be publicly reachable.

**`<Connect><Stream>`, never `<Start><Stream>`.** `<Connect>` is bidirectional. `<Start>` forks a copy of the caller's audio to you and sends nothing back, which produces a bot that hears everything and cannot be heard — and looks perfectly healthy in the logs. `tests/test_telephony.py` asserts on this.

**Status is polled, not pushed.** Webhooks are the better mechanism and need a second public endpoint mounted on the bot's own web server. Polling once a second needs nothing and reports the same six outcomes. The point to switch is when this becomes a campaign runner placing calls in parallel.

**SignalWire is the second carrier, because Twilio has no trial in Pakistan.** Twilio does not offer trial accounts in every country, which made the reference implementation untestable for the person building this. SignalWire's Compatibility API is a deliberate reimplementation of Twilio's — same REST paths under `/api/laml`, same inline `Twiml` parameter, same call statuses, same error numbering, and the same Media Streams protocol down to `event`, `streamSid`, `callSid` and `customParameters`, at the same 8kHz μ-law. So `SignalWireProvider` is a subclass of `TwilioProvider` overriding an API host and three strings, and Pipecat detects a SignalWire call as `twilio` because that is genuinely what it is speaking.

Its trial also happens to be the right shape for development: no credit card, a phone number included, and one verified number you may call.

**The provider builds the serializer; Pipecat's dev runner is not allowed to.** This is the one place Phase 4 takes something back from the framework, and the reason is concrete. `pipecat.runner.utils._create_telephony_transport` does:

```python
params.serializer = TwilioFrameSerializer(
    ..., account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
         auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""))
```

and `TwilioFrameSerializer` **raises** when those are empty. Two consequences, both observed on this machine:

* On a SignalWire bot those variables are empty, so every call dies during setup. Filling them in with SignalWire values would still leave every hang-up authenticating against `api.twilio.com`.
* A bot with **no** carrier configured at all cannot answer a call either — which breaks `tests/fake_carrier.py`, the only free way to test any of this, and anybody's first run.

The serializer takes a `base_url` and an `auto_hang_up` switch, which between them are the whole fix. `TelephonyProvider.make_serializer` owns the configured case, and `transport._unauthenticated_serializer` covers the unconfigured one by turning auto hang-up off — the audio works both ways and the only loss is telling the carrier to hang up over REST, which matters little because closing the websocket ends the call anyway. `tests/test_telephony.py` asserts that Pipecat's default still raises; if that ever stops being true, the override can go.

**Only two carriers can place a call, but four can be received.** A Telnyx or Plivo provider written against no account would be untested code pretending to be a feature. The seam is real and demonstrated by SignalWire instead; `src/telephony/__init__.py` lists the four edits a third would need, none of them in `bot.py`.

**Echo suppression is off by default, even though it is what a laptop needs.** Live testing found the agent talking to itself: with no headphones its voice leaves the speakers, re-enters the microphone, is transcribed as the caller, and gets answered — and since each reply feeds the next, the conversation runs away with no human in it. `ECHO_SUPPRESSION=always` stops it dead by ignoring the caller while the agent speaks.

It is still off by default, because turning it on **disables barge-in** — a Phase 2 feature with an eval asserting it. A default that silently broke that would trade a loud, obvious problem for a quiet one, and headphones fix the echo without costing anything. So it is opt-in, printed on the startup line, and the first thing the README's troubleshooting section tells you to reach for.

The `greeting` middle setting exists because the opening turn is the worst case: it is the longest uninterrupted stretch of agent audio in the call, and it plays before the caller has said anything, so an echo there starts the loop before the conversation has begun.

**The bot watches for a browser that vanishes, because nothing else will.** WebRTC has no "the far end went away" signal. aiortc has no disconnected state — Pipecat's own source says so in a comment — and Pipecat only emits its `disconnected` event when a renegotiation explicitly restarts the connection. So a caller who closes cleanly is handled, and a caller whose laptop sleeps is not: the session runs on forever, and the dev runner keeps the dead connection registered under its `pc_id`. The next connect sends that same id, the runner *renegotiates* it rather than creating one, renegotiation starts no bot, and the page connects to silence. Reloading discards the id, which is exactly the reported "it only works again after a refresh".

`PeerWatchdog` polls the same predicate the transport itself trusts (`is_connected()` — a keep-alive ping within three seconds) and reports a disconnect through the normal path, so `ConnectionGuard` still gets its grace window for a caller who is coming back. `_release_webrtc` then closes the connection when the session ends, which is what makes the runner forget the `pc_id`. Both halves are needed: without the watchdog the session never ends, and without the release the runner never forgets.

**The carrier simulator is a repo tool, not a scratch file.** `tests/fake_carrier.py` is what made all of this verifiable on a machine with no carrier account, and the *next* person will have the same problem — either because they also cannot get an account, or because they do not want to spend a call to find out whether a change broke the greeting. It is deliberately not part of the eval suite: it needs a bot running on a known port and it proves plumbing rather than behaviour.

**Every eval scenario is audio mode.** Text mode bypasses the bot's STT, and on this project the STT is the turn-taking, so a text pass proves nothing about what Phase 2 changed. `smoke_text.yaml` is the deliberate exception: a ten-second wiring check needing no local models.

**Eval suite runs at `concurrency: 1`.** Kokoro, Moonshine and Silero all run locally on the CPU; running bots in parallel makes them contend and the latency numbers stop meaning anything.

**The judge is Groq via the harness's `factory` hook.** The harness natively supports only Ollama (not installed) and OpenAI (no key). Groq is free, fast, and its key is already required.

### Phase 5

**Campaign tables are unprefixed; knowledge base tables keep `kb_`.** `kb_` marks
a subsystem you can switch off entirely (`KB_ENABLED=false`); `prospects`,
`campaigns`, `campaign_prospects` and `call_attempts` are the application's own
state and there is no version of this product without them. No collision either
way — the database had exactly two tables before this phase.

**`DATABASE_URL` defaults to `KB_DATABASE_URL`.** So an existing setup needs no
new configuration and both live in one database, while staying separable. They
are separate settings because the knowledge base is optional and the campaign
tables are not.

**`phone_normalized` is UNIQUE and nullable, and that combination is the whole
duplicate story.** Postgres permits many NULLs under a UNIQUE constraint, so any
number of un-normalisable prospects coexist while every dialable number is
unique. Re-importing a list therefore cannot create a second row for somebody, and
a person written `+92 322 1234567` in one file and `0322 1234567` in another is
one prospect.

**Email is indexed but deliberately NOT unique.** Several contacts at one company
legitimately share an `info@` address, and a unique constraint would reject that
whole import. The importer *reports* duplicate emails instead, where a person can
judge them. Phone duplicates are hard errors because importing them means calling
one person twice.

**Call attempts survive their campaign** — `ON DELETE SET NULL`, not `CASCADE`.
The call history is the record of what was done to a person, and deleting a
campaign should not erase it.

**Retry policy is per-outcome, not per-failure.** `NO_ANSWER` and `BUSY` are
retried; `FAILED` is not, because it usually means the number is wrong and
retrying a wrong number only spends money.

**Campaign state is not written by `bot.py`.** The bot is a separate process the
*carrier* starts when a call is answered; it holds the conversation and knows
nothing about campaigns. Outcomes are reconciled by polling the carrier from
`dialer.refresh`, the same mechanism `call.py` uses. Webhooks are the upgrade
when this becomes a scheduler — the attempt row is already keyed by
`telephony_call_id` for exactly that.

**The dialer passes prospect, campaign and attempt ids into the call** as media-
stream parameters, using the mechanism Phase 4 built. Phase 6 reads them, in
`conversation/sources.py`, without the campaign tables becoming reachable from
`bot.py`.

### Phase 24

- **Mount, do not merge.** The dashboard and the automation API keep their
  own modules, their own tests and their own standalone servers; the
  unified app builds both with the existing factories and mounts them under
  prefixes. One origin, one cookie, and nothing in `src/dashboard` or
  `src/automation` had to know it is mounted — except the API learning to
  read the session cookie, which is the one place a browser and n8n meet.
- **A cookie on a JSON API needs a CSRF check, and a header is enough.**
  Browsers do not add `X-Requested-With` cross-site without a preflight
  the CORS policy refuses, so its presence proves the page made the call.
  Reads need no header. Bearer keys skip the check, as they cannot be sent
  by a browser form.
- **No framework, no build.** Three static files, a hash router, `fetch`.
  The pages are views over the existing JSON; the business logic — the
  importer's validation, the gate, the roles, the state machine — stays in
  the backend, which is where the brief said it must be. A build step
  would have been the first thing to break the local development flow.
- **The wizard writes only at the end.** Every step is held in memory
  until *Create*; then the draft, its configuration, its compliance, its
  contacts and its CSV are written in that order. Abandoning the wizard
  leaves nothing behind, and a CSV is previewed with the importer's own
  dry run so what the user sees rejected is exactly what the import would
  reject.
- **A campaign is created as `DRAFT` and a CSV never dials.** Starting is
  a separate, confirmed action, and the scheduler is a separate process;
  the dashboard warns when campaigns are running with no scheduler alive
  rather than starting one from a web request.
- **Stop stays an admin action.** Phase 18 made completing or cancelling a
  campaign `manage`; the page hides the button from operators rather than
  weakening the API.
- **Not supported is shown, not faked.** A per-campaign number, a
  per-campaign concurrency, a `FAILED` campaign state and a scheduled start
  would each have needed a backend the brief said not to build; the pages
  show what the system does and say so.

### Phase 23

- **One story, not twenty-five scripts.** The earlier scripts prove each
  phase in isolation over doubles; what none of them proved is that the
  rows one phase writes are the rows the next one reads. The suite runs the
  real store, service, gate, dialer, worker, conversation, tools, calendar,
  receiver, syncer, deliverer, dashboard and API over one schema, in the
  order a real campaign runs them, and asserts on what each left for the
  next. It reuses the earlier scripts' doubles for the outside world only.
- **The outside world stays outside.** The carrier, the CRM, the speech
  services and n8n are replaced; nothing else is. A suite that rang a phone
  or filed a HubSpot contact would be a suite nobody runs.
- **The verdict is red while anything critical is unverified.** `validate.py`
  exits 1 on the TTS credential and the readiness document says not ready
  on its first line. A green report that hid a bot that cannot speak would
  be the one lie the phase exists to prevent.
- **The real-phone test is one command, and it refuses to dial without two
  flags.** Preconditions are checked by code (health, the bot's readiness,
  the number's shape, the public URL); the call is placed by the existing
  `tests/live_call.py`, unchanged.
- **Hardening only where the suite found something.** Four fixes, each a
  behaviour a real customer would have met: a listed number counted as
  added, a CSV note the agent never saw, a health check naming the wrong
  vendor, a duplicated variable nobody could see. No refactors.
- **Measured means measured.** The suite's timings are labelled as the
  pipeline's own paths; `validate.py measure` reads real rows and says
  "none yet" where there are none; the latency table in the readiness
  document is the eval harness's and says so.

### Phase 22

- **A registry of our own, not `prometheus_client`.** The exposition format
  is a handful of lines per metric; a dependency would be one more thing to
  keep current for the sake of a formatter, and it would not have given
  exact percentiles in JSON, which is what an operator without a Prometheus
  reads. The rendering is checked against the format's rules.
- **Labels are a closed list, enforced at definition.** The one way a
  metric leaks a person is a label, and the one way a label leaks is a
  future `phone=` added in good faith. `LABEL_NAMES_ALLOWED` makes that a
  `MetricError` in a check rather than a series in a scrape; a campaign is
  its id, a route is its template, a worker is never a label (the process
  is the instance).
- **The trace is born at the dialer and lives on the row.** The earliest
  point every downstream hop can reach: the receiver has only a call id,
  the syncer a result, the deliverer an event — all of them find the
  attempt row, so the row is where the id has to be. The handshake copy is
  for the bot's *first* line, before any read. The alternative — the bot
  reading it from the row once the brief resolves — would cost a read on
  the greeting path and leave the first seconds' lines without it.
- **Counters per process; fleet figures from the rows.** What one process
  did (errors, latency, tokens) is that process's; what the deployment
  holds (queue, workers, throughput) is in PostgreSQL and is read into
  gauges by every server, so a single scrape target still sees the queue.
  No metrics table, no push gateway: the rows already are the durable
  record.
- **`/readyz` asks only what a session cannot do without.** The database.
  Seven vendor round trips every ten seconds is a probe somebody turns off
  and then forgets; `health.py` keeps the vendor checks for a person.
- **The scheduler binds its own socket.** uvicorn's bind failure ends the
  process, and a worker must never fail to *dial* because a scrape port was
  taken. So the socket is bound first, exclusively (Windows shares a port
  under `SO_REUSEADDR`), and handed over; a taken port is one log line.
- **The tools' public methods wrap private bodies.** `ActionService` is a
  Protocol the conversation calls by name; renaming the bodies and keeping
  the names is what lets one `_observed` count every tool without a line in
  each, and without the conversation layer importing monitoring at all.
- **Nothing on the call path gained a dependency.** `metrics.py` and
  `tracing.py` import nothing from the project; the package imports no
  campaigns, security, automation, CRM or conversation code — so the
  supervisor, the diagnostics observer, the reporter and the dialer record
  a number without acquiring anything, and Phases 15, 17 and 18's boundary
  checks stay true unchanged.
- **The probes are unauthenticated; the numbers may be gated.** A liveness
  probe holds no key, and `/healthz` and `/readyz` carry nothing but a role
  and a scrubbed reason. `/metrics` names campaign ids and error rates, so
  it takes a bearer once one is configured — and the default stays open
  because every server binds loopback by default and a token nobody set
  would only be a probe that fails.

### Phase 21

- **PostgreSQL, not Redis.** Every process already holds a pool to it; a second store is a second failure and a second truth. What the fleet shares fits an advisory lock, a row, a table and a column, and none of it sits on the call path.
- **The reservation is serialised, not merely counted.** Phase 11's count inside the transaction is not exact under READ COMMITTED; a transaction-level advisory lock is what makes "one below the limit" true at the moment of the insert. The lock is held for the length of one short transaction, and only reservations take it.
- **Pacing is taken, not checked.** A slot is claimed at placement time under a lock and written before the carrier is asked; "has the interval elapsed" is a question two processes can both answer yes to. The process-local limiter stays as the cheap pre-check.
- **Ownership lives on the attempt row, and a live owner is never overridden.** "Whose call is this" is a column, so the follow set survives a process and can be inspected. A claim takes only from the stale, the stopped and the unknown; a worker that finds another live worker on its row steps back rather than fighting.
- **Stale is judged by the database's clock.** `now()` in the query, not the worker's wall clock: hosts disagree by seconds, and a heartbeat's age must not.
- **A clean stop hands over; a crash is found by the beat.** Clearing ownership at `finish()` makes a restart or a rolling deploy pick calls up at once; the stale window is only for the process that never got to say goodbye.
- **A worker's id is per process, even when configured.** Two processes started with the same `WORKER_ID` would share a heartbeat row and each read the other's beat as its own; the suffix is what stops that.
- **Only the system's failures are retried.** A 503, a timeout, a worker that died are the deployment's problem; an invalid number, a blocked one, a do-not-call are the prospect's answer. The classifier is a short list of patterns and the default is "final", as Phase 5 decided.
- **The dashboard strip is unfiltered.** Whether anyone is running is a fact about the deployment, not about the campaign in view.
- **Nothing was added to the realtime pipeline.** The bot places no calls, follows no calls and beats no heartbeat; `test_scaling.py` asserts `bot.py` and `coordination.py` do not mention each other.

### Phase 20

- **Filters go inside the aggregate, never around it.** A narrowed view is the same single-pass `count(*) FILTER` scan with a `WHERE`, not a wider fetch trimmed in Python. Phase 11 measured the scan as the cost; a filter that read more rows to show fewer would spend it twice.
- **Still no indexes.** Failed §28 stands: these are full-table aggregates, and the new `WHERE campaign_id = $1 AND created_at >= $2` clauses want a subset only when the range is small — the write-cost regression measured then is the same. The cache, keyed by view, is what makes twenty viewers of one campaign cost one read.
- **Latency and errors come from a summary written at teardown, not from the transcripts.** The per-call figures live in `conversation_data` (the whole report, turns and all); aggregating them would scan every transcript on every refresh. The sink now writes the six numbers a dashboard wants beside the usage it already writes, in the same statement. History before this phase has none, and says so.
- **Every rate names its denominator, and a rate over nothing is unavailable.** A meeting rate over dials nobody answered describes the list, not the conversation; conversion is over answered calls, answer and voicemail rates over every call, and the footnote says which.
- **The transcript is gated where the API gates it.** `read_pii`, audited per read, exactly as `GET /api/v1/results/{id}`; a viewer's detail page says the transcript is withheld rather than showing an empty box. One rule for both surfaces.
- **A number search is a `read_pii` act.** Searching by digits answers "is this number on file", which is the fact a viewer may not have. Names, companies, emails and call ids search for everybody.
- **The dashboard serializes its own rows.** `src/dashboard/` importing `src/automation/serialize.py` would put the API package on the reporting path, which `test_automation.py`'s boundary check forbids for a reason: the API package must be importable by nothing a call runs. A forty-line `_plain` walk is cheaper than the coupling.
- **The view is in the URL.** A filtered dashboard is a link somebody can send and a bookmark somebody can keep; the page writes its state to the query string and reads it back on load.
- **One page, extended, not two.** The filter bar, the strips and the calls list are the same document and the same renderer; the call detail is the one new document, because one call is a different question from a hundred.

### Phase 19

- **The list is by number and outlives the prospect row.** The status on the person (Phase 5) closes their memberships the moment they ask; the list survives a re-import under a new row, a second row with the same number, and a caller who was never a prospect. Both are enforced, everywhere, because each covers what the other cannot.
- **Nothing is removed from the list; a removal is a stamp.** "When was this number blocked, by whom, and who took it off" is the question an operator has to answer, and a DELETE cannot answer it.
- **The jurisdiction layer is applied last.** A campaign may narrow a country's window or ceiling and may not loosen it. The region comes from the number's country code — the one thing a number does determine — and never becomes a timezone (Phase 9's rule, kept, for the reason it gave).
- **No law is encoded, and the documentation says so first.** `COMPLIANCE_JURISDICTIONS` is a JSON object the operator writes; the examples are labelled as shapes. A "US preset" shipped in code would be wrong somewhere, out of date soon, and read by somebody as a promise.
- **One gate, verdicts, and the consequence each deserves.** A do-not-call closes the attempt as `DO_NOT_CALL` (a disposition, not a `FAILED` with a sentence in it); a closed window or an undue retry gives the reservation back unspent (Phase 13's path); a reached ceiling closes the membership; anything else releases (Phase 9's path). The gate decides; the dialer acts; the service writes.
- **`OPTED_OUT` is told; `DO_NOT_CALL` is known.** A report that cannot tell "they asked us on this call" from "we already knew and did not dial" cannot answer the first question a compliance review asks. The change is documented for consumers, and `next_action` is `DO_NOT_CONTACT` for both.
- **The disclosure is words on the brief, not policy in the conversation layer.** `src/conversation/` gets the sentences and an instruction to say them first; it does not know why. That keeps the conversation layer importing nothing from `src/compliance/`, and it keeps the instruction testable without a policy.
- **The instruction is recorded; the utterance is not verified.** The audit row says what the opening was told to include. Whether a model said it is a transcript and an eval question, and claiming otherwise would be claiming something the software cannot see.
- **Recording is a flag for a disclosure, not a feature.** This software does not record audio; the operator's carrier or proxy may. The flag exists so a required sentence can be required.
- **A database without the table keeps dialling on the status, with a warning.** The alternative — every reservation failing until `init` is run — would stop a campaign that was safe yesterday on a table it did not have yesterday. The status is what it was; the list is added protection.
- **A DNC prospect never joins a campaign, however many times they are imported.** The queue would refuse them anyway; not opening the membership keeps the counts honest and the outlook true.
- **The gate exhausts a jurisdiction's ceiling after reserving, rather than teaching the SQL a per-number figure.** One reservation and no dial, audited, per such prospect. A per-number ceiling in the reservation would need the region on the prospect row, which is a Phase for when somebody has a list that needs it.

### Phase 18

- **Roles are sets of permissions, and routes ask for a permission.** `need(Permission.WRITE)`, never `if role == "admin"`. A fourth role later (a supervisor who may read transcripts but not dial) is one line in `ROLE_PERMISSIONS` and no change to any route.
- **Masking is one walk over the JSON, keyed by field name, in a route class.** The alternative — remembering to mask in each of twenty handlers — is the failure mode a per-route approach invites; a new serializer field called `phone` is masked without anybody adding a call. Names are *not* masked: the dashboard always showed them, and the number is what reaches a person.
- **A viewer asking for the part they may not have gets 403, not a silently trimmed answer.** `include=transcript` from a viewer key is refused and audited rather than quietly dropped, so an integration learns at once that it holds the wrong key instead of shipping empty transcripts for a month.
- **The anonymous budget is spent before a refusal is audited.** Otherwise a script guessing keys writes a row per guess, and the audit table becomes the thing it was attacking.
- **scrypt from the standard library, not bcrypt or argon2.** Both are installed here, but only as somebody else's transitive dependency, and a login that stops working when an unrelated package drops its dependency is the wrong surprise. scrypt is memory-hard and on every platform this project runs on.
- **Stateless signed sessions, with the role inside the token.** No session table, nothing to share between two dashboard processes but the secret; the trade is that a role change takes effect at the next login, and revocation is "rotate the secret". Documented; a revocation list is a later phase if anyone needs it.
- **Users in the environment, not in a table.** `DASHBOARD_USERS` lives where every other secret does, so a deployment has one place to manage credentials and this repository can never commit one. Hashes are not passwords, but a hash in a log is a hash somebody can crack offline, so the directory is scrubbed too.
- **The dashboard refuses to start with nobody configured, and the opt-out is loopback-only.** A page that silently served without a login because somebody forgot a variable would be Phase 10 with a false sense of Phase 18. `DASHBOARD_AUTH_DISABLED=true` exists for local work and `dashboard.py` will not bind it to a network interface.
- **HTTPS is enforced but not terminated.** `SECURITY_REQUIRE_HTTPS` makes every server refuse plain HTTP; TLS itself is the proxy's job, and `X-Forwarded-Proto` is believed only from `SECURITY_TRUSTED_PROXIES` — a forged header would otherwise be the whole check. A dashboard page is redirected; an API call is refused, because a redirect would make a client replay its POST over the wire it just used.
- **CORS is off unless origins are listed, and a wildcard is a configuration error.** The browser's own default is the secure one; the setting exists for a front end somebody builds later.
- **An audit failure logs loudly and lets the action through, by default.** A phone that cannot ring because the audit table is missing is a worse outage than an unrecorded row — the log line is still written. `SECURITY_AUDIT_STRICT=true` inverts that for a deployment where "not recorded" must mean "did not happen".
- **The carrier webhook's signature check is untouched; the receiver counts refusals.** Phase 14's verification was already right. What was missing was a bound on how many forged deliveries an address may have verified per minute; a genuine carrier is never refused, so the limit only ever slows a prober.
- **The bot's own web server is left alone.** Adding middleware to Pipecat's runner app would sit in front of the browser client and the carrier's websocket, in the audio path's process. The webhook router mounted there gets the refusal limiter (a router-level thing); the rest is documented as a development surface.
- **`.env` untracked, not rewritten.** `git rm --cached` and a `.gitignore` are staged; a history rewrite of somebody's repository, and the key rotation that a pushed history would demand, are the user's calls and are written up in [Pending tasks](#4-pending-tasks).

### Phase 17

**A call request is a row, and the scheduler places it.** The obvious
design — `POST /calls` reserves and dials, the way `campaign.py call` does —
would put a carrier round trip inside an HTTP handler and would make the
API a second dialer with its own view of the calling window, the
concurrency limit and the pacing, none of which is shared across processes
(Phase 9 left them in-process on purpose). Writing the pending-callback row
the agent itself writes when a prospect asks to be phoned back costs
nothing new: the worker already places due callbacks ahead of the queue,
reopens a closed membership for them, and applies every rule. It also
answers the user's constraint exactly: n8n is asynchronous by construction,
because the only thing it can do is leave a row. The price is that 202
means "queued" and never "ringing", and a caller has to be told so — hence
`dialled_by` and `warnings` in every answer.

**Idempotency lives in the rows first and in the ledger second.** The
natural keys Phases 5–7 built (a number, a name, a membership pair, one
pending callback per prospect) are what make a repeated *write* harmless;
the `Idempotency-Key` ledger is what makes a repeated *answer* the same.
Only the second needed building. A ledger without the natural keys would
protect a client retrying its own request and nothing else — two workflows
asking for the same call would still queue it twice — which is Phase 9's
"derived, not generated" lesson applied to HTTP.

**Events are created from rows, in SQL, under unique keys.** The
alternative — the bot or the worker publishing an event when something
happens — would put a second write on the call path and would need the
event to exist before the fact was durable. Reading the rows later, from
a separate process, with `INSERT … SELECT … ON CONFLICT DO NOTHING`, means
a fact becomes one event however many deliverers run, a deliverer that
starts a week late catches up, and nothing on the call path knows an
outbox exists. It is Phase 15's `claim_results_for_sync` generalised to
six statements.

**The settle window is the answer to the race Phase 8 documented.** The
carrier's result and the conversation's result land seconds apart and the
upsert's precedence rule decides which stands; an event created at the
first write would carry the thin one. Thirty seconds of quiet is cheap and
makes `call.completed` the rich result nearly always; `call.updated`
covers the rest honestly rather than pretending the first delivery was
final.

**The payload is built at delivery, not at creation.** An event that waited
(the receiver was down for an hour) should carry the rows as they are,
not as they were; and building at delivery keeps the creation statements
pure SQL over ids. The payload *sent* is then stored on the row, so an
operator can read exactly what n8n got.

**`Retry-After` is a floor applied after the jitter.** The receiver said
"not before"; a jitter that landed under it would be a request it asked
not to get. The CRM syncer jitters after taking the max, which can undercut
the floor by ten percent — a small thing, noted, not changed there.

**A manual retry resets the attempt count.** A person reopening a row has
fixed something; one try left against the new state of the world is not
what they meant. (Phase 15's `retry_crm_sync` keeps the count, and a
reopened row that fails transiently once is closed again as "after N
attempts" — a latent quirk, noted here, not changed.)

**404 is transient.** n8n answers 404 for a workflow that exists but is not
active, and for the Test URL once the test window closes. Closing the
event as failed would lose it for the two minutes somebody takes to click
Activate; retrying for the full budget (six hours by default) is the
useful behaviour, and the reason is in `last_error`.

**The API takes its own settings object.** `ApiSettings` carries the seven
things the API needs; `create_automation_app` accepts it or a full
`Config`. The checks build one by hand, so the API is tested on a machine
with no vendor keys — the same reason `AutomationConfig.from_env` takes a
`problems` list like every other section.

**Lenient ISO in query strings.** A `+05:00` offset becomes ` 05:00` when
the client does not URL-encode the plus, and n8n expressions rarely do.
Reading a single space before a `HH:MM` tail as the plus it was costs one
line and removes a support question.

**Loopback and a mandatory key.** Every other server in this project binds
to loopback without authentication because it only reads. This one writes
and can make a phone ring, so it has both: a key it refuses to run without,
and loopback until told otherwise, with a warning that names TLS.

### Phase 16

**Hardening, not rebuilding.** The brief said not to recreate the actions,
and the Phase 7 seams turned out to be the right ones: every change is
behind `CalendarProvider`, `TelephonyProvider` or the action service, and
`src/conversation/` did not change by a line. The way to check that this is
true, rather than merely intended, is `test_actions.py` passing unchanged.

**A lost booking answer is looked up, never repeated.** Cal.com takes no
idempotency key, so a second POST after a timeout could book twice — the
same rule Phase 9 applies to placing a call, and Phase 15 to filing one. The
provider lists the attendee's bookings around the requested start and adopts
what it finds; a definite miss is reported as unavailable *with the words
"nothing was booked"*, because the caller is about to be told something and
it must be true either way.

**The double-booking guard is the write.** For the local calendar the check
before the write stays — it keeps the *offer* honest — but the exclusion
constraint is what keeps the *diary* honest, and it needs no lock and no
care at the call site. Five simultaneous bookings of one slot produce one
row. Cal.com's diary is Cal.com's, so its mirror rows are outside the rule;
a constraint that spanned both would refuse a Cal.com booking that Cal.com
had already accepted.

**The constraint is added under a savepoint.** `create_schema` is one
transaction, and an existing database whose local bookings already overlap
cannot take the constraint. Failing `init` for that would stop every other
table being brought up; a warning naming the fix does not.

**The transfer's outcome comes from the carrier, through the receiver.**
The bot has left the call the moment the carrier applies the TwiML, so
nothing in the bot can know whether the colleague answered. `<Dial action>`
is the carrier's own way of saying, and it lands on the Phase 14 route with
the same signature check as every other event. The fallback moves from the
TwiML into the receiver's answer, because with an `action` the verbs after
`<Dial>` are never reached — a fact worth stating, since the obvious
"add the attribute and keep the fallback" leaves the caller in silence.

**Every answer to a transfer report is TwiML, a duplicate included.** A
plain-text `duplicate` would be handed to a carrier that acts on the body,
and the caller would be dropped. The receipt carries the body and the media
type; every other delivery still answers with the outcome word.

**The `REQUESTED` row is guarded and bounded.** It is written inside the
tool turn, after the carrier has already taken the call, so it must neither
fail the transfer nor hold the person: five seconds, then a log line.

**The transfer outcome is its own table, not a result column.** The result
is composed by the conversation at call end and its shape is Phase 8's
contract; the outcome arrives later, from another process, keyed by the
call id. A row that joins on the attempt says everything a result column
would, without changing the result.

### Phase 15

**A poller over the rows, not a push from the sink.** The sink runs in the
bot at the end of a call; a CRM request there would tie the call's teardown
to somebody else's uptime, and a CRM outage would cost every call that ended
during it. A separate process that claims unsynced rows survives the outage,
retries on its own schedule, and — the point the requirement makes — cannot
touch a turn. It is also the shape the handoff recommended when Phase 8 left
the integration for later.

**One `crm_sync` row per result, and the result's `updated_at` is the
version.** A `SYNCED` row is finished until the result it filed changes, and
the store already bumps `call_results.updated_at` on every rewrite (the
carrier's thin result replaced by the conversation's), so "has the CRM seen
the latest" is one comparison in the claim. The alternative — resending every
result on a schedule — would rewrite thousands of activities to change one.

**Writes are made once; a lost answer is searched for, not repeated.**
Neither HubSpot nor its peers take an idempotency key on create, so the same
rule as `place_call` applies: `WRITE_POLICY` is one attempt, a timeout is
ambiguous, and the next pass asks the CRM whether the activity exists (the
key is in its body) before creating. The one-minute default retry interval is
what covers HubSpot's documented index lag.

**Ids are recorded the moment they are known.** The contact's id is written
before the activity is attempted and the activity's before the contact is
updated, so a crash at any point resumes at that point. The three steps are
in that order because the last one — the contact's own summary of the latest
call — is the one whose repetition is harmless.

**A rejected token stops the pass; a rejected record fails the record.** The
first is not a fact about any row, and marking a whole batch failed for one
expired key would be wrong in the expensive direction (a person reopening
them one by one). The rows are handed back with the reason and no attempt
spent. The second is the CRM saying this record cannot be filed as it is,
which retrying would only repeat.

**Unanswered calls are filed by default.** A CRM's call log is the record of
every attempt, and "tried Tuesday, no answer" is what a rep wants to see
before dialling again. `CRM_SYNC_UNANSWERED=false` for a portal that wants
conversations only; a skipped row says so rather than vanishing.

**The structured facts go on the contact as `ai_*` custom properties, and on
nothing standard.** Writing `hs_lead_status` or `lifecyclestage` would be an
opinion about somebody's pipeline; a property prefixed with the integration's
name is clearly its own, and a portal that will not grant the schema scope
still gets the whole account in the activity body.

**Phone matching strips the trunk zero.** HubSpot searches a phone on its
area code and local number, so the candidates are compared by digits, and a
contact somebody typed as `0300 1234567` is the same person as the
`+923001234567` the campaign dialled. Nine digits in common are required, so
an area code alone never matches.

### Phase 14

**Signatures are checked against the configured public URL, never the
request's own.** The carrier signs the URL it was given — `https://<tunnel>/…`
— and behind a tunnel or a proxy the local server sees
`http://localhost:7860/…`. The sender and the receiver both read
`TelephonyConfig.webhook_url()`, so the two cannot disagree; the test that
signs over the server's own URL and is refused is the one that pins this.

**A carrier that cannot be verified is asked for no events at all.**
SignalWire without `SIGNALWIRE_SIGNING_KEY` could only produce deliveries the
receiver would refuse — a `webhook.refused` warning per event, and nothing
gained. So `webhook_url()` is None in that state, the call is placed exactly
as in Phase 13, and the startup line says why. The alternative — accepting
unsigned events from a carrier that cannot sign — was not considered: an
unverified `completed` frees a prospect who may still be on the phone.

**The API token is not the SignalWire signing key, and is not accepted as
one.** SignalWire's documentation and its SDK say the signing key is a
separate value from the dashboard's API credentials page; the SDK's
`RequestValidator` wraps Twilio's with that key. Falling back to the API
token would have made a misconfiguration look like a working receiver that
refuses everything.

**A ledger table, and why it is not the "table for its own state" Phase 13
refused.** Phase 13 declined a worker-state table because the rows already
held every fact. The ledger holds a fact nothing else does: *this delivery
arrived, with this key, and was handled this way*. Its unique key is what
makes a redelivery a database no-op before any row is read, and its outcome
column is what makes "did the carrier tell us" a query rather than a grep.
The attempt row is still the call's state; the ledger only says how it got
there. A database that predates it still works, with the monotonic write as
the only protection and one warning naming `campaign.py init`.

**The default receiver is the bot's own web server.** One tunnel, one public
address, and `/ws` already has to be on it; a second process would need a
proxy in front of the tunnel that nobody in development has. The handler is
one HMAC and a few awaited statements — measured at 0–1 ms of latency per
event in the checks — and touches nothing a session owns. The standalone
receiver exists for the deployment that does have a proxy, and it is the
same router over the same processor, so choosing between them changes
nothing about what an event does.

**Polling is kept, at two cadences.** A receiver that is down must cost
nothing but the old request rate, so a call the carrier has said nothing
about is polled every tick as before. Once one event has arrived for a call,
the carrier is asked only every `WORKER_WEBHOOK_POLL_SECS` — the safety net
for the event that never comes. The tick still runs at `WORKER_POLL_SECS`
reading the row, because that is how a pushed ending frees its slot; the
saving is in carrier requests, not in wake-ups.

**`unmatched` is 200, not 404.** A delivery for a call this database never
placed — `call.py`, an inbound call, another number on the account — is not
an error the carrier can do anything about, and a 4xx would make it retry or
log a failure against a working receiver. It is recorded on the ledger and
answered as received.

**503 when the database is gone; 403 and 400 only for the request's own
faults.** A carrier that retries on 5xx will redeliver, and the ledger makes
that safe; a carrier that does not is covered by the worker's poll. A forged
or malformed delivery is the sender's problem and is told so.

**The machine verdict is read back from the ledger, not carried on the
completion.** Twilio's asynchronous AMD delivers `AnsweredBy` on its own
event; the `completed` event that follows does not repeat it. Phase 12's rule
— a completed call a machine answered is a voicemail — is kept by having the
completion ask the ledger for the latest verdict, which is exactly what a
poll saw on the call resource. A verdict that arrives *after* the completion
is logged and not applied: relabelling a final row is the thing the monotonic
rule exists to prevent.

**`initiated` on a freshly placed call is `stale`, and that is fine.** The
row is already `QUEUED` from placement, and the same status twice is not an
advance. The outcome word says "nothing new" rather than "wrong", and the
ledger still records that the carrier sent it.

**No new environment for Twilio.** It signs with the auth token the account
already needs to dial, so a Twilio deployment gets webhooks by setting
nothing — the URL is derived from `TELEPHONY_PUBLIC_URL`, which a call cannot
be placed without anyway.

### Phase 13

**The worker decides only *when to ask*.** Every rule about whether a call may
be placed already existed, in SQL, in the guards, in the dialer, in recovery;
the phase's discipline was to add a loop and nothing that competes with them.
Where the loop needed a question answered (is this campaign finished? may this
one membership be reserved?) the answer was added to the store as a query, not
to the worker as a heuristic.

**A separate process, and a check that keeps it one.** The requirement that
the scheduler never adds latency to STT → LLM → TTS is met by structure rather
than by care: `bot.py` does not know the worker exists, and `test_worker.py`
asserts that `worker.py` imports nothing outside `src/campaigns/` and
`src/reliability/`.

**A callback overrides `CAMPAIGN_MAX_ATTEMPTS`** — the decision Phase 7 left
open. The cap exists to stop pestering people who do not answer; a person who
asked to be phoned back is the opposite case. Bounded: one honest try per
callback, waived through `ignore_attempt_limit` on that one reservation and
nowhere else; if nobody answers, the campaign's own retry policy applies. A
refused or ambiguous placement withdraws the callback with the reason in the
log rather than retrying it every tick — an ambiguous one may have rung the
phone, and a second attempt to keep the promise would be the duplicate call
the system exists to prevent. `callbacks_override_attempt_limit=False` on the
worker turns the override off; it is not an env setting, because nothing yet
wants it off.

**Due callbacks go ahead of the queue**, through a targeted reservation under
the queue's own statement. The alternative — letting the reopened membership
be picked up by `next_call` — fails on ordering: the queue puts never-called
rows (`next_attempt_at IS NULL`) first, so a promise for ten would wait behind
the whole list.

**A closed window defers; a refusal releases.** `release` (Phase 5) records a
failed attempt, which is right when the carrier said no and wrong when the
only thing wrong is the hour: a scheduler that released would burn a prospect's
three attempts in three ticks before their morning. `unreserve_attempt` deletes
a row that never represented a dial — the one exception to Phase 9's "the
attempt is recorded, not deleted" — and refuses once `placement_started_at` is
set, which is the point at which a dial may have happened. Recovery uses the
same path for a reservation a dead process never dialled, instead of closing it
as failed and exhausting a prospect nobody phoned.

**The configured window is checked first, in the configured zone; the
prospect's own zone after.** Unchanged from Phase 9, and now a documented
limitation: a prospect's zone can narrow the window, never widen it. Making the
queue hours-aware in SQL would change the reservation statement for every
caller; not this phase.

**A campaign completes itself only when it can never do anything again** —
nothing due, nothing scheduled, nothing live, no pending callback, and at
least one membership. Memberships the queue would never hand out are closed
first with the status that says why. `WORKER_AUTO_COMPLETE=false` leaves that
to a person.

**A run over named campaigns ends when they do; a run over every campaign
waits.** `campaign.py run "Q1"` returns when Q1 is finished or paused, which
is what a person running it expects; plain `campaign.py run` idles, because an
empty list of active campaigns means "none started yet", not "done".

**Counters, not a table.** `WorkerMetrics` is in-process and logged every
`WORKER_REPORT_SECS`; the durable record is `call_attempts`. A table for worker
state would be a second source of truth for facts the rows already hold.

**`record_outcome` acts only on final statuses.** Found rather than decided
(Failed §33), but worth stating as a rule: a live status changes the attempt,
and the membership stays `IN_PROGRESS` until the call is over. A membership
something else closed mid-call (a do-not-call from the command line) is left
as closed; an outcome does not relabel a membership the campaign is finished
with.

### Phase 12

**Measure through the phone path, not the eval transport.** The eval
harness sends clean 16 kHz audio only once the bot says it is ready, which is
why three phases of evals never saw the audio backlog. `tests/phone_drill.py`
speaks over the carrier's own wire protocol from the moment the handshake
completes, as a carrier does, and reads the bot's report rather than a
judge's opinion. That is the tool that found the phase's real defect, and it
needs no account, which is why it is the primary check and `live_call.py` the
confirmation.

**Drop the backlog rather than only start the pipeline sooner.** Both were
done, but the drain is the one that matters: however fast setup gets, a
carrier streams from the answer and the STT must not be handed seconds of
stale silence. It is read and discarded in `bot.py` right before
`runner.run()`, stopping at the first 100 ms window that arrives at real-time
rate, with a 3 s cap. Rejected alternative: teaching the transport to skip
old frames — the frames carry no timestamps, and the transport is Pipecat's.

**A voicemail is a call the bot held, not a call nobody answered.** The
`VOICEMAIL` result keeps the transcript, the notes and who ended the call,
because a reviewer will want to see the greeting the detector fired on, and
forces every qualification field to unknown, because a recording cannot have
needs. The validator enforces both; a model that recorded a pain point from a
greeting is overruled and the record says so in `issues`.

**The length rule requires overlap with the agent.** A machine starts its
greeting the moment the call connects and talks through whatever the agent
says; a person waits for the agent to finish. So only a first turn that began
over the agent's audio, or before the agent had spoken, can be judged by
length. The first version did not have this and hung up on a fast human
answer (Failed §32). The phrase rule has no such condition.

**Hang up is the default; leaving a message is opt-in.** A message needs the
beep to have passed and the beep is not detected, so `message` mode waits a
configurable delay and hopes. Cheap, honest, and off by default.

**The carrier's detection is read by polling, like everything else about a
call.** `answered_by` sits on the call resource; the bot reads it a few times
after connect and the dialer reads it on refresh. No webhook, which is the
Phase 4 decision restated. `async` rather than `sync` is what the
documentation recommends, because `sync` makes every person who answers wait
through the detection in silence.

**The resume after a spurious interruption is an instruction, not a replay.**
Replaying the truncated audio would need the TTS to have kept it, and it did
not; asking the model to continue costs one inference and produces a sentence
that fits. Capped per call so a line full of noise cannot loop.

**The per-response latency line became `k=v`.** The old prose line could not
be grepped for one stage across a run and did not survive `LOG_FORMAT=json`
as fields. The five numbers are unchanged; two are renamed to say what they
measure (`llm_first_token_ms`, `tts_first_audio_ms`).

**The monitor de-duplicates broadcast siblings, not only frame ids.** A
broadcast is two frames with different ids that name each other; the first
report counted every turn, every barge-in and every bot turn twice. Guards on
the state transitions catch the same thing a second way.

**`turn.failed` fires at ten seconds even when the reply is coming.** The
caller waited ten seconds in silence, which is the fact worth a warning;
`turn.late_response` says the reply came, and the report keeps `late` apart
from `failed`. A throttled tier makes the line noisy, and that is the tier's
fault to fix.

### Phase 11

**Measure, then change, then measure again — and keep the negative results.**
Six indexes that looked obviously right made every query slower, and that is
now written down so nobody adds them again. The first benchmark run was
cold-cache and reported numbers 2–5× the warm ones, which nearly justified
optimising the wrong thing; the benchmark takes medians of warmed queries for
that reason.

**Fewer repetitions, not cheaper scans.** A full-table aggregate has no subset
to seek to, so the cost of the dashboard was never going to come down per
query. It came down by running the eight queries at once and by not running
them again for five seconds.

**The cache is short and locked.** Five seconds is well under the page's
15-second refresh, so one viewer never sees a figure older than they expect,
while N viewers cost what one does. The lock matters as much as the TTL: without
it, N requests arriving on a cold cache all start their own read, which is the
stampede that makes a slow query slowest exactly when it is busiest.

**A borrowed pool is used, never closed.** The same `owns_*` pattern
`TwilioProvider` already used for its HTTP session. `Config.shares_database`
decides, so two genuinely separate databases still get two pools.

**The concurrency limit moved inside the transaction.** Checking before
reserving is cheaper and gives a reason, so both are kept — but only the count
inside the reservation's own transaction can hold when two workers check at the
same instant. Advisory limits are fine for rate; this one is about not dialling
more people at once than intended.

**Usage lives on the attempt, not on the call result.** Phase 8's `CallResult`
is the CRM-facing reading of a call; tokens and cost are operational. A CRM
wants to know the prospect was qualified, an operator wants to know the call
spent 6,000 tokens.

**Prices are configuration, units are measurement.** The default stack is three
free tiers where the honest per-call cost is "nothing until the tier runs out",
so an unset rate produces no number rather than a zero. And a stage nobody
reported is named rather than priced at zero — the third phase in a row where
"absent is not zero" turned out to be the load-bearing rule.

**The pipeline was not touched.** The instruction said not to optimise by
swapping providers, and the measurement agreed: turn detection is 653 ms of a
~1,300 ms turn and is Deepgram's own tuned default, where the failure mode is
cutting people off mid-sentence.

### Phase 10

**A separate process, not a route on the bot's runner.** Mounting the dashboard
on Pipecat's dev runner would have been fewer lines and is the obvious move.
Rejected: that process answers phone calls, and a reporting page that refreshes
every fifteen seconds has no business sharing it. Every other tool here —
`campaign.py`, `call.py`, `health.py`, `ingest.py` — already reaches the system
through PostgreSQL alone, and the dashboard is one more reader.

**Aggregates on `CampaignStore` rather than a new query module.** They need the
connection pool and the table-name constants, both of which are the store's, and
splitting reporting SQL into a second module would mean either exposing the pool
or duplicating the constants. `campaign_counts` already lived there, so the
precedent was set.

**Counting in SQL, wording in Python.** The arithmetic belongs next to the
tables it reads and the phrasing belongs next to the page that shows it. It also
means the numbers stay correct when the history is large: `stats.py` never
fetches a row.

**Scalar subqueries for the per-campaign table, not joins.** Joining memberships
*and* attempts to campaigns multiplies them — a campaign with 3 memberships and
4 attempts reports 12 of each — and the usual fix, `count(DISTINCT ...)` on
every column, is slower and easy to forget on the next column somebody adds. A
check pins this.

**Rendering in the browser from the JSON, not on the server.** The JSON endpoint
has to exist regardless — it is the reusable half — so rendering the page from
it means one renderer. Server-rendering the same numbers as well would be two
descriptions of one dataset to keep in step, which is the duplication this phase
was told to avoid. The cost is that the page needs JavaScript, and `<noscript>`
says what to read instead.

**Everything inline: no build step, no framework, no CDN.** `uv sync` is the
only install step this project has, and a dashboard that needs a CDN round trip
before it can draw fails in exactly the situation you opened it for.

**`available: false` rather than `0` for a missing table.** A zero next to
"Qualified prospects" is a claim about the pipeline; an absence is not. The same
distinction Phase 6 made with `UNKNOWN` enum members and Phase 8 made with
`issues` on the call result.

**Both halves of the answered/completed overlap are shown.** Reporting only
"answered" overstates clean endings; only "completed" undercounts reached
people. Showing both and explaining the overlap in `detail` is the only honest
option, and the explanation travels in the JSON rather than living in the page.

**Times in the campaign timezone, formatted on the server.** The viewer's local
zone would make the dashboard and `campaign.py` two answers to one question, and
the campaign zone is the one the agent used when it told a prospect a time.

**Escaping every value in the page.** Prospect names and company names come from
somebody else's CSV and carrier messages come from a vendor. A check renders a
hostile name through the real render functions and asserts it comes out escaped.

### Phase 9

**Ambiguity is a first-class outcome, not a kind of failure.** Every retry
helper collapses "timed out" into "failed", because from the caller's side they
look identical. For `place_call` they are not: a failure means no phone rang and
an ambiguity means one might be ringing now. `Verdict.AMBIGUOUS` and
`AmbiguousOutcomeError` exist so the distinction cannot be lost by accident —
the wrapper type means a caller that treats it as a plain failure has to do so
deliberately.

**An ambiguous attempt stays live rather than being released.** The instinct is
to free the prospect so the campaign can carry on. That is the wrong direction:
a live attempt costs one uncalled person, and releasing one costs a second call
to somebody whose phone may already be ringing. `UNRESOLVED` is in the live set
for that reason, which also means it counts against the concurrency limit and
appears in `health.py` as degraded — both of which are how somebody finds out
they need to run `recover`.

**The idempotency key is derived, not generated.** A random key handed out by a
client protects that client's own retry. It does nothing about two *different*
callers deciding to do the same thing — a worker and a restarted worker, say.
Deriving the key from what the call is (campaign, membership, attempt number)
means they compute the same string without having communicated. It is also
readable, so a duplicate in a log says which call it was a duplicate of.

**The attempt row is the idempotency record.** No separate table and no lease
with an expiry. The row already exists, already carries the unique carrier call
id, and its own live status *is* the lease — which `recovery.py` breaks when the
holder is gone. A lease with a timeout would need a clock the database and every
worker agreed on.

**Recovery never dials, and an unresolvable attempt is closed rather than
retried.** Recovery's job is to find out what happened, not to make something
happen. Deciding to try somebody again is the campaign's retry policy, which has
the attempt count and the retry window; a recovery pass that redialled would
bypass both.

**Monotonic status transitions instead of a special case per writer.** Phase 6
stopped `dialer.refresh` flattening a do-not-call into `COMPLETED` with three
lines in `refresh`. That worked for `refresh` and for nothing else. Making it a
property of the write — `may_advance`, applied inside a `FOR UPDATE` — covers
every writer that will ever exist, including a webhook nobody has written yet.
Those three lines are gone.

**Guardrails are checked before reserving, not after.** Reserving and then
releasing works, but it spends one of the membership's three attempts, and "the
calling window was closed" must not cost a prospect a try.

**Calling hours use the prospect's timezone only when their record supplies
one.** Deriving it from the phone number would be right most of the time and
silently wrong for every country with more than one zone. The importer already
keeps unrecognised CSV columns, so a list *can* carry a timezone; nothing
guesses one.

**Consecutive failures, not total.** Three failures across a twenty-minute call
are three blips; three in a row are an outage. A success from the same stage
resets the count — which is exactly the subtlety that produced Failed §25.

**The supervisor ends the session rather than hanging the call up.** Ending
through `stop_when_done` means the ordinary teardown runs, so the conversation
record and the call result are written exactly as for any other ending. A
supervised ending is a *recorded* ending, which is the difference between an
attempt that reads `FAILED` with a reason and one that stays live and has to be
recovered.

**The LLM's HTTP timeout is set below the supervisor's stall threshold.** Two
layers, deliberately: the network layer abandons the request first, so the
supervisor's much blunter response — ending the call — is only reached when
that did not work. It is applied through the OpenAI SDK's public
`with_options`, guarded, because Pipecat's `create_client` does not pass kwargs
to the client (verified in the installed source) and the SDK's default wait is
ten minutes.

**Health checks read; they never exercise.** Listing models, listing voices,
reading the account. A check that placed a call would cost money and ring a
phone, and one that ran an inference would cost tokens on a tier that is already
the bottleneck. What it therefore *cannot* prove — that a call would sound right
— is the eval suite's job, and the docstring says so.

**Checking the model against the provider's catalogue.** One extra request, and
it catches the Groq-catalogue-churn failure this project has already met, which
otherwise appears as a bot that answers the phone and says nothing.

**Secrets are scrubbed by a patcher, not by care at the call site.** This
project already logs key tails rather than keys. The failure being guarded
against is a vendor SDK putting an Authorization header into an exception
message, which no amount of discipline at the call site prevents.

**`src/reliability/` imports nothing from `src/campaigns/`.** Keeping it a
strictly lower layer is what stops the two becoming circular, and it is why
recovery lives in `campaigns/` — the same rule `dialer.py` and `briefing.py`
already follow: a module that joins two worlds belongs on the side that owns the
rows.

### Phase 8

**A table with typed columns, not another JSON blob.** Phase 6 rejected columns
for the qualification record because its shape was still moving. Phase 8's
whole point is a stable, CRM-mappable shape that can be filtered on — `WHERE
disposition = 'MEETING_BOOKED'` — so the closed-vocabulary fields are columns,
the lists and the transcript are `jsonb`, and `schema_version` is on the row.
`conversation_data` stays as the raw record, so the two do not compete: one is
what happened, the other is what it means.

**The result is a projection, and it says so.** Everything in a conversation
result is derived from `conversation_data` and the attempt row, and
`rebuild-results` proves it. That is what makes changing the shape safe.

**Two sources, `source` on the row, precedence in the statement.** The
alternative — the dialer checking whether a result exists before writing —
races the bot's teardown. `ON CONFLICT ... DO UPDATE ... WHERE source <>
'CONVERSATION' OR EXCLUDED.source = 'CONVERSATION'` cannot.

**Validation in the store, not only in the builder.** The builder always
produces a valid result; the validator is for everything else — a hand-edited
record, a rebuild from an older shape, a future writer. Putting it in
`save_call_result` means nothing unvalidated reaches the table whoever calls.

**Malformed input is tolerated and named, not refused.** The call is over by
the time the result is built; refusing to build one over a stray field would
lose the whole result. The builder leaves what it cannot read `UNKNOWN`/`None`
and lists it in `issues` on the row. The *validator* is strict, and the two
never disagree because the builder derives what the validator checks.

**The summary is deterministic.** An LLM summary would be a second inference
per call on a tier that is already throttling, and — the real reason — it
could not be proved to invent nothing. Six sentences composed from fields,
each saying "not established" when the field is unknown, can be.

**Qualification is re-derived rather than copied.** Copying
`qualification_status` from the record would have been one line. Re-deriving
it through `QualificationRecord` means a record that *claims* `QUALIFIED` with
no pain points is stored as `UNKNOWN` with a note, which is the rule the
phase asks for made mechanical.

**`TRANSFERRED` was added to the disposition list; nothing else was.** The
phase's list plus one, and the one is the case where hiding it under
`COMPLETED` would lose the most important fact about the call. Callback and
meeting have their own status columns (`MeetingOutcome`, `CallbackOutcome`)
rather than reusing the `meetings`/`callbacks` table statuses, because "where
did the meeting get to on this call" and "does the booking still stand" are
different questions.

**`interest_level` on a do-not-call is left as recorded, `next_action` is
not.** After "never call me again", a later "let me speak to a person" writes
`HUMAN_FOLLOW_UP` into the record. The result overrides that to
`DO_NOT_CONTACT` and notes it in `issues`: a rep reading the next action alone
must not phone somebody who asked not to be phoned. It restates the state; it
does not infer beyond it.

**The attempt status is itself evidence of a no.** `derive_disposition` and the
validator accept `call_status = NOT_INTERESTED` as a recorded no, because the
sink only writes it from one. "Unknown is not not-interested" is about
absence, and a status on the row is not absence.

**The transcript's user side comes from the director, not the aggregator
event.** Both carry the same words; the director's are the ones the detectors
ran over and the model replied to, and using them means the transcript, the
record and the signals cannot disagree. The agent side has only one source.

**`fake_carrier.py` carries the campaign ids.** Without them a simulated call
has no attempt row and the sink is a `LoggingSink`; with them the whole
Phase 8 path — bot teardown, sink, validator, upsert — runs against the real
database. Three optional flags, and the tool stays a plumbing check.

### Phase 7

**`FunctionSchema` with a handler, not raw direct functions, and the schema is
still derived.** Phase 6's direct functions had no seam between the model's
arguments and the function body: Pipecat unpacks `**args` straight into the
call, so an unexpected key is a `TypeError` and the model is told "the function
failed and returned no result". `strict_tool` keeps the derivation (the schema
comes from the same signature and docstring, and a test asserts it is identical)
and adds the seam, because the requirement is a *strict* schema and structured
failures, and "TypeError" is neither.

**Unknown arguments are dropped and logged, not refused.** A small model adds
a stray key now and then; refusing the whole call for it would make the model
loop on a tool that did nothing wrong. Missing required arguments and wrong types
*are* refused, with the expected shape in the result, because those change what
the tool would do.

**Two error vocabularies, deliberately.** `ActionOutcome` (backend → conversation)
and `ToolResult` (conversation → model) share the error codes but not the
guidance. The backend never composes a sentence for the model; the conversation
never parses a calendar response. Adding a provider touches one, changing what
the agent says touches the other.

**The record never claims more than the backend confirmed.** `meeting_booked`,
`callback_scheduled_for`, `transferred` are written on `ok` and nowhere else;
the *intent* fields (`meeting_intent`, `callback_when`) are written whatever
happened, so a failed booking still leaves "they agreed to Monday at ten" for a
person to act on. `NextAction` gained `MEETING_BOOKED` and `TRANSFERRED` so the
CRM-facing status can tell "agreed" from "done".

**`request_meeting` stays.** With a calendar, `check_calendar_availability` and
`book_meeting` cover the whole flow and the old tool is nearly redundant. It
stays because a bot with `CALENDAR_PROVIDER=none` — or one whose calendar has
failed mid-call — still needs a way to record that a meeting was agreed, and
because its result now routes the model to the calendar when there is one.

**The output cap is a config setting, not a constant, and it is always sent.**
See Known issues for why its absence was fatal on Groq. 400 by default: three
spoken sentences are under a hundred tokens and a `record_discovery` call with
every field filled is about 150, so it is generous without being the 1,815 Groq
assumes.

**Tool docstrings are one sentence plus argument formats.** Every word is sent on
every turn, twelve times over. The *how* lives in the stage block, which is in
front of the model once, and only for the stage it is in. The first draft's
descriptions were what pushed a request from ~3,100 tokens to ~3,850.

**The calendar hint is on every selling stage.** The same lesson as Phase 6's
discovery hint, learned the same way: with it only on `MEETING_REQUEST`, the
model answered "would Monday work?" with "shall I look up a couple of slots for
you?" — asking permission to call a tool instead of calling it. A meeting is
agreed in whatever stage the call happens to be in.

**Transfer pushes no end frame.** The carrier closes the media stream when it
applies the new TwiML, and that closing is the session's end. An `EndFrame` from
the bot would reach the serializer, whose `auto_hang_up` would then tell the
carrier to *complete* the call — the very call that was just handed to a person.

**Times are aware everywhere and the zone is one setting.** `CALENDAR_TIMEZONE`
is what the agent is told the time in, what the tools expect, what the calendar
offers slots in, and what `campaign.py` prints in. UTC by default with a startup
warning, because guessing a zone offers a Karachi prospect a four-in-the-morning
meeting and saying nothing hides it. `tzdata` was added because Windows has no
zone database of its own — `ZoneInfo("Asia/Karachi")` raised before it.

### Phase 6

**Tools, not transcript parsing, and a phrase list only as a floor.** The
requirement is explicit that conversation state must not depend on fragile
string matching alone. A tool call is the model's own structured statement of
what it understood, which is the only mechanism that reads a real sentence
correctly. The detectors in `signals.py` exist because "the model usually
notices" is not a standard you can apply to a legal obligation — and they are
deliberately the *shorter* list: only a do-not-call request forces a state.
Everything else raises guidance and lets the model decide.

The asymmetry is the decision. Forcing is reserved for the one case where a
false positive costs a sale and a false negative costs a person being phoned
after they asked not to be. `REJECTION` is advisory precisely because "I'm not
interested in switching right now, but tell me more" is a sentence people say.

**A state machine with a table, not a prompt instruction.** "Never push after a
clear rejection" could have been a line in the system prompt. It is a missing
row in `_ALLOWED` instead, so it holds when the model ignores the prompt — which
it will, occasionally, because a sales-trained model treats "not interested" as
an objection to overcome. A refusal is *recorded* rather than raised: a live call
must not die because the model asked for something silly, and the refusal count
is a measurement of how well the prompt is holding.

**Guidance is per inference, on a copy of the context, never in the history.**
The same choice Phase 3 made for the knowledge block, for the same reason, and it
is what lets the stage block say "you still do not know their timing" — a fact
that changes turn by turn and that a system prompt written at connect time could
not carry. Appending it for real would leave forty obsolete stage notes in a long
call, each telling the model to do something it finished ten turns ago.

**The director goes after the retriever.** The retriever builds its search query
from the last user message; a guidance block appended first would become that
query. Both processors carry `tools` and `tool_choice` through their copies,
because silently stopping advertising the tools is the kind of failure that
looks like "the model stopped calling them".

**The system instruction is set at construction, per session, and never
rewritten.** `make_llm` takes it as an argument. Identity is durable — the agent
is the same agent for the whole call — and rewriting a system prompt mid-call to
get one different sentence is the wrong tool; a turn instruction is the right
one. This is the AGENTS.md rule about the two places instructions live, applied.

**Unknown is a value, and the model cannot assert a qualification.** Every enum
has an `UNKNOWN` member, an unparseable argument leaves its field alone, and
`qualification_status` is derived from need, interest and authority rather than
set. A guessed field is worse than a missing one: a missing field says "go and
find out", a guessed one says "no need to ask", and the second is how a rep ends
up on a call quoting a timeline nobody gave.

**Missing prospect fields are named in the prompt, not omitted from it.** A model
reading a prompt where `company` is simply absent cannot tell "not supplied"
from "not applicable", and fills the gap. One reading "Not known: their company,
their job title" has been told both that it does not know and that saying so is
expected. The same shape covers a missing company name for the *agent*: it is
told it has not been given one and forbidden to invent one, rather than handed a
placeholder that would be spoken aloud to a stranger.

**A dev prospect is used only when the call carries no prospect id at all** —
never as a fallback for a lookup that failed. A campaign call whose prospect
cannot be found must stay anonymous, because the alternative is greeting a real
stranger by a test fixture's name.

**Do-not-call fires its backend action mid-call, and the sink swallows its own
errors.** Waiting until the end of the call would lose the request if the line
dropped thirty seconds later. The cost is a database write inside the turn; the
mitigation is that a *failing* write costs nothing but a log line, because the
agent honours the request for the rest of the call regardless.

**`dialer.refresh` will not overwrite a conversation outcome.** `CALLBACK_REQUESTED`,
`NOT_INTERESTED` and `DO_NOT_CALL` come from what somebody said; the carrier only
ever reports that the call completed, and to it those calls look identical. Three
lines in `refresh`, and without them the poller quietly erases the only
interesting thing the call produced.

**One nullable column rather than a new table.** `conversation_data jsonb` on
`call_attempts`, added by `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` inside the
existing idempotent `create_schema`, because the row already exists for exactly
this call and `campaign.py init` is already the documented way to bring a schema
up to date. Readers tolerate the column's absence, so a database nobody has
re-initialised still works.

**The outcome crosses the package boundary as a dict, not a typed object.** It
goes from `src/conversation/` to `src/campaigns/` through `ConversationSink`, and
keeping it untyped means adding a field to the qualification record does not
change the interface between two packages that otherwise know nothing about each
other.

**Retrieval is gated, and the gate skips rather than allows.** A sales
conversation is mostly not questions, and Phase 3's unconditional search
attaches confident, on-topic, irrelevant passages to turns nobody asked anything
in. But the failure modes are not symmetric — a turn wrongly searched costs one
indexed query, a turn wrongly skipped costs the grounding the knowledge base
exists for — so anything with a question mark, an interrogative or a commercial
noun goes through, and only turns with none of the three are dropped.

**The sales evals are text mode, reversing Phase 2's rule.** Phase 2 made every
scenario audio because on this project the STT *is* the turn-taking. Phase 6
changed what the agent decides, and the harness's own heuristic applies: text
tests the brain, audio tests the ears and mouth. Text also turns the important
assertions from opinions into facts, because `function_call` asks which tool was
called rather than asking a judge to infer it from prose.
`sales/interruption.yaml` stays audio, because barge-in is the one thing text
mode cannot exercise at all.

---

## 7. Assumptions

Verify these before relying on them; each was true on 2026-09-02 on this machine.

1. **The Deepgram key has Flux access.** Verified by connecting to `wss://api.deepgram.com/v2/listen?model=flux-general-en`. If a future key does not, `STT_PROVIDER=deepgram` is the fallback.
2. **One bot per process.** The loguru error sink and the session summary assume this.
3. **`qwen/qwen3.8-27b` is still on Groq.** Groq rotates its catalogue often; a 404 `model_not_found` mid-call means it was retired. Set `GROQ_MODEL`.
4. **English only.** `flux-general-en`, Kokoro `af_heart`, Moonshine `en`. Multilingual needs `flux-general-multi` plus `language_hints`.
5. **Local development.** No deployment target chosen; no `Dockerfile` or `pcc-deploy.toml` exists. Telephony makes this more pressing than it was: an ngrok URL that changes on every restart is a development tool, not a deployment.
6. **The user's `.env` has DEEPGRAM, GROQ, CARTESIA and ANTHROPIC keys.** Only the first three are used by the defaults. There are **no** carrier credentials of any kind — neither Twilio nor SignalWire — verified 2026-09-03.
7. **The repo *is* under git as of Phase 9** — this changed since Phase 7, which recorded the opposite. One commit ("first commit") on `main`, with a remote at `github.com/MubeenKhalid10/AI-Call-Agent`. `server/` is a nested repository (it shows as a single modified entry in the parent's `git status`), so check both. Nothing from Phases 8 or 9 has been committed; no session of this project has committed anything.

   > ⚠️ **`server/.env` is tracked and was committed in "first commit".** It
   > holds the live Deepgram, Groq, Cartesia and SignalWire credentials. If that
   > commit was ever pushed to the GitHub remote, **those keys should be treated
   > as disclosed and rotated**, and the file untracked
   > (`git rm --cached server/.env`) with `.env` added to `server/.gitignore` —
   > which currently exists only at the repository root and does not cover it.
   > Untracking alone does not remove the key from history. This is contrary to
   > `AGENTS.md` §5 ("`.env` is git-ignored. Never commit real keys") and to
   > Phase 9's own rule that a credential must never leave the machine, so it is
   > recorded here rather than fixed: rotating somebody's keys and rewriting
   > their published history are their decisions, not a phase's.
   > `server/__pycache__/` is tracked too, which is harmless but noisy.
8. **PostgreSQL with pgvector is running locally and the schema exists.** Verified 2026-09-03; the sample knowledge base (`evals/kb`, 2 documents, 25 chunks) was ingested that day for the eval run. It was empty before that.
9. **`load_dotenv(override=True)`, so `.env` beats the shell environment.** `KB_ENABLED=false uv run bot.py` does *nothing* if `.env` says `KB_ENABLED=true`. This surprised this session; to A/B the knowledge base you must edit `.env`. (`SESSION_IDLE_TIMEOUT_SECS` works from the shell only because `.env` does not set it.)

---

## 8. Files and modules modified

### New in Phase 2

| Path | Purpose |
|---|---|
| `server/src/turns.py` | VAD, turn-start/stop strategies, `mark_interrupted_reply` |
| `server/src/metrics.py` | Per-response latency, session summary, error counting |
| `server/src/resilience.py` | `SilenceHandler`, `ConnectionGuard`, `prompt_agent` |
| `server/evals/` | Suite manifest, scenarios, shared `!include` blocks, Groq judge, README |

### New in Phase 3

| Path | Purpose |
|---|---|
| `server/ingest.py` | Knowledge base CLI: `init`, `add`, `list`, `search`, `remove`, `reset` |
| `server/src/documents.py` | PDF/text extraction and chunking |
| `server/src/embeddings.py` | Local fastembed embedder, warm-up, cache |
| `server/src/knowledge_store.py` | PostgreSQL + pgvector schema, search, counts |
| `server/src/retrieval.py` | The pipeline stage that grounds each answer |
| `server/tests/test_knowledge.py` | Deterministic retrieval checks, no database |
| `server/evals/kb/`, `knowledge_*.yaml` | Sample documents and two eval scenarios |
| `server/scripts/install_pgvector.ps1` | pgvector setup on Windows |

### New in Phase 4

| Path | Purpose |
|---|---|
| `server/src/telephony/base.py` | `TelephonyProvider`, `CallStatus`, `CallRequest`/`CallSnapshot`, TwiML, `stream_url` |
| `server/src/telephony/twilio.py` | The only file that knows Twilio exists |
| `server/src/telephony/signalwire.py` | Twilio's API somewhere else — a subclass overriding a host and three strings |
| `server/src/telephony/transport.py` | Builds a call's transport so the *provider* supplies the serializer |
| `server/src/telephony/session.py` | `CallSession`: the bot's view of the call it is on |
| `server/src/telephony/__init__.py` | Public surface and `make_provider` |
| `server/call.py` | Place an outbound call and watch it; exit codes 0/1/2 |
| `server/tests/test_telephony.py` | 125 deterministic checks: placement, outcomes, errors, handshake, config, serializers, wiring |
| `server/tests/fake_carrier.py` | Simulates a carrier against a running bot; no account, no tunnel, no money |
| `server/tests/fake_browser.py` | Simulates the *browser* against a running bot, including the drop-and-reconnect that nothing else covers |
| `server/tests/test_realtime.py` | 19 deterministic checks: echo suppression and the peer watchdog |

### Modified in Phase 26 (frontend only)

- `server/web/index.html` — the shell, the icon set, the grouped navigation.
- `server/web/styles.css` — rewritten: the design system.
- `server/web/app.js` — rewritten: the same routes and API calls, redesigned pages, `friendly()`, `STATUS`, skeletons, accessible dialogs.
- `README.md`, this file.

### Untouched by Phase 26

Everything else: `bot.py`, `src/` (pipeline, conversation, campaigns, engine, store, telephony, automation API, dashboard API, security), `tests/`, `n8n/`, the schema.

### New in Phase 25

- `server/src/app/engine.py` — `CampaignEngine`.
- `server/src/campaigns/runtime.py` — `build_service / build_guards / build_gate / build_worker`, `Scheduler`.
- `server/src/campaigns/progress.py` — `campaign_progress(store, campaign)`.
- `server/tests/test_engine.py` — 68 checks.

### Modified in Phase 25

- `server/src/app/server.py` — the engine in the lifespan; `/readyz`'s engine check; `GET /api/app/engine`, `GET /api/app/campaigns/{id}/progress`, `GET /api/app/stream`.
- `server/src/campaigns/store.py` — `campaign_progress`, `campaign_concurrency`, `PROGRESS_KEYS`; the campaign ceiling inside `_reserve`.
- `server/src/campaigns/dialer.py` — a reason on every unhappy carrier ending.
- `server/src/campaigns/__init__.py` — the two new exports.
- `server/src/automation/api.py` — `ConfigurationIn.max_concurrent_calls`; the retry route.
- `server/src/config.py` — `WorkerConfig.embedded`, `shutdown_secs`.
- `server/campaign.py` — assembles the scheduler through `runtime.build_worker`.
- `server/app.py` — the engine by default, `--no-engine`, the banner.
- `server/web/app.js` — the progress strip, the event stream, the engine badge, the Retry button, the concurrency field.
- `server/tests/test_worker.py` — the memory store's `campaign_progress` and ceiling.
- `server/validate.py`, `server/.env.example`, `README.md`, this file.

### Untouched by Phase 25

`bot.py`, `src/conversation/`, `src/telephony/`, `src/actions/`, `src/scheduling/`, the worker's loop (`src/campaigns/worker.py`), the service, the recovery, the schema, the dashboard, the security layer, every earlier check script and the eval suites.

### Modified in the Phase 24 integration audit

- `server/web/app.js` — routes matched on the hash's path (query strings kept for filters); `c.queue`; the campaign page re-reads before re-showing a saved tab and remembers its tab; the call page's contact name from first / last; auto-refresh while a campaign runs (`REFRESH_MS`, `state.live`, `state.tabs`).
- `server/src/campaigns/service.py` — `import_csv(dry_run=True)` counts already-known numbers.
- `server/src/campaigns/dialer.py` — `_hold_unresolved` survives an empty error message; and the same hardening of `str(exc).splitlines()[0]` in `src/app/server.py`, `src/automation/api.py`, `src/automation/events.py`, `src/campaigns/service.py`, `src/campaigns/webhooks.py`, `src/campaigns/worker.py`, `src/compliance/gate.py`, `src/crm/sync.py`, `src/dashboard/web.py`, `src/monitoring/http.py`, `src/reliability/health.py`, `src/scheduling/calcom.py`, `src/security/audit.py`.
- `server/tests/test_worker.py` — two checks: an ambiguous placement whose error has no message is held as `UNRESOLVED` with the exception's name as the reason.
- `README.md`, this file.

### New in Phase 24

- `server/app.py` — the unified application's entry point (port 7900; `--with-scheduler`).
- `server/src/app/__init__.py`, `server/src/app/server.py` — `create_unified_app`: mounts the dashboard and the automation API, serves the page, the `/api/app/*` routes.
- `server/web/index.html`, `server/web/styles.css`, `server/web/app.js` — the single-page application.
- `server/tests/test_app.py` — 68 checks.

### Modified in Phase 24

- `server/src/automation/api.py` — the session cookie accepted as a principal (with the CSRF header on writes); `PUT /campaigns/{id}/configuration`.
- `server/src/security/http.py` — `HttpPolicy.csp`.
- `server/validate.py` — `test_app` in the script list.
- `server/.env.example` — `APP_HOST`, `APP_PORT`, `APP_BOT_URL`.
- `README.md`, this file.

### Untouched by Phase 24

`bot.py`, `campaign.py`, `dashboard.py`, `automation.py`, `src/conversation/`, `src/telephony/`, `src/actions/`, `src/scheduling/`, `src/campaigns/` (the store, the service, the dialer, the worker, the brief), `src/dashboard/`, `src/monitoring/`, `src/security/` beyond the one field, `src/compliance/`, `src/crm/`, the schema, every earlier check script and the eval suites.

### New in Phase 23

- `server/tests/test_production.py` — the end-to-end validation suite (158 checks; PostgreSQL required).
- `server/validate.py` — the validation runner, `measure`, `live`; writes `validation-report.md` / `.json`.
- `PRODUCTION_READINESS.md` — at the repository root.

### Modified in Phase 23

- `server/src/campaigns/service.py` — `import_csv` applies the do-not-call list per row, before the membership is opened.
- `server/src/campaigns/briefing.py` — `_notes_for` matches `custom_data` keys case-insensitively.
- `server/src/reliability/health.py` — `_check_tts` probes ElevenLabs (`/v1/user`, `xi-api-key`).
- `server/security.py` — `_env_duplicates()` and the duplicate-variable warning in `check`.
- `README.md`, this file.

### Untouched by Phase 23

`bot.py`, `src/conversation/`, `src/telephony/`, `src/actions/`, `src/monitoring/`, `src/security/`, `src/compliance/`, `src/crm/`, `src/automation/`, `src/dashboard/`, `src/scheduling/`, the store, the dialer, the worker, the receiver, every eval, every other test, `.env.example` (nothing new to configure).

### New in Phase 22

- `server/src/monitoring/__init__.py` — the package: what it is, the boundary, the re-exports.
- `server/src/monitoring/metrics.py` — `MetricsRegistry`, `Counter`, `Gauge`, `Histogram`, `HistogramStats`, `LABEL_NAMES_ALLOWED`, `MetricError`, `percentile`, the Prometheus rendering and the JSON snapshot, `REGISTRY`.
- `server/src/monitoring/instruments.py` — every `aiva_*` metric by name, the bucket sets, `measured`, `outcome_of`, `process_started` / `process_stopping`.
- `server/src/monitoring/tracing.py` — `new_trace_id`, `new_request_id`, `clean_id`, `PARAM_TRACE_ID`, `REQUEST_HEADER`, `trace_from_runner_args`.
- `server/src/monitoring/http.py` — the three routes, `ReadyCheck` / `Readiness`, `run_check`, `store_ready`, `always_ready`, `install_ops_routes`, `create_ops_app`, `RequestIdMiddleware`, `RequestMetricsMiddleware`, `serve_ops` / `OpsServer`.
- `server/src/monitoring/collect.py` — the fleet gauges from the rows: `set_*_gauges`, `refresh_from_store`, `GaugeRefresher`.
- `server/tests/test_monitoring.py` — the twenty-first check script (170 checks).

### Modified in Phase 22

- `server/src/reliability/observability.py` — `trace`, `worker`, `request` in `CALL_FIELDS`; `CallContext.trace_id`; `call_context` maps `*_id` keywords to the short field names; `current_trace_id()`; `configure_logging(component=)` with `component` and `pid` on JSON lines; `MONITORING_TOKEN` in `SECRET_ENV`.
- `server/src/config.py` — `MonitoringConfig` (`MONITORING_*`), `Config.monitoring`.
- `server/src/campaigns/models.py` — `CallAttempt.trace_id`.
- `server/src/campaigns/coordination.py` — `Throughput`.
- `server/src/campaigns/store.py` — the `trace_id` column in `create_schema`, `_attempt()` reads it; `ping()`, `set_attempt_trace()`, `throughput()`; `_timed` on fifteen call-path writes (`store.op` lines, `aiva_store_*`).
- `server/src/campaigns/dialer.py` — the trace born in `dial()`, `_stamp_trace`, `PARAM_TRACE_ID` on the request, `aiva_call_attempts_total` on every path, placement latency, carrier failures; the trace on `refresh()`'s context.
- `server/src/campaigns/worker.py` — outcomes, ticks, heartbeats, in-flight, callback outcomes, `refresh_fleet_gauges()` on the report tick, `_context` with the trace and `worker=` (now an instance method).
- `server/src/campaigns/webhooks.py` — `aiva_webhook_events_total` and latency in `_done`; the trace on the attempt's context; the standalone app mounts the ops routes.
- `server/src/campaigns/briefing.py` — `aiva_call_results_total{outcome}` when a result is saved.
- `server/src/crm/sync.py` — `_sync` split into `_sync` / `_sync_traced` (the trace bound once the attempt is read); `_count`; outcomes and latency.
- `server/src/automation/events.py` — `_deliver` split into `_deliver` / `_send` (the trace from the payload); outcomes and latency.
- `server/src/automation/serialize.py` — `trace_id` in `attempt_dict`.
- `server/src/automation/api.py` — `ApiSettings.monitoring` / `worker_stale_secs`; the ops routes outside the keyed router; a `GaugeRefresher` in the lifespan; readiness includes the deliverer task.
- `server/src/dashboard/web.py` — the ops routes, a `GaugeRefresher` in the lifespan.
- `server/src/actions/service.py` — the five tools wrap private bodies through `_observed`; `aiva_tool_seconds`.
- `server/src/reliability/supervisor.py` — `aiva_service_errors_total`, `aiva_supervisor_terminations_total`.
- `server/src/diagnostics.py` — `aiva_barge_ins_total`.
- `server/src/metrics.py` — `aiva_turn_latency_seconds{stage}`, `aiva_greeting_latency_seconds`.
- `server/bot.py` — the trace from the handshake or made fresh, sessions active, `_record_session_metrics` at teardown, `trace_id` on the report identity, `_install_ops(app)` in `__main__`, `configure_logging(component="bot")`.
- `server/campaign.py` — `serve_ops` in `command_run`, `_worker_readiness`, `command_metrics` and its parser; `configure_logging(component="scheduler")`.
- `server/dashboard.py`, `server/automation.py`, `server/webhooks.py` — `component=` on `configure_logging`, the ops routes in the banner.
- `server/security.py` — the monitoring line in `check`.
- `server/tests/test_worker.py` — `MemoryStore.set_attempt_trace`, `ping`, `throughput`.
- `server/tests/test_campaigns.py` — the callback fixture is the next Monday from today, not 2026-09-07 (it had rotted).
- `server/.env.example` — the MONITORING section and `LOG_COMPONENT`.
- `README.md`, `SECURITY.md` (§9), `n8n/README.md` (`trace_id` on the call payload), this file.

### Untouched by Phase 22

`src/conversation/` (every file), `src/telephony/` (every file), `src/scheduling/`, `src/compliance/`, `src/security/`, `src/reliability/` but for `observability.py` and `supervisor.py`, `src/campaigns/service.py`, `recovery.py`, `results.py`, `csv_import.py`, `phone.py`, `src/dashboard/stats.py` and `page.py`, `src/crm/` but for `sync.py`, `call.py`, `health.py`, `ingest.py`, every eval, every other test.

### New in Phase 21

- `server/src/campaigns/coordination.py` — the vocabulary: the three advisory-lock keys, `make_worker_id`, `WorkerRecord` (`is_stale`, `health`, `to_dict`), `WorkerSummary`, `QueueDepth`, `transient_failure`. Imports nothing from the pipeline or the carrier.
- `server/tests/test_scaling.py` — 151 checks, the twentieth script (§10).

### Modified in Phase 21

- `server/src/campaigns/store.py` — the `worker_id` column, the `scheduler_workers` and `scheduler_state` tables in `create_schema`; `_reserve` under `pg_advisory_xact_lock` and writing `worker_id`; `take_pacing_slot`; `register_worker`, `heartbeat_worker`, `mark_worker_stopped`, `list_workers`, `worker_summary`, `prune_workers`, `set_attempt_worker`, `claim_abandoned_attempts`, `release_abandoned_reservations`, `queue_depth`; `_worker`.
- `server/src/campaigns/worker.py` — identity, heartbeat, adoption pass, ownership check, hand-over; `_register`, `_heartbeat`, `_hand_over`, `_deregister`, `_adopt`, `_note_coordination_error`; three counters; the pacing refusal ending the placing loop.
- `server/src/campaigns/service.py` — `retry_transient_failures`, `transient_retry_minutes`; the transient branch in `record_outcome`; `worker_id=` through `next_call` and `reserve_membership`.
- `server/src/campaigns/dialer.py` — `worker_id=` on `dial_next` / `dial_membership`; the shared slot in `dial()`; `_campaign_pacing`.
- `server/src/campaigns/models.py` — `CallAttempt.worker_id`. `server/src/campaigns/__init__.py` — the exports.
- `server/src/reliability/guardrails.py` — `PacingLimiter.interval_secs`. `server/src/reliability/health.py` — the `scheduler` component. `server/health.py` — `scheduler` in the choices.
- `server/src/config.py` — `WorkerConfig`'s six fleet settings (`WORKER_ID`, `WORKER_HEARTBEAT_SECS`, `WORKER_STALE_SECS` — refused below two heartbeats, `WORKER_ADOPT_SECS`, `WORKER_RETRY_TRANSIENT_FAILURES`, `WORKER_TRANSIENT_RETRY_MINUTES`); `from typing import Any` (a Phase 19 annotation had none).
- `server/campaign.py` — `command_run` wires the identity and timings; `_service` passes the retry settings; `command_workers` and its parser.
- `server/src/dashboard/stats.py` — `Snapshot.scheduler`, `_scheduler`, two more reads in `collect`. `server/src/dashboard/page.py` — the *Workers and queue* strip. `server/src/automation/api.py` — `scheduler` in `/status`.
- `server/tests/test_worker.py` — `MemoryStore`'s coordination methods and `worker_id`; two Phase 13 expectations (a second worker does not adopt a live worker's call). `server/tests/test_webhooks.py` — one expectation (a SIP 503 is retried).
- `server/.env.example` — the "Several workers at once" block. `README.md` — "Multi-worker production scaling (Phase 21)". `HANDOFF.md` — this.

### Untouched by Phase 21

`bot.py`, `call.py`, `dashboard.py`, `automation.py`, `webhooks.py`, `security.py`, `src/conversation/`, `src/actions/`, `src/telephony/`, `src/compliance/`, `src/security/`, `src/crm/`, `src/scheduling/`, `src/automation/` beyond one field in `/status`, `src/campaigns/webhooks.py`, `recovery.py`, `results.py`, `briefing.py`, every eval, every drill, `pyproject.toml`.

### New in Phase 20

Nothing new on disk: the phase extends files that existed. (`page.py`
gained a second document, `CALL_PAGE`, in the same file.)

### Modified in Phase 20

- `server/src/campaigns/store.py` — filters on `prospect_counts`, `attempt_counts` (plus the quality and failure figures), `result_counts` (plus six counts), `disposition_counts`, `meeting_counts`, `callback_counts`, `campaign_result_counts` (plus transferred and opted out), `campaign_overview` (one campaign; the membership statuses; voicemail); `recent_call_rows` (filters, prospect, status, paging, search); `get_attempt_usage`, `search_prospects`.
- `server/src/campaigns/briefing.py` — `_store_usage` writes `usage["quality"]`; `_quality_summary`, `_int_or_none`.
- `server/src/dashboard/stats.py` — `ReportFilter`; `collect(filters=)`; `Snapshot`'s five new strips; `_conversion`, `_performance`, `_errors`, `_compliance`, `_progress`, `_dnc_counts`, `_blocked_counts`; `_campaigns` and `_recent_calls` with the filters; `list_calls`, `search_people`, `call_detail`, `_usage_detail`, `_quality_detail`, `_plain`; `MAX_CALLS_PAGE`.
- `server/src/dashboard/web.py` — `_SnapshotCache` keyed and bounded; `FilterError`, `parse_date`; `/api/dashboard` filters; `/api/campaigns`, `/api/calls`, `/api/search`, `/api/calls/{id}`, `/calls/{id}`; version 20.
- `server/src/dashboard/page.py` — the filter bar, the strips, the calls table, the campaigns table's progress; `CALL_PAGE`, `render_call_page`, `_who`; the shared `_STYLE`.
- `server/src/dashboard/__init__.py` — the exports.
- `server/tests/test_dashboard.py` — `check_analytics`, `metric_value`, the filter section, the route checks over the seeded schema through a `store_factory` (run from the database section in a thread), a viewer user, `check_degraded`'s counts.
- `README.md` — "Production dashboard and analytics (Phase 20)". `HANDOFF.md` — this.

### Untouched by Phase 20

`bot.py`, `call.py`, `campaign.py`, `dashboard.py` (the launcher), `health.py`, `automation.py`, `webhooks.py`, `security.py`, `src/conversation/`, `src/actions/`, `src/telephony/`, `src/compliance/`, `src/security/`, `src/crm/`, `src/scheduling/`, `src/automation/`, `src/campaigns/` beyond the store's reads and the sink's one write (the dialer, the worker, the service, the gate, the models did not change), every eval, every drill, `pyproject.toml`.

### New in Phase 19

- `server/src/compliance/__init__.py`, `policy.py`, `dnc.py`, `gate.py` — the compliance package (§3, Phase 19).
- `server/tests/test_compliance.py` — 146 checks.
- `COMPLIANCE.md` — which controls the software implements, which policies the operator configures, the settings, the list, what is recorded, what the software does not do; and that none of it is legal advice.

### Modified in Phase 19

- `server/src/config.py` — `ComplianceConfig`, `Config.compliance`, `compliance_policy`, `policy_resolver()`; imports `src.compliance.policy` (pure).
- `server/src/campaigns/store.py` — `DNC_TABLE`, `_DNC_EXISTS_SQL`, `_dnc_clause` (spliced into `_reserve` and `queue_outlook` when the table exists), the `dnc_numbers` table and indexes, `find_dnc`, `add_dnc`, `revoke_dnc`, `list_dnc`, `dnc_counts`, `prospects_with_number`, `apply_dnc_list`, `update_campaign_configuration`, `_dnc_entry`.
- `server/src/campaigns/service.py` — `compliance=`, `policy_for`, `policy_for_call`, `max_attempts_for`, `check_callable(max_attempts=)`, the reservations under the campaign's ceiling, `record_outcome` under the policy and listing a `DO_NOT_CALL`, `mark_do_not_call(source=, reason=, actor=, campaign_id=, call_attempt_id=)`, `add_do_not_call_number`, `remove_do_not_call_number`, `is_listed`, `_list_number`, `_apply_dnc_list`, `create_prospect` / `import_csv` / `add_prospects` applying the list and never joining a DNC prospect, `exhaust`.
- `server/src/campaigns/dialer.py` — `gate=`; `dial()` acting on the verdict; the gate-less path unchanged.
- `server/src/campaigns/briefing.py` — the source resolving the call's policy, the disclosures on the brief and the `compliance.disclosure` row; the sink listing a campaign call's opt-out with its ids and an anonymous caller's by number; `open_briefing(compliance=)`.
- `server/src/campaigns/results.py` — `Disposition.OPTED_OUT`; `derive_disposition` and `validate_call_result`.
- `server/src/conversation/brief.py` — `CampaignBrief.disclosures`, carried from the defaults, rendered as REQUIRED DISCLOSURES. `playbook.py` — `opening_instruction` with a required first sentence.
- `server/src/crm/mapping.py`, `server/src/dashboard/stats.py` — the new disposition's word and tone.
- `server/src/automation/api.py` — `ApiSettings.compliance`, the service built with it, `DncIn` / `DncRemoveIn` / `ComplianceIn`, the `/dnc*` and `/campaigns/{ref}/compliance` routes, `do-not-call` listing the number, `POST /calls` refusing a listed number.
- `server/campaign.py` — `_service` with the resolver, `_gate`, both dialers with `gate=`, `dnc` (a number too), `dnc-remove`, `dnc-list`, `dnc-import`, `dnc-apply`, `compliance`, `compliance-log`, `_actor`, `_audit_cli`, `_coerce`.
- `server/bot.py` — the resolver on the briefing; the environment's disclosures on the defaults.
- `server/tests/test_worker.py` — `MemoryStore`'s list, `prospects_with_number`, `apply_dnc_list`, `update_campaign_configuration`, the list in `_eligible`. `test_results.py`, `test_campaigns.py`, `test_crm.py` — the disposition.
- `server/.env.example` — a COMPLIANCE section. `README.md` — "Outbound calling compliance (Phase 19)". `n8n/README.md` — the DNC and compliance routes, the disposition change.
- `HANDOFF.md` — this.

### Untouched by Phase 19

`call.py`, `health.py`, `ingest.py`, `dashboard.py`, `webhooks.py`, `automation.py`, `security.py`, `src/actions/`, `src/telephony/`, `src/campaigns/worker.py`, `recovery.py`, `csv_import.py`, `phone.py`, `models.py`, `webhooks.py`, `src/crm/` (but the one word), `src/scheduling/`, `src/security/`, `src/automation/auth.py`, `events.py`, `serialize.py`, `src/dashboard/web.py`, `page.py`, `src/conversation/` beyond the field and the two instruction branches (the detector, the states, the tools, the sink protocol did not change), every eval, every drill, the n8n workflow files, `pyproject.toml` (no new dependency: libphonenumber was already there).

### New in Phase 18

- `server/src/security/__init__.py`, `roles.py`, `passwords.py`, `sessions.py`, `ratelimit.py`, `pii.py`, `audit.py`, `http.py` — the security package (§3, Phase 18).
- `server/security.py` — `hash-password`, `make-key`, `make-secret`, `check`.
- `server/tests/test_security.py` — 214 checks.
- `server/.gitignore` — `.env` and the usual artefacts, for the nested repository.
- `SECURITY.md` — the reference: what is protected, roles, setup, production HTTPS requirements, CORS, limits, validation, the audit log, secrets and rotation, webhook verification, the checklist, what is not covered.

### Modified in Phase 18

- `server/src/config.py` — `SecurityConfig` and `Config.security`; `AutomationConfig.operator_api_keys`, `viewer_api_keys`, `docs_enabled`, `role_for_key()`, `key_count`, `api_enabled` for any role; `_keys()`; imports from `src.security` (the pure modules only).
- `server/src/reliability/observability.py` — `SECRET_ENV` and `_COMMA_SEPARATED_SECRETS` gained the role-scoped keys, the session secret and `DASHBOARD_USERS`; `_SECRET_PATTERNS` gained a password-by-name, `aiva_session=`, and a scrypt hash.
- `server/src/campaigns/store.py` — `AUDIT_TABLE`, the `audit_log` table and two indexes in `create_schema`, `record_audit`, `list_audit`, `audit_counts`, `_audit_entry`.
- `server/src/campaigns/webhooks.py` — `create_webhook_router(…, security=)` with the refused-delivery limiter and the `Content-Length` check; `create_webhook_app` installs the security middleware; `install_webhook_receiver` passes `config.security`; version 18.
- `server/src/automation/api.py` — everything under "the API" in §3 (Phase 18); `ApiSettings.security`; `ApiError.headers`; the validators; `_PiiRoute`; `GET /audit`; `API_VERSION` 18.
- `server/src/dashboard/web.py` (rewritten around the login), `page.py` (the header, the 401 redirect, `render_login`), `__init__.py` (exports).
- `server/dashboard.py` — the docstring, the loopback rule under `DASHBOARD_AUTH_DISABLED`, `ConfigError` handled, the banner.
- `server/automation.py` — the banner (keys per role, the security line, the audit route), the network warning.
- `server/campaign.py` — `audit`.
- `server/tests/test_worker.py` — `MemoryStore.record_audit`, `list_audit`, `audit_counts`, `audit_entries`.
- `server/tests/test_dashboard.py` — the route checks sign in; the only writing routes are the login and the logout.
- `server/.env.example` — a SECURITY section.
- `README.md` — "Security (Phase 18)". `n8n/README.md` — keys have roles.
- `HANDOFF.md` — this.

### Untouched by Phase 18

`bot.py`, `call.py`, `health.py`, `ingest.py`, `webhooks.py` (the launcher), `src/conversation/`, `src/actions/`, `src/telephony/`, `src/campaigns/worker.py`, `dialer.py`, `service.py`, `recovery.py`, `results.py`, `briefing.py`, `csv_import.py`, `phone.py`, `models.py`, `src/crm/`, `src/scheduling/`, `src/automation/auth.py`, `events.py`, `serialize.py`, `src/dashboard/stats.py`, every eval, every drill, the n8n workflow files, `pyproject.toml` (no new dependency: scrypt and HMAC are the standard library's, Starlette was already there).

### New in Phase 17

| Path | Purpose |
|---|---|
| `server/src/automation/__init__.py` | The package's surface |
| `server/src/automation/auth.py` | API keys compared in constant time; `sign` / `verify_signature` (HMAC-SHA256, `t=…,v1=…`); `fingerprint` |
| `server/src/automation/serialize.py` | Every row as JSON, one shape for the API and the events |
| `server/src/automation/events.py` | `EventDeliverer`, `build_payload`, `AiohttpSender`, `SendResult`, the retry rules |
| `server/src/automation/api.py` | `create_automation_app`, `ApiSettings`, `ApiError`, the routes, the idempotency replay |
| `server/automation.py` | Serves the API and runs the deliverer; `--once`, `--no-deliver`, `--host`, `--port` |
| `server/tests/test_automation.py` | The seventeenth deterministic script (245 checks; the last section needs PostgreSQL) |
| `n8n/README.md` | The integration reference: setup, every variable, every endpoint, the event contract, the signature check, the six workflows, operations |
| `n8n/workflows/01-lead-intake-to-campaign.json` … `06-callback-due-to-call.json` | Importable n8n workflows |

### Modified in Phase 17

- `server/src/campaigns/models.py` — `AutomationEventState`, `AutomationEvent`, `ApiRequestRecord`.
- `server/src/campaigns/store.py` — `AUTOMATION_EVENTS_TABLE`, `API_REQUESTS_TABLE`, `AUTOMATION_EVENT_KINDS`, `_EVENT_CREATE_SQL` (six statements), the two tables' DDL, `claim_automation_events`, `record_automation_event`, `get_automation_event`, `find_automation_event`, `list_automation_events`, `automation_event_counts`, `retry_automation_events`, `_phase17`, `get_api_request`, `save_api_request`, `purge_api_requests`, `get_meeting`, `list_call_results(since=, before_id=)`, `list_callbacks(campaign_id=)`, `_automation_event`, `_api_request`.
- `server/src/campaigns/__init__.py` — the exports.
- `server/src/config.py` — `AUTOMATION_EVENT_KINDS`, `AutomationConfig`, `Config.automation`, `_url`, `_moment`, `_host_of`; `datetime` imported.
- `server/src/reliability/observability.py` — the four automation secrets in `SECRET_ENV`; `_COMMA_SEPARATED_SECRETS` so each key of a list is scrubbed.
- `server/campaign.py` — `events`, `events-retry`.
- `server/.env.example` — the AUTOMATION section. `README.md`, this file.

### Untouched by Phase 17

One exception first: `ruff check --fix` run over `src` at the end of the
session **sorted the import block of `src/actions/service.py`** (an `I001`
that predates this phase). No other line of that file changed, and
`test_actions.py`'s 240 checks pass after it. Otherwise:

`bot.py`, `src/conversation/` (every file), `src/actions/` (but for that import sort), `src/telephony/`, `src/crm/`, `src/scheduling/`, `src/dashboard/`, `call.py`, `webhooks.py`, `dashboard.py`, `health.py`, `ingest.py`, `src/campaigns/service.py`, `dialer.py`, `worker.py`, `recovery.py`, `briefing.py`, `results.py`, `csv_import.py`, `phone.py`, `webhooks.py`, `src/reliability/` (every file but `observability.py`), every eval, every other test.

### New in Phase 16

- `server/tests/test_booking_transfer.py` — the sixteenth deterministic script (99 checks; the last section needs PostgreSQL).

### Modified in Phase 16

- `server/src/telephony/base.py` — `WEBHOOK_TRANSFER`, `WebhookEvent.dial_call_id` / `transfer_answered` / the transfer key, `build_transfer_twiml(action_url=)`, `transfer_response_twiml`, `transfer_call(action_url=, timeout_secs=)`.
- `server/src/telephony/twilio.py` — `transfer_call` passes the action and ring time; `parse_webhook` decodes a `<Dial action>` report first.
- `server/src/telephony/__init__.py` — the exports.
- `server/src/scheduling/base.py` — `find_booking`, `check_credentials`.
- `server/src/scheduling/calcom.py` — `timeout_secs`, the lost-answer lookup, `find_booking`, `check_credentials`, the taken-slot markers, the timeout branch in `_request`.
- `server/src/scheduling/local.py` — docstring only (the constraint).
- `server/src/scheduling/__init__.py` — `make_calendar(calcom_timeout_secs=)`.
- `server/src/campaigns/models.py` — `TransferStatus`, `CallTransfer`, `WebhookOutcome.TRANSFER`.
- `server/src/campaigns/store.py` — the `meetings` exclusion constraint (under a savepoint), `MeetingConflictError` from `add_meeting`, `TRANSFERS_TABLE` and its DDL, `add_transfer`, `complete_transfer`, `list_transfers`, `transfer_counts`, `_phase16`, `_transfer`.
- `server/src/campaigns/webhooks.py` — `WebhookReceipt.body` / `media_type`, `_apply_transfer`, `_with_twiml`, the router answering XML.
- `server/src/campaigns/__init__.py` — the exports.
- `server/src/actions/service.py` — `transfer_action_url`, `transfer_timeout_secs`, `_record_transfer`, `MeetingConflictError` → `slot_taken`.
- `server/src/actions/__init__.py` — passes the receiver URL, the ring time and the Cal.com timeout.
- `server/src/config.py` — `CalendarConfig.calcom_timeout_secs`, `TelephonyConfig.transfer_timeout_secs` (and the ring time on `describe()`).
- `server/src/reliability/health.py` — `_check_calendar`; `server/health.py` — `calendar` in `COMPONENTS`.
- `server/campaign.py` — `transfers`.
- `server/tests/test_actions.py` — the fake carrier's two new arguments, the fake store's `add_transfer`; `server/tests/test_scheduling.py` — the stub session records the timeout. No assertion changed.
- `server/.env.example` — `CALCOM_TIMEOUT_SECS`, `TELEPHONY_TRANSFER_TIMEOUT_SECS`, and the Phase 16 "what a live test needs" section. `README.md`, this file.

### Untouched by Phase 16

`bot.py`, `src/conversation/` (every file), `call.py`, `webhooks.py`, `dashboard.py`, `ingest.py`, `src/crm/`, `src/voice_quality.py`, `src/voicemail.py`, `src/turns.py`, `src/services.py`, `src/metrics.py`, `src/resilience.py`, `src/diagnostics.py`, `src/prompts.py`, `src/retrieval.py`, `src/dashboard/`, `src/campaigns/service.py`, `dialer.py`, `recovery.py`, `worker.py`, `briefing.py`, `results.py`, `csv_import.py`, `phone.py`, every eval, every other test.

### New in Phase 15

- `server/src/crm/__init__.py` — the public surface and `make_crm_provider`.
- `server/src/crm/base.py` — `CrmProvider`, `CrmContact`, `CallActivity`, `CallSync`, `SyncReceipt`, `CallOutcome`, the four errors.
- `server/src/crm/mapping.py` — `build_call_sync`, `sync_key`. Pure.
- `server/src/crm/hubspot.py` — `HubSpotProvider`, `DISPOSITIONS`, `CONTACT_PROPERTIES`, `CALL_TO_CONTACT`.
- `server/src/crm/sync.py` — `CrmSyncer`, `SyncReport`, `SyncTotals`, `WRITE_POLICY`.
- `server/tests/test_crm.py` — the fifteenth deterministic script (179 checks; the last section needs PostgreSQL).

### Modified in Phase 15

- `server/src/campaigns/models.py` — `CrmSyncState`, `CrmSyncRecord`.
- `server/src/campaigns/store.py` — `CRM_SYNC_TABLE` and its DDL in `create_schema`, `claim_results_for_sync`, `record_crm_sync`, `get_crm_sync`, `list_crm_sync`, `crm_sync_counts`, `retry_crm_sync`, `_phase15`, `_crm_sync`.
- `server/src/campaigns/__init__.py` — the exports.
- `server/src/campaigns/results.py` — one sentence of the docstring: the CRM mapping now has a reader.
- `server/src/config.py` — `CRM_PROVIDERS`, `CrmConfig`, `Config.crm`, `describe()`.
- `server/src/reliability/health.py` — `_check_crm`; `server/health.py` — `crm` in `COMPONENTS`.
- `server/src/reliability/observability.py` — `HUBSPOT_ACCESS_TOKEN` in `SECRET_ENV`.
- `server/campaign.py` — `crm-sync`, `crm-status`, `crm-retry`.
- `server/.env.example` — the Phase 15 section. `README.md`, this file.

### Untouched by Phase 15

`bot.py`, `call.py`, `webhooks.py`, `dashboard.py`, `ingest.py`, `src/telephony/`, `src/conversation/`, `src/actions/`, `src/scheduling/`, `src/voice_quality.py`, `src/voicemail.py`, `src/turns.py`, `src/services.py`, `src/metrics.py`, `src/resilience.py`, `src/diagnostics.py`, `src/prompts.py`, `src/retrieval.py`, `src/dashboard/`, `src/campaigns/service.py`, `dialer.py`, `recovery.py`, `worker.py`, `webhooks.py`, `briefing.py`, `csv_import.py`, `phone.py`, every eval, every other test.

### New in Phase 14

- `server/src/campaigns/webhooks.py` — `WebhookProcessor`, `WebhookReceipt`, `WebhookMetrics`, `create_webhook_router`, `install_webhook_receiver`, `create_webhook_app`, `build_webhook_processor`.
- `server/webhooks.py` — the standalone receiver (`TELEPHONY_WEBHOOK_RECEIVER=standalone`), port 7880.
- `server/tests/test_webhooks.py` — the fourteenth deterministic script (191 checks; the last section needs PostgreSQL).

### Modified in Phase 14

- `server/src/telephony/base.py` — `WebhookRequest`, `WebhookEvent`, `WebhookError`, `WebhookSignatureError`, `WEBHOOK_STATUS` / `WEBHOOK_AMD`, `CallRequest.status_callback_url`, `TelephonyProvider.supports_webhooks` / `verify_webhook` / `parse_webhook`, `webhook_url()`.
- `server/src/telephony/twilio.py` — the status-callback fields on `place_call` (the body is now a list of pairs when a field repeats), `compute_signature`, `_url_variants`, `verify_webhook`, `parse_webhook`, `signature_headers`, `webhook_secret_hint`, `webhook_secret=`, `can_verify_webhooks`, `_integer`.
- `server/src/telephony/signalwire.py` — `signing_key=`, the SignalWire header first, `SIGNALWIRE_SIGNING_KEY` as the hint.
- `server/src/telephony/__init__.py` — the exports; `make_provider` passes the signing key.
- `server/src/config.py` — `TelephonyConfig.webhooks_enabled` / `webhook_receiver` / `webhook_path` / `webhook_signing_key` / `webhook_signing_env`, `can_verify_webhooks`, `webhook_url()`, `describe_webhooks()`, `describe()`; `_TELEPHONY_WEBHOOK_SIGNING`, `WEBHOOK_RECEIVERS`; `WorkerConfig.webhook_poll_secs`.
- `server/src/campaigns/models.py` — `WebhookOutcome`, `WebhookDelivery`.
- `server/src/campaigns/store.py` — `WEBHOOKS_TABLE` and its DDL in `create_schema`, `record_webhook_event`, `set_webhook_outcome`, `last_webhook_at`, `webhook_answered_by`, `list_webhook_events`, `webhook_counts`, `_phase14`, `_delivery`.
- `server/src/campaigns/dialer.py` — `status_callback_url=`, `map_call_status`.
- `server/src/campaigns/worker.py` — `webhook_poll_secs=`, `_Tracked.pushed` / `last_polled_at`, `_poll_due`, `_last_webhook_at`, `pushed=` on the completion line.
- `server/src/campaigns/__init__.py` — the exports and the module table.
- `server/src/reliability/observability.py` — `SIGNALWIRE_SIGNING_KEY` in `SECRET_ENV`.
- `server/bot.py` — `install_webhook_receiver(app, CONFIG)` before `main()`, and nothing else.
- `server/campaign.py` — `call` and `run` pass the URL and print the webhook state; `run` passes `webhook_poll_secs`; the `webhooks` subcommand.
- `server/call.py` — `status_callback_url` on the request, `--no-webhooks`, the dry-run line, the `watch()` docstring.
- `server/tests/test_telephony.py` — `StubSession` records the body as pairs as well as a dict; no assertion changed.
- `server/.env.example` — the Phase 14 section and `WORKER_WEBHOOK_POLL_SECS`. `README.md`, this file.

### Untouched by Phase 14

Every session-time path: `bot.py`'s sessions, `src/telephony/transport.py` and `session.py`, `src/conversation/`, `src/actions/`, `src/scheduling/`, `src/voice_quality.py`, `src/voicemail.py`, `src/turns.py`, `src/services.py`, `src/metrics.py`, `src/resilience.py`, `src/diagnostics.py`, `src/prompts.py`, `src/retrieval.py`, `src/dashboard/`, `src/reliability/` beyond the one tuple entry, `src/campaigns/service.py`, `recovery.py`, `results.py`, `briefing.py`, `csv_import.py`, `phone.py`, `health.py`, `dashboard.py`, `ingest.py`, every eval, every other test.

### New in Phase 13

- `server/src/campaigns/worker.py` — `CampaignWorker`, `WorkerMetrics`, `TickReport`, `install_signal_handlers`.
- `server/tests/test_worker.py` — the thirteenth deterministic script (213 checks; the last section needs PostgreSQL).

### Modified in Phase 13

- `server/campaign.py` — the `run` subcommand (`command_run`).
- `server/src/config.py` — `WorkerConfig` (`Config.worker`): `WORKER_POLL_SECS`, `WORKER_IDLE_SECS`, `WORKER_RECOVERY_INTERVAL_SECS`, `WORKER_DRAIN_SECS`, `WORKER_REPORT_SECS`, `WORKER_AUTO_COMPLETE`.
- `server/src/campaigns/store.py` — `QueueOutlook`, `reserve_membership` (and `_reserve` taking a pinned membership and the limit waiver), `unreserve_attempt`, `queue_outlook`, `sweep_memberships`, `list_campaigns(status=)`.
- `server/src/campaigns/service.py` — `clock` and `now()`, `reserve_membership`, `defer`, `unreserve`, `check_callable(ignore_attempt_limit=)` and the last-attempt fix, `record_outcome` acting only on final statuses and leaving a closed membership alone.
- `server/src/campaigns/dialer.py` — `dial_membership`, `dial(ignore_attempt_limit=)`, the fresh prospect read before `check_callable`, deferral on the prospect's window (`DialResult.deferred`), the duplicate placement released.
- `server/src/campaigns/recovery.py` — `_release_unplaced` hands the reservation back through `service.unreserve` before falling back to the close.
- `server/src/campaigns/__init__.py` — the exports.
- `server/src/reliability/guardrails.py` — `CallingWindow.clock`.
- `server/tests/test_reliability.py` — `FakeService.unreserve` (returns False) and `check_callable(..., ignore_attempt_limit=)`, so its fakes match the widened surface; no assertion changed.
- `server/.env.example` — the Phase 13 section. `README.md`, this file.

### Untouched by Phase 13

`bot.py`, `call.py`, `health.py`, `dashboard.py`, `ingest.py`, `src/telephony/`, `src/conversation/`, `src/actions/`, `src/scheduling/`, `src/voice_quality.py`, `src/voicemail.py`, `src/turns.py`, `src/services.py`, `src/metrics.py`, `src/resilience.py`, `src/diagnostics.py`, `src/prompts.py`, `src/retrieval.py`, `src/dashboard/`, `src/reliability/` beyond the one field above, `src/campaigns/models.py`, `results.py`, `briefing.py`, `csv_import.py`, `phone.py`, every eval, every other test.

### New in Phase 12

- `server/src/voice_quality.py` — `TurnMonitor`, `TurnRecord`, `BargeIn`, the report on disk (`write_call_report` / `load_call_report` / `report_path`).
- `server/src/voicemail.py` — `VoicemailDetector`, `VoicemailHandler`, `VoicemailVerdict`, `normalize_answered_by`, `machine_answered`, `DEFAULT_VOICEMAIL_PHRASES`.
- `server/tests/test_voice_quality.py` — the twelfth deterministic script.
- `server/tests/phone_drill.py` — seven scripted callers over the carrier's wire protocol.
- `server/tests/live_call.py` — one real call, checked on both sides.
- `server/evals/short_answers.yaml`, `rapid_speech.yaml`, `overlap.yaml`.

### Modified in Phase 12

- `server/bot.py` — the monitor observer, the voicemail handler and carrier poll, `_drain_stale_audio`, process warm-up in `_preflight`, the structured `telephony.line_closed` / `telephony.failure` lines, the noise resume, `on_user_turn_stop_timeout`, the report at teardown, `quality=` on `finish`.
- `server/src/config.py` — `VoiceQualityConfig` (`Config.voice_quality`), `TelephonyConfig.machine_detection` / `amd_poll_secs` / `amd_window_secs`, `describe_voice_quality`.
- `server/src/metrics.py` — per-response `records`, `summary()`, `_Series.to_dict`, the `k=v` latency line.
- `server/src/prompts.py` — `NOISE_RESUME_INSTRUCTION`, in `TURN_INSTRUCTIONS`.
- `server/src/resilience.py` — `SilenceHandler.closed_call`.
- `server/src/services.py` — `warm_up_llm_module`.
- `server/src/embeddings.py` — `shared_embedder`.
- `server/src/telephony/base.py` — `CallRequest.machine_detection`, `CallSnapshot.answered_by` / `machine_answered`.
- `server/src/telephony/twilio.py` — the `MachineDetection` / `AsyncAmd` parameters, `answered_by` read.
- `server/src/campaigns/models.py` — `CallAttemptStatus.VOICEMAIL`, `FINAL_STATUS_SQL`, `should_retry`.
- `server/src/campaigns/store.py` — `FINAL_STATUS_SQL` in both `ended_at` stamps, `voicemail` in `attempt_counts`.
- `server/src/campaigns/results.py` — `Disposition.VOICEMAIL`, `voicemail_detected`, `attempt_status_for`, the builder's voicemail block, the validator's voicemail exceptions, the summary text.
- `server/src/campaigns/dialer.py` — `machine_detection`, `machine_status` on refresh.
- `server/src/campaigns/recovery.py` — the same mapping on adoption.
- `server/src/conversation/conversation.py` — `note_voicemail`, `quality` / `voicemail` on the outcome.
- `server/src/dashboard/stats.py` — the `VOICEMAIL` row and tone.
- `server/call.py` — `--machine-detection`, `--hang-up-machines`, `call.answered_by`.
- `server/campaign.py` — passes `machine_detection` to the dialer.
- `server/evals/suite.yaml`, `server/evals/README.md`, `server/.env.example`, `README.md`, this file.

### Untouched by Phase 12

`src/turns.py`, `src/services.py`'s factories (the STT, LLM and TTS choices), `src/diagnostics.py`, `src/retrieval.py`, `src/conversation/` beyond the two additions above, `src/actions/`, `src/scheduling/`, `src/reliability/`, `src/telephony/session.py` and `transport.py`, `tests/fake_carrier.py` (subclassed, not edited).

### New in Phase 11

| Path | Purpose |
|---|---|
| `server/src/reliability/usage.py` | What a call consumed and what it cost. Units measured, prices configured, unreported stages named |
| `server/scripts/benchmark_db.py` | Seeds a throwaway schema at scale and times every query. The before/after column |
| `server/tests/test_performance.py` | 53 checks: usage accounting, cost honesty, pooling, the reservation race, the cache |

### Modified in Phase 11

| Path | What changed |
|---|---|
| `server/src/campaigns/store.py` | `connect(pool=...)` and `owns_pool`; `usage`/`cost_usd` columns; `save_call_usage`; `attempt_counts` folds in usage and degrades without the columns; `reserve_next_call(max_concurrent=)` counts inside the transaction |
| `server/src/knowledge_store.py` | `connect(pool=...)`, `owns_pool`, and a `pool` property so another store can borrow it |
| `server/src/campaigns/service.py` | `next_call(max_concurrent=)` |
| `server/src/campaigns/dialer.py` | Passes the concurrency limit into the reservation as well as checking it first |
| `server/src/campaigns/briefing.py` | `open_briefing(pool=...)`; `_store_usage` writes what the call consumed |
| `server/src/conversation/conversation.py` | `finish(usage=, cost=)`; the outcome carries both |
| `server/src/dashboard/stats.py` | The eight reads run concurrently; a usage-and-cost strip |
| `server/src/dashboard/web.py` | `_SnapshotCache` (TTL + lock); the pool sized for one page load; `read_ms` on the payload |
| `server/src/dashboard/page.py` | The usage strip, and the read time in the header |
| `server/src/config.py` | `CostConfig`, `Config.shares_database` |
| `server/bot.py` | The shared pool, the `UsageObserver`, usage through `finish`, the `USAGE`/`COST` lines, `_report_scale` |
| `server/src/reliability/__init__.py` | Exports the usage module |
| `README.md`, `HANDOFF.md`, `server/.env.example` | Phase 11 |

### Untouched by Phase 11

`call.py`, `campaign.py`, `health.py`, `ingest.py`, `dashboard.py`, every
`src/conversation/` module except one method signature, `src/actions/`,
`src/scheduling/`, `src/telephony/`, `src/services.py`, `src/turns.py`,
`src/retrieval.py`, `src/metrics.py`, `src/prompts.py`, `src/embeddings.py`,
every eval scenario.

**The voice pipeline, the providers and the models were not changed at all** —
which was the instruction, and which the measurement supported: the dominant
latency is turn detection at a vendor default, and the dominant cost is prompt
size rather than per-token price.

### New in Phase 10

| Path | Purpose |
|---|---|
| `server/src/dashboard/__init__.py` | The package's contract: reads `campaigns/`, nothing reads it, adds no data |
| `server/src/dashboard/stats.py` | What each number means and its footnote. Counts nothing itself |
| `server/src/dashboard/page.py` | The document: one HTML file, no build step, no CDN |
| `server/src/dashboard/web.py` | The routes — a page, a JSON endpoint, a ping. All reads |
| `server/dashboard.py` | The CLI. `--once` prints the JSON and starts no server |
| `server/tests/test_dashboard.py` | 60+ checks: the aggregates in real SQL, the footnotes, the degraded database, and that no route writes |

### Modified in Phase 10

| Path | What changed |
|---|---|
| `server/src/campaigns/store.py` | A "Reporting" section: `prospect_counts`, `attempt_counts`, `result_counts`, `disposition_counts`, `meeting_counts`, `callback_counts`, `campaign_overview`, `campaign_result_counts`, `recent_call_rows`, `results_for_attempts`. All read-only |
| `server/src/campaigns/models.py` | `REACHED_STATUS_SQL`, so "answered" means the same thing in SQL as `reached_person` does in Python |
| `README.md`, `HANDOFF.md` | Phase 10 |

### Untouched by Phase 10

`bot.py`, `call.py`, `campaign.py`, `health.py`, `ingest.py`, every
`src/conversation/` module, `src/reliability/`, `src/actions/`,
`src/scheduling/`, `src/telephony/`, `src/services.py`, `src/turns.py`,
`src/retrieval.py`, `src/config.py`, `.env.example`, every eval scenario.

The pipeline, the telephony layer, STT, LLM, TTS and the RAG stage were not
touched at all, and no new dependency was added: FastAPI and uvicorn arrive
with Pipecat. The dashboard introduced **no new settings** — it reads
`DATABASE_URL` and `CALENDAR_TIMEZONE`, and its port is a command-line flag.

### New in Phase 9

| Path | Purpose |
|---|---|
| `server/src/reliability/__init__.py` | The package's contract: the five duplicate-call mechanisms, and why nothing here imports `campaigns` |
| `server/src/reliability/retry.py` | `RetryPolicy`, the three verdicts, `call_with_retry`, `guarded`, the presets |
| `server/src/reliability/idempotency.py` | What makes two requests the same call. Derived keys |
| `server/src/reliability/guardrails.py` | `CallingWindow`, `PacingLimiter`, `CampaignGuards`, `Decision`, duration |
| `server/src/reliability/supervisor.py` | In-call failure handling: service failures, a stalled inference, the duration ceiling |
| `server/src/reliability/health.py` | Every dependency, probed cheaply. No call, no inference, no audio |
| `server/src/reliability/observability.py` | Call ids on every log line; credential scrubbing; JSON output |
| `server/src/campaigns/recovery.py` | What a restart does about calls that were in flight. Never dials |
| `server/health.py` | The health CLI. Exit 0/1/2 |
| `server/tests/test_reliability.py` | 150+ checks: injected failures, the five mechanisms one at a time |

### Modified in Phase 9

| Path | What changed |
|---|---|
| `server/src/campaigns/models.py` | `CallAttemptStatus.UNRESOLVED` (live, recoverable); `LIVE_STATUS_SQL`; `may_advance`; `CallAttempt.idempotency_key` and `.placement_started_at` |
| `server/src/campaigns/store.py` | `idempotency_key` + `placement_started_at` columns and a partial unique index; the live-status index rebuilt for the new status; `reserve_next_call` takes a key and rolls back on a collision; `mark_attempt_placed` refuses a second call id; `apply_call_event` (idempotent, monotonic); `mark_placement_started`, `mark_attempt_unresolved`, `count_live_attempts`, `list_live_attempts`, `find_attempt_by_key` |
| `server/src/campaigns/dialer.py` | Guardrails before reserving; placement stamped before the request; `place_call` never retried; `_placement_classifier`; ambiguous → `UNRESOLVED`; a duplicate call id hangs the duplicate up; `refresh` made idempotent; structured logging |
| `server/src/campaigns/service.py` | `next_call` supplies the idempotency key; `record_outcome(write_result=)` |
| `server/src/telephony/base.py` | `find_recent_calls`, `check_credentials`, `CallSnapshot.created_at`, `TelephonyError.retryable` |
| `server/src/telephony/twilio.py` | Both new methods; a per-request timeout; a timeout is its own error branch; `date_created` parsed |
| `server/src/telephony/signalwire.py`, `__init__.py` | `timeout_secs` passed through |
| `server/src/config.py` | `ReliabilityConfig` (twelve settings), `describe_safety()` |
| `server/src/services.py` | `_apply_request_timeout`: the LLM client's own HTTP timeout |
| `server/src/prompts.py` | `MAX_DURATION_INSTRUCTION`, `SERVICE_TROUBLE_INSTRUCTION` |
| `server/bot.py` | `configure_logging`; the supervisor, its observer and its ending path; call context bound for the session; safety and retry policies at startup |
| `server/campaign.py` | `recover`; guardrails always supplied; recovery before `call`; `--no-recover`; pacing between calls; an ambiguous placement stops the run |
| `server/tests/test_telephony.py` | `StubSession` records params/timeout/headers and answers `get`; `_sent` helper |
| `server/tests/test_campaigns.py` | `check_phase9`: the SQL half of duplicate protection, event idempotency, recovery |
| `README.md`, `HANDOFF.md`, `server/.env.example` | Phase 9 |

### Untouched by Phase 9

`call.py`, `ingest.py`, every `src/conversation/` module, `src/actions/`,
`src/scheduling/`, `src/retrieval.py`, `src/knowledge_store.py`,
`src/embeddings.py`, `src/documents.py`, `src/turns.py`, `src/metrics.py`,
`src/resilience.py`, `src/diagnostics.py`, `src/campaigns/{phone,csv_import,briefing,results}.py`,
`src/telephony/{transport,session}.py`, every eval scenario.

The pipeline order did not change and no prompt sent to the model on an
ordinary turn changed — the two new instructions are only used when the
supervisor ends a call.

### New in Phase 8

| Path | Purpose |
|---|---|
| `server/src/campaigns/results.py` | `CallResult`, `Disposition`, `MeetingOutcome`, `CallbackOutcome`, `ResultSource`, `CallSummary`; the two builders, `derive_disposition`, `attempt_status_for`, `validate_call_result`, `extract_questions`; the CRM mapping table. Pure |
| `server/src/conversation/transcript.py` | `Transcript`, `TranscriptEntry`, `render_transcript` — what was said, verbatim |
| `server/tests/test_results.py` | 258 checks: the twelve required cases, disposition precedence, the attempt status from the path, the validator, the export |

### Modified in Phase 8

| Path | What changed |
|---|---|
| `server/src/conversation/conversation.py` | Owns a `Transcript`; `note_user_turn` records the turn; `note_agent_turn(text, interrupted=)`; `outcome()` adds `transcript`, `timezone`, `call_duration_secs`; `finish(call_duration_secs=)` |
| `server/src/conversation/__init__.py` | Exports the transcript module |
| `server/src/campaigns/store.py` | `call_results` table in the idempotent schema; `save_call_result` (validate, upsert with precedence), `get_call_result`, `list_call_results`; `_phase7` generalised to `_optional_table` |
| `server/src/campaigns/service.py` | `record_outcome(write_result=)` writes the carrier result on every final status; `record_carrier_result` |
| `server/src/campaigns/briefing.py` | The sink builds and stores the conversation result; the attempt status comes from `attempt_status_for` (the Phase 6 fix); the duration comes from the phone call |
| `server/src/campaigns/__init__.py` | Exports the results module and `RESULTS_TABLE` |
| `server/bot.py` | Hands the agent's words to `note_agent_turn`; hands the call's duration to `finish` |
| `server/campaign.py` | `results`, `result`, `rebuild-results` |
| `server/tests/fake_carrier.py` | `--prospect`, `--campaign`, `--attempt` |
| `server/tests/test_campaigns.py` | `check_phase8`: both writers, the upsert rule both ways, the readers, the sink end to end, refusals |
| `server/tests/test_conversation.py` | `check_transcript`; the harness's `finish` takes kwargs |
| `README.md`, `HANDOFF.md`, `server/evals/README.md` | Phase 8 |

### Untouched by Phase 8

`call.py`, `ingest.py`, every `src/telephony/` module, `src/actions/`,
`src/scheduling/`, `src/turns.py`, `src/metrics.py`, `src/resilience.py`,
`src/diagnostics.py`, `src/prompts.py`, `src/retrieval.py`,
`src/knowledge_store.py`, `src/embeddings.py`, `src/documents.py`,
`src/config.py`, `src/services.py`, `src/conversation/{states,qualification,brief,playbook,signals,results,actions,timeparse,toolkit,tools,director,sources,sink}.py`,
`src/campaigns/{models,phone,csv_import,dialer}.py`, every eval scenario,
`.env.example` (no new settings).

The pipeline was not touched. The prompt was not touched — no token was added
to any request. The tool list is the same twelve.

### New in Phase 7

| Path | Purpose |
|---|---|
| `server/src/conversation/results.py` | `ToolResult` — the one result shape — and the closed error-code vocabulary |
| `server/src/conversation/actions.py` | `ActionBackend` Protocol, `ActionOutcome`, `Capabilities`, `NullActionBackend` |
| `server/src/conversation/toolkit.py` | `strict_tool`: schema from the signature, argument validation, guard, audit log; `AuditContext` |
| `server/src/conversation/timeparse.py` | Strict ISO 8601 readers, spoken labels, the time-now sentence |
| `server/src/actions/service.py` | `ActionService`: the backend — validation, authorisation and I/O for every action |
| `server/src/actions/__init__.py` | `open_actions`: wires the backend from what the session has |
| `server/src/scheduling/base.py` | `CalendarProvider`, `Slot`, `Attendee`, `Booking`, the calendar errors |
| `server/src/scheduling/hours.py` | `BusinessHours`, parsed and validated (imported by `config.py`) |
| `server/src/scheduling/local.py` | The default calendar: business hours on a grid minus the `meetings` table |
| `server/src/scheduling/calcom.py` | Cal.com API v2 — the only file that knows Cal.com exists. Untested live |
| `server/src/scheduling/__init__.py` | `make_calendar`: the one place that picks a provider |
| `server/tests/test_actions.py` | 200+ checks: every tool through the real boundary against stubbed calendar, store, retriever, carrier |
| `server/tests/test_scheduling.py` | The local calendar's arithmetic and timezone seam; Cal.com against a stub HTTP session |

### Modified in Phase 7

| Path | What changed |
|---|---|
| `server/src/conversation/tools.py` | Rewritten: twelve tools returning `ToolResult`, wrapped by `strict_tool`; `do_not_call`→`mark_do_not_call`, `request_callback`→`schedule_callback`; docstrings cut to schema size |
| `server/src/conversation/conversation.py` | `actions`, `timezone`, `now`, `audit`; the five action methods with their rules; `request_meeting` refuses after a no; `_override_for` picks transfer/callback variants; outcome carries `actions` |
| `server/src/conversation/playbook.py` | Capability-driven system instruction, the time block, the meeting line on every selling stage, callback overrides, twenty new `TOOL_GUIDANCE` entries; the tools section halved |
| `server/src/conversation/qualification.py` | `NextAction.MEETING_BOOKED`, `TRANSFERRED`; `meeting_booked/start/reference`, `callback_scheduled_for`, `transferred` |
| `server/src/conversation/__init__.py` | Exports the new modules |
| `server/src/retrieval.py` | Public `search()` — the same path, no gate — for the tool |
| `server/src/telephony/base.py` | `TransferError`, `is_e164`, `transfer_call` on the provider contract, `build_transfer_twiml` |
| `server/src/telephony/twilio.py` | `transfer_call` via the live-call TwiML update; error 21220 translated |
| `server/src/campaigns/models.py` | `CallbackStatus`, `ScheduledCallback`, `MeetingStatus`, `Meeting` |
| `server/src/campaigns/store.py` | `callbacks` and `meetings` tables in the idempotent schema; their methods; `reopen_membership`; DNC cancels pending callbacks |
| `server/src/campaigns/briefing.py` | `Briefing.store`; the sink reopens the membership for a scheduled callback |
| `server/src/campaigns/dialer.py` | Marks a prospect's pending callbacks `PLACED` when their call is placed |
| `server/src/config.py` | `CalendarConfig`, `TELEPHONY_TRANSFER_NUMBER`, `CALLBACK_MAX_DAYS_AHEAD`, `LLM_MAX_OUTPUT_TOKENS` |
| `server/src/services.py` | `make_llm` always sends the output cap, finding the field on the provider's `Settings` dataclass |
| `server/bot.py` | Builds the action backend per session, hands it and an `AuditContext` to the conversation, closes it; startup report for actions |
| `server/campaign.py` | `callbacks`, `cancel-callback`, `meetings` |
| `server/pyproject.toml` | `tzdata` |
| `server/.env.example` | `ACTIONS` section, `TELEPHONY_TRANSFER_NUMBER`, `LLM_MAX_OUTPUT_TOKENS` |
| `server/tests/test_conversation.py` | The harness invokes `FunctionSchema` handlers; renamed tools; the result shape; new honesty wording |
| `server/tests/test_telephony.py` | Transfer TwiML, `transfer_call`, `is_e164` |
| `server/tests/test_campaigns.py` | Stub provider gained `transfer_call`; SQL checks for callbacks, meetings, reopening |
| `server/evals/sales/{meeting,callback,interested,do_not_call_implicit,wants_human}.yaml` | The booking flow, renamed tools, `within_ms` on tool turns |
| `server/evals/README.md`, `README.md`, `HANDOFF.md` | Phase 7 |

### Untouched by Phase 7

`call.py`, `ingest.py`, `src/turns.py`, `src/metrics.py`, `src/resilience.py`,
`src/diagnostics.py`, `src/prompts.py`, `src/knowledge_store.py`,
`src/embeddings.py`, `src/documents.py`, `src/telephony/{signalwire,transport,session}.py`,
`src/campaigns/{phone,csv_import,service}.py`, `src/conversation/{states,brief,signals,director,sources,sink}.py`,
`tests/{test_knowledge,test_realtime,fake_carrier,fake_browser}.py`, every audio-suite scenario.

The realtime pipeline was not touched: the tools changed shape, the context
advertises them the same way, and the director and retriever are as they were.

### New in Phase 6

| Path | Purpose |
|---|---|
| `server/src/conversation/states.py` | The ten states and the transition table. Two of its rows are promises |
| `server/src/conversation/qualification.py` | What the call learned. Unknown is a value; the status is derived |
| `server/src/conversation/brief.py` | Who is being called and on whose behalf. Names its own gaps |
| `server/src/conversation/playbook.py` | Every word the model is told. No logic |
| `server/src/conversation/signals.py` | The deterministic floor. Only do-not-call forces anything |
| `server/src/conversation/tools.py` | The eight tools, as Pipecat direct functions |
| `server/src/conversation/conversation.py` | `SalesConversation`: the rules, and the only thing that changes state |
| `server/src/conversation/director.py` | The pipeline stage. Attaches guidance, runs the detectors, owns no state |
| `server/src/conversation/sources.py` | Media-stream ids to a `CallBrief`; the `PARAM_*` names live here |
| `server/src/conversation/sink.py` | Where consequences leave the package. Two Protocols, plain data |
| `server/src/campaigns/briefing.py` | The bridge: campaigns ↔ conversation. The Phase 6 analogue of `dialer.py` |
| `server/tests/test_conversation.py` | 249 checks, including all fourteen required scenarios end to end |
| `server/evals/sales/` | 15 scenarios, a text-mode judge block, and their own suite manifest |

### Modified in Phase 6

| Path | What changed |
|---|---|
| `server/bot.py` | Resolves the brief, builds the conversation, passes the system instruction to `make_llm`, advertises the tools on the context, puts the director in the pipeline, opens with the composed greeting, writes the outcome on teardown, reports the sales settings at startup |
| `server/src/config.py` | `SalesConfig` (fourteen settings, none required, none guessed) and `KB_RETRIEVAL_MODE` |
| `server/src/services.py` | `make_llm` takes a per-session `system_instruction` |
| `server/src/prompts.py` | `INSTRUCTION_PREFIX`, `is_turn_instruction`, `is_injected_block` — one place that knows what the application injected |
| `server/src/retrieval.py` | The gate (`looks_like_information_request`), and recognising Phase 6's composed instructions |
| `server/src/campaigns/store.py` | `conversation_data` column, `save_conversation_data`, a reader that tolerates the column's absence |
| `server/src/campaigns/models.py` | `CallAttempt.conversation_data` |
| `server/src/campaigns/dialer.py` | Imports the `PARAM_*` names from the reader; `refresh` will not overwrite a conversation outcome |
| `server/src/campaigns/__init__.py` | Exports the briefing bridge |
| `server/tests/test_knowledge.py` | The retrieval gate, both modes, and that composed guidance is never searched |
| `server/evals/conversation.yaml`, `voice_quality.yaml` | Rewritten for a sales agent; the Phase 3/Phase 2 collision is resolved |
| `server/evals/README.md` | The two suites, and why one of them is text mode |
| `server/.env.example` | `THE SALES CONVERSATION` section, `KB_RETRIEVAL_MODE` |
| `server/.env` | The sales settings, matching the sample knowledge base so the suite runs as-is |
| `README.md`, `HANDOFF.md` | Phase 6 |

### Untouched by Phase 6

`call.py`, `campaign.py`, `ingest.py`, every `src/telephony/` module, `src/turns.py`,
`src/metrics.py`, `src/resilience.py`, `src/diagnostics.py`, `src/knowledge_store.py`,
`src/embeddings.py`, `src/documents.py`, `src/campaigns/{phone,csv_import,service}.py`,
`tests/{test_telephony,test_realtime,test_campaigns,fake_carrier,fake_browser}.py`,
`evals/{barge_in,silence,knowledge_known,knowledge_unknown,smoke_text}.yaml`.

The realtime architecture was not replaced. The conversation layer is a system
instruction, a tool list and one processor.

### New in Phase 5

| Path | Purpose |
|---|---|
| `server/src/campaigns/models.py` | Prospect, Campaign, CampaignProspect, CallAttempt and their statuses. No I/O |
| `server/src/campaigns/phone.py` | E.164 normalisation that refuses to guess a country |
| `server/src/campaigns/csv_import.py` | Header aliasing and row validation; pure, no database |
| `server/src/campaigns/store.py` | The four tables, their indexes, and the queue's transaction |
| `server/src/campaigns/service.py` | The rules: who may be called, what an outcome means |
| `server/src/campaigns/dialer.py` | The only module importing both campaigns and telephony |
| `server/src/campaigns/__init__.py` | Public surface |
| `server/campaign.py` | The CLI: init, import, create, add, start, next, call, status, dnc |
| `server/tests/test_campaigns.py` | 160 checks — pure logic always, SQL in a throwaway schema |

### Modified in Phase 5

| Path | What changed |
|---|---|
| `server/src/config.py` | `DATABASE_URL` (defaults to `KB_DATABASE_URL`), `DEFAULT_PHONE_REGION`, `CAMPAIGN_MAX_ATTEMPTS`, `CAMPAIGN_RETRY_MINUTES`, and a `_region` validator |
| `server/pyproject.toml` | Added `phonenumbers` |
| `server/.env.example` | New PROSPECTS AND CAMPAIGNS section |
| `README.md`, `HANDOFF.md` | Phase 5 |

### Untouched by Phase 5

`bot.py`, `call.py`, `ingest.py`, every `src/telephony/` module, `src/services.py`,
`src/turns.py`, `src/metrics.py`, `src/resilience.py`, `src/diagnostics.py`,
`src/prompts.py`, `src/retrieval.py`, `src/knowledge_store.py`, everything in
`evals/`. The live conversation pipeline was not modified at all — verified after
the fact by booting the bot and driving a simulated phone call through it.

### Modified in Phase 4

| Path | What changed |
|---|---|
| `server/bot.py` | Telephony transport params for four carriers; a provider-built transport when one is configured, falling back to `create_transport`; `CallSession` wiring; greeting moved to a shared `greet_once` so a phone call triggers it from the media stream; zero disconnect grace on a call; call summary; telephony startup report |
| `server/src/config.py` | `TelephonyConfig` (with its own `from_env`, so `call.py` needs no Deepgram/Groq/Cartesia/PostgreSQL configuration); `SUPPORTED_TELEPHONY` and the per-carrier settings table; `has_credentials` for the receive path; `describe()` now reports the phone setup |
| `server/src/resilience.py` | `ConnectionGuard` takes a `grace_secs` override; new `PeerWatchdog` |
| `server/src/turns.py` | `make_mute_strategies`, wired into the aggregator as `user_mute_strategies` |
| `server/pyproject.toml` | Added the `websocket` extra |
| `server/.env.example` | New TELEPHONY section |
| `README.md` | Phase 3 and Phase 4 sections; architecture tree; testing |
| `HANDOFF.md` | This |

### Untouched by Phase 4

`src/services.py`, `src/turns.py`, `src/metrics.py`, `src/diagnostics.py`, `src/prompts.py`, `src/retrieval.py`, `src/knowledge_store.py`, `src/embeddings.py`, `src/documents.py`, `ingest.py`, everything in `evals/`, `AGENTS.md`, `CLAUDE.md`, `.gitignore`.

That list is the point of the phase: the phone did not reach into the voice agent.

---

## 9. Current architecture and workflow

### Pipeline

```
transport.input()              (webrtc | twilio/telnyx/plivo/exotel | eval)
  → Deepgram Flux STT          (transcript + end-of-turn, one websocket)
  → user context aggregator    (VAD, turn strategies, idle detection)
  → KnowledgeRetriever         (gated: embed the turn, search, attach passages)
  → ConversationDirector       (run the detectors, attach this turn's guidance)
  → Groq LLM                   (streaming tokens; twelve tools advertised, each behind strict_tool)
  → Cartesia TTS               (streaming audio, sentence-aggregated)
  → transport.output()
  → assistant context aggregator
```

`transport.input()` is the only line that differs between a browser call and a phone call, and even that is chosen by Pipecat from the runner's arguments rather than by anything here. The retriever drops out entirely when `KB_ENABLED=false`, and the director when `SALES_MODE=false` — with both off this is exactly the Phase 2 agent.

**The director is after the retriever, and that order is load-bearing**: the retriever builds its search query from the last user message, so a guidance block appended first would become the query. Both processors append to a *copy* of the context and carry `tools` and `tool_choice` through it.

**The turn after a tool call does not pass through either of them.** The assistant aggregator pushes its context frame *upstream*, which reaches the LLM without traversing the retriever or the director — so the stage block is not in front of the model on that turn. That is why every tool result carries its own `guidance` string: the tool result is the only thing that can steer the sentence the model is about to say.

### How an action happens (Phase 7)

```
model emits a tool call
  → Pipecat resolves the FunctionSchema's handler          (auto-registered from the context's tools)
  → strict_tool: validate arguments against the schema      (missing/typed → invalid_arguments; unknown keys dropped)
  → the tool function (tools.py)                            (no logic of its own)
  → SalesConversation.<action>()                            (state rules: no booking after a no; parse the time; remember offered slots)
  → ActionBackend.<action>()  = ActionService               (world rules: horizon, prospect row, phone call; then the store / calendar / carrier)
  → ActionOutcome  → ToolResult(+guidance)                  (success only if the durable write happened)
  → strict_tool logs one TOOL line and records the action   → result_callback → the model
```

Every hop can say no, and each one says no about the thing it can see. The
model reads `success` and `guidance` and nothing else.

The assistant aggregator sits **after** `transport.output()` on purpose: it records what the caller actually heard, so an interrupted reply is stored truncated at the point it was cut off.

### How a call is placed safely (Phase 9)

```
campaign.py call
  → AttemptRecovery.run()                    resolve anything left live by an earlier run. Never dials
  → guards.check(live_calls)                 calling hours, concurrency, pacing — BEFORE reserving
  → service.next_call()                      reserve in one transaction, stamped with an idempotency key
  → service.check_callable()                 re-check against fresh rows (a DNC that landed since)
  → guards.window.check(prospect timezone)   the prospect's own hours
  → store.mark_placement_started()           stamped BEFORE the request, so a crash here is recoverable
  → call_with_retry(place_call, NEVER_RETRY) exactly one attempt, with a timeout
      ├─ CallSetupError   → FATAL      → release the attempt, record FAILED. No phone rang
      ├─ ProviderUnavail. → AMBIGUOUS  → mark_attempt_unresolved(). Attempt stays LIVE, prospect blocked
      └─ success          → mark_attempt_placed(). A second, different call id is refused and hung up
```

and later, from any process:

```
campaign.py recover
  → store.list_live_attempts(older_than)     nothing younger than RECOVERY_MIN_AGE_SECS
  → has a call id?      → fetch_call()       → apply_call_event() (monotonic, idempotent)
  → UNRESOLVED/placing? → find_recent_calls() → adopt the call, or close as failed. NEVER dials
  → never placed?       → release it
```

### How a call is traced (Phase 22)

```
campaign.py run                       trace=9f3c… born in dialer.dial()
  ├─ store.set_attempt_trace          call_attempts.trace_id = 9f3c…      (store.op line, trace=9f3c…)
  ├─ provider.place_call              <Parameter name="trace_id">        (call.placed, trace=9f3c…)
  └─ worker follows it                call.started … call.completed       (trace=9f3c… worker=host-pid-abc)
bot.py                                trace_from_runner_args → 9f3c…      (every line of the session)
  ├─ tools                            ACTION | … under the same context   (aiva_calendar_operations_total …)
  └─ sink → store.save_call_result    store.op, RESULT |                  (trace=9f3c…)
webhooks (bot-mounted or standalone)  find_attempt_by_call_id → row.trace_id  (webhook.applied, trace=9f3c…)
campaign.py crm-sync                  get_attempt → row.trace_id          (crm.synced, trace=9f3c…)
automation.py                         payload["call"]["trace_id"]         (automation.delivered, trace=9f3c…; n8n sees it)
```

With `LOG_FORMAT=json` every line also carries `component` and `pid`, so
`jq 'select(.trace=="9f3c…")'` over five processes' shipped logs is the
call in order. The numbers each hop records are in
`src/monitoring/instruments.py`; `/metrics` on any server serves them,
and `/readyz` says whether that server could take work now.

### How the scheduler runs (Phase 13)

```
campaign.py run [campaign...]
  → worker.start()
      → AttemptRecovery.run()                 what an earlier run left live. Never dials
      → adopt every attempt with a call id    still up at the carrier; followed to its end
  → loop, each tick:
      → follow in-flight     get_attempt(); a final status — the bot's, or a webhook's (Phase 14) — ends it
                             else, if a call id and a poll is due: dialer.refresh() → apply_call_event (monotonic)
                             (due every tick until the carrier has pushed an event for the call,
                              then every WORKER_WEBHOOK_POLL_SECS — the safety net under the webhooks)
      → periodic recovery    when due; brought forward after an ambiguous placement
      → due callbacks        list_callbacks(due_before=now) → reopen the membership if closed
                             → dialer.dial_membership(ignore_attempt_limit) → guards → reserve_membership → dial
      → the queue            for each ACTIVE campaign, round robin, while capacity:
                             dialer.dial_next → guards (window, concurrency, pacing) → next_call → dial
                             ├─ placed      → follow it
                             ├─ blocked     → sleep retry_after_secs; nothing else will pass either
                             ├─ deferred    → the prospect's own window; reservation handed back unspent
                             ├─ released    → FAILED with the reason (DNC landed, carrier refused)
                             ├─ ambiguous   → UNRESOLVED holds the slot; recovery brought forward
                             └─ nothing     → queue_outlook: finished? → sweep, COMPLETED
                                                             retry later? → sleep until it
                                                             blocked by a live call? → poll
      → sleep                min(idle, poll if anything live, retry_after, next due, next recovery)
  → Ctrl+C once: stop placing, follow in-flight to the end (≤ WORKER_DRAIN_SECS); twice: stop now
  → worker.finish()          worker.metrics, worker.stopped; anything left live is named for `recover`
```

The bot is a separate process throughout: it answers the carrier's stream,
holds the conversation, and writes the conversation's outcome through the
Phase 8 sink exactly as before. The worker sees that write on its next poll.

### How a carrier event lands (Phase 14)

```
the carrier  --POST-->  /webhooks/telephony        (the bot's runner app, or webhooks.py)
  → WebhookRequest(url=the CONFIGURED public URL, headers, form)
  → provider.verify_webhook      HMAC-SHA1 over url + sorted fields, keyed by the carrier's secret;
                                 the URL with and without :443; AccountSid must be ours
                                 ├─ no secret / no header / mismatch / other account → 403, nothing read
  → provider.parse_webhook       CallSid, CallStatus, SequenceNumber, Timestamp, CallDuration, AnsweredBy…
                                 ├─ no call id, or neither a status nor a verdict → 400
  → store.record_webhook_event   INSERT … ON CONFLICT (event_key) DO NOTHING
                                 ├─ lost the insert → 200 duplicate; the attempt is never read
  → store.find_attempt_by_call_id
                                 ├─ none → 200 unmatched, recorded (call.py, inbound, another number)
  → kind == amd?                 → 200 noted; the verdict waits on the ledger for the completion
  → map_call_status              ├─ UNKNOWN → 200 ignored
  → machine_status               a completion reads the ledger's verdict → VOICEMAIL if a machine answered
  → store.apply_call_event       FOR UPDATE + may_advance
                                 ├─ not applied (same status, backwards, already final) → 200 stale
  → service.record_outcome       membership, prospect, the carrier's call result — as after a poll
  → store.set_webhook_outcome    → 200 applied
  (CampaignStoreError anywhere → 503; the worker's poll still applies the outcome)
```

### How a result reaches the CRM (Phase 15)

```
campaign.py crm-sync            (a third process; the bot and the worker never call a CRM)
  → make_crm_provider(config.crm)                hubspot | …
  → loop, each pass:
      → store.claim_results_for_sync   one transaction: a PENDING crm_sync row for every result
                                       without one, then FOR UPDATE SKIP LOCKED over
                                       PENDING | due RETRY | stale SYNCING | SYNCED-but-result-updated
                                       → SYNCING, attempts + 1
      → provider.ensure_schema         once per run; refused → standard fields only, one warning
      → for each (result, row):
          build_call_sync(result, prospect, campaign, attempt)   the contact and the activity, neutral
          ├─ no phone and no email                → FAILED with the reason
          ├─ unanswered and CRM_SYNC_UNANSWERED=false → SKIPPED
          find_contact (retried read) → create_contact (one attempt) → the id RECORDED
          [attempts > 1: find_activity by key]    a create whose answer was lost, found before repeating
          update_activity | create_activity (one attempt) → the id RECORDED
          update_contact                          the ai_* properties: the latest call's facts
          ├─ ok                 → SYNCED, synced_at, result_updated_at = the result's updated_at
          ├─ unavailable/ambiguous → RETRY, next_attempt_at = retry_secs·2^n (≥ Retry-After), jittered;
          │                        after max_attempts → FAILED "(after N attempts)"
          ├─ rejected           → FAILED with the CRM's words
          └─ token rejected     → the pass stops; every claimed row → RETRY, no attempt spent
      → sleep                  0 if the batch was full, else CRM_SYNC_POLL_SECS
  campaign.py crm-status | crm-retry --result N | --all-failed
```

### How a transfer ends (Phase 16)

```
the model calls transfer_to_human
  → SalesConversation (unchanged): not after a DNC; only if the session can transfer
  → ActionService.transfer_to_human
      → provider.transfer_call(call_id, TELEPHONY_TRANSFER_NUMBER,
                               action_url=<the webhook receiver>, timeout_secs=TELEPHONY_TRANSFER_TIMEOUT_SECS)
          POST Calls/{id}.json  Twiml=<Dial action=… method="POST" timeout=… callerId=…>+92…</Dial>
          ├─ refused / unreachable → transfer_failed / external_error; nothing recorded; the agent offers a callback
      → store.add_transfer(REQUESTED)     guarded, 5 s; never fails or holds the transfer
  → the carrier applies the TwiML: the media stream to the bot closes; the colleague's phone rings
  → the leg ends → the carrier POSTs DialCallStatus/DialCallSid/DialCallDuration to the receiver
      → verify (403 if forged) → parse: kind=transfer → ledger → find the attempt (or not)
      → store.complete_transfer(ANSWERED | NO_ANSWER | BUSY | FAILED | CANCELED, duration)
      → answer with TwiML:  answered → <Hangup/>      not answered → <Say>nobody is available…</Say><Hangup/>
  → campaign.py transfers        REQUESTED → the outcome; `--attempt N` for one call
(without a receiver: <Dial> then the inline <Say> fallback, as Phase 7; the row stays REQUESTED)
```

### How a booking is made (Phase 16)

```
check_calendar_availability → provider.available_slots       Cal.com GET /slots | local: hours minus meetings
book_meeting (only a slot that was offered)
  → provider.book(start, attendee)
      Cal.com: POST /bookings, CALCOM_TIMEOUT_SECS
        ├─ 4xx "not available / no_available_users / out_of_bounds" → slot_taken; the agent offers another time
        ├─ timeout / 5xx → GET /bookings?attendeeEmail&eventTypeId&afterStart&beforeEnd
        │       ├─ found  → adopted, with its uid; never POSTed twice
        │       └─ absent → external_error, "nothing was booked"
        └─ 201 → Booking(reference=uid)
      local: the check (hours, grid, notice, busy) → Booking(reference=None)
  → store.add_meeting                                      the meetings row; Cal.com's uid as `reference`
      └─ ExclusionViolation (a live local booking overlaps) → MeetingConflictError → slot_taken
  → success only now; the model is told the label, the provider and the reference
```

### How n8n reaches it, and hears back (Phase 17)

```
n8n  --POST /api/v1/calls  {phone | prospect_id, campaign, scheduled_at?}-->  automation.py
        Authorization: Bearer <AUTOMATION_API_KEYS>       ├─ no / wrong key → 401, nothing read
        Idempotency-Key: <n8n run id>                     ├─ key seen with this body → the stored answer, Idempotent-Replayed: true
                                                          ├─ key seen with another body → 422
  → prospect (by id, or by number normalised as an import does)   ├─ unknown → 404; DO_NOT_CALL → 409; no number → 409
  → campaign (by id or name)                                      ├─ unknown → 404
  → find_membership | add_to_campaign                             (a prospect not in the campaign joins it)
  → store.schedule_callback(due now | scheduled_at)               one pending callback per prospect: a second ask moves it
  → 202 {queued, callback, membership, replaced, dialled_by, warnings}   — nothing dialled
  → store.save_api_request                                         the answer, for a retry

campaign.py run  (Phase 13, unchanged)
  → each tick: list_callbacks(due_before=now) → reopen the membership if closed → dial_membership → the carrier
  → the bot answers the stream, holds the call, writes the result (Phase 8)

automation.py  (the deliverer, in the same process as the API, or `--once` from cron)
  → store.claim_automation_events(kinds)      one transaction:
      INSERT … SELECT for each kind            call.completed / lead.qualified from settled results,
        ON CONFLICT (event_key) DO NOTHING     call.updated from a result changed after delivery,
                                               meeting.booked, callback.scheduled, campaign.completed from their rows
      FOR UPDATE SKIP LOCKED                   PENDING | due RETRY | stale DELIVERING → DELIVERING, attempts + 1
  → build_payload                              prospect, campaign, call, result, transfers, meeting, callback — read now
  → POST <AUTOMATION_WEBHOOK_URL[_KIND]>       X-Aiva-Event, X-Aiva-Event-Id, X-Aiva-Delivery, X-Aiva-Timestamp,
                                               X-Aiva-Signature (HMAC-SHA256 over "t." + body), X-Aiva-Key
      ├─ 2xx                → DELIVERED (payload, target, status, the result's version kept)
      ├─ timeout / 5xx / 429 / 408 / 425 / 404 → RETRY, backoff ×2 from AUTOMATION_RETRY_SECS, Retry-After a floor;
      │                        after AUTOMATION_MAX_ATTEMPTS → FAILED "(after N attempts)"
      └─ other 4xx          → FAILED with the body's first line
  campaign.py events | events-retry --event N | --all-failed   (a reopened row gets a fresh budget)
```

### How a result is written (Phase 8)

```
the call ends (hang-up, end_call, idle timeout)
  → bot.py: conversation.finish(call_duration_secs=...)
  → SalesConversation.outcome()               (state path, record, actions, transcript, timezone)
  → ConversationSink.on_call_finished          (CampaignConversationSink, briefing.py)
      → save_conversation_data                 (the raw record, untouched)
      → record_outcome(status from the path)   (DO_NOT_CALL / CALLBACK_REQUESTED / NOT_INTERESTED, write_result=False)
      → build_conversation_result              (results.py: parse tolerantly, derive, summarise)
      → save_call_result                       (validate; upsert — a conversation result always wins)

the carrier reports a final status (dialer.refresh, or release)
  → CampaignService.record_outcome
      → build_carrier_result                   (everything the carrier cannot know is UNKNOWN)
      → save_call_result                       (upsert — never over a conversation result)
```

Both paths end in the same statement, and the statement holds the precedence.
`campaign.py rebuild-results` runs the first path again from
`conversation_data` for any finished attempt without a row.

### Module responsibilities

`bot.py` is wiring only — transport, pipeline, observers, event handlers. Every behavioural decision lives in a module beside it, so "why does it wait that long before answering" has one file to read.

| Module | Owns |
|---|---|
| `config.py` | Env parsing and validation. Fails fast, naming every problem at once |
| `services.py` | STT/LLM/TTS factories. Provider swap = `.env` + one branch |
| `turns.py` | VAD, turn strategies, post-barge-in context repair |
| `metrics.py` | Latency series, session summary, error sink |
| `resilience.py` | Silence escalation, disconnect grace window |
| `diagnostics.py` | Turn-cycle tracing, barge-in logging, error frames |
| `prompts.py` | The general-assistant prompt, turn instructions, interruption marker, the knowledge block, and the rule for recognising the application's own injected text |
| `retrieval.py` | What the agent is allowed to know, fetched per turn, and when to bother |
| `conversation/` | The sales call: its ten states, what it learned, who it is calling, every word the model is told, and the tool boundary |
| `actions/` | The backend behind the action tools: validates, authorises, does the I/O, never raises. Knows the store, the calendar, the retriever and the carrier |
| `scheduling/` | The calendar behind `book_meeting`: local business hours, or Cal.com |
| `campaigns/briefing.py` | The bridge between the two: ids in, brief out; facts out, rows written; a scheduled callback back into the queue |
| `campaigns/worker.py` | The scheduler (Phase 13): the loop that places calls unattended — recovery, due callbacks, the queue, calls followed to their end, campaigns closed. Decides only when to ask |
| `campaigns/webhooks.py` | The carrier's pushed events (Phase 14): verified, decoded, recorded once, applied through the same monotonic write a poll uses. The HTTP route, mounted on the bot or served alone |
| `crm/` | The CRM (Phase 15): the neutral contract, the mapping from a result, the HubSpot adapter, and the syncer that files each result once from its own process. Reads `campaigns/`; nothing reads it back |
| `automation/` | n8n (Phase 17): the authenticated API that turns a request into rows the scheduler acts on, and the outbox that turns rows into signed events delivered once. Reads `campaigns/` and `config`; nothing reads it back |
| `knowledge_store.py` / `embeddings.py` / `documents.py` | The knowledge base underneath it |
| `telephony/` | Placing calls, call outcomes, the bot's view of the call it is on, and — Phase 14 — how each carrier signs and phrases the events it pushes |

### How a phone call actually happens

```
call.py  --REST-->  Twilio  --dials-->  the person
                       |
                       '--websocket-->  bot.py's /ws     (audio, both ways)
```

1. `call.py` POSTs to Twilio's Calls API with `To`, `From`, a ring timeout, and inline TwiML that says "when this is answered, open a bidirectional stream to `wss://<public>/ws`".
2. Twilio dials. `call.py` polls the call resource once a second and logs each transition.
3. On answer, Twilio opens a websocket to the dev runner's `/ws`. Pipecat reads the first two messages, recognises Twilio, builds a `TwilioFrameSerializer`, and calls `bot(runner_args)` — one bot process-session per call, exactly as for a browser.
4. `bot.py` builds a `CallSession` from the handshake, greets on the media stream opening, and runs the same pipeline as always. The serializer resamples 8kHz μ-law to the pipeline's rate in both directions.
5. Barge-in works because Flux still drives it: an interruption makes the serializer send Twilio a `clear` event, which flushes the audio Twilio has buffered but not yet played.
6. The call ends when the person hangs up (the websocket closes → zero grace → session cancelled) or when the agent does (an `EndFrame` reaches the serializer, which hangs the call up through Twilio's REST API).

**In development, step 3 needs a tunnel.** `ngrok http 7860`, then `TELEPHONY_PUBLIC_URL=https://<whatever>.ngrok.app`. The URL changes every time a free ngrok restarts. `uv run bot.py` prints the exact `wss://` address it will be reached at, which is the value to check first when a call connects and then goes quiet.

### Turn workflow

1. Flux detects the caller starting a turn → interruption broadcast → in-flight LLM/TTS discarded.
2. Flux decides the turn is over → transcript + `UserStoppedSpeaking` → user aggregator writes the message.
3. LLM streams tokens → TTS synthesises per sentence → audio streams out.
4. Assistant aggregator records what was played. If interrupted, `mark_interrupted_reply` flags the fragment.
5. `BotStoppedSpeaking` arms the idle timer. If it fires: nudge, nudge, goodbye, end.

### Interruption workflow (the part that is easy to get half-right)

Stopping the audio is only half. The other half is the truncated fragment left in the context, which must be marked or it degrades every subsequent reply. Both halves are required; the eval `barge_in.yaml` tests both.

---

## 10. Testing completed

### The eval suite

`server/evals/` drives the real bot over its eval transport with synthesized speech and asserts on transcribed audio.

| Scenario | Catches |
|---|---|
| `conversation` | Broken round trip; lost conversation memory |
| `barge_in` | Interruption no longer stops the agent; it answers the old question |
| `voice_quality` | Output has become unspeakable — markdown, symbols, digits |
| `silence` | Talks into dead air forever, or never notices the caller left |
| `knowledge_known` | The agent no longer finds an answer its documents contain |
| `knowledge_unknown` | The agent invents an answer its documents do not contain |
| `smoke_text` | Pipeline does not assemble; LLM unreachable (not in the suite) |

**Phase 4 result (2026-09-03): 4/6 on each of three full runs**, 5m 58s, 3m 52s and 4m 09s. Each run failed a *different* pair, which is the important part — see [Known issues](#5-known-issues-and-limitations). `barge_in` and `silence` passed all three times; the affected scenarios pass with `KB_ENABLED=false`, which is the evidence that the pipeline itself is intact. `barge_in` passing after the echo work is also what confirms `ECHO_SUPPRESSION` defaulting to `off` left barge-in alone.

*(Phase 2's result, for comparison: 8/8 over 4 scenarios × 2 passes, before the knowledge base existed.)*

### The Phase 25 validation (2026-09-08)

`tests/test_engine.py` — 68 checks, all passing, the last section over a throwaway PostgreSQL schema. `uv run validate.py` — 24 scripts, 3,785 checks, every script exit 0; the one failed finding is health.py tts (the ElevenLabs credential, unchanged since Phase 23); 13 warnings, the same posture plus two stale scheduler_workers rows from earlier sessions.

The live run on this machine — the real application with the real engine
over the real database, the real bot on port 7861 (Deepgram Flux, Groq,
Cartesia, the local calendar), the carrier's HTTP API stood in for by the
audit's bridge — a two-contact campaign created from the page with a CSV, started from the page, dialled by the engine without any other command — Ada (interested: pain point, decision maker, partially qualified, 139 s, 9 transcript turns) and, 17 ms after her call ended, Grace (not interested, 69 s) — and marked COMPLETED by the engine itself; the progress route and the event stream showed every step (connected → completed → next contact → 100%), the calls list and the call pages showed both records, and the rows were deleted afterwards.

### The Phase 24 integration audit (2026-09-08)

The real page in jsdom against the running `app.py` and the real database:
71 checks in the create walk (login, dashboard, wizard, campaign page,
state changes, contacts, import, every other page, three roles, console),
21 in the after-call walk (calls list, call detail, campaign counts, stop,
dashboard, knowledge upload / search / remove) — all passing after the six
fixes; the failures on re-runs were the audit's own earlier rows (a
contact that already existed, a number already on the do-not-call list).
The real worker over the real database dialling the real bot through the
bridge carrier: two answered calls (72 s opted out; 161 s interested,
ended by the supervisor after Cartesia refused an empty sentence), one
placement that timed out (the bridge synthesising speech inside
`place_call` — the harness's fault, which found defect 6). `uv run
validate.py` afterwards: **23 scripts, 3,717 checks, every script exit 0; one automated finding failed (health.py tts, the ElevenLabs credential, unchanged since Phase 23); the extra warning is a stale scheduler_workers row from an earlier session, not the audit's**.

### The Phase 24 validation (2026-09-08)

`uv run validate.py` — 23 scripts, 3,715 checks, every script exit 0; two
failed findings (`health.py tts`, the ElevenLabs credential, as before;
`health.py crm`, HubSpot not answering within 6 s on that run), twelve
warnings (the same posture as Phase 23).

`tests/test_app.py` — 68 checks: serving and the CSP; one login for
`/app`, `/dashboard/api`, `/automation/api/v1` and `/api/app`; the CSRF
refusal; a viewer refused every write; the auth-disabled loopback; the
configuration route and the brief; the whole flow (dry-run import, import,
start, the real worker dialling a fake carrier with the ids on the
handshake, the brief, pause, resume, stop refused to an operator and
allowed with an admin key); the knowledge routes; the boundary.

Against the running `app.py` (auth disabled, loopback): `/healthz`,
`/api/app/session`, `/api/app/config`, `/app/`, `/static/*`,
`/dashboard/api/dashboard`, `/dashboard/api/calls` and one call,
`/automation/api/v1/campaigns`, one campaign and its members, `/status`,
`/prospects`, a dry-run import over `text/csv`, `/api/app/knowledge` —
every answer 200 with the keys the pages read; `/audit` 403 to the
operator, as the page expects. `node --check web/app.js` passes. **The
pages were not rendered in a browser this session.**

### The Phase 23 validation (2026-09-08)

`uv run validate.py` — 22 scripts, 3,647 checks, every script exit 0; one
failed finding (`health.py tts`, the ElevenLabs credential), twelve
warnings (the posture on a loopback development machine: no session
secret, admin-only keys, docs on, HTTP allowed, no signing key, metrics
open, duplicated variables; no jurisdictions; no cost rates). `measure`
over the real rows: 7 finished attempts from earlier drills (2 completed, 5
failed — the SignalWire refusals of unverified numbers), answer rate 0.29,
success rate 0, no call with a quality summary, no priced call, 0 prospects
with two live attempts, 0 repeated keys. The report: `server/validation-report.md`.

`tests/test_production.py` — 158 checks in ~40 s, numbered to the phase's
list (see the module docstring and §3 above). What it measured, on this
machine, for the pipeline's own paths: the API import 62 ms, a worker tick
with a dial 15 ms, the sink's teardown write 15 ms, a webhook applied over
HTTP 31 ms, a pgvector search 32 ms (the search tool 15 ms), a CRM pass
63 ms, an n8n pass over real HTTP 110 ms, a dashboard snapshot 157 ms,
every tool under a millisecond. None of it is audio latency; that table is
in [Measured latency](#measured-latency) and is the eval harness's.

Not run this session: the two eval suites and the phone drills (the TTS
credential), and any real call.

### The Phase 22 monitoring checks (2026-09-08)

`tests/test_monitoring.py` — 170 checks, the twenty-first script. It
imports the earlier scripts' fixtures the way `test_scaling.py` does
(`test_worker`'s world, `test_webhooks`' ledger store and signer,
`test_crm`'s mock and rows, `test_automation`'s fake store and recording
sender, `test_actions`' stubs, `test_security`'s keys) and drives the real
instrumented code through them:

- the registry: label sets, the refused `phone=` label, negative and
  non-finite observations ignored, nearest-rank percentiles, the text
  rendering with escaping and cumulative buckets, the JSON snapshot, reset;
  every instrument namespaced and correctly suffixed;
- the trace: shape, `clean_id`, the handshake, nested contexts, the
  keyword aliases, `current_trace_id`, the JSON line with `component`,
  `pid`, `trace`, `attempt`, `worker` and the token scrubbed;
- placements: the id on the row *and* the handshake *and* the
  `call.placed` line; the worker's ending line with `trace` and `worker`;
  outcomes, ticks, heartbeats, in-flight; the fleet gauges refreshed; a
  refused carrier counted by class with the trace kept on the row;
- in-call: five STT errors counted while the supervisor decides, the stall
  (a 1.3 s wait for the watchdog), terminations by reason, a barge-in
  counted once across three hops, a latency breakdown onto every stage;
- the receiver (refused, applied, duplicate, stale; the trace from the
  row), the syncer (synced, skipped; the trace from the attempt), the
  deliverer (delivered; the trace in the payload and on the line), the
  tools (unreachable calendar → `external_error`, past callback →
  `past_time`, a transfer on a browser → `transfer_unavailable`);
- the collector: gauges from the three records, a complete refresh over
  the in-memory store, a partial one over a store missing a read, the
  refresher loop with an injected sleep, a store still opening;
- the HTTP surface on a bare app: `/healthz`, `/readyz` 200 and 503 with
  the password scrubbed, 503 while stopping, the token (401, wrong, right),
  the content type, `/metrics.json`, request ids honoured / replaced /
  made, the route-template label with a phone number in the raw path,
  `unmatched` for a 404, `run_check` on a hang and a raise, `store_ready`
  on a pinging store, a counting one and one still opening; the real
  dashboard (no login needed for the probes, the page still redirects,
  the gauges refreshed at startup) and the real automation API (outside
  the keyed router, the key never in a label, `MONITORING_ENABLED=false`
  mounts nothing); `serve_ops` on an ephemeral port over a real socket and
  the taken-port path;
- configuration, the boundary (see the module docstring), and the SQL.

Run with the twenty others; `PYTHONIOENCODING=utf-8`. It rebinds the log
capture after the JSON section, because `configure_logging` removes every
handler.

### The Phase 21 scaling checks (2026-09-07)

`uv run python tests/test_scaling.py` — 151 checks. In memory, over
`test_worker.py`'s world with each worker given its own dialer and guards
(a second *process*, not a second object sharing the first's limiter):
two workers over six prospects, the calls ended by "the webhook" between
ticks and the tick order alternating, every prospect called exactly once,
never more live than the fleet limit, every attempt naming its worker,
both workers placing, no call followed twice; pacing — two workers whose
own limiters both say yes and the shared slot says no to the second, the
wait honoured, a campaign's own `pacing_secs` as a second scope; the
heartbeat — registered with host and pid, beating on time with the
in-flight count and the counters, `draining` on the tick after a stop
request, `stopped` with the final metrics, the stale judgement, the
identity's shape; a worker that stops beating — 30 s of silence adopts
nothing, 70 s adopts its live call and releases its unplaced reservation,
the slow worker coming back drops the call and does not get it back, the
ending written once; a clean stop — ownership cleared, the next worker
adopting both calls at once, the closing line saying so; the old store
without the tables served as one worker with one warning; the transient
classifier over eleven reasons, a 503 retried after the wait and the
attempt spent, an invalid number final, the ceiling respected, the
switch off, a separate wait; one webhook delivered six times across two
receivers concurrently — applied once, five duplicates, every delivery
accepted, one ledger row, an out-of-order earlier event ignored; queue
depth and the worker summary as numbers and as words, the dashboard
strip's tones; `bot.py` and `coordination.py` naming nothing of each
other. In SQL: the column and the tables; six concurrent reservations
under a limit of two — exactly two, for two prospects, each stamped; the
pacing slot global and per campaign with one state row per scope; the
heartbeat table's upsert, beat, listing, summary and stale judgement; a
placed call claimed only once its owner is stale, once, and released
reservations back to `PENDING` with the count restored while the live
worker's is untouched; a stopped worker's and an unowned call claimable
at once; queue depth; pruning; five concurrent deliveries of one webhook
applied once with one ledger row.

Full run of all twenty scripts on 2026-09-07: **3,319 checks**, every SQL
section executed. `uv run campaign.py init`, `campaign.py workers` and
`health.py scheduler` were run against this machine's database.

### The Phase 20 dashboard checks (2026-09-07)

`uv run python tests/test_dashboard.py` — 161 checks (72 new), the SQL and
the routes against PostgreSQL in a throwaway schema. What the new ones
prove: every conversion rate is over answered calls with its denominator
and its neighbouring facts in the footnote; answer, voicemail and transfer
rates over the right totals; the latency tile is the average per-call
median with p95 and the greeting, and is unavailable — not zero — without
the column or without a measured call; failed calls carry the rate, the
refusals before dialling and the recorded reasons; the compliance tiles
break the list down by source and the gate's refusals by code; progress is
closed memberships over all and calls remaining is pending plus due
callbacks with the live ones named; `ReportFilter` describes, keys and
serializes itself. In SQL: a campaign filter narrows every count, a date
range narrows and an empty one is empty, the quality summary aggregates
from the usage column, prospects narrow by membership, the overview
narrows and carries the statuses, search finds by name and — only when
asked — by digits, filters compose, paging walks backwards without overlap,
one call's usage reads back. The routes, for the first time over the rows
the checks seeded: every section including the five new strips, the view's
label, filters by id and name, 422 for an unknown campaign, a bad date and
a backwards range, a future range empty and labelled, the campaigns list,
the calls list paging, search by name and — for an operator — by number, a
status filter, the search route, one call in full with usage and quality,
404, the call page; then a viewer: the list masked, a number search finding
nothing, the detail withholding the transcript and the record and saying
so, the page saying the numbers are masked. `check_degraded` proves the
new strips render without the optional tables.

Full run of all nineteen scripts on 2026-09-07: **3,166 checks**, every
SQL section executed. `uv run dashboard.py --once` against this machine's
database produced every section (latency unavailable: no call has written
a summary yet).

### The Phase 19 compliance checks (2026-09-07)

`uv run python tests/test_compliance.py` — 146 checks, about eight
seconds, the last section against PostgreSQL in a throwaway schema. What
it proves, in the order it runs: a policy overlays every key and reports
every bad one; the jurisdiction wins over the campaign and is chosen by
the number's country; `COMPLIANCE_JURISDICTIONS` is validated at startup;
a list entry expires and revokes; the gate allows and audits with the
policy, refuses a listed number before anything else with its source,
refuses a marked prospect, defers a closed window in the prospect's own
zone, lets a campaign switch the window off, exhausts at the campaign's
ceiling and at a jurisdiction's lower one, lets a callback waive the
ceiling and never the list, defers an undue retry for exactly its wait,
releases an inactive campaign, and decides on the status when the table
is missing; the service writes the list on every kind of opt-out with the
facts and keeps the first record, marks a prospect at birth and on import,
refuses an unparseable number, removes with a stamp and reinstates only
when asked, closes and reschedules under the campaign's and the
jurisdiction's figures per outcome, and lists a `DO_NOT_CALL` outcome as
verbal; the dialer over a carrier stub that fails the check if reached
never dials a listed number and closes the attempt as `DO_NOT_CALL`,
defers a closed window with the count restored, exhausts a ceiling the
reservation waived, and — without a gate — still refuses as Phase 9 did;
the briefing puts the campaign's and the jurisdiction's disclosures on the
brief, first in the opening, in the system instruction and on the audit
log, records an anonymous caller's opt-out by number and a campaign
call's with its ids; the dispositions read as documented; the API lists,
checks, refuses a call for a listed number, removes under `manage`,
reads and validates a campaign's settings by role; nothing on the
conversation layer imports the package; and the SQL keeps one active row
per number, hands a listed number to nobody, counts it undialable, marks
on `apply_dnc_list`, revokes with the row kept, replaces an expired entry,
and merges the configuration per key.

Full run of all nineteen scripts on 2026-09-07: **3,094 checks**, every
SQL section executed. `uv run campaign.py init` created `dnc_numbers` on
this machine's database; `campaign.py compliance` printed the environment
policy (window 09:00-18:00 mon-fri Asia/Karachi, three attempts, retry
60 m, no disclosures required, no jurisdictions), `dnc-list` an empty list,
`compliance-log` nothing recorded yet.

### The Phase 18 security checks (2026-09-07)

`uv run python tests/test_security.py` — 214 checks, no database, no
keys, about six seconds. What it proves, in the order it runs: passwords
hash and verify and a plain password is not a hash; sessions round-trip,
expire, and refuse every kind of tampering; a viewer / operator / admin
holds exactly its permissions and the directory refuses a plain password,
an unknown role, a duplicate and a bad name; the limiter allows N, refuses
the N+1th with a `Retry-After`, slides, resets and stays bounded; masking
keeps names and hides numbers, emails, transcripts and custom fields
without touching the original; the scrubber hides every new secret and
every credential-shaped string; the HTTP layer believes forwarded headers
only past a trusted proxy, refuses a wildcard origin, adds every header,
caps a body at 413, refuses / redirects plain HTTP and serves a forwarded
HTTPS request with HSTS, and answers CORS only for a listed origin; the
audit writer stores a masked, scrubbed row and a log line, survives a
broken store once a minute, and raises in strict mode; the dashboard
redirects, refuses, rate-limits the form, refuses a cross-site post, signs
a user in with the right cookie flags, audits it by name without the
password or the token ever reaching a log line, masks a viewer, accepts a
key, limits the anonymous, exposes only two POST routes, refuses to build
with nobody configured, and serves anonymously only when told to; the API
resolves each key's role, refuses a viewer's write and every transcript
route with an audited 403, masks a viewer's every answer, keeps
`complete` / `cancel` / `retry` / the audit log for an admin, refuses
control characters and oversized bodies, turns 401s into 429s per address
and a busy key into a 429 with `Retry-After`, refuses plain HTTP when told
to, and turns the docs off when told to; the webhook router answers 429
before the third refused delivery's signature is computed; and nothing on
the call path imports the security package.

`test_dashboard.py`'s route checks now sign in first (89 checks, five
new), and the login and the logout land in the real `audit_log` when
PostgreSQL is reachable. `test_automation.py`'s 245 pass unchanged — an
admin key is Phase 17's key — and `test_webhooks.py`'s 191 pass over the
router with the limiter in it. Full run of all eighteen scripts on
2026-09-07: **2,945 checks**, every SQL section executed.

`uv run security.py check` against this machine's `.env` reported: login
required but `DASHBOARD_USERS` empty (FAIL, expected — nobody configured
yet); no session secret (WARN); one admin API key and no operator or
viewer key (WARN: give n8n an operator key); the OpenAPI docs served
(WARN); outbound events signed (OK); HTTP allowed (WARN); CORS off (OK);
rate limits at their defaults (OK); audit on (OK); carrier webhooks not
verifiable because `SIGNALWIRE_SIGNING_KEY` is unset (WARN, as since
Phase 14); `.env` not tracked by git (OK, after the `git rm --cached`).

### The Phase 7 scenarios (2026-09-04)

Five sales scenarios changed for Phase 7 — `meeting` (rewritten around the
booking flow), `callback`, `do_not_call_implicit`, `interested` and
`wants_human` — and each was run against a real Groq/Qwen bot booted fresh for
it, with the Phase 7 tables in the real database. Read alongside the Groq note
in [Known issues](#5-known-issues-and-limitations): the runs that failed failed
on the tier, and the day's budget ran out before they could be repeated.

| Scenario | Result | What happened |
|---|---|---|
| `meeting` | **PASS** (3m 48s) | The whole flow, end to end: `check_calendar_availability(day=2026-09-07, preferred_time="morning")` — the model worked Monday's date out from the time block — six slots returned, "I've got Monday at nine, or half past nine, which suits?", `book_meeting(start=2026-09-07T09:00)`, "Booked — Monday the seventh at nine", `end_call`. The booking is row 1 in `meetings`; `uv run campaign.py meetings` shows it. Each tool turn waited ~35 s on the throttle |
| `callback` | **PASS** (1m 06s) | `schedule_callback` called; on an eval session there is no prospect row so the backend refused, and the agent said a colleague would be in touch rather than that anything was scheduled — which is the assertion |
| `do_not_call_implicit` | **PASS** (1m 33s) | `mark_do_not_call` on a phrasing the detector does not catch; accepted and closed |
| `interested` | not established | First run: the bot's turn-1 reply was correct (`Fair enough. In short, we put a simple tracker…`, log shows `ERRORS \| none`) but arrived 31 s after the request on the throttle, and that turn did not yet carry `within_ms`, so the harness gave up at 60 s. `within_ms` added. Re-run refused by Groq: tokens-per-day exhausted (199,843 / 200,000). **Run it again tomorrow, first** |
| `wants_human` | not established | First run: `judge call failed: RateLimitError` on the greeting — the *judge's* Groq request was throttled, not the bot's. Re-run refused: tokens-per-day exhausted. **Run it again tomorrow** |

Three earlier `meeting` attempts on the same day are the story of Failed
§19–§21: refused outright (no output cap), tool called but reply throttled past
60 s, tool not called ("shall I look up a couple of slots?"), then — after the
output cap, the trimmed prompt, the calendar line on every selling stage, the
named-day override and guidance-first results — the pass above. The other ten
sales scenarios and the audio suite were **not** run in this phase: the day's
Groq budget did not stretch to them, and nothing they exercise changed except
two renamed tools that `test_conversation.py` covers. They are the first thing
to run when the budget resets.

### The sales suite (Phase 6)

`server/evals/sales/` drives the same real bot through the fourteen conversations the phase asks for, in **text mode**, with the deterministic assertions on `function_call` rather than on prose. Fifteen scenarios: the fourteen required plus `do_not_call_implicit`, which is the other half of the do-not-call pair.

```bash
SESSION_IDLE_TIMEOUT_SECS=3600 USER_IDLE_TIMEOUT_SECS=120 \
  uv run python -m pipecat.evals suite evals/sales/suite.yaml
```

**`USER_IDLE_TIMEOUT_SECS=120` is not optional and is new.** In text mode a turn is an HTTP round trip rather than a person talking, and the gap between two scenario turns routinely exceeds the 12-second production default — so the silence handler decides the caller has gone quiet and injects a nudge *between* the scenario's turns. Observed on 2026-09-03: the nudge landed after the prospect's question, the model answered the nudge, and the scenario failed for a reason unrelated to what it tested. Both overrides reach the spawned bots because neither is set in `.env`; anything `.env` *does* set would win instead (Failed §13).

### The Phase 9 manual failure tests (2026-09-04)

Against the real database, the real bot and the **live SignalWire account**.
Each one injects a failure and checks what the system does about it.

| Drill | What was done | What happened |
|---|---|---|
| Closed calling window | `CALLING_HOURS=03:00-03:01`, `campaign.py call` | Refused **before reserving**: "outside calling hours … the window opens Mon 07 Sep 03:00". No attempt spent |
| Carrier refuses | Real SignalWire, an unverified number | `call.refused` (21219), attempt released as `FAILED`, `FAILED` call result written. No phone rang |
| **Ambiguous placement** | Carrier pointed at a non-routable address, `CARRIER_TIMEOUT_SECS=3` | One attempt only, `AMBIGUOUS`, attempt held `UNRESOLVED`, run stopped with "Run `campaign.py recover`" |
| Prospect blocked afterwards | `campaign.py call` again | Refused: "concurrency limit reached: 1 call(s) live, limit 1". The held attempt is doing its job |
| Health notices it | `health.py database` | `DEGRADED … 1 live attempt(s) — run campaign.py recover if no calls are in progress` |
| **Recovery, live** | `campaign.py recover --all`, real SignalWire | Queried the carrier for recent calls to that number, found none, closed the attempt as `FAILED` with "nothing was dialled", freed the prospect. **`find_recent_calls` has now been exercised against a live carrier** |
| Restart mid-reservation | Reserved a call, then abandoned the process | `recovery.released` — "reserved before a restart and never dialled". Nothing was placed |
| Database unreachable | `DATABASE_URL` at a dead port | `health.py` reports `FAILED`, and the DSN password is scrubbed to `postgresql:***localhost` |
| Bad LLM key / retired model | A wrong key, then `GROQ_MODEL=not-a-real-model` | `FAILED credentials rejected (HTTP 401)`, and `DEGRADED groq does not list 'not-a-real-model' … Set GROQ_MODEL` |
| **Call over its ceiling** | Bot with `MAX_CALL_SECS=25`, `fake_carrier.py` | Ended at 26s. The goodbye could not be generated (Groq's daily quota was exhausted), so the 12-second grace fired and the call ended anyway — the fallback path working under a real failure |
| **LLM outage on the greeting** | Bot run while Groq's daily budget was exhausted | Found two bugs; see [Failed §25](#25-counting-an-llm-failure-and-then-immediately-forgetting-it-phase-9) and [§26](#26-a-threshold-that-cannot-be-reached-because-nothing-tries-again-phase-9). After the fix: `session.dead_on_arrival`, call ended in 4s, summary says `THE AGENT NEVER SPOKE` |
| Structured logs | `LOG_FORMAT=json` and text | Both carry `campaign/prospect/attempt/call/provider`; the real Groq key printed deliberately came out as `***` |
| Full health check | `uv run health.py` | All seven components OK, including SignalWire's account read and the Groq model catalogue |

Not exercised: a duplicate carrier **webhook** against a live endpoint (there is
no webhook route — status is still polled; the idempotent `apply_call_event`
that would receive one is covered in both check scripts), and a real answered
call, which no session of this project has ever had.

### The Phase 8 manual test (2026-09-04)

Against the real database and the real bot (Deepgram Flux, Groq/Qwen, Cartesia),
in this order:

1. `uv run campaign.py results` on the pre-Phase 8 database printed the
   "created before Phase 8, run init" message. `uv run campaign.py init`
   added the table. `uv run campaign.py rebuild-results` built one result:
   attempt 1, the real SignalWire refusal (HTTP 422, the `To` number not
   verified) — `FAILED`, source `carrier`, the carrier's reason in the summary.
   `result 1 --transcript` and `result 1 --json` render it.
2. A placed attempt row (id 2) was created by hand for prospect 1. The bot was
   booted on port 7863 and `tests/fake_carrier.py --prospect 1 --campaign 1
   --attempt 2 --seconds 20` drove a silent call at it. The bot resolved the
   prospect ("calling Ayesha at Meridian Logistics … attempt=2"), Groq
   answered (the day's budget had reset), the greeting was spoken over the
   stream, and on hang-up the log shows `RESULT | attempt 2 | COMPLETED |
   qualified=UNKNOWN next=UNKNOWN | 1 transcript turn(s), 0 action(s)` and
   `ERRORS | none`. `uv run campaign.py result 2 --transcript` shows the row:
   source `conversation`, duration 15 s (the call's, not the session's),
   ended "by the other end or a dropped line", every enum `UNKNOWN`, the
   summary saying the other end did not speak and the conversation ended
   during the opening, and the greeting as the transcript's one turn.

Not exercised live: a conversation with tool calls end to end (the check
scripts drive every one of those through the real conversation layer with the
backend stubbed), and the dialer's reconciliation path against a real carrier
(no answered call has ever been placed from this machine).

### The Phase 12 drills (2026-09-05)

`tests/phone_drill.py`, against `uv run bot.py --port 786x` with the real
`.env` (Flux, Groq `qwen/qwen3.8-27b`, Deepgram TTS, the knowledge base on).
Kokoro speaks the caller over the fake carrier's Twilio media-stream protocol,
8 kHz μ-law, in real time; the checks read the bot's report.

**Before the fix** (first run, 22:38): the greeting came 14.9 s after the
handshake; the first caller turn was detected **5.5 s** after it began and the
second **3.4 s**; end-of-turn 4.8 s and 2.8 s; both interruptions landed on a
bot that had already stopped; 0 barge-ins recorded while the carrier saw 2
`clear` events. Diagnosis in Failed §31.

**After the fix** (drain + warm-up), all seven drills on the final code:

| Drill | Result | Turn-start lag (ms) | What the report showed |
|---|---|---|---|
| `barge_in` | PASS | 858, 1203 | 1 barge-in; pipeline stop latency **141 ms**; carrier `clear` 1.2 s after the caller began; audio stopped 1.2 s after; interrupting turn transcribed and answered. (Run on the build before the sibling de-duplication, which reported the one barge-in twice; the counting is unit-tested and `overlap` below shows it corrected) |
| `short_answers` | PASS | 750, 983, 640 | "Hello." / "Sure. Go ahead." / "Okay." all heard, all answered |
| `pauses` | PASS | 922 | "We run about forty trucks out of Lahore, [0.7 s] mostly long haul, and half of them are refrigerated." as **one** turn |
| `rapid` | PASS | 1000 | 1.35× sentence transcribed whole, one turn, six of six key words |
| `overlap` | PASS | 982, 952 | 1 barge-in, stop latency **62 ms**, `clear` 0.9 s after the caller began, **0.0 s** of bot audio while the caller kept talking, the callback request answered |
| `noise` | PASS | 906, 874 | white noise at −28 dBFS throughout: 0 barge-ins, 0 spurious, both sentences heard ("forty tracks" for "trucks") and answered |
| `voicemail` | PASS | — | detected by `phrases` at 7.8 s (the greeting, talking over the agent, said "leave a message"); `ended_by=voicemail`; the bot closed the line at 12.6 s |

Greeting after connect, on the fixed build: 4.1–7.3 s, once 12.9 s (a
throttled first request). Per-response latency on the fixed build, from the
caller's silence: `turn_end_ms` 462–1542 (median ~600), `tts_first_audio_ms`
250–476, `llm_first_token_ms` **664–21,467** — the free Groq tier, every time;
one turn in each drill after the first was answered "late" by the monitor's
10 s rule and none failed.

Two drill runs failed for reasons that were the drill's, not the bot's, and
both are recorded because they are things a real caller does: "No, not
really" as the second short answer made the agent (correctly) decline a
callback and `end_call`; and a 0.9 s "quiet" threshold had the caller talk
into the gap between two sentences of one reply. The lines and the threshold
were changed.

The rapid-speech run also hung up on the caller once — my voicemail length
rule, on a fast eight-second answer. Failed §32.

### The Phase 12 evals (2026-09-05)

`short_answers`, `rapid_speech` and `overlap`, run one `suite -s` each with
`SESSION_IDLE_TIMEOUT_SECS=3600 USER_IDLE_TIMEOUT_SECS=60` (the second so a
slow Moonshine transcription between turns is not answered by an idle nudge).

| Scenario | First run | Re-run (with `within_ms: 150000`) | What happened |
|---|---|---|---|
| `overlap` | **PASS** (1m 35s) | — | The long interruption was transcribed whole and the reply addressed the callback request |
| `rapid_speech` | 1/2 turns: the judge was still waiting at 60 s | **PASS** (1m 27s) | The turn arrived whole; the first reply was the tool-turn filler ("let me note that"), and the real answer came after Groq's per-minute wait — the Phase 7 pattern exactly |
| `short_answers` | 3/4 turns: turn 3 timed out at 60 s (Groq) | 3/4 turns: turn 3 **judge said no** | On "No, not really." the agent re-asked its discovery question ("do you run any vehicles"), which the criterion's "asks the caller to clarify" clause caught. It never asked for a repeat; the turn was heard and answered. A conversational choice, not a hearing failure |

The three scenarios now carry `within_ms: 150000` on their replies, as the
sales scenarios do, and `short_answers`' criteria were narrowed afterwards to
fail only on "I didn't catch that" / "say that again" — the thing the scenario
is for. **That narrowed version has not been run**: the day's Groq budget had
gone on the drills and two eval passes, and a third pass risked the daily
cap. Run it, with `-r 2`, before believing either result. The audio suite's
Groq caveat from Phase 7 applies to all of this: every wait above was the
tier, and none was the bot.

### The Phase 11 measurements (2026-09-04)

`uv run python scripts/benchmark_db.py --prospects 20000 --attempts 60000`, in
a throwaway schema on the development machine. **Warm-cache medians** — the
first (cold) run reported 2–5× these and nearly justified the wrong fix.

| Query | Median | Note |
|---|---|---|
| `has_live_attempt` | 0.7 ms | |
| `count_live_attempts` | 0.9 ms | |
| `recent_call_rows` | 1.8 ms | |
| `prospect_counts` | 5.4 ms | |
| `reserve_next_call` | 10.7 ms | Seq scan on prospects; a partial index made it *worse* |
| `disposition_counts` | 19.6 ms | |
| `attempt_counts` | 40.1 ms | Full-table aggregate |
| `campaign_overview` | 41.8 ms | Eight scalar subqueries per campaign |
| `result_counts` | 64.0 ms | Full-table aggregate |
| `campaign_result_counts` | 76.6 ms | Group-by over every result row |

**Before and after:**

| | Before | After |
|---|---|---|
| One dashboard load | 249 ms (7 sequential queries) | **149 ms** (concurrent) — 43% faster |
| 21 simultaneous viewers | 21 database reads | **1 read** (5 s cache + lock) |
| Idle connections per call | 2 | **1** (shared pool) |
| Peak connections per call | 6 | **4** |
| Six racing reservations, limit 2 | advisory | **exactly 2 handed out** |

**Per-turn costs, measured separately:** embedding a query 27.9 ms, pgvector
search 1.5 ms — 29 ms of a ~1,300 ms turn, so retrieval was left alone. The
embedder's one-off warm-up is 1.4 s at startup.

**Prompt composition, measured locally and confirmed live:**

| Part | Size |
|---|---|
| System instruction | ~1,635 tokens |
| Twelve tool schemas | **1,248 tokens (40%)** |
| Per-turn guidance block | ~228 tokens |
| Live measurement on a real call | **3,394 prompt tokens per request** |

The largest single tool is `record_discovery` at ~206 tokens; the smallest,
`end_call`, is ~54.

**The live end-to-end check.** A call driven through `fake_carrier.py` with
campaign ids, against the real database: the bot logged
`USAGE | 1 LLM request(s) | 3,394 prompt + 38 completion tokens | TTS reported
no usage | 17s of audio transcribed`, and `call_attempts.usage` for that
attempt holds the per-model breakdown with `cost_usd` null because no rates are
configured. Startup logged `1 idle / up to 4 database connections per call
provider=shared pool`.

### The Phase 10 dashboard test (2026-09-04)

Against the real database, with the dashboard served on port 7870:

| Check | Result |
|---|---|
| `uv run dashboard.py --once` | The whole snapshot as JSON: 8 tiles, 3 campaigns, 6 recent calls, 2 outcome rows |
| The page over HTTP | 200, ~10 KB, every container the script writes to present, no external URL in it |
| `/api/dashboard` over HTTP | 200 in 130 ms |
| `/api/ping` | `{"ok": true, "detail": "database reachable"}` |
| Write routes | `POST /` → 405, `POST /api/dashboard` → 405, `/docs` → 404 |
| **The page's own script, run in Node against the live JSON** | All eight tiles, 3 campaign rows, 6 recent rows, 2 outcome rows, no unresolved template literal. This is how the render was verified without a browser: the *real* `render`, `campaigns`, `recent` and `outcomes` functions were executed against the served data with a DOM stub |
| **Escaping** | A prospect name of `<img src=x onerror=…><script>alert(2)</script>` came out escaped in all five places it is rendered, and nowhere raw |
| The degraded database | With `call_results`, `meetings` and `callbacks` dropped in a temp schema, the page still renders 8 tiles, the survivors are still right, the two affected read "unavailable", and the outcome breakdown falls back to attempt statuses and says so |

**Not done: a browser screenshot.** Seven Chrome browsers were connected to the
account and choosing one could have opened a window on a different machine, so
it was skipped rather than guessed at. Running the page's real render functions
against live data covers what a screenshot would have shown about *correctness*;
what remains unverified is purely visual.

A real discrepancy the live data exposed: the "Meetings booked" tile read 1
while every campaign row read 0. Both were right — the Phase 7 booking was made
on an eval session with no campaign — so `meeting_counts` gained an
`unattributed` count and the tile now says "1 not tied to a campaign" rather
than looking like a bug.

### The deterministic checks

Seventeen scripts that need no vendors and no phone, and finish in seconds:

```bash
uv run python tests/test_automation.py       # Phase 17 — keys and signatures, config, the API over a fake store (every route, every refusal, Idempotency-Key), the real worker placing what the API queued, the deliverer through every ending, the boundary; both tables and the claim in SQL (last section needs PostgreSQL)
uv run python tests/test_booking_transfer.py # Phase 16 — transfer TwiML with and without a receiver, the Dial report decoded and recorded, the action service, Cal.com's timeout / lost answer / event type, the diary's constraint and the transfers table in SQL (last section needs PostgreSQL)
uv run python tests/test_crm.py           # Phase 15 — the mapping, the HubSpot adapter over a stub, the syncer: successful, duplicate, changed, retried, lost answer, failed, out of attempts, rejected token, policy; the boundary; crm_sync in SQL (last section needs PostgreSQL)
uv run python tests/test_webhooks.py      # Phase 14 — carrier signatures (Twilio's published example, SignalWire's key), decoding, the receiver: valid, forged, duplicate, out of order, unmatched, every ending, the AMD verdict; the route; the worker polling less; the ledger (last section needs PostgreSQL)
uv run python tests/test_voice_quality.py # Phase 12 — turn monitoring, failed turns, barge-in latency, noise recovery, voicemail (detector, handler, carrier AMD, the VOICEMAIL result), config
uv run python tests/test_conversation.py  # the sales layer — states, qualification, signals, tools, transcript, all 14 scenarios
uv run python tests/test_results.py       # Phase 8 — the call result: every required case, precedence, validation, summary, export
uv run python tests/test_reliability.py   # Phase 9 — injected failures: duplicate calls, restarts, retries, guardrails, the supervisor
uv run python tests/test_dashboard.py     # Phase 10 — the aggregates in real SQL, the footnotes, the degraded database, no write routes
uv run python tests/test_performance.py   # Phase 11 — usage accounting, cost honesty, pooling, the reservation race, the cache
uv run python tests/test_actions.py       # Phase 7 — every tool through the real boundary, stubbed world
uv run python tests/test_scheduling.py    # Phase 7 — local calendar arithmetic; Cal.com against a stub session
uv run python tests/test_knowledge.py     # retrieval, chunking, the gate, what the LLM is handed
uv run python tests/test_telephony.py     # placement, transfer, outcomes, errors, handshake, config
uv run python tests/test_realtime.py      # echo suppression, the peer watchdog
uv run python tests/test_campaigns.py     # phones, CSV, the queue, DNC, the dialer, callbacks, meetings, results, duplicate protection (SQL half needs PostgreSQL)
uv run python tests/test_worker.py        # Phase 13 — the scheduler: duplicate reservation, hours, DNC, retries, callbacks, restart, completion, concurrency, pacing, shutdown (last section needs PostgreSQL)

uv run health.py                          # Phase 9 — every dependency, no call placed
uv run campaign.py recover                # Phase 9 — resolve attempts left live by a crash
```

All seventeen pass as of 2026-09-07 — 2,726 checks, the 2,481 from before
plus `test_automation.py`'s 245, with PostgreSQL reachable so every SQL
section ran.

`test_automation.py` is the Phase 17 one, arranged around the two promises the
phase makes — nothing happens twice, nothing happens in the call — and around
what an automation retrying blindly needs: the same request, again, answered
the same way. The API is the real FastAPI app over the real `CampaignService`,
driven through FastAPI's test client over `test_worker.py`'s in-memory store
with the Phase 17 tables added (`FakeStore`, whose claim mirrors the SQL's
rules); the scheduler section runs the real `CampaignWorker` and
`CampaignDialer` over the same store to prove a call the API asked for is
placed by the worker and by nothing else; the deliverer is the real one over
a `RecordingSender` that keeps every POST and answers from a script. Its SQL
section opens a second pool for the app under the test client, because the
client runs the app in a thread with a loop of its own and an asyncpg pool
belongs to the loop that opened it. It also narrows the "skip when no
database" clause to connection errors: its first run hid a type-inference
bug in the claim SQL behind "cannot reach PostgreSQL", which the other
scripts' broader clause would still do.

`test_booking_transfer.py` is the Phase 16 one, arranged around what goes
wrong *after* the agent has done the right thing: a calendar that does not
answer, a slot taken between the offer and the write, a colleague who does
not pick up. The Cal.com client is the real one over a stub session that can
time out; the transfer TwiML and the `<Dial action>` report are read by the
real `TwilioProvider` and applied by the real `WebhookProcessor` over Phase
14's in-memory store with a transfers table added; the real `ActionService`
is driven directly with a fake carrier and store — the conversation above it
is unchanged and has its own script. The last section runs the exclusion
constraint (including five concurrent bookings of one slot), the transfers
table and the receiver against PostgreSQL in a throwaway schema.

`test_crm.py` is the Phase 15 one, arranged around the four things a sync can
be: successful, failed, duplicated and retried. `MockCrm` is a real
`CrmProvider` in memory that records every request and can be told to fail a
call once, permanently, or to perform it and then lose the answer; `FakeStore`
carries the claim rules; the real `CrmSyncer` and the real mapping run between
them, and every scenario asserts on both the CRM's request log and the row.
The HubSpot adapter is the real one over a stub HTTP session, so the endpoints,
property names, disposition ids and the association it sends are pinned. The
last section runs the table, the claim (including four concurrent claims) and
the syncer against PostgreSQL in a throwaway schema.

`test_webhooks.py` is the Phase 14 one, and it is arranged around the four
things a delivery can be: valid, forged, repeated and late. The signatures are
real — computed the way the carriers' own libraries compute them, with
Twilio's documented example reproduced byte for byte and an independent
implementation of the recipe in the test itself — and the code under them is
the real `TwilioProvider`, the real `WebhookProcessor` and the real
`CampaignService` over `test_worker.py`'s in-memory store with the ledger
added. The route is driven through FastAPI's test client, including the check
that a signature over the server's *own* URL is refused. The worker section
counts carrier requests with and without events arriving. The last section
runs the ledger and the processor against PostgreSQL in a throwaway schema.

`test_worker.py` is the Phase 13 one. It runs whole campaigns through the real
`CampaignService`, `CampaignDialer`, `AttemptRecovery` and `CampaignWorker`
over an in-memory store that enforces the queue's rules, with a carrier whose
calls walk a scripted status list and a clock the checks advance by hand —
so "retry in an hour" is one line, not an hour. Every scenario the phase asked
for is a named check, and the last section runs the new SQL and the worker
against PostgreSQL in a throwaway schema, as `test_campaigns.py` does.

`test_dashboard.py` is the Phase 10 one, and it checks each layer where that
layer can actually be wrong: the aggregates against real SQL in a throwaway
schema (because what is being checked *is* whether `count(*) FILTER (...)`
counts the right rows), the shaping against fixed inputs (because that is where
the claims live — that answered and completed differ, that an average carries
its count, that a missing table is unavailable and not zero), and the routes
through FastAPI's test client (including that the application exposes no method
but GET and HEAD).

`test_reliability.py` is the Phase 9 one, and it is the only script in this
project that *injects* failures rather than avoiding them: `FlakyCarrier` can be
told to time out, refuse, or vanish mid-request; `FakeStore` can be told its
database has gone away. The code under test is the real dialer, the real
recovery pass and the real supervisor. It covers the phase's list — a carrier
failure, an STT/LLM/TTS failure, a database that is unavailable, a calendar
failure (through the guarded-write helper), a duplicate webhook, a duplicate
call request, and a process restart — plus the retry policy's three verdicts,
the calling window, pacing, concurrency, the duration ceiling, and the
credential scrubbing. The five duplicate-call mechanisms are each driven **on
their own**, with the others out of the way, so a regression in any one of them
fails a check rather than being covered by the next.

Its SQL half is `check_phase9` in `test_campaigns.py`: the unique index refusing
a second reservation, `mark_attempt_placed` refusing a second carrier call id,
`apply_call_event` being idempotent and monotonic against real rows, and a
recovery pass over a real `UNRESOLVED` attempt.

`test_results.py` is the Phase 8 one. Every result in it is built from a real
`SalesConversation` driven through the real tools, with a scripted
`ActionBackend` where a booking, a schedule or a transfer is needed, and the
outcome handed to `build_conversation_result` exactly as the sink hands it.
It covers the twelve cases the phase asked for — a successful call, a failed
call, no answer (and busy), do-not-call, a booked meeting, a callback
(scheduled and merely requested), an uninterested prospect (and one who then
asks for a callback), an incomplete conversation, a malformed record in three
shapes, missing optional values, transcript preservation, summary generation
— plus the disposition precedence table, the attempt status from the state
path, the question extraction, every validator refusal, and the export. The
SQL half — the table, both writers, the upsert rule in both orders, the
readers, the sink end to end including a callback reopening its membership
and a do-not-call marking the prospect, and the store refusing an invalid or
orphaned result — is `check_phase8` in `test_campaigns.py`, in the throwaway
schema.

`test_actions.py` is the Phase 7 one and covers the list the phase asked for:
successful calendar lookup; unavailable calendar (full day, unreachable,
refusing, crashing, not configured); successful booking; booking failure (slot
not offered, slot taken, refused, unreachable, email required, local write
failed); callback scheduling (valid, vague, bare date, past, right now, too far,
duplicate, database down, no prospect); DNC and duplicate DNC and DNC with no
row; end call; transfer success and every failure (refused, unreachable, browser
session, no destination, no credentials, twice); RAG search (found, nothing,
empty, unavailable, broken); malformed arguments (missing, wrong type, null,
non-object, unknown keys, out-of-vocabulary enum, raising tool, silent tool,
`**kwargs` tool); unauthorized actions (everything after a do-not-call, the
calendar and a meeting after a no); the audit log line and the outcome record;
and that every one of the twelve results has the one shape. The calendar,
store, retriever and carrier are stubs; **no real booking and no real transfer
happens anywhere in the tests.**

`test_conversation.py` is the one to run first now. It drives the *real* conversation layer — the real tools, invoked through a stubbed `FunctionCallParams` exactly as Pipecat invokes them; the real detectors; the real state machine — through all fourteen required scenarios, and asserts on the state transitions and the structured qualification data each produces. What it deliberately does not check is whether the *model* calls the right tool, which is what the sales suite is for. If a sales eval fails and this passes, the finding is about the model or the prompt.

`test_campaigns.py` is the one that needs PostgreSQL, and only for half of
itself. Its pure half — phone normalisation, CSV mapping, duplicate detection —
runs anywhere. Its SQL half runs in a **temporary schema that is dropped
afterwards**, because what it checks *is* the SQL: a stub store would only prove
the stub agrees with itself. With no database reachable it skips that half and
says so, so the file still passes on a machine without one. The telephony
provider is stubbed throughout; no test places a real call.

All pass as of 2026-09-03. They exist because the evals are slow, cost API calls and are judged by an LLM: when an eval fails, run these first. If they pass, the plumbing is right and the problem is the model, the prompt or the audio.

### Simulating a caller

Two tools that need a running bot and nothing else — no account, no tunnel, no phone:

```bash
uv run bot.py                                                    # terminal 1
uv run python tests/fake_carrier.py                              # a phone call
uv run python tests/fake_browser.py --reconnect --abandon        # a browser that drops and returns
```

Together with the eval suite they cover all three transports headlessly. `fake_browser.py` is the newest and the most load-bearing: the WebRTC path was the one nothing tested, and it is where both of the bugs found in live testing were hiding. Its `--reconnect --abandon` run is the regression test for "connects once, then never again" — it fails with `PEER_TIMEOUT_SECS=0` and passes with the default, which is how the fix was confirmed.

### Verifying telephony without a phone

There is no carrier account on this machine, so the audio path is checked by **simulating a carrier**. `tests/fake_carrier.py` connects to the running bot's `/ws`, sends the `connected` and `start` handshake with the custom parameters `call.py` attaches, streams 8kHz μ-law in real time, and decodes what comes back:

```bash
uv run bot.py                            # terminal 1
uv run python tests/fake_carrier.py      # terminal 2
```

Results, on 2026-09-03:

- The handshake was detected as Twilio and `CallSession` logged `outbound twilio call=CAfake… from=+15550001111 to=+923001234567`.
- The agent greeted **without any RTVI client**, which is the specific thing that would otherwise have been silently broken: `"Hi there! I'm here to help, so what's on your mind today?"`.
- Audio came back with real amplitude — 3.6s of it, peak 31100 — so it genuinely reached the far end, encoded correctly.
- After 12s of silence the idle nudge fired, so the Phase 2 resilience work runs on the phone path too.
- `CALL SUMMARY | … duration 18.6s | 0 caller turn(s), 2 agent turn(s)` plus the "the other end never spoke" warning.
- The same run against a bot configured for **SignalWire, with no `TWILIO_*` variables set at all**, also answered and greeted, and reported `ERRORS | none`. Under Pipecat's own serializer selection that configuration cannot answer a call at all — see [Decisions](#6-decisions-made-during-development).

What this does **not** prove: that a carrier accepts the markup, that a real number rings, or that a genuine 8kHz phone line transcribes as well as a clean synthetic one. Those need a real call, and it is the first thing to do when an account exists.

### Running them — exact forms matter

```bash
cd server

# Whole suite, fresh bot per scenario
SESSION_IDLE_TIMEOUT_SECS=3600 uv run python -m pipecat.evals suite evals/suite.yaml

# One scenario against a long-running bot (fast inner loop)
SESSION_IDLE_TIMEOUT_SECS=3600 uv run bot.py -t eval --port 7861     # terminal 1
uv run python -m pipecat.evals run evals/barge_in.yaml --bot-url ws://localhost:7861 -v

# Repeat runs — the only way to tell flaky from broken
... suite evals/suite.yaml -s barge_in -r 4
```

- **`python -m pipecat.evals`, not the `pipecat` console script.** The judge is loaded by dotted path (`evals.groq_judge.make_judge`) and only the module form puts the working directory on `sys.path`.
- **`SESSION_IDLE_TIMEOUT_SECS=3600`.** Under `-t eval` the pipeline starts at boot, so the 300s production default counts down while a bot waits for its scenario.
- **`-r N` before believing any single result.** `barge_in` failed once, passed 3×, then failed 2× — only repeats separated the real defect from noise.

### Measured latency

Two clean passes, 14 responses, Deepgram Flux + Groq `qwen/qwen3.8-27b` + Cartesia, home broadband:

| Stage | p50 | range |
|---|---|---|
| total | 1326ms | 990 – 2533ms |
| turn-end | 653ms | 417 – 2024ms |
| stt | 646ms | 417 – 2021ms |
| llm | 372ms | 312 – 759ms |
| tts | 148ms | 130 – 173ms |

Greeting (connect → first audio) 2.4 – 3.0s, almost all websocket setup to three vendors.

`stt` and `turn-end` start at the same instant and overlap, so they do not add; on the Flux path they are near-identical by construction. `total ≈ turn-end + llm + tts`, which do run in sequence.

**Read these as a pessimistic bound.** Under the eval harness the same CPU runs Kokoro, Moonshine and Silero, and the synthesized caller stops streaming audio the instant its utterance ends where a real microphone keeps sending. A live call has not been measured.

### Other verification

- Config permutations (defaults, fallback STT, tuned Flux, deliberately bad values) exercised in a scratch script — all four provider/turn-strategy paths construct, and bad values are collected into one readable error.
- `mark_interrupted_reply` unit-checked for idempotency and for empty / no-assistant contexts.
- Confirmed across 24 TTS calls that the interruption marker never reaches the synthesiser.

---

## 11. Risks and dependencies

**External services** — Deepgram (STT + turn detection), Groq (LLM), Cartesia (TTS). All three have free tiers. A failure in any one is a mid-call `ErrorFrame`; the pipeline logs it and keeps running rather than dying, which is why the error counter exists.

**Groq catalogue churn** is the most likely thing to break unattended. A retired model ID gives a 404 `model_not_found` at the first turn. Fix by setting `GROQ_MODEL`.

**Deepgram Flux is on `/v2/listen`.** Newer surface than `/v1`. If it becomes unavailable, `STT_PROVIDER=deepgram` degrades gracefully to local turn detection.

**Local ONNX models** — Silero VAD and Smart Turn v3 ship with Pipecat. Kokoro (~310 MB) and Moonshine download on first eval run to `~/.cache/pipecat`. An interrupted download leaves a truncated file that fails later with `INVALID_PROTOBUF`; delete the cache directory rather than debugging it.

**Pipecat 1.8.1 is pinned and pre-2.0.** Several APIs in use have deprecated aliases that disappear in 2.0. Before upgrading, re-verify every symbol against the installed source.

**The carrier is a fourth external service** and the only one that costs money per use. On either Twilio or SignalWire a trial account can usually only call numbers you have verified, and starts with most destination countries switched off; `src/telephony/twilio.py` translates both of those refusals into a message naming the fix, because they are the two everybody hits first. Twilio additionally does not offer trials in every country, Pakistan included — which is why SignalWire exists in this codebase.

**The tunnel is a single point of failure in development.** The carrier reaches the bot at `TELEPHONY_PUBLIC_URL`, and a free ngrok gives a new URL on every restart. A stale value produces a call that connects and then goes quiet, which looks like an audio bug. `uv run bot.py` prints the address it will be reached at; check that first.

**No deployment path and no compliance work.** The still-open question from Phase 1 is now blocking rather than theoretical: are prospects US/UK (TCPA, and the FCC's rules on AI voices in calls) or Pakistani (PTA/LDI licensing)? It picks the carrier, the caller ID, and what the agent has to disclose in its first sentence. Phase 4 built a dialler, not a compliant calling operation, and the difference matters before anyone dials a stranger.

---

## 12. Next recommended steps

**Phase 25 is done and Phase 26 is not specified; ask before building
anything.** First, open `http://127.0.0.1:7900/app/` after `uv run app.py`
and walk every page once with the browser console open — the one check
this session could not make. Then what comes next is not code: it is `PRODUCTION_READINESS.md` §16, in order — fix the TTS line,
rotate the keys, decide the compliance rules, then the fifteen manual
items starting with one real answered call
(`uv run validate.py live --to <your phone> --dial --yes`), the eval suites
(`uv run validate.py --evals`) and the phone drills. Each is a command;
none has been run on this system. Re-run `uv run validate.py` after every
change to `.env` and paste its verdict into the readiness document's §14
with the date.

**Phase 22's loose ends.** (1) Point a real Prometheus at the five ports
and confirm one `aiva_up{role=…} 1` per process and that
`histogram_quantile(0.95, rate(aiva_turn_latency_seconds_bucket[5m]))`
plots; the buckets were chosen for a healthy stack and a throttled Groq
tier, not measured. (2) On the first real call, `grep` the trace across
the bot's and the worker's logs and the receiver's — the one place the id
has not been seen is a carrier's `<Parameter>` coming back on a real
handshake. (3) Wire `/readyz` into whatever restarts the processes and
watch that a database blip flips it to 503 and back without a restart
(it should: readiness is not liveness). (4) Decide whether `MONITORING_TOKEN`
goes on before the first network-reachable port; `security.py check` will
keep saying so. (5) If a request's id should become the call's trace
(n8n `POST /calls` → the dial), add a column on `scheduled_callbacks`
and pass it through `_place_callback`; small, and not built.

**Phase 21's loose ends.** (1) Run two `campaign.py run` processes on one
machine against a real carrier with `MAX_CONCURRENT_CALLS=1`, watch
`campaign.py workers` in a third terminal, kill one with the task manager
mid-call and confirm the other adopts it within `WORKER_STALE_SECS` and
writes the ending — the checks prove the SQL, not the carrier. (2) Choose
`WORKER_STALE_SECS` from the slowest heartbeat you see in `worker.metrics`
lines under load, not from the default. (3) Watch the `worker.handed_over`
line: one is a slow worker; many is a stale window too short.

**Phase 20's loose ends.** (1) Place one call and open its detail page:
the latency and turn figures fill from the summary the sink writes at
teardown, and this is the first time a real report will feed the
dashboard. (2) If history matters, a one-off script over
`conversation_data -> 'quality'` can backfill `usage -> 'quality'` for
calls before this phase; nothing ships for it. (3) Watch `read_ms` in the
page's stamp on a filtered view with a large campaign — the filters are in
the scan, and Phase 11's numbers were for the unfiltered one.

**Phase 19's loose ends, in order.** (1) Decide the jurisdiction rules
with somebody qualified to decide them, write them into
`COMPLIANCE_JURISDICTIONS` and per campaign (`campaign.py compliance
<campaign> --set …`), and read `campaign.py compliance` back; the
software ships with none and must not be read as having any. (2) Load the
suppression lists you screen against (`campaign.py dnc-import <file>
--source registry`), then `dnc-apply` once for the prospects that already
exist. (3) Before turning `COMPLIANCE_AI_DISCLOSURE_REQUIRED` on for real
calls, add an eval scenario to `server/evals/sales/` that asserts the
agent's first sentence contains the configured text, and run it against
the model you deploy — the instruction is verified, the utterance is not.
(4) Watch the first real campaign run with `campaign.py compliance-log`
open: every allowed call should have a `compliance.allowed` row naming
the policy, and the first refusal is the first time the gate has met a
carrier.

**Phase 18's loose ends, in order.** (1) Rotate every key that was in
`server/.env` when that file was in git, if the nested repository was ever
pushed — see [Pending tasks](#4-pending-tasks); the file is untracked now,
not gone from history. (2) Configure the dashboard: `uv run security.py
hash-password --user <you> --role admin`, `make-secret`, and an operator
key for n8n (`make-key --role operator`) so the admin keys stay with
people; `uv run campaign.py init` for the audit table; then `uv run
security.py check --strict` should be clean but for HTTPS. (3) Put a TLS
proxy in front (the Caddy block in `SECURITY.md` is two lines), set
`SECURITY_REQUIRE_HTTPS=true`, sign in over it, and confirm a plain-HTTP
page is redirected and the cookie carries `Secure` — the first time any of
this has met a real proxy. (4) Read `uv run campaign.py audit` after a
day's use and decide whether `pii.transcript_read` is too noisy for the
workflows that legitimately poll transcripts; the fix is a viewer key for
polling and an operator key for reading, not a switch.

**Phase 17's loose end: one delivery to a real n8n.** `AUTOMATION_API_KEYS`
and `AUTOMATION_WEBHOOK_URL` in `.env` (and `AUTOMATION_EVENTS_SINCE=now`,
or every drill result since Phase 8 is delivered), `uv run campaign.py init`
(done here), import `n8n/workflows/04-qualified-lead-notification.json`,
select its two Header Auth credentials, activate it, then
`uv run automation.py --once` and `uv run campaign.py events`. Then workflow
01 with a three-row CSV against a DRAFT campaign — the import report comes
back through n8n — and workflow 06 only once a HubSpot task exists to be
found. If a workflow file refuses to import, the node `typeVersion`s are the
first suspects; they are the current ones as of n8n 1.x.

**Phase 16's loose ends ride on the same real call as everything else.** A
Cal.com key and event type in `.env`, `uv run health.py calendar` green
(it reads the event type back and checks its length), then a call that books
— the booking should appear in Cal.com and in `campaign.py meetings` with
its uid. And `TELEPHONY_TRANSFER_NUMBER` on a phone you can answer, a call
that asks for a person, then `campaign.py transfers` — `REQUESTED` should
become `ANSWERED` with the duration once you hang up, or `NO_ANSWER` with
the carrier's voice telling the prospect nobody is available.

**Phase 15's loose end: file one real call.** A HubSpot private app with the
scopes `CrmConfig` names, the token in `HUBSPOT_ACCESS_TOKEN`,
`CRM_PROVIDER=hubspot`, `uv run health.py crm`, then
`uv run campaign.py crm-sync --once` and `crm-status`. Expect the drill rows
from earlier phases to be filed too; look at the contact's `ai_*` properties
and the call engagement's body in the portal, and read `crm.schema_unavailable`
in the log as "grant `crm.schemas.contacts.write`".

**Phase 12's own loose end comes before anything else: place the real call.**
`uv run python tests/live_call.py --to <your phone>` with the bot up and a
tunnel in `TELEPHONY_PUBLIC_URL`. Follow the script it prints; read the
checklist. Then once more with `--machine-detection async` against a number
that goes to voicemail (`--expect-voicemail`), which is the only way to learn
whether SignalWire populates `answered_by` asynchronously — and, since Phase
14, whether it delivers the verdict to `AsyncAmdStatusCallback` (the ledger
will show an `amd` row if it does).

**Phase 14's loose end rides on the same call.** Set `SIGNALWIRE_SIGNING_KEY`
first (the dashboard's API credentials page), confirm the bot's startup line
says `webhooks at https://…/webhooks/telephony`, and after the call run
`uv run campaign.py webhooks`: four `unmatched` rows for that call id means
the carrier reached the receiver and the signature checked. `webhook.refused`
in the bot's log means the key is wrong; nothing at all means the URL was not
reachable from the carrier.

**Do these first, whatever Phase 13 turns out to be.** None is a phase; each is
small and each blocks honest work on anything else:

0. **Deal with `server/.env` being in git** — see [Assumptions §7](#7-assumptions).
   It carries the live Deepgram, Groq, Cartesia and SignalWire credentials and it
   is in the first commit, against a GitHub remote. Rotate the keys if that
   commit was pushed, untrack the file (`git rm --cached server/.env`), and add
   `.env` to a `server/.gitignore` — the root `.gitignore` does not cover the
   nested repository. Phase 9 went to some trouble to keep credentials out of
   logs; leaving them in version control makes that beside the point.

1. **Decide what to do about the Groq tier.** It is the one thing that makes
   Phase 7 unusable on a live call today, and it is not a code change. Options,
   cheapest first: `LLM_PROVIDER=cerebras` with a free key (larger free-tier
   budget; `llama-3.3-70b` is already the default model and the factory is
   wired), a paid Groq tier, or Anthropic/OpenAI with credits. Whichever it is,
   re-run `evals/sales/suite.yaml` afterwards — the tool-calling behaviour is
   model-dependent (Failed §16, §21) and the stage hints were tuned on Qwen.
2. **Run `uv run campaign.py init` against any database that predates
   2026-09-04.** It adds `callbacks` and `meetings` and is idempotent; done on
   this machine's database already.
3. **Make one real phone call, and try one transfer.** SignalWire credentials
   and a public URL are now in `.env` (they were not on 2026-09-03), so
   `uv run call.py <your number>` should ring you. Then set
   `TELEPHONY_TRANSFER_NUMBER` to a second phone, ask the agent for a person, and
   watch whether the call moves. Nothing in Phases 4 or 7 has ever spoken to a
   carrier.
4. **Set `CALENDAR_TIMEZONE`.** It is UTC and the startup log says so; the
   prospects are not in UTC.

Then, what Phases 9 to 11 leave for Phase 12, in the order I would rank them:

1. **The first unattended run against a real carrier.** Phase 13 built the
   scheduler and proved it against a stub carrier and the real database; what
   it could not do from this machine is let SignalWire ring a phone. One
   campaign, one prospect (your own number), `campaign.py run <it> --max-calls 1`,
   the bot up. Watch for: the `call.status` transitions arriving in order, the
   Phase 8 result landing, `campaign.completed` at the end.
2. **Carrier status webhooks.** The worker polls each call it follows every
   `WORKER_POLL_SECS` — one carrier request per live call per poll, which is
   fine at one call and five a second at ten. `store.apply_call_event` is the
   idempotent entry point; a webhook route on the bot's web server (the only
   HTTP server) would call it by `telephony_call_id` and the worker's poll
   would become a fallback. **Done in Phase 14**, in exactly that shape.
3. **A second worker**, if one machine's concurrency ceiling is reached. The
   reservation is already safe across processes; what is not shared is pacing
   (in-process; two workers halve the interval) and the in-flight set (each
   worker follows its own calls, and adopts unfollowed ones only at start).
   A database-side "last placement at" for pacing and a periodic adoption
   pass would be enough; a broker would not be.
2. **Pushing the result to a CRM.** **Done in Phase 15**, as the poller over
   unsynced rows (a separate `crm_sync` table, a HubSpot call engagement plus
   contact). What remains is a second adapter when somebody has a Pipedrive
   or Salesforce account to test against — three edits, per `crm/__init__.py`.
3. **Fewer tools per turn.** The token budget problem has a code-side lever the
   phase did not pull: advertise only the tools the stage can use, via
   `LLMSetToolsFrame` with a test for the handler-pruning race described in
   [Decisions](#phase-7). Worth ~100 tokens per tool omitted.
4. **Cal.com, live.** One account, one event type, one booking, and the two
   marked lines in `scheduling/calcom.py` either hold or get fixed.
5. **Answering-machine detection**, unchanged from the Phase 6 list, and now
   worth more: a voicemail that "agrees to Monday" would book a meeting.
6. **A warm transfer.** Conference the colleague in and announce the caller.
   A different carrier API and a second public endpoint; only after a blind
   transfer has been seen to work.

Whatever it is, **run `uv run health.py`, then the thirteen check scripts, then the
seven phone drills (`tests/phone_drill.py all`, bot up, no account), then the
sales suite** to confirm the baseline, and add scenarios alongside the feature
rather than after it. `uv run dashboard.py --once` is a quick way to see what
state the database is actually in before and after. If the sales suite is run, `campaign.py results`
afterwards is worth a look: an eval session has no attempt row, so it stores
nothing — the `fake_carrier.py --attempt` route is the one that does.

**After any unclean shutdown, run `uv run campaign.py recover`.** `campaign.py
call` runs it automatically before dialling, but a crashed bot leaves an
attempt live and a live attempt blocks its prospect until something resolves
it. `health.py` reports live attempts as degraded for exactly this reason.

---

## Attempted Approaches That Failed

Everything below was tried and did not work. Do not repeat these.

### 1. Assuming Flux reports no STT latency

**Tried:** Documented (in `metrics.py` and the README) that the `stt` stage would be absent on the Flux path, reasoning from a commented-out `stop_ttfb_metrics()` call in Flux's `_handle_update` with an explanatory comment saying TTFB is meaningless there.

**Why it failed:** The base `STTService` starts TTFB at VAD speech-end and stops it on the *final* transcript, independently of that commented-out line. The first real eval run printed `stt 954ms` on the Flux path, contradicting documentation I had already written.

**Learned:** A commented-out call in a subclass says nothing about what the base class does. Also: write the measurement documentation *after* seeing the first real measurement, not from reading the source. The corrected description is that `stt` and `turn-end` measure overlapping windows from the same start and come out near-identical on the Flux path.

### 2. Fixing the post-barge-in truncation with the system prompt

**Tried:** After finding that the LLM answered the post-interruption question with 2–3 tokens and stopped, added an explicit system-prompt line: *"Some of your earlier replies in this conversation end mid-sentence, because that is where you were cut off. Read those as unfinished… and always answer the latest question in full."*

**Why it failed:** `barge_in` failed **2/2** with that line verifiably live in the request (confirmed by grepping the running bot's logged system instruction). The model kept emitting 1–3 token replies. Worse, each truncated reply joined the context and reinforced the pattern — one interruption degraded every reply after it.

**What worked instead:** Marking the fragment itself. `mark_interrupted_reply` appends `" [cut off here — the user interrupted]"` to the interrupted assistant message in the context. Completion lengths went from 2–3 tokens to 8–58; `barge_in` went 4/4 then 8/8.

**Learned:** A model reads a bare fragment as a turn *someone chose to end there*, and it copies the pattern. Prompt instructions do not override what the conversation transcript demonstrates. When a model is imitating something wrong in its context, fix the context, not the instructions. (Verified separately that the marker never reaches TTS across 24 synthesis calls.)

### 3. Per-frame-type de-duplication in the turn-cycle observer

**Tried:** Inherited from Phase 1 — `TurnDiagnostics` logged each stage once per turn by tracking seen frame *types*, resetting the set on each `UserStartedSpeakingFrame`.

**Why it failed:** An observer sees each frame once per processor **hop**, not once per frame. One `UserStartedSpeakingFrame` crossing a seven-stage pipeline incremented the turn counter seven times, producing logs like `[turn 14] ... [turn 28]` for a three-turn conversation and making the trace useless. Error frames were logged seven times each for the same reason.

**Fix:** A bounded frame-ID set (deque + set, Pipecat's own pattern) checked *before* any other handling, with the per-type check kept underneath it for the legitimate case of several frames of one type in a turn.

**Learned:** In any Pipecat observer, de-duplicate on `frame.id` first. Pipecat's own observers do this; mine did not.

### 4. Ending the session with `stop_when_done()` right after queueing the goodbye

**Tried:** In the silence escalation, calling `worker.stop_when_done()` immediately after queueing the goodbye's `LLMRunFrame`.

**Why it would have failed:** `stop_when_done` queues an `EndFrame` behind what is already queued — but the goodbye is *generated* asynchronously, so the `EndFrame` races the LLM and cuts the agent off mid-sentence.

**Also rejected:** `on_assistant_turn_stopped` as the "audio finished" signal. It fires on `LLMFullResponseEnd`, before playout.

**What worked:** `worker.add_reached_downstream_filter((BotStoppedSpeakingFrame,))` plus `on_frame_reached_downstream`. The audio reaching the end of the pipeline is the only honest signal the last word was heard.

**Learned:** "The LLM finished" and "the caller heard it" are several hundred milliseconds and one whole utterance apart. For anything that terminates a session, wait for the audio.

### 5. Wrong signature on `on_user_turn_started`

**Tried:** `async def on_user_turn_started(aggregator, strategy, params)`, guessed from `UserTurnController._on_user_turn_started`, which does take a `params` argument.

**Why it failed:** The *aggregator's* event passes only `(aggregator, strategy)` — `_call_event_handler("on_user_turn_started", strategy)`. The controller's internal handler is a different thing with a similar name.

**How it hid:** Pipecat catches exceptions in event handlers, logs them, and carries on. The session looked completely healthy while the silence escalation never reset when the caller spoke, and the session summary reported `ERRORS | none`.

**Consequences:** This is why error counting is now a loguru sink over all ERROR records rather than a subscription to `ErrorFrame`.

**Learned:** Never guess an event handler signature from a similarly-named internal method — read the `_call_event_handler` call site or the class docstring's `Example::` block. And assume any handler exception will be silent.

### 6. Bash heredocs for writing Python files

**Tried:** `cat > src/config.py <<'PYEOF' ... PYEOF` to write a ~300-line module.

**Why it failed:** The Bash tool returned `unexpected EOF while looking for matching '` — the heredoc did not survive whatever wrapping the tool applies.

**Learned:** Use the `Write` tool for multi-line source files. Heredocs are fine for short YAML and shell snippets.

### 7. `uvx pipecat-ai-context-hub` for current API docs

**Tried:** The Context Hub (AGENTS.md rung 1/2) for verifying Pipecat APIs. `pipecat context-hub status` reported the `cli` extra was not installed; `uvx pipecat-ai-context-hub status` started downloading grpcio/onnxruntime/pywin32 and hit the 60s command timeout.

**What worked:** Rung 3 — reading the installed package source at `server/.venv/Lib/site-packages/pipecat`. The pinned version is on disk and cannot be stale. Every API in this phase was verified that way.

**Learned:** On a slow connection, rung 3 is faster than rung 2 and equally authoritative. Registering the hub for *future* sessions is still worth offering, but do not block on it.

### 8. Interrupting a model download

**Tried:** Running the first audio-mode eval under a 120s `timeout`, which killed Kokoro's download partway.

**Why it failed:** Left a truncated 53 MB `kokoro-v1.0.onnx` (real size ~310 MB). The next run failed with `[ONNXRuntimeError] : 7 : INVALID_PROTOBUF : Protobuf parsing failed`, which reads like a corrupt-model or version bug rather than a truncated download.

**Fix:** `rm -rf ~/.cache/pipecat/kokoro-onnx` and let it fetch again. On this connection the full download took ~25 minutes.

**Learned:** Run first-time model downloads in the background with no timeout. `INVALID_PROTOBUF` on a cached model means "delete it and re-download", not "debug it".

### 9. Redirecting bot stdout on Windows without setting the encoding

**Tried:** `uv run python bot.py > boot.log 2>&1`.

**Why it failed:** `UnicodeEncodeError: 'charmap' codec can't encode characters` — when stdout is a pipe rather than a console, Python falls back to cp1252 and Pipecat's startup banner contains box-drawing characters.

**Fix:** `PYTHONIOENCODING=utf-8` on every command whose output is captured.

### 10. Assuming port 7860 was free

**Tried:** Booting eval bots on the default port.

**Why it failed:** A pre-existing Python process (started before this session, most likely the user's own bot) held it. Killing it was not mine to do.

**Fix:** `--port 7861` / `7862` for manual runs; the suite assigns its own ports from 7900 upward, which is another reason to prefer the suite.

### 11. Leaving `idle_timeout_secs` at its production value for eval bots

**Tried:** Booting `-t eval` with the default `SESSION_IDLE_TIMEOUT_SECS=300`.

**Why it failed:** Under the eval transport the pipeline starts at **boot**, not at client connect. The first eval bot sat waiting through a 25-minute model download and self-terminated at 300s, so the eval run then failed with a connection error that pointed nowhere near the cause.

**Fix:** `SESSION_IDLE_TIMEOUT_SECS=3600` for every eval invocation, documented in `server/evals/README.md`.

### 12. Trusting a homemade loopback harness before trusting the bot (Phase 4)

**Tried:** Verifying the telephony audio path with a throwaway websocket client pretending to be Twilio. Its first run reported `RESULT: NO AUDIO` — zero events received, zero bytes back.

**Why it failed:** The harness, not the bot. Its receive loop was `asyncio.wait_for(ws.recv(), timeout=<all remaining time>)` and broke out of the loop on the first timeout — so it gave up the moment nothing arrived in the first instant, while the bot was still opening websockets to three vendors. With a one-second timeout and `continue` instead, the same bot returned 27.5 KB of μ-law and a spoken greeting.

**Learned:** When a test harness you wrote ten minutes ago disagrees with a bot that has passed an eval suite, suspect the harness. The bot's own log said `CALL | audio connected` and was the faster thing to read; going there first would have saved the round trip.

The harness became `tests/fake_carrier.py`, with the fix and the reason written into a comment on the receive loop, because the next person to write one will reach for the same wrong shape.

### 13. Setting `KB_ENABLED=false` in the shell (Phase 4)

**Tried:** `KB_ENABLED=false uv run bot.py` to A/B the agent with the knowledge base off.

**Why it failed:** `load_dotenv(override=True)`. `.env` wins over the shell environment, and `.env` says `KB_ENABLED=true`. The bot started, logged `KB=pgvector(...)`, and behaved exactly as before — no error, nothing to notice.

**Fix:** Edit `.env`. (`SESSION_IDLE_TIMEOUT_SECS=3600` works from the shell only because `.env` happens not to set it, which makes this trap worse: one env override appears to work and the next silently does not.)

### 14. Reproducing the reconnect bug by closing the connection cleanly (Phase 4)

**Tried:** Diagnosing "the browser connects once and then never again" by scripting a WebRTC client that connected, ran, called `pc.close()`, and connected again reusing its `pc_id`.

**Why it failed:** It passed, twice. A clean close makes aiortc's connection state go to `closed`, which fires Pipecat's `closed` event, which removes the connection from the runner's map — so the second offer creates a fresh connection and a fresh bot. Everything works, and the bug is invisible.

**What actually reproduced it:** a client that *stops* rather than closes — cancel the keep-alive pings, stop the outgoing track, never close the peer connection. That is a slept laptop or a dead wifi, and it is the case where nothing tells the server anything at all. The second connection then came back with the *same* `pc_id`, which is the tell: the runner reused the stale entry and renegotiated it, and renegotiation starts no bot.

**Learned:** when reproducing a disconnect bug, the disconnect has to be as rude as the real one. A first attempt that stops the media but leaves the data channel pinging does not reproduce it either — the far end still sees a healthy peer, which is exactly why the first "abandon" simulation also passed. The distinguishing signal to assert on is not "did audio arrive" but "did the runner hand back a new `pc_id`"; `tests/fake_browser.py` checks both.

### 16. Describing the tools only in the system prompt (Phase 6)

**Tried:** Eight tools advertised on the context, and a `RECORDING WHAT YOU LEARN` section in the system instruction saying "Call them as you learn things, in the same turn. Do not save them until the end of the call." The per-turn stage block said what the stage was for and ended with "One to three spoken sentences. End your turn with at most one question."

**Why it failed:** Groq/Qwen held a genuinely good discovery conversation and called **nothing**. Three turns, a prospect describing forty trucks and an out-of-control fuel bill on paper records, a reply that reacted to all of it — and no `record_discovery`. The handlers were registered (the bot's log shows all eight auto-registered), the schemas were correct, and the model simply never called one.

The cause is placement. The last line the model reads before generating is the strongest instruction in the whole prompt, and that line said *speak*. The tool instructions were a thousand tokens earlier, competing with the campaign brief, the prospect brief, the conduct rules and the conversation itself.

**What worked:** naming the *one* tool the current stage is most likely to need, in the stage block, immediately before the length rule — "If they just told you anything about their situation … call record_discovery FIRST, before you reply." The next run called `record_objection` on the opening push-back with the right `kind`, unprompted.

**And then it failed again, differently.** The first version of the hint was per-stage only, so a call sitting in `OBJECTION_HANDLING` got a hint about objections and nothing about discovery — and dropped "forty trucks, fuel bill out of control" on the floor, because learning something about a prospect is not confined to the stage called DISCOVERY. The fix is that the discovery line is emitted on *every* selling stage, with the stage-specific hint on top.

**Learned:** with a small open-weight model, where an instruction sits matters more than how emphatically it is worded — the same lesson as [Failed §2](#2-fixing-the-post-barge-in-truncation-with-the-system-prompt), from the other direction. There, a system-prompt line could not fix what the context demonstrated; here, a system-prompt line could not compete with the last line before generation. Both times the fix was to change what the model reads *last*.

### 17. Asserting a function call the deterministic detector makes unnecessary (Phase 6)

**Tried:** `evals/sales/do_not_call.yaml` asserting `function_call: do_not_call` after "Take me off your list and don't call this number again."

**Why it failed:** `signals.py` recognises that phrasing and forces the state before the model reads the turn, so the model's next reply is generated with an override in front of it saying "confirm the removal and end the call" — which it did, perfectly: *"Understood, I'm removing your number from our list so you won't be called again. I'm sorry for the interruption."* It had nothing left to record, so it called nothing, and the scenario failed while the product behaved exactly as designed.

Worse, the failure cost the *next* assertion too. A function-call expectation that never matches burns its whole 60-second budget, and the `response` expectation behind it then timed out on a response that had already arrived.

**What worked:** splitting the scenario in two. `do_not_call.yaml` uses a phrasing the detector catches and asserts the behaviour; `do_not_call_implicit.yaml` uses one it does not — "I'd appreciate it if you didn't contact me about this again" — and asserts the tool call. Each proves one mechanism without the other propping it up, which is the whole reason for having two.

**Learned:** an eval that asserts a mechanism rather than an outcome fails when a *different*, equally correct mechanism does the job. Assert what the person on the phone would notice, and reach for the implementation-level assertion only where you have arranged for exactly one implementation to be able to act.

### 19. Advertising twelve tools with no output cap on Groq (Phase 7)

**Tried:** Booting the Phase 7 bot exactly as Phase 6's had booted — no
`max_tokens`, no `max_completion_tokens` — and running the `meeting` scenario.

**Why it failed:** Every LLM request was refused: `429 Request too large for
model qwen/qwen3.8-27b … on output tokens per minute (OTPM): Limit 1000,
Requested 1815. The request's expected output tokens exceed the enforced limit;
reduce max_tokens`. The bot answered, logged a healthy session, and said
nothing. The error is only in the bot's log; the eval reported "no response text
yet".

**Cause:** With no cap, Groq *estimates* the reply — at 1,815 tokens for this
request — and checks the estimate against the per-minute output limit. Phase 6
had never hit it; a longer prompt and four more tools moved the estimate over.

**What worked:** `LLM_MAX_OUTPUT_TOKENS` (default 400), always sent as
`max_completion_tokens`. `make_llm` finds the field on the provider's `Settings`
by inspecting the dataclass — the first attempt used `__annotations__`, which
does not include inherited fields, and silently applied nothing. Verified by
constructing the service and reading `_settings.max_completion_tokens` back.

**Learned:** A voice agent should always cap its output; and when a provider's
error says "reduce max_tokens", it means the *estimate*, and the fix is to send
one.

### 20. Trusting the eval's 60-second window on a free Groq tier (Phase 7)

**Tried:** With the cap in place, running `meeting` again. The greeting came in
1.4 s; the next turn took 18 s; the tool turn took 49 s and the pipeline's
heartbeat monitor warned that a processor looked stalled.

**Why it failed:** The organisation's limit is 8,000 tokens a minute
(`x-ratelimit-limit-tokens`, read from a direct request). A request with the
twelve tools advertised was 3,843 prompt tokens. The second request inside a
minute gets a 429 with `retry-after`; the OpenAI SDK inside Pipecat's Groq
service honours it and retries **without a line in the bot's log**, because its
logging goes to the standard-library logger loguru does not display. The bot
looked stalled; it was waiting.

**What worked, partly:** Cutting the tool docstrings to one sentence and
argument formats and halving the prompt's tool section: 3,094 tokens per
request. The calendar hint moved into the per-turn block, where it is read once,
instead of the system prompt, where it is read every turn. And the scenarios
that involve a tool carry `within_ms: 150000` on those turns, so the eval
measures the bot rather than the tier.

**What did not:** Getting under the budget. ~1,500 of the 3,094 tokens are the
tool schemas, and Groq's chat template costs about a hundred tokens per tool
before any description. Two requests per tool turn is the shape of function
calling. The tier is the constraint, and it is the user's decision.

**Learned:** Measure the prompt in tokens against the provider's *headers*, not
in characters against a guess, and do it before writing the docstrings. Twelve
tools is not free even when each is short.

### 21. The calendar hint only on the meeting stage (Phase 7)

**Tried:** Putting "call `check_calendar_availability` for the day they prefer"
in the `MEETING_REQUEST` stage hint and the system prompt, mirroring where Phase
6 put the meeting hint.

**Why it failed:** The prospect said "would Monday morning work on your side?"
during discovery, and the model replied "I can check what I have free on Monday
morning — shall I look up a couple of slots for you?" — asking permission to
call a tool. The call was in `DISCOVERY`; the block in front of the model said
nothing about a calendar; the system prompt's instruction was a thousand tokens
earlier. Failed §16 again, from the other side.

**What worked:** `_MEETING_TOOL_LINE` on every selling stage when the session
can book, worded as an order — "do not ask whether to check — call
check_calendar_availability NOW with that day as YYYY-MM-DD". The next run
called it with `2026-09-07`, the correct Monday, worked out from the time block.

**Learned:** The same lesson as §16, and it will apply to every action tool
added later: the line that names the tool has to be in the *last* block before
generation, on every stage where the trigger can occur, and it has to say
"call", not "you can call".

### 22. Smaller things that cost time (Phase 7)

- `pipecat.evals suite -s` takes **one** scenario. `-s a -s b` runs only `b`,
  silently. Run the suite once per scenario, or without `-s`.
- `load_dotenv()` with no path raises `AssertionError` when the script arrives on
  stdin (`python - <<EOF`): it walks the call stack to find the caller's file.
  Pass the `.env` path, or write the script to a file.
- `dataclasses.fields()`, not `__annotations__`, to enumerate a dataclass's
  fields when they are inherited. Cost one boot cycle.
- A long-running eval bot carries its context across scenario runs (documented
  in Phase 2, forgotten here): a second `meeting` run against the same bot is
  not a clean run. Boot fresh, or use the suite.

### 23. Reading the attempt status from the final state (Phase 6, found in Phase 8)

**What was there:** `CampaignConversationSink.on_call_finished` mapped
`outcome["final_state"]` through `{DO_NOT_CALL, CALLBACK, NOT_INTERESTED}` to
set the attempt's status — the mechanism the Phase 6 notes describe as "the
three human-judgement attempt statuses are finally set".

**Why it was wrong:** `end_call` moves the state to `ENDING`, and the
transition table allows `ENDING` from all three. So on every call the agent
closed properly — the normal case — the final state was `ENDING`, the mapping
returned nothing, and the attempt was left to the carrier as `COMPLETED`. The
status was written only when the line dropped *before* the goodbye. Nothing
noticed for two phases because the sales evals run with no prospect row (a
`LoggingSink`), and `test_conversation.py` asserts on the *state path*
(`state_path[-2] == "DO_NOT_CALL"`) rather than on what the sink wrote. The
prospect was still marked do-not-call mid-call by `on_do_not_call`, so the
safety property held; only the attempt's label was lost.

**How it surfaced:** the first three `test_results.py` scenarios built their
result with the status the sink would set and asserted `CALLBACK_REQUESTED`,
`NOT_INTERESTED`, `DO_NOT_CALL` — and got `COMPLETED` for all three.

**Fix:** `results.attempt_status_for(outcome)` reads the last non-`ENDING`
state on `state_path` (a `DO_NOT_CALL` anywhere wins) and the sink uses it.
Nine checks pin the rule, including "a callback that went back to discovery is
not a callback".

**Learned:** a test that asserts on the *record* ("the path went through
DO_NOT_CALL") does not test the *consequence* ("the row says DO_NOT_CALL").
When a phase writes something to a table, assert on the table.

### 24. Smaller things that cost time (Phase 8)

- The first question heuristic kept `"Hello?"` (one word, question mark) and
  `"Can't say I'm keen."` (`cant` was in the interrogative list to catch
  "can't you…"). Now: a question mark needs two words, and the negative
  contractions are out. Both cases are in `check_questions`.
- `build_conversation_result` on an empty `{}` record counted transcript
  turns as zero and the summary said "the other end did not speak" — inferred
  from nothing. The fallback to transcript counts now applies only when a
  transcript was actually present; otherwise the count is unknown and the
  summary says nothing about it.
- A carrier failure reason that already ends in a full stop produced
  "…limitation.." in the summary. Seen on the real attempt 1.
- `Disposition` has no `UNKNOWN` member (a disposition is always derivable),
  so the "everything UNKNOWN on an unanswered call" loop in the validator
  raised `AttributeError` on it until the loop was given its own field list.
- The SQL check counted five results for five attempts; one attempt was left
  `CONNECTED`, which correctly has none.

### 25. Counting an LLM failure and then immediately forgetting it (Phase 9)

**Tried:** `SessionSupervisor` counted consecutive `ErrorFrame`s per stage and
reset the count on a success — where "success" for the LLM was
`LLMFullResponseEndFrame`.

**Why it failed:** Pipecat pushes that frame from a `finally` block, so it
arrives after a *failed* request too. Every LLM error was therefore followed
immediately by something that reset its own count, and the threshold could
never be reached. An LLM refusing every single request looked perfectly
healthy.

**How it surfaced:** running a real call while Groq's daily token budget was
exhausted. Two complete LLM failures in one session, and the end-of-session
line still said `no service failures`. No unit check would have caught it —
the frame ordering is Pipecat's, and a stub would have been written to match
what I believed it was.

**Fix:** an inference counts as a success only if it produced a token
(`LLMTextFrame`). `note_llm_output` is checked before the end frame.

**Learned:** "the operation finished" and "the operation worked" are different
questions, and a framework that reports completion in a `finally` answers only
the first. The same trap is in `metrics.py`'s TTFB and in Failed §4 — the third
time this project has been bitten by treating an *end* signal as a *success*
signal.

### 26. A threshold that cannot be reached because nothing tries again (Phase 9)

**Tried:** With §25 fixed, `MAX_SERVICE_FAILURES=2` and a real Groq outage,
expecting the call to end after two failed inferences.

**Why it failed:** one failure, then nothing. The greeting's inference failed,
so the agent never spoke — and the silence escalation that would have produced
a second inference is armed by `BotStoppedSpeakingFrame`, which only fires when
the agent *has* spoken. So exactly one LLM request was ever made, the count
stopped at one, and the call sat in silence for 57 seconds until the session
idle timeout.

**Fix:** one failure is fatal on its own if the agent has not yet said a word,
whatever the threshold. There is nothing to recover to on a call that never
started. Re-run live: the call now ends in 4 seconds with
`session.dead_on_arrival` and a reason, and the end-of-session summary says
`THE AGENT NEVER SPOKE`.

**Learned:** a threshold assumes the event can recur. Before setting one, ask
what *causes* the second occurrence — here nothing did, because the retry
mechanism was itself downstream of the thing that had failed.

### 27. Smaller things that cost time (Phase 9)

- **`Decision.__bool__` returning False for a refusal** made
  `decision or None`, `if decision:` and `x if decision else y` all read a
  refusal as *absence*. It produced a real bug in `dialer._check_guards` (a
  closed calling window silently allowed the call) and then, an hour later, an
  identical bug in the check that was meant to catch it — which is how the
  first one stayed hidden. Now documented on `__bool__`, with `Decision.refused`
  and `DialResult.refusal` as the forms that cannot be got wrong.
- **`src/reliability/` importing `src/campaigns/`** made the two circular the
  moment `service.py` needed an idempotency key. Recovery moved to
  `campaigns/recovery.py`, which is where `dialer.py` and `briefing.py` already
  established that a module joining two worlds belongs. `health.py` imports the
  store inside a function for the same reason.
- **Two `logger.configure(patcher=...)` calls do not compose** — the second
  replaces the first. `install_scrubber` was silently removing the patcher that
  rendered the call context, and every log record then raised `KeyError:
  '_context'` inside loguru. Split into `load_secrets` (no patcher) and
  `install_scrubber`, and `extra={"_context": ""}` supplies a default so a
  future third caller degrades to a plain line instead of an exception.
- **`.env` beats the shell** (Failed §13, again). Two Phase 9 manual tests set
  `SIGNALWIRE_SPACE_URL` in the environment and watched the real carrier answer
  anyway, because `campaign.py` calls `load_dotenv(override=True)` at import.
  Set it *after* importing the module, or edit `.env`.
- A `CREATE INDEX IF NOT EXISTS` does **not** update an existing index whose
  predicate changed. The live-attempts partial index had to be dropped and
  recreated when `UNRESOLVED` joined the live set, or it would have silently
  stopped being used by the query that needs it most.

### 28. Indexing a full-table aggregate (Phase 11)

**Tried:** The dashboard's aggregates were the slowest thing measured
(`campaign_result_counts` 77 ms, `result_counts` 64 ms, `campaign_overview`
42 ms over 60,000 rows), and every plan showed a sequential scan. Six indexes
that looked exactly right: a partial index on callable prospects for the
queue's join, `(campaign_id, qualification_status, prospect_id)` for the
qualified-prospect count, `(disposition)` for the group-by, a partial index on
booked meetings, `(status)` on attempts, and a covering
`(campaign_id, status) INCLUDE (duration_seconds)`.

**Why it failed:** every measured query got **slower**, several by 20–60%.
`count(*) FILTER (...)` over a whole table *is* a sequential scan — there is no
subset to seek to — and adding indexes gives the planner more options to
consider, more pages to keep warm, and more to maintain on every write, while
removing no work at all. `reserve_next_call` went 11.3 → 17.9 ms; the
disposition group-by 18.6 → 24.8 ms.

**What worked instead:** not indexing but *not repeating*. The queries were
made concurrent (249 → 149 ms) and the result cached for five seconds (21
viewers → 1 read). The cost of an aggregate is the scan; the fix is to run it
fewer times, not to make one scan cheaper.

**Learned:** an index helps a query that wants a *subset*. Before adding one,
ask what it would let the planner skip — and if the answer is "nothing, it
still reads every row", it is a write-cost regression with no upside. Also: the
first benchmark run's numbers were 2–5× the warm ones, so a cold-cache
measurement nearly justified the wrong fix.

### 29. Believing a zero that nobody measured (Phase 11)

**Tried:** Summing Pipecat's usage metrics and reporting the totals, including
`tts.characters`.

**Why it failed:** a live call came back with 3,394 prompt tokens, 16.6 seconds
of transcribed audio, and **0 TTS characters** — after the bot had audibly
spoken. Pipecat 1.8.1's Deepgram TTS has two classes and only the HTTP one
calls `start_tts_usage_metrics`; the websocket one, which `services.py` builds,
reports nothing. Cartesia reports both. So the zero was not a measurement, it
was an absence — and with a TTS rate configured it would have been multiplied
into a cost total that was quietly too low.

**Fix:** each stage carries `reported`, and `estimate_cost` prices a stage only
when a provider actually reported it, naming the rest under `unmeasured` with
`complete: false` on the total.

**Learned:** the same rule Phases 6, 8 and 10 kept arriving at, in a new place:
*an absent measurement is not a zero*. A summary that omits a line and one that
reports a zero look identical and mean opposite things — and for money the
difference is a bill.

### 30. Smaller things that cost time (Phase 11)

- An f-string containing `{usage_columns}` for a later `.format()` evaluates it
  immediately and raises `NameError`. Escape as `{{usage_columns}}`.
- `attempt_counts` grew two columns that an un-migrated database does not have,
  which would have broken the whole dashboard rather than one tile. It now
  builds the query with or without them and reports `usage_available`.
- The first race check passed *vacuously*: `len(reserved) <= 2` is also true
  when the queue hands out nothing. Bounded on both sides.
- A probe that wrote `{}` to `call_attempts.usage` made the dashboard report
  "1 call with usage" from an empty record. `{}` is not NULL.

### 31. Reading the carrier's audio only once the session was ready (Phase 12)

**Tried:** Nothing, which is the point — this was the behaviour every phase
since 4 had shipped. The carrier opens the media stream when the call is
answered and sends a 20 ms frame every 20 ms; `bot.py` then spent ~8 s
building the session (a 4.3 s first import of the Groq/OpenAI SDK, 1.1 s
loading the embedding model, pools, the Silero and Smart Turn models, two
vendor websockets) and read nothing until the pipeline started.

**Why it failed:** The frames piled up in the socket and arrived in a burst
when the input transport started. Flux works through audio at roughly real
time, so it began the call ~8 s behind and caught up slowly. Measured on the
first `barge_in` drill: the caller's first turn was detected **5.5 s** after
it began, the second **3.4 s**, end-of-turn came 4.8 s late, and both
interruptions reached a bot that had already finished the reply they were
meant to cut off — the carrier saw two `clear` events and the report saw zero
barge-ins. The person also heard nothing for 15 s after picking up. None of
the eval scenarios could see it, because the eval harness only starts sending
audio once the bot reports ready.

**What worked:** Read and drop the backlog right before `runner.run()`
(`_drain_stale_audio`: stop at the first 100 ms window that arrives at
real-time rate, 3 s cap), and do the two one-off loads once per process at
startup. Greeting 4–7 s after connect; turn-start detection 640–1203 ms; the
same interruption stopped the bot in 141 ms.

**Learned:** Anything that measures the phone path has to start streaming
from the handshake, as a carrier does — which is what `tests/phone_drill.py`
does and the eval transport does not. And a per-session cost that looks
harmless in a browser (where the client waits for "ready") is paid by a
person listening to silence on a phone.

### 32. Judging a caller's first turn by its length alone (Phase 12)

**Tried:** The voicemail detector's second rule: a first caller turn that
runs longer than `VOICEMAIL_MAX_GREETING_SECS` (8 s) without ending is a
recording.

**Why it failed:** The `rapid` drill answers the agent's opening question
with a fast, dense, eight-second sentence. The turn was still open at 8.3 s,
the watchdog fired, the bot logged `voicemail.detected | greeting_length` and
hung up on the caller mid-sentence. A person who lets the agent finish and
then talks at length is the prospect the whole product exists to reach.

**What worked:** The rule now applies only to a turn that began *over the
agent's audio*, or before the agent had spoken at all — which is what a
recording does and a person almost never does — and `bot.py` passes that from
the turn monitor's record rather than from the live "bot speaking" flag,
which the interruption may already have cleared. The phrase rule is
unconditional. Both are unit-tested against exactly this case.

**Learned:** A hang-up heuristic must be wrong in the cheap direction. Missing
a machine costs one voicemail's worth of airtime; hanging up on a person costs
the call.

### 18. Considered and deliberately rejected

- **Lowering `FLUX_EOT_THRESHOLD` to cut the 653ms `turn-end`.** Tempting, and the biggest single latency lever. Rejected because the only evidence available is synthesized speech with clean endings; nothing here justifies a claim that a lower threshold is safe with real callers, and the failure mode is cutting people off. Left at Deepgram's default and documented as a knob.
- **Enabling `FLUX_EAGER_EOT_THRESHOLD`.** Verified by searching the installed source that nothing in Pipecat's aggregator consumes eager end-of-turn. Setting it produces extra events and no speedup. Exposed with that written on it.
- **Overriding turn strategies on the Flux path.** Would silently disable server-side end-of-turn detection while appearing to work.
- **Hand-tuning VAD timings.** AGENTS.md warns against it and the defaults are tuned; on the Flux path `stop_secs` does not affect responsiveness at all.
- **Writing a Telnyx provider alongside the Twilio one (Phase 4).** Tempting, because it would "prove" the abstraction and Pipecat already ships the Telnyx serializer. Rejected: with no Telnyx account it would be a hundred lines of unrunnable code presenting itself as a working feature, and the first person to try it would be debugging my guesses. The seam is real without it — `src/telephony/__init__.py` documents the four edits a second carrier needs, and four carriers already work on the audio side.
- **Twilio status-callback webhooks instead of polling (Phase 4).** Better data, sooner. Rejected for now: it needs a second public endpoint mounted on the dev runner's own FastAPI app, receiving events for calls this process may not have placed. Polling once a second reports the same six outcomes and needs nothing. Revisit when a campaign runner places calls in parallel.
- **A `--telephony` flag or a separate telephony bot (Phase 4).** Rejected because the runner already serves every transport at once: `uv run bot.py` accepts a browser on `/client` and a carrier on `/ws` in the same process, and a flag would only add a way to have the wrong one running.
- **Defaulting `ECHO_SUPPRESSION` to `always` (Phase 4).** It is what a laptop with no headphones needs, and it was still rejected: it disables barge-in, which Phase 2 built and `barge_in.yaml` asserts. Trading a loud problem for a silent regression of a tested feature is the wrong direction, and headphones fix the echo for free.
- **Detecting self-echo by comparing the transcript to what the agent just said (Phase 4).** Tempting, because it would keep barge-in. Rejected: it only catches a clean echo of a recent sentence, and the observed transcripts degrade into things like "Okay." and "No, I am" that match nothing. A heuristic that works on the easy half of the problem would make the remaining half harder to diagnose, not easier.
- **Pipecat Flows for the conversation state machine (Phase 6).** The obvious candidate, and AGENTS.md's own routing heuristic is what rules it out: Flows is for when the prompt *and the tool set* must change per stage, or the process must be enforced rather than suggested. Here the tools are the same in every stage — you can be told a pain point at any point in a call — and what changes per stage is one paragraph of guidance, which a copy of the context carries for a tenth of the complexity. Flows would also have owned the transitions, and the two transitions that matter here are promises to the person on the phone; they are worth owning in fifty lines that can be read in one sitting. Its API also churns, which AGENTS.md warns about explicitly.
- **A second LLM pass to extract the qualification record from the transcript (Phase 6).** It would remove the dependence on the model remembering to call a tool, which is the one thing that varies. Rejected: it is a whole extra inference per turn on a latency budget Phase 2 spent its entire effort getting to 1.3 seconds, and a second opinion about what was just said is not more reliable than the first — it is the same model reading the same words with less context. If it comes back, the right shape is one pass at the *end* of the call, where latency does not matter, and as a supplement to the tools rather than a replacement.
- **Letting the model set `qualification_status` directly (Phase 6).** One fewer derived field and one more tool argument. Rejected because "qualified" would then mean whatever the model felt on the day, and a campaign report that counts qualified leads needs the word to mean the same thing on every call.
- **Making `NOT_INTERESTED` absorbing, like `DO_NOT_CALL` (Phase 6).** Simpler, and it would have made "stop selling" airtight. Rejected because people do change their mind mid-call — "actually, hang on, what does it cost?" — and a state you cannot leave would make the agent refuse to answer them. The rejection row keeps `CALLBACK` and `ENDING` reachable and drops only the routes back to pitching, which is the behaviour that was actually objectionable.
- **Forcing `NOT_INTERESTED` from the rejection phrase detector (Phase 6).** It would guarantee the agent never pushes past a no. Rejected: "I'm not interested in switching right now, but tell me more" is a real sentence, and ending live calls on the word "interested" trades a rare failure for a common one. The detector raises guidance instead, and the model decides.
- **Writing the whole qualification record into the campaign tables as columns (Phase 6).** Rejected in favour of one `jsonb` column: the record's shape is still moving, and a column per field would mean a migration every time the phase after this one wants to know something new.
- **Advertising fewer tools per turn to stay under Groq's token budget (Phase 7).** The director already sends the LLM a *copy* of the context, so it could carry a stage-filtered tool list — `book_meeting` only after slots were offered, `transfer_to_human` only on a call — and cut ~100 tokens per tool omitted. Untried, for a reason found in the installed source: Pipecat prunes a tool's registered handler when a later context stops advertising it (`_register_advertised_tool_handlers`, "advertised-tool-set-managed … pruned on a later sync"), so a per-turn filter would register and unregister handlers every inference and could unregister one mid-call. Worth doing properly with `LLMSetToolsFrame` and an explicit test of that race; not worth doing under time pressure.
- **Natural-language date parsing for `schedule_callback` and `check_calendar_availability` (Phase 7).** A library would turn "next Tuesday" into a date most of the time. Rejected: the failure is a person phoned on the wrong day, the cost of refusing is one sentence ("which day and time?"), and the model — told the current date, time and weekday in its instructions — turns "next Tuesday" into `2026-09-08` itself. The tools take ISO 8601 or say why not.
- **Calendly as the calendar provider (Phase 7).** Named in the requirement alongside Cal.com. Rejected because Calendly's API cannot create a booking, only issue scheduling links, and a voice agent has nobody to hand a link to. The abstraction is provider-neutral; a Calendly provider would have to lie about `book`.
- **Warm transfer — conference the colleague in and announce the caller (Phase 7).** Would be kinder to the person than a blind redirect. Rejected: it is a different carrier API (`<Conference>`/`<Dial>` with a callback URL, or a second call leg the bot orchestrates), needs a second public endpoint, and nobody has watched even a blind transfer succeed yet. Blind first; the seam is `transfer_call`.
- **A `meetings` write from the calendar provider itself (Phase 7).** The local provider could insert its own row. Rejected so that every provider's booking is recorded the same way, by the action service, after the provider confirmed — which also means the local provider is a check and the service is the write, and the honesty rule ("success only after the durable write") is enforced in one place.
- **An LLM-written summary at the end of the call (Phase 8).** The shape Phase 6 said a second pass should take if it ever came back. Rejected for this phase: a CRM summary has to be provably free of invented facts, and only a summary composed from recorded fields can be proved so. It also costs a request per call on a tier that is already the bottleneck. The record has every fact the summary states; if prose quality ever matters more than provability, the six deterministic parts are the input a model should be given, not the transcript.
- **A `record_question` tool, so the model lists the prospect's questions (Phase 8).** A thirteenth tool on a prompt that is over budget (Failed §19–§20), for a field a filter over the transcript supplies for free. Rejected.
- **Storing the transcript only in `call_results` and stripping it from `conversation_data` (Phase 8).** One home per fact is the project's habit. Rejected because `conversation_data` is the raw record a result is rebuilt from, and a raw record missing the transcript would make a rebuild lossy. The duplication is a few kilobytes.
- **Refusing a malformed outcome in the builder (Phase 8).** Strictness at the wrong layer: the call is over, and a refused build is a lost result. Tolerate and name in the builder; refuse in the validator.
- **Putting `CallResult` in `models.py` (Phase 8).** It needs the conversation's enums for its vocabulary, and `models.py` is the campaign's own nouns with no upward import. A module of its own, `results.py`, pure like `csv_import.py`.
- **An idempotency key on the carrier's call-creation request (Phase 9).** The textbook answer, and neither Twilio nor SignalWire offers one for the Calls resource — checked before designing around it. That absence is *why* `place_call` is never retried and why `find_recent_calls` exists: without a key the only way to answer "did it happen" is to ask what exists.
- **A lock table or a lease with an expiry for reservations (Phase 9).** The usual shape for "one worker holds this work". Rejected: the attempt row already is the lease, its live status already blocks, and an expiry would need a clock that the database and every worker agreed on. Recovery breaks a stale lease by finding out what actually happened, which is strictly better than guessing from a timestamp.
- **A message broker, or a scheduler daemon, for pacing and concurrency (Phase 9).** The phase's own instruction was not to add distributed infrastructure unless necessary, and it is not: correctness is in PostgreSQL and rate is in-process. The honest cost — a second dialer would exceed the intended rate, though it could not duplicate a call — is written into `guardrails.py` and Known issues rather than papered over.
- **Retrying `place_call` on a 5xx (Phase 9).** A 5xx is a server error, so the request "obviously" failed. It is not obvious at all: the carrier may have created the call and failed while answering. Treated as ambiguous with everything else.
- **`ProcessorUnusablePolicy.END` instead of the supervisor (Phase 9).** Pipecat can end the pipeline itself when a processor reports it can no longer work. Rejected because it ends the call *immediately* and silently: no goodbye, no distinction between a stage the agent needs to speak and one it does not, and no count of how many failures preceded it. The supervisor does all three, and the default `CONTINUE` leaves it in charge.
- **A distinct `Disposition` for a call the supervisor ended (Phase 9).** It would read well in a CRM. Rejected for this phase: it changes Phase 8's closed vocabulary and its validation rules for a case that is already recorded — a note on the result and a reason in the log. Worth revisiting if supervised endings turn out to be common, which would itself be the more interesting finding.
- **Advertising fewer tools per turn (Phase 11, reconsidered and rejected again).** Phase 7 deferred it and listed it as Phase 8's third-ranked item; Phase 11 measured what it is worth and read the source to decide. It is worth 300–500 tokens of a 3,394-token request, 10–15%. It is still not safe: `LLMService.process_frame` calls `_sync_registered_tool_handlers(frame.context.tools)` on **every** `LLMContextFrame`, and that unregisters any auto-registered handler the frame does not advertise. A stage-filtered list would therefore register and unregister handlers around every inference, in the layer that carries `mark_do_not_call`. The prize is 12% of a prompt; the risk is a tool handler missing at the moment it is called. Revisit only with `LLMSetToolsFrame` and a test that drives the unregister race directly.
- **Replacing a provider for speed (Phase 11).** Explicitly out of scope, and the measurement agrees: the dominant latency is turn detection at 653 ms, which is Deepgram's own tuned default, and the dominant cost is prompt size rather than per-token price.
- **Caching embeddings per turn (Phase 11).** Retrieval was a suspect before it was measured. It is 29 ms of a ~1,300 ms turn — 2% — and a cache would add a correctness question (a stale embedding for an edited turn) to save nothing anybody can hear.
- **A materialised view for the dashboard aggregates (Phase 11).** The textbook fix for a slow aggregate. Rejected: it needs a refresh schedule, which is the analytics infrastructure Phase 10 was told not to add, and it trades freshness for speed the cache already provides without either.
- **Deriving a prospect's timezone from their phone number (Phase 9).** Rejected for the same reason Phase 5 refused to guess a country for an un-normalisable number: it is right most of the time and invisibly wrong for every country with more than one zone, and the failure is a call at the wrong hour.

---

### 33. What the scheduler found in the dialling path (Phase 13)

None of these was a scheduler bug. Each was in code that had passed every check
since Phase 5 or Phase 9, and each surfaced within minutes of running a loop
that exercised the path *completely* instead of one call at a time.

**The last permitted attempt never dialled.** `reserve_next_call` increments
`attempt_count` in the transaction that hands the call out, and `dial()` then
re-checks the *returned* membership with `check_callable`, whose limit test was
`attempt_count >= max_attempts`. So the third of three attempts — or the only
one of one — was always released as "attempt limit reached (3/3)" and the
membership exhausted, without the carrier being asked. Every existing check used
`max_attempts=2` or `3` and dialled once; the first `test_worker.py` retry
check dialled twice and found it, and a probe against the real database with
`CAMPAIGN_MAX_ATTEMPTS=1` placed nothing at all. Fix: when `ignore_attempt_id`
is given the count already includes this attempt, so the check is against
`attempt_count - 1`. **Learned:** a check that asserts on the *record* ("the
membership went to EXHAUSTED") does not test the *consequence* ("a phone
rang"); and a rule with a boundary needs a check *at* the boundary.

**A ringing phone exhausted its membership.** `dialer.refresh` calls
`record_outcome` for every applied carrier event, including `QUEUED`,
`CALLING` and `CONNECTED`. `record_outcome` was written for final statuses:
`CALLING` is neither `reached_person` nor `should_retry`, so it fell through
to the `else` branch and set the membership `EXHAUSTED` — until the next event
overwrote it. Harmless for one call watched by one command; visible on a
dashboard mid-ring; and fatal the moment a guard was added that leaves a
closed membership alone. Fix: a live status changes the attempt and nothing
else. **Learned:** a function named for the end of a thing will be called in
the middle of it; guard on `is_final` rather than on which branch happens to
be last.

**The pre-dial safety check read a stale row.** `check_callable`'s docstring
says "read fresh, not the copy the queue handed out"; `dial()` passed
`queued.prospect`, the copy read inside the reservation. The Phase 5 check
that covers "marked DNC between reserving and dialling" builds a fresh
`QueuedCall` by hand before calling `dial()`, so it passed while `dial_next`
— the path everything real uses — did not re-read. The worker's version of
that check marks the prospect from inside the reservation hook and found it.
Fix: `dial()` reads the prospect from the store before the check. **Learned:**
a test that prepares the input the code should have fetched is testing its own
preparation.

**A duplicate placement left the attempt live for ever.** `_record_placement`
hung up the duplicate call and returned an error, but did not release the
attempt, which stayed `CALLING` with no call id. Recovery could only find the
same call id and fail to adopt it. Surfaced when a stub carrier reused call
ids across two sections of one PostgreSQL check: the next campaign's worker
was told "concurrency limit reached: 1 call(s) live" 202 times. Fix: release
the attempt after the hang-up. **Learned:** every non-ambiguous failure path
out of `dial()` must end in `release` or `defer`; the docstring said so and one
branch did not.

**Smaller things that cost time:**

- Bash heredocs on this machine (Failed §6, again): the second multi-line
  heredoc of the session was silently truncated and nothing was written.
  Every patch after that was a script written with the Write tool and run
  with `python`.
- A `FOR UPDATE ... SKIP LOCKED` statement with the row pinned by id returns
  nothing when the row is locked by another reservation — which is the right
  answer ("someone else has it"), but reads like "not eligible" in a log.
  `reserve_membership` says so in its docstring.
- `test_worker.py`'s first end-to-end check used `--max-calls 2` and asserted
  the campaign completed: it did not, because the worker stops asking the
  queue once the cap is reached and completion is decided when the queue says
  "nothing". The worker now assesses campaigns after the cap too.
- A callback scheduled *after* the campaign had completed in the test — in
  reality the row is written mid-call, before the outcome — put three checks
  on the wrong side of the completion. The checks now schedule it while the
  call is connected, as the action backend does.

### 34. Smaller things that cost time (Phase 14)

- **A FastAPI handler whose `Request` annotation cannot be resolved becomes
  a query parameter.** `src/campaigns/webhooks.py` first imported `Request`
  *inside* `create_webhook_router`, under `from __future__ import
  annotations`; FastAPI resolves the string annotation against the module's
  globals, found no `Request`, and served every POST a 422
  `{"loc": ["query", "request"], "msg": "Field required"}`. The import moved
  to module level. **Learned:** with postponed annotations, anything a
  framework introspects has to be importable from the module namespace.
- **`include_router` in this FastAPI (0.141) wraps the routes.** `app.routes`
  holds an `_IncludedRouter` with no `path` or `methods`; the real routes are
  under its `original_router.routes`. `test_webhooks.py` walks that.
- **The route's own URL is the wrong one to verify against**, and it is the
  one every example reaches for (`request.url`). Behind a tunnel it is
  `http://testserver/…` or `http://localhost:7860/…`; the carrier signed the
  public `https://` address. The check that signs over the server's own URL
  and is refused is in the test so this cannot regress quietly.
- **SignalWire's signing key is not the API token**, and its documentation
  says so only in passing ("your personalized signing key in the API
  Credentials space"). Its SDK settled it: `signalwire.request_validator.
  RequestValidator(token)` wraps Twilio's validator with whatever it was
  given, and the examples give it the signing key. Reading the SDK was faster
  than the docs.

### 35. Smaller things that cost time (Phase 15)

- **A scripted failure queue on a *read* is spent by the read's own
  retries.** `find_contact` goes through `READ_POLICY` (three attempts inside
  one pass), so a queue of two failures was consumed before the second pass
  and the "out of attempts" scenario synced on pass two. The scenarios that
  count passes script the *write* (`create_activity`, one attempt per pass).
  **Learned:** know which policy the call under test runs under before
  scripting its failures.
- **`Retry-After` was lost under the ambiguity wrapper.** A write's
  `CrmUnavailableError` reaches the syncer as `AmbiguousOutcomeError`, and
  the CRM's wait is on `.cause`. Found by the check that expects a 120 s
  `Retry-After` to beat a 10 s base interval; it scheduled 9 s.
- **The summary paragraph contains the word "Qualification".** An assertion
  that an unanswered call's body has no qualification *section* matched the
  summary's own label. The check now looks for the section header.
- **HubSpot's phone search is area-code-and-local**, and a CRM's own record
  of a number is usually local-format. `_same_number` strips the trunk zero
  and requires nine digits in common; without that the first candidate
  check compared `03001234567` with `923001234567` and found nobody.

### 36. Smaller things that cost time (Phase 16)

- **Adding a keyword argument to a client's HTTP call breaks every stub
  session that spells out the signature.** `CalComProvider._request` gained
  `timeout=`, and `test_scheduling.py`'s stub `request(method, url, headers,
  params, json)` raised `TypeError` — reported as a traceback in the Cal.com
  section and half the script's checks not running. The stub records the
  timeout now; `test_telephony.py`'s stub already took `**`-style extras.
- **`<Dial action>` silences the verbs after `<Dial>`.** The obvious change —
  add the attribute, keep the inline fallback — leaves the prospect in
  silence when nobody answers, because the carrier hands control to the
  action URL's response. The fallback had to move into the receiver's
  answer, and the receiver had to answer TwiML on *every* path, a duplicate
  delivery included.
- **`ALTER TABLE … ADD CONSTRAINT` inside `create_schema`'s single
  transaction.** A failure (existing overlapping rows) would have poisoned
  the whole transaction and failed `init` for every table. A nested
  `connection.transaction()` is a savepoint in asyncpg; the failure is caught
  there and reported as a warning.
- **A `check()` written as one expression with a walrus and a lambda.** It
  read as clever and raised `TypeError: object ActionService can't be used
  in 'await' expression`. Three plain lines replaced it.

### 37. Smaller things that cost time (Phase 17)

- **A parameter first used inside `$1 - make_interval(...)` is typed as an
  interval.** PostgreSQL infers `$1` from `interval - interval`, and the
  comparison `timestamptz <= interval` fails at execute time. Phase 15's
  claim got away with it because its `$2` was first used in a typed
  comparison in the same statement. Cast at first use:
  `$1::timestamptz - make_interval(secs => $2::float8)`.
- **Six statements sharing one argument list must all reference every
  argument.** asyncpg prepares each statement and counts the parameters
  PostgreSQL saw; a statement that uses `$3` but not `$1`/`$2` (or the
  reverse) raises `the server expects 2 arguments for this query, 3 were
  passed`. The fix was an explicit, commented `AND $1::timestamptz IS NOT
  NULL AND $2::float8 IS NOT NULL` in the statements that do not need them —
  ugly, and better than three calling conventions.
- **Both of the above were reported as "cannot reach PostgreSQL".** The
  check scripts' SQL section catches `asyncpg.PostgresError` broadly and
  records a *skip*; a syntax or type error in the SQL under test therefore
  reads as a machine without a database. `test_automation.py` catches only
  connection, authorisation and missing-database errors. The other sixteen
  still catch broadly; worth narrowing when one of them is next touched.
- **An asyncpg pool belongs to the loop that opened it.** FastAPI's
  `TestClient` runs the app in a thread with its own event loop; handing it
  the check's store produced `got result for unknown protocol state` and a
  `Future exception was never retrieved` some lines later. The app gets a
  `store_factory` that opens its own pool (on the same throwaway schema)
  inside its lifespan.
- **A walrus cannot rebind a comprehension's variable.** `part for part in
  … if len(part := part.strip()) >= 8` is a `SyntaxError`, found at import
  time by the next script. Two plain lines.
- **A keyword-only detail named like a positional parameter.**
  `ApiError(409, code, message, status=…)` raised `got multiple values for
  argument 'status'`. The details are `**kwargs`; the name is now
  `current_status`.
- **`+05:00` in a query string arrives as ` 05:00`.** FastAPI's `datetime`
  query parsing then 422s, and n8n expressions do not encode the plus. The
  API reads `since` / `from` as strings and repairs the one space before a
  `HH:MM` tail.
- **The first test expectations for the callback list were wrong, not the
  code.** A `POST /calls` for a second campaign *moves* the prospect's one
  pending callback into that campaign — the documented rule — so the list
  the check expected (three pending, one per request) never existed. The
  check now follows the rule it is checking.
- **`rich_result()` from `test_crm.py` is not valid for `save_call_result`
  as it stands.** `decision_role=UNKNOWN` does not support `QUALIFIED`;
  `validate_call_result` says so. `test_crm.py` never saves it (its fake
  store does not validate). Pass `decision_role=DecisionRole.DECISION_MAKER`.
- **Import order.** `ruff` wants `from x import a` and `from x import event
  as b` on separate lines; `--fix` does it.

## Quick reference for a fresh session

```
D:\Ai-Voice-Agent
├── AGENTS.md / CLAUDE.md   # Pipecat app-building guidance — read it, it is not generic
├── README.md               # User-facing: stack, setup, how it works, measured latency
├── HANDOFF.md              # This file
├── PRODUCTION_READINESS.md # Phase 23: not declared ready; the fifteen manual items and the go-live checklist
├── n8n/                    # Phase 17: README.md (the API + event reference) and workflows/01…06.json
└── server/
    ├── bot.py              # Wiring only (+ one line mounting the webhook route, Phase 14)
    ├── call.py             # Place one outbound call and watch it
    ├── campaign.py         # Prospects, campaigns, the call queue, `run`, `webhooks`, `crm-sync`/`crm-status`/`crm-retry`, `transfers`, `events`/`events-retry`
    ├── automation.py       # The n8n-facing API and the outbound event deliverer, port 7890 (Phase 17)
    ├── webhooks.py         # The standalone webhook receiver, port 7880 (Phase 14; optional)
    ├── ingest.py           # Load documents into the knowledge base
    ├── src/                # config, services, turns, metrics, resilience, diagnostics,
    │   │                   #   prompts, retrieval, knowledge_store, embeddings, documents
    │   ├── conversation/   # states, qualification, brief, playbook, signals, results,
    │   │                   #   actions (Protocol), timeparse, toolkit, tools, conversation,
    │   │                   #   director, sources, sink, transcript — imports no database,
    │   │                   #   no carrier, no calendar, nothing from bot.py
    │   ├── actions/        # service (the ActionBackend), __init__ (open_actions)
    │   ├── scheduling/     # base, hours, local, calcom, __init__ (make_calendar)
    │   │                   #   (Phase 16: calcom has a timeout, find_booking, check_credentials)
    │   ├── campaigns/      # models, phone, csv_import, store, service, dialer, briefing,
    │   │                   #   results (Phase 8), recovery (Phase 9), worker (Phase 13),
    │   │                   #   webhooks (Phase 14: the receiver and the ledger)
    │   ├── monitoring/     # Phase 22: metrics (the registry), instruments (every aiva_* metric),
    │   │                   #   tracing (the correlation id), http (/healthz /readyz /metrics, request ids,
    │   │                   #   the scheduler's server), collect (fleet gauges from the rows). Imports
    │   │                   #   no campaigns, security, automation, CRM or conversation code
    │   ├── reliability/    # Phase 9: retry, idempotency, guardrails, supervisor,
    │   │                   #   health, observability — imports no campaigns
    │   ├── dashboard/      # Phase 10: stats, page, web — reads campaigns/, a leaf
    │   │                   #   Phase 11: reliability/usage.py — tokens, characters, cost
    │   ├── telephony/      # base, twilio, signalwire, transport, session, __init__
    │   │                   #   (Phase 14: WebhookEvent, verify_webhook, parse_webhook live here)
    │   ├── crm/            # Phase 15: base (CrmProvider), mapping (CallResult → contact + activity),
    │   │                   #   hubspot (the only vendor file), sync (CrmSyncer), __init__ (make_crm_provider)
    │   └── automation/     # Phase 17: auth (keys, HMAC), serialize (rows as JSON), events (the outbox
    │                       #   deliverer), api (the FastAPI app). Reads campaigns/ and config; nothing reads it
    ├── app.py              # Phase 24: the unified application, port 7900 — the page, the dashboard and the API on one origin
    ├── src/app/            # Phase 24: server.py — create_unified_app (mounts dashboard/ and automation/, /api/app/*)
    │                       # Phase 25: engine.py — CampaignEngine, the scheduler as a task inside the application
    ├── web/                # Phase 24: index.html, styles.css, app.js — the single-page application, no build step
    ├── validate.py         # Phase 23: every automated check into validation-report.md; measure; live
    ├── health.py           # Every dependency, probed cheaply (Phase 9)
    ├── dashboard.py        # The reporting page and its JSON (Phase 10)
    ├── evals/              # Audio suite (7 scenarios) + sales/ (15, text mode), Groq judge
    │   ├── voice_quality.py # Phase 12: the turn monitor and the per-call report
    │   └── voicemail.py     # Phase 12: answering-machine detection and handling
    ├── tests/              # test_{conversation,results,reliability,dashboard,performance,
    │                       #   actions,scheduling,knowledge,telephony,realtime,campaigns,
    │                       #   voice_quality,worker,webhooks,crm,booking_transfer,automation,
    │                       #   security,compliance,scaling,monitoring,production,app,engine}.py
    │                       # phone_drill.py — seven scripted callers over the phone path (Phase 12)
    │                       # live_call.py — one real call, checked on both sides (Phase 12)
    ├── scripts/            # benchmark_db.py — the Phase 11 measurements, at scale
    │                       # fake_carrier.py / fake_browser.py — simulate a caller
    │                       #   against a running bot; no account needed
    │                       #   (fake_carrier --prospect/--campaign/--attempt lands a result)
    ├── .env                # Keys (git-ignored). SignalWire credentials and a tunnel URL are
    │                       #   set as of 2026-09-04; no TELEPHONY_TRANSFER_NUMBER, no Cal.com
    └── pyproject.toml      # pipecat-ai[anthropic,cartesia,deepgram,evals,runner,silero,webrtc,websocket], tzdata
```

**Before changing anything:** run `uv run validate.py` (health, posture, the rows and the twenty-three check scripts, a few minutes) and note the baseline in `server/validation-report.md`. If you touch anything on the phone path — `bot.py`'s session start, the transport, the serializer choice — run `tests/phone_drill.py all` against a running bot too; it is the only check that streams audio from the handshake the way a carrier does. `uv run dashboard.py --once` shows what is in the database right now. Then the two eval suites if you are touching the pipeline or a prompt — and read the Groq note in Known issues before believing a timeout. **After changing anything:** run them again, with `-r 2` on anything you suspect.

**Changing a tool is changing the prompt, and the prompt has a budget.** Every tool's docstring is sent on every turn. Measure with a direct request (`usage.prompt_tokens`) before and after — the scratch script that did it is described in Failed §20 — and keep the *how* in `playbook.stage_block`, not in the docstring.

**Changing a prompt is changing behaviour.** `tests/test_conversation.py` asserts that the honesty rules are present in the system instruction by their actual wording, so deleting one fails a check in a second instead of surfacing on a live call. If you reword a rule, update the check in the same edit — and read what it is checking before you decide the check is the thing that is wrong.

**Verify Pipecat APIs against `server/.venv/Lib/site-packages/pipecat`,** not from memory. Pipecat moves fast and 1.8.1 carries deprecated aliases for several things that older training data will suggest.

**Respect the phase boundary.** Build the phase that was asked for, and no more.

### 35. Assuming a FastAPI route hands its route class a `JSONResponse` (Phase 18)

The viewer masking was written as a custom `APIRoute` whose handler
wrapper re-serialised the answer when `isinstance(response, JSONResponse)`.
Every check of the masking passed against a probe route and failed against
the real API, with the number in clear. The difference was the return
annotation: FastAPI 0.141 renders a route annotated `-> dict[str, Any]`
through its response model and returns a plain `starlette.responses.Response`
already holding JSON bytes, not a `JSONResponse`, so the `isinstance` was
False on every real route and the wrapper stepped aside. The fix is to key
on the `Content-Type` (`application/json`) and on there being a body,
whatever class FastAPI chose. The check that caught it compared a viewer's
answer to an operator's and found them equal; a check that only asserted
the viewer's answer was "masked" would have been written against the
unmasked value and passed. Compare against the thing that must differ.

### 36. Smaller things that cost time (Phase 18)

- **A `≤` in a startup line.** `SecurityConfig.describe()` printed `body ≤
  5120 KiB`; on this machine's cp1252 console the first smoke test died in
  `charmap_encode`. Failed §9 again, from the other direction: keep ASCII
  in anything printed to a console, and run the smoke checks with
  `PYTHONIOENCODING=utf-8` anyway so the *next* glyph is caught before a
  user sees it.
- **A shell variable and a background subshell.** `S=… && (cmd > "$S/x") &`
  runs the assignment inside the first backgrounded list, so the second
  subshell's `$S` was empty and its log went to `/x` (permission denied).
  `export` it on its own line first.
- **A phone validator that was too clever.** "A phone must contain
  digits" broke Phase 17's promise that an unparseable number is stored
  `UNREACHABLE` with a warning, and `test_automation.py` said so at once.
  Validation past lengths is control characters and bounds; what a number
  *means* is `phone.py`'s job and was already right.
- **A per-key rate limit lower than the checks' own request count.** The
  API section made ~30 requests with one key against a 12/min limit and
  the validation checks started answering 429. The limit in a check must
  be higher than the check's traffic, and the burst that proves the limit
  must be larger than the limit.
- **`_IncludedRouter`.** FastAPI 0.141 keeps an included router as one
  entry in `app.routes` with no `.routes` of its own; the original routes
  are under `.original_router`. Worth knowing before concluding a route
  class was dropped — it was not.

### 37. Deriving the opt-out disposition from the final state alone (Phase 19)

`OPTED_OUT` was first written as "the result's `final_state` is
`DO_NOT_CALL`". `test_results.py` failed at once on a scenario written in
Phase 8: the person asks not to be called, then asks for a human, then the
agent says goodbye — the state path ends `DO_NOT_CALL, ENDING`, the status
carries the request, and the *final* state is `ENDING`. Phase 8 had
already learned this (its `attempt_status_for` reads the path, not the
last state — see the note above `on_call_finished`); the disposition
needed the same lesson. The rule is now: heard, if the final state is
`DO_NOT_CALL` *or* a conversation result (one with a final state at all)
carries the `DO_NOT_CALL` status; known, if a status has no conversation
behind it. A carrier result has no final state, so a list refusal stays
`DO_NOT_CALL`.

### 38. Smaller things that cost time (Phase 19)

- **A package `__init__` that imports the thing that imports `config.py`.**
  `src/compliance/__init__.py` imported `gate`, which imports
  `src/reliability/`, whose `health.py` imports `config.py`, which imports
  `src/compliance/`. Python runs the package `__init__` before the
  submodule `config.py` asked for, so the cycle would have bitten on the
  first `import src.config`. The fix is a lazy `__getattr__` for the gate
  names and a `policy.py` that imports `CallingWindow` inside the method
  that needs it.
- **A reservation that is not due.** Two checks reserved a callback for a
  membership whose retry time had not arrived and read the `None` as a
  compliance failure. The queue refuses an undue membership whether or not
  the attempt limit is waived — a callback waives the ceiling, not the
  wait. Set `next_attempt_at` to None, or advance the clock, before
  expecting a reservation.
- **A busy at the ceiling.** A third attempt on a three-attempt campaign
  that ends busy is `EXHAUSTED`, not "pending in sixty minutes" — the
  check that expected the wait had forgotten the count it had just spent.
- **A re-imported do-not-call prospect got a membership.** The importer
  added every row to the campaign and only then applied the list, and a
  prospect *already* `DO_NOT_CALL` is not "newly marked", so the membership
  stayed `PENDING`. The queue would never have handed it out; the counts
  would have said otherwise. The importer and `add_prospects` now skip a
  `DO_NOT_CALL` prospect before opening anything.
- **`f"a" + b if c else d`.** The conditional binds the whole
  concatenation, so the DNC reason read as `")"` whenever the entry had no
  timestamp. Build the parts in a list.

### 39. Smaller things that cost time (Phase 20)

- **A reporting module that imported the API package.** `dashboard/stats.py`
  reached for `automation/serialize.py`'s dictionaries for the call detail,
  and `test_automation.py`'s boundary check — which greps every module on
  the call *and* reporting paths for `src.automation` — failed at once.
  The dashboard now serializes its own rows (`_plain`, a dataclass → JSON
  walk) and imports nothing from the API package. The check exists for the
  reason it fired: the API package is the one that must never be on a path
  a call takes.
- **Route checks over the wrong schema.** `check_routes` built the app from
  `Config.from_env()` and therefore over the *real* database, while the
  rows the new checks looked for had been seeded in the throwaway schema.
  Phase 18's route checks had not noticed because they asserted nothing
  about rows. The routes now run inside the database section, over a
  `store_factory` that opens its own pool with the schema's `search_path`
  — `setup=`, not `init=`, for the reason `with_temp_schema` documents —
  and in a thread, because the test client runs the app on its own loop.
- **An added test row changed a later section's arithmetic.** The filter
  checks put a sixth attempt in the schema; `check_degraded`, which runs
  after them, still expected five calls and four outcomes. Seed once, or
  count what is there.
- **A new required parameter on a Phase 11 method.** `_SnapshotCache.get`
  gained `filters` and `test_performance.py`'s cache checks called it the
  old way. A parameter added to something an earlier phase's checks call
  gets a default.

### 40. Setting the worker's identity on a shared dialer (Phase 21)

The first cut gave the dialer a `worker_id` attribute and had each
`CampaignWorker` set it in its constructor. In production every process
has its own dialer, so it would have worked; in `test_worker.py`'s world
two workers share one dialer, so the second worker constructed overwrote
the first's identity, worker A's reservations were stamped with B's id,
and A — on its very next tick — found "another worker" on its own call and
stopped following it. The call was then followed by nobody, the
concurrency slot never freed, and a six-prospect run placed one call.

The fix is the better design regardless: the identity is the *worker's*,
so the worker passes `worker_id=` on every `dial_next` /
`dial_membership` and the dialer's attribute is only a default for
`campaign.py call`. State that belongs to a caller should travel with the
call, not be parked on a collaborator the caller may share. The bug is also
why `test_scaling.py` builds each worker its own dialer and guards: two
workers sharing one `PacingLimiter` cannot show that the *shared* slot
refuses the second one.

- **A retry rule changes a Phase 14 expectation.** `test_webhooks.py`
  expected a carrier `failed` with a SIP 503 to exhaust the membership;
  Phase 21 retries a 5xx on purpose, so the check now expects `PENDING`
  with a retry scheduled. When a phase changes behaviour, an earlier
  phase's check that encoded the old behaviour is updated in the same
  edit, with the reason beside it — not made to pass by narrowing the
  rule.
- **Two Phase 13 checks assumed the second worker adopts the first's live
  call.** That was the single-process rule ("adopt everything live at
  start"); with ownership it is precisely the double-follow the phase
  removes. Both checks now assert the opposite and that the row names its
  owner.
- **A pacing deferral did not end the tick's placing loop.** The shared
  slot refused, the reservation was given back with `next_attempt_at`
  pushed by the wait, and the loop asked for the *next* prospect at once —
  which was refused and pushed back too, through every due prospect, every
  tick. The slot is global; a refusal now returns from the loop the way a
  guard refusal does.
- **`FakeClock.now` is an attribute; the store's `now()` is a method.**
  Five minutes on `'datetime.datetime' object is not callable`.
- **The ledger's column is `call_id`; the attempt's is
  `telephony_call_id`.** A wrong column name in a check's raw SQL surfaces
  as "cannot reach PostgreSQL" through the scripts' broad skip clause
  (Failed §37's finding, again): read the SKIPPED line before believing the
  database is down.

### 41. Binding the scheduler's metrics port with `SO_REUSEADDR` (Phase 22)

The scheduler has no web server, so Phase 22 gives it a small one for
`/healthz`, `/readyz` and `/metrics`. The first cut let uvicorn bind the
port; uvicorn's bind failure calls `sys.exit`, which inside a task would
have taken the *dialling* loop down because a scrape port was taken — a
scheduler must never fail to dial for a metric's sake. So the socket is
bound here first and handed to uvicorn, and a taken port is one warning.

The check for "a taken port is a warning and None" then failed on this
machine: with `SO_REUSEADDR` set, **Windows lets a second process bind a
port that another process is already listening on**, and both believe
they own it — the second silently gets no traffic. Linux refuses the
second bind under the same flag. The fix is `SO_EXCLUSIVEADDRUSE` where it
exists (Windows) and `SO_REUSEADDR` elsewhere; the check now passes on
both.

- **`asyncio.run()` inside an async check.** Two of the HTTP checks
  first called `asyncio.run(run_check(...))` from a function invoked by
  the async `main()`; that is a `RuntimeError` on a running loop. The
  checks that await are async; FastAPI's `TestClient` (its own portal
  thread) is the one thing that may be called synchronously from inside.
- **`configure_logging` removes every loguru handler**, the check
  runner's capture included, and a later `logger.remove(handler_id)` then
  raises and masks the real failure. The runner keeps its handler id in a
  dict the JSON-log check updates after re-adding the capture.
- **`CallAttemptStatus` has `CALLBACK_REQUESTED`, not `CALLBACK_SCHEDULED`.**
  The throughput SQL and the in-memory store both used the wrong name;
  the in-memory one raised at once, the SQL would have counted a
  callback-requested ending as unanswered. Read the enum before writing a
  status into a query.
- **`loguru`'s context is a module-level `ContextVar`**
  (`loguru._logger.context`), not an attribute on `logger._core`.
  `current_trace_id()` reads it there.
- **A Phase 7 fixture pinned to a date.** `test_campaigns.py` compared a
  callback at 2026-09-08 10:00 with the real clock; it was red in this
  session's baseline for that reason alone. A fixture that meets the real
  clock must be relative to it.

### 42. Making a callback due after the previous call had closed (Phase 23)

The end-to-end suite runs the real worker with `MAX_CONCURRENT_CALLS=1`. To
prove a scheduled callback is placed *ahead of the queue*, the first cut
ended the current call, ticked, then moved the callback's time to now and
ticked again — and the worker dialled the queue's next prospect, not the
callback. Correctly: the tick that noticed the call had ended also ran its
placing phase, and at that moment the callback was not yet due, so the
queue's next prospect took the one slot; the callback then waited for that
call to end. The fix is the sequence a real deployment produces: the
callback falls due *while* the previous call is still up, so the tick that
closes it finds the callback due and places it first. A check that wants to
prove ordering has to arrange the moment, not the order of its own lines.

- **An asyncpg pool belongs to the loop that opened it** — Phase 17's
  lesson, met a third time. A webhook processor built over the suite's
  store and handed to a FastAPI app under the test client raised `got
  result for unknown protocol state` on every request; the receiver now
  opens its own pool in the app's lifespan, as `webhooks.py` does.
- **A FastAPI handler's `Request` must be imported at module level** under
  `from __future__ import annotations` — Phase 14's lesson, met again: the
  n8n receiver's `request` became a required query parameter and every
  delivery was a 422 the deliverer rightly closed as failed.
- **The CSV parser rejects a second spelling of a number already in the
  file** (`same phone as line 2`) rather than counting it as a duplicate;
  "duplicate" is for a number already in the database.
- **`CrmSyncRecord` has `call_attempt_id`, not `result_id`;
  `RecoveryReport` has `resolved`, not `reconciled`;
  `campaign_call_key` takes keywords; the transcript's agent role is
  `assistant`; the dashboard's call detail keeps the cost under
  `usage.cost_usd` and the meetings under `meetings`.** Read the model
  before writing the assertion.
- **`CallAttempt` has no `usage` attribute**: usage and cost are columns
  read by the dashboard's `call_detail`, not fields of the model.
- **The ElevenLabs key was the real reason `health.py tts` was red**, not
  Cartesia's. A probe that names the wrong vendor is worse than none.

### 43. Reading the API's `via` on the session principal (Phase 24)

The first check that the unified app's `/automation/api/v1/status`
answered a browser session asserted on `principal["via"] == "session"`,
copying the shape `/api/app/session` returns. The API's status route
serialises the principal it was given, and a `Principal` has no `via` —
that is a field the app route adds. Corrected: assert on the role. The
general lesson is the one from §35: read what the route actually returns
before asserting on it, especially when two routes describe the same
object.

Three smaller ones from the same session, recorded so nobody repeats them:

- **`Match` is a dataclass**, not a dict: `content, source, title, ordinal,
  score`. The knowledge search route reads attributes.
- **The worker completes a campaign whose queue is empty**, so a check that
  dials two prospects and then pauses the campaign found it `COMPLETED`.
  The flow check runs the worker with `auto_complete=False`, which is what
  the scaling checks already did.
- **A shell heredoc that starts with Python is intercepted on this
  machine** ("Ctrl click to launch VS Code Native REPL"); write patch
  scripts to a file and run the file.

### 44. Taking the first line of an exception's message (Phase 24 audit)

`str(exc).splitlines()[0]` is the natural way to put an exception on one
log line, and it was in forty places. `str(TimeoutError())` is `""`,
`"".splitlines()` is `[]`, and `[][0]` is `IndexError` — raised *inside an
`except` block*, so the handled failure became an unhandled one. In the
dialer that meant an attempt whose placement timed out was never marked
`UNRESOLVED`: the worker's tick failed, the row stayed reserved, the
prospect stayed blocked. Found by the audit's bridge carrier taking too
long to answer `place_call`. The form that survives is
`(str(exc).splitlines() or [type(exc).__name__])[0]`, and `sed` put it
everywhere at once. The lesson is older than this project: anything that
indexes a list derived from a string must decide what the empty string
means before it runs inside an error handler.

Two harness lessons from the same audit, for whoever repeats it:

- **jsdom is not a browser in three ways that matter to this page:** no
  `HTMLFormElement` named getter (`form.username` is `undefined`; use
  `form.elements.namedItem` or polyfill the getter on the prototype), no
  `Blob.text()` / `arrayBuffer()` (polyfill over `FileReader`), and an
  exception inside an event handler surfaces as an unhandled rejection
  that kills Node rather than a `jsdomError`. Polyfill in `beforeParse`;
  never change the page for jsdom's sake. `resources: "usable"` also loads
  the Live Agent page's iframe, so the bot's client must be up or its CSS
  failures filtered out of the console count.
- **A stand-in carrier must answer `place_call` in milliseconds.** The
  dialer's placement timeout is real; synthesising four Kokoro lines inside
  `place_call` took longer, the placement came back ambiguous, and the
  audit spent twenty minutes on a bug in the harness before finding the
  bug in the product that the harness's bug exposed. Synthesise first,
  then start the worker.
