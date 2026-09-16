# Evals

Headless conversations that drive the real bot and assert on what it does. No
microphone, no browser, no listening to it yourself to find out whether the last
change broke barge-in.

Each scenario is a YAML script. The harness synthesizes the caller's speech
locally (Kokoro), streams it to the bot's eval transport as real audio,
transcribes what the bot says back (Moonshine), and checks the result — some
assertions deterministic (an event arrived, within a time budget, containing a
substring), some judged by an LLM against a plain-English criterion.

There are **two suites**, and they answer different questions:

| Suite | Mode | Asks |
|---|---|---|
| `suite.yaml` | audio | Does the voice agent work? Turn-taking, barge-in, grounding, silence |
| `sales/suite.yaml` | text (one audio) | Does the *sales conversation* work? States, tools, honesty, objections |

Run both after a change to the pipeline; run `sales/` alone after a change to a
prompt or the conversation layer.

## Audio mode, deliberately — and the Phase 6 exception

The harness's text mode is faster and free, and for everything that matters here
it is the wrong tool. It sends each turn as a string, which means the bot's STT
never runs — and on this project the STT *is* the turn-taking: Deepgram Flux
decides when the caller has finished. A text-mode pass would tell you the LLM
answers questions, which was already true in Phase 1, and nothing about what
Phase 2 changed.

So every scenario in the suite pays for real audio: Kokoro speaks, Flux listens,
Cartesia replies, Moonshine transcribes. Both local models run on the CPU and
need no API key.

`smoke_text.yaml` is the one exception and is not in the suite. It is the
ten-second "is the wiring intact" check — no local models, no STT, no TTS. Run it
first after touching the pipeline, and run it when an audio failure could be
either the bot or the harness.

The first audio run downloads Kokoro (~310 MB) and Moonshine to
`~/.cache/pipecat`, which can take a while. If a download is interrupted the
truncated file fails later with `INVALID_PROTOBUF` — delete the cache directory
and let it fetch again rather than debugging it.

**`sales/` inverts this, on purpose.** Phase 6 changed what the agent *decides*,
not how it hears, and the harness's own rule of thumb is the right one: text
tests the brain, audio tests the ears and the mouth. Text mode also turns the
most important assertions from opinions into facts — `function_call` asks which
tool the agent called, where a judge would have to infer it from prose. The whole
set runs in a couple of minutes instead of a quarter of an hour, which is what
makes it usable while editing a prompt.

`sales/interruption.yaml` is the exception inside the exception: barge-in is
real audio arriving over real audio, and text mode has neither. It reuses the
`user_audio.yaml` and `judge_audio.yaml` blocks from this directory.

## Running them

Everything below runs from `server/`.

**The whole suite** — a fresh bot per scenario, so one scenario cannot inherit
another's conversation:

```bash
SESSION_IDLE_TIMEOUT_SECS=3600 uv run python -m pipecat.evals suite evals/suite.yaml
SESSION_IDLE_TIMEOUT_SECS=3600 uv run python -m pipecat.evals suite evals/sales/suite.yaml
```

Raise `SESSION_IDLE_TIMEOUT_SECS` for any eval run. Under `-t eval` the pipeline
starts at boot rather than when a client connects, so the 300s production default
counts down while a bot is waiting for its scenario and can shut it down
mid-suite.

**One scenario, against a bot you keep running** — the fast loop while you are
changing something. Boot the bot once in one terminal:

```bash
SESSION_IDLE_TIMEOUT_SECS=3600 uv run bot.py -t eval --port 7861
```

and drive it from another, as many times as you like:

```bash
uv run python -m pipecat.evals run evals/barge_in.yaml --bot-url ws://localhost:7861 -v
```

Add `-a` to save each conversation to `recordings/<scenario>.wav` and listen to
what the caller actually heard. Add `-d` to also write
`<scenario>.debug.log` with the harness's own internals, which is what to read
when a failure makes no sense.

`python -m pipecat.evals` rather than the `pipecat` console script, because the
judge is loaded by dotted path (`evals.groq_judge.make_judge`) and only the
module form puts the working directory on `sys.path`.

## The judge

Scenarios assert content with `eval:` criteria in plain English, which needs a
judge LLM. The harness knows two out of the box — a local Ollama model and
OpenAI — and this project has neither. `groq_judge.py` supplies a third through
the harness's `factory` hook, reusing the `GROQ_API_KEY` the bot already needs.
It is free and fast enough that judging costs nothing worth counting.

Scenarios with only deterministic assertions need no judge at all.

## The scenarios

| Scenario | What it would catch |
|---|---|
| `conversation` | The round trip is broken, or the agent has stopped remembering the previous turn |
| `barge_in` | Interrupting the agent no longer stops it, or it finishes its old answer instead of taking the new question |
| `voice_quality` | The agent has started writing for a screen — markdown, symbols, digits it cannot say |
| `silence` | It talks over dead air forever, or never notices the caller left |
| `short_answers` | Phase 12. A one-word answer ("Yes.", "Okay.") makes it ask the caller to repeat themselves |
| `rapid_speech` | Phase 12. A long, dense turn is cut off early, or arrives in pieces |
| `overlap` | Phase 12. The caller talks over it for several seconds and it answers the first half, or talks over them |
| `smoke_text` | The pipeline does not assemble, or the LLM is unreachable (not in the suite) |

The three Phase 12 scenarios test what a real caller does that a scripted one
did not. They are audio mode, like the rest of this suite, and Kokoro speaks at
one fixed rate — so `rapid_speech` exercises length and density, not tempo.
Tempo, background noise, a mid-sentence pause and an answering machine are
things the eval harness cannot synthesise; `tests/phone_drill.py` does those
over the carrier's own wire protocol (see the project README), and reads the
bot's per-call report rather than a judge's opinion.

### `sales/` — the cold call itself (Phase 6)

| Scenario | What it would catch |
|---|---|
| `interested` | The arc is broken: it pitches before asking, or never asks for a next step |
| `not_interested` | It argues with a no, or comes back with another angle |
| `angry` | It gets defensive, or explains itself at somebody who is annoyed |
| `price_objection` | It invents a price, or dismisses the concern instead of acknowledging it |
| `existing_provider` | It runs down a competitor it knows nothing about |
| `send_information` | It claims to have sent an email it cannot send |
| `callback` | It squeezes one more question in after being asked to call back, or claims a callback is scheduled when the session could not schedule one |
| `meeting` | It skips the calendar and invents a time, says a meeting is booked before `book_meeting` succeeded, or fails to confirm one after it did |
| `do_not_call` | The request is not honoured immediately, or it keeps talking |
| `do_not_call_implicit` | The *model* misses a request the phrase list does not catch (asserts `mark_do_not_call`) |
| `unknown_question` | It invents an integration, a certification or a policy |
| `interruption` | Barge-in broke under the Phase 6 pipeline (audio mode) |
| `vague_answers` | It records a timeline or an interest level nobody gave it |
| `unrelated_question` | It refuses small talk like a search engine, or invents an office |
| `wants_human` | It claims to be a person, or claims to be transferring the call |

`do_not_call` and `do_not_call_implicit` are a pair and the split is the design.
The first uses a phrasing `signals.py` recognises, so the state is forced before
the model reads the turn and there is no tool call left to assert — it checks the
behaviour. The second uses a phrasing no phrase list catches, so only the model
can act on it, and asserts the tool call. Both mechanisms are required and each
scenario proves one of them without the other propping it up.

**Before these, run `uv run python tests/test_conversation.py`,
`uv run python tests/test_actions.py` and `uv run python tests/test_results.py`.** Between them they cover the conversation
layer's own logic — every state transition, every field of the qualification
record, every detector — and every Phase 7 tool against a stubbed calendar,
store and carrier, in a couple of seconds, with no keys. A failure here after
those pass is a fact about the *model*, which is the only thing this suite can
tell you that the checks cannot.

**Phase 7 and the eval session.** The sales bot the suite spawns has a real
calendar behind it — the default local one, in the campaign database — so
`meeting` books a real row in `meetings` (run `uv run campaign.py init` once on
an existing database, and `uv run campaign.py meetings` to see what the suite
booked). It has *no prospect record* and *is not a phone call*, so
`schedule_callback` and `transfer_to_human` answer that they cannot act, and the
`callback` and `wants_human` scenarios assert that the agent then tells the
truth about it rather than that the action happened. The successful callback and
transfer paths are deterministic, and live in `tests/test_actions.py` and
`tests/test_campaigns.py`.

**The scenarios that involve a tool carry `within_ms: 150000` on those turns,
and that number is about Groq, not the bot.** A tool turn is two LLM requests
(the call, then the reply to its result), and on this organisation's free Groq
tier — 8,000 input tokens a minute against about 3,100 per request with twelve
tools advertised — the second request is held back until the minute resets. The
harness's default 60-second window was timing out on that wait. If you move to a
larger tier or another provider, tighten them again; HANDOFF.md has the
measurements.

The judge shares that budget: `groq_judge.py` uses the agent's own model by
default, so every judge call is a Groq request against the same per-minute limit,
and a scenario can fail with `judge call failed: RateLimitError` while the bot
did nothing wrong. Seen once on 2026-09-04 and again on 2026-09-11, when a
tool turn was refused on input tokens per minute while the judge was reading
the turn before it. Re-run before believing it, leave a minute between
scenarios, and put the judge on a model with its own limit: `EVAL_JUDGE_MODEL`
in the shell (a scenario's `judge.eval.model` still wins), for example

    EVAL_JUDGE_MODEL=openai/gpt-oss-20b EVAL_JUDGE_MAX_TOKENS=900       uv run python -m pipecat.evals suite evals/sales/suite.yaml ...

`EVAL_JUDGE_MAX_TOKENS` raises the harness's 200-token verdict cap for a
reasoning model that would otherwise spend it thinking and return nothing.

Two things the judge module deliberately does **not** do. It does not load
`.env` into the harness process — it reads `GROQ_API_KEY` from the file — because
the harness spawns the bots, and a bot spawned after the first judged run would
inherit the deployment's `SALES_*` from the environment, which `eval_env.py`
then keeps over the sample configuration (with `-r 2`, attempt 1 introduced
Meridian and attempt 2 introduced the deployment's company; 2026-09-11). And it
never lowers the token cap.

There is a daily ceiling as well — 200,000 tokens a day on this tier, about
sixty requests at the sales bot's prompt size — and one afternoon of Phase 7
verification used it up. When every scenario starts failing at the greeting with
`no response text yet`, check the bot's log for `tokens per day (TPD)` before
suspecting anything else.

`-s` on `suite` takes **one** scenario name; `-s a -s b` runs only `b`. Run the
suite once per scenario, or with no `-s` for all of them.

`-r N` runs each of them N times, which is worth doing before believing either a
pass or a failure — flaky and reliable look identical in one pass. `barge_in`
earned its place this way: it failed once, passed three times in a row, then
failed twice in a row, and only the repeats made it clear that the
post-interruption reply collapsing to two tokens was a real defect rather than
noise. The cause and the fix are in `mark_interrupted_reply` in `src/turns.py`.

## What they do *not* measure

Latency. A `within_ms` budget on a `response` event includes the whole spoken
utterance plus local transcription, so it measures the harness as much as the
bot. The real numbers come from the bot's own log — one `LATENCY` line per
response and a summary at the end of the session — because that measurement
starts from the moment the caller actually fell silent, which nothing outside the
pipeline can see. See `src/metrics.py`.

## Adding one

Copy the closest existing scenario. Reuse the shared `user_audio.yaml` and
`judge_audio.yaml` blocks via `!include` so a change to the voice or the judge
lands everywhere at once, and add the name to `suite.yaml`.

Write assertions about behaviour the caller could notice. `response` is the
transcription of what the bot really said and is almost always the right event to
assert on; `llm_response` skips the voice, and `tts_response` skips whether the
voice was intelligible.
