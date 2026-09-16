"""The seam between "place a phone call" and "which carrier places it".

Everything in this module is carrier-agnostic. A provider implementation
(`twilio.py`) is the only place that knows a vendor's URLs, parameter names or
status vocabulary, and nothing outside `src/telephony/` imports one directly —
`make_provider` in `__init__.py` hands back a `TelephonyProvider`, and `call.py`
and `bot.py` only ever see this file's types.

**Two halves of telephony, and they need very different amounts of code.**

* *Audio.* Once a call is up, the carrier streams audio to us over a websocket
  in its own wire format. Pipecat owns the hard part: it detects the carrier
  from the first message and ships a serializer for each of Twilio, Telnyx,
  Plivo and Exotel. All a provider does is *choose and configure* one —
  `make_serializer` — because which host the serializer hangs the call up at,
  and with whose credentials, is carrier knowledge and belongs here.
* *Placement.* Starting an outbound call is a REST request that differs per
  vendor — endpoint, parameter names, status words, error codes. That is the
  half this module really abstracts.

So swapping carrier is: implement `TelephonyProvider` for the new one (one
module, four methods), add it to `SUPPORTED_TELEPHONY` and the settings table in
`config.py`, and add a branch to `make_provider`. The pipeline, the bot and the
call CLI do not change. When the new carrier is *Twilio-compatible*, as
SignalWire is, it is a subclass overriding a hostname — see `signalwire.py`.

**Statuses are normalised on purpose.** Every carrier spells the same six
outcomes differently ("no-answer", "no_answer", "NO_ANSWER", a SIP 480). Callers
of this module should never match on a vendor string, so each provider maps its
own vocabulary onto `CallStatus` and keeps the raw payload in
`CallSnapshot.raw` for anything that genuinely needs the vendor's detail.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape, quoteattr

if TYPE_CHECKING:
    # Imported for the type only: this module stays free of Pipecat at runtime
    # so that `call.py`, which never builds a pipeline, does not pay for it.
    from pipecat.serializers.base_serializer import FrameSerializer

# The providers Pipecat can detect from the first websocket message and
# serialize audio for, without any code from us. Used to register transport
# params in `bot.py`: a call from any of these is understood on the audio side
# even if outbound placement for it is not implemented here.
TELEPHONY_TRANSPORTS = ("twilio", "telnyx", "plivo", "exotel")


class TelephonyError(RuntimeError):
    """Something went wrong talking to the carrier. Message is for the user."""

    #: Whether trying the same operation again could reasonably succeed.
    #: Read by `reliability/retry.py`'s classifiers, which is how a retry policy
    #: knows a carrier failure is transient without importing this module — the
    #: knowledge of what a failure *means* stays with the failure. Phase 9.
    retryable: bool = False


class CallSetupError(TelephonyError):
    """The carrier refused to place the call.

    A bad number, an unverified caller ID, a destination country that is not
    enabled on the account, or wrong credentials. The call never rang, so this
    is a configuration problem to fix rather than a call outcome to record.
    """


class ProviderUnavailableError(TelephonyError):
    """The carrier's API could not be reached, or answered with a server error.

    Distinct from `CallSetupError` because retrying is reasonable: nothing was
    wrong with the request. Note that "reasonable" is only true for a *read* —
    on a placement this is the ambiguous case, and `dialer._placement_classifier`
    treats it as such.
    """

    retryable = True


class TransferError(TelephonyError):
    """The carrier would not move a live call to another number.

    Phase 7. Its own type because the agent has to say something specific when
    it happens — "I could not connect you; a colleague will call you back" —
    and the caller of `transfer_call` should not have to read a message to know
    that the call is still with the bot.
    """


# E.164: a plus, a non-zero country code digit, then six to fourteen more.
# Used to validate a *configured* number (the transfer destination) — for
# numbers that come from a prospect list, `campaigns/phone.py` does the real
# work with libphonenumber. A regex is the right tool here because the question
# is "is this shaped like a dialable number", not "is this a real number".
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def is_e164(number: str | None) -> bool:
    """Whether `number` is shaped like an E.164 phone number (`+923001234567`)."""
    return bool(number) and _E164.match(number.strip()) is not None


class CallStatus(StrEnum):
    """Where a call has got to, in vendor-neutral terms.

    The four unhappy endings are separate values rather than one `FAILED`
    because they call for different responses: `BUSY` and `NO_ANSWER` mean the
    number is fine and the person was not available, `FAILED` means the call
    could not be made at all, and `CANCELED` means we hung up before it was
    answered.
    """

    QUEUED = "queued"
    RINGING = "ringing"
    ANSWERED = "answered"
    COMPLETED = "completed"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    FAILED = "failed"
    CANCELED = "canceled"
    # The carrier reported a status this code does not know. Treated as live
    # rather than final, so a polling loop keeps watching instead of declaring
    # an outcome it did not actually observe.
    UNKNOWN = "unknown"

    @property
    def is_final(self) -> bool:
        """Whether the call is over and its status will not change again."""
        return self in _FINAL

    @property
    def reached_person(self) -> bool:
        """Whether a human (or at least *something*) picked up.

        True for a call still in progress and for one that completed normally.
        False for every ending where nobody answered.
        """
        return self in (CallStatus.ANSWERED, CallStatus.COMPLETED)

    def explain(self) -> str:
        """A short human explanation, for a log line or the CLI's exit message."""
        return _EXPLANATIONS.get(self, "the carrier reported an unrecognised status")


_FINAL = frozenset(
    {
        CallStatus.COMPLETED,
        CallStatus.BUSY,
        CallStatus.NO_ANSWER,
        CallStatus.FAILED,
        CallStatus.CANCELED,
    }
)

_EXPLANATIONS = {
    CallStatus.QUEUED: "the carrier has accepted the call and is about to dial",
    CallStatus.RINGING: "the phone is ringing",
    CallStatus.ANSWERED: "answered — audio is flowing to the agent",
    CallStatus.COMPLETED: "the call was answered and has ended",
    CallStatus.BUSY: "the line was busy",
    CallStatus.NO_ANSWER: "nobody answered, or the call was rejected",
    CallStatus.FAILED: "the call could not be completed as dialled",
    CallStatus.CANCELED: "the call was cancelled before it was answered",
    CallStatus.UNKNOWN: "the carrier reported an unrecognised status",
}


@dataclass(frozen=True)
class CallRequest:
    """One outbound call to place.

    Attributes:
        to_number: Who to call, in E.164 (`+923001234567`).
        from_number: The caller ID to present. Must be a number the account
            owns or has verified.
        stream_url: The `wss://` URL the carrier should stream the call's audio
            to — this bot's `/ws` endpoint, reachable from the public internet.
        answer_timeout_secs: How long to let it ring before giving up and
            recording a no-answer.
        parameters: Extra key/value pairs handed to the bot when the media
            stream opens. This is how an outbound call tells the agent who it
            called and that the call was outbound; see `session.py`.
        machine_detection: Phase 12. Ask the carrier to detect an answering
            machine: `off`, `async` (detect in the background and report on
            the call resource; no delay for a person) or `sync` (decide before
            connecting the call). The verdict comes back as
            `CallSnapshot.answered_by`.
        status_callback_url: Phase 14. Where the carrier should POST the
            call's lifecycle events (initiated, ringing, answered, completed
            and the unhappy endings) as they happen, instead of leaving them
            to be polled for. `None` asks for no callbacks, which is what a
            caller with no public endpoint wants. The receiving side is
            `TelephonyProvider.verify_webhook` / `parse_webhook`.
    """

    to_number: str
    from_number: str
    stream_url: str
    answer_timeout_secs: int = 30
    parameters: Mapping[str, str] = field(default_factory=dict)
    machine_detection: str = "off"
    status_callback_url: str | None = None


@dataclass(frozen=True)
class CallSnapshot:
    """What the carrier says about a call, right now.

    Attributes:
        provider: Which provider this came from.
        call_id: The carrier's identifier for the call. The same value reaches
            the bot as `call_data.call_id`, which is what lets a log line from
            the placing side and one from the bot side be tied together.
        status: Normalised status.
        to_number: Who was called, as the carrier reports it.
        from_number: The caller ID presented.
        duration_secs: Billed/answered duration, once the carrier reports one.
        error_code: Vendor error code, when the carrier gave one.
        error_message: Vendor error text, when the carrier gave one.
        created_at: When the carrier created the call, when it says. Phase 9
            reads it to tell a call this attempt placed from an older one to
            the same number.
        answered_by: Phase 12. The carrier's answering-machine verdict, when
            detection was requested and has finished: `human`, `machine`,
            `fax` or `unknown`. None means the carrier said nothing — detection
            was off, or is still running. Normalised from the carrier's own
            words by `voicemail.normalize_answered_by`; the raw value stays in
            `raw`.
        raw: The carrier's own payload, unmodified, for anything this shape
            does not carry.
    """

    provider: str
    call_id: str
    status: CallStatus
    to_number: str | None = None
    from_number: str | None = None
    duration_secs: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    answered_by: str | None = None
    raw: Mapping[str, object] = field(default_factory=dict)

    @property
    def machine_answered(self) -> bool:
        """Whether the carrier's detection says a machine (or a fax) picked up."""
        return self.answered_by in ("machine", "fax")

    def describe(self) -> str:
        """One line for a log: the call, its status, and why if it went wrong."""
        parts = [f"{self.provider} call={self.call_id}", f"status={self.status.value}"]
        if self.to_number:
            parts.append(f"to={self.to_number}")
        if self.duration_secs is not None:
            parts.append(f"duration={self.duration_secs:.0f}s")
        if self.answered_by:
            parts.append(f"answered_by={self.answered_by}")
        if self.error_code or self.error_message:
            detail = " ".join(filter(None, (self.error_code, self.error_message)))
            parts.append(f"error=({detail})")
        return " | ".join(parts)


# --- Webhooks (Phase 14) ------------------------------------------------------
#
# A carrier reports a call's progress two ways: you ask (`fetch_call`, which
# everything before Phase 14 did, once a second) or it tells you, by POSTing an
# event to a URL you gave it when the call was placed. The second is the
# authoritative one — it is immediate, it carries a sequence number, and it
# costs no API request — and these types are the carrier-neutral shape of it.
# A provider turns its own vocabulary into a `WebhookEvent` exactly as it turns
# a call resource into a `CallSnapshot`; nothing outside `src/telephony/` sees
# a vendor's parameter names.

#: The kinds of event a carrier delivers about a call. A `status` event
#: moves the call along its lifecycle; an `amd` event is the answering-machine
#: verdict, which arrives on its own when detection runs asynchronously; a
#: `transfer` event (Phase 16) says how the colleague's leg of a transfer
#: ended — the carrier asks what to do with the caller next, and the answer
#: must be TwiML.
WEBHOOK_STATUS = "status"
WEBHOOK_AMD = "amd"
WEBHOOK_TRANSFER = "transfer"


class WebhookError(TelephonyError):
    """A carrier's event could not be read.

    Not a call outcome and not a carrier failure: the request reached us and
    did not make sense — no call id, a body that is not a form. The receiver
    answers 400 and leaves the attempt as it was.
    """


class WebhookSignatureError(WebhookError):
    """The event did not prove it came from the carrier.

    Missing signature, wrong signature, or no secret configured to check it
    with. Every one of these is refused, because an unverified event could
    mark a live call finished and free its prospect to be dialled again.
    """


@dataclass(frozen=True)
class WebhookRequest:
    """One HTTP delivery from a carrier, before any vendor knows about it.

    Attributes:
        url: The URL the carrier was told to deliver to — the *configured*
            public address, not whatever the local server saw. Signatures are
            computed over the URL the carrier requested, and behind a tunnel or
            a proxy the two differ; the configured one is the one both sides
            agree on.
        headers: The request headers, keys lower-cased.
        form: The decoded form fields. Carriers deliver
            `application/x-www-form-urlencoded`; a repeated key keeps its
            last value, which no call event uses.
        method: The HTTP method. `POST` unless a carrier is configured otherwise.
    """

    url: str
    headers: Mapping[str, str]
    form: Mapping[str, str]
    method: str = "POST"

    def header(self, name: str) -> str | None:
        """One header by case-insensitive name, or None."""
        value = self.headers.get(name.lower())
        if value is None:
            for key, candidate in self.headers.items():
                if key.lower() == name.lower():
                    value = candidate
                    break
        text = (value or "").strip()
        return text or None


@dataclass(frozen=True)
class WebhookEvent:
    """What a carrier said about a call, in vendor-neutral terms.

    Attributes:
        provider: Which provider decoded it.
        call_id: The carrier's id for the call — the join to `call_attempts`.
        kind: `WEBHOOK_STATUS` or `WEBHOOK_AMD`.
        status: The call's status, for a status event. `CallStatus.UNKNOWN`
            when the carrier's word is not one this code knows; None for an
            AMD event, which says nothing about the lifecycle.
        raw_status: The carrier's own status word, unmapped.
        sequence: The carrier's ordering number for this call's events, when
            it sends one. Twilio numbers a call's callbacks from zero; two
            deliveries of the same event share a number, and a later event
            has a higher one. None when the carrier sent nothing.
        timestamp: When the carrier says the event happened.
        duration_secs: The billed duration, on a terminal event.
        error_code / error_message: The carrier's reason, on a failure.
        answered_by: The answering-machine verdict, normalised as
            `CallSnapshot.answered_by` is.
        to_number / from_number: As the carrier reports them.
        dial_call_id: Phase 16. On a `transfer` event, the carrier's id for
            the colleague's leg. `raw_status` then holds how that leg ended
            (`completed`, `busy`, `no-answer`, `failed`, `canceled`) and
            `duration_secs` how long it lasted.
        raw: The delivered fields, unmodified.
    """

    provider: str
    call_id: str
    kind: str
    status: CallStatus | None = None
    raw_status: str | None = None
    sequence: int | None = None
    timestamp: datetime | None = None
    duration_secs: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    answered_by: str | None = None
    to_number: str | None = None
    from_number: str | None = None
    dial_call_id: str | None = None
    raw: Mapping[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """What makes two deliveries the same event.

        The carrier's sequence number when it sends one — a redelivery repeats
        it, a new event advances it. Without one, the event's own content: a
        second `ringing` for the same call says nothing the first did not, and
        a transfer's outcome is one per colleague leg. Readable on purpose,
        like the idempotency key: a duplicate in a log says which event it
        duplicated.
        """
        if self.kind == WEBHOOK_TRANSFER:
            marker = self.dial_call_id or self.raw_status or "event"
        elif self.sequence is not None:
            marker = f"seq:{self.sequence}"
        else:
            marker = self.raw_status or self.answered_by or "event"
        return f"{self.provider}:{self.call_id}:{self.kind}:{marker}"

    @property
    def transfer_answered(self) -> bool:
        """On a transfer event, whether the colleague picked up."""
        return self.kind == WEBHOOK_TRANSFER and self.raw_status in ("completed", "answered")

    @property
    def machine_answered(self) -> bool:
        """Whether the carrier's detection says a machine (or a fax) picked up."""
        return self.answered_by in ("machine", "fax")

    def to_snapshot(self) -> CallSnapshot:
        """The event as a `CallSnapshot`, so what reads a poll can read a push.

        A status event has everything a poll would have reported at that
        moment; an AMD event becomes a snapshot with `UNKNOWN` status, which
        nothing treats as a lifecycle change.
        """
        return CallSnapshot(
            provider=self.provider,
            call_id=self.call_id,
            status=self.status if self.status is not None else CallStatus.UNKNOWN,
            to_number=self.to_number,
            from_number=self.from_number,
            duration_secs=self.duration_secs,
            error_code=self.error_code,
            error_message=self.error_message,
            created_at=self.timestamp,
            answered_by=self.answered_by,
            raw=dict(self.raw),
        )

    def describe(self) -> str:
        """One line for a log."""
        parts = [f"{self.provider} call={self.call_id}", f"kind={self.kind}"]
        if self.status is not None:
            parts.append(f"status={self.status.value}")
        if self.raw_status and (self.status is None or self.raw_status != self.status.value):
            parts.append(f"raw={self.raw_status}")
        if self.sequence is not None:
            parts.append(f"seq={self.sequence}")
        if self.answered_by:
            parts.append(f"answered_by={self.answered_by}")
        if self.duration_secs is not None:
            parts.append(f"duration={self.duration_secs:.0f}s")
        if self.error_code or self.error_message:
            detail = " ".join(filter(None, (self.error_code, self.error_message)))
            parts.append(f"error=({detail})")
        return " | ".join(parts)


class TelephonyProvider(ABC):
    """Places and inspects outbound calls with one carrier.

    Four methods is the whole contract. Deliberately not in it:

    * *Decoding the audio.* Pipecat's serializers do the actual byte work;
      `make_serializer` only chooses and configures one.
    * *Buying numbers, webhooks, recordings.* Not this phase.

    **Why `make_serializer` is here** and not left to Pipecat's dev runner,
    which will happily pick one on its own: the runner's choice is hard-wired to
    Twilio's own API host and to the `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN`
    environment variables, and it *raises* when they are empty. That is correct
    for Twilio and wrong for every Twilio-compatible carrier — SignalWire speaks
    the identical wire protocol but lives at a different host under different
    credentials, so the runner would either crash the call at setup or spend
    every hang-up talking to the wrong company. The carrier knows which
    serializer it needs and how to authenticate it; that knowledge belongs with
    the rest of the carrier's knowledge, which is here.
    """

    #: Provider name, matching the `TELEPHONY_PROVIDER` value that selects it.
    name: str = "unknown"

    #: Wire protocols this provider speaks, named as Pipecat detects them from a
    #: media stream's first message. Usually one entry, and not always the
    #: provider's own name: SignalWire is `("twilio",)`, because its media
    #: stream *is* Twilio's. `bot.py` checks this before handing the provider a
    #: call to serialize, and falls back to Pipecat's own mapping when a call
    #: arrives from a carrier this provider does not claim.
    transports: tuple[str, ...] = ()

    @abstractmethod
    async def place_call(self, request: CallRequest) -> CallSnapshot:
        """Dial `request.to_number` and stream the audio to `request.stream_url`.

        Returns as soon as the carrier accepts the request — normally in
        `QUEUED`, long before the phone has rung. Watching it through to an
        outcome is `fetch_call`'s job.

        Raises:
            CallSetupError: The carrier refused the request.
            ProviderUnavailableError: The carrier's API was unreachable.
        """

    @abstractmethod
    async def fetch_call(self, call_id: str) -> CallSnapshot:
        """Look up the current state of a call.

        Raises:
            CallSetupError: No such call, or the credentials were rejected.
            ProviderUnavailableError: The carrier's API was unreachable.
        """

    @abstractmethod
    async def hang_up(self, call_id: str) -> None:
        """End a call that is ringing or in progress.

        Must not raise when the call has already ended — that is the common case
        when this races the natural end of a call, and it is not an error.

        This is for abandoning a call from *outside* the bot. A call that ends
        normally hangs itself up: the pipeline's `EndFrame` reaches the
        serializer, which tells the carrier.
        """

    @abstractmethod
    def make_serializer(self, call_data: Any) -> FrameSerializer:
        """Build the serializer for one of this carrier's media streams.

        Args:
            call_data: Pipecat's parsed handshake for this call, carrying at
                least the stream and call identifiers.

        Returns:
            A configured Pipecat serializer, including whatever it needs to hang
            the call up when the pipeline ends.
        """

    async def find_recent_calls(
        self, to_number: str, *, since: datetime, limit: int = 20
    ) -> list[CallSnapshot]:
        """Recent calls this account placed to `to_number`, newest first.

        **The answer to network ambiguity** (Phase 9). When `place_call` times
        out or its connection drops, the request may or may not have created a
        call, and neither Twilio's nor SignalWire's API takes an idempotency key
        that would let it be safely repeated. So the question is not "try
        again?" but "did one happen?", and this is how it is asked: list what
        the carrier actually has for that number since the moment placement
        started. `reliability/recovery.py` is the only caller.

        The default implementation reports that the provider cannot answer,
        which is honest for a carrier whose API has no such listing: recovery
        then marks the attempt failed and leaves the prospect blocked rather
        than guessing.

        Args:
            to_number: The number that was dialled, in E.164.
            since: Only calls created at or after this moment. Timezone-aware.
            limit: How many recent calls to consider.

        Returns:
            Matching calls, newest first. Empty means the carrier has none,
            which is a definite answer: no call was created.

        Raises:
            NotImplementedError: This provider cannot list calls.
            ProviderUnavailableError: The carrier's API was unreachable.
        """
        raise NotImplementedError(
            f"{self.name} cannot list recent calls, so an ambiguous placement cannot be resolved "
            f"automatically. Check the carrier's own call log for {to_number}."
        )

    async def check_credentials(self) -> str:
        """Confirm the account is reachable and the credentials work, dialling nothing.

        The telephony health check (Phase 9). Reads something cheap and
        authenticated — the account resource — because the alternative,
        placing a test call, costs money and rings a real phone. A provider
        with no such endpoint should raise `NotImplementedError` rather than
        inventing a check that passes when the account is suspended.

        Returns:
            A short description of what answered, for the health report.

        Raises:
            CallSetupError: The credentials were rejected.
            ProviderUnavailableError: The carrier's API was unreachable.
            NotImplementedError: This provider has no cheap check.
        """
        raise NotImplementedError(f"{self.name} has no credential check")

    @abstractmethod
    async def transfer_call(
        self,
        call_id: str,
        to_number: str,
        *,
        caller_id: str | None = None,
        action_url: str | None = None,
        timeout_secs: int = 30,
    ) -> None:
        """Move a live call away from the bot and dial `to_number` into it.

        Phase 7's `transfer_to_human`. A *blind* transfer: the carrier replaces
        the call's instructions with "dial this number", the media stream to
        the bot closes as a consequence, and the person on the line hears the
        colleague's phone ring. Nothing is announced to the colleague and the
        bot is not conferenced in — a warm transfer is a different feature with
        a different carrier API, and nobody has asked for one.

        Args:
            call_id: The carrier's id for the live call.
            to_number: Where to send it, in E.164.
            caller_id: The number to present to the colleague. None lets the
                carrier decide, which is usually the original caller ID.
            action_url: Phase 16. Where the carrier should report how the
                colleague's leg ended — answered, busy, no answer, failed —
                and ask what to do with the caller next. The webhook receiver
                records the outcome and answers with the fallback. None keeps
                the fallback inline in the TwiML and records no outcome.
            timeout_secs: How long to ring the colleague before giving up.

        Raises:
            TransferError: The carrier refused — the call has already ended, the
                destination is not allowed, or the account cannot do it.
            ProviderUnavailableError: The carrier's API was unreachable.
        """

    # --- Webhooks (Phase 14) -------------------------------------------------
    #
    # Both default to "this carrier cannot", which is honest for a provider
    # written against no account: a receiver mounted for it answers 501 rather
    # than accepting events it cannot verify. A provider that can overrides
    # both, and `supports_webhooks` says so.

    @property
    def supports_webhooks(self) -> bool:
        """Whether this provider can verify and decode the carrier's call events."""
        return False

    def verify_webhook(self, request: WebhookRequest) -> None:
        """Prove a delivery came from the carrier, or raise.

        Uses the carrier's own mechanism — for Twilio and its compatibles, an
        HMAC over the delivered URL and fields, keyed by a secret only the
        carrier and this account hold — so a forged event cannot move a call.

        Raises:
            WebhookSignatureError: Missing or wrong signature, or no secret to
                check one with. The receiver answers 403.
            NotImplementedError: This provider has no webhook support.
        """
        raise NotImplementedError(f"{self.name} cannot verify webhook deliveries")

    def parse_webhook(self, request: WebhookRequest) -> WebhookEvent:
        """Decode one delivery into a `WebhookEvent`. Verification is separate.

        Raises:
            WebhookError: The delivery is not a call event this carrier sends.
            NotImplementedError: This provider has no webhook support.
        """
        raise NotImplementedError(f"{self.name} cannot decode webhook deliveries")

    async def close(self) -> None:
        """Release the provider's HTTP resources. Safe to call more than once."""

    def describe(self) -> str:
        """One line for the startup log."""
        return self.name


def webhook_url(public_url: str, path: str = "/webhooks/telephony", *, stream_path: str = "/ws") -> str:
    """Build the URL a carrier should POST call events to. Phase 14.

    The same public address `stream_url` turns into `wss://…/ws`, turned into
    the `https://` form a status callback needs — one value in `.env`, two
    routes derived from it, so the two cannot point at different machines::

        https://abc123.ngrok.app        -> https://abc123.ngrok.app/webhooks/telephony
        wss://abc123.ngrok.app/ws       -> https://abc123.ngrok.app/webhooks/telephony
        abc123.ngrok.app                -> https://abc123.ngrok.app/webhooks/telephony
        http://localhost:7860           -> http://localhost:7860/webhooks/telephony

    A trailing `stream_path` is dropped, because a person who pasted the full
    websocket address into `TELEPHONY_PUBLIC_URL` meant the host.

    Args:
        public_url: Public address of this bot's runner (or of the standalone
            receiver, when one is deployed).
        path: The receiver's route.
        stream_path: The telephony websocket route, stripped if present.

    Returns:
        An `https://` (or, for a plain-`http` public URL, `http://`) URL.

    Raises:
        TelephonyError: `public_url` has no hostname in it.
    """
    candidate = (public_url or "").strip()
    if not candidate:
        raise TelephonyError(
            "No public URL was given, so there is nowhere for the carrier to send call "
            "events.  Set TELEPHONY_PUBLIC_URL to your tunnel's address."
        )
    if "//" not in candidate:
        candidate = f"https://{candidate}"

    split = urlsplit(candidate)
    if not split.netloc:
        raise TelephonyError(
            f"{public_url!r} does not look like a URL or a hostname. "
            f"Use something like https://abc123.ngrok.app"
        )

    scheme = "http" if split.scheme in ("http", "ws") else "https"
    existing = split.path.rstrip("/")
    stream = "/" + stream_path.strip("/")
    if existing.endswith(stream):
        existing = existing[: -len(stream)]
    wanted = "/" + path.strip("/")
    final_path = existing if existing.endswith(wanted) else existing + wanted
    return urlunsplit((scheme, split.netloc, final_path, "", ""))


def stream_url(public_url: str, path: str = "/ws") -> str:
    """Build the websocket URL a carrier should stream call audio to.

    Carriers dial into this bot from the public internet, so the URL is a public
    hostname — in development, whatever tunnel is pointed at the local runner
    (`ngrok http 7860` and friends). What you get from a tunnel is an `https://`
    URL, and what a carrier needs is `wss://`, so this converts rather than
    making you keep two copies of the same hostname in `.env` and get one of
    them wrong.

    Both spellings are accepted and the path is normalised, because every one of
    these is something a person will reasonably paste in::

        https://abc123.ngrok.app        -> wss://abc123.ngrok.app/ws
        https://abc123.ngrok.app/       -> wss://abc123.ngrok.app/ws
        wss://abc123.ngrok.app/ws       -> wss://abc123.ngrok.app/ws
        abc123.ngrok.app                -> wss://abc123.ngrok.app/ws

    Args:
        public_url: Public address of this bot's runner.
        path: The runner's telephony websocket route. `/ws` unless you have
            changed it.

    Returns:
        A `wss://` URL.

    Raises:
        TelephonyError: `public_url` has no hostname in it.
    """
    candidate = (public_url or "").strip()
    if not candidate:
        raise TelephonyError(
            "No public URL was given, so there is nowhere for the carrier to send the "
            "call's audio.  Set TELEPHONY_PUBLIC_URL to your tunnel's address."
        )

    if "//" not in candidate:
        # A bare hostname. urlsplit would read it as a path, not a host.
        candidate = f"wss://{candidate}"

    split = urlsplit(candidate)
    if not split.netloc:
        raise TelephonyError(
            f"{public_url!r} does not look like a URL or a hostname. "
            f"Use something like https://abc123.ngrok.app"
        )

    # http/https are what a tunnel prints; ws/wss are what a carrier needs.
    scheme = "ws" if split.scheme in ("http", "ws") else "wss"

    existing = split.path.rstrip("/")
    wanted = "/" + path.strip("/")
    # Respect a path that is already there, so a full wss://host/ws round-trips
    # unchanged, but do not end up with /ws/ws.
    final_path = existing if existing.endswith(wanted) else existing + wanted

    return urlunsplit((scheme, split.netloc, final_path, "", ""))


def build_stream_twiml(request: CallRequest) -> str:
    """Render the TwiML that connects a call to this bot's websocket.

    Twilio-flavoured markup, but it lives here rather than in `twilio.py`
    because Telnyx uses the same dialect and a third provider would want the
    same shape. A provider that needs something else overrides it.

    `<Connect><Stream>` — rather than `<Start><Stream>` — is what makes the
    stream bidirectional: the caller's audio comes to us *and* our audio goes
    back to the caller. `<Start>` only forks a copy of the audio to you, which
    produces a bot that hears everything and cannot be heard, and looks from the
    logs like it is working perfectly.

    The `<Parameter>` elements ride along to the bot as custom parameters on the
    media stream's first message, which is the only channel an outbound call has
    for telling the agent anything about itself.
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<Response>",
        "  <Connect>",
        f"    <Stream url={quoteattr(request.stream_url)}>",
    ]
    for key, value in request.parameters.items():
        name, attr = quoteattr(str(key)), quoteattr(str(value))
        lines.append(f"      <Parameter name={name} value={attr} />")
    lines += ["    </Stream>", "  </Connect>", "</Response>"]
    return "\n".join(lines)


# What the person hears if the colleague does not pick up. Spoken by the
# carrier's own text-to-speech, because by then the bot is no longer on the
# call and cannot say anything itself.
DEFAULT_TRANSFER_FALLBACK = (
    "Sorry, nobody is available to take the call right now. Somebody will call you back. Goodbye."
)


def build_transfer_twiml(
    to_number: str,
    *,
    caller_id: str | None = None,
    timeout_secs: int = 30,
    fallback_message: str = DEFAULT_TRANSFER_FALLBACK,
    action_url: str | None = None,
) -> str:
    """Render the TwiML that moves a live call to a person.

    `<Dial>` rings the destination. What happens when that leg ends depends
    on whether there is somewhere to report it:

    * **With `action_url`** (Phase 16), the carrier POSTs how the leg ended —
      `DialCallStatus`, `DialCallSid`, `DialCallDuration` — to that URL and
      *asks what to do next*; the verbs after `<Dial>` are never reached. The
      webhook receiver records the outcome and answers with
      `transfer_response_twiml`: a hang-up if the colleague answered, the
      fallback sentence and a hang-up if not.
    * **Without one**, the verbs after `<Dial>` run when nobody answers, which
      say the fallback sentence and hang up — the Phase 7 shape, kept for a
      deployment with no public receiver. Without a fallback a transfer to a
      phone nobody answers leaves the prospect listening to silence, then a
      disconnect, with no explanation.

    Twilio-flavoured markup, but it lives here rather than in `twilio.py` for
    the same reason `build_stream_twiml` does: SignalWire accepts the identical
    dialect, and a third Twilio-compatible carrier would too.

    Raises:
        TelephonyError: `to_number` is not shaped like an E.164 number. Checked
            here because the carrier's own message for a bad `<Dial>` target is
            an unhelpful application error after the media stream has already
            been torn down.
    """
    if not is_e164(to_number):
        raise TelephonyError(
            f"Cannot transfer to {to_number!r}: it is not an E.164 number such as +923001234567."
        )
    attributes = f" timeout={quoteattr(str(int(timeout_secs)))}"
    if caller_id:
        attributes += f" callerId={quoteattr(caller_id.strip())}"
    if action_url:
        attributes += f' action={quoteattr(action_url)} method="POST"'
        return "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                "<Response>",
                f"  <Dial{attributes}>{escape(to_number.strip())}</Dial>",
                "</Response>",
            ]
        )
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<Response>",
            f"  <Dial{attributes}>{escape(to_number.strip())}</Dial>",
            f"  <Say>{escape(fallback_message)}</Say>",
            "  <Hangup />",
            "</Response>",
        ]
    )


def transfer_response_twiml(
    answered: bool, *, fallback_message: str = DEFAULT_TRANSFER_FALLBACK
) -> str:
    """What the caller hears once the colleague's leg has ended. Phase 16.

    The answer to a `<Dial action>` request. If the colleague answered, the
    conversation happened and the call simply ends. If not, the prospect is
    told so — by the carrier's own voice, because the bot left the call when
    the transfer began — and the call ends.
    """
    if answered:
        return "\n".join(['<?xml version="1.0" encoding="UTF-8"?>', "<Response>", "  <Hangup />", "</Response>"])
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<Response>",
            f"  <Say>{escape(fallback_message)}</Say>",
            "  <Hangup />",
            "</Response>",
        ]
    )
