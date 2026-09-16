"""Production monitoring and observability. Phase 22.

What was there before this phase, and stands: Phase 9's structured logs
with the call's row ids bound on every line and every credential scrubbed
(`reliability/observability.py`); Phase 2's per-response latency summary
(`metrics.py`); Phase 11's usage and cost per call (`reliability/usage.py`);
Phase 12's per-call quality report; Phase 13's `worker.metrics` line; Phase
14's `WebhookMetrics`; Phase 21's heartbeat table and queue depth. Each
answered its own question in its own shape — a log line, a JSON file, a
row. None of them could be scraped, none of them shared a name across
processes, and no single id followed a call from the scheduler to the CRM.

This package adds the three things a deployment needs on top, and changes
nothing about what a call *does*:

| Module           | What it is                                                                    |
|------------------|-------------------------------------------------------------------------------|
| `metrics.py`     | A registry — counters, gauges, histograms — rendered as Prometheus text or JSON. No dependency. |
| `instruments.py` | Every metric this project records, defined once by name.                       |
| `tracing.py`     | The correlation id: born at the dialer, on the row, on the handshake, on every log line. |
| `http.py`        | `/healthz`, `/readyz`, `/metrics` for every server; request ids; a server for the scheduler. |
| `collect.py`     | Queue depth, worker health and throughput read from PostgreSQL into gauges.    |

**The boundary.** `metrics.py` and `tracing.py` import nothing from this
project; `instruments.py` only them. `http.py` imports FastAPI and the
scrubber. `collect.py` is duck-typed over the store. Nothing here imports
`src.campaigns`, `src.security`, `src.automation` or `src.crm`, so the call
path — the bot, the supervisor, the diagnostics observer, the dialer — can
record a number without acquiring a dependency, and the boundary checks in
the earlier phases' scripts stay true.

Import from here for the common needs; the instruments by name from
`instruments`, the HTTP pieces from `http`.
"""

from __future__ import annotations

from .instruments import measured, outcome_of
from .metrics import (
    LABEL_NAMES_ALLOWED,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    MetricError,
    MetricsRegistry,
)
from .tracing import (
    PARAM_TRACE_ID,
    REQUEST_HEADER,
    clean_id,
    new_request_id,
    new_trace_id,
    trace_from_runner_args,
)

__all__ = [
    "LABEL_NAMES_ALLOWED",
    "PARAM_TRACE_ID",
    "REGISTRY",
    "REQUEST_HEADER",
    "Counter",
    "Gauge",
    "Histogram",
    "MetricError",
    "MetricsRegistry",
    "clean_id",
    "measured",
    "new_request_id",
    "new_trace_id",
    "outcome_of",
    "trace_from_runner_args",
]
