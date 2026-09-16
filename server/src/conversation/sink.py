"""Where a conversation's consequences leave the conversation layer.

The conversation knows what was said. It does not know that prospects live in
PostgreSQL, that a do-not-call closes campaign memberships, or that a call
attempt has a status column — and it must not, or the boundary this project is
built on collapses::

    conversation state  ≠  LLM prompt  ≠  prospect database  ≠  RAG  ≠  telephony

So the conversation talks to a `ConversationSink`: three methods, plain data
arguments, no campaign types in the signature. `src/campaigns/briefing.py`
implements it against the real tables; `LoggingSink` implements it by writing to
the log, which is what a browser session, an eval run or a call with no prospect
id gets.

**Every method must be safe to fail.** These run mid-call, and a database that
has gone away must not take the phone call down with it — the person is still on
the line and the agent is still mid-sentence. Implementations swallow their own
errors and log them; the conversation never sees an exception from here.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from loguru import logger

from .brief import CallBrief


@runtime_checkable
class ConversationSink(Protocol):
    """What the conversation layer needs the rest of the application to do."""

    async def on_do_not_call(self, brief: CallBrief, reason: str) -> bool:
        """Honour a do-not-call request, however it was detected.

        Called the moment the request is recognised — during the call, not at
        the end of it — because a call that drops before its teardown must
        still have recorded the request.

        Args:
            brief: Identifies the person, via `brief.prospect_id`.
            reason: What they said, or which detector fired.

        Returns:
            Whether the request was actually recorded somewhere durable. False
            is a normal answer for a session with no prospect id, and the agent
            still honours it for the rest of the call — it simply has no row to
            write to.
        """
        ...

    async def on_call_finished(self, brief: CallBrief, outcome: dict[str, Any]) -> bool:
        """Store the result of a finished call.

        Args:
            brief: Identifies the call attempt, via `brief.call_attempt_id`.
            outcome: The whole record — final state, qualification, transitions,
                counts. Plain JSON-able data, deliberately not a typed object,
                so that adding a field to the record does not change this
                interface.

        Returns:
            Whether anything was written.
        """
        ...

    async def close(self) -> None:
        """Release whatever the sink holds. Safe to call more than once."""
        ...


class LoggingSink:
    """The sink for a call that has nowhere to write: it logs, and says so.

    Used for browser sessions, eval runs, and any call whose prospect could not
    be resolved. Deliberately not a silent no-op — a do-not-call request that
    reached nothing is exactly the event somebody needs to see in the log,
    because it means a person asked not to be called and the system has no
    record of it.
    """

    async def on_do_not_call(self, brief: CallBrief, reason: str) -> bool:
        """Log the request and report that nothing durable happened."""
        logger.warning(
            f"DNC | {brief.describe()} asked not to be called again ({reason}) — "
            f"nothing to record it against: this call has no prospect id"
        )
        return False

    async def on_call_finished(self, brief: CallBrief, outcome: dict[str, Any]) -> bool:
        """Log the outcome and report that nothing was stored."""
        logger.info(f"OUTCOME | not stored (no call attempt id) | {brief.describe()}")
        return False

    async def close(self) -> None:
        """Nothing to release."""
        return None
