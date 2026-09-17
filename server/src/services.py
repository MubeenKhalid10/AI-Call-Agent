"""Factories for the three swappable pipeline stages: STT, LLM, TTS.

This module exists so that changing a provider is a `.env` change plus one entry
here, and never an edit to `bot.py`. Pipecat's pipeline already treats these as
interchangeable objects; these factories just make the seam explicit and keep the
credential handling in one place.

Adding an LLM provider is two steps: add it to `DEFAULT_MODELS` and `_LLM_KEY_ENV`
in `config.py`, then add it to `_LLM_SERVICES` below.

**All three stages stream.** That is not incidental — it is the whole reason the
agent can answer in well under a second:

* STT streams audio up a websocket and partial transcripts back down, so the
  transcript is essentially ready the moment the caller stops talking.
* The LLM streams tokens, so TTS can start on the first clause instead of
  waiting for the full reply.
* TTS streams audio down a websocket, so the first syllable plays while the rest
  is still being generated.

A single non-streaming stage (`CartesiaHttpTTSService`, an LLM with
`stream=False`) would serialise the whole chain and cost roughly a second.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging

from loguru import logger
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.utils.types import NOT_GIVEN

from .config import Config, ConfigError
from .prompts import SYSTEM_PROMPT
from .tts_fallback import TTSFallbackSwitcher

# Imported lazily: every provider below ships an OpenAI-compatible service class,
# but importing all of them at startup costs time and pulls in SDKs you are not
# using. Maps provider name -> (module path, class name).
_LLM_SERVICES = {
    "groq": ("pipecat.services.groq.llm", "GroqLLMService"),
    "gemini": ("pipecat.services.google.llm", "GoogleLLMService"),
    "anthropic": ("pipecat.services.anthropic.llm", "AnthropicLLMService"),
    "openai": ("pipecat.services.openai.llm", "OpenAILLMService"),
    "cerebras": ("pipecat.services.cerebras.llm", "CerebrasLLMService"),
    "openrouter": ("pipecat.services.openrouter.llm", "OpenRouterLLMService"),
    "mistral": ("pipecat.services.mistral.llm", "MistralLLMService"),
    "ollama": ("pipecat.services.ollama.llm", "OLLamaLLMService"),
}


def _or_not_given(value):
    """Pass a value through, or NOT_GIVEN when it is None.

    Pipecat settings distinguish "leave the provider's default alone"
    (`NOT_GIVEN`) from "explicitly set this to null" (`None`). Unset tuning knobs
    must map to the former, so that not configuring a threshold means Deepgram's
    tuned default rather than a value we made up.
    """
    return NOT_GIVEN if value is None else value


def make_stt(config: Config):
    """Build the speech-to-text service.

    Two providers, and the choice between them *is* the turn-taking design:

    ``deepgram_flux`` (default) runs Deepgram's conversational model, which
    returns transcripts and end-of-turn decisions on the same websocket. It
    judges from the words and the prosody, not from silence alone, so it does not
    cut in on a mid-sentence pause and does not sit waiting after an obviously
    finished question. Pipecat wires this up on its own: the service recommends
    `ExternalUserTurnStrategies`, and the context aggregator adopts that
    recommendation unless we override it — which is why `turns.py` deliberately
    passes no strategies on this path.

    ``deepgram`` is the classic streaming endpoint. Transcription only; turn
    detection falls back to local Silero VAD plus the Smart Turn v3 analyser.
    Keep it as the fallback for accounts without Flux access, and as the A/B
    baseline when measuring what Flux is worth.
    """
    if config.stt_provider == "deepgram_flux":
        return DeepgramFluxSTTService(
            api_key=config.stt_api_key,
            settings=DeepgramFluxSTTService.Settings(
                model=config.stt_model,
                eot_threshold=_or_not_given(config.flux_eot_threshold),
                eot_timeout_ms=_or_not_given(config.flux_eot_timeout_ms),
                eager_eot_threshold=_or_not_given(config.flux_eager_eot_threshold),
            ),
            # Flux owns interruption on this path: when it hears the caller start
            # a turn, the bot stops. This is the barge-in switch.
            should_interrupt=True,
        )

    if config.stt_provider == "deepgram":
        return DeepgramSTTService(
            api_key=config.stt_api_key,
            settings=DeepgramSTTService.Settings(
                model=config.stt_model,
                # Interim results are what let the turn-start strategies react to
                # the first word rather than to the finalised utterance.
                interim_results=True,
                punctuate=True,
                smart_format=True,
            ),
        )

    raise ConfigError(f"No STT factory for provider '{config.stt_provider}'.")


def warm_up_llm_module(config: Config) -> None:
    """Import the configured LLM service's module now, not on the first call. Phase 12.

    `make_llm` imports the provider's module lazily so that unused SDKs cost
    nothing. Measured on a phone call: that first import — Pipecat's Groq
    service plus the OpenAI SDK underneath it — took 4.3 s, *after* the person
    had picked up. Doing it once at startup moves that wait to the operator's
    terminal, where it belongs. A provider that cannot be imported is reported
    the same way `make_llm` would report it, at the first session.
    """
    target = _LLM_SERVICES.get(config.llm_provider)
    if target is None:
        return
    try:
        importlib.import_module(target[0])
    except ImportError as exc:  # pragma: no cover - reported again by make_llm
        logger.warning(f"Could not pre-load {target[0]}: {exc}")


def make_llm(config: Config, system_instruction: str | None = None):
    """Build the LLM service, carrying the system prompt.

    Every supported provider exposes the same `Settings(model, system_instruction)`
    shape, so one construction path covers all of them. Pipecat's LLM services
    stream by default; nothing here turns that off.

    Args:
        config: Which provider, which model, which key.
        system_instruction: Overrides `prompts.SYSTEM_PROMPT`. Phase 6 passes
            the sales instruction here, composed for the specific person being
            called. It is a construction argument rather than something written
            later because identity is durable: the agent is the same agent for
            the whole call, and rewriting the system prompt mid-conversation to
            get one different sentence is the wrong tool for that job — a turn
            instruction is the right one.
    """
    target = _LLM_SERVICES.get(config.llm_provider)
    if target is None:
        raise ConfigError(f"No LLM factory for provider '{config.llm_provider}'.")

    module_path, class_name = target
    try:
        service_cls = getattr(importlib.import_module(module_path), class_name)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(
            f"LLM_PROVIDER={config.llm_provider}, but {class_name} could not be imported.\n"
            f'  Try:  uv add "pipecat-ai[{config.llm_provider}]" && uv sync'
        ) from exc

    settings_fields = {
        "model": config.llm_model,
        "system_instruction": system_instruction or SYSTEM_PROMPT,
    }
    # Cap the reply. Every provider's Settings spells the field one of two ways,
    # and whichever this one has is set — a cap is never silently dropped,
    # because the provider that needs it most (Groq, see `Config`) is the one
    # whose absence of a cap is fatal. See `_output_cap_field`.
    cap_field = _output_cap_field(service_cls.Settings)
    if cap_field:
        settings_fields[cap_field] = config.llm_max_output_tokens
    else:
        logger.warning(
            f"{class_name}.Settings has no max-tokens field; LLM_MAX_OUTPUT_TOKENS is not applied"
        )

    extra = reasoning_extra(config)
    if extra:
        settings_fields["extra"] = extra
        logger.info(f"LLM: {config.llm_model} is a reasoning model; requesting {extra}")

    kwargs = {"settings": service_cls.Settings(**settings_fields)}
    # Ollama runs locally and takes no credential.
    if config.llm_api_key is not None:
        kwargs["api_key"] = config.llm_api_key

    service = service_cls(**kwargs)
    _apply_request_timeout(service, config.reliability.llm_stall_secs)
    _forward_provider_retries()
    return service


# Model families on Groq that reason before they answer. With tools advertised
# the provider's default streams that reasoning into the answer channel — as
# ordinary sentences, not tagged — and the TTS speaks it. Verified 2026-09-10
# with a direct request: "Let me record this question as it seems unrelated…"
# arrived as content; with `reasoning_format: hidden` only the answer did.
_REASONING_MODEL_MARKERS = ("qwen3", "qwen-3", "gpt-oss", "deepseek-r1", "minimax", "qwq")


def reasoning_extra(config: Config) -> dict[str, dict[str, object]]:
    """The request extras that keep a reasoning model's thinking out of the answer.

    Only for Groq and Cerebras, and only for a model that reasons: the
    parameters are the providers' own, and a model that does not reason may
    refuse them. `LLM_REASONING_FORMAT=off` sends nothing (the provider's
    default), for a model or an account where the parameter is not accepted.

    Groq takes `reasoning_format` (hidden / parsed). Cerebras keeps the
    reasoning in its own response field already, so nothing leaks into the
    spoken answer; the problem there is the budget: verified 2026-09-16 with a
    direct request, qwen-3.8-27b spent the whole `max_tokens` thinking and
    returned no content. Cerebras's switch is `disable_reasoning: true`, and
    "parsed" (keep the reasoning, apart from the answer) is its default.
    """
    if config.llm_reasoning_format == "off":
        return {}
    model = (config.llm_model or "").lower()
    if not any(marker in model for marker in _REASONING_MODEL_MARKERS):
        return {}
    # The SDK takes provider-specific parameters through `extra_body`, not as
    # keyword arguments of its own; Pipecat merges `Settings.extra` into the
    # `create()` call as given.
    if config.llm_provider == "groq":
        return {"extra_body": {"reasoning_format": config.llm_reasoning_format}}
    if config.llm_provider == "cerebras" and config.llm_reasoning_format == "hidden":
        return {"extra_body": {"disable_reasoning": True}}
    return {}


class _RetryToLog(logging.Handler):
    """Surface the OpenAI SDK's silent retries as a warning on the bot's log."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102 - stdlib signature
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never raise from a log handler
            return
        if "Retrying" in message:
            logger.warning(
                f"LLM | the provider refused the request and the SDK is waiting to retry: {message} "
                "— a rate limit on the free tier, usually; the caller hears silence meanwhile"
            )


def _forward_provider_retries() -> None:
    """Make the SDK's retry waits visible. Phase 26.

    **The failure this explains.** On the free Groq tier the second request of
    a tool turn is often refused with 429 and a `retry-after` of 20–50 s. The
    OpenAI SDK honours it and retries — silently, at INFO on a stdlib logger
    nothing here listened to — so the call log showed a turn that took a
    minute with no line saying why, and the caller heard silence. This does
    not change the wait; it names it, on the same log as the turn.
    """
    sdk = logging.getLogger("openai._base_client")
    if any(isinstance(handler, _RetryToLog) for handler in sdk.handlers):
        return
    sdk.addHandler(_RetryToLog())
    if sdk.level == logging.NOTSET or sdk.level > logging.INFO:
        sdk.setLevel(logging.INFO)


def _apply_request_timeout(service, stall_secs: float) -> None:
    """Bound how long one LLM request may hang. Phase 9.

    **The failure this prevents.** Pipecat builds its OpenAI-compatible client
    with the SDK's default timeout, which is ten minutes, and `create_client`
    does not pass `**kwargs` through to it — verified in the installed source —
    so there is no constructor argument for this. A provider that accepts a
    request and then stops responding therefore hangs the turn for ten minutes,
    during which the caller hears nothing and the pipeline looks healthy.

    The client is replaced through the SDK's own public `with_options`, which
    returns a configured copy. The attribute it is stored on is private to
    Pipecat, so this is guarded: if a future version renames it, the timeout is
    not applied and the log says so — and `SessionSupervisor` still notices the
    stall from the frames, which is why this is a second line of defence rather
    than the only one.

    The timeout is set slightly *below* the supervisor's stall threshold, so the
    request is abandoned by the HTTP layer first and the supervisor's much
    blunter response — ending the call — is only reached when that did not
    work.
    """
    if stall_secs <= 0:
        return
    timeout = max(5.0, stall_secs * 0.8)
    client = getattr(service, "_client", None)
    if client is None or not hasattr(client, "with_options"):
        logger.debug(
            f"{type(service).__name__} has no OpenAI-style client; the {timeout:.0f}s request "
            f"timeout was not applied (the session supervisor still watches for a stall)"
        )
        return
    try:
        service._client = client.with_options(timeout=timeout)  # noqa: SLF001 - see docstring
    except Exception:  # noqa: BLE001 - never let this stop a session starting
        logger.warning(
            f"Could not apply a {timeout:.0f}s LLM request timeout; relying on the session "
            f"supervisor to notice a stall"
        )


def _output_cap_field(settings_cls: type) -> str | None:
    """Which field, if any, caps output tokens on this provider's `Settings`.

    OpenAI-compatible services (Groq among them) carry both `max_tokens` and the
    newer `max_completion_tokens`; Anthropic carries `max_tokens` only. The
    newer name is preferred where it exists because the older one is deprecated
    on OpenAI's API and some OpenAI-compatible providers warn about it.
    """
    if dataclasses.is_dataclass(settings_cls):
        # Pipecat's Settings classes are dataclasses, and a dataclass's fields
        # include the ones it inherited — `__annotations__` would not.
        names = {f.name for f in dataclasses.fields(settings_cls)}
    elif getattr(settings_cls, "model_fields", None):
        names = set(settings_cls.model_fields)  # A pydantic model.
    else:
        names = {
            name for klass in settings_cls.__mro__ for name in getattr(klass, "__annotations__", {})
        }
    for candidate in ("max_completion_tokens", "max_tokens"):
        if candidate in names:
            return candidate
    return None


def make_tts(config: Config):
    """Build the text-to-speech service.

    `text_aggregation_mode` is the one real latency lever here. In SENTENCE mode
    (the default) the service buffers LLM tokens until it sees a sentence
    boundary, which gives the vendor a whole clause to work with and the best
    prosody, at the cost of waiting for that boundary. In TOKEN mode tokens go
    straight down the websocket, which removes that wait but hands the vendor
    fragments to guess intonation from. Sentence mode is the default because the
    latency it costs is small next to what it buys in how the agent sounds; set
    `TTS_STREAM_TOKENS=true` to measure the trade for yourself.

    Phase 30: with `TTS_FALLBACK_ENABLED=true` the service returned is a
    `TTSFallbackSwitcher` holding the configured provider and the fallback
    provider — the same two service objects this factory builds on their own,
    behind Pipecat's `ServiceSwitcher`. The pipeline puts it where the TTS
    service goes and notices nothing; see `src/tts_fallback.py` for when it
    moves. Off (the default), or with `TTS_PROVIDER` naming the provider you
    want directly, this returns that one service exactly as before.
    """
    primary = _make_tts_service(config, config.tts_provider, config.tts_api_key)
    if not config.tts_fallback_enabled:
        return primary
    fallback = _make_tts_service(config, config.tts_fallback_provider, config.tts_fallback_api_key)
    return TTSFallbackSwitcher(primary, fallback)


def _make_tts_service(config: Config, provider: str, api_key: str | None):
    """One TTS service for `provider`, with the settings the config holds for it."""
    mode = TextAggregationMode.TOKEN if config.tts_stream_tokens else TextAggregationMode.SENTENCE

    if provider == "cartesia":
        return CartesiaTTSService(
            api_key=api_key,
            settings=CartesiaTTSService.Settings(voice=config.cartesia_voice_id),
            text_aggregation_mode=mode,
        )

    if provider == "deepgram":
        return DeepgramTTSService(api_key=api_key, text_aggregation_mode=mode)

    if provider == "elevenlabs":
        if not config.elevenlabs_voice_id:
            raise ConfigError(
                "ELEVENLABS_VOICE_ID is not set. Copy a voice ID from the ElevenLabs voice library."
            )
        return ElevenLabsTTSService(
            api_key=api_key,
            settings=ElevenLabsTTSService.Settings(
                voice=config.elevenlabs_voice_id,
                model=config.elevenlabs_model,
            ),
            text_aggregation_mode=mode,
        )

    raise ConfigError(f"No TTS factory for provider '{provider}'.")
