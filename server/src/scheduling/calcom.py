"""Cal.com: the only file in this project that knows Cal.com exists.

Cal.com is the calendar provider the requirement names, and the one whose public
API can actually *create* a booking — Calendly's cannot; it only issues
scheduling links, which a voice agent cannot hand to somebody on the phone. So
this is the provider behind `CALENDAR_PROVIDER=calcom`, against Cal.com's API v2:

* `GET  /v2/slots`     — free slots for an event type in a window
  (`cal-api-version: 2024-09-04`).
* `POST /v2/bookings`  — book one, with an attendee
  (`cal-api-version: 2024-08-13`).

Authentication is a bearer API key. The event type — which rep's calendar, how
long, which questions — is configured on the Cal.com side and referred to here
by id, which is where "whose calendar" is answered without this code knowing.

**Written against the documented API, not against a live account.** There is no
Cal.com account on the machine this was built on, so — as with SignalWire in
Phase 4 — the request shapes, headers and response parsing are exercised in
`tests/test_scheduling.py` against a stub HTTP session, and the first live call
is the first real test. The two places most likely to need a one-line change
if it disagrees are marked: how the slots response is shaped, and the field
name for booking notes. `check_credentials` (`uv run health.py calendar`)
reads the event type back, which is how a wrong key or id is found before a
call rather than during one.

**Booking is made once, and a lost answer is looked up (Phase 16).** Cal.com
enforces its own availability — a slot taken between the offer and the
booking is refused with a message this module reads as `SlotUnavailableError`
— so the double-booking check is the calendar's, not a copy of it here. What
the calendar cannot do is take an idempotency key, so `book` is never
retried: a POST that times out or loses its connection may have created the
booking, and repeating it could create two. Instead `find_booking` lists the
attendee's bookings around that start and adopts the one Cal.com made, if it
did — the same rule Phase 9 applies to placing a call.

**Every request has a timeout.** Cal.com is called from inside a tool turn
with a person on the line; aiohttp's default wait is five minutes.

**Why `aiohttp` and not an SDK.** Same reason as `twilio.py`: two requests, an
HTTP library already in the tree, and nothing blocking inside a voice pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from loguru import logger

from .base import (
    Attendee,
    Booking,
    CalendarError,
    CalendarProvider,
    CalendarUnavailableError,
    Slot,
    SlotUnavailableError,
)

DEFAULT_API_BASE = "https://api.cal.com/v2"

# The API version each endpoint is pinned to. Cal.com versions endpoints
# individually by date, and a request without the header gets the oldest
# behaviour, whose response shapes differ from the ones parsed below.
_SLOTS_API_VERSION = "2024-09-04"
_BOOKINGS_API_VERSION = "2024-08-13"
_EVENT_TYPES_API_VERSION = "2024-06-14"

# Phrases in a 4xx body that mean "the slot is gone" rather than "the request
# is wrong". Cal.com's messages are prose; this is the honest way to read them.
# `no_available_users` and `out_of_bounds` are its error codes for a slot
# nobody can host and a time outside the event type's availability — both
# answered the same way on a call: offer another time.
_TAKEN_MARKERS = (
    "not available",
    "no longer available",
    "already booked",
    "already has",
    "conflict",
    "no_available_users",
    "out_of_bounds",
    "unavailable",
)

# How far either side of the requested start a listed booking may sit and
# still be "the one we asked for": Cal.com rounds to the minute.
_MATCH_TOLERANCE = timedelta(minutes=1)


class CalComProvider(CalendarProvider):
    """Finds and books slots on a Cal.com event type."""

    name = "calcom"
    requires_email = True

    def __init__(
        self,
        api_key: str,
        event_type_id: int,
        *,
        timezone: str,
        slot_minutes: int,
        api_base: str = DEFAULT_API_BASE,
        session: aiohttp.ClientSession | None = None,
        timeout_secs: float = 15.0,
    ) -> None:
        """Create the provider.

        Args:
            api_key: A Cal.com API key (`cal_live_...`). Never logged.
            event_type_id: The event type to book — this is what decides whose
                calendar, how long, and what the invitation says.
            timezone: IANA zone slots are requested in and attendees see.
            slot_minutes: The event type's length, used to compute slot ends
                because the slots endpoint returns starts only.
            api_base: API root, for a self-hosted instance or a test double.
            session: An existing HTTP session. When omitted, one is created on
                first use and closed by `close()`. The tests pass a stub here.
            timeout_secs: Ceiling on one request (Phase 16). A booking that
                exceeds it is *ambiguous* — it may have been created — and is
                looked up rather than repeated.
        """
        self._api_key = api_key
        self._event_type_id = int(event_type_id)
        self._timezone = timezone
        self._slot_minutes = int(slot_minutes)
        self._length = timedelta(minutes=slot_minutes)
        self._api_base = api_base.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._timeout_secs = timeout_secs

    def describe(self) -> str:
        """One line for the startup log. Never the key."""
        return f"calcom (event type {self._event_type_id}, {self._timezone})"

    async def available_slots(self, start: datetime, end: datetime) -> list[Slot]:
        """Free slots for the event type with a start in `[start, end)`."""
        body = await self._request(
            "GET",
            "slots",
            version=_SLOTS_API_VERSION,
            params={
                "eventTypeId": str(self._event_type_id),
                "start": _iso(start),
                "end": _iso(end),
                "timeZone": self._timezone,
            },
        )
        slots: list[Slot] = []
        for raw_start in _slot_starts(body.get("data")):
            moment = _parse_iso(raw_start)
            if moment is None:
                logger.warning(f"CALENDAR | calcom returned an unreadable slot start {raw_start!r}")
                continue
            if start <= moment < end:
                slots.append(Slot(start=moment, end=moment + self._length))
        slots.sort(key=lambda slot: slot.start)
        return slots

    async def book(self, start: datetime, attendee: Attendee, *, notes: str = "") -> Booking:
        """Create the booking. Raises rather than returning on anything but success."""
        if not attendee.email:
            raise CalendarError("Cal.com requires an attendee email address to book")

        attendee_payload: dict[str, Any] = {
            "name": attendee.name,
            "email": attendee.email,
            "timeZone": attendee.timezone or self._timezone,
            "language": "en",
        }
        if attendee.phone:
            attendee_payload["phoneNumber"] = attendee.phone

        payload: dict[str, Any] = {
            "start": _iso(start.astimezone(UTC)),
            "eventTypeId": self._event_type_id,
            "attendee": attendee_payload,
            "metadata": {"source": "ai-voice-agent"},
        }
        if notes:
            # Both the booking-field response and metadata carry the notes,
            # because which one the event type surfaces depends on how it was
            # set up in Cal.com. Metadata values are capped at 500 characters.
            payload["bookingFieldsResponses"] = {"notes": notes}
            payload["metadata"]["notes"] = notes[:500]

        try:
            body = await self._request("POST", "bookings", version=_BOOKINGS_API_VERSION, json=payload)
        except CalendarUnavailableError as exc:
            # The answer was lost, not refused. Cal.com may have booked it:
            # ask before saying anything, and never POST again.
            found = await self._resolve_lost_answer(start, attendee, exc)
            if found is not None:
                return found
            raise CalendarUnavailableError(
                f"{exc} No booking for {attendee.email} at {_iso(start.astimezone(UTC))} was found "
                f"afterwards, so nothing was booked."
            ) from exc
        return self._booking_from(body.get("data"), start)

    def _booking_from(self, data: Any, start: datetime) -> Booking:
        """Read a booking payload into a `Booking`."""
        data = data if isinstance(data, Mapping) else {}
        booked_start = _parse_iso(data.get("start")) or start
        booked_end = _parse_iso(data.get("end")) or (booked_start + self._length)
        reference = data.get("uid") or data.get("id")
        logger.info(f"CALENDAR | calcom booked {booked_start.isoformat()} ref {reference}")
        return Booking(
            provider=self.name,
            start=booked_start,
            end=booked_end,
            reference=str(reference) if reference is not None else None,
            raw=data,
        )

    async def _resolve_lost_answer(
        self, start: datetime, attendee: Attendee, cause: CalendarUnavailableError
    ) -> Booking | None:
        """After a `book` whose answer was lost: did Cal.com make it anyway?"""
        logger.warning(
            f"CALENDAR | calcom did not answer the booking ({(str(cause).splitlines() or [type(cause).__name__])[0]}); "
            f"checking whether it was created before telling the caller anything"
        )
        try:
            found = await self.find_booking(start, attendee)
        except CalendarError as lookup:
            logger.error(f"CALENDAR | calcom could not be asked either: {lookup}")
            return None
        if found is not None:
            logger.warning(f"CALENDAR | calcom had booked it after all: ref {found.reference}")
        return found

    async def find_booking(self, start: datetime, attendee: Attendee) -> Booking | None:
        """The attendee's upcoming booking on this event type at `start`, if there is one. Phase 16.

        `GET /bookings` filtered by attendee email, event type and a window
        one minute either side of the start. A cancelled or rejected booking
        is not a booking.

        Raises:
            CalendarError / CalendarUnavailableError: As `_request` does.
        """
        if not attendee.email:
            return None
        window_start = start.astimezone(UTC) - _MATCH_TOLERANCE
        window_end = start.astimezone(UTC) + self._length + _MATCH_TOLERANCE
        body = await self._request(
            "GET",
            "bookings",
            version=_BOOKINGS_API_VERSION,
            params={
                "attendeeEmail": attendee.email,
                "eventTypeId": str(self._event_type_id),
                "afterStart": _iso(window_start),
                "beforeEnd": _iso(window_end),
                "take": "20",
            },
        )
        entries = body.get("data")
        if isinstance(entries, Mapping):
            entries = entries.get("bookings") or entries.get("items") or []
        if not isinstance(entries, list):
            return None
        wanted = start.astimezone(UTC)
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            status = str(entry.get("status") or "").lower()
            if status in ("cancelled", "canceled", "rejected"):
                continue
            booked_start = _parse_iso(entry.get("start") or entry.get("startTime"))
            if booked_start is None or abs(booked_start - wanted) > _MATCH_TOLERANCE:
                continue
            return self._booking_from(entry, start)
        return None

    async def check_credentials(self) -> str:
        """Read the event type back: the key works, and the id is bookable. Phase 16.

        Also compares the event type's length with `CALENDAR_SLOT_MINUTES`,
        because a mismatch books thirty-minute meetings into a sixty-minute
        diary silently; the health check reports it as degraded.
        """
        body = await self._request(
            "GET", f"event-types/{self._event_type_id}", version=_EVENT_TYPES_API_VERSION
        )
        data = body.get("data") if isinstance(body.get("data"), Mapping) else {}
        title = str(data.get("title") or data.get("slug") or "").strip() or "untitled"
        length = data.get("lengthInMinutes") or data.get("length")
        text = f"event type {self._event_type_id} '{title}'"
        try:
            minutes = int(length) if length is not None else None
        except (TypeError, ValueError):
            minutes = None
        if minutes is not None:
            text += f", {minutes} min"
            if minutes != self._slot_minutes:
                text += f" — differs from CALENDAR_SLOT_MINUTES={self._slot_minutes}"
        return text

    async def close(self) -> None:
        """Close the HTTP session, if this provider created one."""
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    # --- HTTP ---------------------------------------------------------------

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        version: str,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One authenticated request; the decoded JSON body on 2xx.

        Raises:
            SlotUnavailableError: A 4xx whose message says the slot is gone.
            CalendarError: Any other 4xx — the request itself was wrong.
            CalendarUnavailableError: Unreachable, or a 5xx.
        """
        session = await self._http()
        url = f"{self._api_base}/{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "cal-api-version": version,
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout_secs)
        try:
            async with session.request(
                method, url, headers=headers, params=params, json=json, timeout=timeout
            ) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except Exception:  # noqa: BLE001 - a non-JSON body is itself the problem
                    body = {"message": (await response.text())[:400]}

                if 200 <= status < 300:
                    return body if isinstance(body, dict) else {"data": body}

                message = _error_text(body)
                if status >= 500:
                    raise CalendarUnavailableError(
                        f"Cal.com returned {status} for {method} /{path}. This is Cal.com's end; "
                        f"retrying in a moment is reasonable.  {message}"
                    )
                if status in (401, 403):
                    raise CalendarError(
                        f"Cal.com rejected the API key (HTTP {status}). Check CALCOM_API_KEY in "
                        f"server/.env.  {message}"
                    )
                lowered = message.lower()
                if any(marker in lowered for marker in _TAKEN_MARKERS):
                    raise SlotUnavailableError(message or "that slot is no longer available")
                raise CalendarError(
                    f"Cal.com refused the request (HTTP {status}).  {message}"
                    + (
                        "  Check CALCOM_EVENT_TYPE_ID exists and is bookable."
                        if status == 404
                        else ""
                    )
                )
        except TimeoutError as exc:
            raise CalendarUnavailableError(
                f"Cal.com did not answer within {self._timeout_secs:g}s for {method} /{path}. "
                f"Whether it acted on the request is unknown."
            ) from exc
        except aiohttp.ClientError as exc:
            raise CalendarUnavailableError(
                f"Could not reach Cal.com ({exc.__class__.__name__}: {exc}). "
                f"Check this machine's internet connection."
            ) from exc


def _slot_starts(data: Any) -> list[str]:
    """Every slot start in a slots response, whatever shape it took.

    The 2024-09-04 shape is `{"YYYY-MM-DD": [{"start": "..."}, ...]}`. Older
    shapes are a list, or entries that are bare strings. All are read, because
    the cost of reading one more shape is nothing and the cost of a version
    mismatch would otherwise be "no slots, ever", which looks like a full diary.
    """
    entries: list[Any] = []
    if isinstance(data, Mapping):
        if "slots" in data and isinstance(data["slots"], (Mapping, list)):
            return _slot_starts(data["slots"])
        for value in data.values():
            if isinstance(value, list):
                entries.extend(value)
    elif isinstance(data, list):
        entries = data

    starts: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            starts.append(entry)
        elif isinstance(entry, Mapping):
            value = entry.get("start") or entry.get("time")
            if isinstance(value, str):
                starts.append(value)
    return starts


def _iso(moment: datetime) -> str:
    """ISO 8601 with an explicit offset, which is what Cal.com expects."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    """Read Cal.com's timestamps (`...Z`, `...+05:00`, `...000Z`) into an aware datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _error_text(body: Any) -> str:
    """Pull Cal.com's own message out of an error body, whichever key it used."""
    if not isinstance(body, Mapping):
        return ""
    for key in ("message", "error", "detail"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            nested = _error_text(value)
            if nested:
                return nested
        if isinstance(value, list) and value:
            return "; ".join(str(item) for item in value[:3])
    return ""
