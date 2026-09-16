"""What the agent does when nothing is happening, or when the network blinks.

Two failure modes look identical from inside the pipeline — no audio is arriving
— and want opposite responses:

* **The caller went quiet.** They are thinking, distracted, or have walked away.
  The connection is fine. Talking is the right move: check in, then close the
  call politely rather than holding an open line forever.
* **The connection dropped.** The caller may still be there. Talking is useless
  because nothing reaches them, but hanging up instantly is wrong too — WebRTC
  drops and recovers on flaky wifi, and a caller who reconnects in two seconds
  should find the conversation still running with its context intact.

So silence gets escalating nudges and a graceful goodbye, and a disconnect gets a
grace window before the session is torn down.

Both classes steer the agent the same way: add a message to the context and ask
for a response. That is deliberate — the agent composes its own words, so a
check-in sounds like the rest of the conversation rather than a canned line, and
the nudge is recorded in the context so the agent knows it already asked.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext

from .config import Config
from .prompts import IDLE_GOODBYE_INSTRUCTION, IDLE_INSTRUCTIONS, RECONNECT_INSTRUCTION


async def prompt_agent(worker: PipelineWorker, context: LLMContext, instruction: str) -> None:
    """Add a turn instruction to the context and ask the agent to respond to it.

    The role is "user", not "developer" or "system". That was established in
    Phase 1: "developer" is an OpenAI-only role, and open-weight chat templates
    (verified on Groq/Qwen) reject both it and "system" here, because their
    template requires the conversation to end on a user turn. "user" is the only
    role that renders across every provider in `_LLM_SERVICES`.
    """
    context.add_message({"role": "user", "content": instruction})
    await worker.queue_frames([LLMRunFrame()])


class SilenceHandler:
    """Nudges a caller who has gone quiet, then ends the call gracefully.

    Driven by the context aggregator's `on_user_turn_idle` event, which fires
    once per bot turn: the timer arms when the bot stops speaking and is
    cancelled the moment anyone speaks. Because each nudge is itself a bot turn,
    the timer re-arms on its own and the escalation advances one step per
    timeout, with no timer bookkeeping here.
    """

    def __init__(self, worker: PipelineWorker, context: LLMContext, config: Config) -> None:
        """Create the handler.

        Args:
            worker: The pipeline worker, used to trigger responses and to end.
            context: Conversation context the nudges are added to.
            config: Supplies `max_idle_prompts`.
        """
        self._worker = worker
        self._context = context
        self._max_prompts = min(config.max_idle_prompts, len(IDLE_INSTRUCTIONS))
        self._nudges = 0
        self._closing = False

    @property
    def closed_call(self) -> bool:
        """Whether this handler is ending the call because the caller went quiet. Phase 12."""
        return self._closing

    def on_user_spoke(self) -> None:
        """Reset the escalation because the caller came back.

        This runs on `on_user_turn_started`, which fires before the interruption
        it causes reaches the output transport — so a caller who speaks up *over*
        the goodbye clears `_closing` before the goodbye's truncated
        `BotStoppedSpeakingFrame` would have ended the session. Hanging up on
        someone at the moment they started talking is the worst possible time.
        """
        if self._nudges or self._closing:
            logger.debug("Caller responded; resetting the idle escalation")
        self._nudges = 0
        self._closing = False

    async def on_idle(self) -> None:
        """Handle one idle timeout: nudge, or start closing the call."""
        if self._closing:
            # The goodbye is already in flight; a second timeout while it plays
            # must not queue another turn on top of it.
            return

        if self._nudges < self._max_prompts:
            instruction = IDLE_INSTRUCTIONS[self._nudges]
            self._nudges += 1
            logger.info(f"SILENCE | caller idle, nudge {self._nudges}/{self._max_prompts}")
            await prompt_agent(self._worker, self._context, instruction)
            return

        self._closing = True
        logger.info("SILENCE | caller did not respond; closing the call")
        await prompt_agent(self._worker, self._context, IDLE_GOODBYE_INSTRUCTION)

    async def on_bot_stopped_speaking(self) -> None:
        """End the session once the goodbye has actually finished playing.

        `stop_when_done` queues an `EndFrame`, and anything already queued ahead
        of it is flushed first — but the goodbye is *generated* asynchronously,
        so calling it right after asking for the goodbye would race the LLM and
        cut the agent off mid-sentence. Waiting for the audio to finish reaching
        the caller is the only signal that the last word was actually heard.
        """
        if self._closing:
            logger.info("SILENCE | goodbye delivered; ending the session")
            await self._worker.stop_when_done()


class PeerWatchdog:
    """Notices a browser that went away without saying goodbye.

    **The bug this exists for.** A WebRTC peer that closes cleanly tells us, and
    everything works: the session ends, and the dev runner drops the connection
    from the map it keys by `pc_id`. A peer that simply *stops* — a closed
    laptop, a dropped wifi, a client that abandons the connection without
    closing it — tells us nothing. aiortc has no "disconnected" state, and
    Pipecat only emits its own `disconnected` event when a renegotiation
    explicitly restarts the connection. So nothing fires, the session runs on
    forever with nobody on the other end, and the runner keeps the dead `pc_id`.

    That last part is what makes it user-visible rather than merely wasteful:
    the browser client sends its previous `pc_id` when you press connect again,
    the runner finds the stale entry and *renegotiates* it instead of creating a
    new connection — and renegotiating does not start a bot. The page connects
    to nothing. Reloading the page discards the `pc_id` and works, which is
    exactly the "it only works again after a refresh" symptom.

    So we watch. `is_connected()` is the same predicate the transport trusts:
    a keep-alive ping within the last three seconds, falling back to aiortc's
    connection state for clients that open no data channel. When it stays false
    for `timeout_secs`, we report a disconnect through the normal path — which
    means `ConnectionGuard` still gets to hold the session open for its grace
    window, in case the caller is coming back.
    """

    def __init__(
        self,
        connection,
        guard: ConnectionGuard,
        *,
        timeout_secs: float,
        poll_secs: float = 1.0,
    ) -> None:
        """Create the watchdog.

        Args:
            connection: The transport's peer connection, which must expose
                `is_connected()`. Anything else disables the watchdog.
            guard: Told about the drop, so a recovery is handled the same way as
                a clean disconnect.
            timeout_secs: How long the connection must look dead before we
                believe it. 0 disables the watchdog.
            poll_secs: How often to look.
        """
        self._connection = connection
        self._guard = guard
        self._timeout = timeout_secs
        self._poll = poll_secs
        self._task: asyncio.Task | None = None
        self._reported = False

    def start(self) -> None:
        """Begin watching. Safe to call again when a client reconnects."""
        if self._timeout <= 0 or self._connection is None:
            return
        if not hasattr(self._connection, "is_connected"):
            return
        self.stop()
        self._reported = False
        self._task = asyncio.create_task(self._watch())

    def stop(self) -> None:
        """Stop watching. Safe to call more than once."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _watch(self) -> None:
        dead_since: float | None = None
        try:
            while True:
                await asyncio.sleep(self._poll)

                if self._connection.is_connected():
                    if dead_since is not None:
                        logger.debug("Peer connection recovered before the watchdog fired")
                    dead_since = None
                    continue

                now = asyncio.get_running_loop().time()
                if dead_since is None:
                    dead_since = now
                    continue

                if now - dead_since >= self._timeout and not self._reported:
                    self._reported = True
                    logger.info(
                        f"No sign of the client for {self._timeout:g}s and it never said "
                        f"goodbye — treating it as disconnected"
                    )
                    await self._guard.on_disconnected()
                    return
        except asyncio.CancelledError:
            # The session is ending, or the client came back. Nothing awaits this
            # task, so there is nothing to propagate the cancellation to.
            return


class ConnectionGuard:
    """Rides out a brief disconnect instead of tearing the session down.

    The transport reports a drop the same way whether the caller closed the tab
    or their wifi hiccuped, so this waits `DISCONNECT_GRACE_SECS` before ending.
    A caller who returns inside that window keeps the whole conversation —
    context, transcript, everything — because the pipeline never stopped.
    """

    def __init__(
        self,
        worker: PipelineWorker,
        context: LLMContext,
        config: Config,
        *,
        grace_secs: float | None = None,
    ) -> None:
        """Create the guard.

        Args:
            worker: The pipeline worker to cancel when the grace window expires.
            context: Conversation context, used to acknowledge a reconnection.
            config: Supplies the default `disconnect_grace_secs`.
            grace_secs: Overrides that default. Telephony passes 0 here: a
                browser's WebRTC connection blips and recovers into the same
                session, but a phone call that drops is over — the person
                redials and gets a new call — so holding the pipeline open buys
                nothing and keeps a dead session alive.
        """
        self._worker = worker
        self._context = context
        self._grace = config.disconnect_grace_secs if grace_secs is None else grace_secs
        self._pending: asyncio.Task | None = None
        self._connected_once = False

    async def on_connected(self) -> None:
        """Handle a client connecting, which may be the first time or a recovery."""
        if not self._connected_once:
            self._connected_once = True
            logger.info("Client connected")
            return

        recovered = self._cancel_pending()
        logger.info("Client reconnected" + (" within the grace window" if recovered else ""))
        if recovered:
            # They may have missed the tail of the last reply, so say so rather
            # than continuing as though they heard it.
            await prompt_agent(self._worker, self._context, RECONNECT_INSTRUCTION)

    async def on_disconnected(self) -> None:
        """Handle a client dropping: end now, or start the grace window."""
        if self._grace <= 0:
            logger.info("Client disconnected — ending session")
            await self._worker.cancel()
            return

        logger.info(f"Client disconnected — holding the session open for {self._grace:.0f}s")
        self._cancel_pending()
        self._pending = asyncio.create_task(self._end_after_grace())

    def close(self) -> None:
        """Drop any pending timer. Safe to call more than once."""
        self._cancel_pending()

    async def _end_after_grace(self) -> None:
        try:
            await asyncio.sleep(self._grace)
        except asyncio.CancelledError:
            # Swallowed rather than re-raised on purpose: the only thing that
            # cancels this task is `_cancel_pending`, meaning the caller came
            # back or the session is over. Nobody awaits the task, so there is
            # no cancellation for the re-raise to propagate to.
            return
        logger.info("Client did not return — ending session")
        self._pending = None
        await self._worker.cancel()

    def _cancel_pending(self) -> bool:
        """Cancel the grace timer if one is running. Returns whether there was one."""
        if self._pending is None:
            return False
        self._pending.cancel()
        self._pending = None
        return True
