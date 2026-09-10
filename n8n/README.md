# n8n integration (Phase 17)

The calling agent and n8n meet at two doors, both served by one process
that runs beside the bot and the scheduler:

```
CSV / CRM / event
    ↓
n8n                                      asynchronous; never in the audio path
    ↓  HTTP + API key
the automation API     uv run automation.py      prospects · campaigns · calls · callbacks · results
    ↓  rows
the scheduler          uv run campaign.py run    places the call, under every calling rule
    ↓
the bot                uv run bot.py             holds the conversation; knows nothing of n8n
    ↓  rows (call_results, meetings, callbacks)
the outbox             uv run automation.py      call.completed · lead.qualified · meeting.booked · …
    ↓  signed POST
n8n
    ↓
CRM / calendar / notifications
```

**n8n never touches the call.** Speech-to-text, the language model,
text-to-speech, voice activity detection and turn detection all stay in
`bot.py`. What n8n can do is *ask for* a call and *hear about* one:

- **Asking** is a row. `POST /api/v1/calls` writes the same pending-callback
  row the agent writes when a prospect says "call me back", due now. The
  scheduler places it on its next tick (within `WORKER_IDLE_SECS`, 30 s by
  default), ahead of the campaign's queue, under the calling hours, the
  concurrency limit and the pacing. The API never dials.
- **Hearing** is a row too. The bot writes a call's result at the end of the
  call as it always has. The outbox turns that row into an event once it has
  settled, and delivers it — signed, once per row, with the same `event_id`
  on every redelivery — to a webhook URL you configure.

Everything is idempotent in both directions, which is what an automation
platform that retries needs. Details below.

---

## 1. Setting it up

**On the agent's side** (`server/.env`):

```bash
# Generate a key:  python -c "import secrets; print(secrets.token_urlsafe(32))"
AUTOMATION_API_KEYS=<key>[,<another key during rotation>]

# Where events go: the Webhook node's *Production* URL (see §4)
AUTOMATION_WEBHOOK_URL=https://n8n.example.com/webhook/aiva-events
AUTOMATION_WEBHOOK_SECRET=<a second random string>        # signs every delivery
AUTOMATION_WEBHOOK_AUTH_TOKEN=<a third random string>     # the header n8n checks natively
```

Then, from `server/`:

```bash
uv run campaign.py init        # adds the two Phase 17 tables (idempotent)
uv run automation.py           # API on http://127.0.0.1:7890/api/v1, deliverer on
uv run campaign.py run         # the scheduler — it is what dials
uv run bot.py                  # the agent itself
```

`automation.py` prints where it listens, which event kinds go where, and
whether deliveries are signed. `http://127.0.0.1:7890/api/v1/docs` is the
live OpenAPI page.

**On n8n's side**, two credentials:

| Credential (type: *Header Auth*) | Name | Value | Used by |
|---|---|---|---|
| `AIVA API key` | `Authorization` | `Bearer <AUTOMATION_API_KEYS>` | every HTTP Request node that calls the API |
| `AIVA webhook header` | `<AUTOMATION_WEBHOOK_AUTH_HEADER>` (default `X-Aiva-Key`) | `<AUTOMATION_WEBHOOK_AUTH_TOKEN>` | every Webhook node that receives events |

Then import the workflows in `workflows/` (n8n → Workflows → Import from
File), open each, select those credentials where a node says
`REPLACE-ME`, and edit the `Config` node (the API base URL, the campaign
name). When n8n runs in Docker on the same machine as the agent, the API
base is `http://host.docker.internal:7890/api/v1`.

### Every environment variable

| Variable | Default | What it does |
|---|---|---|
| `AUTOMATION_API_KEYS` | *(unset → API refuses to serve)* | Comma-separated bearer keys, each ≥ 16 characters. Several allow rotation without a gap. `AUTOMATION_API_KEY` (singular) is accepted too. Scrubbed from logs. |
| `AUTOMATION_HOST` / `AUTOMATION_PORT` | `127.0.0.1` / `7890` | Where the API binds. Loopback by default; `0.0.0.0` needs TLS in front (the key travels in clear over plain HTTP). |
| `AUTOMATION_IDEMPOTENCY_TTL_SECS` | `86400` | How long a stored `Idempotency-Key` answer is replayed. |
| `AUTOMATION_WEBHOOK_URL` | *(unset → nothing delivered)* | Where every enabled event kind is POSTed. |
| `AUTOMATION_WEBHOOK_URL_<KIND>` | — | Per-kind override, e.g. `AUTOMATION_WEBHOOK_URL_LEAD_QUALIFIED`. `<KIND>` is the kind upper-cased with `.` → `_`. |
| `AUTOMATION_EVENTS` | all six | Which kinds are created and delivered, comma-separated. |
| `AUTOMATION_WEBHOOK_SECRET` | *(unset → unsigned)* | HMAC-SHA256 key for `X-Aiva-Signature`. Recommended. |
| `AUTOMATION_WEBHOOK_AUTH_HEADER` / `_TOKEN` | `X-Aiva-Key` / *(unset)* | A static header on every delivery, for the Webhook node's Header Auth. The name defaults when only the token is set. |
| `AUTOMATION_EVENTS_SINCE` | *(unset → all history)* | ISO 8601. Only facts recorded at or after this moment become events. **Set it before the first run on a database with history**, or n8n hears about every call ever made. |
| `AUTOMATION_SETTLE_SECS` | `30` | A call result must be unchanged this long before its events exist (see §3). |
| `AUTOMATION_INCLUDE_TRANSCRIPT` | `false` | Whether `call.*` payloads carry the transcript. |
| `AUTOMATION_POLL_SECS` / `AUTOMATION_BATCH` | `10` / `20` | How often the deliverer looks; events per pass. |
| `AUTOMATION_MAX_ATTEMPTS` / `AUTOMATION_RETRY_SECS` / `AUTOMATION_MAX_RETRY_SECS` | `12` / `30` / `1800` | The backoff after a transient failure: doubling from the first, capped at the second, honouring `Retry-After`, for up to `MAX_ATTEMPTS` passes. |
| `AUTOMATION_TIMEOUT_SECS` | `15` | Per delivery request. |
| `AUTOMATION_STALE_SECS` | `600` | An event claimed this long ago by a deliverer that never reported back is claimed again. |

Everything else the API relies on — `DATABASE_URL`, `DEFAULT_PHONE_REGION`,
`CAMPAIGN_MAX_ATTEMPTS`, `CALLBACK_MAX_DAYS_AHEAD`, `CALENDAR_TIMEZONE` — is
the agent's existing configuration, applied unchanged.

---

## 2. The API

**Keys have roles (Phase 18).** Give n8n an **operator** key
(`uv run security.py make-key --role operator` → `AUTOMATION_OPERATOR_API_KEYS`):
it can do everything the six workflows do — create and import prospects,
create and start campaigns, queue calls, read results and transcripts — but
cannot complete or cancel a campaign, reopen outbox rows or read the audit
log, which need an **admin** key (`AUTOMATION_API_KEYS`). A **viewer** key
(`AUTOMATION_VIEWER_API_KEYS`) is for a read-only workflow: it gets phone
numbers masked (`+92••••••••67`), no transcripts and no custom fields, and
a `403` naming the missing permission if it tries to write. Every write n8n
makes is on the audit log under the key's label (`operator-key#1`), and a
key is limited to `SECURITY_API_RATE_LIMIT` requests a minute (300; `429`
with `Retry-After` beyond it). Over a network, put TLS in front and set
`SECURITY_REQUIRE_HTTPS=true` — see `SECURITY.md`.

**Do-not-call and compliance (Phase 19).** A workflow that learns of an
opt-out — an email, a CRM field, a registry file — can record it at once:
`POST /api/v1/dnc` with `{"phone": "+923001234567", "reason": "asked by
email"}` lists the number (source `api`, the key's label as the actor) and
marks every prospect with it; `GET /api/v1/dnc/check?phone=…` answers
whether a number is blocked (any key); `POST /api/v1/prospects/{id}/do-not-call`
does the same for a prospect. A `POST /api/v1/calls` for a listed number is
refused with `409 do_not_call` and audited, so a workflow learns now rather
than leaving a callback the scheduler blocks. `GET` / `PUT
/api/v1/campaigns/{id}/compliance` read and set a campaign's window, attempt
ceiling, retry delays and disclosures, validated before anything is written.
The `call.completed` event's `disposition` now says `OPTED_OUT` when the
person asked on that call and `DO_NOT_CALL` when the list refused the dial.
None of this is legal advice — `COMPLIANCE.md` says what the software does
and what the operator must decide.

Base URL `http://127.0.0.1:7890/api/v1`. Every route but `/api/ping` needs
`Authorization: Bearer <key>` (or `X-API-Key: <key>`). Requests and
responses are JSON; times are ISO 8601 with an offset; a naive time in a
request is read in `CALENDAR_TIMEZONE`.

**Errors** are one shape, with a machine-readable code:

```json
{"error": {"code": "do_not_call", "message": "Sara Ali is marked DO_NOT_CALL and will not be dialled.", "details": {"prospect_id": 7}}}
```

`401 unauthorized` · `404 *_not_found` · `409 do_not_call | not_dialable |
invalid_transition` · `422 invalid_request | idempotency_key_reused | …` ·
`503 database_unavailable`.

**Idempotency.** Every write has a natural key — a prospect is one per
number, a campaign one per name, a membership one per pair, a pending
callback one per prospect — so repeating a request repeats nothing. On top
of that, send an `Idempotency-Key` header on any POST: the answer is stored
and a retry with the same key and body gets the same status and body back
with `Idempotent-Replayed: true`; the same key with a different body is
refused (`422 idempotency_key_reused`). Keys are stored per route.

### Prospects

| | |
|---|---|
| `POST /prospects` | **Create one.** Body: `first_name`, `last_name`, `phone` (any format; normalised with `DEFAULT_PHONE_REGION`), optional `email`, `company`, `job_title`, `industry`, `location`, `website`, `custom_data`; any other field lands in `custom_data`. `201 {"prospect", "created": true, "warnings"}`; the same number again is `200 {"created": false}` with the existing row, nothing overwritten. A number that cannot be normalised is stored `UNREACHABLE` with a warning. |
| `POST /prospects/import` | **Create many.** JSON `{"rows": [{...}, ...], "campaign": <id or name>, "create_campaign": bool, "dry_run": bool}` — each row with any of the CSV import's recognised headers (`First Name`, `first_name`, `Surname`, `Mobile`, `Phone`, `Email`, `Company` …). Or `Content-Type: text/csv` with the file as the body and `?campaign=&create_campaign=&dry_run=` as query. Answers the import report: `created`, `duplicates`, `rejected` (line + reasons), `rejected_count`, `prospect_ids`, `mapping`, `added_to_campaign`, `campaign`. Re-importing creates nothing. |
| `GET /prospects?phone=` | One by number, any format. |
| `GET /prospects?status=&limit=&offset=` | Newest first. |
| `GET /prospects/{id}` | With their recent calls and callbacks. |
| `POST /prospects/{id}/do-not-call` | Never call again, on any campaign; cancels their pending callback. Idempotent (`changed`). |

### Campaigns

| | |
|---|---|
| `POST /campaigns` | Body `{"name", "description"}`. `201` in `DRAFT`; the same name again (any case) is `200 {"created": false}`. |
| `GET /campaigns?status=&limit=` | Each with membership `counts`. |
| `GET /campaigns/{id or name}` | With `counts` and `queue` (due now, next due, pending callbacks, finished?). |
| `GET /campaigns/{id or name}/prospects?limit=&offset=` | Members, each membership paired with its person. |
| `POST /campaigns/{id or name}/prospects` | Body `{"prospect_ids": [...], "phones": [...], "all": bool}`. `added`, `already_members`, `unknown_phones`. Adding twice adds nothing. |
| `POST /campaigns/{id or name}/start` | → `ACTIVE` from `DRAFT` or `PAUSED`. |
| `POST /campaigns/{id or name}/pause` | → `PAUSED` from `ACTIVE`. A call in progress finishes; nothing new is queued. |
| `POST /campaigns/{id or name}/resume` | → `ACTIVE` from `PAUSED` (or `DRAFT`). |
| `POST /campaigns/{id or name}/complete` · `/cancel` | Closes it. |

Every transition answers `{"campaign", "changed"}`: already in the target
status is `changed: false`; a status it may not move from (`start` after
`complete`) is `409 invalid_transition` with `allowed_from`.

### Calls and callbacks — queued, never dialled

| | |
|---|---|
| `POST /calls` | **Ask for a call now.** Body: `prospect_id` *or* `phone`; `campaign_id` *or* `campaign` (name); optional `scheduled_at`, `note`. `202 {"queued": true, "callback", "prospect", "campaign", "membership", "joined_campaign", "replaced", "dialled_by", "warnings"}`. Refused with `409` for a `DO_NOT_CALL` prospect or one with no usable number. A prospect not in the campaign is added to it. |
| `POST /callbacks` | **Ask for a call at a time.** The same body with `scheduled_at` required (not in the past, within `CALLBACK_MAX_DAYS_AHEAD`). |
| `GET /calls?campaign_id=&prospect_id=&limit=` | Call attempts, newest first. |
| `GET /calls/{attempt_id}?include=transcript` | The attempt, its result once there is one, any transfer. |
| `GET /callbacks?status=PENDING\|PLACED\|CANCELLED\|all&due=true&prospect_id=&campaign_id=` | Soonest first. |
| `GET /callbacks/{id}` · `DELETE /callbacks/{id}` | Read; withdraw (`changed`). |

**What 202 means.** A pending callback row exists, due at `scheduled_at`
(now, for `/calls`). `uv run campaign.py run` — the scheduler — places it
on its next tick, ahead of the campaign's queue, if the campaign is
`ACTIVE`, it is within `CALLING_HOURS`, the concurrency limit has room and
pacing allows. The `warnings` list says when one of those is not yet true
(the campaign is `DRAFT`, the prospect is on a call right now). A prospect
has **one** pending callback: asking again moves its time (`replaced` shows
what it was) rather than stacking a second call. To learn what happened,
subscribe to `call.completed` (§3) or poll `GET /calls?prospect_id=`.

### Results, meetings, the outbox

| | |
|---|---|
| `GET /results?campaign_id=&prospect_id=&disposition=&since=&before_id=&limit=&include=transcript` | What finished calls produced, newest first — Phase 8's export shape (`disposition`, `qualification_status`, `summary`, `pain_points`, `objections`, `next_action`, `meeting_*`, `callback_*` …) plus `summary_text`. `since` polls for what changed; `before_id` pages (the answer's `next_before_id`). The transcript is omitted unless asked for (`transcript_included` says which). |
| `GET /results/{attempt_id}` | One, with the transcript, the prospect, the campaign and any transfer. `404 result_not_ready` while the call is live. |
| `GET /meetings?prospect_id=&from=&status=&limit=` | Meetings the agent booked. |
| `GET /events?state=&kind=&prospect_id=&limit=&include=payload` | The outbox: every event, its state, attempts, last status and error, and the `counts` by state. |
| `GET /events/{id}` · `POST /events/{id}/retry` | One event with the payload that was (or will be) sent; reopen it for delivery with a fresh attempt budget. |
| `GET /status` | Prospect and campaign counts, outbox counts, what the deliverer is doing. |
| `GET /api/ping` | *(no key)* Is it up, is the database reachable. |

---

## 3. The events

The deliverer creates events from the rows that already record the fact,
under a key that says what the event *is*, and delivers each once:

| Event | When | `event_id` | Payload carries |
|---|---|---|---|
| `call.completed` | A finished call's result, once unchanged for `AUTOMATION_SETTLE_SECS` | `call.completed:result:<result id>` | `prospect`, `campaign`, `call`, `result`, `transfers` |
| `call.updated` | The result changed *after* `call.completed` was delivered | `call.updated:result:<id>:<unix time of the change>` | the same, with the new result |
| `lead.qualified` | A settled result whose `qualification_status` is `QUALIFIED` | `lead.qualified:result:<id>` | the same |
| `meeting.booked` | The calendar confirmed a booking (at once — the call may still be going, so `result` may be `null`) | `meeting.booked:meeting:<id>` | `prospect`, `campaign`, `call`, `result`, `meeting` |
| `callback.scheduled` | A callback was promised — by the agent on a call, or through `POST /calls` / `/callbacks`. A moved callback is a new event | `callback.scheduled:callback:<id>:<unix time it is due>` | `prospect`, `campaign`, `call`, `callback` |
| `campaign.completed` | The scheduler closed a campaign | `campaign.completed:campaign:<id>:<unix time>` | `campaign` with `counts` |

**Why the settle window.** When a call ends, two results race: the
carrier's thin one (status, duration) and the conversation's rich one
(summary, qualification, pain points …), seconds apart. Waiting until the
row has been unchanged for `AUTOMATION_SETTLE_SECS` is what makes
`call.completed` carry the rich one. Anything that changes later is a
`call.updated`.

**The request.** `POST <url>` with:

```
Content-Type: application/json; charset=utf-8
User-Agent: Ai-Voice-Agent-automation/17
X-Aiva-Event: call.completed
X-Aiva-Event-Id: call.completed:result:12
X-Aiva-Delivery: 1                          ← attempt number; the same event id on every redelivery
X-Aiva-Timestamp: 1789126400                ← unix seconds
X-Aiva-Signature: t=1789126400,v1=<hex>     ← when AUTOMATION_WEBHOOK_SECRET is set
X-Aiva-Key: <AUTOMATION_WEBHOOK_AUTH_TOKEN> ← when set (header name configurable)
```

```json
{
  "event_id": "call.completed:result:12",
  "event": "call.completed",
  "occurred_at": "2026-09-07T10:04:50+00:00",
  "sequence": 42,
  "prospect": {"id": 7, "first_name": "Sara", "last_name": "Ali", "full_name": "Sara Ali",
               "phone": "0300 1234567", "phone_normalized": "+923001234567",
               "email": "sara@example.com", "company": "Ravi Logistics", "job_title": "Operations Director",
               "custom_data": {"lead_score": 87}, "status": "CONTACTED", "dialable": true, "...": "..."},
  "campaign": {"id": 3, "name": "Q1 Outreach", "status": "ACTIVE", "...": "..."},
  "call": {"id": 34, "attempt_number": 1, "status": "COMPLETED", "telephony_call_id": "CA0034",
           "started_at": "...", "connected_at": "...", "ended_at": "...", "duration_seconds": 290,
           "trace_id": "9f3c2a71b0d4e582", "...": "..."},
  "result": {"id": 12, "call_attempt_id": 34, "disposition": "MEETING_BOOKED", "reached": true,
             "qualification_status": "QUALIFIED", "interest_level": "INTERESTED",
             "buying_timeline": "UNKNOWN", "decision_role": "DECISION_MAKER", "next_action": "MEETING_BOOKED",
             "meeting_status": "BOOKED", "meeting_start": "2026-09-08T10:00:00+00:00", "meeting_reference": "bk_abc123",
             "callback_status": "UNKNOWN", "callback_scheduled_for": null,
             "pain_points": ["fuel spend", "no visibility of idling"],
             "objections": [{"kind": "PRICE", "detail": "sounds expensive", "handled": true}],
             "questions": ["How long does installation take?"],
             "summary": {"what_happened": "...", "prospect_needs": "...", "objections": "...",
                         "interest": "...", "qualification": "...", "next_step": "..."},
             "summary_text": "...",
             "transcript": null, "transcript_included": false,
             "tool_actions": [{"name": "book_meeting", "success": true, "detail": "Tue 08 Sep 15:00"}],
             "...": "..."},
  "transfers": []
}
```

The `prospect`, `campaign`, `call`, `result`, `meeting` and `callback`
objects are exactly what the API returns for the same rows.

**Responding.** Answer `2xx` as soon as the event is *accepted* (n8n's
Webhook node in "Immediately" mode does), and do the work afterwards. The
deliverer treats a timeout, a refused connection, `408`/`425`/`429`/`5xx`
and `404` (n8n's answer for a workflow that is not active yet) as
transient and retries with backoff; any other `4xx` closes the event as
`FAILED` for a person to look at (`uv run campaign.py events`, then
`events-retry`). Redeliveries carry the same `event_id`; if your workflow
must not act twice, keep the ids you have seen (an n8n Data Table, a CRM
note carrying the id — workflow 03 does the latter).

**Verifying the signature.** `X-Aiva-Signature` is `t=<unix seconds>,v1=<hex>`
where `hex = HMAC-SHA256(secret, "<t>." + raw body)`. Check that `t` is
within five minutes of now, then compare digests in constant time. In
Python, `src.automation.verify_signature(secret, header, body)` is the
reference check. In an n8n Code node (the instance needs
`NODE_FUNCTION_ALLOW_BUILTIN=crypto`, and the Webhook node's *Raw Body*
option on, so the exact bytes are available):

```js
const crypto = require('crypto');
const secret = 'AUTOMATION_WEBHOOK_SECRET';
const item = $input.first();
const header = item.json.headers['x-aiva-signature'] || '';
const raw = Buffer.from(item.binary.data.data, 'base64');           // the bytes as sent
const parts = Object.fromEntries(header.split(',').map(p => p.split('=')));
const expected = crypto.createHmac('sha256', secret).update(`${parts.t}.`).update(raw).digest('hex');
const fresh = Math.abs(Date.now() / 1000 - Number(parts.t)) <= 300;
const ok = fresh && parts.v1 && crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1));
if (!ok) throw new Error('bad signature');
return [{ json: JSON.parse(raw.toString('utf8')) }];
```

The example workflows use the static header (Header Auth) because n8n
checks it without a Code node; add the block above after the Webhook node
when you want the body's integrity proven too.

---

## 4. The six workflows

All in `workflows/`, importable as they are. Each `Config` node holds the
API base URL and the campaign name; each `REPLACE-ME` is a credential to
select. None of them dials: every call goes through `POST /calls` or a
campaign's `start`, and the scheduler does the rest.

| # | File | Trigger | What it does |
|---|---|---|---|
| 01 | `01-lead-intake-to-campaign.json` | Manual (swap for Google Sheets / Airtable / a CRM trigger) | Reads a CSV → one `POST /prospects/import` with every row and `create_campaign: true` → `POST /campaigns/{id}/start`. Re-running creates nothing and adds nobody twice. |
| 02 | `02-campaign-to-outbound-calls.json` | Cron, weekdays 09:00 and 18:00 | `resume` in the morning, `pause` in the evening, then `GET /campaigns/{name}` + `GET /results?since=today` → a Slack summary by disposition. The agent's own `CALLING_HOURS` still apply underneath. |
| 03 | `03-completed-call-to-crm.json` | Webhook `aiva-call-completed` | `call.completed` / `call.updated` → find the HubSpot contact (email, then the last nine digits of the number) → update or create it with the `ai_*` properties → add a note carrying the summary, pain points, objections and the `event_id`. |
| 04 | `04-qualified-lead-notification.json` | Webhook `aiva-lead-qualified` | `lead.qualified` → a Slack message with who, why, the next step and the call id. |
| 05 | `05-meeting-booked-to-crm.json` | Webhook `aiva-meeting-booked` | `meeting.booked` → find the contact → a HubSpot meeting engagement (with the Cal.com uid when there is one) → stamp `ai_meeting_at`; no contact → a Slack warning. |
| 06 | `06-callback-due-to-call.json` | Cron, every 5 minutes | HubSpot `CALL` tasks that have fallen due → the task's contact → `POST /prospects` (upsert by number) → `POST /calls` with `Idempotency-Key: hubspot-task-<id>` → the task marked `IN_PROGRESS`. |

**On 06.** The agent's *own* callbacks — the ones a prospect asks for on a
call, and the ones scheduled through `POST /callbacks` — are placed by
`campaign.py run` with no help from n8n. Workflow 06 is for callbacks that
live somewhere else; HubSpot tasks are the example, and the only thing the
agent needs from any source is `POST /calls`.

**Pointing the agent at a workflow.** Open the Webhook node, copy the
**Production URL** (the Test URL only listens while you click *Listen for
test event*), and set it as `AUTOMATION_WEBHOOK_URL` — or as the per-kind
`AUTOMATION_WEBHOOK_URL_<KIND>` when each event has its own workflow, which
is how 03/04/05 are laid out. Activate the workflow; until it is active n8n
answers 404 and the deliverer keeps retrying.

**One URL for everything** works too: send every kind to one workflow and
branch on `{{ $json.body.event }}` with a Switch node.

---

## 5. Operating it

```bash
uv run automation.py                    # the API + the deliverer
uv run automation.py --no-deliver       # the API only
uv run automation.py --once             # one delivery pass, no server — for cron
uv run campaign.py events               # the outbox: PENDING, RETRY, DELIVERED, FAILED, with errors
uv run campaign.py events --state failed
uv run campaign.py events-retry --all-failed
uv run campaign.py events-retry --event 42
```

Log lines to grep: `automation.api_ready`, `automation.refused` (a bad
key), `automation.call_queued`, `automation.import`, `automation.campaign`,
`automation.delivered`, `automation.retry_scheduled`, `automation.failed`,
`automation.claim_failed`. Keys, tokens and the secret are scrubbed from
every line.

**Two deliverers** (two `automation.py`, or `--once` from cron while one
runs) are safe: events are claimed `FOR UPDATE SKIP LOCKED`, so no event is
sent twice; a deliverer that dies mid-pass leaves its rows to be reclaimed
after `AUTOMATION_STALE_SECS`.

**Ordering.** Events are delivered oldest fact first within a pass, but a
retried event can arrive after a newer one. Key on `event_id` and
`occurred_at`, not on arrival order.

**What has never been done.** As of Phase 17 no real n8n instance has
received a delivery from this agent: the request shape, the signature and
the retry rules are verified by `tests/test_automation.py` against a
recording sender, and the workflows are valid n8n JSON whose connections
name real nodes — but the first workflow you activate is the first live
test. Start with 04 (one Slack message) and `uv run automation.py --once`.
