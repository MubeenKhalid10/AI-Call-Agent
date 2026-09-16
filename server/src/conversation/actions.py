"""The contract between the conversation and the things that act on the world.

Phase 6 gave the conversation two ways out — `ProspectSource` (ids in, brief
out) and `ConversationSink` (facts out) — and kept everything else behind them,
so that the conversation package imports no database, no carrier and no
calendar. Phase 7 adds a third: `ActionBackend`, the handler side of the
architecture the phase asks for::

    LLM -> tool request -> backend handler -> validation -> database / API -> result -> LLM

The tools in `tools.py` are the "tool request" step, `SalesConversation` applies
the rules that depend on where the call is, and everything from "validation"
onwards happens behind this Protocol, in `src/actions/`. The conversation never
sees a calendar API, a callbacks table or a carrier; it sees an `ActionOutcome`
and decides what to tell the model.

**Plain data in, plain data out.** Every argument is a standard-library type and
every result is an `ActionOutcome`, so the Protocol can be implemented by the
real service and by a ten-line stub in a test with equal ease — and so a change
to how meetings are stored does not change the interface between two packages
that otherwise know nothing about each other.

**Every method must be safe to fail, and none may raise.** These run mid-call,
with a person on the line. An implementation turns every exception into a failed
outcome with a message; the conversation turns that into a sentence the model
can say honestly. `NullActionBackend` is what a session with nothing configured
gets: every action fails with `unavailable`, plainly, which is what makes the
agent say "I can't do that from here" instead of pretending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Protocol, runtime_checkable

from .results import UNAVAILABLE


@dataclass(frozen=True)
class Capabilities:
    """What this session can actually do, so the prompt does not promise more.

    Read by `playbook.build_system_instruction` and `playbook.stage_block`: an
    agent on a browser session with no carrier is told it cannot transfer, so it
    offers a callback instead of trying; an agent with no calendar is told to
    record the intent and say a colleague will confirm. The prompt describing
    features the session does not have is how an agent ends up saying "let me
    check the calendar" and then failing.

    Attributes:
        timezone: IANA name the calendar and callbacks are expressed in, and the
            one the agent is told the current time in.
        booking_requires_email: Whether `book_meeting` needs an attendee email —
            true for Cal.com, false for the local calendar — so the agent asks
            for one before trying rather than after failing.
    """

    can_search_knowledge: bool = False
    can_check_calendar: bool = False
    can_book_meeting: bool = False
    booking_requires_email: bool = False
    can_schedule_callback: bool = False
    can_transfer: bool = False
    timezone: str = "UTC"

    def describe(self) -> str:
        """One line for the log at the start of the session."""
        on = [
            name
            for name, enabled in (
                ("knowledge", self.can_search_knowledge),
                ("calendar", self.can_check_calendar),
                ("booking", self.can_book_meeting),
                ("callback", self.can_schedule_callback),
                ("transfer", self.can_transfer),
            )
            if enabled
        ]
        return f"{', '.join(on) if on else 'none'} ({self.timezone})"


@dataclass(frozen=True)
class AttendeeDetails:
    """Who a meeting is for, as much of it as the call knows."""

    name: str
    email: str | None = None
    phone: str | None = None


@dataclass(frozen=True)
class ActionOutcome:
    """What the backend says happened. Deliberately not the model-facing shape.

    `ToolResult` (what the model sees) is built from this by the conversation,
    which adds the guidance and applies whatever state change the outcome
    implies. Keeping the two apart means the backend never composes a sentence
    for the model and the conversation never parses a calendar response.

    Attributes:
        ok: Whether the action happened.
        data: Plain JSON-able detail of what happened.
        error_code: One of `results.ERROR_CODES` when `ok` is False.
        message: Why, in one sentence. Written for the log and for the model.
    """

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    message: str | None = None

    @classmethod
    def success(cls, **data: Any) -> ActionOutcome:
        """An action that happened."""
        return cls(ok=True, data=data)

    @classmethod
    def failure(cls, error_code: str, message: str, **data: Any) -> ActionOutcome:
        """An action that did not."""
        return cls(ok=False, data=data, error_code=error_code, message=message)


@runtime_checkable
class ActionBackend(Protocol):
    """What the conversation needs the rest of the application to be able to do."""

    @property
    def capabilities(self) -> Capabilities:
        """What this session can do. Fixed for the session's lifetime."""
        ...

    async def search_knowledge(self, query: str) -> ActionOutcome:
        """Search the knowledge base through the existing retrieval path.

        Returns:
            On success, `data` has `found` (bool) and `passages`, a list of
            `{"title", "content"}` dicts, closest first and possibly empty.
        """
        ...

    async def check_availability(self, day: date, preferred_time: time | None) -> ActionOutcome:
        """Find open meeting slots on one day.

        Args:
            day: The calendar day, in the session's timezone.
            preferred_time: A time of day to sort towards, when the prospect
                gave one. Never a filter — a prospect who asked for the morning
                and is offered two morning slots plus one at two o'clock is
                better served than one offered nothing.

        Returns:
            On success, `data` has `slots`: a list of `{"start", "end",
            "label"}` with ISO 8601 timestamps, and `timezone`. An empty list is
            a success — the day is simply full.
        """
        ...

    async def book_meeting(
        self, start: datetime, attendee: AttendeeDetails, *, notes: str = ""
    ) -> ActionOutcome:
        """Book one slot.

        Returns:
            On success, `data` has `start`, `end`, `label`, `provider` and
            `reference` (the provider's id, or ours). A failure carries one of
            `slot_taken`, `email_required`, `external_error` or `unavailable`.
        """
        ...

    async def schedule_callback(self, when: datetime, *, note: str = "") -> ActionOutcome:
        """Create a future callback for this call's prospect.

        Returns:
            On success, `data` has `scheduled_for` (ISO 8601), `label` and
            `reference`. Fails with `past_time`, `too_far_ahead`,
            `no_prospect` or `external_error`.
        """
        ...

    async def transfer_to_human(self, reason: str) -> ActionOutcome:
        """Hand the live call to a person.

        Returns:
            On success, `data` has `destination`, masked. Fails with
            `transfer_unavailable` (not a phone call, or nothing to transfer to)
            or `transfer_failed` (the carrier refused).
        """
        ...

    async def close(self) -> None:
        """Release whatever the backend holds. Safe to call more than once."""
        ...


class NullActionBackend:
    """The backend for a session that can act on nothing.

    Every action fails with `unavailable` and says so. Not a silent no-op, and
    deliberately not a success: an agent told a booking succeeded when nothing
    was booked is the exact lie Phase 7 exists to make impossible.
    """

    def __init__(self, timezone: str = "UTC") -> None:
        """Create the backend. `timezone` is still needed to tell the agent the time."""
        self._capabilities = Capabilities(timezone=timezone)

    @property
    def capabilities(self) -> Capabilities:
        """Nothing is possible."""
        return self._capabilities

    async def search_knowledge(self, query: str) -> ActionOutcome:
        """No knowledge base on this session."""
        return ActionOutcome.failure(UNAVAILABLE, "no knowledge base is available on this session")

    async def check_availability(self, day: date, preferred_time: time | None) -> ActionOutcome:
        """No calendar on this session."""
        return ActionOutcome.failure(UNAVAILABLE, "no calendar is configured on this session")

    async def book_meeting(
        self, start: datetime, attendee: AttendeeDetails, *, notes: str = ""
    ) -> ActionOutcome:
        """No calendar on this session."""
        return ActionOutcome.failure(UNAVAILABLE, "no calendar is configured on this session")

    async def schedule_callback(self, when: datetime, *, note: str = "") -> ActionOutcome:
        """Nowhere to record a callback on this session."""
        return ActionOutcome.failure(UNAVAILABLE, "callbacks cannot be scheduled on this session")

    async def transfer_to_human(self, reason: str) -> ActionOutcome:
        """Not a phone call."""
        return ActionOutcome.failure(UNAVAILABLE, "this session cannot be transferred")

    async def close(self) -> None:
        """Nothing to release."""
        return None
