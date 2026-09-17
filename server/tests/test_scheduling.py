#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for the calendar providers. No account, no network, no database.

Run it from the `server/` directory::

    uv run python tests/test_scheduling.py

Two providers, checked two different ways because they are different kinds of
thing:

* **The local calendar** is pure arithmetic over business hours, a slot grid and
  a list of busy intervals, so it is checked exhaustively against fixed dates —
  including the timezone seam, where a Karachi working day is asked for in UTC.
* **Cal.com** is an HTTP client, so it is checked the way `test_telephony.py`
  checks Twilio: the real provider wired to a stub session that records every
  request and replays canned responses. What is asserted is the request the
  provider builds — URL, headers, query, JSON body — and how it reads what comes
  back, including the three kinds of failure. **No real booking is made.**

Plain script, no test dependency. Exit status 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from src.scheduling import (  # noqa: E402
    Attendee,
    BusinessHours,
    CalendarError,
    CalendarUnavailableError,
    LocalCalendarProvider,
    Slot,
    SlotUnavailableError,
    make_calendar,
)
from src.scheduling.calcom import CalComProvider  # noqa: E402

_failures: list[str] = []

KARACHI = ZoneInfo("Asia/Karachi")
NOW = datetime(2026, 9, 4, 10, 0, tzinfo=KARACHI)  # Friday
MONDAY = datetime(2026, 9, 7, 0, 0, tzinfo=KARACHI)


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def raises(call, exception_type, *, contains: str = "") -> bool:
    try:
        call()
    except exception_type as exc:
        return contains in str(exc)
    except Exception:  # noqa: BLE001
        return False
    return False


async def araises(coroutine, exception_type, *, contains: str = "") -> bool:
    try:
        await coroutine
    except exception_type as exc:
        return contains in str(exc)
    except Exception:  # noqa: BLE001
        return False
    return False


# --- Business hours -----------------------------------------------------------


def check_hours() -> None:
    print("\n=== business hours ===")
    hours = BusinessHours.parse("09:00-17:00", "mon-fri")
    check("hours parse", hours.opens == time(9, 0) and hours.closes == time(17, 0))
    check("a weekday range parses", hours.days == frozenset({0, 1, 2, 3, 4}))
    check("it describes itself", hours.describe() == "09:00-17:00 mon-fri")
    check("a day list parses", BusinessHours.parse("9-17", "mon,wed,fri").days == frozenset({0, 2, 4}))
    check("a wrapping range parses", BusinessHours.parse("9-17", "sat-mon").days == frozenset({5, 6, 0}))
    check("full day names are accepted", BusinessHours.parse("9-17", "Monday-Friday").days == frozenset({0, 1, 2, 3, 4}))
    check("closing before opening is refused", raises(lambda: BusinessHours.parse("17:00-09:00", "mon-fri"), ValueError, contains="close before"))
    check("prose is refused", raises(lambda: BusinessHours.parse("nine to five", "mon-fri"), ValueError))
    check("an unknown day is refused", raises(lambda: BusinessHours.parse("9-17", "mon-funday"), ValueError, contains="not a day"))
    check("no days is refused", raises(lambda: BusinessHours.parse("9-17", ""), ValueError))


# --- The local calendar ----------------------------------------------------------


class BusyList:
    """A `BusySource` over a fixed list."""

    def __init__(self, busy: list[tuple[datetime, datetime]]) -> None:
        self.busy = busy
        self.asked: list[tuple[datetime, datetime]] = []

    async def busy_between(self, start: datetime, end: datetime):
        self.asked.append((start, end))
        return [(s, e) for s, e in self.busy if s < end and e > start]


def local(busy: BusyList | None = None, *, notice: int = 60, slot: int = 30) -> LocalCalendarProvider:
    return LocalCalendarProvider(
        busy,
        hours=BusinessHours.parse("09:00-17:00", "mon-fri"),
        slot_minutes=slot,
        tz=KARACHI,
        min_notice_minutes=notice,
        now=lambda: NOW,
    )


async def check_local() -> None:
    print("\n=== the local calendar ===")

    provider = local()
    slots = await provider.available_slots(MONDAY, MONDAY + timedelta(days=1))
    check("a working day has sixteen half-hour slots", len(slots) == 16, str(len(slots)))
    check("starting at opening time", slots[0].start == datetime(2026, 9, 7, 9, 0, tzinfo=KARACHI))
    check("ending so the last one finishes at closing", slots[-1].end == datetime(2026, 9, 7, 17, 0, tzinfo=KARACHI))
    check("every slot is timezone-aware", all(s.start.tzinfo is not None for s in slots))
    check("a slot renders for the model", slots[2].to_dict(KARACHI) == {"start": "2026-09-07T10:00", "end": "2026-09-07T10:30", "label": "Monday 7 September at 10:00"})

    saturday = MONDAY - timedelta(days=2)
    check("a weekend has none", not await provider.available_slots(saturday, saturday + timedelta(days=1)))

    # The timezone seam: the same Karachi day asked for as a UTC window.
    utc_window_start = MONDAY.astimezone(UTC)
    slots_utc = await provider.available_slots(utc_window_start, utc_window_start + timedelta(days=1))
    check("the same day asked for in UTC gives the same slots", [s.start for s in slots_utc] == [s.start for s in slots])

    today = NOW.replace(hour=0, minute=0)
    todays = await provider.available_slots(today, today + timedelta(days=1))
    check("today, the minimum notice hides the next hour", todays[0].start == datetime(2026, 9, 4, 11, 0, tzinfo=KARACHI), str(todays[0].start))
    check("and nothing before now", all(s.start > NOW for s in todays))

    busy = BusyList([(datetime(2026, 9, 7, 10, 0, tzinfo=KARACHI), datetime(2026, 9, 7, 11, 0, tzinfo=KARACHI))])
    slots = await local(busy).available_slots(MONDAY, MONDAY + timedelta(days=1))
    check("a busy hour removes its two slots", len(slots) == 14 and all(not (time(10, 0) <= s.start.time() < time(11, 0)) for s in slots))
    check("the busy source was asked about the window", busy.asked and busy.asked[0][0] == MONDAY)
    partial = BusyList([(datetime(2026, 9, 7, 10, 15, tzinfo=KARACHI), datetime(2026, 9, 7, 10, 20, tzinfo=KARACHI))])
    slots = await local(partial).available_slots(MONDAY, MONDAY + timedelta(days=1))
    check("a five-minute booking still blocks the slot it sits in", not any(s.start.time() == time(10, 0) for s in slots) and len(slots) == 15)

    hourly = local(slot=60)
    check("an hour grid gives eight slots", len(await hourly.available_slots(MONDAY, MONDAY + timedelta(days=1))) == 8)

    attendee = Attendee(name="Sarah Khan", timezone="Asia/Karachi")
    booking = await local(busy).book(datetime(2026, 9, 7, 11, 0, tzinfo=KARACHI), attendee)
    check("a free grid slot books", booking.provider == "local" and booking.reference is None)
    check("for the slot's length", booking.end - booking.start == timedelta(minutes=30))
    check("a busy slot refuses", await araises(local(busy).book(datetime(2026, 9, 7, 10, 30, tzinfo=KARACHI), attendee), SlotUnavailableError, contains="taken"))
    check("outside hours refuses", await araises(provider.book(datetime(2026, 9, 7, 17, 0, tzinfo=KARACHI), attendee), SlotUnavailableError, contains="outside"))
    check("a weekend refuses", await araises(provider.book(datetime(2026, 9, 5, 10, 0, tzinfo=KARACHI), attendee), SlotUnavailableError, contains="not a working day"))
    check("off the grid refuses", await araises(provider.book(datetime(2026, 9, 7, 10, 10, tzinfo=KARACHI), attendee), SlotUnavailableError, contains="grid"))
    check("too soon refuses", await araises(provider.book(datetime(2026, 9, 4, 10, 30, tzinfo=KARACHI), attendee), SlotUnavailableError, contains="too soon"))
    check("a UTC start on the grid books", (await provider.book(datetime(2026, 9, 7, 5, 0, tzinfo=UTC), attendee)).start == datetime(2026, 9, 7, 5, 0, tzinfo=UTC))
    check("it needs no email", provider.requires_email is False)
    check("it describes itself", "09:00-17:00" in provider.describe())
    check("a zero slot length is refused", raises(lambda: local(slot=0), ValueError))


# --- Cal.com against a stub -----------------------------------------------------------


class StubResponse:
    def __init__(self, status: int, body) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class StubSession:
    """An `aiohttp.ClientSession` stand-in for the Cal.com client's call shape."""

    def __init__(self, responses: list[StubResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.closed = False

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        # Phase 16: every Cal.com request carries a timeout; recorded so it can be asserted.
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {}), "params": dict(params or {}), "json": json, "timeout": timeout})
        if not self._responses:
            raise AssertionError(f"stub had no response left for {method} {url}")
        return self._responses.pop(0)

    async def close(self):
        self.closed = True


def calcom(responses: list[StubResponse]) -> tuple[CalComProvider, StubSession]:
    session = StubSession(responses)
    provider = CalComProvider("cal_live_secret", 4242, timezone="Asia/Karachi", slot_minutes=30, session=session)
    return provider, session


async def check_calcom() -> None:
    print("\n=== Cal.com, against a stub ===")

    provider, session = calcom([
        StubResponse(200, {"status": "success", "data": {
            "2026-09-07": [{"start": "2026-09-07T05:00:00.000Z"}, {"start": "2026-09-07T09:00:00.000Z"}],
        }}),
    ])
    slots = await provider.available_slots(MONDAY, MONDAY + timedelta(days=1))
    request = session.requests[0]
    check("availability is a GET to /slots", request["method"] == "GET" and request["url"] == "https://api.cal.com/v2/slots")
    check("for the event type, in the zone", request["params"]["eventTypeId"] == "4242" and request["params"]["timeZone"] == "Asia/Karachi")
    check("over the window, with offsets", request["params"]["start"] == "2026-09-07T00:00:00+05:00")
    check("with the bearer key", request["headers"]["Authorization"] == "Bearer cal_live_secret")
    check("and the slots API version", request["headers"]["cal-api-version"] == "2024-09-04")
    check("two slots come back", len(slots) == 2)
    check("read into aware datetimes — 05:00Z is 10:00 Karachi", slots[0].start == datetime(2026, 9, 7, 10, 0, tzinfo=KARACHI))
    check("with the configured length", slots[0].end - slots[0].start == timedelta(minutes=30))
    check("the key is never in describe()", "secret" not in provider.describe() and "4242" in provider.describe())
    check("it needs an email", provider.requires_email is True)

    provider, session = calcom([StubResponse(200, {"status": "success", "data": {"slots": {"2026-09-07": ["2026-09-07T05:00:00Z"]}}})])
    check("an older response shape still reads", len(await provider.available_slots(MONDAY, MONDAY + timedelta(days=1))) == 1)
    provider, session = calcom([StubResponse(200, {"status": "success", "data": {}})])
    check("no slots is an empty list, not an error", await provider.available_slots(MONDAY, MONDAY + timedelta(days=1)) == [])

    provider, session = calcom([
        StubResponse(201, {"status": "success", "data": {"uid": "bk_abc123", "start": "2026-09-07T05:00:00.000Z", "end": "2026-09-07T05:30:00.000Z"}}),
    ])
    attendee = Attendee(name="Sarah Khan", email="sarah@meridian.example", phone="+923001234567", timezone="Asia/Karachi")
    booking = await provider.book(datetime(2026, 9, 7, 10, 0, tzinfo=KARACHI), attendee, notes="fuel numbers")
    request = session.requests[0]
    check("booking is a POST to /bookings", request["method"] == "POST" and request["url"] == "https://api.cal.com/v2/bookings")
    check("with the bookings API version", request["headers"]["cal-api-version"] == "2024-08-13")
    body = request["json"]
    check("the start is sent in UTC", body["start"] == "2026-09-07T05:00:00Z")
    check("the event type is an integer", body["eventTypeId"] == 4242)
    check("the attendee has name, email, zone and phone", body["attendee"] == {"name": "Sarah Khan", "email": "sarah@meridian.example", "timeZone": "Asia/Karachi", "language": "en", "phoneNumber": "+923001234567"})
    check("the notes travel both ways Cal.com might read them", body["bookingFieldsResponses"]["notes"] == "fuel numbers" and body["metadata"]["notes"] == "fuel numbers")
    check("the source is marked", body["metadata"]["source"] == "ai-voice-agent")
    check("the booking carries the uid", booking.reference == "bk_abc123" and booking.provider == "calcom")
    check("and the confirmed times", booking.start == datetime(2026, 9, 7, 5, 0, tzinfo=UTC) and booking.end == datetime(2026, 9, 7, 5, 30, tzinfo=UTC))

    provider, session = calcom([])
    check("no email is refused before any request", await araises(provider.book(MONDAY, Attendee(name="X")), CalendarError, contains="email") and not session.requests)

    provider, _ = calcom([StubResponse(400, {"status": "error", "error": {"message": "User either already has booking at this time or is not available"}})])
    check("a taken slot is `SlotUnavailableError`", await araises(provider.book(MONDAY, attendee), SlotUnavailableError))
    provider, _ = calcom([StubResponse(401, {"message": "Unauthorized"})])
    check("a rejected key names the setting", await araises(provider.book(MONDAY, attendee), CalendarError, contains="CALCOM_API_KEY"))
    provider, _ = calcom([StubResponse(404, {"message": "Event type not found"})])
    check("a missing event type names the setting", await araises(provider.available_slots(MONDAY, MONDAY + timedelta(days=1)), CalendarError, contains="CALCOM_EVENT_TYPE_ID"))
    provider, _ = calcom([StubResponse(503, {"message": "upstream"})])
    check("a 5xx is `CalendarUnavailableError`", await araises(provider.available_slots(MONDAY, MONDAY + timedelta(days=1)), CalendarUnavailableError, contains="Cal.com's end"))
    provider, _ = calcom([StubResponse(422, {"message": ["start must be in the future"]})])
    check("a list-shaped error message is read", await araises(provider.book(MONDAY, attendee), CalendarError, contains="start must be in the future"))

    provider, session = calcom([])
    await provider.close()
    check("closing does not close a session it did not open", session.closed is False)


def check_factory() -> None:
    print("\n=== make_calendar ===")
    hours = BusinessHours.parse("09:00-17:00", "mon-fri")
    common = dict(tz=KARACHI, timezone_name="Asia/Karachi", slot_minutes=30, hours=hours, min_notice_minutes=60)
    check("none is None", make_calendar("none", **common) is None)
    check("local builds the local provider", isinstance(make_calendar("local", **common), LocalCalendarProvider))
    check("calcom without credentials is refused", raises(lambda: make_calendar("calcom", **common), ValueError, contains="CALCOM_API_KEY"))
    check("calcom with credentials builds", isinstance(make_calendar("calcom", calcom_api_key="k", calcom_event_type_id=1, **common), CalComProvider))
    check("an unknown provider is refused", raises(lambda: make_calendar("outlook", **common), ValueError))
    check("a Slot is a plain pair", Slot(MONDAY, MONDAY + timedelta(minutes=30)).end > MONDAY)


async def main() -> int:
    print("Scheduling checks — no account, no network, no database.")
    check_hours()
    await check_local()
    await check_calcom()
    check_factory()

    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
