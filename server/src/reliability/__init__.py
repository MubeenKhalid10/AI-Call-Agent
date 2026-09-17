"""Making the system safe to point at real phone numbers. Phase 9.

Everything in this package answers a question about *failure* rather than about
what the agent says. Phases 1–8 built a cold caller that works; this one is
about what happens when a piece of it does not.

**The one requirement that shapes the rest.** The system must never place two
calls to the same person by accident. That is not a retry policy, it is a
design constraint, and it is met by five independent mechanisms, any one of
which would usually be enough:

1. `reserve_next_call` picks and reserves in one transaction, with
   `FOR UPDATE SKIP LOCKED` (Phase 5).
2. That reservation carries an `idempotency_key` derived from what the call
   *is* — campaign, membership, attempt number — under a unique index, so two
   callers who mean the same call resolve to one row even without the lock
   (`idempotency.py`).
3. `place_call` is **never retried**. Its policy is `NEVER_RETRY`, and an
   ambiguous answer becomes `CallAttemptStatus.UNRESOLVED` rather than a second
   attempt (`retry.py`, `campaigns/dialer.py`).
4. `UNRESOLVED` is a *live* status, so the prospect stays blocked until
   somebody knows what happened. Not-knowing costs one uncalled prospect, which
   is the cheap direction.
5. `recovery.py` resolves it by asking the carrier what calls exist, never by
   dialling. It is the only thing that unblocks an ambiguous attempt.

**The modules.**

| Module | Answers |
|---|---|
| `retry.py` | May this be tried again? Bounded, backed off, jittered — and never for an ambiguous write. |
| `idempotency.py` | What makes two requests the same call? |
| `guardrails.py` | May a call go out right now — hours, pacing, concurrency, duration? |
| `supervisor.py` | What does the bot do when a service fails or the call runs long? |
| `health.py` | Is every dependency reachable? Asked without placing a call. |
| `observability.py` | Structured logs that follow one call, with credentials scrubbed. |
| `usage.py` | What a call consumed, and what that cost. Units measured, prices configured, nothing invented. |

Mechanism 5, the recovery pass, is `campaigns/recovery.py` rather than a module
here: it reads campaign rows and talks to a carrier, so by the rule
`dialer.py` and `briefing.py` already follow it belongs on the side that owns
the rows. That also keeps **this package a strictly lower layer** — nothing in
`src/reliability/` imports `src/campaigns/`, which is what stops the two
becoming circular, and it is why `health.py` imports the store inside a
function rather than at the top of the file.

**No distributed infrastructure.** There is no broker, no lock service and no
scheduler daemon here. Correctness lives in PostgreSQL — a transaction, two
unique indexes and a status that blocks — because that is the one component
this system already depends on being consistent. Pacing and the concurrency
limiter are about *rate*, and are honestly documented as in-process: a second
dialer would halve the interval, and could not cause a duplicate call, because
none of the five mechanisms above depends on being the only process running.

Nothing here imports `bot.py`. Everything except `health.py` is pure enough to
test against fixed inputs, which is what `tests/test_reliability.py` does.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .guardrails import (
    CallingWindow,
    CampaignGuards,
    Decision,
    PacingLimiter,
    check_concurrency,
    check_duration,
    prospect_timezone,
    resolve_zone,
)
from .health import Component, HealthChecker, HealthReport, Status, check_health
from .idempotency import campaign_call_key, manual_call_key
from .observability import (
    CallContext,
    Timer,
    call_context,
    configure_logging,
    event,
    install_scrubber,
    redact,
)
from .retry import (
    DATABASE_POLICY,
    NEVER_RETRY,
    READ_POLICY,
    AmbiguousOutcomeError,
    RetryPolicy,
    Verdict,
    call_with_retry,
    describe_policies,
    guarded,
    is_ambiguous,
    jittered,
    read_classifier,
    write_classifier,
)

if TYPE_CHECKING:
    from .supervisor import Reason, ServiceHealth, SessionSupervisor
    from .usage import CallUsage, CostRates, ModelUsage, UsageObserver, estimate_cost

# The supervisor and the usage observer are Pipecat observers: they import its
# frames at module load. Only the bot uses them, so they are resolved on first
# access (PEP 562) rather than imported here — the application (the dashboard,
# the automation API, the Vercel function) takes `check_health`, the retry
# policies and the logging from this package without loading Pipecat.
_LAZY = {
    "Reason": ".supervisor",
    "ServiceHealth": ".supervisor",
    "SessionSupervisor": ".supervisor",
    "CallUsage": ".usage",
    "CostRates": ".usage",
    "ModelUsage": ".usage",
    "UsageObserver": ".usage",
    "estimate_cost": ".usage",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "DATABASE_POLICY",
    "NEVER_RETRY",
    "READ_POLICY",
    "AmbiguousOutcomeError",
    "CallContext",
    "CallUsage",
    "CallingWindow",
    "CampaignGuards",
    "Component",
    "CostRates",
    "Decision",
    "ModelUsage",
    "UsageObserver",
    "estimate_cost",
    "HealthChecker",
    "HealthReport",
    "PacingLimiter",
    "Reason",
    "RetryPolicy",
    "ServiceHealth",
    "SessionSupervisor",
    "Status",
    "Timer",
    "Verdict",
    "call_context",
    "call_with_retry",
    "campaign_call_key",
    "check_concurrency",
    "check_duration",
    "check_health",
    "configure_logging",
    "describe_policies",
    "event",
    "guarded",
    "install_scrubber",
    "is_ambiguous",
    "jittered",
    "manual_call_key",
    "prospect_timezone",
    "read_classifier",
    "redact",
    "resolve_zone",
    "write_classifier",
]
