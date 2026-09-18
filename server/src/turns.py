"""How the agent decides when the caller has started and stopped talking.

Turn-taking is the difference between a demo and something you would put on a
phone. Two mistakes make an agent feel wrong, and they pull in opposite
directions: waiting too long after the caller finishes (dead air), and cutting in
during a mid-sentence pause (rude, and it truncates the caller). A pure silence
timer cannot avoid both, because "they paused to think" and "they finished" look
identical to it.

Pipecat models this as two independent decisions, each driven by a list of
strategies: what *starts* a user turn and what *stops* it. This module builds
that configuration for whichever STT provider is in play.

There are two paths, and the choice of STT provider decides which one runs:

**Flux (default).** Deepgram Flux returns end-of-turn decisions on the same
websocket as the transcript, judged from the words and the acoustics rather than
from silence. The service tells the context aggregator so itself — it recommends
`ExternalUserTurnStrategies`, and the aggregator adopts that recommendation
**unless we pass our own**. So on this path we deliberately pass no strategies.
Overriding them here would silently switch Flux's turn detection off and leave
the transcripts, which looks like it works and feels a whole lot worse.

**Silero + Smart Turn v3 (fallback).** With classic streaming transcription there
is no server-side turn signal, so Pipecat's defaults apply: VAD and the first
transcript start the turn, and the local Smart Turn v3 model — a small ONNX
classifier that scores whether an utterance sounds finished — stops it.

VAD is configured on **both** paths. On the Flux path it does not decide where
a turn ends, but it still emits the speech-start and speech-stop frames that
`metrics.py` measures response latency between — and, since 2026-09-17, it is
what cuts the agent off when the caller talks over it (`VADBargeInStartStrategy`
below): Flux's StartOfTurn needs recognised words and was measured arriving
0.8–1.1 s after the caller began, which is a whole second of the agent talking
over them.

This module also owns the cleanup after a barge-in: `discard_interrupted_reply`
and `BargeInGate`. Stopping the agent mid-sentence is only half of handling an
interruption; the other half is that the interrupted reply is over for good —
its half-sentence leaves the context, a wordless interruption starts no
inference, and nothing picks the old answer back up.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_mute import (
    AlwaysUserMuteStrategy,
    BaseUserMuteStrategy,
    MuteUntilFirstBotCompleteUserMuteStrategy,
)
from pipecat.turns.user_start.base_user_turn_start_strategy import BaseUserTurnStartStrategy
from pipecat.turns.user_start.external_user_turn_start_strategy import (
    ExternalUserTurnStartStrategy,
)
from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
    MinWordsUserTurnStartStrategy,
)
from pipecat.turns.user_stop.external_user_turn_stop_strategy import ExternalUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from .config import Config

# Providers whose service pushes its own turn-strategy recommendation, which we
# must not override.
_SERVER_SIDE_TURN_PROVIDERS = frozenset({"deepgram_flux"})


class VADBargeInStartStrategy(BaseUserTurnStartStrategy):
    """Open the caller's turn from the local VAD, but only over the agent's voice.

    On the Flux path the turn normally opens on Flux's StartOfTurn, which is
    sent once Flux has recognised words. Measured 2026-09-17 with
    `tests/phone_drill.py barge_in`: 766 ms and 1078 ms after the caller began,
    and the agent was still audible 922 ms into the interruption — while the
    pipeline, once told, stopped it in 16 ms. The wait was all detection.

    Silero reports speech `VAD_START_SECS` (0.2 s) after it begins, so while the
    agent is speaking this strategy opens the turn — and with it the
    interruption that stops the TTS and clears the queued audio — on that
    instead. Flux's own StartOfTurn then arrives into a turn that is already
    open and is ignored; its EndOfTurn still closes the turn, so *where a turn
    ends* is decided exactly as before.

    While the agent is quiet this does nothing, deliberately: there is nothing
    to stop, and leaving the start to Flux means a cough in a silence never
    opens a turn that has to time out empty. Silero is a speech classifier, not
    an energy gate, so steady line noise does not trip it (`phone_drill.py
    noise` checks that); a sound it does take for speech stops the agent, the
    turn closes with no words, and the agent stays quiet until somebody speaks
    (`BargeInGate`; the idle nudge is what eventually breaks a long silence).
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._bot_speaking = False

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        """STOP when the caller spoke over the agent, CONTINUE otherwise."""
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, VADUserStartedSpeakingFrame) and self._bot_speaking:
            logger.debug("BARGE-IN | the caller spoke over the agent; interrupting on the VAD")
            await self.trigger_user_turn_started()
            return ProcessFrameResult.STOP
        return ProcessFrameResult.CONTINUE


def make_vad_analyzer(config: Config) -> SileroVADAnalyzer:
    """Build the Silero VAD analyser.

    The defaults in `config.py` are Pipecat's own tuned values, not numbers we
    picked. They are exposed as environment variables so a tuning session is an
    `.env` edit, but the honest default is to leave them alone: `stop_secs` in
    particular looks like the obvious "make it snappier" dial and is really the
    "start truncating callers" dial, because on the Flux path it does not affect
    responsiveness at all and on the fallback path Smart Turn is what decides.
    """
    return SileroVADAnalyzer(
        params=VADParams(
            confidence=config.vad_confidence,
            start_secs=config.vad_start_secs,
            stop_secs=config.vad_stop_secs,
            min_volume=config.vad_min_volume,
        )
    )


def make_mute_strategies(config: Config) -> list[BaseUserMuteStrategy]:
    """Decide whether the caller is ignored while the agent is talking.

    This exists for one specific, very recognisable failure: on a laptop with no
    headphones, the agent's own voice leaves the speakers, re-enters the
    microphone, and is transcribed as the caller speaking. The agent then
    answers itself, and since each reply feeds the next the conversation runs
    away with no human in it at all. In a transcript it is unmistakable — the
    "user" turn is the agent's own previous sentence, slightly garbled:

        assistant: Hi there! How can I help you today?
        user:      Hi there. How can I help
        assistant: Oh, I think we might have crossed wires there!

    **Muting is the last line of defence, not the first.** Echo is normally
    cancelled before it ever reaches us: headphones make the acoustic path
    physically impossible, browsers run AEC (Pipecat's client asks for it), and
    phone networks cancel echo on the line. Every one of those keeps barge-in
    working. Muting is what is left when none of them is enough, and it costs
    barge-in, which is a Phase 2 feature the eval suite checks — so `off` is the
    default and this is opt-in.

    The middle setting exists because the opening turn is the worst case: the
    greeting is the longest uninterrupted stretch of bot audio in the call, it
    plays before the caller has said anything, and an echo there starts the loop
    before the conversation has begun.
    """
    if config.echo_suppression == "always":
        # No barge-in: nothing the caller says while the agent is talking is
        # heard at all.
        return [AlwaysUserMuteStrategy()]

    if config.echo_suppression == "greeting":
        # Barge-in everywhere except the opening turn.
        return [MuteUntilFirstBotCompleteUserMuteStrategy()]

    return []


def make_user_aggregator_params(config: Config) -> LLMUserAggregatorParams:
    """Build the user-side aggregator parameters: VAD, turn strategies, idle detection."""
    return LLMUserAggregatorParams(
        vad_analyzer=make_vad_analyzer(config),
        user_turn_strategies=_turn_strategies(config),
        user_mute_strategies=make_mute_strategies(config),
        # Fires `on_user_turn_idle` when the caller has said nothing for this
        # long after the bot finished speaking. `bot.py` turns that into a nudge
        # rather than letting the line go quiet. 0 disables it.
        user_idle_timeout=config.user_idle_timeout_secs,
        # If audio stops arriving mid-utterance — a muted mic, a stalled WebRTC
        # track — close the turn after this long instead of waiting forever for a
        # speech-stop that will never come.
        audio_idle_timeout=1.0,
    )


_SENTENCE_END = re.compile(r"[.!?…][\"”’')\]]*(?=\s|$)")


def discard_interrupted_reply(context: LLMContext) -> None:
    """Drop the unfinished sentence of the reply the caller just cut off.

    Barge-in leaves the agent's own half-finished sentence in the context, and
    that fragment poisons the next reply. Measured here on Groq/Qwen: with

        assistant: "Sunlight looks white, but it's actually made up of"
        user:      "Sorry, never mind. What is the capital of France?"

    in the context, the model answered with two tokens — "The" — and stopped,
    imitating a turn it read as one it chose to end there. Until 2026-09-18 the
    fragment was kept and a bracketed note in English was appended to say it
    had been cut off. That note was text in the model's own history: it could
    be echoed into a reply, and with it there the model treated the old answer
    as unfinished business and went back to it.

    An interruption now invalidates the reply instead. The sentences the
    caller heard to the end stay — they were said — and the dangling one goes;
    a reply cut before its first sentence ended is removed whole. That the
    reply was interrupted is state, not prose: `AssistantTurnStoppedMessage.
    interrupted`, the transcript entry's `interrupted` flag, and the one-turn
    guidance `SalesConversation` raises from it.
    """

    def drop_fragment(messages):
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str) and content and not message.get("tool_calls"):
                ends = list(_SENTENCE_END.finditer(content))
                heard = content[: ends[-1].end()].strip() if ends else ""
                if heard:
                    message["content"] = heard
                else:
                    del messages[index]
                logger.debug(f"BARGE-IN | interrupted reply kept as {heard!r} (was {content!r})")
            # Only the most recent assistant message can be the interrupted one.
            break
        return messages

    context.transform_messages(drop_fragment)


# Sounds a recogniser writes down that are not words: what Flux makes of a
# cough, a hum or a breath over the agent's voice ("m", "Mm.", "Uh"). Yes-sounds
# ("mhm", "uh-huh") are deliberately absent — those answer something.
_NON_WORDS = frozenset("m mm mmm mmmm hm hmm hmmm h uh uhh um umm er erm ah ahh oh eh huh".split())


def is_meaningful_speech(text: str | None) -> bool:
    """Whether a transcript holds at least one word, as opposed to a noise written down."""
    words = re.sub(r"[^\w\s]", " ", (text or "").lower()).split()
    return any(word not in _NON_WORDS for word in words)


class BargeInGate(FrameProcessor):
    """What happens after the caller cuts the agent off, decided from state.

    Sits directly after the user aggregator. It knows one thing the context
    does not — that the turn now arriving *interrupted the agent* — from the
    frames themselves: an `InterruptionFrame` that passed while the bot was
    speaking. Two decisions follow from it:

    * The interrupting turn held no words (Flux wrote a hum down as "m"): the
      turn is removed from the context and **no inference is started**. The
      agent has stopped and stays stopped until somebody says something.
      Measured 2026-09-18 before this existed: "m" went to the model with the
      knowledge base attached and the agent talked for another 8 s, 2.5 s
      after a sound nobody meant as a turn.
    * It held words: `on_barge_in_turn` runs first (the retriever is told not
      to pair this turn with the question the agent was answering — "Wait."
      paired with it retrieved the old answer's passages and the model gave
      the old answer again), then the turn goes on to inference unchanged.
    """

    def __init__(
        self,
        context: LLMContext,
        *,
        on_barge_in_turn: Callable[[], None] | None = None,
        on_discarded_turn: Callable[[str], None] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._context = context
        self._on_barge_in_turn = on_barge_in_turn
        self._on_discarded_turn = on_discarded_turn
        self._bot_speaking = False
        self._barged_in = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Track the barge-in; hold back a wordless interrupting turn."""
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, InterruptionFrame) and self._bot_speaking:
            self._barged_in = True
        elif isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM and self._barged_in:
            text = _latest_user_text(frame.context)
            if text and not is_meaningful_speech(text):
                # Still barged-in: the next real utterance is the interrupting one.
                self._discard(text)
                return
            self._barged_in = False
            if self._on_barge_in_turn:
                self._on_barge_in_turn()

        await self.push_frame(frame, direction)

    def _discard(self, text: str) -> None:
        def drop_latest_user(messages):
            if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "user":
                del messages[-1]
            return messages

        self._context.transform_messages(drop_latest_user)
        logger.info(f"BARGE-IN | the interruption held no words ({text!r}); discarded, staying silent")
        if self._on_discarded_turn:
            self._on_discarded_turn(text)


def _latest_user_text(context: LLMContext) -> str:
    messages = context.messages
    if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "user":
        content = messages[-1].get("content")
        return content.strip() if isinstance(content, str) else ""
    return ""


def _turn_strategies(config: Config) -> UserTurnStrategies | None:
    """Choose turn strategies, or None to accept the STT service's recommendation."""
    if config.stt_provider in _SERVER_SIDE_TURN_PROVIDERS:
        if config.barge_in_trigger == "vad":
            # Flux's own pair — the two External strategies are exactly what
            # its recommendation consists of, so server-side end-of-turn
            # detection stays on — with the VAD barge-in in front of them.
            return UserTurnStrategies(
                start=[VADBargeInStartStrategy(), ExternalUserTurnStartStrategy()],
                stop=[ExternalUserTurnStopStrategy()],
            )
        # See the module docstring: Flux supplies its own, and overriding them
        # here would quietly disable server-side end-of-turn detection.
        return None

    if config.interrupt_min_words:
        # Replaces the default VAD-based turn start with a word-count gate. While
        # the bot is speaking it takes `min_words` before the caller is treated
        # as interrupting; once the bot is quiet a single word is enough. That
        # asymmetry is the point: it stops "mm-hm" and a cough from killing the
        # bot's turn without making the agent slow to respond in normal
        # back-and-forth.
        return UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=config.interrupt_min_words)]
        )

    # Pipecat's defaults: VAD + first transcript to start, Smart Turn v3 to stop.
    return None
