"""The one place the sales conversation meets the prospect database.

`dialer.py` is the module that knows about campaigns *and* carriers. This is the
module that knows about campaigns *and* conversations, and it exists for exactly
the same reason: so that neither of the two packages it joins has to know about
the other.

It implements the two Protocols `src/conversation/` defines and nothing else:

* `CampaignProspectSource` — three ids in, a `CallBrief` out. This is what makes
  the agent know it is calling Sarah at Meridian Logistics rather than "the
  caller".
* `CampaignConversationSink` — a do-not-call request, and a finished call's
  outcome, turned into writes against the Phase 5 tables.

**`bot.py` calls `open_briefing` and gets back two objects it can use without
knowing what is behind them.** It never imports `CampaignService`,
`CampaignStore`, or any of the models; the deepest it goes is a factory function
and a `close()`. That is the boundary the phase's architecture requirement asks
for, and it is one import line away from being violated, so it is worth stating
plainly: nothing in `bot.py` should ever import from `src.campaigns` except this
module's `open_briefing`.

**Everything here fails soft.** A prospect lookup happens while a phone is
ringing and an outcome write happens as a call ends; neither is worth dropping a
call over. A failure logs and returns the honest answer — no brief, nothing
stored — and the agent carries on knowing less.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from ..compliance.dnc import DncSource
from ..compliance.policy import PolicyResolver
from ..conversation import CallBrief, CallIdentifiers, CampaignBrief, ProspectBrief
from ..monitoring.instruments import CALL_RESULTS
from ..security import AuditLog, Principal, Role
from .models import CallAttempt, CallAttemptStatus, Prospect
from .results import (
    CallResult,
    CallResultValidationError,
    attempt_status_for,
    build_conversation_result,
)
from .service import CampaignService
from .store import CampaignStore, CampaignStoreError

# Where a prospect's own notes might be, in the free-form `custom_data` a CSV
# import keeps unrecognised columns in. Checked in this order; the first one
# present wins. Anything else in `custom_data` is left alone — it is the
# customer's data, not ours to interpret.
_NOTE_KEYS = ("notes", "note", "comments", "background", "context")

# Which conversations set the attempt's status is `results.attempt_status_for`:
# the three states that mean the prospect said something worth recording,
# read from the state *path* — because a call the agent closes properly ends
# in ENDING, and the state before the goodbye is the one that counts. (Phase 6
# read only the final state, so a callback followed by a goodbye was recorded
# as a plain COMPLETED; Phase 8 fixed that.) Anything else leaves the status
# to `dialer.refresh`, which reconciles it from the carrier — a call the agent
# simply finished is `COMPLETED` either way, and there is no reason for two
# writers to race over it.


class CampaignProspectSource:
    """Turns the ids on a media stream into everything the agent should know."""

    def __init__(self, service: CampaignService) -> None:
        """Create the source.

        Args:
            service: The campaign service. Held rather than a bare store so
                that anything needing a rule — the compliance policy for the
                call, since Phase 19 — has one to hand.
        """
        self._service = service
        # Phase 19: the disclosures the opening was instructed to make are
        # a compliance decision, and recorded like one.
        self._audit = AuditLog(lambda: service.store, keep_recent=50)
        self._principal = Principal(name="bot", role=Role.OPERATOR, via="process")

    async def load(self, ids: CallIdentifiers, defaults: CampaignBrief) -> CallBrief | None:
        """Build the brief for a call that arrived carrying campaign ids.

        Args:
            ids: What the handshake claimed. Only `prospect_id` is required;
                the campaign is looked up when it is there, and its absence
                simply means the environment's campaign settings are used.
            defaults: Campaign settings from `.env`, overlaid per field by the
                campaign's own `configuration`.

        Returns:
            The brief, or None when the prospect id resolved to nothing — which
            is a real possibility for a stale call and must not be an exception,
            because somebody has already answered the phone.
        """
        if ids.prospect_id is None:
            return None

        store = self._service.store
        prospect = await store.get_prospect(ids.prospect_id)
        if prospect is None:
            return None

        campaign_brief = defaults
        campaign = None
        if ids.campaign_id is not None:
            campaign = await store.get_campaign(ids.campaign_id)
            if campaign is not None:
                campaign_brief = CampaignBrief.from_configuration(
                    campaign.configuration,
                    defaults=CampaignBrief(
                        agent_name=defaults.agent_name,
                        company_name=defaults.company_name,
                        company_description=defaults.company_description,
                        services=list(defaults.services),
                        campaign_name=campaign.name,
                        offer=defaults.offer,
                        value_points=defaults.value_points,
                        qualification_criteria=defaults.qualification_criteria,
                        meeting_ask=defaults.meeting_ask,
                        notes=defaults.notes,
                    ),
                )

        notes = await self._notes_for(prospect, exclude_attempt_id=ids.call_attempt_id)

        # Phase 19: the disclosures for *this* call — the campaign's policy,
        # then the number's jurisdiction — as words on the brief, and a row
        # saying what the opening was instructed to include.
        policy = self._service.policy_for_call(campaign, prospect)
        disclosures = policy.disclosures()
        if disclosures != list(campaign_brief.disclosures):
            campaign_brief = dataclasses.replace(campaign_brief, disclosures=disclosures)
        try:
            await self._audit.record(
                "compliance.disclosure",
                principal=self._principal,
                target=("prospect", prospect.id),
                outcome="instructed" if disclosures else "none required",
                attempt=ids.call_attempt_id,
                campaign=ids.campaign_id,
                jurisdiction=policy.jurisdiction,
                disclosures=disclosures,
            )
        except Exception:  # noqa: BLE001 - a phone is ringing; the log line is the record
            logger.exception("COMPLIANCE | the disclosure audit row could not be written")

        return CallBrief(
            prospect=ProspectBrief(
                prospect_id=prospect.id,
                first_name=prospect.first_name or None,
                last_name=prospect.last_name or None,
                company=prospect.company,
                job_title=prospect.job_title,
                industry=prospect.industry,
                location=prospect.location,
                phone=prospect.phone_normalized or prospect.phone,
                email=prospect.email,
                notes=notes,
            ),
            campaign=campaign_brief,
            campaign_id=ids.campaign_id,
            call_attempt_id=ids.call_attempt_id,
            source="campaign",
        )

    async def _notes_for(self, prospect: Prospect, *, exclude_attempt_id: int | None) -> list[str]:
        """Prior notes about this person, from the import and from earlier calls.

        Two sources, both bounded and both factual:

        * whatever the CSV had in a notes column, which the importer kept in
          `custom_data` rather than dropping;
        * the previous call's own record — what they said they needed, and what
          was agreed — which is the difference between "hello again" and "last
          time you mentioned the March renewal".

        Only *what was recorded* is passed on; nothing here summarises or
        embellishes, because the agent is told these are facts and will use them
        as such.
        """
        notes: list[str] = []

        # Phase 23: the importer keeps an unmapped column under its own
        # header, so a CSV's "Notes" column landed as `custom_data["Notes"]`
        # and a case-sensitive lookup never found it. Matched by lowered key.
        lowered = {str(key).strip().lower(): value for key, value in (prospect.custom_data or {}).items()}
        for key in _NOTE_KEYS:
            value = lowered.get(key)
            if isinstance(value, str) and value.strip():
                notes.append(value.strip())
                break

        try:
            attempts = await self._service.store.list_attempts(prospect_id=prospect.id, limit=5)
        except CampaignStoreError as exc:
            logger.warning(f"PROSPECT | could not read call history: {exc}")
            return notes

        for attempt in attempts:
            if attempt.id == exclude_attempt_id or not attempt.conversation_data:
                continue
            summary = _previous_call_note(attempt.conversation_data)
            if summary:
                notes.append(summary)
                break  # The most recent one only. Older calls are history, not context.

        return notes

    async def close(self) -> None:
        """Nothing of its own to release; `open_briefing` owns the store."""
        return None


class CampaignConversationSink:
    """Writes a conversation's consequences into the campaign tables."""

    def __init__(self, service: CampaignService) -> None:
        """Create the sink.

        Args:
            service: The campaign rules. Used rather than the store directly
                because marking do-not-call has to close the person's open
                memberships as well as set their status, and that rule lives in
                the service.
        """
        self._service = service

    async def on_do_not_call(self, brief: CallBrief, reason: str) -> bool:
        """Mark the prospect as never to be called again, immediately.

        Called mid-call, the moment the request is recognised, and it does the
        urgent half only: the person's status and their open memberships. The
        *attempt's* status is written at the end of the call by
        `on_call_finished`, because an attempt is not over while somebody is
        still on the line.
        """
        if brief.prospect_id is None:
            # Phase 19: no prospect row, but there may be a number — an
            # inbound caller, or a call placed outside the campaign tables.
            # The list is keyed by number, so the request is recorded there.
            number = brief.prospect.phone
            if number:
                try:
                    entry, inserted, marked = await self._service.add_do_not_call_number(
                        number,
                        source=DncSource.VERBAL,
                        reason=reason,
                        actor="bot",
                        campaign_id=brief.campaign_id,
                        call_attempt_id=brief.call_attempt_id,
                    )
                except (CampaignStoreError, ValueError) as exc:
                    logger.error(
                        f"DNC | {brief.describe()} asked not to be called again ({reason}); "
                        f"the number could not be listed — THE REQUEST IS NOT RECORDED: {exc}"
                    )
                    return False
                logger.info(
                    f"DNC | {entry.phone_normalized} {'added to' if inserted else 'already on'} the "
                    f"do-not-call list from an anonymous call ({reason}); {marked} prospect(s) marked"
                )
                return True
            logger.warning(
                f"DNC | {brief.describe()} asked not to be called again ({reason}), but this "
                f"call carries no prospect id and no number — nothing to mark"
            )
            return False

        try:
            marked = await self._service.mark_do_not_call(
                brief.prospect_id,
                source=DncSource.VERBAL,
                reason=reason,
                actor="bot",
                campaign_id=brief.campaign_id,
                call_attempt_id=brief.call_attempt_id,
            )
        except CampaignStoreError:
            logger.exception(
                f"DNC | could not mark prospect {brief.prospect_id} — THE REQUEST IS NOT RECORDED"
            )
            return False

        if not marked:
            logger.warning(f"DNC | prospect {brief.prospect_id} does not exist")
            return False
        logger.info(f"DNC | prospect {brief.prospect_id} marked DO_NOT_CALL ({reason})")
        return True

    async def on_call_finished(self, brief: CallBrief, outcome: dict[str, Any]) -> bool:
        """Store the conversation on its call attempt, and set the status it implies.

        Three writes, deliberately separate:

        * `conversation_data` always, whatever happened. It is the raw record of
          the call — the outcome exactly as the conversation produced it,
          transcript included — and the source everything else here is
          derived from.
        * the attempt's `status` only for the three outcomes that come from what
          the person *said* — do-not-call, callback, not interested. Everything
          else is left to `dialer.refresh`, which reconciles from the carrier;
          there is nothing to gain from two writers racing over "the call
          completed".
        * the `CallResult` (Phase 8): the validated, CRM-ready reading of the
          record, one row per attempt. Built from the same outcome, so it can
          always be rebuilt from `conversation_data` if its shape changes.
        """
        attempt_id = brief.call_attempt_id
        if attempt_id is None:
            logger.info(f"OUTCOME | {brief.describe()} | not stored: no call attempt id")
            return False

        stored = False
        try:
            stored = await self._service.store.save_conversation_data(attempt_id, outcome)
        except CampaignStoreError as exc:
            logger.error(f"OUTCOME | could not store the conversation: {exc}")

        attempt = await self._service.store.get_attempt(attempt_id)
        status = attempt_status_for(outcome)
        if status is not None and attempt is not None:
            attempt = (
                await self._service.record_outcome(
                    attempt,
                    status,
                    duration_seconds=_duration(outcome),
                    # The rich result is written just below; a thin carrier one
                    # a moment earlier would only be overwritten.
                    write_result=False,
                )
                or attempt
            )
            logger.info(f"OUTCOME | attempt {attempt_id} recorded as {status.value}")

        if attempt is not None:
            await self._store_usage(attempt.id, outcome)
            await self._store_result(attempt, outcome, status)

        # Phase 7: a callback the backend actually scheduled becomes a queued
        # call. `record_outcome` has just closed the membership as COMPLETED —
        # "we reached them" — which is true, and also not the end of it, because
        # they asked to be phoned again at a time they chose. Reopening the
        # membership for that moment is what turns the promise into a dial: the
        # queue already orders by `next_attempt_at` and refuses anything not yet
        # due. Done here rather than when the callback was created because the
        # attempt was still live then, and the outcome write would have undone it.
        scheduled = _scheduled_callback(outcome)
        if scheduled is not None and attempt is not None and attempt.campaign_prospect_id is not None:
            try:
                reopened = await self._service.store.reopen_membership(
                    attempt.campaign_prospect_id, next_attempt_at=scheduled
                )
            except CampaignStoreError as exc:
                logger.error(f"CALLBACK | could not reopen the membership: {exc}")
            else:
                if reopened:
                    logger.info(
                        f"CALLBACK | membership {attempt.campaign_prospect_id} queued again for "
                        f"{scheduled:%Y-%m-%d %H:%M %Z}"
                    )

        return stored

    async def _store_usage(self, attempt_id: int, outcome: dict[str, Any]) -> None:
        """Record what the call consumed, on the attempt row. Phase 11.

        Best effort and never raises: the call is over, and losing the token
        count is not worth failing a teardown that still has a result to write.
        A call that measured nothing — one that never reached a provider —
        writes nothing rather than a row of zeros.
        """
        usage = outcome.get("usage")
        if not isinstance(usage, dict) or not usage:
            return
        cost = outcome.get("cost")
        total = cost.get("total_usd") if isinstance(cost, dict) else None
        record = {**usage, "cost": cost}
        # Phase 20: a compact copy of what Phase 12 measured, beside the usage,
        # so the dashboard's latency and error figures are one small JSON
        # field per row rather than a scan of every transcript.
        quality = _quality_summary(outcome.get("quality"))
        if quality:
            record["quality"] = quality
        try:
            await self._service.store.save_call_usage(attempt_id, record, cost_usd=total)
        except CampaignStoreError as exc:
            logger.warning(f"USAGE | attempt {attempt_id} | not recorded: {exc}")

    async def _store_result(
        self, attempt: CallAttempt, outcome: dict[str, Any], status: CallAttemptStatus | None
    ) -> CallResult | None:
        """Build the call's structured result and store it. Phase 8.

        Never raises: the call is over, and a result that cannot be stored is
        a loud log line, not a crash in teardown. The two ways it fails are
        kept apart in the log because they need different people: a
        validation failure is a bug in the builder or a record edited by hand;
        a store error is a database that needs `campaign.py init`.
        """
        call_status = status or (
            attempt.status if attempt.status.is_final else CallAttemptStatus.COMPLETED
        )
        try:
            result = build_conversation_result(attempt, outcome, call_status=call_status)
        except Exception:  # noqa: BLE001 - the builder is tolerant; this is a last line
            logger.exception(f"RESULT | attempt {attempt.id} | could not build the call result")
            return None
        if result.issues:
            logger.warning(
                f"RESULT | attempt {attempt.id} | {len(result.issues)} field(s) could not be read "
                f"and were left unknown: " + "; ".join(result.issues)
            )
        try:
            saved = await self._service.store.save_call_result(result)
        except CallResultValidationError as exc:
            logger.error(f"RESULT | attempt {attempt.id} | NOT STORED — {exc}")
            return None
        except CampaignStoreError as exc:
            logger.error(f"RESULT | attempt {attempt.id} | NOT STORED — {exc}")
            return None
        if saved is None:
            logger.error(f"RESULT | attempt {attempt.id} | the store refused the conversation's result")
            return None
        # Phase 22: every result the conversation wrote, by disposition —
        # the "call success" figure, counted where it is decided.
        CALL_RESULTS.inc(outcome=saved.disposition.value)
        logger.info(
            f"RESULT | attempt {attempt.id} | {saved.disposition.value} | "
            f"qualified={saved.qualification_status.value} next={saved.next_action.value} | "
            f"{len(saved.transcript)} transcript turn(s), {len(saved.tool_actions)} action(s)"
        )
        return saved

    async def close(self) -> None:
        """Nothing of its own to release; `open_briefing` owns the store."""
        return None


class Briefing:
    """The pair `bot.py` needs, and the store they share.

    A tiny object rather than a tuple so that closing is one call and cannot be
    forgotten for one half of the pair.
    """

    def __init__(
        self,
        source: CampaignProspectSource | None,
        sink: CampaignConversationSink | None,
        store: CampaignStore | None,
    ) -> None:
        """Prefer `open_briefing`; this takes already-built parts."""
        self.source = source
        self.sink = sink
        self._store = store

    @property
    def store(self) -> CampaignStore | None:
        """The shared store, for the action service to write callbacks and meetings through.

        Phase 7. Exposed rather than opening a third pool per session; the
        action service holds it for the session and never closes it — that is
        this object's job, in `close`.
        """
        return self._store

    async def close(self) -> None:
        """Close the connection pool. Safe to call more than once."""
        if self._store is not None:
            await self._store.close()
            self._store = None


async def open_briefing(
    database_url: str | None,
    *,
    default_region: str | None = None,
    max_attempts: int = 3,
    retry_minutes: float = 60.0,
    pool=None,
    compliance: PolicyResolver | None = None,
) -> Briefing:
    """Connect to the prospect database, if there is one to connect to.

    Args:
        database_url: `DATABASE_URL`. None disables the campaign path entirely,
            which is what a bot configured only for the browser gets.
        default_region: Passed through to the service; unused on this path, but
            constructing the service without it would leave a differently
            configured object in the same process.
        max_attempts: Passed through to the service.
        retry_minutes: Passed through to the service.
        compliance: Phase 19. The policy resolver, so the brief carries the
            disclosures for the call and an opt-out lands on the list with
            the right ceiling and waits applied afterwards.
        pool: An existing connection pool to borrow rather than opening one
            (Phase 11). `bot.py` passes the knowledge base's pool when both
            point at the same database; the store then does not close it, and
            `Briefing.close` leaves it to whoever opened it.

    Returns:
        A `Briefing`. Both halves are `None` when there is no database or it
        could not be reached — which is a working configuration, not a failure:
        the agent runs anonymously and says so. This deliberately does not raise
        the way the knowledge base's preflight does, because the knowledge base
        is what the agent answers *from* and the prospect database only decides
        how well it knows who it is talking to.
    """
    if not database_url:
        return Briefing(None, None, None)

    try:
        store = await CampaignStore.connect(database_url, max_size=2, pool=pool)
    except CampaignStoreError as exc:
        logger.warning(
            f"PROSPECT | no prospect database on this call — the agent will not know who it is "
            f"calling.\n  {exc}"
        )
        return Briefing(None, None, None)

    service = CampaignService(
        store,
        default_region=default_region,
        max_attempts=max_attempts,
        retry_minutes=retry_minutes,
        compliance=compliance,
    )
    return Briefing(CampaignProspectSource(service), CampaignConversationSink(service), store)


def _quality_summary(quality: Any) -> dict[str, Any]:
    """The few figures a dashboard wants from a Phase 12 call report. Phase 20.

    The report itself stays in `conversation_data`, turns and all; this is
    the per-call latency (the `total` stage's median, p95 and the greeting)
    and the counts of what went wrong, small enough to aggregate over every
    row without reading a transcript.
    """
    if not isinstance(quality, dict):
        return {}
    latency = quality.get("latency") if isinstance(quality.get("latency"), dict) else {}
    stages = latency.get("stages") if isinstance(latency.get("stages"), dict) else {}
    total = stages.get("total") if isinstance(stages.get("total"), dict) else {}
    errors = quality.get("errors")
    summary = {
        "responses": _int_or_none(latency.get("responses")),
        "greeting_ms": _int_or_none(latency.get("greeting_ms")),
        "p50_ms": _int_or_none(total.get("p50_ms")),
        "p95_ms": _int_or_none(total.get("p95_ms")),
        "max_ms": _int_or_none(total.get("max_ms")),
        "failed_turns": _int_or_none(quality.get("failed_turn_count")),
        "late_turns": _int_or_none(quality.get("late_turn_count")),
        "barge_ins": _int_or_none(quality.get("barge_in_count")),
        "spurious_interruptions": _int_or_none(quality.get("spurious_interruptions")),
        "errors": len(errors) if isinstance(errors, list) else 0,
    }
    return {k: v for k, v in summary.items() if v is not None}


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(round(value))


def _duration(outcome: dict[str, Any]) -> int | None:
    """The call's duration for the attempt row: the phone call's, else the conversation's."""
    for key in ("call_duration_secs", "duration_secs"):
        value = outcome.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(round(value))
    return None


def _scheduled_callback(outcome: dict[str, Any]) -> datetime | None:
    """The moment the backend scheduled a callback for, if it did. Phase 7.

    Read from `qualification.callback_scheduled_for`, which only a successful
    `schedule_callback` sets — `callback_when` next to it is what the prospect
    *said* and is deliberately not used here, because "next week" is not a time
    the queue can dial at.
    """
    qualification = outcome.get("qualification")
    if not isinstance(qualification, dict):
        return None
    value = qualification.get("callback_scheduled_for")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        logger.warning(f"CALLBACK | unreadable scheduled time {value!r} in the call record")
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _previous_call_note(data: dict[str, Any]) -> str:
    """One line describing an earlier call, from the record it left behind.

    Deliberately terse and deliberately factual. It goes into the prompt as a
    fact about the person, so it says only what was recorded: the outcome, what
    they said hurt, and what was agreed. Anything vaguer would be an invitation
    to the model to elaborate on it.
    """
    qualification = data.get("qualification")
    if not isinstance(qualification, dict):
        return ""

    parts: list[str] = []
    state = data.get("final_state")
    if isinstance(state, str) and state not in ("ENDING", "GREETING"):
        parts.append(f"last call ended in {state.replace('_', ' ').lower()}")

    pain = qualification.get("pain_points")
    if isinstance(pain, list) and pain:
        parts.append(f"they mentioned {pain[0]}")

    next_action = qualification.get("next_action")
    if isinstance(next_action, str) and next_action not in ("UNKNOWN", "NONE"):
        parts.append(f"agreed next step was {next_action.replace('_', ' ').lower()}")

    if not parts:
        return ""
    return "From an earlier call: " + "; ".join(parts) + "."
