#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for the outbound calling compliance controls. Phase 19. No keys, no phone, no carrier.

Run it from the `server/` directory::

    uv run python tests/test_compliance.py

**What this is for.** Phase 19 puts one gate in front of every outbound
call and a list behind every opt-out, so the checks are arranged around
the promises the phase makes: *a listed number never rings* (the queue's
SQL, the gate, the importer, the API all refuse it, independently); *an
opt-out is recorded the moment it is heard* (the person's status and the
number's row, from a campaign call or an anonymous one); *the policy for a
call is the operator's, layered* (environment, campaign, jurisdiction —
and the jurisdiction wins); *the agent says what the policy requires
first*; and *every decision leaves a row*.

**The real code, a fake world.** The policy, list and gate modules are
checked directly. The service, the dialer, the briefing and the API are
the real objects over `test_automation.py`'s in-memory store; the dialer's
carrier is a stub that fails the check if it is ever asked to dial. The
SQL — the `dnc_numbers` table, the queue excluding a listed number, the
outlook, the configuration merge — runs against PostgreSQL in a throwaway
schema when one is reachable, and is skipped with a message otherwise.

A plain script rather than a pytest suite, like the other eighteen. Exit
status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from loguru import logger  # noqa: E402
from test_automation import FakeStore, api_settings  # noqa: E402
from test_worker import NOW, FakeClock  # noqa: E402

from src.campaigns import (  # noqa: E402
    CallAttemptStatus,
    CampaignDialer,
    CampaignService,
    CampaignStatus,
    Disposition,
    MembershipStatus,
    ProspectStatus,
    derive_disposition,
)
from src.campaigns.briefing import CampaignConversationSink, CampaignProspectSource  # noqa: E402
from src.compliance import (  # noqa: E402
    CONFIG_KEY,
    ComplianceGate,
    CompliancePolicy,
    Disclosure,
    DncEntry,
    DncSource,
    PolicyResolver,
    RetryDelays,
    Verdict,
    parse_jurisdictions,
    parse_source,
)
from src.conversation import CallBrief, CallIdentifiers, CampaignBrief, ProspectBrief  # noqa: E402
from src.conversation.playbook import build_system_instruction, opening_instruction  # noqa: E402
from src.conversation.states import ConversationState  # noqa: E402
from src.security import AuditLog  # noqa: E402

_failures: list[str] = []
_skipped: list[str] = []
LOGS: list[str] = []

PK_NUMBER = "+923001234567"
PK_OTHER = "+923009998877"
US_NUMBER = "+14155552671"
ADMIN_KEY = "admin-key-0123456789abcdef"
OPERATOR_KEY = "operator-key-0123456789abc"
VIEWER_KEY = "viewer-key-0123456789abcde"


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {label}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        _failures.append(label + (f" — {detail}" if detail else ""))


def base_policy(**overrides: Any) -> CompliancePolicy:
    fields: dict[str, Any] = dict(
        jurisdiction=None,
        calling_hours="09:00-18:00",
        calling_days="mon-fri",
        timezone="UTC",
        enforce_calling_hours=True,
        max_attempts=3,
        retry_minutes=60.0,
        retry=RetryDelays(),
        ai_disclosure=Disclosure("I'm an AI assistant", False),
        recording_enabled=False,
        recording_disclosure=Disclosure("this call may be recorded", False),
        sources=("env",),
    )
    fields.update(overrides)
    return CompliancePolicy(**fields)


# --- The policy -------------------------------------------------------------------------


def check_policy() -> None:
    print("\n=== the policy ===")
    policy = base_policy()
    check("the defaults honour DNC and require no disclosure", policy.honor_dnc and policy.disclosures() == [])
    check("the window is the configured one", policy.window().describe().startswith("09:00-18:00"))
    check("describe() is one ASCII line", "\n" not in policy.describe() and policy.describe().isascii())

    problems: list[str] = []
    overlaid = policy.overlay(
        {
            "calling_hours": "10:00-17:00",
            "max_attempts": 2,
            "retry_minutes_voicemail": 240,
            "ai_disclosure_required": "true",
            "recording_enabled": True,
            "recording_disclosure_required": True,
            "jurisdiction": "PK",
        },
        source="campaign:1",
        problems=problems,
    )
    check("an overlay applies every key", not problems and overlaid.calling_hours == "10:00-17:00" and overlaid.max_attempts == 2 and overlaid.jurisdiction == "PK", "; ".join(problems))
    check("a per-outcome retry overrides the general one", overlaid.retry_minutes_for(CallAttemptStatus.VOICEMAIL) == 240.0 and overlaid.retry_minutes_for(CallAttemptStatus.BUSY) == 60.0)
    check("the disclosures follow, in order", overlaid.disclosures() == ["I'm an AI assistant", "this call may be recorded"])
    check("and the sources say where it came from", overlaid.sources == ("env", "campaign:1"))
    check("the original is untouched", policy.max_attempts == 3 and policy.disclosures() == [])
    check("to_dict() carries every key and the disclosures", overlaid.to_dict()["max_attempts"] == 2 and overlaid.to_dict()["disclosures"] == overlaid.disclosures() and overlaid.to_dict()["honor_dnc"] is True)

    problems = []
    policy.overlay({"calling_hours": "25:00-26:00"}, source="campaign:2", problems=problems)
    check("a window that cannot be read is a problem, not a crash", len(problems) == 1 and "calling window" in problems[0], "; ".join(problems))
    problems = []
    policy.overlay({"timezone": "Mars/Olympus"}, source="campaign:2", problems=problems)
    check("an unknown timezone is a problem", problems and "timezone" in problems[0])
    problems = []
    policy.overlay({"max_attempts": 99}, source="campaign:2", problems=problems)
    check("an attempt ceiling above the cap is a problem", problems and "max_attempts" in problems[0])
    problems = []
    policy.overlay({"surprise": 1}, source="campaign:2", problems=problems)
    check("an unknown key is a problem", problems and "unknown compliance setting" in problems[0])
    problems = []
    policy.overlay({"ai_disclosure_required": "sometimes"}, source="campaign:2", problems=problems)
    check("a non-boolean flag is a problem", problems and "true or false" in problems[0])
    problems = []
    policy.overlay({"ai_disclosure": "x" * 500}, source="campaign:2", problems=problems)
    check("an over-long disclosure is a problem", problems and "at most" in problems[0])
    try:
        policy.overlay({"max_attempts": 0}, source="campaign:2")
        check("with no problem list the overlay raises", False, "did not raise")
    except ValueError:
        check("with no problem list the overlay raises", True)
    check("an empty overlay is the same policy", policy.overlay({}, source="x") is policy and policy.overlay(None, source="x") is policy)

    resolver = PolicyResolver(policy, jurisdictions={"us": {"max_attempts": 1, "calling_hours": "08:00-21:00"}}, default_region="PK")
    check("the region comes from the number", PolicyResolver.region_of(PK_NUMBER) == "PK" and PolicyResolver.region_of(US_NUMBER) == "US" and PolicyResolver.region_of("nonsense") is None)
    for_pk, region = resolver.for_call({CONFIG_KEY: {"max_attempts": 5}}, PK_NUMBER, campaign_id=3)
    check("a campaign's ceiling applies to a number with no jurisdiction rule", for_pk.max_attempts == 5 and region == "PK" and for_pk.sources == ("env", "campaign:3"))
    for_us, region = resolver.for_call({CONFIG_KEY: {"max_attempts": 5}}, US_NUMBER, campaign_id=3)
    check("the jurisdiction is applied last, so a campaign cannot loosen it", for_us.max_attempts == 1 and for_us.calling_hours == "08:00-21:00" and region == "US" and for_us.jurisdiction == "US", str(for_us.sources))
    check("a number with no country code falls back to the default region", resolver.for_call(None, None)[1] == "PK")
    check("for_campaign ignores the jurisdictions", resolver.for_campaign({CONFIG_KEY: {"max_attempts": 5}}, campaign_id=3).max_attempts == 5)
    check("a campaign with no compliance block is the environment's policy", resolver.for_campaign({"offer": "x"}) is policy and resolver.for_campaign(None) is policy)
    problems = []
    quiet = resolver.for_campaign({CONFIG_KEY: {"max_attempts": 99, "retry_minutes": 5}}, campaign_id=4, problems=problems)
    check("a stored setting that does not validate is reported and the rest applied", len(problems) == 1 and quiet.retry_minutes == 5.0 and quiet.max_attempts == 3)

    problems = []
    parsed = parse_jurisdictions('{"US": {"calling_hours": "08:00-21:00", "max_attempts": 3}, "gb": {"ai_disclosure_required": true}}', problems)
    check("COMPLIANCE_JURISDICTIONS parses, upper-casing the codes", not problems and set(parsed) == {"US", "GB"}, "; ".join(problems))
    problems = []
    parse_jurisdictions('{"US": {"calling_hours": "nope"}}', problems)
    check("a bad rule inside is a startup problem", problems and "calling window" in problems[0])
    problems = []
    parse_jurisdictions("not json", problems)
    check("not JSON is a startup problem", problems and "valid JSON" in problems[0])
    problems = []
    parse_jurisdictions('{"United States": {}}', problems)
    check("a key that is not a region code is a startup problem", problems and "region code" in problems[0])
    check("empty is empty", parse_jurisdictions("") == {} and parse_jurisdictions(None) == {})


# --- The list ---------------------------------------------------------------------------


def check_dnc_entries() -> None:
    print("\n=== list entries ===")
    entry = DncEntry(PK_NUMBER, DncSource.VERBAL, reason="asked", created_at=NOW)
    check("an entry is active until revoked", entry.active and entry.is_active_at(NOW + timedelta(days=400)))
    expiring = dataclasses.replace(entry, expires_at=NOW + timedelta(days=30))
    check("an expiry ends it", expiring.is_active_at(NOW + timedelta(days=29)) and not expiring.is_active_at(NOW + timedelta(days=31)))
    revoked = dataclasses.replace(entry, revoked_at=NOW, revoked_by="alice")
    check("a revocation ends it", not revoked.active and not revoked.is_active_at(NOW - timedelta(days=1)))
    check("to_dict() carries the facts", entry.to_dict()["source"] == "verbal" and entry.to_dict()["active"] is True)
    check("sources parse, defaulting for junk", parse_source("API") is DncSource.API and parse_source("nonsense") is DncSource.MANUAL and parse_source(None, DncSource.CLI) is DncSource.CLI)


# --- A world ------------------------------------------------------------------------------


class NeverDial:
    """A carrier stub that fails the check if the dialer ever reaches it."""

    name = "stub"

    def __init__(self) -> None:
        self.calls = 0

    async def place_call(self, request: Any) -> Any:
        self.calls += 1
        raise AssertionError("the dialer reached the carrier for a call the gate should have stopped")

    async def close(self) -> None:
        return None


async def seed(store: FakeStore, service: CampaignService, *, phone: str = PK_NUMBER, configuration: dict[str, Any] | None = None, name: str = "Compliance"):
    prospect = await service.create_prospect(first_name="Hina", last_name="Qureshi", phone=phone)
    campaign = await store.create_campaign(name=name, status=CampaignStatus.ACTIVE, configuration=configuration or {})
    membership = await store.add_to_campaign(campaign.id, prospect.id)
    return prospect, campaign, membership


def make_world(clock: FakeClock, *, jurisdictions: dict[str, dict[str, Any]] | None = None):
    store = FakeStore(clock=clock)
    resolver = PolicyResolver(base_policy(), jurisdictions=jurisdictions or {}, default_region="PK")
    service = CampaignService(store, default_region="PK", max_attempts=3, retry_minutes=60.0, clock=clock, compliance=resolver)
    audit = AuditLog(lambda: store, clock=clock)
    gate = ComplianceGate(service, resolver, audit=audit, clock=clock, actor="dialer")
    return store, service, gate


def audits(store: FakeStore, action: str) -> list[Any]:
    return [e for e in store.audit_entries if e.action == action]


# --- The gate -----------------------------------------------------------------------------


async def check_gate() -> None:
    print("\n=== the gate ===")
    clock = FakeClock(NOW)  # Monday 10:00 UTC: inside 09-18 mon-fri.
    store, service, gate = make_world(clock, jurisdictions={"US": {"max_attempts": 1}})
    prospect, campaign, membership = await seed(store, service)

    decision = await gate.check(prospect, campaign, membership, purpose="check")
    check("a clean prospect on an ACTIVE campaign in the window is allowed", decision.allowed and decision.verdict is Verdict.ALLOW and decision.region == "PK", decision.reason)
    allowed = audits(store, "compliance.allowed")
    check("and the decision is on the audit log with the policy", len(allowed) == 1 and allowed[0].actor == "dialer" and allowed[0].via == "process" and allowed[0].detail["region"] == "PK" and "window" in allowed[0].detail["policy"], str(allowed[0].detail if allowed else None))

    await store.add_dnc(PK_NUMBER, source=DncSource.IMPORT, reason="registry file")
    decision = await gate.check(prospect, campaign, membership)
    check("a listed number is refused as DNC before anything else", decision.refused and decision.verdict is Verdict.DNC and decision.code == "dnc_list" and "registry file" in decision.reason, decision.reason)
    check("with the entry and no retry", decision.entry is not None and decision.retry_after_secs is None)
    blocked = audits(store, "compliance.blocked")
    check("and audited as blocked, naming the source", len(blocked) == 1 and blocked[0].outcome == "dnc_list" and blocked[0].detail["dnc_source"] == "import")
    check("as_decision() is Phase 9's shape", decision.as_decision().refused and decision.as_decision().reason == decision.reason)
    await store.revoke_dnc(PK_NUMBER, revoked_by="alice")
    check("revoked, the number is allowed again", (await gate.check(prospect, campaign, membership)).allowed)

    await store.set_prospect_status(prospect.id, ProspectStatus.DO_NOT_CALL)
    marked = await store.get_prospect(prospect.id)
    decision = await gate.check(marked, campaign, membership)
    check("a prospect marked DO_NOT_CALL is refused as DNC", decision.refused and decision.verdict is Verdict.DNC and decision.code == "dnc_status")
    await store.set_prospect_status(prospect.id, ProspectStatus.NEW)
    fresh = await store.get_prospect(prospect.id)
    membership = await store.get_membership(membership.id)
    await store.set_membership_status(membership.id, MembershipStatus.PENDING)
    membership = await store.get_membership(membership.id)

    # The window, in the policy's zone and in the prospect's own.
    clock.now = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)  # a Sunday, 03:00
    decision = await gate.check(fresh, campaign, membership)
    check("outside the window the call is deferred until it opens", decision.refused and decision.verdict is Verdict.DEFER and decision.code == "window_closed" and decision.retry_after_secs and decision.retry_after_secs > 3600, decision.reason)
    clock.now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
    karachi = dataclasses.replace(fresh, custom_data={"timezone": "Asia/Karachi"})
    clock.now = datetime(2026, 9, 7, 14, 30, tzinfo=UTC)  # 19:30 in Karachi: closed there, open in UTC
    decision = await gate.check(karachi, campaign, membership)
    check("the prospect's own timezone is the one that counts", decision.refused and decision.code == "window_closed" and "for them" in decision.reason, decision.reason)
    clock.now = NOW
    loose = store.campaigns[campaign.id] = dataclasses.replace(campaign, configuration={CONFIG_KEY: {"enforce_calling_hours": False}})
    clock.now = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)
    check("a campaign may switch the window off (a test call at night)", (await gate.check(fresh, loose, membership)).allowed)
    clock.now = NOW
    store.campaigns[campaign.id] = campaign

    # The ceiling: the campaign's, and the jurisdiction's lower one.
    capped = store.campaigns[campaign.id] = dataclasses.replace(campaign, configuration={CONFIG_KEY: {"max_attempts": 1}})
    used = dataclasses.replace(membership, attempt_count=1)
    decision = await gate.check(fresh, capped, used)
    check("a campaign's ceiling exhausts the membership", decision.refused and decision.verdict is Verdict.EXHAUST and decision.code == "attempt_limit" and "1/1" in decision.reason, decision.reason)
    store.campaigns[campaign.id] = campaign
    us_prospect, us_campaign, us_membership = await seed(store, service, phone=US_NUMBER, name="US list")
    us_used = dataclasses.replace(us_membership, attempt_count=1)
    decision = await gate.check(us_prospect, us_campaign, us_used)
    check("a jurisdiction's lower ceiling applies by the number's country", decision.refused and decision.verdict is Verdict.EXHAUST and decision.region == "US" and decision.policy.max_attempts == 1, decision.reason)
    check("under the environment's ceiling the same count is fine for a PK number", (await gate.check(fresh, campaign, dataclasses.replace(membership, attempt_count=1))).allowed)
    check("a callback may waive the ceiling, never the list", (await gate.check(us_prospect, us_campaign, us_used, ignore_attempt_limit=True)).allowed)
    await store.add_dnc(US_NUMBER, source=DncSource.API)
    check("", (await gate.check(us_prospect, us_campaign, us_used, ignore_attempt_limit=True)).verdict is Verdict.DNC)

    # A retry that is not yet due, and a live call.
    waiting = dataclasses.replace(membership, next_attempt_at=NOW + timedelta(minutes=30))
    decision = await gate.check(fresh, campaign, waiting)
    check("a retry not yet due is deferred for exactly that long", decision.refused and decision.verdict is Verdict.DEFER and decision.code == "retry_wait" and 1790 <= (decision.retry_after_secs or 0) <= 1800, str(decision.retry_after_secs))
    paused = store.campaigns[campaign.id] = dataclasses.replace(campaign, status=CampaignStatus.PAUSED)
    decision = await gate.check(fresh, paused, membership)
    check("a campaign that is not ACTIVE releases", decision.refused and decision.verdict is Verdict.RELEASE and decision.code == "campaign_inactive")
    store.campaigns[campaign.id] = campaign
    check("the gate counted every check and every refusal", gate.checked >= 12 and gate.refused >= 8, f"{gate.checked}/{gate.refused}")

    # A store without the list (a database that predates Phase 19).
    class Older(FakeStore):
        async def find_dnc(self, phone_normalized: str):
            from src.campaigns import CampaignStoreError

            raise CampaignStoreError("the dnc_numbers table does not exist")

    old_store = Older(clock=clock)
    old_service = CampaignService(old_store, default_region="PK", clock=clock, compliance=PolicyResolver(base_policy()))
    old_gate = ComplianceGate(old_service, PolicyResolver(base_policy()), clock=clock)
    p, c, m = await seed(old_store, old_service)
    since = len(LOGS)
    check("a store without the list still decides on the prospect's status, and warns once", (await old_gate.check(p, c, m)).allowed and (await old_gate.check(p, c, m)).allowed and sum(1 for line in LOGS[since:] if "compliance.dnc_list_unavailable" in line) == 1)


# --- The service ----------------------------------------------------------------------------


async def check_service() -> None:
    print("\n=== the service ===")
    clock = FakeClock(NOW)
    store, service, gate = make_world(clock)
    prospect, campaign, membership = await seed(store, service)

    await service.mark_do_not_call(prospect.id, source=DncSource.VERBAL, reason="said stop calling", actor="bot", campaign_id=campaign.id, call_attempt_id=7)
    entry = await store.find_dnc(PK_NUMBER)
    check("marking a prospect writes the number onto the list with the facts", entry is not None and entry.source is DncSource.VERBAL and entry.reason == "said stop calling" and entry.prospect_id == prospect.id and entry.call_attempt_id == 7 and entry.created_by == "bot", str(entry))
    check("and the prospect is DO_NOT_CALL with the membership closed", (await store.get_prospect(prospect.id)).status is ProspectStatus.DO_NOT_CALL and (await store.get_membership(membership.id)).status is MembershipStatus.SKIPPED)
    await service.mark_do_not_call(prospect.id, source=DncSource.API, reason="again")
    check("a second request keeps the first record", (await store.find_dnc(PK_NUMBER)).reason == "said stop calling")

    entry, inserted, marked = await service.add_do_not_call_number("0300 9998877", source=DncSource.REGISTRY, reason="national registry", actor="alice")
    check("a bare number is listed, normalised, with no prospect", inserted and entry.phone_normalized == PK_OTHER and marked == 0 and entry.prospect_id is None)
    created = await service.create_prospect(first_name="Sara", last_name="Ali", phone=PK_OTHER)
    check("a prospect created for a listed number is DO_NOT_CALL at birth", created.status is ProspectStatus.DO_NOT_CALL)
    outcome = await service.import_csv("first_name,last_name,phone\nTariq,Khan,+923009998877\nAyesha,Malik,+923001112233\n", campaign_id=campaign.id)
    tariq = next((p for p in store.prospects.values() if p.first_name == "Tariq"), None)
    ayesha = next((p for p in store.prospects.values() if p.first_name == "Ayesha"), None)
    check("an import marks a listed number and leaves the rest NEW", outcome.duplicates == 1 and ayesha is not None and ayesha.status is ProspectStatus.NEW and (await store.get_prospect(created.id)).status is ProspectStatus.DO_NOT_CALL, str(outcome.summary()))
    check("the listed one's new membership is closed", all(m.status is MembershipStatus.SKIPPED for m in store.memberships.values() if m.prospect_id == created.id))
    try:
        await service.add_do_not_call_number("call me maybe")
        check("an unparseable number is refused", False, "listed")
    except ValueError:
        check("an unparseable number is refused", True)

    revoked, reinstated = await service.remove_do_not_call_number(PK_OTHER, actor="alice", reason="wrong number entered")
    check("removing keeps the row, stamped, and leaves the prospects DO_NOT_CALL", revoked is not None and revoked.revoked_by == "alice" and reinstated == 0 and (await store.get_prospect(created.id)).status is ProspectStatus.DO_NOT_CALL)
    check("the number is no longer listed", await service.is_listed(PK_OTHER) is None)
    await service.add_do_not_call_number(PK_OTHER, source=DncSource.MANUAL)
    revoked, reinstated = await service.remove_do_not_call_number(PK_OTHER, actor="alice", reinstate_prospects=True)
    check("with reinstate, the prospects go back to NEW", reinstated == 1 and (await store.get_prospect(created.id)).status is ProspectStatus.NEW)
    check("removing an unlisted number says so", (await service.remove_do_not_call_number(US_NUMBER, actor="alice"))[0] is None)

    # The ceiling and the waits, per campaign and per outcome.
    store2, service2, _ = make_world(clock, jurisdictions={"US": {"retry_minutes_no_answer": 5}})
    p2, c2, m2 = await seed(store2, service2, configuration={CONFIG_KEY: {"max_attempts": 1, "retry_minutes_voicemail": 240}})
    queued = await service2.next_call(c2.id)
    check("the queue hands the first attempt out", queued is not None)
    attempt = await store2.update_attempt_status(queued.attempt.id, CallAttemptStatus.CALLING) or queued.attempt
    await service2.record_outcome(attempt, CallAttemptStatus.VOICEMAIL)
    m2 = await store2.get_membership(m2.id)
    check("at the campaign's ceiling of one, a voicemail exhausts the membership", m2.status is MembershipStatus.EXHAUSTED)
    await store2.update_campaign_configuration(c2.id, CONFIG_KEY, {"max_attempts": 3, "retry_minutes_voicemail": 240})
    await store2.set_membership_status(m2.id, MembershipStatus.PENDING)
    queued = await service2.next_call(c2.id)
    check("raising the campaign's ceiling reopens the queue", queued is not None)
    attempt = await store2.update_attempt_status(queued.attempt.id, CallAttemptStatus.CALLING) or queued.attempt
    await service2.record_outcome(attempt, CallAttemptStatus.VOICEMAIL)
    m2 = await store2.get_membership(m2.id)
    check("a voicemail waits the campaign's voicemail delay", m2.status is MembershipStatus.PENDING and m2.next_attempt_at == NOW + timedelta(minutes=240), str(m2.next_attempt_at))
    # A callback the person asked for, due now: the retry wait is theirs to waive.
    store2.memberships[m2.id] = dataclasses.replace(store2.memberships[m2.id], next_attempt_at=None)
    queued = await service2.reserve_membership(m2.id, ignore_attempt_limit=True)
    check("a callback reservation still applies the campaign's ceiling figure", queued is not None)
    attempt = await store2.update_attempt_status(queued.attempt.id, CallAttemptStatus.CALLING) or queued.attempt
    await service2.record_outcome(attempt, CallAttemptStatus.BUSY)
    m2 = await store2.get_membership(m2.id)
    check("at the ceiling, a busy exhausts even a callback's membership", m2.status is MembershipStatus.EXHAUSTED, str(m2))
    us_p, us_c, us_m = await seed(store2, service2, phone=US_NUMBER, name="US")
    queued = await service2.next_call(us_c.id)
    attempt = await store2.update_attempt_status(queued.attempt.id, CallAttemptStatus.CALLING) or queued.attempt
    await service2.record_outcome(attempt, CallAttemptStatus.NO_ANSWER)
    check("a jurisdiction's wait applies by the number", (await store2.get_membership(us_m.id)).next_attempt_at == NOW + timedelta(minutes=5))

    clock.now = NOW + timedelta(minutes=6)  # the wait is over
    queued = await service2.reserve_membership(us_m.id, ignore_attempt_limit=True)
    attempt = await store2.update_attempt_status(queued.attempt.id, CallAttemptStatus.CONNECTED) or queued.attempt
    await service2.record_outcome(attempt, CallAttemptStatus.DO_NOT_CALL)
    listed = await store2.find_dnc(US_NUMBER)
    check("a DO_NOT_CALL outcome lists the number as a verbal request naming the attempt", listed is not None and listed.source is DncSource.VERBAL and listed.call_attempt_id == attempt.id and listed.campaign_id == us_c.id)
    check("and the carrier result for it is DO_NOT_CALL (no conversation behind it)", (await store2.get_call_result(attempt.id)).disposition is Disposition.DO_NOT_CALL)

    exhausted_queued = await service2.next_call(c2.id)
    check("exhaust() gives the reservation back and closes the membership", exhausted_queued is None or (await service2.exhaust(exhausted_queued, "ceiling")) and (await store2.get_membership(m2.id)).status is MembershipStatus.EXHAUSTED)


# --- The dialer ---------------------------------------------------------------------------


async def check_dialer() -> None:
    print("\n=== the dialer ===")
    clock = FakeClock(NOW)
    store, service, gate = make_world(clock)
    carrier = NeverDial()
    dialer = CampaignDialer(service, carrier, from_number="+920000000000", public_url="https://bot.example.test", gate=gate)  # type: ignore[arg-type]
    prospect, campaign, membership = await seed(store, service)

    queued = await service.next_call(campaign.id)
    await store.add_dnc(PK_NUMBER, source=DncSource.API, reason="added after reserving")
    result = await dialer.dial(queued)
    attempt = await store.get_attempt(queued.attempt.id)
    check("a number listed between reserving and dialling is not dialled", carrier.calls == 0 and result.error and "do-not-call list" in result.error, result.error or "")
    check("the attempt closes as DO_NOT_CALL — a disposition, not a failure", attempt.status is CallAttemptStatus.DO_NOT_CALL and (await store.get_call_result(attempt.id)).disposition is Disposition.DO_NOT_CALL, str(attempt.status))
    check("the prospect is marked and the membership closed", (await store.get_prospect(prospect.id)).status is ProspectStatus.DO_NOT_CALL and (await store.get_membership(membership.id)).status is MembershipStatus.SKIPPED)
    check("the decision is on the audit log", any(e.action == "compliance.blocked" and e.detail.get("attempt") == queued.attempt.id for e in store.audit_entries))
    check("the queue hands nothing more out for them", await service.next_call(campaign.id) is None)

    p2, c2, m2 = await seed(store, service, phone=PK_OTHER, name="Window")
    queued = await service.next_call(c2.id)
    clock.now = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)
    result = await dialer.dial(queued)
    m2 = await store.get_membership(m2.id)
    check("a closed window defers: nothing dialled, the reservation given back", carrier.calls == 0 and result.deferred and result.blocked_by is not None and queued.attempt.id not in store.attempts, result.error or "")
    check("with the membership PENDING again, due when the window opens, its count restored", m2.status is MembershipStatus.PENDING and m2.attempt_count == 0 and m2.next_attempt_at == datetime(2026, 9, 7, 9, 0, tzinfo=UTC), str(m2))
    clock.now = NOW

    store.campaigns[c2.id] = dataclasses.replace(store.campaigns[c2.id], configuration={CONFIG_KEY: {"max_attempts": 1}})
    await store.set_membership_status(m2.id, MembershipStatus.PENDING)
    store.memberships[m2.id] = dataclasses.replace(store.memberships[m2.id], attempt_count=1)
    queued = await service.reserve_membership(m2.id, ignore_attempt_limit=True)
    # The reservation waived the limit (a callback); the gate does not.
    result = await dialer.dial(queued)
    m2 = await store.get_membership(m2.id)
    check("a ceiling the reservation did not apply is applied at the gate: exhausted, not dialled", carrier.calls == 0 and result.deferred and m2.status is MembershipStatus.EXHAUSTED and queued.attempt.id not in store.attempts, str(m2.status))

    plain = CampaignDialer(service, carrier, from_number="+920000000000", public_url="https://bot.example.test")  # type: ignore[arg-type]
    p3, c3, m3 = await seed(store, service, phone="+923005550001", name="No gate")
    queued = await service.next_call(c3.id)
    await store.set_prospect_status(p3.id, ProspectStatus.DO_NOT_CALL)
    result = await plain.dial(queued)
    check("without a gate, Phase 9's check still refuses a do-not-call (nothing was removed)", carrier.calls == 0 and result.error and "DO_NOT_CALL" in result.error, result.error or "")


# --- The briefing: disclosures in, opt-outs out ----------------------------------------------


async def check_briefing() -> None:
    print("\n=== the briefing ===")
    clock = FakeClock(NOW)
    store, service, gate = make_world(clock, jurisdictions={"US": {"ai_disclosure_required": True, "recording_enabled": True, "recording_disclosure_required": True}})
    prospect, campaign, membership = await seed(store, service, configuration={CONFIG_KEY: {"ai_disclosure_required": True, "ai_disclosure": "I am an automated assistant"}})
    source = CampaignProspectSource(service)
    defaults = CampaignBrief(agent_name="Aiva", company_name="Acme")
    brief = await source.load(CallIdentifiers(prospect_id=prospect.id, campaign_id=campaign.id, call_attempt_id=5), defaults)
    check("the brief carries the campaign's disclosure, in the campaign's words", brief is not None and brief.campaign.disclosures == ["I am an automated assistant"], str(brief.campaign.disclosures if brief else None))
    instruction = opening_instruction(brief)
    check("the opening instruction requires it first", "first sentence must include" in instruction and "I am an automated assistant" in instruction and "unless they ask" not in instruction, instruction)
    system = build_system_instruction(brief, knowledge_base=False)
    check("the system instruction lists it as required", "REQUIRED DISCLOSURES" in system and "I am an automated assistant" in system)
    rows = audits(store, "compliance.disclosure")
    check("the instruction is on the audit log", len(rows) == 1 and rows[0].outcome == "instructed" and rows[0].detail["attempt"] == 5 and "I am an automated assistant" in str(rows[0].detail["disclosures"]))

    us_p, us_c, us_m = await seed(store, service, phone=US_NUMBER, name="US")
    us_brief = await source.load(CallIdentifiers(prospect_id=us_p.id, campaign_id=us_c.id, call_attempt_id=6), defaults)
    check("a jurisdiction adds its disclosures by the number", us_brief is not None and us_brief.campaign.disclosures == ["I'm an AI assistant", "this call may be recorded"], str(us_brief.campaign.disclosures if us_brief else None))
    plain_p, plain_c, _ = await seed(store, service, phone="+923005550002", name="Plain")
    plain_brief = await source.load(CallIdentifiers(prospect_id=plain_p.id, campaign_id=plain_c.id, call_attempt_id=7), defaults)
    # Phase 33: with no disclosure required the opening is a short, natural greeting that does
    # NOT force an AI disclosure or a scripted introduction; the AI identity is disclosed
    # truthfully only when asked, which lives in the system instruction and the override, not the
    # opening. A required disclosure still leads the opening (checked above).
    check(
        "with nothing required the opening is natural and does not force a disclosure",
        plain_brief is not None
        and plain_brief.campaign.disclosures == []
        and "first sentence must include" not in opening_instruction(plain_brief)
        and "do not announce that you are an AI" in opening_instruction(plain_brief),
    )
    check(
        "but the honesty rule still discloses when asked",
        "you are an AI assistant" in build_system_instruction(plain_brief, knowledge_base=False),
    )
    check("and audited as none required", any(e.action == "compliance.disclosure" and e.outcome == "none required" for e in store.audit_entries))

    sink = CampaignConversationSink(service)
    anonymous = CallBrief(prospect=ProspectBrief(phone="+923007770001"), source="none")
    stored = await sink.on_do_not_call(anonymous, "said do not call")
    listed = await store.find_dnc("+923007770001")
    check("an anonymous caller's opt-out is recorded by number (the Phase 6 gap, closed)", stored and listed is not None and listed.source is DncSource.VERBAL and listed.reason == "said do not call" and listed.created_by == "bot", str(listed))
    nothing = CallBrief()
    check("with no number at all there is nothing to record, and it says so", not await sink.on_do_not_call(nothing, "asked"))
    stored = await sink.on_do_not_call(brief, "take me off your list")
    listed = await store.find_dnc(PK_NUMBER)
    check("a campaign call's opt-out marks the person and lists the number with the call's ids", stored and (await store.get_prospect(prospect.id)).status is ProspectStatus.DO_NOT_CALL and listed is not None and listed.call_attempt_id == 5 and listed.campaign_id == campaign.id and listed.source is DncSource.VERBAL, str(listed))
    check("and the queue will not hand them out again", await service.next_call(campaign.id) is None)


# --- Dispositions ---------------------------------------------------------------------------


def check_dispositions() -> None:
    print("\n=== dispositions ===")
    check("a verbal request is OPTED_OUT", derive_disposition(CallAttemptStatus.DO_NOT_CALL, final_state=ConversationState.DO_NOT_CALL) is Disposition.OPTED_OUT)
    check("heard, then a goodbye, is OPTED_OUT", derive_disposition(CallAttemptStatus.DO_NOT_CALL, final_state=ConversationState.ENDING) is Disposition.OPTED_OUT)
    check("a list refusal with no conversation is DO_NOT_CALL", derive_disposition(CallAttemptStatus.DO_NOT_CALL) is Disposition.DO_NOT_CALL)
    check("no answer, busy, voicemail and failed keep their words", [derive_disposition(s) for s in (CallAttemptStatus.NO_ANSWER, CallAttemptStatus.BUSY, CallAttemptStatus.VOICEMAIL, CallAttemptStatus.FAILED)] == [Disposition.NO_ANSWER, Disposition.BUSY, Disposition.VOICEMAIL, Disposition.FAILED])
    check("a completed call with nothing recorded is COMPLETED", derive_disposition(CallAttemptStatus.COMPLETED) is Disposition.COMPLETED)
    check("the enum names both", Disposition("OPTED_OUT") and Disposition("DO_NOT_CALL"))


# --- The API ---------------------------------------------------------------------------------


def check_api() -> None:
    print("\n=== the API ===")
    from fastapi.testclient import TestClient

    from src.automation import API_PREFIX, create_automation_app

    clock = FakeClock(NOW)
    store = FakeStore(clock=clock)
    resolver = PolicyResolver(base_policy(), jurisdictions={"US": {"max_attempts": 1}}, default_region="PK")
    settings = dataclasses.replace(
        api_settings(api_keys=(ADMIN_KEY,), operator_api_keys=(OPERATOR_KEY,), viewer_api_keys=(VIEWER_KEY,)),
        compliance=resolver,
    )

    async def factory() -> Any:
        return store

    app = create_automation_app(settings, store_factory=factory, deliver=False, clock=clock)
    admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
    operator = {"Authorization": f"Bearer {OPERATOR_KEY}"}
    viewer = {"X-API-Key": VIEWER_KEY}
    v = API_PREFIX
    with TestClient(app) as client:
        made = client.post(f"{v}/prospects", json={"first_name": "Hina", "last_name": "Qureshi", "phone": PK_NUMBER}, headers=operator)
        pid = made.json()["prospect"]["id"]
        camp = client.post(f"{v}/campaigns", json={"name": "Compliance"}, headers=operator).json()["campaign"]
        client.post(f"{v}/campaigns/{camp['id']}/start", headers=operator)

        listed = client.post(f"{v}/dnc", json={"phone": "0300 1234567", "reason": "asked by email"}, headers=operator)
        check("an operator lists a number, normalised, and the prospects with it are marked", listed.status_code == 201 and listed.json()["dnc"]["phone_normalized"] == PK_NUMBER and listed.json()["dnc"]["source"] == "api" and listed.json()["prospects_marked"] == 1, listed.text[:200])
        check("the actor is the key's label, never the key", listed.json()["dnc"]["created_by"] == "operator-key#1")
        check("listing twice is 200 with the first record", client.post(f"{v}/dnc", json={"phone": PK_NUMBER, "reason": "again"}, headers=operator).status_code == 200)
        check("a viewer cannot list a number", client.post(f"{v}/dnc", json={"phone": PK_OTHER}, headers=viewer).status_code == 403)
        check("and audited", any(e.action == "compliance.dnc_added" and e.actor == "operator-key#1" for e in store.audit_entries))
        check("the prospect now reads DO_NOT_CALL", client.get(f"{v}/prospects/{pid}", headers=operator).json()["prospect"]["status"] == "DO_NOT_CALL")

        queued = client.post(f"{v}/calls", json={"prospect_id": pid, "campaign_id": camp["id"]}, headers=operator)
        check("a call for a listed number is refused as do_not_call", queued.status_code == 409 and queued.json()["error"]["code"] == "do_not_call", queued.text[:200])

        seen = client.get(f"{v}/dnc/check", params={"phone": PK_NUMBER}, headers=viewer)
        check("anybody may ask whether a number is blocked, and learns only that", seen.status_code == 200 and seen.json()["blocked"] is True and seen.json()["on_list"] is True and seen.json()["source"] == "api")
        check("an unlisted number is not blocked", client.get(f"{v}/dnc/check", params={"phone": US_NUMBER}, headers=viewer).json()["blocked"] is False)
        check("the list itself needs read_pii", client.get(f"{v}/dnc", headers=viewer).status_code == 403)
        rows = client.get(f"{v}/dnc", headers=operator)
        check("an operator reads it with counts", rows.status_code == 200 and rows.json()["count"] == 1 and rows.json()["counts"]["active"] == 1 and rows.json()["counts"]["api"] == 1, rows.text[:200])
        check("removing needs manage", client.delete(f"{v}/dnc/{PK_NUMBER}", headers=operator).status_code == 403)
        gone = client.request("DELETE", f"{v}/dnc/{PK_NUMBER}", json={"reason": "entered in error", "reinstate_prospects": True}, headers=admin)
        check("an admin removes it, reinstating the prospects when asked", gone.status_code == 200 and gone.json()["prospects_reinstated"] == 1 and gone.json()["dnc"]["revoked_by"] == "admin-key#1", gone.text[:200])
        check("removing an unlisted number is 404", client.delete(f"{v}/dnc/{PK_NUMBER}", headers=admin).status_code == 404)
        check("the prospect is NEW again", client.get(f"{v}/prospects/{pid}", headers=operator).json()["prospect"]["status"] == "NEW")

        dnc = client.post(f"{v}/prospects/{pid}/do-not-call", params={"reason": "asked by letter"}, headers=operator)
        check("do-not-call on a prospect lists the number as an API request", dnc.status_code == 200 and dnc.json()["changed"] is True and dnc.json()["dnc"]["source"] == "api" and dnc.json()["dnc"]["reason"] == "asked by letter", dnc.text[:200])

        got = client.get(f"{v}/campaigns/{camp['id']}/compliance", headers=viewer)
        check("a viewer reads a campaign's compliance settings and the effective policy", got.status_code == 200 and got.json()["settings"] == {} and got.json()["policy"]["max_attempts"] == 3 and "US" in got.json()["jurisdictions"], got.text[:300])
        bad = client.put(f"{v}/campaigns/{camp['id']}/compliance", json={"calling_hours": "25:00-26:00"}, headers=operator)
        check("settings that do not validate are 422 with the problems, and nothing is written", bad.status_code == 422 and bad.json()["error"]["details"]["problems"] and client.get(f"{v}/campaigns/{camp['id']}/compliance", headers=operator).json()["settings"] == {}, bad.text[:200])
        check("an unknown key is refused", client.put(f"{v}/campaigns/{camp['id']}/compliance", json={"surprise": 1}, headers=operator).status_code == 422)
        put = client.put(f"{v}/campaigns/{camp['id']}/compliance", json={"max_attempts": 2, "calling_hours": "10:00-16:00", "ai_disclosure_required": True}, headers=operator)
        check("good settings are saved and the policy reflects them", put.status_code == 200 and put.json()["policy"]["max_attempts"] == 2 and put.json()["policy"]["calling_hours"] == "10:00-16:00" and put.json()["policy"]["disclosures"] == ["I'm an AI assistant"], put.text[:300])
        check("and the sales settings on the campaign survive", store.campaigns[camp["id"]].configuration.get(CONFIG_KEY, {}).get("max_attempts") == 2)
        check("a viewer may not change them", client.put(f"{v}/campaigns/{camp['id']}/compliance", json={"max_attempts": 1}, headers=viewer).status_code == 403)
        check("and the change is audited", any(e.action == "campaign.compliance_updated" for e in store.audit_entries))


# --- The boundary ---------------------------------------------------------------------------


def check_boundary() -> None:
    print("\n=== the boundary ===")
    conversation = SERVER / "src" / "conversation"
    offenders = [str(p.relative_to(SERVER)) for p in conversation.rglob("*.py") if "compliance" in p.read_text(encoding="utf-8") and "import" in p.read_text(encoding="utf-8") and ("from ..compliance" in p.read_text(encoding="utf-8") or "src.compliance" in p.read_text(encoding="utf-8"))]
    check("the conversation layer never imports the compliance package (it gets words on the brief)", not offenders, ", ".join(offenders))
    bot = (SERVER / "bot.py").read_text(encoding="utf-8")
    check("bot.py reaches compliance only through Config (the policy resolver on the briefing)", "from src.compliance" not in bot and "policy_resolver()" in bot)
    for name in ("policy.py", "dnc.py"):
        text = (SERVER / "src" / "compliance" / name).read_text(encoding="utf-8")
        check(f"compliance/{name} imports nothing from campaigns or reliability at module level", "from ..campaigns" not in text and "\nfrom ..reliability" not in text)
    doc = (SERVER.parent / "COMPLIANCE.md").read_text(encoding="utf-8") if (SERVER.parent / "COMPLIANCE.md").exists() else ""
    check("COMPLIANCE.md exists and says the software does not make the operator compliant", "not legal advice" in doc.lower() and "operator" in doc.lower())


# --- SQL ---------------------------------------------------------------------------------------


async def run_database_checks(dsn: str) -> None:
    print("\n=== the rows (PostgreSQL) ===")
    from test_campaigns import with_temp_schema

    store, admin, schema = await with_temp_schema(dsn)
    try:
        resolver = PolicyResolver(base_policy(), default_region="PK")
        service = CampaignService(store, default_region="PK", compliance=resolver)
        prospect = await service.create_prospect(first_name="Hina", last_name="Qureshi", phone=PK_NUMBER)
        campaign = await service.create_campaign("SQL compliance")
        await store.set_campaign_status(campaign.id, CampaignStatus.ACTIVE)
        await store.add_to_campaign(campaign.id, prospect.id)

        entry, inserted = await store.add_dnc(PK_NUMBER, source=DncSource.REGISTRY, reason="registry", created_by="alice")
        check("a row lands, with its facts", inserted and entry.id is not None and entry.source is DncSource.REGISTRY and entry.created_at is not None)
        again, inserted = await store.add_dnc(PK_NUMBER, source=DncSource.API)
        check("the partial unique index keeps one active row per number", not inserted and again.id == entry.id)
        check("find_dnc reads it", (await store.find_dnc(PK_NUMBER)).id == entry.id and await store.find_dnc(US_NUMBER) is None)
        check("the queue hands a listed number out to nobody", await store.reserve_next_call(campaign.id, max_attempts=3) is None)
        outlook = await store.queue_outlook(campaign.id, max_attempts=3)
        check("the outlook counts it as undialable, not due", outlook.due_now == 0 and outlook.undialable == 1, str(outlook))
        check("apply_dnc_list marks the prospect", await store.apply_dnc_list([prospect.id]) == 1 and (await store.get_prospect(prospect.id)).status is ProspectStatus.DO_NOT_CALL)
        check("and again is nothing", await store.apply_dnc_list(None) == 0)
        counts = await store.dnc_counts()
        check("counts per source", counts["active"] == 1 and counts["registry"] == 1 and counts["revoked"] == 0)
        revoked = await store.revoke_dnc(PK_NUMBER, revoked_by="alice", reason="mistake")
        check("revoking stamps the row and keeps it", revoked is not None and revoked.revoked_by == "alice" and await store.find_dnc(PK_NUMBER) is None and len(await store.list_dnc(include_revoked=True)) == 1 and len(await store.list_dnc()) == 0)
        await store.set_prospect_status(prospect.id, ProspectStatus.NEW)
        await store.set_membership_status((await store.list_campaign_prospects(campaign.id))[0][0].id, MembershipStatus.PENDING)
        queued = await store.reserve_next_call(campaign.id, max_attempts=3)
        check("revoked, the queue hands them out again", queued is not None)
        second, inserted = await store.add_dnc(PK_NUMBER, source=DncSource.VERBAL, expires_at=datetime.now(UTC) - timedelta(seconds=1))
        check("an expired entry is inserted but not found", inserted and await store.find_dnc(PK_NUMBER) is None)
        third, inserted = await store.add_dnc(PK_NUMBER, source=DncSource.VERBAL)
        check("a fresh one replaces the expired one", inserted and third.id != second.id and (await store.find_dnc(PK_NUMBER)).id == third.id)

        updated = await store.update_campaign_configuration(campaign.id, CONFIG_KEY, {"max_attempts": 2})
        await store.update_campaign_configuration(campaign.id, "offer", "widgets")
        merged = await store.get_campaign(campaign.id)
        check("the configuration merges per key", merged.configuration == {CONFIG_KEY: {"max_attempts": 2}, "offer": "widgets"}, str(merged.configuration))
        removed = await store.update_campaign_configuration(campaign.id, CONFIG_KEY, None)
        check("and a None removes one", removed.configuration == {"offer": "widgets"})
        check("a missing campaign is None", await store.update_campaign_configuration(999999, "x", 1) is None)
        check("prospects_with_number finds every row with the number", [p.id for p in await store.prospects_with_number(PK_NUMBER)] == [prospect.id])
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def main() -> int:
    """Run every check and report."""
    print("Compliance checks — the policy and its layers, the list, the gate, the service, the dialer, the briefing, dispositions, the API, the boundary, and the rows.")
    handler = logger.add(LOGS.append, format="{message}", level="DEBUG")
    try:
        check_policy()
        check_dnc_entries()
        await check_gate()
        await check_service()
        await check_dialer()
        await check_briefing()
        check_dispositions()
        check_api()
        check_boundary()

        from dotenv import load_dotenv

        load_dotenv(override=True)
        dsn = os.getenv("DATABASE_URL") or os.getenv("KB_DATABASE_URL")
        if not dsn:
            _skipped.append("database checks (no DATABASE_URL or KB_DATABASE_URL)")
        else:
            import asyncpg

            try:
                await run_database_checks(dsn)
            except (
                OSError,
                asyncpg.exceptions.PostgresConnectionError,
                asyncpg.exceptions.InvalidAuthorizationSpecificationError,
                asyncpg.exceptions.InvalidCatalogNameError,
            ) as exc:
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
