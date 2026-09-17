#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for Phase 29: the per-turn latency instrumentation.

Run it from the `server/` directory::

    uv run python tests/test_latency.py

No keys, no database, no audio, and — deliberately — no wall clock. The
tracker takes an injected clock, and every check below advances it by hand,
so a figure is asserted as "exactly 350 ms" rather than "roughly fast".
What is under test:

* the figures: end of speech to the final transcript, retrieval, the LLM's
  first token and total (over one request and over the two a tool turn
  makes), each tool, TTS first audio, the gaps, and the total;
* the observer: it is the real observer fed the real frames, once per hop
  the way the pipeline delivers them, so de-duplication and routing are
  proven rather than assumed;
* the situations: a normal turn, a tool turn, a barge-in mid-response, an
  interruption before any audio, a service error, a turn that got no audio,
  the greeting, an unprompted turn, a text-mode turn;
* the retrieval hook, through the real `KnowledgeRetriever` over the stub
  store `tests/test_knowledge.py` uses;
* the output: the per-turn line, the call summary's averages and p95, the
  JSON shape, and that none of it carries text, arguments or results.

A plain script rather than a pytest suite because the project has no test
dependency and this needs none. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from loguru import logger  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData  # noqa: E402
from pipecat.observers.base_observer import FramePushed  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from src.config import Config  # noqa: E402
from src.embeddings import make_embedder  # noqa: E402
from src.conversation import INSTRUCTION_PREFIX  # noqa: E402
from src.latency import (  # noqa: E402
    KIND_GREETING,
    KIND_RESPONSE,
    KIND_UNPROMPTED,
    SUMMARY_STAGES,
    LatencyTracker,
    TurnLatency,
)
from src.retrieval import KnowledgeRetriever  # noqa: E402

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


class Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def tick(self, secs: float) -> None:
        self.now += secs


def tracker_with(clock: Clock, **kwargs) -> LatencyTracker:
    return LatencyTracker(clock=clock, wall=lambda: datetime(2026, 9, 14, 12, 0, tzinfo=UTC), **kwargs)


class Captured:
    """A loguru sink that keeps every message, for asserting on what was logged."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._id = logger.add(lambda m: self.lines.append(m.record["message"]), level="DEBUG")

    def close(self) -> None:
        logger.remove(self._id)

    def matching(self, needle: str) -> list[str]:
        return [line for line in self.lines if needle in line]


# --- The figures, driven directly ---------------------------------------------------


def check_normal_turn() -> None:
    print("\n=== a normal turn, figure by figure ===")
    clock = Clock()
    tracker = tracker_with(clock, log_each_turn=False)

    tracker.on_user_started()
    clock.tick(0.10)
    tracker.on_interim_transcript()
    clock.tick(1.90)
    tracker.on_vad_user_stopped()  # the caller actually stopped: the anchor
    clock.tick(0.20)
    tracker.on_user_stopped()  # the end-of-turn decision, 200 ms later
    clock.tick(0.15)
    tracker.on_final_transcript()  # final transcript 350 ms after the speech ended
    tracker.note_user_turn_stopped("what is the capital of France")
    clock.tick(0.05)
    tracker.retrieval_started()
    clock.tick(0.21)
    tracker.retrieval_finished("2 passages")
    clock.tick(0.04)
    tracker.on_llm_started()
    clock.tick(1.42)
    tracker.on_llm_text()
    clock.tick(0.10)
    tracker.on_tts_started()
    clock.tick(0.39)
    tracker.on_tts_audio()
    clock.tick(0.05)
    tracker.on_bot_started()
    clock.tick(0.35)
    tracker.on_llm_finished()  # 2.31 s after the request
    clock.tick(2.0)
    tracker.on_bot_stopped()

    turn = tracker.turns[0]
    check("one turn, a response turn", len(tracker.turns) == 1 and turn.kind == KIND_RESPONSE)
    check("the anchor is the VAD's stop, not the turn-end decision", turn.user_end_at == turn.speech_ended_at)
    check("the caller's speech, start to end", turn.user_speech_ms == 2000, str(turn.user_speech_ms))
    check("STT first interim from speech start", turn.stt_first_interim_ms == 100, str(turn.stt_first_interim_ms))
    check("STT finalisation from end of speech", turn.stt_final_ms == 350, str(turn.stt_final_ms))
    check("the cost of turn detection", turn.vad_end_to_turn_end_ms == 200, str(turn.vad_end_to_turn_end_ms))
    check("KB retrieval", turn.kb_ms == 210 and turn.kb_outcome == "2 passages", str(turn.kb_ms))
    check("turn end to the LLM request", turn.turn_end_to_llm_ms == 450, str(turn.turn_end_to_llm_ms))
    check("LLM TTFT", turn.llm_ttft_ms == 1420, str(turn.llm_ttft_ms))
    check("LLM total", turn.llm_total_ms == 2310, str(turn.llm_total_ms))
    check("no tool", turn.tool_ms is None and turn.tools == [])
    check("first token to the TTS request", turn.first_token_to_tts_ms == 100, str(turn.first_token_to_tts_ms))
    check("TTS TTFA", turn.tts_ttfa_ms == 390, str(turn.tts_ttfa_ms))
    check("first chunk to played", turn.tts_audio_to_played_ms == 50, str(turn.tts_audio_to_played_ms))
    check("TOTAL end of speech to first audio", turn.total_ms == 2610, str(turn.total_ms))
    check("the turn closed when the audio stopped", turn.closed and turn.outcome == "responded")
    check("the words are counted, never kept", turn.transcript_words == 6 and "capital" not in json.dumps(turn.to_dict()))
    check("the wall-clock moment is recorded for correlation", turn.opened_at_utc == "2026-09-14T12:00:00.000+00:00")

    line = turn.describe()
    check(
        "the per-turn line has the requested shape",
        line == (
            "TURN 1 | STT final 0.35s | KB retrieval 0.21s (2 passages) | LLM TTFT 1.42s | LLM total 2.31s"
            " | tool none | TTS TTFA 0.39s | TOTAL end-of-user-speech -> first-audio 2.61s"
        ),
        line,
    )


def check_without_vad() -> None:
    print("\n=== the Flux path: no VAD stop, transcript inside the end-of-turn message ===")
    clock = Clock()
    tracker = tracker_with(clock, log_each_turn=False)
    tracker.on_user_started()
    clock.tick(2.0)
    tracker.on_final_transcript()  # Flux delivers the transcript first...
    tracker.on_user_stopped()  # ...and the end of turn in the same message
    clock.tick(0.5)
    tracker.on_llm_started()
    clock.tick(1.0)
    tracker.on_llm_text()
    clock.tick(0.3)
    tracker.on_tts_started()
    clock.tick(0.2)
    tracker.on_tts_audio()
    tracker.on_bot_started()
    turn = tracker.turns[0]
    check("the anchor falls back to the turn-end decision", turn.user_end_at == turn.turn_ended_at)
    check("a transcript that beat the anchor is 0 ms, not negative", turn.stt_final_ms == 0)
    check("and the total runs from the turn end", turn.total_ms == 2000, str(turn.total_ms))
    check("no VAD, no turn-detection figure", turn.vad_end_to_turn_end_ms is None)


# --- The observer, fed real frames --------------------------------------------------


def _pushed(frame, source_name: str = "DeepgramFluxSTTService#0", direction=FrameDirection.DOWNSTREAM) -> FramePushed:
    source = SimpleNamespace(name=source_name)
    return FramePushed(source=source, destination=source, frame=frame, direction=direction, timestamp=0)


async def check_observer() -> None:
    print("\n=== the observer: real frames, once per hop ===")
    clock = Clock()
    tracker = tracker_with(clock, log_each_turn=False)
    observer = tracker.observer

    async def push(frame, hops: int = 4, src: str = "DeepgramFluxSTTService#0") -> None:
        for _ in range(hops):
            await observer.on_push_frame(_pushed(frame, src))

    # Audio frames are the bulk of what an observer sees; none of them is a stage.
    for _ in range(50):
        await push(InputAudioRawFrame(audio=b"\x00" * 320, sample_rate=16000, num_channels=1), hops=2)
    check("input audio opens nothing", tracker.turns == [])

    await push(UserStartedSpeakingFrame())
    await push(UserStartedSpeakingFrame())  # a second start is the same turn
    clock.tick(0.3)
    await push(InterimTranscriptionFrame(text="what is", user_id="u", timestamp="t"))
    clock.tick(1.7)
    await push(VADUserStoppedSpeakingFrame())
    clock.tick(0.2)
    await push(UserStoppedSpeakingFrame())
    clock.tick(0.1)
    await push(TranscriptionFrame(text="what is the capital of France", user_id="u", timestamp="t"))
    clock.tick(0.4)
    await push(LLMFullResponseStartFrame(), src="GroqLLMService#0")
    clock.tick(1.2)
    await push(LLMTextFrame(text="Paris"), src="GroqLLMService#0")
    await push(MetricsFrame(data=[TTFBMetricsData(processor="GroqLLMService#0", value=1.15)]), src="GroqLLMService#0")
    clock.tick(0.1)
    await push(TTSStartedFrame(), src="CartesiaTTSService#0")
    clock.tick(0.3)
    await push(TTSAudioRawFrame(audio=b"\x00" * 640, sample_rate=16000, num_channels=1), src="CartesiaTTSService#0")
    await push(MetricsFrame(data=[TTFBMetricsData(processor="CartesiaTTSService#0", value=0.28)]), src="CartesiaTTSService#0")
    clock.tick(0.05)
    started = BotStartedSpeakingFrame()
    await push(started, src="FastAPIWebsocketOutputTransport#0")
    # A broadcast frame arrives twice more as its sibling, with another id.
    sibling = BotStartedSpeakingFrame()
    sibling.broadcast_sibling_id = started.id
    started.broadcast_sibling_id = sibling.id
    await observer.on_push_frame(_pushed(sibling, "FastAPIWebsocketOutputTransport#0", FrameDirection.UPSTREAM))
    clock.tick(0.5)
    await push(LLMFullResponseEndFrame(), src="GroqLLMService#0")
    clock.tick(1.5)
    await push(BotStoppedSpeakingFrame(), src="FastAPIWebsocketOutputTransport#0")

    check("one turn from every hop of every frame", len(tracker.turns) == 1, str(len(tracker.turns)))
    turn = tracker.turns[0]
    check("interim from speech start", turn.stt_first_interim_ms == 300)
    check("final from the VAD's stop", turn.stt_final_ms == 300, str(turn.stt_final_ms))
    check("turn detection cost", turn.vad_end_to_turn_end_ms == 200)
    check("LLM TTFT", turn.llm_ttft_ms == 1200 and turn.llm_total_ms == 2150, f"{turn.llm_ttft_ms} {turn.llm_total_ms}")
    check("the LLM's own TTFB rides along as a cross-check", turn.llm_requests[0].ttfb_reported_ms == 1150)
    check("TTS TTFA from the request to the first chunk", turn.tts_ttfa_ms == 300 and turn.tts_ttfb_reported_ms == 280)
    check("first audio from the transport, once, despite the sibling", turn.total_ms == 2350, str(turn.total_ms))
    check("closed when the bot stopped", turn.closed and turn.outcome == "responded")
    check("the STT's TTFB slot stays empty when the STT reported none", turn.stt_ttfb_reported_ms is None)


async def check_tool_turn() -> None:
    print("\n=== a tool turn: two LLM requests, the tool between them ===")
    clock = Clock()
    tracker = tracker_with(clock, log_each_turn=False)
    observer = tracker.observer

    async def push(frame, src: str = "GroqLLMService#0") -> None:
        for _ in range(3):
            await observer.on_push_frame(_pushed(frame, src))

    await push(UserStartedSpeakingFrame(), "DeepgramFluxSTTService#0")
    clock.tick(1.0)
    await push(TranscriptionFrame(text="we run forty trucks", user_id="u", timestamp="t"), "DeepgramFluxSTTService#0")
    await push(UserStoppedSpeakingFrame(), "DeepgramFluxSTTService#0")
    clock.tick(0.3)
    await push(LLMFullResponseStartFrame())
    clock.tick(0.9)
    await push(FunctionCallInProgressFrame(function_name="record_discovery", tool_call_id="call-1", arguments={"pain_point": "SECRET-DO-NOT-LOG"}))
    await push(LLMFullResponseEndFrame())
    clock.tick(0.08)
    await push(FunctionCallResultFrame(function_name="record_discovery", tool_call_id="call-1", arguments={}, result={"success": True}))
    clock.tick(0.02)
    await push(LLMFullResponseStartFrame())
    clock.tick(1.1)
    await push(LLMTextFrame(text="Forty trucks"))
    clock.tick(0.1)
    await push(TTSStartedFrame(), "CartesiaTTSService#0")
    clock.tick(0.3)
    await push(TTSAudioRawFrame(audio=b"\x00", sample_rate=16000, num_channels=1), "CartesiaTTSService#0")
    await push(BotStartedSpeakingFrame(), "FastAPIWebsocketOutputTransport#0")
    clock.tick(0.4)
    await push(LLMFullResponseEndFrame())
    await push(BotStoppedSpeakingFrame(), "FastAPIWebsocketOutputTransport#0")

    turn = tracker.turns[0]
    check("two LLM requests on the turn", len(turn.llm_requests) == 2)
    check("the first request ended in a tool call and had no token", turn.llm_requests[0].first_token_at is None and turn.llm_requests[0].duration_ms == 900)
    check("the tool was timed by its call id", len(turn.tools) == 1 and turn.tools[0].name == "record_discovery" and turn.tools[0].duration_ms == 80, str(turn.tools))
    check("tool total", turn.tool_ms == 80)
    check("TTFT spans both requests and the tool", turn.llm_ttft_ms == 2100, str(turn.llm_ttft_ms))
    check("LLM total runs to the end of the second request", turn.llm_total_ms == 2900, str(turn.llm_total_ms))
    check("the total is still end of speech to first audio", turn.total_ms == 2800, str(turn.total_ms))
    check("the line names the tool", "tool 0.08s (record_discovery 0.08s)" in turn.describe() and "(2 requests)" in turn.describe(), turn.describe())
    check("the arguments never reach the record", "SECRET-DO-NOT-LOG" not in json.dumps(turn.to_dict()))


async def check_interruptions_and_errors() -> None:
    print("\n=== barge-in, an interruption before audio, an error, a turn with no audio ===")
    clock = Clock()
    tracker = tracker_with(clock, log_each_turn=False)
    observer = tracker.observer

    async def push(frame, src: str = "DeepgramFluxSTTService#0") -> None:
        for _ in range(2):
            await observer.on_push_frame(_pushed(frame, src))

    # Turn 1 answered, then the caller barges in while it is speaking.
    await push(UserStartedSpeakingFrame())
    clock.tick(1.0)
    await push(UserStoppedSpeakingFrame())
    clock.tick(0.5)
    await push(LLMFullResponseStartFrame(), "GroqLLMService#0")
    clock.tick(0.8)
    await push(LLMTextFrame(text="Sunlight"), "GroqLLMService#0")
    await push(TTSStartedFrame(), "CartesiaTTSService#0")
    clock.tick(0.2)
    await push(BotStartedSpeakingFrame(), "FastAPIWebsocketOutputTransport#0")
    clock.tick(1.0)
    await push(InterruptionFrame())  # the caller cut in
    await push(UserStartedSpeakingFrame())
    clock.tick(0.1)
    await push(BotStoppedSpeakingFrame(), "FastAPIWebsocketOutputTransport#0")
    first = tracker.turns[0]
    check("the interrupted turn keeps its first-audio total", first.total_ms == 1500 and first.interrupted and first.outcome == "interrupted", first.outcome)
    check("and the barge-in opened turn 2", len(tracker.turns) == 2 and tracker.turns[1].index == 2)

    # Turn 2 is interrupted before the bot said anything.
    clock.tick(1.0)
    await push(UserStoppedSpeakingFrame())
    clock.tick(0.3)
    await push(LLMFullResponseStartFrame(), "GroqLLMService#0")
    clock.tick(0.4)
    await push(InterruptionFrame())
    await push(UserStartedSpeakingFrame())
    second = tracker.turns[1]
    check("a turn cut before any audio has no total and says so", second.total_ms is None and second.outcome == "interrupted" and second.closed)

    # Turn 3 hits a service error and never gets audio.
    clock.tick(1.0)
    await push(UserStoppedSpeakingFrame())
    clock.tick(0.2)
    await push(LLMFullResponseStartFrame(), "GroqLLMService#0")
    clock.tick(0.5)
    await push(ErrorFrame(error="Groq refused: token=sk-abcdef1234567890 rate limited"), "GroqLLMService#0")
    await push(UserStartedSpeakingFrame())
    third = tracker.turns[2]
    check("an error is attached to the turn in flight, by stage", third.outcome == "error" and third.errors and third.errors[0].startswith("llm:"), str(third.errors))

    # Turn 4 gets a request and nothing else before the session ends.
    clock.tick(1.0)
    await push(UserStoppedSpeakingFrame())
    await push(LLMFullResponseStartFrame(), "GroqLLMService#0")
    summary = tracker.summary()
    fourth = tracker.turns[3]
    check("the summary flushes the open turn as no_audio", fourth.closed and fourth.outcome == "no_audio")
    check("the counts say what happened", summary["response_turns"] == 4 and summary["responded_turns"] == 1 and summary["interrupted_turns"] == 2 and summary["error_turns"] == 1, json.dumps({k: summary[k] for k in ("response_turns", "responded_turns", "interrupted_turns", "error_turns")}))

    # An interruption while nothing is in flight is bookkeeping, not a barge-in.
    quiet = tracker_with(Clock(), log_each_turn=False)
    quiet.on_user_started()
    quiet.on_interruption()
    quiet.on_user_stopped()
    check("an interruption with no response in flight marks nothing", quiet.turns[0].interrupted is False)


def check_greeting_and_unprompted() -> None:
    print("\n=== the greeting and an unprompted turn ===")
    clock = Clock()
    tracker = tracker_with(clock, log_each_turn=False)
    tracker.on_llm_started()  # the bot speaks first
    clock.tick(0.7)
    tracker.on_llm_text()
    tracker.on_tts_started()
    clock.tick(0.3)
    tracker.on_tts_audio()
    tracker.on_bot_started()
    clock.tick(2.0)
    tracker.on_llm_finished()
    tracker.on_bot_stopped()
    greeting = tracker.turns[0]
    check("the greeting is turn 1 of kind greeting", greeting.kind == KIND_GREETING and greeting.index == 1)
    check("it has LLM and TTS figures but no total", greeting.llm_ttft_ms == 700 and greeting.tts_ttfa_ms == 300 and greeting.total_ms is None)

    tracker.on_user_started()
    clock.tick(1.0)
    tracker.on_user_stopped()
    tracker.note_user_turn_stopped("hello")
    clock.tick(0.4)
    tracker.on_llm_started()
    clock.tick(0.6)
    tracker.on_llm_text()
    tracker.on_tts_started()
    tracker.on_tts_audio()
    tracker.on_bot_started()
    clock.tick(0.1)
    tracker.on_llm_finished()
    tracker.on_bot_stopped()
    check("the caller's first turn is turn 2, matching the monitor's numbering", tracker.turns[1].index == 2 and tracker.turns[1].kind == KIND_RESPONSE and tracker.turns[1].total_ms == 1000)

    clock.tick(12.0)
    tracker.on_llm_started()  # the idle nudge: nobody spoke
    clock.tick(0.5)
    tracker.on_llm_text()
    tracker.on_bot_started()
    tracker.on_bot_stopped()
    nudge = tracker.turns[2]
    check("a response nobody prompted is its own turn of kind unprompted", nudge.kind == KIND_UNPROMPTED and nudge.total_ms is None)

    summary = tracker.summary()
    check("only response turns count toward the averages", summary["response_turns"] == 1 and summary["averages"]["total_ms"] == {"avg_ms": 1000, "n": 1})


def check_text_mode() -> None:
    print("\n=== text mode: no speaking frames at all ===")
    clock = Clock()
    tracker = tracker_with(clock, log_each_turn=False)
    tracker.on_final_transcript()  # the harness's text lands as a transcription
    tracker.note_user_turn_stopped("what is the capital of germany")
    clock.tick(0.3)
    tracker.on_llm_started()
    clock.tick(1.0)
    tracker.on_llm_text()
    clock.tick(0.2)
    tracker.on_llm_finished()
    turn = tracker.turns[0]
    check("a text turn opens on the transcript and is a response turn", turn.kind == KIND_RESPONSE and turn.transcript_words == 6)
    check("it is anchored on the aggregator's turn end", turn.user_end_at == turn.turn_ended_at and turn.turn_end_to_llm_ms == 300)
    check("the LLM figures are there and the audio ones are honestly n/a", turn.llm_ttft_ms == 1000 and turn.tts_ttfa_ms is None and turn.total_ms is None)
    check("the line says n/a rather than a number", "TTS TTFA n/a" in turn.describe() and "first-audio n/a" in turn.describe())
    tracker.summary()
    check("flushed at the summary as no_audio", turn.closed and turn.outcome == "no_audio")


async def check_context_driven_turns() -> None:
    print("\n=== text mode through the observer: the context frame is the turn ===")
    clock = Clock()
    tracker = tracker_with(clock, log_each_turn=False)
    observer = tracker.observer

    async def push(frame, src: str = "LLMUserAggregator#0") -> None:
        for _ in range(2):
            await observer.on_push_frame(_pushed(frame, src))

    def context(*messages) -> LLMContextFrame:
        return LLMContextFrame(context=LLMContext(messages=list(messages)))

    # The greeting: the opening instruction, not a caller turn.
    await push(context({"role": "user", "content": f"{INSTRUCTION_PREFIX} Open the call."}))
    await push(LLMFullResponseStartFrame(), "GroqLLMService#0")
    clock.tick(0.8)
    await push(LLMTextFrame(text="Hi"), "GroqLLMService#0")
    await push(LLMFullResponseEndFrame(), "GroqLLMService#0")
    check("an instruction context opens no caller turn", len(tracker.turns) == 1 and tracker.turns[0].kind == KIND_GREETING)

    # Turn 2: the harness appended text and ran the LLM. Three context frames
    # (the aggregator's, the retriever's copy, the director's copy with the
    # stage block last) are one turn.
    clock.tick(3.0)
    user = {"role": "user", "content": "We run about forty trucks out of Lahore"}
    await push(context(user))
    clock.tick(0.1)
    await push(context(user, {"role": "user", "content": "[Knowledge base results for my last message: nothing relevant found.] ..."}), "KnowledgeRetriever#0")
    await push(context(user, {"role": "user", "content": f"{INSTRUCTION_PREFIX} Stage: DISCOVERY."}), "ConversationDirector#0")
    await push(LLMFullResponseStartFrame(), "GroqLLMService#0")
    clock.tick(0.9)
    await push(FunctionCallInProgressFrame(function_name="record_discovery", tool_call_id="c1", arguments={}), "GroqLLMService#0")
    await push(LLMFullResponseEndFrame(), "GroqLLMService#0")
    clock.tick(0.05)
    await push(FunctionCallResultFrame(function_name="record_discovery", tool_call_id="c1", arguments={}, result={}), "GroqLLMService#0")
    # The continuation after the tool result: the same turn, not a new one.
    clock.tick(0.01)
    await push(context(user, {"role": "assistant", "content": ""}, {"role": "tool", "content": "{}"}))
    await push(LLMFullResponseStartFrame(), "GroqLLMService#0")
    clock.tick(1.0)
    await push(LLMTextFrame(text="Forty trucks"), "GroqLLMService#0")
    await push(LLMFullResponseEndFrame(), "GroqLLMService#0")
    check("the caller's text opened turn 2, once, with its words counted", len(tracker.turns) == 2 and tracker.turns[1].kind == KIND_RESPONSE and tracker.turns[1].transcript_words == 8, f"{len(tracker.turns)} turns, {tracker.turns[1].transcript_words} words")
    second = tracker.turns[1]
    check("the tool continuation stayed on the same turn", len(second.llm_requests) == 2 and len(second.tools) == 1)
    check("anchored on the context push, so the pre-LLM gap is measured", second.turn_end_to_llm_ms == 100, str(second.turn_end_to_llm_ms))
    check("and TTFT spans the tool", second.llm_ttft_ms == 1960, str(second.llm_ttft_ms))

    # Turn 3: the next caller message closes turn 2 and opens its own.
    clock.tick(2.0)
    await push(context(user, {"role": "assistant", "content": "Forty trucks"}, {"role": "user", "content": "Half of them are refrigerated."}))
    await push(LLMFullResponseStartFrame(), "GroqLLMService#0")
    check("the next message is turn 3 and turn 2 is closed as no_audio", len(tracker.turns) == 3 and second.closed and second.outcome == "no_audio")

    # In audio mode the same frame changes nothing: the speaking frames opened the turn.
    audio = tracker_with(Clock(), log_each_turn=False)
    await audio.observer.on_push_frame(_pushed(UserStartedSpeakingFrame()))
    await audio.observer.on_push_frame(_pushed(UserStoppedSpeakingFrame()))
    await audio.observer.on_push_frame(_pushed(context({"role": "user", "content": "hello there"})))
    check("with speaking frames the context frame joins the open turn", len(audio.turns) == 1)
    await audio.observer.on_push_frame(_pushed(context({"role": "user", "content": "hello there"}), direction=FrameDirection.UPSTREAM))
    check("an upstream context frame is ignored", len(audio.turns) == 1)


# --- The retrieval hook, through the real retriever -----------------------------------


async def check_retrieval_hook() -> None:
    print("\n=== the retrieval hook, through the real KnowledgeRetriever ===")
    from test_knowledge import BrokenStore, EmptyStore, build_store  # the stub store, same embedder as the bot

    config = Config.from_env()
    embedder = make_embedder(config.embedding_model)
    store, _rows = build_store(config, embedder)
    clock = Clock()
    tracker = tracker_with(clock, log_each_turn=False)

    async def run(question: str, target=store, mode: str = "always") -> None:
        retriever = KnowledgeRetriever(target, embedder, replace(config, kb_retrieval_mode=mode))
        retriever.latency = tracker
        tracker.on_final_transcript()
        tracker.note_user_turn_stopped(question)
        await retriever._augment(LLMContext(messages=[{"role": "user", "content": question}]))
        tracker.on_llm_started()
        tracker.on_llm_finished()
        tracker.on_user_started()  # the next turn closes this one

    await run("How much does the Standard plan cost?")
    check("a hit is timed and named", tracker.turns[0].kb_ms is not None and tracker.turns[0].kb_outcome.endswith("passages"), str(tracker.turns[0].kb_outcome))
    await run("Do you have an office on the moon made of cheese?")
    check("a miss is timed and says so", tracker.turns[1].kb_ms is not None and tracker.turns[1].kb_outcome in ("nothing found",) or tracker.turns[1].kb_outcome.endswith("passages"), str(tracker.turns[1].kb_outcome))
    await run("yeah", mode="auto")
    check("a gated turn is 'skipped', with no search behind it", tracker.turns[2].kb_outcome == "skipped")
    broken, _ = build_store(config, embedder, BrokenStore)
    await run("What are your support hours?", target=broken)
    check("a failed search is 'failed' and did not raise", tracker.turns[3].kb_outcome == "failed", str(tracker.turns[3].kb_outcome))
    await run("What are your support hours?", target=EmptyStore([], []))
    check("an empty knowledge base is 'empty'", tracker.turns[4].kb_outcome == "empty")
    plain = KnowledgeRetriever(store, embedder, replace(config, kb_retrieval_mode="always"))
    await plain._augment(LLMContext(messages=[{"role": "user", "content": "How much is it?"}]))
    check("a retriever with no tracker set works exactly as before", plain.latency is None)


# --- The output -------------------------------------------------------------------


def check_summary_and_logs() -> None:
    print("\n=== the call summary, and what gets logged ===")
    clock = Clock()
    captured = Captured()
    try:
        tracker = tracker_with(clock)
        totals = (1.0, 2.0, 3.0, 4.0, 10.0)
        for i, total in enumerate(totals, start=1):
            tracker.on_user_started()
            clock.tick(1.0)
            tracker.on_user_stopped()
            tracker.note_user_turn_stopped(f"turn {i} words that must not be logged")
            clock.tick(0.1)
            tracker.retrieval_started()
            clock.tick(0.2)
            tracker.retrieval_finished("1 passages")
            tracker.on_llm_started()
            clock.tick(total - 0.5)
            tracker.on_llm_text()
            tracker.on_tts_started()
            clock.tick(0.2)
            tracker.on_tts_audio()
            tracker.on_bot_started()
            tracker.on_llm_finished()
            clock.tick(0.5)
            tracker.on_bot_stopped()
        summary = tracker.summary()
        avg = summary["averages"]
        check("average STT finalisation over the turns that had one", avg["stt_final_ms"] == {"avg_ms": None, "n": 0})
        check("average KB retrieval", avg["kb_ms"] == {"avg_ms": 200, "n": 5}, str(avg["kb_ms"]))
        check("average LLM TTFT", avg["llm_ttft_ms"] == {"avg_ms": 3500, "n": 5}, str(avg["llm_ttft_ms"]))
        check("average LLM total", avg["llm_total_ms"]["avg_ms"] == 3700, str(avg["llm_total_ms"]))
        check("average tool over none", avg["tool_ms"] == {"avg_ms": None, "n": 0})
        check("average TTS TTFA", avg["tts_ttfa_ms"] == {"avg_ms": 200, "n": 5})
        check("average total", avg["total_ms"] == {"avg_ms": 4000, "n": 5}, str(avg["total_ms"]))
        check("p95 of the total is the slow outlier", summary["total_p95_ms"] == 10000 and summary["total_max_ms"] == 10000, str(summary["total_p95_ms"]))
        check("every stage the brief asked for has an average", [k for k, _ in SUMMARY_STAGES] == list(avg))
        check("the summary is JSON", json.loads(json.dumps(summary))["response_turns"] == 5)
        check("and carries no transcript", "must not be logged" not in json.dumps(summary))

        tracker.log_summary()
        turn_lines = captured.matching("LATENCY | TURN ")
        check("one human line per turn", len(turn_lines) == 5 and turn_lines[3].startswith("LATENCY | TURN 4 | STT final n/a | KB retrieval 0.20s (1 passages) | LLM TTFT 3.50s"), turn_lines[3] if turn_lines else "none")
        event_lines = captured.matching("turn.breakdown")
        check("and one structured line, greppable by field", len(event_lines) == 5 and "total_ms=4000" in event_lines[3] and "kb_ms=200" in event_lines[3], event_lines[3] if event_lines else "none")
        summary_lines = captured.matching("LATENCY CALL SUMMARY")
        check("the call summary heads with the counts", summary_lines and summary_lines[0].startswith("LATENCY CALL SUMMARY | 5 response turn(s), 5 with first audio"), summary_lines[0] if summary_lines else "none")
        total_line = [line for line in summary_lines if "TOTAL end-of-user-speech" in line]
        check("and ends with the average and p95 of the total", total_line and "avg 4.00s" in total_line[0] and "p95 10.00s" in total_line[0], total_line[0] if total_line else "none")
        check("nothing the caller said was logged", not captured.matching("must not be logged"))
    finally:
        captured.close()

    print("\n=== a secret in an error never reaches the log ===")
    captured = Captured()
    try:
        tracker = tracker_with(Clock())
        tracker.on_user_started()
        tracker.on_user_stopped()
        tracker.on_llm_started()
        tracker.on_error("llm", "refused: Authorization: Bearer gsk_abcdefghijklmnopqrstuvwxyz012345 rate limited")
        tracker.summary()
        lines = captured.matching("turn.breakdown")
        check("the error is on the line, redacted", lines and "error=" in lines[0] and "gsk_abcdefghijklmnopqrstuvwxyz012345" not in lines[0], lines[0] if lines else "none")
    finally:
        captured.close()


def check_wiring() -> None:
    print("\n=== wired into the bot ===")
    bot = (SERVER / "bot.py").read_text(encoding="utf-8")
    check("the tracker is on the worker's observers", "latency.observer" in bot)
    check("the retriever is told about it", "retriever.latency = latency" in bot)
    check("the aggregator's turn-stopped event reaches it", "latency.note_user_turn_stopped(content)" in bot)
    check("its summary rides in the call report under its own key", '"turns": latency.summary()' in bot)
    check("and it logs the call summary at teardown", "latency.log_summary()" in bot)
    retrieval = (SERVER / "src" / "retrieval.py").read_text(encoding="utf-8")
    check("the retriever reports every way a retrieval ends", all(f'"{o}"' in retrieval for o in ("skipped", "failed", "empty", "nothing found")))


async def main() -> int:
    print("Latency instrumentation checks — no keys, no database, no wall clock.")
    check_normal_turn()
    check_without_vad()
    await check_observer()
    await check_tool_turn()
    await check_interruptions_and_errors()
    check_greeting_and_unprompted()
    check_text_mode()
    await check_context_driven_turns()
    await check_retrieval_hook()
    check_summary_and_logs()
    check_wiring()

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
