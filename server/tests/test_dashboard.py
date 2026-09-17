#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for the dashboard (Phase 10). No vendors, no phone, no audio.

Run it from the `server/` directory::

    uv run python tests/test_dashboard.py

**Three layers, and each is checked where it can actually be wrong.**

* *The aggregates* are SQL, so they run against a real PostgreSQL in a
  throwaway schema — the same arrangement `test_campaigns.py` uses, and for the
  same reason: a stub store would only prove the stub agrees with itself, and
  what is being checked here is whether `count(*) FILTER (...)` counts the
  right rows. Skipped with a message when no database is reachable.
* *The shaping* is pure, so it runs against fixed inputs. This is where the
  claims live — that "answered" and "completed" are different numbers, that an
  average carries the count it averages, and that a missing table is reported
  as unavailable rather than as zero.
* *The routes* run through FastAPI's test client, which exercises the real
  application: the real page, the real JSON, and the real absence of any way to
  write.

The dashboard reads and never writes, so there is no failure here that can
damage anything — which is exactly why the checks concentrate on it being
*honest* rather than on it being safe.

A plain script rather than a pytest suite because the project has no test
dependency and this needs none. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from src.campaigns import (  # noqa: E402
    CallAttemptStatus,
    CampaignService,
    CampaignStatus,
    CampaignStore,
    MembershipStatus,
)
from src.dashboard import collect, render_page  # noqa: E402
from src.dashboard.stats import _duration, _humanise, _tone_for, _totals  # noqa: E402
from src.dashboard.web import API_PATH  # noqa: E402

load_dotenv(override=True)

REGION = "PK"
_failures: list[str] = []
_skipped: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def metric(metrics: list[Any], key: str) -> Any:
    """One metric out of a list, by key."""
    return next(m for m in metrics if m.key == key)


def metric_value(metrics: list[dict[str, Any]], key: str) -> Any:
    """One metric's value out of the JSON, by key."""
    return next(m["value"] for m in metrics if m["key"] == key)


# --- The shaping, against fixed inputs ---------------------------------------


def check_formatting() -> None:
    """Durations, labels and tones."""
    print("\n=== formatting ===")
    check("seconds render", _duration(45) == "45s")
    check("minutes render", _duration(120) == "2m")
    check("both render", _duration(135) == "2m 15s")
    check("a fraction rounds", _duration(74.6) == "1m 15s")
    check("zero is not nothing", _duration(0) == "0s")
    check("nothing is None", _duration(None) is None)

    check("a disposition reads as English", _humanise("MEETING_BOOKED") == "Meeting booked")
    check("an unknown value still reads", _humanise(None) == "Unknown")
    check("good outcomes are green", _tone_for("MEETING_BOOKED") == "good")
    check("bad ones are red", _tone_for("FAILED") == "bad" and _tone_for("DO_NOT_CALL") == "bad")
    check("the rest are neutral", _tone_for("COMPLETED") == "neutral")


def check_metrics() -> None:
    """The tiles: what each one counts, and what it says about itself."""
    print("\n=== the headline numbers ===")
    prospects = {"total": 120, "do_not_call": 4, "unreachable": 6, "callable": 110}
    attempts = {
        "total": 90, "answered": 40, "completed": 31, "failed": 12, "no_answer": 25,
        "busy": 13, "do_not_call": 3, "not_interested": 5, "callback_requested": 1,
        "live": 2, "unresolved": 1, "with_duration": 40,
        "average_duration_secs": 132.0, "total_duration_secs": 5280,
    }
    results = {
        "total": 88, "qualified_prospects": 7, "qualified_calls": 9,
        "partially_qualified": 20, "disqualified": 15, "meetings_booked": 5,
        "callbacks_scheduled": 3, "transferred": 1, "human_requested": 2,
    }
    meetings = {"booked": 5, "upcoming": 4, "unattributed": 1, "cancelled": 0}
    tiles = _totals(prospects, attempts, results, meetings)

    check("every number the phase asked for is a tile", [m.key for m in tiles] == [
        "contacts", "calls", "answered", "completed", "failed",
        "average_duration", "qualified", "meetings",
    ], str([m.key for m in tiles]))

    check("contacts count people", metric(tiles, "contacts").value == 120)
    check("and say how many are callable", "110 callable" in metric(tiles, "contacts").detail)
    check("calls count every dial", metric(tiles, "calls").value == 90)

    # The claim this dashboard could most easily get wrong.
    answered, completed = metric(tiles, "answered"), metric(tiles, "completed")
    check("answered and completed are different numbers", answered.value == 40 and completed.value == 31)
    check("and answered says what it includes", "do-not-call" in answered.detail)
    check("and completed says it overlaps", "overlaps with answered" in completed.detail)

    failed = metric(tiles, "failed")
    check("failed counts only failures", failed.value == 12)
    check("with no-answer and busy named separately", "25 no answer" in failed.detail and "13 busy" in failed.detail)

    average = metric(tiles, "average_duration")
    check("the average is formatted", average.value == "2m 12s")
    check("and carries the count it averages", "over the 40 calls" in average.detail, average.detail)

    check("qualified counts people, not calls", metric(tiles, "qualified").value == 7)
    check("meetings come from the calendar", metric(tiles, "meetings").value == 5)
    check("and explain a booking with no campaign", "not tied to a campaign" in metric(tiles, "meetings").detail)


def check_unavailable_is_not_zero() -> None:
    """A missing table must never be reported as a zero."""
    print("\n=== unavailable is not zero ===")
    prospects = {"total": 10, "do_not_call": 0, "unreachable": 0, "callable": 10}
    attempts = {
        "total": 0, "answered": 0, "completed": 0, "failed": 0, "no_answer": 0, "busy": 0,
        "do_not_call": 0, "not_interested": 0, "callback_requested": 0, "live": 0,
        "unresolved": 0, "with_duration": 0, "average_duration_secs": None,
        "total_duration_secs": 0,
    }
    tiles = _totals(prospects, attempts, None, None)

    qualified, meetings = metric(tiles, "qualified"), metric(tiles, "meetings")
    check("qualified is unavailable, not 0", not qualified.available and qualified.value is None)
    check("meetings is unavailable, not 0", not meetings.available and meetings.value is None)
    check("and each says what is missing", "call_results" in qualified.detail and "meetings table" in meetings.detail)
    check("the JSON reports null, not a number", qualified.to_dict()["value"] is None)
    check("a real zero stays a zero", metric(tiles, "calls").value == 0 and metric(tiles, "calls").available)

    average = metric(tiles, "average_duration")
    check("no durations reads as unavailable", not average.available)
    check("and says so rather than showing 0s", "no call has a recorded duration" in average.detail)


def check_page() -> None:
    """The document: one renderer, no network, nothing unfilled."""
    print("\n=== the page ===")
    html = render_page(api_path=API_PATH, refresh_secs=15)
    check("it is a complete document", html.startswith("<!doctype html>") and html.rstrip().endswith("</html>"))
    check("it points at its own API", f'const API = "{API_PATH}"' in html)
    check("with the refresh it was given", "REFRESH_MS = 15 * 1000" in html)
    check("every container the script writes to exists", all(
        f'id="{name}"' in html
        for name in ("totals", "attention", "outcomes", "campaigns", "recent", "notes", "stamp", "zone")
    ))
    check("it escapes what it renders", "const esc" in html)
    check("it fetches nothing off this machine", "https://" not in html and "cdn" not in html.lower())
    check("it says what to do without JavaScript", "<noscript>" in html and "campaign.py" in html)
    # `str.format` would raise on an unknown field, so reaching here means every
    # placeholder was filled; this catches the opposite mistake — a stray `{}`
    # in the CSS that got eaten rather than escaped.
    check("no CSS was swallowed by the formatter", "grid-template-columns" in html and "@media" in html)


def check_analytics() -> None:
    """Phase 20's strips, against fixed inputs: every rate carries its denominator."""
    print("\n=== conversion, performance, errors, compliance, progress ===")
    from src.dashboard.stats import (
        ReportFilter,
        _compliance,
        _conversion,
        _errors,
        _performance,
        _progress,
    )

    attempts = {
        "total": 100, "answered": 40, "completed": 30, "failed": 10, "no_answer": 30, "busy": 5,
        "voicemail": 15, "do_not_call": 2, "not_interested": 6, "callback_requested": 3,
        "live": 1, "unresolved": 1, "with_duration": 40, "average_duration_secs": 150.0,
        "total_duration_secs": 6000, "with_failure_reason": 7, "refused_before_dial": 4, "unreached": 60,
        "usage_available": True, "with_usage": 20, "prompt_tokens": 60000, "completion_tokens": 4000,
        "llm_requests": 200, "with_cost": 20, "total_cost_usd": 1.5, "average_cost_usd": 0.075,
        "quality_available": True, "with_latency": 18, "response_p50_ms": 1850, "response_p95_ms": 3200,
        "greeting_ms": 4100, "with_quality": 20, "failed_turns": 3, "late_turns": 5, "barge_ins": 9,
        "calls_with_errors": 2,
    }
    results = {
        "total": 40, "qualified_prospects": 8, "qualified_calls": 10, "partially_qualified": 12,
        "disqualified": 6, "meetings_booked": 4, "meetings_agreed": 2, "callbacks_scheduled": 3,
        "callbacks_requested": 2, "transferred": 3, "human_requested": 5, "opted_out": 2,
        "do_not_call": 1, "not_interested": 6, "answered": 40,
    }
    meetings = {"booked": 4, "upcoming": 3, "unattributed": 0, "cancelled": 1}
    callbacks = {"pending": 4, "due": 1, "placed": 2, "cancelled": 0}

    conversion = _conversion(attempts, results, meetings, callbacks)
    check("conversion is five rates", [m.key for m in conversion] == ["qualification_rate", "meeting_rate", "callback_rate", "transfer_rate", "not_interested_rate"], str([m.key for m in conversion]))
    check("qualified is over answered calls", metric(conversion, "qualification_rate").value == "25%" and "of 40 answered" in metric(conversion, "qualification_rate").detail)
    check("meetings booked over answered, with the agreed-not-booked named", metric(conversion, "meeting_rate").value == "10%" and "2 agreed but not booked" in metric(conversion, "meeting_rate").detail)
    check("callbacks count requested and scheduled", metric(conversion, "callback_rate").value == "12.5%" and "3 at a chosen time" in metric(conversion, "callback_rate").detail)
    check("transfers over answered", metric(conversion, "transfer_rate").value == "7.5%")
    check("with no results the strip is unavailable, not zero", not _conversion(attempts, None, meetings, callbacks)[0].available)
    nobody = dict(attempts, answered=0)
    check("with nobody answered the rates are unavailable", not metric(_conversion(nobody, results, meetings, callbacks), "qualification_rate").available)

    performance = _performance(attempts, results)
    check("answer rate over every call", metric(performance, "answer_rate").value == "40%" and "40 answered of 100" in metric(performance, "answer_rate").detail)
    check("voicemail rate over every call", metric(performance, "voicemail_rate").value == "15%")
    check("human-transfer rate over answered calls", metric(performance, "human_transfer_rate").value == "7.5%" and "3 of 40" in metric(performance, "human_transfer_rate").detail)
    check("average duration with its count and the total", metric(performance, "average_duration").value == "2m 30s" and "40 calls" in metric(performance, "average_duration").detail and "1h" not in metric(performance, "average_duration").detail)
    latency = metric(performance, "response_latency")
    check("response latency is the average per-call median, in seconds, with p95 and the greeting", latency.value == "1.9s" and "18 measured calls" in latency.detail and "p95 3.2s" in latency.detail and "greeting 4.1s" in latency.detail, latency.detail)
    check("without the usage column latency is unavailable and says why", not metric(_performance(dict(attempts, quality_available=False), results), "response_latency").available)
    check("with no measured call it is unavailable", not metric(_performance(dict(attempts, with_latency=0, response_p50_ms=None), results), "response_latency").available)

    errors = _errors(attempts)
    check("failed calls carry the rate, the refusals and the reasons", metric(errors, "failed_calls").value == 10 and "10% of 100" in metric(errors, "failed_calls").detail and "4 refused before dialling" in metric(errors, "failed_calls").detail)
    check("unresolved is its own tile", metric(errors, "unresolved").value == 1 and "recover" in metric(errors, "unresolved").detail)
    check("unreached breaks down", metric(errors, "unreached").value == 60 and "15 voicemail" in metric(errors, "unreached").detail)
    check("failed turns and service errors come from the quality summary", metric(errors, "failed_turns").value == 3 and metric(errors, "calls_with_errors").value == 2)
    check("without the column the turn figures are absent, not zero", [m.key for m in _errors(dict(attempts, quality_available=False))] == ["failed_calls", "unresolved", "unreached"])

    prospects = {"total": 120, "do_not_call": 4, "unreachable": 6, "callable": 110}
    dnc = {"active": 5, "revoked": 1, "verbal": 2, "api": 1, "import": 2}
    blocked = {"blocked": 3, "allowed": 50, "dnc_list": 2, "window_closed": 1}
    compliance = _compliance(prospects, results, dnc, blocked)
    check("do-not-call prospects, the list by source, the opt-outs and the refused dials", [m.key for m in compliance] == ["dnc_prospects", "dnc_list", "opted_out", "blocked_dials"])
    check("the list tile names each source and the revoked", metric(compliance, "dnc_list").value == 5 and "2 verbal" in metric(compliance, "dnc_list").detail and "1 revoked" in metric(compliance, "dnc_list").detail)
    check("opted out is told apart from refused by the list", metric(compliance, "opted_out").value == 2 and "1 dial refused" in metric(compliance, "opted_out").detail)
    check("the gate's decisions are counted by code", metric(compliance, "blocked_dials").value == 3 and "50 allowed" in metric(compliance, "blocked_dials").detail and "2 dnc list" in metric(compliance, "blocked_dials").detail)
    check("without the list table it is unavailable", not metric(_compliance(prospects, results, None, blocked), "dnc_list").available)

    campaigns = [
        {"prospects": 50, "pending": 10, "in_progress": 2, "status": "ACTIVE"},
        {"prospects": 20, "pending": 0, "in_progress": 0, "status": "COMPLETED"},
        {"prospects": 30, "pending": 30, "in_progress": 0, "status": "PAUSED"},
    ]
    progress = _progress(campaigns, callbacks, attempts)
    check("active campaigns, with the paused and completed named", metric(progress, "campaigns_active").value == 1 and "1 paused" in metric(progress, "campaigns_active").detail and "1 completed" in metric(progress, "campaigns_active").detail)
    check("progress is closed memberships over all", metric(progress, "progress").value == "58%" and "58 of 100" in metric(progress, "progress").detail)
    check("calls remaining is pending plus due callbacks, with the live ones named", metric(progress, "calls_remaining").value == 41 and "1 callback due" in metric(progress, "calls_remaining").detail and "1 live now" in metric(progress, "calls_remaining").detail)
    check("with no memberships progress is unavailable", not metric(_progress([], callbacks, attempts), "progress").available)

    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Asia/Karachi")
    check("an empty filter reads as everything", ReportFilter().describe(zone) == "all campaigns · all time" and not ReportFilter().active)
    narrowed = ReportFilter(campaign_id=3, campaign_name="Q1", since=datetime(2026, 9, 1, tzinfo=zone), until=datetime(2026, 9, 8, tzinfo=zone))
    check("a narrowed one names the campaign and the inclusive days", narrowed.describe(zone) == "Q1 · 01 Sep 2026 to 07 Sep 2026", narrowed.describe(zone))
    check("its key is what the cache uses", narrowed.key() == (3, narrowed.since, narrowed.until) and narrowed.to_dict()["active"] is True)


# --- The aggregates, against real SQL ----------------------------------------


async def check_aggregates(store: CampaignStore) -> None:
    """The counting, against rows this check put there itself."""
    print("\n=== the aggregates (real SQL) ===")
    service = CampaignService(store, default_region=REGION, max_attempts=3, retry_minutes=60)
    campaign = await store.create_campaign(name="Dashboard", status=CampaignStatus.ACTIVE)

    people = []
    for index in range(4):
        person = await service.create_prospect(
            first_name=f"P{index}", last_name="Test", phone=f"+9232210000{index:02d}"
        )
        people.append(person)
        await store.add_to_campaign(campaign.id, person.id)

    counts = await store.prospect_counts()
    check("prospects are counted", counts["total"] == 4)
    check("and all are callable", counts["callable"] == 4 and counts["do_not_call"] == 0)
    await service.mark_do_not_call(people[3].id)
    counts = await store.prospect_counts()
    check("a do-not-call is counted apart", counts["do_not_call"] == 1 and counts["callable"] == 3)

    # One attempt per outcome, so every FILTER can be checked independently.
    outcomes = [
        (people[0].id, CallAttemptStatus.COMPLETED, 120),
        (people[1].id, CallAttemptStatus.NO_ANSWER, None),
        (people[2].id, CallAttemptStatus.FAILED, None),
        (people[3].id, CallAttemptStatus.NOT_INTERESTED, 60),
    ]
    for prospect_id, status, duration in outcomes:
        attempt = await store.create_attempt(prospect_id=prospect_id, campaign_id=campaign.id)
        await store.update_attempt_status(attempt.id, status, duration_seconds=duration)

    stats = await store.attempt_counts()
    check("total calls counts every attempt", stats["total"] == 4)
    check("answered counts the ones somebody picked up", stats["answered"] == 2, str(stats["answered"]))
    check("completed counts only COMPLETED", stats["completed"] == 1)
    check("failed counts only FAILED", stats["failed"] == 1)
    check("no-answer is its own number", stats["no_answer"] == 1)
    check("the average is over calls that have a duration", stats["average_duration_secs"] == 90.0)
    check("and says how many that was", stats["with_duration"] == 2)
    check("nothing is live", stats["live"] == 0)

    live = await store.create_attempt(prospect_id=people[0].id, campaign_id=campaign.id)
    await store.mark_attempt_unresolved(
        (await store.mark_placement_started(live.id)).id, "carrier never answered"
    )
    stats = await store.attempt_counts()
    check("an unresolved placement counts as live", stats["live"] == 1 and stats["unresolved"] == 1)

    print("\n=== per-campaign statistics ===")
    rows = await store.campaign_overview(limit=10)
    row = next(r for r in rows if r["id"] == campaign.id)
    check("the campaign is listed", row["name"] == "Dashboard")
    check("with its contacts", row["prospects"] == 4)
    check("its calls", row["attempts"] == 5)
    check("its answered calls", row["answered"] == 2)
    check("and its own average", row["average_duration_secs"] == 90.0)

    # The bug scalar subqueries exist to avoid: joining memberships *and*
    # attempts to a campaign multiplies them together.
    check(
        "counts are not multiplied by the join",
        row["prospects"] == 4 and row["attempts"] == 5,
        f"{row['prospects']} x {row['attempts']} would be 20",
    )

    print("\n=== the recent lists ===")
    recent = await store.recent_call_rows(limit=3)
    check("the newest calls come back first", len(recent) == 3 and recent[0]["id"] > recent[1]["id"])
    check("each carries who was called", all(r["first_name"] for r in recent))
    check("and which campaign", all(r["campaign_name"] == "Dashboard" for r in recent))

    print("\n=== the filters (Phase 20, real SQL) ===")
    other = await store.create_campaign(name="Other", status=CampaignStatus.ACTIVE)
    stranger = await service.create_prospect(first_name="Zara", last_name="Other", phone="+923221000099")
    await store.add_to_campaign(other.id, stranger.id)
    extra = await store.create_attempt(prospect_id=stranger.id, campaign_id=other.id)
    await store.update_attempt_status(extra.id, CallAttemptStatus.VOICEMAIL, duration_seconds=20)
    everything = await store.attempt_counts()
    mine = await store.attempt_counts(campaign_id=campaign.id)
    theirs = await store.attempt_counts(campaign_id=other.id)
    check("a campaign filter narrows the scan", everything["total"] == 6 and mine["total"] == 5 and theirs["total"] == 1 and theirs["voicemail"] == 1, f"{everything['total']} {mine['total']} {theirs['total']}")
    future = datetime.now(UTC) + timedelta(days=1)
    check("a date range narrows it too, and an empty range is empty not an error", (await store.attempt_counts(since=future))["total"] == 0 and (await store.attempt_counts(until=future))["total"] == 6)
    check("the new failure figures are counted", everything["with_failure_reason"] >= 1 and everything["unreached"] == 3 and everything["refused_before_dial"] == 1, str({k: everything[k] for k in ("with_failure_reason", "unreached", "refused_before_dial")}))
    check("the quality figures are absent, not zero, when no call recorded them", everything["quality_available"] and everything["with_latency"] == 0 and everything["response_p50_ms"] is None)
    await store.save_call_usage(extra.id, {"llm": {"requests": 4, "prompt_tokens": 400, "completion_tokens": 40}, "quality": {"p50_ms": 1500, "p95_ms": 2500, "greeting_ms": 4000, "failed_turns": 1, "errors": 1}}, cost_usd=0.01)
    measured = await store.attempt_counts(campaign_id=other.id)
    check("a call's quality summary is aggregated from the usage column", measured["with_latency"] == 1 and measured["response_p50_ms"] == 1500 and measured["failed_turns"] == 1 and measured["calls_with_errors"] == 1, str({k: measured[k] for k in ("with_latency", "response_p50_ms", "failed_turns", "calls_with_errors")}))
    check("prospects narrow by campaign membership", (await store.prospect_counts(campaign_id=other.id))["total"] == 1 and (await store.prospect_counts())["total"] == 5)
    overview = await store.campaign_overview(limit=10, campaign_id=other.id)
    check("the overview narrows to one campaign and carries the membership statuses", len(overview) == 1 and overview[0]["id"] == other.id and overview[0]["voicemail"] == 1 and "exhausted" in overview[0] and overview[0]["pending"] == 1)
    found = await store.recent_call_rows(limit=10, search="zara")
    check("a search finds by name, case-insensitively", len(found) == 1 and found[0]["prospect_id"] == stranger.id)
    check("and by the digits of a number only when asked to", len(await store.recent_call_rows(limit=10, search="1000099", search_phone=True)) == 1 and len(await store.recent_call_rows(limit=10, search="1000099", search_phone=False)) == 0)
    check("and by campaign and status together", len(await store.recent_call_rows(limit=10, campaign_id=other.id, status="VOICEMAIL")) == 1 and len(await store.recent_call_rows(limit=10, campaign_id=other.id, status="COMPLETED")) == 0)
    page = await store.recent_call_rows(limit=2)
    older = await store.recent_call_rows(limit=2, before_id=page[-1]["id"])
    check("paging walks backwards without overlap", len(page) == 2 and len(older) == 2 and older[0]["id"] < page[-1]["id"])
    people = await store.search_prospects("test", limit=10)
    check("people are searchable by name", len(people) == 4 and all(p.last_name == "Test" for p in people))
    usage, cost = await store.get_attempt_usage(extra.id)
    check("one call's usage reads back", usage is not None and usage["llm"]["requests"] == 4 and cost == 0.01)
    check("and a call without any is (None, None)", await store.get_attempt_usage(live.id) == (None, None))


async def check_degraded(store: CampaignStore, admin: asyncpg.Connection, schema: str) -> None:
    """A database without the Phase 7 and 8 tables still produces a dashboard."""
    print("\n=== a database missing the optional tables ===")
    snapshot = await collect(store, timezone="Asia/Karachi")
    check("with the tables present, results are available", metric(snapshot.totals, "qualified").available)
    check("and there is nothing to warn about", not snapshot.notes, str(snapshot.notes))

    # Drop exactly what a pre-Phase-8 database would not have.
    await admin.execute(f'DROP TABLE IF EXISTS "{schema}".call_results CASCADE')
    await admin.execute(f'DROP TABLE IF EXISTS "{schema}".meetings CASCADE')
    await admin.execute(f'DROP TABLE IF EXISTS "{schema}".callbacks CASCADE')

    snapshot = await collect(store, timezone="Asia/Karachi")
    check("the dashboard still renders", len(snapshot.totals) == 8)
    check("the numbers that survive are still right", metric(snapshot.totals, "calls").value == 6)
    check("qualified is unavailable", not metric(snapshot.totals, "qualified").available)
    check("meetings is unavailable", not metric(snapshot.totals, "meetings").available)
    check("and it says which command fixes it", any("campaign.py init" in note for note in snapshot.notes))
    check(
        "outcomes fall back to attempt statuses",
        snapshot.outcomes and snapshot.outcomes[0]["source"] == "attempt status",
        str([o["source"] for o in snapshot.outcomes][:1]),
    )
    check("and still add up", sum(o["count"] for o in snapshot.outcomes) == 5)
    check("the Phase 20 strips still render without the optional tables", snapshot.conversion and not snapshot.conversion[0].available and snapshot.performance and snapshot.errors and snapshot.progress)
    check("the timezone travels with the snapshot", snapshot.timezone == "Asia/Karachi")
    check("times are rendered in it", snapshot.to_dict()["generated_at_label"])


# --- The routes ---------------------------------------------------------------


def check_routes(dsn: str | None, schema: str | None = None) -> None:
    """The real application: the page, the JSON, and no way to write.

    Phase 20: run from inside the database section, over the throwaway
    schema the aggregate checks seeded, through a `store_factory` that opens
    its own pool with that schema's `search_path` — the app under FastAPI's
    test client runs on a loop of its own, and an asyncpg pool belongs to the
    loop that opened it.
    """
    print("\n=== the routes ===")
    if not dsn or not schema:
        _skipped.append("route checks (no database)")
        print("  SKIP  no database configured")
        return

    from fastapi.testclient import TestClient

    from src.config import Config
    from src.dashboard import LOGIN_PATH, LOGOUT_PATH, create_app
    from src.security import hash_password

    async def use_schema(connection: asyncpg.Connection) -> None:
        await connection.execute(f'SET search_path TO "{schema}"')

    async def real_store() -> CampaignStore:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, setup=use_schema)
        return CampaignStore(pool)

    os.environ["DATABASE_URL"] = dsn
    # Phase 18: the page is behind a login. One operator, made the way a
    # deployment makes one; the audit rows land in the real audit_log table.
    os.environ["DASHBOARD_USERS"] = (
        f"tester:operator:{hash_password('dashboard-check-password')},"
        f"viewer:viewer:{hash_password('dashboard-viewer-password')}"
    )
    os.environ.pop("DASHBOARD_AUTH_DISABLED", None)
    app = create_app(Config.from_env(), store_factory=real_store)

    with TestClient(app) as client:
        client.headers["origin"] = "http://testserver"
        check("the page needs a login", client.get("/", follow_redirects=False).status_code == 303)
        check("the JSON needs a login", client.get(API_PATH).status_code == 401)
        signed_in = client.post(LOGIN_PATH, data={"username": "tester", "password": "dashboard-check-password"}, follow_redirects=False)
        check("an operator signs in", signed_in.status_code == 303, signed_in.text[:120])

        page = client.get("/")
        check("the page is served", page.status_code == 200 and "Calling dashboard" in page.text)
        check("as HTML", page.headers["content-type"].startswith("text/html"))
        check("naming who is signed in", "<b>tester</b>" in page.text)

        data = client.get(API_PATH)
        check("the JSON endpoint answers", data.status_code == 200)
        body = data.json()
        check("with every section the page renders", set(body) >= {
            "totals", "attention", "outcomes", "campaigns", "recent_calls", "notes",
            "generated_at", "timezone", "filters", "conversion", "performance", "errors",
            "compliance", "progress",
        }, str(sorted(body)))
        check("eight headline numbers", len(body["totals"]) == 8)
        check("each with a label and a footnote", all(m["label"] and m["detail"] for m in body["totals"]))
        check("the view reads as everything", body["filters"]["active"] is False and body["filters"]["label"] == "all campaigns · all time")

        # Phase 20: the filters, the calls list, the search, one call.
        from src.dashboard import (
            CALL_PAGE_PATH,
            CALLS_API_PATH,
            CAMPAIGNS_API_PATH,
            SEARCH_API_PATH,
        )

        campaigns = client.get(CAMPAIGNS_API_PATH).json()["campaigns"]
        check("the campaigns list feeds the filter", any(c["name"] == "Dashboard" for c in campaigns) and all({"id", "name", "status"} <= set(c) for c in campaigns))
        mine = next(c for c in campaigns if c["name"] == "Dashboard")
        narrowed = client.get(API_PATH, params={"campaign": mine["id"]}).json()
        check("a campaign filter narrows the numbers and names itself", narrowed["filters"]["campaign_name"] == "Dashboard" and metric_value(narrowed["totals"], "calls") == 5 and len(narrowed["campaigns"]) == 1, str(narrowed["filters"]))
        check("by name as well as by id", client.get(API_PATH, params={"campaign": "Dashboard"}).json()["filters"]["campaign_id"] == mine["id"])
        check("an unknown campaign is 422", client.get(API_PATH, params={"campaign": "Nope"}).status_code == 422)
        check("a bad date is 422", client.get(API_PATH, params={"from": "yesterday"}).status_code == 422)
        check("a backwards range is 422", client.get(API_PATH, params={"from": "2026-09-08", "to": "2026-09-01"}).status_code == 422)
        ranged = client.get(API_PATH, params={"from": "2030-01-01", "to": "2030-01-02"}).json()
        check("a future range is empty, not an error, and says what it covers", metric_value(ranged["totals"], "calls") == 0 and "01 Jan 2030 to 02 Jan 2030" in ranged["filters"]["label"], ranged["filters"]["label"])
        check("the new strips are there with their footnotes", all(m["label"] and m["detail"] for section in ("conversion", "performance", "errors", "compliance", "progress") for m in body[section]))
        check("answer rate and calls remaining are among them", metric_value(body["performance"], "answer_rate") is not None and "calls_remaining" in [m["key"] for m in body["progress"]])

        listed = client.get(CALLS_API_PATH, params={"limit": 3}).json()
        check("the calls list pages", listed["count"] == 3 and listed["next_before_id"] and all("disposition_label" in row and "qualification" in row for row in listed["calls"]))
        older = client.get(CALLS_API_PATH, params={"limit": 3, "before_id": listed["next_before_id"]}).json()
        check("and walks backwards", older["count"] >= 1 and older["calls"][0]["attempt_id"] < listed["next_before_id"])
        found = client.get(CALLS_API_PATH, params={"q": "zara"}).json()
        check("a search by name finds the call", found["count"] == 1 and found["calls"][0]["prospect"] == "Zara Other")
        check("an operator may search by number", client.get(CALLS_API_PATH, params={"q": "1000099"}).json()["count"] == 1)
        check("a status filter applies", client.get(CALLS_API_PATH, params={"status": "voicemail"}).json()["count"] == 1 and client.get(CALLS_API_PATH, params={"status": "no-such"}).status_code == 422)
        searched = client.get(SEARCH_API_PATH, params={"q": "zara"}).json()
        check("the search box finds people and their calls", searched["prospects"][0]["name"] == "Zara Other" and searched["calls"][0]["prospect"] == "Zara Other")

        attempt_id = found["calls"][0]["attempt_id"]
        detail = client.get(f"{CALLS_API_PATH}/{attempt_id}")
        check("one call reads in full for an operator", detail.status_code == 200 and detail.json()["call"]["id"] == attempt_id and detail.json()["prospect"]["phone_normalized"] == "+923221000099" and detail.json()["usage"]["llm_requests"] == 4, detail.text[:200])
        check("with the quality figures from the usage column", detail.json()["quality"]["p50_ms"] == 1500)
        check("and the result's status where there is no result", detail.json()["result"] is None and detail.json()["call_labels"]["status"] == "Voicemail" and detail.json()["transcript_available"] is False)
        check("a missing call is 404", client.get(f"{CALLS_API_PATH}/999999").status_code == 404)
        page = client.get(f"{CALL_PAGE_PATH}/{attempt_id}")
        check("the call page is served", page.status_code == 200 and f"call #{attempt_id}" in page.text and f"{CALLS_API_PATH}/{attempt_id}" in page.text)

        ping = client.get("/api/ping")
        check("the liveness check answers", ping.status_code == 200 and ping.json()["ok"] is True)

        # Read-only is the property that makes this safe to point at a live
        # system, so it is checked rather than assumed.
        check("POST to the page is refused", client.post("/").status_code == 405)
        check("POST to the API is refused", client.post(API_PATH).status_code == 405)
        check("DELETE is refused", client.delete(API_PATH).status_code == 405)
        check("there are no interactive docs to write from", client.get("/docs").status_code == 404)
        writes = {(route.path, m) for route in app.routes for m in getattr(route, "methods", set()) if m not in ("GET", "HEAD")}
        check("the only writing routes are the login and the logout", writes == {(LOGIN_PATH, "POST"), (LOGOUT_PATH, "POST")}, str(sorted(writes)))
        out = client.post(LOGOUT_PATH, follow_redirects=False)
        check("signing out ends the session", out.status_code == 303 and client.get(API_PATH).status_code == 401)

        # A viewer: the same pages with the numbers masked and no transcript.
        client.cookies.clear()
        check("a viewer signs in", client.post(LOGIN_PATH, data={"username": "viewer", "password": "dashboard-viewer-password"}, follow_redirects=False).status_code == 303)
        masked = client.get(CALLS_API_PATH, params={"q": "zara"}).json()
        check("a viewer's calls list is masked", masked["masked"] is True and masked["calls"][0]["phone"] != "+923221000099" and masked["calls"][0]["phone"].endswith("99"))
        check("a viewer cannot search by number", client.get(CALLS_API_PATH, params={"q": "1000099"}).json()["count"] == 0)
        seen = client.get(f"{CALLS_API_PATH}/{attempt_id}").json()
        check("a viewer's call detail withholds the transcript and the record, and says so", seen["transcript_included"] is False and seen["conversation"] is None and seen["viewer"]["can_read_pii"] is False and seen["prospect"]["phone_normalized"] != "+923221000099")
        check("the call page for a viewer says the numbers are masked", "numbers masked" in client.get(f"{CALL_PAGE_PATH}/{attempt_id}").text)


async def run_database_checks(dsn: str) -> None:
    """Everything that needs real SQL, in a schema that is thrown away."""
    from tests.test_campaigns import with_temp_schema

    store, admin, schema = await with_temp_schema(dsn)
    try:
        await check_aggregates(store)
        # The routes, over the rows just seeded, before the degraded check
        # drops the optional tables. Sync on purpose: the test client runs
        # the app on its own loop, and its store opens its own pool.
        await asyncio.to_thread(check_routes, dsn, schema)
        await check_degraded(store, admin, schema)
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def main() -> int:
    """Run every check and report."""
    print("Dashboard checks — no vendors, no phone, no audio.")

    check_formatting()
    check_metrics()
    check_unavailable_is_not_zero()
    check_page()
    check_analytics()

    dsn = os.getenv("DATABASE_URL") or os.getenv("KB_DATABASE_URL")
    if not dsn:
        _skipped.append("aggregate checks (no DATABASE_URL or KB_DATABASE_URL)")
    else:
        try:
            await run_database_checks(dsn)
        except (OSError, asyncpg.PostgresError) as exc:
            _skipped.append(f"aggregate checks (cannot reach PostgreSQL: {exc})")

    if not dsn:
        check_routes(None)

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
