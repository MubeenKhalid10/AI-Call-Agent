"""The outbox deliverer: each fact, told to the automation platform once. Phase 17.

The shape is Phase 15's `CrmSyncer`, for the same reasons. A delivery to n8n
is a write to somebody else's system: it must happen once, from a process of
its own, after the fact it describes is durable, and a failure must be
retried or closed with a reason — never repeated blindly.

```
the bot / the worker  --write-->  call_results, meetings, callbacks, campaigns
                                            |
        automation.py  --claim-->  automation_events  (one row per fact, keyed on what it is)
                       --POST-->   n8n's webhook URL   (signed; the same event_id on every redelivery)
```

**Where the events come from.** `store.claim_automation_events` creates them
from the rows in SQL, under unique keys, so this module never decides *what*
happened — only how to say it. A call result must have been unchanged for
`settle_secs` before its events exist: the carrier's thin result and the
conversation's rich one land seconds apart, and the settle window is what
makes `call.completed` carry the rich one.

**What is sent.** One JSON object per event: `event_id`, `event`,
`occurred_at`, and the rows it concerns — the prospect, the campaign, the
call, the result (transcript on request), the transfers, the meeting or the
callback — in the shapes `serialize.py` gives the API. The receiver can
de-duplicate on `event_id` (a redelivery carries the same one) and needs no
second request to act.

**How it is sent.** A POST with `Content-Type: application/json`, the
`X-Aiva-Event` / `X-Aiva-Event-Id` / `X-Aiva-Delivery` / `X-Aiva-Timestamp`
headers, `X-Aiva-Signature` when a secret is configured, and the static auth
header when one is. 2xx is delivered. A timeout, a connection failure,
408/425/429/5xx, and 404 (n8n answers 404 for a workflow that is not active
yet) are transient: the row backs off from `retry_secs`, doubling to
`max_retry_secs`, honouring `Retry-After`, for up to `max_attempts` passes,
then is `FAILED` with the reason. Any other 4xx is a refusal on the merits
and is `FAILED` at once. `campaign.py events-retry` reopens what a person
has fixed.

**Not in the audio path.** Nothing here is imported by `bot.py` or by
anything under `src/conversation/`; `tests/test_automation.py` asserts it.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from loguru import logger

from ..campaigns.models import AutomationEvent, AutomationEventState
from ..campaigns.store import CampaignStoreError
from ..monitoring.instruments import AUTOMATION_DELIVERIES, AUTOMATION_LATENCY
from ..reliability.observability import CallContext, Timer, call_context
from ..reliability.observability import event as log_event
from ..reliability.retry import jittered
from .auth import SIGNATURE_HEADER, sign
from .serialize import (
    attempt_dict,
    callback_dict,
    campaign_dict,
    meeting_dict,
    prospect_dict,
    result_dict,
    transfer_dict,
)

#: The headers on every delivery, beside the signature.
EVENT_HEADER = "X-Aiva-Event"
EVENT_ID_HEADER = "X-Aiva-Event-Id"
DELIVERY_HEADER = "X-Aiva-Delivery"
TIMESTAMP_HEADER = "X-Aiva-Timestamp"
USER_AGENT = "Ai-Voice-Agent-automation/17"

#: Statuses worth another try. 404 is here for n8n: a workflow that is not
#: active yet answers 404 with "webhook … is not registered", and activating
#: it is the fix — the event should still arrive once somebody does.
TRANSIENT_STATUSES = frozenset({404, 408, 425, 429})

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class SendResult:
    """What one POST came back with.

    Attributes:
        status: The HTTP status, or None when there was no response at all —
            a timeout, a refused connection, a DNS failure.
        body: The first part of the response body, for a log line.
        retry_after: The receiver's `Retry-After`, in seconds, when it sent one.
        error: What went wrong when there was no response.
    """

    status: int | None
    body: str = ""
    retry_after: float | None = None
    error: str | None = None

    @property
    def delivered(self) -> bool:
        """Whether the receiver said it took the event."""
        return self.status is not None and 200 <= self.status < 300

    @property
    def transient(self) -> bool:
        """Whether another try could reasonably succeed."""
        return self.status is None or self.status in TRANSIENT_STATUSES or self.status >= 500

    def describe(self) -> str:
        """One line for the row's `last_error`."""
        if self.status is None:
            return self.error or "no response"
        summary = self.body.strip().replace("\n", " ")[:160]
        return f"HTTP {self.status}" + (f": {summary}" if summary else "")


#: A function that performs one delivery: `(url, body, headers) -> SendResult`.
#: The checks script one; production uses `AiohttpSender`.
Sender = Callable[[str, bytes, dict[str, str]], Awaitable[SendResult]]


class AiohttpSender:
    """The real sender: one `aiohttp` session for the life of the deliverer."""

    def __init__(self, *, timeout_secs: float = 15.0, session: aiohttp.ClientSession | None = None) -> None:
        self._timeout = aiohttp.ClientTimeout(total=max(1.0, timeout_secs))
        self._session = session
        self._owns_session = session is None

    async def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> SendResult:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        try:
            async with self._session.post(url, data=body, headers=headers) as response:
                text = await response.text(errors="replace")
                return SendResult(
                    status=response.status,
                    body=text[:1000],
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                )
        except TimeoutError:
            return SendResult(status=None, error=f"timed out after {self._timeout.total:g}s")
        except aiohttp.ClientError as exc:
            return SendResult(status=None, error=f"{exc.__class__.__name__}: {exc}")

    async def close(self) -> None:
        """Close the session, if this sender opened it."""
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None  # An HTTP-date; not worth parsing for a backoff floor.


# --- The payload ------------------------------------------------------------------


async def build_payload(
    store: Any, event: AutomationEvent, *, include_transcript: bool = False
) -> tuple[dict[str, Any], datetime | None]:
    """Everything the receiver needs to act, read fresh from the rows.

    Read at delivery rather than at creation, so an event that waited (the
    receiver was down) carries the rows as they are now. Optional tables that
    a database predating their phase lacks — transfers, meetings, callbacks —
    are tolerated and reported as absent.

    Returns:
        `(payload, result_updated_at)` — the second is the result's
        `updated_at` when a result was read, for the row's bookkeeping.
    """
    prospect = await store.get_prospect(event.prospect_id) if event.prospect_id else None
    campaign = await store.get_campaign(event.campaign_id) if event.campaign_id else None
    attempt = await store.get_attempt(event.call_attempt_id) if event.call_attempt_id else None
    result = None
    if event.call_attempt_id and event.kind != "campaign.completed":
        result = await _quiet(store.get_call_result(event.call_attempt_id))
    transfers: list[Any] = []
    if event.call_attempt_id:
        transfers = await _quiet(store.list_transfers(call_attempt_id=event.call_attempt_id)) or []
    meeting = await _quiet(store.get_meeting(event.meeting_id)) if event.meeting_id else None
    callback = await _quiet(store.get_callback(event.callback_id)) if event.callback_id else None

    payload: dict[str, Any] = {
        "event_id": event.event_key,
        "event": event.kind,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "sequence": event.id,
        "prospect": prospect_dict(prospect) if prospect else None,
        "campaign": campaign_dict(campaign) if campaign else None,
        "call": attempt_dict(attempt) if attempt else None,
        "result": result_dict(result, include_transcript=include_transcript) if result else None,
        "transfers": [transfer_dict(t) for t in transfers],
    }
    if event.kind == "meeting.booked":
        payload["meeting"] = meeting_dict(meeting) if meeting else None
    if event.kind == "callback.scheduled":
        payload["callback"] = callback_dict(callback) if callback else None
    if event.kind == "campaign.completed" and campaign is not None:
        counts = await _quiet(store.campaign_counts(campaign.id))
        if counts is not None:
            payload["campaign"] = campaign_dict(campaign, counts=counts)
    return payload, (result.updated_at if result else None)


async def _quiet(operation: Awaitable[Any]) -> Any:
    """Await a read against a table that may be missing; None when it is."""
    try:
        return await operation
    except CampaignStoreError as exc:
        if "does not exist" in str(exc):
            return None
        raise


def encode_payload(payload: dict[str, Any]) -> bytes:
    """The bytes that are sent and signed. Compact, UTF-8, keys in insertion order."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


# --- The deliverer ------------------------------------------------------------------


@dataclass
class DeliveryReport:
    """What one pass did."""

    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0
    released: int = 0
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """One line for the CLI and the log."""
        if not self.claimed:
            return "nothing to deliver"
        parts = [f"{self.claimed} claimed", f"{self.delivered} delivered"]
        if self.retried:
            parts.append(f"{self.retried} to retry")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.released:
            parts.append(f"{self.released} released")
        return ", ".join(parts)


@dataclass
class DeliveryTotals:
    """The counters over a whole run."""

    passes: int = 0
    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0
    released: int = 0

    def add(self, report: DeliveryReport) -> None:
        self.passes += 1
        self.claimed += report.claimed
        self.delivered += report.delivered
        self.retried += report.retried
        self.failed += report.failed
        self.skipped += report.skipped
        self.released += report.released

    def describe(self) -> str:
        return (
            f"{self.passes} pass(es), {self.claimed} claimed, {self.delivered} delivered, "
            f"{self.retried} retried, {self.failed} failed, {self.skipped} skipped"
        )

    def snapshot(self) -> dict[str, int]:
        return {
            "passes": self.passes,
            "claimed": self.claimed,
            "delivered": self.delivered,
            "retried": self.retried,
            "failed": self.failed,
            "skipped": self.skipped,
            "released": self.released,
        }


class EventDeliverer:
    """Claims due events and POSTs each to its target, once."""

    def __init__(
        self,
        store: Any,
        *,
        targets: dict[str, str],
        secret: str | None = None,
        auth_header: str | None = None,
        auth_token: str | None = None,
        sender: Sender | None = None,
        timeout_secs: float = 15.0,
        max_attempts: int = 12,
        retry_secs: float = 30.0,
        max_retry_secs: float = 1800.0,
        batch: int = 20,
        stale_secs: float = 600.0,
        settle_secs: float = 30.0,
        since: datetime | None = None,
        include_transcript: bool = False,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        """Create the deliverer.

        Args:
            store: The campaign store — the outbox and the rows the payloads
                are built from. Typed loosely so the checks can hand in a fake.
            targets: Which event kinds to deliver, and where. Only these
                kinds are created and claimed.
            secret: Signs every delivery when set.
            auth_header / auth_token: A static header sent on every delivery.
            sender: Performs the POST. `AiohttpSender` by default.
            timeout_secs: Per request, for the default sender.
            max_attempts: Passes an event may be claimed for before it is
                closed as failed.
            retry_secs / max_retry_secs: The backoff after a transient failure.
            batch: Events claimed per pass.
            stale_secs: A row left `DELIVERING` this long belongs to a
                deliverer that died; it is claimed again.
            settle_secs / since: Passed to the claim — see the store.
            include_transcript: Whether `call.*` payloads carry the transcript.
            clock / sleep: Injected by the checks.
        """
        self._store = store
        self._targets = dict(targets)
        self._kinds = tuple(self._targets)
        self._secret = secret
        self._auth_header = auth_header
        self._auth_token = auth_token
        self._own_sender = sender is None
        self._sender: Sender = sender or AiohttpSender(timeout_secs=timeout_secs)
        self._max_attempts = max(1, max_attempts)
        self._retry_secs = max(0.0, retry_secs)
        self._max_retry_secs = max(self._retry_secs, max_retry_secs)
        self._batch = max(1, batch)
        self._stale_secs = max(0.0, stale_secs)
        self._settle_secs = max(0.0, settle_secs)
        self._since = since
        self._include_transcript = include_transcript
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._stopping = False
        self._wake = asyncio.Event()
        self.totals = DeliveryTotals()

    @property
    def targets(self) -> dict[str, str]:
        """Where each kind goes."""
        return dict(self._targets)

    def now(self) -> datetime:
        """The current moment, from the injected clock."""
        return self._clock()

    def request_stop(self) -> None:
        """End the run after the current pass. Safe from a signal handler."""
        self._stopping = True
        self._wake.set()

    async def close(self) -> None:
        """Release the sender's connections, if this deliverer made them."""
        closer = getattr(self._sender, "close", None)
        if self._own_sender and callable(closer):
            await closer()

    # --- Running ------------------------------------------------------------------------

    async def run(self, *, poll_secs: float = 10.0, once: bool = False) -> DeliveryTotals:
        """Deliver until stopped, sleeping `poll_secs` between passes that found nothing."""
        logger.info(
            log_event(
                "automation.deliverer_started",
                outcome=f"{len(self._kinds)} kind(s), batch {self._batch}, up to {self._max_attempts} attempts, "
                f"retry from {self._retry_secs:g}s, settle {self._settle_secs:g}s",
            )
        )
        try:
            while not self._stopping:
                report = await self.run_once()
                if once or self._stopping:
                    break
                await self._pause(0.0 if report.claimed else poll_secs)
        finally:
            logger.info(log_event("automation.deliverer_stopped", outcome=self.totals.describe()))
        return self.totals

    async def run_once(self) -> DeliveryReport:
        """Claim a batch and deliver each event. Never raises for a bad row."""
        report = DeliveryReport()
        now = self.now()
        try:
            claimed = await self._store.claim_automation_events(
                self._kinds,
                limit=self._batch,
                now=now,
                settle_secs=self._settle_secs,
                stale_secs=self._stale_secs,
                since=self._since,
            )
        except CampaignStoreError as exc:
            logger.error(log_event("automation.claim_failed", error=(str(exc).splitlines() or [type(exc).__name__])[0]))
            report.notes.append((str(exc).splitlines() or [type(exc).__name__])[0])
            self.totals.add(report)
            return report

        report.claimed = len(claimed)
        for item in claimed:
            if self._stopping:
                await self._release(item, "stopping", report)
                continue
            context = CallContext(
                campaign_id=item.campaign_id,
                prospect_id=item.prospect_id,
                attempt_id=item.call_attempt_id,
                extra={"event": item.event_key},
            )
            with call_context(context):
                try:
                    await self._deliver(item, report)
                except CampaignStoreError as exc:
                    logger.error(log_event("automation.store_unavailable", error=(str(exc).splitlines() or [type(exc).__name__])[0]))
                    report.notes.append(f"event {item.id}: {exc}")
                except Exception as exc:  # noqa: BLE001 - one row must not stop the pass
                    logger.exception(log_event("automation.delivery_crashed", error=str(exc)))
                    await self._retry_later(item, f"unexpected error: {exc.__class__.__name__}: {exc}", report)
        if report.claimed:
            logger.info(log_event("automation.pass", outcome=report.describe()))
        self.totals.add(report)
        return report

    # --- One event ----------------------------------------------------------------------

    async def _deliver(self, item: AutomationEvent, report: DeliveryReport) -> None:
        url = self._targets.get(item.kind)
        if url is None:
            await self._store.record_automation_event(
                item.id, state=AutomationEventState.SKIPPED, error="no target URL for this kind"
            )
            report.skipped += 1
            AUTOMATION_DELIVERIES.inc(kind=item.kind, outcome="skipped")
            logger.info(log_event("automation.skipped", outcome=item.kind, error="no target URL"))
            return

        timer = Timer()
        try:
            payload, result_updated_at = await build_payload(
                self._store, item, include_transcript=self._include_transcript
            )
        except CampaignStoreError as exc:
            # The rows could not be read; nothing was sent. Hand the row back
            # without spending an attempt — this is not the receiver's fault.
            await self._release(item, f"could not build the payload: {(str(exc).splitlines() or [type(exc).__name__])[0]}", report)
            return

        # Phase 22: the call's correlation id rides in the payload; bind it
        # here so the delivery's lines join the call's.
        call = payload.get("call") if isinstance(payload, dict) else None
        trace_id = call.get("trace_id") if isinstance(call, dict) else None
        with call_context(trace_id=trace_id):
            await self._send(item, url, payload, result_updated_at, report, timer)

    async def _send(
        self,
        item: AutomationEvent,
        url: str,
        payload: dict[str, Any],
        result_updated_at: Any,
        report: DeliveryReport,
        timer: Timer,
    ) -> None:
        """POST one built payload and record what came back. See `_deliver`."""
        body = encode_payload(payload)
        headers = self._headers(item, body)
        sent = await self._sender(url, body, headers)
        AUTOMATION_LATENCY.observe(timer.elapsed_secs)

        if sent.delivered:
            await self._store.record_automation_event(
                item.id,
                state=AutomationEventState.DELIVERED,
                error=None,
                status_code=sent.status,
                delivered_at=self.now(),
                payload=payload,
                target_url=url,
                result_updated_at=result_updated_at,
            )
            report.delivered += 1
            AUTOMATION_DELIVERIES.inc(kind=item.kind, outcome="delivered")
            logger.info(
                log_event(
                    "automation.delivered",
                    outcome=item.kind,
                    provider=_host(url),
                    latency_ms=timer.elapsed_ms,
                    retries=item.attempts - 1,
                )
            )
            return

        # Kept for the operator either way: what was sent, and where.
        await self._store.record_automation_event(
            item.id, status_code=sent.status, payload=payload, target_url=url, result_updated_at=result_updated_at
        )
        if sent.transient:
            await self._retry_later(item, sent.describe(), report, retry_after=sent.retry_after)
        else:
            await self._fail(item, sent.describe(), report)

    def _headers(self, item: AutomationEvent, body: bytes) -> dict[str, str]:
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
            EVENT_HEADER: item.kind,
            EVENT_ID_HEADER: item.event_key,
            DELIVERY_HEADER: str(item.attempts),
            TIMESTAMP_HEADER: str(timestamp),
        }
        if self._secret:
            headers[SIGNATURE_HEADER] = sign(self._secret, body, timestamp=timestamp)
        if self._auth_header and self._auth_token:
            headers[self._auth_header] = self._auth_token
        return headers

    async def _retry_later(
        self, item: AutomationEvent, error: str, report: DeliveryReport, *, retry_after: float | None = None
    ) -> None:
        """Schedule another attempt, or close the row once the attempts are spent."""
        if item.attempts >= self._max_attempts:
            await self._fail(item, f"{error} (after {item.attempts} attempts)", report)
            return
        wait = self._backoff(item.attempts, retry_after)
        due = self.now() + timedelta(seconds=wait)
        await self._store.record_automation_event(
            item.id, state=AutomationEventState.RETRY, error=error, next_attempt_at=due
        )
        report.retried += 1
        AUTOMATION_DELIVERIES.inc(kind=item.kind, outcome="retry")
        logger.warning(
            log_event(
                "automation.retry_scheduled",
                outcome=item.kind,
                error=error,
                retries=item.attempts,
                latency_ms=None,
            )
            + f" next_in={wait:.0f}s of {self._max_attempts}"
        )

    async def _fail(self, item: AutomationEvent, error: str, report: DeliveryReport) -> None:
        await self._store.record_automation_event(item.id, state=AutomationEventState.FAILED, error=error)
        report.failed += 1
        AUTOMATION_DELIVERIES.inc(kind=item.kind, outcome="failed")
        logger.error(
            log_event(
                "automation.failed",
                outcome=item.kind,
                error=error,
            )
            + " ; closed — `campaign.py events-retry` reopens it"
        )

    async def _release(self, item: AutomationEvent, error: str, report: DeliveryReport) -> None:
        """Hand a claimed row back, due after one retry interval, without spending an attempt."""
        due = self.now() + timedelta(seconds=self._backoff(1, None))
        await self._store.record_automation_event(
            item.id,
            state=AutomationEventState.RETRY,
            error=error,
            next_attempt_at=due,
            attempts=max(0, item.attempts - 1),
        )
        report.released += 1
        AUTOMATION_DELIVERIES.inc(kind=item.kind, outcome="released")

    def _backoff(self, attempts: int, retry_after: float | None) -> float:
        """Seconds until the next try: exponential from `retry_secs`, capped, jittered.

        A `Retry-After` the receiver sent is a floor, applied *after* the
        jitter: the receiver said "not before", and a jitter that landed
        under it would be a request it had asked not to get.
        """
        raw = min(self._retry_secs * (2 ** max(0, attempts - 1)), self._max_retry_secs)
        wait = jittered(raw) if raw > 0 else 0.0
        if retry_after is not None:
            wait = max(wait, retry_after)
        return wait

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


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc or url


__all__ = [
    "DELIVERY_HEADER",
    "EVENT_HEADER",
    "EVENT_ID_HEADER",
    "TIMESTAMP_HEADER",
    "TRANSIENT_STATUSES",
    "USER_AGENT",
    "AiohttpSender",
    "DeliveryReport",
    "DeliveryTotals",
    "EventDeliverer",
    "SendResult",
    "Sender",
    "build_payload",
    "encode_payload",
]
