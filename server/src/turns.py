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

VAD is configured on **both** paths. On the Flux path it no longer drives
turn-taking, but it still emits the speech-start and speech-stop frames that
`metrics.py` measures response latency between, so removing it would cost us the
measurements Phase 2 is meant to produce.

This module also owns `mark_interrupted_reply`, the cleanup after a barge-in.
Stopping the agent mid-sentence is only half of handling an interruption; the
other half is what the half-sentence does to the conversation record afterwards.
"""

from __future__ import annotations

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams
from pipecat.turns.user_mute import (
    AlwaysUserMuteStrategy,
    BaseUserMuteStrategy,
    MuteUntilFirstBotCompleteUserMuteStrategy,
)
from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
    MinWordsUserTurnStartStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from .config import Config
from .prompts import INTERRUPTED_REPLY_MARKER

# Providers whose service pushes its own turn-strategy recommendation, which we
# must not override.
_SERVER_SIDE_TURN_PROVIDERS = frozenset({"deepgram_flux"})


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


def mark_interrupted_reply(context: LLMContext) -> None:
    """Flag the last assistant message as cut off by the caller.

    Barge-in leaves the agent's own half-finished sentence in the context, and
    that fragment poisons the next reply. Measured here on Groq/Qwen: with

        assistant: "Sunlight looks white, but it's actually made up of"
        user:      "Sorry, never mind. What is the capital of France?"

    in the context, the model answered with two tokens — "The" — and stopped. Its
    truncated answer then joined the context, and the turn after that came back
    three tokens long. One interruption degrades every reply that follows it.

    Marking the fragment is what breaks that. The model can only read a bare
    fragment as a turn it chose to end there, which sets the pattern it goes on to
    imitate; the marker says the sentence was interrupted, which is both true and
    the thing that stops the imitation. The system prompt alone does not fix this
    — it was tried first, and the model kept truncating.

    The marker text is meta, in brackets, and never spoken: it is the last thing
    in an assistant turn the model is being asked to continue *past*, not a style
    to copy. The eval suite is what checks that stays true.
    """

    def append_marker(messages):
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str) and content and INTERRUPTED_REPLY_MARKER not in content:
                message["content"] = content + INTERRUPTED_REPLY_MARKER
                logger.debug(f"Marked interrupted reply: {message['content']!r}")
            # Only the most recent assistant message can be the interrupted one.
            break
        return messages

    context.transform_messages(append_marker)


def _turn_strategies(config: Config) -> UserTurnStrategies | None:
    """Choose turn strategies, or None to accept the STT service's recommendation."""
    if config.stt_provider in _SERVER_SIDE_TURN_PROVIDERS:
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
