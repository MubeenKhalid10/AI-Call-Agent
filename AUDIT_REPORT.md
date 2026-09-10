# AI Voice Agent - Project Audit Report

**Audit date:** 2026-09-05  
**Repository:** `D:\Ai-Voice-Agent`  
**Primary runtime:** Python 3.12.14, Pipecat 1.8.1, Windows  
**Audit basis:** source code, configuration, tests, eval artifacts, health checks, and project handoff

## 1. Executive Summary

This repository contains a substantial outbound AI sales-agent prototype. The working core can:

- conduct a streaming voice conversation in a browser, evaluation harness, or telephony media stream;
- transcribe speech, detect turns, handle interruptions, synthesize replies, and recover from silence or dropped browser connections;
- answer from a PostgreSQL/pgvector knowledge base;
- run a stateful discovery and qualification conversation for a known prospect;
- record objections, interest, pain points, authority, timeline, next action, transcript, and tool activity;
- check and book meetings, schedule callbacks, mark do-not-call, transfer to a human, search knowledge, and end a call, subject to capability and state checks;
- import prospects and manage campaigns, attempts, retries, do-not-call state, calling windows, concurrency, and recovery;
- produce a validated structured call result and deterministic summary;
- expose a read-only operational dashboard and per-call usage/cost data;
- pass the repository's eleven deterministic test scripts and currently report all seven configured health components as healthy.

It is **not yet an unattended production calling platform**. The following are absent or not proven end to end:

- no scheduler/worker that continuously places queued calls or automatically executes callbacks;
- no CRM synchronization, webhook delivery, or external result push;
- no carrier webhook server; carrier status is polled;
- no dashboard authentication or authorization;
- no distributed rate/concurrency coordination;
- no proven live Twilio/SignalWire outbound call, live Cal.com booking, or live transfer;
- no real-microphone, noisy, accented, overlapping-speech, voicemail, or answering-machine coverage;
- unstable historical barge-in eval behavior, particularly after an interruption;
- significant static typing debt and a Python-version mismatch in the declared compatibility range.

**Overall assessment:** a well-structured, heavily tested prototype with a credible local and simulated-call workflow, but not ready to operate unattended or expose sensitive campaign data publicly.

## 2. Status Vocabulary

| Status | Meaning |
|---|---|
| Implemented | Code exists and is covered by focused tests or direct local verification. |
| Locally verified | Demonstrated against the current configured environment or a local simulator. |
| Stub verified | Tested with fakes/mocks; external behavior is not proven. |
| Partially implemented | Core mechanism exists, but an important workflow or operational piece is absent. |
| Not implemented | No working project capability exists. |
| Risk / unverified | The code exists, but evidence is insufficient or historical runs show instability. |

## 3. Architecture and Runtime Flow

The main pipeline is assembled in [server/bot.py](server/bot.py):

```text
transport input
  -> speech-to-text / turn detection
  -> user context aggregator
  -> optional knowledge retrieval
  -> sales conversation director
  -> LLM and tools
  -> text-to-speech
  -> transport output
  -> assistant context aggregator
```

The assistant aggregator is deliberately after output, so the stored assistant transcript represents what the caller actually heard, including an interrupted response. The same pipeline is reused across browser WebRTC, eval, and telephony transports.

The application is split into clear boundaries:

- `server/bot.py`: runtime wiring and session lifecycle.
- `server/src/services.py`: provider factories.
- `server/src/conversation/`: sales state, qualification, prompts, tools, transcript, and results.
- `server/src/actions/`: validated side effects and external integrations.
- `server/src/campaigns/`: prospects, campaigns, queue, dialer, persistence, recovery, and results.
- `server/src/telephony/`: carrier placement, serializers, call sessions, and transport setup.
- `server/src/scheduling/`: local calendar and Cal.com abstraction.
- `server/src/reliability/`: retries, guardrails, supervision, health, observability, idempotency, and usage.
- `server/src/dashboard/`: read-only reporting page and JSON endpoint.

## 4. Providers, Models, and Tools

### Default configured stack

| Function | Provider / model | Status |
|---|---|---|
| STT and default turn detection | Deepgram Flux, `flux-general-en` | Locally health-checked; Flux API key accepted |
| Fallback STT | Deepgram Nova 3, `nova-3` | Factory implemented; not separately live-verified in this audit |
| LLM | Groq, `qwen/qwen3.8-27b` | Locally health-checked; key and model accepted |
| TTS in current environment | Deepgram TTS | Locally health-checked; key accepted |
| TTS default in documentation | Cartesia Sonic | Factory implemented; historical eval stack and config support it |
| Embeddings | FastEmbed `BAAI/bge-small-en-v1.5`, 384 dimensions | Local knowledge tests and current KB health pass |
| Database | PostgreSQL with pgvector | Current database health pass |
| Calendar default | Local PostgreSQL-backed calendar | Tested and usable locally |

### Alternative provider configuration

The factories support these LLM providers: Groq, Anthropic, OpenAI, Cerebras, OpenRouter, Mistral, and Ollama. They are configuration-supported, but most do not have live end-to-end evidence in this repository. TTS supports Cartesia and Deepgram. STT supports Deepgram Flux and classic Deepgram.

Outbound placement supports Twilio and SignalWire. Incoming media serialization supports Twilio, Telnyx, Plivo, and Exotel, but receiving audio and placing outbound calls are separate capabilities.

### Model-facing tools

The advertised tool set contains twelve tools:

1. `record_discovery`
2. `record_interest`
3. `record_objection`
4. `move_stage`
5. `record_meeting_intent`
6. `search_knowledge_base`
7. `check_calendar_availability`
8. `book_meeting`
9. `schedule_callback`
10. `mark_do_not_call`
11. `transfer_to_human`
12. `end_call`

The strict toolkit validates arguments, drops/logs unknown fields, converts exceptions into structured failures, records an audit entry, and only exposes success after the backend confirms the side effect. This is one of the strongest parts of the design.

## 5. Capability Audit

### Voice and conversation

| Capability | Status | Assessment |
|---|---|---|
| Streaming STT, LLM, and TTS | Implemented / locally verified | Pipeline stages stream; current health checks accept all configured credentials. |
| Flux speech turn detection | Implemented / locally verified | Flux owns end-of-turn behavior on the default path. |
| Classic Deepgram fallback | Implemented | Uses Silero VAD and Smart Turn v3; limited live evidence. |
| Barge-in | Implemented with risk | Interruption path exists and is tested, but historical audio evals failed twice after interruption. |
| Interrupted-reply marking | Implemented | Prevents truncated assistant context from poisoning later responses; model-specific quality still needs repeated validation. |
| Silence check-ins and graceful close | Implemented / eval verified | Audio silence scenarios passed in recorded runs. |
| Browser reconnect grace | Implemented / deterministic tests | WebRTC peer watchdog and reconnect handling exist. |
| Session idle timeout | Implemented | Eval runs require a longer override because the timer starts at bot startup. |
| Echo suppression | Implemented | `off`, `greeting`, and `always`; `always` disables barge-in. |
| Real microphone behavior | Not proven | Existing audio evals use synthetic audio, not a human microphone. |
| Voicemail / answering-machine detection | Not implemented | Machine detection is not configured; voicemail is treated as a non-speaking endpoint. |

### Knowledge base

| Capability | Status | Assessment |
|---|---|---|
| PDF and text/Markdown ingestion | Implemented | `server/ingest.py` and `src/documents.py`. |
| Chunking and SHA-256 change detection | Implemented | Re-ingestion skips unchanged documents unless forced. |
| Local embeddings | Implemented / locally verified | FastEmbed BGE model, no per-query external embedding cost. |
| PostgreSQL vector storage | Implemented / locally verified | Current health reports 2 documents and 25 chunks. |
| Per-turn retrieval | Implemented | Retrieval is a pipeline stage and does not pollute conversation history. |
| Retrieval gating | Implemented with risk | `auto` skips likely non-information turns; unusual business questions may be skipped. |
| Grounded refusal | Implemented | Prompt instructs the model to refuse when excerpts do not contain the answer. |
| Retrieval CLI inspection | Implemented | `ingest.py search` shows passages, scores, and threshold behavior. |

### Sales intelligence

| Capability | Status | Assessment |
|---|---|---|
| Ten-state sales conversation machine | Implemented / deterministic tests | Refused transitions are recorded rather than crashing the call. |
| Prospect and campaign briefing | Implemented | Unknown fields are explicitly rendered as not known. |
| Qualification | Implemented / deterministic tests | Derived from need, interest, and authority; unknown is preserved. |
| Objection capture | Implemented | Closed vocabulary plus details. |
| DNC handling | Implemented | Deterministic detection, tool path, prospect update, membership closure, and queue exclusion. |
| Human request and callback signals | Implemented | Signals guide the model; only DNC deterministically forces state. |
| Transcript | Implemented | Verbatim user and heard-assistant turns are stored. |
| Deterministic summary | Implemented | Six-part summary is derived from structured evidence, not a second model pass. |

### Actions and workflows

| Capability | Status | Assessment |
|---|---|---|
| Local meeting availability | Implemented / deterministic tests | Business hours, slot grid, notice window, horizon, and existing meetings are checked. |
| Local meeting booking | Implemented with concurrency risk | Check-then-write is not transactionally protected against parallel double booking. |
| Cal.com integration | Stub verified only | Request shape is tested against HTTP stubs; no live Cal.com account test. |
| Callback scheduling | Implemented | Stores one pending callback per prospect and reopens campaign membership. |
| Automatic callback execution | Not implemented | A callback only becomes queue work; a human must run the campaign command. |
| Do-not-call | Implemented / deterministic and eval evidence | Anonymous inbound DNC cannot be persisted because no prospect lookup exists. |
| Human transfer | Implemented / stub verified | Blind carrier call update; no live transfer observed. |
| End call | Implemented | Uses graceful worker termination behavior. |
| Email or information sending | Not implemented | The agent must not claim an email was sent. |

### Campaigns and telephony

| Capability | Status | Assessment |
|---|---|---|
| CSV import and preview | Implemented / deterministic tests | Known fields are mapped; unknown fields are retained in `custom_data`. |
| Phone normalization | Implemented / deterministic tests | Uses libphonenumber and refuses ambiguous local numbers without a region. |
| Campaign lifecycle | Implemented | Draft, active, paused, completed, and cancelled states. |
| Queue reservation | Implemented / concurrency tested | PostgreSQL transaction with row locks and `SKIP LOCKED`. |
| Duplicate-call protection | Implemented / reliability tested | Derived idempotency key, unique index, live-attempt exclusion, and fresh pre-placement DNC check. |
| Calling hours | Implemented | Prospect timezone when present, configured fallback otherwise. |
| In-process concurrency and pacing | Implemented with scaling limit | Protects one process; multiple dialers can exceed the intended rate. |
| Manual outbound call | Implemented | `server/call.py` places one call and polls status. |
| Campaign outbound call | Implemented | `server/campaign.py call` reserves and places calls, one command invocation at a time. |
| Continuous scheduler | Not implemented | No daemon, worker loop, queue service, or unattended runner. |
| Carrier status polling | Implemented | Polling is used instead of webhooks. |
| Carrier webhook endpoint | Not implemented | Idempotent storage entry point exists, but no server route receives events. |
| Real carrier outbound call | Unverified | Tests use stubs; fake carrier verifies media protocol but not carrier placement. |
| Inbound caller lookup | Not implemented | Inbound audio can be accepted, but phone-number prospect lookup is absent. |

### Results, reporting, and operations

| Capability | Status | Assessment |
|---|---|---|
| One validated result per finished attempt | Implemented | Conversation and carrier writers reconcile through a precedence rule. |
| CRM-ready flat export | Implemented | `campaign.py result --json` and `CallResult.to_dict()`. |
| CRM synchronization | Not implemented | No HubSpot, Salesforce, Pipedrive, webhook, or export push client. |
| Usage tracking | Implemented from Phase 11 onward | LLM, TTS, STT, and telephony measurements are stored when reported. |
| Cost estimates | Implemented with explicit unknowns | Prices are configured; unmeasured stages are named, not priced as zero. |
| Health checks | Implemented / current live verification | Current report: 7 ok, including database, KB, STT, LLM, TTS, and SignalWire. |
| Structured logging and credential scrubbing | Implemented | Loguru context, JSON option, and secret replacement. |
| Dashboard HTML and JSON | Implemented / deterministic tests | Read-only aggregates, campaigns, recent calls, attention metrics, usage, and costs. |
| Dashboard authentication | Not implemented | Loopback default only; public bind exposes contact data. |
| Dashboard filters/drill-down | Not implemented | No date range or campaign drill-down controls. |

## 6. Data Model and Persistence

The PostgreSQL application tables are:

- `prospects`
- `campaigns`
- `campaign_prospects`
- `call_attempts`
- `callbacks`
- `meetings`
- `call_results`

The knowledge subsystem uses separate `kb_` tables for documents and chunks. The stores use asyncpg directly rather than an ORM. Schema setup is idempotent and doubles as an additive migration mechanism, but there is no formal migration framework.

Important persistence properties:

- original and normalized phone values are retained;
- invalid numbers remain visible but are undialable;
- attempts survive campaign deletion through nullable foreign keys;
- DNC closes memberships and pending callbacks;
- pending callbacks are unique per prospect;
- call results are unique per attempt;
- conversation data is raw evidence, while `call_results` is a validated projection;
- usage is only available for calls captured after the Phase 11 columns and observer were introduced;
- older databases require rerunning `uv run campaign.py init` after later schema additions.

## 7. Security and Safety Audit

### Positive controls

- DNC is enforced in the person record, campaign membership, queue SQL, and immediately before placement.
- Ambiguous carrier placement is marked `UNRESOLVED` and is not retried automatically.
- Carrier placement itself is never retried because a timeout may mean the call was created.
- Status updates are monotonic, reducing duplicate/out-of-order event damage.
- Tool results distinguish confirmed success from failure and explicitly instruct the model not to claim failed side effects.
- Credentials are scrubbed from structured logs.
- The dashboard binds to loopback by default and warns when publicly bound.
- Health checks do not place calls or synthesize speech.

### Security gaps before internet exposure

- Dashboard has no authentication or authorization and reveals names and phone numbers.
- No documented TLS termination, reverse proxy, origin validation, or webhook signature validation exists for a public operational deployment.
- No user/role model exists for campaign operators.
- No retention, deletion, masking, or export access policy is implemented for transcripts and call data.
- No distributed lock/rate service exists for multiple dialer processes.
- Local calendar booking is vulnerable to parallel check-then-write races.

## 8. Verification Results

### Current checks run during this audit

All eleven deterministic scripts exited successfully:

| Test | Result |
|---|---|
| `test_actions.py` | PASS |
| `test_campaigns.py` | PASS |
| `test_conversation.py` | PASS |
| `test_dashboard.py` | PASS |
| `test_knowledge.py` | PASS |
| `test_performance.py` | PASS |
| `test_realtime.py` | PASS |
| `test_reliability.py` | PASS |
| `test_results.py` | PASS |
| `test_scheduling.py` | PASS |
| `test_telephony.py` | PASS |

The current health command returned:

```text
healthy: true
summary: 7 ok
database: 5 prospects, 0 live attempts
knowledge: 2 documents, 25 chunks
stt: deepgram_flux:flux-general-en - key accepted
llm: groq:qwen/qwen3.8-27b - key accepted
tts: deepgram - key accepted
telephony: signalwire: Main (active)
```

### Historical eval evidence

Recorded eval artifacts show successful runs for conversation, voice quality, silence, barge-in, callback, and implicit DNC scenarios under some configurations. They also show meaningful instability:

- the final recorded suite failed `barge_in` twice;
- both failures occurred after interruption, with an incomplete or incorrect answer;
- historical logs include Groq rate limits, judge timeouts, missing response text, and connection-refused startup failures;
- the sales suite depends on special timeout overrides and model behavior, so a single pass is not sufficient evidence of production stability.

The correct conclusion is **scenario capability exists, but audio/model stability is not yet proven**.

### Static quality checks

- VS Code diagnostics provider: no reported errors for the workspace path checked.
- Ruff: failed with five findings, including unsorted imports and use of Python 3.12 type-parameter syntax in `src/conversation/qualification.py` while `pyproject.toml` declares `requires-python = ">=3.11"`.
- Pyright: failed with 373 errors, primarily optional-value narrowing, test doubles, and scheduling/telephony stub typing. This is a quality and maintenance risk even though the runtime scripts pass.

## 9. Performance and Cost Findings

The handoff records a Phase 11 benchmark using 20,000 prospects and 60,000 attempts:

- dashboard read time improved from about 249 ms to 149 ms through concurrent reads;
- a five-second snapshot cache prevents simultaneous viewers from stampeding the database;
- shared pools reduced per-call database connection usage;
- six simultaneous reservations respected a configured concurrency limit of two;
- retrieval was measured at roughly 29 ms versus about 1,300 ms total turn latency;
- the dominant measured latency was turn-end detection, approximately 653 ms;
- the dominant cost/rate-limit issue is the prompt, with roughly 3,394 tokens per request and about 1,248 tokens from tool schemas;
- the Groq free tier is a major operational constraint: throttling can create 20-50 second turns, and the daily token budget can be consumed by evaluation runs.

The benchmark claims were not rerun during this audit. They should be treated as historical measurements, not current performance SLAs.

## 10. What Is Actually Usable Today

### Usable now for development and controlled demos

1. Start the bot with the configured services and use the browser WebRTC client.
2. Ingest local PDFs/text documents into PostgreSQL/pgvector and test retrieval.
3. Run deterministic conversation, action, campaign, reliability, result, scheduling, telephony, and dashboard checks.
4. Use the fake carrier to exercise the telephony media-stream handshake and audio return without a carrier account.
5. Import a prospect CSV, create a campaign, reserve and manually place calls through a configured carrier.
6. Inspect validated results, transcripts, summaries, action audits, usage, cost, and dashboard aggregates.

### Not usable as an unattended product

1. The system will not continuously call a campaign without a human or external process invoking `campaign.py call`.
2. A scheduled callback will not automatically trigger a call.
3. Completed results will not arrive in a CRM automatically.
4. Public dashboard use is unsafe without an external authentication layer.
5. Live booking, live transfer, and real carrier behavior remain unproven.
6. Real-world audio conditions are not covered by the current eval evidence.

## 11. Priority Risks and Recommended Order

### P0 - Before any real customer campaign

1. Resolve the Groq capacity problem by moving to a suitable paid/provider tier, changing the model/provider, or reducing per-turn tool schemas.
2. Run a controlled real-carrier call with one owned test number and verify placement, media, hang-up, status reconciliation, call result, and usage persistence.
3. Add authentication and authorization before exposing the dashboard beyond loopback.
4. Run a real microphone/headset test and repeat interruption tests under the chosen production provider.
5. Fix or explicitly narrow Python version support and establish a clean static-check baseline.

### P1 - Before unattended operation

1. Implement a scheduler/worker for campaign calls and callbacks.
2. Add carrier webhook ingestion with signature validation, retry handling, and observability.
3. Define distributed concurrency and pacing semantics for more than one worker.
4. Make local calendar booking atomic or use a provider with atomic booking guarantees.
5. Add answering-machine detection and explicit voicemail disposition.
6. Add durable job ownership/lease behavior for worker crashes and operational restart.

### P2 - Before CRM-oriented operation

1. Implement a CRM adapter or signed webhook export with retries and idempotency.
2. Define transcript/result retention, redaction, deletion, and access policy.
3. Add campaign filters, date filters, and operator drill-down to the dashboard.
4. Add inbound number lookup and persistent anonymous DNC handling.
5. Validate Cal.com and transfer against real accounts.

## 12. Documentation and Repository Hygiene

The main [README.md](README.md) is stale and still describes the project as Phase 7. It omits Phases 8-11, usage/cost tracking, recovery, dashboard behavior, and current limitations. [HANDOFF.md](HANDOFF.md) is the more accurate source but contains a few contradictory older pending-task statements, including text claiming concurrency and calling windows are absent even though they are implemented.

The audit should therefore be used as the current review baseline. The next documentation change should reconcile `README.md` and `HANDOFF.md` with the actual Phase 11 state rather than adding more feature claims.

## 13. Final Verdict

The project has a real, modular voice-agent core and a credible controlled outbound-sales workflow. The deterministic business logic, safety checks, persistence model, action honesty rules, recovery strategy, and local verification harness are substantially built.

The project is best classified as:

> **Phase 11 complete: controlled prototype / pre-production system.**

It is not yet a production campaign automation platform. The decisive missing layer is operational: scheduling, external event ingestion, authentication, CRM delivery, and live integration validation. The decisive current runtime risk is provider capacity and model-dependent interruption behavior.