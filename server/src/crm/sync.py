"""The syncer: every finished call's result, filed with the CRM once. Phase 15.

**Architecture: agent → database → CRM adapter.** The bot writes a `CallResult`
at the end of a call, as it has since Phase 8, and knows nothing about a CRM.
This module runs in *its own process* (`uv run campaign.py crm-sync`), claims
results the CRM has not seen, sends each one through a `CrmProvider`, and
records what happened on a `crm_sync` row. Nothing here touches a session, a
pipeline or a turn; a slow CRM cannot add a millisecond to a call.

**Idempotent, at three layers.**

1. One `crm_sync` row per result, unique on `call_result_id`, claimed with
   `FOR UPDATE SKIP LOCKED` — so two syncers never file the same result, and a
   result is not claimed again once it is `SYNCED`, until the *result itself*
   changes (the conversation's rich result replacing the carrier's thin one),
   when it is claimed once more and the existing activity is *updated*.
2. The CRM ids are recorded the moment they are known, so a crash between the
   contact and the activity resumes with the contact, not with a duplicate.
3. A create whose answer was lost — a timeout, a dropped connection — is not
   repeated. The key `mapping.sync_key` puts in the activity is looked up
   first (`find_activity`), and only a miss creates. The same shape as Phase
   9's ambiguous placement, for the same reason.

**Retries are bounded and backed off.** A transient failure — the CRM down,
rate-limited, a timeout — schedules the row for `retry_secs × 2^n`, capped and
jittered, up to `max_attempts`, then `FAILED` with the CRM's words on the row
for a person. A refusal on the merits (a bad value, a missing property) is
`FAILED` at once: retrying reproduces it. A rejected *token* stops the pass:
it is not a fact about one record, and marking a hundred rows failed for one
expired key would be wrong in the expensive direction.

**What is sent** is `mapping.build_call_sync`'s reading of the result and the
prospect: contact identity, the call, and every structured fact. The syncer
adds nothing; it only decides *when* and *again*.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from ..campaigns.models import CrmSyncRecord, CrmSyncState
from ..campaigns.results import CallResult
from ..campaigns.store import CampaignStoreError
from ..monitoring.instruments import CRM_LATENCY, CRM_SYNCS
from ..reliability.observability import CallContext, Timer, call_context, event
from ..reliability.retry import (
    READ_POLICY,
    AmbiguousOutcomeError,
    RetryPolicy,
    call_with_retry,
    jittered,
    read_classifier,
    write_classifier,
)
from .base import (
    CallSync,
    CrmAuthError,
    CrmError,
    CrmProvider,
    CrmRejectedError,
    CrmUnavailableError,
    SyncReceipt,
)
from .mapping import build_call_sync

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]

#: A CRM write is never retried inside a pass: neither HubSpot nor its peers
#: take an idempotency key, so a repeat could file the call twice. One
#: attempt, bounded; an ambiguous answer becomes a scheduled retry that
#: searches before it creates.
WRITE_POLICY = RetryPolicy(attempts=1, timeout_secs=30.0)


@dataclass
class SyncReport:
    """What one pass did.

    Attributes:
        claimed: Rows handed to this pass.
        synced: Filed with the CRM, and recorded so.
        retried: Scheduled for another attempt after a transient failure.
        failed: Closed as failed — refused on the merits, or out of attempts.
        skipped: Not sent, by policy (an unanswered call, when configured so).
        stopped: Why the pass ended early, when it did — a rejected token.
    """

    claimed: int = 0
    synced: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0
    stopped: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def touched(self) -> int:
        """Rows this pass wrote an outcome for."""
        return self.synced + self.retried + self.failed + self.skipped

    def describe(self) -> str:
        """One line for the CLI and the log."""
        if not self.claimed and self.stopped is None:
            return "nothing to sync"
        parts = [f"{self.claimed} claimed", f"{self.synced} synced"]
        if self.retried:
            parts.append(f"{self.retried} to retry")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.stopped:
            parts.append(f"stopped: {self.stopped}")
        return ", ".join(parts)


@dataclass
class SyncTotals:
    """The counters over a whole run."""

    passes: int = 0
    claimed: int = 0
    synced: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0

    def add(self, report: SyncReport) -> None:
        self.passes += 1
        self.claimed += report.claimed
        self.synced += report.synced
        self.retried += report.retried
        self.failed += report.failed
        self.skipped += report.skipped

    def describe(self) -> str:
        return (
            f"{self.passes} pass(es), {self.claimed} claimed, {self.synced} synced, "
            f"{self.retried} retried, {self.failed} failed, {self.skipped} skipped"
        )


class CrmSyncer:
    """Claims unsynced results and files each one with the CRM, once."""

    def __init__(
        self,
        store: Any,
        provider: CrmProvider,
        *,
        from_number: str | None = None,
        max_attempts: int = 8,
        retry_secs: float = 60.0,
        max_retry_secs: float = 3600.0,
        sync_unanswered: bool = True,
        batch: int = 20,
        stale_secs: float = 900.0,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        """Create the syncer.

        Args:
            store: The campaign store — results, prospects, and the `crm_sync`
                rows. Typed loosely so the checks can hand in a fake.
            provider: The CRM. Never named here.
            from_number: The caller ID calls were placed from, for the
                activity's "from" field.
            max_attempts: Passes a result may be claimed for before it is
                closed as failed.
            retry_secs: Wait after the first transient failure; doubles each
                time up to `max_retry_secs`, jittered.
            sync_unanswered: Whether calls nobody answered are filed too. On
                by default: a CRM's call log is the record of every attempt.
            batch: Rows claimed per pass.
            stale_secs: A row left `SYNCING` this long belongs to a syncer
                that died; it is claimed again.
            clock / sleep: Injected by the checks.
        """
        self._store = store
        self._provider = provider
        self._from_number = from_number
        self._max_attempts = max(1, max_attempts)
        self._retry_secs = max(0.0, retry_secs)
        self._max_retry_secs = max(self._retry_secs, max_retry_secs)
        self._sync_unanswered = sync_unanswered
        self._batch = max(1, batch)
        self._stale_secs = max(0.0, stale_secs)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._schema_checked = False
        self._stopping = False
        self._wake = asyncio.Event()
        self.totals = SyncTotals()

    def now(self) -> datetime:
        """The current moment, from the injected clock."""
        return self._clock()

    def request_stop(self) -> None:
        """End the run after the current pass. Safe from a signal handler."""
        self._stopping = True
        self._wake.set()

    # --- Running ---------------------------------------------------------------------

    async def run(self, *, poll_secs: float = 15.0, once: bool = False) -> SyncTotals:
        """Sync until stopped, sleeping `poll_secs` between passes that found nothing."""
        logger.info(
            event(
                "crm.sync_started",
                provider=self._provider.name,
                outcome=f"batch {self._batch}, up to {self._max_attempts} attempts, "
                f"retry from {self._retry_secs:g}s",
            )
        )
        try:
            while not self._stopping:
                report = await self.run_once()
                if once or self._stopping:
                    break
                if report.stopped is not None:
                    # A rejected token: nothing will succeed until a person
                    # fixes it. Wait the retry interval, not the poll interval.
                    wait = max(poll_secs, self._retry_secs)
                elif report.claimed:
                    wait = 0.0  # There may be more; ask again at once.
                else:
                    wait = poll_secs
                await self._pause(wait)
        finally:
            logger.info(event("crm.sync_stopped", outcome=self.totals.describe()))
        return self.totals

    async def run_once(self) -> SyncReport:
        """Claim a batch and sync each row. Never raises for a bad row."""
        report = SyncReport()
        now = self.now()
        try:
            claimed = await self._store.claim_results_for_sync(
                self._provider.name, limit=self._batch, now=now, stale_secs=self._stale_secs
            )
        except CampaignStoreError as exc:
            logger.error(event("crm.claim_failed", error=(str(exc).splitlines() or [type(exc).__name__])[0]))
            report.notes.append((str(exc).splitlines() or [type(exc).__name__])[0])
            self.totals.add(report)
            return report

        report.claimed = len(claimed)
        if claimed:
            await self._ensure_schema()
        for result, record in claimed:
            if report.stopped is not None:
                # The token was rejected mid-pass; hand the rest back untouched
                # except for the retry stamp, so nothing is lost.
                await self._release(record, report.stopped)
                continue
            with call_context(CallContext(attempt_id=result.call_attempt_id, prospect_id=result.prospect_id, extra={"result": result.id, "crm": self._provider.name})):
                try:
                    await self._sync(result, record, report)
                except CampaignStoreError as exc:
                    logger.error(event("crm.store_unavailable", error=(str(exc).splitlines() or [type(exc).__name__])[0]))
                    report.notes.append(f"result {result.id}: {exc}")
                except Exception as exc:  # noqa: BLE001 - one row must not stop the pass
                    logger.exception(event("crm.sync_crashed", error=str(exc)))
                    await self._retry_later(record, f"unexpected error: {exc.__class__.__name__}: {exc}", report)
        if report.claimed:
            logger.info(event("crm.pass", outcome=report.describe()))
        self.totals.add(report)
        return report

    # --- One result --------------------------------------------------------------------

    async def _sync(self, result: CallResult, record: CrmSyncRecord, report: SyncReport) -> None:
        """File one result, and write the outcome onto its row."""
        timer = Timer()
        if not self._sync_unanswered and not result.reached:
            await self._store.record_crm_sync(record.id, state=CrmSyncState.SKIPPED, result_updated_at=result.updated_at)
            report.skipped += 1
            self._count("skipped")
            logger.info(event("crm.skipped", outcome=result.disposition.value, error="unanswered calls are not synced"))
            return

        prospect = await self._store.get_prospect(result.prospect_id)
        if prospect is None:
            await self._fail(record, "the prospect row is gone", report)
            return
        campaign = await self._store.get_campaign(result.campaign_id) if result.campaign_id else None
        attempt = await self._store.get_attempt(result.call_attempt_id)
        payload = build_call_sync(result, prospect, campaign=campaign, attempt=attempt, from_number=self._from_number)
        if not payload.contact.has_identity:
            await self._fail(record, "the prospect has neither a phone number nor an email to match on", report)
            return

        # Phase 22: the call's correlation id, read from its row, on every
        # line the filing writes — the CRM hop of the trace.
        with call_context(trace_id=getattr(attempt, "trace_id", None)):
            await self._sync_traced(payload, result, record, report, timer)

    async def _sync_traced(
        self, payload: CallSync, result: CallResult, record: CrmSyncRecord, report: SyncReport, timer: Timer
    ) -> None:
        """The filing itself, under the call's trace. See `_sync`."""
        try:
            receipt = await self._file(payload, record)
        except CrmAuthError as exc:
            report.stopped = (str(exc).splitlines() or [type(exc).__name__])[0]
            self._count("auth_rejected", timer)
            logger.error(event("crm.auth_rejected", error=report.stopped, outcome="pass stopped; rows kept for retry"))
            await self._release(record, report.stopped)
            return
        except CrmRejectedError as exc:
            await self._fail(record, str(exc), report, timer)
            return
        except (CrmUnavailableError, AmbiguousOutcomeError, TimeoutError, ConnectionError) as exc:
            # A write's failure arrives wrapped as ambiguous; the CRM's own
            # `Retry-After` is on the cause underneath.
            cause = exc.cause if isinstance(exc, AmbiguousOutcomeError) else exc
            await self._retry_later(
                record, (str(exc).splitlines() or [type(exc).__name__])[0], report, retry_after=getattr(cause, "retry_after_secs", None), timer=timer
            )
            return

        await self._store.record_crm_sync(
            record.id,
            state=CrmSyncState.SYNCED,
            external_contact_id=receipt.contact_id,
            external_activity_id=receipt.activity_id,
            synced_at=self.now(),
            result_updated_at=result.updated_at,
            error=None,
        )
        report.synced += 1
        self._count("synced", timer)
        logger.info(
            event(
                "crm.synced",
                outcome=f"{'created' if receipt.created else 'updated'} activity {receipt.activity_id} "
                f"for contact {receipt.contact_id}",
                latency_ms=timer.elapsed_ms,
                attempts=record.attempts,
            )
        )

    async def _file(self, payload: CallSync, record: CrmSyncRecord) -> SyncReceipt:
        """The contact, then the activity — each step recorded before the next.

        Reads are retried (they cannot do anything twice); writes are made
        once, and an answer that never came is raised as ambiguous for the
        next pass to resolve by searching first.
        """
        provider = self._provider
        contact_id = record.external_contact_id
        if not contact_id:
            contact_id = await call_with_retry(
                lambda: provider.find_contact(payload.contact),
                policy=READ_POLICY,
                classify=read_classifier,
                name="crm.find_contact",
            )
            if contact_id:
                logger.info(event("crm.contact_found", outcome=contact_id))
            else:
                contact_id = await call_with_retry(
                    lambda: provider.create_contact(payload.contact),
                    policy=WRITE_POLICY,
                    classify=write_classifier,
                    name="crm.create_contact",
                )
                logger.info(event("crm.contact_created", outcome=contact_id))
            await self._store.record_crm_sync(record.id, external_contact_id=contact_id)

        activity_id = record.external_activity_id
        created = False
        if not activity_id and record.attempts > 1:
            # A previous pass may have created the activity and lost the
            # answer. Ask before creating: the key is in the body.
            activity_id = await call_with_retry(
                lambda: provider.find_activity(payload.activity.key),
                policy=READ_POLICY,
                classify=read_classifier,
                name="crm.find_activity",
            )
            if activity_id:
                logger.info(event("crm.activity_found", outcome=activity_id, error="an earlier pass had created it"))
                await self._store.record_crm_sync(record.id, external_activity_id=activity_id)

        if activity_id:
            await call_with_retry(
                lambda: provider.update_activity(activity_id, contact_id, payload.activity),
                policy=WRITE_POLICY,
                classify=write_classifier,
                name="crm.update_activity",
            )
        else:
            activity_id = await call_with_retry(
                lambda: provider.create_activity(contact_id, payload.activity),
                policy=WRITE_POLICY,
                classify=write_classifier,
                name="crm.create_activity",
            )
            created = True
            await self._store.record_crm_sync(record.id, external_activity_id=activity_id)

        # The contact's own summary of the latest call comes last: a failure
        # here leaves the call filed, and the next pass repeats only this.
        await call_with_retry(
            lambda: provider.update_contact(contact_id, payload.contact, payload.activity),
            policy=WRITE_POLICY,
            classify=write_classifier,
            name="crm.update_contact",
        )
        return SyncReceipt(contact_id=contact_id, activity_id=activity_id, created=created)

    # --- Outcomes ------------------------------------------------------------------------

    def _count(self, outcome: str, timer: Timer | None = None) -> None:
        """One filing's outcome, scrapeable. Phase 22."""
        CRM_SYNCS.inc(provider=self._provider.name, outcome=outcome)
        if timer is not None:
            CRM_LATENCY.observe(timer.elapsed_secs)

    async def _retry_later(
        self,
        record: CrmSyncRecord,
        error: str,
        report: SyncReport,
        *,
        retry_after: float | None = None,
        timer: Timer | None = None,
    ) -> None:
        """Schedule another attempt, or close the row once the attempts are spent."""
        if record.attempts >= self._max_attempts:
            await self._fail(record, f"{error} (after {record.attempts} attempts)", report, timer)
            return
        wait = self._backoff(record.attempts, retry_after)
        due = self.now() + timedelta(seconds=wait)
        await self._store.record_crm_sync(record.id, state=CrmSyncState.RETRY, error=error, next_attempt_at=due)
        report.retried += 1
        self._count("retry", timer)
        logger.warning(
            event(
                "crm.retry_scheduled",
                error=error,
                outcome=f"attempt {record.attempts}/{self._max_attempts}; next in {wait:.0f}s",
            )
        )

    async def _fail(self, record: CrmSyncRecord, error: str, report: SyncReport, timer: Timer | None = None) -> None:
        await self._store.record_crm_sync(record.id, state=CrmSyncState.FAILED, error=error)
        report.failed += 1
        self._count("failed", timer)
        logger.error(event("crm.failed", error=error, outcome="closed; `campaign.py crm-retry` reopens it"))

    async def _release(self, record: CrmSyncRecord, error: str) -> None:
        """Hand a claimed row back, due after one retry interval, without spending an attempt."""
        due = self.now() + timedelta(seconds=self._backoff(1, None))
        await self._store.record_crm_sync(
            record.id, state=CrmSyncState.RETRY, error=error, next_attempt_at=due, attempts=max(0, record.attempts - 1)
        )

    def _backoff(self, attempts: int, retry_after: float | None) -> float:
        """Seconds until the next try: exponential from `retry_secs`, capped, jittered."""
        raw = min(self._retry_secs * (2 ** max(0, attempts - 1)), self._max_retry_secs)
        if retry_after is not None:
            raw = max(raw, retry_after)
        return jittered(raw) if raw > 0 else 0.0

    async def _ensure_schema(self) -> None:
        """Ask the provider for its custom fields once per run; carry on without them if refused."""
        if self._schema_checked:
            return
        self._schema_checked = True
        try:
            await self._provider.ensure_schema()
        except CrmError as exc:
            logger.warning(
                event(
                    "crm.schema_unavailable",
                    error=(str(exc).splitlines() or [type(exc).__name__])[0],
                    outcome="filing calls with standard fields only",
                )
            )
            disable = getattr(self._provider, "disable_custom_properties", None)
            if callable(disable):
                disable()

    async def _pause(self, secs: float) -> None:
        """Wait, unless a stop has been requested. A stop request cuts the wait short."""
        if secs <= 0 or self._stopping:
            return
        if self._sleep is not None:
            await self._sleep(secs)
            return
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=secs)
        except TimeoutError:
            pass


__all__ = ["WRITE_POLICY", "CrmSyncer", "SyncReport", "SyncTotals"]
