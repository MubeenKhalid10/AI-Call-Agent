"""What a restart does about calls that were in flight when it happened. Phase 9.

An attempt row in a live status means "somebody is watching this call". After a
restart nobody is, and the row will stay live forever — which blocks the
prospect from ever being dialled again, because a live attempt is exactly what
`has_live_attempt` refuses to dial past. So every start of the dialer, and the
`campaign.py recover` command, runs a pass over the live rows and resolves each
one.

**The rule that shapes all of it: recovery never dials.** Every branch below
ends in reading from the carrier or writing a status, and none of them ends in
`place_call`. A prospect whose state cannot be determined is left *failed*, not
retried — the campaign's own retry policy can offer them again later under its
own rules, with its own attempt count, which is the only place a redial should
ever be decided.

**Four shapes of live row, and what each means.**

| Row | What happened | What recovery does |
|---|---|---|
| has a carrier call id | placement succeeded; the watcher died | ask the carrier and apply the answer |
| `UNRESOLVED`, no call id | the placement request never reported | search the carrier for a call to that number since placement started |
| `PENDING`, never placed | reserved, then the process died before dialling | hand it back: nothing was placed, so the queue offers it again (Phase 13) |
| `CALLING`, no call id, placement started | died *during* the request | same search as `UNRESOLVED` — this is the same ambiguity |

The search is bounded by `placement_started_at`, so a call the carrier made for
some *earlier* attempt to the same number cannot be mistaken for this one. When
the carrier cannot list calls at all, the attempt is marked failed and the
reason says to check the carrier's log by hand — an honest dead end rather than
a guess.

**Age matters.** A pass only considers rows untouched for `min_age_secs`,
because a healthy call two seconds old is indistinguishable from an abandoned
one except by age, and reconciling a live call would end it.

**Why this lives in `campaigns/` and not in `reliability/`.** It is the third
module in the pattern `dialer.py` and `briefing.py` established: one that knows
two worlds at once — here the campaign tables and the carrier — and therefore
belongs on the side that owns the rows. Keeping it out of `src/reliability/`
also keeps that package a strictly lower layer with no campaign imports, which
is what stops the two from becoming circular.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from loguru import logger

from ..reliability.observability import CallContext, call_context, event
from ..reliability.retry import READ_POLICY, call_with_retry, read_classifier
from ..telephony import CallSnapshot, CallStatus, TelephonyError, TelephonyProvider
from .models import CallAttempt, CallAttemptStatus
from .service import CampaignService
from .store import CampaignStoreError

#: The carrier's vocabulary, in the attempt's. Identical to the dialer's map and
#: deliberately kept here rather than imported from it: this module must resolve
#: a call even when the dialer is not running, and a shared constant between the
#: two would make one of them import the other for three lines.
_STATUS_MAP = {
    CallStatus.QUEUED: CallAttemptStatus.QUEUED,
    CallStatus.RINGING: CallAttemptStatus.CALLING,
    CallStatus.ANSWERED: CallAttemptStatus.CONNECTED,
    CallStatus.COMPLETED: CallAttemptStatus.COMPLETED,
    CallStatus.BUSY: CallAttemptStatus.BUSY,
    CallStatus.NO_ANSWER: CallAttemptStatus.NO_ANSWER,
    CallStatus.FAILED: CallAttemptStatus.FAILED,
    CallStatus.CANCELED: CallAttemptStatus.FAILED,
}

#: How far back to look for a call that an ambiguous placement may have made.
#: Generous, because the alternative to finding it is a person being phoned
#: twice; bounded, because a wider window starts finding *other* attempts.
_SEARCH_BACK = timedelta(minutes=15)


@dataclass
class RecoveryReport:
    """What one recovery pass did, for the log and the CLI.

    Attributes:
        resolved: Attempts whose real outcome was read from the carrier.
        released: Attempts that had never been placed and were freed.
        found_calls: Ambiguous placements that turned out to have made a call.
        no_call: Ambiguous placements that turned out not to have.
        failed: Attempts that could not be resolved and were closed as failed.
        left: Attempts deliberately left alone — too young, or still genuinely
            live according to the carrier.
    """

    resolved: int = 0
    released: int = 0
    found_calls: int = 0
    no_call: int = 0
    failed: int = 0
    left: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """How many attempts the pass touched or considered."""
        return self.resolved + self.released + self.failed + self.left

    def describe(self) -> str:
        """One line for a log or the CLI."""
        if not self.total:
            return "nothing to recover"
        parts = [
            f"{self.resolved} reconciled",
            f"{self.released} released",
            f"{self.failed} closed as failed",
            f"{self.left} left alone",
        ]
        if self.found_calls or self.no_call:
            parts.append(
                f"ambiguous: {self.found_calls} had placed a call, {self.no_call} had not"
            )
        return ", ".join(parts)


class AttemptRecovery:
    """Resolves call attempts that were live when something stopped watching them."""

    def __init__(
        self,
        service: CampaignService,
        provider: TelephonyProvider | None,
        *,
        min_age_secs: float = 120.0,
        limit: int = 100,
    ) -> None:
        """Create the recovery pass.

        Args:
            service: Campaign rules and persistence.
            provider: The carrier to ask. `None` — no credentials configured —
                still runs: rows that were never placed are released, and ones
                that might have been are closed as failed with a reason saying
                a carrier is needed to do better.
            min_age_secs: Ignore attempts touched more recently than this, so a
                pass never reconciles a call that is happening right now.
            limit: Attempts per pass.
        """
        self._service = service
        self._provider = provider
        self._min_age = max(0.0, min_age_secs)
        self._limit = limit

    async def run(self) -> RecoveryReport:
        """Resolve every stale live attempt. Never raises, never dials.

        Returns:
            What the pass did. An empty report is the normal result.
        """
        report = RecoveryReport()
        try:
            attempts = await self._service.store.list_live_attempts(
                older_than_secs=self._min_age, limit=self._limit
            )
        except CampaignStoreError as exc:
            logger.error(event("recovery.unavailable", error=str(exc)))
            report.notes.append(f"could not list attempts: {exc}")
            return report

        if not attempts:
            return report

        logger.info(
            event("recovery.started", outcome=f"{len(attempts)} live attempt(s) to resolve")
        )
        for attempt in attempts:
            with call_context(
                CallContext(
                    campaign_id=attempt.campaign_id,
                    prospect_id=attempt.prospect_id,
                    attempt_id=attempt.id,
                    call_id=attempt.telephony_call_id,
                    provider=attempt.telephony_provider,
                )
            ):
                try:
                    await self._resolve(attempt, report)
                except Exception:  # noqa: BLE001 - one bad row must not stop the pass
                    logger.exception(event("recovery.error", attempt=attempt.id))
                    report.notes.append(f"attempt {attempt.id} raised during recovery")
        logger.info(event("recovery.finished", outcome=report.describe()))
        return report

    async def _resolve(self, attempt: CallAttempt, report: RecoveryReport) -> None:
        """Resolve one attempt."""
        if attempt.telephony_call_id:
            await self._reconcile_known_call(attempt, report)
        elif attempt.placement_started_at is not None or attempt.status is CallAttemptStatus.UNRESOLVED:
            await self._resolve_ambiguous(attempt, report)
        else:
            await self._release_unplaced(attempt, report)

    async def _reconcile_known_call(self, attempt: CallAttempt, report: RecoveryReport) -> None:
        """The placement succeeded, so the carrier knows the answer. Ask it."""
        if self._provider is None:
            report.left += 1
            logger.warning(
                event(
                    "recovery.no_provider",
                    outcome="left live",
                    error="no carrier credentials, so this call cannot be reconciled",
                )
            )
            return

        snapshot = await self._fetch(attempt.telephony_call_id or "")
        if snapshot is None:
            report.left += 1
            return

        if not snapshot.status.is_final:
            # Still up according to the carrier. Someone else may be on the
            # line right now, so hanging it up is not ours to decide here.
            report.left += 1
            logger.info(
                event("recovery.still_live", outcome=snapshot.status.value, call=snapshot.call_id)
            )
            return

        await self._apply(attempt, snapshot, report)

    async def _resolve_ambiguous(self, attempt: CallAttempt, report: RecoveryReport) -> None:
        """The placement never reported. Find out from the carrier whether it happened."""
        prospect = await self._prospect_number(attempt)
        since = (attempt.placement_started_at or attempt.created_at or datetime.now(UTC)) - timedelta(
            seconds=30
        )

        if self._provider is None or not prospect:
            await self._close_unknown(
                attempt,
                report,
                "placement never reported an outcome and it cannot be looked up "
                + ("(no carrier credentials)" if self._provider is None else "(no number on file)"),
            )
            return

        try:
            found = await call_with_retry(
                lambda: self._provider.find_recent_calls(prospect, since=since),
                policy=READ_POLICY,
                classify=read_classifier,
                name=f"find_recent_calls({prospect})",
            )
        except NotImplementedError:
            await self._close_unknown(
                attempt,
                report,
                f"placement never reported an outcome and {self._provider.name} cannot list "
                f"calls; check the carrier's log for {prospect} by hand",
            )
            return
        except TelephonyError as exc:
            report.left += 1
            logger.warning(event("recovery.search_failed", error=str(exc), outcome="left live"))
            return

        if not found:
            # A definite answer: the carrier has no call to this number since
            # the placement began, so no phone rang. Safe to close and let the
            # campaign's retry policy decide about trying again.
            report.no_call += 1
            logger.info(event("recovery.no_call_placed", outcome="closed as failed"))
            await self._close(
                attempt,
                report,
                CallAttemptStatus.FAILED,
                "the placement request never reported an outcome, and the carrier has no "
                "call to this number: nothing was dialled",
            )
            return

        # A call does exist. Adopt it, so the attempt now has a call id and can
        # be reconciled like any other — and, crucially, so nothing tries to
        # place a second one.
        snapshot = found[0]
        report.found_calls += 1
        logger.warning(
            event(
                "recovery.call_found",
                call=snapshot.call_id,
                outcome=snapshot.status.value,
                error="the ambiguous placement had in fact created a call",
            )
        )
        try:
            await self._service.store.mark_attempt_placed(
                attempt.id,
                telephony_call_id=snapshot.call_id,
                provider=snapshot.provider,
            )
        except CampaignStoreError as exc:
            report.left += 1
            logger.error(event("recovery.adopt_failed", error=str(exc)))
            return

        if snapshot.status.is_final:
            await self._apply(attempt, snapshot, report)
        else:
            report.left += 1

    async def _release_unplaced(self, attempt: CallAttempt, report: RecoveryReport) -> None:
        """Reserved but never dialled. Nothing happened, so give it back.

        Phase 13: handed back to the queue rather than closed as failed. A
        failed attempt is never retried, so the old close exhausted a prospect
        nobody had phoned — a job lost to a restart. The store undoes the
        reservation only while the row is in the never-placed shape; if it is
        not, the attempt is closed as before, with the reason on the row.
        """
        report.released += 1
        try:
            undone = await self._service.unreserve(attempt)
        except CampaignStoreError as exc:
            logger.warning(event("recovery.unreserve_failed", error=str(exc)))
            undone = False
        if undone:
            logger.info(
                event(
                    "recovery.unreserved",
                    outcome="handed back to the queue",
                    error="reserved before a restart and never dialled",
                )
            )
            return
        logger.info(
            event(
                "recovery.released",
                outcome="never placed",
                error="reserved before a restart and never dialled",
            )
        )
        await self._close(
            attempt,
            report,
            CallAttemptStatus.FAILED,
            "reserved but never placed; released by recovery after a restart",
            counted=False,
        )

    async def _apply(
        self, attempt: CallAttempt, snapshot: CallSnapshot, report: RecoveryReport
    ) -> None:
        """Write a carrier outcome onto the attempt and move its membership on."""
        mapped = _STATUS_MAP.get(snapshot.status)
        if mapped is None:
            report.left += 1
            return
        # Phase 12: a completed call the carrier's own detection says a
        # machine answered is a voicemail, not a conversation. The same rule
        # `dialer.machine_status` applies, restated here for the reason the
        # status map is: this module resolves calls the dialer never saw.
        if mapped is CallAttemptStatus.COMPLETED and snapshot.machine_answered:
            mapped = CallAttemptStatus.VOICEMAIL
        updated, applied = await self._service.store.apply_call_event(
            attempt_id=attempt.id,
            status=mapped,
            telephony_call_id=snapshot.call_id,
            duration_seconds=int(snapshot.duration_secs) if snapshot.duration_secs else None,
            failure_reason=snapshot.error_message,
        )
        if applied and updated is not None:
            # The membership still has to be moved on, and the result written.
            # `record_outcome` owns both, and re-applying the status it already
            # has is harmless because `update_attempt_status` is idempotent for
            # an unchanged value.
            await self._service.record_outcome(
                updated,
                mapped,
                failure_reason=snapshot.error_message,
                duration_seconds=int(snapshot.duration_secs) if snapshot.duration_secs else None,
            )
        report.resolved += 1
        logger.info(event("recovery.reconciled", outcome=mapped.value, call=snapshot.call_id))

    async def _close_unknown(
        self, attempt: CallAttempt, report: RecoveryReport, reason: str
    ) -> None:
        """Close an attempt whose fate cannot be established. Says so on the row."""
        logger.error(event("recovery.unresolvable", error=reason, outcome="closed as failed"))
        report.notes.append(f"attempt {attempt.id}: {reason}")
        await self._close(attempt, report, CallAttemptStatus.FAILED, reason)

    async def _close(
        self,
        attempt: CallAttempt,
        report: RecoveryReport,
        status: CallAttemptStatus,
        reason: str,
        *,
        counted: bool = True,
    ) -> None:
        """Record a final status for an attempt, moving its membership on."""
        try:
            await self._service.record_outcome(attempt, status, failure_reason=reason)
        except CampaignStoreError as exc:
            report.left += 1
            logger.error(event("recovery.close_failed", error=str(exc)))
            return
        if counted:
            report.failed += 1

    async def _fetch(self, call_id: str) -> CallSnapshot | None:
        """Read one call from the carrier, or None when it cannot be read."""
        try:
            return await call_with_retry(
                lambda: self._provider.fetch_call(call_id),
                policy=READ_POLICY,
                classify=read_classifier,
                name=f"fetch_call({call_id})",
            )
        except TelephonyError as exc:
            logger.warning(event("recovery.fetch_failed", call=call_id, error=str(exc)))
            return None

    async def _prospect_number(self, attempt: CallAttempt) -> str | None:
        """The number this attempt would have dialled, for the carrier search."""
        try:
            prospect = await self._service.store.get_prospect(attempt.prospect_id)
        except CampaignStoreError:
            return None
        return prospect.phone_normalized if prospect else None


__all__ = ["AttemptRecovery", "RecoveryReport"]
