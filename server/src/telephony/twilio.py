"""Twilio: the only file in this project that knows Twilio exists.

Everything vendor-specific is here — the REST endpoints, the parameter names,
the status vocabulary and the error codes. The rest of the codebase talks to
`TelephonyProvider` and never imports this module directly.

**Why the REST API and not the `twilio` SDK.** The official SDK is synchronous,
and a blocking HTTP call inside an asyncio voice pipeline stalls audio. The
three requests needed here are a POST, a GET and another POST; `aiohttp` is
already a dependency because Pipecat uses it, so the SDK would add a package and
a threadpool to avoid writing thirty lines.

**Why the TwiML is inline rather than a webhook URL.** Twilio's `Twiml`
parameter takes the markup directly on the call-creation request, which means an
outbound call needs no second public endpoint and no round trip back to us
before it can dial. The bot's `/ws` route still has to be publicly reachable —
that is where the audio goes — but nothing else does.

**Status callbacks (Phase 14).** When a `CallRequest` carries a
`status_callback_url`, the call is created with `StatusCallback` and every
lifecycle event asked for, so Twilio POSTs `initiated`, `ringing`, `answered`
and the terminal status to us as they happen. Each delivery is signed:
`X-Twilio-Signature` is a base64 HMAC-SHA1 over the delivered URL followed by
every form field, sorted by name, keyed by the account's auth token — the
mechanism Twilio's own helper libraries implement, reproduced in
`compute_signature` so that nothing here depends on the synchronous SDK.
`verify_webhook` checks it, `parse_webhook` turns the fields into a
`WebhookEvent`, and the same two work for every Twilio-compatible carrier that
signs the same way.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from loguru import logger

from ..voicemail import normalize_answered_by
from .base import (
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
)

if TYPE_CHECKING:
    from pipecat.serializers.base_serializer import FrameSerializer

# Everything before the API version. Kept separate from the version because
# Pipecat's serializer wants the host on its own — it appends the version and
# the rest of the path itself — and a subclass pointing at a Twilio-compatible
# carrier only ever needs to change this part.
DEFAULT_API_BASE = "https://api.twilio.com"
API_VERSION = "2010-04-01"

# Twilio's status words, mapped onto ours. `initiated` appears on status
# callbacks rather than on the call resource, but it is cheap to accept and its
# absence would otherwise read as UNKNOWN.
_STATUSES = {
    "queued": CallStatus.QUEUED,
    "initiated": CallStatus.QUEUED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.ANSWERED,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.BUSY,
    "no-answer": CallStatus.NO_ANSWER,
    "failed": CallStatus.FAILED,
    "canceled": CallStatus.CANCELED,
    "cancelled": CallStatus.CANCELED,
}

# The error codes worth translating, because each one has a specific fix and the
# carrier's own message does not say what it is. Anything not listed is reported
# with the carrier's text and a link to its documentation.
#
# `{brand}` and `{creds}` are filled in per provider, so a SignalWire failure
# names SignalWire and the SignalWire environment variables. The *codes* are
# shared because SignalWire's compatibility API reproduces Twilio's error
# numbering along with the rest of the API.
_ERROR_HINTS = {
    "20003": ("{brand} rejected the credentials. Check {creds} in server/.env."),
    "21205": (
        "{brand} could not use the audio stream URL. TELEPHONY_PUBLIC_URL must be a public "
        "https address that reaches this machine (a tunnel such as `ngrok http 7860`)."
    ),
    "21210": (
        "The From number is not one this {brand} account owns or has verified. Buy the number "
        "in the {brand} console, or verify it, then set TELEPHONY_FROM_NUMBER to it."
    ),
    "21211": (
        "The To number was not accepted. It must be in E.164 form, e.g. +923001234567 — "
        "country code, no spaces, no leading zero."
    ),
    "21215": (
        "This {brand} account is not permitted to call that country. Enable the destination "
        "under the account's geographic permissions. Trial accounts start with almost "
        "everything switched off."
    ),
    "21219": (
        "The To number is not verified. A trial account can usually only call numbers you "
        "have verified in the {brand} console."
    ),
    "21606": (
        "The From number cannot place outbound calls. Check the number is voice-capable and "
        "that it is not an incoming-only or messaging-only number."
    ),
    "21220": "The call is no longer in progress, so it cannot be redirected.",
}

# Error codes that mean "the call is over", which for a transfer is a specific
# failure — the person hung up while the agent was offering to connect them —
# rather than a configuration problem.
_CALL_GONE = ("20404", "21220")

# Phase 14: the lifecycle events a status callback is asked for. Twilio sends
# only `completed` unless told otherwise, and each value goes as its own
# repeated `StatusCallbackEvent` field, which is how the REST API wants a list.
STATUS_CALLBACK_EVENTS = ("initiated", "ringing", "answered", "completed")


def compute_signature(secret: str, url: str, params: Any) -> str:
    """Twilio's request signature: base64(HMAC-SHA1(secret, url + sorted fields)).

    The string signed is the URL exactly as delivered, followed by every form
    field's name and value concatenated in name order — raw values, not
    URL-encoded. This is what Twilio's helper libraries compute, and what a
    Twilio-compatible carrier's helper computes with its own key.

    Args:
        secret: The auth token (Twilio) or signing key (SignalWire).
        url: The URL the carrier requested, query string included.
        params: The delivered form fields. A value may be a list, for a
            repeated field; each is appended in sorted order.
    """
    text = url
    if params:
        for name in sorted(set(params.keys())):
            value = params[name]
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in sorted(str(v) for v in values):
                text += name + item
    digest = hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii").strip()


def _url_variants(url: str) -> list[str]:
    """The URL as given, plus the same URL with the default port added or removed.

    Twilio signs the URL it requested, and whether that carries an explicit
    `:443` has varied between its own products over the years, so its helper
    libraries check both forms. So does this.
    """
    split = urlsplit(url)
    host = split.hostname or ""
    if not host:
        return [url]
    default_port = 443 if split.scheme == "https" else 80
    port = split.port or default_port
    auth = f"{split.username}:{split.password}@" if split.username else ""
    with_port = urlunsplit(split._replace(netloc=f"{auth}{host}:{port}"))
    without_port = urlunsplit(split._replace(netloc=f"{auth}{host}"))
    variants = [url]
    for candidate in (without_port, with_port):
        if candidate not in variants:
            variants.append(candidate)
    return variants


class TwilioProvider(TelephonyProvider):
    """Places outbound calls through Twilio's Programmable Voice REST API."""

    name = "twilio"
    transports = ("twilio",)

    #: How the carrier is named in an error message, and which environment
    #: variables that message should tell you to look at. A Twilio-compatible
    #: carrier overrides these three and inherits everything else — see
    #: `signalwire.py`, which is what keeps them from being hard-coded strings.
    brand = "Twilio"
    credential_hint = "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN"
    error_docs = "https://www.twilio.com/docs/api/errors/{code}"

    #: Phase 14. Where the carrier puts the signature, in order of preference,
    #: and which environment variable holds the secret it is keyed by — for
    #: the message when a delivery cannot be checked. A compatible carrier
    #: overrides both; see `signalwire.py`.
    signature_headers: tuple[str, ...] = ("x-twilio-signature",)
    webhook_secret_hint = "TWILIO_AUTH_TOKEN"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        session: aiohttp.ClientSession | None = None,
        timeout_secs: float = 20.0,
        webhook_secret: str | None = None,
    ) -> None:
        """Create the provider.

        Args:
            account_sid: Twilio account SID (`AC...`).
            auth_token: Twilio auth token. Never logged.
            api_base: API host, *without* the `/2010-04-01` version segment —
                for a regional edge, a test double, or a Twilio-compatible
                carrier. See `signalwire.py`.
            session: An existing HTTP session to use. When omitted, one is
                created on first use and closed by `close()`. The tests pass a
                stub here; nothing else needs to.
            timeout_secs: Ceiling on one HTTP request (Phase 9). Without it
                aiohttp waits five minutes by default, which for `place_call`
                means a dialer that looks hung and, worse, an outcome nobody
                learns for five minutes. A request that exceeds this raises
                `ProviderUnavailableError` — and for a *placement* the caller
                must treat that as ambiguous rather than as a failure, because
                the request may have been received.
            webhook_secret: Phase 14. What the carrier keys its request
                signatures with. Twilio uses the auth token itself, so the
                default is right here; a compatible carrier with a separate
                signing key passes it. `None` on such a carrier means
                deliveries cannot be verified, and `verify_webhook` refuses
                them rather than guessing.
        """
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._api_base = api_base.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._timeout_secs = timeout_secs
        self._webhook_secret = auth_token if webhook_secret is None else webhook_secret

    def describe(self) -> str:
        """One line for the startup log. Shows the SID's tail, never the token."""
        return f"{self.name} (account …{self._account_sid[-4:]})"

    def make_serializer(self, call_data: Any) -> FrameSerializer:
        """Build the Media Streams serializer for one call.

        `base_url` is the whole reason this method exists rather than letting
        Pipecat's runner pick a serializer: it is what makes the serializer's
        automatic hang-up talk to *this* carrier's API instead of to
        `api.twilio.com`. On the Twilio path it resolves to the same host the
        runner would have used; on a compatible carrier it does not.
        """
        from pipecat.serializers.twilio import TwilioFrameSerializer

        return TwilioFrameSerializer(
            stream_sid=call_data["stream_id"],
            call_sid=call_data["call_id"],
            account_sid=self._account_sid,
            auth_token=self._auth_token,
            base_url=self._api_base,
        )

    async def place_call(self, request: CallRequest) -> CallSnapshot:
        """Dial a number and connect it to this bot's websocket."""
        payload = {
            "To": request.to_number,
            "From": request.from_number,
            "Twiml": build_stream_twiml(request),
            # Twilio's own default is 60s, which is long enough for most numbers
            # to reach voicemail. Ours is shorter and configurable, because on a
            # cold call a no-answer is a result, not a failure to wait harder.
            "Timeout": str(request.answer_timeout_secs),
        }
        # Phase 12: answering-machine detection, when asked for. `Enable`
        # rather than `DetectMessageEnd` because nothing here waits for the
        # greeting to end before acting — the bot decides that itself. With
        # `AsyncAmd` the call connects at once and the verdict lands on the
        # call resource's `answered_by` a few seconds later, which is what
        # `fetch_call` reads back; without it the carrier holds the call until
        # it has decided. SignalWire's Compatibility API takes the same two
        # parameters.
        if request.machine_detection in ("async", "sync"):
            payload["MachineDetection"] = "Enable"
            if request.machine_detection == "async":
                payload["AsyncAmd"] = "true"

        # Phase 14: ask for every lifecycle event to be pushed to us. The
        # events go as repeated fields — the REST API's list form — so from
        # here on the body is a list of pairs rather than a dict. An async
        # AMD verdict has its own callback, pointed at the same receiver, which
        # is how `answered_by` reaches the attempt without a poll.
        fields: list[tuple[str, str]] = list(payload.items())
        if request.status_callback_url:
            fields.append(("StatusCallback", request.status_callback_url))
            fields.append(("StatusCallbackMethod", "POST"))
            fields.extend(("StatusCallbackEvent", name) for name in STATUS_CALLBACK_EVENTS)
            if request.machine_detection == "async":
                fields.append(("AsyncAmdStatusCallback", request.status_callback_url))
                fields.append(("AsyncAmdStatusCallbackMethod", "POST"))
        data = await self._request("POST", "Calls.json", fields)
        snapshot = self._snapshot(data)
        logger.info(f"CALL | placed | {snapshot.describe()}")
        return snapshot

    async def fetch_call(self, call_id: str) -> CallSnapshot:
        """Read a call's current state from Twilio."""
        data = await self._request("GET", f"Calls/{call_id}.json")
        return self._snapshot(data)

    async def find_recent_calls(
        self, to_number: str, *, since: datetime, limit: int = 20
    ) -> list[CallSnapshot]:
        """Recent calls to a number, for resolving an ambiguous placement. Phase 9.

        `GET Calls.json?To=...` lists the account's calls to that number,
        newest first. Twilio also accepts a `StartTime>` inequality parameter,
        which is deliberately not used: it is date-granular, its name needs URL
        encoding that varies by client, and the window this needs is minutes.
        Filtering `date_created` here is exact and depends on nothing but the
        field being present.

        `date_created` is RFC 2822 (`Tue, 31 Aug 2010 20:36:28 +0000`), which
        `email.utils.parsedate_to_datetime` reads. A call whose timestamp
        cannot be read is *kept* rather than dropped: this method exists to
        answer "might a call have been created", and discarding a candidate
        because its date did not parse would answer it wrongly in the dangerous
        direction.
        """
        data = await self._request(
            "GET", "Calls.json", params={"To": to_number, "PageSize": str(max(1, min(limit, 100)))}
        )
        calls = data.get("calls")
        if not isinstance(calls, list):
            return []

        cutoff = since if since.tzinfo else since.replace(tzinfo=UTC)
        found = []
        for entry in calls:
            if not isinstance(entry, dict):
                continue
            snapshot = self._snapshot(entry)
            if snapshot.created_at is not None and snapshot.created_at < cutoff:
                continue
            found.append(snapshot)
            if len(found) >= limit:
                break
        return found

    async def check_credentials(self) -> str:
        """Read the account resource: cheap, authenticated, and dials nothing. Phase 9."""
        data = await self._request("GET", "")
        name = _string(data.get("friendly_name")) or "account"
        status = _string(data.get("status")) or "unknown"
        return f"{name} ({status})"

    async def hang_up(self, call_id: str) -> None:
        """End a call that is ringing or in progress.

        A call that has already finished comes back as a 404 with code 20404,
        which is not an error here: the goal was for the call to be over.
        """
        try:
            await self._request("POST", f"Calls/{call_id}.json", {"Status": "completed"})
            logger.info(f"CALL | hung up | twilio call={call_id}")
        except CallSetupError as exc:
            if "20404" in str(exc):
                logger.debug(f"CALL | already ended | twilio call={call_id}")
                return
            raise

    async def transfer_call(
        self,
        call_id: str,
        to_number: str,
        *,
        caller_id: str | None = None,
        action_url: str | None = None,
        timeout_secs: int = 30,
    ) -> None:
        """Redirect a live call to a person.

        Twilio's "modify a live call" is a POST to the call resource with new
        instructions; the `Twiml` parameter carries them inline, exactly as
        `place_call` does, so no second public endpoint is needed. When the
        carrier applies the new TwiML it ends the `<Connect><Stream>` the bot is
        attached to — which is the bot's signal that the call has left, and why
        `transfer_to_human` must not also hang the call up.

        With `action_url` (Phase 16) the `<Dial>` reports how the colleague's
        leg ended to the webhook receiver, which is how the transfer's outcome
        reaches the database.

        Raises:
            TransferError: The call has ended, or the carrier refused.
            ProviderUnavailableError: The carrier's API was unreachable.
        """
        try:
            twiml = build_transfer_twiml(
                to_number, caller_id=caller_id, timeout_secs=timeout_secs, action_url=action_url
            )
        except TelephonyError as exc:
            raise TransferError(str(exc)) from exc

        try:
            await self._request("POST", f"Calls/{call_id}.json", {"Twiml": twiml})
        except CallSetupError as exc:
            text = str(exc)
            if any(code in text for code in _CALL_GONE):
                raise TransferError(
                    f"the call has already ended, so it cannot be transferred ({self.brand} call={call_id})"
                ) from exc
            raise TransferError(text) from exc
        logger.info(f"CALL | transferred | {self.name} call={call_id} to={to_number}")

    async def close(self) -> None:
        """Close the HTTP session, if this provider created one."""
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    # --- Webhooks (Phase 14) ------------------------------------------------

    @property
    def supports_webhooks(self) -> bool:
        """Twilio and its compatibles sign and deliver call events. Always true here."""
        return True

    @property
    def can_verify_webhooks(self) -> bool:
        """Whether a signing secret is configured, so a delivery can be checked."""
        return bool(self._webhook_secret)

    def verify_webhook(self, request: WebhookRequest) -> None:
        """Check the carrier's signature, or raise.

        Constant-time comparison against the signature computed for the
        configured URL — and for the same URL with the default port added or
        removed, which is the allowance Twilio's own libraries make. A
        delivery naming another account is refused too: a valid signature from
        a different account would still be somebody else's call.

        Raises:
            WebhookSignatureError: No secret configured, no signature header,
                or a signature that does not match.
        """
        if not self._webhook_secret:
            raise WebhookSignatureError(
                f"{self.brand} deliveries cannot be verified: {self.webhook_secret_hint} is not "
                f"set. The event was refused; polling remains the source of call status."
            )
        signature = None
        for name in self.signature_headers:
            signature = request.header(name)
            if signature:
                break
        if not signature:
            raise WebhookSignatureError(
                f"the request carries no {self.signature_headers[0]} header, so it cannot have "
                f"come from {self.brand}"
            )
        params = dict(request.form)
        for candidate in _url_variants(request.url):
            expected = compute_signature(self._webhook_secret, candidate, params)
            if hmac.compare_digest(expected, signature):
                break
        else:
            raise WebhookSignatureError(
                f"the {self.signature_headers[0]} header does not match this account's "
                f"{self.webhook_secret_hint} for {request.url}; the event was refused"
            )
        account = _string(params.get("AccountSid"))
        if account and account != self._account_sid:
            raise WebhookSignatureError(
                f"the event names account …{account[-4:]}, not this one (…{self._account_sid[-4:]})"
            )

    def parse_webhook(self, request: WebhookRequest) -> WebhookEvent:
        """Turn a status or AMD callback's fields into a `WebhookEvent`.

        A status callback carries `CallStatus`; the asynchronous
        answering-machine callback carries `AnsweredBy` and no status. Both
        carry `CallSid`, which is the only field a delivery cannot do without.
        A status word this code does not know maps to `UNKNOWN`, exactly as
        `fetch_call` does, so the receiver ignores it rather than guessing.

        Raises:
            WebhookError: No `CallSid`, or neither a status nor a verdict.
        """
        form = dict(request.form)
        call_id = _string(form.get("CallSid"))
        if not call_id:
            raise WebhookError(f"the delivery has no CallSid, so it is not a {self.brand} call event")

        # Phase 16: a `<Dial action>` request. It also carries the parent
        # call's `CallStatus` (in-progress), so it is recognised first.
        dial_status = (_string(form.get("DialCallStatus")) or "").lower() or None
        if dial_status is not None:
            return WebhookEvent(
                provider=self.name,
                call_id=call_id,
                kind=WEBHOOK_TRANSFER,
                raw_status=dial_status,
                dial_call_id=_string(form.get("DialCallSid")),
                duration_secs=_seconds(form.get("DialCallDuration")),
                timestamp=_created_at(form.get("Timestamp")),
                to_number=_string(form.get("To")) or _string(form.get("Called")),
                from_number=_string(form.get("From")) or _string(form.get("Caller")),
                raw=form,
            )

        raw_status = (_string(form.get("CallStatus")) or "").lower() or None
        answered_by = normalize_answered_by(form.get("AnsweredBy"))
        if raw_status is None and answered_by is None:
            raise WebhookError(
                f"the delivery for call {call_id} carries neither CallStatus nor AnsweredBy"
            )

        status: CallStatus | None = None
        kind = WEBHOOK_AMD
        if raw_status is not None:
            kind = WEBHOOK_STATUS
            status = _STATUSES.get(raw_status, CallStatus.UNKNOWN)
            if status is CallStatus.UNKNOWN:
                logger.warning(
                    f"CALL | {self.name} delivered an unrecognised status {raw_status!r} "
                    f"for call {call_id}"
                )

        error_code = _string(form.get("ErrorCode"))
        error_message = _string(form.get("ErrorMessage"))
        sip = _string(form.get("SipResponseCode"))
        if status is CallStatus.FAILED and sip and not error_message:
            error_message = f"SIP {sip}"

        return WebhookEvent(
            provider=self.name,
            call_id=call_id,
            kind=kind,
            status=status,
            raw_status=raw_status,
            sequence=_integer(form.get("SequenceNumber")),
            timestamp=_created_at(form.get("Timestamp")),
            duration_secs=_seconds(form.get("CallDuration")),
            error_code=error_code,
            error_message=error_message,
            answered_by=answered_by,
            to_number=_string(form.get("To")) or _string(form.get("Called")),
            from_number=_string(form.get("From")) or _string(form.get("Caller")),
            raw=form,
        )

    # --- HTTP ---------------------------------------------------------------

    def _url(self, path: str) -> str:
        """The URL for a sub-resource, or for the account itself when `path` is empty."""
        account = f"{self._api_base}/{API_VERSION}/Accounts/{self._account_sid}"
        return f"{account}/{path}" if path else f"{account}.json"

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, str] | list[tuple[str, str]] | None = None,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make one authenticated request and return the decoded JSON body.

        `payload` is the form body: a dict, or a list of pairs when a field
        repeats (Phase 14's `StatusCallbackEvent`). aiohttp encodes both.

        Raises:
            CallSetupError: Twilio answered 4xx — the request itself was wrong.
            ProviderUnavailableError: The API was unreachable, answered 5xx, or
                did not answer within the configured timeout.
        """
        session = await self._http()
        auth = aiohttp.BasicAuth(self._account_sid, self._auth_token)
        url = self._url(path)
        timeout = aiohttp.ClientTimeout(total=self._timeout_secs)

        try:
            async with session.request(
                method, url, auth=auth, data=payload, params=params, timeout=timeout
            ) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except Exception:  # noqa: BLE001 - a non-JSON body is itself the problem
                    body = {"message": (await response.text())[:400]}

                if 200 <= status < 300:
                    return body if isinstance(body, dict) else {}

                if status >= 500:
                    raise ProviderUnavailableError(
                        f"{self.brand} returned {status} for {method} {path}. This is "
                        f"{self.brand}'s end; retrying in a moment is reasonable.  "
                        f"{_error_text(body)}"
                    )

                raise CallSetupError(self._explain(status, body))
        except TimeoutError as exc:
            # Its own branch because the caller has to tell it apart: a request
            # that timed out may have been received and acted on, where one that
            # could not connect certainly was not. Both are the same type to
            # aiohttp; the message is what says which, and `dialer.py` treats
            # every `ProviderUnavailableError` from a placement as ambiguous.
            raise ProviderUnavailableError(
                f"{self.brand} did not answer within {self._timeout_secs:g}s for {method} "
                f"{path or 'the account'}. Whether it acted on the request is unknown."
            ) from exc
        except aiohttp.ClientError as exc:
            raise ProviderUnavailableError(
                f"Could not reach {self.brand} ({exc.__class__.__name__}: {exc}). "
                f"Check this machine's internet connection."
            ) from exc

    def _explain(self, status: int, body: Any) -> str:
        """Turn a 4xx into a message that says what to change.

        The carrier's own error text is accurate and rarely actionable ("The
        'To' number is not a valid phone number"), so the codes that have a
        specific fix get one attached. The vendor's message is kept either way:
        it is what a search will match if the fix here turns out not to be the
        one.
        """
        code = _string((body or {}).get("code") if isinstance(body, dict) else None)
        message = _error_text(body)
        fill = {"brand": self.brand, "creds": self.credential_hint}

        if status in (401, 403) and not code:
            return (
                f"{self.brand} rejected the credentials (HTTP {status}). "
                f"Check {self.credential_hint} in server/.env."
            )

        headline = f"{self.brand} refused the request (HTTP {status}"
        headline += f", code {code})." if code else ")."

        if code and code in _ERROR_HINTS:
            fix = "\n  " + _ERROR_HINTS[code].format(**fill)
        elif code:
            fix = f"\n  Look the code up at {self.error_docs.format(code=code)}"
        else:
            fix = ""

        return headline + (f"  {message}" if message else "") + fix

    def _snapshot(self, data: dict[str, Any]) -> CallSnapshot:
        """Turn a Call resource into a `CallSnapshot`."""
        raw_status = str(data.get("status") or "").lower()
        status = _STATUSES.get(raw_status, CallStatus.UNKNOWN)
        if status is CallStatus.UNKNOWN and raw_status:
            logger.warning(f"CALL | {self.name} reported an unrecognised status {raw_status!r}")

        return CallSnapshot(
            provider=self.name,
            call_id=str(data.get("sid") or ""),
            status=status,
            to_number=data.get("to") or None,
            from_number=data.get("from") or None,
            duration_secs=_seconds(data.get("duration")),
            error_code=_string(data.get("error_code")),
            error_message=_string(data.get("error_message")),
            created_at=_created_at(data.get("date_created")),
            # Phase 12: `human`, `machine_start`, `machine_end_beep`, ... or
            # null. Normalised here so nothing downstream matches on the
            # carrier's spelling.
            answered_by=normalize_answered_by(data.get("answered_by")),
            raw=data,
        )


def _error_text(body: Any) -> str:
    """Pull the carrier's own message out of an error body."""
    if isinstance(body, dict):
        return str(body.get("message") or body.get("more_info") or "").strip()
    return ""


def _string(value: Any) -> str | None:
    """Normalise a JSON field to a non-empty string, or None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _seconds(value: Any) -> float | None:
    """Twilio reports duration as a string of whole seconds, or null while live."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    """A form field as a whole number, or None when absent or not one."""
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _created_at(value: Any) -> datetime | None:
    """Read Twilio's RFC 2822 `date_created`, or None when it is absent or odd.

    Returning None on an unreadable date is deliberate and is read as "cannot
    say when": `find_recent_calls` keeps such a call rather than filtering it
    out, so an unparseable timestamp never causes a real call to be missed.
    """
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
