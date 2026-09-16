#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Prospects, campaigns and the call queue, from the command line.

The service layer for Phase 5. This project has no web API of its own — the only
HTTP server is Pipecat's dev runner, which exists to host the bot — so the shape
that fits is the one `ingest.py` already established: business logic in
`src/campaigns/`, and a thin argparse front end over it. Everything here is a few
lines calling `CampaignService`; when a web or n8n front end arrives it will call
the same service, not this file.

Run it from the `server/` directory::

    uv run campaign.py init                                  # create the tables, once
    uv run campaign.py import prospects.csv --dry-run        # check the mapping first
    uv run campaign.py import prospects.csv
    uv run campaign.py create "Q1 Outreach" --description "..."
    uv run campaign.py add "Q1 Outreach" --all
    uv run campaign.py start "Q1 Outreach"
    uv run campaign.py next "Q1 Outreach"                    # who is up, without dialling
    uv run campaign.py call "Q1 Outreach"                    # actually place one call
    uv run campaign.py status "Q1 Outreach"
    uv run campaign.py prospects
    uv run campaign.py attempts --campaign "Q1 Outreach"
    uv run campaign.py dnc 42                                # never call prospect 42 again
    uv run campaign.py callbacks --due                       # who asked to be called back, and is due
    uv run campaign.py cancel-callback 7
    uv run campaign.py meetings                              # what the agent has booked (Phase 7)
    uv run campaign.py results --campaign "Q1 Outreach"      # what every finished call produced (Phase 8)
    uv run campaign.py result 12 --transcript                # one call's full result, with the transcript
    uv run campaign.py result 12 --json                      # the same, as the CRM-ready export
    uv run campaign.py rebuild-results                       # results for finished attempts that have none
    uv run campaign.py recover                               # resolve attempts left live by a crash (Phase 9)
    uv run campaign.py run                                   # place calls unattended for every ACTIVE campaign (Phase 13)
    uv run campaign.py run "Q1 Outreach" --max-calls 20      # ... one campaign, stopping after twenty

`--dry-run` on `import` is the "review the mapping before importing" step: it
prints which column became which field, which columns were kept as custom data,
and every row it would reject and why — and writes nothing.

`next` and `call` differ in one way that matters: `next` shows you who the queue
would choose and reserves nothing, while `call` reserves and dials. Use `next`
to understand the queue; use `call` when you mean it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.campaigns import (
    AttemptRecovery,
    CallbackStatus,
    CallResult,
    CallResultValidationError,
    CampaignDialer,
    CampaignService,
    CampaignStatus,
    CampaignStore,
    CampaignStoreError,
    CampaignWorker,
    Disposition,
    ProspectStatus,
    attempt_status_for,
    build_carrier_result,
    build_conversation_result,
    install_signal_handlers,
    map_headers,
    parse_csv,
)
from src.campaigns.runtime import build_gate, build_guards, build_service, build_worker
from src.compliance import CONFIG_KEY as COMPLIANCE_KEY
from src.compliance import ComplianceGate, DncSource, parse_source
from src.config import Config, ConfigError
from src.monitoring import REGISTRY
from src.monitoring.collect import refresh_from_store
from src.monitoring.http import (
    HEALTH_PATH,
    METRICS_PATH,
    READY_PATH,
    Readiness,
    ReadyCheck,
    create_ops_app,
    serve_ops,
    store_ready,
)
from src.reliability import (
    CallingWindow,
    CampaignGuards,
    PacingLimiter,
    configure_logging,
    jittered,
)
from src.telephony import TelephonyError, make_provider

load_dotenv(override=True)
# Phase 9: credentials scrubbed from every line, and `LOG_FORMAT=json` honoured.
configure_logging(component="scheduler")

EXIT_OK = 0
EXIT_FAILED = 1


async def _store(config: Config, *, create_schema: bool = False) -> CampaignStore:
    """Open the campaign database, or fail with a message naming the setting."""
    if not config.database_url:
        raise CampaignStoreError(
            "No database is configured.\n"
            "  Set DATABASE_URL (or KB_DATABASE_URL, which it defaults to) to a PostgreSQL\n"
            "  database, e.g. postgresql://postgres:PASSWORD@localhost:5432/voice_agent"
        )
    return await CampaignStore.connect(config.database_url, create_schema=create_schema)


def _service(config: Config, store: CampaignStore) -> CampaignService:
    """Build the service with the calling rules from config (Phase 25: shared with the application)."""
    return build_service(config, store)


def _gate(config: Config, service: CampaignService) -> ComplianceGate:
    """The compliance gate every dial from this file passes through. Phase 19.

    Always supplied alongside `_guards`: the list, the person, the campaign's
    rules under the number's policy, the window — and a row per decision.
    Phase 25: assembled by `src/campaigns/runtime.py`, which the unified
    application's engine shares.
    """
    return build_gate(config, service)


def _guards(config: Config) -> CampaignGuards:
    """The calling-hours, concurrency and pacing limits from config. Phase 9.

    Always supplied to the dialer from this file. A campaign placed from the
    command line is still a campaign placed at a real person's phone, and the
    limits are not something a caller should have to remember to opt into.
    """
    return build_guards(config)


async def _resolve_campaign(store: CampaignStore, reference: str):
    """Find a campaign by id or by name, so commands can take either."""
    if reference.isdigit():
        campaign = await store.get_campaign(int(reference))
        if campaign:
            return campaign
    return await store.find_campaign_by_name(reference)


# --- Commands ---------------------------------------------------------------


async def command_init(config: Config, args: argparse.Namespace) -> int:
    """Create the campaign tables."""
    store = await _store(config, create_schema=True)
    try:
        prospects = await store.count_prospects()
        campaigns = await store.list_campaigns(limit=1000)
    finally:
        await store.close()
    print(
        f"Campaign tables ready: {prospects} prospect(s), {len(campaigns)} campaign(s)."
        f"\nPhone numbers are normalised for "
        f"{config.default_phone_region or 'international format only (set DEFAULT_PHONE_REGION)'}."
    )
    return EXIT_OK


async def command_import(config: Config, args: argparse.Namespace) -> int:
    """Import prospects from a CSV, optionally straight into a campaign."""
    path = Path(args.path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"Cannot read {path}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    # Parsed once here purely to show the mapping before anything is written;
    # the service parses again when it imports. Cheap, and it keeps the service
    # free of any assumption that somebody is watching.
    preview = parse_csv(text, default_region=config.default_phone_region)
    if preview.error:
        print(f"Cannot import {path.name}: {preview.error}", file=sys.stderr)
        return EXIT_FAILED

    print(f"Column mapping for {path.name}:")
    for line in preview.mapping.describe():
        print(line)

    if not preview.mapping.is_usable:
        missing = ", ".join(preview.mapping.missing_required)
        print(
            f"\nCannot import: no column supplies {missing}.\n"
            f"  Rename the column in the CSV, or add one. Recognised spellings include "
            f"'First Name', 'Surname', 'Mobile Number'.",
            file=sys.stderr,
        )
        return EXIT_FAILED

    campaign = None
    if args.campaign:
        campaign = await _resolve_campaign_or_fail(config, args.campaign)
        if campaign is None:
            return EXIT_FAILED

    store = await _store(config)
    try:
        outcome = await _service(config, store).import_csv(
            text,
            campaign_id=campaign.id if campaign else None,
            dry_run=args.dry_run,
        )
    finally:
        await store.close()

    if outcome.report.invalid_rows:
        print(f"\n{len(outcome.report.invalid_rows)} row(s) will not be imported:")
        for row in outcome.report.invalid_rows[: args.show_errors]:
            print(f"  {row.describe()}")
        remaining = len(outcome.report.invalid_rows) - args.show_errors
        if remaining > 0:
            print(f"  ... and {remaining} more")

    print()
    if args.dry_run:
        print(f"Dry run: {outcome.report.summary()}. Nothing was written.")
        return EXIT_OK

    print(f"Imported: {outcome.summary()}.")
    if campaign:
        print(f"Campaign {campaign.name!r} now has {(await _counts(config, campaign.id)).total}.")
    return EXIT_OK


async def command_create(config: Config, args: argparse.Namespace) -> int:
    """Create a campaign."""
    store = await _store(config)
    try:
        campaign = await _service(config, store).create_campaign(args.name, args.description)
    except CampaignStoreError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED
    finally:
        await store.close()
    print(f"Created campaign {campaign.id}: {campaign.name!r} ({campaign.status.value})")
    print("Add prospects with:  uv run campaign.py add " + repr(campaign.name) + " --all")
    return EXIT_OK


async def command_add(config: Config, args: argparse.Namespace) -> int:
    """Add prospects to a campaign."""
    store = await _store(config)
    try:
        campaign = await _resolve_campaign(store, args.campaign)
        if campaign is None:
            print(f"No campaign called {args.campaign!r}.", file=sys.stderr)
            return EXIT_FAILED

        if args.all:
            prospects = await store.list_prospects(limit=100_000)
            ids = [p.id for p in prospects if p.status is not ProspectStatus.DO_NOT_CALL]
        else:
            ids = [int(value) for value in args.prospect_ids]

        added = await _service(config, store).add_prospects(campaign.id, ids)
        counts = await store.campaign_counts(campaign.id)
    finally:
        await store.close()

    print(f"Added {added} prospect(s) to {campaign.name!r}; {counts.total} in the campaign.")
    return EXIT_OK


async def command_status(config: Config, args: argparse.Namespace) -> int:
    """Show one campaign's progress, or list them all."""
    store = await _store(config)
    try:
        if not args.campaign:
            campaigns = await store.list_campaigns()
            if not campaigns:
                print("No campaigns yet.  Create one:  uv run campaign.py create <name>")
                return EXIT_OK
            for campaign in campaigns:
                counts = await store.campaign_counts(campaign.id)
                print(
                    f"  {campaign.id:>4}  {campaign.status.value:<10} {campaign.name!r} "
                    f"— {counts.total} prospect(s), {counts.pending} pending"
                )
            return EXIT_OK

        campaign = await _resolve_campaign(store, args.campaign)
        if campaign is None:
            print(f"No campaign called {args.campaign!r}.", file=sys.stderr)
            return EXIT_FAILED
        counts = await store.campaign_counts(campaign.id)
        attempts = await store.list_attempts(campaign_id=campaign.id, limit=1000)
    finally:
        await store.close()

    print(f"{campaign.name!r} (id {campaign.id}) — {campaign.status.value}")
    if campaign.description:
        print(f"  {campaign.description}")
    print(
        f"  prospects   {counts.total} total, {counts.pending} pending, "
        f"{counts.in_progress} in progress, {counts.completed} completed, "
        f"{counts.exhausted} exhausted, {counts.skipped} skipped"
    )
    print(f"  attempts    {len(attempts)}")
    return EXIT_OK


async def command_transition(config: Config, args: argparse.Namespace) -> int:
    """Move a campaign to a new status (start / pause / complete / cancel)."""
    store = await _store(config)
    try:
        campaign = await _resolve_campaign(store, args.campaign)
        if campaign is None:
            print(f"No campaign called {args.campaign!r}.", file=sys.stderr)
            return EXIT_FAILED
        updated = await _service(config, store).set_status(campaign.id, args.status)
    finally:
        await store.close()
    print(f"{updated.name!r} is now {updated.status.value}.")
    return EXIT_OK


async def command_next(config: Config, args: argparse.Namespace) -> int:
    """Show who the queue would call next, without reserving or dialling.

    Read-only on purpose: it answers "why is nothing being called" without
    consuming an attempt. It re-runs the same eligibility rules the queue does,
    per prospect, and prints the first reason each one is not eligible.
    """
    store = await _store(config)
    try:
        campaign = await _resolve_campaign(store, args.campaign)
        if campaign is None:
            print(f"No campaign called {args.campaign!r}.", file=sys.stderr)
            return EXIT_FAILED

        service = _service(config, store)
        pairs = await store.list_campaign_prospects(campaign.id, limit=args.limit)
        if not pairs:
            print(f"{campaign.name!r} has no prospects.")
            return EXIT_OK

        print(f"{campaign.name!r} ({campaign.status.value}), {len(pairs)} membership(s):")
        eligible = 0
        for membership, prospect in pairs:
            check = await service.check_callable(prospect, campaign, membership)
            if check.allowed:
                eligible += 1
                marker, note = "->", f"ready (attempt {membership.attempt_count + 1})"
            else:
                marker, note = "  ", check.reason
            print(
                f"  {marker} {prospect.full_name:<28} "
                f"{prospect.phone_normalized or prospect.phone:<16} "
                f"{membership.status.value:<12} {note}"
            )
    finally:
        await store.close()

    print(f"\n{eligible} ready to call now.")
    return EXIT_OK


async def command_call(config: Config, args: argparse.Namespace) -> int:
    """Reserve and place the next call for a campaign.

    The one command that spends money. It goes through the same telephony
    abstraction as `call.py`, so whichever carrier is configured is the one that
    dials, and this file never names one.

    Phase 9 puts three things in front of it, in this order:

    1. **Recovery**, unless `--no-recover`. Attempts left live by a previous
       run are reconciled against the carrier *before* anything new is placed,
       because one of them may be a call to the very prospect the queue is
       about to hand out again.
    2. **Guardrails** — calling hours, the concurrency limit, pacing — checked
       before a reservation is taken, so a closed window does not spend one of
       a prospect's attempts.
    3. **A placement that is never retried.** An ambiguous answer from the
       carrier holds the attempt as `UNRESOLVED` and stops the run, because the
       one thing worse than a call that may not have happened is a second one
       on top of it.
    """
    telephony = config.telephony
    try:
        telephony.require_outbound()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED

    store = await _store(config)
    provider = make_provider(telephony, timeout_secs=config.reliability.carrier_timeout_secs)
    guards = _guards(config)
    print(f"Limits: {guards.describe()}")
    print(f"Webhooks: {telephony.describe_webhooks()}")
    try:
        campaign = await _resolve_campaign(store, args.campaign)
        if campaign is None:
            print(f"No campaign called {args.campaign!r}.", file=sys.stderr)
            return EXIT_FAILED

        service = _service(config, store)

        if not args.no_recover:
            report = await AttemptRecovery(
                service, provider, min_age_secs=config.reliability.recovery_min_age_secs
            ).run()
            if report.total:
                print(f"Recovery: {report.describe()}")

        dialer = CampaignDialer(
            service,
            provider,
            from_number=telephony.from_number or "",
            public_url=telephony.public_url or "",
            stream_path=telephony.stream_path,
            answer_timeout_secs=telephony.answer_timeout_secs,
            guards=guards,
            # Phase 12: carrier-side answering-machine detection, when set.
            machine_detection=telephony.machine_detection,
            # Phase 14: the carrier pushes each call's events to the receiver,
            # which writes the attempt; the poll below is the fallback.
            status_callback_url=telephony.webhook_url(),
            # Phase 19: the compliance gate, before every placement.
            gate=_gate(config, service),
        )

        placed = 0
        ambiguous = False
        for index in range(args.count):
            result = await dialer.dial_next(campaign.id)
            print(f"  {result.describe()}")
            if result.ambiguous:
                # Stop the run. The prospect is held, and until recovery has
                # resolved them nothing here knows whether a phone is ringing.
                ambiguous = True
                break
            if result.blocked or result.queued is None:
                break
            if result.placed:
                placed += 1
            # Pacing is enforced by the guard, which would refuse the next call
            # rather than wait; sleeping here is what turns that refusal into a
            # paced run. Jittered so two operators starting at the same moment
            # do not stay in step.
            if config.reliability.pacing_secs and index + 1 < args.count:
                await asyncio.sleep(jittered(config.reliability.pacing_secs))
    except TelephonyError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED
    finally:
        await provider.close()
        await store.close()

    print(f"\n{placed} call(s) placed.")
    if ambiguous:
        print(
            "Stopped: a placement did not report an outcome. Run "
            "`uv run campaign.py recover` once the carrier is reachable.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    return EXIT_OK if placed or args.count == 0 else EXIT_FAILED


async def command_run(config: Config, args: argparse.Namespace) -> int:
    """Place calls unattended until stopped. Phase 13.

    The loop that `call` was the manual form of: recovery first, then — for
    every `ACTIVE` campaign, or the ones named — due callbacks, then the queue,
    each call through the same dialer, the same guards and the same
    never-retried placement, followed to its end and written back. Runs in its
    own process; the bot is untouched and must be up separately, because the
    carrier streams each call's audio to it.

    Ctrl+C once stops placing and lets the calls in progress finish (up to
    `WORKER_DRAIN_SECS`); twice stops now and leaves them to `recover`.
    """
    telephony = config.telephony
    try:
        telephony.require_outbound()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED

    store = await _store(config)
    provider = make_provider(telephony, timeout_secs=config.reliability.carrier_timeout_secs)
    guards = _guards(config)
    try:
        campaign_ids: list[int] | None = None
        if args.campaign:
            campaign_ids = []
            for reference in args.campaign:
                campaign = await _resolve_campaign(store, reference)
                if campaign is None:
                    print(f"No campaign called {reference!r}.", file=sys.stderr)
                    return EXIT_FAILED
                if not campaign.status.is_dialable:
                    print(
                        f"Campaign {campaign.name!r} is {campaign.status.value}, not ACTIVE. "
                        f"Start it first:  uv run campaign.py start {campaign.id}",
                        file=sys.stderr,
                    )
                    return EXIT_FAILED
                campaign_ids.append(campaign.id)

        # Phase 25: the same assembly the unified application's engine uses —
        # the service, the guards, the gate, the recovery pass, the dialer
        # (Phase 14: events pushed to the receiver; Phase 19: the gate before
        # every placement; Phase 21: this process's name in the fleet).
        scheduler = build_worker(
            config,
            store,
            provider,
            campaign_ids=campaign_ids,
            max_calls=args.max_calls,
            once=args.once,
            auto_complete=config.worker.auto_complete and not args.no_auto_complete,
        )
        worker = scheduler.worker
        install_signal_handlers(worker)

        # Phase 22: this process's own /healthz, /readyz and /metrics. The
        # scheduler has no web server otherwise; a taken port (a second
        # worker on this machine) is a warning, and it dials regardless.
        ops = None
        if config.monitoring.serves_worker:
            ops = await serve_ops(
                create_ops_app(
                    "scheduler",
                    readiness=_worker_readiness(store, worker),
                    token=config.monitoring.token,
                    version="22",
                    info=lambda: {"worker_id": worker.worker_id, "in_flight": len(worker.in_flight), "stopping": worker.stopping},
                    stopping=lambda: worker.stopping,
                ),
                host=config.monitoring.host,
                port=config.monitoring.port,
                role="scheduler",
            )

        print(f"Limits: {guards.describe()}")
        print(f"Worker: {worker.worker_id} — {config.worker.describe()}")
        print(f"Webhooks: {telephony.describe_webhooks()}")
        print(
            f"Metrics: {ops.url}{METRICS_PATH}  ({HEALTH_PATH}, {READY_PATH})"
            if ops is not None
            else "Metrics: not served by this process (MONITORING_PORT=0, monitoring off, or the port is taken)"
        )
        print(
            "Serving: "
            + ("every ACTIVE campaign" if campaign_ids is None else f"campaign(s) {campaign_ids}")
            + (f", stopping after {args.max_calls} call(s)" if args.max_calls else "")
            + (", one pass" if args.once else "")
        )
        print(
            f"The bot must be up at {telephony.public_url} to answer the calls this places.\n"
            f"Ctrl+C once: stop placing and let calls in progress finish. Twice: stop now."
        )
        metrics = await worker.run()
    except TelephonyError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED
    finally:
        if ops is not None:
            await ops.stop()
        await provider.close()
        await store.close()

    print(f"\n{metrics.describe()}.")
    if worker.in_flight:
        print(
            "Stopped with calls in progress. Run `uv run campaign.py recover` once they "
            "have ended.",
            file=sys.stderr,
        )
    return EXIT_OK


def _worker_readiness(store: CampaignStore, worker: CampaignWorker):
    """The scheduler's `/readyz`: the database answers and the loop is not draining. Phase 22."""

    async def readiness() -> Readiness:
        checks = [await store_ready(lambda: store)]
        checks.append(
            ReadyCheck(
                "worker",
                not worker.stopping,
                "placing calls" if not worker.stopping else "draining: no new calls will be placed",
            )
        )
        return Readiness(checks)

    return readiness


async def command_metrics(config: Config, args: argparse.Namespace) -> int:
    """The deployment's numbers from the rows, without a running server. Phase 22.

    Throughput over a window, the queue, the fleet — the same reads the
    servers refresh into their gauges — printed for a person, as JSON, or in
    the Prometheus text format for a textfile collector or a cron job. Dials
    nothing; opens one pool.
    """
    store = await _store(config)
    window = float(args.hours) * 3600.0 if args.hours else config.monitoring.throughput_window_secs
    stale_secs = config.worker.stale_secs
    try:
        throughput = await store.throughput(window_secs=window)
        depth = await store.queue_depth(max_attempts=config.campaign_max_attempts)
        try:
            summary = await store.worker_summary(stale_after_secs=stale_secs)
        except CampaignStoreError as exc:
            if "does not exist" not in str(exc):
                raise
            summary = None
        if args.prometheus:
            await refresh_from_store(
                store, stale_secs=stale_secs, max_attempts=config.campaign_max_attempts, window_secs=window
            )
    finally:
        await store.close()

    if args.prometheus:
        print(REGISTRY.render_prometheus(), end="")
        return EXIT_OK
    if args.json:
        payload = {
            "throughput": throughput.to_dict(),
            "queue": depth.to_dict(),
            "workers": summary.to_dict(datetime.now(UTC), stale_secs) if summary is not None else None,
            "stale_secs": stale_secs,
        }
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK

    print(f"Throughput: {throughput.describe()} ({throughput.per_hour:.1f} calls/hour)")
    if throughput.prompt_tokens or throughput.completion_tokens:
        print(f"  tokens in the window: {throughput.prompt_tokens:,} prompt, {throughput.completion_tokens:,} completion")
    for row in throughput.per_campaign:
        print(f"  campaign {row['campaign_id']:>4}: {row['placed']} placed, {row['finished']} finished")
    print(f"Queue: {depth.describe()}")
    if summary is None:
        print("Workers: no scheduler_workers table (run `uv run campaign.py init`)")
    else:
        print(f"Workers: {summary.describe()} (stale after {stale_secs:g}s)")
    print(
        f"Live figures: every server answers {METRICS_PATH} ({config.monitoring.describe()})"
    )
    return EXIT_OK


async def command_workers(config: Config, args: argparse.Namespace) -> int:
    """Who is running, what they hold, and how deep the queue is. Phase 21.

    Reads the heartbeat table and the queue in one pass and dials nothing.
    A `stale` worker is one whose last beat is older than `WORKER_STALE_SECS`:
    its calls are adopted and its reservations released by the next live
    worker's adoption pass, or by `recover` when nobody is running.
    """
    store = await _store(config)
    stale_secs = config.worker.stale_secs
    try:
        try:
            summary = await store.worker_summary(stale_after_secs=stale_secs)
        except CampaignStoreError as exc:
            if "does not exist" not in str(exc):
                raise
            print(
                "This database has no scheduler_workers table. Run `uv run campaign.py init`.",
                file=sys.stderr,
            )
            return EXIT_FAILED
        depth = await store.queue_depth(max_attempts=config.campaign_max_attempts)
        pruned = 0
        if args.prune:
            pruned = await store.prune_workers(older_than_secs=args.prune * 3600)
    finally:
        await store.close()

    if args.json:
        payload = {
            "workers": summary.to_dict(datetime.now(UTC), stale_secs),
            "queue": depth.to_dict(),
            "stale_secs": stale_secs,
        }
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK

    print(f"Workers: {summary.describe()} (stale after {stale_secs:g}s without a heartbeat)")
    now = datetime.now(UTC)
    for record in summary.workers:
        health = record.health(now, stale_secs)
        age = (
            f"{(now - record.heartbeat_at).total_seconds():.0f}s ago"
            if record.heartbeat_at
            else "never"
        )
        campaigns = "all active" if record.campaign_ids is None else ",".join(map(str, record.campaign_ids))
        metrics = record.metrics or {}
        print(
            f"  {health.upper():<9} {record.worker_id:<40} {record.hostname}:{record.pid}  "
            f"in flight {record.in_flight}  beat {age}  campaigns {campaigns}  "
            f"started {metrics.get('started', 0)} completed {metrics.get('completed', 0)} "
            f"failed {metrics.get('failed', 0)}"
        )
    if not summary.workers:
        print("  (none registered yet — start one with `uv run campaign.py run`)")
    print(f"Queue: {depth.describe()}")
    for row in depth.per_campaign:
        print(
            f"  {row['campaign_id']:>4}  {row['name']!r:<32} due now {row['due_now']}, "
            f"scheduled {row['scheduled']}, in progress {row['in_progress']}"
        )
    if pruned:
        print(f"Pruned {pruned} stopped worker row(s) older than {args.prune:g}h.")
    return EXIT_OK


async def command_recover(config: Config, args: argparse.Namespace) -> int:
    """Resolve call attempts left live by a crash, a restart or a lost answer. Phase 9.

    Reads what the carrier actually has and writes the answer onto each
    attempt. **It never dials.** An attempt it cannot resolve is closed as
    failed with a reason on the row, so the prospect stops being blocked and
    the campaign's own retry policy decides whether to try them again.

    Run it after any unclean shutdown, and whenever `health.py` reports live
    attempts on a system where no call is in progress.
    """
    telephony = config.telephony
    store = await _store(config)
    provider = (
        make_provider(telephony, timeout_secs=config.reliability.carrier_timeout_secs)
        if telephony.has_credentials
        else None
    )
    if provider is None:
        print(
            "No carrier credentials, so attempts that may have placed a call cannot be "
            "checked against one. Only never-placed attempts will be released."
        )
    try:
        report = await AttemptRecovery(
            _service(config, store),
            provider,
            min_age_secs=0.0 if args.all else config.reliability.recovery_min_age_secs,
            limit=args.limit,
        ).run()
    finally:
        if provider is not None:
            await provider.close()
        await store.close()

    print(f"\n{report.describe()}.")
    for note in report.notes:
        print(f"  - {note}", file=sys.stderr)
    return EXIT_OK


async def command_prospects(config: Config, args: argparse.Namespace) -> int:
    """List prospects."""
    store = await _store(config)
    try:
        prospects = await store.list_prospects(limit=args.limit)
        total = await store.count_prospects()
    finally:
        await store.close()

    if not prospects:
        print("No prospects yet.  Import some:  uv run campaign.py import <file.csv>")
        return EXIT_OK
    for prospect in prospects:
        print(
            f"  {prospect.id:>5}  {prospect.full_name:<28} "
            f"{prospect.phone_normalized or '(unusable) ' + prospect.phone:<18} "
            f"{prospect.status.value:<12} {prospect.company or ''}"
        )
    print(f"\n{len(prospects)} shown of {total}.")
    return EXIT_OK


async def command_attempts(config: Config, args: argparse.Namespace) -> int:
    """Show call history."""
    store = await _store(config)
    try:
        campaign_id = None
        if args.campaign:
            campaign = await _resolve_campaign(store, args.campaign)
            if campaign is None:
                print(f"No campaign called {args.campaign!r}.", file=sys.stderr)
                return EXIT_FAILED
            campaign_id = campaign.id
        attempts = await store.list_attempts(
            campaign_id=campaign_id, prospect_id=args.prospect, limit=args.limit
        )
    finally:
        await store.close()

    if not attempts:
        print("No call attempts recorded.")
        return EXIT_OK
    for attempt in attempts:
        when = attempt.started_at.strftime("%Y-%m-%d %H:%M") if attempt.started_at else "-"
        print(
            f"  {attempt.id:>5}  {when:<17} prospect {attempt.prospect_id:<6} "
            f"try {attempt.attempt_number}  {attempt.status.value:<18} "
            f"{attempt.telephony_call_id or ''} {attempt.failure_reason or ''}"
        )
    print(f"\n{len(attempts)} attempt(s).")
    return EXIT_OK


def _actor(args: argparse.Namespace) -> str:
    """Who ran the command, for the list row and the audit log."""
    import getpass

    return getattr(args, "actor", None) or f"cli:{getpass.getuser() or 'unknown'}"


async def _audit_cli(config: Config, store: CampaignStore, action: str, args: argparse.Namespace, **detail) -> None:
    """One audit row for a compliance action taken from the command line. Phase 19."""
    from src.security import AuditLog, Principal, Role

    log = AuditLog(lambda: store, enabled=config.security.audit_enabled, strict=config.security.audit_strict)
    await log.record(action, principal=Principal(name=_actor(args), role=Role.ADMIN, via="cli"), **detail)


async def command_dnc(config: Config, args: argparse.Namespace) -> int:
    """Put a prospect, or a bare number, on the do-not-call list. Phase 5; Phase 19 added the number and the list."""
    store = await _store(config)
    try:
        service = _service(config, store)
        source = parse_source(args.source, DncSource.CLI)
        if args.prospect_id is not None:
            updated = await service.mark_do_not_call(
                args.prospect_id, source=source, reason=args.reason, actor=_actor(args)
            )
            if not updated:
                print(f"No prospect with id {args.prospect_id}.", file=sys.stderr)
                return EXIT_FAILED
            await _audit_cli(config, store, "compliance.dnc_added", args, target=("prospect", args.prospect_id), source=source.value, reason=args.reason)
            print(
                f"Prospect {args.prospect_id} is marked DO_NOT_CALL, removed from every open campaign,"
                f" and their number is on the do-not-call list ({source.value})."
            )
            return EXIT_OK
        try:
            entry, inserted, marked = await service.add_do_not_call_number(
                args.number, source=source, reason=args.reason, actor=_actor(args), note=args.note
            )
        except ValueError as exc:
            print(f"Cannot list {args.number!r}: {exc}", file=sys.stderr)
            return EXIT_FAILED
        await _audit_cli(config, store, "compliance.dnc_added", args, target=("number", entry.phone_normalized), source=source.value, reason=args.reason, prospects_marked=marked)
    finally:
        await store.close()
    print(
        f"{entry.phone_normalized} {'added to' if inserted else 'was already on'} the do-not-call list"
        f" ({entry.source.value}); {marked} prospect(s) marked DO_NOT_CALL."
    )
    return EXIT_OK


async def command_dnc_remove(config: Config, args: argparse.Namespace) -> int:
    """Take a number off the do-not-call list; the row stays, stamped. Phase 19."""
    store = await _store(config)
    try:
        service = _service(config, store)
        try:
            entry, reinstated = await service.remove_do_not_call_number(
                args.number, actor=_actor(args), reason=args.reason, reinstate_prospects=args.reinstate
            )
        except ValueError as exc:
            print(f"Cannot read {args.number!r}: {exc}", file=sys.stderr)
            return EXIT_FAILED
        if entry is None:
            print(f"{args.number} is not on the do-not-call list.", file=sys.stderr)
            return EXIT_FAILED
        await _audit_cli(config, store, "compliance.dnc_removed", args, target=("number", entry.phone_normalized), reason=args.reason, reinstated=reinstated)
    finally:
        await store.close()
    print(
        f"{entry.phone_normalized} removed from the do-not-call list (was {entry.source.value}, since "
        f"{entry.created_at:%Y-%m-%d})."
        + (f" {reinstated} prospect(s) set back to NEW." if args.reinstate else " Their prospect rows stay DO_NOT_CALL (use --reinstate to reopen them).")
    )
    return EXIT_OK


async def command_dnc_list(config: Config, args: argparse.Namespace) -> int:
    """List the do-not-call list, newest first. Phase 19."""
    tz = _zone(config)
    store = await _store(config)
    try:
        counts = await store.dnc_counts()
        rows = await store.list_dnc(
            phone_normalized=args.number, source=args.source, include_revoked=args.all, limit=args.limit
        )
    finally:
        await store.close()
    by_source = ", ".join(f"{k} {v}" for k, v in counts.items() if k not in ("active", "revoked"))
    print(f"Do-not-call list: {counts['active']} active ({by_source or 'none'}), {counts['revoked']} revoked.")
    if not rows:
        print("\nNothing listed" + (" for that filter." if (args.number or args.source) else "."))
        return EXIT_OK
    print()
    for row in rows:
        when = row.created_at.astimezone(tz).strftime("%Y-%m-%d %H:%M") if row.created_at else "-"
        state = "revoked " + (row.revoked_at.astimezone(tz).strftime("%Y-%m-%d") if row.revoked_at else "") if row.revoked_at else "active"
        refs = " ".join(
            f"{k}={v}" for k, v in (("prospect", row.prospect_id), ("campaign", row.campaign_id), ("attempt", row.call_attempt_id)) if v is not None
        )
        print(
            f"  #{row.id:<6} {when}  {row.phone_normalized:<16} {row.source.value:<9} {state:<20}"
            f" {row.reason or ''}{'  ' + refs if refs else ''}{'  by ' + row.created_by if row.created_by else ''}"
        )
    return EXIT_OK


async def command_dnc_import(config: Config, args: argparse.Namespace) -> int:
    """Load a suppression file: one number per line, or a CSV with a phone column. Phase 19."""
    path = Path(args.path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"Cannot read {path}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    numbers: list[str] = []
    header_seen = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        cells = [cell.strip().strip('"') for cell in line.split(",")]
        if not header_seen and not any(ch.isdigit() for ch in line):
            header_seen = True  # a header row; the phone is the first cell with digits below
            continue
        chosen = next((cell for cell in cells if sum(ch.isdigit() for ch in cell) >= 5), None)
        if chosen:
            numbers.append(chosen)
    if not numbers:
        print(f"No phone numbers found in {path.name}.", file=sys.stderr)
        return EXIT_FAILED

    source = parse_source(args.source, DncSource.IMPORT)
    store = await _store(config)
    added = known = rejected = marked = 0
    try:
        service = _service(config, store)
        for number in numbers:
            try:
                _entry, inserted, count = await service.add_do_not_call_number(
                    number, source=source, reason=args.reason or f"imported from {path.name}", actor=_actor(args)
                )
            except ValueError:
                rejected += 1
                continue
            added += 1 if inserted else 0
            known += 0 if inserted else 1
            marked += count
        await _audit_cli(
            config, store, "compliance.dnc_imported", args, target=("file", path.name),
            source=source.value, added=added, already_listed=known, rejected=rejected, prospects_marked=marked,
        )
    finally:
        await store.close()
    print(
        f"{path.name}: {added} number(s) added to the do-not-call list ({source.value}), {known} already listed, "
        f"{rejected} could not be read; {marked} prospect(s) marked DO_NOT_CALL."
    )
    return EXIT_OK


async def command_dnc_apply(config: Config, args: argparse.Namespace) -> int:
    """Mark every prospect whose number is on the list. Phase 19. For a list loaded before its prospects."""
    store = await _store(config)
    try:
        marked = await store.apply_dnc_list(None)
        if marked:
            await _audit_cli(config, store, "compliance.dnc_applied", args, prospects_marked=marked)
    finally:
        await store.close()
    print(f"{marked} prospect(s) newly marked DO_NOT_CALL from the do-not-call list.")
    return EXIT_OK


async def command_compliance(config: Config, args: argparse.Namespace) -> int:
    """Show or set a campaign's compliance settings, and the policy they produce. Phase 19."""
    store = await _store(config)
    try:
        service = _service(config, store)
        campaign = await _resolve_campaign(store, args.campaign) if args.campaign else None
        if args.campaign and campaign is None:
            print(f"No campaign called {args.campaign!r}.", file=sys.stderr)
            return EXIT_FAILED
        if campaign is not None and (args.set or args.clear):
            current = dict(campaign.configuration.get(COMPLIANCE_KEY) or {}) if isinstance(campaign.configuration, dict) else {}
            for item in args.set or []:
                key, sep, value = item.partition("=")
                if not sep:
                    print(f"--set takes key=value, not {item!r}.", file=sys.stderr)
                    return EXIT_FAILED
                current[key.strip()] = _coerce(value.strip())
            for key in args.clear or []:
                current.pop(key.strip(), None)
            problems: list[str] = []
            config.compliance_policy.overlay(current, source=f"campaign:{campaign.id}", problems=problems)
            if problems:
                print("Not saved:\n  - " + "\n  - ".join(problems), file=sys.stderr)
                return EXIT_FAILED
            updated = await store.update_campaign_configuration(campaign.id, COMPLIANCE_KEY, current or None)
            if updated is None:
                print("The campaign vanished.", file=sys.stderr)
                return EXIT_FAILED
            await _audit_cli(config, store, "campaign.compliance_updated", args, target=("campaign", campaign.id), settings=current)
            campaign = updated
            print(f"Saved. Campaign {campaign.name!r} compliance settings: {json.dumps(current or {}, sort_keys=True)}\n")
        policy = service.policy_for(campaign)
    finally:
        await store.close()

    print(f"Environment policy:  {config.compliance_policy.describe()}")
    if campaign is not None:
        print(f"Campaign {campaign.name!r}:  {policy.describe()}")
        print(f"  settings: {json.dumps(campaign.configuration.get(COMPLIANCE_KEY) or {}, sort_keys=True)}")
    resolver = config.policy_resolver()
    if resolver.jurisdictions:
        print("Jurisdictions (applied on top of the campaign, by the number's country):")
        for code, overrides in sorted(resolver.jurisdictions.items()):
            print(f"  {code}: {json.dumps(overrides, sort_keys=True)}")
    else:
        print("Jurisdictions: none configured (COMPLIANCE_JURISDICTIONS).")
    print(f"Disclosures: {config.compliance.describe()}")
    print("\nThe software applies these; deciding them is the operator's. See COMPLIANCE.md.")
    return EXIT_OK


def _coerce(value: str):
    """A CLI value as JSON when it is JSON (true, 3, 2.5), else text."""
    try:
        return json.loads(value)
    except ValueError:
        return value


async def command_compliance_log(config: Config, args: argparse.Namespace) -> int:
    """The compliance decisions, newest first: what was blocked, what was allowed, and why. Phase 19."""
    args.action = args.action or "compliance."
    return await command_audit(config, args)


async def command_callbacks(config: Config, args: argparse.Namespace) -> int:
    """List scheduled callbacks. Phase 7.

    `--due` narrows to the ones whose time has come, which is the list a person
    running the diary by hand needs. A due callback is also a membership the
    queue will hand out — `campaign.py call` places it — unless that membership
    has hit its attempt limit, in which case it appears here and nowhere else.
    """
    tz = _zone(config)
    store = await _store(config)
    try:
        status = None if args.all else CallbackStatus.PENDING
        callbacks = await store.list_callbacks(
            prospect_id=args.prospect,
            status=status,
            due_before=datetime.now(UTC) if args.due else None,
            limit=args.limit,
        )
        names = {}
        for callback in callbacks:
            if callback.prospect_id not in names:
                prospect = await store.get_prospect(callback.prospect_id)
                names[callback.prospect_id] = (
                    f"{prospect.full_name} ({prospect.phone_normalized or prospect.phone})"
                    if prospect
                    else f"prospect {callback.prospect_id} (deleted)"
                )
    finally:
        await store.close()

    if not callbacks:
        print("No callbacks" + (" due" if args.due else " scheduled") + ".")
        return EXIT_OK
    now = datetime.now(UTC)
    for callback in callbacks:
        when = callback.scheduled_for.astimezone(tz)
        due = "DUE " if callback.status is CallbackStatus.PENDING and callback.scheduled_for <= now else "    "
        print(
            f"  {callback.id:>5}  {due}{when:%a %d %b %H:%M}  {callback.status.value:<10} "
            f"{names[callback.prospect_id]:<44} {callback.note or ''}"
        )
    print(f"\n{len(callbacks)} callback(s), times in {config.calendar.timezone}.")
    return EXIT_OK


async def command_cancel_callback(config: Config, args: argparse.Namespace) -> int:
    """Withdraw a pending callback. Phase 7."""
    store = await _store(config)
    try:
        cancelled = await store.cancel_callback(args.callback_id)
    finally:
        await store.close()
    if not cancelled:
        print(f"Callback {args.callback_id} is not pending.", file=sys.stderr)
        return EXIT_FAILED
    print(f"Callback {args.callback_id} cancelled. The membership keeps its retry time; pause the "
          f"campaign or mark the prospect if they should not be called.")
    return EXIT_OK


async def command_meetings(config: Config, args: argparse.Namespace) -> int:
    """List meetings the agent booked. Phase 7.

    With the local calendar this *is* the diary. With Cal.com it is the mirror,
    and the reference column is the Cal.com booking uid.
    """
    tz = _zone(config)
    store = await _store(config)
    try:
        meetings = await store.list_meetings(
            prospect_id=args.prospect,
            from_time=datetime.now(UTC) if args.upcoming else None,
            limit=args.limit,
        )
    finally:
        await store.close()

    if not meetings:
        print("No meetings booked" + (" from now on" if args.upcoming else "") + ".")
        return EXIT_OK
    for meeting in meetings:
        start = meeting.start_at.astimezone(tz)
        end = meeting.end_at.astimezone(tz)
        who = meeting.attendee_name or (f"prospect {meeting.prospect_id}" if meeting.prospect_id else "unknown")
        print(
            f"  {meeting.id:>5}  {start:%a %d %b %H:%M}-{end:%H:%M}  {meeting.provider:<7} "
            f"{who:<28} {meeting.attendee_email or '':<28} {meeting.reference or ''} {meeting.notes or ''}"
        )
    print(f"\n{len(meetings)} meeting(s), times in {config.calendar.timezone}.")
    return EXIT_OK


async def command_results(config: Config, args: argparse.Namespace) -> int:
    """List what finished calls produced. Phase 8.

    One line per result: who, what the call came to, how qualified, and the
    next step. `result <attempt>` shows one in full.
    """
    store = await _store(config)
    try:
        campaign_id = None
        if args.campaign:
            campaign = await _resolve_campaign(store, args.campaign)
            if campaign is None:
                print(f"No campaign called {args.campaign!r}.", file=sys.stderr)
                return EXIT_FAILED
            campaign_id = campaign.id
        disposition = Disposition(args.disposition.upper()) if args.disposition else None
        results = await store.list_call_results(
            campaign_id=campaign_id,
            prospect_id=args.prospect,
            disposition=disposition,
            limit=args.limit,
        )
        names = {}
        for result in results:
            if result.prospect_id not in names:
                prospect = await store.get_prospect(result.prospect_id)
                names[result.prospect_id] = prospect.full_name if prospect else f"prospect {result.prospect_id}"
    finally:
        await store.close()

    if args.json:
        print(json.dumps([result.to_dict() for result in results], indent=2, ensure_ascii=False))
        return EXIT_OK
    if not results:
        print("No call results recorded.")
        return EXIT_OK
    for result in results:
        when = result.created_at.strftime("%Y-%m-%d %H:%M") if result.created_at else "-"
        print(
            f"  attempt {result.call_attempt_id:>5}  {when:<17} {names[result.prospect_id]:<24} "
            f"{result.disposition.value:<19} {result.qualification_status.value:<20} "
            f"{result.next_action.value:<18} {result.source.value.lower()}"
        )
    print(f"\n{len(results)} result(s).")
    return EXIT_OK


async def command_result(config: Config, args: argparse.Namespace) -> int:
    """Show one call's full structured result. Phase 8."""
    store = await _store(config)
    try:
        result = await store.get_call_result(args.attempt_id)
        if result is None:
            attempt = await store.get_attempt(args.attempt_id)
            if attempt is None:
                print(f"No call attempt with id {args.attempt_id}.", file=sys.stderr)
            else:
                print(
                    f"No result yet for attempt {args.attempt_id} (status {attempt.status.value}). "
                    f"A result is written when the call reaches a final status.",
                    file=sys.stderr,
                )
            return EXIT_FAILED
        prospect = await store.get_prospect(result.prospect_id)
        campaign = await store.get_campaign(result.campaign_id) if result.campaign_id else None
    finally:
        await store.close()

    if args.json:
        export = result.to_dict()
        export["prospect"] = (
            {
                "id": prospect.id,
                "first_name": prospect.first_name,
                "last_name": prospect.last_name,
                "phone": prospect.phone_normalized or prospect.phone,
                "email": prospect.email,
                "company": prospect.company,
            }
            if prospect
            else None
        )
        export["campaign"] = {"id": campaign.id, "name": campaign.name} if campaign else None
        print(json.dumps(export, indent=2, ensure_ascii=False))
        return EXIT_OK

    _print_result(result, prospect, campaign, config, transcript=args.transcript)
    return EXIT_OK


async def command_rebuild_results(config: Config, args: argparse.Namespace) -> int:
    """Build results for finished attempts that have none, from what is on their rows. Phase 8.

    The result is a projection of the attempt row and its `conversation_data`,
    so it can always be rebuilt: for attempts that finished before the table
    existed, or after the result's shape changes. With `--all` every finished
    attempt is rebuilt; a rebuilt conversation result replaces whatever is
    there, exactly as a live one would.
    """
    store = await _store(config)
    built = skipped = failed = 0
    try:
        attempts = await store.list_attempts(limit=args.limit)
        for attempt in attempts:
            if not attempt.status.is_final:
                continue
            if not args.all and await store.get_call_result(attempt.id) is not None:
                skipped += 1
                continue
            if attempt.conversation_data:
                status = attempt_status_for(attempt.conversation_data) or attempt.status
                result = build_conversation_result(attempt, attempt.conversation_data, call_status=status)
            else:
                result = build_carrier_result(attempt)
            try:
                stored = await store.save_call_result(result)
            except CallResultValidationError as exc:
                failed += 1
                print(f"  attempt {attempt.id}: not stored — {exc}", file=sys.stderr)
                continue
            if stored is None:
                skipped += 1
                continue
            built += 1
            print(f"  attempt {attempt.id:>5}  {stored.disposition.value:<19} {stored.source.value.lower()}")
    finally:
        await store.close()
    print(f"\n{built} result(s) built, {skipped} already there, {failed} refused.")
    return EXIT_OK if not failed else EXIT_FAILED


def _print_result(result: CallResult, prospect, campaign, config: Config, *, transcript: bool) -> None:
    """Render one result for a terminal."""
    tz = _zone(config)
    who = f"{prospect.full_name} ({prospect.phone_normalized or prospect.phone})" if prospect else f"prospect {result.prospect_id}"
    where = f" — campaign {campaign.name!r}" if campaign else ""
    print(f"Attempt {result.call_attempt_id} — {who}{where}")
    ended = {True: "by the agent", False: "by the other end or a dropped line", None: "unknown"}[result.agent_ended_call]
    print(f"  status       {result.call_status.value:<20} disposition  {result.disposition.value:<20} source  {result.source.value.lower()}")
    print(f"  duration     {str(result.duration_seconds) + ' s' if result.duration_seconds is not None else 'unknown':<20} ended        {ended}")
    if result.failure_reason:
        print(f"  failure      {result.failure_reason}")
    print(
        f"  qualified    {result.qualification_status.value:<20} interest     {result.interest_level.value:<20} "
        f"timeline  {result.buying_timeline.value}  decision  {result.decision_role.value}"
    )
    meeting = result.meeting_status.value
    if result.meeting_start:
        meeting += f"  {result.meeting_start.astimezone(tz):%a %d %b %Y %H:%M}"
    if result.meeting_reference:
        meeting += f"  (ref {result.meeting_reference})"
    elif result.meeting_when:
        meeting += f"  ({result.meeting_when})"
    callback = result.callback_status.value
    if result.callback_scheduled_for:
        callback += f"  {result.callback_scheduled_for.astimezone(tz):%a %d %b %Y %H:%M}"
    elif result.callback_when:
        callback += f"  ({result.callback_when})"
    print(f"  next action  {result.next_action.value:<20} meeting      {meeting}")
    print(f"  {'':<33} callback     {callback}")
    for label, items in (
        ("pain points", result.pain_points),
        ("questions", result.questions),
        ("notes", result.notes),
    ):
        if items:
            print(f"  {label:<12} - " + "\n               - ".join(items))
    if result.objections:
        print(
            "  objections   - "
            + "\n               - ".join(
                f"{o.get('kind')} ({'handled' if o.get('handled') else 'open'})"
                + (f": {o.get('detail')}" if o.get("detail") else "")
                for o in result.objections
            )
        )
    if result.tool_actions:
        print(
            "  actions      "
            + "; ".join(
                f"{a.get('tool')} {'ok' if a.get('success') else 'FAIL ' + str(a.get('error_code') or '')}".strip()
                for a in result.tool_actions
            )
        )
    if result.issues:
        print("  issues       - " + "\n               - ".join(result.issues))
    print("  summary")
    for line in result.summary.text.splitlines():
        print(f"    {line}")
    if transcript:
        print("  transcript")
        text = result.transcript_text()
        if text:
            for line in text.splitlines():
                print(f"    {line}")
        else:
            print("    (none recorded)")
    print(f"\nTimes in {config.calendar.timezone}. Add --json for the CRM-ready export.")


def _zone(config: Config):
    """The configured timezone, for printing times the way the prospect heard them."""
    name = config.calendar.timezone
    return UTC if name.upper() == "UTC" else ZoneInfo(name)


async def command_webhooks(config: Config, args: argparse.Namespace) -> int:
    """What the carrier has pushed about calls, and what was done with it. Phase 14.

    The ledger, newest first: one line per delivery with the call id, the
    event, the carrier's sequence number and the outcome the receiver
    recorded — `applied`, `duplicate`, `stale`, `unmatched`, `ignored`,
    `noted`. The line at the top says whether events are being asked for at
    all, and where they are sent.
    """
    print(f"Webhooks: {config.telephony.describe_webhooks()}")
    store = await _store(config)
    try:
        counts = await store.webhook_counts()
        if counts is None:
            print(
                "The webhook ledger does not exist yet: this database predates Phase 14.\n"
                "  Run:  uv run campaign.py init"
            )
            return EXIT_FAILED
        summary = ", ".join(f"{n} {outcome}" for outcome, n in sorted(counts.items())) or "none"
        print(f"Deliveries: {summary}")
        rows = await store.list_webhook_events(
            call_id=args.call, attempt_id=args.attempt, limit=args.limit
        )
        if not rows:
            print("No deliveries match.")
            return EXIT_OK
        print(
            f"\n  {'received':<20} {'call':<36} {'event':<12} {'seq':>4} {'attempt':>8}  outcome"
        )
        for row in rows:
            received = row.received_at.strftime("%Y-%m-%d %H:%M:%S") if row.received_at else "-"
            what = row.raw_status or row.answered_by or row.kind
            seq = "-" if row.sequence is None else str(row.sequence)
            attempt = "-" if row.attempt_id is None else str(row.attempt_id)
            print(f"  {received:<20} {row.call_id:<36} {what:<12} {seq:>4} {attempt:>8}  {row.outcome}")
    finally:
        await store.close()
    return EXIT_OK


async def command_crm_sync(config: Config, args: argparse.Namespace) -> int:
    """File finished calls' results with the CRM, until stopped. Phase 15.

    The third long-running process beside the bot and the worker, and the one
    that talks to the CRM: it claims results the CRM has not seen, sends each
    through the configured provider, and records the outcome on `crm_sync`.
    `--once` runs one pass, for cron. The bot is never involved: a result is
    written at the end of a call as it always was, and this reads it later.
    """
    from src.crm import CrmError, CrmSyncer, make_crm_provider

    crm = config.crm
    if not crm.enabled:
        print("\nNo CRM is configured. Set CRM_PROVIDER=hubspot and HUBSPOT_ACCESS_TOKEN in .env.\n", file=sys.stderr)
        return EXIT_FAILED
    try:
        provider = make_crm_provider(crm, timeout_secs=config.reliability.carrier_timeout_secs)
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED

    store = await _store(config)
    syncer = CrmSyncer(
        store,
        provider,
        from_number=config.telephony.from_number,
        max_attempts=crm.sync_max_attempts,
        retry_secs=crm.sync_retry_secs,
        max_retry_secs=crm.sync_max_retry_secs,
        sync_unanswered=crm.sync_unanswered,
        batch=args.limit or crm.sync_batch,
        stale_secs=crm.sync_stale_secs,
    )
    try:
        if args.retry_failed:
            reopened = await store.retry_crm_sync(all_failed=True)
            print(f"Reopened {reopened} failed row(s).")
        print(f"CRM: {provider.describe()} — {crm.describe()}")
        print("Ctrl+C stops after the current pass." if not args.once else "One pass.")
        # The worker's handler fits: it calls `request_stop()` with no
        # arguments, which is the syncer's whole stop surface.
        install_signal_handlers(syncer)  # type: ignore[arg-type]
        totals = await syncer.run(poll_secs=crm.sync_poll_secs, once=args.once)
    except KeyboardInterrupt:
        syncer.request_stop()
        totals = syncer.totals
    except CrmError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED
    finally:
        await provider.close()
        await store.close()
    print(f"\n{totals.describe()}.")
    return EXIT_OK


async def command_crm_status(config: Config, args: argparse.Namespace) -> int:
    """What the CRM holds, and what it does not yet. Phase 15."""
    from src.campaigns import CrmSyncState

    print(f"CRM: {config.crm.describe()}")
    store = await _store(config)
    try:
        counts = await store.crm_sync_counts()
        if counts is None:
            print(
                "The crm_sync table does not exist yet: this database predates Phase 15.\n"
                "  Run:  uv run campaign.py init"
            )
            return EXIT_FAILED
        summary = ", ".join(f"{n} {state.lower()}" for state, n in sorted(counts.items()) if n) or "no results"
        print(f"Results: {summary}")
        state = CrmSyncState(args.state) if args.state else None
        rows = await store.list_crm_sync(state=state, limit=args.limit)
        if not rows:
            print("No sync rows match.")
            return EXIT_OK
        print(f"\n  {'result':>7} {'attempt':>8} {'state':<8} {'tries':>5} {'contact':<12} {'activity':<12}  last error / synced at")
        for row in rows:
            detail = row.last_error or (row.synced_at.strftime("synced %Y-%m-%d %H:%M") if row.synced_at else "")
            if row.state is CrmSyncState.RETRY and row.next_attempt_at:
                detail = f"due {row.next_attempt_at:%H:%M:%S} — {detail}"
            print(
                f"  {row.call_result_id:>7} {row.call_attempt_id:>8} {row.state.value:<8} {row.attempts:>5} "
                f"{(row.external_contact_id or '-'):<12} {(row.external_activity_id or '-'):<12}  {detail[:80]}"
            )
    finally:
        await store.close()
    return EXIT_OK


async def command_crm_retry(config: Config, args: argparse.Namespace) -> int:
    """Reopen failed sync rows, or one row, so the next pass tries again. Phase 15."""
    store = await _store(config)
    try:
        reopened = await store.retry_crm_sync(call_result_id=args.result, all_failed=args.all_failed)
    finally:
        await store.close()
    print(f"Reopened {reopened} row(s). They are picked up by the next `campaign.py crm-sync` pass.")
    return EXIT_OK


async def command_transfers(config: Config, args: argparse.Namespace) -> int:
    """Every transfer to a person, and how it ended. Phase 16.

    `REQUESTED` means the carrier accepted the redirect and has not yet said
    how the colleague's leg ended — which it only does through the webhook
    receiver, so a deployment without one sees `REQUESTED` for ever.
    """
    telephony = config.telephony
    tracked = telephony.webhook_url() is not None
    print(
        f"Transfers: to {telephony.transfer_number or '(TELEPHONY_TRANSFER_NUMBER not set)'}, "
        f"{telephony.transfer_timeout_secs}s ring, outcome "
        + ("reported to the webhook receiver" if tracked else "not reported (no webhook receiver)")
    )
    store = await _store(config)
    try:
        counts = await store.transfer_counts()
        if counts is None:
            print(
                "The call_transfers table does not exist yet: this database predates Phase 16.\n"
                "  Run:  uv run campaign.py init"
            )
            return EXIT_FAILED
        summary = ", ".join(f"{n} {status.lower()}" for status, n in sorted(counts.items())) or "none"
        print(f"Recorded: {summary}")
        rows = await store.list_transfers(call_attempt_id=args.attempt, limit=args.limit)
        if not rows:
            print("No transfers match.")
            return EXIT_OK
        print(f"\n  {'requested':<20} {'attempt':>8} {'to':<16} {'status':<10} {'secs':>5}  reason / error")
        for row in rows:
            when = row.requested_at.strftime("%Y-%m-%d %H:%M:%S") if row.requested_at else "-"
            attempt = "-" if row.call_attempt_id is None else str(row.call_attempt_id)
            secs = "-" if row.duration_seconds is None else str(row.duration_seconds)
            detail = row.error or row.reason or ""
            print(f"  {when:<20} {attempt:>8} {row.to_number:<16} {row.status.value:<10} {secs:>5}  {detail[:60]}")
    finally:
        await store.close()
    return EXIT_OK


async def command_audit(config: Config, args: argparse.Namespace) -> int:
    """List the audit log: who did what, newest first. Phase 18."""
    from datetime import UTC, datetime, timedelta

    tz = _zone(config)
    store = await _store(config)
    try:
        since = None
        if args.since_hours:
            since = datetime.now(UTC) - timedelta(hours=float(args.since_hours))
        rows = await store.list_audit(action=args.action, actor=args.actor, since=since, limit=args.limit)
        counts = await store.audit_counts(since=since)
    finally:
        await store.close()

    total = sum(counts.values())
    print(f"Audit log: {total} entr{'y' if total == 1 else 'ies'}" + (f" in the last {args.since_hours:g} h" if args.since_hours else "") + ".")
    if counts:
        print("  " + ", ".join(f"{name} {n}" for name, n in counts.items()))
    if not rows:
        print("\nNothing recorded" + (" for that filter." if (args.action or args.actor) else " yet."))
        return EXIT_OK
    print()
    for row in rows:
        when = row.created_at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "-"
        target = f" {row.target_kind}:{row.target_id}" if row.target_kind else ""
        detail = " ".join(f"{k}={v}" for k, v in row.detail.items()) if row.detail else ""
        print(
            f"  #{row.id:<6} {when}  {row.action:<26} {row.actor} ({row.role}, {row.via})"
            f"{target}  {row.outcome}" + (f"  {detail}" if detail else "") + (f"  from {row.ip}" if row.ip else "")
        )
    return EXIT_OK


async def command_events(config: Config, args: argparse.Namespace) -> int:
    """List the automation outbox: what has been sent to n8n, and what has not. Phase 17."""
    from src.campaigns import AutomationEventState

    tz = _zone(config)
    store = await _store(config)
    try:
        counts = await store.automation_event_counts()
        if counts is None:
            print(
                "\nThe automation_events table does not exist. Run:  uv run campaign.py init\n",
                file=sys.stderr,
            )
            return EXIT_FAILED
        state = None
        if args.state:
            try:
                state = AutomationEventState(args.state.upper())
            except ValueError:
                print(
                    f"Unknown state {args.state!r}; use one of "
                    f"{', '.join(s.value for s in AutomationEventState)}.",
                    file=sys.stderr,
                )
                return EXIT_FAILED
        rows = await store.list_automation_events(state=state, kind=args.kind, limit=args.limit)
    finally:
        await store.close()

    automation = config.automation
    print(f"Automation: {automation.describe()}")
    if counts:
        print("  " + ", ".join(f"{name} {n}" for name, n in sorted(counts.items())))
    else:
        print("  no events yet")
    if not rows:
        print("\nNo events" + (f" in state {state.value}" if state else "") + ".")
        return EXIT_OK
    print()
    for row in rows:
        when = f"{row.occurred_at.astimezone(tz):%a %d %b %H:%M}" if row.occurred_at else "-"
        target = (row.target_url or automation.target_for(row.kind) or "-").split("://", 1)[-1][:40]
        error = f"  {row.last_error}" if row.last_error else ""
        print(
            f"  {row.id:>5}  {row.state.value:<10} {row.kind:<19} {when:<16} "
            f"x{row.attempts}  {target:<40}{error}"
        )
    print(f"\n{len(rows)} event(s), times in {config.calendar.timezone}.")
    return EXIT_OK


async def command_events_retry(config: Config, args: argparse.Namespace) -> int:
    """Reopen failed automation events, or one event, for delivery. Phase 17."""
    store = await _store(config)
    try:
        reopened = await store.retry_automation_events(event_id=args.event, all_failed=args.all_failed)
    finally:
        await store.close()
    if not args.event and not args.all_failed:
        print("Nothing to do: give --event N or --all-failed.", file=sys.stderr)
        return EXIT_FAILED
    print(f"Reopened {reopened} event(s); `uv run automation.py` (or `--once`) delivers them.")
    return EXIT_OK


async def command_headers(config: Config, args: argparse.Namespace) -> int:
    """Show how a CSV's columns would map, without reading its rows."""
    path = Path(args.path)
    try:
        first_line = path.read_text(encoding="utf-8-sig").splitlines()[0]
    except (OSError, IndexError) as exc:
        print(f"Cannot read {path}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    import csv as csv_module

    mapping = map_headers(next(csv_module.reader([first_line])))
    for line in mapping.describe():
        print(line)
    if mapping.missing_required:
        print(f"\nMissing required: {', '.join(mapping.missing_required)}")
        return EXIT_FAILED
    return EXIT_OK


async def _counts(config: Config, campaign_id: int):
    """Campaign counts, for a message after an import."""
    store = await _store(config)
    try:
        return await store.campaign_counts(campaign_id)
    finally:
        await store.close()


async def _resolve_campaign_or_fail(config: Config, reference: str):
    """Look a campaign up, printing the failure. Used before opening a second pool."""
    store = await _store(config)
    try:
        campaign = await _resolve_campaign(store, reference)
    finally:
        await store.close()
    if campaign is None:
        print(f"No campaign called {reference!r}.", file=sys.stderr)
    return campaign


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="campaign.py",
        description="Manage prospects, campaigns and the outbound call queue.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="create the campaign tables").set_defaults(run=command_init)

    importer = subparsers.add_parser("import", help="import prospects from a CSV")
    importer.add_argument("path", help="the CSV file")
    importer.add_argument("--campaign", help="also add every imported prospect to this campaign")
    importer.add_argument(
        "--dry-run",
        action="store_true",
        help="show the column mapping and what would be rejected; write nothing",
    )
    importer.add_argument(
        "--show-errors", type=int, default=20, help="how many rejected rows to list (default 20)"
    )
    importer.set_defaults(run=command_import)

    headers = subparsers.add_parser("headers", help="show how a CSV's columns would map")
    headers.add_argument("path", help="the CSV file")
    headers.set_defaults(run=command_headers)

    create = subparsers.add_parser("create", help="create a campaign")
    create.add_argument("name")
    create.add_argument("--description")
    create.set_defaults(run=command_create)

    add = subparsers.add_parser("add", help="add prospects to a campaign")
    add.add_argument("campaign", help="campaign name or id")
    add.add_argument("prospect_ids", nargs="*", help="prospect ids")
    add.add_argument("--all", action="store_true", help="add every prospect that is not DNC")
    add.set_defaults(run=command_add)

    status = subparsers.add_parser("status", help="campaign progress")
    status.add_argument("campaign", nargs="?", help="campaign name or id; omit to list all")
    status.set_defaults(run=command_status)

    for name, target, help_text in (
        ("start", CampaignStatus.ACTIVE, "make a campaign ACTIVE so calls can be queued"),
        ("pause", CampaignStatus.PAUSED, "stop queueing calls, keeping all state"),
        ("complete", CampaignStatus.COMPLETED, "mark a campaign finished"),
        ("cancel", CampaignStatus.CANCELLED, "abandon a campaign"),
    ):
        transition = subparsers.add_parser(name, help=help_text)
        transition.add_argument("campaign", help="campaign name or id")
        transition.set_defaults(run=command_transition, status=target)

    next_up = subparsers.add_parser("next", help="show who would be called next, dialling nothing")
    next_up.add_argument("campaign", help="campaign name or id")
    next_up.add_argument("--limit", type=int, default=25, help="memberships to examine")
    next_up.set_defaults(run=command_next)

    call = subparsers.add_parser("call", help="place the next call(s) for a campaign")
    call.add_argument("campaign", help="campaign name or id")
    call.add_argument("--count", type=int, default=1, help="how many calls to place (default 1)")
    call.add_argument(
        "--no-recover",
        action="store_true",
        help="skip the recovery pass that runs first (Phase 9). Only for debugging: "
        "without it, an attempt left live by an earlier run keeps its prospect blocked.",
    )
    call.set_defaults(run=command_call)

    recover = subparsers.add_parser(
        "recover", help="resolve call attempts left live by a crash or restart (Phase 9)"
    )
    recover.add_argument(
        "--all",
        action="store_true",
        help="include attempts updated seconds ago. Only when you are certain no call is live: "
        "reconciling a call in progress ends it.",
    )
    recover.add_argument("--limit", type=int, default=100, help="attempts to examine (default 100)")
    recover.set_defaults(run=command_recover)

    run = subparsers.add_parser(
        "run", help="place calls unattended for ACTIVE campaigns until stopped (Phase 13)"
    )
    run.add_argument(
        "campaign",
        nargs="*",
        help="campaign name(s) or id(s) to serve; omit to serve every ACTIVE campaign",
    )
    run.add_argument(
        "--max-calls", type=int, default=None, help="stop after placing this many calls"
    )
    run.add_argument(
        "--once",
        action="store_true",
        help="run recovery and one placement pass, then exit without following the calls",
    )
    run.add_argument(
        "--no-auto-complete",
        action="store_true",
        help="leave a campaign ACTIVE when its queue is empty instead of marking it COMPLETED",
    )
    run.set_defaults(run=command_run)

    workers = subparsers.add_parser(
        "workers", help="the scheduler fleet: who is alive, what each holds, and the queue depth (Phase 21)"
    )
    workers.add_argument("--json", action="store_true", help="print as JSON")
    workers.add_argument(
        "--prune",
        type=float,
        default=None,
        metavar="HOURS",
        help="delete stopped/stale worker rows whose last heartbeat is older than this many hours",
    )
    workers.set_defaults(run=command_workers)

    metrics = subparsers.add_parser(
        "metrics", help="throughput, queue depth and worker health from the rows, for a person, JSON or Prometheus (Phase 22)"
    )
    metrics.add_argument("--json", action="store_true", help="print as JSON")
    metrics.add_argument("--prometheus", action="store_true", help="print in the Prometheus text format (for a textfile collector)")
    metrics.add_argument("--hours", type=float, default=None, help="the throughput window in hours (default MONITORING_THROUGHPUT_WINDOW_SECS)")
    metrics.set_defaults(run=command_metrics)

    prospects = subparsers.add_parser("prospects", help="list prospects")
    prospects.add_argument("--limit", type=int, default=50)
    prospects.set_defaults(run=command_prospects)

    attempts = subparsers.add_parser("attempts", help="show call history")
    attempts.add_argument("--campaign", help="campaign name or id")
    attempts.add_argument("--prospect", type=int, help="prospect id")
    attempts.add_argument("--limit", type=int, default=50)
    attempts.set_defaults(run=command_attempts)

    dnc = subparsers.add_parser("dnc", help="never call this prospect (or number) again; goes on the do-not-call list (Phase 19)")
    dnc.add_argument("prospect_id", type=int, nargs="?", help="a prospect id; or use --number")
    dnc.add_argument("--number", help="a phone number with no prospect row, e.g. +923001234567")
    dnc.add_argument("--reason", help="why, for the record")
    dnc.add_argument("--source", default="cli", help="verbal, api, cli, import, registry or manual (default cli)")
    dnc.add_argument("--note", help="anything else worth keeping with the entry")
    dnc.add_argument("--actor", help="who is doing this (default: the OS user)")
    dnc.set_defaults(run=command_dnc)

    dnc_remove = subparsers.add_parser("dnc-remove", help="take a number off the do-not-call list; the row stays, stamped (Phase 19)")
    dnc_remove.add_argument("number")
    dnc_remove.add_argument("--reason", help="why, for the record")
    dnc_remove.add_argument("--reinstate", action="store_true", help="also set the number's prospects back to NEW")
    dnc_remove.add_argument("--actor", help="who is doing this (default: the OS user)")
    dnc_remove.set_defaults(run=command_dnc_remove)

    dnc_list = subparsers.add_parser("dnc-list", help="the do-not-call list, newest first (Phase 19)")
    dnc_list.add_argument("--number", help="one number")
    dnc_list.add_argument("--source", help="only one source: verbal, api, cli, import, registry, manual")
    dnc_list.add_argument("--all", action="store_true", help="include revoked entries")
    dnc_list.add_argument("--limit", type=int, default=50)
    dnc_list.set_defaults(run=command_dnc_list)

    dnc_import = subparsers.add_parser("dnc-import", help="load a suppression file: one number per line, or a CSV with a phone column (Phase 19)")
    dnc_import.add_argument("path")
    dnc_import.add_argument("--source", default="import", help="import (default) or registry")
    dnc_import.add_argument("--reason", help="why, for the record")
    dnc_import.add_argument("--actor", help="who is doing this (default: the OS user)")
    dnc_import.set_defaults(run=command_dnc_import)

    dnc_apply = subparsers.add_parser("dnc-apply", help="mark every prospect whose number is on the list (Phase 19)")
    dnc_apply.add_argument("--actor", help="who is doing this (default: the OS user)")
    dnc_apply.set_defaults(run=command_dnc_apply)

    compliance = subparsers.add_parser("compliance", help="show or set a campaign's compliance settings and the resulting policy (Phase 19)")
    compliance.add_argument("campaign", nargs="?", help="a campaign id or name; omit for the environment's policy")
    compliance.add_argument("--set", action="append", metavar="KEY=VALUE", help="e.g. --set max_attempts=2 --set calling_hours=10:00-17:00 --set ai_disclosure_required=true")
    compliance.add_argument("--clear", action="append", metavar="KEY", help="remove one campaign setting")
    compliance.add_argument("--actor", help="who is doing this (default: the OS user)")
    compliance.set_defaults(run=command_compliance)

    compliance_log = subparsers.add_parser("compliance-log", help="the compliance decisions on the audit log, newest first (Phase 19)")
    compliance_log.add_argument("--action", help="narrow: compliance.blocked, compliance.allowed, compliance.dnc_added, compliance.disclosure")
    compliance_log.add_argument("--actor", help="one actor: dialer, bot, or a user / key label")
    compliance_log.add_argument("--since-hours", type=float, default=None)
    compliance_log.add_argument("--limit", type=int, default=50)
    compliance_log.set_defaults(run=command_compliance_log)

    callbacks = subparsers.add_parser("callbacks", help="scheduled callbacks (Phase 7)")
    callbacks.add_argument("--due", action="store_true", help="only callbacks whose time has come")
    callbacks.add_argument("--all", action="store_true", help="every status, not only pending")
    callbacks.add_argument("--prospect", type=int, help="prospect id")
    callbacks.add_argument("--limit", type=int, default=50)
    callbacks.set_defaults(run=command_callbacks)

    cancel = subparsers.add_parser("cancel-callback", help="withdraw a pending callback")
    cancel.add_argument("callback_id", type=int)
    cancel.set_defaults(run=command_cancel_callback)

    meetings = subparsers.add_parser("meetings", help="meetings the agent booked (Phase 7)")
    meetings.add_argument("--upcoming", action="store_true", help="only meetings from now on")
    meetings.add_argument("--prospect", type=int, help="prospect id")
    meetings.add_argument("--limit", type=int, default=50)
    meetings.set_defaults(run=command_meetings)

    results = subparsers.add_parser("results", help="what finished calls produced (Phase 8)")
    results.add_argument("--campaign", help="campaign name or id")
    results.add_argument("--prospect", type=int, help="prospect id")
    results.add_argument(
        "--disposition",
        choices=[d.value for d in Disposition],
        type=str.upper,
        help="only results with this disposition",
    )
    results.add_argument("--limit", type=int, default=50)
    results.add_argument("--json", action="store_true", help="print the CRM-ready export instead")
    results.set_defaults(run=command_results)

    result = subparsers.add_parser("result", help="one call's full structured result (Phase 8)")
    result.add_argument("attempt_id", type=int, help="the call attempt id, from `attempts`")
    result.add_argument("--transcript", action="store_true", help="print the transcript too")
    result.add_argument("--json", action="store_true", help="print the CRM-ready export instead")
    result.set_defaults(run=command_result)

    webhooks = subparsers.add_parser(
        "webhooks", help="what the carrier has pushed about calls, and what was done with it (Phase 14)"
    )
    webhooks.add_argument("--call", metavar="CALL_ID", help="only deliveries for this carrier call id")
    webhooks.add_argument("--attempt", type=int, help="only deliveries applied to this attempt")
    webhooks.add_argument("--limit", type=int, default=50, help="how many to show (default 50)")
    webhooks.set_defaults(run=command_webhooks)

    crm_sync = subparsers.add_parser("crm-sync", help="file finished calls with the CRM, until stopped (Phase 15)")
    crm_sync.add_argument("--once", action="store_true", help="one pass, then exit (for cron)")
    crm_sync.add_argument("--limit", type=int, default=None, help="results per pass (default CRM_SYNC_BATCH)")
    crm_sync.add_argument("--retry-failed", action="store_true", help="reopen every FAILED row first")
    crm_sync.set_defaults(run=command_crm_sync)

    crm_status = subparsers.add_parser("crm-status", help="what the CRM holds, and what it does not yet (Phase 15)")
    crm_status.add_argument("--state", choices=["PENDING", "SYNCING", "SYNCED", "RETRY", "FAILED", "SKIPPED"], type=str.upper, help="only rows in this state")
    crm_status.add_argument("--limit", type=int, default=50, help="how many rows to show (default 50)")
    crm_status.set_defaults(run=command_crm_status)

    crm_retry = subparsers.add_parser("crm-retry", help="reopen failed CRM sync rows (Phase 15)")
    crm_retry.add_argument("--result", type=int, help="one call result id, in any state")
    crm_retry.add_argument("--all-failed", action="store_true", help="every FAILED row")
    crm_retry.set_defaults(run=command_crm_retry)

    transfers = subparsers.add_parser("transfers", help="transfers to a person, and how each ended (Phase 16)")
    transfers.add_argument("--attempt", type=int, help="only transfers made on this call attempt")
    transfers.add_argument("--limit", type=int, default=50, help="how many to show (default 50)")
    transfers.set_defaults(run=command_transfers)

    events = subparsers.add_parser("events", help="the automation outbox: what n8n has been sent, and what not (Phase 17)")
    events.add_argument("--state", help="only PENDING, DELIVERING, DELIVERED, RETRY, FAILED or SKIPPED")
    events.add_argument("--kind", help="only one event kind, e.g. call.completed")
    events.add_argument("--limit", type=int, default=50, help="how many to show (default 50)")
    events.set_defaults(run=command_events)

    audit = subparsers.add_parser("audit", help="the audit log: who did what, newest first (Phase 18)")
    audit.add_argument("--action", help="an action or a prefix: auth., campaign.start, pii.transcript_read")
    audit.add_argument("--actor", help="one user name or key label, e.g. alice or operator-key#1")
    audit.add_argument("--since-hours", type=float, default=None, help="only the last N hours")
    audit.add_argument("--limit", type=int, default=50, help="how many to show (default 50)")
    audit.set_defaults(run=command_audit)

    events_retry = subparsers.add_parser("events-retry", help="reopen failed automation events for delivery (Phase 17)")
    events_retry.add_argument("--event", type=int, help="reopen this one event, whatever its state")
    events_retry.add_argument("--all-failed", action="store_true", help="reopen every FAILED event")
    events_retry.set_defaults(run=command_events_retry)

    rebuild = subparsers.add_parser("rebuild-results", help="build results for finished attempts that have none (Phase 8)")
    rebuild.add_argument("--all", action="store_true", help="rebuild every finished attempt, replacing what is there")
    rebuild.add_argument("--limit", type=int, default=1000, help="how many recent attempts to examine")
    rebuild.set_defaults(run=command_rebuild_results)

    return parser


def main() -> int:
    """Parse arguments and run the chosen command."""
    args = _parser().parse_args()
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED

    try:
        return asyncio.run(args.run(config, args))
    except CampaignStoreError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
