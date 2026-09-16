"""Every metric this project records, defined once, by name. Phase 22.

One module so that the whole vocabulary can be read in a minute, and so a
dashboard built against these names finds them in every process. Each
process records the subset it lives: the bot the turn latencies and the
service errors, the scheduler the placements and outcomes, the receiver the
webhooks, the syncer the CRM filings. A metric a process never touches is
still *defined* there and simply has no series, which is what a scrape
should see rather than an absent name.

The seventeen things the phase asks to track, and where each lands:

| Tracked                         | Metric(s)                                              | Recorded by |
|---------------------------------|--------------------------------------------------------|-------------|
| call attempts                   | `aiva_call_attempts_total{campaign,outcome}`           | the dialer |
| call success / failure          | `aiva_call_outcomes_total{campaign,status}`, `aiva_call_results_total{outcome}` | the worker, the sink |
| carrier failures                | `aiva_carrier_failures_total{provider,kind}`           | the dialer, recovery |
| STT / LLM / TTS errors          | `aiva_service_errors_total{stage,kind}`                | the supervisor |
| barge-in events                 | `aiva_barge_ins_total`                                 | the diagnostics observer |
| webhook failures                | `aiva_webhook_events_total{provider,outcome}`, `aiva_automation_deliveries_total{kind,outcome}` | the receiver, the deliverer |
| CRM failures                    | `aiva_crm_syncs_total{provider,outcome}`               | the syncer |
| calendar failures               | `aiva_calendar_operations_total{operation,outcome}`    | the action service |
| callback failures               | `aiva_callback_operations_total{operation,outcome}`    | the action service, the worker |
| average and percentile latency  | `aiva_turn_latency_seconds{stage}` and the other histograms | the latency reporter, everything timed |
| token usage                     | `aiva_llm_tokens_total{kind,model}`, `aiva_llm_tokens_per_call` | the bot at teardown |
| estimated cost per call         | `aiva_call_cost_usd`, `aiva_cost_usd_total`            | the bot at teardown |
| campaign throughput             | `aiva_throughput_calls{kind}` (per hour, from the rows) | `collect.py` |
| worker health                   | `aiva_workers{state}`, `aiva_worker_heartbeats_total`, `aiva_worker_in_flight` | `collect.py`, the worker |
| queue depth                     | `aiva_queue_depth{bucket}`                             | `collect.py` |

Names follow the Prometheus conventions: a unit suffix (`_seconds`,
`_usd`), `_total` on a counter, base units (seconds, not milliseconds).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .metrics import REGISTRY, Counter, Gauge, Histogram

T = TypeVar("T")

# --- Buckets, in base units --------------------------------------------------------

#: A spoken turn: silence to first audio. Hundreds of milliseconds when the
#: stack is healthy, tens of seconds when the LLM's free tier throttles it.
TURN_BUCKETS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0)
#: A carrier's answer to "place this call", a webhook applied, a CRM filing.
REQUEST_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)
#: One database statement.
STORE_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)
#: One HTTP request to a server here.
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
#: A whole call, in seconds.
CALL_BUCKETS = (5.0, 15.0, 30.0, 60.0, 120.0, 180.0, 300.0, 600.0, 900.0, 1800.0)
#: What a call cost, in dollars.
COST_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
#: Tokens one call sent and received.
TOKEN_BUCKETS = (500.0, 1000.0, 2500.0, 5000.0, 10000.0, 25000.0, 50000.0, 100000.0, 250000.0)

# --- The instruments -----------------------------------------------------------------

PROCESS_INFO = REGISTRY.gauge(
    "aiva_process_start_time_seconds", "When this process started, as a Unix timestamp.", ("role",)
)
PROCESS_UP = REGISTRY.gauge("aiva_up", "1 while the process is serving.", ("role",))

# Sessions and calls, from the bot's side.
SESSIONS = REGISTRY.counter("aiva_sessions_total", "Voice sessions started, by transport.", ("transport",))
SESSIONS_ACTIVE = REGISTRY.gauge("aiva_sessions_active", "Voice sessions running right now.", ("transport",))
SESSION_ENDINGS = REGISTRY.counter(
    "aiva_session_endings_total", "How voice sessions ended: who or what closed them.", ("reason",)
)
CALL_DURATION = REGISTRY.histogram(
    "aiva_call_duration_seconds", "Audio-connected duration of phone calls, from the bot's side.", buckets=CALL_BUCKETS
)

# Placing calls, from the scheduler's side.
CALL_ATTEMPTS = REGISTRY.counter(
    "aiva_call_attempts_total",
    "Reservations the dialer acted on, by what happened: placed, refused by the carrier, "
    "blocked by a rule, deferred, exhausted, released, paced, unresolved.",
    ("campaign", "outcome"),
)
CALL_OUTCOMES = REGISTRY.counter(
    "aiva_call_outcomes_total", "Call attempts followed to a final status.", ("campaign", "status")
)
CALL_RESULTS = REGISTRY.counter(
    "aiva_call_results_total", "Call results the conversation sink wrote, by disposition.", ("outcome",)
)
PLACEMENT_LATENCY = REGISTRY.histogram(
    "aiva_placement_seconds", "How long the carrier took to accept or refuse a placement.", buckets=REQUEST_BUCKETS
)
CARRIER_FAILURES = REGISTRY.counter(
    "aiva_carrier_failures_total",
    "Times the carrier refused, timed out, answered ambiguously or could not be asked about a call.",
    ("provider", "kind"),
)

# Inside a call.
SERVICE_ERRORS = REGISTRY.counter(
    "aiva_service_errors_total", "Errors from the speech and language services during calls.", ("stage", "kind")
)
SUPERVISOR_TERMINATIONS = REGISTRY.counter(
    "aiva_supervisor_terminations_total", "Calls the session supervisor ended, by reason.", ("reason",)
)
BARGE_INS = REGISTRY.counter("aiva_barge_ins_total", "Times a caller interrupted the agent mid-utterance.")
TURN_LATENCY = REGISTRY.histogram(
    "aiva_turn_latency_seconds",
    "Per-response latency by stage: total (silence to first audio), turn_end, stt, llm (first token), tts (first audio).",
    ("stage",),
    buckets=TURN_BUCKETS,
)
GREETING_LATENCY = REGISTRY.histogram(
    "aiva_greeting_latency_seconds", "Connect to the agent's first audio.", buckets=TURN_BUCKETS
)
LLM_TOKENS = REGISTRY.counter(
    "aiva_llm_tokens_total", "Tokens sent to and received from the LLM, by kind and model.", ("kind", "model")
)
LLM_TOKENS_PER_CALL = REGISTRY.histogram(
    "aiva_llm_tokens_per_call", "Prompt plus completion tokens one session used.", buckets=TOKEN_BUCKETS
)
LLM_REQUESTS = REGISTRY.counter("aiva_llm_requests_total", "Inferences made, by model.", ("model",))
TTS_CHARACTERS = REGISTRY.counter("aiva_tts_characters_total", "Characters synthesised, by model.", ("model",))
STT_AUDIO_SECONDS = REGISTRY.counter(
    "aiva_stt_audio_seconds_total", "Audio submitted for transcription, by model.", ("model",)
)
CALL_COST = REGISTRY.histogram(
    "aiva_call_cost_usd", "Estimated cost of one call, from configured rates and measured usage.", buckets=COST_BUCKETS
)
COST_TOTAL = REGISTRY.counter("aiva_cost_usd_total", "Estimated cost of every call this process handled, by stage.", ("stage",))

# Tools the agent calls.
CALENDAR_OPERATIONS = REGISTRY.counter(
    "aiva_calendar_operations_total", "Calendar checks and bookings, by outcome.", ("operation", "outcome")
)
CALLBACK_OPERATIONS = REGISTRY.counter(
    "aiva_callback_operations_total", "Callbacks scheduled by the agent and placed by the worker, by outcome.", ("operation", "outcome")
)
TRANSFER_OPERATIONS = REGISTRY.counter("aiva_transfer_operations_total", "Transfers to a person, by outcome.", ("outcome",))
KNOWLEDGE_SEARCHES = REGISTRY.counter("aiva_knowledge_searches_total", "Knowledge-base searches from the tool, by outcome.", ("outcome",))
TOOL_LATENCY = REGISTRY.histogram(
    "aiva_tool_seconds", "How long a tool's backend took, by operation.", ("operation",), buckets=REQUEST_BUCKETS
)

# The rows.
STORE_OPERATIONS = REGISTRY.counter(
    "aiva_store_operations_total", "Database operations on the call path, by outcome.", ("operation", "outcome")
)
STORE_LATENCY = REGISTRY.histogram(
    "aiva_store_seconds", "How long a database operation took.", ("operation",), buckets=STORE_BUCKETS
)

# What comes back from, and goes out to, other systems.
WEBHOOK_EVENTS = REGISTRY.counter(
    "aiva_webhook_events_total", "Carrier webhook deliveries received, by outcome.", ("provider", "outcome")
)
WEBHOOK_LATENCY = REGISTRY.histogram(
    "aiva_webhook_seconds", "Time to verify, record and apply a carrier webhook.", buckets=REQUEST_BUCKETS
)
AUTOMATION_DELIVERIES = REGISTRY.counter(
    "aiva_automation_deliveries_total", "Outbound automation events, by kind and outcome.", ("kind", "outcome")
)
AUTOMATION_LATENCY = REGISTRY.histogram(
    "aiva_automation_delivery_seconds", "Time to deliver one automation event.", buckets=REQUEST_BUCKETS
)
CRM_SYNCS = REGISTRY.counter("aiva_crm_syncs_total", "CRM filings, by outcome.", ("provider", "outcome"))
CRM_LATENCY = REGISTRY.histogram("aiva_crm_sync_seconds", "Time to file one result with the CRM.", buckets=REQUEST_BUCKETS)

# The fleet and the queue, refreshed from PostgreSQL.
QUEUE_DEPTH = REGISTRY.gauge(
    "aiva_queue_depth", "Calling waiting deployment-wide: due_now, scheduled, callbacks_due, reserved, live, backlog.", ("bucket",)
)
CAMPAIGNS_ACTIVE = REGISTRY.gauge("aiva_campaigns_active", "Campaigns whose status is ACTIVE.")
WORKERS = REGISTRY.gauge("aiva_workers", "Scheduler workers by health: running, draining, stale, stopped.", ("state",))
WORKERS_IN_FLIGHT = REGISTRY.gauge("aiva_workers_in_flight", "Calls the whole fleet is following.")
WORKER_IN_FLIGHT = REGISTRY.gauge("aiva_worker_in_flight", "Calls this worker process is following.")
WORKER_HEARTBEATS = REGISTRY.counter("aiva_worker_heartbeats_total", "Heartbeats this worker wrote, by outcome.", ("outcome",))
WORKER_TICKS = REGISTRY.counter("aiva_worker_ticks_total", "Scheduler loop iterations.")
THROUGHPUT = REGISTRY.gauge(
    "aiva_throughput_calls",
    "Calls in the last window (default one hour), deployment-wide: placed, finished, answered, failed.",
    ("kind",),
)
THROUGHPUT_WINDOW = REGISTRY.gauge("aiva_throughput_window_seconds", "The window aiva_throughput_calls counts over.")
THROUGHPUT_COST = REGISTRY.gauge("aiva_throughput_cost_usd", "Estimated cost of the calls in the window, where rates were configured.")
COLLECTOR_REFRESHES = REGISTRY.counter(
    "aiva_collector_refreshes_total", "Times the database-derived gauges were refreshed, by outcome.", ("outcome",)
)

# The HTTP servers.
HTTP_REQUESTS = REGISTRY.counter(
    "aiva_http_requests_total", "HTTP requests served, by route template and status.", ("role", "method", "route", "status")
)
HTTP_LATENCY = REGISTRY.histogram(
    "aiva_http_request_seconds", "HTTP request duration, by route template.", ("role", "route"), buckets=HTTP_BUCKETS
)


# --- Helpers the instrumented modules share --------------------------------------------


async def measured(
    operation: str,
    call: Callable[[], Awaitable[T]],
    *,
    counter: Counter = STORE_OPERATIONS,
    latency: Histogram = STORE_LATENCY,
) -> T:
    """Run `call`, counting it under `operation` with `ok` or the exception's class, and timing it.

    The exception is re-raised untouched: this only watches. Used on the
    store's call-path writes so the *database* hop of a trace has a number
    and a `store.op` log line of its own.
    """
    started = time.monotonic()
    try:
        result = await call()
    except Exception as exc:
        counter.inc(operation=operation, outcome=exc.__class__.__name__)
        latency.observe(time.monotonic() - started, operation=operation)
        raise
    counter.inc(operation=operation, outcome="ok")
    latency.observe(time.monotonic() - started, operation=operation)
    return result


def outcome_of(result: Any) -> str:
    """`ok`, or an action outcome's error code — the label for a tool's counter."""
    if getattr(result, "ok", False):
        return "ok"
    return str(getattr(result, "error_code", None) or "error")


def process_started(role: str, started_at: float | None = None) -> None:
    """Stamp the two process gauges. Called once, by whichever entry point owns the process."""
    PROCESS_INFO.set(started_at if started_at is not None else time.time(), role=role)
    PROCESS_UP.set(1, role=role)


def process_stopping(role: str) -> None:
    PROCESS_UP.set(0, role=role)


__all__ = [
    "AUTOMATION_DELIVERIES",
    "AUTOMATION_LATENCY",
    "BARGE_INS",
    "CALENDAR_OPERATIONS",
    "CALLBACK_OPERATIONS",
    "CALL_ATTEMPTS",
    "CALL_COST",
    "CALL_DURATION",
    "CALL_OUTCOMES",
    "CALL_RESULTS",
    "CAMPAIGNS_ACTIVE",
    "CARRIER_FAILURES",
    "COLLECTOR_REFRESHES",
    "COST_TOTAL",
    "CRM_LATENCY",
    "CRM_SYNCS",
    "GREETING_LATENCY",
    "HTTP_LATENCY",
    "HTTP_REQUESTS",
    "KNOWLEDGE_SEARCHES",
    "LLM_REQUESTS",
    "LLM_TOKENS",
    "LLM_TOKENS_PER_CALL",
    "PLACEMENT_LATENCY",
    "PROCESS_INFO",
    "PROCESS_UP",
    "QUEUE_DEPTH",
    "SERVICE_ERRORS",
    "SESSIONS",
    "SESSIONS_ACTIVE",
    "SESSION_ENDINGS",
    "STORE_LATENCY",
    "STORE_OPERATIONS",
    "STT_AUDIO_SECONDS",
    "SUPERVISOR_TERMINATIONS",
    "THROUGHPUT",
    "THROUGHPUT_COST",
    "THROUGHPUT_WINDOW",
    "TOOL_LATENCY",
    "TRANSFER_OPERATIONS",
    "TTS_CHARACTERS",
    "TURN_LATENCY",
    "WEBHOOK_EVENTS",
    "WEBHOOK_LATENCY",
    "WORKERS",
    "WORKERS_IN_FLIGHT",
    "WORKER_HEARTBEATS",
    "WORKER_IN_FLIGHT",
    "WORKER_TICKS",
    "measured",
    "outcome_of",
    "process_started",
    "process_stopping",
]
