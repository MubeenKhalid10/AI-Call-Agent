"""What happened on each turn of a call, and whether it went right. Phase 12.

`metrics.py` already says how *fast* a response was. This module says whether
there was one, what cut it off, and what the call looked like when it was over
— the questions a person listening to a recording answers by ear, made into
numbers a script can assert on.

**One observer, four questions.**

* *Did every caller turn get a response?* A turn that closed with words in it
  and produced no bot audio within `response_timeout_secs` is a **failed
  turn**. It is logged as `turn.failed` with the reason — no response at all,
  or an inference that started and produced no audio — because from the
  caller's side those are the same silence and from the log they are not.
* *How fast did the bot stop when interrupted?* Barge-in has two halves —
  stopping the audio, then answering the new thing — and the first half has
  a latency of its own: from the interruption reaching the pipeline to the
  output transport reporting the bot stopped. Logged as `turn.interrupted`
  with that number and, once the assistant aggregator reports it, the words
  the caller heard before the cut.
* *Was the interruption real?* A cough, a door, a car horn, the agent's own
  voice echoing back — Flux opens a turn, the bot goes quiet, and the turn
  closes with **no words**. Nothing then asks the model to speak, so the bot
  sits in silence until the idle nudge fires twelve seconds later. That is a
  **spurious interruption**; `should_resume_after_noise` is the question
  `bot.py` asks before prompting the agent to pick up where it left off.
* *What did the call look like overall?* `report()` assembles every turn, every
  barge-in, every failure, the latency summary from `metrics.py`, and the
  voicemail verdict into one JSON-able record. `bot.py` writes it to
  `CALL_REPORT_DIR/<call id>.json` at the end of a phone call, which is how
  `tests/live_call.py` and `tests/phone_drill.py` — separate processes that
  cannot see inside the pipeline — find out what the bot actually did.

The monitor never changes the pipeline. It observes frames (de-duplicated on
`frame.id`, for the reason written up in `diagnostics.py`), is told a few
things by `bot.py`'s event handlers that frames alone cannot say (the
transcript, the assistant's interrupted text), and writes a log line when
something is worth one. Every timestamp comes from an injected clock, so
`tests/test_voice_quality.py` drives whole calls in milliseconds.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    FunctionCallInProgressFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSStartedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

from .reliability.observability import event
from .reliability.supervisor import stage_of

REPORT_SCHEMA_VERSION = 1

FAILED_NO_RESPONSE = "no_response"
FAILED_NO_AUDIO = "no_audio_after_llm_started"
FAILED_ERROR = "service_error"


@dataclass
class TurnRecord:
    """One caller turn and what the bot did about it. Times are seconds into the call."""

    index: int
    started_at: float | None = None
    stopped_at: float | None = None
    transcript: str | None = None
    interrupted_bot: bool = False
    llm_started_at: float | None = None
    first_token_at: float | None = None
    tts_started_at: float | None = None
    responded_at: float | None = None
    tool_call: bool = False
    error: str | None = None
    failed: str | None = None
    spurious: bool = False
    resumed_after_noise: bool = False

    @property
    def release_to_audio_ms(self) -> int | None:
        """Milliseconds from the turn being released to the bot's first audio."""
        if self.stopped_at is None or self.responded_at is None:
            return None
        return max(0, int(round((self.responded_at - self.stopped_at) * 1000)))

    def to_dict(self) -> dict[str, Any]:
        """Plain data for the report."""
        return {
            "turn": self.index,
            "started_at_secs": _r(self.started_at),
            "stopped_at_secs": _r(self.stopped_at),
            "transcript": self.transcript,
            "interrupted_bot": self.interrupted_bot,
            "spurious": self.spurious,
            "resumed_after_noise": self.resumed_after_noise,
            "responded": self.responded_at is not None,
            "late": bool(self.failed) and self.responded_at is not None,
            "release_to_audio_ms": self.release_to_audio_ms,
            "llm_started_ms": _ms_after(self.stopped_at, self.llm_started_at),
            "first_token_ms": _ms_after(self.stopped_at, self.first_token_at),
            "tts_started_ms": _ms_after(self.stopped_at, self.tts_started_at),
            "tool_call": self.tool_call,
            "error": self.error,
            "failed": self.failed,
        }


@dataclass
class BargeIn:
    """One interruption of the bot while it was speaking."""

    at_secs: float
    turn: int | None
    stop_latency_ms: int | None = None
    spoken_before_cut: str | None = None
    spurious: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain data for the report."""
        return {
            "at_secs": _r(self.at_secs),
            "turn": self.turn,
            "stop_latency_ms": self.stop_latency_ms,
            "spoken_before_cut": self.spoken_before_cut,
            "spurious": self.spurious,
        }


class TurnMonitor:
    """Watches one session's turns, interruptions and failures."""

    def __init__(
        self,
        *,
        response_timeout_secs: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the monitor.

        Args:
            response_timeout_secs: A turn with words in it that has produced no
                bot audio after this long is a failed turn. 0 disables the
                check (turns are still recorded).
            clock: Monotonic seconds. Injected for tests.
        """
        self._timeout = max(0.0, response_timeout_secs)
        self._clock = clock
        self._started_at: float | None = None
        self.turns: list[TurnRecord] = []
        self.barge_ins: list[BargeIn] = []
        self.errors: list[dict[str, Any]] = []
        self.stop_timeouts = 0
        self.noise_resumes = 0
        self.greeting_at: float | None = None
        self.agent_turns = 0
        self._bot_speaking = False
        self._pending_barge_in: BargeIn | None = None
        self._open_turn: TurnRecord | None = None
        self._watchdog: asyncio.Task | None = None
        self._observer = _MonitorObserver(self)

    @property
    def observer(self) -> BaseObserver:
        """The observer to pass to `PipelineWorker(observers=[...])`."""
        return self._observer

    @property
    def bot_speaking(self) -> bool:
        """Whether the bot is producing audio right now."""
        return self._bot_speaking

    @property
    def last_turn_over_agent(self) -> bool:
        """Whether the newest caller turn began over the agent's audio, or before it ever spoke.

        The question the voicemail detector asks: a recording talks over the
        greeting, a person waits for it. Read from the turn record rather than
        from `bot_speaking`, because by the time an event handler asks, the
        interruption may already have stopped the audio.
        """
        if self.greeting_at is None:
            return True
        return bool(self.turns) and self.turns[-1].interrupted_bot

    @property
    def failed_turns(self) -> list[TurnRecord]:
        """Every turn that got no response at all."""
        return [turn for turn in self.turns if turn.failed and turn.responded_at is None]

    @property
    def late_turns(self) -> list[TurnRecord]:
        """Every turn that was flagged and then answered anyway."""
        return [turn for turn in self.turns if turn.failed and turn.responded_at is not None]

    @property
    def caller_turns(self) -> int:
        """Caller turns that carried words."""
        return sum(1 for turn in self.turns if turn.transcript)

    def elapsed(self) -> float:
        """Seconds since the first frame, or the connect if one was noted."""
        if self._started_at is None:
            return 0.0
        return self._clock() - self._started_at

    # --- Lifecycle ----------------------------------------------------------

    def note_connected(self) -> None:
        """The call's audio is flowing. Everything is timed from here."""
        if self._started_at is None:
            self._started_at = self._clock()

    def start(self) -> None:
        """Begin the failed-turn watchdog. Idempotent."""
        if self._watchdog is None and self._timeout > 0:
            self._watchdog = asyncio.create_task(self._watch())

    def stop(self) -> None:
        """Stop the watchdog. Safe to call more than once."""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    # --- What the frames say ------------------------------------------------

    def on_user_started(self) -> None:
        """A caller turn opened."""
        if self._open_turn is not None:
            # Two consecutive starts are one turn, however many frames said so.
            return
        now = self._now()
        turn = TurnRecord(index=len(self.turns) + 1, started_at=now, interrupted_bot=self._bot_speaking)
        self.turns.append(turn)
        self._open_turn = turn
        if self._pending_barge_in is not None and self._pending_barge_in.turn is None:
            self._pending_barge_in.turn = turn.index

    def on_user_stopped(self) -> None:
        """The caller's turn was released downstream."""
        turn = self._open_turn or self._last_turn()
        if turn is None:
            return
        if turn.stopped_at is None:
            turn.stopped_at = self._now()
        self._open_turn = None

    def on_interruption(self) -> None:
        """An interruption reached the pipeline. Only one during bot audio is a barge-in."""
        if not self._bot_speaking:
            return
        if self._pending_barge_in is not None and self._pending_barge_in.stop_latency_ms is None:
            # The bot has not yet stopped from the last one: the same interruption.
            return
        now = self._now()
        turn = self._open_turn
        barge_in = BargeIn(at_secs=now, turn=turn.index if turn else None)
        if turn is not None:
            turn.interrupted_bot = True
        self.barge_ins.append(barge_in)
        self._pending_barge_in = barge_in

    def on_bot_started(self) -> None:
        """Audio is reaching the caller."""
        if self._bot_speaking:
            return
        now = self._now()
        self._bot_speaking = True
        self.agent_turns += 1
        if self.greeting_at is None and not self.turns:
            self.greeting_at = now
        turn = self._responding_turn()
        if turn is not None and turn.responded_at is None:
            turn.responded_at = now
            if turn.failed:
                logger.info(
                    event(
                        "turn.late_response",
                        latency_ms=turn.release_to_audio_ms,
                        outcome=f"turn {turn.index} answered after being flagged {turn.failed}",
                    )
                )
            else:
                logger.debug(
                    event(
                        "turn.responded",
                        latency_ms=turn.release_to_audio_ms,
                        outcome=f"turn {turn.index}",
                    )
                )

    def on_bot_stopped(self) -> None:
        """The bot's audio stopped — finished, or cut off."""
        if not self._bot_speaking:
            return
        now = self._now()
        self._bot_speaking = False
        pending = self._pending_barge_in
        if pending is not None and pending.stop_latency_ms is None:
            pending.stop_latency_ms = max(0, int(round((now - pending.at_secs) * 1000)))
            logger.info(
                event(
                    "turn.interrupted",
                    latency_ms=pending.stop_latency_ms,
                    outcome=f"barge-in #{len(self.barge_ins)}",
                )
            )

    def on_llm_started(self) -> None:
        """An inference began."""
        turn = self._responding_turn()
        if turn is not None and turn.llm_started_at is None:
            turn.llm_started_at = self._now()

    def on_llm_text(self) -> None:
        """A token came back."""
        turn = self._responding_turn()
        if turn is not None and turn.first_token_at is None:
            turn.first_token_at = self._now()

    def on_tts_started(self) -> None:
        """Synthesis began."""
        turn = self._responding_turn()
        if turn is not None and turn.tts_started_at is None:
            turn.tts_started_at = self._now()

    def on_function_call(self, name: str) -> None:
        """A tool is running; the response is legitimately slower."""
        turn = self._responding_turn()
        if turn is not None:
            turn.tool_call = True

    def on_error(self, stage: str, error: str) -> None:
        """A service failed. Attached to the turn in flight, and to the telephony log if it was the line."""
        now = self._now()
        record = {"at_secs": _r(now), "stage": stage, "error": error[:200]}
        self.errors.append(record)
        turn = self._responding_turn()
        if turn is not None and turn.error is None:
            turn.error = f"{stage}: {error[:120]}"
        if stage == "other":
            # Not STT, LLM or TTS: the transport, the serializer, the line.
            logger.warning(event("telephony.error", error=error, latency_ms=int(now * 1000)))

    # --- What the handlers say ---------------------------------------------

    def note_user_turn_stopped(self, transcript: str | None) -> None:
        """The aggregator closed a turn with this transcript (possibly empty)."""
        text = (transcript or "").strip()
        turn = self._turn_awaiting_transcript()
        if turn is None:
            # A turn the frames never showed us — realtime mode, or an event
            # without a preceding UserStartedSpeaking. Record it anyway.
            turn = TurnRecord(index=len(self.turns) + 1, stopped_at=self._now())
            self.turns.append(turn)
        turn.transcript = text
        if turn.stopped_at is None:
            turn.stopped_at = self._now()
        if not text and turn.interrupted_bot:
            turn.spurious = True
            barge_in = self._barge_in_for(turn)
            if barge_in is not None:
                barge_in.spurious = True
            logger.warning(
                event(
                    "turn.spurious_interruption",
                    outcome=f"turn {turn.index} interrupted the bot and carried no words",
                )
            )
        elif text and turn.interrupted_bot:
            barge_in = self._barge_in_for(turn)
            if barge_in is not None:
                barge_in.spurious = False

    def should_resume_after_noise(self, transcript: str | None) -> bool:
        """Whether the turn that just closed cut the bot off for nothing.

        True when the turn carried no words, it interrupted the bot, and the
        bot had actually said something before the cut — so there is something
        to pick back up. `bot.py` then prompts the agent to continue.
        """
        if (transcript or "").strip():
            return False
        turn = self._last_turn()
        if turn is None or not turn.spurious:
            return False
        barge_in = self._barge_in_for(turn)
        return barge_in is not None

    def note_noise_resume(self) -> None:
        """The agent was asked to continue after a spurious interruption."""
        self.noise_resumes += 1
        turn = self._last_turn()
        if turn is not None:
            turn.resumed_after_noise = True

    def note_assistant_turn(self, text: str | None, *, interrupted: bool) -> None:
        """The assistant aggregator reported a finished (or cut off) reply."""
        if not interrupted:
            return
        for barge_in in reversed(self.barge_ins):
            if barge_in.spoken_before_cut is None:
                barge_in.spoken_before_cut = (text or "").strip() or None
                break

    def note_stop_timeout(self) -> None:
        """A turn opened and never closed on its own; the aggregator timed it out."""
        self.stop_timeouts += 1
        turn = self._open_turn or self._last_turn()
        logger.warning(
            event(
                "turn.stop_timeout",
                outcome=f"turn {turn.index if turn else '?'} closed by the stop timeout",
            )
        )

    # --- The report ---------------------------------------------------------

    def report(
        self,
        *,
        latency: dict[str, Any] | None = None,
        voicemail: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Everything this monitor saw, as plain data.

        Args:
            latency: `LatencyReporter.summary()`, when one ran.
            voicemail: `VoicemailDetector.to_dict()`, when one ran.
            extra: Identifiers and figures only the caller knows — the call id,
                the numbers, how the session ended.
        """
        failed = self.failed_turns
        report: dict[str, Any] = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "duration_secs": _r(self.elapsed()),
            "greeted": self.greeting_at is not None,
            "greeting_at_secs": _r(self.greeting_at),
            "caller_turns": self.caller_turns,
            "agent_turns": self.agent_turns,
            "turns": [turn.to_dict() for turn in self.turns],
            "barge_ins": [barge_in.to_dict() for barge_in in self.barge_ins],
            "barge_in_count": len(self.barge_ins),
            "spurious_interruptions": sum(1 for b in self.barge_ins if b.spurious),
            "noise_resumes": self.noise_resumes,
            "failed_turns": [turn.to_dict() for turn in failed],
            "failed_turn_count": len(failed),
            "late_turn_count": len(self.late_turns),
            "stop_timeouts": self.stop_timeouts,
            "errors": list(self.errors),
            "latency": latency or {},
            "voicemail": voicemail or {"detected": False},
        }
        if extra:
            report.update({k: v for k, v in extra.items() if v is not None})
        return report

    def describe(self) -> str:
        """One line for the end-of-session log."""
        parts = [
            f"{self.caller_turns} caller turn(s)",
            f"{self.agent_turns} agent turn(s)",
            f"{len(self.barge_ins)} barge-in(s)",
        ]
        spurious = sum(1 for b in self.barge_ins if b.spurious)
        if spurious:
            parts.append(f"{spurious} spurious")
        failed = len(self.failed_turns)
        parts.append(f"{failed} failed turn(s)" if failed else "no failed turns")
        if self.late_turns:
            parts.append(f"{len(self.late_turns)} late")
        if self.stop_timeouts:
            parts.append(f"{self.stop_timeouts} stop timeout(s)")
        return " | ".join(parts)

    # --- Internals ----------------------------------------------------------

    def _now(self) -> float:
        if self._started_at is None:
            self._started_at = self._clock()
        return self._clock() - self._started_at

    def _last_turn(self) -> TurnRecord | None:
        return self.turns[-1] if self.turns else None

    def _responding_turn(self) -> TurnRecord | None:
        """The turn a response would belong to: the newest one that closed unanswered.

        A turn already flagged as failed still counts — a reply that arrives
        after the timeout is late, not absent, and the record says which.
        """
        for turn in reversed(self.turns):
            if turn.stopped_at is not None and turn.responded_at is None:
                return turn
            if turn.responded_at is not None:
                return None
        return None

    def _turn_awaiting_transcript(self) -> TurnRecord | None:
        for turn in reversed(self.turns):
            if turn.transcript is None:
                return turn
        return None

    def _barge_in_for(self, turn: TurnRecord) -> BargeIn | None:
        for barge_in in reversed(self.barge_ins):
            if barge_in.turn == turn.index:
                return barge_in
        return None

    async def _watch(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                self.check_failed_turns()
        except asyncio.CancelledError:
            return

    def check_failed_turns(self) -> list[TurnRecord]:
        """Flag turns that have waited too long for a response. Returns the newly flagged ones."""
        if self._timeout <= 0:
            return []
        now = self._now()
        flagged = []
        for turn in self.turns:
            if turn.failed or turn.stopped_at is None or turn.responded_at is not None:
                continue
            if not turn.transcript or turn.tool_call:
                continue
            waited = now - (turn.stopped_at or now)
            if waited < self._timeout:
                continue
            if turn.error:
                turn.failed = FAILED_ERROR
            elif turn.llm_started_at is not None:
                turn.failed = FAILED_NO_AUDIO
            else:
                turn.failed = FAILED_NO_RESPONSE
            flagged.append(turn)
            logger.warning(
                event(
                    "turn.failed",
                    latency_ms=int(waited * 1000),
                    error=turn.error or turn.failed,
                    outcome=f"turn {turn.index} got no response: {turn.failed}",
                )
            )
        return flagged


class _MonitorObserver(BaseObserver):
    """Feeds frames to the monitor, once each."""

    def __init__(self, monitor: TurnMonitor, *, history: int = 200) -> None:
        super().__init__()
        self._monitor = monitor
        self._seen: set[int] = set()
        self._order: list[int] = []
        self._history = history

    async def on_push_frame(self, data: FramePushed) -> None:
        """Route one frame to the monitor, de-duplicated on its id.

        A broadcast frame — every speaking and interruption frame here is one —
        is two frames, one pushed upstream and one downstream, with different
        ids and each naming the other as its sibling. They are one event, so
        the sibling's id is recorded as seen along with the frame's own.
        """
        frame = data.frame
        if self._already_seen(frame.id):
            return
        sibling = getattr(frame, "broadcast_sibling_id", None)
        if sibling is not None:
            self._already_seen(sibling)
        monitor = self._monitor
        if isinstance(frame, UserStartedSpeakingFrame):
            monitor.on_user_started()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            monitor.on_user_stopped()
        elif isinstance(frame, InterruptionFrame):
            monitor.on_interruption()
        elif isinstance(frame, BotStartedSpeakingFrame):
            monitor.on_bot_started()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            monitor.on_bot_stopped()
        elif isinstance(frame, LLMFullResponseStartFrame):
            monitor.on_llm_started()
        elif isinstance(frame, LLMTextFrame):
            monitor.on_llm_text()
        elif isinstance(frame, TTSStartedFrame):
            monitor.on_tts_started()
        elif isinstance(frame, FunctionCallInProgressFrame):
            monitor.on_function_call(getattr(frame, "function_name", ""))
        elif isinstance(frame, ErrorFrame):
            monitor.on_error(stage_of(data.source), str(getattr(frame, "error", frame)))

    def _already_seen(self, frame_id: int) -> bool:
        if frame_id in self._seen:
            return True
        if len(self._order) >= self._history:
            self._seen.discard(self._order.pop(0))
        self._order.append(frame_id)
        self._seen.add(frame_id)
        return False


# --- The report on disk ---------------------------------------------------------

# Letters, digits, underscore and dash only: a dot is what "../" is made of.
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")


def report_path(directory: str | Path, call_id: str) -> Path:
    """Where a call's report lives. The id is sanitised so it cannot escape the directory."""
    safe = _SAFE_ID.sub("_", str(call_id).strip()) or "call"
    return Path(directory) / f"{safe}.json"


def write_call_report(directory: str | Path | None, call_id: str | None, report: dict[str, Any]) -> Path | None:
    """Write a report as JSON. Never raises: a report that cannot be written is a log line.

    Returns:
        The path written, or None when there was no directory, no id, or a failure.
    """
    if not directory or not call_id:
        return None
    path = report_path(directory, call_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    except OSError as exc:
        logger.warning(event("call.report_failed", error=str(exc), outcome=str(path)))
        return None
    logger.info(event("call.report_written", outcome=str(path)))
    return path


def load_call_report(directory: str | Path, call_id: str) -> dict[str, Any] | None:
    """Read a report back, or None when it is not there (yet) or unreadable."""
    path = report_path(directory, call_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _ms_after(start: float | None, moment: float | None) -> int | None:
    if start is None or moment is None:
        return None
    return max(0, int(round((moment - start) * 1000)))


__all__ = [
    "FAILED_ERROR",
    "FAILED_NO_AUDIO",
    "FAILED_NO_RESPONSE",
    "REPORT_SCHEMA_VERSION",
    "BargeIn",
    "TurnMonitor",
    "TurnRecord",
    "load_call_report",
    "report_path",
    "write_call_report",
]
