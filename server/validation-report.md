# Validation report

Generated 2026-09-18 11:34 UTC by `uv run validate.py` on `MUBEEN-PC`.

**Verdict: 7 automated check(s) FAILED. Items marked *requires manual verification* have not been proven on this system and must be done by a person before go-live.**

## Requirements

| Requirement | Automated evidence | Status | Requires manual verification |
|---|---|---|---|
| 1. CSV/prospect import | `test_campaigns`, `test_automation`, `test_production` | FAILED | — |
| 2. Campaign creation | `test_campaigns`, `test_automation`, `test_production` | FAILED | — |
| 3. Campaign activation | `test_campaigns`, `test_worker`, `test_production` | FAILED | — |
| 4. Automatic prospect selection | `test_worker`, `test_scaling`, `test_production` | FAILED | — |
| 5. DNC enforcement | `test_compliance`, `test_worker`, `test_production` | FAILED | — |
| 6. Calling-hour enforcement | `test_reliability`, `test_worker`, `test_production` | FAILED | — |
| 7. Automatic outbound call | `test_worker`, `test_production` | FAILED | one real call through the configured carrier: `uv run validate.py live --to <your phone>` (no call has ever been answered on this system) |
| 8. Human conversation | `test_conversation`, `test_production` | FAILED | the sales eval suite against the deployed model: `uv run validate.py --evals`, then a real call |
| 9. Barge-in | `test_voice_quality`, `test_production` | FAILED | the audio eval suite (`evals/suite.yaml`) and `tests/phone_drill.py barge_in` against a running bot; a real handset |
| 10. Knowledge-base retrieval | `test_knowledge`, `test_production` | FAILED | `uv run ingest.py add <your documents>` then `evals/sales/unknown_question.yaml` against the deployed model |
| 11. Qualification | `test_conversation`, `test_results`, `test_production` | FAILED | read a real call's result: `uv run campaign.py result <attempt>` |
| 12. Objection handling | `test_conversation`, `test_production` | FAILED | `evals/sales/price_objection.yaml` against the deployed model |
| 13. Meeting booking | `test_actions`, `test_booking_transfer`, `test_scheduling`, `test_production` | FAILED | one real Cal.com booking on a real call (`uv run health.py calendar` first); no live booking has been observed |
| 14. Human transfer | `test_booking_transfer`, `test_production` | FAILED | one real transfer to `TELEPHONY_TRANSFER_NUMBER`, then `uv run campaign.py transfers`; no live transfer has been observed |
| 15. Callback scheduling | `test_actions`, `test_worker`, `test_production` | FAILED | `evals/sales/callback.yaml` against the deployed model |
| 16. Callback execution | `test_worker`, `test_production` | FAILED | a scheduled callback placed by `campaign.py run` at its time, on a real carrier |
| 17. Voicemail/no-answer handling | `test_voice_quality`, `test_worker`, `test_production` | FAILED | `tests/phone_drill.py voicemail`, then one real call to a voicemail with `TELEPHONY_MACHINE_DETECTION=async` |
| 18. Call completion webhook | `test_webhooks`, `test_scaling`, `test_production` | FAILED | one real delivery from the carrier: `uv run campaign.py webhooks` after the first real call (needs `TELEPHONY_PUBLIC_URL` and the signing key) |
| 19. Database persistence | `test_campaigns`, `test_dashboard`, `test_production`, `measure` | FAILED | — |
| 20. CRM synchronization | `test_crm`, `test_production` | FAILED | one real filing: `CRM_PROVIDER=hubspot`, `uv run health.py crm`, `uv run campaign.py crm-sync --once`, `crm-status`; no real HubSpot filing has been observed |
| 21. n8n workflow | `test_automation`, `test_production` | FAILED | import `n8n/workflows/04-qualified-lead-notification.json` into a live n8n, activate it, `uv run automation.py --once`, `uv run campaign.py events` |
| 22. Dashboard visibility | `test_dashboard`, `test_security`, `test_production` | FAILED | open `uv run dashboard.py` in a browser behind TLS and check one real call's detail page |
| 23. Authentication/authorization | `test_security`, `test_production`, `posture` | FAILED | sign in over HTTPS behind the real proxy (`SECURITY_REQUIRE_HTTPS=true`) and confirm the `Secure` cookie and the redirect |
| 24. Retry/recovery behavior | `test_reliability`, `test_scaling`, `test_worker`, `test_production` | FAILED | kill one of two `campaign.py run` processes mid-call and watch the other adopt it (`campaign.py workers`) |
| 25. Cost/usage tracking | `test_performance`, `test_production`, `measure` | FAILED | set `COST_*` rates and read one real call's cost on its detail page |
| No duplicate calls | `test_reliability`, `test_scaling`, `test_performance`, `test_production`, `measure` | FAILED | after the first real campaign: `uv run validate.py measure` reports zero prospects with two live attempts |
| No unauthorized dashboard/API access | `test_security`, `test_production`, `posture` | FAILED | — |
| Secrets not exposed | `test_security`, `test_monitoring`, `test_production`, `posture` | FAILED | rotate every key that was in `server/.env` while it was tracked, if that repository was ever pushed |
| Graceful failure of external providers | `test_reliability`, `test_booking_transfer`, `test_crm`, `test_automation`, `test_production` | FAILED | — |
| Latency | `test_production` | FAILED | measured on real audio only: `tests/phone_drill.py all` against a running bot and one real call (`validate.py live`); the last measured figures are in HANDOFF.md §10 |
| Call success rate | `measure` | verified (automated) | needs real calls: `uv run validate.py measure` after the first campaign |
| Health, readiness, metrics | `test_monitoring`, `health` | FAILED | point a Prometheus at every port and confirm `aiva_up` per role; probe `/readyz` from the orchestrator |

## Automated checks

### config

- **WARN** .env defines every variable once — defined more than once, last line wins: AUTOMATION_API_KEYS
- **PASS** server/.env is not tracked by git
- **PASS** providers: deepgram_flux / cerebras:qwen-3.8-27b / deepgram
- **PASS** carrier: signalwire
- **WARN** carrier webhooks verifiable — webhooks off — SIGNALWIRE_SIGNING_KEY is not set, so signalwire events could not be verified; call status is polled
- **PASS** calendar: calcom
- **PASS** CRM: hubspot
- **PASS** automation API keys
- **PASS** n8n delivery URL
- **PASS** dashboard users
- **WARN** HTTPS required — SECURITY_REQUIRE_HTTPS unset: fine on loopback, not behind a public address
- **WARN** compliance jurisdictions — none configured: the operator decides the rules per country (COMPLIANCE.md)
- **WARN** cost rates — no COST_* rates: usage is measured, cost is not estimated
- **PASS** monitoring — /healthz /readyz /metrics on every server, scheduler on 127.0.0.1:7895; metrics open (set MONITORING_TOKEN once a port is reachable from a network); fleet gauges refreshed every 30s over a 60 min throughput window

### health

- **PASS** application — pgvector(bge-small-en-v1.5, top4@0.5, auto) knowledge base, sales on
- **FAIL** database — no answer within 6s
- **FAIL** scheduler — no answer within 6s
- **FAIL** knowledge — no answer within 6s
- **PASS** stt — deepgram_flux:flux-general-en — key accepted
- **PASS** llm — cerebras:qwen-3.8-27b — key accepted
- **PASS** tts — deepgram — key accepted
- **SKIP** tts_fallback — no fallback (TTS_FALLBACK_ENABLED=false)
- **PASS** telephony — signalwire: Main (active)
- **PASS** calendar — calcom: event type 6963977 '30 min meeting', 30 min
- **FAIL** crm — HubSpot did not answer within 6s for GET /crm/v3/objects/contacts; whether it acted on the request is unknown

### posture

- **PASS** dashboard login on: 1 user(s): 1 admin
- **PASS** sessions signed with DASHBOARD_SESSION_SECRET
- **PASS** automation API keys: 1 admin, 0 operator, 0 viewer
- **WARN** every API key is an admin key
- **WARN** the OpenAPI docs are served at /api/v1/docs
- **PASS** outbound events signed (X-Aiva-Signature)
- **WARN** HTTP is allowed (SECURITY_REQUIRE_HTTPS unset)
- **PASS** CORS off (no cross-origin browser access)
- **PASS** rate limits: 300/min per key, 30/min anonymous, 5/min logins; bodies up to 5120 KiB
- **PASS** audit log on (audit_log table; `uv run campaign.py audit`)
- **WARN** carrier webhooks cannot be verified: webhooks off — SIGNALWIRE_SIGNING_KEY is not set, so signalwire events could not be verified; call status is polled
- **WARN** /metrics is open (no MONITORING_TOKEN)
- **PASS** server/.env is not tracked by git
- **WARN** .env defines AUTOMATION_API_KEYS more than once; the last line wins

### measure

- **PASS** no prospect has two live attempts — {'prospects_with_two_live_attempts': 0, 'repeated_idempotency_keys': 0}
- **PASS** no idempotency key is repeated
- **PASS** attempts by status: {}
- **SKIP** answer rate None over 0 finished attempt(s); success rate None over 0 result(s) — no finished real calls yet
- **SKIP** response latency p50 None ms / p95 None ms over 0 call(s) with figures — no call has written a quality summary yet
- **SKIP** cost: 0 priced call(s), $0.0000 total — no priced calls yet (set COST_* rates)

### test_conversation

- **PASS** the sales conversation (Phase 6) — 417 passed (25.9s)

### test_results

- **PASS** the call result (Phase 8) — 260 passed (18.0s)

### test_reliability

- **PASS** failure injection (Phase 9) — 141 passed (31.9s)

### test_performance

- **PASS** usage, cost, pooling, concurrency (Phase 11) — 36 passed, skipped: database checks (cannot reach PostgreSQL: (EMAXCONNSESSION) max clients reached in session mode - max clients are limited to pool_size: 15) (39.4s)

### test_actions

- **PASS** the tools (Phase 7) — 242 passed (30.5s)

### test_scheduling

- **PASS** the calendar (Phase 7) — 68 passed (6.8s)

### test_knowledge

- **PASS** retrieval (Phase 3) — 81 passed (53.7s)

### test_telephony

- **PASS** the carriers (Phase 4) — 143 passed (43.3s)

### test_realtime

- **PASS** turn-taking wiring (Phase 2) — 44 passed (28.9s)

### test_campaigns

- **PASS** prospects, campaigns, the queue (Phase 5) — 248 passed (710.6s)

### test_voice_quality

- **PASS** turn monitoring, voicemail (Phase 12) — 193 passed (45.5s)

### test_worker

- **PASS** the scheduler (Phase 13) — 195 passed, skipped: database checks (cannot reach PostgreSQL: (EMAXCONNSESSION) max clients reached in session mode - max clients are limited to pool_size: 15) (209.6s)

### test_webhooks

- **PASS** carrier webhooks (Phase 14) — 172 passed, skipped: database checks (cannot reach PostgreSQL: (EMAXCONNSESSION) max clients reached in session mode - max clients are limited to pool_size: 15) (39.6s)

### test_crm

- **PASS** CRM sync (Phase 15) — 180 passed (257.4s)

### test_booking_transfer

- **PASS** booking and transfer (Phase 16) — 81 passed, skipped: database checks (cannot reach PostgreSQL: (EMAXCONNSESSION) max clients reached in session mode - max clients are limited to pool_size: 15) (52.4s)

### test_automation

- **FAIL** the n8n API and outbox (Phase 17) — exit 1: asyncpg.exceptions.InternalServerError: (EMAXCONNSESSION) max clients reached in session mode - max clients are limited to pool_size: 15

### test_security

- **PASS** authentication, authorisation, hardening (Phase 18) — 216 passed (44.0s)

### test_compliance

- **FAIL** compliance controls (Phase 19) — exit 1: asyncpg.exceptions.InternalServerError: (EMAXCONNSESSION) max clients reached in session mode - max clients are limited to pool_size: 15

### test_dashboard

- **PASS** the dashboard (Phases 10, 20) — 106 passed, skipped: aggregate checks (cannot reach PostgreSQL: (EMAXCONNSESSION) max clients reached in session mode - max clients are limited to pool_size: 15) (180.8s)

### test_scaling

- **PASS** multi-worker scaling (Phase 21) — 114 passed, skipped: database checks (cannot reach PostgreSQL: (EMAXCONNSESSION) max clients reached in session mode - max clients are limited to pool_size: 15) (39.4s)

### test_monitoring

- **PASS** monitoring and observability (Phase 22) — 170 passed (139.8s)

### test_production

- **FAIL** the end-to-end story over PostgreSQL (Phase 23) — exit 1: asyncpg.exceptions.InternalServerError: (EMAXCONNSESSION) max clients reached in session mode - max clients are limited to pool_size: 15

### test_app

- **PASS** the unified application (Phase 24) — 162 passed (124.8s)

### test_engine

- **PASS** the campaign execution engine (Phase 25) — 63 passed (132.5s)

### test_spoken_text

- **PASS** what the caller may hear (Phase 26) — 113 passed (28.7s)

### test_latency

- **PASS** per-turn latency instrumentation (Phase 29) — 97 passed (55.0s)

### test_tts_fallback

- **PASS** the TTS fallback (Phase 30) — 78 passed (47.7s)

### test_tool_advertising

- **PASS** the prompt budget and per-stage tool advertising (Phase 31) — 112 passed (45.2s)

### test_tool_round_trips

- **PASS** which tools need a second LLM request (Phase 32) — 54 passed (56.5s)

### test_client_theme

- **PASS** the browser client in the application's design (Phase 33) — 44 passed (20.4s)

### test_knowledge_index

- **PASS** the in-memory knowledge index and the retrieval timeout (Phase 37) — 0 passed (21.7s)

### test_spoken_values

- **PASS** dictated phone numbers and email addresses stored as values (Phase 40) — 138 passed (26.9s)

**32 check scripts, 4,298 checks passed, 0 failed.**

The eval suites were not run in this invocation (pass `--evals`; they need vendor keys and a working TTS).

## Still requiring manual verification

- **7. Automatic outbound call** — one real call through the configured carrier: `uv run validate.py live --to <your phone>` (no call has ever been answered on this system)
- **8. Human conversation** — the sales eval suite against the deployed model: `uv run validate.py --evals`, then a real call
- **9. Barge-in** — the audio eval suite (`evals/suite.yaml`) and `tests/phone_drill.py barge_in` against a running bot; a real handset
- **10. Knowledge-base retrieval** — `uv run ingest.py add <your documents>` then `evals/sales/unknown_question.yaml` against the deployed model
- **11. Qualification** — read a real call's result: `uv run campaign.py result <attempt>`
- **12. Objection handling** — `evals/sales/price_objection.yaml` against the deployed model
- **13. Meeting booking** — one real Cal.com booking on a real call (`uv run health.py calendar` first); no live booking has been observed
- **14. Human transfer** — one real transfer to `TELEPHONY_TRANSFER_NUMBER`, then `uv run campaign.py transfers`; no live transfer has been observed
- **15. Callback scheduling** — `evals/sales/callback.yaml` against the deployed model
- **16. Callback execution** — a scheduled callback placed by `campaign.py run` at its time, on a real carrier
- **17. Voicemail/no-answer handling** — `tests/phone_drill.py voicemail`, then one real call to a voicemail with `TELEPHONY_MACHINE_DETECTION=async`
- **18. Call completion webhook** — one real delivery from the carrier: `uv run campaign.py webhooks` after the first real call (needs `TELEPHONY_PUBLIC_URL` and the signing key)
- **20. CRM synchronization** — one real filing: `CRM_PROVIDER=hubspot`, `uv run health.py crm`, `uv run campaign.py crm-sync --once`, `crm-status`; no real HubSpot filing has been observed
- **21. n8n workflow** — import `n8n/workflows/04-qualified-lead-notification.json` into a live n8n, activate it, `uv run automation.py --once`, `uv run campaign.py events`
- **22. Dashboard visibility** — open `uv run dashboard.py` in a browser behind TLS and check one real call's detail page
- **23. Authentication/authorization** — sign in over HTTPS behind the real proxy (`SECURITY_REQUIRE_HTTPS=true`) and confirm the `Secure` cookie and the redirect
- **24. Retry/recovery behavior** — kill one of two `campaign.py run` processes mid-call and watch the other adopt it (`campaign.py workers`)
- **25. Cost/usage tracking** — set `COST_*` rates and read one real call's cost on its detail page
- **No duplicate calls** — after the first real campaign: `uv run validate.py measure` reports zero prospects with two live attempts
- **Secrets not exposed** — rotate every key that was in `server/.env` while it was tracked, if that repository was ever pushed
- **Latency** — measured on real audio only: `tests/phone_drill.py all` against a running bot and one real call (`validate.py live`); the last measured figures are in HANDOFF.md §10
- **Call success rate** — needs real calls: `uv run validate.py measure` after the first campaign
- **Health, readiness, metrics** — point a Prometheus at every port and confirm `aiva_up` per role; probe `/readyz` from the orchestrator

See `PRODUCTION_READINESS.md` for the go-live checklist these feed.
