"""The seam between "book a meeting" and "which calendar it goes into".

Everything in this module is provider-agnostic. A provider implementation
(`calcom.py`, `local.py`) is the only place that knows a vendor's URLs, payload
shapes or error vocabulary, and nothing outside `src/scheduling/` imports one
directly — `make_calendar` in `__init__.py` hands back a `CalendarProvider`, and
the action service only ever sees this file's types. That is the requirement
"do not hard-code a provider into conversation logic", made structural: the
conversation layer does not import this package at all, and the one module that
does (`src/actions/service.py`) sees an abstract base class.

**Two operations is the whole contract.** Find the free slots in a window, and
book one. Deliberately not in it: rescheduling, cancelling, listing bookings,
managing event types. None of those happens on a cold call, and a contract that
carried them would be a contract every provider had to lie about implementing.

**Times are timezone-aware everywhere.** A provider receives aware `datetime`s
and returns aware `datetime`s. The person being called, the sales rep whose
calendar this is, and the machine running the bot are routinely in three
different zones, and a naive time anywhere in this chain is a meeting at the
wrong hour.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any


class CalendarError(RuntimeError):
    """The calendar refused or could not do what was asked. Message is for the log."""


class CalendarUnavailableError(CalendarError):
    """The calendar's API could not be reached, or answered with a server error.

    Distinct from `CalendarError` because retrying is reasonable: nothing was
    wrong with the request.
    """


class SlotUnavailableError(CalendarError):
    """The slot was free when offered and is not any more.

    Its own type because the right response is specific — offer another of the
    free times — where every other failure means "a colleague will confirm".
    """


@dataclass(frozen=True)
class Slot:
    """One bookable window."""

    start: datetime
    end: datetime

    def to_dict(self, tz: tzinfo) -> dict[str, Any]:
        """The slot as the model sees it: ISO times in `tz`, plus a spoken label."""
        from ..conversation.timeparse import label

        local_start = self.start.astimezone(tz)
        return {
            "start": local_start.strftime("%Y-%m-%dT%H:%M"),
            "end": self.end.astimezone(tz).strftime("%Y-%m-%dT%H:%M"),
            "label": label(local_start),
        }


@dataclass(frozen=True)
class Attendee:
    """Who the meeting is for, as much as the call knows.

    Attributes:
        timezone: The IANA zone the attendee should see the meeting in — the
            call's zone, which is the best guess a cold call has.
    """

    name: str
    email: str | None = None
    phone: str | None = None
    timezone: str = "UTC"


@dataclass(frozen=True)
class Booking:
    """A confirmed booking, as the provider reports it.

    Attributes:
        reference: The provider's own id for the booking, when it gives one.
            The local calendar gives none; the row it becomes is the reference.
        raw: The provider's payload, unmodified, for anything this shape does
            not carry.
    """

    provider: str
    start: datetime
    end: datetime
    reference: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


class CalendarProvider(ABC):
    """Finds free slots and books one of them, with one calendar."""

    #: Provider name, matching the `CALENDAR_PROVIDER` value that selects it.
    name: str = "unknown"

    #: Whether `book` needs `Attendee.email`. Cal.com does; the local calendar
    #: does not. Surfaced so the agent asks for the address *before* trying,
    #: rather than after a failure.
    requires_email: bool = False

    @abstractmethod
    async def available_slots(self, start: datetime, end: datetime) -> list[Slot]:
        """Free slots whose start lies in `[start, end)`, earliest first.

        Raises:
            CalendarError: The calendar refused the request.
            CalendarUnavailableError: The calendar could not be reached.
        """

    @abstractmethod
    async def book(self, start: datetime, attendee: Attendee, *, notes: str = "") -> Booking:
        """Book the slot starting at `start`.

        Raises:
            SlotUnavailableError: The slot is no longer free.
            CalendarError: The calendar refused for another reason — a missing
                email, an unknown event type, bad credentials.
            CalendarUnavailableError: The calendar could not be reached.
        """

    async def find_booking(self, start: datetime, attendee: Attendee) -> Booking | None:
        """The booking this calendar already holds for `attendee` at `start`, if any. Phase 16.

        The ambiguity resolver: `book` whose answer was lost — a timeout, a
        dropped connection — may or may not have created the booking, and a
        provider that can list its bookings asks before the caller is told
        anything. The default says it cannot look, which is honest for the
        local calendar (its bookings are the rows one level up).
        """
        return None

    async def check_credentials(self) -> str:
        """Confirm the calendar is reachable and configured, booking nothing. Phase 16.

        The calendar health check: the cheapest authenticated read the
        provider offers — for Cal.com, the event type itself, which is also
        the setting most likely to be wrong.

        Returns:
            A short description of what answered, for the health report.

        Raises:
            CalendarError: The credentials or the event type were rejected.
            CalendarUnavailableError: The calendar could not be reached.
            NotImplementedError: This provider has no cheap check.
        """
        raise NotImplementedError(f"{self.name} has no credential check")

    async def close(self) -> None:
        """Release whatever the provider holds. Safe to call more than once."""

    def describe(self) -> str:
        """One line for the startup log. Never includes a credential."""
        return self.name
