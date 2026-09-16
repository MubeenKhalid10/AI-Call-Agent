"""Working out who this call is to, from whatever the transport happened to carry.

Phase 5 put three ids on the wire — the prospect, the campaign and the call
attempt ride into an outbound call as media-stream custom parameters — and
deliberately left them unread. This is where they are read.

**The names live here, not in the dialer.** `campaigns/dialer.py` imports them
from this module rather than the other way round, because a parameter's name is
a contract between a writer and a reader and the reader is the one that breaks
when it changes. It also keeps the dependency pointing the right way: the
conversation package knows nothing about campaigns, and the campaign package
knows about the conversation.

**Three sources, in order, and the third is not a failure.**

1. **The campaign database**, when an outbound call carried a prospect id and a
   `ProspectSource` is wired. The real path.
2. **The environment**, when it did not. `DEV_PROSPECT_*` lets a browser session
   or an eval scenario run against a named, fixed prospect, so personalisation
   can be tested headlessly and deterministically without a database.
3. **Nothing.** An anonymous brief, which renders into a prompt telling the
   agent it does not know who it is speaking to. That is the correct state for
   an inbound call, and it is emphatically not an error — see
   `brief.ProspectBrief.render` for what the agent is told to do about it.

A lookup that fails falls through to the next source rather than ending the
call. Somebody has already picked up the phone by this point; an agent that
knows nothing about them is worse than one that does, and infinitely better than
silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from .brief import CallBrief, CampaignBrief, ProspectBrief

# The custom parameters an outbound call carries on the carrier's media-stream
# handshake. Written by `campaigns/dialer.py`, read here.
PARAM_PROSPECT_ID = "prospect_id"
PARAM_CAMPAIGN_ID = "campaign_id"
PARAM_ATTEMPT_ID = "call_attempt_id"


@dataclass(frozen=True)
class CallIdentifiers:
    """The campaign rows an inbound media stream claims this call belongs to.

    All three are optional and all three are *claims*: they arrive as strings in
    a handshake and are trusted only as far as a database lookup, which is the
    thing that decides whether the prospect actually exists.
    """

    prospect_id: int | None = None
    campaign_id: int | None = None
    call_attempt_id: int | None = None

    def __bool__(self) -> bool:
        """Truthy when the call claims to belong to a campaign at all."""
        return self.prospect_id is not None


@runtime_checkable
class ProspectSource(Protocol):
    """Turns campaign ids into a brief, without the caller knowing how."""

    async def load(self, ids: CallIdentifiers, defaults: CampaignBrief) -> CallBrief | None:
        """Look up the call's brief.

        Args:
            ids: What the media stream claimed.
            defaults: Campaign settings from the environment, which a campaign's
                own `configuration` overlays field by field.

        Returns:
            The brief, or None when the ids resolved to nothing.
        """
        ...

    async def close(self) -> None:
        """Release whatever the source holds. Safe to call more than once."""
        ...


def identifiers_from_runner_args(runner_args: Any) -> CallIdentifiers:
    """Read the campaign ids off a session's runner arguments.

    Pipecat parses the carrier's handshake before the bot is called and exposes
    the custom parameters as `call_data.body`. Everything here tolerates their
    absence, because three of the four transports this project supports carry
    none of them.
    """
    call_data = getattr(runner_args, "call_data", None)
    body = getattr(call_data, "body", None) if call_data is not None else None
    if not isinstance(body, dict):
        # A websocket runner also exposes a plain `body` for non-telephony
        # clients; using it means a test harness can supply the ids too.
        body = getattr(runner_args, "body", None)
    if not isinstance(body, dict):
        return CallIdentifiers()

    return CallIdentifiers(
        prospect_id=_as_id(body.get(PARAM_PROSPECT_ID)),
        campaign_id=_as_id(body.get(PARAM_CAMPAIGN_ID)),
        call_attempt_id=_as_id(body.get(PARAM_ATTEMPT_ID)),
    )


async def resolve_brief(
    runner_args: Any,
    *,
    defaults: CampaignBrief,
    source: ProspectSource | None = None,
    fallback: ProspectBrief | None = None,
) -> CallBrief:
    """Build the brief for this call from the best source available.

    Args:
        runner_args: The session's runner arguments, carrying the handshake.
        defaults: Campaign settings from the environment.
        source: Where to look a prospect id up. None disables the database path
            entirely, which is what a bot with no campaign database gets.
        fallback: A prospect described in the environment, for development and
            eval runs. Used only when the database path produced nothing.

    Returns:
        Always a `CallBrief`, never None. See the module docstring for why an
        anonymous one is a valid answer rather than a failure.
    """
    ids = identifiers_from_runner_args(runner_args)

    if ids and source is not None:
        try:
            brief = await source.load(ids, defaults)
        except Exception:  # noqa: BLE001 - a live call must not die over a lookup
            logger.exception(
                f"PROSPECT | could not load prospect {ids.prospect_id} — "
                f"continuing without their details"
            )
            brief = None
        if brief is not None:
            return brief
        logger.warning(
            f"PROSPECT | the call carried prospect id {ids.prospect_id} but nothing was found; "
            f"the agent will not use a name"
        )
    elif ids and source is None:
        logger.warning(
            f"PROSPECT | the call carried prospect id {ids.prospect_id} but no prospect "
            f"database is configured (set DATABASE_URL)"
        )

    # `not ids` is the condition, not "the lookup failed". A call that carried a
    # prospect id and could not resolve it must stay anonymous: telling the
    # agent it is speaking to whoever `.env` last described would be the exact
    # fabrication this layer exists to prevent, and on a real call it would mean
    # greeting a stranger by a test fixture's name.
    if not ids and fallback is not None and not fallback.is_anonymous:
        return CallBrief(
            prospect=fallback,
            campaign=defaults,
            campaign_id=ids.campaign_id,
            call_attempt_id=ids.call_attempt_id,
            source="environment",
        )

    return CallBrief(
        prospect=ProspectBrief(prospect_id=ids.prospect_id),
        campaign=defaults,
        campaign_id=ids.campaign_id,
        call_attempt_id=ids.call_attempt_id,
        source="none",
    )


def _as_id(value: Any) -> int | None:
    """Read a handshake value as a positive row id, or None.

    Everything arrives as a string, and a carrier that passes an empty
    `<Parameter>` sends `""`. Anything that is not a positive integer is treated
    as absent rather than as an error: the call is already connected, and a
    malformed id is not worth ending it over.
    """
    if value is None:
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
