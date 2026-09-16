"""Campaign state in PostgreSQL, in the shape `knowledge_store.py` established.

Same decisions as the knowledge base, for the same reasons: asyncpg with a pool,
no ORM, an idempotent `create_schema()` run by a CLI rather than a migration
tool, table names as module constants, and every outside value bound as a
parameter. A second persistence style in one project costs more than any ORM
saves at this size.

**Table names are unprefixed** — `prospects`, not `crm_prospects` — while the
knowledge base uses `kb_`. That is deliberate: `kb_` marks tables belonging to a
subsystem you can switch off (`KB_ENABLED=false`), whereas these four are the
application's own state and there is no version of this product without them.
They do not collide with anything.

**Where the queue lives.** `reserve_next_call` is here rather than in the
service layer because the reservation has to be one transaction: choose a
prospect, mark the membership in progress, and write the attempt row together,
or two workers hand out the same person. It uses `FOR UPDATE SKIP LOCKED`, which
is the standard Postgres way to make that safe, and which means adding a second
caller later is a deployment change rather than a rewrite. The *policy* — who is
eligible — is still expressed in one place, in `service.py`, and mirrored here
in SQL because that is the only way to apply it and take the lock at once.
"""

from __future__ import annotations

import functools
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from loguru import logger

from ..compliance.dnc import DncEntry, DncSource, parse_source
from ..conversation.qualification import (
    BuyingTimeline,
    DecisionRole,
    InterestLevel,
    NextAction,
    QualificationStatus,
)
from ..monitoring.instruments import STORE_LATENCY, STORE_OPERATIONS
from ..reliability.observability import event as log_event
from ..security.audit import AuditEntry
from .coordination import (
    CLAIM_LOCK_KEY,
    GLOBAL_PACING_KEY,
    PACING_LOCK_KEY,
    RESERVE_LOCK_KEY,
    WORKER_RUNNING,
    WORKER_STOPPED,
    QueueDepth,
    Throughput,
    WorkerRecord,
    WorkerSummary,
    campaign_pacing_key,
)
from .models import (
    FINAL_STATUS_SQL,
    LIVE_STATUS_SQL,
    REACHED_STATUS_SQL,
    ApiRequestRecord,
    AutomationEvent,
    AutomationEventState,
    CallAttempt,
    CallAttemptStatus,
    CallbackStatus,
    CallTransfer,
    Campaign,
    CampaignProspect,
    CampaignStatus,
    CrmSyncRecord,
    CrmSyncState,
    Meeting,
    MeetingStatus,
    MembershipStatus,
    Prospect,
    ProspectStatus,
    QueuedCall,
    ScheduledCallback,
    TransferStatus,
    WebhookDelivery,
    may_advance,
)
from .results import (
    CallbackOutcome,
    CallResult,
    CallResultValidationError,
    CallSummary,
    Disposition,
    MeetingOutcome,
    ResultSource,
    validate_call_result,
)

# Duplicated from `knowledge_store` rather than imported: it is three lines, and
# reaching across modules for a private helper couples this store to the shape of
# a subsystem it otherwise shares nothing with.
_DSN_PASSWORD = re.compile(r"(?<=://)([^:/@]+):([^@]*)(?=@)")


def _redact(dsn: str) -> str:
    """Hide the password in a DSN so it is safe to put in a log or an error."""
    return _DSN_PASSWORD.sub(r"\1:***", dsn)


PROSPECTS_TABLE = "prospects"
CAMPAIGNS_TABLE = "campaigns"
MEMBERSHIPS_TABLE = "campaign_prospects"
ATTEMPTS_TABLE = "call_attempts"
# Phase 7. Two more tables, created by the same idempotent `create_schema`, so
# an existing database needs `uv run campaign.py init` and nothing else. The
# methods that touch them translate a missing table into a message naming that
# command — see `_phase7`.
CALLBACKS_TABLE = "callbacks"
MEETINGS_TABLE = "meetings"
# Phase 8. One row per finished call attempt: the validated, CRM-ready result.
RESULTS_TABLE = "call_results"
# Phase 14. One row per carrier webhook delivery: the ledger that makes the
# receiver idempotent (`event_key` is unique) and the audit trail of what the
# carrier said about each call and what was done with it.
WEBHOOKS_TABLE = "telephony_webhook_events"
# Phase 15. One row per call result: whether, when and under which ids the
# result was filed with the CRM, and why not when it was not.
CRM_SYNC_TABLE = "crm_sync"
# Phase 16. One row per transfer to a person: requested by the bot, completed
# by the carrier's report of how the colleague's leg ended.
TRANSFERS_TABLE = "call_transfers"
# Phase 17. The outbox: one row per fact an automation platform is told about,
# delivered once from a process of its own. And the replay cache behind the
# automation API's `Idempotency-Key`.
AUTOMATION_EVENTS_TABLE = "automation_events"
API_REQUESTS_TABLE = "api_requests"
#: Phase 18: who did what. Written by the dashboard and the automation API
#: through `src/security/audit.py`; read by `campaign.py audit` and
#: `GET /api/v1/audit`.
AUDIT_TABLE = "audit_log"
#: Phase 19: the do-not-call list, keyed by normalised number. Consulted by
#: the queue's SQL, the pre-dial gate, the importer and the API,
#: independently of the prospect's status.
DNC_TABLE = "dnc_numbers"
#: Phase 21: the heartbeat table and the shared scheduler state (pacing).
WORKERS_TABLE = "scheduler_workers"
STATE_TABLE = "scheduler_state"
#: Phase 27: people who signed up on the application's Register page. Read
#: by the dashboard's login next to `DASHBOARD_USERS`; the hash is
#: `src/security/passwords.py`'s, never a password.
DASHBOARD_USERS_TABLE = "dashboard_users"

#: The SQL that says "this prospect's number is on the active list", as a
#: correlated subquery on `p`. Spliced into the queue and the outlook only
#: when the table exists (`_dnc_clause`), so a database that predates Phase 19
#: keeps working until `campaign.py init` is run — with a warning.
_DNC_EXISTS_SQL = (
    f"EXISTS (SELECT 1 FROM {DNC_TABLE} d WHERE d.phone_normalized = p.phone_normalized "
    f"AND d.revoked_at IS NULL AND (d.expires_at IS NULL OR d.expires_at > now()))"
)

#: The event kinds the outbox knows how to create. Mirrored by
#: `config.AUTOMATION_EVENT_KINDS`; the claim refuses any other name.
AUTOMATION_EVENT_KINDS = (
    "call.completed",
    "call.updated",
    "lead.qualified",
    "meeting.booked",
    "callback.scheduled",
    "campaign.completed",
)

_TABLE_MISSING = (
    "The {table} table does not exist.\n"
    "  This database was created before {phase}. Run:  uv run campaign.py init\n"
    "  (it is idempotent and adds the table without touching your data)."
)


# Phase 17: how each event kind is created from the rows that record the fact.
# `$1` is now, `$2` the settle window in seconds, `$3` the optional `since`.
# Every statement is idempotent on its own — `NOT EXISTS` for the kinds keyed
# on a row, `ON CONFLICT (event_key) DO NOTHING` for every kind — so running
# a pass twice creates nothing the first pass did not.
_EVENT_CREATE_SQL: dict[str, str] = {
    "call.completed": f"""
        INSERT INTO {AUTOMATION_EVENTS_TABLE}
            (event_key, kind, call_result_id, call_attempt_id, prospect_id, campaign_id,
             result_updated_at, occurred_at)
        SELECT 'call.completed:result:' || r.id::text, 'call.completed',
               r.id, r.call_attempt_id, r.prospect_id, r.campaign_id,
               r.updated_at, COALESCE(a.ended_at, r.created_at)
        FROM {RESULTS_TABLE} r
        LEFT JOIN {ATTEMPTS_TABLE} a ON a.id = r.call_attempt_id
        WHERE r.updated_at <= $1::timestamptz - make_interval(secs => $2::float8)
          AND ($3::timestamptz IS NULL OR r.created_at >= $3)
          AND NOT EXISTS (
                SELECT 1 FROM {AUTOMATION_EVENTS_TABLE} e
                WHERE e.kind = 'call.completed' AND e.call_result_id = r.id)
        ON CONFLICT (event_key) DO NOTHING
        """,
    # A result that changed after its `call.completed` was closed. Keyed on
    # the new `updated_at`, so each change is one event; and only one open
    # `call.updated` per result, because a pending one is built from the
    # latest row at delivery anyway.
    "call.updated": f"""
        INSERT INTO {AUTOMATION_EVENTS_TABLE}
            (event_key, kind, call_result_id, call_attempt_id, prospect_id, campaign_id,
             result_updated_at, occurred_at)
        SELECT 'call.updated:result:' || r.id::text || ':'
                   || floor(extract(epoch FROM r.updated_at))::bigint::text,
               'call.updated',
               r.id, r.call_attempt_id, r.prospect_id, r.campaign_id,
               r.updated_at, r.updated_at
        FROM {RESULTS_TABLE} r
        JOIN {AUTOMATION_EVENTS_TABLE} done
          ON done.call_result_id = r.id
         AND done.kind = 'call.completed'
         AND done.state IN ('DELIVERED', 'FAILED', 'SKIPPED')
        WHERE r.updated_at <= $1::timestamptz - make_interval(secs => $2::float8)
          -- `since` already bounded the completed event this hangs off;
          -- repeated here so every statement takes the same three arguments.
          AND ($3::timestamptz IS NULL OR r.updated_at >= $3)
          AND done.result_updated_at IS NOT NULL
          AND r.updated_at > done.result_updated_at
          AND NOT EXISTS (
                SELECT 1 FROM {AUTOMATION_EVENTS_TABLE} e
                WHERE e.kind = 'call.updated' AND e.call_result_id = r.id
                  AND (e.result_updated_at >= r.updated_at
                       OR e.state IN ('PENDING', 'RETRY', 'DELIVERING')))
        ON CONFLICT (event_key) DO NOTHING
        """,
    "lead.qualified": f"""
        INSERT INTO {AUTOMATION_EVENTS_TABLE}
            (event_key, kind, call_result_id, call_attempt_id, prospect_id, campaign_id,
             result_updated_at, occurred_at)
        SELECT 'lead.qualified:result:' || r.id::text, 'lead.qualified',
               r.id, r.call_attempt_id, r.prospect_id, r.campaign_id,
               r.updated_at, COALESCE(a.ended_at, r.created_at)
        FROM {RESULTS_TABLE} r
        LEFT JOIN {ATTEMPTS_TABLE} a ON a.id = r.call_attempt_id
        WHERE r.qualification_status = 'QUALIFIED'
          AND r.updated_at <= $1::timestamptz - make_interval(secs => $2::float8)
          AND ($3::timestamptz IS NULL OR r.created_at >= $3)
          AND NOT EXISTS (
                SELECT 1 FROM {AUTOMATION_EVENTS_TABLE} e
                WHERE e.kind = 'lead.qualified' AND e.call_result_id = r.id)
        ON CONFLICT (event_key) DO NOTHING
        """,
    # No settle window: the meeting row is written only after the calendar
    # confirmed the booking, and the sooner the CRM knows the better.
    "meeting.booked": f"""
        INSERT INTO {AUTOMATION_EVENTS_TABLE}
            (event_key, kind, meeting_id, call_attempt_id, prospect_id, campaign_id, occurred_at)
        SELECT 'meeting.booked:meeting:' || m.id::text, 'meeting.booked',
               m.id, m.call_attempt_id, m.prospect_id, m.campaign_id, m.created_at
        FROM {MEETINGS_TABLE} m
        WHERE m.status = 'BOOKED'
          -- $1 and $2 (now, the settle window) play no part here; they are
          -- named so every statement takes the same three arguments.
          AND $1::timestamptz IS NOT NULL AND $2::float8 IS NOT NULL
          AND ($3::timestamptz IS NULL OR m.created_at >= $3)
          AND NOT EXISTS (
                SELECT 1 FROM {AUTOMATION_EVENTS_TABLE} e
                WHERE e.kind = 'meeting.booked' AND e.meeting_id = m.id)
        ON CONFLICT (event_key) DO NOTHING
        """,
    # Keyed on the time as well as the row: a callback that is moved is a
    # new promise, and the receiver should hear about it.
    "callback.scheduled": f"""
        INSERT INTO {AUTOMATION_EVENTS_TABLE}
            (event_key, kind, callback_id, call_attempt_id, prospect_id, campaign_id, occurred_at)
        SELECT 'callback.scheduled:callback:' || c.id::text || ':'
                   || floor(extract(epoch FROM c.scheduled_for))::bigint::text,
               'callback.scheduled',
               c.id, c.call_attempt_id, c.prospect_id, c.campaign_id, c.updated_at
        FROM {CALLBACKS_TABLE} c
        WHERE c.status = 'PENDING'
          AND $1::timestamptz IS NOT NULL AND $2::float8 IS NOT NULL
          AND ($3::timestamptz IS NULL OR c.updated_at >= $3)
        ON CONFLICT (event_key) DO NOTHING
        """,
    "campaign.completed": f"""
        INSERT INTO {AUTOMATION_EVENTS_TABLE}
            (event_key, kind, campaign_id, occurred_at)
        SELECT 'campaign.completed:campaign:' || c.id::text || ':'
                   || floor(extract(epoch FROM c.completed_at))::bigint::text,
               'campaign.completed', c.id, c.completed_at
        FROM {CAMPAIGNS_TABLE} c
        WHERE c.status = 'COMPLETED' AND c.completed_at IS NOT NULL
          AND $1::timestamptz IS NOT NULL AND $2::float8 IS NOT NULL
          AND ($3::timestamptz IS NULL OR c.completed_at >= $3)
        ON CONFLICT (event_key) DO NOTHING
        """,
}


class CampaignStoreError(RuntimeError):
    """The campaign database is unreachable or not set up. Message is for the user."""


class _AlreadyReserved(Exception):
    """Internal: this exact attempt already exists, so nothing was reserved.

    Never leaves `reserve_next_call`. Raised inside the transaction so that the
    membership's attempt count rolls back with the refused insert, and turned
    into `None` — an empty queue — by the caller a few lines later, because
    "somebody else already holds this call" and "there is nothing to call" are
    the same instruction to a dialer: do nothing.
    """


class MeetingConflictError(CampaignStoreError):
    """The slot is already taken in this system's own diary. Phase 16.

    Raised by `add_meeting` when the `meetings` table's exclusion constraint
    refuses an overlapping local booking — the write itself is the
    double-booking check, so two bots booking one slot in the same second get
    one row and one of these. The action service turns it into `slot_taken`.
    """


class DuplicateUserError(CampaignStoreError):
    """A dashboard user with this name or email already exists. Phase 27.

    Its own type because it is the Register page's expected refusal, not a
    failure: the route turns it into a 409 that names the field.
    """

    def __init__(self, field: str, value: str) -> None:
        """Record which field clashed; the value is the name or the email as given."""
        super().__init__(f"a user with that {field} already exists")
        self.field = field
        self.value = value


class DuplicateProspectError(CampaignStoreError):
    """A prospect with this phone number already exists.

    Its own type because it is the expected outcome of importing a list twice,
    not a failure: the importer counts these and carries on.
    """

    def __init__(self, phone: str, existing_id: int) -> None:
        """Record which number clashed and which row already holds it."""
        super().__init__(f"a prospect with phone {phone} already exists (id {existing_id})")
        self.phone = phone
        self.existing_id = existing_id


@dataclass(frozen=True)
class CampaignCounts:
    """How a campaign is progressing, for a status line."""

    total: int
    pending: int
    in_progress: int
    completed: int
    exhausted: int
    skipped: int


def campaign_concurrency(raw: object) -> int:
    """A campaign's own live-call ceiling from its `configuration` JSON. Phase 25.

    `configuration.max_concurrent_calls`, an integer; anything else (unset,
    empty, a word, a negative) is 0, meaning the deployment's
    `MAX_CONCURRENT_CALLS` alone applies. Read by the reservation under the
    advisory lock, so the same rule must hold for the in-memory store the
    checks use; that is why it is a function and not a line of SQL.
    """
    if raw is None or isinstance(raw, bool):
        return 0
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, 100))


# The counters `campaign_progress` returns, in the order a page shows them.
# Zero for every key when a campaign has no rows, so a reader never has to
# ask whether a key exists.
PROGRESS_KEYS = (
    "contacts", "pending", "queued", "scheduled", "in_progress",
    "members_completed", "exhausted", "skipped",
    "attempts", "reserved", "calling", "connected", "unresolved", "live",
    "answered", "completed", "failed", "no_answer", "busy", "voicemail",
    "not_interested", "do_not_call", "callback_requested",
)


@dataclass(frozen=True)
class QueueOutlook:
    """What a campaign's queue holds right now, and when it next has work. Phase 13.

    One query, asked by the scheduler after the queue hands out nothing, to
    tell four situations apart that "nothing eligible" does not: work that is
    due but blocked by a live call elsewhere, work that becomes due later,
    memberships that can never be dialled, and a campaign that is finished.

    Attributes:
        total: Memberships in the campaign.
        pending: `PENDING` memberships, due or not.
        in_progress: Memberships with a call live right now.
        due_now: Pending memberships the queue would hand out this moment,
            ignoring the live-call and concurrency checks.
        next_due_at: The earliest future `next_attempt_at` among dialable
            pending memberships, or None. The scheduler sleeps until it.
        undialable: Pending memberships the queue will never hand out — the
            prospect is do-not-call, has no usable number, or has used every
            attempt and has no pending callback to justify one more.
        pending_callbacks: `PENDING` callbacks for this campaign, due or not.
        next_callback_at: The earliest of them, or None.
    """

    total: int = 0
    pending: int = 0
    in_progress: int = 0
    due_now: int = 0
    next_due_at: datetime | None = None
    undialable: int = 0
    pending_callbacks: int = 0
    next_callback_at: datetime | None = None

    @property
    def has_live_work(self) -> bool:
        """Whether anything is on the phone, due, or scheduled for later."""
        return bool(self.in_progress or self.due_now or self.next_due_at or self.pending_callbacks)

    @property
    def is_finished(self) -> bool:
        """Whether the campaign has nothing left it could ever dial.

        Requires at least one membership: a campaign that was started before
        its list was imported is empty, not finished.
        """
        return self.total > 0 and not self.has_live_work and self.pending == self.undialable


def _timed(operation: str) -> Callable[[Any], Any]:
    """Count and time one store method under `operation`. Phase 22.

    On the writes a call's trace passes through — the reservation, the
    placement, the carrier's event, the conversation record, the result,
    the usage, the ledger, the CRM and automation rows. Each becomes one
    `aiva_store_operations_total{operation,outcome}` increment, one
    `aiva_store_seconds` observation, and one `store.op` line at DEBUG that
    carries whatever call context the caller bound — the *database* hop of
    a trace. The exception, if any, is re-raised untouched.
    """

    def wrap(method: Any) -> Any:
        @functools.wraps(method)
        async def timed(self: Any, *args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            outcome = "ok"
            try:
                return await method(self, *args, **kwargs)
            except Exception as exc:
                outcome = exc.__class__.__name__
                raise
            finally:
                elapsed = time.monotonic() - started
                STORE_OPERATIONS.inc(operation=operation, outcome=outcome)
                STORE_LATENCY.observe(elapsed, operation=operation)
                logger.debug(
                    log_event("store.op", operation=operation, outcome=outcome, latency_ms=int(round(elapsed * 1000)))
                )

        return timed

    return wrap


class CampaignStore:
    """Async access to prospects, campaigns, memberships and call attempts."""

    def __init__(self, pool: asyncpg.Pool, *, owns_pool: bool = True) -> None:
        """Prefer `CampaignStore.connect`; this takes an already-built pool.

        Args:
            pool: The connection pool to use.
            owns_pool: Whether `close()` should close it. False when the pool is
                shared with another store — see `connect(pool=...)`.
        """
        self._pool = pool
        self._owns_pool = owns_pool
        # Phase 19: whether `dnc_numbers` exists, learned once. None until asked.
        self._dnc_table_present: bool | None = None
        # Phase 22: the missing-column warning for `set_attempt_trace`, said once.
        self._warned_trace_column = False

    @classmethod
    async def connect(
        cls,
        dsn: str,
        *,
        create_schema: bool = False,
        min_size: int = 1,
        max_size: int = 4,
        timeout: float = 10.0,
        pool: asyncpg.Pool | None = None,
    ) -> CampaignStore:
        """Open a pool and check the schema is there.

        Args:
            dsn: `postgresql://user:password@host:port/database`.
            create_schema: Create the tables if missing. The CLI's `init` passes
                True; everything else passes False, so no command silently
                creates an empty campaign database it should have found.
            min_size: Connections held open.
            max_size: Connection ceiling.
            timeout: Seconds to wait for the initial connection.
            pool: An existing pool to share instead of opening one (Phase 11).
                The store then does **not** close it — whoever opened it does.
                `bot.py` passes the knowledge base's pool here when both point
                at the same database, which halves the connections a call holds.

        Raises:
            CampaignStoreError: Unreachable, or the schema is missing.
        """
        owns_pool = pool is None
        if pool is None:
            try:
                pool = await asyncpg.create_pool(
                    dsn,
                    min_size=min_size,
                    max_size=max_size,
                    timeout=timeout,
                    command_timeout=timeout,
                )
            except (OSError, asyncpg.PostgresError) as exc:
                raise CampaignStoreError(
                    f"Could not connect to the campaign database at {_redact(dsn)}.\n"
                    f"  Check that PostgreSQL is running and that DATABASE_URL is right.\n"
                    f"  Underlying error: {exc}"
                ) from exc

            if pool is None:  # asyncpg types this as optional.
                raise CampaignStoreError(f"Could not connect to {_redact(dsn)}.")

        store = cls(pool, owns_pool=owns_pool)
        try:
            if create_schema:
                await store.create_schema()
            await store._verify()
        except Exception:
            if owns_pool:
                await pool.close()
            raise
        return store

    async def close(self) -> None:
        """Close the pool, if this store opened it. Safe to call more than once."""
        if self._owns_pool:
            await self._pool.close()

    async def create_schema(self) -> None:
        """Create the four tables and their indexes if they are not there. Idempotent."""
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {PROSPECTS_TABLE} (
                    id               bigserial PRIMARY KEY,
                    first_name       text NOT NULL,
                    last_name        text NOT NULL,
                    -- The number exactly as imported. Never rewritten: a
                    -- normalisation that went wrong is only diagnosable
                    -- against the original.
                    phone            text NOT NULL,
                    -- E.164, or NULL when it could not be normalised safely.
                    -- NULL is what makes a prospect undialable, and Postgres
                    -- allows many NULLs under a UNIQUE constraint, so any
                    -- number of unusable numbers can coexist while dialable
                    -- ones stay unique.
                    phone_normalized text UNIQUE,
                    email            text,
                    company          text,
                    job_title        text,
                    industry         text,
                    location         text,
                    website          text,
                    custom_data      jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
                    status           text        NOT NULL DEFAULT 'NEW',
                    created_at       timestamptz NOT NULL DEFAULT now(),
                    updated_at       timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            # Email is indexed for lookup but NOT unique: several contacts at one
            # company legitimately share an info@ address, and a unique
            # constraint would reject that whole import. Duplicates are reported
            # by the importer instead, where a person can judge them.
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {PROSPECTS_TABLE}_email_idx "
                f"ON {PROSPECTS_TABLE} (lower(email)) WHERE email IS NOT NULL"
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {PROSPECTS_TABLE}_status_idx "
                f"ON {PROSPECTS_TABLE} (status)"
            )

            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {CAMPAIGNS_TABLE} (
                    id            bigserial PRIMARY KEY,
                    name          text        NOT NULL UNIQUE,
                    description   text,
                    status        text        NOT NULL DEFAULT 'DRAFT',
                    configuration jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at    timestamptz NOT NULL DEFAULT now(),
                    updated_at    timestamptz NOT NULL DEFAULT now(),
                    started_at    timestamptz,
                    paused_at     timestamptz,
                    completed_at  timestamptz
                )
                """
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {CAMPAIGNS_TABLE}_status_idx "
                f"ON {CAMPAIGNS_TABLE} (status)"
            )

            # The join table that keeps campaign state off the prospect. UNIQUE
            # on the pair stops a prospect being added to one campaign twice;
            # it deliberately does not stop the same prospect joining several
            # campaigns, which is the reuse this table exists for.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {MEMBERSHIPS_TABLE} (
                    id              bigserial PRIMARY KEY,
                    campaign_id     bigint NOT NULL
                                    REFERENCES {CAMPAIGNS_TABLE}(id) ON DELETE CASCADE,
                    prospect_id     bigint NOT NULL
                                    REFERENCES {PROSPECTS_TABLE}(id) ON DELETE CASCADE,
                    status          text        NOT NULL DEFAULT 'PENDING',
                    attempt_count   integer     NOT NULL DEFAULT 0,
                    last_attempt_at timestamptz,
                    next_attempt_at timestamptz,
                    created_at      timestamptz NOT NULL DEFAULT now(),
                    updated_at      timestamptz NOT NULL DEFAULT now(),
                    UNIQUE (campaign_id, prospect_id)
                )
                """
            )
            # The queue's own index: it looks for open memberships of one
            # campaign whose retry time has come.
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {MEMBERSHIPS_TABLE}_queue_idx "
                f"ON {MEMBERSHIPS_TABLE} (campaign_id, status, next_attempt_at)"
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {MEMBERSHIPS_TABLE}_prospect_idx "
                f"ON {MEMBERSHIPS_TABLE} (prospect_id)"
            )

            # Attempts outlive their campaign and their membership on purpose:
            # the call history is the record of what was done to a person, and
            # deleting a campaign should not erase that. Hence ON DELETE SET
            # NULL rather than CASCADE, and nullable campaign columns.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {ATTEMPTS_TABLE} (
                    id                   bigserial PRIMARY KEY,
                    prospect_id          bigint NOT NULL
                                         REFERENCES {PROSPECTS_TABLE}(id) ON DELETE CASCADE,
                    campaign_id          bigint
                                         REFERENCES {CAMPAIGNS_TABLE}(id) ON DELETE SET NULL,
                    campaign_prospect_id bigint
                                         REFERENCES {MEMBERSHIPS_TABLE}(id) ON DELETE SET NULL,
                    attempt_number       integer     NOT NULL DEFAULT 1,
                    status               text        NOT NULL DEFAULT 'PENDING',
                    -- The carrier's id. Unique so that a status callback or a
                    -- reconciliation job can look an attempt up by it, and so
                    -- the same call cannot be recorded twice.
                    telephony_call_id    text UNIQUE,
                    telephony_provider   text,
                    started_at           timestamptz,
                    connected_at         timestamptz,
                    ended_at             timestamptz,
                    duration_seconds     integer,
                    failure_reason       text,
                    created_at           timestamptz NOT NULL DEFAULT now(),
                    updated_at           timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            # Added in Phase 6, when something finally had a conversation to
            # store. `ADD COLUMN IF NOT EXISTS` rather than a new table because
            # this is one nullable column on a row that already exists for
            # exactly this call — and because `campaign.py init` is idempotent
            # and already the documented way to bring a schema up to date, so an
            # existing database needs one command and no migration tool.
            #
            # Nullable and never backfilled: an attempt from before this phase
            # genuinely has no conversation record, and NULL says so. Everything
            # that reads it treats a missing column as NULL too, so a database
            # that has not been re-initialised still works.
            await connection.execute(
                f"ALTER TABLE {ATTEMPTS_TABLE} ADD COLUMN IF NOT EXISTS conversation_data jsonb"
            )
            # Phase 21: which worker process is following the attempt.
            # Nullable: a one-off dial has no worker, and a dead worker's calls
            # are handed on by clearing it.
            await connection.execute(
                f"ALTER TABLE {ATTEMPTS_TABLE} ADD COLUMN IF NOT EXISTS worker_id text"
            )
            # Phase 22: the correlation id every process logs for this call.
            # Nullable: rows from before the phase, and reservations never
            # dialled, have none. Written by the dialer, read by whoever
            # starts from the row.
            await connection.execute(
                f"ALTER TABLE {ATTEMPTS_TABLE} ADD COLUMN IF NOT EXISTS trace_id text"
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {ATTEMPTS_TABLE}_prospect_idx "
                f"ON {ATTEMPTS_TABLE} (prospect_id, created_at DESC)"
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {ATTEMPTS_TABLE}_campaign_idx "
                f"ON {ATTEMPTS_TABLE} (campaign_id, status)"
            )
            # Phase 9: the duplicate-call protection that does not depend on a
            # lock being taken. `idempotency_key` is what an attempt *is* —
            # "the third attempt at membership 12" — computed identically by any
            # caller that means the same call, so two workers, a retried API
            # request or a restart mid-reservation resolve to one row. Nullable
            # because attempts written before Phase 9, and one-off manual dials
            # that supply no key, are real rows with no key to give.
            await connection.execute(
                f"ALTER TABLE {ATTEMPTS_TABLE} ADD COLUMN IF NOT EXISTS idempotency_key text"
            )
            # Phase 9: stamped before the carrier is asked to dial, so an
            # attempt whose process died mid-request is still recognisable as
            # one that may have placed a call. `started_at` cannot serve: it is
            # set when placement *succeeds*.
            await connection.execute(
                f"ALTER TABLE {ATTEMPTS_TABLE} ADD COLUMN IF NOT EXISTS "
                f"placement_started_at timestamptz"
            )
            # Phase 11: what the call consumed, and what that cost. On the
            # *attempt* rather than on the call result because it is operational
            # rather than commercial — a CRM wants to know the prospect was
            # qualified, an operator wants to know the call spent 6,000 tokens.
            #
            # `cost_usd` is nullable and stays null unless per-unit rates are
            # configured: a price is a commercial arrangement this code cannot
            # know, and a guessed one would be worse than none. See
            # `reliability/usage.py`.
            await connection.execute(
                f"ALTER TABLE {ATTEMPTS_TABLE} ADD COLUMN IF NOT EXISTS usage jsonb"
            )
            await connection.execute(
                f"ALTER TABLE {ATTEMPTS_TABLE} ADD COLUMN IF NOT EXISTS cost_usd numeric(12, 6)"
            )
            # A partial unique index rather than a column constraint: many rows
            # legitimately have no key, and Postgres would allow only one NULL
            # under a plain UNIQUE... it would allow many, in fact, but the
            # partial form says the intent out loud and keeps the index small.
            await connection.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {ATTEMPTS_TABLE}_idempotency_idx "
                f"ON {ATTEMPTS_TABLE} (idempotency_key) WHERE idempotency_key IS NOT NULL"
            )
            # Partial index over live attempts only: "is this prospect on a call
            # right now" is asked before every dial, and the live set stays tiny
            # however long the history grows.
            #
            # Dropped and recreated because Phase 9 added `UNRESOLVED` to the
            # live set: a partial index is only used for a query whose predicate
            # matches, so an index left with the old four-status predicate would
            # silently stop being used by the query that needs it most.
            # `CREATE INDEX IF NOT EXISTS` would not have updated it.
            await connection.execute(f"DROP INDEX IF EXISTS {ATTEMPTS_TABLE}_live_idx")
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {ATTEMPTS_TABLE}_live_idx "
                f"ON {ATTEMPTS_TABLE} (prospect_id) "
                f"WHERE status IN ({LIVE_STATUS_SQL})"
            )
            # Phase 9: recovery's query — every live attempt, oldest first,
            # across all campaigns.
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {ATTEMPTS_TABLE}_recovery_idx "
                f"ON {ATTEMPTS_TABLE} (updated_at) WHERE status IN ({LIVE_STATUS_SQL})"
            )

            # Phase 7: a callback is a promise to a person, so it survives its
            # campaign and its attempt (SET NULL) but not the person (CASCADE —
            # deleting the prospect is the one case where calling them back
            # cannot be right). The partial unique index is the rule "one
            # pending callback per prospect": asking twice moves the time.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {CALLBACKS_TABLE} (
                    id                   bigserial PRIMARY KEY,
                    prospect_id          bigint NOT NULL
                                         REFERENCES {PROSPECTS_TABLE}(id) ON DELETE CASCADE,
                    campaign_id          bigint
                                         REFERENCES {CAMPAIGNS_TABLE}(id) ON DELETE SET NULL,
                    call_attempt_id      bigint
                                         REFERENCES {ATTEMPTS_TABLE}(id) ON DELETE SET NULL,
                    campaign_prospect_id bigint
                                         REFERENCES {MEMBERSHIPS_TABLE}(id) ON DELETE SET NULL,
                    scheduled_for        timestamptz NOT NULL,
                    status               text        NOT NULL DEFAULT 'PENDING',
                    note                 text,
                    created_at           timestamptz NOT NULL DEFAULT now(),
                    updated_at           timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {CALLBACKS_TABLE}_due_idx "
                f"ON {CALLBACKS_TABLE} (status, scheduled_for)"
            )
            await connection.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {CALLBACKS_TABLE}_one_pending_idx "
                f"ON {CALLBACKS_TABLE} (prospect_id) WHERE status = 'PENDING'"
            )

            # Phase 7: a meeting row is written only after the calendar
            # confirmed the booking. `prospect_id` is nullable because a
            # booking made on an anonymous session is still a real booking in
            # the calendar and deserves a record here.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {MEETINGS_TABLE} (
                    id               bigserial PRIMARY KEY,
                    prospect_id      bigint
                                     REFERENCES {PROSPECTS_TABLE}(id) ON DELETE SET NULL,
                    campaign_id      bigint
                                     REFERENCES {CAMPAIGNS_TABLE}(id) ON DELETE SET NULL,
                    call_attempt_id  bigint
                                     REFERENCES {ATTEMPTS_TABLE}(id) ON DELETE SET NULL,
                    provider         text        NOT NULL,
                    reference        text,
                    start_at         timestamptz NOT NULL,
                    end_at           timestamptz NOT NULL,
                    timezone         text        NOT NULL DEFAULT 'UTC',
                    status           text        NOT NULL DEFAULT 'BOOKED',
                    attendee_name    text,
                    attendee_email   text,
                    notes            text,
                    created_at       timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            # The local calendar asks "what is booked in this window" before
            # every offer; the partial index keeps that a lookup over live
            # bookings only.
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {MEETINGS_TABLE}_time_idx "
                f"ON {MEETINGS_TABLE} (start_at, end_at) WHERE status = 'BOOKED'"
            )
            # Phase 16: the local calendar's double-booking guard, in the
            # write itself. Two live local bookings may not overlap; a
            # cancelled one, or a Cal.com mirror (whose diary is Cal.com's),
            # is outside the rule. A range exclusion needs nothing beyond
            # core PostgreSQL. An existing database whose rows already
            # overlap cannot take the constraint; that is reported, not
            # fatal, because `init` must still bring every other table up.
            try:
                async with connection.transaction():
                    await connection.execute(
                        f"""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM pg_constraint
                                WHERE conname = '{MEETINGS_TABLE}_no_double_booking'
                                  AND conrelid = '{MEETINGS_TABLE}'::regclass
                            ) THEN
                                ALTER TABLE {MEETINGS_TABLE}
                                    ADD CONSTRAINT {MEETINGS_TABLE}_no_double_booking
                                    EXCLUDE USING gist (tstzrange(start_at, end_at) WITH &&)
                                    WHERE (status = 'BOOKED' AND provider = 'local');
                            END IF;
                        END $$
                        """
                    )
            except asyncpg.ExclusionViolationError as exc:
                logger.warning(
                    f"MEETINGS | the no-double-booking constraint could not be added: existing "
                    f"local bookings overlap ({exc}). Cancel one of them and run `campaign.py "
                    f"init` again; until then the local calendar checks before it writes, as before."
                )

            # Phase 8: the structured result of a finished call, one per
            # attempt (UNIQUE), gone with the attempt (CASCADE) because it is a
            # reading of that attempt and nothing else. Typed columns for
            # everything a CRM filters or maps on; JSON for the lists and for
            # the transcript, which is stored verbatim and never edited. The
            # `source` column is what the upsert's precedence rule reads: a
            # conversation result always replaces a carrier one and a carrier
            # one never replaces a conversation one — see `save_call_result`.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
                    id                     bigserial PRIMARY KEY,
                    call_attempt_id        bigint NOT NULL UNIQUE
                                           REFERENCES {ATTEMPTS_TABLE}(id) ON DELETE CASCADE,
                    prospect_id            bigint NOT NULL
                                           REFERENCES {PROSPECTS_TABLE}(id) ON DELETE CASCADE,
                    campaign_id            bigint
                                           REFERENCES {CAMPAIGNS_TABLE}(id) ON DELETE SET NULL,
                    source                 text        NOT NULL,
                    schema_version         integer     NOT NULL DEFAULT 1,
                    call_status            text        NOT NULL,
                    disposition            text        NOT NULL,
                    duration_seconds       integer,
                    failure_reason         text,
                    qualification_status   text        NOT NULL DEFAULT 'UNKNOWN',
                    interest_level         text        NOT NULL DEFAULT 'UNKNOWN',
                    buying_timeline        text        NOT NULL DEFAULT 'UNKNOWN',
                    decision_role          text        NOT NULL DEFAULT 'UNKNOWN',
                    next_action            text        NOT NULL DEFAULT 'UNKNOWN',
                    meeting_status         text        NOT NULL DEFAULT 'UNKNOWN',
                    meeting_start          timestamptz,
                    meeting_reference      text,
                    meeting_when           text,
                    callback_status        text        NOT NULL DEFAULT 'UNKNOWN',
                    callback_scheduled_for timestamptz,
                    callback_when          text,
                    pain_points            jsonb       NOT NULL DEFAULT '[]'::jsonb,
                    objections             jsonb       NOT NULL DEFAULT '[]'::jsonb,
                    questions              jsonb       NOT NULL DEFAULT '[]'::jsonb,
                    existing_provider      text,
                    current_process        text,
                    impact                 text,
                    desired_outcome        text,
                    notes                  jsonb       NOT NULL DEFAULT '[]'::jsonb,
                    -- Tri-state on purpose: NULL is "could not be known",
                    -- which is the usual value on a call nobody answered.
                    human_requested        boolean,
                    transferred            boolean,
                    agent_ended_call       boolean,
                    caller_turns           integer,
                    agent_turns            integer,
                    final_state            text,
                    timezone               text        NOT NULL DEFAULT 'UTC',
                    summary                jsonb       NOT NULL,
                    summary_text           text        NOT NULL,
                    transcript             jsonb       NOT NULL DEFAULT '[]'::jsonb,
                    tool_actions           jsonb       NOT NULL DEFAULT '[]'::jsonb,
                    issues                 jsonb       NOT NULL DEFAULT '[]'::jsonb,
                    created_at             timestamptz NOT NULL DEFAULT now(),
                    updated_at             timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {RESULTS_TABLE}_prospect_idx "
                f"ON {RESULTS_TABLE} (prospect_id, created_at DESC)"
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {RESULTS_TABLE}_campaign_idx "
                f"ON {RESULTS_TABLE} (campaign_id, disposition)"
            )

            # Phase 14: the webhook ledger. `event_key` is unique, which is
            # what makes a redelivered event a no-op at the database rather
            # than a matter of care in the receiver. Deliveries outlive their
            # attempt (SET NULL): an event about a call is a fact about the
            # carrier's behaviour, worth keeping even if the attempt row goes.
            # No foreign key on `call_id`: the carrier sends events for calls
            # this database never placed (`call.py`, an inbound call), and
            # those are recorded as `unmatched` rather than refused.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {WEBHOOKS_TABLE} (
                    id                 bigserial PRIMARY KEY,
                    provider           text        NOT NULL,
                    call_id            text        NOT NULL,
                    event_key          text        NOT NULL UNIQUE,
                    kind               text        NOT NULL,
                    status             text,
                    raw_status         text,
                    sequence           integer,
                    carrier_timestamp  timestamptz,
                    answered_by        text,
                    duration_seconds   integer,
                    outcome            text        NOT NULL DEFAULT 'received',
                    attempt_id         bigint
                                       REFERENCES {ATTEMPTS_TABLE}(id) ON DELETE SET NULL,
                    payload            jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
                    received_at        timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            # The worker's question — "has the carrier pushed anything for
            # this call, and when" — asked once per followed call per tick.
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {WEBHOOKS_TABLE}_call_idx "
                f"ON {WEBHOOKS_TABLE} (call_id, received_at DESC)"
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {WEBHOOKS_TABLE}_attempt_idx "
                f"ON {WEBHOOKS_TABLE} (attempt_id) WHERE attempt_id IS NOT NULL"
            )

            # Phase 15: one row per call result, unique, so a result is filed
            # with the CRM once however many syncers run. CASCADE with the
            # result: a sync record without its result means nothing.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {CRM_SYNC_TABLE} (
                    id                    bigserial PRIMARY KEY,
                    call_result_id        bigint NOT NULL UNIQUE
                                          REFERENCES {RESULTS_TABLE}(id) ON DELETE CASCADE,
                    call_attempt_id       bigint NOT NULL,
                    prospect_id           bigint NOT NULL,
                    provider              text        NOT NULL,
                    state                 text        NOT NULL DEFAULT 'PENDING',
                    sync_key              text        NOT NULL,
                    external_contact_id   text,
                    external_activity_id  text,
                    attempts              integer     NOT NULL DEFAULT 0,
                    last_error            text,
                    next_attempt_at       timestamptz,
                    started_at            timestamptz,
                    synced_at             timestamptz,
                    result_updated_at     timestamptz,
                    created_at            timestamptz NOT NULL DEFAULT now(),
                    updated_at            timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            # The syncer's claim: open rows by due time. Small, because the
            # open set is what has not been filed yet.
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {CRM_SYNC_TABLE}_due_idx "
                f"ON {CRM_SYNC_TABLE} (state, next_attempt_at)"
            )

            # Phase 16: transfers to a person. Outlives its attempt (SET NULL):
            # "we handed this call to a colleague and they answered" is a fact
            # about the carrier's behaviour worth keeping. Keyed by the call id
            # because that is all the carrier's report carries.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TRANSFERS_TABLE} (
                    id                 bigserial PRIMARY KEY,
                    call_attempt_id    bigint
                                       REFERENCES {ATTEMPTS_TABLE}(id) ON DELETE SET NULL,
                    prospect_id        bigint
                                       REFERENCES {PROSPECTS_TABLE}(id) ON DELETE SET NULL,
                    telephony_call_id  text        NOT NULL,
                    provider           text        NOT NULL,
                    to_number          text        NOT NULL,
                    reason             text,
                    status             text        NOT NULL DEFAULT 'REQUESTED',
                    dial_call_id       text,
                    duration_seconds   integer,
                    error              text,
                    requested_at       timestamptz NOT NULL DEFAULT now(),
                    completed_at       timestamptz,
                    updated_at         timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {TRANSFERS_TABLE}_call_idx "
                f"ON {TRANSFERS_TABLE} (telephony_call_id, requested_at DESC)"
            )

            # Phase 17: the outbox for an automation platform (n8n). One row
            # per fact worth telling it about — a finished call, a qualified
            # lead, a booked meeting, a scheduled callback, a completed
            # campaign — created from the rows that already record the fact,
            # never from inside a call. `event_key` is unique, so a fact
            # becomes one event however many deliverers look, and the
            # receiver sees the same `event_id` on every redelivery. A result
            # takes its events with it (CASCADE); an attempt, prospect or
            # campaign that goes leaves the event as a record of what was
            # sent (SET NULL).
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {AUTOMATION_EVENTS_TABLE} (
                    id                 bigserial PRIMARY KEY,
                    event_key          text        NOT NULL UNIQUE,
                    kind               text        NOT NULL,
                    state              text        NOT NULL DEFAULT 'PENDING',
                    call_result_id     bigint
                                       REFERENCES {RESULTS_TABLE}(id) ON DELETE CASCADE,
                    call_attempt_id    bigint
                                       REFERENCES {ATTEMPTS_TABLE}(id) ON DELETE SET NULL,
                    prospect_id        bigint
                                       REFERENCES {PROSPECTS_TABLE}(id) ON DELETE SET NULL,
                    campaign_id        bigint
                                       REFERENCES {CAMPAIGNS_TABLE}(id) ON DELETE SET NULL,
                    meeting_id         bigint
                                       REFERENCES {MEETINGS_TABLE}(id) ON DELETE CASCADE,
                    callback_id        bigint
                                       REFERENCES {CALLBACKS_TABLE}(id) ON DELETE CASCADE,
                    result_updated_at  timestamptz,
                    occurred_at        timestamptz NOT NULL DEFAULT now(),
                    payload            jsonb,
                    target_url         text,
                    attempts           integer     NOT NULL DEFAULT 0,
                    last_status        integer,
                    last_error         text,
                    next_attempt_at    timestamptz,
                    started_at         timestamptz,
                    delivered_at       timestamptz,
                    created_at         timestamptz NOT NULL DEFAULT now(),
                    updated_at         timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            # The deliverer's claim: open rows by due time. Small, because the
            # open set is what has not been delivered yet.
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {AUTOMATION_EVENTS_TABLE}_due_idx "
                f"ON {AUTOMATION_EVENTS_TABLE} (state, next_attempt_at)"
            )
            # The "does this result already have an event of this kind" test
            # the claim's first statement runs for every result.
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {AUTOMATION_EVENTS_TABLE}_result_idx "
                f"ON {AUTOMATION_EVENTS_TABLE} (call_result_id, kind)"
            )

            # Phase 17: the replay cache behind the automation API. A request
            # that carried an `Idempotency-Key` has its answer kept here, so a
            # client that retries after a lost answer gets the same one. Rows
            # expire (`purge_api_requests`); nothing else reads them.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {API_REQUESTS_TABLE} (
                    id               bigserial PRIMARY KEY,
                    scope            text        NOT NULL,
                    idempotency_key  text        NOT NULL,
                    fingerprint      text        NOT NULL,
                    status_code      integer     NOT NULL,
                    response         jsonb       NOT NULL,
                    created_at       timestamptz NOT NULL DEFAULT now(),
                    UNIQUE (scope, idempotency_key)
                )
                """
            )

            # Phase 18: the audit log. Append-only by convention — nothing
            # here updates or deletes a row — and never carrying a secret or
            # an unmasked number: `security/audit.py` scrubs before it writes.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
                    id           bigserial PRIMARY KEY,
                    created_at   timestamptz NOT NULL DEFAULT now(),
                    action       text        NOT NULL,
                    actor        text        NOT NULL,
                    role         text        NOT NULL,
                    via          text        NOT NULL,
                    outcome      text        NOT NULL,
                    target_kind  text,
                    target_id    text,
                    ip           text,
                    detail       jsonb       NOT NULL DEFAULT '{{}}'::jsonb
                )
                """
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {AUDIT_TABLE}_created_idx ON {AUDIT_TABLE} (created_at DESC)"
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {AUDIT_TABLE}_action_idx ON {AUDIT_TABLE} (action, created_at DESC)"
            )

            # Phase 19: the do-not-call list. One *active* row per number
            # (the partial unique index); a removal is a `revoked_at` stamp
            # with a name on it, never a DELETE, so the history of a number
            # is always answerable.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {DNC_TABLE} (
                    id                bigserial PRIMARY KEY,
                    phone_normalized  text        NOT NULL,
                    source            text        NOT NULL DEFAULT 'manual',
                    reason            text,
                    prospect_id       bigint,
                    campaign_id       bigint,
                    call_attempt_id   bigint,
                    created_by        text,
                    note              text,
                    created_at        timestamptz NOT NULL DEFAULT now(),
                    expires_at        timestamptz,
                    revoked_at        timestamptz,
                    revoked_by        text,
                    revoke_reason     text
                )
                """
            )
            await connection.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {DNC_TABLE}_active_number_idx "
                f"ON {DNC_TABLE} (phone_normalized) WHERE revoked_at IS NULL"
            )
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS {DNC_TABLE}_created_idx ON {DNC_TABLE} (created_at DESC)"
            )
            self._dnc_table_present = True

            # Phase 21: the fleet. One row per worker process, kept after it
            # stops (a stopped row is history; `prune_workers` tidies old
            # ones), and one row per pacing scope holding the moment of the
            # last placement, taken under an advisory lock.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {WORKERS_TABLE} (
                    worker_id     text        PRIMARY KEY,
                    hostname      text        NOT NULL,
                    pid           integer     NOT NULL,
                    status        text        NOT NULL DEFAULT 'running',
                    started_at    timestamptz NOT NULL DEFAULT now(),
                    heartbeat_at  timestamptz NOT NULL DEFAULT now(),
                    stopped_at    timestamptz,
                    campaign_ids  jsonb,
                    in_flight     integer     NOT NULL DEFAULT 0,
                    metrics       jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
                    version       text
                )
                """
            )
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
                    key               text        PRIMARY KEY,
                    last_placement_at timestamptz,
                    value             jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
                    updated_at        timestamptz NOT NULL DEFAULT now()
                )
                """
            )

            # Phase 27: sign-ups from the application's Register page. The
            # name and the email are unique case-insensitively (the
            # directory compares them that way); the hash is scrypt from
            # `src/security/passwords.py`. `DASHBOARD_USERS` is not copied
            # here: the environment stays the place for hand-configured
            # accounts, and the login consults both.
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {DASHBOARD_USERS_TABLE} (
                    id            bigserial PRIMARY KEY,
                    name          text        NOT NULL,
                    email         text        NOT NULL,
                    role          text        NOT NULL DEFAULT 'viewer',
                    password_hash text        NOT NULL,
                    created_at    timestamptz NOT NULL DEFAULT now(),
                    -- 'active', or 'pending' for an operator / admin request
                    -- an admin has not approved yet. A rejected request is
                    -- deleted (the audit log keeps it), so the name is free.
                    status        text        NOT NULL DEFAULT 'active',
                    decided_by    text,
                    decided_at    timestamptz
                )
                """
            )
            # The table was created without the approval columns on some
            # databases (Phase 27, first part); add them in place.
            await connection.execute(
                f"ALTER TABLE {DASHBOARD_USERS_TABLE} ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active'"
            )
            await connection.execute(f"ALTER TABLE {DASHBOARD_USERS_TABLE} ADD COLUMN IF NOT EXISTS decided_by text")
            await connection.execute(f"ALTER TABLE {DASHBOARD_USERS_TABLE} ADD COLUMN IF NOT EXISTS decided_at timestamptz")
            await connection.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {DASHBOARD_USERS_TABLE}_name_idx "
                f"ON {DASHBOARD_USERS_TABLE} (lower(name))"
            )
            await connection.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {DASHBOARD_USERS_TABLE}_email_idx "
                f"ON {DASHBOARD_USERS_TABLE} (lower(email))"
            )

    # --- Prospects ----------------------------------------------------------

    async def add_prospect(
        self,
        *,
        first_name: str,
        last_name: str,
        phone: str,
        phone_normalized: str | None,
        email: str | None = None,
        company: str | None = None,
        job_title: str | None = None,
        industry: str | None = None,
        location: str | None = None,
        website: str | None = None,
        custom_data: dict[str, Any] | None = None,
        status: ProspectStatus | None = None,
    ) -> Prospect:
        """Insert one prospect.

        Args:
            phone: The number as supplied, kept for audit.
            phone_normalized: E.164, or None when it could not be normalised —
                which stores the person but makes them undialable.
            status: Defaults to `NEW`, or `UNREACHABLE` when there is no
                normalised number, since that is what the row actually is.

        Raises:
            DuplicateProspectError: `phone_normalized` is already stored.
        """
        resolved_status = status or (
            ProspectStatus.NEW if phone_normalized else ProspectStatus.UNREACHABLE
        )
        try:
            row = await self._pool.fetchrow(
                f"""
                INSERT INTO {PROSPECTS_TABLE}
                    (first_name, last_name, phone, phone_normalized, email, company,
                     job_title, industry, location, website, custom_data, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12)
                RETURNING *
                """,
                first_name,
                last_name,
                phone,
                phone_normalized,
                email,
                company,
                job_title,
                industry,
                location,
                website,
                json.dumps(custom_data or {}),
                str(resolved_status),
            )
        except asyncpg.UniqueViolationError as exc:
            existing = await self.find_prospect_by_phone(phone_normalized or "")
            raise DuplicateProspectError(
                phone_normalized or phone, existing.id if existing else 0
            ) from exc
        return _prospect(row)

    async def get_prospect(self, prospect_id: int) -> Prospect | None:
        """One prospect by id, or None."""
        row = await self._pool.fetchrow(
            f"SELECT * FROM {PROSPECTS_TABLE} WHERE id = $1", prospect_id
        )
        return _prospect(row) if row else None

    async def find_prospect_by_phone(self, phone_normalized: str) -> Prospect | None:
        """One prospect by dialable number. The duplicate check, in one query."""
        if not phone_normalized:
            return None
        row = await self._pool.fetchrow(
            f"SELECT * FROM {PROSPECTS_TABLE} WHERE phone_normalized = $1", phone_normalized
        )
        return _prospect(row) if row else None

    async def list_prospects(
        self, *, limit: int = 50, offset: int = 0, status: ProspectStatus | None = None
    ) -> list[Prospect]:
        """Prospects, newest first, optionally filtered by status."""
        rows = await self._pool.fetch(
            f"""
            SELECT * FROM {PROSPECTS_TABLE}
            WHERE ($3::text IS NULL OR status = $3)
            ORDER BY id DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
            str(status) if status else None,
        )
        return [_prospect(row) for row in rows]

    async def count_prospects(self) -> int:
        """How many prospects exist."""
        return int(await self._pool.fetchval(f"SELECT count(*) FROM {PROSPECTS_TABLE}"))

    async def set_prospect_status(self, prospect_id: int, status: ProspectStatus) -> bool:
        """Change a prospect's status.

        Setting `DO_NOT_CALL` also closes their open memberships, so the queue
        stops offering them immediately rather than filtering them out on every
        future poll. Both happen in one transaction: a prospect marked DNC whose
        memberships stayed open would be a prospect the queue keeps picking up
        and the safety check keeps rejecting.

        Returns:
            True if a prospect was updated.
        """
        async with self._pool.acquire() as connection, connection.transaction():
            result = await connection.execute(
                f"UPDATE {PROSPECTS_TABLE} SET status = $2, updated_at = now() WHERE id = $1",
                prospect_id,
                str(status),
            )
            if status is ProspectStatus.DO_NOT_CALL:
                await connection.execute(
                    f"""
                    UPDATE {MEMBERSHIPS_TABLE}
                    SET status = $2, updated_at = now()
                    WHERE prospect_id = $1 AND status IN ('PENDING', 'IN_PROGRESS')
                    """,
                    prospect_id,
                    str(MembershipStatus.SKIPPED),
                )
                # Phase 7: a pending callback is a promise to phone them, and a
                # do-not-call revokes it. Guarded on the table existing, because
                # a failing statement would abort the whole transaction on a
                # database that predates the table — and the membership update
                # above must not be lost to that.
                if await connection.fetchval("SELECT to_regclass($1) IS NOT NULL", CALLBACKS_TABLE):
                    await connection.execute(
                        f"""
                        UPDATE {CALLBACKS_TABLE}
                        SET status = $2, updated_at = now()
                        WHERE prospect_id = $1 AND status = 'PENDING'
                        """,
                        prospect_id,
                        str(CallbackStatus.CANCELLED),
                    )
        return result.rsplit(" ", 1)[-1] != "0"

    # --- Campaigns ----------------------------------------------------------

    async def create_campaign(
        self,
        *,
        name: str,
        description: str | None = None,
        status: CampaignStatus = CampaignStatus.DRAFT,
        configuration: dict[str, Any] | None = None,
    ) -> Campaign:
        """Create a campaign.

        Raises:
            CampaignStoreError: A campaign with this name already exists. Names
                are unique because they are how a person refers to one on the
                command line, and two campaigns called "Q1 Outreach" make every
                later instruction ambiguous.
        """
        try:
            row = await self._pool.fetchrow(
                f"""
                INSERT INTO {CAMPAIGNS_TABLE} (name, description, status, configuration)
                VALUES ($1, $2, $3, $4::jsonb)
                RETURNING *
                """,
                name,
                description,
                str(status),
                json.dumps(configuration or {}),
            )
        except asyncpg.UniqueViolationError as exc:
            raise CampaignStoreError(f"A campaign named {name!r} already exists.") from exc
        return _campaign(row)

    async def get_campaign(self, campaign_id: int) -> Campaign | None:
        """One campaign by id, or None."""
        row = await self._pool.fetchrow(
            f"SELECT * FROM {CAMPAIGNS_TABLE} WHERE id = $1", campaign_id
        )
        return _campaign(row) if row else None

    async def find_campaign_by_name(self, name: str) -> Campaign | None:
        """One campaign by name, so the CLI can take a name instead of an id."""
        row = await self._pool.fetchrow(
            f"SELECT * FROM {CAMPAIGNS_TABLE} WHERE lower(name) = lower($1)", name
        )
        return _campaign(row) if row else None

    async def list_campaigns(
        self, *, status: CampaignStatus | None = None, limit: int = 50
    ) -> list[Campaign]:
        """Campaigns, newest first, optionally only those in one status."""
        rows = await self._pool.fetch(
            f"""
            SELECT * FROM {CAMPAIGNS_TABLE}
            WHERE ($1::text IS NULL OR status = $1)
            ORDER BY id DESC
            LIMIT $2
            """,
            str(status) if status else None,
            limit,
        )
        return [_campaign(row) for row in rows]

    async def update_campaign_configuration(
        self, campaign_id: int, key: str, value: Any
    ) -> Campaign | None:
        """Set one top-level key of a campaign's `configuration` JSON. Phase 19.

        `value` None removes the key. The rest of the object is untouched, so
        the sales settings a campaign carries (`offer`, `value_points`, …)
        survive a compliance change and vice versa.

        Returns:
            The campaign as it now stands, or None when there is no such campaign.
        """
        if value is None:
            row = await self._pool.fetchrow(
                f"UPDATE {CAMPAIGNS_TABLE} SET configuration = configuration - $2, updated_at = now() "
                f"WHERE id = $1 RETURNING *",
                campaign_id,
                str(key),
            )
        else:
            row = await self._pool.fetchrow(
                f"UPDATE {CAMPAIGNS_TABLE} SET configuration = configuration || $2::jsonb, updated_at = now() "
                f"WHERE id = $1 RETURNING *",
                campaign_id,
                _dumps({str(key): value}),
            )
        return _campaign(row) if row else None

    async def set_campaign_status(
        self, campaign_id: int, status: CampaignStatus
    ) -> Campaign | None:
        """Move a campaign to a new status, stamping the matching timestamp.

        The timestamps are set here rather than by the caller so that they
        cannot disagree with the status they describe.
        """
        # `started_at` is COALESCEd so resuming a paused campaign keeps the time
        # it originally started rather than claiming it started again; the other
        # two describe the most recent transition and are overwritten.
        stamp = {
            CampaignStatus.ACTIVE: ", started_at = COALESCE(started_at, now())",
            CampaignStatus.PAUSED: ", paused_at = now()",
            CampaignStatus.COMPLETED: ", completed_at = now()",
        }.get(status, "")
        row = await self._pool.fetchrow(
            f"UPDATE {CAMPAIGNS_TABLE} SET status = $2, updated_at = now(){stamp} "
            f"WHERE id = $1 RETURNING *",
            campaign_id,
            str(status),
        )
        return _campaign(row) if row else None

    async def campaign_counts(self, campaign_id: int) -> CampaignCounts:
        """Membership counts by status, for a progress line."""
        row = await self._pool.fetchrow(
            f"""
            SELECT count(*)                                        AS total,
                   count(*) FILTER (WHERE status = 'PENDING')      AS pending,
                   count(*) FILTER (WHERE status = 'IN_PROGRESS')  AS in_progress,
                   count(*) FILTER (WHERE status = 'COMPLETED')    AS completed,
                   count(*) FILTER (WHERE status = 'EXHAUSTED')    AS exhausted,
                   count(*) FILTER (WHERE status = 'SKIPPED')      AS skipped
            FROM {MEMBERSHIPS_TABLE} WHERE campaign_id = $1
            """,
            campaign_id,
        )
        return CampaignCounts(
            total=int(row["total"]),
            pending=int(row["pending"]),
            in_progress=int(row["in_progress"]),
            completed=int(row["completed"]),
            exhausted=int(row["exhausted"]),
            skipped=int(row["skipped"]),
        )

    async def campaign_progress(self, campaign_id: int) -> dict[str, int]:
        """Every counter a live progress view needs, in two statements. Phase 25.

        Memberships (the queue: who is waiting, due, scheduled, on a call,
        done) and attempts by status (the calls: reserved, ringing, connected,
        and each ending). Keys are `PROGRESS_KEYS`, always all present.
        `queued` is the queue as the scheduler sees it — pending memberships
        whose time has come — plus attempts reserved but not yet ringing.
        """
        members = await self._pool.fetchrow(
            f"""
            SELECT count(*)                                        AS contacts,
                   count(*) FILTER (WHERE status = 'PENDING')      AS pending,
                   count(*) FILTER (WHERE status = 'PENDING'
                                      AND (next_attempt_at IS NULL OR next_attempt_at <= now()))
                                                                   AS due,
                   count(*) FILTER (WHERE status = 'PENDING'
                                      AND next_attempt_at > now()) AS scheduled,
                   count(*) FILTER (WHERE status = 'IN_PROGRESS')  AS in_progress,
                   count(*) FILTER (WHERE status = 'COMPLETED')    AS members_completed,
                   count(*) FILTER (WHERE status = 'EXHAUSTED')    AS exhausted,
                   count(*) FILTER (WHERE status = 'SKIPPED')      AS skipped
            FROM {MEMBERSHIPS_TABLE} WHERE campaign_id = $1
            """,
            campaign_id,
        )
        attempts = await self._pool.fetchrow(
            f"""
            SELECT count(*)                                                 AS attempts,
                   count(*) FILTER (WHERE status IN ('PENDING', 'QUEUED'))  AS reserved,
                   count(*) FILTER (WHERE status = 'CALLING')               AS calling,
                   count(*) FILTER (WHERE status = 'CONNECTED')             AS connected,
                   count(*) FILTER (WHERE status = 'UNRESOLVED')            AS unresolved,
                   count(*) FILTER (WHERE status IN ({LIVE_STATUS_SQL}))    AS live,
                   count(*) FILTER (WHERE status IN ({REACHED_STATUS_SQL})) AS answered,
                   count(*) FILTER (WHERE status = 'COMPLETED')             AS completed,
                   count(*) FILTER (WHERE status = 'FAILED')                AS failed,
                   count(*) FILTER (WHERE status = 'NO_ANSWER')             AS no_answer,
                   count(*) FILTER (WHERE status = 'BUSY')                  AS busy,
                   count(*) FILTER (WHERE status = 'VOICEMAIL')             AS voicemail,
                   count(*) FILTER (WHERE status = 'NOT_INTERESTED')        AS not_interested,
                   count(*) FILTER (WHERE status = 'DO_NOT_CALL')           AS do_not_call,
                   count(*) FILTER (WHERE status = 'CALLBACK_REQUESTED')    AS callback_requested
            FROM {ATTEMPTS_TABLE} WHERE campaign_id = $1
            """,
            campaign_id,
        )
        out = {key: 0 for key in PROGRESS_KEYS}
        for key in ("contacts", "pending", "scheduled", "in_progress", "members_completed", "exhausted", "skipped"):
            out[key] = int(members[key])
        for key in ("attempts", "reserved", "calling", "connected", "unresolved", "live", "answered", "completed",
                    "failed", "no_answer", "busy", "voicemail", "not_interested", "do_not_call", "callback_requested"):
            out[key] = int(attempts[key])
        out["queued"] = int(members["due"]) + out["reserved"]
        return out

    # --- Memberships --------------------------------------------------------

    async def add_to_campaign(self, campaign_id: int, prospect_id: int) -> CampaignProspect | None:
        """Add one prospect to one campaign.

        Returns:
            The membership, or None if it was already there. Already-there is
            not an error: adding a list twice should be safe, and the second add
            must not reset an attempt count.
        """
        row = await self._pool.fetchrow(
            f"""
            INSERT INTO {MEMBERSHIPS_TABLE} (campaign_id, prospect_id)
            VALUES ($1, $2)
            ON CONFLICT (campaign_id, prospect_id) DO NOTHING
            RETURNING *
            """,
            campaign_id,
            prospect_id,
        )
        return _membership(row) if row else None

    async def list_campaign_prospects(
        self, campaign_id: int, *, limit: int = 50, offset: int = 0
    ) -> list[tuple[CampaignProspect, Prospect]]:
        """Memberships of a campaign, each with the person it refers to.

        The membership columns are aliased because the two tables share several
        names — `id`, `status`, `created_at` — and a `SELECT m.*, p.*` would
        silently keep only the last of each, handing back a membership wearing
        the prospect's id.
        """
        rows = await self._pool.fetch(
            f"""
            SELECT m.id              AS m_id,
                   m.campaign_id     AS m_campaign_id,
                   m.prospect_id     AS m_prospect_id,
                   m.status          AS m_status,
                   m.attempt_count   AS m_attempt_count,
                   m.last_attempt_at AS m_last_attempt_at,
                   m.next_attempt_at AS m_next_attempt_at,
                   m.created_at      AS m_created_at,
                   m.updated_at      AS m_updated_at,
                   p.*
            FROM {MEMBERSHIPS_TABLE} m
            JOIN {PROSPECTS_TABLE} p ON p.id = m.prospect_id
            WHERE m.campaign_id = $1
            ORDER BY m.id
            LIMIT $2 OFFSET $3
            """,
            campaign_id,
            limit,
            offset,
        )
        return [(_membership(row, prefix="m_"), _prospect(row)) for row in rows]

    async def get_membership(self, membership_id: int) -> CampaignProspect | None:
        """One membership by id, or None."""
        row = await self._pool.fetchrow(
            f"SELECT * FROM {MEMBERSHIPS_TABLE} WHERE id = $1", membership_id
        )
        return _membership(row) if row else None

    async def find_membership(self, campaign_id: int, prospect_id: int) -> CampaignProspect | None:
        """One membership by its campaign and prospect, or None."""
        row = await self._pool.fetchrow(
            f"SELECT * FROM {MEMBERSHIPS_TABLE} WHERE campaign_id = $1 AND prospect_id = $2",
            campaign_id,
            prospect_id,
        )
        return _membership(row) if row else None

    async def set_membership_status(
        self,
        membership_id: int,
        status: MembershipStatus,
        *,
        next_attempt_at: datetime | None = None,
    ) -> None:
        """Move a membership to a new status, optionally scheduling its retry."""
        await self._pool.execute(
            f"""
            UPDATE {MEMBERSHIPS_TABLE}
            SET status = $2, next_attempt_at = $3, updated_at = now()
            WHERE id = $1
            """,
            membership_id,
            str(status),
            next_attempt_at,
        )

    # --- The queue ----------------------------------------------------------

    @_timed("reserve")
    async def reserve_next_call(
        self,
        campaign_id: int,
        *,
        max_attempts: int,
        idempotency_key: Callable[[int, int], str] | None = None,
        max_concurrent: int = 0,
        worker_id: str | None = None,
    ) -> QueuedCall | None:
        """Pick the next prospect to call and reserve them, in one transaction.

        Phase 21: `worker_id` is written onto the attempt as its owner, and the
        whole statement runs under a deployment-wide advisory lock, so the
        concurrency count is exact across workers.

        Every eligibility rule is applied *inside* the statement that takes the
        row lock, because a check made before the lock can be true when it is
        made and false by the time the call is placed. In order, a membership is
        eligible when:

        1. its campaign is `ACTIVE`;
        2. the membership is `PENDING`;
        3. the prospect is not `DO_NOT_CALL`;
        4. the prospect has a normalised number to dial;
        5. its retry time has arrived, or it has none;
        6. it is under the attempt limit;
        7. the prospect has no live call attempt — including one from a
           *different* campaign, since the constraint is that a person can only
           be on one phone call at a time.

        `FOR UPDATE ... SKIP LOCKED` is what makes this safe to call from more
        than one place at once: a second caller skips the locked row rather than
        waiting for it or handing out the same person twice.

        **Phase 9 adds a second, independent guarantee.** The lock protects
        concurrent callers of *this* statement; it does nothing about a caller
        that crashed halfway, or one that reserved through some other path. So
        the attempt row also carries an `idempotency_key` derived from the
        campaign, the membership and the attempt number, under a unique index —
        and a collision on it is caught here and reported as "nothing eligible"
        rather than as an error, because another holder of that exact attempt
        already exists and handing out a second one is the thing being
        prevented.

        Args:
            campaign_id: Which campaign to draw work from.
            max_attempts: Dials allowed per membership before it is exhausted.
            idempotency_key: Builds the key from `(membership_id,
                attempt_number)`. `None` writes no key, which is what a caller
                that predates Phase 9 gets — the lock still protects it.
            max_concurrent: Live calls allowed at once, counted *inside* this
                transaction (Phase 11). 0 does not check. The dialer also
                checks before calling, which is cheaper and gives a reason;
                this is the backstop that makes the limit hold when two
                workers check at the same moment and both see room. Without it
                the limit is advisory across processes.

        Returns:
            A reserved `QueuedCall`, or None when nothing is eligible *or* when
            this exact attempt already exists. Holding one means the membership
            is already `IN_PROGRESS` and an attempt row already exists.
        """
        try:
            return await self._reserve(
                campaign_id, max_attempts, idempotency_key, max_concurrent, worker_id=worker_id
            )
        except _AlreadyReserved:
            return None

    @_timed("reserve_membership")
    async def reserve_membership(
        self,
        membership_id: int,
        *,
        max_attempts: int,
        idempotency_key: Callable[[int, int], str] | None = None,
        max_concurrent: int = 0,
        ignore_attempt_limit: bool = False,
        worker_id: str | None = None,
    ) -> QueuedCall | None:
        """Reserve one named membership, under exactly the queue's rules. Phase 13.

        The same statement as `reserve_next_call` with the membership pinned,
        so a scheduled callback — a promise to phone somebody at a time they
        chose — can be placed ahead of the never-called rows the queue orders
        first. Same transaction, same `FOR UPDATE SKIP LOCKED`, same
        idempotency key, same concurrency count: a membership somebody else is
        reserving this instant is skipped, and one that is not `PENDING`, not
        due, do-not-call, without a number, on a live call or in a campaign
        that is not `ACTIVE` is refused.

        Args:
            membership_id: Which membership.
            max_attempts: As for `reserve_next_call`.
            idempotency_key: As for `reserve_next_call`.
            max_concurrent: As for `reserve_next_call`.
            ignore_attempt_limit: Hand the membership out even at the attempt
                limit. For a callback the prospect asked for, and nothing else.

        Returns:
            The reservation, or None when the membership is not eligible now.
        """
        try:
            return await self._reserve(
                None,
                max_attempts,
                idempotency_key,
                max_concurrent,
                membership_id=membership_id,
                ignore_attempt_limit=ignore_attempt_limit,
                worker_id=worker_id,
            )
        except _AlreadyReserved:
            return None

    async def _reserve(
        self,
        campaign_id: int | None,
        max_attempts: int,
        idempotency_key: Callable[[int, int], str] | None,
        max_concurrent: int = 0,
        *,
        membership_id: int | None = None,
        ignore_attempt_limit: bool = False,
        worker_id: str | None = None,
    ) -> QueuedCall | None:
        """The body of `reserve_next_call` and `reserve_membership`, in one transaction.

        Exactly one of `campaign_id` (the queue's next row) and `membership_id`
        (one named row) is given. See the two docstrings.
        """
        if (campaign_id is None) == (membership_id is None):
            raise ValueError("_reserve needs a campaign_id or a membership_id, not both")
        async with self._pool.acquire() as connection, connection.transaction():
            # Phase 21: one reservation at a time, deployment-wide. The
            # advisory lock is released with the transaction, so the second
            # worker's count includes the first worker's new row — which is
            # what makes the concurrency limit exact across processes.
            # Phase 11 counted inside the transaction; under READ COMMITTED
            # two concurrent transactions could still both count the old
            # total. Now they cannot.
            await connection.execute("SELECT pg_advisory_xact_lock($1)", RESERVE_LOCK_KEY)
            if max_concurrent > 0:
                live = await connection.fetchval(
                    f"SELECT count(*) FROM {ATTEMPTS_TABLE} "
                    f"WHERE status IN ({LIVE_STATUS_SQL})"
                )
                if int(live) >= max_concurrent:
                    return None
            # Phase 25: a campaign's own ceiling, `configuration.max_concurrent_calls`,
            # counted under the same lock so it holds across workers. 0 or
            # unset leaves only the deployment's limit above.
            if campaign_id is not None:
                cap = campaign_concurrency(
                    await connection.fetchval(
                        f"SELECT configuration ->> 'max_concurrent_calls' FROM {CAMPAIGNS_TABLE} WHERE id = $1",
                        campaign_id,
                    )
                )
                if cap > 0:
                    live_here = await connection.fetchval(
                        f"SELECT count(*) FROM {ATTEMPTS_TABLE} "
                        f"WHERE campaign_id = $1 AND status IN ({LIVE_STATUS_SQL})",
                        campaign_id,
                    )
                    if int(live_here) >= cap:
                        return None

            # Phase 19: a number on the do-not-call list is never handed out,
            # whatever the prospect row says. Spliced in only when the table
            # exists; see `_dnc_clause`.
            dnc = await self._dnc_clause(connection)
            row = await connection.fetchrow(
                f"""
                SELECT m.id AS membership_id, m.campaign_id, m.prospect_id, m.attempt_count
                FROM {MEMBERSHIPS_TABLE} m
                JOIN {CAMPAIGNS_TABLE} c ON c.id = m.campaign_id
                JOIN {PROSPECTS_TABLE}  p ON p.id = m.prospect_id
                WHERE ($1::bigint IS NULL OR m.campaign_id = $1)
                  AND ($3::bigint IS NULL OR m.id = $3)
                  AND c.status = 'ACTIVE'
                  AND m.status = 'PENDING'
                  AND p.status <> 'DO_NOT_CALL'
                  AND p.phone_normalized IS NOT NULL
                  {dnc}
                  AND (m.next_attempt_at IS NULL OR m.next_attempt_at <= now())
                  AND ($4::boolean OR m.attempt_count < $2)
                  AND NOT EXISTS (
                      SELECT 1 FROM {ATTEMPTS_TABLE} a
                      WHERE a.prospect_id = m.prospect_id
                        AND a.status IN ({LIVE_STATUS_SQL})
                  )
                ORDER BY m.next_attempt_at NULLS FIRST, m.id
                FOR UPDATE OF m SKIP LOCKED
                LIMIT 1
                """,
                campaign_id,
                max_attempts,
                membership_id,
                bool(ignore_attempt_limit),
            )
            if row is None:
                return None

            membership_id = int(row["membership_id"])
            campaign_id = int(row["campaign_id"])
            attempt_number = int(row["attempt_count"]) + 1
            key = idempotency_key(membership_id, attempt_number) if idempotency_key else None

            membership_row = await connection.fetchrow(
                f"""
                UPDATE {MEMBERSHIPS_TABLE}
                SET status = 'IN_PROGRESS',
                    attempt_count = attempt_count + 1,
                    last_attempt_at = now(),
                    updated_at = now()
                WHERE id = $1
                RETURNING *
                """,
                membership_id,
            )
            try:
                attempt_row = await connection.fetchrow(
                    f"""
                    INSERT INTO {ATTEMPTS_TABLE}
                        (prospect_id, campaign_id, campaign_prospect_id, attempt_number,
                         status, idempotency_key, worker_id)
                    VALUES ($1, $2, $3, $4, 'PENDING', $5, $6)
                    RETURNING *
                    """,
                    int(row["prospect_id"]),
                    campaign_id,
                    membership_id,
                    attempt_number,
                    key,
                    worker_id,
                )
            except asyncpg.UniqueViolationError:
                # This exact attempt already exists. The transaction rolls back,
                # so the membership's attempt count is not advanced either —
                # which is right: nothing new was reserved. Reported as an empty
                # queue rather than raised, because from the caller's side there
                # is simply nothing to dial.
                logger.warning(
                    f"QUEUE | attempt {key} already exists; not reserving a second one"
                )
                raise _AlreadyReserved from None
            prospect_row = await connection.fetchrow(
                f"SELECT * FROM {PROSPECTS_TABLE} WHERE id = $1", int(row["prospect_id"])
            )
            campaign_row = await connection.fetchrow(
                f"SELECT * FROM {CAMPAIGNS_TABLE} WHERE id = $1", campaign_id
            )

        return QueuedCall(
            attempt=_attempt(attempt_row),
            prospect=_prospect(prospect_row),
            campaign=_campaign(campaign_row),
            membership=_membership(membership_row),
        )

    async def unreserve_attempt(self, attempt_id: int, *, next_attempt_at: datetime) -> bool:
        """Give a reservation back as if it had not been taken. Phase 13.

        For an attempt that was reserved and then found not to be placeable
        *yet* — the prospect's own calling window is closed — as opposed to one
        the carrier refused. Nothing was dialled, so there is no history to
        keep: the attempt row is removed, the membership goes back to
        `PENDING` with its count restored and `next_attempt_at` set to when the
        window opens, and the idempotency key is free for the attempt that
        will actually happen.

        Refuses — returns False, writes nothing — unless the attempt is still
        `PENDING` with no carrier call id and no placement started, which is
        the only shape that can honestly be said never to have happened.

        Returns:
            Whether the reservation was undone.
        """
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"""
                DELETE FROM {ATTEMPTS_TABLE}
                WHERE id = $1
                  AND status = 'PENDING'
                  AND telephony_call_id IS NULL
                  AND placement_started_at IS NULL
                RETURNING campaign_prospect_id
                """,
                attempt_id,
            )
            if row is None:
                return False
            if row["campaign_prospect_id"] is not None:
                await connection.execute(
                    f"""
                    UPDATE {MEMBERSHIPS_TABLE}
                    SET status = 'PENDING',
                        attempt_count = GREATEST(attempt_count - 1, 0),
                        next_attempt_at = $2,
                        updated_at = now()
                    WHERE id = $1 AND status = 'IN_PROGRESS'
                    """,
                    int(row["campaign_prospect_id"]),
                    next_attempt_at,
                )
        return True

    async def has_live_attempt(
        self, prospect_id: int, *, exclude_attempt_id: int | None = None
    ) -> bool:
        """Whether this prospect is on a call right now, in any campaign.

        "Right now" includes `UNRESOLVED` — an attempt whose placement never
        reported an outcome (Phase 9). Treating a maybe-call as a call is the
        conservative direction: the cost is one prospect left undialled until
        recovery resolves them, and the cost of the other direction is somebody
        being phoned twice.

        Args:
            prospect_id: Who to check.
            exclude_attempt_id: An attempt to ignore. Needed because reserving
                work *creates* a live attempt, so a caller re-checking safety
                between reserving and dialling would otherwise find the very
                attempt it is about to place and refuse to place it.
        """
        return bool(
            await self._pool.fetchval(
                f"""
                SELECT EXISTS (
                    SELECT 1 FROM {ATTEMPTS_TABLE}
                    WHERE prospect_id = $1
                      AND status IN ({LIVE_STATUS_SQL})
                      AND ($2::bigint IS NULL OR id <> $2)
                )
                """,
                prospect_id,
                exclude_attempt_id,
            )
        )

    async def count_live_attempts(self, *, campaign_id: int | None = None) -> int:
        """How many calls are live right now. The concurrency limit's input. Phase 9."""
        return int(
            await self._pool.fetchval(
                f"""
                SELECT count(*) FROM {ATTEMPTS_TABLE}
                WHERE status IN ({LIVE_STATUS_SQL})
                  AND ($1::bigint IS NULL OR campaign_id = $1)
                """,
                campaign_id,
            )
        )

    async def queue_outlook(self, campaign_id: int, *, max_attempts: int) -> QueueOutlook:
        """What a campaign's queue holds, and when it next has work. Phase 13.

        The scheduler's question after `reserve_next_call` returns nothing:
        is that because the campaign is finished, because the next retry is
        an hour away, or because the one due membership is on a call in
        another campaign? See `QueueOutlook`.

        The callbacks table is optional (a schema that predates Phase 7 has
        none); without it the callback fields are zero rather than an error.
        """
        # Phase 19: a listed number is undialable, not due. Same guard as the queue.
        dnc = await self._dnc_clause()
        listed = _DNC_EXISTS_SQL if dnc else "FALSE"
        row = await self._pool.fetchrow(
            f"""
            SELECT count(*)                                            AS total,
                   count(*) FILTER (WHERE m.status = 'PENDING')        AS pending,
                   count(*) FILTER (WHERE m.status = 'IN_PROGRESS')    AS in_progress,
                   count(*) FILTER (
                       WHERE m.status = 'PENDING'
                         AND p.status <> 'DO_NOT_CALL'
                         AND p.phone_normalized IS NOT NULL
                         {dnc}
                         AND m.attempt_count < $2
                         AND (m.next_attempt_at IS NULL OR m.next_attempt_at <= now())
                   )                                                   AS due_now,
                   min(m.next_attempt_at) FILTER (
                       WHERE m.status = 'PENDING'
                         AND p.status <> 'DO_NOT_CALL'
                         AND p.phone_normalized IS NOT NULL
                         {dnc}
                         AND m.attempt_count < $2
                         AND m.next_attempt_at > now()
                   )                                                   AS next_due_at,
                   count(*) FILTER (
                       WHERE m.status = 'PENDING'
                         AND (p.status = 'DO_NOT_CALL'
                              OR p.phone_normalized IS NULL
                              OR {listed}
                              OR m.attempt_count >= $2)
                   )                                                   AS undialable
            FROM {MEMBERSHIPS_TABLE} m
            JOIN {PROSPECTS_TABLE} p ON p.id = m.prospect_id
            WHERE m.campaign_id = $1
            """,
            campaign_id,
            max_attempts,
        )
        outlook = QueueOutlook(
            total=int(row["total"]),
            pending=int(row["pending"]),
            in_progress=int(row["in_progress"]),
            due_now=int(row["due_now"]),
            next_due_at=row["next_due_at"],
            undialable=int(row["undialable"]),
        )

        if not await self._pool.fetchval("SELECT to_regclass($1) IS NOT NULL", CALLBACKS_TABLE):
            return outlook
        callbacks = await self._pool.fetchrow(
            f"""
            SELECT count(*) AS pending, min(scheduled_for) AS next_at
            FROM {CALLBACKS_TABLE}
            WHERE campaign_id = $1 AND status = 'PENDING'
            """,
            campaign_id,
        )
        pending_callbacks = int(callbacks["pending"])
        # A membership at the attempt limit that has a pending callback is
        # dialable through the callback path, so it is not undialable.
        with_callback = 0
        if pending_callbacks and outlook.undialable:
            with_callback = int(
                await self._pool.fetchval(
                    f"""
                    SELECT count(*) FROM {MEMBERSHIPS_TABLE} m
                    JOIN {PROSPECTS_TABLE} p ON p.id = m.prospect_id
                    WHERE m.campaign_id = $1
                      AND m.status = 'PENDING'
                      AND p.status <> 'DO_NOT_CALL'
                      AND p.phone_normalized IS NOT NULL
                      AND m.attempt_count >= $2
                      AND EXISTS (
                          SELECT 1 FROM {CALLBACKS_TABLE} cb
                          WHERE cb.campaign_prospect_id = m.id AND cb.status = 'PENDING'
                      )
                    """,
                    campaign_id,
                    max_attempts,
                )
            )
        return QueueOutlook(
            total=outlook.total,
            pending=outlook.pending,
            in_progress=outlook.in_progress,
            due_now=outlook.due_now,
            next_due_at=outlook.next_due_at,
            undialable=outlook.undialable - with_callback,
            pending_callbacks=pending_callbacks,
            next_callback_at=callbacks["next_at"],
        )

    async def sweep_memberships(self, campaign_id: int, *, max_attempts: int) -> tuple[int, int]:
        """Close the pending memberships the queue will never hand out. Phase 13.

        Two kinds, closed with the status that says why: a prospect with no
        usable number or marked do-not-call becomes `SKIPPED`, and one that has
        used every attempt — a callback that was cancelled after it reopened
        the membership, say — becomes `EXHAUSTED`. A membership at the limit
        that still has a pending callback is left alone: the callback is the
        reason it may be dialled once more.

        Without this a campaign whose list held one unusable number would
        never be finished, because one row would stay `PENDING` for ever.

        Returns:
            `(skipped, exhausted)` — how many memberships each rule closed.
        """
        async with self._pool.acquire() as connection, connection.transaction():
            skipped = await connection.execute(
                f"""
                UPDATE {MEMBERSHIPS_TABLE} m
                SET status = $2, updated_at = now()
                FROM {PROSPECTS_TABLE} p
                WHERE p.id = m.prospect_id
                  AND m.campaign_id = $1
                  AND m.status = 'PENDING'
                  AND (p.status = 'DO_NOT_CALL' OR p.phone_normalized IS NULL)
                """,
                campaign_id,
                str(MembershipStatus.SKIPPED),
            )
            has_callbacks = await connection.fetchval(
                "SELECT to_regclass($1) IS NOT NULL", CALLBACKS_TABLE
            )
            keep_for_callback = (
                f"""
                  AND NOT EXISTS (
                      SELECT 1 FROM {CALLBACKS_TABLE} cb
                      WHERE cb.campaign_prospect_id = m.id AND cb.status = 'PENDING'
                  )
                """
                if has_callbacks
                else ""
            )
            exhausted = await connection.execute(
                f"""
                UPDATE {MEMBERSHIPS_TABLE} m
                SET status = $3, updated_at = now()
                WHERE m.campaign_id = $1
                  AND m.status = 'PENDING'
                  AND m.attempt_count >= $2
                  {keep_for_callback}
                """,
                campaign_id,
                max_attempts,
                str(MembershipStatus.EXHAUSTED),
            )
        return int(skipped.rsplit(" ", 1)[-1]), int(exhausted.rsplit(" ", 1)[-1])

    async def find_attempt_by_key(self, idempotency_key: str) -> CallAttempt | None:
        """The attempt with this idempotency key, or None. Phase 9.

        What a caller asks after losing a race, to find the row that won it.
        """
        row = await self._pool.fetchrow(
            f"SELECT * FROM {ATTEMPTS_TABLE} WHERE idempotency_key = $1", idempotency_key
        )
        return _attempt(row) if row else None

    async def list_live_attempts(self, *, older_than_secs: float = 0.0, limit: int = 100) -> list[CallAttempt]:
        """Attempts still marked live, oldest first. Recovery's input. Phase 9.

        Args:
            older_than_secs: Only attempts untouched for at least this long, so
                a recovery pass running while calls are in flight does not
                reconcile a call that is perfectly healthy and two seconds old.
            limit: Cap, so one pass is bounded.
        """
        rows = await self._pool.fetch(
            f"""
            SELECT * FROM {ATTEMPTS_TABLE}
            WHERE status IN ({LIVE_STATUS_SQL})
              AND updated_at <= now() - make_interval(secs => $1)
            ORDER BY updated_at
            LIMIT $2
            """,
            float(max(0.0, older_than_secs)),
            limit,
        )
        return [_attempt(row) for row in rows]

    @_timed("mark_placement_started")
    async def mark_placement_started(self, attempt_id: int) -> CallAttempt | None:
        """Stamp an attempt as about to be dialled, before the carrier is asked. Phase 9.

        The row is the record that a placement *may* have happened. Writing it
        first is what makes a process that dies mid-request recoverable: the
        attempt has a `placement_started_at` and no `telephony_call_id`, which
        is exactly the shape recovery looks for.
        """
        row = await self._pool.fetchrow(
            f"""
            UPDATE {ATTEMPTS_TABLE}
            SET status = CASE WHEN status = 'PENDING' THEN 'CALLING' ELSE status END,
                placement_started_at = COALESCE(placement_started_at, now()),
                updated_at = now()
            WHERE id = $1
            RETURNING *
            """,
            attempt_id,
        )
        return _attempt(row) if row else None

    async def mark_attempt_unresolved(self, attempt_id: int, reason: str) -> CallAttempt | None:
        """Record that a placement never reported an outcome. Phase 9.

        The attempt stays *live*, so the prospect is not dialled again, and
        `reliability/recovery.py` resolves it by asking the carrier what exists.
        Never applied to an attempt that already has a carrier call id: if we
        know the call id, the placement did happen and there is nothing
        ambiguous about it.
        """
        row = await self._pool.fetchrow(
            f"""
            UPDATE {ATTEMPTS_TABLE}
            SET status = 'UNRESOLVED',
                failure_reason = COALESCE(failure_reason, $2),
                updated_at = now()
            WHERE id = $1 AND telephony_call_id IS NULL AND status IN ({LIVE_STATUS_SQL})
            RETURNING *
            """,
            attempt_id,
            reason,
        )
        return _attempt(row) if row else None

    # --- Call attempts ------------------------------------------------------

    async def create_attempt(
        self,
        *,
        prospect_id: int,
        campaign_id: int | None = None,
        campaign_prospect_id: int | None = None,
        attempt_number: int = 1,
        status: CallAttemptStatus = CallAttemptStatus.PENDING,
    ) -> CallAttempt:
        """Record a call attempt outside the queue, for a one-off manual dial."""
        row = await self._pool.fetchrow(
            f"""
            INSERT INTO {ATTEMPTS_TABLE}
                (prospect_id, campaign_id, campaign_prospect_id, attempt_number, status)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            prospect_id,
            campaign_id,
            campaign_prospect_id,
            attempt_number,
            str(status),
        )
        return _attempt(row)

    async def get_attempt(self, attempt_id: int) -> CallAttempt | None:
        """One attempt by id, or None."""
        row = await self._pool.fetchrow(f"SELECT * FROM {ATTEMPTS_TABLE} WHERE id = $1", attempt_id)
        return _attempt(row) if row else None

    @_timed("set_attempt_trace")
    async def set_attempt_trace(self, attempt_id: int, trace_id: str) -> bool:
        """Write the correlation id onto an attempt, before the carrier is asked. Phase 22.

        Never raises for a schema that predates the column: the id still
        travels on the handshake and the logs, and the next `campaign.py
        init` adds the column. Returns whether the row was written.
        """
        try:
            status = await self._pool.execute(
                f"UPDATE {ATTEMPTS_TABLE} SET trace_id = $2 WHERE id = $1", attempt_id, trace_id
            )
        except asyncpg.UndefinedColumnError:
            if not self._warned_trace_column:
                self._warned_trace_column = True
                logger.warning(
                    "TRACE | this database has no trace_id column; run `uv run campaign.py init` "
                    "so a call's correlation id is kept on its row"
                )
            return False
        return str(status).endswith("1")

    async def ping(self) -> bool:
        """The cheapest round trip: `SELECT 1`. For a readiness probe. Phase 22."""
        return int(await self._pool.fetchval("SELECT 1")) == 1

    async def throughput(self, *, window_secs: float = 3600.0) -> Throughput:
        """What the deployment placed and finished in the last `window_secs`. Phase 22.

        One aggregate over the attempt rows. `placed` counts placements that
        *began* in the window (`placement_started_at`, the Phase 9 stamp,
        falling back to `started_at` for rows that predate it); the rest
        count endings in the window by their final status. Cost and tokens
        come from Phase 11's columns where they were recorded.
        """
        window = max(60.0, float(window_secs))
        try:
            row = await self._pool.fetchrow(
                f"""
                SELECT
                    count(*) FILTER (WHERE COALESCE(placement_started_at, started_at) >= now() - $1::float8 * interval '1 second') AS placed,
                    count(*) FILTER (WHERE ended_at >= now() - $1::float8 * interval '1 second') AS finished,
                    count(*) FILTER (WHERE ended_at >= now() - $1::float8 * interval '1 second'
                                       AND status IN ('COMPLETED', 'CALLBACK_REQUESTED', 'NOT_INTERESTED', 'DO_NOT_CALL')) AS answered,
                    count(*) FILTER (WHERE ended_at >= now() - $1::float8 * interval '1 second' AND status = 'FAILED') AS failed,
                    sum(cost_usd) FILTER (WHERE ended_at >= now() - $1::float8 * interval '1 second') AS cost_usd,
                    coalesce(sum((usage -> 'llm' ->> 'prompt_tokens')::bigint) FILTER (WHERE ended_at >= now() - $1::float8 * interval '1 second'), 0) AS prompt_tokens,
                    coalesce(sum((usage -> 'llm' ->> 'completion_tokens')::bigint) FILTER (WHERE ended_at >= now() - $1::float8 * interval '1 second'), 0) AS completion_tokens
                FROM {ATTEMPTS_TABLE}
                """,
                window,
            )
            rows = await self._pool.fetch(
                f"""
                SELECT campaign_id,
                       count(*) FILTER (WHERE COALESCE(placement_started_at, started_at) >= now() - $1::float8 * interval '1 second') AS placed,
                       count(*) FILTER (WHERE ended_at >= now() - $1::float8 * interval '1 second') AS finished
                FROM {ATTEMPTS_TABLE}
                WHERE campaign_id IS NOT NULL
                  AND (COALESCE(placement_started_at, started_at) >= now() - $1::float8 * interval '1 second'
                       OR ended_at >= now() - $1::float8 * interval '1 second')
                GROUP BY campaign_id
                ORDER BY campaign_id
                """,
                window,
            )
        except asyncpg.UndefinedColumnError:
            # A schema without Phase 9's or Phase 11's columns: the simple counts still stand.
            row = await self._pool.fetchrow(
                f"""
                SELECT count(*) FILTER (WHERE started_at >= now() - $1::float8 * interval '1 second') AS placed,
                       count(*) FILTER (WHERE ended_at >= now() - $1::float8 * interval '1 second') AS finished,
                       count(*) FILTER (WHERE ended_at >= now() - $1::float8 * interval '1 second'
                                          AND status IN ('COMPLETED', 'CALLBACK_REQUESTED', 'NOT_INTERESTED', 'DO_NOT_CALL')) AS answered,
                       count(*) FILTER (WHERE ended_at >= now() - $1::float8 * interval '1 second' AND status = 'FAILED') AS failed
                FROM {ATTEMPTS_TABLE}
                """,
                window,
            )
            rows = []
        cost = row["cost_usd"] if "cost_usd" in row.keys() else None
        return Throughput(
            window_secs=window,
            placed=int(row["placed"]),
            finished=int(row["finished"]),
            answered=int(row["answered"]),
            failed=int(row["failed"]),
            cost_usd=float(cost) if cost is not None else None,
            prompt_tokens=int(row["prompt_tokens"]) if "prompt_tokens" in row.keys() else 0,
            completion_tokens=int(row["completion_tokens"]) if "completion_tokens" in row.keys() else 0,
            per_campaign=tuple(
                {"campaign_id": int(r["campaign_id"]), "placed": int(r["placed"]), "finished": int(r["finished"])}
                for r in rows
            ),
        )

    async def find_attempt_by_call_id(self, telephony_call_id: str) -> CallAttempt | None:
        """One attempt by the carrier's call id.

        The lookup a status callback or a reconciliation job needs: the carrier
        knows its own id and nothing else about us.
        """
        row = await self._pool.fetchrow(
            f"SELECT * FROM {ATTEMPTS_TABLE} WHERE telephony_call_id = $1", telephony_call_id
        )
        return _attempt(row) if row else None

    async def list_attempts(
        self,
        *,
        prospect_id: int | None = None,
        campaign_id: int | None = None,
        limit: int = 50,
    ) -> list[CallAttempt]:
        """Call history, newest first, optionally narrowed to one prospect or campaign."""
        rows = await self._pool.fetch(
            f"""
            SELECT * FROM {ATTEMPTS_TABLE}
            WHERE ($1::bigint IS NULL OR prospect_id = $1)
              AND ($2::bigint IS NULL OR campaign_id = $2)
            ORDER BY id DESC
            LIMIT $3
            """,
            prospect_id,
            campaign_id,
            limit,
        )
        return [_attempt(row) for row in rows]

    @_timed("mark_attempt_placed")
    async def mark_attempt_placed(
        self, attempt_id: int, *, telephony_call_id: str, provider: str
    ) -> CallAttempt | None:
        """Record that the carrier accepted the call and gave us an id.

        **Refuses to overwrite a different call id** (Phase 9). An attempt that
        already carries one has already been placed, so a second placement for
        it is a duplicate call — and quietly replacing the id would hide the
        very thing that needs to be seen. Writing the *same* id again is
        allowed and changes nothing, which is what makes a retried write safe.

        Raises:
            CampaignStoreError: The attempt already has a different carrier call
                id. Nothing is written.
        """
        existing = await self._pool.fetchval(
            f"SELECT telephony_call_id FROM {ATTEMPTS_TABLE} WHERE id = $1", attempt_id
        )
        if existing and existing != telephony_call_id:
            raise CampaignStoreError(
                f"Attempt {attempt_id} is already placed as call {existing}; refusing to "
                f"record a second call {telephony_call_id} against it. This means two calls "
                f"were placed for one attempt — check the carrier's log for both."
            )

        try:
            row = await self._pool.fetchrow(
                f"""
                UPDATE {ATTEMPTS_TABLE}
                SET status = CASE WHEN status IN ({LIVE_STATUS_SQL}) THEN 'QUEUED' ELSE status END,
                    telephony_call_id = $2,
                    telephony_provider = $3,
                    started_at = COALESCE(started_at, now()),
                    updated_at = now()
                WHERE id = $1
                RETURNING *
                """,
                attempt_id,
                telephony_call_id,
                provider,
            )
        except asyncpg.UniqueViolationError as exc:
            # `telephony_call_id` is unique across the table: this call id is
            # already recorded against a *different* attempt. One carrier call
            # cannot belong to two attempts, and the existing row is the one
            # that has it.
            raise CampaignStoreError(
                f"Carrier call {telephony_call_id} is already recorded against another attempt. "
                f"Attempt {attempt_id} was not updated."
            ) from exc
        return _attempt(row) if row else None

    @_timed("apply_call_event")
    async def apply_call_event(
        self,
        *,
        status: CallAttemptStatus,
        attempt_id: int | None = None,
        telephony_call_id: str | None = None,
        duration_seconds: int | None = None,
        failure_reason: str | None = None,
    ) -> tuple[CallAttempt | None, bool]:
        """Apply one carrier status event to its attempt, idempotently. Phase 9.

        The single entry point for "the carrier says this call is now X",
        whether X arrived from a poll, a webhook, a duplicate of either, or a
        recovery pass. Two properties make it safe to call with the same event
        any number of times, in any order:

        * the row is selected `FOR UPDATE`, so two deliveries of the same event
          cannot both read the old status and both write;
        * `models.may_advance` decides whether the move is forward, so a final
          status is never overwritten and a stale `ringing` arriving after
          `answered` is dropped.

        Args:
            status: The new status.
            attempt_id: Which attempt, when the caller knows.
            telephony_call_id: The carrier's id, for a caller that knows only
                that — a webhook. Exactly one of the two is required.
            duration_seconds: Recorded when given and not already set.
            failure_reason: Recorded when given and not already set.

        Returns:
            `(attempt, applied)`. `applied` is False when the event was a
            duplicate or went backwards; `attempt` is the row as it stands
            either way, or None when no such attempt exists.
        """
        if attempt_id is None and not telephony_call_id:
            raise ValueError("apply_call_event needs an attempt_id or a telephony_call_id")

        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"""
                SELECT * FROM {ATTEMPTS_TABLE}
                WHERE ($1::bigint IS NOT NULL AND id = $1)
                   OR ($1::bigint IS NULL AND telephony_call_id = $2)
                FOR UPDATE
                """,
                attempt_id,
                telephony_call_id,
            )
            if row is None:
                return None, False

            current = _attempt(row)
            if not may_advance(current.status, status):
                logger.debug(
                    f"CALL | attempt {current.id} | ignoring {status.value}: it does not follow "
                    f"{current.status.value}"
                )
                return current, False

            updated = await connection.fetchrow(
                f"""
                UPDATE {ATTEMPTS_TABLE}
                SET status = $2,
                    failure_reason = COALESCE(failure_reason, $3),
                    duration_seconds = COALESCE($4, duration_seconds),
                    telephony_call_id = COALESCE(telephony_call_id, $5),
                    connected_at = CASE
                        WHEN $2 = 'CONNECTED' THEN COALESCE(connected_at, now())
                        ELSE connected_at END,
                    ended_at = CASE
                        WHEN $2 IN ({FINAL_STATUS_SQL})
                        THEN COALESCE(ended_at, now())
                        ELSE ended_at END,
                    updated_at = now()
                WHERE id = $1
                RETURNING *
                """,
                current.id,
                str(status),
                failure_reason,
                duration_seconds,
                telephony_call_id,
            )
            return _attempt(updated), True

    async def update_attempt_status(
        self,
        attempt_id: int,
        status: CallAttemptStatus,
        *,
        failure_reason: str | None = None,
        duration_seconds: int | None = None,
    ) -> CallAttempt | None:
        """Move an attempt to a new status, stamping the times that go with it.

        `connected_at` is only ever set once, and `ended_at` only on a final
        status, so the timestamps cannot contradict the status they belong to.

        Unconditional: this is the *decision* being recorded, by a caller that
        has already made it — the service closing an attempt, an operator
        overriding one. For applying an event the carrier reported, use
        `apply_call_event`, which is idempotent and refuses to move a status
        backwards.
        """
        row = await self._pool.fetchrow(
            f"""
            UPDATE {ATTEMPTS_TABLE}
            SET status = $2,
                failure_reason = COALESCE($3, failure_reason),
                duration_seconds = COALESCE($4, duration_seconds),
                connected_at = CASE
                    WHEN $2 = 'CONNECTED' THEN COALESCE(connected_at, now())
                    ELSE connected_at END,
                ended_at = CASE
                    WHEN $2 IN ({FINAL_STATUS_SQL})
                    THEN COALESCE(ended_at, now())
                    ELSE ended_at END,
                updated_at = now()
            WHERE id = $1
            RETURNING *
            """,
            attempt_id,
            str(status),
            failure_reason,
            duration_seconds,
        )
        return _attempt(row) if row else None

    @_timed("save_conversation_data")
    async def save_conversation_data(
        self, attempt_id: int, data: dict[str, Any]
    ) -> bool:
        """Store what the conversation established, on the attempt it belongs to.

        Written separately from `update_attempt_status` on purpose. The status
        is reconciled from the *carrier* by `dialer.refresh`, which may run
        after the bot process has exited; this is written by the bot at the end
        of the call and must not be undone by that reconciliation. Two writers,
        two columns, no contention.

        Returns:
            Whether the attempt existed. False for an id that resolved to
            nothing, which is a lost record but never an exception — the call is
            already over by the time this runs.

        Raises:
            CampaignStoreError: The column does not exist, i.e. the schema
                predates Phase 6 and `campaign.py init` has not been re-run.
        """
        try:
            row = await self._pool.fetchrow(
                f"""
                UPDATE {ATTEMPTS_TABLE}
                SET conversation_data = $2::jsonb, updated_at = now()
                WHERE id = $1
                RETURNING id
                """,
                attempt_id,
                json.dumps(data, ensure_ascii=False, default=str),
            )
        except asyncpg.UndefinedColumnError as exc:
            raise CampaignStoreError(
                "The call_attempts table has no conversation_data column.\n"
                "  This database was created before Phase 6. Run:  uv run campaign.py init\n"
                "  (it is idempotent and adds the column without touching your data)."
            ) from exc
        return row is not None

    # --- Callbacks (Phase 7) ------------------------------------------------

    @_timed("schedule_callback")
    async def schedule_callback(
        self,
        *,
        prospect_id: int,
        scheduled_for: datetime,
        campaign_id: int | None = None,
        call_attempt_id: int | None = None,
        campaign_prospect_id: int | None = None,
        note: str | None = None,
    ) -> ScheduledCallback:
        """Create the prospect's pending callback, or move the one they already have.

        One pending callback per prospect is the rule the unique index enforces;
        this is the write that respects it. A second request replaces the time
        rather than failing, because "actually, make it Wednesday" is how people
        talk and a constraint violation is not an answer to it.

        Raises:
            CampaignStoreError: The table is missing (schema predates Phase 7).
        """
        if scheduled_for.tzinfo is None:
            raise ValueError("scheduled_for must be timezone-aware")

        async def write() -> asyncpg.Record:
            async with self._pool.acquire() as connection, connection.transaction():
                row = await connection.fetchrow(
                    f"""
                    UPDATE {CALLBACKS_TABLE}
                    SET scheduled_for = $2,
                        campaign_id = COALESCE($3, campaign_id),
                        call_attempt_id = COALESCE($4, call_attempt_id),
                        campaign_prospect_id = COALESCE($5, campaign_prospect_id),
                        note = COALESCE($6, note),
                        updated_at = now()
                    WHERE prospect_id = $1 AND status = 'PENDING'
                    RETURNING *
                    """,
                    prospect_id,
                    scheduled_for,
                    campaign_id,
                    call_attempt_id,
                    campaign_prospect_id,
                    note,
                )
                if row is None:
                    row = await connection.fetchrow(
                        f"""
                        INSERT INTO {CALLBACKS_TABLE}
                            (prospect_id, campaign_id, call_attempt_id, campaign_prospect_id,
                             scheduled_for, note)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING *
                        """,
                        prospect_id,
                        campaign_id,
                        call_attempt_id,
                        campaign_prospect_id,
                        scheduled_for,
                        note,
                    )
                return row

        return _callback(await self._phase7(write(), CALLBACKS_TABLE))

    async def get_callback(self, callback_id: int) -> ScheduledCallback | None:
        """One callback by id, or None."""
        row = await self._phase7(
            self._pool.fetchrow(f"SELECT * FROM {CALLBACKS_TABLE} WHERE id = $1", callback_id),
            CALLBACKS_TABLE,
        )
        return _callback(row) if row else None

    async def list_callbacks(
        self,
        *,
        prospect_id: int | None = None,
        status: CallbackStatus | None = CallbackStatus.PENDING,
        due_before: datetime | None = None,
        limit: int = 50,
        campaign_id: int | None = None,
    ) -> list[ScheduledCallback]:
        """Callbacks, soonest first.

        Args:
            status: Filter; None returns every status.
            due_before: Only callbacks scheduled at or before this moment — pass
                `now` for "what is due".
            campaign_id: Only callbacks for one campaign (Phase 17).
        """
        rows = await self._phase7(
            self._pool.fetch(
                f"""
                SELECT * FROM {CALLBACKS_TABLE}
                WHERE ($1::bigint IS NULL OR prospect_id = $1)
                  AND ($2::text IS NULL OR status = $2)
                  AND ($3::timestamptz IS NULL OR scheduled_for <= $3)
                  AND ($5::bigint IS NULL OR campaign_id = $5)
                ORDER BY scheduled_for, id
                LIMIT $4
                """,
                prospect_id,
                str(status) if status else None,
                due_before,
                limit,
                campaign_id,
            ),
            CALLBACKS_TABLE,
        )
        return [_callback(row) for row in rows]

    async def set_callbacks_status(
        self,
        prospect_id: int,
        status: CallbackStatus,
        *,
        only: CallbackStatus = CallbackStatus.PENDING,
    ) -> int:
        """Move a prospect's callbacks in status `only` to `status`. Returns how many."""
        result = await self._phase7(
            self._pool.execute(
                f"""
                UPDATE {CALLBACKS_TABLE}
                SET status = $2, updated_at = now()
                WHERE prospect_id = $1 AND status = $3
                """,
                prospect_id,
                str(status),
                str(only),
            ),
            CALLBACKS_TABLE,
        )
        return int(result.rsplit(" ", 1)[-1])

    async def cancel_callback(self, callback_id: int) -> bool:
        """Withdraw one pending callback. Returns whether it was pending."""
        result = await self._phase7(
            self._pool.execute(
                f"""
                UPDATE {CALLBACKS_TABLE}
                SET status = 'CANCELLED', updated_at = now()
                WHERE id = $1 AND status = 'PENDING'
                """,
                callback_id,
            ),
            CALLBACKS_TABLE,
        )
        return result.rsplit(" ", 1)[-1] != "0"

    async def reopen_membership(self, membership_id: int, *, next_attempt_at: datetime) -> bool:
        """Put a membership back in the queue for a specific time.

        The mechanism by which a scheduled callback becomes a call: the queue
        already orders by `next_attempt_at` and refuses anything not yet due, so
        a membership reopened for Tuesday at ten is handed out on Tuesday at ten
        and not before. The attempt limit still applies — see the handoff.

        Returns:
            Whether a membership was updated.
        """
        result = await self._pool.execute(
            f"""
            UPDATE {MEMBERSHIPS_TABLE}
            SET status = 'PENDING', next_attempt_at = $2, updated_at = now()
            WHERE id = $1
            """,
            membership_id,
            next_attempt_at,
        )
        return result.rsplit(" ", 1)[-1] != "0"

    # --- Meetings (Phase 7) -------------------------------------------------

    @_timed("add_meeting")
    async def add_meeting(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        provider: str,
        prospect_id: int | None = None,
        reference: str | None = None,
        timezone: str = "UTC",
        campaign_id: int | None = None,
        call_attempt_id: int | None = None,
        attendee_name: str | None = None,
        attendee_email: str | None = None,
        notes: str | None = None,
    ) -> Meeting:
        """Record a booking the calendar has confirmed.

        Raises:
            CampaignStoreError: The table is missing (schema predates Phase 7).
        """
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise ValueError("meeting times must be timezone-aware")
        try:
            row = await self._phase7(
                self._pool.fetchrow(
                    f"""
                    INSERT INTO {MEETINGS_TABLE}
                        (prospect_id, campaign_id, call_attempt_id, provider, reference,
                         start_at, end_at, timezone, attendee_name, attendee_email, notes)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    RETURNING *
                    """,
                    prospect_id,
                    campaign_id,
                    call_attempt_id,
                    provider,
                    reference,
                    start_at,
                    end_at,
                    timezone,
                    attendee_name,
                    attendee_email,
                    notes,
                ),
                MEETINGS_TABLE,
            )
        except asyncpg.ExclusionViolationError as exc:
            # Phase 16: the diary refused the overlap. The slot was free when
            # it was offered and is not now — the same thing the provider's
            # own check reports, decided here where it cannot be raced.
            raise MeetingConflictError(
                f"that time has just been taken ({start_at:%Y-%m-%d %H:%M %Z} overlaps a booking)"
            ) from exc
        return _meeting(row)

    async def list_meetings(
        self,
        *,
        prospect_id: int | None = None,
        from_time: datetime | None = None,
        status: MeetingStatus | None = MeetingStatus.BOOKED,
        limit: int = 50,
    ) -> list[Meeting]:
        """Meetings, soonest first, optionally from a moment onwards."""
        rows = await self._phase7(
            self._pool.fetch(
                f"""
                SELECT * FROM {MEETINGS_TABLE}
                WHERE ($1::bigint IS NULL OR prospect_id = $1)
                  AND ($2::timestamptz IS NULL OR start_at >= $2)
                  AND ($3::text IS NULL OR status = $3)
                ORDER BY start_at, id
                LIMIT $4
                """,
                prospect_id,
                from_time,
                str(status) if status else None,
                limit,
            ),
            MEETINGS_TABLE,
        )
        return [_meeting(row) for row in rows]

    async def get_meeting(self, meeting_id: int) -> Meeting | None:
        """One meeting by id, or None. Phase 17: the outbox reads a booking by its row."""
        row = await self._phase7(
            self._pool.fetchrow(f"SELECT * FROM {MEETINGS_TABLE} WHERE id = $1", meeting_id),
            MEETINGS_TABLE,
        )
        return _meeting(row) if row else None

    async def busy_between(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """Booked intervals overlapping `[start, end)`. The local calendar's `BusySource`."""
        rows = await self._phase7(
            self._pool.fetch(
                f"""
                SELECT start_at, end_at FROM {MEETINGS_TABLE}
                WHERE status = 'BOOKED' AND start_at < $2 AND end_at > $1
                ORDER BY start_at
                """,
                start,
                end,
            ),
            MEETINGS_TABLE,
        )
        return [(row["start_at"], row["end_at"]) for row in rows]

    # --- Call results (Phase 8) ---------------------------------------------

    @_timed("save_call_result")
    async def save_call_result(self, result: CallResult) -> CallResult | None:
        """Validate a call result and write it, one row per attempt.

        The write is an upsert with one rule, applied in the statement itself
        so that two writers cannot race past it: **a conversation result always
        wins.** The bot writes the rich result at the end of a call it held;
        the dialer writes a thin one from the carrier's report, which may
        arrive before or after. A carrier result replaces only another carrier
        result, and a conversation result replaces anything. Whichever order
        they land in, the row ends up as the conversation's.

        Args:
            result: The result to store. Validated first — see
                `results.validate_call_result` — and refused, not stored, when
                it is not consistent with its own evidence.

        Returns:
            The stored row, or None when the rule above refused the write
            (a carrier result arriving after a conversation one). None is a
            normal answer, not a failure.

        Raises:
            CallResultValidationError: The result is not valid. Nothing is written.
            CampaignStoreError: The table is missing (schema predates Phase 8),
                or the attempt does not exist.
        """
        problems = validate_call_result(result)
        if problems:
            raise CallResultValidationError(problems)

        row = await self._optional_table(
            self._pool.fetchrow(
                f"""
                INSERT INTO {RESULTS_TABLE} (
                    call_attempt_id, prospect_id, campaign_id, source, schema_version,
                    call_status, disposition, duration_seconds, failure_reason,
                    qualification_status, interest_level, buying_timeline, decision_role, next_action,
                    meeting_status, meeting_start, meeting_reference, meeting_when,
                    callback_status, callback_scheduled_for, callback_when,
                    pain_points, objections, questions,
                    existing_provider, current_process, impact, desired_outcome, notes,
                    human_requested, transferred, agent_ended_call, caller_turns, agent_turns,
                    final_state, timezone, summary, summary_text, transcript, tool_actions, issues
                )
                VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12, $13, $14,
                    $15, $16, $17, $18,
                    $19, $20, $21,
                    $22::jsonb, $23::jsonb, $24::jsonb,
                    $25, $26, $27, $28, $29::jsonb,
                    $30, $31, $32, $33, $34,
                    $35, $36, $37::jsonb, $38, $39::jsonb, $40::jsonb, $41::jsonb
                )
                ON CONFLICT (call_attempt_id) DO UPDATE SET
                    prospect_id = EXCLUDED.prospect_id,
                    campaign_id = EXCLUDED.campaign_id,
                    source = EXCLUDED.source,
                    schema_version = EXCLUDED.schema_version,
                    call_status = EXCLUDED.call_status,
                    disposition = EXCLUDED.disposition,
                    duration_seconds = EXCLUDED.duration_seconds,
                    failure_reason = EXCLUDED.failure_reason,
                    qualification_status = EXCLUDED.qualification_status,
                    interest_level = EXCLUDED.interest_level,
                    buying_timeline = EXCLUDED.buying_timeline,
                    decision_role = EXCLUDED.decision_role,
                    next_action = EXCLUDED.next_action,
                    meeting_status = EXCLUDED.meeting_status,
                    meeting_start = EXCLUDED.meeting_start,
                    meeting_reference = EXCLUDED.meeting_reference,
                    meeting_when = EXCLUDED.meeting_when,
                    callback_status = EXCLUDED.callback_status,
                    callback_scheduled_for = EXCLUDED.callback_scheduled_for,
                    callback_when = EXCLUDED.callback_when,
                    pain_points = EXCLUDED.pain_points,
                    objections = EXCLUDED.objections,
                    questions = EXCLUDED.questions,
                    existing_provider = EXCLUDED.existing_provider,
                    current_process = EXCLUDED.current_process,
                    impact = EXCLUDED.impact,
                    desired_outcome = EXCLUDED.desired_outcome,
                    notes = EXCLUDED.notes,
                    human_requested = EXCLUDED.human_requested,
                    transferred = EXCLUDED.transferred,
                    agent_ended_call = EXCLUDED.agent_ended_call,
                    caller_turns = EXCLUDED.caller_turns,
                    agent_turns = EXCLUDED.agent_turns,
                    final_state = EXCLUDED.final_state,
                    timezone = EXCLUDED.timezone,
                    summary = EXCLUDED.summary,
                    summary_text = EXCLUDED.summary_text,
                    transcript = EXCLUDED.transcript,
                    tool_actions = EXCLUDED.tool_actions,
                    issues = EXCLUDED.issues,
                    updated_at = now()
                WHERE {RESULTS_TABLE}.source <> $42 OR EXCLUDED.source = $42
                RETURNING *
                """,
                result.call_attempt_id,
                result.prospect_id,
                result.campaign_id,
                result.source.value,
                result.schema_version,
                result.call_status.value,
                result.disposition.value,
                result.duration_seconds,
                result.failure_reason,
                result.qualification_status.value,
                result.interest_level.value,
                result.buying_timeline.value,
                result.decision_role.value,
                result.next_action.value,
                result.meeting_status.value,
                result.meeting_start,
                result.meeting_reference,
                result.meeting_when,
                result.callback_status.value,
                result.callback_scheduled_for,
                result.callback_when,
                _dumps(list(result.pain_points)),
                _dumps(list(result.objections)),
                _dumps(list(result.questions)),
                result.existing_provider,
                result.current_process,
                result.impact,
                result.desired_outcome,
                _dumps(list(result.notes)),
                result.human_requested,
                result.transferred,
                result.agent_ended_call,
                result.caller_turns,
                result.agent_turns,
                result.final_state,
                result.timezone,
                _dumps(result.summary.to_dict()),
                result.summary.text,
                _dumps(list(result.transcript)),
                _dumps(list(result.tool_actions)),
                _dumps(list(result.issues)),
                ResultSource.CONVERSATION.value,
            ),
            RESULTS_TABLE,
            phase="Phase 8",
            foreign_key=(
                f"call attempt {result.call_attempt_id} or prospect {result.prospect_id} does not exist; "
                f"a result belongs to an attempt row"
            ),
        )
        return _call_result(row) if row else None

    async def get_call_result(self, call_attempt_id: int) -> CallResult | None:
        """The result of one attempt, or None when none has been written."""
        row = await self._optional_table(
            self._pool.fetchrow(
                f"SELECT * FROM {RESULTS_TABLE} WHERE call_attempt_id = $1", call_attempt_id
            ),
            RESULTS_TABLE,
            phase="Phase 8",
        )
        return _call_result(row) if row else None

    async def list_call_results(
        self,
        *,
        prospect_id: int | None = None,
        campaign_id: int | None = None,
        disposition: Disposition | None = None,
        limit: int = 50,
        since: datetime | None = None,
        before_id: int | None = None,
    ) -> list[CallResult]:
        """Results, newest first, optionally narrowed to a prospect, a campaign or a disposition.

        Phase 17 adds two ways to page: `since` keeps results updated at or
        after a moment (an integration asking "what is new since I last
        looked"), and `before_id` keeps results with a smaller id than the
        last one seen (a stable cursor down a list that is being appended to).
        """
        rows = await self._optional_table(
            self._pool.fetch(
                f"""
                SELECT * FROM {RESULTS_TABLE}
                WHERE ($1::bigint IS NULL OR prospect_id = $1)
                  AND ($2::bigint IS NULL OR campaign_id = $2)
                  AND ($3::text IS NULL OR disposition = $3)
                  AND ($5::timestamptz IS NULL OR updated_at >= $5)
                  AND ($6::bigint IS NULL OR id < $6)
                ORDER BY id DESC
                LIMIT $4
                """,
                prospect_id,
                campaign_id,
                disposition.value if disposition else None,
                limit,
                since,
                before_id,
            ),
            RESULTS_TABLE,
            phase="Phase 8",
        )
        return [_call_result(row) for row in rows]

    @_timed("save_call_usage")
    async def save_call_usage(
        self,
        attempt_id: int,
        usage: dict[str, Any],
        *,
        cost_usd: float | None = None,
    ) -> bool:
        """Store what one call consumed. Phase 11.

        Written by the bot at teardown, next to the conversation record and for
        the same reason: it is the only moment the totals are complete. Never
        raises for a schema that predates the columns — a database nobody has
        re-initialised keeps working and simply records no usage.

        Returns:
            Whether the attempt existed and the columns were there to write to.
        """
        try:
            row = await self._pool.fetchrow(
                f"""
                UPDATE {ATTEMPTS_TABLE}
                SET usage = $2::jsonb, cost_usd = $3, updated_at = now()
                WHERE id = $1
                RETURNING id
                """,
                attempt_id,
                json.dumps(usage, ensure_ascii=False, default=str),
                cost_usd,
            )
        except asyncpg.UndefinedColumnError:
            logger.warning(
                "USAGE | this database has no usage column; run `uv run campaign.py init` "
                "to record what calls consume"
            )
            return False
        return row is not None

    # --- Reporting (Phase 10) ------------------------------------------------
    #
    # Read-only aggregates for the dashboard. Every one of them counts in SQL
    # rather than fetching rows and counting in Python: the numbers have to stay
    # correct when the history is large, and a dashboard that loads a hundred
    # thousand attempt rows to display "total calls" is a dashboard nobody
    # leaves open.
    #
    # The two tables that a database might not have yet — `call_results` and
    # `meetings`, added in Phases 8 and 7 — are read through `_optional_table`,
    # and their callers return `None` rather than raising. A dashboard on an
    # older schema shows the numbers it can and says the rest are unavailable,
    # which is more useful than a stack trace.

    async def prospect_counts(self, *, campaign_id: int | None = None) -> dict[str, int]:
        """How many people are on the list, and how many are callable.

        Phase 20: `campaign_id` narrows to the people in one campaign, through
        the memberships. Prospects are not time-scoped, so a date range does
        not apply here.
        """
        row = await self._pool.fetchrow(
            f"""
            SELECT count(*)                                          AS total,
                   count(*) FILTER (WHERE status = 'DO_NOT_CALL')    AS do_not_call,
                   count(*) FILTER (WHERE status = 'UNREACHABLE')    AS unreachable,
                   count(*) FILTER (WHERE phone_normalized IS NOT NULL
                                      AND status <> 'DO_NOT_CALL')   AS callable
            FROM {PROSPECTS_TABLE} p
            WHERE ($1::bigint IS NULL OR EXISTS (
                      SELECT 1 FROM {MEMBERSHIPS_TABLE} m
                      WHERE m.prospect_id = p.id AND m.campaign_id = $1))
            """,
            campaign_id,
        )
        return {name: int(row[name]) for name in ("total", "do_not_call", "unreachable", "callable")}

    async def attempt_counts(
        self,
        *,
        campaign_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        """Call totals by outcome, and the average duration of the answered ones.

        Phase 20: `campaign_id`, `since` and `until` narrow the scan (on
        `created_at`); the statement is otherwise the same single pass, and
        the quality figures Phase 12 measured per call are read from the
        `usage` column in it — the average of each call's median response
        latency, the failed turns, the calls that logged an error.

        `answered` and `completed` overlap on purpose and are not the same
        number: `completed` is the carrier's word for a call that ran to its
        end, and `answered` is every status that means somebody picked up —
        which includes a call the person ended by asking never to be called
        again. Both are reported because a dashboard that showed only one would
        undercount reached people or overcount clean endings.

        The average is over calls that *have* a duration, and `with_duration`
        says how many that was: an average over three calls is a number worth
        distrusting, and the only way for a reader to know that is to be told
        the count next to it.

        Phase 11 folded the usage totals into the same pass rather than adding
        a query: a full scan of `call_attempts` is the expensive part (measured
        at 40 ms over 60,000 rows) and doing it twice to read two more columns
        would double that for nothing.
        """
        query = f"""
            SELECT count(*)                                              AS total,
                   count(*) FILTER (WHERE status IN ({REACHED_STATUS_SQL})) AS answered,
                   count(*) FILTER (WHERE status = 'COMPLETED')          AS completed,
                   count(*) FILTER (WHERE status = 'FAILED')             AS failed,
                   count(*) FILTER (WHERE status = 'NO_ANSWER')          AS no_answer,
                   count(*) FILTER (WHERE status = 'BUSY')               AS busy,
                   count(*) FILTER (WHERE status = 'VOICEMAIL')          AS voicemail,
                   count(*) FILTER (WHERE status = 'DO_NOT_CALL')        AS do_not_call,
                   count(*) FILTER (WHERE status = 'NOT_INTERESTED')     AS not_interested,
                   count(*) FILTER (WHERE status = 'CALLBACK_REQUESTED') AS callback_requested,
                   count(*) FILTER (WHERE status IN ({LIVE_STATUS_SQL})) AS live,
                   count(*) FILTER (WHERE status = 'UNRESOLVED')         AS unresolved,
                   count(*) FILTER (WHERE duration_seconds > 0)          AS with_duration,
                   avg(duration_seconds) FILTER (WHERE duration_seconds > 0) AS average_duration,
                   sum(duration_seconds) FILTER (WHERE duration_seconds > 0) AS total_duration,
                   count(*) FILTER (WHERE failure_reason IS NOT NULL)    AS with_failure_reason,
                   count(*) FILTER (WHERE status = 'FAILED'
                                      AND telephony_call_id IS NULL)     AS refused_before_dial,
                   count(*) FILTER (WHERE status IN ('NO_ANSWER', 'BUSY', 'VOICEMAIL', 'FAILED'))
                                                                          AS unreached
                   {{usage_columns}}
            FROM {ATTEMPTS_TABLE}
            WHERE ($1::bigint IS NULL OR campaign_id = $1)
              AND ($2::timestamptz IS NULL OR created_at >= $2)
              AND ($3::timestamptz IS NULL OR created_at < $3)
            """

        # Phase 11's usage columns are read in the same pass when they exist,
        # and the whole clause is dropped when they do not — a database nobody
        # has re-initialised still answers every other question rather than
        # failing the dashboard entirely. Phase 20 reads the per-call quality
        # summary the sink writes into the same JSON (`usage -> 'quality'`).
        usage_sql = """,
                   count(*) FILTER (WHERE usage IS NOT NULL)             AS with_usage,
                   sum((usage -> 'llm' ->> 'prompt_tokens')::bigint)     AS prompt_tokens,
                   sum((usage -> 'llm' ->> 'completion_tokens')::bigint) AS completion_tokens,
                   sum((usage -> 'llm' ->> 'requests')::bigint)          AS llm_requests,
                   count(*) FILTER (WHERE cost_usd IS NOT NULL)          AS with_cost,
                   sum(cost_usd)                                         AS total_cost,
                   avg(cost_usd)                                         AS average_cost,
                   count(*) FILTER (WHERE (usage -> 'quality' ->> 'p50_ms') IS NOT NULL)
                                                                         AS with_latency,
                   avg((usage -> 'quality' ->> 'p50_ms')::double precision)
                                                                         AS response_p50_ms,
                   avg((usage -> 'quality' ->> 'p95_ms')::double precision)
                                                                         AS response_p95_ms,
                   avg((usage -> 'quality' ->> 'greeting_ms')::double precision)
                                                                         AS greeting_ms,
                   count(*) FILTER (WHERE usage -> 'quality' IS NOT NULL) AS with_quality,
                   sum((usage -> 'quality' ->> 'failed_turns')::int)     AS failed_turns,
                   sum((usage -> 'quality' ->> 'late_turns')::int)       AS late_turns,
                   sum((usage -> 'quality' ->> 'barge_ins')::int)        AS barge_ins,
                   count(*) FILTER (WHERE (usage -> 'quality' ->> 'errors')::int > 0)
                                                                         AS calls_with_errors"""
        try:
            row = await self._pool.fetchrow(query.format(usage_columns=usage_sql), campaign_id, since, until)
            has_usage = True
        except asyncpg.UndefinedColumnError:
            row = await self._pool.fetchrow(query.format(usage_columns=""), campaign_id, since, until)
            has_usage = False

        counts: dict[str, Any] = {
            name: int(row[name])
            for name in (
                "total", "answered", "completed", "failed", "no_answer", "busy",
                "voicemail", "do_not_call", "not_interested", "callback_requested", "live",
                "unresolved", "with_duration", "with_failure_reason", "refused_before_dial",
                "unreached",
            )
        }
        counts["quality_available"] = has_usage
        counts.update(
            {
                name: int(row[name] or 0) if has_usage else 0
                for name in ("with_latency", "with_quality", "failed_turns", "late_turns",
                             "barge_ins", "calls_with_errors")
            }
        )
        for name in ("response_p50_ms", "response_p95_ms", "greeting_ms"):
            counts[name] = (
                int(round(float(row[name]))) if has_usage and row[name] is not None else None
            )
        counts["average_duration_secs"] = (
            round(float(row["average_duration"]), 1) if row["average_duration"] is not None else None
        )
        counts["total_duration_secs"] = int(row["total_duration"] or 0)
        counts["usage_available"] = has_usage
        counts.update(
            {
                name: int(row[name] or 0) if has_usage else 0
                for name in ("with_usage", "prompt_tokens", "completion_tokens",
                             "llm_requests", "with_cost")
            }
        )
        counts["total_cost_usd"] = (
            round(float(row["total_cost"]), 4)
            if has_usage and row["total_cost"] is not None
            else None
        )
        counts["average_cost_usd"] = (
            round(float(row["average_cost"]), 4)
            if has_usage and row["average_cost"] is not None
            else None
        )
        return counts

    async def result_counts(
        self,
        *,
        campaign_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Qualification and disposition totals from the call results. Phase 8's table.

        `qualified_prospects` counts *people*, not calls: somebody qualified on
        two attempts is one qualified prospect, and a dashboard that said two
        would overstate the pipeline.

        Phase 20 narrows by campaign and by the result's `created_at`, and
        reads the rest of what a conversion or compliance figure needs:
        callbacks requested as well as scheduled, the opt-outs, the calls the
        list refused, the meetings agreed but not booked.

        Returns:
            None when the table does not exist — a database that predates
            Phase 8. The dashboard shows "unavailable" rather than zero, since
            zero would read as "nobody qualified".
        """
        try:
            row = await self._optional_table(
                self._pool.fetchrow(
                    f"""
                    SELECT count(*)                                                   AS total,
                           count(DISTINCT prospect_id)
                               FILTER (WHERE qualification_status = 'QUALIFIED')      AS qualified_prospects,
                           count(*) FILTER (WHERE qualification_status = 'QUALIFIED') AS qualified_calls,
                           count(*) FILTER (WHERE qualification_status = 'PARTIALLY_QUALIFIED')
                                                                                      AS partially_qualified,
                           count(*) FILTER (WHERE qualification_status = 'DISQUALIFIED')
                                                                                      AS disqualified,
                           count(*) FILTER (WHERE meeting_status = 'BOOKED')          AS meetings_booked,
                           count(*) FILTER (WHERE meeting_status = 'AGREED')          AS meetings_agreed,
                           count(*) FILTER (WHERE callback_status = 'SCHEDULED')      AS callbacks_scheduled,
                           count(*) FILTER (WHERE callback_status = 'REQUESTED')      AS callbacks_requested,
                           count(*) FILTER (WHERE transferred)                        AS transferred,
                           count(*) FILTER (WHERE human_requested)                    AS human_requested,
                           count(*) FILTER (WHERE disposition = 'OPTED_OUT')          AS opted_out,
                           count(*) FILTER (WHERE disposition = 'DO_NOT_CALL')        AS do_not_call,
                           count(*) FILTER (WHERE disposition = 'NOT_INTERESTED')     AS not_interested,
                           count(*) FILTER (WHERE call_status IN ({REACHED_STATUS_SQL}))
                                                                                      AS answered
                    FROM {RESULTS_TABLE}
                    WHERE ($1::bigint IS NULL OR campaign_id = $1)
                      AND ($2::timestamptz IS NULL OR created_at >= $2)
                      AND ($3::timestamptz IS NULL OR created_at < $3)
                    """,
                    campaign_id,
                    since,
                    until,
                ),
                RESULTS_TABLE,
                phase="Phase 8",
            )
        except CampaignStoreError:
            return None
        return {
            name: int(row[name])
            for name in (
                "total", "qualified_prospects", "qualified_calls", "partially_qualified",
                "disqualified", "meetings_booked", "meetings_agreed", "callbacks_scheduled",
                "callbacks_requested", "transferred", "human_requested", "opted_out",
                "do_not_call", "not_interested", "answered",
            )
        }

    async def disposition_counts(
        self,
        *,
        campaign_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, int] | None:
        """How many results reached each disposition. Phase 8's one-word outcome.

        Returns:
            `{disposition: count}`, or None when the table does not exist.
        """
        try:
            rows = await self._optional_table(
                self._pool.fetch(
                    f"""
                    SELECT disposition, count(*) AS count
                    FROM {RESULTS_TABLE}
                    WHERE ($1::bigint IS NULL OR campaign_id = $1)
                      AND ($2::timestamptz IS NULL OR created_at >= $2)
                      AND ($3::timestamptz IS NULL OR created_at < $3)
                    GROUP BY disposition
                    ORDER BY count DESC, disposition
                    """,
                    campaign_id,
                    since,
                    until,
                ),
                RESULTS_TABLE,
                phase="Phase 8",
            )
        except CampaignStoreError:
            return None
        return {str(row["disposition"]): int(row["count"]) for row in rows}

    async def meeting_counts(
        self,
        *,
        campaign_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, int] | None:
        """Booked and upcoming meetings. Phase 7's table.

        `unattributed` counts bookings with no campaign — an eval session or a
        browser call books a real slot in the calendar and has no campaign to
        attribute it to. Without that number the dashboard shows a booking in
        its total that appears against no campaign in the table below it, which
        reads as a bug rather than as the truth it is.

        Returns:
            None when the table does not exist.
        """
        try:
            row = await self._optional_table(
                self._pool.fetchrow(
                    f"""
                    SELECT count(*) FILTER (WHERE status = 'BOOKED')       AS booked,
                           count(*) FILTER (WHERE status = 'BOOKED'
                                              AND start_at >= now())       AS upcoming,
                           count(*) FILTER (WHERE status = 'BOOKED'
                                              AND campaign_id IS NULL)     AS unattributed,
                           count(*) FILTER (WHERE status = 'CANCELLED')    AS cancelled
                    FROM {MEETINGS_TABLE}
                    WHERE ($1::bigint IS NULL OR campaign_id = $1)
                      AND ($2::timestamptz IS NULL OR created_at >= $2)
                      AND ($3::timestamptz IS NULL OR created_at < $3)
                    """,
                    campaign_id,
                    since,
                    until,
                ),
                MEETINGS_TABLE,
                phase="Phase 7",
            )
        except CampaignStoreError:
            return None
        return {
            name: int(row[name])
            for name in ("booked", "upcoming", "unattributed", "cancelled")
        }

    async def callback_counts(
        self,
        *,
        campaign_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, int] | None:
        """Pending and due callbacks. Phase 7's table.

        Phase 20: the date range applies to when the callback was *created*;
        `pending` and `due` are about now, whatever the range.

        Returns:
            None when the table does not exist.
        """
        try:
            row = await self._optional_table(
                self._pool.fetchrow(
                    f"""
                    SELECT count(*) FILTER (WHERE status = 'PENDING')      AS pending,
                           count(*) FILTER (WHERE status = 'PENDING'
                                              AND scheduled_for <= now())  AS due,
                           count(*) FILTER (WHERE status = 'PLACED')       AS placed,
                           count(*) FILTER (WHERE status = 'CANCELLED')    AS cancelled
                    FROM {CALLBACKS_TABLE}
                    WHERE ($1::bigint IS NULL OR campaign_id = $1)
                      AND ($2::timestamptz IS NULL OR created_at >= $2)
                      AND ($3::timestamptz IS NULL OR created_at < $3)
                    """,
                    campaign_id,
                    since,
                    until,
                ),
                CALLBACKS_TABLE,
                phase="Phase 7",
            )
        except CampaignStoreError:
            return None
        return {name: int(row[name]) for name in ("pending", "due", "placed", "cancelled")}

    async def campaign_overview(
        self, *, limit: int = 10, campaign_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Per-campaign progress and call outcomes, newest campaign first.

        Every count is a scalar subquery rather than a join. Joining
        memberships *and* attempts to campaigns in one statement multiplies the
        two together — a campaign with 3 memberships and 4 attempts would
        report 12 of each — and the usual fix, `count(DISTINCT ...)` on every
        column, is both slower and easy to forget on the next column somebody
        adds. Subqueries cannot be got wrong that way.

        The qualification and meeting numbers are *not* here: they live in
        `call_results`, which a database may not have. `dashboard/stats.py`
        merges them in from `campaign_result_counts`, so a missing table costs
        those two columns rather than the whole table.
        """
        # Phase 20: one campaign when asked, and the membership statuses that
        # say how far along it is. The attempt columns stay unfiltered by
        # date on purpose — a campaign's progress is the whole campaign.
        rows = await self._pool.fetch(
            f"""
            SELECT c.id, c.name, c.status, c.created_at, c.started_at, c.completed_at,
                   (SELECT count(*) FROM {MEMBERSHIPS_TABLE} m
                     WHERE m.campaign_id = c.id)                            AS prospects,
                   (SELECT count(*) FROM {MEMBERSHIPS_TABLE} m
                     WHERE m.campaign_id = c.id AND m.status = 'PENDING')   AS pending,
                   (SELECT count(*) FROM {MEMBERSHIPS_TABLE} m
                     WHERE m.campaign_id = c.id AND m.status = 'IN_PROGRESS') AS in_progress,
                   (SELECT count(*) FROM {MEMBERSHIPS_TABLE} m
                     WHERE m.campaign_id = c.id AND m.status = 'COMPLETED') AS completed,
                   (SELECT count(*) FROM {MEMBERSHIPS_TABLE} m
                     WHERE m.campaign_id = c.id AND m.status = 'EXHAUSTED') AS exhausted,
                   (SELECT count(*) FROM {MEMBERSHIPS_TABLE} m
                     WHERE m.campaign_id = c.id AND m.status = 'SKIPPED')   AS skipped,
                   (SELECT count(*) FROM {ATTEMPTS_TABLE} a
                     WHERE a.campaign_id = c.id)                            AS attempts,
                   (SELECT count(*) FROM {ATTEMPTS_TABLE} a
                     WHERE a.campaign_id = c.id
                       AND a.status IN ({REACHED_STATUS_SQL}))              AS answered,
                   (SELECT count(*) FROM {ATTEMPTS_TABLE} a
                     WHERE a.campaign_id = c.id AND a.status = 'FAILED')    AS failed,
                   (SELECT count(*) FROM {ATTEMPTS_TABLE} a
                     WHERE a.campaign_id = c.id AND a.status = 'VOICEMAIL') AS voicemail,
                   (SELECT count(*) FROM {ATTEMPTS_TABLE} a
                     WHERE a.campaign_id = c.id
                       AND a.status IN ({LIVE_STATUS_SQL}))                 AS live,
                   (SELECT avg(a.duration_seconds) FROM {ATTEMPTS_TABLE} a
                     WHERE a.campaign_id = c.id AND a.duration_seconds > 0) AS average_duration
            FROM {CAMPAIGNS_TABLE} c
            WHERE ($2::bigint IS NULL OR c.id = $2)
            ORDER BY c.id DESC
            LIMIT $1
            """,
            limit,
            campaign_id,
        )
        return [
            {
                "id": int(row["id"]),
                "name": row["name"],
                "status": row["status"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "prospects": int(row["prospects"]),
                "pending": int(row["pending"]),
                "in_progress": int(row["in_progress"]),
                "completed_members": int(row["completed"]),
                "exhausted": int(row["exhausted"]),
                "skipped": int(row["skipped"]),
                "attempts": int(row["attempts"]),
                "answered": int(row["answered"]),
                "failed": int(row["failed"]),
                "voicemail": int(row["voicemail"]),
                "live": int(row["live"]),
                "average_duration_secs": (
                    round(float(row["average_duration"]), 1)
                    if row["average_duration"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    async def campaign_result_counts(
        self,
        *,
        campaign_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[int, dict[str, int]] | None:
        """Qualified prospects, booked meetings, transfers and opt-outs per campaign, from the results.

        Returns:
            `{campaign_id: {...}}`, or None when `call_results` does not exist.
        """
        try:
            rows = await self._optional_table(
                self._pool.fetch(
                    f"""
                    SELECT campaign_id,
                           count(DISTINCT prospect_id)
                               FILTER (WHERE qualification_status = 'QUALIFIED') AS qualified,
                           count(*) FILTER (WHERE meeting_status = 'BOOKED')     AS meetings,
                           count(*) FILTER (WHERE callback_status = 'SCHEDULED') AS callbacks,
                           count(*) FILTER (WHERE transferred)                   AS transferred,
                           count(*) FILTER (WHERE disposition = 'OPTED_OUT')     AS opted_out
                    FROM {RESULTS_TABLE}
                    WHERE campaign_id IS NOT NULL
                      AND ($1::bigint IS NULL OR campaign_id = $1)
                      AND ($2::timestamptz IS NULL OR created_at >= $2)
                      AND ($3::timestamptz IS NULL OR created_at < $3)
                    GROUP BY campaign_id
                    """,
                    campaign_id,
                    since,
                    until,
                ),
                RESULTS_TABLE,
                phase="Phase 8",
            )
        except CampaignStoreError:
            return None
        return {
            int(row["campaign_id"]): {
                "qualified": int(row["qualified"]),
                "meetings": int(row["meetings"]),
                "callbacks": int(row["callbacks"]),
                "transferred": int(row["transferred"]),
                "opted_out": int(row["opted_out"]),
            }
            for row in rows
        }

    async def recent_call_rows(
        self,
        *,
        limit: int = 15,
        campaign_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        prospect_id: int | None = None,
        status: str | None = None,
        before_id: int | None = None,
        search: str | None = None,
        search_phone: bool = False,
    ) -> list[dict[str, Any]]:
        """The newest call attempts, with who was called and for which campaign.

        One query with two joins, because a dashboard row needs a name and a
        campaign and fetching those per row is the classic way to turn a
        fifteen-row list into thirty-one queries. The prospect join is inner —
        an attempt always has one — and the campaign join is outer, because an
        attempt outlives its campaign (`ON DELETE SET NULL`).

        The disposition and summary come from `call_results` and are merged in
        by `dashboard/stats.py` from `results_for_attempts`, so a database
        without that table still gets the list.

        Phase 20: every filter is optional and applied in the same statement —
        campaign, date range (on `created_at`), prospect, status, a page
        boundary (`before_id`), and a search over the person's name, company
        and email, plus the carrier's call id. `search_phone` adds the digits
        of the number, which is a `read_pii` act; the caller decides.
        """
        pattern = f"%{search.strip()}%" if search and search.strip() else None
        digits = "".join(ch for ch in (search or "") if ch.isdigit()) if search_phone and search else ""
        digit_pattern = f"%{digits}%" if len(digits) >= 4 else None
        rows = await self._pool.fetch(
            f"""
            SELECT a.id, a.status, a.attempt_number, a.duration_seconds,
                   a.telephony_call_id, a.telephony_provider, a.failure_reason,
                   COALESCE(a.started_at, a.created_at) AS at,
                   a.prospect_id, p.first_name, p.last_name,
                   COALESCE(p.phone_normalized, p.phone) AS phone, p.company,
                   a.campaign_id, c.name AS campaign_name
            FROM {ATTEMPTS_TABLE} a
            JOIN {PROSPECTS_TABLE} p ON p.id = a.prospect_id
            LEFT JOIN {CAMPAIGNS_TABLE} c ON c.id = a.campaign_id
            WHERE ($2::bigint IS NULL OR a.campaign_id = $2)
              AND ($3::timestamptz IS NULL OR a.created_at >= $3)
              AND ($4::timestamptz IS NULL OR a.created_at < $4)
              AND ($5::bigint IS NULL OR a.prospect_id = $5)
              AND ($6::text IS NULL OR a.status = $6)
              AND ($7::bigint IS NULL OR a.id < $7)
              AND ($8::text IS NULL
                   OR p.first_name ILIKE $8 OR p.last_name ILIKE $8
                   OR (p.first_name || ' ' || p.last_name) ILIKE $8
                   OR p.company ILIKE $8 OR p.email ILIKE $8
                   OR a.telephony_call_id ILIKE $8
                   OR ($9::text IS NOT NULL AND p.phone_normalized LIKE $9))
            ORDER BY a.id DESC
            LIMIT $1
            """,
            limit,
            campaign_id,
            since,
            until,
            prospect_id,
            status,
            before_id,
            pattern,
            digit_pattern,
        )
        return [dict(row) for row in rows]

    async def get_attempt_usage(self, attempt_id: int) -> tuple[dict[str, Any] | None, float | None]:
        """What one call consumed and cost, from the Phase 11 columns. Phase 20.

        `(None, None)` when the row has none, or the database predates the
        columns — the detail page then says usage was not measured.
        """
        try:
            row = await self._pool.fetchrow(
                f"SELECT usage, cost_usd FROM {ATTEMPTS_TABLE} WHERE id = $1", attempt_id
            )
        except asyncpg.UndefinedColumnError:
            return None, None
        if row is None:
            return None, None
        usage = _json(row["usage"]) if row["usage"] else None
        cost = float(row["cost_usd"]) if row["cost_usd"] is not None else None
        return (usage or None), cost

    async def search_prospects(
        self, query: str, *, limit: int = 20, search_phone: bool = False
    ) -> list[Prospect]:
        """People whose name, company or email match, newest first. Phase 20.

        `search_phone` matches the digits of the number as well — a `read_pii`
        act, so the caller decides. A sequential scan bounded by `LIMIT`; on
        this project's list sizes that is milliseconds, and Phase 11 found
        that indexes on these tables cost more than they saved.
        """
        text = (query or "").strip()
        if not text:
            return []
        pattern = f"%{text}%"
        digits = "".join(ch for ch in text if ch.isdigit()) if search_phone else ""
        digit_pattern = f"%{digits}%" if len(digits) >= 4 else None
        rows = await self._pool.fetch(
            f"""
            SELECT * FROM {PROSPECTS_TABLE} p
            WHERE p.first_name ILIKE $1 OR p.last_name ILIKE $1
               OR (p.first_name || ' ' || p.last_name) ILIKE $1
               OR p.company ILIKE $1 OR p.email ILIKE $1
               OR ($3::text IS NOT NULL AND p.phone_normalized LIKE $3)
            ORDER BY p.id DESC
            LIMIT $2
            """,
            pattern,
            max(1, min(int(limit), 200)),
            digit_pattern,
        )
        return [_prospect(row) for row in rows]

    async def results_for_attempts(self, attempt_ids: list[int]) -> dict[int, dict[str, Any]] | None:
        """Disposition, qualification and summary for a set of attempts.

        Returns:
            `{call_attempt_id: {...}}` for the ones that have a result, or None
            when `call_results` does not exist. An attempt with no result is
            simply absent, which is normal: a call still in progress has none.
        """
        if not attempt_ids:
            return {}
        try:
            rows = await self._optional_table(
                self._pool.fetch(
                    f"""
                    SELECT call_attempt_id, disposition, qualification_status, next_action,
                           meeting_status, callback_status, summary_text
                    FROM {RESULTS_TABLE}
                    WHERE call_attempt_id = ANY($1::bigint[])
                    """,
                    attempt_ids,
                ),
                RESULTS_TABLE,
                phase="Phase 8",
            )
        except CampaignStoreError:
            return None
        return {int(row["call_attempt_id"]): dict(row) for row in rows}

    # --- Webhook deliveries (Phase 14) --------------------------------------

    @_timed("record_webhook_event")
    async def record_webhook_event(
        self,
        *,
        provider: str,
        call_id: str,
        event_key: str,
        kind: str,
        status: str | None = None,
        raw_status: str | None = None,
        sequence: int | None = None,
        carrier_timestamp: datetime | None = None,
        answered_by: str | None = None,
        duration_seconds: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[WebhookDelivery | None, bool]:
        """Write one delivery to the ledger, unless it is already there. Phase 14.

        The idempotency gate: `event_key` is unique, so of two deliveries of
        the same event — a carrier retry, a duplicate POST — exactly one is
        inserted, and the other is told so before anything reads the attempt.

        Returns:
            `(delivery, inserted)`. `inserted` is False for a duplicate, in
            which case `delivery` is the row that got there first.

        Raises:
            CampaignStoreError: The table does not exist (a database that
                predates Phase 14), or the database is unavailable.
        """
        row = await self._phase14(
            self._pool.fetchrow(
                f"""
                INSERT INTO {WEBHOOKS_TABLE}
                    (provider, call_id, event_key, kind, status, raw_status, sequence,
                     carrier_timestamp, answered_by, duration_seconds, payload)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
                ON CONFLICT (event_key) DO NOTHING
                RETURNING *
                """,
                provider,
                call_id,
                event_key,
                kind,
                status,
                raw_status,
                sequence,
                carrier_timestamp,
                answered_by,
                duration_seconds,
                _dumps(payload or {}),
            )
        )
        if row is not None:
            return _delivery(row), True
        existing = await self._pool.fetchrow(
            f"SELECT * FROM {WEBHOOKS_TABLE} WHERE event_key = $1", event_key
        )
        return (_delivery(existing) if existing else None), False

    async def set_webhook_outcome(
        self, delivery_id: int, outcome: str, *, attempt_id: int | None = None
    ) -> None:
        """Record what the receiver did with a delivery, and which attempt it touched."""
        await self._phase14(
            self._pool.execute(
                f"""
                UPDATE {WEBHOOKS_TABLE}
                SET outcome = $2, attempt_id = COALESCE($3, attempt_id)
                WHERE id = $1
                """,
                delivery_id,
                outcome,
                attempt_id,
            )
        )

    async def last_webhook_at(self, call_id: str) -> datetime | None:
        """When the carrier last pushed an event for a call, or None if never.

        The worker's question (Phase 14): a call the carrier is reporting on
        needs polling only as a safety net. One indexed read, in place of one
        carrier request.
        """
        return await self._phase14(
            self._pool.fetchval(
                f"SELECT max(received_at) FROM {WEBHOOKS_TABLE} WHERE call_id = $1", call_id
            )
        )

    async def webhook_answered_by(self, call_id: str) -> str | None:
        """The carrier's latest answering-machine verdict for a call, or None.

        An asynchronous detection's verdict arrives on its own delivery, before
        the completion that needs it; the receiver reads it back here so a
        completed call a machine answered becomes `VOICEMAIL`, exactly as a
        poll that saw `answered_by` on the call resource would make it.
        """
        return await self._phase14(
            self._pool.fetchval(
                f"""
                SELECT answered_by FROM {WEBHOOKS_TABLE}
                WHERE call_id = $1 AND answered_by IS NOT NULL
                ORDER BY received_at DESC
                LIMIT 1
                """,
                call_id,
            )
        )

    async def list_webhook_events(
        self,
        *,
        call_id: str | None = None,
        attempt_id: int | None = None,
        limit: int = 50,
    ) -> list[WebhookDelivery]:
        """Deliveries, newest first, optionally for one call or one attempt."""
        rows = await self._phase14(
            self._pool.fetch(
                f"""
                SELECT * FROM {WEBHOOKS_TABLE}
                WHERE ($1::text IS NULL OR call_id = $1)
                  AND ($2::bigint IS NULL OR attempt_id = $2)
                ORDER BY received_at DESC, id DESC
                LIMIT $3
                """,
                call_id,
                attempt_id,
                limit,
            )
        )
        return [_delivery(row) for row in rows]

    async def webhook_counts(self) -> dict[str, int] | None:
        """How many deliveries ended each way, or None when the table is missing."""
        try:
            rows = await self._phase14(
                self._pool.fetch(
                    f"SELECT outcome, count(*) AS n FROM {WEBHOOKS_TABLE} GROUP BY outcome"
                )
            )
        except CampaignStoreError:
            return None
        return {str(row["outcome"]): int(row["n"]) for row in rows}

    async def _phase14(self, operation: Any) -> Any:
        """Await a statement against the webhook ledger, naming the fix if it is missing."""
        return await self._optional_table(operation, WEBHOOKS_TABLE, phase="Phase 14")

    # --- CRM synchronisation (Phase 15) ---------------------------------------

    async def claim_results_for_sync(
        self,
        provider: str,
        *,
        limit: int = 20,
        now: datetime | None = None,
        stale_secs: float = 900.0,
    ) -> list[tuple[CallResult, CrmSyncRecord]]:
        """Hand out the results the CRM has not seen, marking each as being synced. Phase 15.

        One transaction, two statements:

        1. Every result without a sync row gets one, `PENDING`, keyed by
           `crm.mapping.sync_key` — computed here in SQL so a row exists
           before any syncer looks, and identically however many look.
        2. The claimable rows are selected `FOR UPDATE SKIP LOCKED` and moved
           to `SYNCING` with `attempts + 1`: `PENDING` and due `RETRY` rows, a
           `SYNCING` row older than `stale_secs` (its syncer died), and a
           `SYNCED` row whose result was updated after it was filed.

        Two syncers therefore never receive the same row, and a row is never
        received twice for the same version of its result.

        Args:
            provider: Which CRM these rows belong to. Rows are created for the
                configured provider; rows for another are left alone.
            limit: Rows per claim.
            now: The current moment, injected by the checks.
            stale_secs: How old a `SYNCING` claim may be before it is retaken.

        Returns:
            `(result, sync record)` pairs, oldest first.
        """
        moment = now or datetime.now(UTC)
        async with self._pool.acquire() as connection, connection.transaction():
            await self._phase15(
                connection.execute(
                    f"""
                    INSERT INTO {CRM_SYNC_TABLE}
                        (call_result_id, call_attempt_id, prospect_id, provider, state, sync_key)
                    SELECT r.id, r.call_attempt_id, r.prospect_id, $1, 'PENDING',
                           'aiva' || r.id::text || 'x' || r.call_attempt_id::text
                    FROM {RESULTS_TABLE} r
                    LEFT JOIN {CRM_SYNC_TABLE} s ON s.call_result_id = r.id
                    WHERE s.id IS NULL
                    ON CONFLICT (call_result_id) DO NOTHING
                    """,
                    provider,
                )
            )
            rows = await connection.fetch(
                f"""
                WITH claimable AS (
                    SELECT s.id
                    FROM {CRM_SYNC_TABLE} s
                    JOIN {RESULTS_TABLE} r ON r.id = s.call_result_id
                    WHERE s.provider = $1
                      AND (
                            (s.state IN ('PENDING', 'RETRY')
                             AND (s.next_attempt_at IS NULL OR s.next_attempt_at <= $2))
                         OR (s.state = 'SYNCING'
                             AND (s.started_at IS NULL
                                  OR s.started_at <= $2 - make_interval(secs => $4)))
                         OR (s.state = 'SYNCED'
                             AND (s.result_updated_at IS NULL OR r.updated_at > s.result_updated_at))
                      )
                    ORDER BY s.id
                    LIMIT $3
                    FOR UPDATE OF s SKIP LOCKED
                )
                UPDATE {CRM_SYNC_TABLE} s
                SET state = 'SYNCING',
                    started_at = $2,
                    attempts = s.attempts + 1,
                    updated_at = now()
                FROM claimable
                WHERE s.id = claimable.id
                RETURNING s.*
                """,
                provider,
                moment,
                limit,
                float(max(0.0, stale_secs)),
            )
            if not rows:
                return []
            records = sorted((_crm_sync(row) for row in rows), key=lambda r: r.id)
            results = await connection.fetch(
                f"SELECT * FROM {RESULTS_TABLE} WHERE id = ANY($1::bigint[])",
                [record.call_result_id for record in records],
            )
            by_id = {row["id"]: _call_result(row) for row in results}
            return [(by_id[r.call_result_id], r) for r in records if r.call_result_id in by_id]

    @_timed("record_crm_sync")
    async def record_crm_sync(
        self,
        sync_id: int,
        *,
        state: CrmSyncState | None = None,
        external_contact_id: str | None = None,
        external_activity_id: str | None = None,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
        synced_at: datetime | None = None,
        result_updated_at: datetime | None = None,
        attempts: int | None = None,
    ) -> CrmSyncRecord | None:
        """Write what the syncer learned onto a row. Only the given fields change.

        `error=None` with a `state` clears the last error (a success); the ids
        are never blanked once known; `attempts` is set only when given (the
        claim increments it; a release hands one back).
        """
        row = await self._phase15(
            self._pool.fetchrow(
                f"""
                UPDATE {CRM_SYNC_TABLE}
                SET state = COALESCE($2, state),
                    external_contact_id = COALESCE($3, external_contact_id),
                    external_activity_id = COALESCE($4, external_activity_id),
                    last_error = CASE WHEN $2 IS NULL THEN last_error ELSE $5 END,
                    next_attempt_at = CASE WHEN $2 IS NULL THEN next_attempt_at ELSE $6 END,
                    synced_at = COALESCE($7, synced_at),
                    result_updated_at = COALESCE($8, result_updated_at),
                    attempts = COALESCE($9, attempts),
                    updated_at = now()
                WHERE id = $1
                RETURNING *
                """,
                sync_id,
                str(state) if state is not None else None,
                external_contact_id,
                external_activity_id,
                error,
                next_attempt_at,
                synced_at,
                result_updated_at,
                attempts,
            )
        )
        return _crm_sync(row) if row else None

    async def get_crm_sync(self, call_result_id: int) -> CrmSyncRecord | None:
        """One result's sync row, or None if the syncer has never seen it."""
        row = await self._phase15(
            self._pool.fetchrow(f"SELECT * FROM {CRM_SYNC_TABLE} WHERE call_result_id = $1", call_result_id)
        )
        return _crm_sync(row) if row else None

    async def list_crm_sync(
        self, *, state: CrmSyncState | None = None, limit: int = 50
    ) -> list[CrmSyncRecord]:
        """Sync rows, most recently changed first, optionally in one state."""
        rows = await self._phase15(
            self._pool.fetch(
                f"""
                SELECT * FROM {CRM_SYNC_TABLE}
                WHERE ($1::text IS NULL OR state = $1)
                ORDER BY updated_at DESC, id DESC
                LIMIT $2
                """,
                str(state) if state is not None else None,
                limit,
            )
        )
        return [_crm_sync(row) for row in rows]

    async def crm_sync_counts(self) -> dict[str, int] | None:
        """How many rows are in each state, plus results the syncer has not seen; None if no table."""
        try:
            rows = await self._phase15(
                self._pool.fetch(f"SELECT state, count(*) AS n FROM {CRM_SYNC_TABLE} GROUP BY state")
            )
            unseen = await self._phase15(
                self._pool.fetchval(
                    f"""
                    SELECT count(*) FROM {RESULTS_TABLE} r
                    LEFT JOIN {CRM_SYNC_TABLE} s ON s.call_result_id = r.id
                    WHERE s.id IS NULL
                    """
                )
            )
        except CampaignStoreError:
            return None
        counts = {str(row["state"]): int(row["n"]) for row in rows}
        counts["UNSEEN"] = int(unseen or 0)
        return counts

    async def retry_crm_sync(self, *, call_result_id: int | None = None, all_failed: bool = False) -> int:
        """Reopen failed rows (or one row in any state) as `PENDING`, due now.

        Returns:
            How many rows were reopened.
        """
        if call_result_id is None and not all_failed:
            return 0
        status = await self._phase15(
            self._pool.execute(
                f"""
                UPDATE {CRM_SYNC_TABLE}
                SET state = 'PENDING', next_attempt_at = NULL, last_error = NULL, updated_at = now()
                WHERE ($1::bigint IS NOT NULL AND call_result_id = $1)
                   OR ($1::bigint IS NULL AND state = 'FAILED')
                """,
                call_result_id,
            )
        )
        return int(str(status).rsplit(" ", 1)[-1])

    async def _phase15(self, operation: Any) -> Any:
        """Await a statement against the CRM sync table, naming the fix if it is missing."""
        return await self._optional_table(operation, CRM_SYNC_TABLE, phase="Phase 15")

    # --- Transfers (Phase 16) --------------------------------------------------

    async def add_transfer(
        self,
        *,
        telephony_call_id: str,
        provider: str,
        to_number: str,
        call_attempt_id: int | None = None,
        prospect_id: int | None = None,
        reason: str | None = None,
    ) -> CallTransfer:
        """Record that the carrier accepted a transfer of this call. Phase 16.

        Written by the bot the moment `transfer_call` returns; the outcome
        arrives later through `complete_transfer`.
        """
        row = await self._phase16(
            self._pool.fetchrow(
                f"""
                INSERT INTO {TRANSFERS_TABLE}
                    (call_attempt_id, prospect_id, telephony_call_id, provider, to_number, reason)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
                """,
                call_attempt_id,
                prospect_id,
                telephony_call_id,
                provider,
                to_number,
                reason,
            )
        )
        return _transfer(row)

    async def complete_transfer(
        self,
        telephony_call_id: str,
        *,
        status: TransferStatus,
        provider: str,
        dial_call_id: str | None = None,
        duration_seconds: int | None = None,
        error: str | None = None,
        to_number: str | None = None,
        call_attempt_id: int | None = None,
        prospect_id: int | None = None,
    ) -> CallTransfer | None:
        """Write how the colleague's leg ended onto the call's open transfer. Phase 16.

        The latest `REQUESTED` transfer for the call is completed. When there
        is none — the bot had no database, or the row was never written — one
        is inserted already completed, so the carrier's report is kept either
        way. A second report for a transfer already completed changes nothing
        and returns the row as it stands.

        Returns:
            The transfer as it stands, or None only when nothing could be
            written.
        """
        async with self._pool.acquire() as connection, connection.transaction():
            row = await self._phase16(
                connection.fetchrow(
                    f"""
                    SELECT * FROM {TRANSFERS_TABLE}
                    WHERE telephony_call_id = $1
                    ORDER BY requested_at DESC, id DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    telephony_call_id,
                )
            )
            if row is not None and str(row["status"]) != str(TransferStatus.REQUESTED):
                return _transfer(row)
            if row is not None:
                updated = await connection.fetchrow(
                    f"""
                    UPDATE {TRANSFERS_TABLE}
                    SET status = $2, dial_call_id = COALESCE($3, dial_call_id),
                        duration_seconds = COALESCE($4, duration_seconds),
                        error = $5, completed_at = now(), updated_at = now()
                    WHERE id = $1
                    RETURNING *
                    """,
                    row["id"],
                    str(status),
                    dial_call_id,
                    duration_seconds,
                    error,
                )
                return _transfer(updated)
            inserted = await connection.fetchrow(
                f"""
                INSERT INTO {TRANSFERS_TABLE}
                    (call_attempt_id, prospect_id, telephony_call_id, provider, to_number, status,
                     dial_call_id, duration_seconds, error, completed_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
                RETURNING *
                """,
                call_attempt_id,
                prospect_id,
                telephony_call_id,
                provider,
                to_number or "unknown",
                str(status),
                dial_call_id,
                duration_seconds,
                error,
            )
            return _transfer(inserted)

    async def list_transfers(
        self, *, call_attempt_id: int | None = None, limit: int = 50
    ) -> list[CallTransfer]:
        """Transfers, newest first, optionally for one attempt."""
        rows = await self._phase16(
            self._pool.fetch(
                f"""
                SELECT * FROM {TRANSFERS_TABLE}
                WHERE ($1::bigint IS NULL OR call_attempt_id = $1)
                ORDER BY requested_at DESC, id DESC
                LIMIT $2
                """,
                call_attempt_id,
                limit,
            )
        )
        return [_transfer(row) for row in rows]

    async def transfer_counts(self) -> dict[str, int] | None:
        """How many transfers ended each way, or None when the table is missing."""
        try:
            rows = await self._phase16(
                self._pool.fetch(f"SELECT status, count(*) AS n FROM {TRANSFERS_TABLE} GROUP BY status")
            )
        except CampaignStoreError:
            return None
        return {str(row["status"]): int(row["n"]) for row in rows}

    async def _phase16(self, operation: Any) -> Any:
        """Await a statement against the transfers table, naming the fix if it is missing."""
        return await self._optional_table(operation, TRANSFERS_TABLE, phase="Phase 16")

    # --- Automation events (Phase 17) ---------------------------------------------

    async def claim_automation_events(
        self,
        kinds: tuple[str, ...] | list[str],
        *,
        limit: int = 20,
        now: datetime | None = None,
        settle_secs: float = 30.0,
        stale_secs: float = 600.0,
        since: datetime | None = None,
    ) -> list[AutomationEvent]:
        """Create the events the rows now justify, then hand out the ones that are due. Phase 17.

        One transaction, two halves — the same shape as `claim_results_for_sync`:

        1. **Create.** For each enabled kind, an `INSERT … SELECT` over the
           rows that already record the fact — `call_results`, `meetings`,
           `callbacks`, `campaigns` — for every row without an event of that
           kind, keyed on what the event *is* (`call.completed:result:12`),
           under `ON CONFLICT (event_key) DO NOTHING`. So a fact becomes one
           event however many deliverers run this, and running it again
           creates nothing.

           A result must have been unchanged for `settle_secs` first: the
           carrier's thin result and the conversation's rich one land seconds
           apart, and the settle window is what makes `call.completed` carry
           the rich one. A change *after* the completed event has been
           delivered becomes a `call.updated`, keyed on the result's new
           `updated_at`, and only one open `call.updated` exists per result.
        2. **Claim.** The claimable rows — `PENDING` and due `RETRY` rows, and
           a `DELIVERING` row older than `stale_secs` (its deliverer died) —
           are selected `FOR UPDATE SKIP LOCKED`, oldest fact first, and moved
           to `DELIVERING` with `attempts + 1`. Two deliverers never receive
           the same row.

        Args:
            kinds: Which kinds to create and claim. Anything outside
                `AUTOMATION_EVENT_KINDS` raises `ValueError` before any SQL.
            limit: Rows per claim.
            now: The current moment, injected by the checks.
            settle_secs: How long a result must have been unchanged before
                its events are created.
            stale_secs: How old a `DELIVERING` claim may be before it is retaken.
            since: Only facts recorded at or after this moment get events.
                None means everything the tables hold — on a first run that
                is every result ever written, which is what
                `AUTOMATION_EVENTS_SINCE` exists to bound.

        Returns:
            The claimed events, oldest fact first.
        """
        wanted = tuple(dict.fromkeys(kinds))
        unknown = [kind for kind in wanted if kind not in AUTOMATION_EVENT_KINDS]
        if unknown:
            raise ValueError(
                f"unknown automation event kind(s): {', '.join(unknown)}; "
                f"known: {', '.join(AUTOMATION_EVENT_KINDS)}"
            )
        if not wanted:
            return []
        moment = now or datetime.now(UTC)
        settle = float(max(0.0, settle_secs))

        async with self._pool.acquire() as connection, connection.transaction():
            for kind in wanted:
                statement = _EVENT_CREATE_SQL[kind]
                await self._phase17(connection.execute(statement, moment, settle, since))
            rows = await connection.fetch(
                f"""
                WITH claimable AS (
                    SELECT e.id
                    FROM {AUTOMATION_EVENTS_TABLE} e
                    WHERE e.kind = ANY($1::text[])
                      AND (
                            (e.state IN ('PENDING', 'RETRY')
                             AND (e.next_attempt_at IS NULL OR e.next_attempt_at <= $2))
                         OR (e.state = 'DELIVERING'
                             AND (e.started_at IS NULL
                                  OR e.started_at <= $2::timestamptz - make_interval(secs => $4::float8)))
                      )
                    ORDER BY e.occurred_at, e.id
                    LIMIT $3
                    FOR UPDATE OF e SKIP LOCKED
                )
                UPDATE {AUTOMATION_EVENTS_TABLE} e
                SET state = 'DELIVERING',
                    started_at = $2,
                    attempts = e.attempts + 1,
                    updated_at = now()
                FROM claimable
                WHERE e.id = claimable.id
                RETURNING e.*
                """,
                list(wanted),
                moment,
                limit,
                float(max(0.0, stale_secs)),
            )
        events = [_automation_event(row) for row in rows]
        events.sort(key=lambda e: (e.occurred_at or moment, e.id))
        return events

    @_timed("record_automation_event")
    async def record_automation_event(
        self,
        event_id: int,
        *,
        state: AutomationEventState | None = None,
        error: str | None = None,
        status_code: int | None = None,
        next_attempt_at: datetime | None = None,
        delivered_at: datetime | None = None,
        payload: dict[str, Any] | None = None,
        target_url: str | None = None,
        result_updated_at: datetime | None = None,
        attempts: int | None = None,
    ) -> AutomationEvent | None:
        """Write what the deliverer learned onto a row. Only the given fields change.

        `error=None` with a `state` clears the last error (a success); the
        payload and target are kept once known; `attempts` is set only when
        given (the claim increments it; a release hands one back).
        """
        row = await self._phase17(
            self._pool.fetchrow(
                f"""
                UPDATE {AUTOMATION_EVENTS_TABLE}
                SET state = COALESCE($2, state),
                    last_error = CASE WHEN $2 IS NULL THEN last_error ELSE $3 END,
                    last_status = COALESCE($4, last_status),
                    next_attempt_at = CASE WHEN $2 IS NULL THEN next_attempt_at ELSE $5 END,
                    delivered_at = COALESCE($6, delivered_at),
                    payload = COALESCE($7::jsonb, payload),
                    target_url = COALESCE($8, target_url),
                    result_updated_at = COALESCE($9, result_updated_at),
                    attempts = COALESCE($10, attempts),
                    updated_at = now()
                WHERE id = $1
                RETURNING *
                """,
                event_id,
                str(state) if state is not None else None,
                error,
                status_code,
                next_attempt_at,
                delivered_at,
                _dumps(payload) if payload is not None else None,
                target_url,
                result_updated_at,
                attempts,
            )
        )
        return _automation_event(row) if row else None

    async def get_automation_event(self, event_id: int) -> AutomationEvent | None:
        """One event by id, or None."""
        row = await self._phase17(
            self._pool.fetchrow(f"SELECT * FROM {AUTOMATION_EVENTS_TABLE} WHERE id = $1", event_id)
        )
        return _automation_event(row) if row else None

    async def find_automation_event(self, event_key: str) -> AutomationEvent | None:
        """One event by its stable key, or None."""
        row = await self._phase17(
            self._pool.fetchrow(
                f"SELECT * FROM {AUTOMATION_EVENTS_TABLE} WHERE event_key = $1", event_key
            )
        )
        return _automation_event(row) if row else None

    async def list_automation_events(
        self,
        *,
        state: AutomationEventState | None = None,
        kind: str | None = None,
        prospect_id: int | None = None,
        limit: int = 50,
    ) -> list[AutomationEvent]:
        """Events, most recently changed first, optionally in one state or of one kind."""
        rows = await self._phase17(
            self._pool.fetch(
                f"""
                SELECT * FROM {AUTOMATION_EVENTS_TABLE}
                WHERE ($1::text IS NULL OR state = $1)
                  AND ($2::text IS NULL OR kind = $2)
                  AND ($3::bigint IS NULL OR prospect_id = $3)
                ORDER BY updated_at DESC, id DESC
                LIMIT $4
                """,
                str(state) if state is not None else None,
                kind,
                prospect_id,
                limit,
            )
        )
        return [_automation_event(row) for row in rows]

    async def automation_event_counts(self) -> dict[str, int] | None:
        """How many events are in each state, or None when the table is missing."""
        try:
            rows = await self._phase17(
                self._pool.fetch(
                    f"SELECT state, count(*) AS n FROM {AUTOMATION_EVENTS_TABLE} GROUP BY state"
                )
            )
        except CampaignStoreError:
            return None
        return {str(row["state"]): int(row["n"]) for row in rows}

    async def retry_automation_events(
        self, *, event_id: int | None = None, all_failed: bool = False
    ) -> int:
        """Reopen failed rows (or one row in any closed state) as `PENDING`, due now.

        The attempt count starts again: a person reopening a row has fixed
        something — the URL, the workflow, the secret — and the row deserves
        the full budget against the new state of the world, not the one try
        that was left.

        Returns:
            How many rows were reopened.
        """
        if event_id is None and not all_failed:
            return 0
        status = await self._phase17(
            self._pool.execute(
                f"""
                UPDATE {AUTOMATION_EVENTS_TABLE}
                SET state = 'PENDING', next_attempt_at = NULL, last_error = NULL,
                    attempts = 0, updated_at = now()
                WHERE ($1::bigint IS NOT NULL AND id = $1 AND state <> 'DELIVERING')
                   OR ($1::bigint IS NULL AND state = 'FAILED')
                """,
                event_id,
            )
        )
        return int(str(status).rsplit(" ", 1)[-1])

    async def _phase17(self, operation: Any) -> Any:
        """Await a statement against a Phase 17 table, naming the fix if it is missing."""
        return await self._optional_table(operation, AUTOMATION_EVENTS_TABLE, phase="Phase 17")

    # --- API idempotency (Phase 17) ---------------------------------------------------

    async def get_api_request(self, scope: str, idempotency_key: str) -> ApiRequestRecord | None:
        """The stored answer to a request with this key in this scope, or None."""
        row = await self._optional_table(
            self._pool.fetchrow(
                f"SELECT * FROM {API_REQUESTS_TABLE} WHERE scope = $1 AND idempotency_key = $2",
                scope,
                idempotency_key,
            ),
            API_REQUESTS_TABLE,
            phase="Phase 17",
        )
        return _api_request(row) if row else None

    async def save_api_request(
        self,
        *,
        scope: str,
        idempotency_key: str,
        fingerprint: str,
        status_code: int,
        response: dict[str, Any],
    ) -> tuple[ApiRequestRecord, bool]:
        """Keep the answer given to a request, unless one is already kept.

        Returns:
            `(record, inserted)`. `inserted` is False when another request with
            the same key got there first — in which case `record` is *its*
            answer, which is what the caller should now return.
        """
        row = await self._optional_table(
            self._pool.fetchrow(
                f"""
                INSERT INTO {API_REQUESTS_TABLE}
                    (scope, idempotency_key, fingerprint, status_code, response)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (scope, idempotency_key) DO NOTHING
                RETURNING *
                """,
                scope,
                idempotency_key,
                fingerprint,
                status_code,
                _dumps(response),
            ),
            API_REQUESTS_TABLE,
            phase="Phase 17",
        )
        if row is not None:
            return _api_request(row), True
        existing = await self.get_api_request(scope, idempotency_key)
        if existing is None:  # pragma: no cover - a delete raced the insert
            raise CampaignStoreError("the idempotency record vanished between insert and read")
        return existing, False

    async def purge_api_requests(self, *, older_than: datetime) -> int:
        """Forget stored answers older than a moment. Returns how many were removed."""
        status = await self._optional_table(
            self._pool.execute(
                f"DELETE FROM {API_REQUESTS_TABLE} WHERE created_at < $1", older_than
            ),
            API_REQUESTS_TABLE,
            phase="Phase 17",
        )
        return int(str(status).rsplit(" ", 1)[-1])

    # --- Coordination across workers (Phase 21) ---------------------------------------

    async def take_pacing_slot(
        self,
        min_interval_secs: float,
        *,
        campaign_id: int | None = None,
        campaign_interval_secs: float = 0.0,
    ) -> tuple[bool, float]:
        """Claim the next placement moment, deployment-wide and per campaign. Phase 21.

        Under one advisory lock: read every scope's `last_placement_at`, and
        if every interval has elapsed, stamp them all `now()`. Two workers
        cannot both find the interval elapsed, because the second reads the
        first's stamp. Nothing is stamped when any scope refuses.

        Returns:
            `(True, 0.0)` when the slot is taken; `(False, wait_secs)` when a
            scope's interval has not elapsed.
        """
        scopes: list[tuple[str, float]] = []
        if min_interval_secs > 0:
            scopes.append((GLOBAL_PACING_KEY, float(min_interval_secs)))
        if campaign_id is not None and campaign_interval_secs > 0:
            scopes.append((campaign_pacing_key(campaign_id), float(campaign_interval_secs)))
        if not scopes:
            return True, 0.0
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", PACING_LOCK_KEY)
            wait = 0.0
            for key, interval in scopes:
                row = await self._optional_table(
                    connection.fetchrow(
                        f"SELECT last_placement_at, now() AS moment FROM {STATE_TABLE} WHERE key = $1", key
                    ),
                    STATE_TABLE,
                    phase="Phase 21",
                )
                if row is not None and row["last_placement_at"] is not None:
                    elapsed = (row["moment"] - row["last_placement_at"]).total_seconds()
                    if elapsed < interval:
                        wait = max(wait, interval - elapsed)
            if wait > 0:
                return False, wait
            for key, _interval in scopes:
                await connection.execute(
                    f"""
                    INSERT INTO {STATE_TABLE} (key, last_placement_at, updated_at)
                    VALUES ($1, now(), now())
                    ON CONFLICT (key) DO UPDATE SET last_placement_at = now(), updated_at = now()
                    """,
                    key,
                )
        return True, 0.0

    async def register_worker(
        self,
        worker_id: str,
        *,
        hostname: str,
        pid: int,
        campaign_ids: list[int] | None = None,
        version: str | None = None,
    ) -> WorkerRecord:
        """Announce a worker: a fresh row, or an old row of the same id started again."""
        row = await self._optional_table(
            self._pool.fetchrow(
                f"""
                INSERT INTO {WORKERS_TABLE}
                    (worker_id, hostname, pid, status, started_at, heartbeat_at, stopped_at, campaign_ids, version)
                VALUES ($1, $2, $3, $4, now(), now(), NULL, $5::jsonb, $6)
                ON CONFLICT (worker_id) DO UPDATE
                    SET hostname = EXCLUDED.hostname, pid = EXCLUDED.pid, status = EXCLUDED.status,
                        started_at = now(), heartbeat_at = now(), stopped_at = NULL,
                        campaign_ids = EXCLUDED.campaign_ids, version = EXCLUDED.version,
                        in_flight = 0, metrics = '{{}}'::jsonb
                RETURNING *
                """,
                worker_id,
                hostname,
                int(pid),
                WORKER_RUNNING,
                _dumps(list(campaign_ids)) if campaign_ids is not None else None,
                version,
            ),
            WORKERS_TABLE,
            phase="Phase 21",
        )
        return _worker(row)

    async def heartbeat_worker(
        self,
        worker_id: str,
        *,
        status: str = WORKER_RUNNING,
        in_flight: int = 0,
        metrics: dict[str, Any] | None = None,
    ) -> WorkerRecord | None:
        """Say the worker is alive, and what it is doing. None when it was never registered."""
        row = await self._optional_table(
            self._pool.fetchrow(
                f"""
                UPDATE {WORKERS_TABLE}
                SET heartbeat_at = now(), status = $2, in_flight = $3, metrics = $4::jsonb
                WHERE worker_id = $1
                RETURNING *
                """,
                worker_id,
                status,
                int(in_flight),
                _dumps(metrics or {}),
            ),
            WORKERS_TABLE,
            phase="Phase 21",
        )
        return _worker(row) if row else None

    async def mark_worker_stopped(self, worker_id: str, *, metrics: dict[str, Any] | None = None) -> bool:
        """The worker has finished. Its row stays, stamped."""
        status = await self._optional_table(
            self._pool.execute(
                f"""
                UPDATE {WORKERS_TABLE}
                SET status = $2, stopped_at = now(), heartbeat_at = now(), in_flight = 0,
                    metrics = COALESCE($3::jsonb, metrics)
                WHERE worker_id = $1
                """,
                worker_id,
                WORKER_STOPPED,
                _dumps(metrics) if metrics is not None else None,
            ),
            WORKERS_TABLE,
            phase="Phase 21",
        )
        return str(status).endswith("1")

    async def list_workers(self, *, include_stopped: bool = True, limit: int = 100) -> list[WorkerRecord]:
        """Every worker row, the most recently heard first."""
        rows = await self._optional_table(
            self._pool.fetch(
                f"""
                SELECT * FROM {WORKERS_TABLE}
                WHERE ($1::boolean OR status <> '{WORKER_STOPPED}')
                ORDER BY heartbeat_at DESC
                LIMIT $2
                """,
                bool(include_stopped),
                max(1, min(int(limit), 1000)),
            ),
            WORKERS_TABLE,
            phase="Phase 21",
        )
        return [_worker(row) for row in rows]

    async def worker_summary(self, *, stale_after_secs: float, limit: int = 100) -> WorkerSummary:
        """The fleet in numbers, judged against the database's clock."""
        workers = await self.list_workers(limit=limit)
        moment = await self._pool.fetchval("SELECT now()")
        counts = {"running": 0, "draining": 0, "stale": 0, "stopped": 0}
        in_flight = 0
        for worker in workers:
            health = worker.health(moment, stale_after_secs)
            counts[health] = counts.get(health, 0) + 1
            if health in ("running", "draining"):
                in_flight += worker.in_flight
        return WorkerSummary(
            running=counts["running"],
            draining=counts["draining"],
            stale=counts["stale"],
            stopped=counts["stopped"],
            in_flight=in_flight,
            workers=tuple(workers),
        )

    async def prune_workers(self, *, older_than_secs: float) -> int:
        """Forget stopped and stale rows nobody has heard from for a long time."""
        status = await self._optional_table(
            self._pool.execute(
                f"DELETE FROM {WORKERS_TABLE} WHERE heartbeat_at < now() - make_interval(secs => $1)",
                float(max(0.0, older_than_secs)),
            ),
            WORKERS_TABLE,
            phase="Phase 21",
        )
        return int(str(status).rsplit(" ", 1)[-1])

    async def set_attempt_worker(self, attempt_id: int, worker_id: str | None) -> bool:
        """Change who follows an attempt. None hands it on to whoever claims it next."""
        status = await self._pool.execute(
            f"UPDATE {ATTEMPTS_TABLE} SET worker_id = $2 WHERE id = $1", attempt_id, worker_id
        )
        return str(status).endswith("1")

    async def claim_abandoned_attempts(
        self, worker_id: str, *, stale_after_secs: float, limit: int = 50
    ) -> list[CallAttempt]:
        """Take over the live calls of workers that are dead, stopped or unknown. Phase 21.

        A call is abandoned when it has a carrier id, is still live, and is
        owned by nobody, by a worker whose heartbeat is older than
        `stale_after_secs`, by one that said it stopped, or by one the table
        has never heard of. Never a call a live worker owns. Under an advisory
        lock, so two live workers cannot both claim the same row at the same
        instant; `SKIP LOCKED` covers the row lock as well.
        """
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", CLAIM_LOCK_KEY)
            rows = await self._optional_table(
                connection.fetch(
                    f"""
                    UPDATE {ATTEMPTS_TABLE} a
                    SET worker_id = $1
                    WHERE a.id IN (
                        SELECT x.id FROM {ATTEMPTS_TABLE} x
                        WHERE x.status IN ({LIVE_STATUS_SQL})
                          AND x.telephony_call_id IS NOT NULL
                          AND (x.worker_id IS NULL OR x.worker_id <> $1)
                          AND NOT EXISTS (
                              SELECT 1 FROM {WORKERS_TABLE} w
                              WHERE w.worker_id = x.worker_id
                                AND w.status <> '{WORKER_STOPPED}'
                                AND w.heartbeat_at > now() - make_interval(secs => $2)
                          )
                        ORDER BY x.id
                        LIMIT $3
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING a.*
                    """,
                    worker_id,
                    float(max(0.0, stale_after_secs)),
                    max(1, int(limit)),
                ),
                WORKERS_TABLE,
                phase="Phase 21",
            )
        return [_attempt(row) for row in rows]

    async def release_abandoned_reservations(self, *, stale_after_secs: float, limit: int = 50) -> int:
        """Hand back reservations a dead worker took and never dialled. Phase 21.

        The never-placed shape only (`PENDING`, no call id, no placement
        started): the reservation is undone as Phase 13's `unreserve_attempt`
        undoes it, so the prospect goes back to the queue for a live worker.
        A placement that *started* is left for recovery, which asks the
        carrier whether anything rang.
        """
        rows = await self._optional_table(
            self._pool.fetch(
                f"""
                SELECT x.id FROM {ATTEMPTS_TABLE} x
                WHERE x.status = 'PENDING'
                  AND x.telephony_call_id IS NULL
                  AND x.placement_started_at IS NULL
                  AND x.worker_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM {WORKERS_TABLE} w
                      WHERE w.worker_id = x.worker_id
                        AND w.status <> '{WORKER_STOPPED}'
                        AND w.heartbeat_at > now() - make_interval(secs => $1)
                  )
                ORDER BY x.id
                LIMIT $2
                """,
                float(max(0.0, stale_after_secs)),
                max(1, int(limit)),
            ),
            WORKERS_TABLE,
            phase="Phase 21",
        )
        released = 0
        for row in rows:
            if await self.unreserve_attempt(int(row["id"]), next_attempt_at=datetime.now(UTC)):
                released += 1
        return released

    async def queue_depth(self, *, max_attempts: int = 3) -> QueueDepth:
        """How much calling is waiting, deployment-wide. Phase 21's metric.

        `due_now` applies the queue's own eligibility (the campaign ACTIVE,
        the membership pending and due, the prospect dialable and not listed,
        nobody live on their number); `max_attempts` is the environment's
        ceiling, a campaign's own may differ. Callbacks and the list are
        optional tables and read as zero when absent.
        """
        dnc = await self._dnc_clause()
        rows = await self._pool.fetch(
            f"""
            SELECT c.id, c.name,
                   count(*) FILTER (
                       WHERE m.status = 'PENDING'
                         AND p.status <> 'DO_NOT_CALL'
                         AND p.phone_normalized IS NOT NULL
                         {dnc}
                         AND m.attempt_count < $1
                         AND (m.next_attempt_at IS NULL OR m.next_attempt_at <= now())
                         AND NOT EXISTS (
                             SELECT 1 FROM {ATTEMPTS_TABLE} a
                             WHERE a.prospect_id = m.prospect_id AND a.status IN ({LIVE_STATUS_SQL}))
                   ) AS due_now,
                   count(*) FILTER (WHERE m.status = 'PENDING' AND m.next_attempt_at > now()) AS scheduled,
                   count(*) FILTER (WHERE m.status = 'IN_PROGRESS') AS in_progress
            FROM {CAMPAIGNS_TABLE} c
            JOIN {MEMBERSHIPS_TABLE} m ON m.campaign_id = c.id
            JOIN {PROSPECTS_TABLE} p ON p.id = m.prospect_id
            WHERE c.status = 'ACTIVE'
            GROUP BY c.id, c.name
            ORDER BY c.id
            """,
            int(max_attempts),
        )
        attempts = await self._pool.fetchrow(
            f"""
            SELECT count(*) FILTER (WHERE status IN ({LIVE_STATUS_SQL})) AS live,
                   count(*) FILTER (WHERE status = 'PENDING' AND telephony_call_id IS NULL) AS reserved
            FROM {ATTEMPTS_TABLE}
            """
        )
        callbacks_due = 0
        try:
            counts = await self.callback_counts()
            callbacks_due = int(counts["due"]) if counts else 0
        except CampaignStoreError:
            callbacks_due = 0
        per_campaign = tuple(
            {
                "campaign_id": int(row["id"]),
                "name": row["name"],
                "due_now": int(row["due_now"]),
                "scheduled": int(row["scheduled"]),
                "in_progress": int(row["in_progress"]),
            }
            for row in rows
        )
        active = await self._pool.fetchval(f"SELECT count(*) FROM {CAMPAIGNS_TABLE} WHERE status = 'ACTIVE'")
        return QueueDepth(
            due_now=sum(r["due_now"] for r in per_campaign),
            scheduled=sum(r["scheduled"] for r in per_campaign),
            callbacks_due=callbacks_due,
            reserved=int(attempts["reserved"]),
            live=int(attempts["live"]),
            active_campaigns=int(active),
            per_campaign=per_campaign,
        )

    # --- The audit log (Phase 18) -----------------------------------------------------

    @_timed("record_audit")
    async def record_audit(
        self,
        *,
        action: str,
        actor: str,
        role: str,
        via: str,
        outcome: str = "ok",
        target_kind: str | None = None,
        target_id: str | None = None,
        ip: str | None = None,
        detail: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> AuditEntry:
        """Append one audit row. The caller (`security/audit.py`) has already scrubbed it."""
        row = await self._optional_table(
            self._pool.fetchrow(
                f"""
                INSERT INTO {AUDIT_TABLE}
                    (action, actor, role, via, outcome, target_kind, target_id, ip, detail, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, COALESCE($10, now()))
                RETURNING *
                """,
                action,
                actor,
                role,
                via,
                outcome,
                target_kind,
                target_id,
                ip,
                _dumps(detail or {}),
                created_at,
            ),
            AUDIT_TABLE,
            phase="Phase 18",
        )
        return _audit_entry(row)

    async def list_audit(
        self,
        *,
        action: str | None = None,
        actor: str | None = None,
        since: datetime | None = None,
        before_id: int | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Audit rows, newest first. `action` matches a prefix (`auth.` finds every auth event)."""
        clauses: list[str] = []
        params: list[Any] = []
        if action:
            params.append(action)
            clauses.append(f"action LIKE ${len(params)} || '%'")
        if actor:
            params.append(actor)
            clauses.append(f"actor = ${len(params)}")
        if since is not None:
            params.append(since)
            clauses.append(f"created_at >= ${len(params)}")
        if before_id is not None:
            params.append(before_id)
            clauses.append(f"id < ${len(params)}")
        params.append(max(1, min(int(limit), 1000)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._optional_table(
            self._pool.fetch(
                f"SELECT * FROM {AUDIT_TABLE} {where} ORDER BY id DESC LIMIT ${len(params)}",
                *params,
            ),
            AUDIT_TABLE,
            phase="Phase 18",
        )
        return [_audit_entry(row) for row in rows]

    async def audit_counts(self, *, since: datetime | None = None) -> dict[str, int]:
        """How many rows per action, for `campaign.py audit` and `/api/v1/audit`."""
        rows = await self._optional_table(
            self._pool.fetch(
                f"SELECT action, count(*) AS n FROM {AUDIT_TABLE} "
                f"WHERE ($1::timestamptz IS NULL OR created_at >= $1) GROUP BY action ORDER BY action",
                since,
            ),
            AUDIT_TABLE,
            phase="Phase 18",
        )
        return {row["action"]: int(row["n"]) for row in rows}

    # --- The do-not-call list (Phase 19) ---------------------------------------------

    async def _dnc_clause(self, connection: Any = None) -> str:
        """`AND NOT <listed>` for the queue's SQL, or empty when the table is absent.

        Learned once per store. A database that predates Phase 19 keeps
        dialling on the prospect's status alone, and is told, once, to run
        `campaign.py init`.
        """
        if self._dnc_table_present is None:
            executor = connection if connection is not None else self._pool
            present = await executor.fetchval("SELECT to_regclass($1) IS NOT NULL", DNC_TABLE)
            self._dnc_table_present = bool(present)
            if not present:
                logger.warning(
                    f"DNC | the {DNC_TABLE} table does not exist, so the do-not-call *list* is not "
                    f"enforced in the queue (the prospect's status still is). Run:  uv run campaign.py init"
                )
        return f"AND NOT {_DNC_EXISTS_SQL}" if self._dnc_table_present else ""

    async def find_dnc(self, phone_normalized: str) -> DncEntry | None:
        """The active list entry for a number, or None. An expired entry is None too."""
        if not phone_normalized:
            return None
        row = await self._optional_table(
            self._pool.fetchrow(
                f"""
                SELECT * FROM {DNC_TABLE}
                WHERE phone_normalized = $1 AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY id DESC LIMIT 1
                """,
                phone_normalized,
            ),
            DNC_TABLE,
            phase="Phase 19",
        )
        return _dnc_entry(row) if row else None

    async def add_dnc(
        self,
        phone_normalized: str,
        *,
        source: DncSource | str = DncSource.MANUAL,
        reason: str | None = None,
        prospect_id: int | None = None,
        campaign_id: int | None = None,
        call_attempt_id: int | None = None,
        created_by: str | None = None,
        note: str | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[DncEntry, bool]:
        """Put a number on the list, or find it already there.

        Returns:
            `(entry, inserted)`. `inserted` is False when an active entry
            already existed — in which case `entry` is *that* one, untouched:
            the first record of a request is the one that stands.
        """
        if not phone_normalized:
            raise CampaignStoreError("a do-not-call entry needs a normalised number")
        kind = parse_source(str(source)) if not isinstance(source, DncSource) else source
        row = await self._optional_table(
            self._pool.fetchrow(
                f"""
                INSERT INTO {DNC_TABLE}
                    (phone_normalized, source, reason, prospect_id, campaign_id, call_attempt_id,
                     created_by, note, expires_at)
                SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9
                WHERE NOT EXISTS (
                    SELECT 1 FROM {DNC_TABLE} WHERE phone_normalized = $1 AND revoked_at IS NULL
                )
                RETURNING *
                """,
                phone_normalized,
                kind.value,
                reason,
                prospect_id,
                campaign_id,
                call_attempt_id,
                created_by,
                note,
                expires_at,
            ),
            DNC_TABLE,
            phase="Phase 19",
        )
        if row is not None:
            return _dnc_entry(row), True
        existing = await self.find_dnc(phone_normalized)
        if existing is None:
            # An active-but-expired row blocked the insert. Revoke it as expired and insert again.
            await self._pool.execute(
                f"UPDATE {DNC_TABLE} SET revoked_at = now(), revoked_by = 'system', revoke_reason = 'expired' "
                f"WHERE phone_normalized = $1 AND revoked_at IS NULL",
                phone_normalized,
            )
            return await self.add_dnc(
                phone_normalized, source=kind, reason=reason, prospect_id=prospect_id, campaign_id=campaign_id,
                call_attempt_id=call_attempt_id, created_by=created_by, note=note, expires_at=expires_at,
            )
        return existing, False

    async def revoke_dnc(self, phone_normalized: str, *, revoked_by: str, reason: str | None = None) -> DncEntry | None:
        """Take a number off the list. The row stays, stamped with who and why.

        Returns:
            The revoked entry, or None when the number was not on the list.
        """
        row = await self._optional_table(
            self._pool.fetchrow(
                f"""
                UPDATE {DNC_TABLE}
                SET revoked_at = now(), revoked_by = $2, revoke_reason = $3
                WHERE phone_normalized = $1 AND revoked_at IS NULL
                RETURNING *
                """,
                phone_normalized,
                revoked_by,
                reason,
            ),
            DNC_TABLE,
            phase="Phase 19",
        )
        return _dnc_entry(row) if row else None

    async def list_dnc(
        self,
        *,
        phone_normalized: str | None = None,
        source: DncSource | str | None = None,
        include_revoked: bool = False,
        since: datetime | None = None,
        before_id: int | None = None,
        limit: int = 100,
    ) -> list[DncEntry]:
        """List entries, newest first."""
        clauses: list[str] = []
        params: list[Any] = []
        if phone_normalized:
            params.append(phone_normalized)
            clauses.append(f"phone_normalized = ${len(params)}")
        if source:
            params.append(str(getattr(source, "value", source)))
            clauses.append(f"source = ${len(params)}")
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        if since is not None:
            params.append(since)
            clauses.append(f"created_at >= ${len(params)}")
        if before_id is not None:
            params.append(before_id)
            clauses.append(f"id < ${len(params)}")
        params.append(max(1, min(int(limit), 1000)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._optional_table(
            self._pool.fetch(f"SELECT * FROM {DNC_TABLE} {where} ORDER BY id DESC LIMIT ${len(params)}", *params),
            DNC_TABLE,
            phase="Phase 19",
        )
        return [_dnc_entry(row) for row in rows]

    async def dnc_counts(self) -> dict[str, int]:
        """Active entries per source, plus `active` and `revoked` totals."""
        rows = await self._optional_table(
            self._pool.fetch(
                f"SELECT source, count(*) FILTER (WHERE revoked_at IS NULL) AS active, "
                f"count(*) FILTER (WHERE revoked_at IS NOT NULL) AS revoked FROM {DNC_TABLE} GROUP BY source ORDER BY source"
            ),
            DNC_TABLE,
            phase="Phase 19",
        )
        counts: dict[str, int] = {"active": 0, "revoked": 0}
        for row in rows:
            counts[str(row["source"])] = int(row["active"])
            counts["active"] += int(row["active"])
            counts["revoked"] += int(row["revoked"])
        return counts

    async def prospects_with_number(self, phone_normalized: str) -> list[Prospect]:
        """Every prospect row carrying a normalised number (a re-import can make more than one)."""
        if not phone_normalized:
            return []
        rows = await self._pool.fetch(
            f"SELECT * FROM {PROSPECTS_TABLE} WHERE phone_normalized = $1 ORDER BY id", phone_normalized
        )
        return [_prospect(row) for row in rows]

    async def apply_dnc_list(self, prospect_ids: list[int] | None = None) -> int:
        """Mark every prospect whose number is on the active list `DO_NOT_CALL`. Phase 19.

        Run after an import or a create, and by `campaign.py dnc-apply`, so a
        number that was listed *before* its prospect row existed is blocked
        the moment the row appears. Closes the open memberships and pending
        callbacks of everybody it marks, as `set_prospect_status` does.

        Args:
            prospect_ids: Only these rows; None means every prospect.

        Returns:
            How many prospects were newly marked.
        """
        if self._dnc_table_present is False:
            return 0
        rows = await self._optional_table(
            self._pool.fetch(
                f"""
                SELECT p.id FROM {PROSPECTS_TABLE} p
                WHERE ($1::bigint[] IS NULL OR p.id = ANY($1))
                  AND p.status <> 'DO_NOT_CALL'
                  AND p.phone_normalized IS NOT NULL
                  AND {_DNC_EXISTS_SQL}
                """,
                prospect_ids,
            ),
            DNC_TABLE,
            phase="Phase 19",
        )
        marked = 0
        for row in rows:
            if await self.set_prospect_status(int(row["id"]), ProspectStatus.DO_NOT_CALL):
                marked += 1
        return marked

    async def _phase7(self, operation: Any, table: str) -> Any:
        """Await a statement against a Phase 7 table, naming the fix if it is missing."""
        return await self._optional_table(operation, table, phase="Phase 7")

    async def _optional_table(
        self, operation: Any, table: str, *, phase: str, foreign_key: str | None = None
    ) -> Any:
        """Await a statement against a table a re-init adds, naming the fix if it is missing.

        Args:
            operation: The awaitable statement.
            table: Which table, for the message.
            phase: Which phase added it, for the message.
            foreign_key: What a foreign-key violation means here, when the
                statement has one worth translating.
        """
        try:
            return await operation
        except asyncpg.UndefinedTableError as exc:
            raise CampaignStoreError(_TABLE_MISSING.format(table=table, phase=phase)) from exc
        except asyncpg.ForeignKeyViolationError as exc:
            if foreign_key is None:
                raise
            raise CampaignStoreError(foreign_key) from exc

    async def _verify(self) -> None:
        """Check the tables exist.

        Raises:
            CampaignStoreError: They do not, naming the command that makes them.
        """
        missing = await self._pool.fetchval(
            """
            SELECT count(*) FROM (VALUES ($1), ($2), ($3), ($4)) AS wanted(name)
            WHERE to_regclass(wanted.name) IS NULL
            """,
            PROSPECTS_TABLE,
            CAMPAIGNS_TABLE,
            MEMBERSHIPS_TABLE,
            ATTEMPTS_TABLE,
        )
        if missing:
            raise CampaignStoreError(
                "The campaign tables do not exist yet.\n  Run:  uv run campaign.py init"
            )


    # --- Dashboard users (Phase 27) ----------------------------------------

    async def add_dashboard_user(
        self, *, name: str, email: str, role: str, password_hash: str, status: str = "active"
    ) -> DashboardUser:
        """Insert one sign-up. The caller has validated the fields, hashed the password and decided the status.

        Raises:
            DuplicateUserError: The name or the email is already taken
                (case-insensitively), naming which.
            CampaignStoreError: The table is missing (`campaign.py init`).
        """
        try:
            row = await self._optional_table(
                self._pool.fetchrow(
                    f"""
                    INSERT INTO {DASHBOARD_USERS_TABLE} (name, email, role, password_hash, status)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING *
                    """,
                    name,
                    email,
                    role,
                    password_hash,
                    status,
                ),
                DASHBOARD_USERS_TABLE,
                phase="Phase 27",
            )
        except asyncpg.UniqueViolationError as exc:
            field = "email" if "email" in (exc.constraint_name or "") else "name"
            raise DuplicateUserError(field, email if field == "email" else name) from exc
        return _dashboard_user(row)

    async def get_dashboard_user(self, name_or_email: str) -> DashboardUser | None:
        """The sign-up whose name or email this is (case-insensitively), or None.

        Raises:
            CampaignStoreError: The table is missing (`campaign.py init`).
        """
        row = await self._optional_table(
            self._pool.fetchrow(
                f"SELECT * FROM {DASHBOARD_USERS_TABLE} WHERE lower(name) = lower($1) OR lower(email) = lower($1)",
                name_or_email.strip(),
            ),
            DASHBOARD_USERS_TABLE,
            phase="Phase 27",
        )
        return _dashboard_user(row) if row is not None else None

    async def list_dashboard_users(self, *, status: str | None = None) -> list[DashboardUser]:
        """Sign-ups, oldest first; `status` narrows to one status (the pending requests). Never a hash to a page: the route drops it."""
        rows = await self._optional_table(
            self._pool.fetch(
                f"SELECT * FROM {DASHBOARD_USERS_TABLE} WHERE $1::text IS NULL OR status = $1 ORDER BY id",
                status,
            ),
            DASHBOARD_USERS_TABLE,
            phase="Phase 27",
        )
        return [_dashboard_user(row) for row in rows]

    async def decide_dashboard_user(self, user_id: int, *, approve: bool, decided_by: str) -> DashboardUser | None:
        """Approve a pending sign-up (it becomes active, as the role it asked for) or reject it (the row is deleted).

        Returns the row as it was decided, or None when there is no pending
        sign-up with this id — decided already, or never there.
        """
        if approve:
            statement = (
                f"UPDATE {DASHBOARD_USERS_TABLE} SET status = 'active', decided_by = $2, decided_at = now() "
                f"WHERE id = $1 AND status = 'pending' RETURNING *"
            )
        else:
            statement = f"DELETE FROM {DASHBOARD_USERS_TABLE} WHERE id = $1 AND status = 'pending' RETURNING *"
        row = await self._optional_table(
            self._pool.fetchrow(statement, user_id, *([decided_by] if approve else [])),
            DASHBOARD_USERS_TABLE,
            phase="Phase 27",
        )
        return _dashboard_user(row) if row is not None else None


# --- Row mapping ------------------------------------------------------------


@dataclass(frozen=True)
class DashboardUser:
    """One row of `dashboard_users`: a sign-up. Never a password. Phase 27."""

    id: int
    name: str
    email: str
    role: str
    password_hash: str
    created_at: datetime
    #: `active`, or `pending` for an operator / admin request awaiting an admin.
    status: str = "active"
    decided_by: str | None = None
    decided_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        """The row for a page: never the hash."""
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }


def _dashboard_user(row: Any) -> DashboardUser:
    """Build a `DashboardUser` from a row."""
    keys = set(row.keys())
    return DashboardUser(
        id=int(row["id"]),
        name=str(row["name"]),
        email=str(row["email"]),
        role=str(row["role"]),
        password_hash=str(row["password_hash"]),
        created_at=row["created_at"],
        status=str(row["status"]) if "status" in keys and row["status"] else "active",
        decided_by=row["decided_by"] if "decided_by" in keys else None,
        decided_at=row["decided_at"] if "decided_at" in keys else None,
    )



def _json(value: Any) -> dict[str, Any]:
    """asyncpg hands jsonb back as text unless a codec is registered."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return {}


def _prospect(row: asyncpg.Record) -> Prospect:
    """Build a `Prospect` from a `prospects` row."""
    return Prospect(
        id=row["id"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        phone=row["phone"],
        phone_normalized=row["phone_normalized"],
        email=row["email"],
        company=row["company"],
        job_title=row["job_title"],
        industry=row["industry"],
        location=row["location"],
        website=row["website"],
        custom_data=_json(row["custom_data"]),
        status=ProspectStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _campaign(row: asyncpg.Record) -> Campaign:
    """Build a `Campaign` from a `campaigns` row."""
    return Campaign(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        status=CampaignStatus(row["status"]),
        configuration=_json(row["configuration"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        paused_at=row["paused_at"],
        completed_at=row["completed_at"],
    )


def _membership(row: asyncpg.Record, *, prefix: str = "") -> CampaignProspect:
    """Build a `CampaignProspect`, optionally from aliased columns of a join."""
    return CampaignProspect(
        id=row[f"{prefix}id"],
        campaign_id=row[f"{prefix}campaign_id"],
        prospect_id=row[f"{prefix}prospect_id"],
        status=MembershipStatus(row[f"{prefix}status"]),
        attempt_count=row[f"{prefix}attempt_count"],
        last_attempt_at=row[f"{prefix}last_attempt_at"],
        next_attempt_at=row[f"{prefix}next_attempt_at"],
        created_at=row[f"{prefix}created_at"],
        updated_at=row[f"{prefix}updated_at"],
    )


def _attempt(row: asyncpg.Record) -> CallAttempt:
    """Build a `CallAttempt` from a `call_attempts` row."""
    return CallAttempt(
        id=row["id"],
        prospect_id=row["prospect_id"],
        campaign_id=row["campaign_id"],
        campaign_prospect_id=row["campaign_prospect_id"],
        attempt_number=row["attempt_number"],
        status=CallAttemptStatus(row["status"]),
        telephony_call_id=row["telephony_call_id"],
        telephony_provider=row["telephony_provider"],
        started_at=row["started_at"],
        connected_at=row["connected_at"],
        ended_at=row["ended_at"],
        duration_seconds=row["duration_seconds"],
        failure_reason=row["failure_reason"],
        # Tolerates a schema that predates Phase 6: a row with no such column
        # reads as no conversation, which is exactly what it is.
        conversation_data=_optional_json(row, "conversation_data"),
        # Likewise for the Phase 9 columns.
        idempotency_key=_optional(row, "idempotency_key"),
        placement_started_at=_optional(row, "placement_started_at"),
        # And Phase 21's owner.
        worker_id=_optional(row, "worker_id"),
        # And Phase 22's correlation id.
        trace_id=_optional(row, "trace_id"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _delivery(row: asyncpg.Record) -> WebhookDelivery:
    """Build a `WebhookDelivery` from a `telephony_webhook_events` row. Phase 14."""
    return WebhookDelivery(
        id=row["id"],
        provider=row["provider"],
        call_id=row["call_id"],
        event_key=row["event_key"],
        kind=row["kind"],
        status=row["status"],
        raw_status=row["raw_status"],
        sequence=row["sequence"],
        carrier_timestamp=row["carrier_timestamp"],
        answered_by=row["answered_by"],
        duration_seconds=row["duration_seconds"],
        outcome=row["outcome"],
        attempt_id=row["attempt_id"],
        received_at=row["received_at"],
        payload=_json(row["payload"]),
    )


def _transfer(row: asyncpg.Record) -> CallTransfer:
    """Build a `CallTransfer` from a `call_transfers` row. Phase 16."""
    return CallTransfer(
        id=row["id"],
        telephony_call_id=row["telephony_call_id"],
        provider=row["provider"],
        to_number=row["to_number"],
        status=TransferStatus(row["status"]),
        call_attempt_id=row["call_attempt_id"],
        prospect_id=row["prospect_id"],
        reason=row["reason"],
        dial_call_id=row["dial_call_id"],
        duration_seconds=row["duration_seconds"],
        error=row["error"],
        requested_at=row["requested_at"],
        completed_at=row["completed_at"],
        updated_at=row["updated_at"],
    )


def _crm_sync(row: asyncpg.Record) -> CrmSyncRecord:
    """Build a `CrmSyncRecord` from a `crm_sync` row. Phase 15."""
    return CrmSyncRecord(
        id=row["id"],
        call_result_id=row["call_result_id"],
        call_attempt_id=row["call_attempt_id"],
        prospect_id=row["prospect_id"],
        provider=row["provider"],
        state=CrmSyncState(row["state"]),
        sync_key=row["sync_key"],
        external_contact_id=row["external_contact_id"],
        external_activity_id=row["external_activity_id"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        next_attempt_at=row["next_attempt_at"],
        started_at=row["started_at"],
        synced_at=row["synced_at"],
        result_updated_at=row["result_updated_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _automation_event(row: asyncpg.Record) -> AutomationEvent:
    """Build an `AutomationEvent` from an `automation_events` row. Phase 17."""
    return AutomationEvent(
        id=row["id"],
        event_key=row["event_key"],
        kind=row["kind"],
        state=AutomationEventState(row["state"]),
        call_result_id=row["call_result_id"],
        call_attempt_id=row["call_attempt_id"],
        prospect_id=row["prospect_id"],
        campaign_id=row["campaign_id"],
        meeting_id=row["meeting_id"],
        callback_id=row["callback_id"],
        result_updated_at=row["result_updated_at"],
        occurred_at=row["occurred_at"],
        payload=_json(row["payload"]) if row["payload"] is not None else None,
        target_url=row["target_url"],
        attempts=int(row["attempts"]),
        last_status=row["last_status"],
        last_error=row["last_error"],
        next_attempt_at=row["next_attempt_at"],
        started_at=row["started_at"],
        delivered_at=row["delivered_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _worker(row: asyncpg.Record) -> WorkerRecord:
    """Build a `WorkerRecord` from a `scheduler_workers` row. Phase 21."""
    ids = _json_list(row["campaign_ids"]) if row["campaign_ids"] is not None else None
    return WorkerRecord(
        worker_id=row["worker_id"],
        hostname=row["hostname"],
        pid=int(row["pid"]),
        status=row["status"],
        started_at=row["started_at"],
        heartbeat_at=row["heartbeat_at"],
        stopped_at=row["stopped_at"],
        campaign_ids=tuple(int(i) for i in ids) if ids is not None else None,
        in_flight=int(row["in_flight"] or 0),
        metrics=_json(row["metrics"]),
        version=row["version"],
    )


def _dnc_entry(row: asyncpg.Record) -> DncEntry:
    """Build a `DncEntry` from a `dnc_numbers` row. Phase 19."""
    return DncEntry(
        id=row["id"],
        phone_normalized=row["phone_normalized"],
        source=parse_source(row["source"]),
        reason=row["reason"],
        prospect_id=row["prospect_id"],
        campaign_id=row["campaign_id"],
        call_attempt_id=row["call_attempt_id"],
        created_by=row["created_by"],
        note=row["note"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        revoked_by=row["revoked_by"],
        revoke_reason=row["revoke_reason"],
    )


def _audit_entry(row: asyncpg.Record) -> AuditEntry:
    """Build an `AuditEntry` from an `audit_log` row. Phase 18."""
    return AuditEntry(
        id=row["id"],
        created_at=row["created_at"],
        action=row["action"],
        actor=row["actor"],
        role=row["role"],
        via=row["via"],
        outcome=row["outcome"],
        target_kind=row["target_kind"],
        target_id=row["target_id"],
        ip=row["ip"],
        detail=_json(row["detail"]),
    )


def _api_request(row: asyncpg.Record) -> ApiRequestRecord:
    """Build an `ApiRequestRecord` from an `api_requests` row. Phase 17."""
    return ApiRequestRecord(
        id=row["id"],
        scope=row["scope"],
        idempotency_key=row["idempotency_key"],
        fingerprint=row["fingerprint"],
        status_code=int(row["status_code"]),
        response=_json(row["response"]),
        created_at=row["created_at"],
    )


def _optional(row: asyncpg.Record, name: str) -> Any:
    """Read a column that may not exist on this database yet.

    `SELECT *` on an older schema simply returns fewer columns, and indexing a
    missing one raises. One line here means a database nobody has re-initialised
    still reads, which is the same courtesy `_optional_json` extends to
    `conversation_data`.
    """
    return row[name] if name in row.keys() else None


def _callback(row: asyncpg.Record) -> ScheduledCallback:
    """Build a `ScheduledCallback` from a `callbacks` row."""
    return ScheduledCallback(
        id=row["id"],
        prospect_id=row["prospect_id"],
        scheduled_for=row["scheduled_for"],
        status=CallbackStatus(row["status"]),
        campaign_id=row["campaign_id"],
        call_attempt_id=row["call_attempt_id"],
        campaign_prospect_id=row["campaign_prospect_id"],
        note=row["note"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _meeting(row: asyncpg.Record) -> Meeting:
    """Build a `Meeting` from a `meetings` row."""
    return Meeting(
        id=row["id"],
        prospect_id=row["prospect_id"],
        start_at=row["start_at"],
        end_at=row["end_at"],
        provider=row["provider"],
        status=MeetingStatus(row["status"]),
        reference=row["reference"],
        timezone=row["timezone"],
        campaign_id=row["campaign_id"],
        call_attempt_id=row["call_attempt_id"],
        attendee_name=row["attendee_name"],
        attendee_email=row["attendee_email"],
        notes=row["notes"],
        created_at=row["created_at"],
    )


def _call_result(row: asyncpg.Record) -> CallResult:
    """Build a `CallResult` from a `call_results` row.

    Enum columns are read strictly: a value this code does not know would mean
    a row written by a newer version, and raising is better than silently
    reading it as UNKNOWN — which is the one thing a result must never be by
    accident.
    """
    summary = _json(row["summary"])
    return CallResult(
        id=row["id"],
        call_attempt_id=row["call_attempt_id"],
        prospect_id=row["prospect_id"],
        campaign_id=row["campaign_id"],
        source=ResultSource(row["source"]),
        schema_version=row["schema_version"],
        call_status=CallAttemptStatus(row["call_status"]),
        disposition=Disposition(row["disposition"]),
        duration_seconds=row["duration_seconds"],
        failure_reason=row["failure_reason"],
        qualification_status=QualificationStatus(row["qualification_status"]),
        interest_level=InterestLevel(row["interest_level"]),
        buying_timeline=BuyingTimeline(row["buying_timeline"]),
        decision_role=DecisionRole(row["decision_role"]),
        next_action=NextAction(row["next_action"]),
        meeting_status=MeetingOutcome(row["meeting_status"]),
        meeting_start=row["meeting_start"],
        meeting_reference=row["meeting_reference"],
        meeting_when=row["meeting_when"],
        callback_status=CallbackOutcome(row["callback_status"]),
        callback_scheduled_for=row["callback_scheduled_for"],
        callback_when=row["callback_when"],
        pain_points=tuple(_json_list(row["pain_points"])),
        objections=tuple(_json_list(row["objections"])),
        questions=tuple(_json_list(row["questions"])),
        existing_provider=row["existing_provider"],
        current_process=row["current_process"],
        impact=row["impact"],
        desired_outcome=row["desired_outcome"],
        notes=tuple(_json_list(row["notes"])),
        human_requested=row["human_requested"],
        transferred=row["transferred"],
        agent_ended_call=row["agent_ended_call"],
        caller_turns=row["caller_turns"],
        agent_turns=row["agent_turns"],
        final_state=row["final_state"],
        timezone=row["timezone"],
        summary=CallSummary(
            what_happened=str(summary.get("what_happened", "")),
            prospect_needs=str(summary.get("prospect_needs", "")),
            objections=str(summary.get("objections", "")),
            interest=str(summary.get("interest", "")),
            qualification=str(summary.get("qualification", "")),
            next_step=str(summary.get("next_step", "")),
        ),
        transcript=tuple(_json_list(row["transcript"])),
        tool_actions=tuple(_json_list(row["tool_actions"])),
        issues=tuple(_json_list(row["issues"])),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _json_list(value: Any) -> list[Any]:
    """asyncpg hands jsonb back as text unless a codec is registered; lists too."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _dumps(value: Any) -> str:
    """JSON for a jsonb parameter. `default=str` covers a datetime that slipped in."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _optional_json(row: asyncpg.Record, name: str) -> dict[str, Any] | None:
    """Read a JSONB column that may not exist on this database yet.

    `SELECT *` means an older schema simply returns fewer columns, and indexing
    a missing one raises. Checking is one line here and saves a re-initialisation
    being a hard requirement for a database somebody is only reading from.
    """
    if name not in row.keys():
        return None
    value = row[name]
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def retry_at(minutes: float, *, now: datetime | None = None) -> datetime:
    """When a failed attempt may be retried."""
    return (now or datetime.now(UTC)) + timedelta(minutes=minutes)
