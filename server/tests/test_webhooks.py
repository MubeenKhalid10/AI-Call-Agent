#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for the carrier status webhooks. Phase 14. No keys, no phone, no audio.

Run it from the `server/` directory::

    uv run python tests/test_webhooks.py

**What this is for.** A webhook is the one path into this system that an
outsider can drive: a POST to a public URL that, if believed, marks a call
finished and frees its prospect to be dialled again. So the checks here are
arranged around the four things a delivery can be — valid, forged, repeated,
and late — and assert on the rows afterwards: what the attempt says, what the
membership says, and what the ledger recorded about it.

**Stubs, not mocks — and the real code in the middle.** The provider under
test is the real `TwilioProvider` (and `SignalWireProvider`), verifying real
signatures computed the way the carriers' own helper libraries compute them,
including Twilio's published example. The processor is the real
`WebhookProcessor` over the real `CampaignService`; the store is
`test_worker.py`'s in-memory store with the ledger added, so a whole call's
events run in milliseconds. The worker section drives the real
`CampaignWorker` with a scripted carrier and counts how often it still asks
the carrier once events are arriving. The SQL — the ledger table, its unique
key, the processor over real rows — is checked at the end against PostgreSQL
in a temporary schema, skipped with a message when none is reachable.

A plain script rather than a pytest suite, like the other thirteen. Exit
status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import hmac
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from loguru import logger  # noqa: E402
from test_telephony import StubResponse, StubSession, call_resource  # noqa: E402
from test_worker import NOW, FakeClock, MemoryStore, ScriptedCarrier, World  # noqa: E402

from src.campaigns import (  # noqa: E402
    AttemptRecovery,
    CallAttemptStatus,
    CampaignDialer,
    CampaignService,
    CampaignStatus,
    CampaignStoreError,
    MembershipStatus,
    ProspectStatus,
    WebhookDelivery,
    WebhookOutcome,
    WebhookProcessor,
    create_webhook_app,
    create_webhook_router,
    install_webhook_receiver,
)
from src.config import Config, ConfigError, TelephonyConfig  # noqa: E402
from src.reliability import CallingWindow, CampaignGuards, PacingLimiter  # noqa: E402
from src.telephony import (  # noqa: E402
    WEBHOOK_AMD,
    WEBHOOK_STATUS,
    CallRequest,
    CallStatus,
    WebhookError,
    WebhookEvent,
    WebhookRequest,
    WebhookSignatureError,
    webhook_url,
)
from src.telephony.signalwire import SignalWireProvider  # noqa: E402
from src.telephony.twilio import (  # noqa: E402
    STATUS_CALLBACK_EVENTS,
    TwilioProvider,
    compute_signature,
)

_failures: list[str] = []
_skipped: list[str] = []
LOGS: list[str] = []

ACCOUNT = "ACtestsid00000000000000000000abcd"
TOKEN = "twilio-auth-token-secret"
SIGNING_KEY = "PSK_signalwire_signing_key_1234"
URL = "https://abc123.ngrok.app/webhooks/telephony"


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def _logged(name: str, since: int = 0) -> int:
    """How many log lines since `since` carry `name`."""
    return sum(1 for line in LOGS[since:] if name in line)


def _mark() -> int:
    return len(LOGS)


# --- Deliveries, as a carrier sends them -------------------------------------


def reference_signature(secret: str, url: str, fields: dict[str, str]) -> str:
    """The signature computed from the documented recipe, independently of the code under test.

    base64(HMAC-SHA1(secret, url + every field's name and value in name order)).
    """
    text = url + "".join(name + fields[name] for name in sorted(fields))
    return base64.b64encode(hmac.new(secret.encode(), text.encode(), hashlib.sha1).digest()).decode()


def twilio_fields(
    call_sid: str = "CA0001",
    status: str | None = "ringing",
    seq: int | None = None,
    *,
    account: str = ACCOUNT,
    **extra: str,
) -> dict[str, str]:
    """The form a Twilio status callback carries, in the shape Twilio sends it."""
    fields = {
        "AccountSid": account,
        "CallSid": call_sid,
        "From": "+15550001111",
        "To": "+923001234567",
        "ApiVersion": "2010-04-01",
        "Direction": "outbound-api",
        "CallbackSource": "call-progress-events",
        "Timestamp": "Mon, 07 Sep 2026 10:00:00 +0000",
    }
    if status is not None:
        fields["CallStatus"] = status
    if seq is not None:
        fields["SequenceNumber"] = str(seq)
    fields.update(extra)
    return fields


def signed(
    fields: dict[str, str],
    *,
    secret: str = TOKEN,
    url: str = URL,
    header: str = "x-twilio-signature",
    signature: str | None = None,
) -> WebhookRequest:
    """A delivery signed the way the carrier signs one."""
    value = signature if signature is not None else reference_signature(secret, url, fields)
    return WebhookRequest(url=url, headers={header: value, "content-type": "application/x-www-form-urlencoded"}, form=fields)


def unsigned(fields: dict[str, str], *, url: str = URL) -> WebhookRequest:
    return WebhookRequest(url=url, headers={}, form=fields)


# --- The store, with the ledger ------------------------------------------------


@dataclass
class LedgerStore(MemoryStore):
    """`test_worker.py`'s in-memory store, plus the Phase 14 ledger.

    `ledger_missing` makes every ledger method raise the way a database that
    predates Phase 14 does, so the "applied without the ledger" path is a
    check rather than a hope.
    """

    deliveries: dict[str, WebhookDelivery] = field(default_factory=dict)
    ledger_missing: bool = False
    membership_writes: int = 0
    attempt_writes: int = 0

    def _ledger(self) -> None:
        self._guard()
        if self.ledger_missing:
            raise CampaignStoreError(
                "The telephony_webhook_events table does not exist.\n  Run:  uv run campaign.py init"
            )

    async def record_webhook_event(self, **fields: Any) -> tuple[WebhookDelivery | None, bool]:
        self._ledger()
        key = fields["event_key"]
        if key in self.deliveries:
            return self.deliveries[key], False
        row = WebhookDelivery(id=self._next("delivery"), received_at=self.now(), **fields)
        self.deliveries[key] = row
        return row, True

    async def set_webhook_outcome(self, delivery_id: int, outcome: str, *, attempt_id: int | None = None) -> None:
        self._ledger()
        for key, row in self.deliveries.items():
            if row.id == delivery_id:
                self.deliveries[key] = dataclasses.replace(
                    row, outcome=outcome, attempt_id=attempt_id if attempt_id is not None else row.attempt_id
                )

    async def last_webhook_at(self, call_id: str) -> datetime | None:
        self._ledger()
        times = [r.received_at for r in self.deliveries.values() if r.call_id == call_id and r.received_at]
        return max(times) if times else None

    async def webhook_answered_by(self, call_id: str) -> str | None:
        self._ledger()
        rows = [r for r in self.deliveries.values() if r.call_id == call_id and r.answered_by]
        return sorted(rows, key=lambda r: r.id)[-1].answered_by if rows else None

    async def list_webhook_events(self, *, call_id=None, attempt_id=None, limit=50) -> list[WebhookDelivery]:
        self._ledger()
        rows = [
            r
            for r in self.deliveries.values()
            if (call_id is None or r.call_id == call_id) and (attempt_id is None or r.attempt_id == attempt_id)
        ]
        return sorted(rows, key=lambda r: -r.id)[:limit]

    def _write(self, attempt_id: int, **changes: Any):
        self.attempt_writes += 1
        return super()._write(attempt_id, **changes)

    async def set_membership_status(self, membership_id: int, status: MembershipStatus, *, next_attempt_at=None):
        self.membership_writes += 1
        return await super().set_membership_status(membership_id, status, next_attempt_at=next_attempt_at)

    def rows_for(self, call_id: str) -> list[WebhookDelivery]:
        return sorted((r for r in self.deliveries.values() if r.call_id == call_id), key=lambda r: r.id)


@dataclass
class Setup:
    """A campaign with one placed call, and a processor to deliver its events to."""

    clock: FakeClock
    store: LedgerStore
    service: CampaignService
    carrier: ScriptedCarrier
    dialer: CampaignDialer
    processor: WebhookProcessor
    provider: TwilioProvider
    campaign: Any = None
    call_id: str = ""
    attempt_id: int = 0

    async def place(self, number: str = "+923001234567", *, campaign_name: str = "Webhooks") -> None:
        """One prospect, one campaign, one call placed through the real dialer."""
        self.campaign = await self.store.create_campaign(name=campaign_name, status=CampaignStatus.ACTIVE)
        prospect = await self.store.add_prospect(
            first_name="Web", last_name="Hook", phone=number, phone_normalized=number
        )
        await self.store.add_to_campaign(self.campaign.id, prospect.id)
        result = await self.dialer.dial_next(self.campaign.id)
        assert result.placed, result.describe()
        self.call_id = result.snapshot.call_id
        self.attempt_id = result.attempt.id

    async def attempt(self):
        return await self.store.get_attempt(self.attempt_id)

    async def membership(self):
        attempt = await self.attempt()
        return await self.store.get_membership(attempt.campaign_prospect_id)

    async def deliver(self, fields: dict[str, str], **kwargs: Any):
        return await self.processor.receive(signed(fields, **kwargs))


def build(*, max_attempts: int = 3, retry_minutes: float = 60.0, script: list[CallStatus] | None = None) -> Setup:
    clock = FakeClock(NOW)
    store = LedgerStore(clock=clock)
    service = CampaignService(store, default_region="PK", max_attempts=max_attempts, retry_minutes=retry_minutes, clock=clock)  # type: ignore[arg-type]
    # The carrier that *places* is scripted (so a call id exists on the row);
    # the provider that *verifies* is the real Twilio one, keyed with TOKEN.
    carrier = ScriptedCarrier(clock, script=script or [CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.ANSWERED])
    dialer = CampaignDialer(service, carrier, from_number="+15550001111", public_url="https://abc123.ngrok.app", status_callback_url=URL)
    provider = TwilioProvider(ACCOUNT, TOKEN)
    processor = WebhookProcessor(service, provider, expected_url=URL)
    return Setup(clock=clock, store=store, service=service, carrier=carrier, dialer=dialer, processor=processor, provider=provider)


# --- Checks ---------------------------------------------------------------------


def check_signatures() -> None:
    """The carrier's proof that a delivery is its own — and every way it can fail."""
    print("\n=== signatures ===")
    fields = twilio_fields(seq=1)

    # Twilio's own worked example, from its request-validation documentation:
    # auth token 12345, that URL, those five fields, this signature.
    example = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+12349013030",
        "Digits": "1234",
        "From": "+12349013030",
        "To": "+18005551212",
    }
    check(
        "reproduces Twilio's documented example signature",
        compute_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", example)
        == "0/KCTR6DLpKmkAf8muzZqo1nDgQ=",
        compute_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", example),
    )
    check(
        "and agrees with the recipe computed independently here",
        compute_signature(TOKEN, URL, fields) == reference_signature(TOKEN, URL, fields),
    )

    provider = TwilioProvider(ACCOUNT, TOKEN)
    check("a provider with a token can verify", provider.supports_webhooks and provider.can_verify_webhooks)

    def verifies(request: WebhookRequest) -> str:
        try:
            provider.verify_webhook(request)
        except WebhookSignatureError as exc:
            return f"refused: {exc}"
        return "accepted"

    check("a delivery signed with the auth token is accepted", verifies(signed(fields)) == "accepted")
    check(
        "signed for the URL with an explicit :443, still accepted",
        verifies(signed(fields, url="https://abc123.ngrok.app:443/webhooks/telephony", signature=reference_signature(TOKEN, "https://abc123.ngrok.app:443/webhooks/telephony", fields)))
        == "accepted"
        and verifies(signed(fields, signature=reference_signature(TOKEN, "https://abc123.ngrok.app:443/webhooks/telephony", fields))) == "accepted",
    )
    check("the header name is matched case-insensitively", verifies(signed(fields, header="X-Twilio-Signature")) == "accepted")
    check(
        "the same fields in a different order verify the same",
        verifies(signed(dict(reversed(list(fields.items()))))) == "accepted",
    )

    wrong = verifies(signed(fields, secret="not-the-token"))
    check("signed with the wrong secret is refused", wrong.startswith("refused"), wrong[:90])
    check("and the refusal names the setting", "TWILIO_AUTH_TOKEN" in wrong)
    check("no signature header is refused", verifies(unsigned(fields)).startswith("refused"))
    tampered = signed(fields)
    tampered = WebhookRequest(url=tampered.url, headers=tampered.headers, form={**fields, "CallStatus": "completed"})
    check("a field changed after signing is refused", verifies(tampered).startswith("refused"))
    check(
        "signed for a different URL than the configured one is refused",
        verifies(signed(fields, signature=reference_signature(TOKEN, "https://evil.example/webhooks/telephony", fields))).startswith("refused"),
    )
    other = verifies(signed(twilio_fields(seq=1, account="ACsomebodyelse000000000000000000")))
    check("a valid signature naming another account is refused", other.startswith("refused") and "account" in other)
    check(
        "verification never raises anything but WebhookSignatureError",
        all(isinstance(exc, WebhookSignatureError) for exc in (_raises(provider.verify_webhook, unsigned(fields)),)),
    )

    print("\n=== signalwire signatures ===")
    keyed = SignalWireProvider("project-abcd", "PT-api-token", "example.signalwire.com", signing_key=SIGNING_KEY)
    sw_fields = twilio_fields(seq=1, account="project-abcd")

    def sw_verifies(request: WebhookRequest) -> str:
        try:
            keyed.verify_webhook(request)
        except WebhookSignatureError as exc:
            return f"refused: {exc}"
        return "accepted"

    check("with a signing key, X-SignalWire-Signature is accepted", sw_verifies(signed(sw_fields, secret=SIGNING_KEY, header="x-signalwire-signature")) == "accepted")
    check("and the Twilio-named header with the same key too", sw_verifies(signed(sw_fields, secret=SIGNING_KEY)) == "accepted")
    api_token = sw_verifies(signed(sw_fields, secret="PT-api-token", header="x-signalwire-signature"))
    check("but the API token is not the signing key", api_token.startswith("refused"), api_token[:80])
    unkeyed = SignalWireProvider("project-abcd", "PT-api-token", "example.signalwire.com")
    check("without a signing key the provider says it cannot verify", not unkeyed.can_verify_webhooks and unkeyed.supports_webhooks)
    try:
        unkeyed.verify_webhook(signed(sw_fields, secret=SIGNING_KEY, header="x-signalwire-signature"))
        check("and refuses every delivery, naming the setting", False, "accepted")
    except WebhookSignatureError as exc:
        check("and refuses every delivery, naming the setting", "SIGNALWIRE_SIGNING_KEY" in str(exc), str(exc)[:90])


def _raises(call, *args):
    try:
        call(*args)
    except Exception as exc:  # noqa: BLE001
        return exc
    return None


def check_parsing() -> None:
    """The carrier's fields, read into an event."""
    print("\n=== decoding a delivery ===")
    provider = TwilioProvider(ACCOUNT, TOKEN)

    for word, expected in (
        ("queued", CallStatus.QUEUED),
        ("initiated", CallStatus.QUEUED),
        ("ringing", CallStatus.RINGING),
        ("in-progress", CallStatus.ANSWERED),
        ("completed", CallStatus.COMPLETED),
        ("busy", CallStatus.BUSY),
        ("no-answer", CallStatus.NO_ANSWER),
        ("failed", CallStatus.FAILED),
        ("canceled", CallStatus.CANCELED),
        ("something-new", CallStatus.UNKNOWN),
    ):
        parsed = provider.parse_webhook(unsigned(twilio_fields(status=word, seq=2)))
        check(f"{word:<14} -> {expected.value}", parsed.status is expected and parsed.kind == WEBHOOK_STATUS and parsed.raw_status == word)

    parsed = provider.parse_webhook(unsigned(twilio_fields(status="completed", seq=3, CallDuration="42")))
    check("reads the call id", parsed.call_id == "CA0001")
    check("reads the sequence number", parsed.sequence == 3)
    check("reads the carrier's timestamp", parsed.timestamp == datetime(2026, 9, 7, 10, 0, tzinfo=UTC), str(parsed.timestamp))
    check("reads the billed duration", parsed.duration_secs == 42.0)
    check("reads both numbers", parsed.to_number == "+923001234567" and parsed.from_number == "+15550001111")
    check("keys on the sequence number", parsed.key == "twilio:CA0001:status:seq:3", parsed.key)
    check("keeps the raw fields", parsed.raw.get("CallbackSource") == "call-progress-events")
    snapshot = parsed.to_snapshot()
    check("becomes a snapshot a poll would have produced", snapshot.status is CallStatus.COMPLETED and snapshot.duration_secs == 42.0 and snapshot.call_id == "CA0001")

    no_seq = provider.parse_webhook(unsigned(twilio_fields(status="ringing")))
    check("without a sequence number, keys on the status", no_seq.key == "twilio:CA0001:status:ringing", no_seq.key)

    failed = provider.parse_webhook(unsigned(twilio_fields(status="failed", seq=2, SipResponseCode="503")))
    check("a failure keeps the SIP code as its reason", failed.error_message == "SIP 503", str(failed.error_message))
    coded = provider.parse_webhook(unsigned(twilio_fields(status="failed", seq=2, ErrorCode="13224", ErrorMessage="Invalid number")))
    check("and the carrier's own error when it gives one", coded.error_code == "13224" and coded.error_message == "Invalid number")

    amd = provider.parse_webhook(unsigned(twilio_fields(status=None, AnsweredBy="machine_start", MachineDetectionDuration="3400")))
    check("an AMD callback is its own kind of event", amd.kind == WEBHOOK_AMD and amd.status is None)
    check("with the verdict normalised", amd.answered_by == "machine" and amd.machine_answered)
    check("keyed on the verdict", amd.key == "twilio:CA0001:amd:machine", amd.key)
    both = provider.parse_webhook(unsigned(twilio_fields(status="in-progress", seq=2, AnsweredBy="human")))
    check("a status event carrying a verdict is still a status event", both.kind == WEBHOOK_STATUS and both.answered_by == "human")
    check("describes itself in one line", "CA0001" in parsed.describe() and "seq=3" in parsed.describe(), parsed.describe())

    for label, fields in (
        ("no CallSid", {k: v for k, v in twilio_fields(seq=1).items() if k != "CallSid"}),
        ("neither a status nor a verdict", twilio_fields(status=None)),
        ("an empty form", {}),
    ):
        exc = _raises(provider.parse_webhook, unsigned(fields))
        check(f"{label:<32} is a WebhookError", isinstance(exc, WebhookError) and not isinstance(exc, WebhookSignatureError), repr(exc)[:80])


async def check_placement() -> None:
    """What is asked of the carrier when a call is placed with a callback URL."""
    print("\n=== asking the carrier to push events ===")
    session = StubSession([StubResponse(201, call_resource())])
    provider = TwilioProvider(ACCOUNT, TOKEN, session=session)
    request = CallRequest(to_number="+923001234567", from_number="+15550001111", stream_url="wss://abc123.ngrok.app/ws", status_callback_url=URL)
    await provider.place_call(request)
    _, _, sent = session.requests[0]
    pairs = sent["pairs"]
    check("sends the callback URL", ("StatusCallback", URL) in pairs)
    check("as POST", ("StatusCallbackMethod", "POST") in pairs)
    events = [value for name, value in pairs if name == "StatusCallbackEvent"]
    check("asks for every lifecycle event, as repeated fields", events == list(STATUS_CALLBACK_EVENTS), str(events))
    check("no AMD callback without AMD", not any(name == "AsyncAmdStatusCallback" for name, _ in pairs))
    check("the TwiML and the numbers are still sent", sent["data"].get("To") == "+923001234567" and "Twiml" in sent["data"])

    session = StubSession([StubResponse(201, call_resource())])
    provider = TwilioProvider(ACCOUNT, TOKEN, session=session)
    await provider.place_call(dataclasses.replace(request, machine_detection="async"))
    pairs = session.requests[0][2]["pairs"]
    check("with async AMD, the verdict is pushed to the same receiver", ("AsyncAmdStatusCallback", URL) in pairs and ("AsyncAmdStatusCallbackMethod", "POST") in pairs)

    session = StubSession([StubResponse(201, call_resource())])
    provider = TwilioProvider(ACCOUNT, TOKEN, session=session)
    await provider.place_call(dataclasses.replace(request, status_callback_url=None))
    names = {name for name, _ in session.requests[0][2]["pairs"]}
    check("without a URL nothing about callbacks is sent — Phase 4's request, unchanged", not names & {"StatusCallback", "StatusCallbackEvent", "AsyncAmdStatusCallback"})

    print("\n=== the dialer carries the URL ===")
    setup = build()
    await setup.place()
    check("the campaign dialer asks for events", setup.carrier.requests[0].status_callback_url == URL)
    plain = CampaignDialer(setup.service, setup.carrier, from_number="+15550001111", public_url="https://x.test")
    campaign = await setup.store.create_campaign(name="Plain", status=CampaignStatus.ACTIVE)
    prospect = await setup.store.add_prospect(first_name="No", last_name="Hook", phone="+923001234599", phone_normalized="+923001234599")
    await setup.store.add_to_campaign(campaign.id, prospect.id)
    await plain.dial_next(campaign.id)
    check("and a dialer given none asks for none", setup.carrier.requests[-1].status_callback_url is None)


def check_config() -> None:
    """Where events go, and every reason they might not."""
    print("\n=== configuration ===")
    names = (
        "TELEPHONY_PROVIDER", "TELEPHONY_FROM_NUMBER", "TELEPHONY_PUBLIC_URL", "TELEPHONY_STREAM_PATH",
        "TELEPHONY_WEBHOOKS", "TELEPHONY_WEBHOOK_PATH", "TELEPHONY_WEBHOOK_RECEIVER",
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
        "SIGNALWIRE_PROJECT_ID", "SIGNALWIRE_API_TOKEN", "SIGNALWIRE_SPACE_URL", "SIGNALWIRE_SIGNING_KEY",
        "WORKER_WEBHOOK_POLL_SECS",
    )
    saved = {name: os.environ.pop(name, None) for name in names}
    try:
        for given, expected in (
            ("https://abc123.ngrok.app", "https://abc123.ngrok.app/webhooks/telephony"),
            ("https://abc123.ngrok.app/", "https://abc123.ngrok.app/webhooks/telephony"),
            ("wss://abc123.ngrok.app/ws", "https://abc123.ngrok.app/webhooks/telephony"),
            ("abc123.ngrok.app", "https://abc123.ngrok.app/webhooks/telephony"),
            ("http://localhost:7860", "http://localhost:7860/webhooks/telephony"),
            ("https://host/prefix/ws", "https://host/prefix/webhooks/telephony"),
            ("https://host/webhooks/telephony", "https://host/webhooks/telephony"),
        ):
            actual = webhook_url(given)
            check(f"{given!r:<38} -> {expected}", actual == expected, actual)
        check("a custom path is honoured", webhook_url("https://h", "/hooks/calls") == "https://h/hooks/calls")

        telephony = TelephonyConfig.from_env()
        check("with nothing configured there is no URL", telephony.webhook_url() is None)
        check("and the reason is given", "no carrier credentials" in telephony.describe_webhooks(), telephony.describe_webhooks())

        os.environ["TWILIO_ACCOUNT_SID"] = ACCOUNT
        os.environ["TWILIO_AUTH_TOKEN"] = TOKEN
        os.environ["TELEPHONY_FROM_NUMBER"] = "+15550001111"
        telephony = TelephonyConfig.from_env()
        check("credentials without a public URL: no URL, and says so", telephony.webhook_url() is None and "TELEPHONY_PUBLIC_URL" in telephony.describe_webhooks())

        os.environ["TELEPHONY_PUBLIC_URL"] = "https://abc123.ngrok.app"
        telephony = TelephonyConfig.from_env()
        check("Twilio needs nothing more: the auth token signs", telephony.can_verify_webhooks and telephony.webhook_url() == URL)
        check("served by the bot by default", telephony.webhook_receiver == "bot" and "served by the bot" in telephony.describe_webhooks())
        check("the startup line says where events go", URL in telephony.describe())

        os.environ["TELEPHONY_WEBHOOKS"] = "false"
        telephony = TelephonyConfig.from_env()
        check("TELEPHONY_WEBHOOKS=false asks for none", telephony.webhook_url() is None and "TELEPHONY_WEBHOOKS=false" in telephony.describe_webhooks())
        del os.environ["TELEPHONY_WEBHOOKS"]

        os.environ["TELEPHONY_WEBHOOK_PATH"] = "hooks"
        try:
            TelephonyConfig.from_env()
            check("a path without a leading slash is a config problem", False, "accepted")
        except ConfigError as exc:
            check("a path without a leading slash is a config problem", "TELEPHONY_WEBHOOK_PATH" in str(exc))
        os.environ["TELEPHONY_WEBHOOK_PATH"] = "/hooks/calls"
        telephony = TelephonyConfig.from_env()
        check("a custom path is used", telephony.webhook_url() == "https://abc123.ngrok.app/hooks/calls")
        del os.environ["TELEPHONY_WEBHOOK_PATH"]

        os.environ["TELEPHONY_WEBHOOK_RECEIVER"] = "elsewhere"
        try:
            TelephonyConfig.from_env()
            check("an unknown receiver is a config problem", False, "accepted")
        except ConfigError as exc:
            check("an unknown receiver is a config problem", "TELEPHONY_WEBHOOK_RECEIVER" in str(exc))
        os.environ["TELEPHONY_WEBHOOK_RECEIVER"] = "standalone"
        telephony = TelephonyConfig.from_env()
        check("standalone is described as such", "webhooks.py" in telephony.describe_webhooks())
        del os.environ["TELEPHONY_WEBHOOK_RECEIVER"]

        os.environ["TELEPHONY_PROVIDER"] = "signalwire"
        os.environ["SIGNALWIRE_PROJECT_ID"] = "project-abcd"
        os.environ["SIGNALWIRE_API_TOKEN"] = "PT-token"
        os.environ["SIGNALWIRE_SPACE_URL"] = "example.signalwire.com"
        telephony = TelephonyConfig.from_env()
        check("SignalWire without a signing key asks for no events", telephony.webhook_url() is None and not telephony.can_verify_webhooks)
        check("and names SIGNALWIRE_SIGNING_KEY", "SIGNALWIRE_SIGNING_KEY" in telephony.describe_webhooks(), telephony.describe_webhooks())
        check("but can still dial", telephony.is_configured)
        os.environ["SIGNALWIRE_SIGNING_KEY"] = SIGNING_KEY
        telephony = TelephonyConfig.from_env()
        check("with the key, events are asked for", telephony.webhook_url() == URL and telephony.can_verify_webhooks)
        check("the key never appears in a description", SIGNING_KEY not in telephony.describe())
        from src.telephony import make_provider

        provider = make_provider(telephony)
        check("and the provider is built with it", provider.can_verify_webhooks)

        os.environ["WORKER_WEBHOOK_POLL_SECS"] = "45"
        config = Config.from_env()
        check("the worker's webhook poll interval is read", config.worker.webhook_poll_secs == 45.0 and "45s" in config.worker.describe())
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def check_processor() -> None:
    """Valid, forged, repeated and late events, against the rows."""
    print("\n=== a valid sequence of events ===")
    setup = build()
    await setup.place()
    call = setup.call_id
    check("the placed attempt is QUEUED with the carrier's call id", (await setup.attempt()).status is CallAttemptStatus.QUEUED and call.startswith("CA"))
    writes = setup.store.attempt_writes

    receipt = await setup.deliver(twilio_fields(call, "initiated", 0))
    check("`initiated` repeats the status the row already has: accepted, nothing changes", receipt.http_status == 200 and receipt.outcome == WebhookOutcome.STALE, receipt.detail)
    receipt = await setup.deliver(twilio_fields(call, "ringing", 1))
    check("`ringing` is applied", receipt.accepted and receipt.outcome == WebhookOutcome.APPLIED and (await setup.attempt()).status is CallAttemptStatus.CALLING, receipt.detail)
    check("the membership stays IN_PROGRESS while it rings", (await setup.membership()).status is MembershipStatus.IN_PROGRESS)
    receipt = await setup.deliver(twilio_fields(call, "in-progress", 2))
    attempt = await setup.attempt()
    check("`answered` connects it, with connected_at stamped", attempt.status is CallAttemptStatus.CONNECTED and attempt.connected_at is not None)
    mark = _mark()
    receipt = await setup.deliver(twilio_fields(call, "completed", 3, CallDuration="42"))
    attempt = await setup.attempt()
    check("`completed` closes it with the carrier's duration", attempt.status is CallAttemptStatus.COMPLETED and attempt.duration_seconds == 42 and attempt.ended_at is not None)
    check("the membership is COMPLETED and the prospect CONTACTED", (await setup.membership()).status is MembershipStatus.COMPLETED and (await setup.store.get_prospect(attempt.prospect_id)).status is ProspectStatus.CONTACTED)
    check("a structured line records the transition", _logged("webhook.applied", mark) == 1 and "CONNECTED -> COMPLETED" in "".join(LOGS[mark:]))
    check("no carrier request was made for any of it", setup.carrier.fetches == 0)
    rows = setup.store.rows_for(call)
    check("four ledger rows, one per delivery", [r.outcome for r in rows] == ["stale", "applied", "applied", "applied"], str([r.outcome for r in rows]))
    check("each tied to the attempt", all(r.attempt_id == setup.attempt_id for r in rows))
    check("with the carrier's sequence numbers kept", [r.sequence for r in rows] == [0, 1, 2, 3])
    check("the metrics count it", setup.processor.metrics.applied == 3 and setup.processor.metrics.stale == 1 and setup.processor.metrics.received == 4, str(setup.processor.metrics.snapshot()))

    print("\n=== an invalid event ===")
    setup = build()
    await setup.place()
    call = setup.call_id
    before = await setup.attempt()
    writes = setup.store.attempt_writes
    mark = _mark()
    forged = await setup.processor.receive(signed(twilio_fields(call, "completed", 3), secret="attacker"))
    check("a forged signature is refused with 403", forged.http_status == 403 and forged.outcome == "refused", forged.detail[:80])
    check("and nothing is written — not the attempt, not the ledger", setup.store.attempt_writes == writes and not setup.store.rows_for(call) and (await setup.attempt()) == before)
    check("with a warning saying so", _logged("webhook.refused", mark) == 1)
    missing = await setup.processor.receive(unsigned(twilio_fields(call, "completed", 3)))
    check("an unsigned delivery likewise", missing.http_status == 403 and (await setup.attempt()) == before)
    garbage = await setup.deliver({k: v for k, v in twilio_fields(call, "completed", 3).items() if k != "CallSid"})
    check("a signed delivery with no call id is 400", garbage.http_status == 400 and garbage.outcome == "malformed")
    unknown = await setup.deliver(twilio_fields(call, "teleported", 4))
    check("a signed delivery with a status this code does not know is ignored", unknown.accepted and unknown.outcome == WebhookOutcome.IGNORED and (await setup.attempt()).status is CallAttemptStatus.QUEUED)
    check("the refusals are counted", setup.processor.metrics.refused == 2 and setup.processor.metrics.malformed == 1, str(setup.processor.metrics.snapshot()))

    print("\n=== a duplicate event ===")
    setup = build()
    await setup.place()
    call = setup.call_id
    await setup.deliver(twilio_fields(call, "ringing", 1))
    await setup.deliver(twilio_fields(call, "in-progress", 2))
    first = await setup.deliver(twilio_fields(call, "completed", 3, CallDuration="42"))
    membership_writes = setup.store.membership_writes
    attempt_writes = setup.store.attempt_writes
    ended = (await setup.attempt()).ended_at
    setup.clock.advance(5)
    mark = _mark()
    again = await setup.deliver(twilio_fields(call, "completed", 3, CallDuration="42"))
    check("the first delivery is applied, the second is a duplicate", first.outcome == WebhookOutcome.APPLIED and again.outcome == WebhookOutcome.DUPLICATE and again.accepted)
    check("the attempt is untouched by the redelivery", setup.store.attempt_writes == attempt_writes and (await setup.attempt()).ended_at == ended)
    check("and so is the membership", setup.store.membership_writes == membership_writes)
    check("one ledger row, not two", len([r for r in setup.store.rows_for(call) if r.sequence == 3]) == 1)
    check("logged as a duplicate of an applied event", _logged("webhook.duplicate", mark) == 1 and "already applied" in "".join(LOGS[mark:]))
    check("a duplicate `ringing` after the call ended is a duplicate, not a regression", (await setup.deliver(twilio_fields(call, "ringing", 1))).outcome == WebhookOutcome.DUPLICATE and (await setup.attempt()).status is CallAttemptStatus.COMPLETED)

    print("\n=== events out of order ===")
    setup = build()
    await setup.place()
    call = setup.call_id
    done = await setup.deliver(twilio_fields(call, "completed", 3, CallDuration="17"))
    check("`completed` arriving first is applied", done.outcome == WebhookOutcome.APPLIED and (await setup.attempt()).status is CallAttemptStatus.COMPLETED)
    ended = (await setup.attempt()).ended_at
    membership_writes = setup.store.membership_writes
    setup.clock.advance(3)
    mark = _mark()
    late = await setup.deliver(twilio_fields(call, "in-progress", 2))
    attempt = await setup.attempt()
    check("`answered` arriving after it is stale, and accepted so the carrier stops", late.outcome == WebhookOutcome.STALE and late.accepted)
    check("the call stays COMPLETED with its duration and end time", attempt.status is CallAttemptStatus.COMPLETED and attempt.duration_seconds == 17 and attempt.ended_at == ended)
    check("the membership is not reopened", (await setup.membership()).status is MembershipStatus.COMPLETED and setup.store.membership_writes == membership_writes)
    check("logged as stale, saying what it did not follow", _logged("webhook.stale", mark) == 1 and "does not follow COMPLETED" in "".join(LOGS[mark:]))
    earlier = await setup.deliver(twilio_fields(call, "ringing", 1))
    check("`ringing` after that likewise", earlier.outcome == WebhookOutcome.STALE)
    check("every delivery is on the ledger with its outcome", [r.outcome for r in setup.store.rows_for(call)] == ["applied", "stale", "stale"])

    setup = build()
    await setup.place()
    call = setup.call_id
    await setup.deliver(twilio_fields(call, "in-progress", 2))
    ringing = await setup.deliver(twilio_fields(call, "ringing", 1))
    check("a live call does not walk backwards either: ringing after answered is stale", ringing.outcome == WebhookOutcome.STALE and (await setup.attempt()).status is CallAttemptStatus.CONNECTED)

    print("\n=== an event for a call this database never placed ===")
    setup = build()
    await setup.place()
    before = await setup.attempt()
    mark = _mark()
    stranger = await setup.deliver(twilio_fields("CAnobody0000", "completed", 3))
    check("is accepted, so the carrier does not retry, and marked unmatched", stranger.accepted and stranger.outcome == WebhookOutcome.UNMATCHED)
    check("touches no attempt", (await setup.attempt()) == before)
    check("and is on the ledger, unmatched, with no attempt", setup.store.rows_for("CAnobody0000")[0].outcome == "unmatched" and setup.store.rows_for("CAnobody0000")[0].attempt_id is None)
    check("logged as unmatched", _logged("webhook.unmatched", mark) == 1)

    print("\n=== the unhappy endings ===")
    for word, expected, membership_after in (
        ("busy", CallAttemptStatus.BUSY, MembershipStatus.PENDING),
        ("no-answer", CallAttemptStatus.NO_ANSWER, MembershipStatus.PENDING),
        # Phase 21: a `failed` with a SIP 503 is the carrier's failure, not
        # the number's, so the membership is queued again; `canceled` stays final.
        ("failed", CallAttemptStatus.FAILED, MembershipStatus.PENDING),
        ("canceled", CallAttemptStatus.FAILED, MembershipStatus.EXHAUSTED),
    ):
        setup = build(max_attempts=3)
        await setup.place()
        await setup.deliver(twilio_fields(setup.call_id, "ringing", 1))
        extra = {"SipResponseCode": "503"} if word == "failed" else {}
        receipt = await setup.deliver(twilio_fields(setup.call_id, word, 2, **extra))
        attempt = await setup.attempt()
        membership = await setup.membership()
        check(f"{word:<10} -> {expected.value}, membership {membership_after.value}", receipt.outcome == WebhookOutcome.APPLIED and attempt.status is expected and membership.status is membership_after, f"{attempt.status.value} / {membership.status.value}")
        if membership_after is MembershipStatus.PENDING:
            check(f"{word:<10} is scheduled for a retry", membership.next_attempt_at is not None and membership.next_attempt_at > setup.clock())
        if word == "failed":
            check("failed keeps the carrier's reason on the row", attempt.failure_reason == "SIP 503", str(attempt.failure_reason))
    setup = build(max_attempts=1)
    await setup.place()
    await setup.deliver(twilio_fields(setup.call_id, "busy", 1))
    check("busy on the last permitted attempt exhausts the membership", (await setup.membership()).status is MembershipStatus.EXHAUSTED)

    print("\n=== the answering-machine verdict ===")
    setup = build()
    await setup.place()
    call = setup.call_id
    await setup.deliver(twilio_fields(call, "in-progress", 2))
    mark = _mark()
    verdict = await setup.deliver(twilio_fields(call, None, AnsweredBy="machine_start"))
    check("an AMD delivery is noted, and changes no status", verdict.outcome == WebhookOutcome.NOTED and (await setup.attempt()).status is CallAttemptStatus.CONNECTED)
    check("logged as the carrier's verdict", _logged("call.answered_by", mark) == 1)
    await setup.deliver(twilio_fields(call, "completed", 3, CallDuration="9"))
    check("the completion that follows becomes VOICEMAIL — Phase 12's rule, without a poll", (await setup.attempt()).status is CallAttemptStatus.VOICEMAIL)
    check("and the membership is retried, as after a poll", (await setup.membership()).status is MembershipStatus.PENDING)
    setup = build()
    await setup.place()
    await setup.deliver(twilio_fields(setup.call_id, None, AnsweredBy="human"))
    await setup.deliver(twilio_fields(setup.call_id, "completed", 3, CallDuration="60"))
    check("a human verdict leaves a completion COMPLETED", (await setup.attempt()).status is CallAttemptStatus.COMPLETED)
    setup = build()
    await setup.place()
    await setup.deliver(twilio_fields(setup.call_id, "completed", 3, CallDuration="4"))
    mark = _mark()
    late = await setup.deliver(twilio_fields(setup.call_id, None, AnsweredBy="machine_end_beep"))
    check("a verdict after the call is final is noted and warned about, and the record stands", late.outcome == WebhookOutcome.NOTED and _logged("webhook.late_verdict", mark) == 1 and (await setup.attempt()).status is CallAttemptStatus.COMPLETED)

    print("\n=== what the conversation wrote outranks the carrier ===")
    setup = build()
    await setup.place()
    call = setup.call_id
    await setup.deliver(twilio_fields(call, "in-progress", 2))
    # The bot's sink, at the end of the call: the person asked to be phoned back.
    await setup.service.record_outcome(await setup.attempt(), CallAttemptStatus.CALLBACK_REQUESTED, write_result=False)
    membership_writes = setup.store.membership_writes
    receipt = await setup.deliver(twilio_fields(call, "completed", 3, CallDuration="88"))
    attempt = await setup.attempt()
    check("the carrier's `completed` after a conversation outcome is stale", receipt.outcome == WebhookOutcome.STALE and attempt.status is CallAttemptStatus.CALLBACK_REQUESTED)
    check("and the membership is left as the conversation left it", setup.store.membership_writes == membership_writes)

    print("\n=== when the database is not there ===")
    setup = build()
    await setup.place()
    call = setup.call_id
    setup.store.fail_with = CampaignStoreError("the database went away")
    mark = _mark()
    down = await setup.deliver(twilio_fields(call, "completed", 3))
    check("a database that is gone answers 503, so the carrier may retry", down.http_status == 503 and down.outcome == "error")
    check("with an error line naming the fallback", _logged("webhook.store_unavailable", mark) == 1)
    setup.store.fail_with = None
    retried = await setup.deliver(twilio_fields(call, "completed", 3))
    check("and the retry lands once it is back", retried.outcome == WebhookOutcome.APPLIED and (await setup.attempt()).status is CallAttemptStatus.COMPLETED)

    setup = build()
    await setup.place()
    call = setup.call_id
    setup.store.ledger_missing = True
    mark = _mark()
    first = await setup.deliver(twilio_fields(call, "ringing", 1))
    second = await setup.deliver(twilio_fields(call, "ringing", 1))
    done = await setup.deliver(twilio_fields(call, "completed", 3, CallDuration="5"))
    check("a database that predates the ledger still applies events", first.outcome == WebhookOutcome.APPLIED and done.outcome == WebhookOutcome.APPLIED and (await setup.attempt()).status is CallAttemptStatus.COMPLETED)
    check("a redelivery is then caught by the monotonic write instead", second.outcome == WebhookOutcome.STALE)
    check("and the missing table is reported once, naming the command", _logged("webhook.ledger_missing", mark) == 1 and "campaign.py init" in "".join(LOGS[mark:]))


async def check_http_route() -> None:
    """The route, through FastAPI's test client."""
    print("\n=== the HTTP route ===")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    setup = build()
    await setup.place()
    call = setup.call_id

    async def get_processor() -> WebhookProcessor:
        return setup.processor

    app = FastAPI()
    app.include_router(create_webhook_router(get_processor, path="/webhooks/telephony"))

    with TestClient(app) as client:
        fields = twilio_fields(call, "ringing", 1)
        good = client.post("/webhooks/telephony", data=fields, headers={"X-Twilio-Signature": reference_signature(TOKEN, URL, fields)})
        check("a signed POST is accepted", good.status_code == 200 and good.text == "applied", f"{good.status_code} {good.text}")
        check("and applied to the attempt", (await setup.attempt()).status is CallAttemptStatus.CALLING)

        fields = twilio_fields(call, "in-progress", 2)
        local = client.post("/webhooks/telephony", data=fields, headers={"X-Twilio-Signature": reference_signature(TOKEN, "http://testserver/webhooks/telephony", fields)})
        check("a signature over the server's own URL is refused: the configured public URL is what the carrier signed", local.status_code == 403, f"{local.status_code} {local.text}")
        check("so the attempt is unchanged", (await setup.attempt()).status is CallAttemptStatus.CALLING)

        bad = client.post("/webhooks/telephony", data=fields, headers={"X-Twilio-Signature": "nonsense"})
        check("a bad signature is 403 with a plain-text body", bad.status_code == 403 and bad.text == "refused")
        none = client.post("/webhooks/telephony", data=fields)
        check("no signature is 403", none.status_code == 403)
        check("GET is not served", client.get("/webhooks/telephony").status_code == 405)
        json_body = client.post("/webhooks/telephony", json={"CallSid": call}, headers={"X-Twilio-Signature": "x"})
        check("a JSON body is not a form the carrier sends: refused before anything is read", json_body.status_code in (400, 403))

        again = client.post("/webhooks/telephony", data=twilio_fields(call, "ringing", 1), headers={"X-Twilio-Signature": reference_signature(TOKEN, URL, twilio_fields(call, "ringing", 1))})
        check("a redelivered event answers 200 duplicate", again.status_code == 200 and again.text == "duplicate")

    print("\n=== mounting on the bot ===")

    def routes_of(app: Any) -> list[tuple[str, set[str]]]:
        """Every (path, methods) the app serves. `include_router` wraps its routes; unwrap them."""
        found: list[tuple[str, set[str]]] = []

        def walk(routes: Any) -> None:
            for route in routes:
                inner = getattr(route, "original_router", None)
                if inner is not None:
                    walk(inner.routes)
                elif hasattr(route, "path"):
                    found.append((route.path, set(getattr(route, "methods", None) or [])))

        walk(app.routes)
        return found

    def mounted_on(app: Any) -> list[set[str]]:
        return [methods for path, methods in routes_of(app) if path == "/webhooks/telephony"]

    names = ("TELEPHONY_PROVIDER", "TELEPHONY_FROM_NUMBER", "TELEPHONY_PUBLIC_URL", "TELEPHONY_WEBHOOKS", "TELEPHONY_WEBHOOK_RECEIVER", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "DATABASE_URL", "KB_DATABASE_URL")
    saved = {name: os.environ.pop(name, None) for name in names}
    try:
        os.environ.update({"TELEPHONY_PROVIDER": "twilio", "TWILIO_ACCOUNT_SID": ACCOUNT, "TWILIO_AUTH_TOKEN": TOKEN, "TELEPHONY_FROM_NUMBER": "+15550001111", "TELEPHONY_PUBLIC_URL": "https://abc123.ngrok.app", "DATABASE_URL": "postgresql://nobody@localhost/never-opened"})
        config = Config.from_env()
        app = FastAPI()
        mounted = install_webhook_receiver(app, config)
        check(
            "the receiver mounts one POST route on the bot's app",
            mounted == URL and mounted_on(app) == [{"POST"}],
            f"mounted={mounted!r} routes={routes_of(app)}",
        )
        check("and touches no other route", [p for p, _ in routes_of(app) if not p.startswith(("/docs", "/redoc", "/openapi"))] == ["/webhooks/telephony"])

        os.environ["TELEPHONY_WEBHOOK_RECEIVER"] = "standalone"
        app = FastAPI()
        check("with the standalone receiver chosen, the bot mounts nothing", install_webhook_receiver(app, Config.from_env()) is None and not mounted_on(app))
        standalone = create_webhook_app(Config.from_env())
        check("and the standalone app carries the route instead", mounted_on(standalone) == [{"POST"}] and any(p == "/api/ping" for p, _ in routes_of(standalone)))
        del os.environ["TELEPHONY_WEBHOOK_RECEIVER"]

        os.environ["TELEPHONY_WEBHOOKS"] = "false"
        app = FastAPI()
        check("with webhooks off, the bot mounts nothing", install_webhook_receiver(app, Config.from_env()) is None and not mounted_on(app))
        try:
            create_webhook_app(Config.from_env())
            check("and the standalone app refuses to build, saying why", False, "built")
        except ConfigError as exc:
            check("and the standalone app refuses to build, saying why", "TELEPHONY_WEBHOOKS=false" in str(exc))
        del os.environ["TELEPHONY_WEBHOOKS"]
        del os.environ["DATABASE_URL"]
        app = FastAPI()
        check("with no database, the bot mounts nothing", install_webhook_receiver(app, Config.from_env()) is None and not mounted_on(app))

        source = (SERVER / "bot.py").read_text(encoding="utf-8")
        check("bot.py mounts the receiver before the runner starts", "install_webhook_receiver(app, CONFIG)" in source and source.index("install_webhook_receiver(app, CONFIG)") < source.rindex("main()"))
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def check_worker() -> None:
    """The worker polls less once the carrier is pushing, and not at all differently until it is."""
    print("\n=== the worker, with events arriving ===")

    def world_with_ledger(*, script: list[CallStatus]) -> tuple[World, LedgerStore, WebhookProcessor]:
        clock = FakeClock(NOW)
        store = LedgerStore(clock=clock)
        service = CampaignService(store, default_region="PK", max_attempts=3, retry_minutes=60, clock=clock)  # type: ignore[arg-type]
        carrier = ScriptedCarrier(clock, script=script)
        guards = CampaignGuards(window=CallingWindow.parse("00:00-23:59", "mon-sun", "UTC", clock=clock), pacing=PacingLimiter(0, clock=clock.monotonic), max_concurrent=1)
        dialer = CampaignDialer(service, carrier, from_number="+15550001111", public_url="https://abc123.ngrok.app", guards=guards, status_callback_url=URL)
        recovery = AttemptRecovery(service, carrier, min_age_secs=120.0)
        world = World(clock=clock, store=store, service=service, carrier=carrier, guards=guards, dialer=dialer, recovery=recovery)
        processor = WebhookProcessor(service, TwilioProvider(ACCOUNT, TOKEN), expected_url=URL)
        return world, store, processor

    # A call the carrier never reports finished on its own: only a webhook ends it.
    world, store, processor = world_with_ledger(script=[CallStatus.RINGING, CallStatus.ANSWERED])
    campaign = await world.campaign("Pushed", ["+923001111111"])
    events = {2: ("ringing", 1), 4: ("in-progress", 2), 34: ("completed", 3)}

    async def push(n: int) -> None:
        if n in events:
            status, seq = events[n]
            call = world.carrier.last_call_id
            extra = {"CallDuration": "58"} if status == "completed" else {}
            receipt = await processor.receive(signed(twilio_fields(call, status, seq, **extra)))
            assert receipt.accepted, receipt.detail

    world.on_sleep = push
    mark = _mark()
    worker = world.make_worker(campaign_ids=[campaign.id], max_calls=1, webhook_poll_secs=30.0)
    metrics = await worker.run()
    attempts = await world.attempts_to("+923001111111")
    check("the call ends by webhook, and the worker sees it end", metrics.completed == 1 and attempts[-1].status is CallAttemptStatus.COMPLETED and attempts[-1].duration_seconds == 58, metrics.describe())
    check("the worker noticed the carrier was pushing", _logged("call.pushed", mark) == 1)
    check("and asked the carrier only as a safety net", world.carrier.fetches <= 4, f"{world.carrier.fetches} fetches over {len(world.slept)} ticks")
    check("a transition the webhook wrote is logged by the worker as a status change", any("call.status" in line and "CALLING -> CONNECTED" in line for line in LOGS[mark:]))
    check("the completion line says the call was pushed", any("call.completed" in line and "pushed=True" in line for line in LOGS[mark:]))
    check("and the campaign closed itself", (await store.get_campaign(campaign.id)).status is CampaignStatus.COMPLETED)

    print("\n=== the worker, with no events arriving ===")
    world, store, processor = world_with_ledger(script=[CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.ANSWERED, CallStatus.ANSWERED, CallStatus.COMPLETED])
    campaign = await world.campaign("Polled", ["+923002222222"])
    mark = _mark()
    worker = world.make_worker(campaign_ids=[campaign.id], max_calls=1, webhook_poll_secs=30.0)
    metrics = await worker.run()
    check("a call the carrier says nothing about is polled to its end as before", metrics.completed == 1 and world.carrier.fetches >= 5, f"{world.carrier.fetches} fetches")
    check("and never treated as pushed", _logged("call.pushed", mark) == 0)

    world, store, processor = world_with_ledger(script=[CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.COMPLETED])
    campaign = await world.campaign("Off", ["+923003333333"])
    worker = world.make_worker(campaign_ids=[campaign.id], max_calls=1, webhook_poll_secs=0.0)
    ticks_before = world.carrier.fetches
    metrics = await worker.run()
    check("with the setting off, every tick polls — Phase 13's worker, unchanged", metrics.completed == 1 and world.carrier.fetches == 3, f"{world.carrier.fetches} fetches")

    print("\n=== the worker, on a database without the ledger ===")
    world, store, processor = world_with_ledger(script=[CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.COMPLETED])
    store.ledger_missing = True
    campaign = await world.campaign("Old", ["+923004444444"])
    mark = _mark()
    worker = world.make_worker(campaign_ids=[campaign.id], max_calls=1, webhook_poll_secs=30.0)
    metrics = await worker.run()
    check("polls every tick and says once why", metrics.completed == 1 and world.carrier.fetches == 3 and _logged("worker.webhooks_unavailable", mark) == 1)

    print("\n=== the worker and the receiver, racing on one call ===")
    # The webhook and a poll report the same ending at the same moment: one wins,
    # the other is a no-op, and the membership moves on exactly once.
    world, store, processor = world_with_ledger(script=[CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.COMPLETED])
    campaign = await world.campaign("Race", ["+923005555555"])

    async def push_completed(n: int) -> None:
        if n == 3:
            call = world.carrier.last_call_id
            await processor.receive(signed(twilio_fields(call, "completed", 3, CallDuration="42")))

    world.on_sleep = push_completed
    worker = world.make_worker(campaign_ids=[campaign.id], max_calls=1, webhook_poll_secs=0.0)
    metrics = await worker.run()
    attempts = await world.attempts_to("+923005555555")
    membership = await world.membership_of("+923005555555", campaign)
    check("one call, one completion, one membership closed", metrics.completed == 1 and len(attempts) == 1 and attempts[0].status is CallAttemptStatus.COMPLETED and membership.status is MembershipStatus.COMPLETED)


async def run_database_checks(dsn: str) -> None:
    """The ledger and the processor, against real rows in a schema that is thrown away."""
    from test_campaigns import with_temp_schema

    store, admin, schema = await with_temp_schema(dsn)
    try:
        print("\n=== the ledger, against PostgreSQL ===")
        check("create_schema makes the ledger", await store.webhook_counts() == {})
        first, inserted = await store.record_webhook_event(provider="twilio", call_id="CAsql1", event_key="twilio:CAsql1:status:seq:1", kind="status", status="ringing", raw_status="ringing", sequence=1, payload={"CallStatus": "ringing"})
        again, inserted_again = await store.record_webhook_event(provider="twilio", call_id="CAsql1", event_key="twilio:CAsql1:status:seq:1", kind="status", status="ringing", raw_status="ringing", sequence=1, payload={"CallStatus": "ringing"})
        check("the first delivery is inserted", inserted and first is not None and first.outcome == "received")
        check("the second with the same key is not, and is handed the first", not inserted_again and again is not None and again.id == first.id)
        check("the delivery is found by call id, newest first", [r.id for r in await store.list_webhook_events(call_id="CAsql1")] == [first.id])
        check("last_webhook_at answers for a pushed call and not for a silent one", await store.last_webhook_at("CAsql1") is not None and await store.last_webhook_at("CAsilent") is None)
        check("no verdict yet", await store.webhook_answered_by("CAsql1") is None)
        await store.record_webhook_event(provider="twilio", call_id="CAsql1", event_key="twilio:CAsql1:amd:machine", kind="amd", answered_by="machine", payload={})
        check("the latest verdict is read back", await store.webhook_answered_by("CAsql1") == "machine")
        await store.set_webhook_outcome(first.id, "applied")
        check("the outcome is written", (await store.list_webhook_events(call_id="CAsql1"))[-1].outcome == "applied")
        check("and counted", (await store.webhook_counts()).get("applied") == 1)

        print("\n=== the processor over real rows ===")
        service = CampaignService(store, default_region="PK", max_attempts=2, retry_minutes=60)
        campaign = await service.create_campaign(f"Webhooks {uuid.uuid4().hex[:6]}")
        await service.set_status(campaign.id, CampaignStatus.ACTIVE)
        prospect = await service.create_prospect(first_name="Web", last_name="Hook", phone="0306 1111111")
        await service.add_prospects(campaign.id, [prospect.id])
        queued = await service.next_call(campaign.id)
        await store.mark_placement_started(queued.attempt.id)
        placed = await store.mark_attempt_placed(queued.attempt.id, telephony_call_id="CAwebhook1", provider="twilio")
        processor = WebhookProcessor(service, TwilioProvider(ACCOUNT, TOKEN), expected_url=URL)

        receipt = await processor.receive(signed(twilio_fields("CAwebhook1", "ringing", 1)))
        check("ringing is applied to the real row", receipt.outcome == WebhookOutcome.APPLIED and (await store.get_attempt(placed.id)).status is CallAttemptStatus.CALLING)
        receipt = await processor.receive(signed(twilio_fields("CAwebhook1", "ringing", 1)))
        check("its redelivery is a duplicate at the database", receipt.outcome == WebhookOutcome.DUPLICATE)
        receipt = await processor.receive(signed(twilio_fields("CAwebhook1", "completed", 3, CallDuration="42")))
        attempt = await store.get_attempt(placed.id)
        check("completed closes it with the duration", receipt.outcome == WebhookOutcome.APPLIED and attempt.status is CallAttemptStatus.COMPLETED and attempt.duration_seconds == 42)
        membership = await store.get_membership(attempt.campaign_prospect_id)
        check("the membership is COMPLETED", membership.status is MembershipStatus.COMPLETED)
        check("and the carrier's call result is written", (await store.get_call_result(attempt.id)) is not None)
        receipt = await processor.receive(signed(twilio_fields("CAwebhook1", "in-progress", 2)))
        check("the late answered event is stale against the real row", receipt.outcome == WebhookOutcome.STALE and (await store.get_attempt(placed.id)).status is CallAttemptStatus.COMPLETED)
        rows = await store.list_webhook_events(attempt_id=placed.id)
        check("three ledger rows tied to the attempt, with their outcomes", sorted(r.outcome for r in rows) == ["applied", "applied", "stale"], str([r.outcome for r in rows]))
        forged = await processor.receive(signed(twilio_fields("CAwebhook1", "completed", 4), secret="attacker"))
        check("a forged delivery writes nothing to the ledger either", forged.http_status == 403 and len(await store.list_webhook_events(call_id="CAwebhook1")) == 3)

        second = await service.create_prospect(first_name="Voice", last_name="Mail", phone="0306 2222222")
        await service.add_prospects(campaign.id, [second.id])
        queued = await service.next_call(campaign.id)
        await store.mark_placement_started(queued.attempt.id)
        await store.mark_attempt_placed(queued.attempt.id, telephony_call_id="CAwebhook2", provider="twilio")
        await processor.receive(signed(twilio_fields("CAwebhook2", "in-progress", 2)))
        await processor.receive(signed(twilio_fields("CAwebhook2", None, AnsweredBy="machine_end_beep")))
        await processor.receive(signed(twilio_fields("CAwebhook2", "completed", 3, CallDuration="6")))
        attempt = await store.get_attempt(queued.attempt.id)
        check("a machine verdict then a completion is VOICEMAIL on the real row", attempt.status is CallAttemptStatus.VOICEMAIL)
        check("with the membership scheduled to retry", (await store.get_membership(attempt.campaign_prospect_id)).status is MembershipStatus.PENDING)
        check("nothing live is left behind", await store.count_live_attempts() == 0)
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def main() -> int:
    """Run every check and report."""
    print("Webhook checks — real signatures, the real processor, the real worker, and rows to assert on.")
    handler = logger.add(LOGS.append, format="{message}", level="DEBUG")
    try:
        check_signatures()
        check_parsing()
        await check_placement()
        check_config()
        await check_processor()
        await check_http_route()
        await check_worker()

        from dotenv import load_dotenv

        load_dotenv(override=True)
        dsn = os.getenv("DATABASE_URL") or os.getenv("KB_DATABASE_URL")
        if not dsn:
            _skipped.append("database checks (no DATABASE_URL or KB_DATABASE_URL)")
        else:
            import asyncpg

            try:
                await run_database_checks(dsn)
            except (OSError, asyncpg.PostgresError) as exc:
                _skipped.append(f"database checks (cannot reach PostgreSQL: {exc})")
    finally:
        logger.remove(handler)

    print()
    if _skipped:
        print("SKIPPED:")
        for item in _skipped:
            print(f"  - {item}")
        print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed." + (" (some were skipped)" if _skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
