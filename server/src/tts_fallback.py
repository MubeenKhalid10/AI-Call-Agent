"""A second TTS provider on standby, taking over once per call when the first cannot speak. Phase 30.

`services.make_tts` builds one TTS service, and the pipeline is built around
that one object. When its provider stops synthesising — Cartesia answering
HTTP 402 because the account's credits are gone (observed 2026-09-14), a
websocket that will not connect, a timeout — the caller listens to silence
until the supervisor ends the call. This module gives `make_tts` a second
service to hand back instead: a *switcher* that holds the primary and a
fallback, routes every frame to one of them, and moves to the fallback the
first time the primary reports a failure. It moves once and never back.

**The boundary.** Nothing here replaces a processor inside a running
pipeline: a `FrameProcessor` is linked to its neighbours at construction, and
Pipecat has no way to relink one. What Pipecat does have is
`pipecat.pipeline.service_switcher.ServiceSwitcher`, a `ParallelPipeline`
whose branches each hold one service behind a pair of filters that only let
frames into the *active* branch. Both services are constructed, set up and
started with the pipeline, so a switch is a change of which filter is open,
not a change to the pipeline. The stock failover strategy only switches when
the failed service reports itself unusable, which Pipecat reserves for
rejected credentials and malformed requests; a 402, a dropped connection or
a timeout are classed as things a service might recover from. This
subclass makes the decision itself, on any failure the primary reports
except one caused by application code, and retires the primary with
`set_usable(False)` — the public call that stops a service being given work
and stops its websocket from reconnecting.

**When the switch takes effect.** Each service aggregates the LLM's tokens
into sentences in its own buffer and holds frames in its own input queue, so
flipping the filter in the middle of a response would leave half a sentence
in the primary and hand the other half to the fallback. The flip therefore
waits for the response boundary: from the failure to the end of the
response the primary stays *routed* but *retired* — it keeps aggregating and
keeps its place in the transcript, and every sentence it would have spoken
is handed to the fallback as a `TTSSpeakFrame`. The fallback speaks them in
order. The sentences are seen through a *text transformer* registered on the
primary (`TTSService.add_text_transformer`, a public hook Pipecat awaits
inline for every sentence, before it synthesises — and still for a retired
service, which then declines to synthesise). It returns the text unchanged;
it is there because it is synchronous with the primary's own processing,
which the `on_tts_request` event is not (Pipecat delivers events on
separate tasks, so an error could overtake the record of what was asked). When the response's end frame comes out of the
primary, or a new response starts, or the caller interrupts, the filter
flips and every later frame goes to the fallback directly. A failure with no
response in flight — the connection refused at start, say — flips at once.

**What is spoken once.** The sentence the primary was asked for when it
failed is spoken by the fallback only if the primary produced no audio for
it: each request is remembered until audio from the primary reaches the
switcher, and a failure re-speaks what is still remembered, in order. Audio
clears everything remembered, not only the sentence it belongs to — Cartesia
streams one context per response, so a chunk cannot be attributed to a
sentence — which means "produced audio" is per response: when some of a
response has been heard, the rest of it is not re-spoken, because a caller
hearing a sentence twice is worse than a caller missing one. The re-spoken
text is not appended to the LLM context either: the retired primary's own
bookkeeping still writes it there, so the transcript records each sentence
once. With `TTS_STREAM_TOKENS=true` the unit Pipecat hands over is a token
rather than a sentence, so the failed response is spoken by the fallback
token by token; the responses after it are aggregated by the fallback
itself as usual.

**What is logged.** Provider names, the error's category, the switch and
its timing, and how many sentences were re-spoken or handed over. The
error text goes through `event()`, which redacts credentials. Keys are never
logged.

**Concurrency.** Everything here runs on the pipeline's asyncio loop —
frame handlers, the `on_tts_request` event, `push_frame` — so the state is a
handful of attributes changed in one task at a time; there are no threads
and no locks are needed. Each call has its own switcher, so the switch is
per call: the next call starts on the primary again.

**What it does not do.** It does not probe the fallback ahead of time (the
health check validates its key without synthesising), does not retry the
primary, and does not change what the primary or the fallback are: they
are the same service objects `make_tts` has always built.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.service_switcher import ServiceSwitcher, ServiceSwitcherStrategyManual
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.errors import ErrorCategory

from .reliability.observability import event

#: The switcher's states, in the order a call moves through them.
STATE_PRIMARY = "primary"  # The primary speaks; nothing has failed.
STATE_DRAINING = "draining"  # The primary is retired but still routed until the response ends.
STATE_FALLBACK = "fallback"  # The fallback speaks; the filter has flipped.
STATE_FAILED = "failed"  # The primary is retired and the fallback cannot take over.

#: Requests remembered for re-speaking. A response is one to three sentences;
#: anything older than this has long since been spoken or reported.
MAX_REMEMBERED_REQUESTS = 8

#: How much of an error's text is logged with the switch.
_ERROR_EXCERPT = 160


class TTSFallbackStrategy(ServiceSwitcherStrategyManual):
    """The stock manual strategy, plus the one call the switcher needs to fail over.

    The base class decides nothing on its own (`handle_error` returns None),
    which is the point: the switcher below decides, because it sees the frames
    the decision depends on.
    """

    async def fail_over_to(self, service: FrameProcessor) -> FrameProcessor | None:
        """Make `service` the active one. Returns it, or None if it can no longer work."""
        return await self._set_active_if_available(service)


class TTSFallbackSwitcher(ServiceSwitcher[TTSFallbackStrategy]):
    """Routes speech to the primary TTS service until it fails, then to the fallback.

    Build it with the two services `make_tts` would otherwise return on their
    own, and put it in the pipeline where the TTS service goes. `contains`
    tells the supervisor which processors are inside it, so that the errors
    it answers for are not counted twice.
    """

    def __init__(
        self,
        primary: FrameProcessor,
        fallback: FrameProcessor,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the switcher.

        Args:
            primary: The TTS service that speaks first.
            fallback: The TTS service that takes over. It must not be the
                same object as the primary.
            clock: Monotonic seconds, for the timings in the log lines.
        """
        if primary is fallback:
            raise ValueError("The fallback TTS service must be a different service from the primary.")
        super().__init__(services=[primary, fallback], strategy_type=TTSFallbackStrategy)
        self._primary = primary
        self._fallback = fallback
        self._clock = clock
        self._started_at = clock()
        self._state = STATE_PRIMARY
        self._response_open = False
        # What the primary has been asked to say and has produced no audio
        # for yet, in request order.
        self._remembered: list[str] = []
        self._members: frozenset[int] | None = None
        # The record.
        self._switched_at: float | None = None
        self._switch_category: str | None = None
        self._respoken = 0
        self._handed_over = 0
        self._activation_reason: str | None = None
        # Every sentence the primary is asked for passes through here first,
        # inline, whether or not the primary will speak it (see the module
        # docstring for why this and not the `on_tts_request` event).
        primary.add_text_transformer(self._on_primary_sentence, "*")
        fallback.add_event_handler("on_usable_changed", self._on_fallback_usable_changed)
        logger.info(
            "TTS FALLBACK | "
            + event("tts.fallback.armed", primary=primary.name, fallback=fallback.name)
        )

    # --- What the rest of the application reads ------------------------------------

    @property
    def primary(self) -> FrameProcessor:
        """The service that speaks until it fails."""
        return self._primary

    @property
    def fallback(self) -> FrameProcessor:
        """The service that takes over."""
        return self._fallback

    @property
    def active(self) -> FrameProcessor:
        """The service frames are routed to right now."""
        return self.strategy.active_service

    @property
    def state(self) -> str:
        """One of `STATE_PRIMARY`, `STATE_DRAINING`, `STATE_FALLBACK`, `STATE_FAILED`."""
        return self._state

    @property
    def switched(self) -> bool:
        """Whether the primary has been retired this call."""
        return self._state != STATE_PRIMARY

    def contains(self, processor: object) -> bool:
        """Whether `processor` is one of the processors inside this switcher.

        The two services and the filters, sources and sinks around them. Not
        the switcher itself: what it pushes out is what the pipeline should
        judge it by.
        """
        if self._members is None:
            members: set[int] = set()

            def walk(node: Any) -> None:
                members.add(id(node))
                for child in getattr(node, "processors", None) or []:
                    walk(child)

            walk(self)
            members.discard(id(self))
            self._members = frozenset(members)
        return id(processor) in self._members

    def summary(self) -> dict[str, Any]:
        """The switch as plain data for the call record. Names and numbers; never text."""
        return {
            "primary": self._primary.name,
            "fallback": self._fallback.name,
            "state": self._state,
            "active": self.active.name,
            "switched": self.switched,
            "switched_after_ms": _ms(self._started_at, self._switched_at),
            "category": self._switch_category,
            "respoken": self._respoken,
            "handed_over": self._handed_over,
            "activation": self._activation_reason,
        }

    def describe(self) -> str:
        """One line for the session log."""
        if not self.switched:
            return f"{self._primary.name} throughout"
        return (
            f"{self._primary.name} -> {self._fallback.name} after "
            f"{_secs(_ms(self._started_at, self._switched_at))} ({self._switch_category}); "
            f"{self._respoken} re-spoken, {self._handed_over} handed over; state={self._state}"
        )

    # --- Frames entering ----------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Route a frame, flipping to the fallback at the boundaries a retired primary allows.

        Args:
            frame: The frame.
            direction: Which way it is travelling.
        """
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMFullResponseStartFrame):
            # A new response while the last one is still draining: the primary
            # has handed over every sentence it aggregated (that happens as it
            # processes the text, before the end frame reaches its queue), so
            # the new response can go to the fallback directly.
            if self._state == STATE_DRAINING:
                await self._activate_fallback("next response started")
            self._response_open = True
        elif direction == FrameDirection.DOWNSTREAM and isinstance(frame, InterruptionFrame):
            self._remembered.clear()
            self._response_open = False
            if self._state == STATE_DRAINING:
                # Both services need this one. The primary, still active, gets
                # it through its filter and drops the rest of the response it
                # was aggregating. The fallback is inactive, so its filter
                # would keep the frame from it — and it is the one speaking
                # the handed-over sentences, which the caller just cut off.
                await super().process_frame(frame, direction)
                await self._fallback.queue_frame(frame, direction)
                await self._activate_fallback("interrupted")
                return
        await super().process_frame(frame, direction)

    # --- Frames leaving ------------------------------------------------------------

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        """Watch what comes out of the services, and answer for the primary's errors.

        Args:
            frame: The frame.
            direction: Which way it is travelling.
        """
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TTSAudioRawFrame) and self._state == STATE_PRIMARY:
                # The primary produced audio: what it has been asked for so
                # far is being heard, so a later failure must not re-speak it.
                self._remembered.clear()
            await super().push_frame(frame, direction)
            if isinstance(frame, LLMFullResponseEndFrame):
                self._response_open = False
                if self._state == STATE_DRAINING:
                    await self._activate_fallback("response finished")
            return

        if isinstance(frame, ErrorFrame) and frame.processor is self._primary and not frame.fatal:
            if self._state == STATE_PRIMARY:
                if self._should_fail_over(frame):
                    await self._retire_primary(frame)
                    return
            else:
                # Retired: its errors are the failure already answered for,
                # or the noise of a service being given no work.
                return
        await super().push_frame(frame, direction)

    # --- The decision ---------------------------------------------------------------

    def _should_fail_over(self, error: ErrorFrame) -> bool:
        """Whether this error from the primary costs it the call.

        Every failure of the provider does: quota (HTTP 402), credentials,
        connectivity, a timeout, a server error, a provider error message, a
        context that completed in silence. The one exception is a failure of
        application code the service reported on its behalf, which says
        nothing about the provider. And there has to be a fallback that can
        still work.
        """
        category = error.category or ErrorCategory.UNKNOWN
        if category is ErrorCategory.APPLICATION:
            return False
        return bool(self._fallback.is_usable)

    async def _retire_primary(self, error: ErrorFrame) -> None:
        """Stop the primary speaking, re-speak what it never said, and schedule the flip."""
        category = (error.category or ErrorCategory.UNKNOWN).value
        self._switch_category = category
        self._switched_at = self._clock()
        respoken = list(self._remembered)
        self._remembered.clear()
        deferred = self._response_open
        self._state = STATE_DRAINING
        # Public API: the service is given no more work and its websocket
        # stops reconnecting. Pipecat still runs its bookkeeping for the text
        # it is routed, which is what keeps the transcript whole.
        await self._primary.set_usable(False)
        for text in respoken:
            await self._hand_over(text)
        self._respoken = len(respoken)
        logger.warning(
            "TTS FALLBACK | "
            + event(
                "tts.fallback.engaged",
                primary=self._primary.name,
                fallback=self._fallback.name,
                category=category,
                error=error.error[:_ERROR_EXCERPT],
                respoken=len(respoken),
                deferred=deferred,
                after_ms=_ms(self._started_at, self._switched_at),
            )
        )
        if not deferred:
            await self._activate_fallback("no response in flight")

    async def _activate_fallback(self, reason: str) -> None:
        """Flip the filter so every later frame goes to the fallback."""
        if self._state in (STATE_FALLBACK, STATE_FAILED):
            return
        service = await self.strategy.fail_over_to(self._fallback)
        if service is None:
            self._state = STATE_FAILED
            self._activation_reason = reason
            logger.error(
                "TTS FALLBACK | "
                + event(
                    "tts.fallback.unavailable",
                    primary=self._primary.name,
                    fallback=self._fallback.name,
                    outcome="no TTS provider can speak for the rest of the call",
                )
            )
            await self._report_nobody_can_speak()
            return
        self._state = STATE_FALLBACK
        self._activation_reason = reason
        logger.info(
            "TTS FALLBACK | "
            + event(
                "tts.fallback.active",
                fallback=self._fallback.name,
                reason=reason,
                after_ms=_ms(self._started_at, self._clock()),
                respoken=self._respoken,
                handed_over=self._handed_over,
            )
        )

    async def _hand_over(self, text: str) -> None:
        """Give the fallback one sentence the primary will not speak.

        `append_to_context=False`: the retired primary's own bookkeeping still
        records the sentence in the LLM context, so the fallback must not.
        """
        if not text.strip():
            return
        self._handed_over += 1
        await self._fallback.queue_frame(TTSSpeakFrame(text, append_to_context=False))

    async def _report_nobody_can_speak(self) -> None:
        """Report, as the switcher's own error, that nothing can speak.

        Reported as the switcher's, the way the stock switcher reports a
        failure it could not switch away from: the supervisor counts errors
        by stage, and this processor's name puts it in the TTS stage.
        """
        await self.push_error(
            f"{self._primary.name} can no longer speak and {self._fallback.name} cannot take over",
            category=ErrorCategory.UNKNOWN,
        )

    # --- Events from the services ------------------------------------------------------

    async def _on_primary_sentence(self, text: str, aggregation_type: object) -> str:
        """The primary is about to be asked to say `text`. Returns it unchanged.

        Registered as a text transformer, so it runs inline in the primary's
        own processing, once per sentence, before synthesis. It must never
        raise: Pipecat would report a transformer's exception as an
        application error and drop the sentence.
        """
        try:
            if self._state == STATE_PRIMARY:
                self._remembered.append(text)
                del self._remembered[:-MAX_REMEMBERED_REQUESTS]
            elif self._state == STATE_FAILED:
                # Nothing can say it. One error per sentence, so the
                # supervisor's threshold ends a call that has gone silent
                # rather than leaving it to the idle timeout.
                await self._report_nobody_can_speak()
            else:
                # Retired, so Pipecat declines to synthesise it; the fallback
                # speaks it instead, in the order the primary aggregated it.
                await self._hand_over(text)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never cost a sentence
            logger.warning(
                "TTS FALLBACK | "
                + event("tts.fallback.bookkeeping_failed", error=str(exc)[:_ERROR_EXCERPT])
            )
        return text

    async def _on_fallback_usable_changed(self, service: FrameProcessor, is_usable: bool) -> None:
        """The fallback reported whether it can still be given work."""
        if not is_usable:
            logger.warning(
                "TTS FALLBACK | "
                + event(
                    "tts.fallback.unusable",
                    fallback=service.name,
                    outcome="it cannot take over if the primary fails",
                )
            )


def _ms(start: float | None, end: float | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int(round((end - start) * 1000)))


def _secs(ms: int | None) -> str:
    return "n/a" if ms is None else f"{ms / 1000:.2f}s"


__all__ = [
    "STATE_DRAINING",
    "STATE_FAILED",
    "STATE_FALLBACK",
    "STATE_PRIMARY",
    "TTSFallbackStrategy",
    "TTSFallbackSwitcher",
]
