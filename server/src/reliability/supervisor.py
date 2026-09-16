"""What the bot does when a service fails, stalls, or the call runs too long.

Phase 9. Everything in this module is about the *live call*, which is the one
place where a failure has a person waiting on the other end of it.

**The failure this exists for is silence.** Pipecat reports a service failure as
an `ErrorFrame` and carries on, which is right — a single Deepgram hiccup should
not drop a call. But nothing was watching how many of those had happened, so a
provider that was down stayed down: the caller said something, nothing came
back, they said it again, nothing came back, and the session sat there until the
idle timeout eventually decided *they* had gone quiet. From the bot's log it
looked healthy. From the caller's side it was a company that phoned them and
then stopped talking.

So this counts failures per stage, resets the count on any success from that
stage, and when one crosses a threshold it ends the call *deliberately* — with a
goodbye if the agent can still speak, and immediately if it cannot.

**Three watchers, one class.**

* *Service failures.* Consecutive `ErrorFrame`s from one processor. Consecutive
  is the important word: three failures spread over a twenty-minute call are
  three blips, and three in a row are an outage.
* *A stalled inference.* An LLM that starts a response and never finishes it is
  the failure the error path cannot see — no exception is raised, so no error
  frame is pushed, and the pipeline simply stops advancing. The OpenAI client's
  own timeout is ten minutes by default. This watches the frames instead, so it
  works for every provider and needs nothing from any of them.
* *Maximum call duration.* A hard ceiling, because a call that never ends costs
  money for as long as it lasts, and the two failure modes that produce one — a
  loop in the conversation, a carrier that never reports a hang-up — are both
  invisible from inside the call.

**Ending, not hanging up.** The supervisor asks the worker to stop; the ordinary
teardown in `bot.py` then writes the conversation record and the call result
exactly as it would for a call that ended normally. A supervised ending is a
*recorded* ending, which is the difference between a call that shows up as
`FAILED` with a reason and one that shows up as still live and has to be
recovered.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from loguru import logger
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    ErrorFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

from ..monitoring.instruments import SERVICE_ERRORS, SUPERVISOR_TERMINATIONS
from ..prompts import MAX_DURATION_INSTRUCTION, SERVICE_TROUBLE_INSTRUCTION
from .guardrails import check_duration
from .observability import event


class Reason(StrEnum):
    """Why the supervisor ended a call."""

    SERVICE_FAILURE = "service_failure"
    LLM_STALLED = "llm_stalled"
    MAX_DURATION = "max_duration"


#: Which pipeline stage a processor belongs to, matched against its name. The
#: same shape `metrics.py` uses, and for the same reason: Pipecat identifies a
#: processor as `DeepgramFluxSTTService#0`, and the stage is the part that
#: matters for deciding whether the agent can still speak.
_STAGES = (("stt", "STT"), ("llm", "LLM"), ("tts", "TTS"))


def stage_of(source: object) -> str:
    """Which stage a frame's source belongs to: `stt`, `llm`, `tts` or `other`."""
    name = str(source)
    for stage, marker in _STAGES:
        if marker in name:
            return stage
    return "other"


@dataclass
class ServiceHealth:
    """Consecutive failures per stage, and the last error each one gave."""

    failures: dict[str, int] = field(default_factory=dict)
    last_error: dict[str, str] = field(default_factory=dict)

    def record_failure(self, stage: str, error: str) -> int:
        """Count one failure. Returns the new consecutive count for that stage."""
        self.failures[stage] = self.failures.get(stage, 0) + 1
        self.last_error[stage] = error
        return self.failures[stage]

    def record_success(self, stage: str) -> None:
        """Clear a stage's failure count, because it just worked."""
        self.failures.pop(stage, None)

    def describe(self) -> str:
        """One line for the end-of-session summary, or empty when nothing failed."""
        if not self.failures:
            return ""
        return ", ".join(f"{stage} x{count}" for stage, count in sorted(self.failures.items()))


class SessionSupervisor:
    """Watches one session for the failures that leave a caller in silence."""

    def __init__(
        self,
        *,
        end_session: Callable[[], Awaitable[None]],
        cancel_session: Callable[[], Awaitable[None]],
        say: Callable[[str], Awaitable[None]] | None = None,
        max_call_secs: float = 600.0,
        llm_stall_secs: float = 45.0,
        max_service_failures: int = 3,
        goodbye_grace_secs: float = 12.0,
        on_terminated: Callable[[Reason, str], None] | None = None,
        ignore_errors_from: Callable[[object], bool] | None = None,
    ) -> None:
        """Create the supervisor.

        Args:
            end_session: Ends the session gracefully, draining queued audio.
                `PipelineWorker.stop_when_done`.
            cancel_session: Ends it immediately. `PipelineWorker.cancel`.
            say: Adds a turn instruction and asks the agent to speak it. `None`
                means the agent cannot be asked to say anything, so every
                ending is immediate.
            max_call_secs: Hard ceiling on one call. 0 disables.
            llm_stall_secs: How long an inference may run without finishing
                before the call is treated as stalled. 0 disables.
            max_service_failures: Consecutive failures from one stage before the
                call is ended. 0 disables.
            goodbye_grace_secs: How long to wait for the closing line to finish
                playing before ending anyway. A goodbye that never arrives is
                itself a symptom, and waiting forever for it defeats the point.
            on_terminated: Called with the reason and a sentence about it, so
                the caller can note it on the call record.
            ignore_errors_from: Phase 30. Given the processor an error frame was
                pushed from, says whether that error is somebody else's to
                answer for — the services inside `tts_fallback.TTSFallbackSwitcher`,
                whose failures the switcher absorbs by moving to the fallback.
                Only what the switcher itself pushes out is counted then. `None`
                counts every error.
        """
        self._end = end_session
        self._cancel = cancel_session
        self._say = say
        self._max_call = max(0.0, max_call_secs)
        self._llm_stall = max(0.0, llm_stall_secs)
        self._max_failures = max(0, max_service_failures)
        self._grace = max(0.0, goodbye_grace_secs)
        self._on_terminated = on_terminated
        self._ignore_errors_from = ignore_errors_from

        self.health = ServiceHealth()
        self._started = time.monotonic()
        self._observer = _SupervisorObserver(self)
        self._watchdog: asyncio.Task | None = None
        self._llm_started_at: float | None = None
        self._llm_produced = False
        self._ever_spoke = False
        self._closing: Reason | None = None
        self._ended = False

    @property
    def observer(self) -> BaseObserver:
        """The observer to pass to `PipelineWorker(observers=[...])`."""
        return self._observer

    @property
    def terminated_by(self) -> Reason | None:
        """Why the supervisor ended the call, or None if it did not."""
        return self._closing

    @property
    def elapsed_secs(self) -> float:
        """How long the session has been running."""
        return time.monotonic() - self._started

    def start(self) -> None:
        """Begin watching. Idempotent."""
        if self._watchdog is not None:
            return
        if self._max_call <= 0 and self._llm_stall <= 0:
            return
        self._watchdog = asyncio.create_task(self._watch())

    def stop(self) -> None:
        """Stop watching. Safe to call more than once."""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    async def on_bot_stopped_speaking(self) -> None:
        """End the session once a closing line has finished playing.

        The same signal `SilenceHandler` waits for, and for the same reason:
        "the LLM finished" and "the caller heard it" are an utterance apart, and
        ending on the first cuts the goodbye off mid-word.
        """
        if self._closing is not None and not self._ended:
            await self._finish(f"{self._closing.value}: goodbye delivered")

    def describe(self) -> str:
        """One line for the end-of-session summary."""
        parts = [f"duration {self.elapsed_secs:.0f}s"]
        failures = self.health.describe()
        parts.append(f"service failures: {failures}" if failures else "no service failures")
        if not self._ever_spoke:
            parts.append("THE AGENT NEVER SPOKE")
        if self._closing is not None:
            parts.append(f"ended by supervisor ({self._closing.value})")
        return " | ".join(parts)

    # --- Reacting to the pipeline ------------------------------------------

    def note_llm_started(self) -> None:
        """An inference has begun. Arms the stall watchdog."""
        self._llm_started_at = time.monotonic()
        self._llm_produced = False

    def note_llm_output(self) -> None:
        """The inference produced a token, so it is working."""
        self._llm_produced = True

    def note_llm_finished(self) -> None:
        """An inference has finished. Disarms the stall watchdog.

        **Only counts as a success if the inference produced something.** This
        is the distinction the first live test of this class got wrong: Pipecat
        pushes `LLMFullResponseEndFrame` from a `finally`, so it arrives
        immediately *after* the error frame for a failed request too. Treating
        every end frame as a success reset the failure count on every failure,
        and the counter could never reach its threshold — an LLM that was
        refusing every request looked perfectly healthy. Observed against a real
        Groq daily-quota 429.
        """
        self._llm_started_at = None
        if self._llm_produced:
            self.health.record_success("llm")

    def error_is_internal(self, source: object) -> bool:
        """Whether an error pushed from `source` is answered for elsewhere (see `ignore_errors_from`)."""
        return bool(self._ignore_errors_from is not None and self._ignore_errors_from(source))

    def note_success(self, stage: str) -> None:
        """A stage produced something, so it is working."""
        self.health.record_success(stage)

    async def note_speech(self) -> None:
        """The agent produced audio, so the caller has heard something."""
        self._ever_spoke = True
        self.health.record_success("tts")

    async def note_error(self, stage: str, error: str) -> None:
        """Record one service failure and end the call if that stage has had enough."""
        count = self.health.record_failure(stage, error)
        # Phase 22: every error frame, by stage, whatever the supervisor then
        # decides. `kind` is `error`; a stall is counted under its own kind
        # below, since no error frame ever describes it.
        SERVICE_ERRORS.inc(stage=stage, kind="error")
        if self._max_failures <= 0 or self._closing is not None:
            return

        # A failure before the agent has said a single word is fatal on its own,
        # whatever the threshold. Observed live: when the greeting's inference
        # fails, no further inference is ever attempted — the silence
        # escalation is armed by the agent *finishing speaking*, which never
        # happens — so the count stays at one, the threshold is never reached,
        # and the caller listens to nothing until the session idle timeout
        # minutes later. There is nothing to recover to on a call that has not
        # started, so it ends now.
        if not self._ever_spoke and stage in ("llm", "tts"):
            logger.error(
                event(
                    "session.dead_on_arrival",
                    provider=stage,
                    error=error,
                    outcome="the agent never spoke; ending the call rather than leaving silence",
                )
            )
            await self._close(
                Reason.SERVICE_FAILURE,
                f"{stage} failed before the agent had said anything: {error}",
                speak=False,
            )
            return

        if count < self._max_failures:
            logger.warning(
                event("session.service_error", provider=stage, retries=count, error=error)
            )
            return

        logger.error(
            event(
                "session.service_down",
                provider=stage,
                retries=count,
                error=error,
                outcome="ending the call",
            )
        )
        # An LLM that is down cannot compose a goodbye, so there is nothing to
        # wait for; anything else can still speak one.
        await self._close(
            Reason.SERVICE_FAILURE,
            f"{stage} failed {count} times in a row: {error}",
            speak=stage != "llm",
        )

    # --- The watchdog --------------------------------------------------------

    async def _watch(self) -> None:
        """Poll the two time-based limits once a second."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if self._closing is not None:
                    continue
                if await self._check_duration():
                    continue
                await self._check_llm_stall()
        except asyncio.CancelledError:
            return

    async def _check_duration(self) -> bool:
        """End the call if it has run past its ceiling. Returns whether it did."""
        decision = check_duration(self.elapsed_secs, self._max_call)
        if decision:
            return False
        logger.warning(
            event("session.max_duration", latency_ms=int(self.elapsed_secs * 1000), outcome=decision.reason)
        )
        await self._close(Reason.MAX_DURATION, decision.reason, speak=True)
        return True

    async def _check_llm_stall(self) -> None:
        """End the call if an inference has been running with no result for too long."""
        if self._llm_stall <= 0 or self._llm_started_at is None:
            return
        waiting = time.monotonic() - self._llm_started_at
        if waiting < self._llm_stall:
            return
        logger.error(
            event(
                "session.llm_stalled",
                latency_ms=int(waiting * 1000),
                error=f"no response for {waiting:.0f}s",
                outcome="ending the call",
            )
        )
        self._llm_started_at = None
        SERVICE_ERRORS.inc(stage="llm", kind="stall")
        await self._close(
            Reason.LLM_STALLED,
            f"the language model produced nothing for {waiting:.0f}s",
            speak=False,
        )

    # --- Ending ---------------------------------------------------------------

    async def _close(self, reason: Reason, detail: str, *, speak: bool) -> None:
        """Begin ending the call, speaking a closing line first when that is possible."""
        if self._closing is not None:
            return
        self._closing = reason
        SUPERVISOR_TERMINATIONS.inc(reason=reason.value)
        if self._on_terminated is not None:
            try:
                self._on_terminated(reason, detail)
            except Exception:  # noqa: BLE001 - a note must not stop the ending
                logger.exception("SUPERVISOR | could not note the termination")

        if not speak or self._say is None:
            await self._finish(detail)
            return

        instruction = (
            MAX_DURATION_INSTRUCTION if reason is Reason.MAX_DURATION else SERVICE_TROUBLE_INSTRUCTION
        )
        try:
            await self._say(instruction)
        except Exception:  # noqa: BLE001 - if it cannot speak, it still has to end
            logger.exception("SUPERVISOR | could not ask for a closing line")
            await self._finish(detail)
            return
        # If the goodbye never plays — which is likely, since something is
        # already wrong — end anyway rather than holding the line open.
        asyncio.create_task(self._finish_after_grace(detail))

    async def _finish_after_grace(self, detail: str) -> None:
        try:
            await asyncio.sleep(self._grace)
        except asyncio.CancelledError:
            return
        if not self._ended:
            logger.warning(
                event("session.goodbye_timeout", outcome=f"ending after {self._grace:g}s anyway")
            )
            await self._finish(detail, graceful=False)

    async def _finish(self, detail: str, *, graceful: bool = True) -> None:
        """Ask the worker to stop. Idempotent: several paths can reach this."""
        if self._ended:
            return
        self._ended = True
        self.stop()
        logger.info(event("session.ended_by_supervisor", outcome=detail))
        try:
            await (self._end() if graceful else self._cancel())
        except Exception:  # noqa: BLE001 - the session is going away regardless
            logger.exception("SUPERVISOR | could not end the session cleanly")


class _SupervisorObserver(BaseObserver):
    """Turns pipeline frames into the supervisor's four questions.

    De-duplicates on `frame.id` first, for the reason written up in
    `diagnostics.py`: an observer sees each frame once per processor *hop*, so
    one `ErrorFrame` crossing a nine-stage pipeline would otherwise be counted
    nine times and trip a three-failure threshold on its own.
    """

    def __init__(self, supervisor: SessionSupervisor, *, history: int = 200) -> None:
        super().__init__()
        self._supervisor = supervisor
        self._seen: set[int] = set()
        self._order: list[int] = []
        self._history = history

    async def on_push_frame(self, data: FramePushed) -> None:
        """Feed one frame to the supervisor, once."""
        frame = data.frame
        # Before the de-duplication: an error absorbed inside the TTS fallback
        # switcher is not counted, and the same frame, should the switcher push
        # it on as its own, still is.
        if isinstance(frame, ErrorFrame) and self._supervisor.error_is_internal(data.source):
            return
        if self._already_seen(frame.id):
            return

        if isinstance(frame, ErrorFrame):
            stage = stage_of(data.source)
            await self._supervisor.note_error(stage, str(getattr(frame, "error", frame))[:200])
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._supervisor.note_llm_started()
        elif isinstance(frame, LLMTextFrame):
            # A token came back, so this inference is working. Checked before
            # the end frame, which arrives for a failed request too.
            self._supervisor.note_llm_output()
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._supervisor.note_llm_finished()
        elif isinstance(frame, TranscriptionFrame):
            self._supervisor.note_success("stt")
        elif isinstance(frame, TTSAudioRawFrame | BotStoppedSpeakingFrame):
            await self._supervisor.note_speech()

    def _already_seen(self, frame_id: int) -> bool:
        if frame_id in self._seen:
            return True
        if len(self._order) >= self._history:
            self._seen.discard(self._order.pop(0))
        self._order.append(frame_id)
        self._seen.add(frame_id)
        return False


__all__ = ["Reason", "ServiceHealth", "SessionSupervisor", "stage_of"]
