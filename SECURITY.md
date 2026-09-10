# Security — Ai-Voice-Agent (Phase 18)

The dashboard, the automation API and the webhook receiver show or change
customer data: names, phone numbers, emails, transcripts, and the rows that
make a phone ring. This document says how each is protected, what a
production deployment **must** do before any of them faces a network, and
what is deliberately not covered.

Everything here is configured in `server/.env`. `uv run security.py check`
reads that file and prints the posture line by line; run it before going
live and after every change.

---

## 1. What is protected, and how

| Surface | Process | Authentication | Authorisation | Also |
|---|---|---|---|---|
| Dashboard page and JSON (`/`, `/api/dashboard`, `/api/me`) | `uv run dashboard.py` (port 7870) | Login form → signed session cookie `aiva_session`; or an API key in `Authorization: Bearer` on the JSON | Role from the user (viewer sees phone numbers masked) | Login rate limit, cross-site POST refused, CSP, audit of every login/logout |
| Automation API (`/api/v1/*`) | `uv run automation.py` (port 7890) | API key (`Authorization: Bearer` or `X-API-Key`) | Role from which key list the key is in; `403` with the missing permission named | Per-key and per-address rate limits, input validation, body cap, audit of every write and every transcript read |
| Carrier webhooks (`/webhooks/telephony`) | the bot, or `uv run webhooks.py` (7880) | The carrier's own signature (Twilio `X-Twilio-Signature` / SignalWire), verified against the configured public URL | — (a verified event is applied; anything else is `403` and touches nothing) | Refused-delivery rate limit per address, body cap |
| Outbound events to n8n | the deliverer in `automation.py` | `X-Aiva-Signature: t=…,v1=<HMAC-SHA256>` over `"<t>." + body`, plus a static header for n8n's Header Auth | — | Replay window 5 min; see `n8n/README.md` |
| `/api/ping` on each | — | none (says only "up" and "database reachable") | — | Shares the anonymous rate limit |

Unauthenticated endpoints never return customer data. The OpenAPI docs
(`/api/v1/docs`) describe routes, not rows, and can be switched off with
`AUTOMATION_DOCS_ENABLED=false`.

## 2. Roles

Three roles, and four permissions a route may ask for. A role is a set of
permissions, so a route asks for the permission and never for the role name.

| Role | `read` | `read_pii` | `write` | `manage` |
|---|---|---|---|---|
| **viewer** | ✓ | | | |
| **operator** | ✓ | ✓ | ✓ | |
| **admin** | ✓ | ✓ | ✓ | ✓ |

* `read` — totals, campaigns, calls, results, callbacks, meetings, the outbox: with phone numbers masked (`+92••••••••67`), emails masked, transcripts withheld (`transcript: null, transcript_included: false`) and custom fields emptied.
* `read_pii` — the numbers, the emails, the transcripts, the custom fields, event payloads, a lookup by phone number.
* `write` — create prospects and campaigns, import, add to a campaign, start / pause / resume, queue a call, schedule or withdraw a callback, mark do-not-call.
* `manage` — complete or cancel a campaign, reopen an outbox row, read the audit log (`GET /api/v1/audit`).

Masking is one walk over the JSON of every answer (`src/security/pii.py`),
keyed by field name, so a new serializer field called `phone` is masked
without anybody remembering to add a call.

## 3. Setting it up

```bash
cd server

# Dashboard users: name:role:hash, comma-separated. Never a plain password.
uv run security.py hash-password --user alice --role admin
uv run security.py hash-password --user sam --role viewer
#   DASHBOARD_USERS=alice:admin:scrypt$...,sam:viewer:scrypt$...

# Sessions survive a restart (and are shared by two processes) only with a fixed secret.
uv run security.py make-secret
#   DASHBOARD_SESSION_SECRET=...

# API keys, one list per role. Give n8n an operator key; keep admin keys for people.
uv run security.py make-key --role operator
#   AUTOMATION_OPERATOR_API_KEYS=...
uv run security.py make-key --role admin
#   AUTOMATION_API_KEYS=...

uv run campaign.py init          # adds the audit_log table (idempotent)
uv run security.py check         # the posture, line by line
```

Passwords are hashed with scrypt (`n=2^14, r=8, p=1`, 16-byte salt) from
the standard library; the parameters travel inside the hash so they can be
raised later. A login for a name that does not exist takes the same time as
one for a name that does. Sessions are stateless: a signed cookie (HMAC-SHA256)
carrying the user, the role, and an expiry (`DASHBOARD_SESSION_TTL_SECS`,
default 12 h). A role change takes effect at the next login; to sign
everybody out at once, rotate `DASHBOARD_SESSION_SECRET`.

`DASHBOARD_AUTH_DISABLED=true` restores the login-free page of Phase 10 for
local work. `dashboard.py` then refuses any `--host` but loopback.

## 4. Production HTTPS requirements

**Nothing here terminates TLS. Something in front must.** A password, a
session cookie and an API key all travel in the request, and over plain
HTTP they travel in clear.

Required for any deployment reachable beyond the machine it runs on:

1. **A reverse proxy or tunnel that terminates TLS** in front of the
   dashboard, the API and the webhook receiver (Caddy, nginx, a cloud load
   balancer, or ngrok/Cloudflare Tunnel for the webhook address). The
   processes themselves keep binding to `127.0.0.1`.
2. **`SECURITY_REQUIRE_HTTPS=true`.** Every server then refuses a request
   whose effective scheme is not `https`: a dashboard page is redirected
   (308), an API call or a webhook delivery is refused (403,
   `https_required`). Responses carry `Strict-Transport-Security`, and the
   session cookie is marked `Secure`.
3. **`SECURITY_TRUSTED_PROXIES`** listing the proxy's address(es) (CIDR).
   `X-Forwarded-Proto` and `X-Forwarded-For` are believed **only** from a
   trusted proxy — otherwise any client could claim to be HTTPS or to be
   somebody else. The default is loopback (`127.0.0.0/8, ::1/128`), which
   covers a proxy or tunnel agent on the same machine. Behind a proxy on
   another host, list it: `SECURITY_TRUSTED_PROXIES=10.0.0.5/32`.
4. **The carriers' public URL is `https://`.** `TELEPHONY_PUBLIC_URL` is
   what the carrier signs against; the tunnel forwards to the bot with
   `X-Forwarded-Proto: https`.

Minimal Caddy (automatic certificates):

```
dash.example.com {
    reverse_proxy 127.0.0.1:7870
}
api.example.com {
    reverse_proxy 127.0.0.1:7890
}
```

Minimal nginx location (the certificate lines omitted):

```
location / {
    proxy_pass         http://127.0.0.1:7870;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    client_max_body_size 6m;
}
```

Both proxies run on the same host as the servers, so the default trusted
proxies apply. Keep the processes on loopback; only the proxy listens on
the network.

## 5. CORS

Off by default: with no `SECURITY_CORS_ORIGINS`, no `Access-Control-*`
header is ever sent and a browser on another origin cannot read an answer.
List explicit origins (`https://app.example.com`) to allow one; a wildcard
is refused at startup. Credentials (the cookie) are allowed cross-origin
only on the dashboard. The API's allowed headers are `Authorization`,
`Content-Type`, `X-API-Key` and `Idempotency-Key`.

The dashboard's login and logout forms are additionally checked for a
same-site origin (`Sec-Fetch-Site`, then `Origin`/`Referer`), and the
cookie is `SameSite=Strict`, `HttpOnly`.

## 6. Rate limits

In-process sliding windows (`src/security/ratelimit.py`), one per purpose:

| Setting | Default | Applies to |
|---|---|---|
| `SECURITY_API_RATE_LIMIT` | 300 / min | each API key, on the automation API |
| `SECURITY_ANON_RATE_LIMIT` | 30 / min | each address, for requests with no valid credential (401s, `/api/ping`, refused webhook deliveries, unauthenticated dashboard JSON) |
| `SECURITY_LOGIN_RATE_LIMIT` | 5 / min | each address, on the dashboard login form |

A refusal is `429` with `Retry-After`. A refused API request spends the
address's anonymous budget *before* anything is written, so a script guessing
keys cannot fill the audit table. Two processes would each allow their own
limit; that bounds the rate at twice the figure, which is the shape of this
stage (Phase 9's pacing limiter and Phase 11's cache are in-process for the
same reason).

## 7. Input validation

Every API body is a Pydantic model with bounded lengths. Phase 18 adds:
control characters refused in every text field (newlines allowed only in
a description or a note); `custom_data` and extra fields capped at 16 KiB
and 100 keys; import rows capped at 60 columns and 2,000 characters per
cell; ids in paths must be positive; `Idempotency-Key` must be 1–200
characters of `[A-Za-z0-9._:-]`; `kind` must name a real event kind; and a
request body may be at most `SECURITY_MAX_BODY_BYTES` (5 MiB; 64 KiB on
the dashboard, 1 MiB on the webhook route), checked from `Content-Length`
before a byte is buffered and again while it streams. A phone that is not
a number is still accepted and stored `UNREACHABLE`, as Phase 17 promised.
Everything rendered on the dashboard is HTML-escaped in the page.

## 8. The audit log

Every sensitive action is one row in `audit_log` **and** one
`audit.<action>` log line:

| Action | When |
|---|---|
| `auth.login`, `auth.login_failed`, `auth.logout`, `auth.login_rate_limited`, `auth.login_refused` | the dashboard form |
| `auth.refused`, `auth.rate_limited`, `auth.forbidden` | an API key unknown, over its limit, or lacking a permission |
| `prospect.create`, `prospect.import`, `prospect.do_not_call` | |
| `campaign.create`, `campaign.add_prospects`, `campaign.start` / `pause` / `resume` / `complete` / `cancel` | |
| `call.queue`, `callback.schedule`, `callback.cancel` | |
| `pii.transcript_read` | a transcript included in an answer |
| `event.retry` | an outbox row reopened |

Each row carries the actor (a user name or a key label such as
`operator-key#2` — never the key), the role, how they authenticated, the
client address (past trusted proxies), the target row, the outcome and a
scrubbed detail. Read it with `uv run campaign.py audit [--action auth.]
[--actor alice] [--since-hours 24]` or `GET /api/v1/audit` (admin).

If the table cannot be written, the action still goes ahead and
`audit.unavailable` is logged once a minute; `SECURITY_AUDIT_STRICT=true`
refuses the action instead (503). `SECURITY_AUDIT_ENABLED=false` keeps the
log lines and drops the rows.

## 9. Secrets

* **Every secret is an environment variable** read from `server/.env`, and
  nothing else: no credential is in code, in a workflow file, or in the
  database. `.env.example` lists every variable with its default.
* **`server/.env` is not tracked by git.** `server/.gitignore` excludes it.
  It *was* tracked in the nested repository's first commit; if that commit
  was ever pushed, every key it carried is compromised and must be rotated
  (see below) — removing the file from the tree does not remove it from
  history.
* **Nothing secret is logged.** Phase 9's scrubber replaces the value of
  every variable in `SECRET_ENV` with `***` in every log record, exception
  text included, and Phase 18 adds the role-scoped keys, the session
  secret and the user directory — plus patterns for anything shaped like a
  password field, a session cookie or a scrypt hash, wherever it appears.
  The audit writer drops any detail field *named* like a credential. The
  checks in `tests/test_security.py` capture the log during logins and
  refused requests and assert no key, password, hash or cookie is in it.
* **Rotation.** Keys are lists: add the new key, move the clients, remove
  the old — no gap. Rotating `DASHBOARD_SESSION_SECRET` signs everyone out.
  A user's password is replaced by replacing their hash. Carrier and vendor
  keys rotate in the vendor's console and then in `.env`.
* **The monitoring routes (Phase 22).** Every server answers `GET
  /healthz`, `GET /readyz` and `GET /metrics` (`campaign.py run` on
  `MONITORING_PORT`), unauthenticated by design — a liveness probe holds no
  key. They carry nothing about a person: no metric label can be a phone
  number, a name or an email (`LABEL_NAMES_ALLOWED` is a closed list a
  check enforces), a readiness detail is scrubbed of credentials before it
  leaves, and requests are counted by route *template*, never by the path
  a number was in. `/metrics` does name campaign ids, error rates and
  costs, so `MONITORING_TOKEN` (a bearer, scrubbed from the log like every
  other secret) is expected once a port is reachable from a network;
  `uv run security.py check` says when it is missing. A call's correlation
  id (`trace`) is sixteen random hex characters — not a secret, not a row
  id, and safe to quote in a ticket.

## 10. Webhook verification

Inbound: a carrier delivery is verified with the carrier's own scheme against
`TELEPHONY_PUBLIC_URL` — never the URL the local server saw behind a tunnel —
and the account id must match. No signing key configured means every
delivery is refused (the worker's polling still applies outcomes). A forged,
unsigned or malformed delivery is `403`/`400`, touches nothing, and counts
toward the address's refused-delivery limit.

Outbound: every delivery to n8n carries `X-Aiva-Signature` (HMAC-SHA256 over
the timestamp and the exact body, 5-minute tolerance) when
`AUTOMATION_WEBHOOK_SECRET` is set, plus a static header for n8n's native
Header Auth. `n8n/README.md` shows the receiver's check.

## 11. Pre-production checklist

```
uv run security.py check --strict
```

exits 0 only when: the dashboard has users; a session secret is set; API
keys are per role; HTTPS is required with the proxy trusted; CORS is off or
explicit; the audit log is on; the carrier's signing key is set; and
`server/.env` is not tracked by git.

## 12. Not covered (deliberately, this phase)

* **No multi-factor authentication** and no password reset flow: users are
  operators configured by whoever runs the deployment.
* **No session revocation list.** A session ends when it expires or when
  the secret is rotated. Keep the TTL short if that matters.
* **Rate limits are per process.** A distributed limiter needs
  infrastructure the project has none of.
* **Encryption at rest** is PostgreSQL's and the disk's business; transcripts
  and numbers are stored in the clear in the database.
* **The bot's own web server** (`/client`, `/ws`) is Pipecat's dev runner:
  a browser test surface for development. It is not hardened here and
  should not be exposed in production beyond the tunnel the carrier needs
  for `/ws` and the webhook route.
