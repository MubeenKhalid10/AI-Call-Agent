"""The rules that stop a campaign doing something it should not. Phase 9.

Everything here answers one question — *may this call be placed, right now?* —
and each rule answers it about a different kind of harm:

* **The calling window.** Phoning a stranger at four in the morning is the
  worst thing this system can do by accident, and it is also the easiest to do:
  the queue has no clock, so a worker started at midnight would empty it by
  morning. The window is the *prospect's* local time where one can be worked
  out, not the server's — a Karachi list dialled from a European server is the
  case this exists for.
* **Pacing.** A minimum gap between placements. Not politeness: a carrier that
  is rate-limiting, or a bug that empties the queue in a loop, both look like
  "many calls very quickly", and a floor on the interval turns either into a
  slow problem rather than a fast one.
* **Concurrency.** How many calls may be live at once. One by default, because
  one bot per process is how this runs and two concurrent calls on a machine
  running Silero, Kokoro and Moonshine locally is not a throughput improvement.
* **Maximum call duration.** A call that never ends costs money for as long as
  it lasts. Enforced by the bot on itself (`supervisor.py`), because the bot is
  the only party that knows the call is still going.

**Every rule reports a reason, and no rule raises.** A refusal is a normal
event — the window is closed, the limit is reached — and the caller logs it and
moves on. `Decision` carries the reason and `retry_after` so a scheduler can be
told "not now, and here is when to ask again" rather than spinning.

Pure except for the clock: no database, no carrier, no config. That is what
makes the whole file testable against a fixed `now`.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..scheduling.hours import BusinessHours


@dataclass(frozen=True)
class Decision:
    """Whether something may go ahead, and why not.

    Attributes:
        allowed: Whether to proceed.
        reason: Why not, in a sentence a person reading a log needs. Empty when
            allowed.
        retry_after_secs: How long until asking again could give a different
            answer. `None` means "nothing here will change on a timer" — a
            do-not-call, an exhausted membership.
    """

    allowed: bool
    reason: str = ""
    retry_after_secs: float | None = None

    def __bool__(self) -> bool:
        """Truthy when the thing may go ahead.

        **The trap this creates, written down because it has been fallen into
        more than once.** A *refusal* is falsy, so `decision or None`,
        `if decision:` and `x if decision else y` all read a refusal as absence.
        Anywhere a decision is being passed around rather than acted on
        immediately, compare with `is None` — and prefer `Decision.refused`
        below, which cannot be got wrong.
        """
        return self.allowed

    @property
    def refused(self) -> bool:
        """Whether this decision says no. The safe way to ask, in any context."""
        return not self.allowed

    @classmethod
    def ok(cls) -> Decision:
        """A decision to proceed."""
        return cls(True)

    @classmethod
    def no(cls, reason: str, *, retry_after_secs: float | None = None) -> Decision:
        """A refusal, with its reason."""
        return cls(False, reason, retry_after_secs)


@dataclass(frozen=True)
class CallingWindow:
    """The hours during which a prospect may be phoned, in their own timezone.

    Attributes:
        hours: When calling is allowed, as `BusinessHours` — the same parser the
            calendar uses, so one syntax covers both.
        timezone: The zone to apply them in when the prospect's own is unknown.
        enabled: False allows any hour. Left as a setting rather than removed
            because a test call to your own phone at nine in the evening is a
            legitimate thing to want, and editing the hours to get it is worse.
        clock: Where "now" comes from when `check` is not given one. The real
            clock by default; injected by the scheduler's checks (Phase 13) so
            a whole worker run can be judged against a fixed Monday morning.
            Never a pipeline concern: the bot does not read this.
    """

    hours: BusinessHours
    timezone: str = "UTC"
    enabled: bool = True
    clock: Callable[[], datetime] | None = field(default=None, compare=False, repr=False)

    @classmethod
    def parse(
        cls,
        hours: str,
        days: str,
        timezone: str = "UTC",
        *,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> CallingWindow:
        """Build a window from the configured strings.

        Raises:
            ValueError: The hours, days or timezone cannot be read. Callers in
                `config.py` collect that into the startup problem list.
        """
        parsed = BusinessHours.parse(hours, days)
        resolve_zone(timezone)  # Raises here rather than on the first call.
        return cls(hours=parsed, timezone=timezone, enabled=enabled, clock=clock)

    def check(self, *, now: datetime | None = None, timezone: str | None = None) -> Decision:
        """Whether a call may be placed at `now`, in `timezone` or the default.

        Args:
            now: The moment to judge. Defaults to the real clock. Timezone-aware
                or it is read as UTC, because a naive datetime here would mean
                the answer depends on where the server is.
            timezone: The prospect's zone, when their record supplies one.

        Returns:
            A `Decision` whose `retry_after_secs` is the wait until the window
            next opens, so a scheduler can sleep exactly that long.
        """
        if not self.enabled:
            return Decision.ok()

        if now is None and self.clock is not None:
            now = self.clock()
        zone = resolve_zone(timezone or self.timezone)
        moment = _aware(now).astimezone(zone)
        local = moment.timetz()

        if self.hours.is_open_on(moment.weekday()) and self.hours.opens <= local.replace(tzinfo=None) < self.hours.closes:
            return Decision.ok()

        opens_at = self.next_open(moment, zone)
        wait = max(0.0, (opens_at - moment).total_seconds())
        return Decision.no(
            f"outside calling hours ({self.describe()}): it is "
            f"{moment:%a %H:%M} for them; the window opens {opens_at:%a %d %b %H:%M}",
            retry_after_secs=wait,
        )

    def next_open(self, moment: datetime, zone: tzinfo | None = None) -> datetime:
        """The next moment the window is open, at or after `moment`.

        Searches day by day for a week. A window with no open days would loop
        forever, which `BusinessHours.parse` already refuses to build.
        """
        zone = zone or resolve_zone(self.timezone)
        local = _aware(moment).astimezone(zone)
        if self.hours.is_open_on(local.weekday()) and local.time() < self.hours.opens:
            return local.replace(
                hour=self.hours.opens.hour, minute=self.hours.opens.minute, second=0, microsecond=0
            )
        for ahead in range(1, 8):
            day = (local + timedelta(days=ahead)).replace(
                hour=self.hours.opens.hour, minute=self.hours.opens.minute, second=0, microsecond=0
            )
            if self.hours.is_open_on(day.weekday()):
                return day
        return local  # Unreachable for a valid window; never loops.

    def describe(self) -> str:
        """One line for the startup log."""
        if not self.enabled:
            return "any hour (CALLING_HOURS_ENFORCED=false)"
        return f"{self.hours.describe()} {self.timezone}"


class PacingLimiter:
    """A floor on the interval between call placements.

    In-process and deliberately so: this stage runs one dialer at a time, and a
    shared limiter would mean a lock table or a broker, which the phase's own
    "no distributed infrastructure" rule rules out. The database-level
    protections — the reservation lock, the live-attempt check, the idempotency
    key — are what make *correctness* independent of this; pacing is about rate,
    and a second dialer would halve the interval rather than double a call.
    """

    def __init__(self, min_interval_secs: float, *, clock=_time.monotonic) -> None:
        """Create the limiter.

        Args:
            min_interval_secs: Minimum seconds between placements. 0 disables.
            clock: Monotonic clock, injected for tests.
        """
        self._interval = max(0.0, min_interval_secs)
        self._clock = clock
        self._last: float | None = None

    @property
    def enabled(self) -> bool:
        """Whether any pacing is applied."""
        return self._interval > 0

    @property
    def interval_secs(self) -> float:
        """The configured minimum interval, for the store's shared slot (Phase 21)."""
        return self._interval

    def check(self) -> Decision:
        """Whether enough time has passed since the last placement."""
        if not self.enabled or self._last is None:
            return Decision.ok()
        waited = self._clock() - self._last
        if waited >= self._interval:
            return Decision.ok()
        remaining = self._interval - waited
        return Decision.no(
            f"pacing: {remaining:.1f}s until the next call may be placed "
            f"(one every {self._interval:g}s)",
            retry_after_secs=remaining,
        )

    def record_placement(self) -> None:
        """Note that a call has just been placed. Call this only on a real placement."""
        self._last = self._clock()

    def describe(self) -> str:
        """One line for the startup log."""
        return f"one call every {self._interval:g}s" if self.enabled else "unpaced"


def check_concurrency(live: int, limit: int) -> Decision:
    """Whether another call may be placed alongside `live` already running.

    A function rather than an object because the count is a database query and
    the rule is one comparison; keeping the rule here means the SQL and the
    policy are not written in the same place.
    """
    if limit <= 0:
        return Decision.ok()
    if live < limit:
        return Decision.ok()
    return Decision.no(
        f"concurrency limit reached: {live} call(s) live, limit {limit}",
        # No fixed answer: it changes when a call ends, and the caller polls.
        retry_after_secs=15.0,
    )


def check_duration(elapsed_secs: float, limit_secs: float) -> Decision:
    """Whether a call that has lasted `elapsed_secs` may continue.

    The bot's own hard stop. Separate from the session idle timeout, which ends
    a call where *nothing* is happening: this ends one where too much is.
    """
    if limit_secs <= 0:
        return Decision.ok()
    if elapsed_secs < limit_secs:
        return Decision.ok()
    return Decision.no(
        f"maximum call duration reached ({elapsed_secs:.0f}s of {limit_secs:.0f}s)"
    )


@dataclass
class CampaignGuards:
    """Every "may this call go out now" rule, asked in one place.

    Pure: the caller supplies the live-call count (a database query) and the
    prospect's timezone (a column), so the rules stay testable against fixed
    inputs and the SQL stays in the store.

    The order the checks run in is the order a person would want to be told
    about them: the window first, because it is the one that means "not for
    hours"; then concurrency, then pacing, which mean "not for seconds".
    """

    window: CallingWindow
    pacing: PacingLimiter
    max_concurrent: int = 1

    def check(
        self, *, live_calls: int, now: datetime | None = None, prospect_timezone: str | None = None
    ) -> Decision:
        """Whether a call may be placed right now.

        Args:
            live_calls: How many calls are live, from the store.
            now: The moment to judge. Defaults to the real clock.
            prospect_timezone: The prospect's own zone, when their record has
                one. Never inferred from their phone number — a country code is
                not a timezone, and guessing one puts the call at the wrong hour
                with no way to tell.
        """
        window = self.window.check(now=now, timezone=prospect_timezone)
        if not window:
            return window
        concurrency = check_concurrency(live_calls, self.max_concurrent)
        if not concurrency:
            return concurrency
        return self.pacing.check()

    def record_placement(self) -> None:
        """Note that a call was placed, for pacing."""
        self.pacing.record_placement()

    def describe(self) -> str:
        """One line for the startup log."""
        return (
            f"calling hours {self.window.describe()}, "
            f"max {self.max_concurrent} concurrent, {self.pacing.describe()}"
        )


def prospect_timezone(custom_data: dict[str, Any] | None) -> str | None:
    """A prospect's own timezone, if their imported record supplied one.

    Read from the free-form `custom_data` the CSV importer keeps unrecognised
    columns in, under any of the obvious spellings. Deliberately the *only*
    source: a timezone can be imported alongside the number, but it is never
    derived from the number, because a country code does not determine one —
    Pakistan is a single zone and the United States is six, so a derivation
    that looked right for one list would put the other list's calls at
    breakfast.

    Returns:
        The zone name, or None to use the configured default. An unreadable
        value is None too: a bad zone must not silently become UTC on a row
        that was trying to say something else.
    """
    if not custom_data:
        return None
    for key in ("timezone", "time_zone", "tz", "iana_timezone"):
        value = custom_data.get(key)
        if isinstance(value, str) and value.strip():
            try:
                resolve_zone(value)
            except ValueError:
                return None
            return value.strip()
    return None


def resolve_zone(name: str | None) -> tzinfo:
    """Read an IANA timezone name, or raise `ValueError` naming what was wrong.

    `ZoneInfo` raises three different exception types for the ways a zone name
    can be wrong; this narrows them to one so callers have a single thing to
    catch.
    """
    cleaned = (name or "UTC").strip()
    if not cleaned or cleaned.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(
            f"{cleaned!r} is not a known timezone. Use an IANA name such as "
            f"Asia/Karachi, Europe/London or America/New_York."
        ) from exc


def _aware(moment: datetime | None) -> datetime:
    """The moment, or now; a naive one is read as UTC rather than as local time."""
    if moment is None:
        return datetime.now(UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


__all__ = [
    "CallingWindow",
    "CampaignGuards",
    "Decision",
    "PacingLimiter",
    "check_concurrency",
    "check_duration",
    "prospect_timezone",
    "resolve_zone",
]
