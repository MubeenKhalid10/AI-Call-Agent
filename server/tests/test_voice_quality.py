#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for Phase 12: turn quality, barge-in, noise recovery and voicemail.

Run it from the `server/` directory::

    uv run python tests/test_voice_quality.py

No keys, no database, no phone, no audio. Everything under test is driven by
an injected clock or a scripted callable:

* `TurnMonitor` is fed the frame events a real call produces — a turn opening,
  closing, the LLM starting, audio reaching the caller, an interruption — and
  asked what it saw: the per-turn latency, a failed turn, how fast a barge-in
  stopped the bot, and whether an interruption was noise.
* `VoicemailDetector` is told what the first caller turns said and how long
  they ran, and asked whether a machine answered. `VoicemailHandler` is driven
  through the three callables `bot.py` gives it, so both the hang-up and the
  leave-a-message paths run end to end with nothing from Pipecat.
* The carrier side — the `MachineDetection` parameters on a placement, the
  `answered_by` field read back — goes through the real `TwilioProvider`
  against the stub session `tests/test_telephony.py` already has.
* The campaign side — `VOICEMAIL` as an attempt status, a disposition, a
  result that validates, a retry — goes through the real builders and the
  real validator.

A plain script rather than a pytest suite because the project has no test
dependency and this needs none. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import FramePushed  # noqa: E402
from pipecat.observers.user_bot_latency_observer import (  # noqa: E402
    LatencyBreakdown,
    TTFBBreakdownMetrics,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from src.campaigns import (  # noqa: E402
    CallAttempt,
    CallAttemptStatus,
    Disposition,
    attempt_status_for,
    build_carrier_result,
    build_conversation_result,
    derive_disposition,
    validate_call_result,
)
from src.campaigns.dialer import machine_status  # noqa: E402
from src.campaigns.models import FINAL_STATUS_SQL, may_advance  # noqa: E402
from src.campaigns.results import voicemail_detected  # noqa: E402
from src.config import Config, ConfigError  # noqa: E402
from src.conversation import InterestLevel, QualificationStatus  # noqa: E402
from src.dashboard.stats import _outcomes, _tone_for  # noqa: E402
from src.metrics import LatencyReporter  # noqa: E402
from src.prompts import NOISE_RESUME_INSTRUCTION, TURN_INSTRUCTIONS, is_turn_instruction  # noqa: E402
from src.telephony import CallRequest, CallSnapshot, CallStatus  # noqa: E402
from src.voice_quality import (  # noqa: E402
    FAILED_ERROR,
    FAILED_NO_AUDIO,
    FAILED_NO_RESPONSE,
    TurnMonitor,
    load_call_report,
    report_path,
    write_call_report,
)
from src.voicemail import (  # noqa: E402
    ACTION_HANGUP,
    ACTION_MESSAGE,
    DEFAULT_VOICEMAIL_PHRASES,
    METHOD_CARRIER,
    METHOD_GREETING_LENGTH,
    METHOD_PHRASES,
    VoicemailDetector,
    VoicemailHandler,
    machine_answered,
    matched_phrase,
    normalize_answered_by,
)

# The stub carrier session and fixtures the telephony checks already use.
from test_telephony import StubResponse, call_resource, make_twilio, sample_request  # noqa: E402

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


def config_with(**env: str) -> Config:
    """Build a config with some environment variables set, then put them back."""
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


# --- The turn monitor -----------------------------------------------------------


def check_turn_monitor() -> None:
    """Per-turn timing, failed turns, barge-in stop latency, and noise."""
    print("\n=== turn monitor: a normal turn ===")
    clock = Clock()
    monitor = TurnMonitor(response_timeout_secs=5.0, clock=clock)
    monitor.note_connected()

    # The greeting: bot audio with no caller turn before it.
    clock.tick(2.0)
    monitor.on_bot_started()
    clock.tick(3.0)
    monitor.on_bot_stopped()
    check("the greeting is timed from connect", monitor.greeting_at == 2.0, str(monitor.greeting_at))

    # A caller turn: opens, closes, gets a response.
    monitor.on_user_started()
    clock.tick(2.0)
    monitor.on_user_stopped()
    monitor.note_user_turn_stopped("We run about forty trucks.")
    clock.tick(0.3)
    monitor.on_llm_started()
    clock.tick(0.3)
    monitor.on_llm_text()
    clock.tick(0.2)
    monitor.on_tts_started()
    clock.tick(0.2)
    monitor.on_bot_started()
    turn = monitor.turns[0]
    check("one caller turn recorded", len(monitor.turns) == 1 and monitor.caller_turns == 1)
    check("release to first audio measured", turn.release_to_audio_ms == 1000, str(turn.release_to_audio_ms))
    record = turn.to_dict()
    check("LLM start, first token and TTS start are timed from the release",
          (record["llm_started_ms"], record["first_token_ms"], record["tts_started_ms"]) == (300, 600, 800),
          str(record))
    check("the turn is not interrupted, spurious or failed",
          not turn.interrupted_bot and not turn.spurious and turn.failed is None)
    clock.tick(6.0)
    check("a responded turn never fails, however long ago", monitor.check_failed_turns() == [])
    monitor.on_bot_stopped()

    print("\n=== turn monitor: failed turns ===")
    monitor.on_user_started()
    clock.tick(1.0)
    monitor.on_user_stopped()
    monitor.note_user_turn_stopped("Is anybody there?")
    clock.tick(4.0)
    check("nothing is flagged before the timeout", monitor.check_failed_turns() == [])
    clock.tick(1.5)
    flagged = monitor.check_failed_turns()
    check("a turn with no response is flagged after the timeout",
          [t.index for t in flagged] == [2] and flagged[0].failed == FAILED_NO_RESPONSE, str(flagged))
    check("and only once", monitor.check_failed_turns() == [])
    check("failed turns are listed", [t.index for t in monitor.failed_turns] == [2])

    monitor.on_user_started()
    clock.tick(1.0)
    monitor.on_user_stopped()
    monitor.note_user_turn_stopped("Hello?")
    monitor.on_llm_started()
    clock.tick(6.0)
    flagged = monitor.check_failed_turns()
    check("an inference that started and produced no audio is its own reason",
          flagged and flagged[0].failed == FAILED_NO_AUDIO, str([t.failed for t in flagged]))

    monitor.on_user_started()
    clock.tick(1.0)
    monitor.on_user_stopped()
    monitor.note_user_turn_stopped("And now?")
    monitor.on_error("llm", "429 rate limited")
    clock.tick(6.0)
    flagged = monitor.check_failed_turns()
    check("a service error during the wait is the reason",
          flagged and flagged[0].failed == FAILED_ERROR and "429" in (flagged[0].error or ""),
          str([(t.failed, t.error) for t in flagged]))
    check("errors are kept on the report", monitor.errors and monitor.errors[0]["stage"] == "llm")

    monitor.on_user_started()
    clock.tick(1.0)
    monitor.on_user_stopped()
    monitor.note_user_turn_stopped("Book me a slot.")
    monitor.on_function_call("check_calendar_availability")
    clock.tick(40.0)
    check("a turn waiting on a tool call is never flagged", monitor.check_failed_turns() == [])

    monitor.on_user_started()
    clock.tick(0.5)
    monitor.on_user_stopped()
    monitor.note_user_turn_stopped("")
    clock.tick(6.0)
    check("an empty turn cannot fail: nothing was asked", monitor.check_failed_turns() == [])

    print("\n=== turn monitor: barge-in ===")
    clock = Clock()
    monitor = TurnMonitor(response_timeout_secs=5.0, clock=clock)
    monitor.note_connected()
    monitor.on_bot_started()
    clock.tick(1.5)
    monitor.on_user_started()
    monitor.on_interruption()
    clock.tick(0.25)
    monitor.on_bot_stopped()
    check("an interruption during bot audio is a barge-in", len(monitor.barge_ins) == 1)
    barge_in = monitor.barge_ins[0]
    check("its stop latency is interruption to bot-stopped", barge_in.stop_latency_ms == 250, str(barge_in.stop_latency_ms))
    check("and it is tied to the turn that caused it", barge_in.turn == 1 and monitor.turns[0].interrupted_bot)
    monitor.note_assistant_turn("Sunlight looks white, but it's actually", interrupted=True)
    check("the words the caller heard before the cut are kept",
          barge_in.spoken_before_cut == "Sunlight looks white, but it's actually")
    clock.tick(1.0)
    monitor.on_user_stopped()
    monitor.note_user_turn_stopped("Sorry, what is the capital of France?")
    check("a barge-in with words in it is real", barge_in.spurious is False and not monitor.turns[0].spurious)
    check("and does not ask for a resume", not monitor.should_resume_after_noise("Sorry, what is the capital of France?"))

    monitor.on_user_started()
    monitor.on_interruption()
    check("an interruption while the bot is silent is not a barge-in", len(monitor.barge_ins) == 1)
    monitor.on_user_stopped()
    monitor.note_user_turn_stopped("")
    check("an empty turn with no barge-in is not noise to recover from", not monitor.should_resume_after_noise(""))

    check("a turn that began before the agent spoke counts as over the agent",
          TurnMonitor(clock=Clock()).last_turn_over_agent)
    check("a turn that waited for the agent to finish does not", monitor.last_turn_over_agent is False)

    print("\n=== turn monitor: a spurious interruption ===")
    monitor.on_bot_started()
    clock.tick(1.0)
    monitor.on_user_started()
    monitor.on_interruption()
    clock.tick(0.1)
    monitor.on_bot_stopped()
    monitor.note_assistant_turn("Every vehicle's position is", interrupted=True)
    clock.tick(0.4)
    monitor.on_user_stopped()
    monitor.note_user_turn_stopped("")
    turn = monitor.turns[-1]
    check("a barge-in whose turn carried no words is spurious", turn.spurious and monitor.barge_ins[-1].spurious is True)
    check("and the agent should be asked to resume", monitor.should_resume_after_noise(""))
    monitor.note_noise_resume()
    check("the resume is counted on the turn", monitor.noise_resumes == 1 and turn.resumed_after_noise)
    monitor.note_stop_timeout()
    check("stop timeouts are counted", monitor.stop_timeouts == 1)

    report = monitor.report(latency={"responses": 1}, voicemail={"detected": False}, extra={"call_id": "CA1", "skip": None})
    check("the report carries the counts",
          report["barge_in_count"] == 2 and report["spurious_interruptions"] == 1 and report["noise_resumes"] == 1
          and report["stop_timeouts"] == 1 and report["caller_turns"] == 1,
          str({k: report[k] for k in ("barge_in_count", "spurious_interruptions", "noise_resumes", "caller_turns")}))
    check("and the identity the caller adds, dropping unknowns", report["call_id"] == "CA1" and "skip" not in report)
    check("and the latency summary it was given", report["latency"] == {"responses": 1})
    check("and is JSON", json.loads(json.dumps(report))["schema_version"] == 1)
    check("describes itself", "2 barge-in(s)" in monitor.describe() and "1 spurious" in monitor.describe(), monitor.describe())


async def check_monitor_observer() -> None:
    """The observer sees each frame once per hop and must count it once."""
    print("\n=== turn monitor: the observer ===")
    clock = Clock()
    monitor = TurnMonitor(response_timeout_secs=0, clock=clock)
    observer = monitor.observer
    source = SimpleNamespace(name="DeepgramFluxSTTService#0")

    async def push(frame, hops: int = 3, src=source):
        for _ in range(hops):
            await observer.on_push_frame(
                FramePushed(source=src, destination=source, frame=frame, direction=FrameDirection.DOWNSTREAM, timestamp=0)
            )

    await push(BotStartedSpeakingFrame())
    await push(UserStartedSpeakingFrame())
    await push(InterruptionFrame())
    clock.tick(0.2)
    await push(BotStoppedSpeakingFrame())
    await push(UserStoppedSpeakingFrame())
    await push(LLMFullResponseStartFrame())
    await push(ErrorFrame(error="boom"), src=SimpleNamespace(name="GroqLLMService#0"))
    await push(ErrorFrame(error="socket closed"), src=SimpleNamespace(name="FastAPIWebsocketOutputTransport#0"))
    check("one turn from three hops of the same frame", len(monitor.turns) == 1)

    check("one barge-in from three hops", len(monitor.barge_ins) == 1 and monitor.barge_ins[0].stop_latency_ms == 200)
    check("the error is attached to the turn, once", monitor.turns[0].error == "llm: boom" and len(monitor.errors) == 2)
    check("a transport error is recorded as 'other'", monitor.errors[1]["stage"] == "other")
    check("start and stop are harmless with the check disabled", (monitor.start(), monitor.stop()) == (None, None))

    # A broadcast is two frames with different ids that name each other. They
    # are one event: one user turn, one interruption, one bot start.
    down, up = UserStartedSpeakingFrame(), UserStartedSpeakingFrame()
    down.broadcast_sibling_id, up.broadcast_sibling_id = up.id, down.id
    await push(BotStartedSpeakingFrame())
    await push(down)
    await push(up)
    idown, iup = InterruptionFrame(), InterruptionFrame()
    idown.broadcast_sibling_id, iup.broadcast_sibling_id = iup.id, idown.id
    await push(idown)
    await push(iup)
    check("broadcast siblings are one user turn", len(monitor.turns) == 2, f"{len(monitor.turns)} turns")
    check("and one barge-in", len(monitor.barge_ins) == 2, f"{len(monitor.barge_ins)} barge-ins")
    bdown, bup = BotStoppedSpeakingFrame(), BotStoppedSpeakingFrame()
    bdown.broadcast_sibling_id, bup.broadcast_sibling_id = bup.id, bdown.id
    await push(bdown)
    await push(bup)
    sdown, sup = BotStartedSpeakingFrame(), BotStartedSpeakingFrame()
    sdown.broadcast_sibling_id, sup.broadcast_sibling_id = sup.id, sdown.id
    await push(sdown)
    await push(sup)
    check("and one agent turn per bot start", monitor.agent_turns == 3, f"{monitor.agent_turns} agent turns")


def check_report_files() -> None:
    """The report on disk: written, read back, and never outside its directory."""
    print("\n=== the call report on disk ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = write_call_report(tmp, "CA123", {"schema_version": 1, "turns": []})
        check("written under the directory, named by the call id", path is not None and path.name == "CA123.json")
        check("read back", load_call_report(tmp, "CA123") == {"schema_version": 1, "turns": []})
        check("a missing report is None, not an error", load_call_report(tmp, "CA999") is None)
        check("no directory means no file, quietly", write_call_report(None, "CA1", {}) is None)
        check("no call id means no file, quietly", write_call_report(tmp, None, {}) is None)
        sneaky = report_path(tmp, "../../etc/passwd")
        check("an id cannot escape the directory", sneaky.parent == Path(tmp) and ".." not in sneaky.name, str(sneaky))


async def check_latency_reporter() -> None:
    """The per-response records and the summary the report carries. Phase 12 additions."""
    print("\n=== latency records ===")
    reporter = LatencyReporter(log_each_turn=False)

    def breakdown(user_turn: float, llm: float, tts: float) -> LatencyBreakdown:
        return LatencyBreakdown(
            ttfb=[
                TTFBBreakdownMetrics(processor="DeepgramFluxSTTService#0", start_time=1.0, duration_secs=user_turn - 0.01),
                TTFBBreakdownMetrics(processor="GroqLLMService#0", start_time=2.0, duration_secs=llm),
                TTFBBreakdownMetrics(processor="CartesiaTTSService#0", start_time=3.0, duration_secs=tts),
                # A second TTS sample, for the second sentence: must not replace the first.
                TTFBBreakdownMetrics(processor="CartesiaTTSService#0", start_time=4.0, duration_secs=tts * 3),
            ],
            user_turn_secs=user_turn,
        )

    await reporter._on_latency_measured(None, 1.2)
    await reporter._on_latency_breakdown(None, breakdown(0.6, 0.35, 0.14))
    await reporter._on_latency_measured(None, 2.0)
    await reporter._on_latency_breakdown(None, breakdown(1.0, 0.5, 0.2))
    # The greeting: a breakdown with no measured total is not a response.
    await reporter._on_first_bot_speech_latency(None, 2.5)
    await reporter._on_latency_breakdown(None, breakdown(0.1, 0.1, 0.1))

    first = reporter.records[0]
    check("one record per response, none for the greeting", len(reporter.records) == 2)
    check("the record names every stage in milliseconds",
          first == {"response": 1, "total_ms": 1200, "turn_end_ms": 600, "stt_ms": 590, "llm_first_token_ms": 350, "tts_first_audio_ms": 140},
          str(first))
    summary = reporter.summary()
    check("the summary carries the greeting", summary["greeting_ms"] == 2500)
    check("and percentiles per stage", summary["stages"]["total"]["p50_ms"] == 1200 and summary["stages"]["total"]["p95_ms"] == 2000
          and summary["stages"]["total"]["n"] == 2, str(summary["stages"]["total"]))
    check("and the records", summary["per_response"] == reporter.records)
    check("an empty stage is None, not zero", LatencyReporter(log_each_turn=False).summary()["stages"]["llm"]["p50_ms"] is None)


# --- Voicemail ------------------------------------------------------------------


def check_voicemail_detector() -> None:
    """When the first caller turns are a recording, and when they are a person."""
    print("\n=== voicemail: the carrier's vocabulary ===")
    for raw, expected in (
        ("human", "human"),
        ("machine_start", "machine"),
        ("machine_end_beep", "machine"),
        ("machine_end_silence", "machine"),
        ("machine_end_other", "machine"),
        ("MACHINE_END_BEEP", "machine"),
        ("fax", "fax"),
        ("unknown", "unknown"),
        ("something-new", "unknown"),
        ("", None),
        (None, None),
    ):
        check(f"answered_by {raw!r:<22} -> {expected}", normalize_answered_by(raw) == expected, str(normalize_answered_by(raw)))
    check("a machine or a fax answered", machine_answered("machine_end_beep") and machine_answered("fax"))
    check("a human, unknown or nothing did not", not machine_answered("human") and not machine_answered("unknown") and not machine_answered(None))

    print("\n=== voicemail: phrases ===")
    check("the built-in list is not empty", len(DEFAULT_VOICEMAIL_PHRASES) > 10)
    for text, expected in (
        ("Hi, you've reached Sarah. Please leave a message after the tone.", True),
        ("The person you are calling is not available. Please record your message.", True),
        ("Hello?", False),
        ("Yeah, hi, who's this?", False),
        ("I'm not available next week, can you call back?", False),
        ("Sorry, I can't take your call right now, I'm driving", True),
    ):
        got = matched_phrase(text, DEFAULT_VOICEMAIL_PHRASES) is not None
        check(f"{text[:48]!r:<52} recording={expected}", got == expected, str(matched_phrase(text, DEFAULT_VOICEMAIL_PHRASES)))
    check("matching ignores case and extra spaces", matched_phrase("PLEASE   LEAVE A   MESSAGE", DEFAULT_VOICEMAIL_PHRASES) == "leave a message")

    print("\n=== voicemail: the detector ===")
    clock = Clock()
    detector = VoicemailDetector(max_greeting_secs=8.0, window_secs=30.0, clock=clock)
    detector.note_connected()
    detector.note_user_turn_started()
    clock.tick(1.0)
    verdict = detector.note_user_turn_stopped("Hello?")
    check("a person saying hello is not a machine", verdict is None and not detector.detected)
    detector.note_user_turn_started()
    clock.tick(6.0)
    verdict = detector.note_user_turn_stopped("Hi, you've reached Sarah. I can't take your call right now, leave a message after the tone.")
    check("a greeting with a recording's phrase is", verdict is not None and verdict.method == METHOD_PHRASES, str(verdict))
    check("with the phrase as evidence and the transcript kept",
          verdict is not None and "leave a message" in verdict.evidence and verdict.transcript is not None and "Sarah" in verdict.transcript)
    check("timed from the connect", verdict is not None and verdict.at_secs == 7.0, str(verdict.at_secs if verdict else None))
    check("a verdict is final: a later human turn changes nothing", detector.note_user_turn_stopped("Hello?") is None and detector.verdict is verdict)
    check("to_dict says detected", detector.to_dict()["detected"] is True and detector.to_dict()["method"] == METHOD_PHRASES)

    clock = Clock()
    detector = VoicemailDetector(max_greeting_secs=8.0, window_secs=30.0, clock=clock)
    detector.note_connected()
    detector.note_user_turn_started(over_agent_audio=True)
    clock.tick(9.0)
    verdict = detector.note_user_turn_stopped("we are a family run business established in nineteen ninety two and we")
    check("a first turn that talks over the agent past the limit is a recording, whatever it said",
          verdict is not None and verdict.method == METHOD_GREETING_LENGTH, str(verdict))

    clock = Clock()
    detector = VoicemailDetector(max_greeting_secs=8.0, window_secs=30.0, clock=clock)
    detector.note_connected()
    detector.note_user_turn_started(over_agent_audio=False)
    clock.tick(12.0)
    check("a long answer that waited for the agent to finish is a person",
          detector.note_user_turn_stopped("right so quickly we have got forty trucks running out of Lahore and") is None)
    detector.note_user_turn_started(over_agent_audio=False)
    clock.tick(12.0)
    check("and the open-turn watchdog leaves it alone too", detector.check_open_turn() is None)

    # 2026-09-17, found by `phone_drill.py overlap`: a live caller answered the
    # greeting, then interrupted the agent for ten seconds, and was hung up on.
    clock = Clock()
    detector = VoicemailDetector(max_greeting_secs=8.0, window_secs=30.0, clock=clock)
    detector.note_connected()
    detector.note_user_turn_started(over_agent_audio=False)
    clock.tick(2.0)
    detector.note_user_turn_stopped("Tell me a bit about what you do and how it works.")
    detector.note_user_turn_started(over_agent_audio=True)
    clock.tick(9.0)
    check("somebody who waited for the agent and answered is a person: a long barge-in after that is not a recording",
          detector.check_open_turn() is None)
    check("at the end of that turn too",
          detector.note_user_turn_stopped("sorry hang on a second I have got someone at the door and I cannot really talk") is None)

    clock = Clock()
    detector = VoicemailDetector(max_greeting_secs=8.0, window_secs=30.0, clock=clock)
    detector.note_connected()
    detector.note_user_turn_started(over_agent_audio=False)
    clock.tick(1.0)
    detector.note_user_turn_stopped("")
    detector.note_user_turn_started(over_agent_audio=True)
    clock.tick(9.0)
    check("a turn with no words in it establishes nobody: the length rule still applies",
          detector.check_open_turn() is not None)

    clock = Clock()
    detector = VoicemailDetector(max_greeting_secs=8.0, window_secs=30.0, clock=clock)
    detector.note_connected()
    detector.note_user_turn_started(over_agent_audio=True)
    clock.tick(5.0)
    check("an open turn under the limit is left alone", detector.check_open_turn() is None)
    clock.tick(4.0)
    verdict = detector.check_open_turn()
    check("an open turn past the limit is decided before it ends", verdict is not None and verdict.method == METHOD_GREETING_LENGTH)

    clock = Clock()
    detector = VoicemailDetector(max_greeting_secs=8.0, window_secs=30.0, max_turns=2, clock=clock)
    detector.note_connected()
    for _ in range(2):
        detector.note_user_turn_started()
        clock.tick(1.0)
        detector.note_user_turn_stopped("Yes.")
    detector.note_user_turn_started()
    clock.tick(12.0)
    check("a long third turn is a person talking, not a machine", detector.note_user_turn_stopped("please leave a message") is None)

    clock = Clock()
    detector = VoicemailDetector(max_greeting_secs=8.0, window_secs=10.0, clock=clock)
    detector.note_connected()
    clock.tick(20.0)
    detector.note_user_turn_started()
    clock.tick(1.0)
    check("nothing is judged after the window", detector.note_user_turn_stopped("leave a message after the tone") is None)

    clock = Clock()
    detector = VoicemailDetector(max_greeting_secs=0.0, clock=clock)
    detector.note_connected()
    detector.note_user_turn_started()
    clock.tick(30.0)
    check("a zero limit disables the length rule", detector.note_user_turn_stopped("hello there how are you") is None and detector.check_open_turn() is None)

    detector = VoicemailDetector(enabled=False, clock=Clock())
    detector.note_connected()
    detector.note_user_turn_started()
    check("a disabled detector says nothing", detector.note_user_turn_stopped("leave a message after the tone") is None
          and detector.note_carrier_answered_by("machine_end_beep") is None and detector.to_dict()["detected"] is False)

    detector = VoicemailDetector(clock=Clock())
    detector.note_connected()
    check("the carrier's 'human' is not a verdict", detector.note_carrier_answered_by("human") is None)
    check("nor is 'unknown'", detector.note_carrier_answered_by("unknown") is None)
    verdict = detector.note_carrier_answered_by("machine_end_beep")
    check("the carrier's 'machine' is", verdict is not None and verdict.method == METHOD_CARRIER and "machine_end_beep" in verdict.evidence)


class Pipeline:
    """The three callables the handler talks to, recording what it asked for."""

    def __init__(self) -> None:
        self.ended = 0
        self.cancelled = 0
        self.spoken: list[str] = []
        self.noted: list[tuple[str, str]] = []

    async def end(self) -> None:
        self.ended += 1

    async def cancel(self) -> None:
        self.cancelled += 1

    async def speak(self, text: str) -> None:
        self.spoken.append(text)

    def note(self, verdict, action: str) -> None:
        self.noted.append((verdict.method, action))


async def check_voicemail_handler() -> None:
    """Hanging up, leaving a message, and reading the carrier."""
    print("\n=== voicemail: hang up ===")
    pipe = Pipeline()
    detector = VoicemailDetector(clock=Clock())
    handler = VoicemailHandler(
        detector, action=ACTION_HANGUP, end_session=pipe.end, cancel_session=pipe.cancel, speak=pipe.speak, on_detected=pipe.note
    )
    handler.on_connected()
    handler.on_user_turn_started()
    await handler.on_user_turn_stopped("please leave a message after the beep")
    check("a detected machine ends the call immediately", pipe.cancelled == 1 and pipe.ended == 0)
    check("and nothing is spoken", pipe.spoken == [])
    check("the caller is told what happened and why", pipe.noted == [(METHOD_PHRASES, ACTION_HANGUP)])
    check("the handler reports itself active", handler.active)
    await handler.on_user_turn_stopped("please leave a message after the beep")
    check("a second verdict cannot end the call twice", pipe.cancelled == 1)
    handler.stop()

    print("\n=== voicemail: leave a message ===")
    pipe = Pipeline()
    detector = VoicemailDetector(clock=Clock())
    handler = VoicemailHandler(
        detector,
        action=ACTION_MESSAGE,
        message="Sorry I missed you.",
        message_delay_secs=0.05,
        message_grace_secs=1.0,
        end_session=pipe.end,
        cancel_session=pipe.cancel,
        speak=pipe.speak,
        on_detected=pipe.note,
    )
    handler.on_connected()
    handler.on_user_turn_started()
    await handler.on_user_turn_stopped("you have reached the voicemail of Sarah, leave a message after the tone")
    check("a message-mode detection does not end the call yet", pipe.cancelled == 0 and pipe.ended == 0 and handler.active)
    await asyncio.sleep(0.15)
    check("the message is spoken after the greeting's turn ended plus the delay", pipe.spoken == ["Sorry I missed you."], str(pipe.spoken))
    await handler.on_bot_stopped_speaking()
    check("and the call ends gracefully once it has played", pipe.ended == 1 and pipe.cancelled == 0)
    check("the note says a message was left", pipe.noted == [(METHOD_PHRASES, ACTION_MESSAGE)])
    handler.stop()

    pipe = Pipeline()
    detector = VoicemailDetector(clock=Clock())
    handler = VoicemailHandler(
        detector, action=ACTION_MESSAGE, message="Hi.", message_delay_secs=0.05, message_grace_secs=0.2,
        end_session=pipe.end, cancel_session=pipe.cancel, speak=pipe.speak,
    )
    handler.on_connected()
    await handler.on_carrier_answered_by("machine_start")
    await asyncio.sleep(0.12)
    check("a carrier verdict speaks after the delay alone: there is no turn to wait for", pipe.spoken == ["Hi."], str(pipe.spoken))
    await asyncio.sleep(0.3)
    check("a message that never finishes playing still ends the call", pipe.cancelled == 1, str((pipe.ended, pipe.cancelled)))
    handler.stop()

    pipe = Pipeline()
    handler = VoicemailHandler(
        VoicemailDetector(clock=Clock()), action=ACTION_MESSAGE, message="",
        end_session=pipe.end, cancel_session=pipe.cancel, speak=pipe.speak,
    )
    check("message mode with nothing to say degrades to hang up", handler.action == ACTION_HANGUP)
    handler = VoicemailHandler(
        VoicemailDetector(clock=Clock()), action=ACTION_MESSAGE, message="Hi.",
        end_session=pipe.end, cancel_session=pipe.cancel, speak=None,
    )
    check("and so does one that cannot speak", handler.action == ACTION_HANGUP)

    print("\n=== voicemail: polling the carrier ===")
    pipe = Pipeline()
    answers = iter([None, "unknown", "machine_end_silence", "machine_end_silence"])
    reads = 0

    async def fetch():
        nonlocal reads
        reads += 1
        return next(answers, "machine_end_silence")

    handler = VoicemailHandler(VoicemailDetector(clock=Clock()), end_session=pipe.end, cancel_session=pipe.cancel, on_detected=pipe.note)
    handler.on_connected()
    handler.watch_carrier(fetch, poll_secs=0.5, window_secs=5.0)
    await asyncio.sleep(1.3)
    check("the poll stops at the first definite answer", reads == 3, f"{reads} reads")
    check("and a machine ends the call", pipe.cancelled == 1 and pipe.noted == [(METHOD_CARRIER, ACTION_HANGUP)])
    handler.stop()

    pipe = Pipeline()

    async def human():
        return "human"

    handler = VoicemailHandler(VoicemailDetector(clock=Clock()), end_session=pipe.end, cancel_session=pipe.cancel)
    handler.on_connected()
    handler.watch_carrier(human, poll_secs=0.5, window_secs=5.0)
    await asyncio.sleep(0.2)
    check("a human leaves the call alone", pipe.cancelled == 0 and not handler.active)
    handler.stop()

    pipe = Pipeline()

    async def broken():
        raise RuntimeError("carrier down")

    handler = VoicemailHandler(VoicemailDetector(clock=Clock()), end_session=pipe.end, cancel_session=pipe.cancel)
    handler.on_connected()
    handler.watch_carrier(broken, poll_secs=0.5, window_secs=0.6)
    await asyncio.sleep(0.9)
    check("a carrier that cannot be read is not a verdict", pipe.cancelled == 0)
    handler.stop()

    print("\n=== voicemail: the watchdog ===")
    pipe = Pipeline()
    clock = Clock()
    handler = VoicemailHandler(
        VoicemailDetector(max_greeting_secs=8.0, clock=clock), end_session=pipe.end, cancel_session=pipe.cancel, on_detected=pipe.note
    )
    handler.on_connected()
    handler.on_user_turn_started()
    clock.tick(9.0)
    await asyncio.sleep(0.8)
    check("a greeting that runs on is cut off before it ends", pipe.cancelled == 1 and pipe.noted == [(METHOD_GREETING_LENGTH, ACTION_HANGUP)])
    handler.stop()


# --- The carrier ------------------------------------------------------------------


async def check_carrier_amd() -> None:
    """What goes on the wire when detection is asked for, and what comes back."""
    print("\n=== carrier: requesting detection ===")
    for mode, machine, async_flag in (("off", None, None), ("async", "Enable", "true"), ("sync", "Enable", None)):
        provider, session = make_twilio([StubResponse(201, call_resource())])
        await provider.place_call(sample_request(machine_detection=mode))
        _, _, data = session.requests[0][0], session.requests[0][1], session.requests[0][2]["data"]
        check(f"{mode:<6} sends MachineDetection={machine}", data.get("MachineDetection") == machine, str(data.get("MachineDetection")))
        check(f"{mode:<6} sends AsyncAmd={async_flag}", data.get("AsyncAmd") == async_flag, str(data.get("AsyncAmd")))
    check("the default request asks for nothing", CallRequest("+1", "+2", "wss://h/ws").machine_detection == "off")

    print("\n=== carrier: reading the verdict ===")
    for raw, expected in (("machine_end_beep", "machine"), ("human", "human"), (None, None), ("fax", "fax")):
        provider, _ = make_twilio([StubResponse(200, call_resource(status="in-progress", answered_by=raw))])
        snapshot = await provider.fetch_call("CA1")
        check(f"answered_by {raw!r:<20} -> {expected}", snapshot.answered_by == expected, str(snapshot.answered_by))
    provider, _ = make_twilio([StubResponse(200, call_resource(status="completed", answered_by="machine_end_other", duration="12"))])
    snapshot = await provider.fetch_call("CA1")
    check("machine_answered reads the normalised value", snapshot.machine_answered)
    check("and the description shows it", "answered_by=machine" in snapshot.describe(), snapshot.describe())

    print("\n=== the dialer's mapping ===")
    machine = CallSnapshot(provider="twilio", call_id="CA1", status=CallStatus.COMPLETED, answered_by="machine")
    human = CallSnapshot(provider="twilio", call_id="CA1", status=CallStatus.COMPLETED, answered_by="human")
    silent = CallSnapshot(provider="twilio", call_id="CA1", status=CallStatus.COMPLETED)
    check("a completed call a machine answered is a voicemail", machine_status(CallAttemptStatus.COMPLETED, machine) is CallAttemptStatus.VOICEMAIL)
    check("one a human answered is completed", machine_status(CallAttemptStatus.COMPLETED, human) is CallAttemptStatus.COMPLETED)
    check("no verdict changes nothing", machine_status(CallAttemptStatus.COMPLETED, silent) is CallAttemptStatus.COMPLETED)
    check("a live call stays live: the bot may still end it", machine_status(CallAttemptStatus.CONNECTED, machine) is CallAttemptStatus.CONNECTED)
    check("a busy line is not second-guessed", machine_status(CallAttemptStatus.BUSY, machine) is CallAttemptStatus.BUSY)

    import test_reliability as rel

    store = rel.FakeStore()
    service = rel.FakeService(store)
    carrier = rel.FlakyCarrier()
    dialer = rel.make_dialer(service, carrier, machine_detection="async")
    result = await dialer.dial(rel.queued_call(store))
    check("the dialer asks the carrier for detection on every call", result.placed and carrier.placed[0].machine_detection == "async")
    dialer = rel.make_dialer(service, rel.FlakyCarrier())
    check("and for nothing when not configured", (await dialer.dial(rel.queued_call(store, id=222))).placed and dialer._machine_detection == "off")


# --- Campaign state -----------------------------------------------------------------


def _attempt(status: CallAttemptStatus, **fields) -> CallAttempt:
    base = dict(id=11, prospect_id=7, campaign_id=3, campaign_prospect_id=5, attempt_number=1, status=status)
    base.update(fields)
    return CallAttempt(**base)


def _voicemail_outcome(**overrides) -> dict:
    outcome = {
        "final_state": "GREETING",
        "state_path": ["GREETING"],
        "transitions": [],
        "refused_transitions": [],
        "qualification": {
            "interest_level": "UNKNOWN",
            "buying_timeline": "UNKNOWN",
            "decision_role": "UNKNOWN",
            "next_action": "UNKNOWN",
            "meeting_intent": "UNKNOWN",
            "callback_intent": "UNKNOWN",
            "pain_points": [],
            "objections": [],
            "notes": ["An answering machine answered (phrases: the greeting said 'leave a message'); the agent hung up."],
            "meeting_booked": False,
            "human_requested": None,
            "transferred": None,
        },
        "actions": [],
        "user_turns": 1,
        "agent_turns": 1,
        "duration_secs": 9.0,
        "call_duration_secs": 9.4,
        "agent_ended_call": True,
        "transcript": [
            {"role": "assistant", "text": "Hi, is that Sarah?", "at": 0.5, "interrupted": True},
            {"role": "user", "text": "You've reached Sarah, leave a message after the tone.", "at": 8.1, "interrupted": False},
        ],
        "voicemail": {"detected": True, "method": "phrases", "evidence": "the greeting said 'leave a message'", "at_secs": 8.1},
        "timezone": "UTC",
    }
    outcome.update(overrides)
    return outcome


def check_campaign_state() -> None:
    """VOICEMAIL as a status, a disposition, a result, and a retry."""
    print("\n=== the attempt status ===")
    status = CallAttemptStatus.VOICEMAIL
    check("VOICEMAIL is final", status.is_final and not status.is_live)
    check("nobody was reached", not status.reached_person)
    check("and it is worth retrying", status.should_retry)
    check("it is in the SQL list of final statuses", "'VOICEMAIL'" in FINAL_STATUS_SQL, FINAL_STATUS_SQL)
    check("a live call may become a voicemail", may_advance(CallAttemptStatus.CONNECTED, status) and may_advance(CallAttemptStatus.QUEUED, status))
    check("a voicemail is never overwritten by a later 'completed'", not may_advance(status, CallAttemptStatus.COMPLETED))
    check("nor does it overwrite a completed call", not may_advance(CallAttemptStatus.COMPLETED, status))

    print("\n=== reading the outcome ===")
    check("a voicemail verdict on the outcome sets the status", attempt_status_for(_voicemail_outcome()) is status)
    check("a do-not-call on the path still wins", attempt_status_for(_voicemail_outcome(state_path=["GREETING", "DO_NOT_CALL"])) is CallAttemptStatus.DO_NOT_CALL)
    check("no verdict leaves the status to the carrier", attempt_status_for(_voicemail_outcome(voicemail={"detected": False})) is None)
    check("voicemail_detected is strict about its shape",
          voicemail_detected({"voicemail": {"detected": True}}) and not voicemail_detected({"voicemail": {"detected": "yes"}})
          and not voicemail_detected({"voicemail": True}) and not voicemail_detected(None))

    print("\n=== the disposition ===")
    check("VOICEMAIL derives from the status", derive_disposition(status) is Disposition.VOICEMAIL)
    check("and means nobody was reached", not Disposition.VOICEMAIL.reached)
    check("a do-not-call on the status still outranks it", derive_disposition(CallAttemptStatus.DO_NOT_CALL) is Disposition.DO_NOT_CALL)

    print("\n=== the carrier's result ===")
    result = build_carrier_result(_attempt(status, duration_seconds=14))
    problems = validate_call_result(result)
    check("a carrier-reported voicemail validates", problems == [], str(problems))
    check("with the VOICEMAIL disposition", result.disposition is Disposition.VOICEMAIL)
    check("and a summary that says so", "answering machine" in result.summary.what_happened.lower(), result.summary.what_happened)
    check("and a next step that says retry", "retry" in result.summary.next_step.lower(), result.summary.next_step)

    print("\n=== the conversation's result ===")
    result = build_conversation_result(_attempt(status), _voicemail_outcome(), call_status=status)
    problems = validate_call_result(result)
    check("a voicemail the bot detected validates", problems == [], str(problems))
    check("the disposition is VOICEMAIL", result.disposition is Disposition.VOICEMAIL)
    check("the transcript is kept as evidence", len(result.transcript) == 2 and "leave a message" in result.transcript[1]["text"])
    check("the note is kept", any("answering machine" in note.lower() for note in result.notes))
    check("the agent is recorded as having ended it", result.agent_ended_call is True)
    check("qualification is unknown", result.qualification_status is QualificationStatus.UNKNOWN and result.interest_level is InterestLevel.UNKNOWN)
    check("no questions are extracted from a recording", result.questions == ())
    check("the summary names the machine and the hang-up",
          "answering machine" in result.summary.what_happened.lower() and "hung up" in result.summary.what_happened.lower(),
          result.summary.what_happened)
    check("and every other part says it is not applicable", "answering machine" in result.summary.prospect_needs.lower(), result.summary.prospect_needs)

    claimed = _voicemail_outcome()
    claimed["qualification"]["interest_level"] = "INTERESTED"
    claimed["qualification"]["pain_points"] = ["fuel costs"]
    result = build_conversation_result(_attempt(status), claimed, call_status=status)
    check("a model that qualified a recording is overruled", result.interest_level is InterestLevel.UNKNOWN and result.pain_points == ())
    check("and the record says so", any("answering machine" in issue and "interest_level" in issue for issue in result.issues), str(result.issues))
    check("and the overruled result still validates", validate_call_result(result) == [], str(validate_call_result(result)))

    # The status is what makes a voicemail; without it the same outcome is a
    # conversation with a person who happened to say the phrase.
    result = build_conversation_result(_attempt(CallAttemptStatus.COMPLETED), _voicemail_outcome(voicemail={"detected": False}), call_status=CallAttemptStatus.COMPLETED)
    check("without the verdict the same call is an ordinary completed one", result.disposition is Disposition.COMPLETED and len(result.transcript) == 2)

    print("\n=== the dashboard ===")
    rows = _outcomes(None, {"completed": 3, "failed": 1, "no_answer": 2, "busy": 0, "voicemail": 4, "do_not_call": 0, "not_interested": 0, "callback_requested": 0})
    voicemail_row = next((row for row in rows if row["key"] == "VOICEMAIL"), None)
    check("voicemails are their own row when results are missing", voicemail_row is not None and voicemail_row["count"] == 4, str(rows))
    check("with a warning tone", _tone_for("VOICEMAIL") == "warn")
    rows = _outcomes(None, {"completed": 3, "failed": 1, "no_answer": 2, "busy": 0, "do_not_call": 0, "not_interested": 0, "callback_requested": 0})
    check("a snapshot from before the count still renders", all(row["key"] != "VOICEMAIL" for row in rows))


# --- Configuration and wiring ---------------------------------------------------------


def check_configuration() -> None:
    """The Phase 12 settings: defaults, choices, and what a typo does."""
    print("\n=== configuration ===")
    config = Config.from_env()
    vq = config.voice_quality
    check("the defaults judge phone calls and hang up on a machine",
          vq.voicemail_detection == "heuristic" and vq.voicemail_action == "hangup" and vq.voicemail_enabled)
    check("the default greeting limit and window", vq.voicemail_max_greeting_secs == 8.0 and vq.voicemail_window_secs == 30.0)
    check("reports go to call-reports by default", vq.call_report_dir == "call-reports")
    check("noise resume is on, three times", vq.noise_resume and vq.noise_resume_max == 3)
    check("the failed-turn timeout is generous", vq.turn_response_timeout_secs == 10.0)
    check("carrier detection is off by default", config.telephony.machine_detection == "off" and not config.telephony.wants_machine_detection)
    check("the startup line describes it", "voicemail heuristic -> hangup" in config.describe_voice_quality(), config.describe_voice_quality())

    config = config_with(CALL_REPORT_DIR="off", VOICEMAIL_DETECTION="off", TELEPHONY_MACHINE_DETECTION="async", VOICEMAIL_PHRASES="one|two")
    check("reports can be switched off", config.voice_quality.call_report_dir is None)
    check("and so can detection", not config.voice_quality.voicemail_enabled and "detection off" in config.describe_voice_quality())
    check("carrier detection reads its mode", config.telephony.machine_detection == "async" and config.telephony.wants_machine_detection)
    check("the phrase list is a pipe-separated override", config.voice_quality.voicemail_phrases == ("one", "two"))

    for name, value in (("VOICEMAIL_ACTION", "shout"), ("VOICEMAIL_DETECTION", "maybe"), ("TELEPHONY_MACHINE_DETECTION", "yes"), ("TURN_RESPONSE_TIMEOUT_SECS", "-1")):
        try:
            config_with(**{name: value})
            check(f"{name}={value} is rejected", False, "did not raise")
        except ConfigError as exc:
            check(f"{name}={value} is rejected", name in str(exc))

    print("\n=== the prompt ===")
    check("the resume instruction is one the retriever skips", NOISE_RESUME_INSTRUCTION in TURN_INSTRUCTIONS and is_turn_instruction(NOISE_RESUME_INSTRUCTION))
    check("and it tells the agent not to apologise", "not apologise" in NOISE_RESUME_INSTRUCTION.lower())


def check_bot_wiring() -> None:
    """The monitor is observing, and voicemail handling exists only on a phone call."""
    print("\n=== bot wiring ===")
    import inspect

    import bot as bot_module

    source = inspect.getsource(bot_module._run_pipeline)
    check("the turn monitor observes the pipeline", "monitor.observer" in source)
    check("the report is written for a phone call", "write_call_report(" in source)
    check("the quality record travels with the outcome", "quality=quality" in source)
    check("the aggregator's stop timeout is handled", "on_user_turn_stop_timeout" in source)

    worker = SimpleNamespace(stop_when_done=None, cancel=None, queue_frames=None)
    check("no phone call, no voicemail handler", bot_module._make_voicemail_handler(worker, None, None) is None)
    from src.telephony import CallSession

    call = CallSession(provider="twilio", call_id="CA1")
    handler = bot_module._make_voicemail_handler(worker, None, call)
    check("a phone call gets one", handler is not None and handler.action == "hangup")
    check("with the built-in phrases", handler is not None and handler.detector._phrases == DEFAULT_VOICEMAIL_PHRASES)
    check("the default message names nobody it was not told about", "Sorry I missed you" in bot_module._default_voicemail_message())


async def main() -> int:
    """Run every check and report."""
    started = time.monotonic()
    check_turn_monitor()
    await check_monitor_observer()
    check_report_files()
    await check_latency_reporter()
    check_voicemail_detector()
    await check_voicemail_handler()
    await check_carrier_amd()
    check_campaign_state()
    check_configuration()
    check_bot_wiring()

    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print(f"All checks passed. ({time.monotonic() - started:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
