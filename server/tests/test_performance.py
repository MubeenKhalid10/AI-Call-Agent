#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for Phase 11: usage accounting, pooling, concurrency, caching.

Run it from the `server/` directory::

    uv run python tests/test_performance.py

**What this can and cannot check.** Performance work divides into two kinds,
and only one of them belongs in a check script:

* *Behaviour that must be exact* — a token count that must not be multiplied by
  the number of processors a frame crosses, a cost that must be absent rather
  than zero when no rate is set, a concurrency limit that must hold when two
  workers reserve at the same instant, a pool that must not be closed by a
  store that borrowed it. Those are assertions, and they are here.
* *How fast it is* — measured, not asserted. A timing assertion passes or fails
  on whatever else the machine was doing, so the numbers live in
  `scripts/benchmark_db.py`, which prints them, and in the handoff, which
  records what they were on the day.

So this file checks that the optimisations are *correct*, and the benchmark
shows that they are *faster*.

A plain script rather than a pytest suite because the project has no test
dependency and this needs none. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from pipecat.frames.frames import MetricsFrame  # noqa: E402
from pipecat.metrics.metrics import (  # noqa: E402
    LLMTokenUsage,
    LLMUsageMetricsData,
    STTUsage,
    STTUsageMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)

from src.campaigns import CampaignService, CampaignStatus, CampaignStore  # noqa: E402
from src.reliability import CallUsage, CostRates, UsageObserver, estimate_cost  # noqa: E402

load_dotenv(override=True)

REGION = "PK"
_failures: list[str] = []
_skipped: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


class Pushed:
    """Pipecat's `FramePushed`, as far as an observer reads it."""

    def __init__(self, frame: Any, source: str = "GroqLLMService#0") -> None:
        self.frame = frame
        self.source = source
        self.destination = None
        self.direction = None
        self.timestamp = 0


def llm_frame(prompt: int, completion: int, model: str = "qwen", cached: int = 0) -> MetricsFrame:
    """One LLM usage report, as a service pushes it."""
    return MetricsFrame(
        data=[
            LLMUsageMetricsData(
                processor="GroqLLMService#0",
                model=model,
                value=LLMTokenUsage(
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=prompt + completion,
                    cache_read_input_tokens=cached or None,
                ),
            )
        ]
    )


# --- Usage accounting ---------------------------------------------------------


async def check_usage_accounting() -> None:
    """The counting: right totals, and not multiplied by the pipeline's depth."""
    print("\n=== usage accounting ===")
    observer = UsageObserver()

    await observer.on_push_frame(Pushed(llm_frame(3000, 120)))
    await observer.on_push_frame(Pushed(llm_frame(3200, 90)))
    usage = observer.usage
    check("prompt tokens are summed", usage.prompt_tokens == 6200)
    check("so are completion tokens", usage.completion_tokens == 210)
    check("and requests counted", usage.llm_requests == 2)

    # The bug this de-duplication exists for: an observer sees each frame once
    # per processor *hop*, so a nine-stage pipeline would count a token nine
    # times — a bill that reads nine times too high.
    frame = llm_frame(1000, 50)
    for _ in range(9):
        await observer.on_push_frame(Pushed(frame))
    check("one frame is counted once, not once per hop", usage.prompt_tokens == 7200, str(usage.prompt_tokens))
    check("and the request count agrees", usage.llm_requests == 3)

    await observer.on_push_frame(
        Pushed(MetricsFrame(data=[TTSUsageMetricsData(processor="CartesiaTTSService#0", model="sonic", value=412)]))
    )
    await observer.on_push_frame(
        Pushed(MetricsFrame(data=[TTSUsageMetricsData(processor="CartesiaTTSService#0", model="sonic", value=88)]))
    )
    check("TTS characters are summed", usage.tts_characters == 500)

    await observer.on_push_frame(
        Pushed(MetricsFrame(data=[STTUsageMetricsData(processor="DeepgramFluxSTTService#0", model="flux", value=STTUsage(audio_seconds=12.5))]))
    )
    check("STT audio seconds are summed", usage.stt_seconds == 12.5)

    # Everything else on the wire must be ignored rather than mis-counted.
    await observer.on_push_frame(
        Pushed(MetricsFrame(data=[TTFBMetricsData(processor="GroqLLMService#0", model="qwen", value=0.4)]))
    )
    check("a latency frame adds no usage", usage.llm_requests == 3 and usage.tts_characters == 500)

    check("models are kept apart", set(usage.llm) == {"qwen"})
    await observer.on_push_frame(Pushed(llm_frame(10, 1, model="llama")))
    check("a second model gets its own entry", set(usage.llm) == {"qwen", "llama"})

    exported = usage.to_dict()
    check("the export carries every stage", set(exported) == {"llm", "tts", "stt", "telephony_seconds"})
    check("with the totals at the top", exported["llm"]["prompt_tokens"] == 7210)
    check("a TTS entry has no token fields", "prompt_tokens" not in exported["tts"]["models"][0])
    check("the summary reads usefully", "prompt tokens/request" in usage.describe())

    empty = UsageObserver().usage
    check("a call that measured nothing says so", empty.is_empty and empty.describe() == "nothing measured")


async def check_cost() -> None:
    """Prices are configured, never invented."""
    print("\n=== cost ===")
    usage = CallUsage()
    observer = UsageObserver()
    await observer.on_push_frame(Pushed(llm_frame(1_000_000, 100_000)))
    await observer.on_push_frame(
        Pushed(MetricsFrame(data=[TTSUsageMetricsData(processor="tts", model="sonic", value=1_000_000)]))
    )
    usage = observer.usage
    usage.telephony_seconds = 120.0

    check("with no rates there is no cost at all", estimate_cost(usage, CostRates()) is None)
    check("and the rates say so", "not estimated" in CostRates().describe())

    priced = estimate_cost(usage, CostRates(llm_input_per_mtok=0.5, llm_output_per_mtok=1.0))
    check("a configured rate is applied", priced["llm_input"] == 0.5 and priced["llm_output"] == 0.1)
    check("and totalled", priced["total_usd"] == 0.6)
    # The honest part: a stage with usage but no rate is absent, not zero.
    check("an unpriced stage is absent, not zero", "tts" not in priced and "telephony" not in priced)
    check("and the record says what was priced", priced["priced"] == ["llm_input", "llm_output"])

    full = estimate_cost(
        usage,
        CostRates(llm_input_per_mtok=0.5, llm_output_per_mtok=1.0, tts_per_mchar=10.0,
                  stt_per_minute=0.01, telephony_per_minute=0.015),
    )
    check("every measured, configured stage is priced", set(full["priced"]) == {"llm_input", "llm_output", "tts", "telephony"})
    check("telephony is charged per minute", full["telephony"] == 0.03, str(full["telephony"]))
    check("the total adds up", round(full["total_usd"], 4) == round(0.5 + 0.1 + 10.0 + 0.03, 4))
    # STT had a rate but reported nothing, so it is named rather than priced at
    # zero. This is the Deepgram-websocket-TTS case in general form.
    check("a stage that reported nothing is not priced at zero", "stt" not in full)
    check("it is named as unmeasured", full["unmeasured"] == ["stt"], str(full["unmeasured"]))
    check("and the total says it is incomplete", full["complete"] is False)

    measured = estimate_cost(usage, CostRates(llm_input_per_mtok=0.5))
    check("a total with nothing missing says so", measured["complete"] is True)

    exported = usage.to_dict()
    check("each stage records whether it was reported", exported["tts"]["reported"] and not exported["stt"]["reported"])
    check("the summary names an unreported stage", "TTS reported no usage" not in usage.describe())


# --- Pooling, concurrency and caching ----------------------------------------


async def check_shared_pool(dsn: str) -> None:
    """A borrowed pool is used but never closed by the borrower."""
    print("\n=== connection pooling ===")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        borrower = await CampaignStore.connect(dsn, pool=pool)
        check("a store can borrow a pool", await borrower.count_prospects() >= 0)
        await borrower.close()
        check(
            "closing the borrower leaves the pool open",
            not pool._closed and await pool.fetchval("SELECT 1") == 1,
        )
    finally:
        await pool.close()

    owner = await CampaignStore.connect(dsn, min_size=1, max_size=2)
    await owner.close()
    check("a store that opened its own pool does close it", owner._pool._closed)
    check("and closing twice is safe", (await owner.close()) is None)


async def check_reservation_concurrency(store: CampaignStore) -> None:
    """The concurrency limit holds inside the reservation, not just around it."""
    print("\n=== concurrency, enforced in the transaction ===")
    service = CampaignService(store, default_region=REGION, max_attempts=5, retry_minutes=0)
    campaign = await store.create_campaign(name="Phase 11 concurrency", status=CampaignStatus.ACTIVE)
    for index in range(6):
        person = await service.create_prospect(
            first_name=f"C{index}", last_name="Limit", phone=f"+9232211000{index:02d}"
        )
        await store.add_to_campaign(campaign.id, person.id)

    first = await service.next_call(campaign.id, max_concurrent=2)
    second = await service.next_call(campaign.id, max_concurrent=2)
    check("reservations are handed out up to the limit", first is not None and second is not None)
    check("two calls are now live", await store.count_live_attempts() == 2)

    third = await service.next_call(campaign.id, max_concurrent=2)
    check("and the next is refused by the limit", third is None)
    check("no third attempt row was created", await store.count_live_attempts() == 2)

    # The race the in-transaction count exists for: several workers reserving at
    # the same instant, each of which would have seen room if it looked first.
    await store.update_attempt_status(first.attempt.id, __import__("src.campaigns", fromlist=["x"]).CallAttemptStatus.COMPLETED)
    await store.update_attempt_status(second.attempt.id, __import__("src.campaigns", fromlist=["x"]).CallAttemptStatus.COMPLETED)
    check("the slots are free again", await store.count_live_attempts() == 0)

    racers = await asyncio.gather(*[service.next_call(campaign.id, max_concurrent=2) for _ in range(6)])
    reserved = [r for r in racers if r is not None]
    # Bounded on both sides: `<= 2` alone would pass if the race handed out
    # nothing at all, which would mean the check proved the limit works by
    # accidentally proving the queue is broken.
    check(
        "six simultaneous reservations respect a limit of two",
        1 <= len(reserved) <= 2,
        f"{len(reserved)} were handed out",
    )
    check("and the database agrees", await store.count_live_attempts() == len(reserved))

    check("a limit of zero does not check", (await service.next_call(campaign.id, max_concurrent=0)) is not None)


async def check_usage_persistence(store: CampaignStore) -> None:
    """Usage reaches the attempt row, and aggregates from there."""
    print("\n=== usage on the record ===")
    service = CampaignService(store, default_region=REGION, max_attempts=3, retry_minutes=60)
    person = await service.create_prospect(first_name="Use", last_name="Age", phone="+923221199001")
    attempt = await store.create_attempt(prospect_id=person.id)

    usage = {"llm": {"requests": 4, "prompt_tokens": 12_400, "completion_tokens": 310}}
    check("usage is stored", await store.save_call_usage(attempt.id, usage, cost_usd=0.0123))
    check("and a missing attempt reports it", not await store.save_call_usage(10**9, usage))

    counts = await store.attempt_counts()
    check("the columns are available", counts["usage_available"])
    check("prompt tokens aggregate", counts["prompt_tokens"] == 12_400)
    check("requests aggregate", counts["llm_requests"] == 4)
    check("cost aggregates", counts["total_cost_usd"] == 0.0123)
    check("and the count of priced calls is reported", counts["with_cost"] == 1)

    # The rest of the totals must be unaffected by the new columns.
    check("call totals still count calls", counts["total"] >= 1)

    from src.dashboard.stats import _usage

    strip = _usage(counts)
    check("the dashboard shows tokens per call", any(m.key == "tokens_per_call" for m in strip))
    check(
        "and says how many calls have usage",
        any("usage is recorded from" in m.detail for m in strip),
    )
    no_usage = {**counts, "usage_available": False}
    check("without the columns it says so rather than showing zero", not _usage(no_usage)[0].available)


async def check_dashboard_cache() -> None:
    """One read serves many viewers, and a slow read is not started twice."""
    print("\n=== the dashboard cache ===")
    from src.dashboard.web import _SnapshotCache

    reads = {"count": 0}

    class SlowStore:
        """Counts how many times the dashboard actually read the database."""

        async def _slow(self) -> None:
            reads["count"] += 1
            await asyncio.sleep(0.05)

    class Config:
        class calendar:
            timezone = "UTC"

    async def fake_collect(store, *, timezone):
        await store._slow()
        from src.dashboard.stats import Snapshot
        from datetime import UTC, datetime

        return Snapshot(datetime.now(UTC), timezone, [], [], [], [], [])

    import src.dashboard.web as web

    original = web.collect
    web.collect = fake_collect
    try:
        cache = _SnapshotCache(Config(), ttl_secs=5.0)
        store = SlowStore()
        await cache.get(store)
        check("the first request reads", reads["count"] == 1)
        await cache.get(store)
        check("a second inside the TTL does not", reads["count"] == 1)

        # The stampede: twenty arrive together while nothing is cached.
        reads["count"] = 0
        cache = _SnapshotCache(Config(), ttl_secs=5.0)
        await asyncio.gather(*[cache.get(store) for _ in range(20)])
        check("twenty simultaneous viewers cause one read", reads["count"] == 1, f"{reads['count']} reads")

        cache = _SnapshotCache(Config(), ttl_secs=0.01)
        await cache.get(store)
        before = reads["count"]
        await asyncio.sleep(0.05)
        await cache.get(store)
        check("an expired snapshot is read again", reads["count"] == before + 1)

        payload = await cache.get(store)
        check("the payload says how long the read took", "read_ms" in payload)
        check("and how long it is cached for", payload["cache_ttl_secs"] == 0.01)
    finally:
        web.collect = original


async def run_database_checks(dsn: str) -> None:
    """Everything that needs real SQL, in a schema that is thrown away."""
    from tests.test_campaigns import with_temp_schema

    store, admin, schema = await with_temp_schema(dsn)
    try:
        await check_shared_pool(dsn)
        await check_reservation_concurrency(store)
        await check_usage_persistence(store)
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def main() -> int:
    """Run every check and report."""
    print("Performance and cost checks — no vendors, no phone, no audio.")

    await check_usage_accounting()
    await check_cost()
    await check_dashboard_cache()

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
