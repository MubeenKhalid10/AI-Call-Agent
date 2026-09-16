"""Business hours: which times of which days a meeting may be offered.

Pure, and imported by `config.py` for validation, so it must not import config
or anything that does. The parsing is strict for the same reason phone numbers
are: an hours string that is silently misread offers meetings at midnight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HOURS = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*$")


@dataclass(frozen=True)
class BusinessHours:
    """When meetings may be offered, in the calendar's own timezone.

    Attributes:
        opens: First slot may start at or after this.
        closes: Every slot must end at or before this.
        days: Weekdays meetings may be offered on, Monday=0 .. Sunday=6.
    """

    opens: time
    closes: time
    days: frozenset[int]

    @classmethod
    def parse(cls, hours: str, days: str) -> BusinessHours:
        """Read `"09:00-17:00"` and `"mon-fri"` (or `"mon,wed,fri"`).

        Raises:
            ValueError: Either string is not in that form, or closes before opens.
        """
        match = _HOURS.match(hours or "")
        if not match:
            raise ValueError(
                f"business hours must be written like 09:00-17:00; got {hours!r}"
            )
        open_h, open_m, close_h, close_m = match.groups()
        try:
            opens = time(int(open_h), int(open_m or 0))
            closes = time(int(close_h), int(close_m or 0)) if int(close_h) < 24 else time(23, 59)
        except ValueError as exc:
            raise ValueError(f"business hours {hours!r} contain an impossible time") from exc
        if closes <= opens:
            raise ValueError(f"business hours {hours!r} close before they open")

        return cls(opens=opens, closes=closes, days=frozenset(_parse_days(days)))

    def is_open_on(self, weekday: int) -> bool:
        """Whether meetings may be offered on this weekday (Monday=0)."""
        return weekday in self.days

    def describe(self) -> str:
        """One line for the startup log: `09:00-17:00 mon-fri`."""
        return f"{self.opens:%H:%M}-{self.closes:%H:%M} {_describe_days(self.days)}"


def _parse_days(text: str) -> list[int]:
    cleaned = (text or "").strip().lower()
    if not cleaned:
        raise ValueError("business days must name at least one day, e.g. mon-fri")
    result: list[int] = []
    for part in re.split(r"[,\s]+", cleaned):
        if not part:
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            start, end = _day_index(first), _day_index(last)
            span = list(range(start, end + 1)) if start <= end else list(range(start, 7)) + list(range(0, end + 1))
            result.extend(span)
        else:
            result.append(_day_index(part))
    if not result:
        raise ValueError(f"business days {text!r} name no day")
    return sorted(set(result))


def _day_index(name: str) -> int:
    key = name.strip()[:3].lower()
    if key not in _DAY_NAMES:
        raise ValueError(f"{name!r} is not a day; use mon, tue, wed, thu, fri, sat or sun")
    return _DAY_NAMES.index(key)


def _describe_days(days: frozenset[int]) -> str:
    ordered = sorted(days)
    if ordered == list(range(ordered[0], ordered[-1] + 1)) and len(ordered) > 2:
        return f"{_DAY_NAMES[ordered[0]]}-{_DAY_NAMES[ordered[-1]]}"
    return ",".join(_DAY_NAMES[d] for d in ordered)
