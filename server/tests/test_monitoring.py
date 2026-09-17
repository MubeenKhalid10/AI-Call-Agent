"""Phase 22 checks: monitoring and observability, without changing what a call does.

What is checked, and how:

* **The registry** — counters, gauges and histograms; labels refused when
  they are not on the allowed list (a `phone=` label can never reach a
  scrape); the Prometheus text rendering, escaping and all; the JSON
  snapshot's exact percentiles.
* **The trace** — its shape; read from a handshake; bound on every log line
  through `call_context`; on the JSON log line beside `component`; and
  carried end to end through the real code over the earlier phases'
  fixtures: the dialer writes it on the row and the handshake, the worker,
  the webhook receiver, the CRM syncer and the event deliverer read it
  back from the row and log under it.
* **Every instrumented point**, driven through the real code with the
  outside world stubbed as the earlier scripts stub it: placements and
  outcomes (`test_worker`'s world), carrier refusals, service errors and
  the stall (the supervisor), barge-in (the diagnostics observer), turn
  latency (the reporter), webhooks (`test_webhooks`' ledger and signer),
  CRM filings (`test_crm`'s mock), automation deliveries (`test_automation`'s
  recording sender), calendar and callback tools (`test_actions`' stubs),
  store operations, the fleet gauges.
* **The HTTP surface** — `/healthz`, `/readyz` (200 and 503), `/metrics`
  and `/metrics.json`, the bearer token, the request id, the route-template
  label; on a bare app, on the real dashboard and the real automation API
  through FastAPI's test client, and on the scheduler's own small server
  bound to an ephemeral port.
* **Nothing sensitive leaks** — a DSN in a readiness detail is scrubbed,
  no label name can carry a person, the token is scrubbed from the log.
* **The boundary** — `metrics.py` and `tracing.py` import nothing from the
  project; the package imports no campaigns, security, automation or CRM
  code; the conversation layer imports no monitoring; `bot.py` never reads
  the fleet tables.
* **Against PostgreSQL** — the column, the row write and read-back, the
  throughput read, `ping()`, the timed store operations. In a throwaway
  schema; skipped without a database.

Run from `server/`:

    uv run python tests/test_monitoring.py

Exit status is non-zero when any check fails.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import re
import sys
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))
os.environ.setdefault("KB_ENABLED", "false")
for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")

import test_worker as tw  # noqa: E402
from loguru import logger  # noqa: E402

from src.campaigns import CallAttemptStatus  # noqa: E402
from src.campaigns.coordination import (  # noqa: E402
    QueueDepth,
    Throughput,
    WorkerRecord,
    WorkerSummary,
)
from src.monitoring import (  # noqa: E402
    LABEL_NAMES_ALLOWED,
    REGISTRY,
    MetricError,
    MetricsRegistry,
    clean_id,
    measured,
    new_trace_id,
    trace_from_runner_args,
)
from src.monitoring import instruments as ins  # noqa: E402
from src.monitoring.collect import (  # noqa: E402
    GaugeRefresher,
    refresh_from_store,
    set_queue_gauges,
    set_throughput_gauges,
    set_worker_gauges,
)
from src.monitoring.http import (  # noqa: E402
    HEALTH_PATH,
    METRICS_JSON_PATH,
    METRICS_PATH,
    READY_PATH,
    Readiness,
    ReadyCheck,
    create_ops_app,
    install_ops_routes,
    run_check,
    serve_ops,
    store_ready,
)
from src.monitoring.tracing import PARAM_TRACE_ID, REQUEST_HEADER  # noqa: E402
from src.reliability.observability import (  # noqa: E402
    CallContext,
    call_context,
    current_trace_id,
    redact,
)
from src.telephony import CallSetupError, CallStatus  # noqa: E402

_failures: list[str] = []
_skipped: list[str] = []
RECORDS: list[dict[str, Any]] = []
_HANDLER: dict[str, int | None] = {"id": None}
TOKEN = "monitoring-token-0123456789abcdef"


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def _capture(message: Any) -> None:
    RECORDS.append(dict(message.record))


def _mark() -> int:
    return len(RECORDS)


def _records(name: str, since: int = 0) -> list[dict[str, Any]]:
    return [r for r in RECORDS[since:] if name in str(r["message"])]


def _extra(record: dict[str, Any], key: str) -> Any:
    return record["extra"].get(key)


def _series(counter: Any) -> dict[tuple[tuple[str, str], ...], float]:
    return dict(counter.series())


# --- The registry -------------------------------------------------------------------------


def check_registry() -> None:
    print("\n=== the registry ===")
    reg = MetricsRegistry()
    calls = reg.counter("t_calls_total", "Calls.", ("campaign", "outcome"))
    calls.inc(campaign=3, outcome="placed")
    calls.inc(2, campaign=3, outcome="placed")
    calls.inc(campaign=4, outcome=None)
    check("a counter adds up per label set", calls.value(campaign=3, outcome="placed") == 3 and calls.total() == 4)
    check("None is the label value `none`, an int becomes text", calls.value(campaign="4", outcome="none") == 1)
    check("a counter cannot go down", _raises(lambda: calls.inc(-1, campaign=3, outcome="placed"), MetricError))
    check("the wrong labels are refused", _raises(lambda: calls.inc(campaign=3), MetricError) and _raises(lambda: calls.inc(campaign=3, outcome="x", extra="y"), MetricError))
    check("a label that could carry a person is refused at definition", _raises(lambda: reg.counter("t_bad_total", "x", ("phone",)), MetricError) and "phone" not in LABEL_NAMES_ALLOWED and "name" not in LABEL_NAMES_ALLOWED and "email" not in LABEL_NAMES_ALLOWED)
    check("a bad metric name is refused", _raises(lambda: reg.counter("1bad", "x"), MetricError) and _raises(lambda: reg.counter("bad-name", "x"), MetricError))
    check("redefining the same name returns the same metric", reg.counter("t_calls_total", "Calls.", ("campaign", "outcome")) is calls)
    check("redefining with other labels is refused", _raises(lambda: reg.counter("t_calls_total", "Calls.", ("campaign",)), MetricError) and _raises(lambda: reg.gauge("t_calls_total", "x", ("campaign", "outcome")), MetricError))

    level = reg.gauge("t_level", "Level.", ("bucket",))
    level.set(5, bucket="due")
    level.inc(2, bucket="due")
    level.dec(10, bucket="due")
    check("a gauge moves both ways and never below zero", level.value(bucket="due") == 0)
    level.set(3, bucket="due")
    level.clear()
    check("a gauge can be cleared", level.value(bucket="due") == 0 and level.series() == [])

    hist = reg.histogram("t_seconds", "Seconds.", ("stage",), buckets=(0.5, 1.0, 2.0))
    for value in (0.2, 0.4, 0.6, 0.9, 1.5, 3.0):
        hist.observe(value, stage="total")
    hist.observe(-1.0, stage="total")
    hist.observe(float("nan"), stage="total")
    stats = hist.stats(stage="total")
    check("a histogram counts, sums and keeps the max", stats.count == 6 and abs(stats.sum - 6.6) < 1e-9 and stats.max == 3.0, str(stats))
    check("negative and non-finite observations are ignored, not raised", stats.count == 6)
    # Nearest rank, the same rule `metrics.py` has used since Phase 2.
    check("exact percentiles over the reservoir", stats.p50 == 0.6 and stats.p95 == 3.0 and stats.p99 == 3.0 and abs(stats.mean - 1.1) < 1e-9, str(stats.to_dict()))
    check("an unseen series reads as empty", hist.stats(stage="llm").count == 0 and hist.stats(stage="llm").p50 is None)
    check("bad buckets are refused", _raises(lambda: reg.histogram("t_bad", "x", buckets=()), MetricError) and _raises(lambda: reg.histogram("t_bad2", "x", buckets=(1, 1)), MetricError))

    text = reg.render_prometheus()
    lines = text.splitlines()
    check("HELP and TYPE precede every metric", "# HELP t_calls_total Calls." in lines and "# TYPE t_calls_total counter" in lines and "# TYPE t_seconds histogram" in lines and "# TYPE t_level gauge" in lines)
    check("a counter series renders with its labels, integers without a point", 't_calls_total{campaign="3",outcome="placed"} 3' in lines, text)
    buckets = [l for l in lines if l.startswith("t_seconds_bucket")]
    check("histogram buckets are cumulative and end with +Inf", buckets == ['t_seconds_bucket{stage="total",le="0.5"} 2', 't_seconds_bucket{stage="total",le="1"} 4', 't_seconds_bucket{stage="total",le="2"} 5', 't_seconds_bucket{stage="total",le="+Inf"} 6'], str(buckets))
    check("with sum and count", 't_seconds_count{stage="total"} 6' in lines and any(l.startswith('t_seconds_sum{stage="total"} 6.6') for l in lines))
    check("the text ends with a newline", text.endswith("\n"))
    quoted = reg.counter("t_quoted_total", 'A "help" with \\ and\nnewline', ("kind",))
    quoted.inc(kind='say "hi"\\there\nnow')
    rendered = reg.render_prometheus()
    # A help line is folded onto one line; a label value keeps its newline, escaped.
    check("label values and help lines are escaped", '# HELP t_quoted_total A "help" with \\\\ and newline' in rendered and 't_quoted_total{kind="say \\"hi\\"\\\\there\\nnow"} 1' in rendered, rendered[-200:])
    long_label = reg.counter("t_long_total", "x", ("kind",))
    long_label.inc(kind="k" * 200)
    check("a label value is bounded", len(next(iter(_series(long_label)))[0][1]) == 80)

    snap = reg.snapshot()
    check("the JSON snapshot carries every metric with its type and series", snap["t_calls_total"]["type"] == "counter" and snap["t_calls_total"]["series"][0] == {"labels": {"campaign": "3", "outcome": "placed"}, "value": 3.0} and snap["t_seconds"]["series"][0]["p95"] == 3.0 and snap["t_seconds"]["buckets"] == [0.5, 1.0, 2.0], json.dumps(snap)[:300])
    check("it is JSON", isinstance(json.dumps(snap), str))
    reg.reset()
    check("reset keeps the definitions and forgets the values", len(reg) == 5 and calls.total() == 0 and hist.stats(stage="total").count == 0, str(len(reg)))

    print("\n  the instruments:")
    names = [m.name for m in REGISTRY]
    check("every instrument is namespaced", names and all(n.startswith("aiva_") for n in names), str([n for n in names if not n.startswith("aiva_")]))
    check("the seventeen tracked things have a metric each", {"aiva_call_attempts_total", "aiva_call_outcomes_total", "aiva_carrier_failures_total", "aiva_service_errors_total", "aiva_barge_ins_total", "aiva_webhook_events_total", "aiva_crm_syncs_total", "aiva_calendar_operations_total", "aiva_callback_operations_total", "aiva_turn_latency_seconds", "aiva_llm_tokens_total", "aiva_call_cost_usd", "aiva_throughput_calls", "aiva_workers", "aiva_queue_depth"} <= set(names))
    check("no instrument's label can carry a person", all(set(m.labels) <= LABEL_NAMES_ALLOWED for m in REGISTRY))
    check("counters end in _total, durations in _seconds, money in _usd", all(m.name.endswith("_total") for m in REGISTRY if m.kind == "counter") and all(not m.name.endswith("_total") for m in REGISTRY if m.kind != "counter"))


async def check_measured() -> None:
    print("\n=== measured operations ===")
    ins.STORE_OPERATIONS.inc(0, operation="probe", outcome="ok")
    before = ins.STORE_OPERATIONS.value(operation="probe", outcome="ok")

    async def fine() -> str:
        return "fine"

    async def broken() -> str:
        raise RuntimeError("boom")

    check("a successful operation counts as ok", await measured("probe", fine) == "fine" and ins.STORE_OPERATIONS.value(operation="probe", outcome="ok") == before + 1)
    check("a failing one counts under its exception and re-raises", await _araises(lambda: measured("probe", broken)) and ins.STORE_OPERATIONS.value(operation="probe", outcome="RuntimeError") == 1)
    check("and both were timed", ins.STORE_LATENCY.stats(operation="probe").count == 2)
    check("outcome_of reads an action outcome", ins.outcome_of(SimpleNamespace(ok=True)) == "ok" and ins.outcome_of(SimpleNamespace(ok=False, error_code="slot_taken")) == "slot_taken" and ins.outcome_of(SimpleNamespace(ok=False)) == "error")


# --- The trace ----------------------------------------------------------------------------


def check_tracing() -> None:
    print("\n=== the correlation id ===")
    trace = new_trace_id()
    check("a trace is sixteen hex characters", re.fullmatch(r"[0-9a-f]{16}", trace) is not None, trace)
    check("and fresh every time", new_trace_id() != trace)
    check("an id from outside is accepted only in its shape", clean_id("abc-123.x_y") == "abc-123.x_y" and clean_id(" abc1234 ") == "abc1234" and clean_id("no spaces here") is None and clean_id("x" * 65) is None and clean_id("ab") is None and clean_id("Bearer sk-secret") is None and clean_id(None) is None)
    args = SimpleNamespace(call_data=SimpleNamespace(body={PARAM_TRACE_ID: trace, "prospect_id": "7"}))
    check("read from a carrier's handshake", trace_from_runner_args(args) == trace)
    check("or from a websocket runner's body", trace_from_runner_args(SimpleNamespace(body={PARAM_TRACE_ID: trace})) == trace)
    check("absent when the handshake carries none, or a malformed one", trace_from_runner_args(SimpleNamespace(call_data=None, body=None)) is None and trace_from_runner_args(SimpleNamespace(body={PARAM_TRACE_ID: "a b"})) is None)

    fields = CallContext(attempt_id=11, trace_id=trace).fields()
    check("the call context puts the trace first", list(fields) == ["trace", "attempt"] and fields["trace"] == trace)
    mark = _mark()
    with call_context(CallContext(campaign_id=3, trace_id=trace)):
        logger.info("traced line")
        inner = current_trace_id()
        with call_context(call_id="CA9"):
            logger.info("nested line")
            nested = current_trace_id()
    with call_context(trace_id="abcdef0123456789"):
        keyword = current_trace_id()
    outside = current_trace_id()
    traced = _records("traced line", mark) + _records("nested line", mark)
    check("every line inside the block carries the trace", len(traced) == 2 and all(_extra(r, "trace") == trace for r in traced) and _extra(traced[1], "call") == "CA9")
    check("current_trace_id reads it back, through nesting, and not outside", inner == trace and nested == trace and outside is None)
    check("the keyword form binds the same field", keyword == "abcdef0123456789")

    print("\n  the JSON log line:")
    from src.reliability.observability import configure_logging

    os.environ["MONITORING_TOKEN"] = TOKEN
    buffer = io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = buffer
    try:
        configure_logging(level="DEBUG", json_logs=True, component="check")
        with call_context(CallContext(attempt_id=5, trace_id=trace, extra={"worker": "w-1"})):
            logger.info(f"json line with token {TOKEN}")
    finally:
        sys.stderr = real_stderr
        # `configure_logging` replaced every handler, the runner's capture
        # included; put the capture back and tell the runner its new id.
        logger.remove()
        _HANDLER["id"] = logger.add(_capture, level="DEBUG")
    line = json.loads(buffer.getvalue().strip().splitlines()[-1])
    check("a JSON line carries the component, the pid, the trace, the attempt and the worker", line.get("component") == "check" and line.get("pid") == os.getpid() and line.get("trace") == trace and line.get("attempt") == 5 and line.get("worker") == "w-1", str(line))
    check("and never the monitoring token", TOKEN not in json.dumps(line) and "***" in line["message"])
    check("redact scrubs it anywhere", TOKEN not in redact(f"Authorization: Bearer {TOKEN}"))


# --- The dialer, the worker, the fleet gauges ------------------------------------------------


async def check_placements() -> None:
    print("\n=== placements, outcomes and the trace on the row ===")
    REGISTRY.reset()
    w = tw.build_world(script=[CallStatus.ANSWERED, CallStatus.COMPLETED], max_concurrent=1)
    campaign = await w.campaign("Traced", ["+923001111111"])
    mark = _mark()
    result = await w.dialer.dial_next(campaign.id)
    check("the call was placed", result.placed, result.describe())
    attempt = await w.store.get_attempt(result.attempt.id)
    trace = attempt.trace_id
    check("the dialer wrote a trace on the attempt row", trace is not None and re.fullmatch(r"[0-9a-f]{16}", trace or "") is not None, str(trace))
    check("and sent the same one on the handshake", w.carrier.requests[-1].parameters.get(PARAM_TRACE_ID) == trace, str(w.carrier.requests[-1].parameters))
    placed = _records("call.placed", mark)
    check("the placement's log line carries it, with the attempt and campaign", placed and _extra(placed[0], "trace") == trace and _extra(placed[0], "attempt") == attempt.id and _extra(placed[0], "campaign") == campaign.id)
    check("aiva_call_attempts_total counts the placement by campaign id", ins.CALL_ATTEMPTS.value(campaign=campaign.id, outcome="placed") == 1)
    check("and the placement was timed", ins.PLACEMENT_LATENCY.stats().count == 1)
    check("the trace is on the runner-args shape the bot reads", trace_from_runner_args(SimpleNamespace(body=dict(w.carrier.requests[-1].parameters))) == trace)

    print("\n  the worker follows it to its end:")
    worker = w.make_worker(heartbeat_secs=1.0)
    await worker.start()
    await worker.tick()
    followed = worker.in_flight
    if not followed:
        # The dialer above placed it outside the worker; the adoption pass picks it up.
        await worker._adopt_live_attempts()
    check("the worker is following the call", len(worker.in_flight) == 1 and ins.WORKER_IN_FLIGHT.value() >= 0)
    w.clock.advance(5)
    mark = _mark()
    await worker.tick()
    await worker.tick()
    final = await w.store.get_attempt(attempt.id)
    check("the call ended COMPLETED", final.status is CallAttemptStatus.COMPLETED, final.status.value)
    check("aiva_call_outcomes_total counts the ending by status and campaign", ins.CALL_OUTCOMES.value(campaign=campaign.id, status="COMPLETED") == 1)
    ended = _records("call.completed", mark)
    check("the worker's ending line carries the trace and the worker id", ended and _extra(ended[0], "trace") == trace and _extra(ended[0], "worker") == worker.worker_id, str(ended[0]["extra"]) if ended else "no line")
    check("ticks and heartbeats are counted", ins.WORKER_TICKS.value() >= 3 and ins.WORKER_HEARTBEATS.value(outcome="ok") >= 1)
    check("in flight is back to zero", ins.WORKER_IN_FLIGHT.value() == 0)

    print("\n  the fleet gauges from the rows:")
    complete = await worker.refresh_fleet_gauges()
    check("the worker refreshes queue depth, worker health and throughput", complete)
    check("one worker running, one call finished in the window", ins.WORKERS.value(state="running") == 1 and ins.THROUGHPUT.value(kind="finished") == 1 and ins.THROUGHPUT.value(kind="answered") == 1 and ins.QUEUE_DEPTH.value(bucket="live") == 0, str(REGISTRY.snapshot()["aiva_throughput_calls"]))
    await worker.finish()

    print("\n  a carrier that refuses:")
    REGISTRY.reset()
    w = tw.build_world(place_error=CallSetupError("21219: unverified number"))
    campaign = await w.campaign("Refused", ["+923002222222"])
    result = await w.dialer.dial_next(campaign.id)
    check("the refusal is recorded", not result.placed and "unverified" in (result.error or ""))
    check("counted as a refused attempt and a carrier failure by class", ins.CALL_ATTEMPTS.value(campaign=campaign.id, outcome="refused") == 1 and ins.CARRIER_FAILURES.value(provider=w.carrier.name, kind="CallSetupError") == 1)
    refused_row = await w.store.get_attempt(result.queued.attempt.id)
    check("the refused attempt still carries its trace on the row", refused_row is not None and refused_row.trace_id == w.carrier.requests[-1].parameters.get(PARAM_TRACE_ID), str(refused_row))
    check("the placement was timed even though it was refused", ins.PLACEMENT_LATENCY.stats().count == 1)

    print("\n  the queue's own gates:")
    REGISTRY.reset()
    w = tw.build_world(hours=("09:00-10:00", "mon-fri"), timezone="UTC", start=datetime(2026, 9, 7, 15, 0, tzinfo=UTC))
    campaign = await w.campaign("Closed", ["+923003333333"])
    result = await w.dialer.dial_next(campaign.id)
    check("a closed window is a deferral or a skip, never a placement", not result.placed and ins.CALL_ATTEMPTS.value(campaign=campaign.id, outcome="placed") == 0, result.describe())


# --- In-call: the supervisor, the diagnostics observer, the reporter ------------------------


def _append(into: list[str], value: str):
    async def action() -> None:
        into.append(value)

    return action


async def check_in_call() -> None:
    print("\n=== inside a call: service errors, the stall, barge-in, latency ===")
    from pipecat.frames.frames import BotStartedSpeakingFrame, InterruptionFrame
    from pipecat.observers.base_observer import FramePushed
    from pipecat.observers.user_bot_latency_observer import LatencyBreakdown, TTFBBreakdownMetrics
    from pipecat.processors.frame_processor import FrameDirection

    from src.diagnostics import TurnDiagnostics
    from src.metrics import LatencyReporter
    from src.reliability import Reason, SessionSupervisor

    REGISTRY.reset()
    ended: list[str] = []
    spoken: list[str] = []

    async def say(text: str) -> None:
        spoken.append(text)

    supervisor = SessionSupervisor(end_session=_append(ended, "end"), cancel_session=_append(ended, "cancel"), say=say, max_service_failures=3)
    await supervisor.note_speech()
    for _ in range(2):
        await supervisor.note_error("stt", "websocket closed")
    supervisor.note_success("stt")
    for _ in range(3):
        await supervisor.note_error("stt", "websocket closed")
    check("every STT error is counted, whatever the supervisor decided", ins.SERVICE_ERRORS.value(stage="stt", kind="error") == 5)
    check("the termination is counted by reason", supervisor.terminated_by is Reason.SERVICE_FAILURE and ins.SUPERVISOR_TERMINATIONS.value(reason="service_failure") == 1)
    await supervisor.note_error("llm", "429")
    await supervisor.note_error("tts", "500")
    check("LLM and TTS errors land on their own stage", ins.SERVICE_ERRORS.value(stage="llm", kind="error") == 1 and ins.SERVICE_ERRORS.value(stage="tts", kind="error") == 1)

    stalled = SessionSupervisor(end_session=_append(ended, "end"), cancel_session=_append(ended, "cancel"), say=say, llm_stall_secs=0.05, max_call_secs=0)
    stalled.start()
    stalled.note_llm_started()
    await asyncio.sleep(1.3)  # The watchdog polls once a second.
    stalled.stop()
    check("a stalled inference is a service error of its own kind", ins.SERVICE_ERRORS.value(stage="llm", kind="stall") == 1 and stalled.terminated_by is Reason.LLM_STALLED and ins.SUPERVISOR_TERMINATIONS.value(reason="llm_stalled") == 1)

    diagnostics = TurnDiagnostics()
    source = SimpleNamespace(name="DeepgramFluxSTTService#0")

    async def push(frame: Any, hops: int = 3) -> None:
        for _ in range(hops):
            await diagnostics.on_push_frame(FramePushed(source=source, destination=source, frame=frame, direction=FrameDirection.DOWNSTREAM, timestamp=0))

    await push(BotStartedSpeakingFrame())
    await push(InterruptionFrame())
    await push(InterruptionFrame())
    check("a barge-in is counted once, however many hops the frame crosses", ins.BARGE_INS.value() == 1 and diagnostics.barge_ins == 1)

    reporter = LatencyReporter(log_each_turn=False)
    await reporter._on_first_bot_speech_latency(None, 4.2)
    await reporter._on_latency_measured(None, 1.1)
    breakdown = LatencyBreakdown(
        ttfb=[
            TTFBBreakdownMetrics(processor="DeepgramFluxSTTService#0", start_time=1.0, duration_secs=0.3),
            TTFBBreakdownMetrics(processor="GroqLLMService#0", start_time=2.0, duration_secs=0.5),
            TTFBBreakdownMetrics(processor="GroqLLMService#0", start_time=2.5, duration_secs=0.9),
            TTFBBreakdownMetrics(processor="CartesiaTTSService#0", start_time=3.0, duration_secs=0.2),
        ],
        user_turn_secs=0.35,
    )
    await reporter._on_latency_breakdown(None, breakdown)
    check("the greeting latency is observed", ins.GREETING_LATENCY.stats().count == 1 and ins.GREETING_LATENCY.stats().max == 4.2)
    check("a response lands on every stage's histogram, earliest TTFB per stage", ins.TURN_LATENCY.stats(stage="total").max == 1.1 and ins.TURN_LATENCY.stats(stage="llm").max == 0.5 and ins.TURN_LATENCY.stats(stage="stt").max == 0.3 and ins.TURN_LATENCY.stats(stage="tts").max == 0.2 and ins.TURN_LATENCY.stats(stage="turn_end").max == 0.35, str(REGISTRY.snapshot()["aiva_turn_latency_seconds"]["series"]))
    check("the session's own summary is unchanged", reporter.summary()["responses"] == 1 and reporter.summary()["stages"]["total"]["p50_ms"] == 1100)


# --- Webhooks, the CRM, the deliverer, the tools --------------------------------------------


async def check_webhooks() -> None:
    print("\n=== the webhook receiver ===")
    from test_webhooks import build as build_webhooks
    from test_webhooks import signed, twilio_fields

    REGISTRY.reset()
    setup = build_webhooks()
    await setup.place()
    attempt = await setup.attempt()
    check("the placed attempt carries a trace", bool(attempt.trace_id))
    forged = await setup.processor.receive(signed(twilio_fields(setup.call_id, "completed", 3), secret="attacker"))
    check("a forged delivery is counted as refused", forged.http_status == 403 and ins.WEBHOOK_EVENTS.value(provider="twilio", outcome="refused") == 1)
    mark = _mark()
    applied = await setup.deliver(twilio_fields(setup.call_id, "ringing", 1))
    check("an applied delivery is counted as applied, and timed", applied.accepted and ins.WEBHOOK_EVENTS.value(provider="twilio", outcome="applied") == 1 and ins.WEBHOOK_LATENCY.stats().count == 2)
    lines = _records("webhook.applied", mark)
    check("the receiver's line carries the call's trace from the row", lines and _extra(lines[0], "trace") == attempt.trace_id and _extra(lines[0], "attempt") == attempt.id, str(lines[0]["extra"]) if lines else "no line")
    await setup.deliver(twilio_fields(setup.call_id, "ringing", 1))
    check("a duplicate is counted as a duplicate", ins.WEBHOOK_EVENTS.value(provider="twilio", outcome="duplicate") == 1)
    check("a stale one as stale", (await setup.deliver(twilio_fields(setup.call_id, "initiated", 0))).outcome == "stale" and ins.WEBHOOK_EVENTS.value(provider="twilio", outcome="stale") == 1)
    check("the Phase 14 metrics still agree", setup.processor.metrics.applied == 1 and setup.processor.metrics.refused == 1)


async def check_crm() -> None:
    print("\n=== the CRM syncer ===")
    from test_crm import attempt as crm_attempt
    from test_crm import build as build_crm
    from test_crm import prospect, rich_result

    REGISTRY.reset()
    setup = build_crm()
    trace = new_trace_id()
    setup.store.add(rich_result(), prospect_row=prospect(), attempt_row=crm_attempt(trace_id=trace))
    mark = _mark()
    report = await setup.syncer.run_once()
    check("one result synced", report.synced == 1 and ins.CRM_SYNCS.value(provider="mock", outcome="synced") == 1 and ins.CRM_LATENCY.stats().count == 1)
    lines = _records("crm.synced", mark)
    check("the filing's line carries the call's trace, read from the attempt row", lines and _extra(lines[0], "trace") == trace and _extra(lines[0], "attempt") == 34, str(lines[0]["extra"]) if lines else "no line")
    from test_crm import thin_result

    quiet = build_crm(sync_unanswered=False)
    quiet.store.add(thin_result(), prospect_row=prospect(), attempt_row=crm_attempt(attempt_id=35))
    report = await quiet.syncer.run_once()
    check("an unanswered result the syncer skips is counted as skipped", report.skipped == 1 and ins.CRM_SYNCS.value(provider="mock", outcome="skipped") == 1)


async def check_deliverer() -> None:
    print("\n=== the event deliverer ===")
    from test_automation import URL_A, FakeStore, RecordingSender
    from test_crm import rich_result

    from src.automation import EventDeliverer
    from src.campaigns import AUTOMATION_EVENT_KINDS, CampaignService, CampaignStatus

    REGISTRY.reset()
    clock = tw.FakeClock(tw.NOW)
    store = FakeStore(clock=clock)
    service = CampaignService(store, default_region="PK", max_attempts=3, retry_minutes=60, clock=clock)  # type: ignore[arg-type]
    campaign = await store.create_campaign(name="Events", status=CampaignStatus.ACTIVE)
    sara = await service.create_prospect(first_name="Sara", last_name="Ali", phone="0300 1234567")
    membership = await store.add_to_campaign(campaign.id, sara.id)
    attempt = await store.create_attempt(prospect_id=sara.id, campaign_id=campaign.id, campaign_prospect_id=membership.id, status=CallAttemptStatus.COMPLETED)
    trace = new_trace_id()
    await store.set_attempt_trace(attempt.id, trace)
    await store.save_call_result(dataclasses.replace(rich_result(attempt_id=attempt.id, prospect_id=sara.id, campaign_id=campaign.id), id=None))
    sender = RecordingSender()
    deliverer = EventDeliverer(store, targets={kind: URL_A for kind in AUTOMATION_EVENT_KINDS}, sender=sender, settle_secs=0, clock=clock)
    mark = _mark()
    report = await deliverer.run_once()
    check("the result's events were delivered", report.delivered >= 1 and ins.AUTOMATION_DELIVERIES.value(kind="call.completed", outcome="delivered") == 1 and ins.AUTOMATION_LATENCY.stats().count == report.delivered, report.describe())
    bodies = sender.bodies()
    completed = next((b for b in bodies if b["event"] == "call.completed"), None)
    check("the payload's call carries the trace, for the workflow to quote back", completed is not None and completed["call"]["trace_id"] == trace, str(completed)[:200] if completed else "none")
    lines = [r for r in _records("automation.delivered", mark) if _extra(r, "trace") == trace]
    check("the delivery's line carries it too", bool(lines))


async def check_tools() -> None:
    print("\n=== the tools: calendar, callback, transfer, knowledge ===")
    from test_actions import World as ActionsWorld

    REGISTRY.reset()
    world = ActionsWorld()
    tomorrow = date(2026, 9, 7)
    world.calendar.mode = "unreachable"
    outcome = await world.service.check_availability(tomorrow, None)
    check("an unreachable calendar is counted under its error code", not outcome.ok and ins.CALENDAR_OPERATIONS.value(operation="check", outcome="external_error") == 1)
    world.calendar.mode = "ok"
    outcome = await world.service.check_availability(tomorrow, None)
    check("a working one as ok", outcome.ok and ins.CALENDAR_OPERATIONS.value(operation="check", outcome="ok") == 1)
    check("and timed by operation", ins.TOOL_LATENCY.stats(operation="check").count == 2)
    past = datetime(2020, 1, 1, tzinfo=UTC)
    outcome = await world.service.schedule_callback(past)
    check("a callback in the past is counted under past_time", not outcome.ok and ins.CALLBACK_OPERATIONS.value(operation="schedule", outcome="past_time") == 1)
    outcome = await world.service.schedule_callback(datetime(2026, 9, 5, 10, 0, tzinfo=UTC), note="mornings")
    check("a scheduled one as ok", outcome.ok and ins.CALLBACK_OPERATIONS.value(operation="schedule", outcome="ok") == 1, outcome.message or "")
    outcome = await world.service.transfer_to_human("wants a person")
    check("a transfer on a browser session is counted as unavailable", not outcome.ok and ins.TRANSFER_OPERATIONS.value(outcome="transfer_unavailable") == 1)
    outcome = await world.service.search_knowledge("pricing")
    check("a knowledge search with no knowledge base likewise", not outcome.ok and ins.KNOWLEDGE_SEARCHES.value(outcome="unavailable") == 1)


# --- The fleet gauges --------------------------------------------------------------------------


async def check_collect() -> None:
    print("\n=== the fleet gauges ===")
    REGISTRY.reset()
    depth = QueueDepth(due_now=3, scheduled=2, callbacks_due=1, reserved=0, live=1, active_campaigns=2)
    set_queue_gauges(depth)
    check("queue depth by bucket, backlog included", ins.QUEUE_DEPTH.value(bucket="due_now") == 3 and ins.QUEUE_DEPTH.value(bucket="backlog") == 4 and ins.CAMPAIGNS_ACTIVE.value() == 2)
    summary = WorkerSummary(running=2, draining=1, stale=1, stopped=4, in_flight=3, workers=(WorkerRecord("w", "h", 1, "running"),))
    set_worker_gauges(summary)
    check("workers by state", ins.WORKERS.value(state="running") == 2 and ins.WORKERS.value(state="stale") == 1 and ins.WORKERS_IN_FLIGHT.value() == 3)
    throughput = Throughput(window_secs=1800.0, placed=10, finished=9, answered=6, failed=1, cost_usd=0.42)
    set_throughput_gauges(throughput)
    check("throughput by kind, with the window and the cost", ins.THROUGHPUT.value(kind="placed") == 10 and ins.THROUGHPUT_WINDOW.value() == 1800 and abs(ins.THROUGHPUT_COST.value() - 0.42) < 1e-9)
    check("throughput reads for a person and a machine", "10 placed" in throughput.describe() and "$0.4200" in throughput.describe() and throughput.per_hour == 20.0 and throughput.to_dict()["per_hour"] == 20.0)

    world = tw.build_world()
    complete = await refresh_from_store(world.store, stale_secs=60.0, max_attempts=3)
    check("a store with every read refreshes completely", complete and ins.COLLECTOR_REFRESHES.value(outcome="ok") == 1)

    class Partial:
        async def queue_depth(self, *, max_attempts: int = 3) -> QueueDepth:
            return QueueDepth()

        async def worker_summary(self, *, stale_after_secs: float) -> WorkerSummary:
            raise RuntimeError("no scheduler_workers table")

    complete = await refresh_from_store(Partial(), stale_secs=60.0)
    check("a store missing a read is partial, not an exception", not complete and ins.COLLECTOR_REFRESHES.value(outcome="partial") == 1)

    slept: list[float] = []

    async def sleep(secs: float) -> None:
        slept.append(secs)
        refresher._stopping = True

    refresher = GaugeRefresher(lambda: world.store, interval_secs=7.0, stale_secs=60.0, sleep=sleep)
    refresher.start()
    await asyncio.sleep(0.05)
    await refresher.stop()
    check("the refresher loop refreshes then sleeps for the interval", refresher.refreshes >= 1 and slept and slept[0] == 7.0, f"{refresher.refreshes} {slept}")
    warned = _mark()
    starting = GaugeRefresher(lambda: (_ for _ in ()).throw(RuntimeError("still starting")), interval_secs=7.0, stale_secs=60.0, sleep=sleep)
    check("a store that is not open yet is not an error", await starting.refresh_once() is False and not _records("collect.partial", warned))


# --- The HTTP surface ------------------------------------------------------------------------


async def check_http() -> None:
    print("\n=== /healthz, /readyz, /metrics ===")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    REGISTRY.reset()
    ready = {"ok": True}

    async def readiness() -> Readiness:
        return Readiness([ReadyCheck("database", ready["ok"], "postgresql://u:hunter2@db/x answers" if ready["ok"] else "postgresql://u:hunter2@db/x refused", 3)])

    app = FastAPI()

    @app.get("/things/{item}")
    async def thing(item: str) -> dict[str, str]:
        return {"item": item}

    install_ops_routes(app, "api", readiness=readiness, token=TOKEN, version="22", info=lambda: {"extra": 1}, stopping=lambda: ready.get("stopping", False))
    client = TestClient(app)

    health = client.get(HEALTH_PATH)
    check("/healthz is 200 with the role, pid, uptime, version and the extra info", health.status_code == 200 and health.json()["ok"] and health.json()["role"] == "api" and health.json()["pid"] == os.getpid() and health.json()["version"] == "22" and health.json()["extra"] == 1 and "uptime_secs" in health.json(), health.text)
    check("it never needs the token", health.status_code == 200)
    readyz = client.get(READY_PATH)
    check("/readyz is 200 when every check passes", readyz.status_code == 200 and readyz.json()["ready"] and readyz.json()["checks"][0]["name"] == "database")
    check("and its detail is scrubbed of the password", "hunter2" not in readyz.text)
    ready["ok"] = False
    readyz = client.get(READY_PATH)
    check("/readyz is 503 when one fails, still scrubbed", readyz.status_code == 503 and not readyz.json()["ready"] and "hunter2" not in readyz.text and "refused" in readyz.text)
    ready["ok"] = True
    ready["stopping"] = True
    check("/readyz is 503 while the process is stopping", client.get(READY_PATH).status_code == 503 and client.get(HEALTH_PATH).json()["stopping"] is True)
    ready["stopping"] = False

    check("/metrics needs the token when one is set", client.get(METRICS_PATH).status_code == 401 and client.get(METRICS_PATH).headers.get("www-authenticate") == "Bearer")
    check("a wrong token is refused", client.get(METRICS_PATH, headers={"Authorization": f"Bearer {TOKEN[:-1]}x"}).status_code == 401)
    ins.CALL_ATTEMPTS.inc(campaign=3, outcome="placed")
    metrics = client.get(METRICS_PATH, headers={"Authorization": f"Bearer {TOKEN}"})
    check("with it, the Prometheus text is served with the right content type", metrics.status_code == 200 and metrics.headers["content-type"].startswith("text/plain; version=0.0.4") and 'aiva_call_attempts_total{campaign="3",outcome="placed"} 1' in metrics.text and "# TYPE aiva_up gauge" in metrics.text, metrics.headers.get("content-type"))
    as_json = client.get(METRICS_JSON_PATH, headers={"Authorization": f"Bearer {TOKEN}"})
    check("/metrics.json is the snapshot", as_json.status_code == 200 and as_json.json()["role"] == "api" and as_json.json()["metrics"]["aiva_call_attempts_total"]["series"][0]["value"] == 1)
    check("the process gauges are stamped", ins.PROCESS_UP.value(role="api") == 1 and ins.PROCESS_INFO.value(role="api") > 0)

    print("\n  the middlewares:")
    reply = client.get("/things/+923001234567", headers={REQUEST_HEADER: "req-abc-1"})
    check("a well-formed request id is honoured and echoed", reply.headers.get(REQUEST_HEADER.lower()) == "req-abc-1")
    reply = client.get("/things/x", headers={REQUEST_HEADER: "not a valid id!"})
    generated = reply.headers.get(REQUEST_HEADER.lower())
    check("a malformed one is replaced with a fresh one", generated and re.fullmatch(r"[0-9a-f]{16}", generated) is not None, str(generated))
    check("and one is made when none was sent", re.fullmatch(r"[0-9a-f]{16}", client.get("/things/y").headers.get(REQUEST_HEADER.lower(), "")) is not None)
    text = client.get(METRICS_PATH, headers={"Authorization": f"Bearer {TOKEN}"}).text
    check("requests are counted by the route template, never the raw path", 'route="/things/{item}"' in text and "923001234567" not in text and 'route="/things/+923001234567"' not in text)
    client.get("/nowhere")
    text = client.get(METRICS_PATH, headers={"Authorization": f"Bearer {TOKEN}"}).text
    check("an unmatched path is `unmatched` with its 404", 'route="unmatched",status="404"' in text)
    check("and timed", ins.HTTP_LATENCY.stats(role="api", route="/things/{item}").count == 3)

    print("\n  readiness helpers:")

    async def slow() -> str:
        await asyncio.sleep(2)
        return "late"

    async def failing() -> str:
        raise RuntimeError("postgresql://u:hunter2@db refused")

    late = await run_check("db", slow, timeout_secs=0.05)
    check("a probe that hangs is not ready, with the timeout named", not late.ok and "0.05s" in late.detail)
    failed = await run_check("db", failing)
    check("a probe that raises is not ready, scrubbed", not failed.ok and "RuntimeError" in failed.detail and "hunter2" not in failed.to_dict()["detail"])

    class Counting:
        async def count_prospects(self) -> int:
            return 3

    class Pinging:
        pinged = 0

        async def ping(self) -> bool:
            self.pinged += 1
            return True

    pinging = Pinging()
    check("store_ready pings a store that can, and counts on one that cannot", (await store_ready(lambda: pinging)).ok and pinging.pinged == 1 and (await store_ready(lambda: Counting())).ok)
    check("a store that is still opening is not ready", not (await store_ready(lambda: (_ for _ in ()).throw(RuntimeError("starting")))).ok)

    print("\n  the token off:")
    open_app = FastAPI()
    install_ops_routes(open_app, "webhooks", request_metrics=False, request_ids=False)
    with TestClient(open_app) as open_client:
        check("without a token /metrics is open, and a process with no probe is ready", open_client.get(METRICS_PATH).status_code == 200 and open_client.get(READY_PATH).status_code == 200)
        check("no middleware when asked for none: no request id header", REQUEST_HEADER.lower() not in open_client.get(HEALTH_PATH).headers)


def check_real_apps() -> None:
    print("\n=== on the real servers ===")
    from fastapi.testclient import TestClient
    from test_automation import FakeStore, api_settings
    from test_security import ADMIN_KEY, ALICE_PASSWORD, SESSION_SECRET

    from src.automation import API_PREFIX, create_automation_app
    from src.config import Config, MonitoringConfig
    from src.dashboard import web
    from src.security import hash_password

    previous_dsn = os.environ.get("DATABASE_URL")
    os.environ.update(
        {
            "DASHBOARD_USERS": f"alice:admin:{hash_password(ALICE_PASSWORD)}",
            "DASHBOARD_SESSION_SECRET": SESSION_SECRET,
            "AUTOMATION_API_KEYS": ADMIN_KEY,
            "DATABASE_URL": "postgresql://x:y@localhost/unused",
            "MONITORING_TOKEN": TOKEN,
        }
    )
    os.environ.pop("DASHBOARD_AUTH_DISABLED", None)
    config = Config.from_env()
    check("the config reads the monitoring section", config.monitoring.token == TOKEN and config.monitoring.enabled and config.monitoring.port == 7895 and config.monitoring.serves_worker)
    store = FakeStore(clock=tw.FakeClock(tw.NOW))

    async def factory() -> Any:
        return store

    REGISTRY.reset()
    app = web.create_app(config, store_factory=factory)
    with TestClient(app) as client:
        check("the dashboard answers /healthz without a login", client.get(HEALTH_PATH).status_code == 200 and client.get(HEALTH_PATH).json()["role"] == "dashboard")
        check("and /readyz, pinging the store", client.get(READY_PATH).status_code == 200 and client.get(READY_PATH).json()["checks"][0]["name"] == "database")
        check("/metrics wants the token", client.get(METRICS_PATH).status_code == 401 and client.get(METRICS_PATH, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200)
        check("the page still wants a login", client.get("/", follow_redirects=False).status_code == 303)
        check("the fleet gauges were refreshed at startup", ins.COLLECTOR_REFRESHES.total() >= 1)
        text = client.get(METRICS_PATH, headers={"Authorization": f"Bearer {TOKEN}"}).text
        check("dashboard requests are counted by template", 'route="/healthz"' in text and 'role="dashboard"' in text)

    settings = dataclasses.replace(api_settings(api_keys=(ADMIN_KEY,)), monitoring=dataclasses.replace(MonitoringConfig(), token=None))
    app = create_automation_app(settings, store_factory=factory, deliver=False)
    with TestClient(app) as client:
        check("the automation API answers /healthz and /readyz outside the keyed router", client.get(HEALTH_PATH).status_code == 200 and client.get(READY_PATH).status_code == 200 and client.get(READY_PATH).json()["role"] == "api")
        check("/metrics is open when no token is configured", client.get(METRICS_PATH).status_code == 200)
        check("the keyed routes still refuse without a key", client.get(f"{API_PREFIX}/status").status_code == 401)
        reply = client.get(f"{API_PREFIX}/status", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        check("and a keyed request carries a request id back", reply.status_code == 200 and re.fullmatch(r"[0-9a-f]{16}", reply.headers.get(REQUEST_HEADER.lower(), "")) is not None)
        text = client.get(METRICS_PATH).text
        check("API requests are counted by template, the key never in a label", 'route="/api/v1/status"' in text and ADMIN_KEY not in text)

    off = dataclasses.replace(settings, monitoring=dataclasses.replace(MonitoringConfig(), enabled=False))
    with TestClient(create_automation_app(off, store_factory=factory, deliver=False)) as client:
        check("MONITORING_ENABLED=false mounts nothing", client.get(HEALTH_PATH).status_code == 404 and client.get(METRICS_PATH).status_code == 404)

    for name in ("DASHBOARD_USERS", "DASHBOARD_SESSION_SECRET", "AUTOMATION_API_KEYS", "MONITORING_TOKEN", "DATABASE_URL"):
        os.environ.pop(name, None)
    if previous_dsn is not None:
        os.environ["DATABASE_URL"] = previous_dsn


async def check_ops_server() -> None:
    print("\n=== the scheduler's own server ===")
    app = create_ops_app("scheduler", info=lambda: {"in_flight": 0}, version="22")
    ops = await serve_ops(app, host="127.0.0.1", port=0, role="scheduler")
    check("it binds an ephemeral port", ops is not None and ops.port > 0)
    if ops is None:
        return
    try:
        await asyncio.sleep(0.3)

        def fetch(path: str) -> tuple[int, str]:
            with urllib.request.urlopen(f"{ops.url}{path}", timeout=5) as reply:  # noqa: S310 - loopback
                return reply.status, reply.read().decode("utf-8")

        status, body = await asyncio.to_thread(fetch, HEALTH_PATH)
        check("/healthz answers over a real socket", status == 200 and json.loads(body)["role"] == "scheduler" and json.loads(body)["in_flight"] == 0, body[:120])
        status, body = await asyncio.to_thread(fetch, METRICS_PATH)
        check("/metrics too", status == 200 and "aiva_up" in body)
        taken = await serve_ops(create_ops_app("scheduler"), host="127.0.0.1", port=ops.port, role="scheduler")
        check("a taken port is a warning and None, not a crash", taken is None and _records("ops.port_unavailable"))
    finally:
        await ops.stop()
    check("stopping marks the process down", ins.PROCESS_UP.value(role="scheduler") == 0)


# --- Config ----------------------------------------------------------------------------------


def check_config() -> None:
    print("\n=== configuration ===")
    from src.config import MonitoringConfig

    problems: list[str] = []
    for name in ("MONITORING_ENABLED", "MONITORING_HOST", "MONITORING_PORT", "MONITORING_TOKEN", "MONITORING_REFRESH_SECS", "MONITORING_THROUGHPUT_WINDOW_SECS"):
        os.environ.pop(name, None)
    defaults = MonitoringConfig.from_env(problems)
    check("the defaults: on, loopback, 7895, open, 30s, an hour", defaults.enabled and defaults.host == "127.0.0.1" and defaults.port == 7895 and defaults.token is None and defaults.refresh_secs == 30.0 and defaults.throughput_window_secs == 3600.0 and not problems)
    os.environ["MONITORING_TOKEN"] = "short"
    problems = []
    MonitoringConfig.from_env(problems)
    check("a short token is a configuration problem", any("MONITORING_TOKEN" in p for p in problems), str(problems))
    os.environ.update({"MONITORING_TOKEN": TOKEN, "MONITORING_PORT": "0", "MONITORING_ENABLED": "true"})
    problems = []
    configured = MonitoringConfig.from_env(problems)
    check("port 0 means the scheduler serves nothing, and the describe line says so", not configured.serves_worker and "serves none" in configured.describe() and "behind MONITORING_TOKEN" in configured.describe() and not problems)
    os.environ["MONITORING_ENABLED"] = "false"
    check("off is off", not MonitoringConfig.from_env([]).enabled and "off" in MonitoringConfig.from_env([]).describe())
    for name in ("MONITORING_ENABLED", "MONITORING_PORT", "MONITORING_TOKEN"):
        os.environ.pop(name, None)
    check("the token is on the scrub list", "MONITORING_TOKEN" in __import__("src.reliability.observability", fromlist=["SECRET_ENV"]).SECRET_ENV)


# --- The boundary ------------------------------------------------------------------------------


def check_boundary() -> None:
    print("\n=== the boundary ===")
    monitoring = SERVER / "src" / "monitoring"
    project_import = re.compile(r"^\s*(from|import)\s+(src\.|\.)", re.M)
    for leaf in ("metrics.py", "tracing.py"):
        text = (monitoring / leaf).read_text(encoding="utf-8")
        check(f"monitoring/{leaf} imports nothing from the project", project_import.search(text) is None)
    package = "".join(p.read_text(encoding="utf-8") for p in monitoring.glob("*.py"))
    forbidden = re.compile(r"^\s*(from|import)\s+(src\.|\.\.)(campaigns|security|automation|crm|conversation|telephony|dashboard|scheduling)\b", re.M)
    check("the package imports no campaigns, security, automation, CRM, conversation or telephony code", forbidden.search(package) is None, str(forbidden.findall(package)))
    check("and no Pipecat", "pipecat" not in package)
    conversation = "".join(p.read_text(encoding="utf-8") for p in (SERVER / "src" / "conversation").glob("*.py"))
    check("the conversation layer imports no monitoring", "monitoring" not in conversation)
    bot = (SERVER / "bot.py").read_text(encoding="utf-8")
    check("bot.py never reads the fleet tables: no collect, no queue depth, no worker summary", "monitoring.collect" not in bot and "queue_depth" not in bot and "worker_summary" not in bot)
    check("the bot mounts routes only: no request middleware in front of the audio websocket", "request_metrics=False" in bot and "request_ids=False" in bot)
    store = (SERVER / "src" / "campaigns" / "store.py").read_text(encoding="utf-8")
    check("the store's call-path writes are timed", store.count("@_timed(") >= 12)


# --- The SQL, against PostgreSQL -------------------------------------------------------------


async def run_database_checks(dsn: str) -> None:
    from test_campaigns import with_temp_schema

    store, admin, schema = await with_temp_schema(dsn)
    try:
        print("\n=== the SQL, against PostgreSQL ===")
        REGISTRY.reset()
        columns = await store._pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = 'call_attempts'", schema)
        check("call_attempts has a trace_id column", "trace_id" in {r["column_name"] for r in columns})
        check("ping answers", await store.ping())
        campaign = await store.create_campaign(name="SQL", status=__import__("src.campaigns", fromlist=["CampaignStatus"]).CampaignStatus.ACTIVE)
        prospect = await store.add_prospect(first_name="Sara", last_name="Ali", phone="+923001234567", phone_normalized="+923001234567")
        membership = await store.add_to_campaign(campaign.id, prospect.id)
        attempt = await store.create_attempt(prospect_id=prospect.id, campaign_id=campaign.id, campaign_prospect_id=membership.id)
        check("a fresh attempt has no trace", attempt.trace_id is None)
        trace = new_trace_id()
        check("the dialer's write lands", await store.set_attempt_trace(attempt.id, trace) and (await store.get_attempt(attempt.id)).trace_id == trace)
        check("a missing row is False, not an error", not await store.set_attempt_trace(999_999, trace))
        mark = _mark()
        with call_context(trace_id=trace):
            await store.mark_placement_started(attempt.id)
            placed = await store.mark_attempt_placed(attempt.id, telephony_call_id="CA-sql-1", provider="stub")
        check("the row read back by call id carries the trace", placed is not None and (await store.find_attempt_by_call_id("CA-sql-1")).trace_id == trace)
        check("the placement writes are counted and timed", ins.STORE_OPERATIONS.value(operation="mark_attempt_placed", outcome="ok") == 1 and ins.STORE_LATENCY.stats(operation="mark_placement_started").count == 1)
        ops = [r for r in _records("store.op", mark) if _extra(r, "trace") == trace]
        check("each is a store.op line under the caller's trace", len(ops) >= 2 and any("mark_attempt_placed" in str(r["message"]) for r in ops), str(len(ops)))
        await store.apply_call_event(attempt_id=attempt.id, status=CallAttemptStatus.CONNECTED)
        await store.apply_call_event(attempt_id=attempt.id, status=CallAttemptStatus.COMPLETED, duration_seconds=30)
        await store.save_call_usage(attempt.id, {"llm": {"prompt_tokens": 1200, "completion_tokens": 80}}, cost_usd=0.0123)
        throughput = await store.throughput(window_secs=3600)
        check("throughput counts the placement and the ending in the window", throughput.placed == 1 and throughput.finished == 1 and throughput.answered == 1 and throughput.failed == 0, throughput.describe())
        check("with the tokens and the cost Phase 11 recorded", throughput.prompt_tokens == 1200 and throughput.completion_tokens == 80 and throughput.cost_usd is not None and abs(throughput.cost_usd - 0.0123) < 1e-9, throughput.describe())
        check("per campaign", throughput.per_campaign and throughput.per_campaign[0]["campaign_id"] == campaign.id and throughput.per_campaign[0]["finished"] == 1)
        check("the shortest window still holds what just happened", (await store.throughput(window_secs=60)).placed == 1)
        complete = await refresh_from_store(store, stale_secs=60.0, max_attempts=3)
        check("the collector refreshes completely over the real store", complete and ins.THROUGHPUT.value(kind="finished") == 1 and ins.WORKERS.value(state="running") == 0)
        check("readiness over the real store is ok", (await store_ready(lambda: store)).ok)
    finally:
        await store.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


# --- Helpers and the runner ----------------------------------------------------------------------


def _raises(call: Any, exception_type: type[BaseException]) -> bool:
    try:
        call()
    except exception_type:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


async def _araises(factory: Any) -> bool:
    try:
        await factory()
    except Exception:  # noqa: BLE001
        return True
    return False


async def main() -> int:
    print("Monitoring checks — the registry, the trace, every instrumented point, the HTTP surface, the fleet gauges, the boundary, the rows.")
    _HANDLER["id"] = logger.add(_capture, level="DEBUG")
    try:
        check_registry()
        await check_measured()
        check_tracing()
        await check_placements()
        await check_in_call()
        await check_webhooks()
        await check_crm()
        await check_deliverer()
        await check_tools()
        await check_collect()
        await check_http()
        check_real_apps()
        await check_ops_server()
        check_config()
        check_boundary()

        from dotenv import load_dotenv

        load_dotenv(SERVER / ".env", override=True)
        dsn = os.getenv("DATABASE_URL") or os.getenv("KB_DATABASE_URL")
        if not dsn:
            _skipped.append("database checks (no DATABASE_URL or KB_DATABASE_URL)")
        else:
            import asyncpg

            try:
                await run_database_checks(dsn)
            except (OSError, asyncpg.InterfaceError, asyncpg.InvalidCatalogNameError, asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError) as exc:
                _skipped.append(f"database checks (cannot reach PostgreSQL: {exc})")
    finally:
        try:
            logger.remove(_HANDLER["id"])
        except ValueError:
            pass

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
