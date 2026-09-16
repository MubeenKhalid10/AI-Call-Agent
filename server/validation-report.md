# Validation report

Generated 2026-09-15 11:22 UTC by `uv run validate.py` on `MUBEEN-PC`.

**Verdict: 1 automated check(s) FAILED. Items marked *requires manual verification* have not been proven on this system and must be done by a person before go-live.**

## Requirements

| Requirement | Automated evidence | Status | Requires manual verification |
|---|---|---|---|
| 1. CSV/prospect import | `test_campaigns`, `test_automation`, `test_production` | verified (automated) | — |
| 2. Campaign creation | `test_campaigns`, `test_automation`, `test_production` | verified (automated) | — |
| 3. Campaign activation | `test_campaigns`, `test_worker`, `test_production` | verified (automated) | — |
| 4. Automatic prospect selection | `test_worker`, `test_scaling`, `test_production` | verified (automated) | — |
| 5. DNC enforcement | `test_compliance`, `test_worker`, `test_production` | verified (automated) | — |
| 6. Calling-hour enforcement | `test_reliability`, `test_worker`, `test_production` | verified (automated) | — |
| 7. Automatic outbound call | `test_worker`, `test_production` | verified (automated) | one real call through the configured carrier: `uv run validate.py live --to <your phone>` (no call has ever been answered on this system) |
| 8. Human conversation | `test_conversation`, `test_production` | verified (automated) | the sales eval suite against the deployed model: `uv run validate.py --evals`, then a real call |
| 9. Barge-in | `test_voice_quality`, `test_production` | verified (automated) | the audio eval suite (`evals/suite.yaml`) and `tests/phone_drill.py barge_in` against a running bot; a real handset |
| 10. Knowledge-base retrieval | `test_knowledge`, `test_production` | verified (automated) | `uv run ingest.py add <your documents>` then `evals/sales/unknown_question.yaml` against the deployed model |
| 11. Qualification | `test_conversation`, `test_results`, `test_production` | verified (automated) | read a real call's result: `uv run campaign.py result <attempt>` |
| 12. Objection handling | `test_conversation`, `test_production` | verified (automated) | `evals/sales/price_objection.yaml` against the deployed model |
| 13. Meeting booking | `test_actions`, `test_booking_transfer`, `test_scheduling`, `test_production` | verified (automated) | one real Cal.com booking on a real call (`uv run health.py calendar` first); no live booking has been observed |
| 14. Human transfer | `test_booking_transfer`, `test_production` | verified (automated) | one real transfer to `TELEPHONY_TRANSFER_NUMBER`, then `uv run campaign.py transfers`; no live transfer has been observed |
| 15. Callback scheduling | `test_actions`, `test_worker`, `test_production` | verified (automated) | `evals/sales/callback.yaml` against the deployed model |
| 16. Callback execution | `test_worker`, `test_production` | verified (automated) | a scheduled callback placed by `campaign.py run` at its time, on a real carrier |
| 17. Voicemail/no-answer handling | `test_voice_quality`, `test_worker`, `test_production` | verified (automated) | `tests/phone_drill.py voicemail`, then one real call to a voicemail with `TELEPHONY_MACHINE_DETECTION=async` |
| 18. Call completion webhook | `test_webhooks`, `test_scaling`, `test_production` | verified (automated) | one real delivery from the carrier: `uv run campaign.py webhooks` after the first real call (needs `TELEPHONY_PUBLIC_URL` and the signing key) |
| 19. Database persistence | `test_campaigns`, `test_dashboard`, `test_production`, `measure` | verified (automated) | — |
| 20. CRM synchronization | `test_crm`, `test_production` | verified (automated) | one real filing: `CRM_PROVIDER=hubspot`, `uv run health.py crm`, `uv run campaign.py crm-sync --once`, `crm-status`; no real HubSpot filing has been observed |
| 21. n8n workflow | `test_automation`, `test_production` | verified (automated) | import `n8n/workflows/04-qualified-lead-notification.json` into a live n8n, activate it, `uv run automation.py --once`, `uv run campaign.py events` |
| 22. Dashboard visibility | `test_dashboard`, `test_security`, `test_production` | verified (automated) | open `uv run dashboard.py` in a browser behind TLS and check one real call's detail page |
| 23. Authentication/authorization | `test_security`, `test_production`, `posture` | verified (automated) | sign in over HTTPS behind the real proxy (`SECURITY_REQUIRE_HTTPS=true`) and confirm the `Secure` cookie and the redirect |
| 24. Retry/recovery behavior | `test_reliability`, `test_scaling`, `test_worker`, `test_production` | verified (automated) | kill one of two `campaign.py run` processes mid-call and watch the other adopt it (`campaign.py workers`) |
| 25. Cost/usage tracking | `test_performance`, `test_production`, `measure` | verified (automated) | set `COST_*` rates and read one real call's cost on its detail page |
| No duplicate calls | `test_reliability`, `test_scaling`, `test_performance`, `test_production`, `measure` | verified (automated) | after the first real campaign: `uv run validate.py measure` reports zero prospects with two live attempts |
| No unauthorized dashboard/API access | `test_security`, `test_production`, `posture` | verified (automated) | — |
| Secrets not exposed | `test_security`, `test_monitoring`, `test_production`, `posture` | verified (automated) | rotate every key that was in `server/.env` while it was tracked, if that repository was ever pushed |
| Graceful failure of external providers | `test_reliability`, `test_booking_transfer`, `test_crm`, `test_automation`, `test_production` | verified (automated) | — |
| Latency | `test_production` | verified (automated) | measured on real audio only: `tests/phone_drill.py all` against a running bot and one real call (`validate.py live`); the last measured figures are in HANDOFF.md §10 |
| Call success rate | `measure` | verified (automated) | needs real calls: `uv run validate.py measure` after the first campaign |
| Health, readiness, metrics | `test_monitoring`, `health` | FAILED | point a Prometheus at every port and confirm `aiva_up` per role; probe `/readyz` from the orchestrator |

## Automated checks

### config

- **PASS** .env defines every variable once
- **PASS** server/.env is not tracked by git
- **PASS** providers: deepgram_flux / groq:qwen/qwen3.8-27b / elevenlabs
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
- **PASS** database — 25 prospect(s), 0 live attempt(s)
- **WARN** scheduler — 1 alive, 7 STALE, 12 stopped, 0 call(s) followed; queue: 0 due now (0 queued + 0 callbacks), 0 scheduled later, 0 reserved, 0 live, 0 active campaign(s) — 7 worker(s) stopped beating; a live worker adopts their calls
- **PASS** knowledge — 2 document(s), 25 chunk(s)
- **PASS** stt — deepgram_flux:flux-general-en — key accepted
- **PASS** llm — groq:qwen/qwen3.8-27b — key accepted
- **WARN** tts — elevenlabs:eleven_flash_v2_5 — key recognised but restricted (The API key you used is missing the permission user_read to execute this operati); only synthesis can confirm it
- **SKIP** tts_fallback — no fallback (TTS_FALLBACK_ENABLED=false)
- **PASS** telephony — signalwire: Main (active)
- **PASS** calendar — calcom: event type 6963977 '30 min meeting', 30 min
- **FAIL** crm — HubSpot did not answer within 6s for GET /crm/v3/objects/contacts; whether it acted on the request is unknown

### posture

- **PASS** dashboard login on: 1 user(s): 1 admin
- **WARN** no DASHBOARD_SESSION_SECRET: sessions end at every restart
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
- **PASS** .env defines every variable once

### measure

- **PASS** no prospect has two live attempts — {'prospects_with_two_live_attempts': 0, 'repeated_idempotency_keys': 0}
- **PASS** no idempotency key is repeated
- **PASS** attempts by status: {'COMPLETED': 2, 'FAILED': 29}
- **PASS** answer rate 0.065 over 31 finished attempt(s); success rate 0.0 over 31 result(s)
- **SKIP** response latency p50 None ms / p95 None ms over 0 call(s) with figures — no call has written a quality summary yet
- **SKIP** cost: 0 priced call(s), $0.0000 total — no priced calls yet (set COST_* rates)

### test_conversation

- **PASS** the sales conversation (Phase 6) — 398 passed (12.2s)

### test_results

- **PASS** the call result (Phase 8) — 260 passed (16.6s)

### test_reliability

- **PASS** failure injection (Phase 9) — 141 passed (25.6s)

### test_performance

- **PASS** usage, cost, pooling, concurrency (Phase 11) — 59 passed (26.3s)

### test_actions

- **PASS** the tools (Phase 7) — 240 passed (19.2s)

### test_scheduling

- **PASS** the calendar (Phase 7) — 68 passed (11.1s)

### test_knowledge

- **PASS** retrieval (Phase 3) — 58 passed (47.9s)

### test_telephony

- **PASS** the carriers (Phase 4) — 143 passed (37.9s)

### test_realtime

- **PASS** turn-taking wiring (Phase 2) — 19 passed (21.6s)

### test_campaigns

- **PASS** prospects, campaigns, the queue (Phase 5) — 248 passed (25.0s)

### test_voice_quality

- **PASS** turn monitoring, voicemail (Phase 12) — 190 passed (41.4s)

### test_worker

- **PASS** the scheduler (Phase 13) — 216 passed (26.8s)

### test_webhooks

- **PASS** carrier webhooks (Phase 14) — 192 passed (30.9s)

### test_crm

- **PASS** CRM sync (Phase 15) — 180 passed (27.9s)

### test_booking_transfer

- **PASS** booking and transfer (Phase 16) — 99 passed (40.6s)

### test_automation

- **PASS** the n8n API and outbox (Phase 17) — 245 passed (42.0s)

### test_security

- **PASS** authentication, authorisation, hardening (Phase 18) — 214 passed (47.7s)

### test_compliance

- **PASS** compliance controls (Phase 19) — 147 passed (29.5s)

### test_dashboard

- **PASS** the dashboard (Phases 10, 20) — 161 passed (39.8s)

### test_scaling

- **PASS** multi-worker scaling (Phase 21) — 151 passed (30.8s)

### test_monitoring

- **PASS** monitoring and observability (Phase 22) — 170 passed (40.3s)

### test_production

- **PASS** the end-to-end story over PostgreSQL (Phase 23) — 158 passed (57.4s)

### test_app

- **PASS** the unified application (Phase 24) — 140 passed (85.3s)

### test_engine

- **PASS** the campaign execution engine (Phase 25) — 68 passed (115.5s)

### test_spoken_text

- **PASS** what the caller may hear (Phase 26) — 68 passed (16.9s)

### test_latency

- **PASS** per-turn latency instrumentation (Phase 29) — 97 passed (42.3s)

### test_tts_fallback

- **PASS** the TTS fallback (Phase 30) — 74 passed (36.2s)

### test_tool_advertising

- **PASS** the prompt budget and per-stage tool advertising (Phase 31) — 112 passed (49.6s)

### test_tool_round_trips

- **PASS** which tools need a second LLM request (Phase 32) — 54 passed (53.4s)

**29 check scripts, 4,370 checks passed, 0 failed.**

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
