#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Measure the database queries at a scale where the difference exists. Phase 11.

    uv run python scripts/benchmark_db.py --prospects 20000 --attempts 60000

**Why this exists.** Phase 11 was told to find the actual bottlenecks before
changing anything, and the development database has five prospects in it —
at that size every query is instant and every plan is a sequential scan that
costs nothing. A query that will be slow in production is indistinguishable
from one that will not.

So this seeds a throwaway schema with a realistic amount of history, runs each
query the system actually issues, and reports the timing and the plan. It drops
the schema afterwards; the real tables are never touched.

The numbers it prints are the *before* column of Phase 11's report, and running
it again after a change is the *after*.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-this-benchmark")
os.environ["KB_ENABLED"] = "false"

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from src.campaigns import CampaignStore  # noqa: E402

load_dotenv(override=True)


async def seed(pool: asyncpg.Pool, *, prospects: int, attempts: int, campaigns: int) -> None:
    """Fill the schema with a plausible history.

    Shaped like real data rather than uniformly: most attempts belong to the
    few most recent campaigns, most prospects have one or two attempts, and the
    outcome mix is roughly what a cold-calling campaign produces. A uniform
    distribution would flatter any query that filters by campaign.
    """
    print(f"  seeding {prospects} prospects, {campaigns} campaigns, {attempts} attempts…")
    await pool.execute(
        """
        INSERT INTO campaigns (name, status)
        SELECT 'Campaign ' || g, CASE WHEN g % 4 = 0 THEN 'COMPLETED' ELSE 'ACTIVE' END
        FROM generate_series(1, $1) g
        """,
        campaigns,
    )
    await pool.execute(
        """
        INSERT INTO prospects (first_name, last_name, phone, phone_normalized, status, company)
        SELECT 'First' || g, 'Last' || g, '+9230' || lpad(g::text, 8, '0'),
               '+9230' || lpad(g::text, 8, '0'),
               CASE WHEN g % 50 = 0 THEN 'DO_NOT_CALL' ELSE 'NEW' END,
               'Company ' || (g % 500)
        FROM generate_series(1, $1) g
        """,
        prospects,
    )
    await pool.execute(
        """
        INSERT INTO campaign_prospects (campaign_id, prospect_id, status, attempt_count)
        SELECT c.id, p.id,
               CASE WHEN p.id % 3 = 0 THEN 'PENDING' ELSE 'COMPLETED' END,
               p.id % 3
        FROM prospects p
        JOIN campaigns c ON c.id = 1 + (p.id % (SELECT count(*) FROM campaigns))
        ON CONFLICT DO NOTHING
        """
    )
    # The outcome mix: about a third answered, the rest no-answer, busy or failed.
    await pool.execute(
        """
        INSERT INTO call_attempts
            (prospect_id, campaign_id, campaign_prospect_id, attempt_number, status,
             duration_seconds, telephony_call_id, telephony_provider, started_at, created_at)
        SELECT m.prospect_id, m.campaign_id, m.id, 1 + (g % 3),
               (ARRAY['COMPLETED','NO_ANSWER','BUSY','FAILED','NOT_INTERESTED',
                      'CALLBACK_REQUESTED','DO_NOT_CALL'])[1 + (g % 7)],
               CASE WHEN g % 7 IN (0, 4, 5, 6) THEN 30 + (g % 300) ELSE NULL END,
               'CA' || g, 'stub',
               now() - make_interval(mins => g % 100000),
               now() - make_interval(mins => g % 100000)
        FROM generate_series(1, $1) g
        JOIN campaign_prospects m ON m.id = 1 + (g % (SELECT count(*) FROM campaign_prospects))
        """,
        attempts,
    )
    # One result per finished attempt, which is what Phase 8 guarantees.
    await pool.execute(
        """
        INSERT INTO call_results
            (call_attempt_id, prospect_id, campaign_id, source, call_status, disposition,
             duration_seconds, qualification_status, interest_level, buying_timeline,
             decision_role, next_action, meeting_status, callback_status, summary, summary_text)
        SELECT a.id, a.prospect_id, a.campaign_id, 'CONVERSATION', a.status,
               (ARRAY['COMPLETED','NO_ANSWER','BUSY','FAILED','NOT_INTERESTED',
                      'CALLBACK_REQUESTED','QUALIFIED','MEETING_BOOKED'])[1 + (a.id % 8)],
               a.duration_seconds,
               (ARRAY['UNKNOWN','QUALIFIED','PARTIALLY_QUALIFIED','DISQUALIFIED'])[1 + (a.id % 4)],
               'UNKNOWN', 'UNKNOWN', 'UNKNOWN', 'UNKNOWN',
               CASE WHEN a.id % 8 = 7 THEN 'BOOKED' ELSE 'UNKNOWN' END,
               'UNKNOWN', '{}'::jsonb, 'What happened: a seeded call.'
        FROM call_attempts a
        ON CONFLICT DO NOTHING
        """
    )
    await pool.execute("ANALYZE")


async def time_it(name: str, call, *, runs: int = 5) -> tuple[str, float]:
    """Run a query a few times and report the median in milliseconds.

    Median rather than mean: the first run pays for a cold cache and would drag
    an average somewhere that describes no real request.
    """
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        await call()
        samples.append((time.perf_counter() - started) * 1000)
    median = statistics.median(samples)
    flag = "  <-- SLOW" if median > 100 else ("  <- worth a look" if median > 25 else "")
    print(f"    {name:<44} {median:8.1f} ms{flag}")
    return name, median


async def explain(pool: asyncpg.Pool, label: str, sql: str, *args) -> None:
    """Print whether a query uses an index or scans the table."""
    plan = await pool.fetch(f"EXPLAIN (ANALYZE, BUFFERS) {sql}", *args)
    lines = [row["QUERY PLAN"] for row in plan]
    scans = [line.strip() for line in lines if "Seq Scan" in line]
    top = lines[0].strip()
    print(f"    {label:<44} {top[:90]}")
    for scan in scans[:3]:
        print(f"      {'seq scan:':<12} {scan[:88]}")


async def main() -> int:
    """Seed, measure, report, drop."""
    parser = argparse.ArgumentParser(description="Measure the campaign queries at scale.")
    parser.add_argument("--prospects", type=int, default=20000)
    parser.add_argument("--attempts", type=int, default=60000)
    parser.add_argument("--campaigns", type=int, default=12)
    args = parser.parse_args()

    dsn = os.getenv("DATABASE_URL") or os.getenv("KB_DATABASE_URL")
    if not dsn:
        print("No DATABASE_URL; nothing to measure against.", file=sys.stderr)
        return 1

    schema = f"bench_{int(time.time())}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def use_schema(connection: asyncpg.Connection) -> None:
        await connection.execute(f'SET search_path TO "{schema}"')

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=6, setup=use_schema)
    store = CampaignStore(pool)
    try:
        await store.create_schema()
        await seed(pool, prospects=args.prospects, attempts=args.attempts, campaigns=args.campaigns)

        size = await pool.fetchval(
            f"SELECT pg_size_pretty(sum(pg_total_relation_size(c.oid))) "
            f"FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = '{schema}'"
        )
        counts = await pool.fetchrow(
            "SELECT (SELECT count(*) FROM prospects) AS p, "
            "       (SELECT count(*) FROM call_attempts) AS a, "
            "       (SELECT count(*) FROM call_results) AS r"
        )
        print(f"\n  {counts['p']} prospects, {counts['a']} attempts, {counts['r']} results, {size}\n")

        print("  THE DIALER'S HOT PATH")
        await time_it("reserve_next_call (the queue)", lambda: store.reserve_next_call(1, max_attempts=3))
        await time_it("has_live_attempt", lambda: store.has_live_attempt(7))
        await time_it("count_live_attempts (concurrency)", lambda: store.count_live_attempts())
        await time_it("list_live_attempts (recovery)", lambda: store.list_live_attempts(older_than_secs=60))
        await time_it("get_attempt", lambda: store.get_attempt(50))
        await time_it("find_attempt_by_call_id", lambda: store.find_attempt_by_call_id("CA500"))

        print("\n  THE BOT'S PER-CALL PATH")
        await time_it("get_prospect", lambda: store.get_prospect(100))
        await time_it("list_attempts (prospect history, limit 5)",
                      lambda: store.list_attempts(prospect_id=100, limit=5))
        await time_it("save_conversation_data", lambda: store.save_conversation_data(1, {"x": 1}))

        print("\n  THE DASHBOARD")
        await time_it("prospect_counts", lambda: store.prospect_counts())
        await time_it("attempt_counts", lambda: store.attempt_counts())
        await time_it("result_counts", lambda: store.result_counts())
        await time_it("disposition_counts", lambda: store.disposition_counts())
        await time_it("meeting_counts", lambda: store.meeting_counts())
        await time_it("callback_counts", lambda: store.callback_counts())
        await time_it("campaign_overview", lambda: store.campaign_overview(limit=8))
        await time_it("campaign_result_counts", lambda: store.campaign_result_counts())
        await time_it("recent_call_rows", lambda: store.recent_call_rows(limit=15))
        await time_it("list_call_results", lambda: store.list_call_results(limit=50))

        print("\n  PLANS (looking for sequential scans)")
        await explain(pool, "queue reservation",
                      "SELECT m.id FROM campaign_prospects m "
                      "JOIN campaigns c ON c.id = m.campaign_id "
                      "JOIN prospects p ON p.id = m.prospect_id "
                      "WHERE m.campaign_id = 1 AND c.status = 'ACTIVE' AND m.status = 'PENDING' "
                      "AND p.status <> 'DO_NOT_CALL' AND p.phone_normalized IS NOT NULL "
                      "AND m.attempt_count < 3 ORDER BY m.next_attempt_at NULLS FIRST, m.id LIMIT 1")
        await explain(pool, "attempt totals", "SELECT count(*) FROM call_attempts")
        await explain(pool, "disposition group-by",
                      "SELECT disposition, count(*) FROM call_results GROUP BY disposition")
        await explain(pool, "qualified distinct prospects",
                      "SELECT count(DISTINCT prospect_id) FROM call_results "
                      "WHERE qualification_status = 'QUALIFIED'")
        await explain(pool, "recent calls", "SELECT a.id FROM call_attempts a "
                      "JOIN prospects p ON p.id = a.prospect_id ORDER BY a.id DESC LIMIT 15")
        await explain(pool, "prospect history",
                      "SELECT * FROM call_attempts WHERE prospect_id = 100 ORDER BY id DESC LIMIT 5")
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()
    print("\n  (schema dropped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
