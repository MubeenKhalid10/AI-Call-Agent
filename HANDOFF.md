# Handoff — Ai-Voice-Agent

**Written for whoever picks this up next, including a fresh Claude session with no memory of how it got here.** Read this before touching code. The section that will save you the most time is [Attempted Approaches That Failed](#attempted-approaches-that-failed) — several of the things below look obviously right and are not.

- **Status:** Phase 9 complete. Phase 10 not specified — see [Next recommended steps](#12-next-recommended-steps).
- **Last verified:** 2026-09-04 (Phase 9). All **nine** deterministic check scripts pass (the eight from before plus `test_reliability.py`), `uv run health.py` reports all seven components OK against the real vendors, and the ambiguous-placement → hold → recover loop was driven end to end against the **live SignalWire API** — see [Testing completed](#10-testing-completed). The Phase 7 Groq finding still stands and still gates live use: 8,000 input tokens a minute, 200,000 a day, and a turn with twelve tools costs ~3,100. Read [Known issues](#5-known-issues-and-limitations) before believing a timeout.
- **Two bugs were found by running Phase 9's own code against a real outage**, both now fixed and both worth knowing about: an LLM failure could never accumulate towards its threshold, and a call whose *greeting* failed sat silent until the idle timeout. See [Failed §25](#25-counting-an-llm-failure-and-then-immediately-forgetting-it-phase-9) and [§26](#26-a-threshold-that-cannot-be-reached-because-nothing-tries-again-phase-9).
- **Phase 8 fixed a Phase 6 bug worth knowing about:** the attempt status for a callback, a no, or a do-not-call was only written when the call ended *without* the agent's goodbye. It now reads the state path. See [Failed §23](#23-reading-the-attempt-status-from-the-final-state-phase-6-found-in-phase-8).
- **Stack:** Pipecat 1.8.1, Python 3.12, Deepgram Flux + Groq + Cartesia, SmallWebRTC + Twilio/SignalWire, PostgreSQL (pgvector for the knowledge base, plain tables for campaigns, callbacks, meetings and call results), phonenumbers, tzdata. Optional Cal.com for the calendar. No broker, no lock service, no scheduler daemon — Phase 9's correctness is a transaction and two unique indexes.

> **This file was stale between Phase 2 and Phase 4.** Phase 3 shipped without it
> being updated, so the Phase 3 notes below were reconstructed from the code on
> 2026-09-03 by whoever did Phase 4, and carry none of the "we tried X and it
> failed" detail that makes the rest of this document worth reading. Treat the
> Phase 3 sections as a map, not as testimony. If you are picking this up: update
> this file as part of your phase, not after it.

---

## 1. Project overview and objectives

`D:\Ai-Voice-Agent` is an **AI cold-calling sales agent**. The finished product phones prospects, runs discovery, qualifies them, handles objections, and books meetings with a sales rep. Every call must emit a transcript, summary, pain points, objections, qualification status, meeting status and next action, in a CRM-ready shape.

As of Phase 7 the agent does all of that, including booking the meeting: it phones a prospect it can name, runs discovery, qualifies, handles objections, checks a calendar and books a slot the prospect chose, schedules a callback at an exact time, honours a do-not-call, can hand a live call to a person, and writes a structured record — including every action it took and whether it succeeded — onto the call attempt. As of Phase 8 every finished attempt — answered or not — also has one validated, CRM-ready `CallResult` row: a disposition, the qualification fields, the meeting and callback status, the transcript kept verbatim, and a six-part summary composed from the record rather than by a model. What is missing is a scheduler that places the queued calls unattended, and anything that pushes the result to a CRM.

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
| 9 | Safety and reliability: duplicate-call protection, idempotency, recovery after a restart, bounded retries, campaign guardrails, health checks, structured logs, failure-injection tests | **Done (2026-09-04)** |
| 10 | Not yet specified by the user — see [Next recommended steps](#12-next-recommended-steps) | Not started |

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

**Test it:**

```bash
cd server
uv run health.py                           # Phase 9 — is every dependency actually up?
uv run python tests/test_conversation.py   # 291 checks — the sales layer. Run this first
uv run python tests/test_results.py        # 258 checks — Phase 8: the call result, no database
uv run python tests/test_reliability.py    # 150+ checks — Phase 9: injected failures
uv run python tests/test_knowledge.py
uv run python tests/test_telephony.py
uv run python tests/test_realtime.py
uv run python tests/test_campaigns.py

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

### Phase 9 (this session)

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

Nothing is half-finished. Phase 9 is complete as specified. What follows is *not started*, and most of it is deliberately deferred:

- **Phase 10 scope** — the user has not specified it yet. Do not guess and start building.
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
| Whether the model calls a tool is model-dependent | A missed `record_discovery` leaves a pain point out of the CRM record — quietly | The stage block names the stage's tool on every turn, which is what made it reliable on Qwen. Re-verify after changing `GROQ_MODEL` |
| A do-not-call fires a database write inside the turn | A slow database adds that latency to the reply | Deliberate: the request must be recorded before the call can drop. The sink swallows its own errors, so a *failing* database costs nothing |
| An anonymous call's do-not-call is honoured but not stored | A browser or inbound caller who asks not to be called has no row marked | Logged as a warning naming the gap. Inbound number lookup is the fix and nobody has asked for it |
| `conversation_data` is the raw record; `call_results` is the reading of it | Two copies of the transcript per attempt (raw and projected) | Phase 8, deliberate: the raw record is what a rebuild reads. `campaign.py result` shows the projection |
| The user side of the transcript comes from the director, the agent side from the aggregator | A caller turn that never reached an inference (spoken after `end_call`, say) is not in the transcript | Phase 8. The director's path is the one the detectors and the model saw, which is why it was chosen; the aggregator's own `on_user_turn_stopped` would be the other source |
| `questions` is a heuristic | A question phrased as a statement is missed; "do it by hand" opens with an interrogative and may be kept | Phase 8. Verbatim and beside the transcript, so a reader can see. `extract_questions` is one function to tune |
| A result's duration is the bot's view on a phone call | A second or two shorter than the carrier bills; `call_attempts.duration_seconds` takes the carrier's on reconciliation | Phase 8. `source` on the row says who wrote it; both numbers are kept, on their own rows |
| Results are not backfilled automatically | An attempt that finished before `campaign.py init` added the table has no row | `uv run campaign.py rebuild-results`, once. Done on this machine |
| An ambiguous placement blocks its prospect until recovery runs | One prospect uncalled, for as long as nobody runs `campaign.py recover` | Phase 9, deliberate and the safe direction. `campaign.py call` runs recovery first; `health.py` flags live attempts as degraded |
| Recovery cannot resolve an attempt if the carrier cannot list calls | The attempt is closed as failed and the reason says to check the carrier's log by hand | Both supported carriers *can* list calls, and it was exercised live against SignalWire. A future carrier without the endpoint gets the honest dead end rather than a guess |
| Concurrency and pacing are in-process | Two dialers would each allow their own limit | Cannot cause a duplicate call — that is protected in the database. It is a rate limit, not a correctness one |
| No webhook endpoint exists | Carrier status is still polled, so an outcome is up to `--poll` seconds late | `store.apply_call_event` is the idempotent entry point a webhook would use; nothing serves one yet |
| The calling window uses the prospect's timezone only if their record supplies one | A list imported without a `timezone` column is called in `CALLING_TIMEZONE` | Deliberate: a country code does not determine a timezone, and guessing puts the call at the wrong hour invisibly |
| A supervised ending writes a note, not a distinct status | A call cut off by the duration ceiling reads as `COMPLETED` with a note | The note is on the call result and the reason is in the log. A distinct disposition would need a Phase 8 vocabulary change |
| Retrieval gating is a heuristic | A product question phrased with no interrogative and no commercial noun is not searched | Written to skip rather than allow, so the failure needs all three signals absent. `KB_RETRIEVAL_MODE=always` restores Phase 3 |
| The conversation opens a second database pool per session | Two more connections per call, closed at session end | One bot per call, so it is bounded. A long-lived multi-session host would want a shared pool |
| A prospect's outcome is reconciled by polling, not pushed | An attempt can sit in `QUEUED` until something calls `dialer.refresh` | Carrier webhooks are the upgrade; `call_attempts.telephony_call_id` is unique so a callback can find the row |
| Campaign calls are placed one at a time, by hand | No throughput, no calling-hours window | `campaign.py call --count N` loops; a real scheduler is a later phase |
| `DEFAULT_PHONE_REGION` is unset by default | Local-format numbers in a CSV are all rejected | Deliberate — guessing a country dials a stranger. Set it to the country your lists are written in |
| No real phone call has ever been placed | The one thing that cannot be proved here | No carrier account on this machine. Placement is verified against a stub, audio against `tests/fake_carrier.py` |
| Twilio has no trial in Pakistan | The reference carrier is unusable for the person building this | Hence SignalWire, whose trial needs no card. Both are supported and the bot is identical either way |
| SignalWire's REST paths are assumed to accept the `.json` suffix | A 404 on the first real call | Its Compatibility API mirrors Twilio's URL scheme, but this has not been exercised against a live account. If placement 404s, drop `.json` in `TwilioProvider.place_call`/`fetch_call` |
| A bot with no carrier credentials answers a call but cannot hang it up over REST | The agent's own goodbye does not end the call; it ends when the websocket closes | Deliberate — it is what makes `fake_carrier.py` and a first run work. Configure a carrier for the full path; the warning at call setup says so |
| Answering machines are treated as people | The agent talks to voicemail | No AMD; the call summary flags "the other end never spoke" after the fact |
| Call status is polled, not pushed | Up to `--poll` seconds late, a few extra API calls per call | Webhooks would need a second public endpoint; see the docstring on `call.watch` |
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
| Cal.com provider never run against a live account | First real booking may 4xx on a response shape or field name | Two places marked in `scheduling/calcom.py`; the stub tests pin the request shape |
| Transfer never exercised on a live call | Unknown whether SignalWire moves the call cleanly and closes the stream | Verified against a stub only. Try it on your own number first |
| A callback does not lift the attempt cap | A prospect at the cap who asks for a callback is queued but never handed out | Shows in `campaign.py callbacks --due`; policy decision deferred |
| Local calendar check-then-write is not atomic | Parallel bots could double-book a slot | Irrelevant with one bot per process; an exclusion constraint is the fix |
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
| `knowledge_store.py` / `embeddings.py` / `documents.py` | The knowledge base underneath it |
| `telephony/` | Placing calls, call outcomes, and the bot's view of the call it is on |

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

### The deterministic checks

Nine scripts that need no vendors and no phone, and finish in seconds:

```bash
uv run python tests/test_conversation.py  # the sales layer — states, qualification, signals, tools, transcript, all 14 scenarios
uv run python tests/test_results.py       # Phase 8 — the call result: every required case, precedence, validation, summary, export
uv run python tests/test_reliability.py   # Phase 9 — injected failures: duplicate calls, restarts, retries, guardrails, the supervisor
uv run python tests/test_actions.py       # Phase 7 — every tool through the real boundary, stubbed world
uv run python tests/test_scheduling.py    # Phase 7 — local calendar arithmetic; Cal.com against a stub session
uv run python tests/test_knowledge.py     # retrieval, chunking, the gate, what the LLM is handed
uv run python tests/test_telephony.py     # placement, transfer, outcomes, errors, handshake, config
uv run python tests/test_realtime.py      # echo suppression, the peer watchdog
uv run python tests/test_campaigns.py     # phones, CSV, the queue, DNC, the dialer, callbacks, meetings, results, duplicate protection (SQL half needs PostgreSQL)

uv run health.py                          # Phase 9 — every dependency, no call placed
uv run campaign.py recover                # Phase 9 — resolve attempts left live by a crash
```

All nine pass as of 2026-09-04.

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

**The user has not specified Phase 10. Ask before building.**

**Do these first, whatever Phase 10 turns out to be.** None is a phase; each is
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

Then, what Phase 9 leaves for Phase 10, in the order I would rank them:

1. **A scheduler — and Phase 9 built most of what it needs.** A scheduled
   callback reopens its membership with `next_attempt_at`; nothing dials it
   unless a person runs `campaign.py call` at the right moment. The pieces that
   were missing are now there: the calling window, the concurrency limit,
   pacing, the recovery pass to run at startup, and `Decision.retry_after_secs`
   so a loop can sleep exactly as long as it should rather than spinning. What
   remains is the loop itself, plus two decisions: whether a prospect-requested
   callback overrides `CAMPAIGN_MAX_ATTEMPTS`, and whether the concurrency
   limit needs to become a database counter once more than one worker exists.
   Carrier status webhooks stop being optional here —
   `store.apply_call_event` is already the idempotent entry point one would
   use.
2. **Pushing the result to a CRM.** The row, the export and the field mapping
   exist (`results.py`); what is missing is a client, a sync state
   (`synced_at` / external id on `call_results`, or a separate table), and a
   decision about which object each result becomes. HubSpot's call engagement
   is the closest fit to the row as it stands. Push on write from the sink, or
   a poller over unsynced rows — the second survives a CRM outage.
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

Whatever it is, **run `uv run health.py`, then the nine check scripts, then the
sales suite** to confirm the baseline, and add scenarios alongside the feature
rather than after it. If the sales suite is run, `campaign.py results`
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
- **Deriving a prospect's timezone from their phone number (Phase 9).** Rejected for the same reason Phase 5 refused to guess a country for an un-normalisable number: it is right most of the time and invisibly wrong for every country with more than one zone, and the failure is a call at the wrong hour.

---

## Quick reference for a fresh session

```
D:\Ai-Voice-Agent
├── AGENTS.md / CLAUDE.md   # Pipecat app-building guidance — read it, it is not generic
├── README.md               # User-facing: stack, setup, how it works, measured latency
├── HANDOFF.md              # This file
└── server/
    ├── bot.py              # Wiring only
    ├── call.py             # Place one outbound call and watch it
    ├── campaign.py         # Prospects, campaigns, the call queue
    ├── ingest.py           # Load documents into the knowledge base
    ├── src/                # config, services, turns, metrics, resilience, diagnostics,
    │   │                   #   prompts, retrieval, knowledge_store, embeddings, documents
    │   ├── conversation/   # states, qualification, brief, playbook, signals, results,
    │   │                   #   actions (Protocol), timeparse, toolkit, tools, conversation,
    │   │                   #   director, sources, sink, transcript — imports no database,
    │   │                   #   no carrier, no calendar, nothing from bot.py
    │   ├── actions/        # service (the ActionBackend), __init__ (open_actions)
    │   ├── scheduling/     # base, hours, local, calcom, __init__ (make_calendar)
    │   ├── campaigns/      # models, phone, csv_import, store, service, dialer, briefing,
    │   │                   #   results (Phase 8), recovery (Phase 9)
    │   ├── reliability/    # Phase 9: retry, idempotency, guardrails, supervisor,
    │   │                   #   health, observability — imports no campaigns
    │   └── telephony/      # base, twilio, signalwire, transport, session, __init__
    ├── health.py           # Every dependency, probed cheaply (Phase 9)
    ├── evals/              # Audio suite (7 scenarios) + sales/ (15, text mode), Groq judge
    ├── tests/              # test_{conversation,results,reliability,actions,scheduling,
    │                       #   knowledge,telephony,realtime,campaigns}.py
    │                       # fake_carrier.py / fake_browser.py — simulate a caller
    │                       #   against a running bot; no account needed
    │                       #   (fake_carrier --prospect/--campaign/--attempt lands a result)
    ├── .env                # Keys (git-ignored). SignalWire credentials and a tunnel URL are
    │                       #   set as of 2026-09-04; no TELEPHONY_TRANSFER_NUMBER, no Cal.com
    └── pyproject.toml      # pipecat-ai[anthropic,cartesia,deepgram,evals,runner,silero,webrtc,websocket], tzdata
```

**Before changing anything:** run `uv run health.py` (seconds, no call placed) and the nine check scripts (seconds, no keys), and note the baseline. Then the two eval suites if you are touching the pipeline or a prompt — and read the Groq note in Known issues before believing a timeout. **After changing anything:** run them again, with `-r 2` on anything you suspect.

**Changing a tool is changing the prompt, and the prompt has a budget.** Every tool's docstring is sent on every turn. Measure with a direct request (`usage.prompt_tokens`) before and after — the scratch script that did it is described in Failed §20 — and keep the *how* in `playbook.stage_block`, not in the docstring.

**Changing a prompt is changing behaviour.** `tests/test_conversation.py` asserts that the honesty rules are present in the system instruction by their actual wording, so deleting one fails a check in a second instead of surfacing on a live call. If you reword a rule, update the check in the same edit — and read what it is checking before you decide the check is the thing that is wrong.

**Verify Pipecat APIs against `server/.venv/Lib/site-packages/pipecat`,** not from memory. Pipecat moves fast and 1.8.1 carries deprecated aliases for several things that older training data will suggest.

**Respect the phase boundary.** Build the phase that was asked for, and no more.
