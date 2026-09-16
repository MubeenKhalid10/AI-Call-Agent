"""Building the transport for an incoming call, with the right serializer on it.

Pipecat's `create_transport` handles every other transport this bot has, and it
handles telephony too — but it picks the serializer itself, and that choice is
wired to Twilio specifically:

    params.serializer = TwilioFrameSerializer(
        stream_sid=..., call_sid=...,
        account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
        auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
    )

Two things follow from that, and both are fatal for a Twilio-*compatible*
carrier such as SignalWire:

1. Those environment variables will be empty, and `TwilioFrameSerializer`
   **raises** when they are — so the call fails during setup, before any audio,
   with a `ValueError` about `auto_hang_up`.
2. Even with them filled in, the serializer's automatic hang-up posts to
   `api.twilio.com`, which is the wrong company. The call would still end when
   the websocket closed, but every call would log an authentication error on the
   way out.

So this module builds the transport for a phone call, and the serializer comes
from the configured provider when there is one.

**And when there is not.** A bot with no carrier configured at all still has to
be able to answer — that is how `tests/fake_carrier.py` works, and it is what
somebody gets on their first run before they have signed up for anything.
Pipecat's path cannot do it, for the reason above. So this falls back to a
serializer built with `auto_hang_up=False`, which needs no credentials: the
audio works in both directions and the only thing given up is telling the
carrier to hang up over REST, which matters little because closing the websocket
ends the call anyway.

Everything else about the transport is Pipecat's: the handshake parse, the
websocket transport class, and the serializer implementations themselves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from .base import TelephonyProvider

if TYPE_CHECKING:
    from pipecat.serializers.base_serializer import FrameSerializer


async def create_provider_transport(
    runner_args: Any,
    provider: TelephonyProvider | None,
    params: FastAPIWebsocketParams,
) -> BaseTransport | None:
    """Build a telephony transport, with the best serializer available.

    Args:
        runner_args: The runner's `WebSocketRunnerArguments` for this call.
        provider: The configured carrier, or `None` when none is configured.
        params: Transport parameters, which this fills in.

    Returns:
        A transport, or `None` when the carrier that called is one neither the
        provider nor this module can serialize — in which case the caller should
        fall back to Pipecat's `create_transport` and let it report the problem.
    """
    # Reads the carrier's first two messages. The websocket's message stream is
    # single-use, but this is cached on the websocket, so `create_transport`
    # calling it again on the fallback path is free and safe.
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)

    # `create_transport` sets these two for the bot's benefit; on this path we
    # are the ones who have to, and `session.py` reads them straight after.
    runner_args.transport_type = transport_type
    runner_args.call_data = call_data

    if provider is not None and transport_type in provider.transports:
        serializer = provider.make_serializer(call_data)
    else:
        if provider is not None:
            logger.warning(
                f"CALL | a {transport_type} call arrived but TELEPHONY_PROVIDER is "
                f"{provider.name!r}, which speaks "
                f"{', '.join(provider.transports) or 'nothing'}"
            )
        serializer = _unauthenticated_serializer(transport_type, call_data)
        if serializer is None:
            return None

    # Telephony audio is never WAV-framed; the serializer owns the framing.
    params.add_wav_header = False
    params.serializer = serializer

    return FastAPIWebsocketTransport(websocket=runner_args.websocket, params=params)


def _unauthenticated_serializer(transport_type: str, call_data: Any) -> FrameSerializer | None:
    """A serializer for a carrier we hold no credentials for.

    Every one of Pipecat's telephony serializers refuses to be built without the
    carrier's credentials — but only because each defaults to hanging the call up
    over REST when the pipeline ends. Turning that off is all it takes to answer
    a call with nothing configured, and the cost is small: closing the websocket
    ends the call from the carrier's side regardless. What is lost is the
    graceful case, where the agent says goodbye and *then* the call is
    deliberately terminated.

    This is what makes the bot answerable out of the box, which
    `tests/fake_carrier.py` depends on and a first run benefits from.

    Returns:
        A serializer, or `None` for a carrier Pipecat does not recognise.
    """
    serializer = _build_unauthenticated(transport_type, call_data)
    if serializer is None:
        logger.error(f"CALL | no serializer for a {transport_type!r} media stream")
        return None

    logger.warning(
        f"CALL | answering a {transport_type} call with no carrier credentials. Audio works; "
        f"the call cannot be hung up over the carrier's API. Set TELEPHONY_PROVIDER and its "
        f"credentials in .env for the full path."
    )
    return serializer


def _build_unauthenticated(transport_type: str, call_data: Any) -> FrameSerializer | None:
    """Construct the credential-free serializer for one carrier, or None."""
    stream_id, call_id = call_data["stream_id"], call_data["call_id"]

    if transport_type == "twilio":
        from pipecat.serializers.twilio import TwilioFrameSerializer

        return TwilioFrameSerializer(
            stream_sid=stream_id,
            call_sid=call_id,
            params=TwilioFrameSerializer.InputParams(auto_hang_up=False),
        )

    if transport_type == "telnyx":
        from pipecat.serializers.telnyx import TelnyxFrameSerializer

        return TelnyxFrameSerializer(
            stream_id=stream_id,
            call_control_id=call_id,
            outbound_encoding=call_data.get("outbound_encoding", "PCMU"),
            inbound_encoding="PCMU",
            params=TelnyxFrameSerializer.InputParams(auto_hang_up=False),
        )

    if transport_type == "plivo":
        from pipecat.serializers.plivo import PlivoFrameSerializer

        return PlivoFrameSerializer(
            stream_id=stream_id,
            call_id=call_id,
            params=PlivoFrameSerializer.InputParams(auto_hang_up=False),
        )

    if transport_type == "exotel":
        from pipecat.serializers.exotel import ExotelFrameSerializer

        # Exotel's serializer has no hang-up path, so it needs nothing extra.
        return ExotelFrameSerializer(stream_sid=stream_id, call_sid=call_id)

    return None
