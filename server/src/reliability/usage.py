"""What one call consumed, and what that cost. Phase 11.

`metrics.py` measures how *fast* a call was; this measures how *much* it used.
Pipecat has been emitting the numbers since Phase 2 — `enable_usage_metrics` has
been on in `bot.py` all along — and until now nothing read them. Every LLM
request reports its prompt and completion tokens, every TTS request its
characters, every STT service its audio seconds. Summing them per call is the
whole of this module.

**Units are measured; prices are configured; nothing is invented.** A token
count is a fact the provider reported. A price is a commercial arrangement this
code cannot know, changes without notice, and differs per account — so a rate
that is not configured produces *no* cost, not a guessed one. `usage` is always
present; `cost_usd` is `None` until somebody fills in what they actually pay.
That is the same rule Phase 6 applied to unknown fields and Phase 8 to a missing
table: an absent number is reported as absent.

**What it is for.** Two questions that could not be answered before: "what does a
call cost us" and "which stage is spending it". On the free Groq tier the answer
to the second is the prompt — twelve tool schemas are 40% of every request — and
having the number per call is what turns that from a memory of one afternoon's
debugging into something a dashboard shows.

The observer costs one dictionary update per metrics frame and holds no
references to anything, so it is safe to leave on in production. It is on by
default for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    STTUsageMetricsData,
    TTSUsageMetricsData,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed


@dataclass
class ModelUsage:
    """What one model was asked to do, and how much of it.

    Per model rather than per stage, because a session can legitimately use two
    — a different LLM after a provider swap mid-deployment, or an STT fallback —
    and a single total would hide that.
    """

    model: str
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    characters: int = 0
    audio_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Plain data, omitting the counters that stayed at zero.

        A TTS entry has no tokens and an LLM entry has no characters; writing
        both as zeros would make every record look like it measured something
        it never had.
        """
        values = {
            "model": self.model,
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "characters": self.characters,
            "audio_seconds": round(self.audio_seconds, 1),
        }
        return {
            name: value
            for name, value in values.items()
            if name == "model" or value
        }


@dataclass
class CallUsage:
    """Everything one call consumed, by stage.

    Attributes:
        llm / tts / stt: One `ModelUsage` per model seen in that stage.
        telephony_seconds: The call's own connected duration, supplied by the
            caller rather than measured here — the pipeline does not know it.
    """

    llm: dict[str, ModelUsage] = field(default_factory=dict)
    tts: dict[str, ModelUsage] = field(default_factory=dict)
    stt: dict[str, ModelUsage] = field(default_factory=dict)
    telephony_seconds: float | None = None

    @property
    def prompt_tokens(self) -> int:
        """Every prompt token this call sent."""
        return sum(entry.prompt_tokens for entry in self.llm.values())

    @property
    def completion_tokens(self) -> int:
        """Every completion token this call received."""
        return sum(entry.completion_tokens for entry in self.llm.values())

    @property
    def llm_requests(self) -> int:
        """How many inferences the call made.

        Worth watching on its own: a *tool turn is two requests* — the call and
        the reply to its result — so this is roughly twice the number of spoken
        turns on a call that used tools, and that doubling is what exhausts a
        per-minute token budget.
        """
        return sum(entry.requests for entry in self.llm.values())

    @property
    def tts_characters(self) -> int:
        """Every character the agent spoke."""
        return sum(entry.characters for entry in self.tts.values())

    @property
    def stt_seconds(self) -> float:
        """Audio submitted for transcription, in seconds."""
        return sum(entry.audio_seconds for entry in self.stt.values())

    @property
    def is_empty(self) -> bool:
        """Whether nothing was measured — a call that never reached a provider."""
        return not (self.llm or self.tts or self.stt)

    def to_dict(self) -> dict[str, Any]:
        """The usage as plain data, for the call record and the JSON API.

        Each stage carries `reported`, which is **not** the same as its total
        being zero. Not every service tells Pipecat what it used: in 1.8.1
        Cartesia's TTS reports characters and Deepgram's *websocket* TTS does
        not (only its HTTP variant does), so a call on Deepgram TTS has a real
        character count that nobody measured. Recording that as `0` would make
        an unmeasured stage look free, and a cost total built on it would be
        quietly too low — which is the one thing a cost report must not be.
        """
        return {
            "llm": {
                "reported": bool(self.llm),
                "requests": self.llm_requests,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "models": [entry.to_dict() for entry in self.llm.values()],
            },
            "tts": {
                "reported": bool(self.tts),
                "characters": self.tts_characters,
                "models": [entry.to_dict() for entry in self.tts.values()],
            },
            "stt": {
                "reported": bool(self.stt),
                "audio_seconds": round(self.stt_seconds, 1),
                "models": [entry.to_dict() for entry in self.stt.values()],
            },
            "telephony_seconds": (
                round(self.telephony_seconds, 1) if self.telephony_seconds is not None else None
            ),
        }

    def describe(self) -> str:
        """One line for the end-of-session summary."""
        if self.is_empty:
            return "nothing measured"
        parts = [
            f"{self.llm_requests} LLM request(s)",
            f"{self.prompt_tokens:,} prompt + {self.completion_tokens:,} completion tokens",
        ]
        if self.tts:
            parts.append(f"{self.tts_characters:,} TTS characters")
        else:
            # Named rather than left out, because "no TTS line" and "TTS used
            # nothing" look identical in a summary and mean opposite things.
            parts.append("TTS reported no usage")
        if self.stt_seconds:
            parts.append(f"{self.stt_seconds:.0f}s of audio transcribed")
        if self.llm_requests:
            parts.append(f"{self.prompt_tokens // self.llm_requests:,} prompt tokens/request")
        return " | ".join(parts)


@dataclass(frozen=True)
class CostRates:
    """What this account actually pays. Every rate optional, none guessed.

    Unset rates are the normal state: this project's default stack is a free
    Groq tier, a Deepgram trial credit and a Cartesia free tier, where the
    honest per-call cost is "nothing, until the tier runs out". Filling these in
    is a statement about a specific billing arrangement, so it is left to the
    person who has one.

    Attributes:
        llm_input_per_mtok / llm_output_per_mtok: Dollars per million tokens.
        tts_per_mchar: Dollars per million characters.
        stt_per_minute / telephony_per_minute: Dollars per minute.
    """

    llm_input_per_mtok: float | None = None
    llm_output_per_mtok: float | None = None
    tts_per_mchar: float | None = None
    stt_per_minute: float | None = None
    telephony_per_minute: float | None = None

    @property
    def configured(self) -> bool:
        """Whether any rate is set at all."""
        return any(
            rate is not None
            for rate in (
                self.llm_input_per_mtok,
                self.llm_output_per_mtok,
                self.tts_per_mchar,
                self.stt_per_minute,
                self.telephony_per_minute,
            )
        )

    def describe(self) -> str:
        """One line for the startup log."""
        if not self.configured:
            return "no rates configured — usage is measured, cost is not estimated"
        parts = []
        if self.llm_input_per_mtok is not None or self.llm_output_per_mtok is not None:
            parts.append(
                f"LLM ${self.llm_input_per_mtok or 0:g}/${self.llm_output_per_mtok or 0:g} per Mtok"
            )
        if self.tts_per_mchar is not None:
            parts.append(f"TTS ${self.tts_per_mchar:g}/Mchar")
        if self.stt_per_minute is not None:
            parts.append(f"STT ${self.stt_per_minute:g}/min")
        if self.telephony_per_minute is not None:
            parts.append(f"calls ${self.telephony_per_minute:g}/min")
        return ", ".join(parts)


def estimate_cost(usage: CallUsage, rates: CostRates) -> dict[str, Any] | None:
    """What this call cost, from measured units and configured rates.

    A stage is priced only when it has **both** a configured rate and a
    provider that actually reported its usage. A service that reports nothing —
    Deepgram's websocket TTS, in Pipecat 1.8.1 — is left out and named in
    `unmeasured`, rather than being multiplied by its rate as though it had
    used nothing. A total quietly missing a stage is worse than a total that
    says which stage is missing.

    Returns:
        `{"total_usd": ..., "priced": [...], "unmeasured": [...]}` with a line
        per priced stage, or `None` when no rate is configured at all.
    """
    if not rates.configured:
        return None

    lines: dict[str, float] = {}
    unmeasured: list[str] = []

    if rates.llm_input_per_mtok is not None:
        if usage.llm:
            lines["llm_input"] = usage.prompt_tokens / 1_000_000 * rates.llm_input_per_mtok
        else:
            unmeasured.append("llm_input")
    if rates.llm_output_per_mtok is not None:
        if usage.llm:
            lines["llm_output"] = usage.completion_tokens / 1_000_000 * rates.llm_output_per_mtok
        else:
            unmeasured.append("llm_output")
    if rates.tts_per_mchar is not None:
        if usage.tts:
            lines["tts"] = usage.tts_characters / 1_000_000 * rates.tts_per_mchar
        else:
            unmeasured.append("tts")
    if rates.stt_per_minute is not None:
        if usage.stt:
            lines["stt"] = usage.stt_seconds / 60 * rates.stt_per_minute
        else:
            unmeasured.append("stt")
    if rates.telephony_per_minute is not None:
        if usage.telephony_seconds is not None:
            lines["telephony"] = usage.telephony_seconds / 60 * rates.telephony_per_minute
        else:
            unmeasured.append("telephony")

    if not lines:
        return None
    breakdown: dict[str, Any] = {name: round(value, 6) for name, value in lines.items()}
    breakdown["total_usd"] = round(sum(lines.values()), 6)
    # Said plainly on the record: a total that omits a stage is not the whole
    # cost, and a reader six months from now will not remember which rates were
    # set on the day or which provider reported nothing.
    breakdown["priced"] = sorted(lines)
    breakdown["unmeasured"] = sorted(unmeasured)
    breakdown["complete"] = not unmeasured
    return breakdown


class UsageObserver(BaseObserver):
    """Sums the usage Pipecat already reports, for one session.

    Reads `MetricsFrame`, which every service pushes when `enable_usage_metrics`
    is on. De-duplicates on `frame.id` for the reason written up in
    `diagnostics.py`: an observer sees each frame once per processor *hop*, so
    a metrics frame crossing a nine-stage pipeline would otherwise be counted
    nine times — which for a token count is not a cosmetic error but a bill that
    reads nine times too high.
    """

    def __init__(self, *, history: int = 400) -> None:
        """Create the observer."""
        super().__init__()
        self.usage = CallUsage()
        self._seen: set[int] = set()
        self._order: list[int] = []
        self._history = history

    async def on_push_frame(self, data: FramePushed) -> None:
        """Add one metrics frame's numbers to the session's totals."""
        frame = data.frame
        if not isinstance(frame, MetricsFrame) or self._already_seen(frame.id):
            return
        for entry in frame.data:
            try:
                self._record(entry)
            except Exception:  # noqa: BLE001 - accounting must never break a call
                logger.debug("USAGE | could not read a metrics entry", exc_info=True)

    def _record(self, entry: Any) -> None:
        """Fold one metrics entry into the right stage."""
        model = entry.model or entry.processor

        if isinstance(entry, LLMUsageMetricsData):
            usage = _entry(self.usage.llm, model)
            usage.requests += 1
            usage.prompt_tokens += entry.value.prompt_tokens or 0
            usage.completion_tokens += entry.value.completion_tokens or 0
            usage.cached_tokens += entry.value.cache_read_input_tokens or 0
        elif isinstance(entry, TTSUsageMetricsData):
            usage = _entry(self.usage.tts, model)
            usage.requests += 1
            usage.characters += int(entry.value or 0)
        elif isinstance(entry, STTUsageMetricsData):
            usage = _entry(self.usage.stt, model)
            usage.requests += 1
            usage.audio_seconds += float(entry.value.audio_seconds or 0.0)

    def _already_seen(self, frame_id: int) -> bool:
        if frame_id in self._seen:
            return True
        if len(self._order) >= self._history:
            self._seen.discard(self._order.pop(0))
        self._order.append(frame_id)
        self._seen.add(frame_id)
        return False


def _entry(bucket: dict[str, ModelUsage], model: str) -> ModelUsage:
    """The bucket's entry for one model, created on first sight."""
    if model not in bucket:
        bucket[model] = ModelUsage(model=model)
    return bucket[model]


__all__ = ["CallUsage", "CostRates", "ModelUsage", "UsageObserver", "estimate_cost"]
