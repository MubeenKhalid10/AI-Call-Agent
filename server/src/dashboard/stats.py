"""The numbers a dashboard shows, assembled from the campaign store. Phase 10; Phase 20 extended it.

**This module counts nothing itself.** Every total comes from a SQL aggregate in
`campaigns/store.py`; what happens here is shaping — turning counts into
labelled metrics with the footnote each one needs, and merging the two sources
that a campaign row is built from. That split is deliberate: the arithmetic
belongs next to the tables it reads, and the wording belongs next to the page
that shows it.

**Two numbers on this page could mislead, and both carry their footnote.**

* *Completed* and *answered* overlap and are not the same. `COMPLETED` is the
  carrier's word for a call that ran to its end; *answered* is every status
  meaning somebody picked up, which includes a call the person ended by asking
  never to be called again. Showing one without the other would either
  undercount reached people or overstate clean endings, so both are shown and
  each says what it is.
* *Average call duration* is meaningless without knowing how many calls it
  averages. `detail` always says, because "4 min 12 s" over three calls and the
  same figure over three hundred are different claims.

**A missing table is reported, never counted as zero.** `call_results` and
`meetings` arrive in Phases 8 and 7, so a database that predates them has no
qualification or booking numbers. Those metrics come back `available=False` and
the page says "unavailable" — because a zero there would read as "nobody
qualified", which is a different and wrong statement.

**Phase 20.** Every read takes a `ReportFilter` — a campaign, a date range —
applied inside the same single-pass aggregates rather than by fetching more
and filtering here, so a narrowed view costs the same scan as the whole. Four
new strips answer the questions the totals do not: *conversion* (of the
people reached, how many qualified, booked, asked for a callback, were
handed to a person), *performance* (answer, voicemail and transfer rates,
duration, the agent's response latency from Phase 12's measurements),
*errors* (what failed and where), and *compliance* (the do-not-call list and
the opt-outs, from Phase 19). Rates carry their denominator, always: a rate
over three calls is a number worth distrusting, and the only way a reader
knows that is to be told.

Nothing here writes. The dashboard is a reader, and the only reason it can be
pointed at a live calling system is that it cannot change anything.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from ..campaigns.coordination import DEFAULT_STALE_SECS, QueueDepth, WorkerSummary
from ..campaigns.store import CampaignStore, CampaignStoreError
from ..reliability.guardrails import resolve_zone

#: How many rows the two lists show. Small on purpose: a dashboard is a glance,
#: and `campaign.py attempts` is the tool for reading history properly.
RECENT_CALLS = 15
RECENT_CAMPAIGNS = 8
#: The most rows the calls list pages through at once.
MAX_CALLS_PAGE = 100


@dataclass(frozen=True)
class ReportFilter:
    """What a view is narrowed to. Phase 20. Empty means everything.

    Attributes:
        campaign_id: One campaign, or None for all.
        campaign_name: The campaign's name, for the page's stamp.
        since / until: A half-open range on when a row was created,
            timezone-aware. `until` is exclusive, so a day is
            `[00:00, next 00:00)`.
    """

    campaign_id: int | None = None
    campaign_name: str | None = None
    since: datetime | None = None
    until: datetime | None = None

    @property
    def active(self) -> bool:
        return self.campaign_id is not None or self.since is not None or self.until is not None

    def key(self) -> tuple[Any, ...]:
        """A hashable identity, for the snapshot cache."""
        return (self.campaign_id, self.since, self.until)

    def scope(self) -> dict[str, Any]:
        """The keyword arguments the store's aggregates take."""
        return {"campaign_id": self.campaign_id, "since": self.since, "until": self.until}

    def describe(self, zone: tzinfo) -> str:
        """One clause for the page: `Q1 Outreach · 1 Sep – 7 Sep`, or `all campaigns, all time`."""
        parts = [self.campaign_name or (f"campaign {self.campaign_id}" if self.campaign_id else "all campaigns")]
        if self.since or self.until:
            start = f"{self.since.astimezone(zone):%d %b %Y}" if self.since else "the beginning"
            end = f"{(self.until - timedelta(seconds=1)).astimezone(zone):%d %b %Y}" if self.until else "now"
            parts.append(f"{start} to {end}")
        else:
            parts.append("all time")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name,
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "active": self.active,
        }


@dataclass(frozen=True)
class Metric:
    """One headline number, with what it means.

    Attributes:
        key: Stable machine name, for the JSON and the page's markup.
        label: What a person reads.
        value: The number, already formatted when it is not a plain count.
        detail: The footnote that stops the number being misread. Never
            decorative — every metric that could mislead has one.
        available: False when the data could not be read at all. The page shows
            "unavailable" rather than a zero.
        tone: A hint for the page: `neutral`, `good`, `bad` or `warn`.
    """

    key: str
    label: str
    value: Any
    detail: str = ""
    available: bool = True
    tone: str = "neutral"

    def to_dict(self) -> dict[str, Any]:
        """Plain data for the JSON API."""
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value if self.available else None,
            "detail": self.detail,
            "available": self.available,
            "tone": self.tone,
        }


@dataclass(frozen=True)
class Snapshot:
    """Everything the dashboard shows, as of one moment."""

    generated_at: datetime
    timezone: str
    totals: list[Metric]
    attention: list[Metric]
    outcomes: list[dict[str, Any]]
    campaigns: list[dict[str, Any]]
    recent_calls: list[dict[str, Any]]
    usage: list[Metric] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Phase 20.
    filters: ReportFilter = field(default_factory=ReportFilter)
    conversion: list[Metric] = field(default_factory=list)
    performance: list[Metric] = field(default_factory=list)
    errors: list[Metric] = field(default_factory=list)
    compliance: list[Metric] = field(default_factory=list)
    progress: list[Metric] = field(default_factory=list)
    # Phase 21.
    scheduler: list[Metric] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Plain data for the JSON API."""
        zone = resolve_zone(self.timezone)
        return {
            "generated_at": self.generated_at.isoformat(),
            "generated_at_label": _when(self.generated_at, zone),
            "timezone": self.timezone,
            "totals": [metric.to_dict() for metric in self.totals],
            "attention": [metric.to_dict() for metric in self.attention],
            "outcomes": self.outcomes,
            "campaigns": self.campaigns,
            "recent_calls": self.recent_calls,
            "usage": [metric.to_dict() for metric in self.usage],
            "notes": self.notes,
            "filters": {**self.filters.to_dict(), "label": self.filters.describe(zone)},
            "conversion": [metric.to_dict() for metric in self.conversion],
            "performance": [metric.to_dict() for metric in self.performance],
            "errors": [metric.to_dict() for metric in self.errors],
            "compliance": [metric.to_dict() for metric in self.compliance],
            "progress": [metric.to_dict() for metric in self.progress],
            "scheduler": [metric.to_dict() for metric in self.scheduler],
        }


async def collect(
    store: CampaignStore,
    *,
    timezone: str = "UTC",
    filters: ReportFilter | None = None,
) -> Snapshot:
    """Read every number the dashboard shows, in one pass.

    Args:
        store: The campaign store. Only read methods are called.
        timezone: The zone times are displayed in — the campaign timezone, so
            the dashboard agrees with what the agent told the prospect and with
            what `campaign.py` prints.
        filters: Phase 20. A campaign and a date range, applied inside every
            aggregate. None means everything, as Phase 10 showed it.

    Returns:
        A `Snapshot`. Never raises for a missing optional table; the affected
        metrics come back unavailable and `notes` says which and why.
    """
    zone = resolve_zone(timezone)
    notes: list[str] = []
    filters = filters or ReportFilter()
    scope = filters.scope()

    # Phase 11: concurrently, not one after another. They are independent reads
    # on separate pooled connections, and run in sequence they cost the sum of
    # their times — measured at 249 ms over 60,000 attempts, of which the
    # slowest single query was 77 ms. Nothing about the numbers changes; only
    # how long the page waits for them. Phase 20 adds two small reads (the
    # do-not-call counts and the compliance rows) and filters the rest.
    (
        prospects,
        attempts,
        results,
        meetings,
        callbacks,
        dispositions,
        campaigns,
        recent,
        dnc,
        blocked,
        workers,
        depth,
    ) = await asyncio.gather(
        store.prospect_counts(campaign_id=filters.campaign_id),
        store.attempt_counts(**scope),
        store.result_counts(**scope),
        store.meeting_counts(**scope),
        store.callback_counts(**scope),
        store.disposition_counts(**scope),
        _campaigns(store, zone, filters),
        _recent_calls(store, zone, filters=filters),
        _dnc_counts(store),
        _blocked_counts(store, filters),
        # Phase 21: the fleet and the queue. Two small reads on their own
        # connections; the heartbeat table is at most one row per worker.
        _quiet(store.worker_summary(stale_after_secs=DEFAULT_STALE_SECS)),
        _quiet(store.queue_depth()),
    )
    if workers is None:
        notes.append(
            "Worker health is unavailable: this database has no scheduler_workers table. "
            "Run `uv run campaign.py init`."
        )

    if results is None or dispositions is None:
        notes.append(
            "Call results are unavailable: this database has no call_results table. "
            "Run `uv run campaign.py init`, then `uv run campaign.py rebuild-results`."
        )
    if meetings is None or callbacks is None:
        notes.append(
            "Meetings and callbacks are unavailable: this database predates them. "
            "Run `uv run campaign.py init`."
        )
    if dnc is None:
        notes.append(
            "The do-not-call list is unavailable: this database has no dnc_numbers table. "
            "Run `uv run campaign.py init`."
        )

    return Snapshot(
        generated_at=datetime.now(UTC),
        timezone=timezone,
        totals=_totals(prospects, attempts, results, meetings),
        attention=_attention(attempts, callbacks),
        outcomes=_outcomes(dispositions, attempts),
        campaigns=campaigns,
        recent_calls=recent,
        usage=_usage(attempts),
        notes=notes,
        filters=filters,
        conversion=_conversion(attempts, results, meetings, callbacks),
        performance=_performance(attempts, results),
        errors=_errors(attempts),
        compliance=_compliance(prospects, results, dnc, blocked),
        progress=_progress(campaigns, callbacks, attempts),
        scheduler=_scheduler(workers, depth),
    )


def _totals(
    prospects: dict[str, int],
    attempts: dict[str, Any],
    results: dict[str, Any] | None,
    meetings: dict[str, int] | None,
) -> list[Metric]:
    """The headline tiles, each with the footnote that keeps it honest."""
    average = attempts["average_duration_secs"]
    with_duration = attempts["with_duration"]

    return [
        Metric(
            "contacts",
            "Total contacts",
            prospects["total"],
            _join(
                f"{prospects['callable']} callable",
                f"{prospects['do_not_call']} do-not-call" if prospects["do_not_call"] else "",
                f"{prospects['unreachable']} no usable number" if prospects["unreachable"] else "",
            ),
        ),
        Metric(
            "calls",
            "Total calls",
            attempts["total"],
            "every dial attempted, including those the carrier refused",
        ),
        Metric(
            "answered",
            "Answered calls",
            attempts["answered"],
            "somebody picked up — includes calls that ended in a no or a do-not-call",
            tone="good" if attempts["answered"] else "neutral",
        ),
        Metric(
            "completed",
            "Completed calls",
            attempts["completed"],
            "the carrier's word for a call that ran to its end; overlaps with answered",
        ),
        Metric(
            "failed",
            "Failed calls",
            attempts["failed"],
            _join(
                "could not be placed, or dropped before it was answered",
                f"{attempts['no_answer']} no answer" if attempts["no_answer"] else "",
                f"{attempts['busy']} busy" if attempts["busy"] else "",
            ),
            tone="bad" if attempts["failed"] else "neutral",
        ),
        Metric(
            "average_duration",
            "Average call duration",
            _duration(average),
            (
                f"over the {with_duration} calls that have a duration"
                if with_duration != 1
                else "over the single call that has a duration"
            )
            if with_duration
            else "no call has a recorded duration yet",
            available=average is not None,
        ),
        Metric(
            "qualified",
            "Qualified prospects",
            results["qualified_prospects"] if results else None,
            (
                "distinct people, from a need, interest and authority all established"
                if results
                else "needs the call_results table"
            ),
            available=results is not None,
            tone="good" if results and results["qualified_prospects"] else "neutral",
        ),
        Metric(
            "meetings",
            "Meetings booked",
            meetings["booked"] if meetings else None,
            (
                _join(
                    "confirmed by the calendar",
                    f"{meetings['upcoming']} still to come" if meetings["upcoming"] else "",
                    # Says why the campaign table below can show fewer. A
                    # browser or eval session books a real slot and has no
                    # campaign to attribute it to.
                    f"{meetings['unattributed']} not tied to a campaign"
                    if meetings["unattributed"]
                    else "",
                )
                if meetings
                else "needs the meetings table"
            ),
            available=meetings is not None,
            tone="good" if meetings and meetings["booked"] else "neutral",
        ),
    ]


def _usage(attempts: dict[str, Any]) -> list[Metric]:
    """What calls have consumed, and what that cost. Phase 11.

    Its own strip rather than more headline tiles: these answer "what are we
    spending", which is a different question from "how is the campaign going",
    and a reader looking for one should not have to filter out the other.

    Every figure is over the calls that *have* usage recorded, and the count
    says how many that is — usage is only measured from Phase 11 onwards, so on
    an existing database most calls have none and an average over the rest
    would otherwise look like an average over all of them.
    """
    if not attempts.get("usage_available"):
        return [
            Metric(
                "usage",
                "Usage per call",
                None,
                "needs the usage column — run `uv run campaign.py init`",
                available=False,
            )
        ]

    measured = attempts["with_usage"]
    if not measured:
        return [
            Metric(
                "usage",
                "Usage per call",
                None,
                "no call has recorded usage yet; it is measured from the next call onwards",
                available=False,
            )
        ]

    requests = attempts["llm_requests"]
    prompt = attempts["prompt_tokens"]
    priced = attempts["with_cost"]
    strip = [
        Metric(
            "measured_calls",
            "Calls with usage",
            measured,
            f"of {attempts['total']} — usage is recorded from Phase 11 onwards",
        ),
        Metric(
            "tokens_per_call",
            "Tokens per call",
            f"{(prompt + attempts['completion_tokens']) // measured:,}",
            f"{prompt // measured:,} prompt, {attempts['completion_tokens'] // measured:,} completion",
        ),
        Metric(
            "tokens_per_request",
            "Prompt tokens per request",
            f"{prompt // requests:,}" if requests else None,
            "what a per-minute token budget is spent on; a tool turn is two requests",
            available=bool(requests),
        ),
        Metric(
            "llm_requests",
            "LLM requests",
            f"{requests:,}",
            f"{requests / measured:.1f} per measured call" if measured else "",
        ),
    ]
    strip.append(
        Metric(
            "cost_per_call",
            "Cost per call",
            f"${attempts['average_cost_usd']:.4f}" if attempts["average_cost_usd"] else None,
            (
                f"over the {priced} call{'s' if priced != 1 else ''} with configured rates; "
                f"${attempts['total_cost_usd']:.2f} in total"
                if priced
                else "no per-unit rates are configured, so cost is not estimated"
            ),
            available=bool(priced and attempts["average_cost_usd"]),
        )
    )
    strip.append(
        Metric(
            "total_cost",
            "Total cost",
            f"${attempts['total_cost_usd']:.2f}" if attempts["total_cost_usd"] is not None else None,
            f"over {priced} priced call{'s' if priced != 1 else ''}" if priced else "no rates configured",
            available=attempts["total_cost_usd"] is not None,
        )
    )
    return strip


def _attention(attempts: dict[str, Any], callbacks: dict[str, int] | None) -> list[Metric]:
    """The few numbers that mean somebody should go and do something.

    Deliberately separate from the totals: a total is history and these are a
    to-do list. Each is omitted entirely when it is zero, so an idle system
    shows an empty strip rather than a row of noughts to scan past.
    """
    strip: list[Metric] = []
    if attempts["live"]:
        strip.append(
            Metric(
                "live",
                "Calls live now",
                attempts["live"],
                "a call in progress — or an attempt left behind by a crash",
                tone="warn",
            )
        )
    if attempts["unresolved"]:
        strip.append(
            Metric(
                "unresolved",
                "Unresolved placements",
                attempts["unresolved"],
                "the carrier never reported an outcome. Run `campaign.py recover`",
                tone="bad",
            )
        )
    if callbacks and callbacks["due"]:
        strip.append(
            Metric(
                "callbacks_due",
                "Callbacks due",
                callbacks["due"],
                "a prospect asked to be called back and the time has come",
                tone="warn",
            )
        )
    if callbacks and callbacks["pending"]:
        strip.append(
            Metric(
                "callbacks_pending",
                "Callbacks scheduled",
                callbacks["pending"],
                "promised, not yet due",
            )
        )
    return strip


def _outcomes(dispositions: dict[str, int] | None, attempts: dict[str, Any]) -> list[dict[str, Any]]:
    """What calls came to, as a share of the whole.

    Uses Phase 8's `disposition` — one word per finished call, derived by
    precedence — because that is the vocabulary the whole system already agrees
    on. When `call_results` is missing it falls back to the attempt statuses,
    which are coarser but always present, and says so.
    """
    if dispositions:
        total = sum(dispositions.values()) or 1
        return [
            {
                "label": _humanise(name),
                "key": name,
                "count": count,
                "share": round(count * 100 / total),
                "tone": _tone_for(name),
                "source": "disposition",
            }
            for name, count in dispositions.items()
        ]

    fallback = {
        "COMPLETED": attempts["completed"],
        "FAILED": attempts["failed"],
        "NO_ANSWER": attempts["no_answer"],
        "BUSY": attempts["busy"],
        # Phase 12. `.get`, because a snapshot built before the column was
        # counted has no key for it, and a missing count is not a zero worth
        # failing over.
        "VOICEMAIL": attempts.get("voicemail", 0),
        "DO_NOT_CALL": attempts["do_not_call"],
        "NOT_INTERESTED": attempts["not_interested"],
        "CALLBACK_REQUESTED": attempts["callback_requested"],
    }
    present = {name: count for name, count in fallback.items() if count}
    total = sum(present.values()) or 1
    return [
        {
            "label": _humanise(name),
            "key": name,
            "count": count,
            "share": round(count * 100 / total),
            "tone": _tone_for(name),
            "source": "attempt status",
        }
        for name, count in sorted(present.items(), key=lambda item: -item[1])
    ]


# --- Phase 20: conversion, performance, errors, compliance, progress --------------


def _rate(numerator: int | None, denominator: int | None) -> float | None:
    """A percentage, or None when there is nothing to divide by."""
    if not denominator or numerator is None:
        return None
    return round(numerator * 100 / denominator, 1)


def _pct(value: float | None) -> str | None:
    return f"{value:g}%" if value is not None else None


def _conversion(
    attempts: dict[str, Any],
    results: dict[str, Any] | None,
    meetings: dict[str, int] | None,
    callbacks: dict[str, int] | None,
) -> list[Metric]:
    """Of the people reached, what came of it. Phase 20.

    Every rate here is over *answered* calls — the carrier reached somebody —
    because a meeting rate over dials nobody answered describes the list,
    not the conversation. The denominator is in every footnote.
    """
    answered = attempts["answered"]
    over = f"of {answered} answered call{'s' if answered != 1 else ''}"
    if results is None:
        return [
            Metric("conversion", "Conversion", None, "needs the call_results table", available=False),
        ]
    qualified = results["qualified_calls"]
    booked = results["meetings_booked"]
    callbacks_asked = results["callbacks_requested"] + results["callbacks_scheduled"]
    transferred = results["transferred"]
    strip = [
        Metric(
            "qualification_rate",
            "Qualified",
            _pct(_rate(qualified, answered)),
            _join(f"{qualified} call{'s' if qualified != 1 else ''} {over}", f"{results['partially_qualified']} partly qualified" if results["partially_qualified"] else "", f"{results['disqualified']} disqualified" if results["disqualified"] else ""),
            available=answered > 0,
            tone="good" if qualified else "neutral",
        ),
        Metric(
            "meeting_rate",
            "Meetings booked",
            _pct(_rate(booked, answered)),
            _join(f"{booked} {over}", f"{results['meetings_agreed']} agreed but not booked" if results["meetings_agreed"] else ""),
            available=answered > 0,
            tone="good" if booked else "neutral",
        ),
        Metric(
            "callback_rate",
            "Callbacks asked for",
            _pct(_rate(callbacks_asked, answered)),
            _join(
                f"{callbacks_asked} {over}",
                f"{results['callbacks_scheduled']} at a chosen time" if results["callbacks_scheduled"] else "",
                f"{callbacks['pending']} still pending" if callbacks and callbacks["pending"] else "",
            ),
            available=answered > 0,
        ),
        Metric(
            "transfer_rate",
            "Handed to a person",
            _pct(_rate(transferred, answered)),
            _join(f"{transferred} {over}", f"{results['human_requested']} asked for a person" if results["human_requested"] else ""),
            available=answered > 0,
            tone="good" if transferred else "neutral",
        ),
        Metric(
            "not_interested_rate",
            "Not interested",
            _pct(_rate(results["not_interested"], answered)),
            f"{results['not_interested']} {over} — a clear, recorded no",
            available=answered > 0,
            tone="bad" if results["not_interested"] else "neutral",
        ),
    ]
    return strip


def _performance(attempts: dict[str, Any], results: dict[str, Any] | None) -> list[Metric]:
    """How the calls themselves went: reached, voicemail, transfers, duration, latency. Phase 20."""
    total = attempts["total"]
    answered = attempts["answered"]
    voicemail = attempts.get("voicemail", 0)
    over_calls = f"of {total} call{'s' if total != 1 else ''}"
    strip = [
        Metric(
            "answer_rate",
            "Answer rate",
            _pct(_rate(answered, total)),
            f"{answered} answered {over_calls}",
            available=total > 0,
            tone="good" if answered else "neutral",
        ),
        Metric(
            "voicemail_rate",
            "Voicemail rate",
            _pct(_rate(voicemail, total)),
            f"{voicemail} {over_calls} reached a machine — a high figure says something about the calling hours",
            available=total > 0,
            tone="warn" if _rate(voicemail, total) and _rate(voicemail, total) > 30 else "neutral",
        ),
        Metric(
            "human_transfer_rate",
            "Human-transfer rate",
            _pct(_rate(results["transferred"], answered)) if results else None,
            (
                f"{results['transferred']} of {answered} answered call{'s' if answered != 1 else ''} handed to a colleague"
                if results
                else "needs the call_results table"
            ),
            available=results is not None and answered > 0,
        ),
        Metric(
            "average_duration",
            "Average call duration",
            _duration(attempts["average_duration_secs"]),
            f"over the {attempts['with_duration']} call{'s' if attempts['with_duration'] != 1 else ''} with a duration; "
            f"{_duration(attempts['total_duration_secs']) or '0s'} in total",
            available=attempts["average_duration_secs"] is not None,
        ),
    ]
    if not attempts.get("quality_available"):
        strip.append(Metric("response_latency", "Response latency", None, "needs the usage column — run `uv run campaign.py init`", available=False))
        return strip
    with_latency = attempts.get("with_latency", 0)
    p50 = attempts.get("response_p50_ms")
    strip.append(
        Metric(
            "response_latency",
            "Response latency",
            f"{p50 / 1000:.1f}s" if p50 is not None else None,
            (
                _join(
                    f"the average of each call's median, over {with_latency} measured call{'s' if with_latency != 1 else ''}",
                    f"p95 {attempts['response_p95_ms'] / 1000:.1f}s" if attempts.get("response_p95_ms") is not None else "",
                    f"greeting {attempts['greeting_ms'] / 1000:.1f}s" if attempts.get("greeting_ms") is not None else "",
                )
                if with_latency
                else "no call has recorded its latency yet; it is measured from the next call onwards"
            ),
            available=p50 is not None,
            tone="warn" if p50 is not None and p50 > 3000 else "neutral",
        )
    )
    return strip


def _errors(attempts: dict[str, Any]) -> list[Metric]:
    """What went wrong, and where: the carrier, the placement, the turns. Phase 20."""
    total = attempts["total"]
    strip = [
        Metric(
            "failed_calls",
            "Failed calls",
            attempts["failed"],
            _join(
                f"{_pct(_rate(attempts['failed'], total)) or '0%'} of {total}",
                f"{attempts['refused_before_dial']} refused before dialling" if attempts["refused_before_dial"] else "",
                f"{attempts['with_failure_reason']} with a recorded reason" if attempts["with_failure_reason"] else "",
            ),
            tone="bad" if attempts["failed"] else "neutral",
        ),
        Metric(
            "unresolved",
            "Unresolved placements",
            attempts["unresolved"],
            "the carrier never said; `campaign.py recover` asks it" if attempts["unresolved"] else "none — every placement reported an outcome",
            tone="bad" if attempts["unresolved"] else "neutral",
        ),
        Metric(
            "unreached",
            "Unreached",
            attempts["unreached"],
            _join(
                "no answer, busy, voicemail or failed",
                f"{attempts['no_answer']} no answer" if attempts["no_answer"] else "",
                f"{attempts['busy']} busy" if attempts["busy"] else "",
                f"{attempts.get('voicemail', 0)} voicemail" if attempts.get("voicemail") else "",
            ),
        ),
    ]
    if attempts.get("quality_available"):
        measured = attempts.get("with_quality", 0)
        strip.append(
            Metric(
                "failed_turns",
                "Failed turns",
                attempts.get("failed_turns", 0),
                _join(
                    f"over {measured} measured call{'s' if measured != 1 else ''}" if measured else "no call has recorded its turns yet",
                    f"{attempts.get('late_turns', 0)} late" if attempts.get("late_turns") else "",
                    f"{attempts.get('barge_ins', 0)} barge-ins" if attempts.get("barge_ins") else "",
                ),
                available=measured > 0,
                tone="bad" if attempts.get("failed_turns") else "neutral",
            )
        )
        strip.append(
            Metric(
                "calls_with_errors",
                "Calls with service errors",
                attempts.get("calls_with_errors", 0),
                f"a vendor error the sink caught, over {measured} measured call{'s' if measured != 1 else ''}" if measured else "no call has recorded its errors yet",
                available=measured > 0,
                tone="bad" if attempts.get("calls_with_errors") else "neutral",
            )
        )
    return strip


def _compliance(
    prospects: dict[str, int],
    results: dict[str, Any] | None,
    dnc: dict[str, int] | None,
    blocked: dict[str, int] | None,
) -> list[Metric]:
    """The do-not-call list and the opt-outs. Phase 20, from Phase 19's rows."""
    strip = [
        Metric(
            "dnc_prospects",
            "Do-not-call prospects",
            prospects["do_not_call"],
            f"of {prospects['total']} contacts, marked never to be dialled",
            tone="warn" if prospects["do_not_call"] else "neutral",
        ),
        Metric(
            "dnc_list",
            "Numbers on the list",
            dnc["active"] if dnc is not None else None,
            (
                _join(
                    *(f"{count} {source}" for source, count in sorted(dnc.items()) if source not in ("active", "revoked") and count),
                    f"{dnc['revoked']} revoked" if dnc["revoked"] else "",
                )
                or "none yet"
                if dnc is not None
                else "needs the dnc_numbers table"
            ),
            available=dnc is not None,
        ),
        Metric(
            "opted_out",
            "Opted out on a call",
            results["opted_out"] if results else None,
            (
                f"asked, on the call, never to be phoned again" + (f"; {results['do_not_call']} dial{'s' if results['do_not_call'] != 1 else ''} refused by the list" if results["do_not_call"] else "")
                if results
                else "needs the call_results table"
            ),
            available=results is not None,
            tone="warn" if results and results["opted_out"] else "neutral",
        ),
    ]
    if blocked is not None:
        strip.append(
            Metric(
                "blocked_dials",
                "Dials the gate refused",
                blocked.get("blocked", 0),
                _join(
                    f"{blocked.get('allowed', 0)} allowed",
                    *(f"{count} {code.replace('_', ' ')}" for code, count in sorted(blocked.items()) if code not in ("blocked", "allowed") and count),
                )
                or "nothing decided yet",
                tone="warn" if blocked.get("blocked") else "neutral",
            )
        )
    return strip


def _scheduler(workers: WorkerSummary | None, depth: QueueDepth | None) -> list[Metric]:
    """The worker fleet and the queue it serves. Phase 21.

    Unfiltered on purpose: a worker serves every active campaign unless told
    otherwise, and "is anyone running" is a question about the deployment,
    not about the campaign the page is scoped to.
    """
    if workers is None:
        alive_detail = "needs the scheduler_workers table"
    elif not workers.workers:
        alive_detail = "none registered — start one with `campaign.py run`"
    else:
        alive_detail = _join(
            f"{workers.running} running",
            f"{workers.draining} draining" if workers.draining else "",
            f"{workers.stale} stale" if workers.stale else "",
            f"{workers.stopped} stopped" if workers.stopped else "",
        )
    strip = [
        Metric(
            "workers_alive",
            "Workers alive",
            workers.alive if workers is not None else None,
            alive_detail,
            available=workers is not None,
            tone=(
                "bad"
                if workers is not None and workers.stale
                else "good"
                if workers is not None and workers.alive
                else "warn"
                if workers is not None and depth is not None and depth.due_now
                else "neutral"
            ),
        ),
        Metric(
            "workers_in_flight",
            "Calls being followed",
            workers.in_flight if workers is not None else None,
            (
                f"across {workers.alive} live worker(s)" if workers is not None and workers.alive else "no worker is following calls"
            )
            if workers is not None
            else "needs the scheduler_workers table",
            available=workers is not None,
        ),
        Metric(
            "queue_due",
            "Due now",
            depth.due_now if depth is not None else None,
            (
                _join(
                    f"{depth.callbacks_due} callback(s) due" if depth.callbacks_due else "",
                    f"{depth.reserved} reserved" if depth.reserved else "",
                    f"{depth.live} live" if depth.live else "",
                    f"across {depth.active_campaigns} active campaign(s)",
                )
                if depth is not None
                else "queue depth unavailable"
            ),
            available=depth is not None,
            tone="warn" if depth is not None and depth.due_now and (workers is None or not workers.alive) else "neutral",
        ),
        Metric(
            "queue_scheduled",
            "Scheduled later",
            depth.scheduled if depth is not None else None,
            "retries and callbacks whose time has not come" if depth is not None else "queue depth unavailable",
            available=depth is not None,
        ),
    ]
    return strip


def _progress(
    campaigns: list[dict[str, Any]], callbacks: dict[str, int] | None, attempts: dict[str, Any]
) -> list[Metric]:
    """Where the campaigns stand, and how much calling is left. Phase 20."""
    total = sum(row["prospects"] for row in campaigns)
    pending = sum(row["pending"] for row in campaigns)
    in_progress = sum(row["in_progress"] for row in campaigns)
    done = total - pending - in_progress
    active = sum(1 for row in campaigns if row["status"] == "ACTIVE")
    due = callbacks["due"] if callbacks else 0
    strip = [
        Metric(
            "campaigns_active",
            "Active campaigns",
            active,
            _join(
                f"of {len(campaigns)} shown",
                f"{sum(1 for row in campaigns if row['status'] == 'PAUSED')} paused" if any(row["status"] == "PAUSED" for row in campaigns) else "",
                f"{sum(1 for row in campaigns if row['status'] == 'COMPLETED')} completed" if any(row["status"] == "COMPLETED" for row in campaigns) else "",
            ),
        ),
        Metric(
            "progress",
            "Campaign progress",
            _pct(_rate(done, total)),
            f"{done} of {total} memberships closed (reached, exhausted or skipped)" if total else "no memberships yet",
            available=total > 0,
            tone="good" if total and done == total else "neutral",
        ),
        Metric(
            "calls_remaining",
            "Calls remaining",
            pending + due,
            _join(
                f"{pending} pending membership{'s' if pending != 1 else ''}",
                f"{in_progress} on a call" if in_progress else "",
                f"{due} callback{'s' if due != 1 else ''} due" if due else "",
                f"{attempts['live']} live now" if attempts["live"] else "",
            ),
            tone="warn" if pending + due else "neutral",
        ),
    ]
    return strip


async def _dnc_counts(store: CampaignStore) -> dict[str, int] | None:
    """The list's counts, or None when the store has no list."""
    reader = getattr(store, "dnc_counts", None)
    if reader is None:
        return None
    try:
        return await reader()
    except CampaignStoreError:
        return None


async def _blocked_counts(store: CampaignStore, filters: ReportFilter) -> dict[str, int] | None:
    """How many dials the compliance gate allowed and refused, by code, in the range."""
    reader = getattr(store, "list_audit", None)
    counter = getattr(store, "audit_counts", None)
    if reader is None or counter is None:
        return None
    try:
        counts = await counter(since=filters.since)
    except CampaignStoreError:
        return None
    blocked = int(counts.get("compliance.blocked", 0))
    allowed = int(counts.get("compliance.allowed", 0))
    summary: dict[str, int] = {"blocked": blocked, "allowed": allowed}
    if blocked:
        try:
            rows = await reader(action="compliance.blocked", since=filters.since, limit=500)
        except CampaignStoreError:
            rows = []
        for row in rows:
            code = str(row.detail.get("code") or row.outcome or "other")
            summary[code] = summary.get(code, 0) + 1
    return summary


async def _campaigns(store: CampaignStore, zone: tzinfo, filters: ReportFilter) -> list[dict[str, Any]]:
    """Per-campaign progress, with the result-derived columns merged in.

    Two sources because they come from tables with different lifetimes: the
    progress columns are always available, and the qualified/meeting columns
    need `call_results`. Merging here rather than joining in SQL means a
    database without that table loses two columns instead of the whole table.
    """
    rows = await store.campaign_overview(limit=RECENT_CAMPAIGNS, campaign_id=filters.campaign_id)
    extra = await store.campaign_result_counts(**filters.scope())

    campaigns = []
    for row in rows:
        results = (extra or {}).get(row["id"], {})
        attempts = row["attempts"]
        total = row["prospects"]
        pending = row["pending"]
        in_progress = row.get("in_progress", 0)
        done = total - pending - in_progress
        campaigns.append(
            {
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "created_at_label": _when(row["created_at"], zone),
                "prospects": total,
                "pending": pending,
                "in_progress": in_progress,
                "completed_members": row.get("completed_members", 0),
                "exhausted": row.get("exhausted", 0),
                "skipped": row.get("skipped", 0),
                "attempts": attempts,
                "answered": row["answered"],
                "failed": row["failed"],
                "voicemail": row.get("voicemail", 0),
                "live": row["live"],
                # A rate over nothing is not 0%, it is nothing to say.
                "answer_rate": round(row["answered"] * 100 / attempts) if attempts else None,
                "average_duration": _duration(row["average_duration_secs"]),
                "qualified": results.get("qualified") if extra is not None else None,
                "meetings": results.get("meetings") if extra is not None else None,
                "transferred": results.get("transferred") if extra is not None else None,
                "opted_out": results.get("opted_out") if extra is not None else None,
                "results_available": extra is not None,
                # Phase 20.
                "progress_pct": round(done * 100 / total) if total else None,
                "remaining": pending,
            }
        )
    return campaigns


async def _recent_calls(
    store: CampaignStore,
    zone: tzinfo,
    *,
    filters: ReportFilter | None = None,
    limit: int = RECENT_CALLS,
    search: str | None = None,
    search_phone: bool = False,
    status: str | None = None,
    before_id: int | None = None,
    prospect_id: int | None = None,
) -> list[dict[str, Any]]:
    """The newest calls, with the outcome each one produced.

    The attempt rows and their results are fetched in two queries rather than
    one per row: fifteen rows would otherwise be sixteen round trips, which is
    the difference between a dashboard that feels instant and one that does not.
    """
    filters = filters or ReportFilter()
    rows = await store.recent_call_rows(
        limit=limit,
        campaign_id=filters.campaign_id,
        since=filters.since,
        until=filters.until,
        prospect_id=prospect_id,
        status=status,
        before_id=before_id,
        search=search,
        search_phone=search_phone,
    )
    results = await store.results_for_attempts([int(row["id"]) for row in rows])

    calls = []
    for row in rows:
        result = (results or {}).get(int(row["id"]), {})
        name = " ".join(part for part in (row["first_name"], row["last_name"]) if part).strip()
        calls.append(
            {
                "attempt_id": int(row["id"]),
                "at_label": _when(row["at"], zone),
                "at": row["at"].isoformat() if row["at"] else None,
                "prospect_id": int(row["prospect_id"]),
                "prospect": name or f"prospect {row['prospect_id']}",
                "company": row["company"],
                "phone": row["phone"],
                "campaign_id": row["campaign_id"],
                "campaign": row["campaign_name"],
                "status": row["status"],
                "attempt_number": row["attempt_number"],
                "duration": _duration(row["duration_seconds"]),
                "provider": row["telephony_provider"],
                # The disposition is the better answer where there is one; the
                # attempt status is what a call still in flight has.
                "disposition": result.get("disposition") or row["status"],
                "disposition_label": _humanise(result.get("disposition") or row["status"]),
                "tone": _tone_for(result.get("disposition") or row["status"]),
                "qualification": result.get("qualification_status"),
                "qualification_label": _humanise(result.get("qualification_status")) if result.get("qualification_status") else None,
                "meeting_status": result.get("meeting_status"),
                "callback_status": result.get("callback_status"),
                "next_action": result.get("next_action"),
                "headline": _headline(result.get("summary_text"), row["failure_reason"]),
                "has_result": bool(result),
            }
        )
    return calls


async def list_calls(
    store: CampaignStore,
    *,
    timezone: str = "UTC",
    filters: ReportFilter | None = None,
    search: str | None = None,
    search_phone: bool = False,
    status: str | None = None,
    before_id: int | None = None,
    prospect_id: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """The calls list with its filters, search and paging. Phase 20."""
    zone = resolve_zone(timezone)
    limit = max(1, min(int(limit), MAX_CALLS_PAGE))
    rows = await _recent_calls(
        store, zone, filters=filters, limit=limit, search=search, search_phone=search_phone,
        status=status, before_id=before_id, prospect_id=prospect_id,
    )
    return {
        "calls": rows,
        "count": len(rows),
        "next_before_id": rows[-1]["attempt_id"] if len(rows) >= limit else None,
        "filters": (filters or ReportFilter()).to_dict(),
        "search": search or None,
        "phone_searched": bool(search_phone and search and any(ch.isdigit() for ch in search)),
    }


async def search_people(
    store: CampaignStore, query: str, *, limit: int = 20, search_phone: bool = False
) -> list[dict[str, Any]]:
    """Prospects matching a search, for the search box. Phase 20."""
    rows = await store.search_prospects(query, limit=limit, search_phone=search_phone)
    return [
        {
            "prospect_id": p.id,
            "name": p.full_name,
            "company": p.company,
            "phone": p.phone_normalized or p.phone,
            "email": p.email,
            "status": p.status.value,
        }
        for p in rows
    ]


async def call_detail(
    store: CampaignStore, attempt_id: int, *, timezone: str = "UTC", include_transcript: bool = False
) -> dict[str, Any] | None:
    """Everything about one call, for the detail page. Phase 20.

    Keyed reads only — the attempt, its result, its transfers, the person,
    the campaign, their callbacks and meetings, the list entry, the usage —
    each by primary or unique key, so the page costs a handful of index
    lookups whatever the table sizes.

    Args:
        include_transcript: Whether the transcript, the conversation record
            and the full quality report travel. `False` for a viewer: the
            answer says `transcript_included: false` and carries the summary,
            the outcome and the figures instead.

    Returns:
        None when there is no such attempt.
    """
    attempt_dict = campaign_dict = prospect_dict = transfer_dict = callback_dict = meeting_dict = _plain

    def result_dict(found: Any, *, include_transcript: bool) -> dict[str, Any]:
        data = found.to_dict()
        data["transcript"] = [dict(turn) for turn in found.transcript] if include_transcript else None
        data["transcript_included"] = include_transcript
        data["summary_text"] = found.summary.text
        return data

    zone = resolve_zone(timezone)
    attempt = await store.get_attempt(attempt_id)
    if attempt is None:
        return None
    result, transfers, prospect, campaign, usage, callbacks, meetings, entry = await asyncio.gather(
        _quiet(store.get_call_result(attempt_id)),
        _quiet(store.list_transfers(call_attempt_id=attempt_id)),
        store.get_prospect(attempt.prospect_id),
        store.get_campaign(attempt.campaign_id) if attempt.campaign_id else _none(),
        _quiet(store.get_attempt_usage(attempt_id)),
        _quiet(store.list_callbacks(prospect_id=attempt.prospect_id, status=None, limit=20)),
        _quiet(store.list_meetings(prospect_id=attempt.prospect_id, status=None, limit=20)),
        _quiet(_find_dnc(store, prospect_number(await store.get_prospect(attempt.prospect_id)))),
    )
    usage_record, cost = usage if isinstance(usage, tuple) else (None, None)
    quality = (usage_record or {}).get("quality") if isinstance(usage_record, dict) else None
    conversation = attempt.conversation_data if include_transcript else None
    full_quality = conversation.get("quality") if isinstance(conversation, dict) else None

    detail: dict[str, Any] = {
        "call": attempt_dict(attempt),
        "call_labels": {
            "status": _humanise(attempt.status.value),
            "at": _when(attempt.started_at or attempt.created_at, zone),
            "connected_at": _when(attempt.connected_at, zone) if attempt.connected_at else None,
            "ended_at": _when(attempt.ended_at, zone) if attempt.ended_at else None,
            "duration": _duration(attempt.duration_seconds),
            "tone": _tone_for(result.disposition.value if result else attempt.status.value),
        },
        "prospect": prospect_dict(prospect) if prospect else None,
        "campaign": campaign_dict(campaign) if campaign else None,
        "result": result_dict(result, include_transcript=include_transcript) if result else None,
        "result_labels": (
            {
                "disposition": _humanise(result.disposition.value),
                "qualification": _humanise(result.qualification_status.value),
                "meeting": _humanise(result.meeting_status.value),
                "callback": _humanise(result.callback_status.value),
                "next_action": _humanise(result.next_action.value),
                "interest": _humanise(result.interest_level.value),
                "meeting_start": _when(result.meeting_start, zone) if result.meeting_start else None,
                "callback_for": _when(result.callback_scheduled_for, zone) if result.callback_scheduled_for else None,
            }
            if result
            else None
        ),
        "transcript_included": bool(include_transcript and result and result.transcript),
        "transcript_available": bool(result and result.transcript),
        "transfers": [transfer_dict(t) for t in (transfers or [])],
        "callbacks": [callback_dict(c) for c in (callbacks or [])],
        "meetings": [meeting_dict(m) for m in (meetings or [])],
        "dnc": entry.to_dict() if entry else None,
        "usage": _usage_detail(usage_record, cost),
        "quality": _quality_detail(quality, full_quality),
        "conversation": conversation if include_transcript else None,
    }
    return detail


def _usage_detail(usage: dict[str, Any] | None, cost: float | None) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    llm = usage.get("llm") if isinstance(usage.get("llm"), dict) else {}
    tts = usage.get("tts") if isinstance(usage.get("tts"), dict) else {}
    stt = usage.get("stt") if isinstance(usage.get("stt"), dict) else {}
    cost_record = usage.get("cost") if isinstance(usage.get("cost"), dict) else None
    return {
        "llm_requests": llm.get("requests"),
        "prompt_tokens": llm.get("prompt_tokens"),
        "completion_tokens": llm.get("completion_tokens"),
        "llm_models": [m.get("model") for m in llm.get("models", []) if isinstance(m, dict)],
        "tts_characters": tts.get("characters"),
        "tts_reported": tts.get("reported"),
        "stt_audio_seconds": stt.get("audio_seconds"),
        "stt_reported": stt.get("reported"),
        "cost_usd": cost,
        "cost_breakdown": cost_record,
    }


def _quality_detail(summary: dict[str, Any] | None, full: dict[str, Any] | None) -> dict[str, Any] | None:
    """The per-call figures for the page: the summary for everybody, the report with the transcript."""
    if not summary and not full:
        return None
    out: dict[str, Any] = dict(summary or {})
    if isinstance(full, dict):
        out["report"] = {
            k: v
            for k, v in full.items()
            # The turns and barge-ins carry what was said; they travel only
            # with the transcript, and `full` is None without it.
            if k in ("duration_secs", "greeted", "greeting_at_secs", "caller_turns", "agent_turns",
                     "barge_in_count", "spurious_interruptions", "noise_resumes", "failed_turn_count",
                     "late_turn_count", "stop_timeouts", "errors", "latency", "voicemail", "turns",
                     "barge_ins", "failed_turns")
        }
    return out


def prospect_number(prospect: Any) -> str | None:
    return getattr(prospect, "phone_normalized", None) if prospect is not None else None


async def _find_dnc(store: CampaignStore, number: str | None) -> Any:
    finder = getattr(store, "find_dnc", None)
    if finder is None or not number:
        return None
    return await finder(number)


async def _none() -> None:
    return None


def _plain(value: Any) -> Any:
    """A dataclass (or anything JSON-shaped) as plain data: enums to values, datetimes to ISO.

    The dashboard's own serializer, so `src/dashboard/` imports nothing from
    `src/automation/` — the automation checks assert that the call path and
    the reporting path stay clear of the API package, and this module is on
    the reporting path.
    """
    import dataclasses
    from enum import Enum

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return value


async def _quiet(operation: Any) -> Any:
    """Await a read against an optional table; None when it is missing."""
    try:
        return await operation
    except CampaignStoreError as exc:
        if "does not exist" in str(exc):
            return None
        raise


_TONES = {
    "MEETING_BOOKED": "good",
    "QUALIFIED": "good",
    "TRANSFERRED": "good",
    "CALLBACK_REQUESTED": "warn",
    "NOT_INTERESTED": "bad",
    "UNQUALIFIED": "bad",
    "DO_NOT_CALL": "bad",
    "OPTED_OUT": "bad",
    "FAILED": "bad",
    "NO_ANSWER": "warn",
    "BUSY": "warn",
    "VOICEMAIL": "warn",
    "UNRESOLVED": "bad",
}


def _tone_for(name: str | None) -> str:
    """The colour hint for a disposition or status."""
    return _TONES.get(str(name or "").upper(), "neutral")


def _humanise(name: str | None) -> str:
    """`MEETING_BOOKED` → `Meeting booked`."""
    text = str(name or "").replace("_", " ").strip().lower()
    return text[:1].upper() + text[1:] if text else "Unknown"


def _duration(seconds: float | int | None) -> str | None:
    """Seconds as `4m 12s`, or None when there is nothing to show."""
    if seconds is None:
        return None
    total = int(round(float(seconds)))
    minutes, rest = divmod(total, 60)
    if minutes and rest:
        return f"{minutes}m {rest}s"
    if minutes:
        return f"{minutes}m"
    return f"{rest}s"


def _when(moment: datetime | None, zone: tzinfo) -> str:
    """A timestamp in the campaign timezone, or an em dash.

    Formatted here rather than in the browser so that every reader sees the
    same zone — the one the agent used when it told a prospect a time, and the
    one `campaign.py` prints in. A dashboard showing meeting times in the
    viewer's local zone and the CLI showing them in the campaign's would be two
    answers to one question.
    """
    if moment is None:
        return "—"
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return f"{aware.astimezone(zone):%d %b %H:%M}"


def _headline(summary_text: str | None, failure_reason: str | None) -> str:
    """One line about what happened, from the call's own summary.

    The summary's first part is "What happened", which is exactly this line, so
    it is taken rather than rewritten — the dashboard must not become a second
    place where a call is described.
    """
    if summary_text:
        first = summary_text.splitlines()[0]
        return first.removeprefix("What happened:").strip()[:200]
    if failure_reason:
        return failure_reason.strip()[:200]
    return ""


def _join(*parts: str) -> str:
    """Join the non-empty parts with commas."""
    return ", ".join(part for part in parts if part)


__all__ = [
    "MAX_CALLS_PAGE",
    "RECENT_CALLS",
    "RECENT_CAMPAIGNS",
    "Metric",
    "ReportFilter",
    "Snapshot",
    "call_detail",
    "collect",
    "list_calls",
    "search_people",
]
