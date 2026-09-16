"""Carrier status webhooks: the carrier tells us what happened to a call. Phase 14.

Until this phase the only way to learn a call's fate was to ask — `call.py`
once a second, the worker every `WORKER_POLL_SECS`, recovery on demand — one
carrier request per live call per poll. A carrier will also *tell* you: give it
a URL when the call is placed and it POSTs each lifecycle event as it happens,
signed, numbered and immediate. This module is the receiving end.

**What it is.** One HTTP route, one processor, and the rules that make an
event safe to act on:

1. **Verify.** The provider checks the carrier's signature with the carrier's
   own mechanism (`TelephonyProvider.verify_webhook`). An event that does not
   prove where it came from is refused with a 403 and touches nothing — an
   unverified `completed` would free a prospect who is still on the phone.
2. **Decode.** The provider turns its own field names into a `WebhookEvent`
   (`parse_webhook`), so nothing here knows what a `CallSid` is.
3. **Record.** The delivery is written to the ledger, `telephony_webhook_events`,
   whose `event_key` is unique. A redelivery loses the insert and is answered
   200 without reading the attempt: that is the idempotency, and it lives in
   the database rather than in this process.
4. **Match.** The attempt is found by the carrier's call id — the unique
   `telephony_call_id` Phase 4 put on the row for exactly this — and an event
   for a call this database never placed is `unmatched`, recorded, and answered
   200, because there is nothing for the carrier to retry.
5. **Apply.** The status goes through `store.apply_call_event`, the same
   monotonic write a poll uses: a final status is never overwritten, a live
   one only moves forward. An event that arrives late or twice is `stale` and
   changes nothing. What follows an applied status — the membership, the
   prospect, the call result — is `service.record_outcome`, unchanged.

**What it deliberately is not.** A second source of truth: the attempt row is
the call's state and the ledger only says how it got there. A replacement for
polling: the worker still asks the carrier, less often once events are
arriving (`WORKER_WEBHOOK_POLL_SECS`), and at the full rate for any call the
carrier has said nothing about — so a receiver that is down, unmounted or
unreachable costs nothing but the old request rate. And it is not in the audio
path: the handler does one HMAC and a few short database statements, awaits
every one of them, and never touches a pipeline, a frame or a session.

**Where it runs.** `TELEPHONY_WEBHOOK_RECEIVER=bot` (the default) mounts the
route on the bot's own web server — the only address a single tunnel gives
you — through `install_webhook_receiver`. `standalone` serves it from its own
process, `uv run webhooks.py`, through `create_webhook_app`, for a deployment
that can route one path to a second service. Both are the same router over
the same processor; only the lifecycle differs.

This is the fourth module here that imports `src.telephony`, and it does so
for the reason the others do: it joins the carrier's world to the rows, and it
belongs on the side that owns the rows.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from loguru import logger

from ..config import Config, ConfigError, SecurityConfig
from ..monitoring.http import Readiness, install_ops_routes, store_ready
from ..monitoring.instruments import WEBHOOK_EVENTS, WEBHOOK_LATENCY
from ..reliability.observability import CallContext, Timer, call_context, event
from ..security import HttpPolicy, RateLimiter, client_ip, install_security, parse_networks
from ..telephony import (
    WEBHOOK_AMD,
    WEBHOOK_TRANSFER,
    TelephonyProvider,
    WebhookError,
    WebhookEvent,
    WebhookRequest,
    WebhookSignatureError,
    make_provider,
    transfer_response_twiml,
)
from .dialer import machine_status, map_call_status
from .models import CallAttempt, CallAttemptStatus, TransferStatus, WebhookDelivery, WebhookOutcome

#: How a `<Dial>` leg's ending reads as a transfer status. Phase 16.
_TRANSFER_STATUSES = {
    "completed": TransferStatus.ANSWERED,
    "answered": TransferStatus.ANSWERED,
    "busy": TransferStatus.BUSY,
    "no-answer": TransferStatus.NO_ANSWER,
    "failed": TransferStatus.FAILED,
    "canceled": TransferStatus.CANCELED,
    "cancelled": TransferStatus.CANCELED,
}
from .service import CampaignService
from .store import CampaignStore, CampaignStoreError

#: The HTTP answers a receiver gives. 2xx tells the carrier the event landed
#: (including one it had already delivered); 4xx that the request itself was
#: wrong and should not be repeated; 5xx that we could not act on it now.
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_SERVER_ERROR = 500
HTTP_NOT_IMPLEMENTED = 501
HTTP_UNAVAILABLE = 503


@dataclass(frozen=True)
class WebhookReceipt:
    """What the receiver did with one delivery, and what to tell the carrier.

    Attributes:
        http_status: The response code for the carrier.
        outcome: A `WebhookOutcome` value for an event that was recorded, or
            `refused` / `malformed` / `unsupported` / `error` for one that was
            not.
        detail: One sentence for a log or a reply body.
        event: The decoded event, once there was one.
        attempt: The attempt as it stands after this delivery, when one was
            matched.
    """

    http_status: int
    outcome: str
    detail: str = ""
    event: WebhookEvent | None = None
    attempt: CallAttempt | None = None
    #: Phase 16. A body the carrier is waiting for — the TwiML that decides
    #: what the caller hears after a transfer's leg ends. None answers with
    #: the outcome word as plain text, as every other delivery does.
    body: str | None = None
    media_type: str = "text/plain"

    @property
    def accepted(self) -> bool:
        """Whether the carrier was told the delivery landed."""
        return 200 <= self.http_status < 300


@dataclass
class WebhookMetrics:
    """What a receiver has seen since it started. In-process, for the log."""

    received: int = 0
    applied: int = 0
    duplicates: int = 0
    stale: int = 0
    unmatched: int = 0
    refused: int = 0
    malformed: int = 0
    errors: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)

    def note(self, receipt: WebhookReceipt) -> None:
        """Count one receipt."""
        self.outcomes[receipt.outcome] += 1
        if receipt.outcome == WebhookOutcome.APPLIED:
            self.applied += 1
        elif receipt.outcome == WebhookOutcome.DUPLICATE:
            self.duplicates += 1
        elif receipt.outcome == WebhookOutcome.STALE:
            self.stale += 1
        elif receipt.outcome == WebhookOutcome.UNMATCHED:
            self.unmatched += 1
        elif receipt.outcome == "refused":
            self.refused += 1
        elif receipt.outcome == "malformed":
            self.malformed += 1
        elif receipt.outcome in ("error", "unsupported"):
            self.errors += 1

    def snapshot(self) -> dict[str, Any]:
        """The counters as plain data."""
        return {
            "received": self.received,
            "applied": self.applied,
            "duplicates": self.duplicates,
            "stale": self.stale,
            "unmatched": self.unmatched,
            "refused": self.refused,
            "malformed": self.malformed,
            "errors": self.errors,
            "outcomes": dict(sorted(self.outcomes.items())),
        }


class WebhookProcessor:
    """Turns one carrier delivery into at most one change to one attempt.

    Never raises for a delivery: every failure becomes a `WebhookReceipt` with
    the status code the carrier should see and a structured log line saying
    why. Safe to call with the same delivery any number of times, in any
    order, from any number of processes: the ledger's unique key and the
    attempt's monotonic write are both in the database.
    """

    def __init__(
        self,
        service: CampaignService,
        provider: TelephonyProvider,
        *,
        expected_url: str,
    ) -> None:
        """Create the processor.

        Args:
            service: Campaign rules and persistence — `record_outcome` is what
                moves a membership on once a status is applied.
            provider: The configured carrier: verifies and decodes deliveries.
            expected_url: The URL the carrier was told to deliver to. Every
                signature is checked against this, not against whatever URL
                the local server saw behind its tunnel.
        """
        self._service = service
        self._provider = provider
        self._expected_url = expected_url
        self._ledger_missing = False
        self.metrics = WebhookMetrics()

    @property
    def expected_url(self) -> str:
        """The URL signatures are verified against."""
        return self._expected_url

    @property
    def provider(self) -> TelephonyProvider:
        """The carrier this receiver decodes for."""
        return self._provider

    async def receive(self, request: WebhookRequest) -> WebhookReceipt:
        """Verify, decode, record and apply one delivery. Never raises."""
        timer = Timer()
        self.metrics.received += 1
        provider = self._provider.name

        try:
            self._provider.verify_webhook(request)
        except WebhookSignatureError as exc:
            logger.warning(
                event("webhook.refused", provider=provider, error=str(exc), latency_ms=timer.elapsed_ms)
            )
            return self._done(WebhookReceipt(HTTP_FORBIDDEN, "refused", str(exc)), timer)
        except NotImplementedError as exc:
            logger.error(event("webhook.unsupported", provider=provider, error=str(exc)))
            return self._done(WebhookReceipt(HTTP_NOT_IMPLEMENTED, "unsupported", str(exc)), timer)

        try:
            parsed = self._provider.parse_webhook(request)
        except WebhookError as exc:
            logger.warning(
                event("webhook.malformed", provider=provider, error=str(exc), latency_ms=timer.elapsed_ms)
            )
            return self._done(WebhookReceipt(HTTP_BAD_REQUEST, "malformed", str(exc)), timer)

        with call_context(CallContext(call_id=parsed.call_id, provider=parsed.provider)):
            try:
                receipt = await self._process(parsed, timer)
            except CampaignStoreError as exc:
                # The database is what went away, not the event. 503 so a
                # carrier that retries does; the worker's poll covers the rest.
                logger.error(
                    event(
                        "webhook.store_unavailable",
                        error=(str(exc).splitlines() or [type(exc).__name__])[0],
                        outcome="503; polling still applies the outcome",
                        latency_ms=timer.elapsed_ms,
                    )
                )
                receipt = WebhookReceipt(HTTP_UNAVAILABLE, "error", str(exc), event=parsed)
            except Exception as exc:  # noqa: BLE001 - a bug must not become a silent 500
                logger.exception(event("webhook.failed", error=str(exc), latency_ms=timer.elapsed_ms))
                receipt = WebhookReceipt(HTTP_SERVER_ERROR, "error", str(exc), event=parsed)
        return self._done(receipt, timer)

    def _done(self, receipt: WebhookReceipt, timer: Timer | None = None) -> WebhookReceipt:
        self.metrics.note(receipt)
        # Phase 22: the same count, scrapeable. `outcome` is the receipt's
        # word — applied, duplicate, stale, unmatched, refused, malformed,
        # error — so a failing carrier integration shows as a rate.
        WEBHOOK_EVENTS.inc(provider=self._provider.name, outcome=str(receipt.outcome))
        if timer is not None:
            WEBHOOK_LATENCY.observe(timer.elapsed_secs)
        return receipt

    # --- The event, once trusted --------------------------------------------

    async def _process(self, parsed: WebhookEvent, timer: Timer) -> WebhookReceipt:
        """Record the delivery, find its attempt, and apply it."""
        delivery, inserted = await self._record(parsed)
        if delivery is not None and not inserted:
            logger.info(
                event(
                    "webhook.duplicate",
                    outcome=f"already {delivery.outcome}",
                    key=parsed.key,
                    latency_ms=timer.elapsed_ms,
                )
            )
            return self._with_twiml(
                WebhookReceipt(
                    HTTP_OK, WebhookOutcome.DUPLICATE, f"already recorded as {delivery.outcome}", event=parsed
                )
            )

        attempt = await self._service.store.find_attempt_by_call_id(parsed.call_id)
        if parsed.kind == WEBHOOK_TRANSFER:
            # Phase 16: the carrier is holding the caller and waiting to be
            # told what to do. Answered whether or not the call is one of
            # ours, and before anything else, because a person is listening
            # to silence until it is.
            return await self._apply_transfer(parsed, attempt, delivery, timer)
        if attempt is None:
            await self._finish(delivery, WebhookOutcome.UNMATCHED)
            logger.info(
                event(
                    "webhook.unmatched",
                    outcome=parsed.raw_status or parsed.answered_by,
                    error="no attempt carries this call id",
                    latency_ms=timer.elapsed_ms,
                )
            )
            return WebhookReceipt(
                HTTP_OK, WebhookOutcome.UNMATCHED, "no attempt carries this call id", event=parsed
            )

        context = CallContext(
            campaign_id=attempt.campaign_id,
            prospect_id=attempt.prospect_id,
            attempt_id=attempt.id,
            # Phase 22: the id the dialer wrote on the row, so this delivery's
            # lines join the scheduler's and the bot's.
            trace_id=attempt.trace_id,
        )
        with call_context(context):
            if parsed.kind == WEBHOOK_AMD:
                return await self._apply_verdict(parsed, attempt, delivery, timer)
            return await self._apply_status(parsed, attempt, delivery, timer)

    async def _apply_status(
        self,
        parsed: WebhookEvent,
        attempt: CallAttempt,
        delivery: WebhookDelivery | None,
        timer: Timer,
    ) -> WebhookReceipt:
        """A lifecycle event: the same path a poll takes, without the poll."""
        mapped = map_call_status(parsed.status) if parsed.status is not None else None
        if mapped is None:
            await self._finish(delivery, WebhookOutcome.IGNORED, attempt.id)
            logger.info(
                event(
                    "webhook.ignored",
                    outcome=parsed.raw_status,
                    error="the carrier's status maps to nothing here",
                    latency_ms=timer.elapsed_ms,
                )
            )
            return WebhookReceipt(
                HTTP_OK, WebhookOutcome.IGNORED, "unrecognised status", event=parsed, attempt=attempt
            )

        # Phase 12's rule, kept: a completed call a machine answered is a
        # voicemail. A completion event does not carry the verdict — an
        # asynchronous detection delivers it on its own event, earlier — so
        # it is read back from the ledger, which is where `_apply_verdict`
        # left it.
        snapshot = parsed.to_snapshot()
        if mapped is CallAttemptStatus.COMPLETED and not parsed.answered_by:
            verdict = await self._recorded_verdict(parsed.call_id)
            if verdict:
                snapshot = dataclasses.replace(snapshot, answered_by=verdict)
        mapped = machine_status(mapped, snapshot)

        duration = int(parsed.duration_secs) if parsed.duration_secs is not None else None
        before = attempt.status
        updated, applied = await self._service.store.apply_call_event(
            attempt_id=attempt.id,
            status=mapped,
            duration_seconds=duration,
            failure_reason=parsed.error_message,
        )
        current = updated or attempt
        if not applied:
            await self._finish(delivery, WebhookOutcome.STALE, attempt.id)
            logger.info(
                event(
                    "webhook.stale",
                    outcome=f"{mapped.value} does not follow {current.status.value}",
                    sequence=parsed.sequence,
                    latency_ms=timer.elapsed_ms,
                )
            )
            return WebhookReceipt(
                HTTP_OK,
                WebhookOutcome.STALE,
                f"{mapped.value} does not follow {current.status.value}",
                event=parsed,
                attempt=current,
            )

        # The status is written; the consequences — the membership, the
        # prospect, the call result — are the service's, exactly as after a
        # poll. Re-applying an unchanged status there is a no-op write.
        final = (
            await self._service.record_outcome(
                current,
                mapped,
                failure_reason=parsed.error_message,
                duration_seconds=duration,
            )
            or current
        )
        await self._finish(delivery, WebhookOutcome.APPLIED, attempt.id)
        logger.info(
            event(
                "webhook.applied",
                outcome=f"{before.value} -> {mapped.value}",
                sequence=parsed.sequence,
                duration_secs=duration,
                error=parsed.error_message,
                latency_ms=timer.elapsed_ms,
            )
        )
        return WebhookReceipt(
            HTTP_OK, WebhookOutcome.APPLIED, f"{before.value} -> {mapped.value}", event=parsed, attempt=final
        )

    async def _apply_verdict(
        self,
        parsed: WebhookEvent,
        attempt: CallAttempt,
        delivery: WebhookDelivery | None,
        timer: Timer,
    ) -> WebhookReceipt:
        """An answering-machine verdict: kept on the ledger for the completion to read.

        It changes no status by itself — a live call stays live whoever
        answered, and Phase 12's own handling in the bot decides what to do
        while it is up. A verdict that arrives after the call is already final
        cannot relabel it (the monotonic rule), and says so.
        """
        await self._finish(delivery, WebhookOutcome.NOTED, attempt.id)
        logger.info(
            event(
                "call.answered_by",
                outcome=parsed.answered_by,
                provider="webhook",
                latency_ms=timer.elapsed_ms,
            )
        )
        if parsed.machine_answered and attempt.status.is_final:
            logger.warning(
                event(
                    "webhook.late_verdict",
                    outcome=attempt.status.value,
                    error="the machine verdict arrived after the call was final; the record stands",
                )
            )
        return WebhookReceipt(
            HTTP_OK, WebhookOutcome.NOTED, f"answered_by={parsed.answered_by}", event=parsed, attempt=attempt
        )

    async def _apply_transfer(
        self,
        parsed: WebhookEvent,
        attempt: CallAttempt | None,
        delivery: WebhookDelivery | None,
        timer: Timer,
    ) -> WebhookReceipt:
        """How the colleague's leg ended: recorded, and answered with TwiML. Phase 16.

        The attempt's own status is not touched — the prospect's call is
        still up while the carrier speaks the fallback, and its ending arrives
        as a status event like any other. What is written is the transfer's
        outcome, on the row the bot opened when the carrier accepted the
        redirect (or a new one, when the bot had nowhere to write).
        """
        status = _TRANSFER_STATUSES.get(parsed.raw_status or "")
        if status is None:
            await self._finish(delivery, WebhookOutcome.IGNORED, attempt.id if attempt else None)
            logger.warning(
                event(
                    "transfer.ignored",
                    outcome=parsed.raw_status,
                    error="the carrier's dial status maps to nothing here",
                )
            )
            # Unknown to us is not unknown to the caller: say the fallback
            # rather than leave them in silence.
            return self._with_twiml(
                WebhookReceipt(HTTP_OK, WebhookOutcome.IGNORED, "unrecognised dial status", event=parsed, attempt=attempt)
            )

        duration = int(parsed.duration_secs) if parsed.duration_secs is not None else None
        error = None if status.reached_person else f"the colleague's leg ended {parsed.raw_status}"
        with call_context(
            CallContext(
                campaign_id=attempt.campaign_id if attempt else None,
                prospect_id=attempt.prospect_id if attempt else None,
                attempt_id=attempt.id if attempt else None,
                trace_id=attempt.trace_id if attempt else None,
            )
        ):
            transfer = await self._service.store.complete_transfer(
                parsed.call_id,
                status=status,
                provider=parsed.provider,
                dial_call_id=parsed.dial_call_id,
                duration_seconds=duration,
                error=error,
                to_number=parsed.to_number,
                call_attempt_id=attempt.id if attempt else None,
                prospect_id=attempt.prospect_id if attempt else None,
            )
            await self._finish(delivery, WebhookOutcome.TRANSFER, attempt.id if attempt else None)
            logger.info(
                event(
                    "transfer.completed",
                    outcome=status.value,
                    duration_secs=duration,
                    error=error,
                    transfer=transfer.id if transfer else None,
                    latency_ms=timer.elapsed_ms,
                )
            )
        return self._with_twiml(
            WebhookReceipt(
                HTTP_OK,
                WebhookOutcome.TRANSFER,
                f"the colleague's leg ended {parsed.raw_status}",
                event=parsed,
                attempt=attempt,
            )
        )

    @staticmethod
    def _with_twiml(receipt: WebhookReceipt) -> WebhookReceipt:
        """Attach the TwiML a transfer's `action` request is waiting for. Phase 16.

        Every answer to a `<Dial action>` request must be TwiML, a duplicate
        included: the carrier acts on the body, and a plain-text word would
        drop the caller. A hang-up if the colleague answered; the fallback
        sentence and a hang-up if not.
        """
        if receipt.event is None or receipt.event.kind != WEBHOOK_TRANSFER:
            return receipt
        return dataclasses.replace(
            receipt,
            body=transfer_response_twiml(receipt.event.transfer_answered),
            media_type="application/xml",
        )

    # --- The ledger -----------------------------------------------------------

    async def _record(self, parsed: WebhookEvent) -> tuple[WebhookDelivery | None, bool]:
        """Insert the delivery, or learn that it was already there.

        A database that predates the ledger is tolerated: the event is still
        applied, protected by the attempt's monotonic write alone, and the
        missing table is reported once with the command that adds it.
        """
        if self._ledger_missing:
            return None, True
        try:
            return await self._service.store.record_webhook_event(
                provider=parsed.provider,
                call_id=parsed.call_id,
                event_key=parsed.key,
                kind=parsed.kind,
                status=parsed.status.value if parsed.status is not None else None,
                raw_status=parsed.raw_status,
                sequence=parsed.sequence,
                carrier_timestamp=parsed.timestamp,
                answered_by=parsed.answered_by,
                duration_seconds=int(parsed.duration_secs) if parsed.duration_secs is not None else None,
                payload=dict(parsed.raw),
            )
        except CampaignStoreError as exc:
            if "does not exist" not in str(exc):
                raise
            self._ledger_missing = True
            logger.warning(
                event(
                    "webhook.ledger_missing",
                    error=(str(exc).splitlines() or [type(exc).__name__])[0],
                    outcome="events are applied without the ledger; run `campaign.py init`",
                )
            )
            return None, True

    async def _finish(
        self, delivery: WebhookDelivery | None, outcome: str, attempt_id: int | None = None
    ) -> None:
        """Write the outcome onto the ledger row. Best effort: the state write already happened."""
        if delivery is None:
            return
        try:
            await self._service.store.set_webhook_outcome(
                delivery.id, str(outcome), attempt_id=attempt_id
            )
        except CampaignStoreError as exc:
            logger.warning(event("webhook.ledger_write_failed", error=(str(exc).splitlines() or [type(exc).__name__])[0]))

    async def _recorded_verdict(self, call_id: str) -> str | None:
        """The verdict an earlier AMD delivery left, or None. Never raises."""
        if self._ledger_missing:
            return None
        try:
            return await self._service.store.webhook_answered_by(call_id)
        except CampaignStoreError:
            return None


# --- The HTTP surface -----------------------------------------------------------


def build_webhook_processor(config: Config, store: CampaignStore) -> WebhookProcessor:
    """The processor `campaign.py`, `webhooks.py` and the bot all build the same way.

    Raises:
        ConfigError: No carrier credentials, or webhooks are not receivable
            with what is configured (`TelephonyConfig.webhook_url` is None).
    """
    url = config.telephony.webhook_url()
    if url is None:
        raise ConfigError(
            f"Webhooks cannot be received: {config.telephony.describe_webhooks()}."
        )
    provider = make_provider(config.telephony, timeout_secs=config.reliability.carrier_timeout_secs)
    service = CampaignService(
        store,
        default_region=config.default_phone_region,
        max_attempts=config.campaign_max_attempts,
        retry_minutes=config.campaign_retry_minutes,
    )
    return WebhookProcessor(service, provider, expected_url=url)


#: The most a carrier's form body is ever going to be. Twilio's status
#: callbacks are a few hundred bytes; a megabyte is somebody else.
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
#: How many deliveries an address may have *refused* per minute before it
#: is answered 429 without the signature even being computed. Phase 18.
#: A genuine carrier is never refused — its deliveries verify — so this only
#: ever slows down somebody probing the route.
REFUSED_PER_MINUTE = 30


def create_webhook_router(
    get_processor: Callable[[], Awaitable[WebhookProcessor]],
    *,
    path: str,
    security: SecurityConfig | None = None,
) -> APIRouter:
    """The one route: `POST path`, a form body in, a status code out.

    FastAPI is already a dependency — Pipecat's runner and the dashboard use
    it — so it is imported at module level, which is also what lets FastAPI
    read the handler's `Request` annotation; under `from __future__ import
    annotations` a name imported inside this function would not resolve.

    Args:
        get_processor: Where the processor comes from. Awaited per request,
            because the bot opens its database lazily on the first delivery
            and the standalone app opens it at startup.
        path: The route, e.g. `/webhooks/telephony`.
        security: Phase 18. Supplies the trusted proxies behind the client
            address and the anonymous rate limit; None means defaults.
    """
    router = APIRouter()
    networks = parse_networks(security.trusted_proxies if security else ())
    refused = RateLimiter(security.anon_rate_limit if security else REFUSED_PER_MINUTE, 60.0)

    @router.post(path)
    async def receive(request: Request) -> PlainTextResponse:
        """Verify, decode and apply one carrier event. Answers in plain text."""
        ip = client_ip(request, networks)
        # Phase 18: an address whose deliveries keep failing to verify is
        # told to go away before the next signature is computed. Checked
        # without consuming: only a refusal counts.
        if not refused.check(ip, consume=False).allowed:
            logger.warning(event("webhook.rate_limited", ip=ip))
            return PlainTextResponse(
                "too many refused deliveries from this address",
                status_code=429,
                headers={"Retry-After": refused.check(ip, consume=False).retry_after_header},
            )
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_WEBHOOK_BODY_BYTES:
            refused.check(ip)
            return PlainTextResponse("body too large", status_code=413)
        try:
            form = await request.form()
        except Exception:  # noqa: BLE001 - not a form is the whole finding
            refused.check(ip)
            return PlainTextResponse("expected a form-encoded body", status_code=HTTP_BAD_REQUEST)
        fields = {str(name): str(value) for name, value in form.multi_items()}
        try:
            processor = await get_processor()
        except (CampaignStoreError, ConfigError) as exc:
            logger.error(event("webhook.receiver_unavailable", error=(str(exc).splitlines() or [type(exc).__name__])[0]))
            return PlainTextResponse("receiver unavailable", status_code=HTTP_UNAVAILABLE)
        delivery = WebhookRequest(
            # The configured URL, not `request.url`: behind a tunnel the
            # server sees http://localhost:7860/…, and the carrier signed the
            # https address it was given.
            url=processor.expected_url,
            headers={name.lower(): value for name, value in request.headers.items()},
            form=fields,
            method=request.method,
        )
        receipt = await processor.receive(delivery)
        if receipt.http_status in (HTTP_BAD_REQUEST, HTTP_FORBIDDEN):
            refused.check(ip)
        if receipt.body is not None:
            # Phase 16: a transfer's `action` request. The carrier is waiting
            # for TwiML that says what the caller hears next.
            return Response(content=receipt.body, media_type=receipt.media_type, status_code=receipt.http_status)
        return PlainTextResponse(receipt.outcome, status_code=receipt.http_status)

    return router


class _LazyProcessor:
    """Opens the database on the first delivery, once, and keeps it.

    For the bot, whose web server is Pipecat's and has no lifespan of ours to
    hang a pool on. A pool of two: one delivery at a time is the normal case,
    and a burst of them queues on the pool rather than on the carrier.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._processor: WebhookProcessor | None = None

    async def get(self) -> WebhookProcessor:
        if self._processor is not None:
            return self._processor
        async with self._lock:
            if self._processor is None:
                store = await CampaignStore.connect(
                    self._config.database_url or "", min_size=1, max_size=2
                )
                self._processor = build_webhook_processor(self._config, store)
                logger.info(
                    event(
                        "webhook.receiver_ready",
                        provider=self._processor.provider.name,
                        outcome=self._processor.expected_url,
                    )
                )
            return self._processor

    @property
    def metrics(self) -> WebhookMetrics | None:
        """The counters, once a delivery has arrived."""
        return self._processor.metrics if self._processor is not None else None


def install_webhook_receiver(app: Any, config: Config) -> str | None:
    """Mount the route on the bot's web server, if webhooks are receivable. Phase 14.

    Called from `bot.py` before Pipecat's runner starts. Adds one `POST` route
    and nothing else: no middleware, no lifespan, no background task. The
    database is opened lazily on the first delivery.

    Returns:
        The URL the carrier will be sent, or None — with the reason logged —
        when nothing was mounted: webhooks off, no public URL, no signing key
        for a carrier that needs one, no database, or the standalone receiver
        chosen instead.
    """
    telephony = config.telephony
    if telephony.webhook_receiver != "bot":
        logger.info(
            "Webhooks: TELEPHONY_WEBHOOK_RECEIVER=standalone — the bot serves no webhook "
            "route; run `uv run webhooks.py` behind the same public address."
        )
        return None
    url = telephony.webhook_url()
    if url is None:
        logger.info(f"Webhooks: {telephony.describe_webhooks()}.")
        return None
    if not config.database_url:
        logger.warning(
            "Webhooks: TELEPHONY_PUBLIC_URL is set but there is no DATABASE_URL, so carrier "
            "events would have nowhere to land. Not mounting the route."
        )
        return None

    lazy = _LazyProcessor(config)
    app.include_router(create_webhook_router(lazy.get, path=telephony.webhook_path, security=config.security))
    logger.info(f"Webhooks OK | carriers will POST call events to {url}")
    return url


def create_webhook_app(config: Config) -> FastAPI:
    """The standalone receiver: one FastAPI app, one pool, one route. Phase 14.

    For `TELEPHONY_WEBHOOK_RECEIVER=standalone`: `uv run webhooks.py` serves
    this on its own port, and a proxy in front of the public address routes
    `TELEPHONY_WEBHOOK_PATH` here and `/ws` to the bot. Same router, same
    processor as the bot-mounted form; the difference is that the database
    is opened at startup and closed at shutdown, because this process has a
    lifespan of its own.

    Raises:
        ConfigError: Webhooks are not receivable with what is configured.
        CampaignStoreError: There is no `DATABASE_URL`.
    """
    url = config.telephony.webhook_url()
    if url is None:
        raise ConfigError(f"Webhooks cannot be received: {config.telephony.describe_webhooks()}.")
    if not config.database_url:
        raise CampaignStoreError(
            "No database is configured, so carrier events have nowhere to land.\n"
            "  Set DATABASE_URL (or KB_DATABASE_URL, which it defaults to)."
        )

    state: dict[str, Any] = {"processor": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = await CampaignStore.connect(config.database_url or "", min_size=1, max_size=4)
        processor = build_webhook_processor(config, store)
        state["processor"] = processor
        logger.info(
            event("webhook.receiver_ready", provider=processor.provider.name, outcome=url)
        )
        try:
            yield
        finally:
            state["processor"] = None
            await processor.provider.close()
            await store.close()

    async def get_processor() -> WebhookProcessor:
        processor: WebhookProcessor | None = state["processor"]
        if processor is None:
            raise CampaignStoreError("the receiver is still starting")
        return processor

    app = FastAPI(
        title="Ai-Voice-Agent webhooks",
        description="Receives carrier call events. One route; nothing to browse.",
        version="18",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Phase 18: security headers, HTTPS enforcement, the body cap. No CORS:
    # a carrier is not a browser.
    security = config.security
    install_security(
        app,
        HttpPolicy(
            require_https=security.require_https,
            trusted_proxies=security.trusted_proxies,
            cors_origins=(),
            max_body_bytes=min(security.max_body_bytes, MAX_WEBHOOK_BODY_BYTES),
        ),
        kind="webhook",
    )
    app.include_router(
        create_webhook_router(get_processor, path=config.telephony.webhook_path, security=security)
    )
    # Phase 22: /healthz, /readyz, /metrics; readiness is the database the
    # events land in. No fleet gauges here: a receiver is not where anyone
    # reads the queue.
    if config.monitoring.enabled:

        def store_or_starting() -> Any:
            processor: WebhookProcessor | None = state["processor"]
            if processor is None:
                raise CampaignStoreError("the receiver is still starting")
            return processor._service.store

        async def readiness() -> Readiness:
            return Readiness([await store_ready(store_or_starting)])

        install_ops_routes(app, "webhooks", readiness=readiness, token=config.monitoring.token, version="22")

    @app.get("/api/ping")
    async def ping() -> dict[str, Any]:
        """Is the receiver up, and what has it seen?"""
        processor: WebhookProcessor | None = state["processor"]
        if processor is None:
            return {"ok": False, "detail": "starting"}
        return {"ok": True, "url": url, **processor.metrics.snapshot()}

    return app


__all__ = [
    "HTTP_BAD_REQUEST",
    "HTTP_FORBIDDEN",
    "HTTP_OK",
    "HTTP_UNAVAILABLE",
    "WebhookMetrics",
    "WebhookProcessor",
    "WebhookReceipt",
    "build_webhook_processor",
    "create_webhook_app",
    "create_webhook_router",
    "install_webhook_receiver",
]
