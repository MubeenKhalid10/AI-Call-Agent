"""Telephony: placing outbound phone calls, and knowing what happened to them.

The public surface of the package. Import from here, not from the modules
underneath — `make_provider` is the only thing that knows which carrier is
configured, and keeping that single point is what makes the carrier swappable.

    from src.telephony import CallRequest, make_provider, stream_url

    provider = make_provider(config.telephony)
    try:
        call = await provider.place_call(CallRequest(...))
    finally:
        await provider.close()

**Adding a carrier** is four small edits, none of them in `bot.py`:

1. Write `src/telephony/<name>.py` with a `TelephonyProvider` subclass.
2. Add its name to `SUPPORTED_TELEPHONY` in `config.py`.
3. Add its settings to `_TELEPHONY_CREDENTIALS` in `config.py`.
4. Add a branch to `make_provider` below.

`signalwire.py` is what that looks like when the carrier is Twilio-compatible:
one small file that overrides an API host and three strings. A carrier with its
own API is a longer file and the same four edits.

Pipecat can *receive* a call from more carriers than this can place one to —
`TELEPHONY_TRANSPORTS` lists the four it detects and serializes on its own,
which needs no code from us.
"""

from __future__ import annotations

from ..config import ConfigError, TelephonyConfig
from .base import (
    TELEPHONY_TRANSPORTS,
    WEBHOOK_AMD,
    WEBHOOK_STATUS,
    WEBHOOK_TRANSFER,
    CallRequest,
    CallSetupError,
    CallSnapshot,
    CallStatus,
    ProviderUnavailableError,
    TelephonyError,
    TelephonyProvider,
    TransferError,
    WebhookError,
    WebhookEvent,
    WebhookRequest,
    WebhookSignatureError,
    build_stream_twiml,
    build_transfer_twiml,
    is_e164,
    stream_url,
    transfer_response_twiml,
    webhook_url,
)
from .session import (
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    PARAM_DIRECTION,
    PARAM_FROM,
    PARAM_TO,
    CallSession,
)

__all__ = [
    "DIRECTION_INBOUND",
    "DIRECTION_OUTBOUND",
    "PARAM_DIRECTION",
    "PARAM_FROM",
    "PARAM_TO",
    "TELEPHONY_TRANSPORTS",
    "WEBHOOK_AMD",
    "WEBHOOK_STATUS",
    "WEBHOOK_TRANSFER",
    "CallRequest",
    "CallSession",
    "CallSetupError",
    "CallSnapshot",
    "CallStatus",
    "ProviderUnavailableError",
    "TelephonyError",
    "TelephonyProvider",
    "TransferError",
    "WebhookError",
    "WebhookEvent",
    "WebhookRequest",
    "WebhookSignatureError",
    "build_stream_twiml",
    "build_transfer_twiml",
    "is_e164",
    "make_provider",
    "stream_url",
    "transfer_response_twiml",
    "webhook_url",
]


def make_provider(
    config: TelephonyConfig, *, timeout_secs: float | None = None
) -> TelephonyProvider:
    """Build the configured carrier's provider.

    Credentials are checked here rather than at bot startup, because a bot that
    only ever answers browser calls should not need a carrier account to boot —
    and, since `transport.py` can answer a call without one too, neither should a
    bot that only ever *receives*. The trade is that a missing key surfaces when
    you first try to dial: one second into `call.py`, naming the variable.

    Args:
        config: Which carrier, and its credentials.
        timeout_secs: Ceiling on one HTTP request to the carrier (Phase 9).
            None uses the provider's own default.

    Raises:
        ConfigError: The provider is unknown, or its credentials are missing.
    """
    config.require_credentials()
    timeout = {"timeout_secs": timeout_secs} if timeout_secs is not None else {}

    if config.provider == "twilio":
        from .twilio import TwilioProvider

        return TwilioProvider(
            account_sid=config.credentials["account_sid"],
            auth_token=config.credentials["auth_token"],
            **timeout,
        )

    if config.provider == "signalwire":
        from .signalwire import SignalWireProvider

        return SignalWireProvider(
            project_id=config.credentials["project_id"],
            api_token=config.credentials["api_token"],
            space_url=config.credentials["space_url"],
            # Phase 14: the webhook signing key, when set. Not a credential the
            # provider needs to *dial*, so it is not in `require_credentials`.
            signing_key=config.webhook_signing_key,
            **timeout,
        )

    raise ConfigError(
        f"No telephony provider is implemented for TELEPHONY_PROVIDER={config.provider!r}."
    )
