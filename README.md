# Ai-Voice-Agent

An AI cold-calling sales agent you can talk to in your browser **or on the
phone**. Built on [Pipecat](https://docs.pipecat.ai/) as a cascade pipeline:

```
caller -> STT -> knowledge base -> call guidance -> LLM -> TTS -> caller
```

Where "caller" is a browser over WebRTC, or a real phone over a carrier's media
stream. Nothing between the two ends knows which.

This is **Phase 7**. What each phase added:

| Phase | What it added |
|---|---|
| 1 | The cascade pipeline and clean provider seams |
| 2 | Realtime: streaming, turn-taking from speech, barge-in, latency measurement, resilience |
| 3 | A knowledge base: answers grounded in your documents rather than in what the model remembers |
| 4 | Telephony: outbound phone calls, over the same pipeline |
| 5 | Prospects, campaigns and the call queue: who to call, and what happened |
| 6 | The sales conversation: who it is calling, what it asks, what it learned |
| 7 | **Actions: booking a meeting, scheduling a callback, marking do-not-call, transferring to a person, looking things up — each a tool the model calls, each validated by a backend that answers success or failure, and none of it claimed until the backend confirms it** |

No CRM sync or scheduler yet. Those build on top of this.

## Stack

| Stage | Service | Why |
|---|---|---|
| Transport | SmallWebRTC (local) | Real WebRTC audio, no cloud media server or account needed |
| Phone transport | Twilio or SignalWire Media Streams | Bidirectional audio over a websocket; Pipecat serializes it, `src/telephony/` places the call. SignalWire is there because Twilio has no trial in every country |
| Knowledge base | PostgreSQL + pgvector, local `bge-small` embeddings | Grounded answers; no per-query embedding cost and no data leaving the machine |
| STT + turn detection | Deepgram Flux | Transcribes *and* decides end-of-turn on the same websocket, from the words and the prosody rather than from silence |
| LLM | Groq | Fastest time-to-first-token of any hosted provider, which matters more than raw capability mid-conversation. Free tier, no card |
| TTS | Cartesia Sonic | Streaming websocket synthesis; best latency-per-dollar for conversational speech |
| VAD | Silero | Latency measurement anchors, and turn detection on the non-Flux fallback path |
| Fallback turn detection | Smart Turn v3 (local ONNX) | Used when `STT_PROVIDER=deepgram` |

## Setup

**Prerequisites:** [uv](https://docs.astral.sh/uv/). Python 3.12 is fetched
automatically — the project pins it in `.python-version` because several native
dependencies do not yet publish wheels for 3.14.

```bash
cd server
uv sync
cp .env.example .env    # then add your keys
uv run ingest.py init             # create the knowledge base schema, once
uv run ingest.py add evals/kb     # load the sample documents
uv run campaign.py init           # create the prospect/campaign tables, once
uv run bot.py
```

Open **http://localhost:7860/client**, allow microphone access, and click
connect. The agent speaks first.

To run without PostgreSQL, set `KB_ENABLED=false` — the agent then answers from
the model's own knowledge, which is the Phase 2 behaviour and the A/B baseline
for what grounding is worth.

### API keys

Three are required. Put them in `server/.env` (git-ignored):

| Variable | Where to get it |
|---|---|
| `DEEPGRAM_API_KEY` | https://console.deepgram.com — $200 free credit |
| `GROQ_API_KEY` | https://console.groq.com/keys — free, no card required |
| `CARTESIA_API_KEY` | https://play.cartesia.ai/keys — free tier available |

If any are missing, the bot exits immediately and names every one it needs
rather than failing mid-call with a vendor authentication error. The same is true
of a malformed tuning value: `VAD_STOP_SECS=fast` is a startup error, not a
silent fallback.

All three have free tiers, so this costs nothing to run.

> Deepgram Flux is on the `/v2/listen` endpoint. If your account does not have
> access, set `STT_PROVIDER=deepgram` — everything still works, with local turn
> detection instead of server-side.

## How it works

### Streaming, all the way through

Nothing waits for a complete result before starting the next stage:

- **STT** streams audio up a websocket and transcripts back down, so the
  transcript is essentially ready the moment you stop talking.
- **The LLM** streams tokens, so TTS starts on the first clause instead of the
  finished reply.
- **TTS** streams audio down a websocket, so the first syllable plays while the
  rest is still being generated.

One non-streaming stage would serialise the chain and cost about a second.

### Turn-taking

Knowing when you have finished is the hard part, and two mistakes pull in
opposite directions: waiting too long after you finish (dead air), and cutting in
during a mid-sentence pause (rude, and it truncates you). A silence timer cannot
avoid both, because "paused to think" and "finished" look identical to it.

Deepgram Flux decides from the words and the acoustics together, and returns that
decision on the same websocket as the transcript. Pipecat wires this up on its
own: the service recommends its own turn strategies and the context aggregator
adopts them. See `src/turns.py`, which deliberately passes no strategies on that
path — overriding them would silently switch Flux's turn detection off and leave
only the transcripts.

With `STT_PROVIDER=deepgram` the fallback is Silero VAD to start the turn and the
local Smart Turn v3 model to end it.

### Interruption

Talk over the agent and it stops immediately: Flux reports that a caller turn has
started, the in-flight LLM and TTS work is discarded, and the agent answers what
you just said rather than finishing its old sentence. The `barge_in` eval asserts
exactly that.

The assistant context aggregator sits *after* the output transport, so an
interrupted reply is stored truncated at the point you cut it off — what you
actually heard, and what the agent must not later assume it finished saying.

That truncated fragment then has to be **marked**, or it quietly ruins the rest
of the call. Measured here on Groq/Qwen: with its own half-sentence sitting bare
in the context, the model answered the next question with two tokens — "The" —
and stopped; that answer joined the context and the turn after came back three
tokens long. A model can only read a bare fragment as a turn someone chose to end
there, and it copies the pattern. Saying so in the system prompt did not fix it;
appending a cut-off marker to the message did. See `mark_interrupted_reply` in
`src/turns.py`, and `evals/barge_in.yaml`, which is the test that caught it.

### Silence and dropped connections

Two situations look identical from inside the pipeline (no audio arriving) and
want opposite responses:

- **You went quiet.** The agent checks in, twice by default, then says goodbye
  and ends the call. It composes those lines itself, so they sound like the rest
  of the conversation.
- **Your connection dropped.** Talking is useless, but hanging up instantly is
  wrong too — WebRTC drops and recovers on flaky wifi. The session is held open
  for `DISCONNECT_GRACE_SECS` (5s by default); come back inside that window and
  the whole conversation is still there, and the agent offers to repeat what you
  missed.

A backstop `SESSION_IDLE_TIMEOUT_SECS` ends a session with no speech in either
direction, so a forgotten tab does not leak a live pipeline and a live STT
websocket.

## When it misbehaves

Two failures show up the first time you test on a laptop, and neither is
obvious from the logs.

### It talks to itself

You hear the agent answer a question nobody asked, and the transcript has the
agent's own words attributed to you:

```
assistant: Hi there! How can I help you today?
user:      Hi there. How can I help
assistant: Oh, I think we might have crossed wires there!
```

That is **acoustic echo**: the agent's voice leaves your speakers, re-enters the
microphone, and is transcribed as you. Every reply then feeds the next one and
the conversation runs away without you.

**Put headphones on.** That removes the acoustic path entirely and is the only
fix that keeps barge-in working. Browsers do run echo cancellation — Pipecat's
client asks for it — but on a laptop at speaker volume it is often not enough.

If you cannot use headphones:

```bash
ECHO_SUPPRESSION=always      # ignore the caller while the agent is speaking
ECHO_SUPPRESSION=greeting    # only during the opening turn
```

`always` fixes the echo completely and **disables barge-in** — you have to wait
for the agent to finish before it will hear you. That trade is why the default
is `off`. The startup log says which mode is active.

### It connects once, then never again until I refresh

The page connects, and nothing happens — no greeting, no response — until you
reload it or restart the bot.

WebRTC has no way to report that the far end went away. A caller who presses
disconnect closes the connection and says so; a caller whose laptop sleeps or
whose wifi drops says nothing at all. The session then runs on with nobody in
it, and the dev runner keeps the dead connection registered under its id — so
when you press connect again, the browser sends that same id, the runner
renegotiates the corpse instead of starting a bot, and you get silence.
Reloading discards the id, which is why a refresh appears to fix it.

`PEER_TIMEOUT_SECS` (5s by default) is the watchdog for this: when the
connection has looked dead that long, the bot treats it as a disconnect,
finishes the session, and releases the connection. If you see this symptom,
check it is not set to 0.

You can reproduce the whole thing headlessly:

```bash
uv run bot.py                                                    # terminal 1
uv run python tests/fake_browser.py --reconnect --abandon        # terminal 2
```

## Phoning someone

The same agent, on a real phone. Nothing in the pipeline changes: a phone call is
another transport, and the STT, LLM, TTS, knowledge base, turn-taking, barge-in
and metrics are the ones described above.

```
call.py  --REST-->  the carrier  --dials-->  the person
                         |
                         '--websocket-->  bot.py's /ws     (audio, both ways)
```

`call.py` asks the carrier to dial a number and, when it is answered, to stream
the call's audio to this bot. The carrier is the only party talking to both ends.

### Testing it with no account at all

Before signing up for anything, you can exercise the entire telephony path
locally. `tests/fake_carrier.py` connects to the bot's `/ws` and speaks a
carrier's media-stream protocol at it — the handshake, the custom parameters,
8kHz μ-law frames in real time — so the bot cannot tell the difference:

```bash
cd server
uv run bot.py                                # terminal 1
uv run python tests/fake_carrier.py          # terminal 2
```

```
Connecting to ws://localhost:7860/ws as a outbound call
  [  2.4s] the bot started speaking

What the carrier saw:
  events from the bot   {'media': 26}
  audio received        3.6s, peak amplitude 31100

PASS — the bot answered and spoke over the media stream.
```

That answers the questions that otherwise need a phone number: does the
handshake parse, does the bot know who it is talking to, does it **speak
first**, and does audio come back encoded correctly. Add `--say caller.wav` to
speak to it and `--record heard.wav` to listen to what it said.

This works on a bot with no carrier configured at all. The one thing it gives up
is hanging the call up over the carrier's API — with no credentials there is
nobody to ask — so the call ends when the websocket closes rather than when the
agent finishes saying goodbye. The bot warns about that at call setup.

It cannot tell you whether your carrier accepts the markup, whether a real
number rings, or how the agent copes with a real person on a real 8kHz line.
For that you need a real call.

### Choosing a carrier

Two are supported, and the bot is identical either way — only four lines of
`.env` change.

| | Twilio | SignalWire |
|---|---|---|
| API | The reference | A deliberate reimplementation of Twilio's: same markup, same statuses, same media-stream protocol |
| Free trial | Not offered in every country — **Pakistan among them** | No credit card; includes a number; you may call one number you have verified |
| Docs | Best in the industry | Good, and Twilio's mostly apply |

**If Twilio will not give you a trial account, use SignalWire.** Its
Compatibility API is close enough that `SignalWireProvider` is a forty-line
subclass of the Twilio one, overriding a hostname and three strings.

### Setting it up

```bash
# terminal 1 — the agent
cd server && uv run bot.py

# terminal 2 — a public address for it
ngrok http 7860                  # copy the https URL it prints
```

```bash
# server/.env — Twilio
TELEPHONY_PROVIDER=twilio
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...

# server/.env — or SignalWire
TELEPHONY_PROVIDER=signalwire
SIGNALWIRE_PROJECT_ID=...                 # "API" in the sidebar
SIGNALWIRE_API_TOKEN=...
SIGNALWIRE_SPACE_URL=example.signalwire.com

# either way
TELEPHONY_FROM_NUMBER=+15550001111        # a number your account owns
TELEPHONY_PUBLIC_URL=https://abc123.ngrok.app
```

```bash
# terminal 3 — call someone
cd server && uv run call.py +923001234567
```

On a trial account you can usually only call a number you have verified in the
console, which for testing means your own phone.

Restarting `bot.py` after editing `.env` prints the exact `wss://` address
carriers will be told to stream to — check it matches your tunnel, because a
mismatch shows up as a call that connects and then goes quiet.

`uv run call.py +92... --dry-run` prints the request and dials nothing, which is
the fastest way to confirm the configuration without spending a call.

### What it reports

Every call logs its own line, and its exit code says which of three things
happened, so a script can tell them apart:

```
CALL | dialling +923001234567 from +15550001111 via twilio (account …abcd)
CALL | placed | twilio call=CA123… | status=queued | to=+923001234567
CALL | ringing after 1.2s — the phone is ringing
CALL | answered after 6.4s — audio is flowing to the agent
CALL | completed after 48.1s — the call was answered and has ended
```

| Exit code | Meaning |
|---|---|
| 0 | Answered and ended normally |
| 1 | The call could not be placed — configuration, credentials, a number the carrier refused |
| 2 | Placed, but nobody was reached: busy, no answer, or the call failed |

`busy`, `no-answer` and `failed` are kept as separate outcomes rather than
flattened into one failure, because they mean different things to whatever comes
next: the first two say the number is fine and the person was not, the third says
the call could not be made at all.

The bot logs the other side of the same call, keyed by the same call id:

```
CALL | outbound twilio call=CA123… from=+15550001111 to=+923001234567
CALL | audio connected | …
CALL SUMMARY | … | duration 48.1s | 6 caller turn(s), 7 agent turn(s)
```

A call where nobody ever spoke is called out explicitly in that summary —
voicemail and a wrong number both look like that, and neither shows up in the
latency numbers.

### Two things that are different on a phone

**A dropped call does not come back.** WebRTC blips and recovers into the same
session, so a browser disconnect is held open for `DISCONNECT_GRACE_SECS`. A
dropped phone call is over — the person redials and gets a new call — so
`TELEPHONY_DISCONNECT_GRACE_SECS` is 0 and the session ends immediately rather
than keeping a dead pipeline and a live STT websocket alive.

**A carrier does not announce itself.** A browser client tells the bot when it is
ready to hear audio; a carrier just starts streaming. The greeting therefore
hangs off the media stream opening on a phone call, and off the client's ready
signal in a browser. Getting this wrong is silent in the worst way: the bot
answers the phone and never says anything.

### Changing carrier

Two halves, and they need very different amounts of code:

- **Receiving the audio** works for Twilio, Telnyx, Plivo and Exotel. Pipecat
  detects the carrier from the media stream's first message and ships a
  serializer for each, so `bot.py` registers all four and contains no carrier
  code. The provider only *chooses and configures* one — which matters, because
  the serializer is also what hangs the call up, and it has to do that at the
  right company under the right credentials.
- **Placing the call** is a REST API that differs per vendor, and that is what
  `src/telephony/` really abstracts. Adding a carrier is one module implementing
  `TelephonyProvider`, plus two table entries in `config.py` — see the docstring
  in `src/telephony/__init__.py`. `bot.py` and the pipeline do not change.

`src/telephony/signalwire.py` is what that looks like when the new carrier is
Twilio-compatible: one small file that overrides an API host and three strings.

## Prospects and campaigns

Phase 4 can call *a number*. Phase 5 is the layer that decides *which* number,
keeps the history, and refuses to call people who asked not to be called.

```bash
cd server
uv run campaign.py init                              # create the tables, once
uv run campaign.py import leads.csv --dry-run        # check the column mapping
uv run campaign.py import leads.csv
uv run campaign.py create "Q1 Outreach"
uv run campaign.py add "Q1 Outreach" --all
uv run campaign.py start "Q1 Outreach"
uv run campaign.py next "Q1 Outreach"                # who is up, dialling nothing
uv run campaign.py call "Q1 Outreach"                # place one real call
uv run campaign.py status "Q1 Outreach"
uv run campaign.py dnc 42                            # never call prospect 42 again
```

### Four entities, and why they are separate

| | Holds | Reused across campaigns |
|---|---|---|
| **Prospect** | A person and their number. No campaign state at all | Yes — one row, many campaigns |
| **Campaign** | A batch of work, and its lifecycle | — |
| **CampaignProspect** | One person's state *in one campaign*: attempts, retry time | — |
| **CallAttempt** | One dial, and the carrier's call id | — |

The split that matters is the third row. Attempt counts and retry times live on
the *membership*, not the prospect, so the same person can be in two campaigns
with independent progress in each and still be one record. And a prospect is not
a call attempt: they have many, which is what makes the call history a history.

### Importing a CSV

Prospect lists come out of CRMs and spreadsheets, and none of them agree on
column names. Headers are matched after stripping case, spaces, underscores and
punctuation, so `First Name`, `first_name`, `FirstName` and `FIRST-NAME` are all
the same column. Columns that match nothing are **kept** as `custom_data` rather
than dropped, so an unfamiliar file needs no schema change.

`--dry-run` shows the mapping and every row it would reject, and writes nothing:

```
Column mapping for leads.csv:
                'Organization'  ->  company
               'Email Address'  ->  email
                  'First Name'  ->  first_name
                'Phone Number'  ->  phone
                  'Lead Score'  ->  custom_data['Lead Score']

3 row(s) will not be imported:
  line 5: Duplicate Person — same phone as line 2
  line 6: Broken Number — phone '12345': not a number that exists in its country's numbering plan
  line 7: NoLast — missing last name
```

One bad row never stops the others: 900 good rows load while the 3 broken ones
are described precisely enough to fix.

### Phone numbers, and refusing to guess

`DEFAULT_PHONE_REGION=PK` makes all of these one number and one person, which is
what stops somebody being imported three times and called three times:

```
+92 322 1234567    +923221234567    0092 322 1234567    0322 1234567
```

With no default region the first three still work — they carry their country —
and `0322 1234567` is **refused**, because a leading-zero local number is valid
in many countries. That refusal is deliberate. A rejected row costs one uncalled
prospect and a line in a report; a wrongly normalised one calls a stranger.

A prospect whose number will not normalise is still stored, with the original
number kept for correction, but with nothing to dial — so they can never be
called by accident.

### Do not call

A prospect marked `DO_NOT_CALL` is never dialled again, on any campaign. It is
enforced three times over, and the redundancy is the point:

1. Marking them closes their open memberships, so the queue stops offering them.
2. The queue's SQL excludes them, so a membership added afterwards is still
   never handed out.
3. The check runs once more against a freshly read row immediately before the
   call is placed, so somebody marked *while* their call was being set up is
   still not dialled.

None of it is in the UI. There is no UI.

### The queue

`campaign.py next` shows what the queue would do without touching anything:

```
'Q1 Outreach' (ACTIVE), 3 membership(s):
     Ayesha Khan     +923221234567    EXHAUSTED    this campaign is EXHAUSTED for them
     Bilal Ahmed     +923337654321    SKIPPED      the prospect is marked DO_NOT_CALL
  -> Sara Ali        +923001112223    PENDING      ready (attempt 1)
```

A prospect is handed out only when the campaign is `ACTIVE`, the membership is
pending, they are not DNC, they have a dialable number, their retry time has
come, they are under the attempt limit, and they are not already on a call —
including a call from a *different* campaign, since a person can only be on one
phone at a time. Choosing and reserving happen in one transaction with
`FOR UPDATE SKIP LOCKED`, so a second caller can be added later without two of
them dialling the same person.

### Before calling real people

This is a working dialler, not a compliant one. Cold-calling is regulated where
your prospects are, not where your server is: the US has TCPA and the FCC's
AI-voice rules, the UK has Ofcom and PECR, and Pakistan has PTA licensing for
terminating calls. Neither carrier generally sells +92 numbers, so Pakistani
prospects will see a foreign caller ID — a business problem worth knowing about
before the first campaign, since answer rates for unknown international numbers
are poor.

Deciding this is out of scope for this phase and squarely in scope before the
campaign phase, because it picks the carrier and the compliance regime both.

## The sales conversation

The agent runs a cold call rather than answering questions. It opens, asks
before it pitches, handles objections, asks for a next step, and records what it
learned — and it does all of that without ever claiming something nobody told it.

Set who it is and what it sells in `server/.env`:

```bash
SALES_AGENT_NAME=Alex
SALES_COMPANY_NAME=Meridian Fleet Systems
SALES_OFFER=fleet tracking that cuts fuel spend and shows where every vehicle is
SALES_VALUE_POINTS=Customers typically cut fuel spend by around a tenth.|It installs in under an hour per vehicle.
SALES_MEETING_ASK=a fifteen minute call with a specialist later this week
```

**Every one of those is optional, and leaving one out does not produce a
placeholder — it produces a restriction.** With no `SALES_COMPANY_NAME` the
agent introduces itself by name only and is forbidden from inventing a company.
With no `SALES_VALUE_POINTS` it makes no claims about the product at all and
offers to have a specialist confirm anything factual. The startup log names
every gap. A campaign's `configuration` JSON overlays these per field, so one
bot serves campaigns selling different things.

`SALES_MODE=false` removes the whole layer and gives you the Phase 3
knowledge-base assistant, which is the A/B baseline.

### Ten states, and two of them are promises

```
GREETING -> DISCOVERY -> QUALIFICATION -> VALUE_PROPOSITION -> MEETING_REQUEST
                 \             |                 |                    /
                  '------ OBJECTION_HANDLING ----'-------------------'
                                    |
             CALLBACK  ·  NOT_INTERESTED  ·  DO_NOT_CALL  ->  ENDING
```

The state is kept separately from the prospect data, the campaign data, the
system prompt and the knowledge base — it is one small object saying where this
call has got to. Two rows of its transition table are worth stating on their own,
because they are commitments to the person on the phone rather than preferences
about how a call should flow:

- **`NOT_INTERESTED` has no route back to selling.** Not to `VALUE_PROPOSITION`,
  not to `MEETING_REQUEST`. "Never push after a clear rejection" is a table, not
  a sentence in a prompt, so it holds whatever the model decides to do.
- **`DO_NOT_CALL` is forced from anywhere and is absorbing.** It ignores the
  table entirely, because a request not to be called again has to be honoured
  from any point in the call.

### Two mechanisms, and both are needed

The model drives the conversation by calling tools — `record_discovery`,
`record_objection`, `request_meeting`, `mark_do_not_call`, `end_call` and seven
others. That is what reads "honestly, we're happy where we are and I'd rather
you didn't ring again" correctly, and no phrase list ever will.

Underneath sits a deterministic floor: a set of patterns that force the state
whatever the model does. It is deliberately short. **Only a do-not-call request
forces anything** — a false positive there costs one lost sale and a false
negative costs somebody being phoned after they asked not to be. Being asked
"are you a robot?", being asked for a person, and a plain no all raise guidance
for the next turn without forcing a state.

Both halves are tested independently: `evals/sales/do_not_call.yaml` uses a
phrasing the detector catches and checks the behaviour, and
`do_not_call_implicit.yaml` uses one it does not and checks that the model
catches it.

### What it records

Every call keeps a structured record, and **an unknown field stays unknown**. If
the agent never asked about timing, `buying_timeline` is `UNKNOWN` — it does not
become `LATER` because the prospect sounded unenthusiastic:

```
CONVERSATION | MEETING_REQUEST after 6 caller turn(s) | interest=INTERESTED
  qualified=QUALIFIED timeline=THIS_QUARTER decision=DECISION_MAKER
  next=MEETING_REQUESTED pain=2 objections=PRICE
```

Interest level, qualification status, pain points, objections, buying timeline,
decision-maker role, existing provider, next action, meeting intent and callback
intent. `qualification_status` is the one field the model does not set: it is
derived from need, interest and authority, so "qualified" means the same thing on
every call and cannot be talked into existence by an enthusiastic model.

On a campaign call the record is written to the call attempt's
`conversation_data`, and the three outcomes that come from what the person
*said* — `DO_NOT_CALL`, `CALLBACK_REQUESTED`, `NOT_INTERESTED` — become the
attempt's status. Everything else is left to the carrier reconciliation, and the
reconciliation will not overwrite those three: what somebody said outranks the
fact that the call completed.

### The call result (Phase 8)

Every call attempt that reaches a final status gets one row in `call_results`:
the validated, CRM-ready reading of the call. A call nobody answered gets one
too, with everything the carrier cannot know left unknown.

```bash
uv run campaign.py results --campaign "Q1 Outreach"   # one line per finished call
uv run campaign.py result 12 --transcript             # one call in full
uv run campaign.py result 12 --json                   # the flat export an integration would read
uv run campaign.py rebuild-results                    # results for attempts that finished before the table existed
```

What a result carries: the ids (attempt, prospect, campaign); the attempt's
final status and a one-word **disposition** — `QUALIFIED`, `MEETING_BOOKED`,
`TRANSFERRED`, `CALLBACK_REQUESTED`, `NOT_INTERESTED`, `DO_NOT_CALL`,
`UNQUALIFIED`, `COMPLETED`, or `NO_ANSWER` / `BUSY` / `FAILED`; the duration;
the qualification status, interest level, buying timeline and decision role;
the meeting and callback status with their times; pain points, objections,
the prospect's questions, and the discovery fields; every tool call with its
verdict; the **transcript**, verbatim; and a six-part **summary** — what
happened, needs, objections, interest, qualification, next step.

Three rules hold across all of it, and `tests/test_results.py` checks each:

- **Unknown stays unknown.** Every enum has an `UNKNOWN` value, every text
  field is nullable, and a field the record did not supply is never filled in.
  Unknown is not false (`human_requested` is `null` on an unanswered call, not
  `false`), and unknown is not "not interested" — a `NOT_INTERESTED`
  disposition needs a recorded no.
- **Qualification is derived, never asserted.** The result rebuilds it from
  interest, pain points and decision role by the same rule the live call uses,
  and refuses a record that claims otherwise. Every result is validated before
  it is stored; an inconsistent one is logged and not written.
- **The transcript is evidence and the summary is a reading of the record.**
  The transcript is what the pipeline reported, turn by turn, with interrupted
  replies cut where the caller cut them. The summary is composed
  deterministically from the structured fields — no model reads the
  transcript — which is the only way to guarantee it invents nothing. Where a
  field is unknown the summary says so by name.

Two writers, one rule: the bot writes the rich result when it finishes a call;
the dialer writes a thin one from the carrier's report. A conversation result
always wins, whichever arrives first. The raw outcome stays in
`conversation_data`, so a result can always be rebuilt from it.

Nothing pushes a result anywhere yet. The shape is flat and typed so that a
later phase can map it onto a HubSpot call engagement, a Pipedrive activity or
a Salesforce task without changing what is stored; the mapping is sketched in
`src/campaigns/results.py`.

## Safe to point at real numbers (Phase 9)

Phase 9 is about what happens when something breaks. Run the health check
before a calling session, and the recovery pass after anything unclean:

```bash
uv run health.py                 # every dependency, without placing a call
uv run campaign.py recover       # resolve attempts left live by a crash
```

### Never two calls to the same person

The requirement that shapes the rest. Five independent mechanisms, any one of
which would usually be enough:

1. The queue picks and reserves in one transaction, `FOR UPDATE SKIP LOCKED`.
2. Every reservation carries an **idempotency key** derived from what the call
   is — campaign, membership, attempt number — under a unique index. Two
   callers who mean the same call resolve to one row, lock or no lock.
3. **Placing a call is never retried.** Neither Twilio nor SignalWire offers an
   idempotency key for call creation, so a retry is a second phone call.
4. A placement that times out or loses its connection is **ambiguous**, not
   failed. The attempt is held as `UNRESOLVED`, which is a *live* status, so
   the prospect cannot be dialled again.
5. `campaign.py recover` asks the carrier which calls actually exist and writes
   the answer. **It never dials to find out.** An attempt it cannot resolve is
   closed as failed, so the prospect stops being blocked and the campaign's own
   retry policy decides about trying again.

Not knowing costs one uncalled prospect. Guessing costs a stranger's phone
ringing twice. Every branch above errs the first way.

### Retries, and where they are not allowed

| Operation | Policy |
|---|---|
| Reading from a carrier, a vendor, the database | 3 attempts, exponential backoff, jitter, per-attempt timeout |
| Placing a call | **one attempt.** An ambiguous answer is held, never repeated |
| A write whose outcome is unknown | never retried; the caller has to handle not knowing |

A failure is classified before it is retried: `RETRY` (it certainly did not
happen), `FATAL` (it was refused on its merits), or `AMBIGUOUS` (unknown). Only
the first is tried again.

### Campaign safety

Calling hours are enforced by default — 09:00–18:00, Monday to Friday, in the
prospect's timezone where their record supplies one and `CALLING_TIMEZONE`
otherwise. A closed window refuses *before* a reservation is taken, so it never
spends one of a prospect's attempts. Alongside it: a concurrency limit
(one call at a time by default), optional pacing, the existing attempt cap and
do-not-call enforcement, and a hard ceiling on how long one call may last.

### When something breaks mid-call

The bot supervises itself. Consecutive failures from one pipeline stage end the
call deliberately — with a goodbye if the agent can still speak — instead of
leaving the caller listening to silence. An inference that starts and never
finishes is caught the same way, and the LLM's own HTTP timeout is set below
that threshold so the request is abandoned first.

One failure is enough if the agent has not spoken yet: a call whose greeting
never happens has nothing to recover to, and nothing else would trigger another
attempt. That case was found by running a call during a real provider outage,
where it had left the caller in silence for 57 seconds.

### Logs you can follow

Every log line carries the call's ids — campaign, prospect, attempt, carrier
call id, provider. `LOG_FORMAT=json` emits them as fields. Every configured
credential's value is replaced with `***` in every record, including exception
text.

### What it will not do

The honesty rules are in the system prompt and asserted by
`tests/test_conversation.py`, so a prompt edit that drops one fails a check
rather than surfacing on a live call. It will not claim to be human, invent a
price, a feature, a customer or a certification, invent anything about the
person it is calling, promise a result it was not given, or argue with a no. And
it will not say a meeting is booked, a callback is scheduled, or that it is
connecting you to somebody, unless the tool that does that has just answered
`success: true` — which is the rule the next section is built around.

### What it can do (Phase 7)

Seven of the twelve tools act on the world rather than recording it:

| Tool | What it does | What stands behind it |
|---|---|---|
| `search_knowledge_base` | Looks a fact up on the model's request | The same retrieval path as the per-turn knowledge block — same embedder, same store, same thresholds |
| `check_calendar_availability` | Free slots on one day | A calendar provider: the local business-hours calendar by default, or Cal.com |
| `book_meeting` | Books one of the slots that were offered, and only one of those | The same provider, then a row in `meetings` |
| `schedule_callback` | Creates a callback at an exact time, and puts the prospect back in the queue for it | A row in `callbacks`; the membership's retry time |
| `mark_do_not_call` | Marks the prospect immediately, once | Phase 5's do-not-call, which also cancels any pending callback |
| `transfer_to_human` | Hands a live phone call to a person | The carrier's redirect-a-live-call API, to `TELEPHONY_TRANSFER_NUMBER` |
| `end_call` | Ends the call after the goodbye | The pipeline's own graceful end |

The model never touches the database or an API. Every tool goes through the
same boundary — schema derived from the function's signature, arguments
validated against it, the call guarded, one audit line logged with the session,
call, prospect and attempt ids — and hands the request to the conversation,
which applies the rules that depend on where the call is (no booking after a
"no", nothing at all after a do-not-call), and asks a backend that validates the
rest and does the I/O. Every result has one shape:

```
{"success": true,  "data": {...},  "error_code": null,   "message": null,  "guidance": "..."}
{"success": false, "data": null,   "error_code": "...",  "message": "...", "guidance": "..."}
```

`success` is the only thing the model may read as "it happened". `guidance` is
what to say next, and on a failure it always says, in some form, "nothing
happened; do not say it did" — because the turn after a tool call is steered by
nothing else. The record is held to the same standard: `meeting_booked`,
`callback_scheduled_for` and `transferred` are written only on success, and the
call's outcome lists every tool call with its verdict.

**Booking a meeting** is a sequence the prompt and the tools enforce together:
the prospect agrees, the agent asks which day, checks that day, offers at most
two of the returned times, and calls `book_meeting` only with a time it was
given — anything else is refused as `slot_not_offered`. The default calendar is
a real one in this system's own database (`campaign.py meetings` is the diary);
`CALENDAR_PROVIDER=calcom` books into a Cal.com event type instead, so the
invitation really arrives. The agent is told the current date, time and
timezone, so "next Tuesday at ten" becomes an exact moment before any tool sees
it, and a vague time is refused with a message telling it to ask for a specific
one.

**A callback** is the same shape: an exact future time or nothing, refused if
it is in the past or beyond `CALLBACK_MAX_DAYS_AHEAD`, one pending callback per
prospect (asking again moves it), and when the call ends the campaign membership
is reopened for that moment — the queue already refuses to hand anything out
before its retry time, so a callback for Tuesday at ten is dialled on Tuesday at
ten. `campaign.py callbacks --due` lists what is due.

**A transfer** is a blind redirect through the carrier: the agent tells the
person it is connecting them, the carrier moves the call to the configured
number, and the media stream to the bot closes. It works only on a phone call
with carrier credentials and a `TELEPHONY_TRANSFER_NUMBER`; anywhere else the
agent is told it cannot transfer and offers a callback instead.

**What the session can do shapes what the agent is told.** A browser session
with no carrier is not told about transfers; a bot with `CALENDAR_PROVIDER=none`
is told to record that a meeting was agreed and say a colleague will confirm.
An agent that promises what it cannot do is the failure this phase exists to
prevent, and the prompt is built from the capabilities rather than the other
way round.

### Who it is calling

A call placed by `campaign.py` carries its prospect, campaign and attempt ids on
the carrier's media stream, and the agent looks the person up: first name,
company, job title, industry, location, any notes from the import, and a line
from the previous call if there was one. `bot.py` never imports the campaign
tables — it asks `src/campaigns/briefing.py`, which is the one module that knows
both.

A browser session carries none of that and the agent is anonymous, which is
correct and is *stated* in the prompt rather than hidden: it is told, by name,
which fields it does not have, and told to ask rather than guess. Set
`DEV_PROSPECT_*` in `.env` to test personalisation without a database. Those are
used only when a call carries no prospect id — a campaign call whose prospect
cannot be found stays anonymous rather than being told it is talking to whoever
`.env` last described.

## Measuring it

With `LOG_METRICS=true` (the default) every response logs one line:

```
LATENCY | response 2: total 1024ms  (stt 484ms · llm 345ms · tts 135ms · turn-end 486ms)
```

and the session ends with a p50/p95 summary per stage, plus an error count.

- **total** — from the moment you actually fell silent to the first audio out of
  the agent. The only number you experience.
- **turn-end** — silence to the turn being released, i.e. how long it took to
  decide you were finished. The number turn-taking config moves.
- **stt** — silence to the final transcript. **llm** — request to first token.
  **tts** — request to first audio byte.

`stt` and `turn-end` start at the same instant and overlap, so they do not add.
On the Flux path they come out nearly identical, because the transcript and the
end-of-turn decision arrive in the same message; on the fallback path the gap
between them is exactly what local turn detection costs. `total` is roughly
`turn-end + llm + tts`, which do run in sequence.

The measurement starts from your real silence, not from when the VAD got around
to reporting it: `UserBotLatencyObserver` subtracts `stop_secs`. See
`src/metrics.py`.

Measured over two clean passes of the eval suite on this machine (Deepgram Flux,
Groq `qwen/qwen3.8-27b`, Cartesia Sonic, home broadband, 14 responses):

| | p50 | range |
|---|---|---|
| total | 1326ms | 990 – 2533ms |
| turn-end | 653ms | 417 – 2024ms |
| stt | 646ms | 417 – 2021ms |
| llm | 372ms | 312 – 759ms |
| tts | 148ms | 130 – 173ms |

Greeting (connect to first audio) is 2.4 – 3.0s, almost all of it websocket
setup to three vendors.

**Turn detection is the whole story** — it is more of the wait than the LLM and
TTS combined, and it is also the most variable. Read these as a pessimistic
bound rather than what a browser call feels like: under the eval harness the
same CPU is simultaneously running Kokoro, Moonshine and Silero, and the
synthesized caller stops streaming audio the instant its utterance ends, where a
real microphone keeps sending. A live call has not been measured here, because
that needs someone to talk into it.

## Testing it

`server/evals/` holds headless conversations that drive the real bot with
synthesized speech and assert on what it does — barge-in, context memory,
speakable output, the silence check-in. They exist because a voice agent cannot
be eyeballed like a web page, and because the transcript of a clean interruption
and the transcript of an agent that talked over you look identical.

```bash
cd server
SESSION_IDLE_TIMEOUT_SECS=3600 uv run python -m pipecat.evals suite evals/suite.yaml
```

See [`server/evals/README.md`](server/evals/README.md) for why both of those
details are load-bearing.

Alongside them are nine deterministic check scripts that need no vendors, no
database and no phone, and run in seconds:

```bash
uv run python tests/test_conversation.py  # the sales layer: states, record, detectors, transcript, all 14 scenarios
uv run python tests/test_results.py       # Phase 8: the call result — dispositions, validation, summary, transcript
uv run python tests/test_reliability.py   # Phase 9: injected failures — duplicate calls, restarts, retries, guardrails
uv run python tests/test_actions.py       # the Phase 7 tools against a stubbed calendar, store, carrier
uv run python tests/test_scheduling.py    # the local calendar's arithmetic; Cal.com against a stub HTTP session
uv run python tests/test_knowledge.py     # retrieval, chunking, what the LLM is handed
uv run python tests/test_telephony.py     # call placement, transfer, outcomes, config, bot wiring
uv run python tests/test_realtime.py      # echo suppression, the peer watchdog
uv run python tests/test_campaigns.py     # phone numbers, CSV import, the queue, DNC, callbacks, meetings, results, duplicate protection
```

`test_reliability.py` is the one that injects failures rather than avoiding
them: a carrier that times out, a database that has gone away, an LLM that
starts a response and never finishes it, a webhook delivered twice, a process
that dies between reserving a call and placing it.

No test books a real meeting or transfers a real call: the calendar and the
carrier are stubs that can be told to succeed, refuse or fail, which is how the
failure paths get exercised at all.

`test_campaigns.py` is the one exception to "no vendors, no database": its pure
half (phone normalisation, CSV mapping, duplicate detection) runs anywhere, and
its SQL half runs against PostgreSQL **in a temporary schema that is dropped
afterwards**, so your real tables are never touched. With no database reachable
it skips that half and says so. The telephony provider is stubbed throughout —
no test ever places a real call.

and two manual tools that need a running bot but still no account:

```bash
uv run python tests/fake_carrier.py                        # simulate a phone call
uv run python tests/fake_carrier.py --prospect 1 --campaign 1 --attempt 12   # ... as a campaign call, so the result lands on attempt 12
uv run python tests/fake_browser.py --reconnect --abandon  # simulate a browser that drops
```

Between them those cover all three transports headlessly: the eval suite drives
the eval one, `fake_carrier.py` the telephony one, and `fake_browser.py` the
WebRTC one that people actually develop against — which was the only transport
nothing tested, and is where both of the bugs in [When it misbehaves](#when-it-misbehaves)
were hiding.

The division is deliberate. The evals are the only thing that can tell you the
agent *works*, and they take minutes and cost API calls; these cover the parts
that are pure logic, so a failure points at one of them rather than at the whole
stack. Neither can prove the audio on a real phone call — that needs a phone.

## Architecture

```
server/
├── bot.py              # Wiring only: transport, pipeline, event handlers
├── call.py             # Place one outbound phone call and watch it
├── health.py           # Check every dependency, without placing a call (Phase 9)
├── campaign.py         # Prospects, campaigns and the call queue
├── ingest.py           # Load documents into the knowledge base
├── src/
│   ├── config.py       # Reads and validates env; fails fast with clear messages
│   ├── services.py     # make_stt() / make_llm() / make_tts() factories
│   ├── turns.py        # VAD, turn-start/stop strategies, barge-in guard
│   ├── metrics.py      # Per-response latency and the session summary
│   ├── resilience.py   # Silence escalation and disconnect grace window
│   ├── diagnostics.py  # Turn-cycle tracing, errors, barge-in logging
│   ├── prompts.py      # System prompt and the turn instructions
│   ├── retrieval.py    # The pipeline stage that grounds each answer; search() for the tool
│   ├── knowledge_store.py / embeddings.py / documents.py   # The knowledge base
│   ├── conversation/   # The sales call (Phase 6), and the tools it calls (Phase 7)
│   │   ├── states.py / qualification.py / brief.py / signals.py / playbook.py
│   │   ├── results.py    # The one result shape, and the error-code vocabulary
│   │   ├── actions.py    # ActionBackend: the contract with whatever acts on the world
│   │   ├── toolkit.py    # The tool boundary: schema, argument checks, guard, audit log
│   │   ├── tools.py      # The twelve functions the model calls
│   │   ├── transcript.py # What was said, verbatim and in order (Phase 8)
│   │   └── conversation.py / director.py / sources.py / sink.py
│   ├── actions/        # The backend behind the tools: validation, authorisation, I/O
│   ├── scheduling/     # Calendar providers: business-hours local calendar, Cal.com
│   ├── reliability/    # Phase 9: retries, idempotency, guardrails, health, structured logs
│   │   ├── retry.py       # May this be tried again? RETRY / FATAL / AMBIGUOUS
│   │   ├── idempotency.py # What makes two requests the same call
│   │   ├── guardrails.py  # Calling hours, pacing, concurrency, call duration
│   │   ├── supervisor.py  # What the bot does when a service fails mid-call
│   │   ├── health.py      # Every dependency, probed cheaply
│   │   └── observability.py # Call ids on every line; credentials scrubbed
│   ├── campaigns/
│   │   ├── models.py     # Prospect, Campaign, CampaignProspect, CallAttempt, ScheduledCallback, Meeting
│   │   ├── results.py    # CallResult: the validated, CRM-ready reading of a finished call (Phase 8)
│   │   ├── phone.py      # E.164 normalisation that refuses to guess
│   │   ├── csv_import.py # Header mapping and row validation; no database
│   │   ├── store.py      # The seven tables, and the queue's transaction
│   │   ├── service.py    # The rules: who may be called, what an outcome means
│   │   ├── briefing.py   # The one place campaigns meet the conversation
│   │   ├── dialer.py     # The one place campaigns meet the telephony provider
│   │   └── recovery.py   # What a restart does about calls that were in flight
│   └── telephony/
│       ├── base.py       # TelephonyProvider, call outcomes, TwiML, stream URLs
│       ├── twilio.py     # The only file that knows Twilio exists
│       ├── signalwire.py # Twilio's API, somewhere else — a 40-line subclass
│       ├── transport.py  # Builds the call's transport with the right serializer
│       ├── session.py    # What the bot knows about the call it is on
│       └── __init__.py   # make_provider(): the one place that picks a carrier
├── evals/              # Headless conversation tests
├── tests/              # Deterministic checks, plus the browser and carrier simulators
├── .env                # Your keys (git-ignored)
└── pyproject.toml
```

Three boundaries matter.

`src/services.py` — each pipeline stage is built by a factory that reads a
provider name from config, so **swapping a provider is a `.env` change plus one
branch in a factory; `bot.py` never changes.**

`src/telephony/` — the same idea one level up: `make_provider` is the only thing
that knows which carrier is configured, and `bot.py` never imports a carrier.

`bot.py` — wiring only. Every behavioural decision lives in a module next to it,
so "why does it wait that long before answering" has one file to read
(`src/turns.py`) rather than a pipeline to trace.

### Swapping the LLM

`LLM_PROVIDER` accepts `groq`, `anthropic`, `openai`, `cerebras`, `openrouter`,
`mistral` and `ollama` — all work with the packages already installed, no extra
needed. Set the matching `<PROVIDER>_API_KEY` and optionally `<PROVIDER>_MODEL`.

Free tiers: Groq, Cerebras, Mistral, OpenRouter (some models). Ollama is fully
local and needs no key at all. Anthropic and OpenAI both require paid credits —
an Anthropic account with a zero balance returns a `400` at the first turn, which
is what the pipeline surfaces as a non-fatal error mid-call.

### Tuning

Every knob is in `server/.env.example`, documented with what it trades away. The
defaults are Pipecat's and Deepgram's own tuned values rather than numbers picked
here, so the honest starting position is to leave them alone and change one at a
time against the evals.
