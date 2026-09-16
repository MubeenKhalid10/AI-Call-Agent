"""Reading the dates and times the model hands to the tools, strictly.

The model is told the current date, time and timezone in its system instruction
and is asked to give the tools ISO 8601 — `2026-09-08` for a day,
`2026-09-08T10:00` for a moment. This module is the strict reader for those, and
it is strict on purpose: "next Tuesday" is *refused* here, with a message that
tells the model to work the date out and try again, rather than parsed by a
natural-language date library that would sometimes be confidently wrong. A
callback scheduled for the wrong day is a person phoned when they asked not to
be; a tool that says "give me an exact date" costs one extra sentence.

Everything here is pure and timezone-aware. A naive time is read in the
session's timezone, never the machine's, because the machine this runs on and
the person being called are frequently not in the same one.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# `YYYY-MM-DD`, optionally followed by a time. The time part is deliberately
# permissive about the separator (`T` or a space) and about seconds, because
# models produce all of those, and deliberately not permissive about anything
# else.
_DAY = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")
_WHEN = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*"
    r"(Z|[+-]\d{2}:?\d{2})?\s*$",
    re.IGNORECASE,
)
_CLOCK = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$", re.IGNORECASE)
_OFFSET = re.compile(r"^([+-])(\d{2}):?(\d{2})$")
# Ordered: "afternoon" contains "noon", so it must be tried first.
_DAY_PARTS = {
    "afternoon": time(14, 0),
    "morning": time(9, 0),
    "evening": time(17, 0),
    "lunch": time(12, 30),
    "midday": time(12, 0),
    "noon": time(12, 0),
}


def resolve_timezone(name: str | None) -> tzinfo:
    """Turn an IANA zone name into a `tzinfo`, defaulting to UTC.

    Raises:
        ValueError: The name is not a known zone. Raised rather than defaulted,
            because a bot that silently fell back to UTC would offer a Karachi
            prospect meetings at four in the morning.
    """
    if not name or name.strip().upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"{name!r} is not a known timezone. Use an IANA name such as Asia/Karachi, "
            f"Europe/London or America/New_York."
        ) from exc


def parse_day(text: str | None) -> date | None:
    """Read a `YYYY-MM-DD` day, or None for anything else.

    A full timestamp is accepted too and reduced to its day, because a model
    that was just handed a slot's `start` will sometimes echo it back whole.
    """
    if not text:
        return None
    match = _DAY.match(text)
    if match:
        return _date_or_none(*(int(part) for part in match.groups()))
    when = parse_when(text, UTC)
    return when.date() if when else None


def parse_when(text: str | None, tz: tzinfo) -> datetime | None:
    """Read an ISO 8601 moment, or None for anything else.

    A naive value is placed in `tz`. An explicit offset or `Z` is honoured and
    the result is converted into `tz`, so callers compare like with like.
    """
    if not text:
        return None
    match = _WHEN.match(text)
    if not match:
        return None
    year, month, day, hour, minute, second, offset = match.groups()
    the_day = _date_or_none(int(year), int(month), int(day))
    if the_day is None:
        return None
    clock = _time_or_none(int(hour), int(minute), int(second or 0))
    if clock is None:
        return None

    naive = datetime.combine(the_day, clock)
    if not offset:
        return naive.replace(tzinfo=tz)
    if offset.upper() == "Z":
        return naive.replace(tzinfo=UTC).astimezone(tz)
    parsed = _OFFSET.match(offset)
    if parsed is None:
        return None
    sign, hours, minutes = parsed.groups()
    from datetime import timedelta, timezone

    delta = timedelta(hours=int(hours), minutes=int(minutes))
    fixed = timezone(delta if sign == "+" else -delta)
    return naive.replace(tzinfo=fixed).astimezone(tz)


def parse_clock(text: str | None) -> time | None:
    """Read a time of day — `10:00`, `14:30`, `2pm`, `9 am` — or None.

    Looser than `parse_when` on purpose: this is a *preference* the prospect
    expressed ("sometime in the afternoon" becomes `14:00` in the model's
    hands), and a preference that fails to parse costs nothing but the sorting.
    """
    if not text:
        return None
    # A part of the day is a preference too, and the model passes these through
    # from what the prospect said. Rough anchors, used only for sorting.
    word = text.strip().lower()
    for key, anchor in _DAY_PARTS.items():
        if key in word:
            return anchor
    match = _CLOCK.match(text)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour_value = int(hour)
    if meridiem:
        marker = meridiem.lower().replace(".", "")
        if hour_value == 12:
            hour_value = 0
        if marker == "pm":
            hour_value += 12
    return _time_or_none(hour_value, int(minute or 0), 0)


def label(moment: datetime) -> str:
    """A moment as a person would read it out: `Tuesday 8 September at 10:00`.

    Year included only when it is not this year, because a cold call rarely
    books eighteen months out and saying the year every time sounds like a
    machine.
    """
    text = f"{moment:%A} {moment.day} {moment:%B}"
    if moment.year != datetime.now(moment.tzinfo).year:
        text += f" {moment.year}"
    return f"{text} at {moment:%H:%M}"


def label_day(day: date) -> str:
    """A day as a person would say it: `Tuesday 8 September`."""
    return f"{day:%A} {day.day} {day:%B}"


def describe_now(now: datetime, tz: tzinfo) -> str:
    """The sentence that tells the model what time it is.

    Includes the weekday, because "next Tuesday" cannot be resolved without it,
    and the zone name, because the tools want times in that zone.
    """
    local = now.astimezone(tz)
    zone = getattr(tz, "key", None) or ("UTC" if tz is UTC else str(tz))
    return f"{local:%A} {local.day} {local:%B} {local.year}, {local:%H:%M} in {zone}"


def _date_or_none(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _time_or_none(hour: int, minute: int, second: int) -> time | None:
    try:
        return time(hour, minute, second)
    except ValueError:
        return None
