"""The nouns of an outbound campaign, and the states they move through.

Four entities, and the boundaries between them are the whole design:

* **Prospect** — a person, and the phone number that reaches them. Owned by
  nobody in particular, reusable across any number of campaigns, and carrying no
  campaign-specific state at all. If a field would be different depending on
  which campaign you asked about, it does not belong here.
* **Campaign** — a batch of calling work with a name and a lifecycle.
* **CampaignProspect** — one prospect's membership of one campaign, and *this is
  where per-campaign state lives*: how many times we have tried them for this
  campaign, when to try again, whether this campaign is done with them. The same
  prospect in two campaigns has two of these and one Prospect row.
* **CallAttempt** — one actual dial. A prospect is not a call attempt; a
  prospect has many. This is the row that carries the carrier's call id, so a
  log line from the telephony provider and a row in this database can be tied
  together afterwards.

**On statuses.** Each of the four has its own, and they are deliberately not
shared: a call attempt can be `BUSY`, which is not a thing a campaign or a
prospect can be. The one status that crosses all of them is `DO_NOT_CALL`, which
lives on the prospect because it is a fact about the person and must outlive any
campaign that discovered it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ProspectStatus(StrEnum):
    """What we know about a person, independent of any campaign."""

    NEW = "NEW"
    """Imported and never called."""

    CONTACTED = "CONTACTED"
    """We have reached them at least once, on some campaign."""

    DO_NOT_CALL = "DO_NOT_CALL"
    """Never dial this person again, on any campaign, ever.

    The strongest state in the system. It is checked in the queue's SQL, checked
    again before a call is placed, and it is a property of the *person* rather
    than of a campaign membership precisely so that removing them from one
    campaign cannot lose it.
    """

    UNREACHABLE = "UNREACHABLE"
    """Their number could not be normalised, so there is nothing to dial.

    Kept rather than dropped: the row is still the record of a real import, and
    a corrected number can be filled in later.
    """


class CampaignStatus(StrEnum):
    """Where a campaign is in its life."""

    DRAFT = "DRAFT"
    """Being built. Prospects can be added; nothing will be dialled."""

    ACTIVE = "ACTIVE"
    """The only status the queue will hand out work for."""

    PAUSED = "PAUSED"
    """Temporarily stopped. Memberships and attempt counts are untouched."""

    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"

    @property
    def is_dialable(self) -> bool:
        """Whether the queue may hand out calls for a campaign in this state."""
        return self is CampaignStatus.ACTIVE


class MembershipStatus(StrEnum):
    """One prospect's progress through one campaign."""

    PENDING = "PENDING"
    """Waiting to be called, or waiting for its retry window."""

    IN_PROGRESS = "IN_PROGRESS"
    """A call attempt for this membership is live right now.

    This is what stops the same person being dialled twice at once, and it is
    set in the same transaction that reserves the work.
    """

    COMPLETED = "COMPLETED"
    """We reached them; this campaign is finished with them."""

    EXHAUSTED = "EXHAUSTED"
    """Out of attempts without reaching them."""

    SKIPPED = "SKIPPED"
    """Not callable — do-not-call, or no usable number."""

    @property
    def is_open(self) -> bool:
        """Whether the queue may hand this membership out as new work.

        Not simply the opposite of `is_closed`. A membership being dialled right
        now is neither: not open, because the queue must not hand it out a
        second time, and not closed, because the campaign is not finished with
        the person. The two properties answer different questions and both have
        a caller.
        """
        return self is MembershipStatus.PENDING

    @property
    def is_closed(self) -> bool:
        """Whether this campaign is finished with this prospect for good.

        The question a safety check asks, as opposed to the one the queue asks.
        A call that has already been reserved is `IN_PROGRESS`, which is not
        closed — so re-checking it before dialling does not refuse the very
        call being placed. Whether somebody is on *another* call is a separate
        check against live attempts.
        """
        return self in _CLOSED_MEMBERSHIPS


_CLOSED_MEMBERSHIPS = frozenset(
    {
        MembershipStatus.COMPLETED,
        MembershipStatus.EXHAUSTED,
        MembershipStatus.SKIPPED,
    }
)


class CallAttemptStatus(StrEnum):
    """What happened on one dial.

    The unhappy endings are kept apart rather than flattened into one failure,
    for the same reason `CallStatus` in `src/telephony/` keeps them apart: what
    the campaign should do next differs. `BUSY` and `NO_ANSWER` are worth
    retrying; `FAILED` usually means the number is wrong; `DO_NOT_CALL` must
    stop everything.
    """

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    CALLING = "CALLING"
    CONNECTED = "CONNECTED"
    UNRESOLVED = "UNRESOLVED"
    """We asked the carrier to dial and never got a clear answer. Phase 9.

    The state of network ambiguity: the request timed out, or the connection
    dropped mid-request, so the carrier may or may not have created a call. It
    is deliberately **live** rather than final, and that is the whole point —
    a live attempt blocks the prospect from being dialled again, so the
    ambiguity costs at most one uncalled prospect instead of one person's phone
    ringing twice. `reliability/recovery.py` resolves it by asking the carrier
    what exists, never by dialling again.
    """

    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    FAILED = "FAILED"
    VOICEMAIL = "VOICEMAIL"
    """An answering machine picked up. Phase 12.

    Final, and *not* a reached person: the carrier connected the call and
    a recording spoke, which is a no-answer with a different sound. Retried
    like one — the person may pick up next time — and kept apart from
    `NO_ANSWER` because a campaign whose every attempt hits voicemail is
    telling you something about the calling hours, not the numbers.
    """

    COMPLETED = "COMPLETED"
    CALLBACK_REQUESTED = "CALLBACK_REQUESTED"
    NOT_INTERESTED = "NOT_INTERESTED"
    DO_NOT_CALL = "DO_NOT_CALL"

    @property
    def is_live(self) -> bool:
        """Whether this attempt is still on the phone, or might be.

        A prospect with a live attempt is not eligible for another one — the
        check that stops us calling somebody twice at the same moment. Includes
        `UNRESOLVED`, because "might be on a call" has to block for the same
        reason "is on a call" does.
        """
        return self in _LIVE_ATTEMPTS

    @property
    def is_recoverable(self) -> bool:
        """Whether a restart should reconcile this attempt against the carrier.

        Phase 9. Every live status qualifies: the process that was watching a
        `QUEUED` or `CONNECTED` attempt is gone, so nothing is watching it now,
        and its row would otherwise stay live forever and block the prospect.
        """
        return self.is_live

    @property
    def is_final(self) -> bool:
        """Whether the attempt is over."""
        return not self.is_live

    @property
    def reached_person(self) -> bool:
        """Whether somebody actually picked up."""
        return self in _REACHED

    @property
    def should_retry(self) -> bool:
        """Whether this outcome is worth trying again later.

        Nobody was there, and nothing about the number or the person says not to
        call back. `FAILED` is excluded because it usually means the number
        itself is wrong, and retrying a wrong number just spends money.
        `VOICEMAIL` (Phase 12) is included: a machine answered this time and a
        person may answer next time.
        """
        return self in (
            CallAttemptStatus.NO_ANSWER,
            CallAttemptStatus.BUSY,
            CallAttemptStatus.VOICEMAIL,
        )


_LIVE_ATTEMPTS = frozenset(
    {
        CallAttemptStatus.PENDING,
        CallAttemptStatus.QUEUED,
        CallAttemptStatus.CALLING,
        CallAttemptStatus.CONNECTED,
        CallAttemptStatus.UNRESOLVED,
    }
)

#: The live statuses as a SQL literal list, so the queue's `NOT EXISTS`, the
#: partial index over live attempts and `has_live_attempt` cannot drift apart.
#: One definition; `store.py` interpolates it and never spells the list itself.
LIVE_STATUS_SQL = ", ".join(f"'{status.value}'" for status in sorted(_LIVE_ATTEMPTS))

#: The final statuses as a SQL literal list, for the `ended_at` stamp in
#: `store.py`. Phase 12 added `VOICEMAIL` and moved the list here so the two
#: writers that stamp `ended_at` cannot disagree with `is_final`.
FINAL_STATUS_SQL = ", ".join(
    f"'{status.value}'" for status in sorted(CallAttemptStatus) if status not in _LIVE_ATTEMPTS
)

#: How far a status may move. Phase 9: applying a carrier event is *monotonic*,
#: so a duplicate webhook, a poll that races it, or an out-of-order delivery
#: cannot walk a finished call backwards into ringing. Absent from the map means
#: "final": nothing may follow it.
_STATUS_RANK = {
    CallAttemptStatus.PENDING: 0,
    CallAttemptStatus.UNRESOLVED: 1,
    CallAttemptStatus.QUEUED: 2,
    CallAttemptStatus.CALLING: 3,
    CallAttemptStatus.CONNECTED: 4,
}


def may_advance(current: CallAttemptStatus, proposed: CallAttemptStatus) -> bool:
    """Whether a carrier event moving `current` to `proposed` should be applied.

    Phase 9, and the rule that makes status updates idempotent:

    * a final status is never overwritten — the call is over, and a late or
      duplicated event about it says nothing new;
    * a live status only moves forward, or to any final status, so `ringing`
      arriving after `answered` is dropped;
    * the same status applied twice is not an advance, so re-delivering an
      event changes nothing.

    The three conversation outcomes (`CALLBACK_REQUESTED`, `NOT_INTERESTED`,
    `DO_NOT_CALL`) are final, which is what already stopped `dialer.refresh`
    flattening them to `COMPLETED`; this generalises that rule to every writer
    rather than leaving it as three lines in one of them.
    """
    if current is proposed:
        return False
    if current.is_final:
        return False
    if proposed.is_final:
        return True
    return _STATUS_RANK.get(proposed, -1) > _STATUS_RANK.get(current, -1)

_REACHED = frozenset(
    {
        CallAttemptStatus.CONNECTED,
        CallAttemptStatus.COMPLETED,
        CallAttemptStatus.CALLBACK_REQUESTED,
        CallAttemptStatus.NOT_INTERESTED,
        CallAttemptStatus.DO_NOT_CALL,
    }
)

#: The statuses that mean somebody picked up, as a SQL literal list. Phase 10's
#: "answered calls" counts these, and it is the same set `reached_person`
#: returns True for — so a dashboard number and a Python check cannot disagree
#: about what "answered" means. Defined after `_REACHED`, not beside
#: `LIVE_STATUS_SQL`, because it reads it.
REACHED_STATUS_SQL = ", ".join(f"'{status.value}'" for status in sorted(_REACHED))


@dataclass(frozen=True)
class Prospect:
    """A person we might call.

    Attributes:
        phone: Exactly what was imported, never rewritten. Kept because a
            normalisation that went wrong is only diagnosable against the
            original, and because it is what the customer's own records say.
        phone_normalized: E.164, or `None` when the number could not be
            normalised with confidence. `None` means "do not dial this" — the
            queue requires a normalised number, so a bad number can never be
            turned into a call to a wrong person.
        custom_data: Whatever else the CSV had. Columns nobody anticipated land
            here rather than being dropped or forcing a migration.
    """

    id: int
    first_name: str
    last_name: str
    phone: str
    phone_normalized: str | None = None
    email: str | None = None
    company: str | None = None
    job_title: str | None = None
    industry: str | None = None
    location: str | None = None
    website: str | None = None
    custom_data: dict[str, Any] = field(default_factory=dict)
    status: ProspectStatus = ProspectStatus.NEW
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def full_name(self) -> str:
        """First and last name, for a log line."""
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_callable(self) -> bool:
        """Whether this person may be dialled at all.

        Deliberately narrow: it answers only "is there a number and are we
        allowed to use it". Whether a *campaign* should call them right now is a
        separate question with more inputs, and lives in `service.py`.
        """
        return self.status is not ProspectStatus.DO_NOT_CALL and bool(self.phone_normalized)


@dataclass(frozen=True)
class Campaign:
    """A batch of calling work."""

    id: int
    name: str
    description: str | None = None
    status: CampaignStatus = CampaignStatus.DRAFT
    configuration: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class CampaignProspect:
    """One prospect's membership of one campaign.

    Attributes:
        attempt_count: Dials made *for this campaign*. A prospect in two
            campaigns has an independent count in each, which is the point of
            this table existing.
        next_attempt_at: Earliest time the queue may hand this out again. `None`
            means "no wait".
    """

    id: int
    campaign_id: int
    prospect_id: int
    status: MembershipStatus = MembershipStatus.PENDING
    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    next_attempt_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class CallAttempt:
    """One dial.

    Attributes:
        telephony_call_id: The carrier's identifier, once it has given us one.
            This is the join between this database and the telephony provider's
            records — and between this row and the bot's own `CALL |` log lines,
            which print the same id.
        attempt_number: Which try this was for its campaign membership.
        conversation_data: What the conversation established, written by the bot
            at the end of the call (Phase 6). `None` means no conversation was
            recorded — nobody answered, the call predates Phase 6, or the bot
            had nowhere to write. Deliberately untyped: it crosses from the
            conversation layer as plain data, and giving it a type here would
            make two packages that know nothing about each other share one.
        idempotency_key: What this attempt *is*, as a value two independent
            callers would compute identically (Phase 9). Unique in the
            database, so "the third attempt at membership 12" can only ever be
            one row however many workers, retries or restarts ask for it. See
            `reliability/idempotency.py`.
        placement_started_at: When the carrier was asked to dial. Set before the
            request, so an attempt whose process died mid-request is still
            identifiable as one that may have placed a call.
    """

    id: int
    prospect_id: int
    campaign_id: int | None
    campaign_prospect_id: int | None
    attempt_number: int
    status: CallAttemptStatus = CallAttemptStatus.PENDING
    telephony_call_id: str | None = None
    telephony_provider: str | None = None
    conversation_data: dict[str, Any] | None = None
    idempotency_key: str | None = None
    placement_started_at: datetime | None = None
    started_at: datetime | None = None
    connected_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    failure_reason: str | None = None
    #: Phase 21: which worker process is following this attempt, or None for
    #: nobody — a one-off dial, or a call a dead worker left for a live one.
    worker_id: str | None = None
    #: Phase 22: the correlation id that is on every log line about this
    #: call in every process — born at the dialer, sent to the bot on the
    #: handshake, read from here by the webhook receiver, the CRM syncer
    #: and the event deliverer. None on rows from before the phase and on
    #: attempts that were never dialled. See `monitoring/tracing.py`.
    trace_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CallbackStatus(StrEnum):
    """Where a scheduled callback has got to. Phase 7."""

    PENDING = "PENDING"
    """Waiting for its time. The queue picks it up through the membership's
    `next_attempt_at`, which `briefing.py` sets to the same moment."""

    PLACED = "PLACED"
    """The dialer placed a call to this prospect after the callback fell due."""

    CANCELLED = "CANCELLED"
    """Withdrawn — the prospect was marked do-not-call, or a person cancelled it."""


@dataclass(frozen=True)
class ScheduledCallback:
    """A promise to call somebody back at a time they chose. Phase 7.

    One row per promise. A prospect has at most one `PENDING` callback: asking
    twice on one call, or on two calls, moves the time rather than stacking a
    second call they did not ask for.

    Attributes:
        scheduled_for: When they asked to be called. Timezone-aware.
        note: What the agent was asked to pass on, in a few words.
    """

    id: int
    prospect_id: int
    scheduled_for: datetime
    status: CallbackStatus = CallbackStatus.PENDING
    campaign_id: int | None = None
    call_attempt_id: int | None = None
    campaign_prospect_id: int | None = None
    note: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MeetingStatus(StrEnum):
    """Whether a booked meeting still stands. Phase 7."""

    BOOKED = "BOOKED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class Meeting:
    """A meeting the agent booked. Phase 7.

    Written only after the calendar provider confirmed the booking — this row
    is the record that it happened, not a request for it to happen. With the
    local calendar it is also the booking itself, which is why `busy_between`
    reads this table.

    Attributes:
        provider: Which calendar took the booking (`local`, `calcom`).
        reference: The provider's id for it, when it gave one.
        timezone: The IANA zone the prospect was told the time in.
    """

    id: int
    prospect_id: int | None
    start_at: datetime
    end_at: datetime
    provider: str
    status: MeetingStatus = MeetingStatus.BOOKED
    reference: str | None = None
    timezone: str = "UTC"
    campaign_id: int | None = None
    call_attempt_id: int | None = None
    attendee_name: str | None = None
    attendee_email: str | None = None
    notes: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class QueuedCall:
    """A reserved piece of work: who to call, for which campaign, on which attempt.

    Returned by the queue once it has already written the reservation, so
    holding one of these means the membership is marked `IN_PROGRESS` and the
    attempt row exists. Nothing else will be handed the same prospect until this
    attempt reaches a final status.
    """

    attempt: CallAttempt
    prospect: Prospect
    campaign: Campaign
    membership: CampaignProspect


class WebhookOutcome(StrEnum):
    """What the receiver did with one carrier event. Phase 14.

    Written onto the delivery's ledger row, so "did the carrier tell us, and
    what did we do about it" is a query rather than a search through logs.
    """

    RECEIVED = "received"
    """Recorded; not yet acted on. The state a row is in for a few milliseconds."""

    APPLIED = "applied"
    """The event moved the attempt's status and the membership with it."""

    DUPLICATE = "duplicate"
    """A redelivery of an event already recorded. Nothing was done."""

    STALE = "stale"
    """Arrived after a later event, or after the call was already final. The
    monotonic rule refused it; the record it would have overwritten stands."""

    UNMATCHED = "unmatched"
    """No attempt carries this call id — a call `call.py` placed by hand, an
    inbound call, or one from another account's number. Nothing to update."""

    IGNORED = "ignored"
    """A status this code does not know. Left alone rather than guessed at."""

    NOTED = "noted"
    """An answering-machine verdict, kept for the completion that follows."""

    TRANSFER = "transfer"
    """Phase 16. How a transfer's colleague leg ended, written onto the transfer
    row; the carrier was answered with the TwiML that decides what the caller
    hears next."""


@dataclass(frozen=True)
class WebhookDelivery:
    """One carrier event as received, and what was done with it. Phase 14.

    The ledger row behind webhook idempotency. `event_key` is unique, so a
    redelivered event is refused by the database before anything reads it;
    the rest is the audit trail — what the carrier said, when, and which
    attempt it was applied to.

    Attributes:
        event_key: What makes two deliveries the same event; see
            `telephony.WebhookEvent.key`.
        kind: `status` or `amd`.
        status: The normalised `CallStatus` value, for a status event.
        sequence: The carrier's own ordering number, when it sent one.
        carrier_timestamp: When the carrier says the event happened.
        outcome: A `WebhookOutcome` value.
        attempt_id: The attempt the event was matched to, once it was.
        payload: The delivered fields, for the audit trail.
    """

    id: int
    provider: str
    call_id: str
    event_key: str
    kind: str
    status: str | None = None
    raw_status: str | None = None
    sequence: int | None = None
    carrier_timestamp: datetime | None = None
    answered_by: str | None = None
    duration_seconds: int | None = None
    outcome: str = WebhookOutcome.RECEIVED.value
    attempt_id: int | None = None
    received_at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class TransferStatus(StrEnum):
    """How a transfer to a person went. Phase 16.

    `REQUESTED` is written by the bot the moment the carrier accepts the
    redirect; the rest come from the carrier's `<Dial action>` report through
    the webhook receiver, once the colleague's leg has ended. A transfer that
    never reports stays `REQUESTED`, which is honest: the bot left the call
    when the carrier took it, and only the carrier knows what happened next.
    """

    REQUESTED = "REQUESTED"
    ANSWERED = "ANSWERED"
    """The colleague picked up and the two spoke."""

    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    FAILED = "FAILED"
    """The carrier could not route to the destination."""

    CANCELED = "CANCELED"
    """The colleague's leg was cancelled before it was answered."""

    @property
    def is_final(self) -> bool:
        """Whether the carrier has reported the end of the colleague's leg."""
        return self is not TransferStatus.REQUESTED

    @property
    def reached_person(self) -> bool:
        """Whether the prospect ended up talking to a colleague."""
        return self is TransferStatus.ANSWERED


@dataclass(frozen=True)
class CallTransfer:
    """One attempt to hand a live call to a person. Phase 16.

    Attributes:
        telephony_call_id: The prospect's call — the one the bot was on.
        dial_call_id: The carrier's id for the colleague's leg, once reported.
        to_number: Where the call was sent.
        reason: What the agent said the person wanted, if anything.
        status: A `TransferStatus`.
        duration_seconds: How long the colleague and the prospect spoke.
        error: Why it did not connect, in the carrier's words, when it did not.
    """

    id: int
    telephony_call_id: str
    provider: str
    to_number: str
    status: TransferStatus = TransferStatus.REQUESTED
    call_attempt_id: int | None = None
    prospect_id: int | None = None
    reason: str | None = None
    dial_call_id: str | None = None
    duration_seconds: int | None = None
    error: str | None = None
    requested_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None


class CrmSyncState(StrEnum):
    """Where one result's CRM synchronisation has got to. Phase 15.

    One row per `call_results` row. The states a row can be in, and what
    moves it: the syncer claims `PENDING` and `RETRY` rows (and `SYNCED` ones
    whose result changed since), holds them as `SYNCING` while it files them,
    and writes one of the three endings back.
    """

    PENDING = "PENDING"
    """Never sent. The state every result starts in when the syncer first sees it."""

    SYNCING = "SYNCING"
    """Claimed by a running syncer. One left here past `stale_secs` belongs to a
    syncer that died, and is claimed again."""

    SYNCED = "SYNCED"
    """Filed. `external_activity_id` is the CRM's record of the call. Claimed
    again only if the result row is updated afterwards — the conversation's
    result replacing the carrier's — in which case the activity is updated."""

    RETRY = "RETRY"
    """A transient failure; due again at `next_attempt_at`."""

    FAILED = "FAILED"
    """Refused on the merits, or out of attempts. `last_error` says why;
    `campaign.py crm-retry` reopens it."""

    SKIPPED = "SKIPPED"
    """Not sent, by policy: an unanswered call when `CRM_SYNC_UNANSWERED` is off."""

    @property
    def is_open(self) -> bool:
        """Whether the syncer may still act on this row."""
        return self in (CrmSyncState.PENDING, CrmSyncState.RETRY, CrmSyncState.SYNCING)


@dataclass(frozen=True)
class CrmSyncRecord:
    """One result's synchronisation status with the CRM. Phase 15.

    Attributes:
        sync_key: The token written into the CRM activity, by which a create
            whose answer was lost can be found again. See `crm.mapping.sync_key`.
        external_contact_id / external_activity_id: The CRM's ids, once known.
            Recorded the moment each is learned, so a crash between the two
            resumes rather than repeats.
        attempts: Passes this row has been claimed for.
        last_error: The most recent failure, in the CRM's words.
        next_attempt_at: When a `RETRY` row is due.
        started_at: When the current claim began.
        synced_at: When it was last filed.
        result_updated_at: The result row's `updated_at` at the time it was
            filed. A newer result is what makes a `SYNCED` row claimable again.
    """

    id: int
    call_result_id: int
    call_attempt_id: int
    prospect_id: int
    provider: str
    state: CrmSyncState = CrmSyncState.PENDING
    sync_key: str = ""
    external_contact_id: str | None = None
    external_activity_id: str | None = None
    attempts: int = 0
    last_error: str | None = None
    next_attempt_at: datetime | None = None
    started_at: datetime | None = None
    synced_at: datetime | None = None
    result_updated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AutomationEventState(StrEnum):
    """Where an outbound automation event has got to. Phase 17.

    The same shape as `CrmSyncState`, for the same reason: a delivery to an
    automation platform is a write to somebody else's system, made once from a
    process of its own, retried on a transient failure and closed with a reason
    on a permanent one.
    """

    PENDING = "PENDING"
    """Created from a row, not yet sent."""

    DELIVERING = "DELIVERING"
    """Claimed by a deliverer for this pass."""

    DELIVERED = "DELIVERED"
    """The receiver answered 2xx. Final unless a person retries it."""

    RETRY = "RETRY"
    """A transient failure; `next_attempt_at` says when it is tried again."""

    FAILED = "FAILED"
    """Refused on the merits, or out of attempts. `campaign.py events-retry` reopens it."""

    SKIPPED = "SKIPPED"
    """Not sent by policy — no target URL for its kind when it was claimed."""

    @property
    def is_open(self) -> bool:
        """Whether a deliverer may still act on this row."""
        return self in (
            AutomationEventState.PENDING,
            AutomationEventState.RETRY,
            AutomationEventState.DELIVERING,
        )


@dataclass(frozen=True)
class AutomationEvent:
    """One thing that happened, as an outbound event for an automation platform. Phase 17.

    Created from the rows that already record the fact — a call result, a
    meeting, a callback, a completed campaign — never from inside a call. The
    row is the outbox: `event_key` is unique, so a fact becomes one event
    however many deliverers look, and a redelivery of the same event carries
    the same id for the receiver to de-duplicate on.

    Attributes:
        event_key: The stable id the receiver sees as `event_id`. Derived from
            what the event *is* (`call.completed:result:12`), not generated.
        kind: One of `AUTOMATION_EVENT_KINDS` in `config.py`.
        result_updated_at: For an event built from a call result, the result's
            `updated_at` when the payload was built — what decides whether a
            later change to the result deserves a `call.updated`.
        occurred_at: When the underlying fact happened. Events are delivered in
            this order.
        payload: What was (or will be) sent, kept once built so an operator
            can read exactly what the receiver got.
        target_url: Where it was sent.
        attempts: Passes this row has been claimed for.
        last_status: The receiver's HTTP status on the last attempt.
        last_error: The most recent failure, in one line.
    """

    id: int
    event_key: str
    kind: str
    state: AutomationEventState = AutomationEventState.PENDING
    call_result_id: int | None = None
    call_attempt_id: int | None = None
    prospect_id: int | None = None
    campaign_id: int | None = None
    meeting_id: int | None = None
    callback_id: int | None = None
    result_updated_at: datetime | None = None
    occurred_at: datetime | None = None
    payload: dict[str, Any] | None = None
    target_url: str | None = None
    attempts: int = 0
    last_status: int | None = None
    last_error: str | None = None
    next_attempt_at: datetime | None = None
    started_at: datetime | None = None
    delivered_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ApiRequestRecord:
    """A stored answer to an API request that carried an `Idempotency-Key`. Phase 17.

    The replay cache behind the automation API: a client that retries a
    request whose answer was lost gets the answer that was given, provided
    the request is the same one (`fingerprint` is a hash of the route and the
    body). The natural keys underneath — a prospect's number, a campaign's
    name, one pending callback per prospect — are what make the *write* safe
    to repeat; this is what makes the *answer* the same.
    """

    id: int
    scope: str
    idempotency_key: str
    fingerprint: str
    status_code: int
    response: dict[str, Any]
    created_at: datetime | None = None
