"""A calendar with no vendor behind it: business hours, minus what is already booked.

The default `CALENDAR_PROVIDER`, and a real one rather than a stand-in. It
offers slots on a fixed grid inside configured business hours, refuses anything
already booked, and the bookings it makes land in the `meetings` table where
`campaign.py meetings` lists them for whoever runs the diary. What it does *not*
do is put anything into a sales rep's own calendar — that is what `calcom.py`
is for — so a booking here is a booking *in this system*, and the agent's
confirmation is true in exactly that sense.

**Why it exists.** Two reasons, both about honesty. First, the booking flow —
check, offer, choose, book, confirm only on success — has to be exercisable
end to end by the eval suite and by a developer with no Cal.com account, or it
would ship untested. Second, a bot deployed before a calendar integration is
agreed still needs `book_meeting` to *mean* something: with this provider it
means a row a person will act on, which is a smaller promise than a calendar
invitation and a true one.

**It does not own the bookings.** `BusySource` is how it learns what is taken —
implemented by `CampaignStore.busy_between` over the meetings table — and the
action service, not this class, writes the meeting row after a successful
`book`. So `book` here is a check, not a write: the slot is inside hours, in
the future, and not already busy. The write is one step up, where every
provider's booking is recorded the same way. The gap between check and write
was not transactional until Phase 16: the `meetings` table now carries an
exclusion constraint over `(start_at, end_at)` for live local bookings, so
the write itself refuses an overlap — two bots booking the same slot in the
same second get one row and one `SlotUnavailableError`, whichever checked
first. The check here is what keeps the *offer* honest; the constraint is
what keeps the diary honest.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Protocol, runtime_checkable

from .base import Attendee, Booking, CalendarProvider, Slot, SlotUnavailableError
from .hours import BusinessHours


@runtime_checkable
class BusySource(Protocol):
    """Where the local calendar learns what is already taken."""

    async def busy_between(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """Booked intervals overlapping `[start, end)`, as aware `(start, end)` pairs."""
        ...


class LocalCalendarProvider(CalendarProvider):
    """Slots on a grid inside business hours, excluding what `busy` reports."""

    name = "local"
    requires_email = False

    def __init__(
        self,
        busy: BusySource | None,
        *,
        hours: BusinessHours,
        slot_minutes: int,
        tz: tzinfo,
        min_notice_minutes: int = 60,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """Create the provider.

        Args:
            busy: Where existing bookings come from. None means nothing is ever
                busy, which is right for a test and wrong for anything else.
            hours: When slots may be offered, in `tz`.
            slot_minutes: Slot length and grid spacing.
            tz: The calendar's zone. Business hours are read in it.
            min_notice_minutes: Nothing is offered sooner than this from now.
                A meeting "in ten minutes" agreed on a cold call is one nobody
                turns up to.
            now: The clock, for tests. None uses the real one.
        """
        if slot_minutes <= 0:
            raise ValueError("slot_minutes must be positive")
        self._busy = busy
        self._hours = hours
        self._slot = timedelta(minutes=slot_minutes)
        self._tz = tz
        self._notice = timedelta(minutes=max(0, min_notice_minutes))
        self._clock = now or (lambda: datetime.now(UTC))

    def describe(self) -> str:
        """One line for the startup log."""
        return (
            f"local ({self._hours.describe()}, {int(self._slot.total_seconds() // 60)}-minute slots)"
        )

    async def available_slots(self, start: datetime, end: datetime) -> list[Slot]:
        """Every free slot starting in `[start, end)`, earliest first."""
        earliest = self._clock() + self._notice
        busy = await self._busy_between(start, end)

        slots: list[Slot] = []
        day = start.astimezone(self._tz).date()
        last_day = (end.astimezone(self._tz) - timedelta(microseconds=1)).date()
        while day <= last_day:
            if self._hours.is_open_on(day.weekday()):
                cursor = datetime.combine(day, self._hours.opens, tzinfo=self._tz)
                closes = datetime.combine(day, self._hours.closes, tzinfo=self._tz)
                while cursor + self._slot <= closes:
                    slot = Slot(start=cursor, end=cursor + self._slot)
                    if start <= slot.start < end and slot.start >= earliest and not _overlaps(slot, busy):
                        slots.append(slot)
                    cursor += self._slot
            day += timedelta(days=1)
        return slots

    async def book(self, start: datetime, attendee: Attendee, *, notes: str = "") -> Booking:
        """Confirm the slot is bookable. The write happens one level up.

        Raises:
            SlotUnavailableError: Outside hours, in the past, off the grid, or
                already busy — each with a message saying which.
        """
        local = start.astimezone(self._tz)
        if not self._hours.is_open_on(local.weekday()):
            raise SlotUnavailableError(f"{local:%A} is not a working day")
        opens = datetime.combine(local.date(), self._hours.opens, tzinfo=self._tz)
        closes = datetime.combine(local.date(), self._hours.closes, tzinfo=self._tz)
        if local < opens or local + self._slot > closes:
            raise SlotUnavailableError(f"{local:%H:%M} is outside business hours")
        offset = local - opens
        if offset % self._slot != timedelta(0):
            raise SlotUnavailableError(f"{local:%H:%M} is not on the slot grid")
        if local < self._clock() + self._notice:
            raise SlotUnavailableError("that time is too soon to book")

        slot = Slot(start=local, end=local + self._slot)
        if _overlaps(slot, await self._busy_between(slot.start, slot.end)):
            raise SlotUnavailableError("that time has just been taken")

        return Booking(provider=self.name, start=slot.start, end=slot.end, reference=None)

    async def _busy_between(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        if self._busy is None:
            return []
        return [
            (
                busy_start.astimezone(UTC) if busy_start.tzinfo else busy_start.replace(tzinfo=UTC),
                busy_end.astimezone(UTC) if busy_end.tzinfo else busy_end.replace(tzinfo=UTC),
            )
            for busy_start, busy_end in await self._busy.busy_between(start, end)
        ]


def _overlaps(slot: Slot, busy: list[tuple[datetime, datetime]]) -> bool:
    """Whether any busy interval intersects the slot."""
    start, end = slot.start.astimezone(UTC), slot.end.astimezone(UTC)
    return any(busy_start < end and busy_end > start for busy_start, busy_end in busy)
