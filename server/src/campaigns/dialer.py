"""Where a reserved campaign call becomes a real phone call.

This is the only module that imports both `src.campaigns` and `src.telephony`,
and keeping it that way is the point. The telephony package does not know what a
campaign is; the service does not know what a carrier is; `bot.py` knows about
neither and still only knows how to hold a conversation. The seam is one class.

**What it does not do is decide anything.** Whether a prospect may be called is
`service.check_callable`; what an outcome means for a membership is
`service.record_outcome`. This translates between the two vocabularies —
`CallStatus` from the carrier, `CallAttemptStatus` in the database — and drives
the sequence:

1. reserve the next eligible call (queue, in one transaction);
2. re-check callability against freshly read rows;
3. place the call through the existing `TelephonyProvider`;
4. store the carrier's call id on the attempt;
5. follow the call to an outcome and write it back.

Step 2 looks redundant after step 1 and is not: time passes between them, and
the thing that can change in that time is somebody marking the prospect
do-not-call.

**Why the outcome comes from polling the carrier rather than from `bot.py`.**
The bot is a separate process that the *carrier* starts when the call is
answered; it holds the conversation and knows nothing about campaigns. Having it
write campaign state would put business logic in the conversation loop and
couple two processes that currently share nothing but a call id. Polling keeps
that boundary, and it is the same mechanism `call.py` already uses. When this
grows into a scheduler, carrier status webhooks are the upgrade — the attempt
row is already keyed by `telephony_call_id` for exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from ..conversation import PARAM_ATTEMPT_ID, PARAM_CAMPAIGN_ID, PARAM_PROSPECT_ID
from ..monitoring.instruments import CALL_ATTEMPTS, CARRIER_FAILURES, PLACEMENT_LATENCY
from ..monitoring.tracing import PARAM_TRACE_ID, new_trace_id
from ..reliability.guardrails import CampaignGuards, Decision, prospect_timezone
from ..reliability.idempotency import campaign_call_key
from ..reliability.observability import CallContext, Timer, call_context, event
from ..reliability.retry import (
    NEVER_RETRY,
    READ_POLICY,
    AmbiguousOutcomeError,
    Verdict,
    call_with_retry,
    read_classifier,
)
from ..telephony import (
    PARAM_DIRECTION,
    PARAM_FROM,
    PARAM_TO,
    CallRequest,
    CallSetupError,
    CallSnapshot,
    CallStatus,
    ProviderUnavailableError,
    TelephonyError,
    TelephonyProvider,
    stream_url,
)
from ..telephony.session import DIRECTION_OUTBOUND
from .models import CallAttempt, CallAttemptStatus, CallbackStatus, QueuedCall
from .service import CampaignService
from .store import CampaignStoreError

# The carrier's vocabulary, in ours. Both enums keep the unhappy endings apart
# for the same reason, so this is nearly one-to-one; the two that are not are
# worth their comments. `map_call_status` is the public form (Phase 14): the
# webhook receiver reads a pushed status through the same table a poll does.
_STATUS_MAP = {
    CallStatus.QUEUED: CallAttemptStatus.QUEUED,
    CallStatus.RINGING: CallAttemptStatus.CALLING,
    CallStatus.ANSWERED: CallAttemptStatus.CONNECTED,
    CallStatus.COMPLETED: CallAttemptStatus.COMPLETED,
    CallStatus.BUSY: CallAttemptStatus.BUSY,
    CallStatus.NO_ANSWER: CallAttemptStatus.NO_ANSWER,
    CallStatus.FAILED: CallAttemptStatus.FAILED,
    # We hung up before they answered. From the campaign's point of view that is
    # a failed attempt, not a person who declined.
    CallStatus.CANCELED: CallAttemptStatus.FAILED,
}

# Custom parameters carried into the call alongside the ones `call.py` sends.
# They ride on the carrier's media-stream handshake, which is the only channel
# an outbound call has for telling the bot anything about itself — which is how
# the conversation layer knows which prospect it is talking to, without the
# campaign tables being reachable from `bot.py`.
#
# The names are defined in `src/conversation/sources.py` and imported here,
# rather than the other way round, because a parameter name is a contract
# between a writer and a reader and the reader is the one that breaks when it
# changes. Phase 6 made this module the writer and that one the reader.
# The three statuses that come from what somebody *said*, not from what the
# carrier reported. Since Phase 9 they are final, and `models.may_advance`
# refuses to overwrite any final status — so the rule that a conversation
# outcome outranks the carrier's "completed" is now enforced for every writer
# rather than by three lines in `refresh`. Kept as a named set because the
# reason is worth being able to find.
# The carrier endings that get a reason on the row even when the carrier
# gave none (Phase 25). A completed call or a voicemail is not a failure.
_UNHAPPY_ENDINGS = frozenset(
    {CallAttemptStatus.FAILED, CallAttemptStatus.BUSY, CallAttemptStatus.NO_ANSWER}
)

_CONVERSATION_OUTCOMES = frozenset(
    {
        CallAttemptStatus.CALLBACK_REQUESTED,
        CallAttemptStatus.NOT_INTERESTED,
        CallAttemptStatus.DO_NOT_CALL,
    }
)


def map_call_status(status: CallStatus) -> CallAttemptStatus | None:
    """The attempt status a carrier status becomes, or None for one this code ignores.

    None for `UNKNOWN` — and for anything a future `CallStatus` adds without
    a row here — so that a poll and a webhook alike leave the attempt alone
    rather than guess. Phase 14 made this public so the receiver and
    `refresh` cannot disagree about what a status means.
    """
    return _STATUS_MAP.get(status)


def machine_status(mapped: CallAttemptStatus, snapshot: CallSnapshot) -> CallAttemptStatus:
    """A completed call the carrier says a machine answered is a `VOICEMAIL`. Phase 12.

    Only a *completed* call: while the call is live the status stays live, so
    the prospect stays blocked, and the bot's own detection may end it first.
    A final status the carrier chose for another reason (busy, failed) is not
    second-guessed.
    """
    if mapped is CallAttemptStatus.COMPLETED and snapshot.machine_answered:
        logger.info(
            event(
                "call.answered_by_machine",
                call=snapshot.call_id,
                outcome=CallAttemptStatus.VOICEMAIL.value,
                provider=snapshot.answered_by,
            )
        )
        return CallAttemptStatus.VOICEMAIL
    return mapped


def _campaign_pacing(configuration: Any) -> float:
    """A campaign's own minimum interval between its placements, from its JSON. Phase 21.

    `configuration["pacing_secs"]`, a number of seconds; anything else is
    no campaign pacing. Applied on top of the deployment's `CALL_PACING_SECS`.
    """
    if not isinstance(configuration, dict):
        return 0.0
    raw = configuration.get("pacing_secs")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    return max(0.0, min(float(raw), 3600.0))


def _placement_classifier(exc: BaseException) -> Verdict:
    """How a failure of `place_call` is read. Phase 9, and the crux of the phase.

    There is no safe retry here, so the only question is whether a call may
    have been created:

    * `CallSetupError` — the carrier answered 4xx. It read the request and
      refused it, so nothing was dialled. `FATAL`: recording a failure is
      correct and retrying reproduces it.
    * `ProviderUnavailableError` — unreachable, 5xx, or timed out. Any of those
      may have been received and acted on before the answer was lost.
      `AMBIGUOUS`, so the attempt becomes `UNRESOLVED` and stays live.
    * anything else — unrecognised, so treated as ambiguous rather than as a
      clean failure. Erring towards "we do not know" costs one uncalled
      prospect; erring the other way costs a second call to a real person.
    """
    if isinstance(exc, CallSetupError):
        return Verdict.FATAL
    if isinstance(exc, ProviderUnavailableError):
        return Verdict.AMBIGUOUS
    return Verdict.AMBIGUOUS


@dataclass(frozen=True)
class DialResult:
    """What happened when the dialer tried to place one call.

    Attributes:
        queued: The reservation, when there was one. `None` means nothing was
            eligible — an empty queue, not a failure.
        attempt: The attempt row as it stands now.
        snapshot: The carrier's view, when a call was actually placed.
        error: Why no call was placed, when one was expected.
        ambiguous: The placement never reported an outcome, so whether a call
            exists is unknown (Phase 9). The attempt is `UNRESOLVED` and the
            prospect stays blocked until recovery resolves it. Distinct from
            `error` because a caller must not treat it as "no call happened".
        blocked_by: The guardrail that refused, when one did — the calling
            window, the concurrency limit, pacing. Carries `retry_after_secs`,
            so a loop can sleep rather than spin.
        deferred: The reservation was given back *unspent* because the
            prospect's own calling window is closed (Phase 13). Nothing was
            dialled and no attempt was recorded; the membership is scheduled
            for when the window opens. Distinct from `error`, which means an
            attempt was recorded as failed.
    """

    queued: QueuedCall | None = None
    attempt: CallAttempt | None = None
    snapshot: CallSnapshot | None = None
    error: str | None = None
    ambiguous: bool = False
    blocked_by: Decision | None = None
    deferred: bool = False

    @property
    def placed(self) -> bool:
        """Whether a call definitely reached the carrier."""
        return self.snapshot is not None

    @property
    def blocked(self) -> bool:
        """Whether a guardrail stopped the call before anything was reserved."""
        return self.blocked_by is not None

    @property
    def refusal(self) -> str:
        """Why a guardrail refused, or an empty string. Safe to read either way.

        A refusing `Decision` is falsy, so `result.blocked_by.reason if
        result.blocked_by else ""` silently yields the empty string for exactly
        the case that has a reason. This property is what callers should use.
        """
        return self.blocked_by.reason if self.blocked_by is not None else ""

    def describe(self) -> str:
        """One line for a CLI or a log."""
        if self.blocked_by is not None:
            return f"not calling — {self.blocked_by.reason}"
        if self.queued is None:
            return "nothing eligible to call"
        who = self.queued.prospect.full_name
        if self.deferred:
            return f"{who}: deferred — {self.error}"
        if self.ambiguous:
            return (
                f"{who}: OUTCOME UNKNOWN — {self.error}. The attempt is held as UNRESOLVED and "
                f"the prospect will not be dialled again until recovery resolves it."
            )
        if self.error:
            return f"{who}: not called — {self.error}"
        if self.snapshot:
            return f"{who}: {self.snapshot.describe()}"
        return f"{who}: reserved but not placed"


class CampaignDialer:
    """Places campaign calls through the existing telephony abstraction."""

    def __init__(
        self,
        service: CampaignService,
        provider: TelephonyProvider,
        *,
        from_number: str,
        public_url: str,
        stream_path: str = "/ws",
        answer_timeout_secs: int = 30,
        guards: CampaignGuards | None = None,
        machine_detection: str = "off",
        status_callback_url: str | None = None,
        gate: Any = None,
        worker_id: str | None = None,
    ) -> None:
        """Create the dialer.

        Phase 21: `worker_id` is written onto every reservation as its owner,
        and a placement takes the shared pacing slot from the store before
        the carrier is asked — the pacing that used to be this process's
        clock is now the deployment's row. Both may be set after
        construction (`dialer.worker_id = ...`); the worker does.

        Phase 19: `gate` is a `ComplianceGate`. When given, it is what runs
        before every placement — the do-not-call list, the person, the
        campaign's rules under the policy for that number, the window — and
        every decision it takes is on the audit log. `None` keeps Phase 9's
        `check_callable` and Phase 13's window check, which the gate calls
        anyway; `campaign.py` always supplies one.

        Args:
            service: Campaign rules and persistence.
            provider: Any `TelephonyProvider` — Twilio, SignalWire, or whatever
                is configured. This class never names one.
            from_number: Caller ID to present.
            public_url: Where the carrier should stream the call's audio, i.e.
                the running bot. Turned into a `wss://` URL by `stream_url`.
            stream_path: The bot's telephony websocket route.
            answer_timeout_secs: Ring time before a no-answer.
            guards: Calling hours, concurrency and pacing (Phase 9). `None`
                applies none of them, which is what a caller that has already
                checked wants — and, deliberately, is not the default anywhere
                in this project: `campaign.py` always supplies them.
            machine_detection: Phase 12. Ask the carrier to detect an
                answering machine on every call placed: `off`, `async` or
                `sync`. The verdict comes back on `refresh` as `answered_by`
                and turns a completed call into a `VOICEMAIL` attempt.
            status_callback_url: Phase 14. Where the carrier should push each
                call's lifecycle events, so the attempt is written the moment
                the call changes rather than on the next poll. `None` asks for
                none; `refresh` still works either way — it is the fallback.
        """
        self._service = service
        self._provider = provider
        self._from_number = from_number
        self._stream_url = stream_url(public_url, stream_path)
        self._answer_timeout = answer_timeout_secs
        self._guards = guards
        self._machine_detection = machine_detection
        self._status_callback_url = status_callback_url
        self._gate = gate
        self.worker_id = worker_id

    async def dial_next(self, campaign_id: int, *, worker_id: str | None = None) -> DialResult:
        """Reserve and place the next call for a campaign.

        Phase 21: `worker_id` names the owner written onto the reservation;
        it overrides the dialer's own. The worker passes its id on every
        call rather than setting it on a dialer it may share.

        Guardrails are checked *before* anything is reserved (Phase 9), because
        a reservation taken and then released still spends an attempt from the
        membership's budget, and "the calling window is closed" must not cost a
        prospect one of their three tries.

        Returns:
            A `DialResult`. `queued is None` means the queue had nothing
            eligible, which is the normal end of a campaign rather than an
            error; `blocked` means a guardrail said not now.
        """
        blocked = await self._check_guards(campaign_id)
        if blocked is not None:
            logger.info(event("dial.blocked", outcome=blocked.reason))
            return DialResult(blocked_by=blocked)

        # The limit is passed into the reservation as well as checked above:
        # the check is cheaper and produces a reason, and the reservation's own
        # count is what holds when two workers check at the same instant
        # (Phase 11).
        queued = await self._service.next_call(
            campaign_id,
            max_concurrent=self._guards.max_concurrent if self._guards else 0,
            worker_id=worker_id or self.worker_id,
        )
        if queued is None:
            return DialResult()
        return await self.dial(queued)

    async def dial_membership(
        self,
        membership_id: int,
        *,
        ignore_attempt_limit: bool = False,
        worker_id: str | None = None,
    ) -> DialResult:
        """Reserve and place the call for one named membership. Phase 13.

        `dial_next` with the row chosen by the caller instead of by the queue's
        order: how a scheduled callback is placed at the time the prospect
        asked for, ahead of the never-called rows the queue puts first. The
        guardrails run first exactly as for `dial_next`, the reservation is the
        store's own targeted statement under the same rules, and the placement
        is `dial`.

        Args:
            membership_id: Which membership.
            ignore_attempt_limit: Waive `CAMPAIGN_MAX_ATTEMPTS` for this call.
                Only for a callback the prospect asked for.
        """
        blocked = await self._check_guards(None)
        if blocked is not None:
            logger.info(event("dial.blocked", outcome=blocked.reason))
            return DialResult(blocked_by=blocked)

        queued = await self._service.reserve_membership(
            membership_id,
            max_concurrent=self._guards.max_concurrent if self._guards else 0,
            ignore_attempt_limit=ignore_attempt_limit,
            worker_id=worker_id or self.worker_id,
        )
        if queued is None:
            return DialResult()
        return await self.dial(queued, ignore_attempt_limit=ignore_attempt_limit)

    async def _check_guards(self, campaign_id: int | None) -> Decision | None:
        """The guardrails that do not depend on which prospect is next.

        Returns the refusal, or None to proceed. A database that cannot answer
        the concurrency question refuses the call: not knowing how many calls
        are live is not a reason to place another one.
        """
        if self._guards is None:
            return None
        try:
            live = await self._service.store.count_live_attempts()
        except CampaignStoreError as exc:
            return Decision.no(f"cannot count live calls, so not placing another: {exc}")
        # `.refused` rather than truthiness: a refusing `Decision` is falsy,
        # so `decision or None` would return None for exactly the case that
        # must stop the call. See `Decision.__bool__`.
        decision = self._guards.check(live_calls=live)
        return decision if decision.refused else None

    async def dial(self, queued: QueuedCall, *, ignore_attempt_limit: bool = False) -> DialResult:
        """Place one already-reserved call.

        **The order here is the duplicate-call protection** (Phase 9), and each
        step exists because of the failure it prevents:

        1. Re-check callability against fresh rows — a do-not-call that landed
           between reserving and dialling.
        2. Check the prospect's own calling window, now that we know who they
           are and what timezone their record gives.
        3. Stamp `placement_started_at` **before** asking the carrier, so an
           attempt whose process dies mid-request is still recognisable as one
           that may have dialled.
        4. Ask the carrier exactly once. `place_call` is never retried, because
           neither supported carrier offers an idempotency key for it.
        5. On an ambiguous answer, mark the attempt `UNRESOLVED` and stop. It
           stays live, so the prospect cannot be dialled again, and
           `reliability/recovery.py` finds out from the carrier what happened.
        6. On success, record the call id — and refuse to record a *second*
           different id against one attempt, hanging up the duplicate if that
           somehow happens.

        Every failure path except the ambiguous one releases the reservation, so
        a prospect is never left `IN_PROGRESS` with no call happening.
        """
        attempt = queued.attempt
        # Phase 22: the correlation id is born here, before anything else
        # happens to this attempt, and goes three ways at once — onto the
        # row, onto the handshake, onto every log line below. A reservation
        # that already carries one (a retry of a placement that failed
        # before dialling) keeps it, so the whole story of one attempt is
        # one id. See `monitoring/tracing.py`.
        trace_id = attempt.trace_id or new_trace_id()
        context = CallContext(
            campaign_id=queued.campaign.id,
            prospect_id=queued.prospect.id,
            attempt_id=attempt.id,
            provider=self._provider.name,
            trace_id=trace_id,
        )
        campaign_label = queued.campaign.id

        with call_context(context):
            if attempt.trace_id != trace_id:
                await self._stamp_trace(attempt.id, trace_id)
            # Phase 13: against a *fresh* row. The copy the queue handed out
            # was read inside the reservation, and the whole point of this
            # check is what changed since — a do-not-call that landed a moment
            # ago. A row that cannot be read is treated as not callable.
            prospect = await self._service.store.get_prospect(queued.prospect.id)
            if prospect is None:
                await self._service.release(queued, "the prospect row is gone")
                CALL_ATTEMPTS.inc(campaign=campaign_label, outcome="released")
                return DialResult(queued=queued, error="the prospect row is gone")

            if self._gate is not None:
                # Phase 19: one gate, every rule, one audit row. The verdict
                # says what kind of refusal this is, and each kind has the
                # consequence it deserves — a do-not-call is a clear
                # disposition, a closed window is a deferral, a reached
                # ceiling closes the membership, anything else releases.
                decision = await self._gate.check(
                    prospect,
                    queued.campaign,
                    queued.membership,
                    ignore_attempt_id=attempt.id,
                    ignore_attempt_limit=ignore_attempt_limit,
                    attempt_id=attempt.id,
                )
                if decision.refused:
                    blocked = decision.as_decision()
                    verdict = str(decision.verdict)
                    if verdict == "dnc":
                        await self._service.record_outcome(
                            attempt, CallAttemptStatus.DO_NOT_CALL, failure_reason=f"not dialled: {decision.reason}"
                        )
                        logger.warning(event("call.blocked", outcome=decision.code, error=decision.reason))
                        CALL_ATTEMPTS.inc(campaign=campaign_label, outcome="blocked")
                        return DialResult(queued=queued, error=decision.reason, blocked_by=blocked)
                    if verdict == "defer":
                        undone = await self._service.defer(
                            queued, decision.reason, retry_after_secs=decision.retry_after_secs or 0.0
                        )
                        CALL_ATTEMPTS.inc(campaign=campaign_label, outcome="deferred")
                        return DialResult(queued=queued, error=decision.reason, blocked_by=blocked, deferred=undone)
                    if verdict == "exhaust":
                        undone = await self._service.exhaust(queued, decision.reason)
                        CALL_ATTEMPTS.inc(campaign=campaign_label, outcome="exhausted")
                        return DialResult(queued=queued, error=decision.reason, blocked_by=blocked, deferred=undone)
                    await self._service.release(queued, decision.reason)
                    CALL_ATTEMPTS.inc(campaign=campaign_label, outcome="released")
                    return DialResult(queued=queued, error=decision.reason)
            else:
                check = await self._service.check_callable(
                    prospect,
                    queued.campaign,
                    queued.membership,
                    # Reserving already created this attempt; without excluding it
                    # the check would find it and refuse to place the call it
                    # belongs to.
                    ignore_attempt_id=attempt.id,
                    ignore_attempt_limit=ignore_attempt_limit,
                )
                if not check:
                    # The safety net for anything that changed between reserving and
                    # dialling. In practice: somebody marked them do-not-call.
                    await self._service.release(queued, check.reason)
                    CALL_ATTEMPTS.inc(campaign=campaign_label, outcome="released")
                    return DialResult(queued=queued, error=check.reason)

                window = self._check_window(queued)
                if window is not None:
                    # Phase 13: the prospect's own hours, not a fault with the
                    # call. Give the reservation back unspent and schedule the
                    # membership for when their window opens; a scheduler that
                    # released here would burn every attempt a prospect in another
                    # timezone had before their morning arrived.
                    undone = await self._service.defer(
                        queued, window.reason, retry_after_secs=window.retry_after_secs or 0.0
                    )
                    CALL_ATTEMPTS.inc(campaign=campaign_label, outcome="deferred")
                    return DialResult(
                        queued=queued, error=window.reason, blocked_by=window, deferred=undone
                    )

            request = CallRequest(
                to_number=queued.prospect.phone_normalized or "",
                from_number=self._from_number,
                stream_url=self._stream_url,
                answer_timeout_secs=self._answer_timeout,
                parameters={
                    PARAM_DIRECTION: DIRECTION_OUTBOUND,
                    PARAM_FROM: self._from_number,
                    PARAM_TO: queued.prospect.phone_normalized or "",
                    # Carried so the conversation layer can look up who it is
                    # speaking to. Read by `conversation/sources.py` in the bot
                    # process; the bot still never imports these tables.
                    PARAM_PROSPECT_ID: str(queued.prospect.id),
                    PARAM_CAMPAIGN_ID: str(queued.campaign.id),
                    PARAM_ATTEMPT_ID: str(attempt.id),
                    # Phase 22: the correlation id, so the bot's first line
                    # already carries what this process is logging.
                    PARAM_TRACE_ID: trace_id,
                },
                machine_detection=self._machine_detection,
                status_callback_url=self._status_callback_url,
            )

            # Written before the request, so the row itself records that a
            # placement may be in flight. A process killed on the next line
            # leaves an attempt recovery can recognise.
            # Phase 21: the shared pacing slot, taken in the database under a
            # lock, so two workers cannot both find the interval elapsed. A
            # refusal gives the reservation back unspent, due when the slot
            # frees; the in-process limiter above stays as the cheap first
            # check. A campaign's own `pacing_secs` is a second scope.
            pacing = self._guards.pacing.interval_secs if self._guards is not None else 0.0
            campaign_pacing = _campaign_pacing(queued.campaign.configuration)
            taker = getattr(self._service.store, "take_pacing_slot", None)
            if taker is not None and (pacing > 0 or campaign_pacing > 0):
                try:
                    allowed, wait = await taker(
                        pacing, campaign_id=queued.campaign.id, campaign_interval_secs=campaign_pacing
                    )
                except CampaignStoreError as exc:
                    if "does not exist" not in str(exc):
                        raise
                    allowed, wait = True, 0.0  # a database that predates the table: the in-process limiter alone
                if not allowed:
                    reason = f"pacing: {wait:.1f}s until the next call may be placed (shared across workers)"
                    blocked = Decision.no(reason, retry_after_secs=wait)
                    undone = await self._service.defer(queued, reason, retry_after_secs=wait)
                    logger.info(event("dial.paced", outcome=reason))
                    CALL_ATTEMPTS.inc(campaign=campaign_label, outcome="paced")
                    return DialResult(queued=queued, error=reason, blocked_by=blocked, deferred=undone)

            await self._service.store.mark_placement_started(attempt.id)
            if self._guards is not None:
                self._guards.record_placement()

            timer = Timer()
            try:
                snapshot = await call_with_retry(
                    lambda: self._provider.place_call(request),
                    policy=NEVER_RETRY,
                    classify=_placement_classifier,
                    name=f"place_call({queued.prospect.phone_normalized})",
                )
            except AmbiguousOutcomeError as exc:
                PLACEMENT_LATENCY.observe(timer.elapsed_secs)
                CALL_ATTEMPTS.inc(campaign=campaign_label, outcome="unresolved")
                CARRIER_FAILURES.inc(provider=self._provider.name, kind="ambiguous")
                return await self._hold_unresolved(queued, exc, timer)
            except TelephonyError as exc:
                # The carrier read the request and refused it. No phone rang, so
                # the attempt is a recorded failure with the carrier's own words.
                reason = (str(exc).splitlines() or [type(exc).__name__])[0]
                logger.warning(
                    event("call.refused", latency_ms=timer.elapsed_ms, error=reason)
                )
                PLACEMENT_LATENCY.observe(timer.elapsed_secs)
                CALL_ATTEMPTS.inc(campaign=campaign_label, outcome="refused")
                CARRIER_FAILURES.inc(provider=self._provider.name, kind=exc.__class__.__name__)
                await self._service.release(queued, reason)
                return DialResult(queued=queued, error=reason)

            PLACEMENT_LATENCY.observe(timer.elapsed_secs)
            result = await self._record_placement(queued, snapshot, timer)
            CALL_ATTEMPTS.inc(campaign=campaign_label, outcome="placed" if result.placed else "released")
            return result

    async def _stamp_trace(self, attempt_id: int, trace_id: str) -> None:
        """Write the correlation id on the row. Best effort: a store without the method or the column keeps dialling. Phase 22."""
        writer = getattr(self._service.store, "set_attempt_trace", None)
        if writer is None:
            return
        try:
            await writer(attempt_id, trace_id)
        except CampaignStoreError as exc:
            logger.warning(event("trace.not_stored", error=(str(exc).splitlines() or [type(exc).__name__])[0]))

    def _check_window(self, queued: QueuedCall) -> Decision | None:
        """The calling-hours check, in the prospect's own timezone where known."""
        if self._guards is None:
            return None
        decision = self._guards.window.check(
            timezone=prospect_timezone(queued.prospect.custom_data)
        )
        return decision if decision.refused else None

    async def _hold_unresolved(
        self, queued: QueuedCall, exc: AmbiguousOutcomeError, timer: Timer
    ) -> DialResult:
        """Hold an attempt whose placement never reported an outcome. Phase 9.

        Deliberately *not* released: releasing would mark it failed and free the
        prospect, and the prospect must stay blocked precisely because a call
        may be ringing their phone right now.
        """
        # A bare `TimeoutError()` has an empty message; `"".splitlines()` is
        # `[]`, and indexing it here left the attempt reserved and the
        # prospect blocked until recovery (found by the Phase 24 audit).
        lines = str(exc.cause).splitlines()
        reason = lines[0] if lines else type(exc.cause).__name__
        logger.error(
            event(
                "call.unresolved",
                latency_ms=timer.elapsed_ms,
                error=reason,
                outcome="held as UNRESOLVED; the prospect will not be dialled again until "
                "recovery resolves it",
            )
        )
        held = await self._service.store.mark_attempt_unresolved(queued.attempt.id, reason)
        return DialResult(
            queued=queued, attempt=held or queued.attempt, error=reason, ambiguous=True
        )

    async def _record_placement(
        self, queued: QueuedCall, snapshot: CallSnapshot, timer: Timer
    ) -> DialResult:
        """Record a placed call, refusing to attach a second call id to one attempt."""
        try:
            attempt = await self._service.store.mark_attempt_placed(
                queued.attempt.id,
                telephony_call_id=snapshot.call_id,
                provider=self._provider.name,
            )
        except CampaignStoreError as exc:
            # Two calls now exist for one attempt. The first one is the real
            # one — it is the one the attempt row points at, and the one the
            # bot will answer — so the one just placed is hung up rather than
            # left ringing a person nobody is expecting to talk to.
            logger.error(
                event(
                    "call.duplicate_placement",
                    call=snapshot.call_id,
                    error=str(exc),
                    outcome="hanging up the duplicate",
                )
            )
            CARRIER_FAILURES.inc(provider=self._provider.name, kind="duplicate_placement")
            await self._hang_up_quietly(snapshot.call_id)
            # Phase 13: released, not left live. The call this attempt made
            # has just been hung up, so nothing is ringing for it; an attempt
            # left `CALLING` here blocked its prospect and a concurrency slot
            # for ever, because recovery could only find the same call id.
            await self._service.release(queued, str(exc))
            return DialResult(queued=queued, error=str(exc))

        logger.info(
            event(
                "call.placed",
                call=snapshot.call_id,
                latency_ms=timer.elapsed_ms,
                outcome=snapshot.status.value,
            )
            + f" | campaign {queued.campaign.name!r} | {queued.prospect.full_name}"
        )
        await self._fulfil_callbacks(queued.prospect.id, queued.attempt.id)
        return DialResult(queued=queued, attempt=attempt, snapshot=snapshot)

    async def _hang_up_quietly(self, call_id: str) -> None:
        """End a call we should not have placed, without masking the reason we are here."""
        try:
            await self._provider.hang_up(call_id)
        except TelephonyError as exc:
            logger.error(
                event(
                    "call.duplicate_hangup_failed",
                    call=call_id,
                    error=str(exc),
                    outcome="A DUPLICATE CALL MAY STILL BE RINGING — hang it up by hand",
                )
            )

    async def _fulfil_callbacks(self, prospect_id: int, attempt_id: int) -> None:
        """Mark this prospect's pending callbacks as placed. Phase 7.

        The dial that just happened is the callback, whether or not it was the
        callback that put them back in the queue. Best effort: a database that
        predates the table logs a warning and the call proceeds regardless.
        """
        try:
            placed = await self._service.store.set_callbacks_status(
                prospect_id, CallbackStatus.PLACED
            )
        except CampaignStoreError as exc:
            logger.warning(f"CALLBACK | could not update callbacks for attempt {attempt_id}: {exc}")
            return
        if placed:
            logger.info(f"CALLBACK | attempt {attempt_id} fulfils {placed} scheduled callback(s)")

    async def refresh(self, attempt: CallAttempt) -> CallAttempt | None:
        """Ask the carrier where a call got to, and write that back.

        The reconciliation step: the carrier is the authority on what happened
        to a call, and this is how its answer becomes campaign state. An
        unrecognised carrier status leaves the attempt alone rather than
        guessing, exactly as `CallStatus.UNKNOWN` is deliberately not final.

        **Phase 9 made this idempotent rather than careful.** It used to check
        by hand that the attempt was not one of the three conversation outcomes,
        because the carrier reports "completed" for a call the person ended by
        asking never to be called again. That check is now a property of the
        write itself: `apply_call_event` refuses to move a final status, and
        those three are final. So a duplicate poll, a webhook arriving twice, or
        a reconciliation racing the bot's own teardown all land on the same row
        without any of them being able to undo another.

        The read is retried — reading cannot do anything twice — with a bounded
        policy and jittered backoff.

        Returns:
            The attempt as it stands, or None when there is nothing to ask about
            or the carrier could not be reached.
        """
        if not attempt.telephony_call_id:
            return None

        with call_context(
            CallContext(
                campaign_id=attempt.campaign_id,
                prospect_id=attempt.prospect_id,
                attempt_id=attempt.id,
                call_id=attempt.telephony_call_id,
                provider=self._provider.name,
                trace_id=attempt.trace_id,
            )
        ):
            try:
                snapshot = await call_with_retry(
                    lambda: self._provider.fetch_call(attempt.telephony_call_id or ""),
                    policy=READ_POLICY,
                    classify=read_classifier,
                    name=f"fetch_call({attempt.telephony_call_id})",
                )
            except TelephonyError as exc:
                logger.warning(event("call.refresh_failed", error=str(exc)))
                CARRIER_FAILURES.inc(provider=self._provider.name, kind="refresh")
                return None

            mapped = map_call_status(snapshot.status)
            if mapped is None:
                logger.debug(
                    event(
                        "call.status_ignored",
                        outcome=snapshot.status.value,
                        error="the carrier's status maps to nothing here",
                    )
                )
                return None
            mapped = machine_status(mapped, snapshot)
            # Phase 25: an unhappy ending always carries a reason on the row.
            # Carriers usually say why; when one does not, the status itself
            # is the reason, so a call history never shows a bare FAILED.
            reason = snapshot.error_message
            if reason is None and mapped in _UNHAPPY_ENDINGS:
                reason = f"the carrier reported {snapshot.status.value}" + (
                    f" (code {snapshot.error_code})" if snapshot.error_code else ""
                )

            updated, applied = await self._service.store.apply_call_event(
                attempt_id=attempt.id,
                status=mapped,
                duration_seconds=int(snapshot.duration_secs)
                if snapshot.duration_secs is not None
                else None,
                failure_reason=reason,
            )
            if not applied:
                return updated

            # The status is written; the *consequences* — the membership, the
            # prospect's status, the call result — are the service's, and
            # re-applying the same status there is a no-op write.
            return (
                await self._service.record_outcome(
                    updated or attempt,
                    mapped,
                    failure_reason=reason,
                    duration_seconds=int(snapshot.duration_secs)
                    if snapshot.duration_secs is not None
                    else None,
                )
                or updated
            )
