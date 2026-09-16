"""A pipeline observer that makes stalls and interruptions diagnosable.

Pipecat reports service failures as `ErrorFrame` with `fatal=False`. The client
shows them, but the session keeps running and the server log stays silent — so a
bot that stops answering looks identical to a bot that is simply waiting. This
observer logs every error, logs each stage of the turn cycle so you can see
exactly where the loop stops advancing, and calls out barge-in when it happens.

Barge-in is worth a line of its own because it is the one behaviour you cannot
confirm from a transcript. The transcript of a clean interruption and the
transcript of a bot that talked over the caller look the same; only the ordering
of `InterruptionFrame` against `BotStoppedSpeakingFrame` tells them apart.

Wire it in via `PipelineWorker(..., observers=[TurnDiagnostics(...)])`.
"""

from __future__ import annotations

from collections import deque

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
    TTSStartedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

from .monitoring.instruments import BARGE_INS

# The turn cycle, in the order a healthy turn produces them. Whichever line is
# missing from the log tells you which stage stopped.
_STAGES = {
    UserStartedSpeakingFrame: "1 user started speaking  (turn opened)",
    UserStoppedSpeakingFrame: "2 user stopped speaking  (end of turn detected)",
    TranscriptionFrame: "3 transcript received    (STT ok)",
    LLMFullResponseStartFrame: "4 LLM started            (request accepted)",
    LLMFullResponseEndFrame: "5 LLM finished",
    TTSStartedFrame: "6 TTS started            (audio synthesising)",
    BotStartedSpeakingFrame: "7 bot started speaking   (audio reaching caller)",
    BotStoppedSpeakingFrame: "8 bot finished speaking  (turn complete)",
}


class TurnDiagnostics(BaseObserver):
    """Logs errors, barge-in, and turn-cycle progress."""

    def __init__(self, *, history: int = 200) -> None:
        """Create the observer.

        Args:
            history: How many recent frame IDs to remember for de-duplication.
                Only needs to cover the hops of one frame through the pipeline.
        """
        super().__init__()
        self._turn = 0
        self._seen_types: set[type] = set()
        self._bot_speaking = False
        self._barge_ins = 0
        # An observer sees a frame once per processor *hop*, not once per frame:
        # a single UserStartedSpeakingFrame crossing a seven-stage pipeline
        # arrives here seven times. Without this every turn would be counted
        # seven times over and every error logged seven times. Bounded so a long
        # session does not grow a set of every frame ID it ever saw.
        self._seen_ids: set[int] = set()
        self._id_history: deque[int] = deque(maxlen=history)

    @property
    def barge_ins(self) -> int:
        """How many times the caller interrupted the bot mid-utterance."""
        return self._barge_ins

    async def on_push_frame(self, data: FramePushed):
        """Log turn-cycle stages once each, and every error and barge-in unconditionally."""
        frame = data.frame

        if self._already_seen(frame.id):
            return

        if isinstance(frame, ErrorFrame):
            fatal = getattr(frame, "fatal", False)
            # Logged at ERROR level so the session's error counter — a loguru
            # sink, see metrics.py — picks it up along with everything else that
            # goes wrong.
            logger.error(
                f"PIPELINE ERROR (fatal={fatal}) from {data.source}: "
                f"{getattr(frame, 'error', frame)}"
            )
            return

        # Interruptions are pushed by whichever component owns turn detection —
        # Flux on the server-side path, the VAD strategies otherwise. Only the
        # ones that land while audio is playing are barge-in; the rest are
        # bookkeeping at the start of an ordinary turn.
        if isinstance(frame, InterruptionFrame):
            if self._bot_speaking:
                self._barge_ins += 1
                self._bot_speaking = False
                BARGE_INS.inc()  # Phase 22: the same count, scrapeable.
                logger.info(f"BARGE-IN | caller interrupted the bot (#{self._barge_ins})")
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False

        stage = _STAGES.get(type(frame))
        if stage is None:
            return

        # A new user turn resets the cycle so each turn logs a clean sequence.
        if isinstance(frame, UserStartedSpeakingFrame):
            self._turn += 1
            self._seen_types.clear()

        # A turn can legitimately produce several frames of the same type (two
        # transcripts, a reply synthesised in three sentences). The first one is
        # what says the stage was reached; the rest are noise.
        if type(frame) in self._seen_types:
            return
        self._seen_types.add(type(frame))

        logger.debug(f"[turn {self._turn}] {stage}")

    def _already_seen(self, frame_id: int) -> bool:
        """Record a frame ID and report whether it had been seen before."""
        if frame_id in self._seen_ids:
            return True
        if len(self._id_history) == self._id_history.maxlen:
            self._seen_ids.discard(self._id_history[0])
        self._id_history.append(frame_id)
        self._seen_ids.add(frame_id)
        return False
