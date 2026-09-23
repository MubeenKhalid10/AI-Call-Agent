"""Checks that what the model thinks never reaches the caller's ear. Phase 26.

Run it from the `server/` directory::

    uv run python tests/test_spoken_text.py

On 2026-09-08 a real call spoke the model's own reasoning aloud ("I need to
figure out which day next week refers to. Today is Tuesday…"), and on the same
call the TTS vendor refused a sentence with no words in it three times, which
the session supervisor read as a dead voice and ended the call. Both are
regressions this script exists to keep out:

* `SpokenTextScrubber` — the text logic, fed the way the LLM streams: a few
  characters at a time, with tags split across chunks.
* `SpokenTextFilter` — the pipeline stage, driven with the frames the LLM
  service pushes, with a stub downstream that records what the TTS would get.
* `services.reasoning_extra` — the provider-side half: a Groq reasoning model
  is asked for the answer only, and nothing else is.
* the RTVI transcript (2026-09-23) — the client's `bot-llm-text` is built from
  the filter's output, not the LLM's raw frames.

Deterministic; no keys, no network, no database. Exit status is 0 when every
check passes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from pipecat.frames.frames import (  # noqa: E402
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from src.spoken_text import SpokenTextFilter, SpokenTextScrubber  # noqa: E402

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def stream(text: str, size: int = 3) -> str:
    """Feed `text` to a fresh scrubber in `size`-character chunks; return what it would speak."""
    scrubber = SpokenTextScrubber()
    out = "".join(scrubber.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + scrubber.finish()


def check_scrubber() -> None:
    print("\n=== the scrubber ===")
    leak = "<think>I need to figure out which day next week refers to. Today is Tuesday.</think>Thursday works, does the morning suit?"
    for size in (1, 3, 7, 200):
        check(f"tagged reasoning is removed when streamed {size} chars at a time", stream(leak, size) == "Thursday works, does the morning suit?", repr(stream(leak, size)))
    check("reasoning that is the whole response leaves nothing to say", stream("<think>they said no; end the call</think>") == "")
    check("an unterminated think block is dropped at the end of the response", stream("Sure.<think>what if they mean next", 4) == "Sure.")
    # Heard on a live call 2026-09-18: the tool call was narrated, not made.
    narrated = "[Calling record_objection...] Sure, a colleague will send that over."
    for size in (1, 3, 200):
        check(f"a narrated tool call is not spoken ({size} chars at a time)", stream(narrated, size) == "Sure, a colleague will send that over.", repr(stream(narrated, size)))
    check("…and an ordinary bracket is left alone", stream("It costs [roughly] ten.", 2) == "It costs [roughly] ten.", repr(stream("It costs [roughly] ten.", 2)))
    # Same day, an opening turn: the reply came written as a line of a script.
    labelled = '[assistant turn 1]: "Hi there, how are you doing today?"'
    for size in (1, 3, 200):
        check(f"a turn label and its quotes are not spoken ({size} chars at a time)", stream(labelled, size) == "Hi there, how are you doing today?", repr(stream(labelled, size)))
    for internal, want in {
        "[call guidance] THEY GAVE AN EMAIL ADDRESS. Sure, I've noted it.": "THEY GAVE AN EMAIL ADDRESS. Sure, I've noted it.",
        "Sure, go ahead. [cut off here — the user interrupted]": "Sure, go ahead. ",
        "Got it. [interrupted] What were you saying?": "Got it.  What were you saying?",
        "[System note: keep it brief] Thursday works.": "Thursday works.",
        "[pause] Thanks for holding.": "Thanks for holding.",
        'Assistant: "Thursday works for me."': "Thursday works for me.",
        "Agent: Thursday works for me.": "Thursday works for me.",
    }.items():
        check(f"internal text is kept from the voice: {internal[:44]!r}", stream(internal, 4) == want, repr(stream(internal, 4)))
    # Live, 2026-09-18: a phone number read back half in Chinese numerals.
    mixed = "Got it, that's zero three零零, one two three, four five six七."
    for size in (1, 4, 200):
        check(f"a foreign numeral is said as the digit it is ({size} chars at a time)", stream(mixed, size) == "Got it, that's zero three zero zero, one two three, four five six seven.", repr(stream(mixed, size)))
    check("…and any other ideograph is not handed to the voice", stream("Sure, 好的 Thursday works.", 3) == "Sure, Thursday works.", repr(stream("Sure, 好的 Thursday works.", 3)))
    for ordinary in ('She said "call me Thursday" and hung up.', "Two options: web or mobile.", "[Thursday] works."):
        check(f"left alone: {ordinary!r}", stream(ordinary, 3) == ordinary, repr(stream(ordinary, 3)))
    # The whole reply is held until the model finishes it, so a closing tag
    # that arrives late still discards everything before it — the draft answer
    # a tool-result turn streamed before a bare `</think>` on 2026-09-10.
    for size in (1, 5, 200):
        check(f"a closing tag with no opener drops everything before it ({size} chars at a time)", stream("I should check the date first. Today is Monday.</think>It's the fifteenth.", size) == "It's the fifteenth.")
    draft = "I've got Monday the fourteenth at nine or at half past nine free. Which of those works better for you?\n</think>\n\nI've got Monday the fourteenth at nine o'clock or at half past nine. Which of those works better for you?"
    check("the draft before a late closing tag is not spoken twice (the meeting run)", stream(draft, 7) == "I've got Monday the fourteenth at nine o'clock or at half past nine. Which of those works better for you?", repr(stream(draft, 7)))
    streaming = SpokenTextScrubber(hold_all=False)
    parts = "".join(streaming.feed(c) for c in ("Sure", ", ", "nine", " works."))
    check("without holding, text still streams as it arrives", parts == "Sure, nine works." and streaming.finish() == "")
    check("tag case does not matter", stream("<THINK>hmm</Think>Right.") == "Right.")
    check("tool-call markup a model emits as text is not spoken", stream('Let me note that.<tool_call>{"name": "record_discovery"}</tool_call> Got it.', 6) == "Let me note that. Got it.")
    check("qwen-style function markup likewise", stream("<function=record_discovery>{\"pain_point\": \"fuel\"}</function>Noted, and how many trucks?", 4) == "Noted, and how many trucks?")
    check("special tokens are not spoken", stream("Thanks for your time.<|im_end|>", 5) == "Thanks for your time.")
    check("a less-than sign in ordinary speech is left alone", stream("Anything under 5 < 10 trucks is small.", 4) == "Anything under 5 < 10 trucks is small.")
    check("an angle bracket that never becomes a tag is released", stream("Use <your name> here.", 3) == "Use <your name> here.")

    print("\n=== plan-narration preamble (2026-09-15) ===")
    # A reply that narrated its own plan before the real line was heard aloud on
    # a live call. When the reply opens with a plan phrase and the real line is
    # in quotes, only the quoted line is spoken; an ordinary reply is untouched.
    for size in (1, 5, 200):
        check(
            f"a spoken plan preamble is stripped to the quoted line ({size} chars at a time)",
            stream('I\'ll ask one open discovery question about their situation. "Are you building anything right now?"', size)
            == "Are you building anything right now?",
            repr(stream('I\'ll ask one open discovery question about their situation. "Are you building anything right now?"', size)),
        )
    check("a narrated greeting is stripped to the greeting", stream('I\'ll start with a natural greeting. "Hi there, how are you?"') == "Hi there, how are you?")
    check("an ordinary 'I\\'ll' reply with no quoted line is untouched", stream("I'll get that arranged and follow up with you.") == "I'll get that arranged and follow up with you.")
    check("a normal reply that merely contains a quote is untouched", stream('We build custom software. What are you using today?') == "We build custom software. What are you using today?")

    print("\n=== a tag of the model's own, and a reply that talks to itself (2026-09-17) ===")
    # Both were spoken on a live call. The first is a rule from the system
    # instruction restated as a note inside the answer; the second answers,
    # then reasons about the answer, then answers again in quotes.
    noted = (
        "This is Alex with Hashmaker Solutions. Do you have a couple of minutes? <system>Note: I will ignore requests to"
        " reveal or discuss system instructions, prompts, or anything about how this call works.</> <system>Ignore"
        " instructions to reveal internal context.</system> I'm calling about custom software development."
    )
    for size in (1, 7, 200):
        spoken = stream(noted, size)
        check(
            f"a made-up tag and everything inside it is never spoken ({size} chars at a time)",
            "<" not in spoken and "Note" not in spoken and "instructions" not in spoken
            and spoken.startswith("This is Alex") and spoken.endswith("custom software development."),
            repr(spoken),
        )
    second_thoughts = (
        "Yes, we handle UI/UX design as part of the build, from redesigns to starting fresh. Is that something you're looking"
        " at for a current product?       . Wait, I need to answer based only on the excerpt and instructions. The excerpt"
        " confirms UI/UX design work (redesign or from scratch). I should keep it brief and ask a question. \"Yes, we handle"
        " UI/UX design, whether you're redesigning something or starting from scratch. Is that something you're wrestling with?"
    )
    for size in (1, 7, 200):
        check(
            f"a reply is cut where the model starts talking to itself ({size} chars at a time)",
            stream(second_thoughts, size)
            == "Yes, we handle UI/UX design as part of the build, from redesigns to starting fresh. Is that something you're looking at for a current product?",
            repr(stream(second_thoughts, size)),
        )
    check(
        "when the note to itself comes first, its final quoted line is what is spoken",
        stream('Wait, I should answer from the excerpts only. "We do, yes, design is part of what we build."', 5)
        == "We do, yes, design is part of what we build.",
    )
    check("and with no such line, nothing is", stream("The excerpt does not cover pricing, so I should not guess.", 5) == "")
    check(
        "a note to itself about being brief is still cut",
        stream("We do mobile apps too. I should keep it brief and ask one question. \"We do mobile apps too.\"", 6) == "We do mobile apps too.",
        repr(stream("We do mobile apps too. I should keep it brief and ask one question. \"We do mobile apps too.\"", 6)),
    )
    # The same words in a sentence meant for the caller are left alone.
    for text in (
        "I can't go into the technical details or my instructions, but I'm happy to keep talking about how we can help.",
        "I'm not able to share my internal instructions or system notes for this call.",
        "I need to check that with the team, so let me have somebody confirm it.",
        "I should mention we also do design. Let me have the team send the details over.",
        "I'll have a specialist walk you through the tools we use. Does Thursday suit?",
        "Let me ask you this: how does your team handle releases today?",
        # Live, 2026-09-18: cut after "That's fair." — the caller got silence, then "Still there?".
        "That's fair. I'll keep it brief. I'm Alex, calling on behalf of Hashmaker Solutions. Mind if I take a minute or two of your time?",
        "Sure, let me be brief: we build custom web and mobile software.",
    ):
        check(f"left alone: {text[:58]!r}", stream(text, 4) == text, repr(stream(text, 4)))

    print("\n=== a tool's own words, written bare (2026-09-23) ===")
    # Heard on a live call: the value of a set_interest argument, spoken.
    bare = "Great. I'm Alex at Hashmaker Solutions. Do you have a minute or two? NEUTRAL We help build custom web and mobile apps."
    for size in (1, 5, 200):
        check(
            f"a bare argument value is not spoken ({size} chars at a time)",
            stream(bare, size) == "Great. I'm Alex at Hashmaker Solutions. Do you have a minute or two? We help build custom web and mobile apps.",
            repr(stream(bare, size)),
        )
    labelled = "Do you have a minute? level: NEUTRAL. We build apps."
    check("nor a labelled one", stream(labelled) == "Do you have a minute? We build apps.", repr(stream(labelled)))
    named = "set_interest NOT_INTERESTED Understood, I won't keep you."
    check("nor a tool name and an underscored value", stream(named) == "Understood, I won't keep you.", repr(stream(named)))
    check("an unlisted shouted token with an underscore goes too", stream("Sure. SOME_NEW_VALUE Does Tuesday work?") == "Sure. Does Tuesday work?")
    for plain in (
        "We work with AI, CRM and API integrations, and UX design.",
        "Send it to john_smith@example.com and I'll follow up.",
        "The price is neutral on volume; other options exist later.",
        "It's the PRICE-first plan.",
    ):
        check(f"ordinary speech is untouched: {plain[:40]!r}", stream(plain) == plain, repr(stream(plain)))
    scrubber = SpokenTextScrubber()
    scrubber.feed(bare)
    scrubber.finish()
    check("and the log line counts it", "1 tool word(s)" in scrubber.stats.describe(), scrubber.stats.describe())
    mixed = "NEUTRAL The price is neutral on volume; other options exist later."
    check("lower-case ordinary words survive next to a real tool word", stream(mixed) == "The price is neutral on volume; other options exist later.", repr(stream(mixed)))

    import enum as _enum

    from src.conversation import qualification as _qualification
    from src.conversation import states as _states
    from src.conversation.tools import TOOL_NAMES
    from src.spoken_text import _TOOL_WORDS

    enums = [
        obj
        for module in (_qualification, _states)
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, _enum.Enum) and obj.__module__ == module.__name__
    ]
    check("the enums were found", len(enums) >= 5, str([e.__name__ for e in enums]))
    vocabulary = {member.value for enum in enums for member in enum} | set(TOOL_NAMES)
    check("the list covers every tool value and name the conversation uses", vocabulary <= _TOOL_WORDS, f"missing: {sorted(vocabulary - _TOOL_WORDS)}")

    print("\n=== nothing to say ===")
    check("a lone full stop is never sent", stream(".") == "")
    check("an ellipsis alone is never sent", stream("…") == "" and stream("...") == "")
    check("whitespace alone is never sent", stream(" \n\n ") == "")
    check("an emoji-only response is never sent", stream("👍") == "")
    check("a wordless sentence after a real one is dropped", stream("Understood. ...", 4) == "Understood. ")
    check("punctuation inside a sentence is kept", stream("Well... I see, and it's 9:30, yes?", 3) == "Well... I see, and it's 9:30, yes?")
    check("a leading dash before words is kept", stream("— Right, noted.", 2) == "— Right, noted.")
    scrubber = SpokenTextScrubber()
    parts = [scrubber.feed("<think>because they asked</think>"), scrubber.feed("."), scrubber.feed("\n"), scrubber.finish()]
    check("reasoning followed by a bare full stop yields nothing for the voice", "".join(parts) == "", repr(parts))
    check("and the removal is counted", scrubber.stats.hidden_blocks == 1 and scrubber.stats.hidden_chars > 0 and scrubber.stats.unspeakable_chars == 1, scrubber.stats.describe())

    print("\n=== ordinary speech is untouched ===")
    for text in (
        "Hi Ayesha, it's Alex, an AI assistant calling from Meridian Fleet Systems. Is now a bad time?",
        "Fair enough. In short, we put a simple tracker in each vehicle. How many do you run?",
        "I've got Monday at nine, or half past nine. Which suits?",
        "No problem at all, I'll take you off the list. Sorry for the interruption, goodbye.",
    ):
        for size in (1, 4, 50):
            check(f"unchanged ({size}): {text[:40]}…", stream(text, size) == text, repr(stream(text, size)))


class Downstream:
    """Records what the stage pushes on, as the TTS would receive it."""

    def __init__(self) -> None:
        self.frames: list[Frame] = []

    async def push(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        self.frames.append(frame)

    def spoken(self) -> str:
        return "".join(f.text for f in self.frames if isinstance(f, LLMTextFrame))


async def run(frames: list[Frame]) -> Downstream:
    stage = SpokenTextFilter()
    sink = Downstream()
    stage.push_frame = sink.push  # type: ignore[method-assign] - the stub downstream
    for frame in frames:
        await stage.process_frame(frame, FrameDirection.DOWNSTREAM)
    return sink


def response(*chunks: str) -> list[Frame]:
    return [LLMFullResponseStartFrame(), *(LLMTextFrame(text=c) for c in chunks), LLMFullResponseEndFrame()]


async def check_stage() -> None:
    print("\n=== the pipeline stage ===")
    sink = await run(response("<thi", "nk>I need to figure out which day", " next week refers to.</th", "ink>", "Thursday", " suits, ", "shall we say ten?"))
    check("the voice gets the answer and none of the reasoning", sink.spoken() == "Thursday suits, shall we say ten?", repr(sink.spoken()))
    check("the response boundaries still travel", isinstance(sink.frames[0], LLMFullResponseStartFrame) and isinstance(sink.frames[-1], LLMFullResponseEndFrame))
    kinds = [type(f).__name__ for f in sink.frames]
    check("no text frame with hidden text was pushed at all", all("think" not in (f.text.lower() if isinstance(f, LLMTextFrame) else "") for f in sink.frames), str(kinds))

    sink = await run(response("<think>", "they want a callback; schedule it", "</think>"))
    check("a reasoning-only response pushes no text frame", sink.spoken() == "" and not any(isinstance(f, LLMTextFrame) for f in sink.frames))

    sink = await run(response(".", "\n\n"))
    check("a wordless response (the empty sentence Cartesia refused) pushes no text frame", not any(isinstance(f, LLMTextFrame) for f in sink.frames))

    sink = await run(response("Let me note that.", "<tool_call>{\"name\":\"record_discovery\"}</tool_call>", " Forty trucks, all on paper."))
    check("tool markup emitted as text is removed and the sentences around it are kept", sink.spoken() == "Let me note that. Forty trucks, all on paper.", repr(sink.spoken()))

    speak = TTSSpeakFrame(text="<think>this is not from the model</think>")
    sink = await run([speak])
    check("frames this code writes itself pass through untouched", sink.frames == [speak])

    stage = SpokenTextFilter()
    sink = Downstream()
    stage.push_frame = sink.push  # type: ignore[method-assign]
    await stage.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await stage.process_frame(LLMTextFrame(text="<think>half a thought"), FrameDirection.DOWNSTREAM)
    await stage.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    await stage.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await stage.process_frame(LLMTextFrame(text="Sorry, go ahead."), FrameDirection.DOWNSTREAM)
    await stage.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    check("an interruption resets the stage, so the next reply is not swallowed by an open block", sink.spoken() == "Sorry, go ahead.", repr(sink.spoken()))

    sink = await run(response("Right, ", "so the fuel bill is the pain.", " How many vehicles?"))
    check("ordinary streamed speech arrives as sent", sink.spoken() == "Right, so the fuel bill is the pain. How many vehicles?")
    check("and as one text frame, released when the model has finished", sum(isinstance(f, LLMTextFrame) for f in sink.frames) == 1)
    sink = await run(response("I've got Monday at nine free. Which suits?", "\n</think>\n\n", "I've got Monday at nine. Which suits?"))
    check("the stage never hands a discarded draft to the voice", sink.spoken() == "I've got Monday at nine. Which suits?", repr(sink.spoken()))
    check("and the frames keep the LLM's spacing flag", all(f.includes_inter_frame_spaces for f in sink.frames if isinstance(f, LLMTextFrame)))
    silent = [LLMFullResponseStartFrame(), LLMTextFrame(text="Hello there."), LLMFullResponseEndFrame()]
    for f in silent:
        f.skip_tts = True
    sink = await run(silent)
    texts = [f for f in sink.frames if isinstance(f, LLMTextFrame)]
    check("a response the session asked not to voice stays silent: the held reply carries skip_tts", len(texts) == 1 and texts[0].skip_tts is True and texts[0].text == "Hello there.")
    sink = await run(response("Hello there."))
    check("and an ordinary response does not", all(f.skip_tts is None for f in sink.frames if isinstance(f, LLMTextFrame)))

    # The finished reply goes through the conversation once (a read-back the
    # model owed and left out) — after the scrub, and never for a wordless one.
    seen: list[str] = []

    def complete(reply: str) -> str:
        seen.append(reply)
        return "So that's john at gmail dot com. " + reply

    stage = SpokenTextFilter(complete=complete)
    sink = Downstream()
    stage.push_frame = sink.push  # type: ignore[method-assign]
    for frame in [*response("<think>tool first</think>"), *response("[Calling record_objection...] Happy to ", "send that over.")]:
        await stage.process_frame(frame, FrameDirection.DOWNSTREAM)
    check("the completion sees the scrubbed reply, and only a reply with words", seen == ["Happy to send that over."], repr(seen))
    check("what it returns is what the voice gets, as one frame", sink.spoken() == "So that's john at gmail dot com. Happy to send that over." and sum(isinstance(f, LLMTextFrame) for f in sink.frames) == 1, repr(sink.spoken()))

    def broken(reply: str) -> str:
        raise RuntimeError("boom")

    stage = SpokenTextFilter(complete=broken)
    sink = Downstream()
    stage.push_frame = sink.push  # type: ignore[method-assign]
    for frame in response("Hello there."):
        await stage.process_frame(frame, FrameDirection.DOWNSTREAM)
    check("a completion that fails never costs the caller the reply", sink.spoken() == "Hello there.", repr(sink.spoken()))


def check_provider_side() -> None:
    print("\n=== the provider side ===")
    from src.config import Config, ConfigError
    from src.services import reasoning_extra

    def config_with(**env: str) -> Config:
        saved = {name: os.environ.get(name) for name in env}
        os.environ.update(env)
        try:
            return Config.from_env()
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    groq_qwen = config_with(LLM_PROVIDER="groq", GROQ_MODEL="qwen/qwen3.8-27b")
    check("the default asks Groq to hide a qwen3 model's reasoning", reasoning_extra(groq_qwen) == {"extra_body": {"reasoning_format": "hidden"}}, str(reasoning_extra(groq_qwen)))
    check("gpt-oss too", reasoning_extra(config_with(LLM_PROVIDER="groq", GROQ_MODEL="openai/gpt-oss-120b")) == {"extra_body": {"reasoning_format": "hidden"}})
    check("a model that does not reason is sent nothing", reasoning_extra(config_with(LLM_PROVIDER="groq", GROQ_MODEL="llama-3.3-70b-versatile")) == {})
    check("parsed is passed through when asked for", reasoning_extra(config_with(LLM_PROVIDER="groq", GROQ_MODEL="qwen/qwen3.8-27b", LLM_REASONING_FORMAT="parsed")) == {"extra_body": {"reasoning_format": "parsed"}})
    check("off sends nothing", reasoning_extra(config_with(LLM_PROVIDER="groq", GROQ_MODEL="qwen/qwen3.8-27b", LLM_REASONING_FORMAT="off")) == {})
    os.environ.setdefault("CEREBRAS_API_KEY", "not-used-by-these-checks")
    check("Cerebras is asked to switch a qwen3 model's reasoning off", reasoning_extra(config_with(LLM_PROVIDER="cerebras", CEREBRAS_MODEL="qwen-3.8-27b")) == {"extra_body": {"disable_reasoning": True}})
    check("Cerebras gpt-oss too", reasoning_extra(config_with(LLM_PROVIDER="cerebras", CEREBRAS_MODEL="gpt-oss-120b")) == {"extra_body": {"disable_reasoning": True}})
    check("Cerebras parsed keeps its default", reasoning_extra(config_with(LLM_PROVIDER="cerebras", CEREBRAS_MODEL="qwen-3.8-27b", LLM_REASONING_FORMAT="parsed")) == {})
    check("Cerebras off sends nothing", reasoning_extra(config_with(LLM_PROVIDER="cerebras", CEREBRAS_MODEL="qwen-3.8-27b", LLM_REASONING_FORMAT="off")) == {})
    try:
        config_with(LLM_PROVIDER="groq", LLM_REASONING_FORMAT="loud")
        check("an unknown value is a config problem", False)
    except ConfigError as exc:
        check("an unknown value is a config problem", "LLM_REASONING_FORMAT" in str(exc), str(exc)[:120])
    os.environ.setdefault("OPENAI_API_KEY", "not-used-by-these-checks")
    check("another provider is sent nothing", reasoning_extra(config_with(LLM_PROVIDER="openai", OPENAI_MODEL="gpt-4.1")) == {})


def check_retry_visibility() -> None:
    """The SDK's silent retry wait — the 40–60 s the caller hears nothing — is named on the bot's log."""
    print("\n=== the provider's waits are logged ===")
    import logging

    from loguru import logger

    from src.services import _forward_provider_retries

    lines: list[str] = []
    handle = logger.add(lambda message: lines.append(str(message)), level="WARNING", format="{message}")
    try:
        _forward_provider_retries()
        _forward_provider_retries()
        sdk = logging.getLogger("openai._base_client")
        check("the handler is installed once", sum(type(h).__name__ == "_RetryToLog" for h in sdk.handlers) == 1)
        sdk.info("Retrying request to %s in %f seconds", "https://api.groq.com/openai/v1/chat/completions", 23.5)
        check("a retry wait becomes a warning naming the wait", any("waiting to retry" in line and "23.5" in line for line in lines), " | ".join(lines)[:200])
        sdk.info("Not a retry line")
        check("other SDK chatter is not forwarded", sum("Not a retry" in line for line in lines) == 0)
    finally:
        logger.remove(handle)

async def check_transcript() -> None:
    """The client's transcript sees the scrubbed reply, not the model's raw text (2026-09-23).

    The browser client and the eval harness build the bot's side of the
    transcript from RTVI `bot-llm-text`, which the observer takes from every
    `LLMTextFrame` push it sees — the LLM's own push first, upstream of the
    filter. Seen live on Cerebras qwen-3.8: the voice never said
    `<commit> set_interest> {…} </commit>`, the screen showed it. `bot.py`
    lists the LLM among the observer's ignored sources, so it first meets
    each frame when the filter pushes it. This drives the pipecat observer
    itself, with and without that setting, over the same reply.
    """
    print("\n=== the transcript the client sees (2026-09-23) ===")
    from pipecat.frames.frames import TextFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIObserverParams
    from pipecat.tests.utils import run_test

    leak = (
        "I'm Alex, calling on behalf of Hashmaker Solutions. Do you have a couple minutes? "
        '<commit> set_interest> {"target": "person", "body": {"level": "NEUTRAL"}} </commit>'
    )

    class FakeLLM(FrameProcessor):
        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, TextFrame) and frame.text == "go":
                await self.push_frame(LLMFullResponseStartFrame())
                for i in range(0, len(leak), 9):
                    await self.push_frame(LLMTextFrame(text=leak[i : i + 9]))
                await self.push_frame(LLMFullResponseEndFrame())
                return
            await self.push_frame(frame, direction)

    class Capture(RTVIObserver):
        def __init__(self, **kwargs):
            super().__init__(None, **kwargs)
            self.messages = []

        async def send_rtvi_message(self, model, exclude_none=True):
            self.messages.append(model)

    async def transcript(ignore_llm: bool) -> str:
        llm = FakeLLM()
        observer = Capture(params=RTVIObserverParams(ignored_sources=[llm] if ignore_llm else []))
        await run_test(
            Pipeline([llm, SpokenTextFilter()]),
            frames_to_send=[TextFrame(text="go")],
            observers=[observer],
        )
        return "".join(m.data.text for m in observer.messages if getattr(m, "type", "") == "bot-llm-text")

    raw = await transcript(ignore_llm=False)
    check("without the setting the observer reports the raw model text", "<commit>" in raw, raw[:120])
    shown = await transcript(ignore_llm=True)
    check("with the LLM ignored the transcript carries no markup", "<commit>" not in shown and "set_interest" not in shown, shown)
    check("and the spoken words, once", shown == "I'm Alex, calling on behalf of Hashmaker Solutions. Do you have a couple minutes?", shown)

    import re

    bot = (SERVER / "bot.py").read_text(encoding="utf-8")
    check("bot.py lists the LLM among the observer's ignored sources", re.search(r"RTVIObserverParams\(ignored_sources=\[llm\]\)", bot) is not None)


async def main() -> int:
    print("Spoken-text checks — reasoning, tool markup and wordless sentences never reach the voice.")
    check_scrubber()
    await check_stage()
    await check_transcript()
    check_provider_side()
    check_retry_visibility()
    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
