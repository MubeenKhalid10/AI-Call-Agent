"""Campaign business logic: who may be called, and what a call outcome means.

This is the layer between the store (which knows SQL) and the dialer (which
knows the telephony provider). It knows neither, which is what keeps the three
boundaries the project is built on intact::

    telephony provider  ≠  campaign logic  ≠  prospect database  ≠  conversation

**The one rule worth stating on its own: a prospect marked `DO_NOT_CALL` is
never dialled again.** That is enforced in three independent places, and the
redundancy is deliberate, because each one covers a hole in the others:

1. `set_prospect_status` closes their open memberships the moment they are
   marked, so the queue stops offering them at all.
2. The queue's SQL excludes them, so a membership that slipped through — added
   after the marking, say — is never handed out.
3. `check_callable` runs again immediately before the call is placed, against
   a freshly read row, so a prospect marked while their call was being set up is
   still not dialled.

Any one of those could be removed and the system would usually behave. All three
are here because "usually" is not the standard for phoning somebody who asked
not to be phoned.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger

from ..compliance.dnc import DncEntry, DncSource, parse_source
from ..compliance.policy import CompliancePolicy, PolicyResolver
from ..reliability.idempotency import campaign_call_key
from .coordination import transient_failure
from .csv_import import ParseReport, parse_csv
from .models import (
    CallAttempt,
    CallAttemptStatus,
    Campaign,
    CampaignProspect,
    CampaignStatus,
    MembershipStatus,
    Prospect,
    ProspectStatus,
    QueuedCall,
)
from .phone import normalize_phone
from .results import CallResult, CallResultValidationError, build_carrier_result
from .store import CampaignStore, CampaignStoreError, DuplicateProspectError, retry_at


@dataclass(frozen=True)
class CallabilityCheck:
    """Whether a specific prospect may be called for a specific campaign, and why not.

    A result object rather than a bool because every caller wants the reason:
    the CLI prints it, the dialer records it as the attempt's failure reason,
    and an operator needs to know whether they are looking at a do-not-call
    (never retry) or a bad number (fix the data).
    """

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        """Truthy when the call may go ahead."""
        return self.allowed


@dataclass(frozen=True)
class ImportOutcome:
    """What an import did.

    Attributes:
        report: The parse, including every rejected row and why.
        created: Prospects newly stored.
        duplicates: Rows whose number was already in the database. Not errors —
            re-importing a list is normal — but counted so the operator knows
            the file was not all new.
        added_to_campaign: Memberships created, when importing into a campaign.
        prospect_ids: Everything the file resolved to, new and pre-existing, so
            a caller can add them all to a campaign.
    """

    report: ParseReport
    created: int = 0
    duplicates: int = 0
    added_to_campaign: int = 0
    prospect_ids: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """Default the id list without making it a shared mutable default."""
        if self.prospect_ids is None:
            object.__setattr__(self, "prospect_ids", [])

    def summary(self) -> str:
        """One line for the end of an import."""
        parts = [f"{self.created} created", f"{self.duplicates} already known"]
        if self.added_to_campaign:
            parts.append(f"{self.added_to_campaign} added to the campaign")
        parts.append(f"{len(self.report.invalid_rows)} rejected")
        return ", ".join(parts)


class CampaignService:
    """Prospect and campaign operations, with the calling rules applied.

    Holds a `CampaignStore` and adds the decisions the store deliberately does
    not make: what a valid prospect is, when a number may be dialled, and what a
    call outcome does to a membership.
    """

    def __init__(
        self,
        store: CampaignStore,
        *,
        default_region: str | None = None,
        max_attempts: int = 3,
        retry_minutes: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        compliance: PolicyResolver | None = None,
        retry_transient_failures: bool = True,
        transient_retry_minutes: float | None = None,
    ) -> None:
        """Create the service.

        Phase 21: `retry_transient_failures` gives a `FAILED` attempt whose
        reason was the system's fault — a placement that never reported, a
        carrier that was unavailable, a reservation a dead worker never
        dialled — another try after `transient_retry_minutes` (the general
        retry wait when None), within the attempt ceiling. A number's own
        failure stays exhausted, as Phase 5 decided.

        Args:
            store: The persistence layer.
            default_region: Country assumed for phone numbers with no country
                code. Unset refuses them rather than guessing — see `phone.py`.
            max_attempts: Dials per membership before it is exhausted.
            retry_minutes: Wait before a no-answer or busy is tried again.
            clock: Where "now" comes from, for the retry timing. The real clock
                by default; the scheduler's checks inject one (Phase 13) so a
                retry an hour away can be reached without waiting an hour.
            compliance: Phase 19. Resolves the policy for a campaign or a call
                — the attempt ceiling and retry waits per campaign and per
                jurisdiction, and the disclosures. None means the two figures
                above apply everywhere, as they did before.
        """
        self._store = store
        self._default_region = default_region
        self._max_attempts = max_attempts
        self._retry_minutes = retry_minutes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._compliance = compliance
        self._dnc_list_warned = False
        self._retry_transient = retry_transient_failures
        self._transient_retry_minutes = transient_retry_minutes

    @property
    def store(self) -> CampaignStore:
        """The underlying store, for reads the service adds nothing to."""
        return self._store

    @property
    def max_attempts(self) -> int:
        """Dials allowed per membership, before any campaign or jurisdiction overlay."""
        return self._max_attempts

    @property
    def compliance(self) -> PolicyResolver | None:
        """The policy resolver, when one is configured. Phase 19."""
        return self._compliance

    def now(self) -> datetime:
        """The current moment, from the injected clock. Timezone-aware."""
        return self._clock()

    # --- Compliance policy (Phase 19) ---------------------------------------

    def policy_for(self, campaign: Campaign | None) -> CompliancePolicy:
        """The policy a campaign runs under: the environment's, with the campaign's overrides."""
        if self._compliance is None:
            return CompliancePolicy(max_attempts=self._max_attempts, retry_minutes=self._retry_minutes, sources=("service",))
        return self._compliance.for_campaign(
            campaign.configuration if campaign is not None else None,
            campaign_id=campaign.id if campaign is not None else None,
        )

    def policy_for_call(self, campaign: Campaign | None, prospect: Prospect | None) -> CompliancePolicy:
        """The policy for one call: the campaign's, then the number's jurisdiction."""
        if self._compliance is None:
            return self.policy_for(campaign)
        policy, _region = self._compliance.for_call(
            campaign.configuration if campaign is not None else None,
            prospect.phone_normalized if prospect is not None else None,
            campaign_id=campaign.id if campaign is not None else None,
        )
        return policy

    def max_attempts_for(self, campaign: Campaign | None) -> int:
        """The attempt ceiling for a campaign — the queue's SQL applies this one."""
        return self.policy_for(campaign).max_attempts

    # --- Prospects ----------------------------------------------------------

    async def create_prospect(
        self,
        *,
        first_name: str,
        last_name: str,
        phone: str,
        **fields: object,
    ) -> Prospect:
        """Create one prospect, normalising the phone number on the way in.

        A number that will not normalise does not stop the prospect being
        stored — the record is still real and the number can be corrected — but
        it is stored with no `phone_normalized`, which is what makes them
        undialable rather than dialable-to-somewhere-unknown.

        Raises:
            ValueError: A required field is blank.
            DuplicateProspectError: That number is already stored.
        """
        if not first_name.strip() or not last_name.strip():
            raise ValueError("first_name and last_name are required.")
        if not phone.strip():
            raise ValueError("phone is required.")

        number = normalize_phone(phone, default_region=self._default_region)
        if not number.is_dialable:
            logger.warning(
                f"PROSPECT | {first_name} {last_name}: phone {phone!r} — {number.reason}"
            )

        prospect = await self._store.add_prospect(
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            phone=phone.strip(),
            phone_normalized=number.e164,
            **fields,  # type: ignore[arg-type]
        )
        # Phase 19: a number that was on the do-not-call list before its
        # prospect row existed is blocked the moment the row appears.
        if await self._apply_dnc_list([prospect.id]):
            refreshed = await self._store.get_prospect(prospect.id)
            if refreshed is not None:
                logger.info(f"DNC | new prospect {prospect.id} is on the do-not-call list; marked DO_NOT_CALL")
                return refreshed
        return prospect

    async def mark_do_not_call(
        self,
        prospect_id: int,
        *,
        source: DncSource | str = DncSource.MANUAL,
        reason: str | None = None,
        actor: str | None = None,
        campaign_id: int | None = None,
        call_attempt_id: int | None = None,
    ) -> bool:
        """Mark a prospect as never to be called again, on any campaign.

        Two writes, in this order: the person's status (which closes their
        open memberships and pending callbacks in one transaction — Phase 5
        and 7), then the number onto the do-not-call list (Phase 19) with
        where the request came from, so a re-import under a new row, or a
        second prospect with the same number, is blocked too. The status
        write is the urgent one and goes first; a list that cannot be
        written is logged loudly and does not undo it.

        Returns:
            True if the prospect existed.
        """
        prospect = await self._store.get_prospect(prospect_id)
        updated = await self._store.set_prospect_status(prospect_id, ProspectStatus.DO_NOT_CALL)
        if not updated:
            return False
        logger.info(f"DNC | prospect {prospect_id} will not be called again")
        if prospect is not None and prospect.phone_normalized:
            await self._list_number(
                prospect.phone_normalized,
                source=source,
                reason=reason,
                actor=actor,
                prospect_id=prospect_id,
                campaign_id=campaign_id,
                call_attempt_id=call_attempt_id,
            )
        return True

    async def add_do_not_call_number(
        self,
        phone: str,
        *,
        source: DncSource | str = DncSource.MANUAL,
        reason: str | None = None,
        actor: str | None = None,
        campaign_id: int | None = None,
        call_attempt_id: int | None = None,
        note: str | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[DncEntry, bool, int]:
        """Put a number on the do-not-call list, prospect row or no prospect row. Phase 19.

        For a suppression file, a registry, an API call with a bare number,
        or a caller with no prospect id. Every prospect carrying the number is
        marked `DO_NOT_CALL` as well.

        Returns:
            `(entry, inserted, prospects_marked)`.

        Raises:
            ValueError: The number cannot be normalised.
        """
        number = normalize_phone(phone, default_region=self._default_region)
        if not number.e164:
            raise ValueError(f"{phone!r} could not be normalised: {number.reason}")
        entry, inserted = await self._store.add_dnc(
            number.e164,
            source=source,
            reason=reason,
            created_by=actor,
            campaign_id=campaign_id,
            call_attempt_id=call_attempt_id,
            note=note,
            expires_at=expires_at,
        )
        marked = 0
        for prospect in await self._store.prospects_with_number(number.e164):
            if prospect.status is not ProspectStatus.DO_NOT_CALL:
                if await self._store.set_prospect_status(prospect.id, ProspectStatus.DO_NOT_CALL):
                    marked += 1
        logger.info(
            f"DNC | {number.e164} {'added to' if inserted else 'already on'} the do-not-call list "
            f"({getattr(source, 'value', source)}); {marked} prospect(s) marked"
        )
        return entry, inserted, marked

    async def remove_do_not_call_number(
        self, phone: str, *, actor: str, reason: str | None = None, reinstate_prospects: bool = False
    ) -> tuple[DncEntry | None, int]:
        """Take a number off the list. Phase 19. The prospects stay `DO_NOT_CALL` unless asked.

        Deliberately two decisions: revoking the list entry says "this number
        may be dialled again"; `reinstate_prospects` says "and these people
        are back to NEW". An operator removing a number entered by mistake
        wants both; one honouring an expiry usually wants only the first.

        Returns:
            `(revoked entry or None, prospects reinstated)`.
        """
        number = normalize_phone(phone, default_region=self._default_region)
        if not number.e164:
            raise ValueError(f"{phone!r} could not be normalised: {number.reason}")
        entry = await self._store.revoke_dnc(number.e164, revoked_by=actor, reason=reason)
        reinstated = 0
        if entry is not None and reinstate_prospects:
            for prospect in await self._store.prospects_with_number(number.e164):
                if prospect.status is ProspectStatus.DO_NOT_CALL:
                    if await self._store.set_prospect_status(prospect.id, ProspectStatus.NEW):
                        reinstated += 1
        logger.info(
            f"DNC | {number.e164} {'removed from' if entry else 'was not on'} the do-not-call list by {actor}"
            + (f"; {reinstated} prospect(s) reinstated" if reinstated else "")
        )
        return entry, reinstated

    async def is_listed(self, phone_normalized: str | None) -> DncEntry | None:
        """The active list entry for a number, or None. A store without the table is None, with a warning once."""
        if not phone_normalized:
            return None
        finder = getattr(self._store, "find_dnc", None)
        if finder is None:
            return None
        try:
            entry = await finder(phone_normalized)
        except CampaignStoreError as exc:
            self._warn_dnc_list(exc)
            return None
        if entry is not None and not entry.is_active_at(self.now()):
            return None
        return entry

    async def _list_number(
        self,
        phone_normalized: str,
        *,
        source: DncSource | str,
        reason: str | None,
        actor: str | None,
        prospect_id: int | None,
        campaign_id: int | None,
        call_attempt_id: int | None,
    ) -> DncEntry | None:
        adder = getattr(self._store, "add_dnc", None)
        if adder is None:
            return None
        try:
            entry, inserted = await adder(
                phone_normalized,
                source=source,
                reason=reason,
                prospect_id=prospect_id,
                campaign_id=campaign_id,
                call_attempt_id=call_attempt_id,
                created_by=actor,
            )
        except CampaignStoreError as exc:
            self._warn_dnc_list(exc)
            return None
        if inserted:
            logger.info(f"DNC | {phone_normalized} added to the do-not-call list ({getattr(source, 'value', source)})")
        return entry

    async def _apply_dnc_list(self, prospect_ids: list[int]) -> int:
        """Mark listed numbers among these prospects. 0 when the store has no list."""
        apply = getattr(self._store, "apply_dnc_list", None)
        if apply is None or not prospect_ids:
            return 0
        try:
            return int(await apply(prospect_ids))
        except CampaignStoreError as exc:
            self._warn_dnc_list(exc)
            return 0

    def _warn_dnc_list(self, exc: Exception) -> None:
        if self._dnc_list_warned:
            return
        self._dnc_list_warned = True
        logger.warning(
            f"DNC | the do-not-call list could not be used: {(str(exc).splitlines() or [type(exc).__name__])[0]} — "
            f"the prospect's status still applies; run `uv run campaign.py init`"
        )

    # --- Import -------------------------------------------------------------

    async def import_csv(
        self,
        text: str,
        *,
        campaign_id: int | None = None,
        dry_run: bool = False,
    ) -> ImportOutcome:
        """Parse a CSV and store the rows that are usable.

        Rows are handled one at a time and a bad one never stops the others:
        the point of an import report is that 900 good rows load while the 3
        broken ones are described precisely enough to fix.

        Args:
            text: The file's contents.
            campaign_id: Add everything the file resolves to — including
                prospects that already existed — to this campaign.
            dry_run: Parse and report, write nothing. This is what makes
                "review the mapping before importing" possible.
        """
        report = parse_csv(text, default_region=self._default_region)
        outcome = ImportOutcome(report=report)
        if report.error or not report.mapping.is_usable:
            return outcome
        if dry_run:
            # Phase 24 audit: the preview counts the numbers the database
            # already holds, so "2 valid, 0 already known" is never followed
            # by an import that says "0 created, 2 already known".
            duplicates = 0
            for row in report.valid_rows:
                if row.phone and await self._store.find_prospect_by_phone(row.phone.e164):
                    duplicates += 1
            return ImportOutcome(report=report, duplicates=duplicates)

        created = duplicates = added = 0
        prospect_ids: list[int] = []

        for row in report.valid_rows:
            values = dict(row.values)
            phone_raw = values.pop("phone")
            first_name = values.pop("first_name")
            last_name = values.pop("last_name")
            normalized = row.phone.e164 if row.phone else None

            try:
                prospect = await self._store.add_prospect(
                    first_name=first_name,
                    last_name=last_name,
                    phone=phone_raw,
                    phone_normalized=normalized,
                    custom_data=row.custom_data,
                    **values,
                )
                created += 1
            except DuplicateProspectError as exc:
                # Expected when a list is re-imported. The existing row wins:
                # overwriting it could undo a do-not-call or a corrected number.
                duplicates += 1
                existing = await self._store.get_prospect(exc.existing_id)
                if existing is None:
                    continue
                prospect = existing

            prospect_ids.append(prospect.id)
            # Phase 19: a person who asked not to be called does not join a
            # campaign, however many times their number is imported. The
            # queue would refuse them anyway; not opening the membership
            # keeps the campaign's counts honest.
            if prospect.status is ProspectStatus.DO_NOT_CALL:
                continue
            # Phase 23: a *new* row whose number is on the do-not-call list is
            # marked before the membership is opened, not after — the list was
            # applied once at the end, so the report's "added to campaign"
            # counted a number that must never ring and the membership existed
            # (open, then closed) for the length of the import.
            if await self._apply_dnc_list([prospect.id]):
                continue
            if campaign_id is not None and await self._store.add_to_campaign(
                campaign_id, prospect.id
            ):
                added += 1

        # Phase 19: anybody on the do-not-call list is marked before the
        # import is reported, so the report's "added to campaign" can never
        # include a number that must not ring.
        if prospect_ids:
            marked = await self._apply_dnc_list(prospect_ids)
            if marked:
                logger.info(f"IMPORT | {marked} imported prospect(s) are on the do-not-call list; marked DO_NOT_CALL")

        return ImportOutcome(
            report=report,
            created=created,
            duplicates=duplicates,
            added_to_campaign=added,
            prospect_ids=prospect_ids,
        )

    # --- Campaigns ----------------------------------------------------------

    async def create_campaign(self, name: str, description: str | None = None) -> Campaign:
        """Create a campaign in `DRAFT`.

        Draft rather than active on purpose: a campaign that started dialling
        the moment it was named would call whoever was added to it first, before
        anybody had reviewed the list.
        """
        campaign = await self._store.create_campaign(name=name, description=description)
        logger.info(f"CAMPAIGN | created {campaign.name!r} (id {campaign.id}) in DRAFT")
        return campaign

    async def add_prospects(self, campaign_id: int, prospect_ids: list[int]) -> int:
        """Add prospects to a campaign, skipping any already in it.

        Returns:
            How many memberships were created.
        """
        added = 0
        for prospect_id in prospect_ids:
            # Phase 19: a do-not-call prospect never joins a campaign, and a
            # number on the list is checked too — the row may be NEW because
            # the list was loaded after the import.
            prospect = await self._store.get_prospect(prospect_id)
            if prospect is None or prospect.status is ProspectStatus.DO_NOT_CALL:
                continue
            if await self.is_listed(prospect.phone_normalized) is not None:
                await self._apply_dnc_list([prospect_id])
                continue
            if await self._store.add_to_campaign(campaign_id, prospect_id):
                added += 1
        return added

    async def set_status(self, campaign_id: int, status: CampaignStatus) -> Campaign | None:
        """Move a campaign between DRAFT / ACTIVE / PAUSED / COMPLETED / CANCELLED."""
        campaign = await self._store.set_campaign_status(campaign_id, status)
        if campaign:
            logger.info(f"CAMPAIGN | {campaign.name!r} is now {campaign.status.value}")
        return campaign

    # --- The rules ----------------------------------------------------------

    async def check_callable(
        self,
        prospect: Prospect,
        campaign: Campaign | None = None,
        membership: CampaignProspect | None = None,
        *,
        ignore_attempt_id: int | None = None,
        ignore_attempt_limit: bool = False,
        max_attempts: int | None = None,
    ) -> CallabilityCheck:
        """Decide whether this prospect may be dialled right now.

        Phase 19: `max_attempts` is the ceiling to apply — the gate passes
        the policy's, which may be a jurisdiction's lower figure. None means
        the campaign's own (or the environment's).

        The last gate before a call is placed, and the one that runs against
        freshly read rows. The queue applies the same rules in SQL to choose
        work; this re-applies them because time passes between choosing and
        dialling, and a prospect can be marked do-not-call inside that gap.

        Order matters only in what it reports first: do-not-call is checked
        before everything else so that a DNC prospect never produces a message
        about attempt limits, which would suggest the limit is the problem.

        Args:
            prospect: Read fresh, not the copy the queue handed out.
            campaign: Checked for being ACTIVE, when given.
            membership: Checked for attempt limit and retry timing, when given.
            ignore_attempt_id: An attempt not to count as "already on a call".
                The dialer passes the attempt it is about to place, since
                reserving created it and it would otherwise block itself.
                Passing it also tells the limit check that the membership's
                count already includes this attempt.
            ignore_attempt_limit: Waive the attempt limit. Only for a callback
                the prospect asked for (Phase 13): the limit exists to stop
                pestering people who do not answer, and a person who asked to
                be phoned back is the opposite case. Everything else — DNC,
                the number, the campaign's status, a live call — still applies.
        """
        if prospect.status is ProspectStatus.DO_NOT_CALL:
            return CallabilityCheck(False, "the prospect is marked DO_NOT_CALL")

        if not prospect.phone_normalized:
            return CallabilityCheck(
                False, f"no dialable number: {prospect.phone!r} could not be normalised"
            )

        if campaign is not None and not campaign.status.is_dialable:
            return CallabilityCheck(
                False, f"campaign {campaign.name!r} is {campaign.status.value}, not ACTIVE"
            )

        if membership is not None:
            # Closed, not "not open": a membership the queue has already
            # reserved is IN_PROGRESS, and refusing that would refuse the very
            # call being placed. Without this check at all, `campaign.py next`
            # called an exhausted membership "ready" for work the queue would
            # never hand out.
            if membership.status.is_closed:
                return CallabilityCheck(
                    False, f"this campaign is {membership.status.value} for them"
                )
            # The reservation has already counted the attempt being placed
            # (`attempt_count` is incremented in the transaction that hands
            # the call out), so a membership re-checked *for its own reserved
            # attempt* reads one over the limit. Phase 13 found that without
            # this allowance the last permitted attempt — the third of three,
            # or the only one of one — was always released as "attempt limit
            # reached" without dialling.
            used = membership.attempt_count - (1 if ignore_attempt_id is not None else 0)
            limit = max_attempts if max_attempts is not None else self.max_attempts_for(campaign)
            if used >= limit and not ignore_attempt_limit:
                return CallabilityCheck(
                    False,
                    f"attempt limit reached ({used}/{limit})",
                )
            if membership.next_attempt_at and membership.next_attempt_at > self.now():
                return CallabilityCheck(
                    False, f"not due for a retry until {membership.next_attempt_at:%Y-%m-%d %H:%M}"
                )

        if await self._store.has_live_attempt(prospect.id, exclude_attempt_id=ignore_attempt_id):
            return CallabilityCheck(False, "the prospect is already on a call")

        return CallabilityCheck(True)

    async def next_call(
        self, campaign_id: int, *, max_concurrent: int = 0, worker_id: str | None = None
    ) -> QueuedCall | None:
        """Reserve the next call for a campaign, or None if nothing is eligible.

        Delegates to the store because the choice and the reservation have to be
        one transaction — see `CampaignStore.reserve_next_call`. The rules it
        applies are the ones `check_callable` describes.

        Phase 9 supplies the idempotency key: the reserved attempt is stamped
        with what it *is* — this campaign, this membership, this attempt number
        — under a unique index, so no second row for the same call can exist
        however the caller got here.

        Args:
            campaign_id: Which campaign to draw work from.
            max_concurrent: Live calls allowed at once, enforced inside the
                reservation transaction (Phase 11). 0 leaves it to the caller.
        """
        # Phase 19: the campaign's own ceiling, when it has one. One read;
        # the reservation is the same transaction it always was.
        campaign = await self._store.get_campaign(campaign_id)
        queued = await self._store.reserve_next_call(
            campaign_id,
            max_attempts=self.max_attempts_for(campaign),
            max_concurrent=max_concurrent,
            worker_id=worker_id,
            idempotency_key=lambda membership_id, attempt_number: campaign_call_key(
                campaign_id=campaign_id,
                membership_id=membership_id,
                attempt_number=attempt_number,
            ),
        )
        if queued is None:
            return None
        logger.info(
            f"QUEUE | reserved {queued.prospect.full_name} ({queued.prospect.phone_normalized}) "
            f"for {queued.campaign.name!r}, attempt {queued.attempt.attempt_number}"
        )
        return queued

    async def reserve_membership(
        self,
        membership_id: int,
        *,
        max_concurrent: int = 0,
        ignore_attempt_limit: bool = False,
        worker_id: str | None = None,
    ) -> QueuedCall | None:
        """Reserve one specific membership's next call, or None if it is not eligible. Phase 13.

        The targeted form of `next_call`, for a scheduled callback: the queue
        orders never-called prospects first, so a promise to phone somebody at
        ten would otherwise wait behind every fresh row in the list. Every rule
        the queue applies still applies here — the campaign must be `ACTIVE`,
        the membership `PENDING` and due, the prospect not `DO_NOT_CALL`, with
        a number and no live call — and the reservation is the same
        transaction, with the same lock and the same idempotency key.

        Args:
            membership_id: Which membership.
            max_concurrent: As for `next_call`.
            ignore_attempt_limit: Waive `CAMPAIGN_MAX_ATTEMPTS` for this one
                reservation. The callback case; see `check_callable`.
        """
        membership = await self._store.get_membership(membership_id)
        if membership is None:
            return None
        campaign_id = membership.campaign_id
        campaign = await self._store.get_campaign(campaign_id)
        queued = await self._store.reserve_membership(
            membership_id,
            max_attempts=self.max_attempts_for(campaign),
            max_concurrent=max_concurrent,
            ignore_attempt_limit=ignore_attempt_limit,
            worker_id=worker_id,
            idempotency_key=lambda membership_id, attempt_number: campaign_call_key(
                campaign_id=campaign_id,
                membership_id=membership_id,
                attempt_number=attempt_number,
            ),
        )
        if queued is None:
            return None
        logger.info(
            f"QUEUE | reserved {queued.prospect.full_name} ({queued.prospect.phone_normalized}) "
            f"for {queued.campaign.name!r}, attempt {queued.attempt.attempt_number} (targeted)"
        )
        return queued

    async def record_outcome(
        self,
        attempt: CallAttempt,
        status: CallAttemptStatus,
        *,
        failure_reason: str | None = None,
        duration_seconds: int | None = None,
        write_result: bool = True,
    ) -> CallAttempt | None:
        """Close a call attempt and move its membership on accordingly.

        This is where a call outcome becomes campaign state, and the mapping is
        the whole retry policy:

        * reached them → the membership is `COMPLETED`; this campaign is done
          with this person whatever was said. What they *said* is Phase 6's
          problem, not this layer's.
        * no answer or busy → back to `PENDING` with a retry time, unless that
          was the last attempt, in which case `EXHAUSTED`.
        * anything else (a failure, a wrong number) → `EXHAUSTED`, because
          retrying a number that could not be dialled just spends money.
        * `DO_NOT_CALL` → the prospect is marked, which closes every membership
          they have, in this campaign and every other.

        Phase 8: a final status also writes the attempt's `CallResult` from the
        carrier's report — the thin result a call nobody answered gets, so that
        *every* finished attempt has one row in `call_results`. It never
        overwrites a result the conversation wrote.

        Args:
            write_result: Whether to write that carrier-side result. The
                conversation sink passes False, because it writes the rich
                result itself a moment later.
        """
        updated = await self._store.update_attempt_status(
            attempt.id,
            status,
            failure_reason=failure_reason,
            duration_seconds=duration_seconds,
        )
        if updated is None:
            return None

        if write_result and status.is_final:
            await self.record_carrier_result(updated)

        if status is CallAttemptStatus.DO_NOT_CALL:
            # Phase 19: the list row names the call the request came from.
            # `add_dnc` keeps the first record, so a call the gate refused
            # because the number was *already* listed adds nothing.
            await self.mark_do_not_call(
                attempt.prospect_id,
                source=DncSource.VERBAL,
                reason=failure_reason or f"asked during call attempt {attempt.id}",
                actor="bot",
                campaign_id=attempt.campaign_id,
                call_attempt_id=attempt.id,
            )
            return updated

        if not status.is_final:
            # A call that is ringing or connected changes the attempt and
            # nothing else: the membership stays `IN_PROGRESS`, which is what
            # the status means. Phase 13 found that `refresh` had been
            # reaching this point with `CALLING`, which fell through every
            # branch below and marked the membership `EXHAUSTED` for the
            # length of the ring — healed by the next event, and wrong on the
            # dashboard in between.
            return updated

        if attempt.campaign_prospect_id is None:
            return updated

        membership = await self._store.get_membership(attempt.campaign_prospect_id)
        if membership is None:
            return updated
        if membership.status.is_closed:
            # Phase 13: something else closed it while the attempt was live —
            # a do-not-call marked from the command line, say, which set it
            # `SKIPPED`. That decision stands; an outcome must not reopen or
            # relabel a membership the campaign is already finished with.
            return updated

        if status.reached_person:
            await self._store.set_membership_status(membership.id, MembershipStatus.COMPLETED)
            await self._store.set_prospect_status(attempt.prospect_id, ProspectStatus.CONTACTED)
            return updated

        # Phase 19: the ceiling and the wait come from the policy for this
        # campaign and this number — a jurisdiction may allow fewer attempts
        # than the campaign, and a voicemail may wait longer than a busy tone.
        campaign = await self._store.get_campaign(attempt.campaign_id) if attempt.campaign_id else None
        prospect = await self._store.get_prospect(attempt.prospect_id)
        policy = self.policy_for_call(campaign, prospect)
        if status.should_retry and membership.attempt_count < policy.max_attempts:
            await self._store.set_membership_status(
                membership.id,
                MembershipStatus.PENDING,
                next_attempt_at=retry_at(policy.retry_minutes_for(status), now=self.now()),
            )
        elif (
            # Phase 21: a failure that was the system's, not the number's,
            # is worth another try — within the ceiling, after a wait.
            status is CallAttemptStatus.FAILED
            and self._retry_transient
            and transient_failure(failure_reason or updated.failure_reason)
            and membership.attempt_count < policy.max_attempts
        ):
            wait = self._transient_retry_minutes if self._transient_retry_minutes is not None else policy.retry_minutes
            logger.info(
                f"QUEUE | attempt {attempt.id} failed for a transient reason; membership "
                f"{membership.id} retries in {wait:g} min ({membership.attempt_count}/{policy.max_attempts} used)"
            )
            await self._store.set_membership_status(
                membership.id,
                MembershipStatus.PENDING,
                next_attempt_at=retry_at(wait, now=self.now()),
            )
        else:
            await self._store.set_membership_status(membership.id, MembershipStatus.EXHAUSTED)

        return updated

    async def record_carrier_result(self, attempt: CallAttempt) -> CallResult | None:
        """Write the result a finished attempt gets from the carrier's report alone. Phase 8.

        Best effort, and never raises: this runs inside the outcome write, and
        a database that predates the table, or a result that somehow fails its
        own validation, is worth a log line and not a lost campaign update.

        Returns:
            The stored result, or None when nothing was written — the store
            refused it in favour of a conversation result, the table is
            missing, or the attempt is not final.
        """
        if not attempt.status.is_final:
            return None
        try:
            result = build_carrier_result(attempt)
            stored = await self._store.save_call_result(result)
        except CallResultValidationError as exc:
            logger.error(f"RESULT | attempt {attempt.id} | not stored: {exc}")
            return None
        except CampaignStoreError as exc:
            logger.warning(f"RESULT | attempt {attempt.id} | not stored: {exc}")
            return None
        if stored is None:
            logger.debug(
                f"RESULT | attempt {attempt.id} | the conversation's result stands; "
                f"the carrier's {result.disposition.value} was not written over it"
            )
        else:
            logger.info(f"RESULT | attempt {attempt.id} | {stored.disposition.value} (from the carrier)")
        return stored

    async def defer(self, queued: QueuedCall, reason: str, *, retry_after_secs: float) -> bool:
        """Give a reservation back without spending it, to be offered again later. Phase 13.

        The counterpart of `release` for the case where nothing is wrong with
        the call except the moment: the prospect's own calling window is
        closed. `release` records a failed attempt, which is right when the
        carrier refused and wrong here — a scheduler that released would burn
        every attempt a prospect in another timezone had before their morning
        arrived. So the attempt row is removed, the count restored, and the
        membership scheduled for when the window opens.

        Falls back to `release` when the store refuses to undo the
        reservation — the attempt had already started placing — so a caller
        never ends up holding a live row it thinks is gone.

        Returns:
            True if the reservation was undone; False if it had to be released.
        """
        until = self.now() + timedelta(seconds=max(0.0, retry_after_secs))
        logger.info(
            f"QUEUE | deferring {queued.prospect.full_name} until {until:%Y-%m-%d %H:%M %Z}: {reason}"
        )
        undone = await self._store.unreserve_attempt(queued.attempt.id, next_attempt_at=until)
        if not undone:
            await self.release(queued, reason)
        return undone

    async def exhaust(self, queued: QueuedCall, reason: str) -> bool:
        """Give a reservation back and close the membership: the ceiling is reached. Phase 19.

        For a jurisdiction's attempt limit, which the queue's SQL cannot
        apply per number: the reservation was taken under the campaign's
        ceiling and the gate found a lower one. Nothing was dialled, so the
        attempt row is removed as `defer` removes it, and the membership is
        `EXHAUSTED` rather than rescheduled. Falls back to `release` when the
        reservation cannot be undone.

        Returns:
            True if the reservation was undone and the membership closed.
        """
        logger.info(f"QUEUE | exhausting {queued.prospect.full_name}: {reason}")
        undone = await self._store.unreserve_attempt(queued.attempt.id, next_attempt_at=self.now())
        if not undone:
            await self.release(queued, reason)
            return False
        await self._store.set_membership_status(queued.membership.id, MembershipStatus.EXHAUSTED)
        return True

    async def unreserve(self, attempt: CallAttempt) -> bool:
        """Hand back a reservation that never dialled, so the queue offers it again. Phase 13.

        Recovery's version of `defer`: an attempt a crashed process reserved
        and never placed is not call history, and closing it as failed would
        exhaust a prospect nobody phoned. The store refuses unless the row is
        still in the never-placed shape, in which case the caller falls back to
        closing it.

        Returns:
            True if the reservation was undone.
        """
        return await self._store.unreserve_attempt(attempt.id, next_attempt_at=self.now())

    async def release(self, queued: QueuedCall, reason: str) -> None:
        """Give a reservation back without having placed a call.

        For the case where the reservation succeeded but the dial did not even
        start — the safety check refused it, or the carrier rejected the
        request. The attempt is recorded as `FAILED` with the reason rather than
        deleted, because "we tried and could not" is call history too, and a
        reservation that vanished would leave the attempt count raised with
        nothing to show for it.
        """
        logger.warning(f"QUEUE | releasing {queued.prospect.full_name}: {reason}")
        await self.record_outcome(queued.attempt, CallAttemptStatus.FAILED, failure_reason=reason)
