"""End-to-end latency instrumentation, one record per turn. Phase 29.

`metrics.py` already reports what Pipecat's own latency observer measures —
silence to first audio, and the earliest TTFB of each service — and
`voice_quality.py` says whether a turn got a response at all. Neither can say
*where* a slow turn spent its time, which is the question a caller waiting
twelve seconds is asking. This module answers it by timestamping every stage
of the loop between the caller's speech ending and the bot's first audio:

    user speech end
      -> STT final transcript             (stt_final_ms)
      -> knowledge base retrieval         (kb_ms; a hook from `retrieval.py`)
      -> LLM request, first token, end    (llm_ttft_ms, llm_total_ms)
      -> tool calls, each                 (tool_ms, and every tool by name)
      -> TTS request, first audio chunk   (tts_ttfa_ms)
      -> first audio reaching the caller  (total_ms)

**One tracing system, not two.** The frames come from the same observer
mechanism `diagnostics.py` and `voice_quality.py` use (an observer sees a
frame once per processor hop, so it de-duplicates on `frame.id` and the
broadcast sibling, for the reason written up in `diagnostics.py`). Turns are
numbered the way `TurnMonitor` numbers them — one per caller turn, opened by
`UserStartedSpeakingFrame` or, when no speaking frames exist (text-mode
evals), by the aggregator's turn-stopped event — so `turn 4` here is `turn 4`
there. Every log line carries the existing call context (`trace`, `call`,
`attempt`, …) because it goes through the same loguru context every other
line uses; the summary names the trace id explicitly so the record on disk can
be matched to the logs.

**Two clocks.** Stage timestamps are monotonic seconds from an injected clock
(tests advance it by hand — nothing here depends on wall-clock timing); each
turn also records the wall-clock moment it opened, so a per-turn line can be
lined up with the transcript's timestamps.

**What it does not do.** It changes nothing in the pipeline: it observes,
it is told two things by `retrieval.py` (retrieval started, retrieval done),
and it writes log lines. It never logs message text, arguments, results or
anything that could carry a secret — stage names, tool names, milliseconds,
and an outcome word per stage. Error text goes through `event()`, which
redacts.

**Reading the numbers.**

- `total_ms` is end-of-user-speech to the output transport reporting the
  bot started speaking — the number the caller feels. The anchor is the VAD's
  stop when there is one, else the turn-end decision (Flux delivers the
  transcript and the end of turn together, so on the phone path they are
  the same moment). Pipecat's own observer, reported by `metrics.py`,
  additionally subtracts the VAD's `stop_secs`; this one does not, so the
  two `total`s differ by that much, and this one says exactly which frame it
  started from.
- `stt_final_ms` can be zero: Flux sends the final transcript *in* the
  end-of-turn message, so the transcript is never later than the anchor.
- `llm_ttft_ms` runs from the first LLM request of the turn to the first
  token of the turn. On a tool turn that first token usually arrives in the
  *second* request, after the tool ran, so TTFT there includes the tool;
  `llm_requests` says how many requests the turn took and each one's own
  first token, and the retry warnings `services.py` logs land in the same
  window when the provider throttles.
- Gaps between stages are reported too (`turn_end_to_llm_ms`,
  `first_token_to_tts_ms`, `tts_audio_to_played_ms`): time in the pipeline
  that no service measures, which is where queuing hides.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
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
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection

from .prompts import is_injected_block
from .reliability.observability import current_trace_id, event
from .reliability.supervisor import stage_of

# Turn kinds. Only a `response` turn has an end-of-user-speech anchor and
# therefore a total; the other two are the bot speaking on its own.
KIND_RESPONSE = "response"
KIND_GREETING = "greeting"
KIND_UNPROMPTED = "unprompted"

# The stages an average is reported for, in the order the summary prints
# them: (key in `TurnLatency.to_dict()`, label).
SUMMARY_STAGES = (
    ("stt_final_ms", "STT finalisation"),
    ("kb_ms", "KB retrieval"),
    ("llm_ttft_ms", "LLM TTFT"),
    ("llm_total_ms", "LLM total"),
    ("tool_ms", "tool"),
    ("tts_ttfa_ms", "TTS TTFA"),
    ("total_ms", "TOTAL end-of-user-speech -> first-audio"),
)


@dataclass
class ToolTiming:
    """One tool call: from the LLM service starting it to its result being pushed."""

    name: str
    call_id: str
    started_at: float
    finished_at: float | None = None

    @property
    def duration_ms(self) -> int | None:
        """How long the tool took, or None while it is still running."""
        return _ms_between(self.started_at, self.finished_at)

    def to_dict(self) -> dict[str, Any]:
        """Plain data for the report. The name only: never the arguments or the result."""
        return {"name": self.name, "duration_ms": self.duration_ms}


@dataclass
class LLMRequestTiming:
    """One request to the LLM: pushed to the service, first token, finished."""

    started_at: float
    first_token_at: float | None = None
    finished_at: float | None = None
    #: What the service itself measured as request-to-first-byte, from its
    #: `MetricsFrame`. A cross-check on `ttft_ms`: the difference between the
    #: two is time the token spent in the pipeline rather than at the vendor.
    ttfb_reported_ms: int | None = None

    @property
    def ttft_ms(self) -> int | None:
        return _ms_between(self.started_at, self.first_token_at)

    @property
    def duration_ms(self) -> int | None:
        return _ms_between(self.started_at, self.finished_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ttft_ms": self.ttft_ms,
            "duration_ms": self.duration_ms,
            "ttfb_reported_ms": self.ttfb_reported_ms,
        }


@dataclass
class TurnLatency:
    """Every timestamp of one turn, in monotonic seconds, and the figures derived from them."""

    index: int
    kind: str = KIND_RESPONSE
    opened_at_utc: str | None = None
    # The caller's side.
    speech_started_at: float | None = None
    speech_ended_at: float | None = None  # VADUserStoppedSpeaking, when a VAD runs
    turn_ended_at: float | None = None  # UserStoppedSpeaking: the end-of-turn decision
    transcript_words: int | None = None
    # STT.
    stt_first_interim_at: float | None = None
    stt_first_final_at: float | None = None
    stt_last_final_at: float | None = None
    stt_ttfb_reported_ms: int | None = None
    # Knowledge base, told by `retrieval.py`.
    kb_started_at: float | None = None
    kb_finished_at: float | None = None
    kb_outcome: str | None = None
    # The LLM, one entry per request (a tool turn makes two).
    llm_requests: list[LLMRequestTiming] = field(default_factory=list)
    tools: list[ToolTiming] = field(default_factory=list)
    # TTS and the audio.
    tts_started_at: float | None = None
    tts_first_audio_at: float | None = None
    tts_ttfb_reported_ms: int | None = None
    first_audio_at: float | None = None  # BotStartedSpeaking: audio reaching the caller
    audio_stopped_at: float | None = None
    # What went wrong, if anything.
    interrupted: bool = False
    errors: list[str] = field(default_factory=list)
    closed: bool = False

    # --- Anchors --------------------------------------------------------------

    @property
    def user_end_at(self) -> float | None:
        """End of the caller's speech: the VAD's stop when there is one, else the turn end."""
        if self.speech_ended_at is not None:
            return self.speech_ended_at
        return self.turn_ended_at

    @property
    def llm_started_at(self) -> float | None:
        return self.llm_requests[0].started_at if self.llm_requests else None

    @property
    def llm_first_token_at(self) -> float | None:
        for request in self.llm_requests:
            if request.first_token_at is not None:
                return request.first_token_at
        return None

    @property
    def llm_finished_at(self) -> float | None:
        finished = [r.finished_at for r in self.llm_requests if r.finished_at is not None]
        return max(finished) if finished else None

    # --- Figures ---------------------------------------------------------------

    @property
    def stt_final_ms(self) -> int | None:
        """End of speech to the last final transcript before the LLM ran (0 when it came first)."""
        return _ms_between(self.user_end_at, self.stt_last_final_at)

    @property
    def stt_first_interim_ms(self) -> int | None:
        """Start of speech to the first interim transcript."""
        return _ms_between(self.speech_started_at, self.stt_first_interim_at)

    @property
    def vad_end_to_turn_end_ms(self) -> int | None:
        """The cost of turn detection: the VAD's stop to the end-of-turn decision."""
        return _ms_between(self.speech_ended_at, self.turn_ended_at)

    @property
    def user_speech_ms(self) -> int | None:
        """How long the caller spoke: speech start to the end-of-speech anchor."""
        return _ms_between(self.speech_started_at, self.user_end_at)

    @property
    def kb_ms(self) -> int | None:
        return _ms_between(self.kb_started_at, self.kb_finished_at)

    @property
    def llm_ttft_ms(self) -> int | None:
        """First LLM request of the turn to the first token of the turn."""
        return _ms_between(self.llm_started_at, self.llm_first_token_at)

    @property
    def llm_total_ms(self) -> int | None:
        """First LLM request of the turn to the end of its last request."""
        return _ms_between(self.llm_started_at, self.llm_finished_at)

    @property
    def tool_ms(self) -> int | None:
        """Every finished tool call in the turn, summed. None when there were none."""
        durations = [t.duration_ms for t in self.tools if t.duration_ms is not None]
        return sum(durations) if durations else None

    @property
    def tts_ttfa_ms(self) -> int | None:
        """TTS request to the first audio chunk out of the TTS service."""
        return _ms_between(self.tts_started_at, self.tts_first_audio_at)

    @property
    def total_ms(self) -> int | None:
        """End of the caller's speech to the bot's first audio reaching them."""
        return _ms_between(self.user_end_at, self.first_audio_at)

    @property
    def turn_end_to_llm_ms(self) -> int | None:
        """Turn end to the LLM request: aggregation, retrieval and guidance."""
        return _ms_between(self.turn_ended_at, self.llm_started_at)

    @property
    def first_token_to_tts_ms(self) -> int | None:
        """First token to the TTS request: sentence aggregation and the spoken-text filter."""
        return _ms_between(self.llm_first_token_at, self.tts_started_at)

    @property
    def tts_audio_to_played_ms(self) -> int | None:
        """First TTS chunk to the transport reporting the bot speaking."""
        return _ms_between(self.tts_first_audio_at, self.first_audio_at)

    @property
    def responded(self) -> bool:
        return self.first_audio_at is not None

    @property
    def outcome(self) -> str:
        """One word on how the turn ended, for the line and the report."""
        if self.responded:
            return "interrupted" if self.interrupted else "responded"
        if self.errors:
            return "error"
        if self.interrupted:
            return "interrupted"
        if self.llm_requests:
            return "no_audio"
        return "no_response"

    def to_dict(self) -> dict[str, Any]:
        """Plain data for the call report. Milliseconds only; never text."""
        return {
            "turn": self.index,
            "kind": self.kind,
            "opened_at_utc": self.opened_at_utc,
            "outcome": self.outcome,
            "transcript_words": self.transcript_words,
            "user_speech_ms": self.user_speech_ms,
            "stt_first_interim_ms": self.stt_first_interim_ms,
            "stt_final_ms": self.stt_final_ms,
            "stt_ttfb_reported_ms": self.stt_ttfb_reported_ms,
            "vad_end_to_turn_end_ms": self.vad_end_to_turn_end_ms,
            "kb_ms": self.kb_ms,
            "kb_outcome": self.kb_outcome,
            "turn_end_to_llm_ms": self.turn_end_to_llm_ms,
            "llm_requests": [r.to_dict() for r in self.llm_requests],
            "llm_ttft_ms": self.llm_ttft_ms,
            "llm_total_ms": self.llm_total_ms,
            "tools": [t.to_dict() for t in self.tools],
            "tool_ms": self.tool_ms,
            "first_token_to_tts_ms": self.first_token_to_tts_ms,
            "tts_ttfa_ms": self.tts_ttfa_ms,
            "tts_ttfb_reported_ms": self.tts_ttfb_reported_ms,
            "tts_audio_to_played_ms": self.tts_audio_to_played_ms,
            "total_ms": self.total_ms,
            "interrupted": self.interrupted,
            "errors": list(self.errors),
        }

    def describe(self) -> str:
        """The per-turn line: `TURN 4 | STT final 0.35s | KB 0.21s | ... | TOTAL ... 2.11s`."""
        parts = [f"TURN {self.index}" + ("" if self.kind == KIND_RESPONSE else f" ({self.kind})")]
        parts.append(f"STT final {_secs(self.stt_final_ms)}")
        if self.kb_outcome == "skipped":
            kb = "skipped"
        elif self.kb_ms is not None and self.kb_outcome:
            kb = f"{_secs(self.kb_ms)} ({self.kb_outcome})"
        else:
            kb = _secs(self.kb_ms)
        parts.append(f"KB retrieval {kb}")
        parts.append(f"LLM TTFT {_secs(self.llm_ttft_ms)}")
        llm_total = _secs(self.llm_total_ms)
        if len(self.llm_requests) > 1:
            llm_total += f" ({len(self.llm_requests)} requests)"
        parts.append(f"LLM total {llm_total}")
        if self.tools:
            names = ", ".join(
                f"{t.name} {_secs(t.duration_ms)}" for t in self.tools
            )
            parts.append(f"tool {_secs(self.tool_ms)} ({names})")
        else:
            parts.append("tool none")
        parts.append(f"TTS TTFA {_secs(self.tts_ttfa_ms)}")
        parts.append(f"TOTAL end-of-user-speech -> first-audio {_secs(self.total_ms)}")
        if self.outcome != "responded":
            parts.append(self.outcome)
        return " | ".join(parts)


class LatencyTracker:
    """Records the stages of every turn and logs a line per turn and a summary per call.

    Attach `tracker.observer` to the `PipelineWorker`, set it on the
    `KnowledgeRetriever` (`retriever.latency = tracker`) so retrieval is
    timed, call `note_user_turn_stopped` from the user aggregator's handler
    (the only way a text-mode turn is seen), and `log_summary()` at the end.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] | None = None,
        log_each_turn: bool = True,
    ) -> None:
        """Create the tracker.

        Args:
            clock: Monotonic seconds. Injected for tests.
            wall: Wall-clock `datetime` for the per-turn `opened_at_utc`.
            log_each_turn: Whether to log the per-turn line. The summary is
                always logged.
        """
        self._clock = clock
        self._wall = wall or (lambda: datetime.now(UTC))
        self._log_each_turn = log_each_turn
        self.turns: list[TurnLatency] = []
        self._current: TurnLatency | None = None
        self._bot_speaking = False
        self._observer = _TrackerObserver(self)

    @property
    def observer(self) -> BaseObserver:
        """The observer to pass to `PipelineWorker(observers=[...])`."""
        return self._observer

    @property
    def current(self) -> TurnLatency | None:
        """The turn in flight, if any."""
        return self._current

    # --- The caller's side --------------------------------------------------

    def on_user_started(self) -> None:
        """A caller turn opened. A second start before the turn ended is the same turn."""
        current = self._current
        if current is not None and not current.closed and current.kind == KIND_RESPONSE and current.turn_ended_at is None:
            return
        self._open(KIND_RESPONSE).speech_started_at = self._clock()

    def on_vad_user_stopped(self) -> None:
        """The VAD heard silence: the caller's speech actually ended."""
        turn = self._response_turn()
        if turn is not None and turn.speech_ended_at is None:
            turn.speech_ended_at = self._clock()

    def on_user_stopped(self) -> None:
        """The end-of-turn decision: the turn was released downstream."""
        turn = self._response_turn()
        if turn is None:
            turn = self._open(KIND_RESPONSE)
        if turn.turn_ended_at is None:
            turn.turn_ended_at = self._clock()

    def note_user_turn_stopped(self, transcript: str | None) -> None:
        """The aggregator closed a turn with this transcript.

        In audio mode the frames already opened and ended the turn, and this
        only records how many words it carried. In text mode (the eval
        harness) there are no speaking frames at all, so this is what opens
        and ends the turn — and the turn end is the only anchor a text turn
        has, which is why `total_ms` is measured from it.
        """
        turn = self._response_turn()
        if turn is None:
            turn = self._open(KIND_RESPONSE)
        if turn.turn_ended_at is None:
            turn.turn_ended_at = self._clock()
        turn.transcript_words = len((transcript or "").split())

    def on_context(self, messages: list) -> None:
        """A context is on its way to the LLM: the one signal every mode has.

        In audio mode the speaking frames and the transcript opened the turn
        already, and this changes nothing. In text mode (the eval harness)
        there are no speaking frames and no aggregator event — the harness
        appends the caller's text to the context and runs the LLM — so this
        is what opens and anchors a text turn. Observed 2026-09-14: without
        it a six-turn text run was recorded as one turn with nine requests.

        Two kinds of context are *not* a caller turn: one whose newest user
        message is the application's own instruction (the opening, the idle
        nudge, the stage block appended by the director — recognised by the
        same `is_injected_block` rule the retriever uses), and one pushed to
        continue the same turn after a tool result. Neither opens anything.
        """
        latest = None
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    latest = content.strip()
                    break
        if latest is None or is_injected_block(latest):
            return
        turn = self._response_turn()
        if turn is not None:
            if not turn.llm_requests:
                return  # The same turn, before its first request.
            last_request = turn.llm_requests[-1].started_at
            if any(t.finished_at is not None and t.finished_at >= last_request for t in turn.tools):
                return  # The same turn, continuing after a tool result.
        turn = self._open(KIND_RESPONSE)
        turn.turn_ended_at = self._clock()
        turn.transcript_words = len(latest.split())

    def on_interim_transcript(self) -> None:
        turn = self._response_turn()
        if turn is not None and turn.stt_first_interim_at is None:
            turn.stt_first_interim_at = self._clock()

    def on_final_transcript(self) -> None:
        turn = self._response_turn()
        if turn is None:
            # A transcript with no speaking frame before it (text mode, or
            # an STT that reports no VAD): it opens the turn.
            turn = self._open(KIND_RESPONSE)
        now = self._clock()
        if turn.stt_first_final_at is None:
            turn.stt_first_final_at = now
        if not turn.llm_requests:
            turn.stt_last_final_at = now

    # --- Retrieval (told by `retrieval.py`) -----------------------------------

    def retrieval_started(self) -> None:
        turn = self._turn_for_response()
        if turn.kb_started_at is None:
            turn.kb_started_at = self._clock()

    def retrieval_finished(self, outcome: str) -> None:
        """Retrieval is over. `outcome` is a word — passages found, nothing, skipped, failed."""
        turn = self._turn_for_response()
        if turn.kb_started_at is None:
            turn.kb_started_at = self._clock()
        turn.kb_finished_at = self._clock()
        turn.kb_outcome = outcome

    # --- The LLM and the tools -----------------------------------------------

    def on_llm_started(self) -> None:
        """A request went to the LLM service."""
        turn = self._turn_for_response()
        turn.llm_requests.append(LLMRequestTiming(started_at=self._clock()))

    def on_llm_text(self) -> None:
        turn = self._current
        if turn is None or turn.closed or not turn.llm_requests:
            return
        request = turn.llm_requests[-1]
        if request.first_token_at is None:
            request.first_token_at = self._clock()

    def on_llm_finished(self) -> None:
        turn = self._current
        if turn is None or turn.closed or not turn.llm_requests:
            return
        request = turn.llm_requests[-1]
        if request.finished_at is None:
            request.finished_at = self._clock()

    def on_function_call_started(self, name: str, call_id: str) -> None:
        turn = self._turn_for_response()
        if any(t.call_id == call_id for t in turn.tools):
            return
        turn.tools.append(ToolTiming(name=name, call_id=call_id, started_at=self._clock()))

    def on_function_call_finished(self, name: str, call_id: str) -> None:
        turn = self._current
        if turn is None or turn.closed:
            return
        for tool in turn.tools:
            if tool.call_id == call_id and tool.finished_at is None:
                tool.finished_at = self._clock()
                return

    def on_metrics(self, processor: object, ttfb_secs: float) -> None:
        """A service reported its own request-to-first-byte."""
        turn = self._current
        if turn is None or turn.closed:
            return
        stage = stage_of(processor)
        value = int(round(ttfb_secs * 1000))
        if stage == "llm" and turn.llm_requests:
            request = turn.llm_requests[-1]
            if request.ttfb_reported_ms is None:
                request.ttfb_reported_ms = value
        elif stage == "tts" and turn.tts_ttfb_reported_ms is None:
            turn.tts_ttfb_reported_ms = value
        elif stage == "stt" and turn.stt_ttfb_reported_ms is None:
            turn.stt_ttfb_reported_ms = value

    # --- TTS and the audio -----------------------------------------------------

    def on_tts_started(self) -> None:
        turn = self._turn_for_response()
        if turn.tts_started_at is None:
            turn.tts_started_at = self._clock()

    def on_tts_audio(self) -> None:
        turn = self._current
        if turn is None or turn.closed:
            return
        if turn.tts_first_audio_at is None:
            turn.tts_first_audio_at = self._clock()

    def on_bot_started(self) -> None:
        """Audio is reaching the caller."""
        if self._bot_speaking:
            return
        self._bot_speaking = True
        turn = self._turn_for_response()
        if turn.first_audio_at is None:
            turn.first_audio_at = self._clock()

    def on_bot_stopped(self) -> None:
        """The bot's audio stopped: the turn is over, one way or the other."""
        if not self._bot_speaking:
            return
        self._bot_speaking = False
        now = self._clock()
        # The stop belongs to the turn that was speaking, which is not always
        # the current one: a barge-in opens the next turn before the output
        # transport reports the previous one's audio stopped. Closing the
        # current turn on that stop would end a turn that has not started
        # its response.
        for turn in reversed(self.turns):
            if turn.first_audio_at is not None and turn.audio_stopped_at is None:
                turn.audio_stopped_at = now
                if turn is self._current:
                    self._close(turn)
                return

    def on_interruption(self) -> None:
        """An interruption reached the pipeline. Only one during a response counts."""
        turn = self._current
        if turn is None or turn.closed:
            return
        in_flight = self._bot_speaking or (turn.llm_requests and turn.llm_finished_at is None)
        if in_flight:
            turn.interrupted = True

    def on_error(self, stage: str, error: str) -> None:
        turn = self._current
        if turn is None or turn.closed:
            return
        turn.errors.append(f"{stage}: {error[:120]}")

    # --- The output -------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """The call's latency as plain data: every turn, the averages, p95 of the total."""
        self._flush()
        responses = [t for t in self.turns if t.kind == KIND_RESPONSE]
        averages: dict[str, dict[str, Any]] = {}
        for key, _label in SUMMARY_STAGES:
            samples = [v for v in (getattr(t, key) for t in responses) if v is not None]
            averages[key] = {
                "avg_ms": int(round(sum(samples) / len(samples))) if samples else None,
                "n": len(samples),
            }
        totals = sorted(t.total_ms for t in responses if t.total_ms is not None)
        return {
            "trace_id": current_trace_id(),
            "turns": [t.to_dict() for t in self.turns],
            "response_turns": len(responses),
            "responded_turns": len(totals),
            "interrupted_turns": sum(1 for t in responses if t.interrupted),
            "error_turns": sum(1 for t in responses if t.errors),
            "averages": averages,
            "total_p95_ms": _percentile(totals, 0.95),
            "total_max_ms": totals[-1] if totals else None,
        }

    def log_summary(self) -> None:
        """Log the per-call summary: averages per stage and p95 of the total."""
        summary = self.summary()
        responses = summary["response_turns"]
        trace = summary["trace_id"]
        head = (
            f"LATENCY CALL SUMMARY | {responses} response turn(s), "
            f"{summary['responded_turns']} with first audio"
        )
        if summary["interrupted_turns"]:
            head += f", {summary['interrupted_turns']} interrupted"
        if summary["error_turns"]:
            head += f", {summary['error_turns']} with errors"
        if trace:
            head += f" | trace={trace}"
        logger.info(head)
        if not responses:
            return
        for key, label in SUMMARY_STAGES:
            figures = summary["averages"][key]
            line = f"LATENCY CALL SUMMARY |   {label:<42} avg {_secs(figures['avg_ms'])}"
            if key == "total_ms":
                line += f"  p95 {_secs(summary['total_p95_ms'])}  max {_secs(summary['total_max_ms'])}"
            line += f"  (n={figures['n']})"
            logger.info(line)

    # --- Internals ---------------------------------------------------------------

    def _open(self, kind: str) -> TurnLatency:
        current = self._current
        if current is not None and not current.closed:
            self._close(current)
        turn = TurnLatency(
            index=len(self.turns) + 1,
            kind=kind,
            opened_at_utc=self._wall().isoformat(timespec="milliseconds"),
        )
        self.turns.append(turn)
        self._current = turn
        return turn

    def _response_turn(self) -> TurnLatency | None:
        """The open caller turn, if the current turn is one."""
        turn = self._current
        if turn is None or turn.closed or turn.kind != KIND_RESPONSE:
            return None
        return turn

    def _turn_for_response(self) -> TurnLatency:
        """The turn a response stage belongs to, opening a bot-only turn when there is none."""
        turn = self._current
        if turn is not None and not turn.closed:
            return turn
        return self._open(KIND_GREETING if not self.turns else KIND_UNPROMPTED)

    def _close(self, turn: TurnLatency) -> None:
        if turn.closed:
            return
        turn.closed = True
        if self._log_each_turn:
            logger.info("LATENCY | " + turn.describe())
            logger.info(
                "LATENCY | "
                + event(
                    "turn.breakdown",
                    turn=turn.index,
                    kind=turn.kind,
                    outcome=turn.outcome,
                    user_speech_ms=turn.user_speech_ms,
                    stt_final_ms=turn.stt_final_ms,
                    vad_end_to_turn_end_ms=turn.vad_end_to_turn_end_ms,
                    kb_ms=turn.kb_ms,
                    turn_end_to_llm_ms=turn.turn_end_to_llm_ms,
                    llm_ttft_ms=turn.llm_ttft_ms,
                    llm_total_ms=turn.llm_total_ms,
                    llm_requests=len(turn.llm_requests) or None,
                    tool_ms=turn.tool_ms,
                    tools=",".join(t.name for t in turn.tools) or None,
                    first_token_to_tts_ms=turn.first_token_to_tts_ms,
                    tts_ttfa_ms=turn.tts_ttfa_ms,
                    tts_audio_to_played_ms=turn.tts_audio_to_played_ms,
                    total_ms=turn.total_ms,
                    error="; ".join(turn.errors) or None,
                )
            )

    def _flush(self) -> None:
        """Close whatever turn is still open, so the summary and the report include it."""
        current = self._current
        if current is not None and not current.closed:
            self._close(current)


class _TrackerObserver(BaseObserver):
    """Feeds frames to the tracker, once each."""

    def __init__(self, tracker: LatencyTracker, *, history: int = 400) -> None:
        super().__init__()
        self._tracker = tracker
        self._seen: set[int] = set()
        self._order: list[int] = []
        self._history = history

    async def on_push_frame(self, data: FramePushed) -> None:
        """Route one frame to the tracker, de-duplicated on its id and its broadcast sibling."""
        frame = data.frame
        if self._already_seen(frame.id):
            return
        sibling = getattr(frame, "broadcast_sibling_id", None)
        if sibling is not None:
            self._already_seen(sibling)
        tracker = self._tracker
        if isinstance(frame, UserStartedSpeakingFrame):
            tracker.on_user_started()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            tracker.on_vad_user_stopped()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            tracker.on_user_stopped()
        elif isinstance(frame, LLMContextFrame):
            if data.direction == FrameDirection.DOWNSTREAM:
                tracker.on_context(list(getattr(frame.context, "messages", []) or []))
        elif isinstance(frame, InterimTranscriptionFrame):
            tracker.on_interim_transcript()
        elif isinstance(frame, TranscriptionFrame):
            tracker.on_final_transcript()
        elif isinstance(frame, LLMFullResponseStartFrame):
            tracker.on_llm_started()
        elif isinstance(frame, LLMTextFrame):
            tracker.on_llm_text()
        elif isinstance(frame, LLMFullResponseEndFrame):
            tracker.on_llm_finished()
        elif isinstance(frame, FunctionCallInProgressFrame):
            tracker.on_function_call_started(frame.function_name, frame.tool_call_id)
        elif isinstance(frame, FunctionCallResultFrame):
            tracker.on_function_call_finished(frame.function_name, frame.tool_call_id)
        elif isinstance(frame, TTSStartedFrame):
            tracker.on_tts_started()
        elif isinstance(frame, TTSAudioRawFrame):
            tracker.on_tts_audio()
        elif isinstance(frame, BotStartedSpeakingFrame):
            tracker.on_bot_started()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            tracker.on_bot_stopped()
        elif isinstance(frame, InterruptionFrame):
            tracker.on_interruption()
        elif isinstance(frame, ErrorFrame):
            tracker.on_error(stage_of(data.source), str(getattr(frame, "error", frame)))
        elif isinstance(frame, MetricsFrame):
            for item in frame.data:
                if isinstance(item, TTFBMetricsData):
                    tracker.on_metrics(item.processor, item.value)

    def _already_seen(self, frame_id: int) -> bool:
        if frame_id in self._seen:
            return True
        if len(self._order) >= self._history:
            self._seen.discard(self._order.pop(0))
        self._order.append(frame_id)
        self._seen.add(frame_id)
        return False


def _ms_between(start: float | None, end: float | None) -> int | None:
    """Whole milliseconds from start to end, never negative, or None if either is missing."""
    if start is None or end is None:
        return None
    return max(0, int(round((end - start) * 1000)))


def _secs(ms: int | None) -> str:
    """Milliseconds as `1.42s`, or `n/a`."""
    return "n/a" if ms is None else f"{ms / 1000:.2f}s"


def _percentile(ordered: list[int], fraction: float) -> int | None:
    """Nearest-rank percentile over an already-sorted list, or None when empty."""
    if not ordered:
        return None
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


__all__ = [
    "KIND_GREETING",
    "KIND_RESPONSE",
    "KIND_UNPROMPTED",
    "SUMMARY_STAGES",
    "LLMRequestTiming",
    "LatencyTracker",
    "ToolTiming",
    "TurnLatency",
]
