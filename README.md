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

The scheduler (Phase 13), the carrier's webhooks (Phase 14), the CRM sync
(Phase 15), production booking and transfer (Phase 16) and the n8n
automation layer (Phase 17) build on top of this, each in a process of its
own.

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

**On a phone line, Phase 12 measured the other half.** Stopping the audio is
one number — from the interruption reaching the pipeline to the output
transport reporting the bot stopped, measured at 62–141 ms over a simulated
carrier — and the carrier is told to drop what it had buffered at the same
moment (the `clear` event, visible in `tests/fake_carrier.py`'s output). But
the *first* drill of that phase found every interruption landing on a bot
that had already finished, because the STT was running five seconds behind
real time: the carrier had streamed audio into a buffer for the eight seconds
the session took to set up, and Flux was still working through it. `bot.py`
now drops that backlog before the pipeline starts and warms the process once
at startup, which took the greeting from 15 s to 4–7 s after the call
connected and turn-start detection to under a second. Each interruption is
logged as `turn.interrupted | latency_ms=…`; one that turns out to carry no
words — a cough, a door, an echo — is logged as `turn.spurious_interruption`
and the agent is asked to pick up where it left off rather than sit in
silence until the idle nudge. See `src/voice_quality.py`.

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

### One request per recording turn (Phase 32)

A tool call normally costs two LLM requests: the model calls, the result
goes into the context, the model is run again to speak. For the five
recording tools — `record_discovery`, `set_interest`, `record_objection`,
`move_to_stage`, `request_meeting` — the result changes nothing the caller
needs to hear, so when the model has already spoken its reply in the same
response as the call, that reply is the turn and the second request is
skipped. It is skipped only then: a call made without speaking, two calls
in one response, a no that has to be closed, a failure, all run the second
request as before. The tools that act on the world — the calendar, the
booking, the callback, the knowledge search, the do-not-call, the transfer,
`end_call` — always wait for their result before the model may claim
anything. Nothing about what is recorded or when it is written changed.

### What the model reads per request (Phase 31)

`uv run python scripts/prompt_tokens.py` (add `--deployment` for the campaign
in `.env`) counts what every LLM request carries, with the Qwen3 tokenizer
when it is cached: the system instruction section by section, each tool
schema, and the set advertised at each stage. Since Phase 31 the twelve tool
schemas are not all sent on every request: none on the opening, the recording
tools plus what the stage can act on while selling, and only the closing
tools once the call is ending. Every handler stays registered for the whole
call, so a tool the model calls while it is not in view still runs. The
system instruction says each rule once; the per-turn guidance and the tool
results say it again at the moment it applies, which is where a small model
acts on it.

### The voice stops (Phase 30: the TTS fallback)

A TTS provider that stops synthesising — Cartesia answering HTTP 402 when
the account's credits are gone, a websocket that will not connect, a
timeout — used to leave the caller in silence until the supervisor ended
the call. With a second provider on standby the call carries on:

```
TTS_PROVIDER=cartesia
TTS_FALLBACK_ENABLED=true
TTS_FALLBACK_PROVIDER=elevenlabs     # with ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID
```

Both services start with the call. The first time the primary reports a
provider failure (anything but a failure of the application's own code)
it is retired for the rest of that call: the sentence it was asked for is
spoken once by the fallback if the primary produced no audio for it,
the rest of the response is handed over sentence by sentence, and from the
next response on the fallback is spoken to directly. Nothing is spoken
twice, nothing switches back, and the next call starts on the primary
again. The log says so:

```
TTS FALLBACK | tts.fallback.engaged | primary=CartesiaTTSService#0 fallback=ElevenLabsTTSService#0 category=quota error=... respoken=1 deferred=True after_ms=8412
TTS FALLBACK | tts.fallback.active | fallback=ElevenLabsTTSService#0 reason=response finished after_ms=11530 respoken=1 handed_over=2
TTS FALLBACK | CartesiaTTSService#0 -> ElevenLabsTTSService#0 after 8.41s (quota); 1 re-spoken, 2 handed over; state=fallback
```

`uv run health.py tts tts_fallback` checks both keys without synthesising
anything. Leave the fallback off when `TTS_PROVIDER` already names the only
provider you want. See `server/src/tts_fallback.py` for where the switch
happens and why it waits for the end of the response.

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

To reach the **application** (`/app/`) on the same tunnel — a free ngrok
gives one hostname, pointing at one port — run `app.py` with `--proxy-bot`
(or `APP_PROXY_BOT=true`) and tunnel *its* port instead. The application then
forwards every path it does not own (`/client`, `/api/offer`, `/ws`, the
carrier's `POST /`) to the bot, so `https://<tunnel>/app/` is the
application, `https://<tunnel>/client/` is the bot's client, and the carrier's
`TELEPHONY_PUBLIC_URL` stays the same hostname:

```bash
cd server && uv run bot.py                 # terminal 1 — the agent, 7860
cd server && uv run app.py --proxy-bot     # terminal 2 — the application, 7900, forwarding to 7860
ngrok http 7900                            # terminal 3 — one hostname for both
```

### Deploying the application to Vercel

The **application** (the page, the dashboard, the automation API) deploys to
Vercel as one Python function, for as many people as need it at once. The
**bot does not** — a voice pipeline is a long-lived process with WebSockets,
which a function is not — so it keeps running where it does now (the ngrok
tunnel, or a host of your own) and the deployed page frames it from there.
Neither does the campaign engine: campaigns are created, started and watched
on Vercel, and the calls are placed by `uv run app.py` (or
`uv run campaign.py run`) on a machine that shares the same database.

The repository root holds the deployment: `pyproject.toml` (the slim
dependency set, ~345 MB installed, pinned to `server/uv.lock`'s versions),
`uv.lock`, `vercel_app.py` (the entry; its docstring is the full reference),
`vercel.json` and `.vercelignore`.

1. **A PostgreSQL both sides can reach.** Supabase (use the *session pooler*
   URL, `aws-0-<region>.pooler.supabase.com:5432`, user `postgres.<ref>` — the
   direct host is IPv6-only and Vercel is IPv4) or Neon (the pooled URL); add
   `?sslmode=require`. Create the schema once from the laptop:
   `DATABASE_URL=<that url> uv run campaign.py init`.
2. **Import the repository in Vercel** (Root Directory: the repository root,
   the default). Vercel finds the entry through `[tool.vercel]` in
   `pyproject.toml`; no framework preset to choose.
3. **Environment variables:** paste `server/.env` into *Settings →
   Environment Variables* (the editor accepts a whole `.env`), then change:
   `DATABASE_URL` to the hosted one; `DASHBOARD_SESSION_SECRET` set (required
   — every instance must sign sessions with the same secret; `uv run
   security.py make-secret`); `APP_BOT_URL=https://<the bot's public
   address>`; `SECURITY_REQUIRE_HTTPS=true` and
   `SECURITY_TRUSTED_PROXIES=0.0.0.0/0,::/0` (Vercel terminates TLS and every
   request arrives from its network). The provider keys stay: the
   configuration page and the campaign wizard read them.
4. **Deploy.** `https://<project>.vercel.app/app/`.

What is different from `uv run app.py`, all by design of a function: the
engine and the n8n deliverer are off; the live event stream is off
(`APP_STREAM_ENABLED=false`, the page polls every 15 s); the knowledge base
defaults to off (`KB_ENABLED=false`, its embedder is not installed); a CSV
import or a document over 4.5 MB is refused by Vercel with 413; the Live
Agent page frames the bot at `APP_BOT_URL`, so through ngrok the free
tier's "visit site" page shows once inside the frame (or use *Open in a new
tab*). Locally, `uv sync && uv run uvicorn vercel_app:app --port 7902` at
the repository root runs the very same thing against `server/.env`.

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
uv run campaign.py run                               # place calls unattended for every ACTIVE campaign (Phase 13)
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

`campaign.py call` places one such call by hand. `campaign.py run` (Phase 13)
is the loop that places them unattended — see [The scheduler](#the-scheduler-phase-13).

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
SALES_COMPANY_DESCRIPTION=Meridian Fleet Systems builds fleet management software for mid-sized logistics operators.
SALES_SERVICES=live vehicle tracking|route planning|maintenance scheduling
```

The last two are the **always-available campaign context**: the agent answers
"who are you?", "what does your company do?" and "what services do you offer?"
from them directly, in the system instruction, without a knowledge base
lookup — so a retrieval that happens to miss no longer makes it say it has no
information about its own company. Detail stays in the knowledge base. A
campaign's `configuration` can carry its own `company_description` and
`services`, and a campaign that names a *different* `company_name` inherits
neither from the environment.

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
`DEV_PROSPECT_*` in `.env` to test personalisation without a database
(`DEV_PROSPECT_EMAIL` gives a Cal.com booking its attendee). Those are
used only when a call carries no prospect id — a campaign call whose prospect
cannot be found stays anonymous rather than being told it is talking to whoever
`.env` last described.

## Measuring it

With `LOG_METRICS=true` (the default) every response logs one line:

```
LATENCY | turn.latency | response=2 total_ms=1024 turn_end_ms=486 stt_ms=484 llm_first_token_ms=345 tts_first_audio_ms=135
```

and the session ends with a p50/p95 summary per stage, plus an error count.
The fields are `k=v` (Phase 12) so one stage can be grepped across a whole
run, and they land as keys under `LOG_FORMAT=json`. On a phone call the same
figures, per response and as percentiles, go into the call's report — see
[Real-line voice quality](#real-line-voice-quality-and-voicemail-phase-12).

Phase 29 adds the breakdown *inside* that total — where a slow turn spent its
time. Every turn logs two more lines, one for a person and one for `grep`:

```
LATENCY | TURN 4 | STT final 0.35s | KB retrieval 0.21s (4 passages) | LLM TTFT 1.42s | LLM total 2.31s | tool 0.08s (record_discovery 0.08s) | TTS TTFA 0.39s | TOTAL end-of-user-speech -> first-audio 2.11s
LATENCY | turn.breakdown | turn=4 kind=response outcome=responded stt_final_ms=350 kb_ms=210 turn_end_to_llm_ms=290 llm_ttft_ms=1420 llm_total_ms=2310 llm_requests=2 tool_ms=80 tools=record_discovery first_token_to_tts_ms=60 tts_ttfa_ms=390 tts_audio_to_played_ms=20 total_ms=2110
```

and the call ends with `LATENCY CALL SUMMARY`: the average of every stage
over the caller's turns and the p95 and max of the total. `stt_final` is end
of the caller's speech (the VAD's stop, else the end-of-turn decision) to the
final transcript; `kb` is the retrieval stage; `llm_ttft` runs from the first
LLM request of the turn to its first token, which on a tool turn spans the
tool and the second request; `tts_ttfa` is the TTS request to its first chunk;
`total` is end of speech to the output transport reporting the bot speaking.
The gaps between stages (`turn_end_to_llm_ms`, `first_token_to_tts_ms`,
`tts_audio_to_played_ms`) are pipeline time no service measures. A turn that
was interrupted, hit an error or never produced audio says so in `outcome`;
the greeting and any reply nobody prompted (an idle nudge) are turns of their
own kind and stay out of the averages. The same record goes into the call
report under `latency.turns`. Nothing in these lines is text the caller said,
a tool's arguments or a result — stage names, tool names and milliseconds.

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

## The dashboard (Phase 10)

Everything before this reported through a CLI or a log line, which is right for
placing a call and wrong for "how is the campaign going". That question wants
eight numbers next to each other.

```bash
cd server
uv run dashboard.py                 # http://127.0.0.1:7870
uv run dashboard.py --once          # the same numbers as JSON, no server
```

It shows total contacts, total calls, answered, completed, failed, average call
duration, qualified prospects and meetings booked; then a breakdown of what
calls came to, a row per campaign, and the fifteen most recent calls with the
outcome each produced.

**It reads the same PostgreSQL the dialer writes.** No analytics database, no
warehouse, no scheduled rollup, no second copy of anything. Every figure is a
SQL aggregate run at the moment you load the page, so it cannot go stale or
disagree with `campaign.py`. Every route is a `GET`; there is no way to write
through it, which is what makes it safe to point at a system that is dialling.

Two numbers on it could mislead, so both carry their footnote:

- **Answered and completed overlap and are not the same.** `COMPLETED` is the
  carrier's word for a call that ran to its end; *answered* is every status
  meaning somebody picked up, including a call the person ended by asking never
  to be called again.
- **An average duration means nothing without the count it averages**, so the
  tile always says how many calls it is over.

A missing optional table is reported as *unavailable*, never as zero — a zero
next to "Qualified prospects" is a claim, and an absence is not. On a database
that predates Phase 8 the page still renders, says which command fixes it, and
falls back to attempt statuses for the outcome breakdown.

The page is one HTML file with no build step, no framework and no CDN: it
renders in the browser from `/api/dashboard`, which is also the endpoint any
other tool should read. It binds to loopback by default, because it has no
login and shows names and phone numbers; `--host 0.0.0.0` works and prints a
warning saying so.

## Performance, scale and cost (Phase 11)

Phase 11 measured the system before changing it. The measurements, at 20,000
prospects and 60,000 call attempts on a development machine:

| Path | Measured | Verdict |
|---|---|---|
| The dialer's queries | 0.7–10.7 ms | Fine. Left alone |
| Retrieval per turn (embed + pgvector) | 29 ms of a ~1,300 ms turn | Not the bottleneck. Left alone |
| One dashboard load | 249 ms across 7 sequential queries | Fixed |
| Connections held per call | 2 idle, up to 6 | Fixed |
| Prompt per LLM request | 3,394 tokens, 40% of it tool schemas | The real cost driver. See below |

**Candidate indexes were measured and rejected.** Six plausible indexes on the
aggregate queries made every one of them *slower* — a full-table `count(*)` is
a sequential scan whatever you index, and the extra pages and write cost are
real. The negative result is in the handoff so nobody adds them again.

### What changed

- **Dashboard queries run concurrently**, not one after another: **249 ms →
  149 ms (43% faster)**. Same queries, same numbers.
- **A 5-second snapshot cache** with a lock: 21 simultaneous viewers now cause
  **1 database read instead of 21**, and a slow read is never started twice.
- **One connection pool per call instead of two**, when the knowledge base and
  campaign tables share a database (the default). Measured **2 idle
  connections per call → 1**, peak 6 → 4. Against PostgreSQL's default 100
  connections that roughly doubles how many calls can run at once.
- **The concurrency limit is enforced inside the reservation transaction.** It
  was checked before reserving, which two workers could pass at the same
  instant. Six simultaneous reservations against a limit of two now hand out
  exactly two.
- **Per-call usage and cost tracking.** Pipecat has reported tokens, characters
  and audio seconds since Phase 2 and nothing read them. Every call now records
  what it used onto its attempt row, and the dashboard shows tokens and cost
  per call.

```bash
uv run python scripts/benchmark_db.py     # the measurements, at scale, in a throwaway schema
```

### Cost tracking

Units are measured; prices are configured; nothing is invented. With no rates
set — the default, since the stack is three free tiers — you get token counts
and no cost. Set `COST_LLM_INPUT_PER_MTOK` and friends to what you actually
pay and each call gets a figure.

A stage is priced only when the provider *reported* its usage. Cartesia's TTS
reports characters; Deepgram's websocket TTS does not, so a call on Deepgram
TTS lists `tts` as `unmeasured` rather than pricing it at zero. A total quietly
missing a stage is worse than one that says which stage is missing.

### What was deliberately not done

The largest remaining cost is the prompt: **3,394 tokens per request, of which
1,248 are the twelve tool schemas**, measured live. Advertising only the tools
the current stage can use would cut 300–500 tokens per request. It was not
done, for the reason Phase 7 recorded and this phase confirmed in the installed
source: Pipecat re-syncs tool handlers on *every* context frame and unregisters
any tool the frame does not advertise, so a stage-filtered list would register
and unregister handlers around every inference. That is a real race in the
layer that enforces do-not-call, and it is not worth 12% of a prompt.

The providers were not changed either. The dominant latency is turn detection
(653 ms of ~1,300 ms), which is Deepgram's tuned default and where cutting
people off lives.

## Real-line voice quality and voicemail (Phase 12)

Everything before this phase was verified with clean 16 kHz audio and a caller
who never coughed, never talked over the agent, and was never an answering
machine. Phase 12 is about the call as a phone makes it: 8 kHz μ-law, a person
who answers in one word or forty, background noise, and a recording that
picks up instead of a person. Nothing in the audio pipeline changed; what
changed is what is measured, what is logged, what happens when an
interruption is noise, and what happens when a machine answers.

**Every phone call leaves a report.** `bot.py` writes
`CALL_REPORT_DIR/<call id>.json` when the call ends: every caller turn with
its transcript and how long the reply took (turn released → LLM started →
first token → TTS started → first audio), every barge-in with how fast the bot
stopped and what the caller heard before the cut, every failed turn, the
latency percentiles, the voicemail verdict, and how the call ended. The same
record is stored with the conversation on the attempt row (`quality` in
`conversation_data`). It is what the two Phase 12 tools read, because they run
in another process and cannot see inside the pipeline.

**Failed turns, interruptions and telephony failures are structured log
lines.** `turn.failed` (a turn with words in it that got no audio back within
`TURN_RESPONSE_TIMEOUT_SECS`, with the reason: no response, an inference that
produced no audio, or a service error), `turn.late_response` (it came after
all), `turn.interrupted` (with the stop latency), `turn.spurious_interruption`
and `turn.noise_resume`, `turn.stop_timeout`, `telephony.backlog_dropped`,
`telephony.failure` (the line closed before the agent said anything, or
mid-turn) and `telephony.line_closed`, `voicemail.detected`,
`voicemail.carrier_answered_by`. All of them are `event | k=v` lines, so
`grep turn.failed` over a run answers "how often does that happen".

**Answering machines.** Two detectors, independent, both off the audio path:

- The bot's own, from what the line sounds like: a first caller turn that
  contains something only a recording says ("leave a message after the
  tone"), or that talks *over the agent's greeting* for longer than
  `VOICEMAIL_MAX_GREETING_SECS`. A person waits for the greeting to finish; a
  machine does not. Phone calls only, and only the first two turns.
- The carrier's, when `TELEPHONY_MACHINE_DETECTION` is on: the call is placed
  with detection requested and the verdict is read back from the call
  resource's `answered_by` by the bot (a few polls after connect), by
  `campaign.py` when it refreshes the attempt, and by `call.py` — and, since
  Phase 14, delivered by the carrier's own asynchronous-detection webhook, so
  the completion that follows becomes `VOICEMAIL` without a poll.

On a verdict the bot hangs up (`VOICEMAIL_ACTION=hangup`, the default) or
waits for the greeting to end, speaks `VOICEMAIL_MESSAGE` word for word with no
model involved, and hangs up. The attempt becomes `VOICEMAIL` — a new final
status that is retried like a no-answer and shows on the dashboard — and its
call result has the `VOICEMAIL` disposition with the recording's transcript
kept as evidence and every qualification field forced to unknown. The beep is
not detected; `VOICEMAIL_MESSAGE_DELAY_SECS` is the guess that clears it.

**Two tools that need a running bot.**

```bash
uv run bot.py                                             # terminal 1
uv run python tests/phone_drill.py all --record drills    # terminal 2: no account needed
uv run python tests/live_call.py --to +92300XXXXXXX       # terminal 2: your phone rings
```

`phone_drill.py` speaks scripted callers with Kokoro over the carrier's own
wire protocol — the same 8 kHz μ-law path a real call takes — and checks the
bot's report afterwards: one-word answers, an interruption mid-sentence, an
interruption that goes on for seconds, a sentence with a pause in it, a long
sentence at 1.35× speed, steady background noise, and a voicemail greeting
with a beep. It also measures how long after the caller starts talking the bot
notices, which is the number that exposed the audio backlog above. All seven
pass on this machine; the numbers are in HANDOFF.md.

`live_call.py` places one real call to a number you name, prints a script for
you to follow on the phone (greet, answer in one word, interrupt, pause, go
quiet, hang up), polls the carrier as `call.py` does, then reads the bot's
report and prints a PASS/FAIL checklist across both sides. It asks before
dialling, because it costs money and rings a phone. It has not yet been run
against a live carrier from this machine.

## The scheduler (Phase 13)

Until Phase 13 a campaign was a queue somebody emptied by hand, one
`campaign.py call` at a time. Now:

```bash
uv run bot.py                              # terminal 1: the bot answers the calls
uv run campaign.py run                     # terminal 2: places them, for every ACTIVE campaign
uv run campaign.py run "Q1 Outreach" --max-calls 20   # one campaign, twenty calls, then exit
uv run campaign.py run --once              # recovery and one placement pass, for cron
```

Import prospects, `start` a campaign, and the worker does the rest: it runs the
recovery pass, then in a loop places due callbacks first and the queue second,
follows every call it placed to its end with the carrier, writes the outcome
back, moves the membership on, and marks the campaign `COMPLETED` when nothing
is left it could ever dial. A campaign started while it runs is picked up within
`WORKER_IDLE_SECS`; a paused one places no more calls.

**It is a separate process from the bot, on purpose.** The bot's loop is
STT → LLM → TTS with a person waiting on every millisecond; the worker polls
carriers and queries the queue from its own process, and the two share nothing
but the database rows and the call id. Nothing here can add latency to a turn.

**It adds no rules of its own.** Every decision is one the earlier phases
already make — the queue's eligibility SQL, the calling window in the
prospect's own timezone, the concurrency limit counted inside the reservation,
pacing, the never-retried placement, the monotonic status write, recovery. The
worker decides only *when to ask*, and sleeps for exactly as long as a refusal
says (`retry_after_secs`) rather than spinning.

**What "reliable" means here:**

- *A crash loses no job and repeats none.* The rows are the state. On start
  the worker runs recovery, then *adopts* every call still in progress and
  follows it to its end. A reservation a dead process never dialled is handed
  back to the queue; a placement whose outcome is unknown stays blocked until
  the carrier says.
- *Callbacks are kept.* A due `PENDING` callback is placed before any
  never-called prospect, through a targeted reservation under the same rules.
  It is placed even when the membership has used its `CAMPAIGN_MAX_ATTEMPTS`
  — the limit exists to stop pestering people who do not answer, and a person
  who asked to be phoned back is the opposite case. One honest try: a callback
  the carrier refuses is withdrawn with the reason in the log.
- *A prospect in a closed timezone costs nothing.* Their reservation is handed
  back unspent and scheduled for when their window opens, instead of burning an
  attempt every tick until morning.
- *Stopping is graceful.* Ctrl+C once stops placing and lets the calls in
  progress finish (up to `WORKER_DRAIN_SECS`); twice stops now and leaves them
  to `campaign.py recover`.
- *Every transition is a structured event* — `call.started`, `call.status`,
  `call.completed`, `call.failed`, `call.skipped`, `callback.due`,
  `campaign.completed`, `worker.recovery` — and a `worker.metrics` line every
  `WORKER_REPORT_SECS` counts queued, started, completed, failed and skipped
  calls with a breakdown of each.

The settings are the `WORKER_*` block in `.env.example`. None of them decide
whether a call may be placed; the Phase 9 limits do that, unchanged.

**What it deliberately is not:** a broker, a lock service, or a table of its
own. One process is the design point for this phase. The reservation is
already safe across processes, so a second worker could not cause a duplicate
call — but pacing is in-process and the two would halve the interval, and the
concurrency limit is what the reservation counts, not what each worker
follows. See `HANDOFF.md` for what a second worker would need. (The webhook
endpoint it also did without arrived in Phase 14, below.)

## Carrier status webhooks (Phase 14)

Until Phase 14 the only way to learn what happened to a call was to ask the
carrier — `call.py` once a second, the worker every `WORKER_POLL_SECS`. Now
the carrier tells us:

```
campaign.py run  --REST-->  the carrier  --dials-->  the person
                                 |
                                 ├─websocket--> bot.py's /ws                      (audio, both ways)
                                 '--POST------> /webhooks/telephony               (initiated, ringing,
                                                 verify → decode → ledger →        answered, completed,
                                                 apply_call_event → record_outcome  busy, no-answer, failed)
```

Every call is placed with a `StatusCallback`, and the carrier POSTs each
lifecycle event to `TELEPHONY_PUBLIC_URL` + `TELEPHONY_WEBHOOK_PATH` — the
same public address the audio already uses, so one tunnel serves both. The
receiver:

- **Verifies before it reads.** Twilio signs every delivery with a base64
  HMAC-SHA1 over the URL and the sorted fields, keyed by the auth token;
  SignalWire uses the same algorithm keyed by a separate signing key
  (`SIGNALWIRE_SIGNING_KEY`, from the dashboard's API credentials page). The
  check is constant-time, against the *configured* public URL rather than
  whatever a tunnel showed the local server, and an event naming another
  account is refused too. A forged or unsigned event is answered 403 and
  touches nothing.
- **Applies each event once.** Deliveries land on a ledger,
  `telephony_webhook_events`, whose event key is unique: a redelivery loses
  the insert and is answered 200 `duplicate` before the attempt is read. The
  status then goes through the same monotonic write a poll uses, so
  `answered` arriving after `completed` is `stale`, and a conversation
  outcome the bot wrote (a callback, a do-not-call) is never overwritten by
  the carrier's `completed`.
- **Moves the campaign on.** An applied status runs `record_outcome` exactly
  as after a poll: the membership completes or is scheduled to retry, the
  prospect becomes `CONTACTED`, the carrier's call result is written. An
  asynchronous answering-machine verdict arrives on its own event and is read
  back when the completion follows, so it becomes `VOICEMAIL` as it would
  have from a poll.
- **Keeps polling underneath.** The worker still asks the carrier about every
  call it follows — every tick until the first event for that call arrives,
  then every `WORKER_WEBHOOK_POLL_SECS` (30 s) as a safety net. A receiver
  that is down, unmounted or refusing costs nothing but the old request rate.
- **Is not in the audio path.** By default the route is mounted on the bot's
  own web server, because that is the address the tunnel reaches, but the
  handler does one HMAC and a few short awaited database statements and never
  touches a pipeline, a frame or a session. `TELEPHONY_WEBHOOK_RECEIVER=standalone`
  moves it to `uv run webhooks.py` on port 7880 for a deployment that can
  route one path to a second process.

Every delivery is a structured line — `webhook.applied`, `webhook.duplicate`,
`webhook.stale`, `webhook.unmatched`, `webhook.refused`, `webhook.malformed`,
`webhook.store_unavailable` — with the call, attempt, sequence number and
latency, and `uv run campaign.py webhooks` lists the ledger: what the carrier
sent, when, and what was done with it. `uv run call.py --dry-run` says where
a call's events would go.

The carrier code stays where it was. `TelephonyProvider` gained
`verify_webhook` and `parse_webhook`, `TwilioProvider` implements them,
`SignalWireProvider` overrides a header name and the secret, and nothing
outside `src/telephony/` knows what a `CallSid` is. A carrier that cannot be
verified — SignalWire without its signing key — is asked for no events at
all rather than sending events that would be refused; the startup line and
`campaign.py run` say which.

## CRM sync (Phase 15)

Every finished call has had a CRM-ready `CallResult` row since Phase 8. With
a CRM configured, a third process files each one with it:

```bash
uv run campaign.py crm-sync            # until Ctrl+C: claims unsynced results, files them, records the outcome
uv run campaign.py crm-sync --once     # one pass, for cron
uv run campaign.py crm-status          # what is synced, retrying, failed, skipped, and not yet seen
uv run campaign.py crm-retry --all-failed
```

```
the bot  --writes-->  call_results  <--claims--  campaign.py crm-sync  --REST-->  HubSpot
                                                    (contact + call activity, once each)
```

**What the CRM gets.** The person as a **contact** — matched by email, then
by phone number (digits compared, so a number typed as `0300 1234567` is the
`+923001234567` that was dialled), created with name, phone, email, company
and title if the CRM has never seen them — and the call as a **call
activity** associated to it: when, how long, outbound, HubSpot's own
disposition (Connected, No answer, Busy, Left voicemail), and a body that
restates the result in headed sections: Outcome, Summary, Qualification, Pain
points, Objections (each with whether it was handled), Questions, Discovery,
Meeting (`Booked for Tue 08 Sep 2026, 15:00 PKT`), Callback, Next action,
Actions taken, Notes. A fact nobody recorded is named as such rather than
left out. The structured facts also land on the contact as custom `ai_*`
properties — qualification, interest, next action, last disposition and
time, meeting and callback times, pain points, objections, the latest
summary, the campaign — created on the first run.

**It never touches a call.** The bot writes the result at the end of a call
as it always has and knows nothing about a CRM; `crm-sync` reads the row
later, from its own process. `tests/test_crm.py` asserts that nothing on the
call path imports the CRM package.

**Each result is filed once.** A `crm_sync` row per result, claimed under
`FOR UPDATE SKIP LOCKED`, so two sync processes never file the same call and
a filed result is not filed again — until the result itself changes (the
conversation's rich result replacing the carrier's thin one), when the same
activity is updated. The CRM's ids are recorded the moment they are known, so
a crash resumes rather than repeats; and a create whose answer was lost is
looked up by the key written into the activity before it is ever repeated.

**Failures are bounded and honest.** A transient one — the CRM down,
rate-limited, a timeout — backs off from `CRM_SYNC_RETRY_SECS`, doubling and
honouring the CRM's own `Retry-After`, for up to `CRM_SYNC_MAX_ATTEMPTS`,
then the row is `FAILED` with the reason for a person. A refusal on the
merits is `FAILED` at once. A rejected token stops the pass and hands the
rows back, because that is not a fact about any row. `crm-status` shows every
row's state, tries and last error; `crm-retry` reopens what a person has
fixed.

**Swappable.** `src/crm/base.py` is a six-method `CrmProvider` and a
vendor-neutral contact and activity; `mapping.py` turns a result into them
once, for every CRM; `hubspot.py` is the only file that knows HubSpot's
endpoints. A Pipedrive or Salesforce adapter is a module beside it, a name
in `CRM_PROVIDERS`, and a branch in `make_crm_provider`. The token lives in
`HUBSPOT_ACCESS_TOKEN`, is never logged, and `uv run health.py crm` checks it
by reading one contact.

## Production booking and transfer (Phase 16)

Phase 7 built the booking and transfer actions; Phase 16 makes them hold up
when the world does not cooperate. The conversation logic did not change.

**Booking.** Cal.com is the production calendar (`CALENDAR_PROVIDER=calcom`;
Calendly cannot create a booking through its API, so it is not offered).
Real availability comes from Cal.com's slots endpoint; Cal.com enforces its
own availability, so a slot taken between the offer and the booking is
refused and the agent offers another time. Every request has a timeout
(`CALCOM_TIMEOUT_SECS`, 15 s), and a booking whose answer was lost is
**looked up, not repeated**: the client lists the attendee's bookings around
that start and adopts the one Cal.com made, or says plainly that nothing was
booked. The booking's Cal.com uid is stored as the meeting's `reference`.
`uv run health.py calendar` reads the event type back — the key works, the
id exists, and its length matches `CALENDAR_SLOT_MINUTES` — before a call
finds out. The local calendar (the default, no account) became atomic: an
exclusion constraint on the `meetings` table refuses an overlapping live
booking at the write, so five bots booking one slot in the same second get
one row and four "that time has just been taken".

**Transfer.** `transfer_to_human` still hands the live call to
`TELEPHONY_TRANSFER_NUMBER` through the carrier's live-call update, rings it
for `TELEPHONY_TRANSFER_TIMEOUT_SECS`, and tells the prospect nobody is
available if it goes unanswered. What is new is the outcome: with the
webhook receiver configured (Phase 14), the `<Dial>` reports how the
colleague's leg ended — answered, busy, no answer, failed — and the receiver
records it on `call_transfers` and answers the carrier with the TwiML that
decides what the prospect hears next. The request is recorded the moment
the carrier accepts it, in a bounded, guarded write that cannot fail or slow
the transfer, and `uv run campaign.py transfers` lists every transfer with
its outcome and duration. A refused or unreachable carrier is still reported
to the agent as before, which offers a callback instead.

**What a real test needs** is the last section of `server/.env.example`: the
Cal.com key, event type and slot length for a booking; a transfer number you
can answer and a public URL for a transfer.

## Automation with n8n (Phase 17)

An automation platform sits *outside* the call. It never touches
speech-to-text, the model, text-to-speech, VAD or turn detection; it talks
to two things that run in a process of their own, `uv run automation.py`:

```
CSV / CRM / event
    ↓
n8n  --HTTP + API key-->  the automation API      prospects, campaigns, calls, callbacks, results
                                    ↓ rows
                          the scheduler           `campaign.py run` places the call, under every rule
                                    ↓
                          the bot                 holds the conversation, unaware
                                    ↓ rows
n8n  <--signed POST-----  the outbox              call.completed, lead.qualified, meeting.booked,
    ↓                                             callback.scheduled, call.updated, campaign.completed
CRM / calendar / notifications
```

**The API never dials.** `POST /api/v1/calls` writes the same pending-
callback row the agent writes when a prospect says "call me back", due
now; the scheduler places it on its next tick, ahead of the queue, under
the calling hours, the concurrency limit and pacing. `202` means queued,
never ringing. The rest of the API is the CLI's own operations over HTTP:
create or import prospects (the same header aliasing, phone normalisation
and duplicate detection as `campaign.py import`), create a campaign, add
people to it, `start` / `pause` / `resume` / `complete` / `cancel`,
schedule a callback, read a call, a result (Phase 8's export shape), the
meetings, and the outbox itself.

**Every write is idempotent, twice.** The rows have natural keys — a
prospect per number, a campaign per name, a membership per pair, one
pending callback per prospect — so repeating a request repeats nothing.
And a request carrying an `Idempotency-Key` has its answer stored for a
day, so a client retrying after a lost answer gets the same status and
body back (`Idempotent-Replayed: true`); the same key with a different
body is refused.

**Every event is delivered once per row.** The outbox creates events from
the rows that already record the fact — a call result once it has been
unchanged for `AUTOMATION_SETTLE_SECS` (the carrier's thin result and the
conversation's rich one land seconds apart, and the window is what makes
`call.completed` carry the rich one), a meeting the calendar confirmed, a
callback promised, a campaign the scheduler closed — under a key that says
what the event *is*, and POSTs each to n8n's webhook URL with the same
`event_id` on every redelivery. Deliveries carry a static header n8n's
Webhook node checks natively and an HMAC-SHA256 signature over the exact
bytes sent; a transient failure backs off and retries, a refusal is closed
with the reason, and `campaign.py events` / `events-retry` show and reopen
them. Two deliverers are safe: rows are claimed `FOR UPDATE SKIP LOCKED`.

**Authentication is a bearer key**, compared in constant time, on every
route but `/api/ping`; the API refuses to serve without one, because
every write it takes can make a phone ring. It binds to loopback unless
told otherwise.

Six importable n8n workflows — CSV intake → campaign, campaign → outbound
calls on a schedule, completed call → CRM, qualified lead → Slack, meeting
booked → CRM, callback due in the CRM → call — and the full API and event
reference are in [`n8n/README.md`](n8n/README.md). Every variable is in
`server/.env.example` under AUTOMATION. `tests/test_automation.py` drives
the real API over a fake store, proves the scheduler places what the API
asked for and nothing else, drives the deliverer through every ending, and
checks the SQL against PostgreSQL. **No real n8n instance has yet received
a delivery**: the first workflow you activate is the first live test.

## Multi-worker production scaling (Phase 21)

Phase 13's scheduler was one process. It still is — and now any number of
them can run at once, on one machine or many, over the same queue:

```bash
cd server
uv run campaign.py init            # once: the worker_id column, scheduler_workers, scheduler_state
uv run campaign.py run             # start as many of these as you like
uv run campaign.py workers         # who is alive, what each holds, the queue depth (--json, --prune 24)
uv run health.py scheduler         # the same, as a health component
```

**The coordination mechanism is PostgreSQL, and nothing else.** Every process
already holds a pool to it; Redis or a broker would be a second thing to run
and a second place for the truth to live. What the fleet shares, and how:

* **The reservation** was already safe across processes (Phase 9's row lock,
  `SKIP LOCKED` and idempotency key). It now runs under a transaction-level
  advisory lock, so the concurrency count inside it is exact: two workers
  cannot both count "one below the limit" and both reserve. **The same
  prospect is never dialled by two workers** — the live-attempt exclusion in
  the same statement — and `MAX_CONCURRENT_CALLS` is counted across the
  whole fleet, not per process.
* **Pacing is one clock in the database.** A placement *takes the slot*
  (`scheduler_state`, under a lock) before the carrier is asked; a worker
  refused by the slot gives the reservation back and sleeps for the wait.
  `CALL_PACING_SECS` is the deployment's interval; a campaign's own
  `configuration.pacing_secs` is a second scope on top.
* **Every worker has an identity and a heartbeat** (`scheduler_workers`):
  registered at start, beating every `WORKER_HEARTBEAT_SECS`, `draining`
  once asked to stop, `stopped` at the end. No beat for `WORKER_STALE_SECS`
  means dead.
* **Every call names its owner** (`call_attempts.worker_id`). A worker
  follows what it placed and only that. Every `WORKER_ADOPT_SECS` each live
  worker claims — under one lock, so no two claim the same call — the live
  calls of stale, stopped or unknown workers, and releases the reservations
  a dead worker took and never placed. A live worker's calls are never
  touched; a worker that finds another live worker on its call stops
  following it.
* **A clean stop hands over at once.** Ctrl+C stops placing and drains as
  before; at the end the worker clears ownership of anything still live and
  marks itself stopped, so the next adoption pass anywhere picks the calls
  up without waiting for a row to go stale. With no other worker running,
  `campaign.py recover` and the next start behave as they did.
* **Failed jobs are retried when the failure was the system's.** A
  `FAILED` attempt whose reason is the carrier's (503, 429, unavailable), a
  timeout, a lost connection, a worker that died with the call, or a
  placement recovery closed as unconfirmed goes back to the queue after
  `WORKER_TRANSIENT_RETRY_MINUTES` (or the campaign's retry policy), within
  the attempt ceiling. An invalid number, a blocked one, a do-not-call are
  never retried. `WORKER_RETRY_TRANSIENT_FAILURES=false` restores Phase 13's
  behaviour.
* **A webhook delivered twice is applied once**, whichever receiver gets
  it: Phase 14's ledger key and the monotonic write already did this, and
  the checks now prove it under concurrent delivery.
* **Metrics.** `campaign.py workers`, `health.py`'s `scheduler` component,
  a *Workers and queue* strip on the dashboard and `scheduler` in
  `/api/v1/status`: workers alive, draining, stale and stopped; calls being
  followed; due now, scheduled later, reserved and live; per campaign.

The realtime pipeline is untouched: `bot.py`, `src/conversation/`,
`src/telephony/` and `src/actions/` did not change. A database without the
new tables is served by one worker as before, with a warning naming
`campaign.py init`.

```bash
uv run python tests/test_scaling.py                    # 151 checks (SQL half needs PostgreSQL)
```

## Production monitoring and observability (Phase 22)

Everything above already wrote numbers somewhere — a latency summary at the
end of a session, a usage line, a `worker.metrics` line every minute, a
per-call report on disk, a heartbeat row. Phase 22 makes them one system
that a deployment can watch, without changing what a call does:

* **One id follows a call through every process.** The dialer makes a
  sixteen-character `trace` before it asks the carrier to dial, writes it
  on the attempt row, sends it to the bot on the media-stream handshake and
  logs under it; the bot, the tools, the store's writes, the webhook
  receiver, the CRM syncer and the event deliverer all log under the same
  one (the last three read it back from the row). `grep trace=9f3c2a71b0d4e582`
  across the fleet's logs is the whole story of one call:

  ```
  scheduler → telephony → agent → tools → database → webhook → CRM
  ```

  A browser session or an inbound call makes its own. With `LOG_FORMAT=json`
  every line also carries `component` (`bot`, `scheduler`, `dashboard`,
  `api`, `webhooks`) and `pid`, so five processes' logs shipped to one
  place can be told apart. The automation API gives every request an
  `X-Aiva-Request-Id` (honoured when the client sends one) and echoes it.
* **Metrics, by name, the same in every process.** A registry with no
  dependency — counters, gauges, histograms — served at `GET /metrics` in
  the Prometheus text format and at `/metrics.json` with exact p50/p95/p99.
  `src/monitoring/instruments.py` defines every one; the ones the phase
  asked for:

  | Tracked | Metric |
  |---|---|
  | call attempts | `aiva_call_attempts_total{campaign,outcome}` — placed, refused, blocked, deferred, exhausted, released, paced, unresolved |
  | success / failure | `aiva_call_outcomes_total{campaign,status}`, `aiva_call_results_total{outcome}` (by disposition) |
  | carrier failures | `aiva_carrier_failures_total{provider,kind}` |
  | STT / LLM / TTS errors | `aiva_service_errors_total{stage,kind}` (`error`, or `stall` for an LLM that never finished) |
  | barge-in | `aiva_barge_ins_total` |
  | webhook failures | `aiva_webhook_events_total{provider,outcome}` inbound, `aiva_automation_deliveries_total{kind,outcome}` outbound |
  | CRM / calendar / callback failures | `aiva_crm_syncs_total`, `aiva_calendar_operations_total`, `aiva_callback_operations_total` — each `{…,outcome}` |
  | latency | `aiva_turn_latency_seconds{stage}` (total, turn_end, stt, llm, tts), `aiva_greeting_latency_seconds`, plus placement, webhook, CRM, tool, store and HTTP histograms |
  | tokens | `aiva_llm_tokens_total{kind,model}`, `aiva_llm_tokens_per_call`, `aiva_tts_characters_total`, `aiva_stt_audio_seconds_total` |
  | cost | `aiva_call_cost_usd` (per call) and `aiva_cost_usd_total{stage}`, from Phase 11's rates — nothing when no rate is configured |
  | throughput | `aiva_throughput_calls{kind}` over `MONITORING_THROUGHPUT_WINDOW_SECS`, from the rows, deployment-wide |
  | worker health | `aiva_workers{state}`, `aiva_workers_in_flight`, `aiva_worker_heartbeats_total`, `aiva_worker_in_flight` |
  | queue depth | `aiva_queue_depth{bucket}` — due_now, scheduled, callbacks_due, reserved, live, backlog |

  Labels are a closed list that can never name a person (a `phone=` label
  fails a check, not a scrape); a campaign is its id, an HTTP route is its
  template. Counters are per process, as Prometheus expects; the fleet
  figures — queue, workers, throughput — are read from PostgreSQL into
  gauges every `MONITORING_REFRESH_SECS`, so a scrape of any one process
  answers for the deployment.
* **Health and readiness on every server.** `GET /healthz` says the process
  runs (role, pid, uptime, version, sessions active or calls followed);
  `GET /readyz` says it could do its job now — the database answers, the
  deliverer loop is up, the scheduler is not draining — or `503` with one
  scrubbed line per check. The bot's runner, the dashboard, the automation
  API and the webhook receiver mount them; `campaign.py run` serves its own
  on `MONITORING_PORT` (7895; a second worker on the machine finds the port
  taken and dials regardless). `MONITORING_TOKEN` puts a bearer in front of
  `/metrics`; the probes stay open. `MONITORING_ENABLED=false` leaves every
  process exactly as it was.
* **`uv run campaign.py metrics`** prints throughput, the queue and the fleet
  from the rows — `--json`, or `--prometheus` for a textfile collector.

```bash
uv run python tests/test_monitoring.py                 # 170 checks (SQL section needs PostgreSQL)
curl -s localhost:7860/readyz                          # the bot, once it is up
curl -s -H "Authorization: Bearer $MONITORING_TOKEN" localhost:7895/metrics   # a running scheduler
```

## Production readiness and end-to-end validation (Phase 23)

The last phase before real use adds no features. It adds the proof, the
runner and the honest document:

* **`tests/test_production.py`** runs the whole story over the real code and
  a real PostgreSQL (a schema thrown away afterwards): a CSV imported through
  the real API, the campaign created and activated, the scheduler selecting
  and gating (do-not-call, calling hours), the dial, a conversation with a
  barge-in, a knowledge-base answer from real pgvector, qualification, an
  objection, a meeting booked on the real local calendar, a transfer with the
  carrier's report, a callback scheduled and then executed by the worker, a
  no-answer and a voicemail, the signed completion webhook over HTTP, every
  row read back, the CRM filing, an n8n delivery over real HTTP to a receiver
  that verifies the signature, the dashboard and the API with three roles,
  recovery after a dead worker, transient retries, eight concurrent
  reservations, no secret in any log line or answer or tracked file, and every
  external provider failing gracefully. Only the carrier, the CRM, the speech
  services and n8n are stand-ins.
* **`uv run validate.py`** runs configuration hygiene, `health.py`,
  `security.py check`, the figures from the rows and all 24 check scripts,
  and writes `server/validation-report.md` with one line per go-live
  requirement — verified, failed, or *requires manual verification* — and
  exits 1 on any failure. `validate.py measure` reads answer rate, success
  rate, latency, cost and the duplicate-call audit from the rows.
  `validate.py live --to <your phone>` checks the preconditions for the
  controlled real-phone test and dials only with `--dial --yes`.
* **`PRODUCTION_READINESS.md`** at the root: architecture, every variable,
  deployment, external services, the database, webhook / CRM / calendar / n8n
  configuration, the security, compliance and monitoring checklists, known
  limitations, what was verified and how, and the go-live checklist. **It does
  not declare the system production-ready**: fifteen items can only be proven
  on a real line, a real vendor account and a real network, and each is
  listed with its command.

```bash
uv run validate.py                                     # a few minutes; writes server/validation-report.md
uv run python tests/test_production.py                 # 158 checks (needs PostgreSQL)
```

## The unified application (Phase 24)

Everything above ran as separate servers on separate ports — the bot on
7860, the dashboard on 7870, the n8n API on 7890 — each with its own
login. Phase 24 puts one application in front of them without changing any
of them: `uv run app.py` serves a single-page application at
`http://127.0.0.1:7900/app/` and mounts the existing dashboard at
`/dashboard` and the existing automation API at `/automation` on the same
origin, sharing one session cookie, so one login reaches every part.

* **Pages:** Dashboard (totals, recent activity, campaign progress, and a
  warning when campaigns are running with no scheduler alive); Campaigns;
  Create Campaign (a five-step wizard — details, contacts from the list or
  a CSV, the AI agent's profile, calling limits and hours, review); the
  campaign page (contacts, agent, calling, calls; start / pause / resume /
  stop); Contacts and Import CSV (a dry-run preview listing every rejected
  row with its reason, then confirm — **nothing dials on upload**); Calls
  and the call page (transcript, summary, pain points, objections,
  qualification, meeting, callback, next action, usage, the trace id);
  Live AI Agent (the bot's own `/client` in a frame, plus a test call
  queued for a campaign); Knowledge Base (upload, remove, search as the
  agent would); Analytics (the dashboard's strips with campaign and date
  filters); Settings (the effective configuration with secrets scrubbed, a
  health probe, the audit log for admins).
* **Nothing new underneath.** Campaigns, contacts and calls are the Phase 5
  rows; a campaign's agent profile is its `configuration` JSON, which the
  brief has read since Phase 6; the queue, the scheduler, the dialer, the
  bot and the carrier are untouched. The one new write on the API is
  `PUT /campaigns/{id}/configuration`. Starting a campaign marks it
  `ACTIVE`; the scheduler (`uv run campaign.py run`, or
  `app.py --with-scheduler`) dials it exactly as before, and the contact's
  name, company, notes and the campaign's agent profile reach the bot on
  the handshake as they always have.
* **It refreshes itself while a campaign runs.** The dashboard, the calls
  list and a campaign page (on its Contacts or Calls tab) re-read the rows
  every 15 seconds while any campaign is running — never over an open
  dialog or an edit form. Nothing is pushed: the scheduler and the bot write
  the rows, and the page reads them.
* **Security is Phase 18's.** The dashboard login, the three roles, PII
  masking, rate limits and the audit log apply unchanged. The API accepts
  the session cookie when the request carries an `X-Requested-With` header
  (the CSRF check), and API keys still work for n8n. Stopping a campaign
  needs an admin, as it always has.

```bash
uv run bot.py                          # terminal 1 — the voice agent, unchanged
uv run app.py --with-scheduler         # terminal 2 — http://127.0.0.1:7900/app/ ; dials ACTIVE campaigns
uv run python tests/test_app.py        # 136 checks
uv run python tests/test_spoken_text.py # Phase 26: reasoning and machine text never reach the voice
```

Sign in with a `DASHBOARD_USERS` account, or create one on the application's
**Register page** (the *Create one* link under the login, or `/app/#/register`;
Phase 27): name, email, role, password, confirm. A sign-up becomes a row of
the `dashboard_users` table — run `uv run campaign.py init` once to add it —
with the same scrypt hash as `DASHBOARD_USERS`, and signs in through the same
login by name or by email. A **Viewer** is active at once. An **Operator** or
**Admin** request is *pending*: it cannot sign in until an admin approves it
under *Settings → Sign-up requests* (approve as the role asked for, or
reject), and the role the browser sends never grants anything by itself.
`DASHBOARD_REGISTRATION_ENABLED=false` closes the page. Hand-configured
`DASHBOARD_USERS` entries are untouched and cannot be taken by a sign-up. On a loopback development machine
`DASHBOARD_AUTH_DISABLED=true` skips the login (an operator, so no admin
actions). `APP_HOST`, `APP_PORT` (7900) and `APP_BOT_URL` are the only new
variables. The separate `dashboard.py` and `automation.py` servers still
run on their own if you prefer them.

## Automated campaign execution (Phase 25)

Starting a campaign now dials it. The scheduler that `campaign.py run` has
been since Phase 13 runs **inside `app.py`** as the campaign execution
engine: start a campaign on its page and the engine reserves its contacts
one by one from the PostgreSQL queue, places each call through the
configured carrier, hands the contact's details to the bot on the
handshake, follows the call to its end, writes the outcome, and moves to
the next — until the queue is empty and the campaign marks itself
`COMPLETED`.

* **The queue is the rows.** A campaign's contacts are `campaign_prospects`
  rows; a reservation runs under a deployment-wide advisory lock (Phase 21),
  so no contact is handed out twice, whichever process asks — the engine,
  a `campaign.py run` beside it, or both.
* **States.** Memberships: `PENDING` (queued) → `IN_PROGRESS` →
  `COMPLETED` / `EXHAUSTED` / `SKIPPED`. Attempts: `PENDING`/`QUEUED`
  (reserved) → `CALLING` → `CONNECTED` → `COMPLETED`, `FAILED`,
  `NO_ANSWER`, `BUSY`, `VOICEMAIL`, `UNRESOLVED` (a carrier that never
  answered; recovery resolves it), and the conversation's own
  `NOT_INTERESTED`, `DO_NOT_CALL`, `CALLBACK_REQUESTED`. A failed call
  always carries a reason. Stopping a campaign does not cancel a call in
  progress; it cancels the *queue*, and the progress view counts the
  contacts it never reached as `cancelled`.
* **Concurrency.** `MAX_CONCURRENT_CALLS` across the deployment, and from
  this phase a campaign's own `max_concurrent_calls` (the Calling tab, or
  `PUT /campaigns/{id}/configuration`), both enforced inside the
  reservation transaction. No campaign can create an unbounded number of
  calls.
* **Controls.** Start, Pause, Resume, Stop on the campaign page. Pause and
  Stop place no new call and let the call in progress end normally.
* **Nobody twice, unless asked.** A reached or exhausted contact is never
  dialled again by the queue; the *Retry* button (or
  `POST /campaigns/{id}/prospects/{prospect_id}/retry`) queues one
  contact once more, ahead of the queue.
* **Restarts.** Every state is in PostgreSQL. A new engine adopts the calls
  a dead process was following (Phase 21) and reconciles the ambiguous
  ones with the carrier (Phase 9). Shutdown places no new call, gives the
  calls in progress `WORKER_SHUTDOWN_SECS` (30 s), then hands them over.
* **Live progress.** `GET /api/app/campaigns/{id}/progress` returns every
  counter — contacts, queued, calling, connected, completed, failed,
  no-answer, busy, voicemail, qualified, meetings, cancelled — and
  `GET /api/app/stream` (server-sent events) pushes them to the page as
  the rows change, so the campaign page and the dashboard update without
  a reload. `GET /api/app/engine` says what the engine is doing;
  `/readyz` includes it.

```bash
uv run bot.py                          # terminal 1 — the voice agent
uv run app.py                          # terminal 2 — the application and the engine: start a campaign, it dials
uv run app.py --no-engine              # the page only; `uv run campaign.py run` dials (WORKER_EMBEDDED=false)
uv run python tests/test_engine.py     # 68 checks (the last section needs PostgreSQL)
```

The engine is idle when no outbound carrier is configured (`TELEPHONY_*`);
the application still serves and says so. Verified on this machine with
the real engine over the real database dialling the real bot through a
stand-in for the carrier's HTTP API only: a two-contact campaign created from the page with a CSV, started from the page, dialled by the engine without any other command — Ada (interested: pain point, decision maker, partially qualified, 139 s, 9 transcript turns) and, 17 ms after her call ended, Grace (not interested, 69 s) — and marked COMPLETED by the engine itself; the progress route and the event stream showed every step (connected → completed → next contact → 100%), the calls list and the call pages showed both records, and the rows were deleted afterwards.

## The application's interface (Phase 26)

Phase 26 redesigned the page — and only the page: `server/web/index.html`,
`styles.css` and `app.js`. Every route, every API call and every write is
the Phase 24/25 one; nothing underneath changed.

* **One design system.** A small set of tokens and one component per idea;
  every status has one word and one colour everywhere (a dot *and* a word,
  so colour is never the only cue): Running, Paused, Completed, Failed,
  Queued, Calling, Connected, No answer, Busy, Cancelled, Qualified,
  Meeting booked look the same on the dashboard, in the tables, on a
  campaign page and on a call page.
* **Built around what you want to know.** The dashboard leads with contacts,
  active campaigns, calls completed, qualified leads and meetings booked,
  then the running campaigns with their progress and controls, then
  outcomes and rates, then a feed of what just happened. A campaign page
  is a live monitor: a segmented progress bar, the counters, and only the
  controls valid for its state (Start; Pause / Stop; Resume / Stop; nothing
  once completed). A call page starts with the outcome, then the summary,
  qualification, pain points, objections, next action, meeting, callback,
  the transcript, and a collapsed *Technical details* panel for the ids.
* **Clear flows.** New campaign: Details → Contacts → AI agent → Calling →
  Review & launch, with *Save as draft* or *Create and start campaign*.
  Import: Upload → Validate → Confirm → Done, with valid, invalid and
  already-known rows counted and every skipped row's reason shown.
* **Every wait, every empty page, every error.** Skeletons while a page
  loads, a spinner in the button you pressed, empty states that say the
  next action, and errors as a sentence (*Unable to start the campaign…*)
  with the server's own words under *Technical details*. Destructive
  actions (Stop, Do not call, Remove document) say what will happen before
  they do it.
* **Responsive and accessible.** The sidebar collapses under 960 px; tables
  scroll inside their card; dialogs trap focus and close on Escape; the
  active page is `aria-current`; icons have names.

Nothing in the browser talks to anything but its own origin; the icon set
is inline SVG because the content-security policy allows no CDN.

## What the caller may hear (Phase 26, second part)

Two things were found on the first real conversation with the bot and are
now closed off:

* **The model's reasoning never reaches the voice.** A reasoning model on
  Groq (`qwen3`, `gpt-oss`) with tools advertised writes its thinking into
  the answer channel by default, and the caller heard it ("I need to figure
  out which day next week refers to…"). The bot now asks Groq for the
  answer only (`LLM_REASONING_FORMAT=hidden`, the default; `parsed` or
  `off` if a provider rejects it), and a stage between the LLM and the TTS
  (`src/spoken_text.py`) drops tagged reasoning, tool markup a model emits
  as text, special tokens, and sentences with no words in them — the empty
  sentence Cartesia refused three times and the supervisor read as a dead
  voice. The stage holds a reply until the model has finished it, so a
  closing tag that arrives late discards the draft before it is synthesised
  (a few hundred milliseconds of generation time on Groq; the first token's
  wait, which dominates, is unchanged). What was removed is counted on the
  log, never spoken and never reconstructed. `uv run python
  tests/test_spoken_text.py` is the regression check.
* **The opening says what it is.** The agent's first sentence gives its
  name, says plainly that it is an AI assistant, and names the company —
  whether or not a compliance policy requires particular words. It also
  apologises first when somebody is annoyed at being called at all.

The 40–60 s silences seen on the free Groq tier are the provider's
per-minute token limit and the SDK's retry wait, not the bot; they are now
named on the log (`LLM | the provider refused the request and the SDK is
waiting to retry …`) so a quiet turn can be read for what it is.

## Production dashboard and analytics (Phase 20)

The Phase 10 dashboard (`uv run dashboard.py`, `http://127.0.0.1:7870`,
behind Phase 18's login) grew into the reporting surface, without being
rebuilt: the same page, the same JSON route, the same five-second snapshot
cache, extended.

* **Filters.** A campaign (by id or name) and a date range, applied
  *inside* every aggregate — `/api/dashboard?campaign=…&from=YYYY-MM-DD&to=…`
  — so a narrowed view costs the same single scan as the whole. The cache is
  keyed by the view and bounded; a filtered dashboard is a link.
* **Search and the calls list.** `/api/calls?q=…&status=…&before_id=…`
  pages through calls under the filters and searches name, company, email
  and the carrier's call id; the digits of a number only for an operator or
  admin (a viewer cannot learn whether a number is on file). `/api/search`
  finds people and calls.
* **One call in full.** `/calls/{id}` and `/api/calls/{id}`: the outcome,
  qualification, meeting and callback status, next action, pain points and
  objections, transfers, callbacks, meetings, the do-not-call entry, the
  usage and cost, the latency and turn figures — and the **transcript only
  for `read_pii`**, with every read on the audit log. A viewer's page says
  the transcript is withheld.
* **Five more strips.** *Progress* (active campaigns, memberships closed,
  calls remaining); *conversion* (qualified, meetings, callbacks and
  transfers, each over answered calls); *performance* (answer rate,
  voicemail rate, human-transfer rate, average duration, response latency —
  the average of each call's median from Phase 12's measurements);
  *errors* (failed, unresolved, unreached, failed turns, service errors);
  *do-not-call and opt-outs* (marked prospects, the list by source, verbal
  opt-outs, dials the gate refused). Every rate carries its denominator.
* **Per-campaign progress** in the campaigns table: a bar, reached /
  exhausted / skipped, remaining, opted out.
* **Efficient, and off the call path.** The dashboard is its own process
  over its own pool; the aggregates stay single-pass scans with `WHERE`
  filters (Phase 11 measured that indexes on them cost more than they
  saved), the per-call latency and error figures come from a small
  `usage -> 'quality'` JSON the sink writes beside the usage rather than
  from the transcripts, the detail page is keyed lookups, and search is a
  bounded `LIMIT` query the cache never holds.

```bash
cd server
uv run dashboard.py                                    # then sign in
uv run dashboard.py --once                             # the same JSON, no server, no login
uv run python tests/test_dashboard.py                  # 161 checks
```

## Outbound calling compliance (Phase 19)

**`COMPLIANCE.md` is the reference, and it says at the top that none of
this is legal advice or makes a deployment compliant with anything.** The
software implements the controls; deciding the policies is the operator's.

* **One gate before every outbound call** (`src/compliance/`): the
  do-not-call list, the prospect's status, the campaign's rules under the
  policy for that number, the calling window in the prospect's own timezone.
  Every decision — allowed or refused — is a row on the audit log.
* **A persistent do-not-call list** (`dnc_numbers`), keyed by number, with
  source, reason, who and when; never deleted, only revoked with a name on
  it. Enforced in the queue's SQL, at the gate, on import and create, on
  adding to a campaign, and on the API's call requests — each independently.
* **Opt-outs are immediate**: a request heard mid-call writes the person's
  status and the number's list row before the call ends, from a campaign
  call or an anonymous one.
* **Policies layer**: environment (`CALLING_*`, `CAMPAIGN_*`,
  `COMPLIANCE_*`) < campaign (`campaign.py compliance <campaign> --set …`,
  `PUT /api/v1/campaigns/{id}/compliance`) < jurisdiction
  (`COMPLIANCE_JURISDICTIONS`, by the number's country code, applied last so
  a campaign cannot loosen it). Attempt ceilings, per-outcome retry delays,
  calling windows, and AI and recording disclosures are all in it.
* **Disclosures are spoken first.** When a policy requires one, the agent's
  first sentence must include the configured text; the instruction is
  audited per call.
* **Clear dispositions**: `OPTED_OUT` (they said so on this call) is now
  distinct from `DO_NOT_CALL` (the list refused the dial).

```bash
cd server
uv run campaign.py init                                        # adds dnc_numbers
uv run campaign.py dnc --number +923001234567 --reason "asked" # or: dnc <prospect_id>
uv run campaign.py dnc-import registry.csv --source registry   # a suppression file
uv run campaign.py compliance "Q1 Outreach" --set max_attempts=2 --set ai_disclosure_required=true
uv run campaign.py compliance-log --since-hours 24             # every decision
uv run python tests/test_compliance.py                         # 146 checks
```

## Security (Phase 18)

The dashboard, the automation API and the webhook receiver show or change
customer data, so from Phase 18 each has a door on it. **`SECURITY.md` is the
reference**; this is the short version.

* **The dashboard needs a login.** Users live in `DASHBOARD_USERS` as
  `name:role:hash` (`uv run security.py hash-password` makes the hash), and,
  since Phase 27, in the `dashboard_users` table for people who sign up on
  the application's Register page (same hash, same login; a viewer at once,
  an operator or admin only once an admin approves the request);
  the session is a signed, HttpOnly, SameSite=Strict cookie. It refuses to
  start with nobody configured; `DASHBOARD_AUTH_DISABLED=true` restores the
  old login-free page for loopback only.
* **Three roles.** A *viewer* sees totals and outcomes with phone numbers
  masked and transcripts withheld; an *operator* runs campaigns and sees the
  people; an *admin* also closes campaigns, retries the outbox and reads the
  audit log. API keys are per role: `AUTOMATION_API_KEYS` (admin),
  `AUTOMATION_OPERATOR_API_KEYS`, `AUTOMATION_VIEWER_API_KEYS`. A viewer key
  gets `+92••••••••67` wherever a number would be, on every route.
* **Every write and every transcript read is on the audit log**
  (`audit_log`; `uv run campaign.py audit`, or `GET /api/v1/audit` as an
  admin), with the actor, the role, the address and the row — never a key or
  a number.
* **Rate limits** per key, per address and per login form; **input
  validation** beyond lengths (control characters, bounded custom data and
  imports, a body cap); **security headers** and a CSP on every answer;
  **CORS off** unless origins are listed explicitly.
* **HTTPS is your proxy's job, and required in production**:
  `SECURITY_REQUIRE_HTTPS=true` behind a TLS terminator listed in
  `SECURITY_TRUSTED_PROXIES`. Plain HTTP is then redirected (pages) or
  refused (API, webhooks), HSTS is sent and the cookie is `Secure`.
* **Secrets are environment variables and nothing else**, scrubbed from every
  log line. `server/.env` is no longer tracked by git.

```bash
cd server
uv run security.py hash-password --user alice --role admin   # DASHBOARD_USERS
uv run security.py make-secret                              # DASHBOARD_SESSION_SECRET
uv run security.py make-key --role operator                 # a key for n8n
uv run campaign.py init                                     # adds audit_log
uv run security.py check --strict                           # the posture
uv run python tests/test_security.py                        # 214 checks
```

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

Alongside them are twenty deterministic check scripts that need no vendors, no
database and no phone, and run in seconds:

```bash
uv run python tests/test_automation.py    # Phase 17: keys and signatures, the API over a fake store, the scheduler placing what the API asked for, the deliverer through every ending, the outbox in SQL
uv run python tests/test_booking_transfer.py # Phase 16: transfer TwiML and the carrier's report, Cal.com's timeout and lost answers, the diary's constraint, the transfers table
uv run python tests/test_crm.py           # Phase 15: the mapping, the HubSpot adapter over a stub, the syncer — successful, duplicate, retried, failed — and crm_sync in SQL
uv run python tests/test_webhooks.py      # Phase 14: carrier signatures (Twilio's published example), decoding, the receiver — valid, forged, duplicate, out of order — the worker polling less, the ledger in SQL
uv run python tests/test_voice_quality.py # Phase 12: turn monitoring, barge-in latency, noise recovery, voicemail, carrier AMD
uv run python tests/test_conversation.py  # the sales layer: states, record, detectors, transcript, all 14 scenarios
uv run python tests/test_results.py       # Phase 8: the call result — dispositions, validation, summary, transcript
uv run python tests/test_reliability.py   # Phase 9: injected failures — duplicate calls, restarts, retries, guardrails
uv run python tests/test_dashboard.py     # Phase 10: the aggregates, the honest footnotes, and that no route writes
uv run python tests/test_performance.py   # Phase 11: usage accounting, cost, pooling, concurrency, caching
uv run python tests/test_actions.py       # the Phase 7 tools against a stubbed calendar, store, carrier
uv run python tests/test_scheduling.py    # the local calendar's arithmetic; Cal.com against a stub HTTP session
uv run python tests/test_knowledge.py     # retrieval, chunking, what the LLM is handed
uv run python tests/test_telephony.py     # call placement, transfer, outcomes, config, bot wiring
uv run python tests/test_realtime.py      # echo suppression, the peer watchdog
uv run python tests/test_campaigns.py     # phone numbers, CSV import, the queue, DNC, callbacks, meetings, results, duplicate protection
uv run python tests/test_worker.py        # Phase 13: the scheduler — duplicate reservation, hours, DNC, retries, callbacks, restart, completion, shutdown
uv run python tests/test_scaling.py       # Phase 21: several workers over one queue — shared limit and pacing, heartbeats, a dead worker's calls adopted, a clean hand-over, transient retries, concurrent webhooks, the advisory lock in SQL
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
no test ever places a real call. `test_worker.py` follows the same split: the
scheduler runs whole campaigns against an in-memory store with the queue's
rules written out, a scripted carrier and a clock the checks move by hand, and
its last section checks the new SQL against PostgreSQL the same way.

and two manual tools that need a running bot but still no account:

```bash
uv run python tests/fake_carrier.py                        # simulate a phone call
uv run python tests/fake_carrier.py --prospect 1 --campaign 1 --attempt 12   # ... as a campaign call, so the result lands on attempt 12
uv run python tests/fake_browser.py --reconnect --abandon  # simulate a browser that drops
uv run python tests/phone_drill.py all                     # Phase 12: seven scripted callers over the phone path
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
├── dashboard.py        # Serve the read-only reporting page (Phase 10)
├── campaign.py         # Prospects, campaigns, the call queue, and `run`, the scheduler (Phase 13)
├── automation.py       # The n8n-facing API and the outbound event deliverer (Phase 17)
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
│   ├── automation/     # Phase 17: the API n8n drives, and the outbox it is sent. Reads campaigns/; nothing reads it
│   │   ├── auth.py        # API keys in constant time; the HMAC signature on every delivery
│   │   ├── serialize.py   # Every row as JSON, one shape for the API and the events
│   │   ├── events.py      # The deliverer: claim, build, POST once, back off, close
│   │   └── api.py         # The routes. Every write idempotent; none of them dials
│   ├── dashboard/      # Phase 10: the reporting page. Reads campaigns/; nothing reads it
│   │   ├── stats.py       # What each number means, and its footnote. Counts nothing itself
│   │   ├── page.py        # One HTML document: no build step, no CDN
│   │   └── web.py         # The routes. Every one of them a read
│   ├── reliability/    # Phase 9: retries, idempotency, guardrails, health, structured logs
│   │   ├── retry.py       # May this be tried again? RETRY / FATAL / AMBIGUOUS
│   │   ├── idempotency.py # What makes two requests the same call
│   │   ├── guardrails.py  # Calling hours, pacing, concurrency, call duration
│   │   ├── supervisor.py  # What the bot does when a service fails mid-call
│   │   ├── health.py      # Every dependency, probed cheaply
│   │   ├── usage.py       # What a call consumed and what it cost (Phase 11)
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

`LLM_PROVIDER` accepts `groq`, `gemini`, `anthropic`, `openai`, `cerebras`,
`openrouter`, `mistral` and `ollama`. Set the matching `<PROVIDER>_API_KEY` and
optionally `<PROVIDER>_MODEL`; Gemini uses `GEMINI_API_KEY` and `GEMINI_MODEL`.

Free tiers: Groq, Cerebras, Mistral, OpenRouter (some models). Ollama is fully
local and needs no key at all. Anthropic and OpenAI both require paid credits —
an Anthropic account with a zero balance returns a `400` at the first turn, which
is what the pipeline surfaces as a non-fatal error mid-call.

### Tuning

Every knob is in `server/.env.example`, documented with what it trades away. The
defaults are Pipecat's and Deepgram's own tuned values rather than numbers picked
here, so the honest starting position is to leave them alone and change one at a
time against the evals.
