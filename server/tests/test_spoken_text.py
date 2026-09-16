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

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CARTESIA_API_KEY"):
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


async def main() -> int:
    print("Spoken-text checks — reasoning, tool markup and wordless sentences never reach the voice.")
    check_scrubber()
    await check_stage()
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
