"""What several workers share, and how they share it. Phase 21.

Until this phase the scheduler was one process, and three of its rules lived
in that process: pacing (a monotonic clock), the follow set (a dict of the
calls *this* worker placed), and "adopt every live attempt at start" (true
only when there is nobody else). The reservation itself — the row lock, the
`SKIP LOCKED`, the unique idempotency key, the live-attempt exclusion — was
already in PostgreSQL and already safe across processes; Phase 9 built it
that way and Phase 13 leaned on it. Phase 21 moves the other three into the
same database, and adds the two things a fleet needs that one process never
did: a heartbeat, so a worker that died can be told from one that is busy;
and ownership on the attempt row, so the calls a dead worker was following
are picked up by a live one and nothing is followed twice.

**The coordination mechanism is PostgreSQL**, and nothing else, on purpose.
Every process here already holds a pool to it; Redis or a broker would be a
second thing to run, a second thing to fail, and a second place for the
truth to be. What is needed — serialising the reservation across workers,
one shared "last placement" moment, a table of who is alive — PostgreSQL
does with an advisory lock, a row, and a table:

* **`pg_advisory_xact_lock`** around the reservation makes the concurrency
  count exact across workers: two transactions cannot both count "one
  below the limit" and both reserve, because the second waits for the first
  to commit and then counts its row. Phase 11 counted inside the transaction;
  under READ COMMITTED that is not enough on its own, and now it is.
* **`scheduler_state`** holds one `last_placement_at` per pacing scope (the
  deployment, and each campaign with its own interval). A placement *takes
  the slot* under the lock, so two workers cannot both find the interval
  elapsed and both dial.
* **`scheduler_workers`** is the heartbeat table. A worker registers at
  start, beats every `WORKER_HEARTBEAT_SECS`, says `draining` when asked to
  stop and `stopped` when it has, and is *stale* when its beat is older
  than `WORKER_STALE_SECS`. Stale is the definition of dead.
* **`call_attempts.worker_id`** says who is following a call. A live worker
  claims the attempts of a stale or stopped one (and of no one); a worker
  never touches an attempt that a live worker owns.

This module is the vocabulary: the identity, the record, the queue depth,
and the classifier that decides whether a failed placement deserves another
try. The SQL is in `store.py`; the loop is in `worker.py`.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: The advisory lock every reservation takes. One key for the deployment:
#: the count it protects is deployment-wide. Any 64-bit constant works; this
#: one is the CRC-ish of "aiva:reserve", chosen once and never changed.
RESERVE_LOCK_KEY = 7_411_002_101
#: The lock a pacing slot is taken under.
PACING_LOCK_KEY = 7_411_002_102
#: The lock the abandoned-work pass takes, so two live workers do not both
#: claim the same dead worker's attempts at the same instant.
CLAIM_LOCK_KEY = 7_411_002_103

#: The `scheduler_state` row that holds the deployment-wide pacing moment.
GLOBAL_PACING_KEY = "pacing:global"

DEFAULT_HEARTBEAT_SECS = 10.0
DEFAULT_STALE_SECS = 60.0
DEFAULT_ADOPT_SECS = 30.0

WORKER_RUNNING = "running"
WORKER_DRAINING = "draining"
WORKER_STOPPED = "stopped"

_ID_CHARS = re.compile(r"[^A-Za-z0-9._\-]+")


def campaign_pacing_key(campaign_id: int) -> str:
    """The `scheduler_state` row for one campaign's pacing."""
    return f"pacing:campaign:{int(campaign_id)}"


def make_worker_id(explicit: str | None = None) -> str:
    """A name for this worker process: `WORKER_ID`, or `<host>-<pid>-<6 hex>`.

    Unique per process on purpose, even for a configured name: two
    processes started with the same `WORKER_ID` would otherwise share a
    heartbeat row and each read the other's beat as its own.
    """
    suffix = secrets.token_hex(3)
    if explicit and explicit.strip():
        base = _ID_CHARS.sub("-", explicit.strip())[:48]
        return f"{base}-{suffix}"
    host = _ID_CHARS.sub("-", socket.gethostname() or "worker")[:32]
    return f"{host}-{os.getpid()}-{suffix}"


@dataclass(frozen=True)
class WorkerRecord:
    """One row of `scheduler_workers`: who a worker is and when it last spoke."""

    worker_id: str
    hostname: str
    pid: int
    status: str
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    stopped_at: datetime | None = None
    campaign_ids: tuple[int, ...] | None = None
    in_flight: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    version: str | None = None

    def is_stale(self, now: datetime, stale_after_secs: float) -> bool:
        """Whether this worker should be treated as dead."""
        if self.status == WORKER_STOPPED:
            return True
        if self.heartbeat_at is None:
            return True
        return (now - self.heartbeat_at).total_seconds() > stale_after_secs

    def health(self, now: datetime, stale_after_secs: float) -> str:
        """`running`, `draining`, `stopped` or `stale`."""
        if self.status == WORKER_STOPPED:
            return WORKER_STOPPED
        if self.is_stale(now, stale_after_secs):
            return "stale"
        return self.status

    def to_dict(self, now: datetime | None = None, stale_after_secs: float = DEFAULT_STALE_SECS) -> dict[str, Any]:
        moment = now or datetime.now(UTC)
        return {
            "worker_id": self.worker_id,
            "hostname": self.hostname,
            "pid": self.pid,
            "status": self.status,
            "health": self.health(moment, stale_after_secs),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "heartbeat_at": self.heartbeat_at.isoformat() if self.heartbeat_at else None,
            "heartbeat_age_secs": (
                round((moment - self.heartbeat_at).total_seconds(), 1) if self.heartbeat_at else None
            ),
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "campaign_ids": list(self.campaign_ids) if self.campaign_ids is not None else None,
            "in_flight": self.in_flight,
            "metrics": dict(self.metrics),
            "version": self.version,
        }


@dataclass(frozen=True)
class WorkerSummary:
    """The fleet in numbers, for a dashboard tile or a health check."""

    running: int = 0
    draining: int = 0
    stale: int = 0
    stopped: int = 0
    in_flight: int = 0
    workers: tuple[WorkerRecord, ...] = ()

    @property
    def alive(self) -> int:
        return self.running + self.draining

    def to_dict(self, now: datetime | None = None, stale_after_secs: float = DEFAULT_STALE_SECS) -> dict[str, Any]:
        return {
            "running": self.running,
            "draining": self.draining,
            "stale": self.stale,
            "stopped": self.stopped,
            "alive": self.alive,
            "in_flight": self.in_flight,
            "workers": [w.to_dict(now, stale_after_secs) for w in self.workers],
        }

    def describe(self) -> str:
        parts = [f"{self.alive} alive"]
        if self.draining:
            parts.append(f"{self.draining} draining")
        if self.stale:
            parts.append(f"{self.stale} STALE")
        if self.stopped:
            parts.append(f"{self.stopped} stopped")
        parts.append(f"{self.in_flight} call(s) followed")
        return ", ".join(parts)


@dataclass(frozen=True)
class QueueDepth:
    """How much calling is waiting, deployment-wide. Phase 21's metric."""

    due_now: int = 0
    """Memberships the queue would hand out right now, across ACTIVE campaigns."""
    scheduled: int = 0
    """Memberships pending a retry that is not yet due."""
    callbacks_due: int = 0
    """Callbacks whose time has come."""
    reserved: int = 0
    """Attempts reserved and not yet placed (PENDING, no call id)."""
    live: int = 0
    """Attempts on a call, or that might be."""
    active_campaigns: int = 0
    per_campaign: tuple[dict[str, Any], ...] = ()

    @property
    def backlog(self) -> int:
        return self.due_now + self.callbacks_due

    def to_dict(self) -> dict[str, Any]:
        return {
            "due_now": self.due_now,
            "scheduled": self.scheduled,
            "callbacks_due": self.callbacks_due,
            "reserved": self.reserved,
            "live": self.live,
            "backlog": self.backlog,
            "active_campaigns": self.active_campaigns,
            "per_campaign": [dict(row) for row in self.per_campaign],
        }

    def describe(self) -> str:
        return (
            f"{self.backlog} due now ({self.due_now} queued + {self.callbacks_due} callbacks), "
            f"{self.scheduled} scheduled later, {self.reserved} reserved, {self.live} live, "
            f"{self.active_campaigns} active campaign(s)"
        )


@dataclass(frozen=True)
class Throughput:
    """What the deployment placed and finished in a recent window. Phase 22's metric.

    Counted from the attempt rows, so every worker's calls are in it and a
    scrape of any one process answers for the fleet. `placed` is attempts
    whose placement began in the window; the other three are attempts that
    ended in it, by how. `cost_usd` sums what Phase 11 priced, and is None
    when no call in the window carried a cost.
    """

    window_secs: float = 3600.0
    placed: int = 0
    finished: int = 0
    answered: int = 0
    failed: int = 0
    cost_usd: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    per_campaign: tuple[dict[str, Any], ...] = ()

    @property
    def per_hour(self) -> float:
        """Calls placed per hour at the window's rate."""
        return self.placed * 3600.0 / self.window_secs if self.window_secs else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_secs": self.window_secs,
            "placed": self.placed,
            "finished": self.finished,
            "answered": self.answered,
            "failed": self.failed,
            "per_hour": round(self.per_hour, 2),
            "cost_usd": self.cost_usd,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "per_campaign": [dict(row) for row in self.per_campaign],
        }

    def describe(self) -> str:
        minutes = self.window_secs / 60
        cost = f", ${self.cost_usd:.4f} estimated" if self.cost_usd is not None else ""
        return (
            f"last {minutes:g} min: {self.placed} placed, {self.finished} finished "
            f"({self.answered} answered, {self.failed} failed){cost}"
        )


#: Reasons a `FAILED` attempt carries when nothing was wrong with the number
#: — the carrier, the network or this system let it down — and a retry may
#: succeed. Everything else stays what Phase 5 decided: a failed number is
#: not dialled again.
_TRANSIENT_PATTERNS = (
    re.compile(r"never reported an outcome", re.I),
    re.compile(r"nothing was dialled", re.I),
    re.compile(r"reserved but never placed", re.I),
    re.compile(r"carrier (is )?unavailable|provider unavailable", re.I),
    re.compile(r"timed? ?out|timeout", re.I),
    re.compile(r"\b(429|500|502|503|504)\b"),
    re.compile(r"rate.?limit", re.I),
    re.compile(r"temporar", re.I),
    re.compile(r"database (went away|unavailable|is unavailable)|cannot reach postgres", re.I),
    re.compile(r"connection (reset|refused|closed)", re.I),
    re.compile(r"released by recovery", re.I),
    re.compile(r"worker .* (died|stale|stopped)", re.I),
)


def transient_failure(reason: str | None) -> bool:
    """Whether a failure reason describes the system's fault rather than the number's.

    Deliberately a keyword classifier over the sentences this project writes
    onto the row — the dialer's, recovery's, the carrier's — because those
    sentences are the only record of *why* an attempt failed, and they were
    written to be read. A reason nobody here wrote (a carrier's own error
    text) is transient only if it names a status code or a timeout.
    """
    if not reason:
        return False
    return any(pattern.search(reason) for pattern in _TRANSIENT_PATTERNS)


__all__ = [
    "CLAIM_LOCK_KEY",
    "DEFAULT_ADOPT_SECS",
    "DEFAULT_HEARTBEAT_SECS",
    "DEFAULT_STALE_SECS",
    "GLOBAL_PACING_KEY",
    "PACING_LOCK_KEY",
    "RESERVE_LOCK_KEY",
    "WORKER_DRAINING",
    "WORKER_RUNNING",
    "WORKER_STOPPED",
    "QueueDepth",
    "WorkerRecord",
    "WorkerSummary",
    "campaign_pacing_key",
    "make_worker_id",
    "transient_failure",
]
