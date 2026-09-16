"""The pipeline stage that keeps the model pointed at the right part of the call.

One `FrameProcessor`, placed between the knowledge retriever and the LLM::

    transport.input() -> stt -> user_aggregator -> retriever -> director -> llm -> ...

It does two things on every inference, and both are deliberate about *where* the
work happens.

**It appends the stage block to a copy of the context.** Same mechanism the
knowledge retriever uses, and for the same reason: the guidance is true for this
one inference and would be stale by the next, so it must not join the
conversation history. Appending it for real would leave a trail of forty
obsolete stage notes by the end of a long call, each one telling the model to do
something it finished doing ten turns ago. The copy also puts the guidance
*last*, immediately before generation, which is where a small open-weight model
weights hardest — the same finding Phase 3 wrote up for the knowledge block.

**It runs the deterministic detectors before the model sees the turn.** This is
the ordering that matters. The detectors could have been hung off the
aggregator's `on_user_turn_stopped` event in `bot.py`, and that fires at roughly
the same moment — but "roughly" is not good enough for a do-not-call request,
because the context frame carrying that turn is already on its way to the LLM.
Here the frame is in our hands: the state has moved and the override is in the
block before the model reads a single word of it.

The director goes *after* the retriever rather than before it for one concrete
reason: the retriever builds its search query from the last user message, and a
stage block appended first would become that query.
"""

from __future__ import annotations

from loguru import logger
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext, LLMContextMessage
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from ..prompts import is_injected_block
from .conversation import SalesConversation


class ConversationDirector(FrameProcessor):
    """Attaches stage guidance to each inference, and reacts to what was said."""

    def __init__(self, conversation: SalesConversation, **kwargs) -> None:
        """Create the director.

        Args:
            conversation: The call's state. The director never owns it — it
                reads the guidance and reports user turns; every decision lives
                in `SalesConversation`.
            **kwargs: Passed to `FrameProcessor`.
        """
        super().__init__(**kwargs)
        self._conversation = conversation
        # How many real user turns this processor has already reported. The
        # context can be sent for inference more than once for the same turn —
        # a nudge from the silence handler, a re-run after a tool result — and
        # reporting the same utterance twice would run the detectors twice.
        self._reported_turns = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Intercept context frames on their way to the LLM.

        Args:
            frame: The frame.
            direction: Which way it is travelling. Only downstream frames are on
                their way to inference; the assistant aggregator pushes context
                frames upstream after a tool result, and those must pass
                through untouched.
        """
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            await self._note_new_turn(frame.context)
            await self.push_frame(LLMContextFrame(context=self._guided(frame.context)), direction)
            return

        await self.push_frame(frame, direction)

    async def _note_new_turn(self, context: LLMContext) -> None:
        """Report the newest thing the prospect said, if we have not already.

        Never raises: a failure in the detectors or the sink must degrade the
        agent to "the model is on its own", not drop the caller's turn.
        """
        spoken = _spoken_user_messages(context, self._conversation)
        if len(spoken) <= self._reported_turns:
            return

        # Everything since the last inference. Normally exactly one message;
        # more than one only if a context frame was skipped, and processing all
        # of them is what stops a do-not-call request being missed in that case.
        fresh = spoken[self._reported_turns :]
        self._reported_turns = len(spoken)

        for text in fresh:
            try:
                report = await self._conversation.note_user_turn(text)
            except Exception:  # noqa: BLE001 - never drop a turn over this
                logger.exception("CONVERSATION | failed to process a caller turn")
                continue
            if report:
                names = ", ".join(sorted(signal.value for signal in report.matched))
                logger.info(f"SIGNAL | {names} | {text[:80]!r}")

    def _guided(self, context: LLMContext) -> LLMContext:
        """Return a copy of `context` with this turn's guidance appended.

        Phase 31: the tools the copy advertises are the stage's
        (`SalesConversation.advertised_tools`), set on the context itself first
        so that the request that answers a tool result — built from the
        context, not from this copy — starts from the same set. The schemas
        carry no handlers, so advertising a subset registers and unregisters
        nothing on the LLM service. `tool_choice` is carried over.
        """
        block = self._conversation.guidance()
        context.set_tools(self._conversation.advertised_tools())
        return LLMContext(
            messages=[*context.messages, {"role": "user", "content": block}],
            tools=context.tools,
            tool_choice=context.tool_choice,
        )


def _spoken_user_messages(context: LLMContext, conversation: SalesConversation) -> list[str]:
    """Every message in the context that the *prospect* actually said.

    The context's user-role messages are three different things by this point:
    what the person said, the turn instructions this application adds (the
    opening, the idle nudges), and the knowledge block the retriever appends.
    Only the first kind is a turn, and running a do-not-call detector over the
    agent's own stage directions would be a very confusing bug to find.
    """
    spoken: list[str] = []
    for message in context.messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = _text_of(message)
        if not content:
            continue
        if is_injected_block(content) or conversation.is_own_instruction(content):
            continue
        spoken.append(content)
    return spoken


def _text_of(message: LLMContextMessage) -> str:
    """The message's text, whether it is a plain string or a content-part list.

    Multimodal messages carry a list of parts; this project sends none, but the
    aggregators accept them and a string-only reader would raise on one.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return " ".join(part for part in parts if part).strip()
    return ""
