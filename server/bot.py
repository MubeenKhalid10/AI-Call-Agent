#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Ai-Voice-Agent — Phase 6 cold-calling sales agent, on the phone.

A cascade pipeline: caller -> STT -> retrieval -> guidance -> LLM -> TTS ->
caller. Every stage streams, turn-taking is decided from speech rather than from
a silence timer, the caller can interrupt at any moment, every response is
measured, every answer is grounded in a knowledge base rather than in whatever
the model happens to remember — and the agent knows who it is calling, runs a
discovery conversation with them, and records what it learned.

**The same pipeline serves three transports**, which is the whole point of
keeping this file to wiring:

* ``webrtc`` — a browser at http://localhost:7860/client. How this is developed.
* ``twilio`` and the other carriers — a real phone call, audio arriving over a
  websocket. Added in Phase 4; see `src/telephony/`.
* ``eval`` — the headless harness in `evals/`, which drives the real pipeline
  with synthesised speech.

Nothing below the transport knows which one is in play. The differences that do
exist — a phone call has an identity, and a dropped phone call is final where a
dropped browser tab is not — are handled in the handlers, and explained in
`src/telephony/session.py`.

This file is wiring. The decisions live next door:

* `src/services.py`   — which STT / LLM / TTS, and that each one streams.
* `src/turns.py`      — when a turn starts and stops; VAD; barge-in.
* `src/metrics.py`    — per-response latency and the end-of-session summary.
* `src/resilience.py` — silence and dropped connections.
* `src/diagnostics.py`— turn-cycle tracing, errors, barge-in logging.
* `src/retrieval.py`  — what the agent is allowed to know, fetched per turn.
* `src/conversation/` — the sales call: its ten states, what it learned, who it
  is calling, and every word the model is told. `SALES_MODE=false` takes the
  whole layer out and leaves the Phase 3 assistant.
* `src/actions/`      — what the agent can *do* on a call (Phase 7): search the
  knowledge base, check and book a calendar, schedule a callback, transfer to a
  person. The backend behind the tools; the conversation only sees a Protocol.
* `src/scheduling/`   — the calendar behind `book_meeting`: a local business-
  hours calendar, or Cal.com.
* `src/knowledge_store.py` / `src/embeddings.py` / `src/documents.py` — the
  knowledge base underneath it. Load documents with `ingest.py`.
* `src/telephony/`    — placing outbound calls and knowing what happened to
  them. `call.py` is its command line.
* `src/voice_quality.py` — Phase 12: whether each turn got a response, how
  fast an interruption stopped the bot, which interruptions were noise, and
  the per-call report that `tests/live_call.py` reads afterwards.
* `src/voicemail.py`  — Phase 12: noticing that a machine answered, and
  hanging up or leaving a message.

Run from the `server/` directory, not the repository root::

    cd server
    uv run ingest.py init                   # once
    uv run ingest.py add evals/kb           # load some documents
    uv run bot.py

Then open http://localhost:7860/client, or place a real call::

    ngrok http 7860                         # in another terminal
    uv run call.py +923001234567            # once TELEPHONY_* is set in .env
"""

import asyncio
import sys
from contextlib import ExitStack

from dotenv import load_dotenv
from loguru import logger
from pipecat.evals.transport import EvalTransportParams
from pipecat.frames.frames import BotStoppedSpeakingFrame, EndWorkerFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    UserTurnStoppedMessage,
)
from pipecat.runner.types import RunnerArguments, WebSocketRunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.utils.types import NOT_GIVEN
from pipecat.workers.runner import WorkerRunner

from src.actions import ActionService, open_actions
from src.campaigns.briefing import Briefing, open_briefing
from src.config import Config, ConfigError, SalesConfig
from src.conversation import (
    AuditContext,
    CampaignBrief,
    ConversationDirector,
    ProspectBrief,
    SalesConversation,
    resolve_brief,
)
from src.diagnostics import TurnDiagnostics
from src.embeddings import shared_embedder
from src.knowledge_store import KnowledgeStore
from src.latency import LatencyTracker
from src.metrics import LatencyReporter
from src.monitoring.http import Readiness, ReadyCheck, install_ops_routes
from src.monitoring.instruments import (
    CALL_COST,
    CALL_DURATION,
    COST_TOTAL,
    LLM_REQUESTS,
    LLM_TOKENS,
    LLM_TOKENS_PER_CALL,
    SESSION_ENDINGS,
    SESSIONS,
    SESSIONS_ACTIVE,
    STT_AUDIO_SECONDS,
    TTS_CHARACTERS,
)
from src.monitoring.tracing import new_trace_id, trace_from_runner_args
from src.prompts import GREETING_INSTRUCTION, NOISE_RESUME_INSTRUCTION
from src.reliability import (
    CallContext,
    CallUsage,
    CostRates,
    Reason,
    SessionSupervisor,
    Status,
    UsageObserver,
    call_context,
    check_health,
    configure_logging,
    describe_policies,
    estimate_cost,
    event,
)
from src.reliability.observability import current_trace_id
from src.resilience import ConnectionGuard, PeerWatchdog, SilenceHandler, prompt_agent
from src.retrieval import KnowledgeRetriever
from src.services import make_llm, make_stt, make_tts, warm_up_llm_module
from src.tts_fallback import TTSFallbackSwitcher
from src.spoken_text import SpeechObserver, SpeechTally, SpokenTextFilter
from src.telephony import TELEPHONY_TRANSPORTS, CallSession, make_provider, stream_url
from src.telephony.transport import create_provider_transport
from src.turns import make_user_aggregator_params, mark_interrupted_reply
from src.voice_quality import TurnMonitor, write_call_report
from src.voicemail import (
    DEFAULT_VOICEMAIL_PHRASES,
    VoicemailDetector,
    VoicemailHandler,
    VoicemailVerdict,
)

load_dotenv(override=True)
# Phase 9: one sink, every credential scrubbed from every record, and the call
# ids bound by `call_context` rendered on each line. `LOG_FORMAT=json` switches
# the whole process to machine-readable output. Must run after `load_dotenv`,
# because the scrubber reads the secrets it has to hide from the environment.
# Phase 22: `component` names this process on every JSON line.
configure_logging(component="bot")


def _load_config() -> Config:
    """Resolve config, or exit with a readable message.

    Validating here means a missing key stops the process in under a second with
    a message naming the variable, instead of failing mid-call as a vendor auth
    error that is much harder to trace back to its cause.
    """
    try:
        return Config.from_env()
    except ConfigError as exc:
        print(f"\nCannot start the bot.\n\n{exc}\n", file=sys.stderr)
        raise SystemExit(1) from exc


CONFIG = _load_config()


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    """Run one voice session.

    Args:
        transport: Transport for this session, built by `create_transport`.
        runner_args: Runner session arguments (request body, session id).
    """
    logger.info(f"Starting session | {CONFIG.describe()}")
    logger.info(CONFIG.describe_turn_taking())
    logger.info(CONFIG.describe_echo())
    logger.info(CONFIG.describe_voice_quality())

    # `None` for a browser or eval session, a `CallSession` for a phone call.
    # Everything downstream treats it as "am I on a call" rather than comparing
    # transport names, and the three places it changes behaviour are marked.
    call = CallSession.from_runner_args(runner_args)
    if call is not None:
        logger.info(f"CALL | {call.describe()}")

    # Phase 22: the correlation id. An outbound campaign call carries the one
    # the dialer made on its handshake, so this process's first line already
    # matches the scheduler's; anything else — a browser, an eval, an inbound
    # call — gets one of its own so its lines still share an id.
    trace_id = trace_from_runner_args(runner_args) or new_trace_id()
    transport_name = call.provider if call is not None else str(getattr(runner_args, "transport_type", None) or "unknown")
    SESSIONS.inc(transport=transport_name)
    SESSIONS_ACTIVE.inc(transport=transport_name)

    # Phase 9: every log line for the rest of this session carries the call's
    # ids. The prospect and attempt ids are not known yet — they arrive with the
    # brief a few lines below — so the context is opened again there with them.
    try:
        with call_context(
            CallContext(
                call_id=call.call_id if call else None,
                provider=call.provider if call else None,
                trace_id=trace_id,
                extra={"session": getattr(runner_args, "session_id", None)},
            )
        ):
            await _run_session(transport, runner_args, call)
    finally:
        SESSIONS_ACTIVE.dec(transport=transport_name)


async def _run_session(
    transport: BaseTransport, runner_args: RunnerArguments, call: CallSession | None
) -> None:
    """One session, with the call's ids already bound to the log. See `run_bot`."""
    # Phase 9: the prospect and attempt ids are not known until the brief is
    # resolved, a few lines below, but they should be on every log line after
    # that. An `ExitStack` lets a context be opened partway through and still be
    # closed exactly once, when the session ends.
    with ExitStack() as log_context:
        await _run_pipeline(transport, runner_args, call, log_context)


async def _run_pipeline(
    transport: BaseTransport,
    runner_args: RunnerArguments,
    call: CallSession | None,
    log_context: ExitStack,
) -> None:
    """Build and run the pipeline for one session. See `run_bot`."""

    # The knowledge base, if this bot has one. `store` is kept so the pool can be
    # closed when the session ends; `retriever` is the pipeline stage.
    store, retriever = await _make_knowledge_stage()

    # Phase 11: when the knowledge base and the campaign tables are the same
    # database — the default, since `DATABASE_URL` falls back to
    # `KB_DATABASE_URL` — the campaign store borrows this pool instead of
    # opening a second one. Measured: six connections per call became three,
    # which is what caps how many calls can run at once against PostgreSQL's
    # default hundred. `None` when they are separate databases, and the campaign
    # store then opens its own as before.
    shared_pool = store.pool if (store is not None and CONFIG.shares_database) else None

    # Who this call is to, and the conversation that will be had with them.
    # `briefing` owns a connection to the prospect database and is closed with
    # the session; `conversation` is Phase 6's state machine, or None when
    # SALES_MODE is off and this is the Phase 3 assistant; `actions` is Phase
    # 7's backend for the tools that act — calendar, callbacks, transfer — and
    # owns the clients it opened.
    briefing, conversation, actions = await _make_conversation(
        runner_args, retriever=retriever, call=call, pool=shared_pool
    )
    if conversation is not None:
        logger.info(f"CONVERSATION | calling {conversation.brief.describe()}")
        # Phase 9: now that the brief is resolved, the prospect and attempt ids
        # are known, so bind them for the rest of the session. Nesting adds to
        # the context `run_bot` opened rather than replacing it.
        log_context.enter_context(
            call_context(
                CallContext(
                    prospect_id=conversation.brief.prospect_id,
                    attempt_id=conversation.brief.call_attempt_id,
                    campaign_id=conversation.brief.campaign_id,
                )
            )
        )

    stt = make_stt(CONFIG)
    llm = make_llm(
        CONFIG,
        system_instruction=conversation.system_instruction() if conversation else None,
    )
    tts = make_tts(CONFIG)

    # Tools are advertised on the context, which registers their handlers with
    # the LLM service by itself — there is no separate registration step. They
    # are what makes conversation state something the model states explicitly
    # rather than something inferred from its prose; see `conversation/tools.py`.
    # Phase 31. Every tool's handler is registered on the service once, here,
    # explicitly — Pipecat never prunes an explicit registration — and the
    # context advertises only the stage's tools, as handler-less schemas (none
    # before the prospect has spoken). The director refreshes the advertised
    # set on every inference and the tool wrapper after every tool call, so
    # each request describes the tools the moment can use and a handler is
    # there for any tool the model calls, advertised or not.
    # Phase 32. What the model says in each response, as the spoken-text
    # filter lets it through, and how many tool calls the response made: a
    # recording tool reads it to skip the second LLM request when the reply
    # has already been spoken (`tools.TURN_RELEASING`).
    speech = SpeechTally()
    # Phase 26: only the answer reaches the voice — never the model's
    # reasoning, tool markup or a sentence with no words in it.
    spoken_text = SpokenTextFilter()
    if conversation is not None:
        conversation.speech = speech
        for schema in conversation.tools():
            llm.register_function(schema.name, schema.handler)
    context = LLMContext(tools=conversation.advertised_tools() if conversation else NOT_GIVEN)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=make_user_aggregator_params(CONFIG),
    )

    # Attaches this turn's stage guidance to a copy of the context, and runs the
    # deterministic detectors before the model sees the turn. After the
    # retriever, because the retriever builds its search query from the last
    # user message and a guidance block appended first would become that query.
    director = ConversationDirector(conversation) if conversation else None

    # The context aggregators are what give the session its memory: the user
    # aggregator appends each finished user turn and the assistant aggregator
    # appends each finished reply, so the LLM sees the whole conversation.
    #
    # The assistant aggregator sits *after* `transport.output()` on purpose. It
    # records what the caller actually heard, so an interrupted reply is stored
    # truncated at the point it was cut off — which is what the caller
    # experienced, and what the agent must not later assume it finished saying.
    #
    # The retriever sits between the user aggregator and the LLM: the aggregator
    # has just written the finished user turn into the context, and the LLM has
    # not seen it yet, so this is the one point in the pipeline where the whole
    # conversation is assembled and still editable. It searches the knowledge
    # base for what the caller just said and hands the LLM a copy of the context
    # with the passages attached. See `src/retrieval.py` for why it is a
    # pipeline stage and not a tool the model calls.
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            *([retriever] if retriever else []),
            *([director] if director else []),
            llm,
            spoken_text,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    reporter = LatencyReporter(log_each_turn=CONFIG.log_metrics)
    reporter.start()
    diagnostics = TurnDiagnostics()
    # Phase 11. Pipecat has reported token, character and audio-second usage
    # since `enable_usage_metrics` was switched on in Phase 2; nothing read it
    # until now. Summing it costs a dictionary update per metrics frame, so it
    # stays on in production.
    usage = UsageObserver()
    # Phase 12. Whether each caller turn got a response, how fast a barge-in
    # stopped the bot, and which interruptions carried no words. Observes
    # only; the latency numbers stay with `reporter`.
    monitor = TurnMonitor(
        response_timeout_secs=CONFIG.voice_quality.turn_response_timeout_secs
    )
    # Phase 29. Where each turn spent its time between the caller's speech
    # ending and the bot's first audio: STT finalisation, retrieval, the LLM's
    # first token and total, every tool, TTS first audio, and the gaps in
    # between. One line per turn and a summary per call; observes only.
    latency = LatencyTracker(log_each_turn=CONFIG.log_metrics)
    if retriever is not None:
        retriever.latency = latency

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            # Required for the latency numbers: without it no service emits TTFB
            # and every breakdown comes back empty.
            enable_metrics=True,
            enable_usage_metrics=True,
            # A frame that circulates on a timer. If it stops arriving, some
            # processor is wedged — which is otherwise indistinguishable from a
            # caller who has simply stopped talking.
            enable_heartbeats=True,
        ),
        observers=[
            diagnostics,
            reporter.observer,
            usage,
            monitor.observer,
            latency.observer,
            # Phase 32: what the model said in each response, read by the
            # recording tools to skip the second LLM request.
            SpeechObserver(speech, llm=llm, spoken_text=spoken_text),
        ],
        # Backstop for a session nobody is on any more: no speech in either
        # direction for this long ends it rather than leaking a live pipeline
        # (and a live STT websocket) forever.
        idle_timeout_secs=CONFIG.session_idle_timeout_secs or None,
    )

    # Phase 9. Watches for the failures that leave a caller listening to
    # silence: a service that has failed several times in a row, an inference
    # that starts and never finishes, and a call that has run past its ceiling.
    # It ends the session deliberately — through the same teardown as any other
    # ending, so the conversation record and the call result are still written.
    supervisor = SessionSupervisor(
        end_session=worker.stop_when_done,
        cancel_session=worker.cancel,
        say=lambda instruction: prompt_agent(worker, context, instruction),
        max_call_secs=CONFIG.reliability.max_call_secs,
        llm_stall_secs=CONFIG.reliability.llm_stall_secs,
        max_service_failures=CONFIG.reliability.max_service_failures,
        on_terminated=lambda reason, detail: _note_termination(conversation, reason, detail),
        # Phase 30: a failure the TTS fallback switcher answers for (by moving
        # to the fallback) is not a session failure; what the switcher pushes
        # out as its own still is.
        ignore_errors_from=tts.contains if isinstance(tts, TTSFallbackSwitcher) else None,
    )
    worker.add_observer(supervisor.observer)

    # Phase 12. Phone calls only: a person reading a voicemail greeting into a
    # browser microphone is the one false positive nobody needs. `None` on
    # every other transport, and when VOICEMAIL_DETECTION is off.
    voicemail = _make_voicemail_handler(worker, conversation, call)
    amd_provider = None

    silence = SilenceHandler(worker, context, CONFIG)
    connection = ConnectionGuard(
        worker,
        context,
        CONFIG,
        # Phone call difference 1 of 3: a dropped call does not come back.
        grace_secs=CONFIG.telephony.disconnect_grace_secs if call else None,
    )

    # A phone call's websocket closing *is* the disconnect signal, so telephony
    # needs no watchdog. WebRTC does: a browser that goes away without closing
    # reports nothing at all. See `PeerWatchdog`.
    watchdog = PeerWatchdog(
        getattr(runner_args, "webrtc_connection", None),
        connection,
        timeout_secs=0.0 if call else CONFIG.peer_timeout_secs,
    )

    # The idle escalation ends the call after its goodbye, and the only honest
    # signal that the goodbye was heard is the audio finishing. Ask the worker to
    # tell us when that frame reaches the end of the pipeline.
    worker.add_reached_downstream_filter((BotStoppedSpeakingFrame,))

    greeted = False

    async def greet_once() -> None:
        """Speak first. On a cold call the agent always opens.

        Which event this hangs off depends on the transport, and getting it
        wrong is silent. A browser and the eval harness are RTVI clients, and
        RTVI's `on_client_ready` is the right signal there: it fires when the
        client is actually ready to play audio, so the greeting is not spoken
        into a page that cannot hear it yet.

        Phone call difference 2 of 3: a carrier is not an RTVI client and will
        never send that message, so on a phone call `on_client_ready` never
        fires. Waiting for it there produces a bot that answers the phone and
        says nothing, forever — with no error anywhere. The media stream opening
        is the signal on that path.
        """
        nonlocal greeted
        if greeted:
            # A reconnecting client is ready again; `ConnectionGuard` handles
            # what to say in that case, and greeting twice would be jarring.
            return
        greeted = True
        # On a sales call the opening is composed from the brief — the
        # prospect's first name, the company we are calling for — so that the
        # first sentence is the one thing in the call that could not have been
        # written in advance. It falls back to the general-assistant greeting
        # when SALES_MODE is off.
        opening = conversation.opening() if conversation else GREETING_INSTRUCTION
        await prompt_agent(worker, context, opening)

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        await greet_once()

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        nonlocal amd_provider
        monitor.note_connected()
        if call is not None:
            call.on_connected()
        await connection.on_connected()
        # Armed only once a client is actually here, so a slow first connection
        # is never mistaken for one that has already gone.
        watchdog.start()
        if voicemail is not None:
            voicemail.on_connected()
            # Phase 12: when the call was placed with the carrier's own
            # detection on, its verdict lands on the call resource a few
            # seconds after the answer. A few cheap reads fetch it; no
            # webhook is needed. The provider is closed with the session.
            amd_provider = _watch_carrier_amd(voicemail, call)
        if call is not None:
            await greet_once()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        watchdog.stop()
        if call is not None:
            call.on_disconnected()
            # Phase 12: a structured line saying how the line closed, so a
            # call that dropped before the agent said a word is not filed with
            # the ones the caller hung up on at the end.
            _log_line_closed(call, monitor, conversation, supervisor, voicemail, silence)
        await connection.on_disconnected()

    @worker.event_handler("on_frame_reached_downstream")
    async def on_frame_reached_downstream(worker, frame):
        if isinstance(frame, BotStoppedSpeakingFrame):
            # Both wait for the same signal — the audio actually reaching the
            # caller — and both end the session when it is *their* closing line
            # that just finished. Neither acts on the other's.
            await silence.on_bot_stopped_speaking()
            await supervisor.on_bot_stopped_speaking()
            if voicemail is not None:
                await voicemail.on_bot_stopped_speaking()

    @worker.event_handler("on_heartbeat_timeout")
    async def on_heartbeat_timeout(worker):
        logger.warning("Pipeline heartbeat missed — a processor may be stalled")

    @worker.event_handler("on_idle_timeout")
    async def on_idle_timeout(worker):
        logger.info("No speech in either direction for a long time — ending session")

    @user_aggregator.event_handler("on_user_turn_started")
    async def on_user_turn_started(aggregator, strategy):
        silence.on_user_spoke()
        if voicemail is not None:
            # A recording talks over the greeting; a person waits for it. Only
            # a turn that began over the agent's audio can be judged by length.
            voicemail.on_user_turn_started(over_agent_audio=monitor.last_turn_over_agent)

    @user_aggregator.event_handler("on_user_turn_idle")
    async def on_user_turn_idle(aggregator):
        await silence.on_idle()

    @user_aggregator.event_handler("on_user_turn_stop_timeout")
    async def on_user_turn_stop_timeout(aggregator):
        # Phase 12: a turn that opened and never closed on its own — the
        # aggregator gave up waiting. Worth a structured line, because on a
        # phone it is how a stalled STT websocket first shows itself.
        monitor.note_stop_timeout()

    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message: UserTurnStoppedMessage):
        # The caller's side of the transcript is not recorded here but in the
        # director, from the context the model reads, so that the transcript
        # and the detectors see the same words. See `conversation/transcript.py`.
        timestamp = f"[{message.timestamp}] " if message.timestamp else ""
        logger.info(f"Transcript: {timestamp}user: {message.content}")
        content = message.content or ""
        monitor.note_user_turn_stopped(content)
        latency.note_user_turn_stopped(content)
        if call is not None and content.strip():
            call.note_caller_turn()
        if voicemail is not None:
            await voicemail.on_user_turn_stopped(content)
            if voicemail.active:
                return
        # Phase 12: the turn cut the bot off and carried no words — a cough, a
        # door, an echo. Nothing else will make the agent speak again until the
        # idle nudge, so ask it to pick up where it left off.
        if (
            CONFIG.voice_quality.noise_resume
            and monitor.noise_resumes < CONFIG.voice_quality.noise_resume_max
            and supervisor.terminated_by is None
            and not silence.closed_call
            and monitor.should_resume_after_noise(content)
        ):
            monitor.note_noise_resume()
            logger.info(
                event("turn.noise_resume", outcome=f"resume {monitor.noise_resumes}/{CONFIG.voice_quality.noise_resume_max}")
            )
            await prompt_agent(worker, context, NOISE_RESUME_INSTRUCTION)

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message: AssistantTurnStoppedMessage):
        timestamp = f"[{message.timestamp}] " if message.timestamp else ""
        suffix = " (interrupted)" if message.interrupted else ""
        logger.info(f"Transcript: {timestamp}assistant: {message.content}{suffix}")
        monitor.note_assistant_turn(message.content, interrupted=message.interrupted)
        if call is not None:
            call.note_agent_turn()
        if conversation is not None:
            # Phase 8: the words go into the transcript exactly as the caller
            # heard them — this event fires after `transport.output()`, so an
            # interrupted reply arrives already cut off, and is marked as such.
            conversation.note_agent_turn(message.content, interrupted=message.interrupted)
            if conversation.closing_line_needs_hangup(message.content, interrupted=message.interrupted):
                # Phase 26: the goodbye after a no has been spoken and the
                # model did not call `end_call` with it. The call ends here
                # rather than at the idle timeout — same frame, same graceful
                # path as the tool (`closing_line_needs_hangup` says when).
                conversation.begin_ending("closing line spoken")
                logger.info("CALL | closing line spoken after a no and the model left the call open; ending it")
                await worker.queue_frames([EndWorkerFrame()])
        if message.interrupted and message.content:
            # The aggregator has already written the half-finished sentence to
            # the context. Left bare it degrades every following reply — see
            # `mark_interrupted_reply`.
            mark_interrupted_reply(context)

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    supervisor.start()
    monitor.start()

    # Phase 12: everything above took seconds, and the carrier streamed the
    # caller's audio the whole time. Read and drop what queued up, or the STT
    # starts the call several seconds behind real time and the caller cannot
    # interrupt anything for the first half-minute. See `_drain_stale_audio`.
    if call is not None:
        await _drain_stale_audio(runner_args)

    try:
        await runner.run()
    except Exception:
        # A provider dropping mid-call should leave a traceable log line rather
        # than a silent dead connection.
        logger.exception("Session ended with an error")
        raise
    finally:
        supervisor.stop()
        monitor.stop()
        if voicemail is not None:
            voicemail.stop()
        if amd_provider is not None:
            await amd_provider.close()
        watchdog.stop()
        connection.close()
        await _release_webrtc(runner_args)
        # Phase 12: the call's own quality record — every turn, every
        # barge-in, every failure, the latency figures — assembled once and
        # used twice: stored with the conversation record, and written to
        # disk for the process that placed the call to read.
        quality = monitor.report(
            # Phase 29: the per-turn breakdown rides with the observer's
            # figures, under its own key, so every reader of `latency.stages`
            # sees what it always saw.
            latency={**reporter.summary(), "turns": latency.summary()},
            voicemail=voicemail.detector.to_dict() if voicemail is not None else None,
            extra=_report_identity(call, conversation, runner_args, supervisor, voicemail, silence),
        )
        if conversation is not None:
            # Writes the outcome through the sink, which is the only thing in
            # this file that reaches the campaign tables — and it reaches them
            # through a Protocol, not an import. Before the store is closed,
            # because the sink is using it. On a phone call the call's own
            # audio-connected duration goes with it (Phase 8): it is the number
            # the result reports, and the conversation's clock is the fallback.
            # Phase 11: what the call consumed goes with the outcome, so the
            # sink writes it onto the attempt in the same teardown that writes
            # the record. The telephony seconds are the bot's own view of the
            # call, which is the only per-minute number it has.
            if call is not None:
                usage.usage.telephony_seconds = call.duration_secs
            await conversation.finish(
                call_duration_secs=call.duration_secs if call is not None else None,
                usage=usage.usage.to_dict(),
                cost=estimate_cost(usage.usage, _cost_rates()),
                quality=quality,
            )
        if actions is not None:
            await actions.close()
        await briefing.close()
        if store is not None:
            await store.close()
        if diagnostics.barge_ins:
            logger.info(f"BARGE-IN | {diagnostics.barge_ins} interruptions this session")
        logger.info(f"QUALITY | {monitor.describe()}")
        if call is not None:
            write_call_report(CONFIG.voice_quality.call_report_dir, call.call_id, quality)
        reporter.log_summary()
        latency.log_summary()
        # Phase 9: how the session behaved, next to how fast it was. A line
        # here saying "stt x3" is the difference between a call that failed for
        # a reason and one that merely stopped.
        logger.info(f"SESSION | {supervisor.describe()}")
        if isinstance(tts, TTSFallbackSwitcher):
            logger.info(f"TTS FALLBACK | {tts.describe()}")
        # Phase 11: how much it used. The prompt-tokens-per-request figure at
        # the end is the one that matters on a per-minute token budget.
        logger.info(f"USAGE | {usage.usage.describe()}")
        priced = estimate_cost(usage.usage, _cost_rates())
        if priced:
            logger.info(
                f"COST | ${priced['total_usd']:.4f} this call "
                f"({', '.join(priced['priced'])} priced)"
            )
        # Phase 22: the same figures into the process's registry, so tokens,
        # cost, duration and how the session ended are scrapeable across
        # sessions and not only readable in one session's summary.
        _record_session_metrics(
            call, usage.usage, priced, _ended_by(conversation, supervisor, voicemail, silence, call)
        )
        if call is not None:
            # Phone call difference 3 of 3: the call has numbers of its own —
            # how long it lasted, how many turns each side took — that the
            # per-response latency summary does not describe.
            call.log_summary()


async def _drain_stale_audio(
    runner_args: RunnerArguments,
    *,
    max_secs: float = 3.0,
    window_secs: float = 0.1,
    realtime_frames_per_window: int = 8,
) -> int:
    """Discard the carrier audio that queued while the session was being set up. Phase 12.

    **The failure this fixes, measured on a simulated call.** The carrier
    opens the media stream the moment the call is answered and sends a 20 ms
    frame every 20 ms from then on. The bot spends several seconds after that
    building the session — pools, models, vendor websockets — and reads none of
    it, so the frames pile up in the socket. When the input transport finally
    starts reading it gets that pile in a burst and pushes it into Flux, which
    then runs *behind real time*: on the first drill of this phase the caller's
    first turn was detected 5.5 s after it began, the second 3.4 s, and every
    interruption landed on a bot that had already finished speaking.

    Nothing in the pile is worth keeping — it is the seconds before the agent
    had said anything — so it is read and dropped here, right before the
    pipeline starts. A backlog delivers dozens of frames per hundred
    milliseconds where real time delivers five, so the loop stops at the first
    window that looks like real time, or after `max_secs`.

    Only for a phone call, and only over the FastAPI websocket the telephony
    transport reads from; anything else returns at once. Control messages in
    the pile (`mark`, `dtmf`) are dropped with the audio; a `stop` in it means
    the caller already hung up, which the transport learns on its next read.

    Returns:
        How many messages were dropped.
    """
    websocket = getattr(runner_args, "websocket", None)
    if websocket is None or not hasattr(websocket, "receive_text"):
        return 0

    import time as _time

    dropped = 0
    windows = 0
    started = _time.monotonic()
    try:
        while _time.monotonic() - started < max_secs:
            window_end = _time.monotonic() + window_secs
            in_window = 0
            while True:
                remaining = window_end - _time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
                except TimeoutError:
                    break
                in_window += 1
            dropped += in_window
            windows += 1
            if in_window <= realtime_frames_per_window:
                break
    except Exception as exc:  # noqa: BLE001 - a closed socket is the transport's to report
        logger.debug(f"CALL | stopped draining the media stream: {exc.__class__.__name__}: {exc}")

    # Roughly one media frame per 20 ms; the control messages in the pile are few.
    backlog_ms = max(0, (dropped - windows * realtime_frames_per_window)) * 20
    if dropped:
        logger.info(
            event(
                "telephony.backlog_dropped",
                latency_ms=backlog_ms,
                outcome=f"{dropped} queued message(s) dropped in {_time.monotonic() - started:.2f}s",
            )
        )
    return dropped


def _make_voicemail_handler(
    worker: PipelineWorker, conversation: SalesConversation | None, call: CallSession | None
) -> VoicemailHandler | None:
    """Build the answering-machine handler for a phone call, or None. Phase 12.

    The detector's thresholds and the action come from `VOICEMAIL_*` in `.env`;
    the pipeline is reached through the same three callables the supervisor
    uses, so an ending decided here still runs the ordinary teardown and the
    call gets its record and its `VOICEMAIL` result.
    """
    settings = CONFIG.voice_quality
    if call is None or not settings.voicemail_enabled:
        return None

    detector = VoicemailDetector(
        enabled=True,
        phrases=settings.voicemail_phrases or DEFAULT_VOICEMAIL_PHRASES,
        max_greeting_secs=settings.voicemail_max_greeting_secs,
        window_secs=settings.voicemail_window_secs,
    )

    async def speak(text: str) -> None:
        # Fixed text straight to the TTS — no inference, so the message is the
        # one that was configured, word for word.
        await worker.queue_frames([TTSSpeakFrame(text)])

    def noted(verdict: VoicemailVerdict, action: str) -> None:
        if conversation is not None:
            conversation.note_voicemail(verdict.to_dict(), action)

    return VoicemailHandler(
        detector,
        action=settings.voicemail_action,
        message=settings.voicemail_message or _default_voicemail_message(),
        message_delay_secs=settings.voicemail_message_delay_secs,
        end_session=worker.stop_when_done,
        cancel_session=worker.cancel,
        speak=speak,
        on_detected=noted,
    )


def _default_voicemail_message() -> str:
    """The message left on a machine when `VOICEMAIL_MESSAGE` is not set."""
    sales = CONFIG.sales
    who = ""
    if sales.enabled and sales.agent_name:
        who = f"this is {sales.agent_name}"
        if sales.company_name:
            who += f" from {sales.company_name}"
        who += ". "
    return f"Hi, {who}Sorry I missed you. I'll try again another time. Goodbye."


def _watch_carrier_amd(voicemail: VoicemailHandler, call: CallSession | None):
    """Start polling the carrier for its answering-machine verdict, if there is one to poll.

    Returns the provider doing the reads, so the session can close it, or None
    when detection was not requested, the carrier cannot be asked, or the call
    has no id to ask about.
    """
    telephony = CONFIG.telephony
    if call is None or call.call_id is None:
        return None
    if not telephony.wants_machine_detection or not telephony.has_credentials:
        return None
    try:
        provider = make_provider(telephony, timeout_secs=CONFIG.reliability.carrier_timeout_secs)
    except ConfigError as exc:
        logger.warning(f"VOICEMAIL | cannot poll the carrier for answered_by: {exc}")
        return None
    call_id = call.call_id

    async def fetch():
        return (await provider.fetch_call(call_id)).answered_by

    voicemail.watch_carrier(
        fetch, poll_secs=telephony.amd_poll_secs, window_secs=telephony.amd_window_secs
    )
    return provider


def _ended_by(
    conversation: SalesConversation | None,
    supervisor: SessionSupervisor,
    voicemail: VoicemailHandler | None,
    silence: SilenceHandler,
    call: CallSession | None,
) -> str:
    """One word for who ended the session, for the report and the line-closed log."""
    if voicemail is not None and voicemail.active:
        return "voicemail"
    if supervisor.terminated_by is not None:
        return f"supervisor:{supervisor.terminated_by.value}"
    if conversation is not None and conversation.end_requested:
        return "agent"
    if silence.closed_call:
        return "idle"
    if call is not None and call.duration_secs > 0:
        return "caller"
    return "unknown"


def _log_line_closed(
    call: CallSession,
    monitor: TurnMonitor,
    conversation: SalesConversation | None,
    supervisor: SessionSupervisor,
    voicemail: VoicemailHandler | None,
    silence: SilenceHandler,
) -> None:
    """A structured line for how a phone call's media stream closed. Phase 12.

    The carrier closing the websocket looks the same whether the person hung
    up after a goodbye, the line dropped mid-sentence, or the call connected
    and died before the agent said a word. These are different failures, and
    only the last two are failures, so the line names which.
    """
    ended_by = _ended_by(conversation, supervisor, voicemail, silence, call)
    fields = {
        "call": call.call_id,
        "provider": call.provider,
        "latency_ms": int(call.duration_secs * 1000),
        "outcome": ended_by,
    }
    if ended_by in ("agent", "voicemail", "idle") or ended_by.startswith("supervisor"):
        logger.info(event("telephony.line_closed", **fields))
        return
    if not monitor.greeting_at:
        logger.warning(
            event(
                "telephony.failure",
                **fields,
                error="the line closed before the agent said anything",
            )
        )
    elif monitor.bot_speaking or any(t.stopped_at and not t.responded_at and not t.failed for t in monitor.turns):
        logger.warning(
            event("telephony.failure", **fields, error="the line closed mid-turn")
        )
    else:
        logger.info(event("telephony.line_closed", **fields, error="the caller hung up"))


def _report_identity(
    call: CallSession | None,
    conversation: SalesConversation | None,
    runner_args: RunnerArguments,
    supervisor: SessionSupervisor,
    voicemail: VoicemailHandler | None,
    silence: SilenceHandler,
) -> dict:
    """The ids and figures the report needs that only this file knows."""
    identity = {
        "session_id": getattr(runner_args, "session_id", None),
        # Phase 22: the correlation id, so the call report and the stored
        # quality summary can be matched to the logs of every process.
        "trace_id": current_trace_id(),
        "transport": call.provider if call is not None else getattr(runner_args, "transport_type", None),
        "ended_by": _ended_by(conversation, supervisor, voicemail, silence, call),
        "service_failures": supervisor.health.describe() or None,
    }
    if call is not None:
        identity.update(
            {
                "call_id": call.call_id,
                "provider": call.provider,
                "direction": call.direction,
                "from_number": call.from_number,
                "to_number": call.to_number,
                "call_duration_secs": round(call.duration_secs, 1),
            }
        )
    if conversation is not None:
        identity.update(
            {
                "prospect_id": conversation.brief.prospect_id,
                "campaign_id": conversation.brief.campaign_id,
                "attempt_id": conversation.brief.call_attempt_id,
                "final_state": conversation.state.value,
            }
        )
    return identity


def _record_session_metrics(
    call: CallSession | None, used: CallUsage, priced: dict | None, ended_by: str
) -> None:
    """One session's usage, cost, duration and ending into the registry. Phase 22.

    Counts at teardown, from the totals the session already assembled, so a
    session that failed mid-way still lands (the `finally` runs this) and a
    label is never a per-frame cost. Never raises: accounting must not break
    a teardown that also writes the call's record.
    """
    try:
        SESSION_ENDINGS.inc(reason=ended_by.split(":", 1)[0])
        if call is not None and call.duration_secs > 0:
            CALL_DURATION.observe(call.duration_secs)
        for entry in used.llm.values():
            LLM_REQUESTS.inc(entry.requests, model=entry.model)
            LLM_TOKENS.inc(entry.prompt_tokens, kind="prompt", model=entry.model)
            LLM_TOKENS.inc(entry.completion_tokens, kind="completion", model=entry.model)
            if entry.cached_tokens:
                LLM_TOKENS.inc(entry.cached_tokens, kind="cached", model=entry.model)
        if used.llm:
            LLM_TOKENS_PER_CALL.observe(used.prompt_tokens + used.completion_tokens)
        for entry in used.tts.values():
            TTS_CHARACTERS.inc(entry.characters, model=entry.model)
        for entry in used.stt.values():
            STT_AUDIO_SECONDS.inc(entry.audio_seconds, model=entry.model)
        if priced:
            CALL_COST.observe(float(priced["total_usd"]))
            for stage in priced.get("priced", ()):
                COST_TOTAL.inc(float(priced.get(stage, 0.0)), stage=stage)
    except Exception:  # noqa: BLE001 - a number must never break a teardown
        logger.debug("METRICS | could not record the session's figures", exc_info=True)


def _install_ops(app) -> None:
    """Mount `/healthz`, `/readyz` and `/metrics` on the runner's web server. Phase 22.

    Readiness is the database answering — the one dependency a session
    cannot do without — plus the process not being told to stop. The vendor
    checks stay in `health.py`: a probe that costs seven network round trips
    every ten seconds is a probe somebody turns off. No middleware on this
    app: it carries the carrier's audio websocket.
    """
    if not CONFIG.monitoring.enabled:
        logger.info("Monitoring: off (MONITORING_ENABLED=false)")
        return

    async def readiness() -> Readiness:
        if not CONFIG.database_url:
            return Readiness([ReadyCheck("database", True, "no DATABASE_URL; the bot answers without one")])
        report = await check_health(CONFIG, timeout_secs=3.0, only=("database",))
        checks = [
            ReadyCheck(c.name, c.status is not Status.FAILED, c.detail, c.latency_ms) for c in report.components
        ]
        return Readiness(checks or [ReadyCheck("database", False, "no answer")])

    def info() -> dict:
        active = sum(value for _labels, value in SESSIONS_ACTIVE.series())
        return {"sessions_active": int(active), "sales_mode": CONFIG.sales.enabled}

    install_ops_routes(
        app,
        "bot",
        request_metrics=False,
        request_ids=False,
        readiness=readiness,
        token=CONFIG.monitoring.token,
        version="22",
        info=info,
    )
    logger.info(f"Monitoring OK | {CONFIG.monitoring.describe()}")


def _note_termination(conversation: SalesConversation | None, reason: Reason, detail: str) -> None:
    """Record on the call's own record that the supervisor ended it. Phase 9.

    So the stored result says *why* a call stopped, rather than leaving a
    reader to infer it from a short duration. A note rather than a status: the
    call still ended, and what was established during it is still true.
    """
    if conversation is None:
        return
    conversation.record.add_note(f"the call was ended automatically — {detail}")
    logger.warning(event("session.terminated", outcome=reason.value, error=detail))


async def _release_webrtc(runner_args: RunnerArguments) -> None:
    """Close this session's WebRTC peer connection, so the runner forgets it.

    **Without this, the browser connects once and then never again** until the
    page is refreshed or the server restarted. The chain is worth writing down,
    because every link looks harmless:

    1. The dev runner keeps peer connections in a map keyed by `pc_id`, and
       removes one only when the connection fires `closed`.
    2. `SmallWebRTCTransport.disconnect()` closes the peer connection **only if
       the client is still connected** — a guard that makes sense for the case
       where the *bot* hangs up first.
    3. When the caller is the one who leaves, they are gone by the time the
       pipeline shuts down, so that guard is false, the peer connection is never
       closed, `closed` never fires, and the map keeps it forever.
    4. The browser client reuses its `pc_id` when you press connect again. The
       runner finds the stale entry, renegotiates it instead of creating a new
       one — and *renegotiation does not start a bot*. The UI connects to
       nothing. Refreshing the page discards the `pc_id`, which is why a refresh
       appears to fix it.

    So when a session ends, for any reason, we close the connection ourselves.
    That is safe even when it is already closed: aiortc's `close()` is
    idempotent, and doing it here rather than in the disconnect handler leaves
    `ConnectionGuard`'s reconnect window intact — the connection is only
    released once we have actually given up on the session.

    A no-op for every other transport.
    """
    connection = getattr(runner_args, "webrtc_connection", None)
    if connection is None:
        return
    try:
        await connection.disconnect()
    except Exception:  # noqa: BLE001 - never let cleanup mask the real ending
        logger.exception("Failed to close the WebRTC peer connection")


def _cost_rates() -> CostRates:
    """The per-unit rates from config, or none of them. Phase 11.

    Unset is the normal state and produces no cost estimate rather than a
    guessed one — see `reliability/usage.py`.
    """
    rates = CONFIG.cost
    return CostRates(
        llm_input_per_mtok=rates.llm_input_per_mtok,
        llm_output_per_mtok=rates.llm_output_per_mtok,
        tts_per_mchar=rates.tts_per_mchar,
        stt_per_minute=rates.stt_per_minute,
        telephony_per_minute=rates.telephony_per_minute,
    )


async def _make_conversation(
    runner_args: RunnerArguments,
    *,
    retriever: KnowledgeRetriever | None,
    call: CallSession | None,
    pool=None,
) -> tuple[Briefing, SalesConversation | None, ActionService | None]:
    """Work out who is being called, and start a sales conversation about them.

    Returns `(briefing, None, None)` when `SALES_MODE=false`, which leaves the
    Phase 3 agent exactly as it was: the general system prompt, no tools, no
    state machine, no director in the pipeline. That switch exists because it is
    the A/B baseline for what the sales layer is worth, and because the two
    Phase 2 eval scenarios were written against that agent.

    **The boundary this function guards.** It reads the ids off the media
    stream and asks `open_briefing` for something that can resolve them. What
    comes back is a `ProspectSource` and a `ConversationSink` — two Protocols
    with plain-data signatures. Phase 7 adds a third, the `ActionBackend` from
    `open_actions`, built from the same store, the retrieval stage and the phone
    call. Nothing below this line in `bot.py` knows that a prospect is a row,
    that marking somebody do-not-call closes their campaign memberships, that a
    meeting is a calendar API call, or that any of it involves PostgreSQL.

    A bot with no `DATABASE_URL`, or one whose database is unreachable, gets a
    briefing with nothing in it and an anonymous call — which is a working
    configuration and not an error. That is a deliberate difference from the
    knowledge base, whose absence *is* fatal: the agent answers *from* the
    knowledge base, and only *knows who it is talking to* from the prospect
    database. An action the session cannot perform is likewise not an error:
    the agent is told it cannot, and says so.
    """
    if not CONFIG.sales.enabled:
        return Briefing(None, None, None), None, None

    briefing = await open_briefing(
        CONFIG.database_url,
        default_region=CONFIG.default_phone_region,
        max_attempts=CONFIG.campaign_max_attempts,
        retry_minutes=CONFIG.campaign_retry_minutes,
        # Phase 11: share the knowledge base's pool when it is the same
        # database, rather than opening a second one per call.
        pool=pool,
        # Phase 19: the compliance policy, so the brief carries this call's
        # disclosures and an opt-out lands on the do-not-call list.
        compliance=CONFIG.policy_resolver(),
    )
    brief = await resolve_brief(
        runner_args,
        defaults=_campaign_defaults(CONFIG.sales),
        source=briefing.source,
        fallback=_dev_prospect(CONFIG.sales),
    )
    actions = await open_actions(
        CONFIG, brief=brief, store=briefing.store, knowledge=retriever, call=call
    )
    conversation = SalesConversation(
        brief,
        sink=briefing.sink,
        knowledge_base=retriever is not None,
        actions=actions,
        timezone=CONFIG.calendar.timezone,
        # The ids every tool log line carries. A browser session has only the
        # runner's session id; a campaign call has all four.
        audit=AuditContext(
            session_id=getattr(runner_args, "session_id", None),
            call_id=call.call_id if call is not None else None,
            prospect_id=brief.prospect_id,
            call_attempt_id=brief.call_attempt_id,
        ),
    )
    return briefing, conversation, actions


def _campaign_defaults(sales: SalesConfig) -> CampaignBrief:
    """The campaign settings from `.env`, which a campaign's own JSON overlays."""
    return CampaignBrief(
        agent_name=sales.agent_name,
        company_name=sales.company_name,
        company_description=sales.company_description,
        services=list(sales.services),
        offer=sales.offer,
        value_points=list(sales.value_points),
        qualification_criteria=list(sales.qualification_criteria),
        meeting_ask=sales.meeting_ask,
        notes=list(sales.notes),
        # Phase 19: the environment's disclosures, for a call with no campaign
        # row (a browser session, an eval). A campaign call replaces these
        # with the policy resolved for its number.
        disclosures=CONFIG.compliance_policy.disclosures(),
    )


def _dev_prospect(sales: SalesConfig) -> ProspectBrief | None:
    """A prospect described in `.env`, for calls that carry no prospect id.

    Development and eval only, and used *only* when the campaign path produced
    nothing — never as a default applied over a real lookup. A campaign call
    whose prospect could not be found must stay anonymous rather than be told it
    is speaking to whoever `.env` last described.
    """
    fields = sales.prospect
    if not fields:
        return None
    notes = [fields["notes"]] if fields.get("notes") else []
    return ProspectBrief(
        first_name=fields.get("first_name"),
        last_name=fields.get("last_name"),
        company=fields.get("company"),
        job_title=fields.get("job_title"),
        industry=fields.get("industry"),
        location=fields.get("location"),
        email=fields.get("email"),
        notes=notes,
    )


async def _make_knowledge_stage() -> tuple[KnowledgeStore | None, KnowledgeRetriever | None]:
    """Connect the knowledge base and build the retrieval stage.

    Returns `(None, None)` when `KB_ENABLED` is false, which drops the stage out
    of the pipeline entirely and leaves the Phase 2 agent — useful as the A/B
    baseline for what grounding is worth, and for running with no PostgreSQL.

    A connection failure is *not* swallowed. An agent whose whole purpose is to
    answer from a knowledge base, quietly running without one, would take the
    call and then improvise every answer — the exact failure this phase exists
    to prevent, and one nobody would notice until a customer was told something
    untrue. Better to fail here, loudly, with a message naming the cause.
    """
    if not CONFIG.kb_enabled:
        logger.warning("KB_ENABLED is false — answering without a knowledge base")
        return None, None

    # Phase 12: shared across sessions and warmed at startup, so a phone call
    # does not spend its first second loading a model the process already has.
    embedder = shared_embedder(CONFIG.embedding_model)
    # Load the weights before the caller says anything. A no-op once the
    # process has loaded them; without it the first question of the call pays
    # for the model load on top of its own latency.
    await embedder.warm_up()

    store = await KnowledgeStore.connect(
        CONFIG.kb_database_url,
        dimensions=embedder.dimensions,
        embed_model=embedder.model_name,
    )
    documents, chunks = await store.counts()
    logger.info(f"Knowledge base: {documents} document(s), {chunks} chunk(s)")

    return store, KnowledgeRetriever(store, embedder, CONFIG)


def _telephony_params() -> FastAPIWebsocketParams:
    """Transport parameters for a call arriving from a phone carrier.

    Short, because Pipecat fills in the parts that differ per carrier. It
    detects the provider from the media stream's first message, builds the
    matching serializer, and sets `add_wav_header=False` — so the μ-law-versus-
    PCM, 8kHz-versus-16kHz and framing differences between carriers never reach
    this file. The serializer resamples the carrier's 8kHz audio up to the
    pipeline's rate in both directions, which is why the STT, LLM and TTS
    services are constructed identically for a phone call and a browser.

    No VAD analyser here on purpose: `turns.py` puts it on the context
    aggregator, where it is shared by every transport.
    """
    return FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True)


async def bot(runner_args: RunnerArguments):
    """Entry point invoked by the Pipecat runner for each session."""
    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
        # Headless testing. `uv run bot.py -t eval` boots this same bot as an
        # eval websocket server so the scenarios in `evals/` can drive it with
        # synthesised speech — the real STT, turn detection and TTS all run, so
        # barge-in and end-of-turn behaviour are genuinely exercised rather than
        # simulated. See evals/README.md.
        "eval": lambda: EvalTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
        # Every carrier Pipecat can recognise, not only the one that
        # `TELEPHONY_PROVIDER` selects. Receiving a call needs no code from us,
        # so there is nothing to gain by refusing three of them; placing a call
        # does, and that is the shorter list in `config.SUPPORTED_TELEPHONY`.
        **{name: _telephony_params for name in TELEPHONY_TRANSPORTS},
    }

    transport = await _make_telephony_transport(runner_args) or await create_transport(
        runner_args, transport_params
    )

    await run_bot(transport, runner_args)


async def _make_telephony_transport(runner_args: RunnerArguments) -> BaseTransport | None:
    """Build the transport for an incoming call, if this is one and we can.

    Returns `None` for anything that is not a phone call, and for a call from a
    carrier nothing here can serialize — in both cases the caller falls back to
    Pipecat's `create_transport`.

    The reason for taking over at all is the serializer, and there are two of
    them. Pipecat's choice is hard-wired to `api.twilio.com` and the `TWILIO_*`
    environment variables, so a Twilio-compatible carrier such as SignalWire
    cannot use it — and it *raises* when those variables are empty, so a bot with
    no carrier configured cannot answer a call at all. Both are fixed by letting
    the provider choose; `src/telephony/transport.py` explains it in full.
    """
    if not isinstance(runner_args, WebSocketRunnerArguments):
        return None
    if runner_args.transport_type == "websocket":
        return None  # A plain websocket client, not a carrier.

    # No credentials is not a failure here: the call is answered without the
    # ability to hang up over the carrier's API. See `_unauthenticated_serializer`.
    provider = make_provider(CONFIG.telephony) if CONFIG.telephony.has_credentials else None
    try:
        return await create_provider_transport(runner_args, provider, _telephony_params())
    finally:
        if provider is not None:
            # The provider is built here only to supply a serializer, which opens
            # its own connection when it needs one. Nothing on this path places
            # calls, so its HTTP session should not outlive this function.
            await provider.close()


async def _preflight() -> None:
    """Check the knowledge base before the server starts accepting calls.

    Without this the first sign of a database that is down, or a schema that was
    never created, is a session that fails the moment somebody calls in. The
    check costs one connection at startup and turns that into a message on the
    terminal of the person who just ran the command.
    """
    _report_telephony()
    _report_sales()
    _report_actions()
    _report_scale()

    # Phase 12: the two one-off costs a session used to pay after the person
    # picked up. Measured at 4.3 s (the LLM SDK's first import) and 1.1 s (the
    # embedding model) on the first phone call of a process.
    warm_up_llm_module(CONFIG)

    if not CONFIG.kb_enabled:
        return
    embedder = shared_embedder(CONFIG.embedding_model)
    await embedder.warm_up()
    store = await KnowledgeStore.connect(
        CONFIG.kb_database_url,
        dimensions=embedder.dimensions,
        embed_model=embedder.model_name,
    )
    try:
        documents, chunks = await store.counts()
    finally:
        await store.close()

    if chunks == 0:
        logger.warning(
            "Knowledge base is empty — the agent will say it does not have any information. "
            "Load documents with:  uv run ingest.py add <file>"
        )
    else:
        logger.info(f"Knowledge base OK | {documents} document(s), {chunks} chunk(s)")


def _report_sales() -> None:
    """Say, at startup, what the agent will be able to claim on a call.

    Not a check that can fail — none of the sales settings is required, and an
    agent with none of them still holds a conversation. What it prevents is the
    other confusion: hearing a live call where the agent introduces itself with
    no company name and no idea what it is selling, and having to work out
    whether `.env` was read.

    The do-not-call line is the one worth reading. Without a prospect database
    the agent still *honours* a do-not-call request for the rest of the call —
    it stops selling and closes — but there is no row to write it to, so nothing
    stops that person being dialled again tomorrow.
    """
    sales = CONFIG.sales
    if not sales.enabled:
        logger.info("Sales mode: off — the general knowledge-base assistant (SALES_MODE=false)")
        return

    logger.info(f"Sales mode: {sales.describe()}")
    if sales.gaps:
        logger.warning(
            f"Sales settings not set: {', '.join(sales.gaps)} — the agent will not name a "
            f"company, will make no claims about the product, and will refuse questions of "
            f"detail. See the SALES CONVERSATION section of .env.example."
        )
    if not CONFIG.database_url:
        logger.warning(
            "No DATABASE_URL: the agent will not know who it is calling, and a do-not-call "
            "request will be honoured on the call but recorded nowhere."
        )


def _report_actions() -> None:
    """Say, at startup, what the agent will be able to *do* on a call.

    Phase 7. Not a check that can fail — every action is optional and the agent
    is told about the ones it lacks — but the two most consequential gaps are
    worth a line each: a calendar on UTC when the prospects are not, and a
    carrier account with no transfer destination, because both are things
    somebody will otherwise discover on a live call.
    """
    if not CONFIG.sales.enabled:
        return
    calendar = CONFIG.calendar
    logger.info(f"Actions: calendar={calendar.describe()}, callbacks up to {CONFIG.callback_max_days_ahead} days ahead")
    if calendar.enabled and calendar.timezone.upper() == "UTC":
        logger.warning(
            "CALENDAR_TIMEZONE is not set, so meeting times and 'tomorrow' are worked out in UTC. "
            "Set it to your prospects' zone, e.g. CALENDAR_TIMEZONE=Asia/Karachi."
        )
    if calendar.provider == "local" and not CONFIG.database_url:
        logger.warning(
            "CALENDAR_PROVIDER is local but there is no DATABASE_URL, so the agent cannot book meetings."
        )
    if CONFIG.telephony.has_credentials and not CONFIG.telephony.transfer_number:
        logger.info(
            "Actions: TELEPHONY_TRANSFER_NUMBER is not set — the agent will offer callbacks, not transfers."
        )


def _report_scale() -> None:
    """Say, at startup, what limits how many calls can run at once. Phase 11.

    The connection budget is the ceiling nobody sees until they hit it: each
    session holds a pool, PostgreSQL allows a hundred connections by default,
    and the arithmetic between those two decides how many calls this can carry.
    Printing it turns a mystery into a number.

    Not a check that can fail — a bot with no database is a working browser
    bot — and deliberately not a promise: the real ceiling is whatever the
    database's `max_connections` is minus whatever else is using it.
    """
    if not CONFIG.database_url:
        return
    shared = CONFIG.shares_database
    # min_size per pool: the knowledge base holds one, the campaign store holds
    # one more unless it is borrowing.
    idle = 1 if shared else 2
    peak = 4 if shared else 6
    logger.info(
        event(
            "startup.scale",
            outcome=f"{idle} idle / up to {peak} database connections per call",
            provider="shared pool" if shared else "separate pools",
        )
        + (
            ""
            if shared
            else "  (KB_DATABASE_URL and DATABASE_URL differ, so each call opens two pools)"
        )
    )
    logger.info(
        f"Cost: {_cost_rates().describe()}"
    )


def _report_telephony() -> None:
    """Say, at startup, whether this bot can be phoned and can phone out.

    Not a check that can fail. Telephony is optional — the browser agent is how
    this is developed — so a bot with no carrier credentials is a normal bot,
    not a broken one. What is worth avoiding is the *other* confusion: setting
    the variables, restarting, and having no way to tell whether they were read.

    The stream URL is printed because it is the single value that has to match
    between three places — the tunnel, this bot's `/ws` route, and what the
    carrier is told to dial into — and a mismatch shows up as a call that
    connects and then hangs up with nothing in the log.
    """
    telephony = CONFIG.telephony
    if not telephony.is_configured:
        logger.info(
            f"Telephony: {telephony.provider} not configured — browser and eval calls only. "
            f"Set TELEPHONY_* in .env to place phone calls."
        )
        return

    try:
        url = stream_url(telephony.public_url or "", telephony.stream_path)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        logger.warning(f"Telephony: TELEPHONY_PUBLIC_URL is unusable — {exc}")
        return

    logger.info(f"Telephony OK | {telephony.describe()}")
    logger.info(f"Telephony    | carriers will stream call audio to {url}")


if __name__ == "__main__":
    import asyncio
    from pathlib import Path

    from pipecat.runner.run import app, main

    from src.campaigns.webhooks import install_webhook_receiver
    from src.client_theme import install_client_theme
    from src.embeddings import EmbeddingError
    from src.knowledge_store import KnowledgeStoreError

    logger.info(f"Configuration OK | {CONFIG.describe()}")
    logger.info(CONFIG.describe_turn_taking())
    logger.info(CONFIG.describe_echo())
    logger.info(CONFIG.describe_safety())
    logger.info(CONFIG.describe_voice_quality())
    logger.info(f"Retries: {describe_policies()}")
    try:
        asyncio.run(_preflight())
    except (KnowledgeStoreError, EmbeddingError) as exc:
        print(f"\nCannot start the bot.\n\n{exc}\n", file=sys.stderr)
        raise SystemExit(1) from exc
    # Phase 14: the carrier's status webhooks, on the runner's own web server —
    # the one public address a single tunnel gives you. One POST route; the
    # handler verifies a signature and writes two rows, and never touches a
    # pipeline. `TELEPHONY_WEBHOOK_RECEIVER=standalone` moves it to
    # `uv run webhooks.py` instead, and this mounts nothing.
    install_webhook_receiver(app, CONFIG)
    # Phase 22: liveness, readiness and the metrics, on the same server. Three
    # GET routes; nothing in front of the websocket.
    _install_ops(app)
    # Phase 33: the browser client at /client in the application's own design
    # (its tokens, Inter, light and dark), served from this origin. Registered
    # before `main()` mounts the vendor's bundle, so these paths come first.
    install_client_theme(app, Path(__file__).resolve().parent / "web")
    main()
