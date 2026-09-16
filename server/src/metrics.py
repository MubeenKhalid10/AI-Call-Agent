"""Latency measurement for a live session.

Phase 2 asks for a number, not a feeling. This module turns Pipecat's metrics
frames into one log line per response plus a summary at the end of the session,
so "does it feel fast" becomes "p50 was 1.08s, and turn detection owns half of
it".

The measurement is built on Pipecat's own `UserBotLatencyObserver`, which is the
only thing in the stack that knows when the caller *actually* stopped speaking:
it takes the VAD stop timestamp and subtracts `stop_secs`, the silence the VAD
had to hear before it was willing to call it. Timing from the raw frame instead
would flatter every number by that amount.

**What the numbers mean.** All of them start from that same real-silence moment
except `llm` and `tts`, which start when their own request goes out:

- `total` — silence to first audio out of the bot. The only one the caller
  experiences; everything else exists to explain it.
- `turn-end` — silence to the turn being released downstream, which is what
  triggers the LLM. This is the number turn-taking config moves.
- `stt` — silence to the final transcript.
- `llm` — request to first token.
- `tts` — request to first audio byte.

`stt` and `turn-end` measure overlapping windows from the same start, so they do
not add. On the Flux path they come out nearly identical, because Flux delivers
the transcript and the end-of-turn decision in the same message. On the fallback
path they separate, and the gap between them *is* the cost of local turn
detection: `stt` is when the words were ready, `turn-end` adds however long Smart
Turn took to agree the caller had finished.

`total` is roughly `turn-end + llm + tts`, since those three do run in sequence —
but only roughly, because TTS starts on the first clause while the LLM is still
generating.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger
from pipecat.observers.user_bot_latency_observer import (
    LatencyBreakdown,
    UserBotLatencyObserver,
)

from .monitoring.instruments import GREETING_LATENCY, TURN_LATENCY
from .reliability.observability import event

# Maps a Pipecat processor name (e.g. "GroqLLMService#0") onto a stage. Checked
# in order, so the first match wins.
_STAGES = (("stt", "STT"), ("llm", "LLM"), ("tts", "TTS"))


@dataclass
class _Series:
    """A named series of latency samples, in seconds."""

    samples: list[float] = field(default_factory=list)

    def add(self, value: float) -> None:
        """Record one sample."""
        self.samples.append(value)

    def describe(self) -> str:
        """Render p50/p95/min/max in milliseconds, or a placeholder when empty."""
        if not self.samples:
            return "no samples"
        ordered = sorted(self.samples)
        return (
            f"p50 {_ms(_percentile(ordered, 0.50))}  "
            f"p95 {_ms(_percentile(ordered, 0.95))}  "
            f"min {_ms(ordered[0])}  "
            f"max {_ms(ordered[-1])}  "
            f"n={len(ordered)}"
        )

    def to_dict(self) -> dict[str, int | None]:
        """The same four figures as whole milliseconds, for the call report. Phase 12."""
        if not self.samples:
            return {"p50_ms": None, "p95_ms": None, "min_ms": None, "max_ms": None, "n": 0}
        ordered = sorted(self.samples)
        return {
            "p50_ms": _whole_ms(_percentile(ordered, 0.50)),
            "p95_ms": _whole_ms(_percentile(ordered, 0.95)),
            "min_ms": _whole_ms(ordered[0]),
            "max_ms": _whole_ms(ordered[-1]),
            "n": len(ordered),
        }


class LatencyReporter:
    """Logs per-response latency and an end-of-session summary.

    Attach `reporter.observer` to the `PipelineWorker`, call `start()` before the
    session runs and `log_summary()` when it ends.

    Requires `enable_metrics=True` in `PipelineParams`; without it the services
    emit no TTFB and every breakdown comes back empty.
    """

    def __init__(self, *, log_each_turn: bool = True) -> None:
        """Create the reporter and wire up the underlying Pipecat observer.

        Args:
            log_each_turn: Whether to log a line per response. The end-of-session
                summary is always logged.
        """
        self._log_each_turn = log_each_turn
        self._responses = 0
        self._pending_total: float | None = None
        self._greeting_latency: float | None = None
        self._errors: list[str] = []
        self._sink_id: int | None = None
        self._series: dict[str, _Series] = {
            name: _Series() for name in ("total", "turn-end", "stt", "llm", "tts")
        }
        # Phase 12: one record per response, in the order they happened, so
        # the call report can show the timeline and not only the percentiles.
        self.records: list[dict[str, int | None]] = []

        self.observer = UserBotLatencyObserver()
        self.observer.add_event_handler("on_latency_measured", self._on_latency_measured)
        self.observer.add_event_handler("on_latency_breakdown", self._on_latency_breakdown)
        self.observer.add_event_handler(
            "on_first_bot_speech_latency", self._on_first_bot_speech_latency
        )

    def start(self) -> None:
        """Begin counting errors for this session.

        Counting is done with a loguru sink rather than by subscribing to error
        frames, because the things that actually break a voice agent are wider
        than `ErrorFrame`. An exception raised inside an event handler is caught
        by the framework, logged, and swallowed — the session carries on looking
        healthy while some piece of it silently no longer runs. That is exactly
        how a bad handler signature survived the first eval run here. A sink
        catches every one of those, plus vendor errors and error frames, for
        about fifteen lines.

        The sink is process-wide, so a dev runner hosting several sessions at
        once would have each of them counting all the others' errors. That is
        acceptable for one bot per process, which is how this runs.
        """
        if self._sink_id is None:
            self._sink_id = logger.add(self._capture_error, level="ERROR")

    def record_error(self, description: str) -> None:
        """Record an error so the session summary can report it."""
        self._errors.append(description)

    def log_summary(self) -> None:
        """Log the end-of-session latency summary and stop counting errors."""
        if self._sink_id is not None:
            logger.remove(self._sink_id)
            self._sink_id = None

        if self._greeting_latency is not None:
            logger.info(
                f"LATENCY | greeting (connect to first audio): {_ms(self._greeting_latency)}"
            )

        if not self._responses:
            logger.info("LATENCY | no responses measured this session")
        else:
            logger.info(f"LATENCY SUMMARY | {self._responses} responses")
            for name, series in self._series.items():
                logger.info(f"LATENCY SUMMARY |   {name:<9} {series.describe()}")

        if self._errors:
            logger.warning(f"ERRORS | {len(self._errors)} this session")
            for description in self._errors:
                logger.warning(f"ERRORS |   {description}")
        else:
            logger.info("ERRORS | none")

    def _capture_error(self, message) -> None:
        """Loguru sink: record one ERROR-or-worse log record."""
        record = message.record
        self._errors.append(f"{record['name']}: {record['message']}")

    # --- Observer callbacks -------------------------------------------------
    #
    # `on_latency_measured` always fires just before `on_latency_breakdown` for
    # the same response, so the total is stashed and both are logged as one line.

    async def _on_latency_measured(self, observer, latency_seconds: float) -> None:
        self._pending_total = latency_seconds

    async def _on_first_bot_speech_latency(self, observer, latency_seconds: float) -> None:
        self._greeting_latency = latency_seconds
        GREETING_LATENCY.observe(latency_seconds)

    async def _on_latency_breakdown(self, observer, breakdown: LatencyBreakdown) -> None:
        total = self._pending_total
        self._pending_total = None

        stages = _first_ttfb_per_stage(breakdown)
        if breakdown.user_turn_secs is not None:
            stages["turn-end"] = breakdown.user_turn_secs

        if total is None:
            # The greeting: no user turn preceded it, so there is no response
            # latency to record. It is reported separately in the summary.
            return

        self._responses += 1
        self._series["total"].add(total)
        # Phase 22: the same samples into the process's histogram, so p50 and
        # p95 exist across every session and not only in one session's summary.
        TURN_LATENCY.observe(total, stage="total")
        for name, value in stages.items():
            self._series[name].add(value)
            TURN_LATENCY.observe(value, stage=name.replace("-", "_"))

        record = {
            "response": self._responses,
            "total_ms": _whole_ms(total),
            "turn_end_ms": _whole_ms(stages.get("turn-end")),
            "stt_ms": _whole_ms(stages.get("stt")),
            "llm_first_token_ms": _whole_ms(stages.get("llm")),
            "tts_first_audio_ms": _whole_ms(stages.get("tts")),
        }
        self.records.append(record)

        if not self._log_each_turn:
            return

        # Phase 12: the same numbers as `k=v` fields, so the line can be
        # grepped for one stage across a whole run (`turn_end_ms=`) and lands
        # as keys under LOG_FORMAT=json. The names say what each one measures.
        logger.info(
            "LATENCY | "
            + event(
                "turn.latency",
                response=self._responses,
                total_ms=record["total_ms"],
                turn_end_ms=record["turn_end_ms"],
                stt_ms=record["stt_ms"],
                llm_first_token_ms=record["llm_first_token_ms"],
                tts_first_audio_ms=record["tts_first_audio_ms"],
            )
        )

    def summary(self) -> dict[str, object]:
        """The session's latency as plain data, for the call report. Phase 12.

        The same figures `log_summary` prints: the greeting, p50/p95/min/max
        per stage over every response, the per-response records, and the
        errors the sink caught.
        """
        return {
            "responses": self._responses,
            "greeting_ms": _whole_ms(self._greeting_latency),
            "stages": {name: series.to_dict() for name, series in self._series.items()},
            "per_response": list(self.records),
            "errors": list(self._errors),
        }


def _first_ttfb_per_stage(breakdown: LatencyBreakdown) -> dict[str, float]:
    """Pick the earliest TTFB per stage from a breakdown.

    A response can produce several TTFB samples per service — one per sentence
    the TTS synthesises, for instance. Only the first of each contributed to the
    caller's wait for the first audio, so later ones are dropped rather than
    averaged into a number that describes nothing.
    """
    earliest: dict[str, tuple[float, float]] = {}
    for entry in sorted(breakdown.ttfb, key=lambda t: t.start_time):
        stage = _stage_of(entry.processor)
        if stage is not None and stage not in earliest:
            earliest[stage] = (entry.start_time, entry.duration_secs)
    return {stage: duration for stage, (_, duration) in earliest.items()}


def _stage_of(processor: str) -> str | None:
    """Map a processor name onto a pipeline stage, or None if it is not one."""
    for stage, marker in _STAGES:
        if marker in processor:
            return stage
    return None


def _percentile(ordered: list[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted list."""
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _ms(seconds: float) -> str:
    """Format seconds as whole milliseconds."""
    return f"{seconds * 1000:.0f}ms"


def _whole_ms(seconds: float | None) -> int | None:
    """Seconds as a whole number of milliseconds, or None."""
    return None if seconds is None else int(round(seconds * 1000))
