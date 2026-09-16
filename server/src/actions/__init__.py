"""Actions: what the agent can do during a call, and the one place it is wired up.

`open_actions` is to Phase 7 what `open_briefing` is to Phase 6: `bot.py` calls
it with what the session has — the brief, the shared campaign store, the
retrieval stage, the phone call — and gets back an `ActionBackend` it can hand
to `SalesConversation` without knowing what is behind it. It never imports the
calendar package or builds a carrier provider itself.

    from src.actions import open_actions

    actions = await open_actions(config, brief=brief, store=briefing.store,
                                 knowledge=retriever, call=call)
    conversation = SalesConversation(brief, sink=briefing.sink, actions=actions, ...)
    ...
    await actions.close()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from ..config import Config, ConfigError
from ..conversation import CallBrief
from ..conversation.timeparse import resolve_timezone
from ..scheduling import CalendarError, make_calendar
from ..telephony import TelephonyProvider, make_provider
from .service import ActionService

if TYPE_CHECKING:
    from ..campaigns.store import CampaignStore
    from ..retrieval import KnowledgeRetriever
    from ..telephony.session import CallSession

__all__ = ["ActionService", "open_actions"]


async def open_actions(
    config: Config,
    *,
    brief: CallBrief,
    store: CampaignStore | None,
    knowledge: KnowledgeRetriever | None,
    call: CallSession | None,
) -> ActionService:
    """Build the action backend for one session from what the session has.

    Nothing here is fatal. A calendar that cannot be built, or a carrier whose
    provider cannot be constructed, switches that capability off with a log
    line and the call proceeds without it — the person has already answered the
    phone, and an agent that cannot book is better than one that cannot talk.

    Args:
        config: The bot's configuration.
        brief: Whose call this is.
        store: The campaign store the briefing opened, or None. Shared, not
            owned: the briefing closes it.
        knowledge: The retrieval stage, when the knowledge base is on.
        call: The phone call, or None for a browser or eval session.
    """
    tz = resolve_timezone(config.calendar.timezone)

    calendar = None
    if config.calendar.enabled:
        if config.calendar.provider == "local" and store is None:
            logger.warning(
                "ACTIONS | CALENDAR_PROVIDER is local but there is no campaign database, so the "
                "agent cannot book meetings on this session"
            )
        else:
            try:
                calendar = make_calendar(
                    config.calendar.provider,
                    tz=tz,
                    timezone_name=config.calendar.timezone,
                    slot_minutes=config.calendar.slot_minutes,
                    hours=config.calendar.hours,
                    min_notice_minutes=config.calendar.min_notice_minutes,
                    busy=store,
                    calcom_api_key=config.calendar.calcom_api_key,
                    calcom_event_type_id=config.calendar.calcom_event_type_id,
                    calcom_api_base=config.calendar.calcom_api_base,
                    calcom_timeout_secs=config.calendar.calcom_timeout_secs,
                )
            except (ValueError, CalendarError) as exc:
                logger.error(f"ACTIONS | the calendar could not be set up, so booking is off: {exc}")

    telephony: TelephonyProvider | None = None
    if call is not None and config.telephony.has_credentials and config.telephony.transfer_number:
        try:
            telephony = make_provider(config.telephony)
        except ConfigError as exc:
            logger.error(f"ACTIONS | no carrier provider for transfers: {exc}")
    elif call is not None and config.telephony.has_credentials:
        logger.info("ACTIONS | TELEPHONY_TRANSFER_NUMBER is not set, so the agent cannot transfer calls")

    service = ActionService(
        brief=brief,
        tz=tz,
        timezone_name=config.calendar.timezone,
        store=store,
        calendar=calendar,
        knowledge=knowledge,
        telephony=telephony,
        call=call,
        transfer_number=config.telephony.transfer_number,
        caller_id=config.telephony.from_number,
        calendar_max_days_ahead=config.calendar.max_days_ahead,
        callback_max_days_ahead=config.callback_max_days_ahead,
        # Phase 16: the transfer's outcome is reported to the Phase 14
        # receiver, when there is one to report to.
        transfer_action_url=config.telephony.webhook_url(),
        transfer_timeout_secs=config.telephony.transfer_timeout_secs,
    )
    logger.info(f"ACTIONS | {service.describe()}")
    return service
