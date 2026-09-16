"""The backend behind the tools: validation, authorization, and the actual I/O.

This is the "backend tool handler → validation / authorization → database /
external API" part of the Phase 7 architecture, implementing the
`ActionBackend` Protocol the conversation layer defines. It is the one module
that knows four worlds at once — the campaign store, the calendar provider, the
knowledge retriever and the telephony provider — which is exactly why it is its
own package rather than a method on any of them, in the same way `dialer.py`
knows campaigns and carriers and `briefing.py` knows campaigns and conversations.

**What it decides, and what it does not.** It decides whether an action is
*possible and valid*: is there a calendar, is the day inside the horizon, is
the callback in the future, is this a phone call with a transfer destination,
did the carrier accept. It does not decide whether the action is *appropriate
for where the call is* — that a meeting must not be booked after a "no", that a
transfer must not follow a do-not-call — because that depends on the
conversation's state, and the conversation applies it before asking. Two layers
of validation, each about the thing it can see.

**It never raises.** Every method returns an `ActionOutcome`, and every external
call is wrapped so that a calendar API timing out or a database going away
becomes `ok=False` with a code and a message. There is a person on the line;
the conversation turns the failure into a sentence, and the tool log records
it. The one thing that would be worse than an action failing is an action
failing *silently*, so every failure is logged here as well as reported.

**Success is reported only after the durable write.** A local-calendar booking
*is* the row in the meetings table, so if that insert fails the booking did not
happen and the outcome says so. A Cal.com booking exists in Cal.com the moment
it returns, so a failed local mirror is logged loudly and the booking is still
reported — the person would otherwise be told a meeting they are about to
receive an invitation for does not exist.
"""

from __future__ import annotations

import time as time_module
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..campaigns.store import MeetingConflictError
from ..conversation import (
    ActionOutcome,
    AttendeeDetails,
    CallBrief,
    Capabilities,
)
from ..conversation.results import (
    EMAIL_REQUIRED,
    EXTERNAL_ERROR,
    NO_PROSPECT,
    PAST_TIME,
    SLOT_TAKEN,
    TOO_FAR_AHEAD,
    TRANSFER_FAILED,
    TRANSFER_UNAVAILABLE,
    UNAVAILABLE,
)
from ..conversation.timeparse import label
from ..monitoring.instruments import (
    CALENDAR_OPERATIONS,
    CALLBACK_OPERATIONS,
    KNOWLEDGE_SEARCHES,
    TOOL_LATENCY,
    TRANSFER_OPERATIONS,
    outcome_of,
)
from ..reliability.retry import guarded
from ..scheduling import (
    Attendee,
    CalendarError,
    CalendarProvider,
    CalendarUnavailableError,
    Slot,
    SlotUnavailableError,
)
from ..telephony import TelephonyError, TelephonyProvider, TransferError

if TYPE_CHECKING:
    from ..campaigns.store import CampaignStore
    from ..retrieval import KnowledgeRetriever
    from ..telephony.session import CallSession

# The most slots handed to the model at once. It is told to offer two; giving it
# six lets it pick around a stated preference without reading a whole diary.
MAX_OFFERED_SLOTS = 6


class ActionService:
    """The real `ActionBackend`: acts on the world, and says truthfully whether it did."""

    def __init__(
        self,
        *,
        brief: CallBrief,
        tz: tzinfo,
        timezone_name: str,
        store: CampaignStore | None = None,
        calendar: CalendarProvider | None = None,
        knowledge: KnowledgeRetriever | None = None,
        telephony: TelephonyProvider | None = None,
        call: CallSession | None = None,
        transfer_number: str | None = None,
        caller_id: str | None = None,
        calendar_max_days_ahead: int = 30,
        callback_max_days_ahead: int = 60,
        now: Callable[[], datetime] | None = None,
        owns_calendar: bool = True,
        owns_telephony: bool = True,
        transfer_action_url: str | None = None,
        transfer_timeout_secs: int = 30,
    ) -> None:
        """Create the service for one session.

        Every dependency is optional, and each absent one switches a capability
        off rather than failing later: no store means no callbacks and no
        meeting records; no calendar means no booking; no knowledge means no
        search; no telephony, no call or no destination means no transfer.

        Args:
            brief: Whose call this is. Supplies the prospect, campaign and
                attempt ids the writes are keyed by.
            tz / timezone_name: The session's zone, as a `tzinfo` and as the
                IANA name the model is told.
            store: The campaign store, shared with the briefing. Never closed
                here.
            calendar: The calendar provider, or None.
            knowledge: The retrieval stage, reused as the search path.
            telephony: A provider built with credentials, for transfers.
            call: The phone call this session is, or None for a browser or eval
                session — on which a transfer is impossible by definition.
            transfer_number: Where transfers go.
            caller_id: The number to present to the colleague on a transfer.
            calendar_max_days_ahead / callback_max_days_ahead: The horizons.
            now: The clock, for tests.
            owns_calendar / owns_telephony: Whether `close` should close them.
            transfer_action_url: Phase 16. Where the carrier reports how a
                transfer's colleague leg ended — the webhook receiver. None
                keeps the fallback inline and records no outcome.
            transfer_timeout_secs: How long a transfer rings the colleague.
        """
        self._brief = brief
        self._transfer_action_url = transfer_action_url
        self._transfer_timeout_secs = max(5, int(transfer_timeout_secs))
        self._tz = tz
        self._timezone_name = timezone_name
        self._store = store
        self._calendar = calendar
        self._knowledge = knowledge
        self._telephony = telephony
        self._call = call
        self._transfer_number = transfer_number
        self._caller_id = caller_id
        self._calendar_horizon = timedelta(days=calendar_max_days_ahead)
        self._callback_horizon = timedelta(days=callback_max_days_ahead)
        self._clock = now or (lambda: datetime.now(UTC))
        self._owns_calendar = owns_calendar
        self._owns_telephony = owns_telephony
        self._transferred = False

        can_transfer = bool(
            telephony is not None
            and call is not None
            and call.call_id
            and transfer_number
        )
        self._capabilities = Capabilities(
            can_search_knowledge=knowledge is not None,
            can_check_calendar=calendar is not None,
            can_book_meeting=calendar is not None,
            booking_requires_email=bool(calendar is not None and calendar.requires_email),
            can_schedule_callback=store is not None and brief.prospect_id is not None,
            can_transfer=can_transfer,
            timezone=timezone_name,
        )

    @property
    def capabilities(self) -> Capabilities:
        """What this session can do. Fixed at construction."""
        return self._capabilities

    def describe(self) -> str:
        """One line for the log at the start of the session."""
        parts = [self._capabilities.describe()]
        if self._calendar is not None:
            parts.append(f"calendar={self._calendar.describe()}")
        if self._capabilities.can_transfer and self._transfer_number:
            parts.append(f"transfer={_mask(self._transfer_number)}")
        return " | ".join(parts)

    # --- Knowledge -------------------------------------------------------------

    # Phase 22: every tool's outcome and duration, by operation. The public
    # methods below are the `ActionBackend` Protocol and keep their names;
    # each wraps its own body so the count is one line per tool, and a
    # backend that raises is still counted (as its exception's class).

    async def search_knowledge(self, query: str) -> ActionOutcome:
        """Search through the existing retriever. The tool is a second door, not a second path."""
        return await _observed("search", KNOWLEDGE_SEARCHES, lambda: self._search_knowledge(query))

    async def check_availability(self, day: date, preferred_time: time | None) -> ActionOutcome:
        """Free slots on `day`, sorted towards `preferred_time` when one was given."""
        return await _observed("check", CALENDAR_OPERATIONS, lambda: self._check_availability(day, preferred_time), operation="check")

    async def book_meeting(
        self, start: datetime, attendee: AttendeeDetails, *, notes: str = ""
    ) -> ActionOutcome:
        """Book the slot, then record it. Reports success only when both are true — see the module docstring."""
        return await _observed("book", CALENDAR_OPERATIONS, lambda: self._book_meeting(start, attendee, notes=notes), operation="book")

    async def schedule_callback(self, when: datetime, *, note: str = "") -> ActionOutcome:
        """Create the prospect's pending callback, or move it."""
        return await _observed("schedule", CALLBACK_OPERATIONS, lambda: self._schedule_callback(when, note=note), operation="schedule")

    async def transfer_to_human(self, reason: str) -> ActionOutcome:
        """Hand the live call to the configured destination."""
        return await _observed("transfer", TRANSFER_OPERATIONS, lambda: self._transfer_to_human(reason))

    async def _search_knowledge(self, query: str) -> ActionOutcome:
        if self._knowledge is None:
            return ActionOutcome.failure(UNAVAILABLE, "no knowledge base is available on this session")
        try:
            matches = await self._knowledge.search(query)
        except Exception as exc:  # noqa: BLE001 - reported, never raised mid-call
            logger.error(f"ACTION | knowledge search failed: {exc}")
            return ActionOutcome.failure(EXTERNAL_ERROR, f"the knowledge base could not be searched: {exc}")

        if matches is None:
            return ActionOutcome.success(found=False, passages=[], note="the knowledge base is empty")
        passages = [
            {"title": match.title, "content": match.content, "score": round(match.score, 2)}
            for match in matches
        ]
        return ActionOutcome.success(found=bool(passages), passages=passages)

    # --- Calendar ----------------------------------------------------------------

    async def _check_availability(self, day: date, preferred_time: time | None) -> ActionOutcome:
        if self._calendar is None:
            return ActionOutcome.failure(UNAVAILABLE, "no calendar is configured on this session")

        today = self._now().date()
        if day < today:
            return ActionOutcome.failure(PAST_TIME, f"{day.isoformat()} has already passed")
        if day - today > self._calendar_horizon:
            return ActionOutcome.failure(
                TOO_FAR_AHEAD,
                f"{day.isoformat()} is more than {self._calendar_horizon.days} days ahead, which is as "
                f"far as meetings can be booked",
            )

        start = datetime.combine(day, time(0, 0), tzinfo=self._tz)
        end = start + timedelta(days=1)
        try:
            slots = await self._calendar.available_slots(start, end)
        except CalendarUnavailableError as exc:
            logger.error(f"ACTION | calendar unreachable: {exc}")
            return ActionOutcome.failure(EXTERNAL_ERROR, f"the calendar could not be reached: {exc}")
        except CalendarError as exc:
            logger.error(f"ACTION | calendar refused an availability check: {exc}")
            return ActionOutcome.failure(EXTERNAL_ERROR, f"the calendar refused the request: {exc}")
        except Exception as exc:  # noqa: BLE001 - a provider bug must not take the call down
            logger.exception("ACTION | calendar provider raised on availability")
            return ActionOutcome.failure(EXTERNAL_ERROR, f"the calendar failed: {exc.__class__.__name__}")

        ordered = _order_slots(slots, preferred_time, self._tz)[:MAX_OFFERED_SLOTS]
        return ActionOutcome.success(
            slots=[slot.to_dict(self._tz) for slot in ordered],
            timezone=self._timezone_name,
            day=day.isoformat(),
            total_free=len(slots),
        )

    async def _book_meeting(
        self, start: datetime, attendee: AttendeeDetails, *, notes: str = ""
    ) -> ActionOutcome:
        if self._calendar is None:
            return ActionOutcome.failure(UNAVAILABLE, "no calendar is configured on this session")
        if start.tzinfo is None:
            start = start.replace(tzinfo=self._tz)
        if start <= self._now():
            return ActionOutcome.failure(PAST_TIME, f"{label(start.astimezone(self._tz))} has already passed")
        if start - self._now() > self._calendar_horizon:
            return ActionOutcome.failure(
                TOO_FAR_AHEAD,
                f"that is more than {self._calendar_horizon.days} days ahead, which is as far as meetings can be booked",
            )
        if self._calendar.requires_email and not attendee.email:
            return ActionOutcome.failure(
                EMAIL_REQUIRED, f"{self._calendar.name} needs the attendee's email address to book"
            )

        try:
            booking = await self._calendar.book(
                start,
                Attendee(
                    name=attendee.name,
                    email=attendee.email,
                    phone=attendee.phone,
                    timezone=self._timezone_name,
                ),
                notes=notes,
            )
        except SlotUnavailableError as exc:
            logger.warning(f"ACTION | slot not bookable: {exc}")
            return ActionOutcome.failure(SLOT_TAKEN, str(exc))
        except CalendarUnavailableError as exc:
            logger.error(f"ACTION | calendar unreachable during booking: {exc}")
            return ActionOutcome.failure(EXTERNAL_ERROR, f"the calendar could not be reached: {exc}")
        except CalendarError as exc:
            logger.error(f"ACTION | calendar refused the booking: {exc}")
            return ActionOutcome.failure(EXTERNAL_ERROR, f"the calendar refused the booking: {exc}")
        except Exception as exc:  # noqa: BLE001 - a provider bug must not take the call down
            logger.exception("ACTION | calendar provider raised on booking")
            return ActionOutcome.failure(EXTERNAL_ERROR, f"the calendar failed: {exc.__class__.__name__}")

        reference = booking.reference
        if self._store is not None:
            try:
                meeting = await self._store.add_meeting(
                    prospect_id=self._brief.prospect_id,
                    campaign_id=self._brief.campaign_id,
                    call_attempt_id=self._brief.call_attempt_id,
                    provider=booking.provider,
                    reference=booking.reference,
                    start_at=booking.start,
                    end_at=booking.end,
                    timezone=self._timezone_name,
                    attendee_name=attendee.name,
                    attendee_email=attendee.email,
                    notes=notes or None,
                )
                reference = reference or f"meeting-{meeting.id}"
            except MeetingConflictError as exc:
                # Phase 16: the diary's own constraint refused the overlap —
                # the slot was taken between the check and the write. The
                # same answer as the provider's check, decided where it
                # cannot be raced. A Cal.com mirror is outside the rule.
                logger.warning(f"ACTION | slot taken at the write: {exc}")
                return ActionOutcome.failure(SLOT_TAKEN, str(exc))
            except Exception as exc:  # noqa: BLE001 - see below for why this is not fatal for Cal.com
                if booking.reference is None:
                    # No external reference means the row *was* the booking.
                    # Nothing exists anywhere, so nothing was booked.
                    logger.error(f"ACTION | the meeting could not be recorded, so it is not booked: {exc}")
                    return ActionOutcome.failure(
                        EXTERNAL_ERROR, f"the meeting could not be saved: {exc}"
                    )
                logger.error(
                    f"ACTION | booking {booking.reference} exists in {booking.provider} but could not be "
                    f"mirrored locally: {exc}"
                )
        elif booking.reference is None:
            logger.error("ACTION | a local booking has nowhere to be recorded, so it is not booked")
            return ActionOutcome.failure(UNAVAILABLE, "there is no database to record the meeting in")

        local_start = booking.start.astimezone(self._tz)
        return ActionOutcome.success(
            start=local_start.strftime("%Y-%m-%dT%H:%M"),
            end=booking.end.astimezone(self._tz).strftime("%Y-%m-%dT%H:%M"),
            label=label(local_start),
            timezone=self._timezone_name,
            provider=booking.provider,
            reference=reference,
        )

    # --- Callbacks -------------------------------------------------------------

    async def _schedule_callback(self, when: datetime, *, note: str = "") -> ActionOutcome:
        if self._store is None:
            return ActionOutcome.failure(UNAVAILABLE, "there is no database to schedule a callback in")
        if self._brief.prospect_id is None:
            return ActionOutcome.failure(
                NO_PROSPECT, "this call has no prospect record, so a callback cannot be scheduled"
            )
        if when.tzinfo is None:
            when = when.replace(tzinfo=self._tz)
        now = self._now()
        if when <= now:
            return ActionOutcome.failure(PAST_TIME, f"{label(when.astimezone(self._tz))} has already passed")
        if when - now > self._callback_horizon:
            return ActionOutcome.failure(
                TOO_FAR_AHEAD,
                f"that is more than {self._callback_horizon.days} days ahead, which is as far as callbacks go",
            )

        membership_id = None
        if self._brief.campaign_id is not None:
            try:
                membership = await self._store.find_membership(
                    self._brief.campaign_id, self._brief.prospect_id
                )
                membership_id = membership.id if membership else None
            except Exception as exc:  # noqa: BLE001 - the membership is a nicety, the callback is the point
                logger.warning(f"ACTION | could not look up the membership for the callback: {exc}")

        try:
            callback = await self._store.schedule_callback(
                prospect_id=self._brief.prospect_id,
                scheduled_for=when,
                campaign_id=self._brief.campaign_id,
                call_attempt_id=self._brief.call_attempt_id,
                campaign_prospect_id=membership_id,
                note=note or None,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised mid-call
            logger.error(f"ACTION | the callback could not be saved: {exc}")
            return ActionOutcome.failure(EXTERNAL_ERROR, f"the callback could not be saved: {exc}")

        local = callback.scheduled_for.astimezone(self._tz)
        return ActionOutcome.success(
            scheduled_for=local.isoformat(timespec="minutes"),
            label=label(local),
            timezone=self._timezone_name,
            reference=f"callback-{callback.id}",
        )

    # --- Transfer ----------------------------------------------------------------

    async def _transfer_to_human(self, reason: str) -> ActionOutcome:
        if self._call is None:
            return ActionOutcome.failure(TRANSFER_UNAVAILABLE, "this session is not a phone call")
        if self._telephony is None:
            return ActionOutcome.failure(
                TRANSFER_UNAVAILABLE, "no carrier credentials are configured, so the call cannot be moved"
            )
        if not self._transfer_number:
            return ActionOutcome.failure(
                TRANSFER_UNAVAILABLE, "no transfer destination is configured (TELEPHONY_TRANSFER_NUMBER)"
            )
        if not self._call.call_id:
            return ActionOutcome.failure(TRANSFER_UNAVAILABLE, "the carrier's call id is not known")
        if self._transferred:
            return ActionOutcome.failure(TRANSFER_FAILED, "the call has already been transferred")

        try:
            await self._telephony.transfer_call(
                self._call.call_id,
                self._transfer_number,
                caller_id=self._caller_id,
                # Phase 16: the carrier reports how the colleague's leg ended
                # to the webhook receiver, which records it and tells the
                # caller what happens next. Without a receiver the fallback
                # stays inline and nothing is recorded — the Phase 7 shape.
                action_url=self._transfer_action_url,
                timeout_secs=self._transfer_timeout_secs,
            )
        except TransferError as exc:
            logger.warning(f"ACTION | transfer refused: {exc}")
            return ActionOutcome.failure(TRANSFER_FAILED, str(exc))
        except TelephonyError as exc:
            logger.error(f"ACTION | carrier unreachable for transfer: {exc}")
            return ActionOutcome.failure(EXTERNAL_ERROR, f"the carrier could not be reached: {exc}")
        except Exception as exc:  # noqa: BLE001 - a provider bug must not take the call down
            logger.exception("ACTION | telephony provider raised on transfer")
            return ActionOutcome.failure(EXTERNAL_ERROR, f"the transfer failed: {exc.__class__.__name__}")

        self._transferred = True
        # Phase 16: on record before the outcome can arrive. Best effort and
        # bounded — the carrier has already taken the call, and a database
        # that is slow must not hold the tool turn (or the person) any longer.
        await self._record_transfer(reason)
        return ActionOutcome.success(
            destination=_mask(self._transfer_number),
            reason=reason,
            ring_secs=self._transfer_timeout_secs,
            outcome_tracked=bool(self._transfer_action_url),
        )

    async def _record_transfer(self, reason: str) -> None:
        """Write the `REQUESTED` transfer row, if there is anywhere to write it."""
        if self._store is None or self._call is None or not self._call.call_id or self._telephony is None:
            return
        store, call, telephony, destination = self._store, self._call, self._telephony, self._transfer_number or ""

        async def write() -> None:
            await store.add_transfer(
                telephony_call_id=call.call_id or "",
                provider=telephony.name,
                to_number=destination,
                call_attempt_id=self._brief.call_attempt_id,
                prospect_id=self._brief.prospect_id,
                reason=reason or None,
            )
            logger.info(f"TRANSFER | requested to {_mask(destination)} recorded for call {call.call_id}")

        await guarded(write, default=None, name="record the transfer", timeout_secs=5.0)

    async def close(self) -> None:
        """Release the calendar and carrier clients this service owns. Idempotent."""
        calendar, self._calendar = self._calendar, None
        if calendar is not None and self._owns_calendar:
            try:
                await calendar.close()
            except Exception:  # noqa: BLE001 - never let cleanup mask the real ending
                logger.exception("ACTION | failed to close the calendar provider")
        telephony, self._telephony = self._telephony, None
        if telephony is not None and self._owns_telephony:
            try:
                await telephony.close()
            except Exception:  # noqa: BLE001
                logger.exception("ACTION | failed to close the telephony provider")

    # --- Internals ---------------------------------------------------------------

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(self._tz)


async def _observed(
    name: str, counter: Any, call: Callable[[], Awaitable[ActionOutcome]], *, operation: str | None = None
) -> ActionOutcome:
    """Run one tool body, count its outcome (`ok` or the error code) and time it. Phase 22.

    A body that raises — which none should; each catches its own provider —
    is counted under the exception's class and re-raised, so the
    conversation's own guard still turns it into a spoken answer.
    """
    started = time_module.monotonic()
    labels = {"operation": operation} if operation is not None else {}
    try:
        result = await call()
    except Exception as exc:
        counter.inc(outcome=exc.__class__.__name__, **labels)
        TOOL_LATENCY.observe(time_module.monotonic() - started, operation=name)
        raise
    counter.inc(outcome=outcome_of(result), **labels)
    TOOL_LATENCY.observe(time_module.monotonic() - started, operation=name)
    return result


def _order_slots(slots: list[Slot], preferred: time | None, tz: tzinfo) -> list[Slot]:
    """Earliest first, or nearest to a preferred time of day first."""
    if preferred is None:
        return sorted(slots, key=lambda slot: slot.start)
    target = preferred.hour * 60 + preferred.minute

    def distance(slot: Slot) -> tuple[int, datetime]:
        local = slot.start.astimezone(tz)
        return abs(local.hour * 60 + local.minute - target), slot.start

    return sorted(slots, key=distance)


def _mask(number: str) -> str:
    """A phone number safe for a log or a tool result: country code and the last three digits."""
    digits = number.strip()
    if len(digits) <= 6:
        return digits
    return f"{digits[:3]}…{digits[-3:]}"
