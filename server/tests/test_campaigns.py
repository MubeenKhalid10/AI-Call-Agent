#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for the prospect, campaign, queue and call-attempt layer.

Run it from the `server/` directory::

    uv run python tests/test_campaigns.py

**Two halves, and only one of them needs PostgreSQL.**

The first half — phone normalisation, CSV mapping, row validation, duplicate
detection — is pure logic over strings, and is checked exhaustively with no
database and no network. That is deliberate: those are the parts where being
wrong means calling the wrong person, so they are the parts that must be
testable in a second, on any machine, with nothing installed.

The second half needs a real database, because what it is checking *is* the SQL:
that the queue's eligibility rules hold under a transaction, that a unique
constraint stops a duplicate, that a prospect can belong to two campaigns. A
stub store would only prove the stub agrees with itself. So these run against
PostgreSQL in a **temporary schema** which is dropped afterwards — the real
tables are never touched — and are skipped with a clear message when no database
is reachable, so the file still passes on a machine without one.

**No real phone calls.** The telephony provider is stubbed with one that records
what it was asked and returns canned outcomes, so the dialer's whole path —
reserve, check, place, record, retry — is exercised without a carrier account
and without spending a penny.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from src.campaigns import (  # noqa: E402
    CallAttemptStatus,
    CampaignDialer,
    CampaignService,
    CampaignStatus,
    CampaignStore,
    DuplicateProspectError,
    MembershipStatus,
    ProspectStatus,
    map_headers,
    normalize_phone,
    parse_csv,
    same_number,
)
from src.telephony import (  # noqa: E402
    CallRequest,
    CallSetupError,
    CallSnapshot,
    CallStatus,
    TelephonyProvider,
)

load_dotenv(override=True)

REGION = "PK"

_failures: list[str] = []
_skipped: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


# --- A telephony provider that never dials --------------------------------


class StubProvider(TelephonyProvider):
    """A `TelephonyProvider` that records requests and returns canned outcomes.

    The real provider's contract, with the phone network removed. Because the
    dialer talks to `TelephonyProvider` and never to a carrier, this is enough
    to exercise every path it has — including the ones that only happen when a
    carrier refuses.
    """

    name = "stub"
    transports = ("twilio",)

    def __init__(self, *, outcome: CallStatus = CallStatus.QUEUED, refuse: str | None = None):
        """Create the stub.

        Args:
            outcome: What `fetch_call` reports for every call.
            refuse: When set, `place_call` raises this as a `CallSetupError` —
                the carrier declining, which the dialer has to survive.
        """
        self.requests: list[CallRequest] = []
        self.hung_up: list[str] = []
        self._outcome = outcome
        self._refuse = refuse
        self._next_id = 0

    async def place_call(self, request: CallRequest) -> CallSnapshot:
        self.requests.append(request)
        if self._refuse:
            raise CallSetupError(self._refuse)
        self._next_id += 1
        return CallSnapshot(
            provider=self.name,
            call_id=f"CAstub{self._next_id:04d}",
            status=CallStatus.QUEUED,
            to_number=request.to_number,
            from_number=request.from_number,
        )

    async def fetch_call(self, call_id: str) -> CallSnapshot:
        return CallSnapshot(
            provider=self.name,
            call_id=call_id,
            status=self._outcome,
            duration_secs=42.0 if self._outcome is CallStatus.COMPLETED else None,
        )

    async def hang_up(self, call_id: str) -> None:
        self.hung_up.append(call_id)

    async def transfer_call(self, call_id: str, to_number: str, *, caller_id: str | None = None) -> None:
        self.transferred = getattr(self, "transferred", [])
        self.transferred.append((call_id, to_number))

    def make_serializer(self, call_data):  # pragma: no cover - never used here
        raise NotImplementedError


# --- Pure logic: phone numbers ---------------------------------------------


def check_phone_normalization() -> None:
    """E.164 normalisation, and refusing to guess when it cannot."""
    print("\n=== phone normalization ===")

    # The three spellings from the requirement, which must collapse to one
    # number or the same person gets imported and called three times.
    for written in ("+92 322 1234567", "+923221234567", "0092 322 1234567"):
        result = normalize_phone(written, default_region=REGION)
        check(f"{written!r:22} -> +923221234567", result.e164 == "+923221234567", str(result.e164))

    for written, expected in (
        ("0322 1234567", "+923221234567"),
        ("(0322) 123-4567", "+923221234567"),
        ("+1 415 555 2671", "+14155552671"),
        ("  +92 322 1234567  ", "+923221234567"),
    ):
        result = normalize_phone(written, default_region=REGION)
        check(f"{written!r:22} -> {expected}", result.e164 == expected, str(result.e164))

    print("\n  refusing to guess:")
    # A local-format number with no region cannot be resolved to a country, and
    # guessing one would dial a stranger. It must fail, not default.
    local = normalize_phone("0322 1234567")
    check("a local number with no region is refused", not local.is_dialable, str(local.e164))
    check("and says how to fix it", "DEFAULT_PHONE_REGION" in local.reason)

    for written in ("12345", "not a phone", "+92 322 12", "", "   "):
        result = normalize_phone(written, default_region=REGION)
        check(f"{written!r:22} is not dialable", not result.is_dialable, str(result.e164))
        check(f"{written!r:22} explains why", bool(result.reason))

    # The raw value survives normalisation, which is what makes a wrong
    # normalisation diagnosable later.
    kept = normalize_phone("  +92 322 1234567 ", default_region=REGION)
    check("the original is preserved", kept.raw == "+92 322 1234567", kept.raw)

    print("\n  duplicate detection:")
    check(
        "different spellings are the same number",
        same_number("+92 322 1234567", "0092 322 1234567", default_region=REGION),
    )
    check(
        "local and international forms match",
        same_number("0322 1234567", "+923221234567", default_region=REGION),
    )
    check(
        "different numbers do not match",
        not same_number("+923221234567", "+923221234568", default_region=REGION),
    )
    # Two numbers nobody could parse are not evidence of being the same person.
    check(
        "two unparseable numbers are not equal",
        not same_number("junk", "junk", default_region=REGION),
    )


# --- Pure logic: CSV --------------------------------------------------------


def check_csv_mapping() -> None:
    """Header aliases, required columns, and what happens to unknown ones."""
    print("\n=== CSV column mapping ===")

    for headers, field, expected in (
        (["First Name"], "first_name", "First Name"),
        (["first_name"], "first_name", "first_name"),
        (["firstname"], "first_name", "firstname"),
        (["FirstName"], "first_name", "FirstName"),
        (["FIRST-NAME"], "first_name", "FIRST-NAME"),
        (["Last Name"], "last_name", "Last Name"),
        (["surname"], "last_name", "surname"),
        (["Phone"], "phone", "Phone"),
        (["Phone Number"], "phone", "Phone Number"),
        (["mobile"], "phone", "mobile"),
        (["Mobile Number"], "phone", "Mobile Number"),
        (["Job Title"], "job_title", "Job Title"),
        (["Title"], "job_title", "Title"),
        (["Company"], "company", "Company"),
        (["Organization"], "company", "Organization"),
        (["Email"], "email", "Email"),
        (["Email Address"], "email", "Email Address"),
    ):
        mapping = map_headers(headers)
        check(f"{headers[0]!r:18} -> {field}", mapping.columns.get(field) == expected)

    print("\n  whole files:")
    mapping = map_headers(["First Name", "Last Name", "Phone", "Company", "Lead Score"])
    check("required columns are found", mapping.is_usable, str(mapping.missing_required))
    check("unknown columns are kept", mapping.extras == ["Lead Score"], str(mapping.extras))

    mapping = map_headers(["First Name", "Phone"])
    check("a missing required column is named", mapping.missing_required == ["last_name"])
    check("and the mapping is unusable", not mapping.is_usable)

    # A file with both Phone and Mobile: picking one silently is how the wrong
    # number gets dialled, so the second is reported.
    mapping = map_headers(["First Name", "Last Name", "Phone", "Mobile"])
    check(
        "a second phone column is reported", "phone" in mapping.duplicates, str(mapping.duplicates)
    )
    check("and the first one wins", mapping.columns["phone"] == "Phone")

    # A single Name column stands in for both name fields...
    mapping = map_headers(["Name", "Phone"])
    check("a combined name column is usable", mapping.is_usable)
    check("and is recorded as the split source", mapping.full_name_column == "Name")

    # ...but only when the specific columns are absent.
    mapping = map_headers(["Name", "First Name", "Last Name", "Phone"])
    check("specific name columns win over a combined one", mapping.full_name_column is None)


def check_csv_parsing() -> None:
    """Row validation: what gets in, what does not, and why."""
    print("\n=== CSV rows ===")

    report = parse_csv(
        "First Name,Last Name,Phone,Company,Lead Score\n"
        "Ayesha,Khan,+92 322 1234567,Meridian,88\n"
        "Bilal,Ahmed,0333 7654321,Northwind,42\n",
        default_region=REGION,
    )
    check("both rows are usable", len(report.valid_rows) == 2, report.summary())
    check("the phone is normalised", report.valid_rows[0].phone.e164 == "+923221234567")
    check(
        "unmapped columns become custom data",
        report.valid_rows[0].custom_data == {"Lead Score": "88"},
        str(report.valid_rows[0].custom_data),
    )

    print("\n  bad input does not crash anything:")
    report = parse_csv(
        "First Name,Last Name,Phone\n"
        "Ayesha,Khan,+923221234567\n"
        "\n"  # blank line
        ",,\n"  # empty row
        "NoPhone,Person,\n"
        "Bad,Number,12345\n"
        "Dup,Licate,+92 322 1234567\n"
        ",Missing,+923009999999\n",
        default_region=REGION,
    )
    check("the good row survives", len(report.valid_rows) == 1, report.summary())
    # One, not two: `csv.DictReader` drops a truly empty line before we see it,
    # so only the `,,` row reaches the blank check. Both are ignored either way.
    check(
        "blank rows are counted, not reported", report.skipped_blank == 1, str(report.skipped_blank)
    )

    reasons = {row.line: "; ".join(row.errors) for row in report.invalid_rows}
    check("a missing phone is reported", "missing phone" in reasons.get(5, ""), str(reasons.get(5)))
    check("an invalid phone is reported", "phone" in reasons.get(6, ""), str(reasons.get(6)))
    check(
        "a duplicate phone is caught within the file",
        "same phone as line 2" in reasons.get(7, ""),
        str(reasons.get(7)),
    )
    check(
        "a missing first name is reported",
        "missing first name" in reasons.get(8, ""),
        str(reasons.get(8)),
    )

    print("\n  malformed files:")
    check("an empty file is reported", parse_csv("").error is not None)
    check("a headerless file is reported", parse_csv("\n\n").error is not None)

    missing = parse_csv("Name Of Person,Telephone\nx,y\n", default_region=REGION)
    check("unmappable headers stop the import", not missing.mapping.is_usable)
    check("and no rows are claimed usable", missing.valid_rows == [])

    ragged = parse_csv(
        "First Name,Last Name,Phone\nAyesha,Khan,+923221234567,extra\n", default_region=REGION
    )
    check("a row with too many values is reported", len(ragged.invalid_rows) == 1, ragged.summary())

    split = parse_csv("Name,Phone\nAyesha Khan,+923221234567\n", default_region=REGION)
    check("a combined name is split", split.valid_rows[0].values["first_name"] == "Ayesha")
    check("into both fields", split.valid_rows[0].values["last_name"] == "Khan")

    # An unusable email must not stop somebody being phoned.
    email = parse_csv(
        "First Name,Last Name,Phone,Email\nAyesha,Khan,+923221234567,not-an-email\n",
        default_region=REGION,
    )
    check("a bad email does not reject the row", len(email.valid_rows) == 1, email.summary())
    check("and is kept for correction", "email_invalid" in email.valid_rows[0].custom_data)


# --- Database-backed checks -------------------------------------------------


def csv_text(*rows: str) -> str:
    """A CSV with the standard header and the given rows."""
    return "First Name,Last Name,Phone,Company\n" + "".join(row + "\n" for row in rows)


async def check_persistence(store: CampaignStore) -> None:
    """Prospects, campaigns, memberships and attempts, against real SQL."""
    service = CampaignService(store, default_region=REGION, max_attempts=2, retry_minutes=60)

    print("\n=== prospects ===")
    prospect = await service.create_prospect(
        first_name="Ayesha", last_name="Khan", phone="0322 1234567", company="Meridian"
    )
    check("a prospect is created", prospect.id > 0)
    check("the number is normalised", prospect.phone_normalized == "+923221234567")
    check("the original is kept", prospect.phone == "0322 1234567")
    check("it starts as NEW", prospect.status is ProspectStatus.NEW)
    check("and is callable", prospect.is_callable)

    # The same person written differently must not become a second row, or they
    # get called twice.
    try:
        await service.create_prospect(
            first_name="Ayesha", last_name="Khan", phone="+92 322 1234567"
        )
        check("a differently-written duplicate is rejected", False, "no error")
    except DuplicateProspectError as exc:
        check("a differently-written duplicate is rejected", exc.existing_id == prospect.id)

    unusable = await service.create_prospect(first_name="Bad", last_name="Number", phone="12345")
    check("an unusable number still stores the person", unusable.id > 0)
    check("but leaves them undialable", unusable.phone_normalized is None)
    check("and marks them UNREACHABLE", unusable.status is ProspectStatus.UNREACHABLE)
    check("so they are not callable", not unusable.is_callable)

    # Several undialable prospects must coexist: the UNIQUE is on the normalised
    # column, and Postgres allows many NULLs there.
    second_bad = await service.create_prospect(first_name="Also", last_name="Bad", phone="nonsense")
    check("two undialable prospects can coexist", second_bad.id != unusable.id)

    print("\n=== campaigns ===")
    campaign = await service.create_campaign("Q1 Outreach", "First quarter")
    check("a campaign is created", campaign.id > 0)
    check("it starts as DRAFT", campaign.status is CampaignStatus.DRAFT)
    check("DRAFT is not dialable", not campaign.status.is_dialable)

    started = await service.set_status(campaign.id, CampaignStatus.ACTIVE)
    check("it can be activated", started.status is CampaignStatus.ACTIVE)
    check("and stamps started_at", started.started_at is not None)
    paused = await service.set_status(campaign.id, CampaignStatus.PAUSED)
    check("pausing stamps paused_at", paused.paused_at is not None)
    resumed = await service.set_status(campaign.id, CampaignStatus.ACTIVE)
    check("resuming keeps the original start time", resumed.started_at == started.started_at)

    print("\n=== campaign membership ===")
    added = await service.add_prospects(campaign.id, [prospect.id])
    check("a prospect joins a campaign", added == 1)
    check("adding twice is a no-op", await service.add_prospects(campaign.id, [prospect.id]) == 0)

    # The reason CampaignProspect exists: one person, two campaigns, no
    # duplicated prospect row and independent state in each.
    other = await service.create_campaign("Q2 Outreach")
    await service.set_status(other.id, CampaignStatus.ACTIVE)
    check(
        "the same prospect joins a second campaign",
        await service.add_prospects(other.id, [prospect.id]) == 1,
    )
    first_membership = await store.find_membership(campaign.id, prospect.id)
    second_membership = await store.find_membership(other.id, prospect.id)
    check("with two separate memberships", first_membership.id != second_membership.id)
    check("and one prospect row", await store.count_prospects() == 3)

    pairs = await store.list_campaign_prospects(campaign.id)
    check("memberships list with their prospects", len(pairs) == 1, str(len(pairs)))
    check("and the ids are not confused", pairs[0][1].id == prospect.id, str(pairs[0][1].id))

    print("\n=== the queue ===")
    queued = await service.next_call(campaign.id)
    check("the queue hands out the eligible prospect", queued is not None)
    check(
        "reserving marks the membership in progress",
        queued.membership.status is MembershipStatus.IN_PROGRESS,
    )
    check("and counts the attempt", queued.membership.attempt_count == 1)
    check("and creates the attempt row", queued.attempt.id > 0)
    check("numbered from one", queued.attempt.attempt_number == 1)

    # The same person must not be handed out twice at once — not even by the
    # other campaign they belong to.
    check(
        "the same campaign will not hand them out again",
        await service.next_call(campaign.id) is None,
    )
    check(
        "and neither will a different campaign",
        await service.next_call(other.id) is None,
    )
    check("because they have a live attempt", await store.has_live_attempt(prospect.id))

    print("\n=== call attempts and retries ===")
    await service.record_outcome(queued.attempt, CallAttemptStatus.NO_ANSWER)
    membership = await store.get_membership(queued.membership.id)
    check("a no-answer returns them to PENDING", membership.status is MembershipStatus.PENDING)
    check("with a retry time in the future", membership.next_attempt_at > datetime.now(UTC))
    check("and the queue respects it", await service.next_call(campaign.id) is None)

    # Bring the retry forward rather than sleeping an hour.
    await store.set_membership_status(
        membership.id,
        MembershipStatus.PENDING,
        next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    second = await service.next_call(campaign.id)
    check("once the retry is due it is handed out", second is not None)
    check("as attempt two", second.attempt.attempt_number == 2, str(second.attempt.attempt_number))

    # max_attempts is 2 for this service, so this exhausts the membership.
    await service.record_outcome(second.attempt, CallAttemptStatus.NO_ANSWER)
    membership = await store.get_membership(second.membership.id)
    check(
        "the attempt limit exhausts the membership", membership.status is MembershipStatus.EXHAUSTED
    )
    check("and the queue offers nothing more", await service.next_call(campaign.id) is None)
    # The queue's SQL and `check_callable` must agree about who is eligible, or
    # a preview command reports work the queue would never hand out.
    exhausted = await service.check_callable(prospect, campaign, membership)
    check("and the check agrees with the queue", not exhausted.allowed, exhausted.reason)

    attempts = await store.list_attempts(prospect_id=prospect.id)
    check("both attempts are recorded", len(attempts) == 2, str(len(attempts)))
    check("a prospect has many attempts", len({a.id for a in attempts}) == 2)

    print("\n=== campaign status gates the queue ===")
    third = await service.create_campaign("Paused Campaign")
    fresh = await service.create_prospect(first_name="Sara", last_name="Ali", phone="0300 1112223")
    await service.add_prospects(third.id, [fresh.id])
    check("a DRAFT campaign yields nothing", await service.next_call(third.id) is None)
    await service.set_status(third.id, CampaignStatus.ACTIVE)
    check("an ACTIVE one does", (await service.next_call(third.id)) is not None)

    live = await store.list_attempts(prospect_id=fresh.id)
    await service.record_outcome(live[0], CallAttemptStatus.COMPLETED, duration_seconds=42)
    await service.set_status(third.id, CampaignStatus.PAUSED)
    check("a PAUSED campaign yields nothing", await service.next_call(third.id) is None)

    completed = await store.get_attempt(live[0].id)
    check("a completed attempt records its duration", completed.duration_seconds == 42)
    check("and stamps ended_at", completed.ended_at is not None)
    reached = await store.get_prospect(fresh.id)
    check("reaching someone marks them CONTACTED", reached.status is ProspectStatus.CONTACTED)


async def check_do_not_call(store: CampaignStore) -> None:
    """The rule that must never fail, checked from every direction."""
    print("\n=== do not call ===")
    service = CampaignService(store, default_region=REGION, max_attempts=3)

    campaign = await service.create_campaign(f"DNC {uuid.uuid4().hex[:6]}")
    await service.set_status(campaign.id, CampaignStatus.ACTIVE)
    prospect = await service.create_prospect(
        first_name="Do", last_name="NotCall", phone="0301 2223334"
    )
    other = await service.create_campaign(f"DNC other {uuid.uuid4().hex[:6]}")
    await service.set_status(other.id, CampaignStatus.ACTIVE)
    await service.add_prospects(campaign.id, [prospect.id])
    await service.add_prospects(other.id, [prospect.id])

    check("callable before being marked", (await service.check_callable(prospect)).allowed)

    await service.mark_do_not_call(prospect.id)
    marked = await store.get_prospect(prospect.id)
    check("the prospect is marked", marked.status is ProspectStatus.DO_NOT_CALL)
    check("and is no longer callable", not marked.is_callable)

    verdict = await service.check_callable(marked)
    check("the check refuses", not verdict.allowed)
    check("and says why", "DO_NOT_CALL" in verdict.reason, verdict.reason)

    # The queue must not offer them, in this campaign or any other.
    check("the queue will not offer them", await service.next_call(campaign.id) is None)
    check("nor in another campaign", await service.next_call(other.id) is None)

    # And their memberships were closed rather than left for the queue to keep
    # picking up and rejecting.
    membership = await store.find_membership(campaign.id, prospect.id)
    check("their memberships are closed", membership.status is MembershipStatus.SKIPPED)

    # Marking someone after they have already been added to a new campaign must
    # still hold: the queue's SQL is the second line of defence.
    late = await service.create_campaign(f"DNC late {uuid.uuid4().hex[:6]}")
    await service.set_status(late.id, CampaignStatus.ACTIVE)
    await service.add_prospects(late.id, [prospect.id])
    check("even added to a fresh campaign afterwards", await service.next_call(late.id) is None)


async def check_dialer(store: CampaignStore) -> None:
    """The dialer's whole path, with a stub carrier and no real calls."""
    print("\n=== dialer (no real calls) ===")
    service = CampaignService(store, default_region=REGION, max_attempts=2, retry_minutes=60)
    campaign = await service.create_campaign(f"Dial {uuid.uuid4().hex[:6]}")
    await service.set_status(campaign.id, CampaignStatus.ACTIVE)
    prospect = await service.create_prospect(
        first_name="Dial", last_name="Target", phone="0302 3334445"
    )
    await service.add_prospects(campaign.id, [prospect.id])

    provider = StubProvider(outcome=CallStatus.COMPLETED)
    dialer = CampaignDialer(
        service,
        provider,
        from_number="+15550001111",
        public_url="https://example.ngrok.app",
    )

    result = await dialer.dial_next(campaign.id)
    check("a call is placed", result.placed, result.describe())
    check("through the provider", len(provider.requests) == 1)
    request = provider.requests[0]
    check("to the normalised number", request.to_number == "+923023334445", request.to_number)
    check("from the configured caller id", request.from_number == "+15550001111")
    check(
        "streaming to the bot",
        request.stream_url == "wss://example.ngrok.app/ws",
        request.stream_url,
    )
    # The ids that let a future conversation layer know who it is talking to.
    check("carrying the prospect id", request.parameters.get("prospect_id") == str(prospect.id))
    check("carrying the campaign id", request.parameters.get("campaign_id") == str(campaign.id))
    check("carrying the attempt id", "call_attempt_id" in request.parameters)

    attempt = await store.get_attempt(result.attempt.id)
    check("the carrier's call id is stored", attempt.telephony_call_id == "CAstub0001")
    check("with the provider name", attempt.telephony_provider == "stub")
    check("and the attempt is QUEUED", attempt.status is CallAttemptStatus.QUEUED)
    check(
        "and can be found by call id",
        (await store.find_attempt_by_call_id("CAstub0001")).id == attempt.id,
    )

    # The carrier is the authority on what happened; refresh writes that back.
    refreshed = await dialer.refresh(attempt)
    check("refreshing records the outcome", refreshed.status is CallAttemptStatus.COMPLETED)
    check(
        "with the carrier's duration",
        refreshed.duration_seconds == 42,
        str(refreshed.duration_seconds),
    )
    membership = await store.find_membership(campaign.id, prospect.id)
    check("and completes the membership", membership.status is MembershipStatus.COMPLETED)

    print("\n  when the carrier refuses:")
    refusing = StubProvider(refuse="Twilio refused the request (HTTP 400, code 21215).")
    other_campaign = await service.create_campaign(f"Refuse {uuid.uuid4().hex[:6]}")
    await service.set_status(other_campaign.id, CampaignStatus.ACTIVE)
    victim = await service.create_prospect(
        first_name="Refused", last_name="Call", phone="0303 4445556"
    )
    await service.add_prospects(other_campaign.id, [victim.id])

    dialer = CampaignDialer(
        service, refusing, from_number="+15550001111", public_url="https://example.ngrok.app"
    )
    failed = await dialer.dial_next(other_campaign.id)
    check("the failure is reported, not raised", not failed.placed and failed.error is not None)
    check("with the carrier's reason", "21215" in failed.error, failed.error or "")

    # The important part: a refused call must not leave the prospect stuck
    # IN_PROGRESS, or they can never be picked up again.
    membership = await store.find_membership(other_campaign.id, victim.id)
    check("the membership is released", membership.status is not MembershipStatus.IN_PROGRESS)
    check("and no live attempt is left behind", not await store.has_live_attempt(victim.id))
    attempts = await store.list_attempts(prospect_id=victim.id)
    check("the failure is recorded as history", attempts[0].status is CallAttemptStatus.FAILED)
    check("with the reason", "21215" in (attempts[0].failure_reason or ""))

    print("\n  a prospect marked DNC between reserving and dialling:")
    race_campaign = await service.create_campaign(f"Race {uuid.uuid4().hex[:6]}")
    await service.set_status(race_campaign.id, CampaignStatus.ACTIVE)
    racer = await service.create_prospect(first_name="Race", last_name="Case", phone="0304 5556667")
    await service.add_prospects(race_campaign.id, [racer.id])

    queued = await service.next_call(race_campaign.id)
    check("the queue reserved them", queued is not None)
    # Somebody marks them while the call is being set up.
    await service.mark_do_not_call(racer.id)
    reloaded = await store.get_prospect(racer.id)
    fresh_queued = type(queued)(
        attempt=queued.attempt,
        prospect=reloaded,
        campaign=queued.campaign,
        membership=queued.membership,
    )
    stub = StubProvider()
    guard = CampaignDialer(
        service, stub, from_number="+15550001111", public_url="https://example.ngrok.app"
    )
    blocked = await guard.dial(fresh_queued)
    check("the dialer refuses to place the call", not blocked.placed)
    check("naming do-not-call", "DO_NOT_CALL" in (blocked.error or ""), blocked.error or "")
    check("and nothing reached the carrier", stub.requests == [])


async def check_import(store: CampaignStore) -> None:
    """Importing a file end to end, including re-importing it."""
    print("\n=== CSV import into the database ===")
    service = CampaignService(store, default_region=REGION, max_attempts=3)
    campaign = await service.create_campaign(f"Import {uuid.uuid4().hex[:6]}")

    text = csv_text(
        "Imported,One,0311 1112221,Alpha",
        "Imported,Two,0311 1112222,Beta",
        "Imported,Three,bad-number,Gamma",
        "Imported,Two,+92 311 1112222,Beta",  # same as row two, written differently
    )

    dry = await service.import_csv(text, campaign_id=campaign.id, dry_run=True)
    check("a dry run writes nothing", dry.created == 0)
    check("but still reports what it found", len(dry.report.valid_rows) == 2, dry.report.summary())
    check("including the in-file duplicate", len(dry.report.invalid_rows) == 2)

    outcome = await service.import_csv(text, campaign_id=campaign.id)
    check("the usable rows are imported", outcome.created == 2, outcome.summary())
    check("and added to the campaign", outcome.added_to_campaign == 2)
    counts = await store.campaign_counts(campaign.id)
    check("the campaign holds them", counts.total == 2, str(counts.total))

    # Re-importing the same list must not create second copies or reset state.
    again = await service.import_csv(text, campaign_id=campaign.id)
    check("re-importing creates nothing", again.created == 0, again.summary())
    check("and reports them as known", again.duplicates == 2, str(again.duplicates))
    check("and adds no memberships", again.added_to_campaign == 0)


# --- Harness ----------------------------------------------------------------


async def with_temp_schema(dsn: str):
    """Create a throwaway schema and point a store at it.

    The store's SQL is all unqualified, so a `search_path` set on every pooled
    connection puts every table it creates inside this schema. That is what
    makes it safe to run these checks against a developer's real database: the
    tables are real, the constraints and the transaction behaviour are real, and
    nothing outside the schema is touched. It is dropped afterwards either way.
    """
    schema = f"campaign_test_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def use_schema(connection: asyncpg.Connection) -> None:
        await connection.execute(f'SET search_path TO "{schema}"')

    # `setup=`, not `init=`. asyncpg runs `init` once when a connection is
    # created, and runs `RESET ALL` when one is *released* back to the pool —
    # which wipes `search_path`. So with `init` the first query works, the
    # connection goes back to the pool, and every query after that looks in
    # `public` and reports that `prospects` does not exist. `setup` runs on
    # every acquire, after the reset, which is what session state needs.
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, setup=use_schema)
    store = CampaignStore(pool)
    await store.create_schema()
    return store, admin, schema


async def check_phase7(store: CampaignStore) -> None:
    """Callbacks and meetings (Phase 7), against real SQL."""
    from zoneinfo import ZoneInfo

    from src.campaigns import CallbackStatus, MeetingStatus

    service = CampaignService(store, default_region=REGION, max_attempts=2, retry_minutes=60)
    karachi = ZoneInfo("Asia/Karachi")
    # Phase 22 found this pinned to 2026-09-07, which passed until the day
    # after it: "the queue will not hand it out before then" compares the
    # callback's time with the real clock. The next Monday from today keeps
    # every assertion below true on any day it is run.
    today = datetime.now(karachi).date()
    next_monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    monday_ten = datetime.combine(next_monday, datetime.min.time(), tzinfo=karachi).replace(hour=10)

    print("\n=== callbacks (Phase 7) ===")
    prospect = await service.create_prospect(first_name="Cal", last_name="Back", phone="+92 322 7000001")
    campaign = await store.create_campaign(name="Phase 7 callbacks", status=CampaignStatus.ACTIVE)
    membership = await store.add_to_campaign(campaign.id, prospect.id)
    await store.set_membership_status(membership.id, MembershipStatus.COMPLETED)

    callback = await store.schedule_callback(
        prospect_id=prospect.id,
        scheduled_for=monday_ten,
        campaign_id=campaign.id,
        campaign_prospect_id=membership.id,
        note="mornings",
    )
    check("a callback is created pending", callback.status is CallbackStatus.PENDING and callback.note == "mornings")
    check("at the moment asked for, timezone-aware", callback.scheduled_for == monday_ten and callback.scheduled_for.tzinfo is not None)
    moved = await store.schedule_callback(prospect_id=prospect.id, scheduled_for=monday_ten + timedelta(days=1))
    check("asking again moves the time rather than adding a row", moved.id == callback.id and moved.scheduled_for == monday_ten + timedelta(days=1))
    check("keeping what was known", moved.note == "mornings" and moved.campaign_prospect_id == membership.id)
    check("one pending callback per prospect", len(await store.list_callbacks(prospect_id=prospect.id)) == 1)
    check("nothing is due yet", not await store.list_callbacks(due_before=datetime(2026, 9, 1, tzinfo=UTC)))
    check("it is due after its time", len(await store.list_callbacks(due_before=monday_ten + timedelta(days=2))) == 1)
    check("it can be read back by id", (await store.get_callback(callback.id)).prospect_id == prospect.id)

    check("a membership can be reopened for the callback", await store.reopen_membership(membership.id, next_attempt_at=moved.scheduled_for))
    reopened = await store.get_membership(membership.id)
    check("as PENDING with that retry time", reopened.status is MembershipStatus.PENDING and reopened.next_attempt_at == moved.scheduled_for)
    ready = await service.check_callable(prospect, campaign, reopened)
    check("and the queue will not hand it out before then", not ready and "not due" in ready.reason, ready.reason)

    placed = await store.set_callbacks_status(prospect.id, CallbackStatus.PLACED)
    check("placing the call marks the callback placed", placed == 1 and (await store.get_callback(callback.id)).status is CallbackStatus.PLACED)
    check("and it is no longer pending", not await store.list_callbacks(prospect_id=prospect.id))
    check("but is listed with every status", len(await store.list_callbacks(prospect_id=prospect.id, status=None)) == 1)

    again = await store.schedule_callback(prospect_id=prospect.id, scheduled_for=monday_ten + timedelta(days=7))
    check("a new callback after a placed one is a new row", again.id != callback.id)
    check("cancelling withdraws it", await store.cancel_callback(again.id) and not await store.cancel_callback(again.id))

    third = await store.schedule_callback(prospect_id=prospect.id, scheduled_for=monday_ten + timedelta(days=8))
    await service.mark_do_not_call(prospect.id)
    check("a do-not-call cancels the pending callback", (await store.get_callback(third.id)).status is CallbackStatus.CANCELLED)
    check("a naive time is refused", await _araises(store.schedule_callback(prospect_id=prospect.id, scheduled_for=datetime(2026, 9, 7, 10, 0)), ValueError))

    print("\n=== meetings (Phase 7) ===")
    attendee = await service.create_prospect(first_name="Meet", last_name="Ing", phone="+92 322 7000002")
    meeting = await store.add_meeting(
        prospect_id=attendee.id,
        campaign_id=campaign.id,
        provider="local",
        start_at=monday_ten,
        end_at=monday_ten + timedelta(minutes=30),
        timezone="Asia/Karachi",
        attendee_name="Meet Ing",
        notes="fuel numbers",
    )
    check("a meeting is recorded as booked", meeting.status is MeetingStatus.BOOKED and meeting.provider == "local")
    check("with its times, timezone-aware", meeting.start_at == monday_ten and meeting.end_at.tzinfo is not None)
    listed = await store.list_meetings(prospect_id=attendee.id)
    check("it lists for the prospect", len(listed) == 1 and listed[0].notes == "fuel numbers")
    check("and from a moment onwards", len(await store.list_meetings(from_time=monday_ten + timedelta(hours=1))) == 0)
    busy = await store.busy_between(monday_ten - timedelta(hours=1), monday_ten + timedelta(minutes=10))
    check("busy_between finds an overlapping meeting", busy == [(monday_ten, monday_ten + timedelta(minutes=30))])
    check("and not an adjacent one", not await store.busy_between(monday_ten + timedelta(minutes=30), monday_ten + timedelta(hours=1)))
    external = await store.add_meeting(
        prospect_id=None, provider="calcom", reference="bk_1", start_at=monday_ten + timedelta(days=1),
        end_at=monday_ten + timedelta(days=1, minutes=30),
    )
    check("an anonymous external booking is recorded too", external.prospect_id is None and external.reference == "bk_1")


async def check_phase8(store: CampaignStore) -> None:
    """Call results (Phase 8), against real SQL: both writers, the upsert rule, the readers."""
    from src.campaigns import (
        CallbackOutcome,
        CallResultValidationError,
        CampaignConversationSink,
        CampaignStoreError,
        Disposition,
        MeetingOutcome,
        ResultSource,
        build_carrier_result,
    )
    from src.conversation import (
        CallBrief,
        CampaignBrief,
        NextAction,
        ProspectBrief,
        QualificationStatus,
        SalesConversation,
    )

    service = CampaignService(store, default_region=REGION, max_attempts=3, retry_minutes=60)
    campaign = await store.create_campaign(name="Phase 8 results", status=CampaignStatus.ACTIVE)
    prospect = await service.create_prospect(first_name="Res", last_name="Ult", phone="+92 322 8000001", company="Meridian")
    membership = await store.add_to_campaign(campaign.id, prospect.id)

    async def attempt():
        return await store.create_attempt(
            prospect_id=prospect.id, campaign_id=campaign.id, campaign_prospect_id=membership.id
        )

    def brief(attempt_id: int) -> CallBrief:
        return CallBrief(
            prospect=ProspectBrief(prospect_id=prospect.id, first_name="Res"),
            campaign=CampaignBrief(agent_name="Alex", company_name="Northwind"),
            campaign_id=campaign.id,
            call_attempt_id=attempt_id,
            source="campaign",
        )

    print("\n=== call results from the carrier (Phase 8) ===")
    unanswered = await attempt()
    await service.record_outcome(unanswered, CallAttemptStatus.NO_ANSWER)
    stored = await store.get_call_result(unanswered.id)
    check("a no-answer writes a result", stored is not None and stored.disposition is Disposition.NO_ANSWER)
    check("from the carrier", stored.source is ResultSource.CARRIER)
    check("with nothing inferred", stored.qualification_status is QualificationStatus.UNKNOWN and stored.human_requested is None)
    check("and a summary", stored.summary.what_happened == "The call was not answered.")

    failed = await attempt()
    await service.record_outcome(failed, CallAttemptStatus.FAILED, failure_reason="number not in service")
    stored = await store.get_call_result(failed.id)
    check("a failed dial writes a result", stored is not None and stored.disposition is Disposition.FAILED)
    check("with the carrier's reason", stored.failure_reason == "number not in service" and "number not in service" in stored.summary.what_happened)

    live = await attempt()
    await service.record_outcome(live, CallAttemptStatus.CONNECTED)
    check("a live status writes no result", await store.get_call_result(live.id) is None)

    print("\n=== call results from the conversation (Phase 8) ===")
    sink = CampaignConversationSink(service)
    talked = await attempt()
    conversation = SalesConversation(brief(talked.id), sink=sink, timezone="Asia/Karachi")
    conversation.note_agent_turn("Hi Res, it's Alex from Northwind.")
    await conversation.note_user_turn("Go on. What is it about?")
    conversation.record_discovery(pain_point="fuel spend", decision_role="decision_maker")
    conversation.set_interest("interested")
    conversation.request_meeting("Thursday")
    conversation.begin_ending("agreed")
    outcome = await conversation.finish(call_duration_secs=61.4)

    stored = await store.get_call_result(talked.id)
    check("the sink writes the result", stored is not None and stored.source is ResultSource.CONVERSATION)
    check("as QUALIFIED with an agreed meeting", stored.disposition is Disposition.QUALIFIED and stored.meeting_status is MeetingOutcome.AGREED)
    check("with the transcript, verbatim", [e["text"] for e in stored.transcript] == ["Hi Res, it's Alex from Northwind.", "Go on. What is it about?"])
    check("and the question", stored.questions == ("What is it about?",))
    check("and the phone call's duration", stored.duration_seconds == 61)
    check("and a summary that is not the transcript", stored.summary.qualification.startswith("Qualified") and "Hi Res" not in stored.summary.text)
    check("and the raw record beside it", (await store.get_attempt(talked.id)).conversation_data["transcript"] == outcome["transcript"])
    listed = await store.list_call_results(campaign_id=campaign.id)
    check("it lists for the campaign, newest first", [r.call_attempt_id for r in listed] == [talked.id, failed.id, unanswered.id])
    check("and filters by disposition", [r.call_attempt_id for r in await store.list_call_results(disposition=Disposition.FAILED, campaign_id=campaign.id)] == [failed.id])
    check("and by prospect", len(await store.list_call_results(prospect_id=prospect.id)) == 3)

    # The carrier's reconciliation runs later and must not flatten it.
    await service.record_outcome(talked, CallAttemptStatus.COMPLETED, duration_seconds=90)
    after = await store.get_call_result(talked.id)
    check("a later carrier report does not overwrite it", after.source is ResultSource.CONVERSATION and after.disposition is Disposition.QUALIFIED and after.duration_seconds == 61)
    check("though the attempt row takes the carrier's duration", (await store.get_attempt(talked.id)).duration_seconds == 90)

    # And in the other order: the carrier first, the conversation second.
    early = await attempt()
    await service.record_outcome(early, CallAttemptStatus.COMPLETED, duration_seconds=30)
    check("a carrier result lands first", (await store.get_call_result(early.id)).source is ResultSource.CARRIER)
    conversation = SalesConversation(brief(early.id), sink=sink)
    conversation.set_interest("not_interested", "happy as they are")
    await conversation.finish()
    later = await store.get_call_result(early.id)
    check("and the conversation's replaces it", later.source is ResultSource.CONVERSATION and later.disposition is Disposition.NOT_INTERESTED)
    check("with the attempt status it implies", (await store.get_attempt(early.id)).status is CallAttemptStatus.NOT_INTERESTED)
    # Four attempts reached a final status; the one left CONNECTED has no result.
    check("one row per finished attempt throughout", len(await store.list_call_results(prospect_id=prospect.id)) == 4)

    print("\n=== call results and the other tables (Phase 8) ===")
    callback_attempt = await attempt()
    conversation = SalesConversation(brief(callback_attempt.id), sink=sink, timezone="Asia/Karachi")
    conversation.request_callback("Tuesday")
    conversation.record.callback_scheduled_for = "2026-09-08T10:00+05:00"
    await conversation.finish()
    stored = await store.get_call_result(callback_attempt.id)
    check("a scheduled callback is SCHEDULED with its time", stored.callback_status is CallbackOutcome.SCHEDULED and stored.callback_scheduled_for == datetime(2026, 9, 8, 5, 0, tzinfo=UTC))
    check("and the disposition follows", stored.disposition is Disposition.CALLBACK_REQUESTED)
    check("and the membership is queued again for it", (await store.get_membership(membership.id)).next_attempt_at == datetime(2026, 9, 8, 5, 0, tzinfo=UTC))

    dnc_attempt = await attempt()
    conversation = SalesConversation(brief(dnc_attempt.id), sink=sink)
    await conversation.note_user_turn("Take me off your list and never call again.")
    await conversation.finish()
    stored = await store.get_call_result(dnc_attempt.id)
    check("a verbal do-not-call is OPTED_OUT (Phase 19), with DO_NOT_CONTACT", stored.disposition is Disposition.OPTED_OUT and stored.next_action is NextAction.DO_NOT_CONTACT)
    check("and the prospect is marked", (await store.get_prospect(prospect.id)).status is ProspectStatus.DO_NOT_CALL)

    print("\n=== call results refused (Phase 8) ===")
    bogus = build_carrier_result(await store.update_attempt_status((await attempt()).id, CallAttemptStatus.NO_ANSWER))
    check("the store refuses an invalid result", await _araises(store.save_call_result(bogus.__class__(**{**bogus.__dict__, "human_requested": False})), CallResultValidationError))
    check("and stores nothing for it", await store.get_call_result(bogus.call_attempt_id) is None)
    orphan = bogus.__class__(**{**bogus.__dict__, "call_attempt_id": 999_999_999})
    check("a result for no attempt is refused", await _araises(store.save_call_result(orphan), CampaignStoreError))
    check("a stored result reads back equal to what was written", (await store.save_call_result(bogus)).to_dict() | {"id": None, "created_at": None, "updated_at": None} == bogus.to_dict())


async def check_phase9(store: CampaignStore) -> None:
    """Duplicate-call protection and event idempotency, against real SQL. Phase 9.

    The in-memory half is `tests/test_reliability.py`. This is the half that can
    only be checked against a database, because what it checks *is* the
    database: a unique index, a transaction, and a status that blocks.
    """
    from src.campaigns import AttemptRecovery, CampaignStoreError
    from src.campaigns.models import may_advance
    from src.reliability import campaign_call_key

    service = CampaignService(store, default_region=REGION, max_attempts=3, retry_minutes=60)
    campaign = await store.create_campaign(name="Phase 9 safety", status=CampaignStatus.ACTIVE)
    prospect = await service.create_prospect(
        first_name="Dupe", last_name="Guard", phone="+92 322 9000001"
    )
    membership = await store.add_to_campaign(campaign.id, prospect.id)

    print("\n=== duplicate call protection (Phase 9) ===")
    first = await service.next_call(campaign.id)
    check("the queue reserves a call", first is not None and first.attempt.id > 0)
    check(
        "stamped with what the call is",
        first.attempt.idempotency_key
        == campaign_call_key(campaign_id=campaign.id, membership_id=membership.id, attempt_number=1),
        str(first.attempt.idempotency_key),
    )
    check("a live attempt blocks the prospect", await store.has_live_attempt(prospect.id))
    check("so the queue hands out nothing more", await service.next_call(campaign.id) is None)

    # The lock is not the only thing stopping a second row: force the same
    # reservation past it and the unique index refuses.
    check(
        "the same attempt cannot be reserved twice, even directly",
        await store.reserve_next_call(
            campaign.id,
            max_attempts=3,
            idempotency_key=lambda m, n: first.attempt.idempotency_key,
        )
        is None,
    )
    check(
        "and no second attempt row exists",
        len(await store.list_attempts(prospect_id=prospect.id)) == 1,
    )
    check(
        "the winning row can be found by its key",
        (await store.find_attempt_by_key(first.attempt.idempotency_key)).id == first.attempt.id,
    )

    print("\n=== placement and ambiguity (Phase 9) ===")
    started = await store.mark_placement_started(first.attempt.id)
    check("placement is stamped before the carrier is asked", started.placement_started_at is not None)
    check("and the attempt reads as CALLING", started.status is CallAttemptStatus.CALLING)

    held = await store.mark_attempt_unresolved(first.attempt.id, "carrier did not answer in 20s")
    check("an ambiguous placement is held UNRESOLVED", held.status is CallAttemptStatus.UNRESOLVED)
    check("which is live", held.status.is_live and not held.status.is_final)
    check("so the prospect is still blocked", await store.has_live_attempt(prospect.id))
    check("and it counts against the concurrency limit", await store.count_live_attempts() >= 1)

    placed = await store.mark_attempt_placed(
        first.attempt.id, telephony_call_id="CAphase9one", provider="stub"
    )
    check("adopting a call id resolves it to QUEUED", placed.status is CallAttemptStatus.QUEUED)
    check(
        "a second, different call id is refused",
        await _araises(
            store.mark_attempt_placed(
                first.attempt.id, telephony_call_id="CAphase9two", provider="stub"
            ),
            CampaignStoreError,
        ),
    )
    check(
        "and the original is untouched",
        (await store.get_attempt(first.attempt.id)).telephony_call_id == "CAphase9one",
    )
    check(
        "writing the same call id again is harmless",
        (
            await store.mark_attempt_placed(
                first.attempt.id, telephony_call_id="CAphase9one", provider="stub"
            )
        ).telephony_call_id
        == "CAphase9one",
    )
    # One carrier call cannot belong to two attempts either.
    other = await store.create_attempt(prospect_id=prospect.id, campaign_id=campaign.id)
    check(
        "one carrier call cannot be recorded against two attempts",
        await _araises(
            store.mark_attempt_placed(other.id, telephony_call_id="CAphase9one", provider="stub"),
            CampaignStoreError,
        ),
    )
    await store.update_attempt_status(other.id, CallAttemptStatus.FAILED)

    print("\n=== duplicate events (Phase 9) ===")
    _, applied_once = await store.apply_call_event(
        attempt_id=first.attempt.id, status=CallAttemptStatus.CONNECTED
    )
    _, applied_twice = await store.apply_call_event(
        attempt_id=first.attempt.id, status=CallAttemptStatus.CONNECTED
    )
    check("the first delivery of an event is applied", applied_once)
    check("a duplicate delivery is not", not applied_twice)

    by_call_id, _ = await store.apply_call_event(
        telephony_call_id="CAphase9one", status=CallAttemptStatus.COMPLETED, duration_seconds=42
    )
    check("a webhook can address an attempt by call id alone", by_call_id.id == first.attempt.id)
    check("and its duration is recorded", by_call_id.duration_seconds == 42)

    _, late = await store.apply_call_event(
        attempt_id=first.attempt.id, status=CallAttemptStatus.CALLING
    )
    check("an out-of-order event does not walk a finished call backwards", not late)
    check(
        "a missing attempt is a no-op, not an error",
        await store.apply_call_event(telephony_call_id="nope", status=CallAttemptStatus.BUSY)
        == (None, False),
    )
    check(
        "the monotonic rule protects a conversation outcome",
        not may_advance(CallAttemptStatus.DO_NOT_CALL, CallAttemptStatus.COMPLETED),
    )

    print("\n=== recovery against the database (Phase 9) ===")
    stale = await store.create_attempt(prospect_id=prospect.id, campaign_id=campaign.id)
    await store.mark_placement_started(stale.id)
    await store.mark_attempt_unresolved(stale.id, "process died mid-placement")
    check("a stale live attempt is listed for recovery", any(a.id == stale.id for a in await store.list_live_attempts(older_than_secs=0)))
    check(
        "and not when it is younger than the floor",
        not any(a.id == stale.id for a in await store.list_live_attempts(older_than_secs=3600)),
    )

    class NoCallsCarrier(StubProvider):
        """A carrier that has no record of any call to this number."""

        async def find_recent_calls(self, to_number, *, since, limit=20):
            return []

    report = await AttemptRecovery(service, NoCallsCarrier(), min_age_secs=0).run()
    resolved = await store.get_attempt(stale.id)
    check("recovery closes it", report.total >= 1 and resolved.status.is_final)
    check("as a failure with the reason on the row", "nothing was dialled" in (resolved.failure_reason or ""))
    check("and the prospect is free again", not await store.has_live_attempt(prospect.id))
    check("no call was placed to find that out", not NoCallsCarrier().requests)


async def _araises(coroutine, exception_type) -> bool:
    try:
        await coroutine
    except exception_type:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


async def run_database_checks(dsn: str) -> None:
    """Run every check that needs real SQL, in a schema that is thrown away."""
    store, admin, schema = await with_temp_schema(dsn)
    try:
        await check_persistence(store)
        await check_do_not_call(store)
        await check_dialer(store)
        await check_import(store)
        await check_phase7(store)
        await check_phase8(store)
        await check_phase9(store)
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def main() -> int:
    """Run every check and report."""
    check_phone_normalization()
    check_csv_mapping()
    check_csv_parsing()

    dsn = os.getenv("DATABASE_URL") or os.getenv("KB_DATABASE_URL")
    if not dsn:
        _skipped.append("database checks (no DATABASE_URL or KB_DATABASE_URL)")
    else:
        try:
            await run_database_checks(dsn)
        except (OSError, asyncpg.PostgresError) as exc:
            _skipped.append(f"database checks (cannot reach PostgreSQL: {exc})")

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
