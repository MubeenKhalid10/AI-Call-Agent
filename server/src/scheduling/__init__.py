"""Scheduling: finding a free meeting slot and booking it, with whichever calendar.

The public surface of the package. Import from here, not from the modules
underneath — `make_calendar` is the only thing that knows which provider is
configured, and keeping that single point is what makes the calendar swappable
without the conversation, the tools or the action service noticing.

    from src.scheduling import make_calendar

    calendar = make_calendar("calcom", api_key=..., event_type_id=..., ...)
    slots = await calendar.available_slots(start, end)
    booking = await calendar.book(slots[0].start, Attendee(name=..., email=...))

**Adding a provider** is three small edits, none of them outside this package:

1. Write `src/scheduling/<name>.py` with a `CalendarProvider` subclass.
2. Add its name to `CALENDAR_PROVIDERS` in `config.py`, and its settings.
3. Add a branch to `make_calendar` below.

Named `scheduling` rather than `calendar` because the standard library already
owns that name and a package that shadows it is a bug waiting for an import.
"""

from __future__ import annotations

from datetime import tzinfo

from .base import (
    Attendee,
    Booking,
    CalendarError,
    CalendarProvider,
    CalendarUnavailableError,
    Slot,
    SlotUnavailableError,
)
from .hours import BusinessHours
from .local import BusySource, LocalCalendarProvider

__all__ = [
    "Attendee",
    "Booking",
    "BusinessHours",
    "BusySource",
    "CalendarError",
    "CalendarProvider",
    "CalendarUnavailableError",
    "LocalCalendarProvider",
    "Slot",
    "SlotUnavailableError",
    "make_calendar",
]


def make_calendar(
    provider: str,
    *,
    tz: tzinfo,
    timezone_name: str,
    slot_minutes: int,
    hours: BusinessHours,
    min_notice_minutes: int,
    busy: BusySource | None = None,
    calcom_api_key: str | None = None,
    calcom_event_type_id: int | None = None,
    calcom_api_base: str | None = None,
    calcom_timeout_secs: float = 15.0,
) -> CalendarProvider | None:
    """Build the configured calendar provider, or None for `"none"`.

    Takes the settings as arguments rather than a `CalendarConfig`, so this
    package never imports `config.py` — which imports `hours.py` from here for
    validation, and a cycle between the two would be the first thing to break.

    Raises:
        ValueError: The provider is unknown, or Cal.com is selected without its
            key and event type. Raised at the point the calendar is built,
            which is session start, so the message reaches the log before a
            call rather than mid-booking.
    """
    if provider == "none":
        return None

    if provider == "local":
        return LocalCalendarProvider(
            busy,
            hours=hours,
            slot_minutes=slot_minutes,
            tz=tz,
            min_notice_minutes=min_notice_minutes,
        )

    if provider == "calcom":
        if not calcom_api_key or calcom_event_type_id is None:
            raise ValueError(
                "CALENDAR_PROVIDER is calcom but CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID are not "
                "both set. See the CALENDAR section of server/.env.example."
            )
        from .calcom import DEFAULT_API_BASE, CalComProvider

        return CalComProvider(
            calcom_api_key,
            calcom_event_type_id,
            timezone=timezone_name,
            slot_minutes=slot_minutes,
            api_base=calcom_api_base or DEFAULT_API_BASE,
            timeout_secs=calcom_timeout_secs,
        )

    raise ValueError(f"No calendar provider is implemented for CALENDAR_PROVIDER={provider!r}.")
