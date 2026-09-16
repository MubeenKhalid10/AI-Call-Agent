#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for Phase 32: which tools need a second LLM request, and which do not.

Run it from the `server/` directory::

    uv run python tests/test_tool_round_trips.py

No keys, no database, no network. What is under test:

* the classification: every tool is either turn-releasing (a recording tool
  whose result changes nothing the caller needs to hear) or result-blocking
  (the model must see the result before it may claim anything);
* the mechanism: a recording tool delivers its result with ``run_llm=False``
  only when the model already spoke in the same response, that response made
  exactly one tool call, and the result needs no follow-up; otherwise the
  result is delivered as before and the second request runs — driven through
  the real tool wrapper with the real `SpeechTally`;
* persistence: the record is written either way, once, and the sink receives
  one outcome; a tally that never answers, or that raises, costs nothing but
  the second request;
* the blocking tools still deliver their results before returning, never
  released, and `end_call` still ends the pipeline;
* the pipeline, end to end: a scripted LLM service in a real pipeline under
  a real `PipelineWorker`, with the real aggregators and the real spoken-text
  filter — one response for a recording call the model spoke with, two for a
  silent call, two for a calendar call — which is the before/after
  measurement in requests rather than seconds.

A plain script rather than a pytest suite because the project has no test
dependency and this needs none. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from loguru import logger  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    EndWorkerFrame,
    Frame,
    FunctionCallFromLLM,
    FunctionCallResultProperties,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.services.llm_service import FunctionCallParams, LLMService  # noqa: E402
from pipecat.tests.utils import SleepFrame, run_test  # noqa: E402

import src.conversation.toolkit as toolkit  # noqa: E402
from src.conversation.states import ConversationState  # noqa: E402
from src.conversation.tools import RESULT_BLOCKING, TOOL_NAMES, TURN_RELEASING  # noqa: E402
from src.spoken_text import SpeechObserver, SpeechTally, SpokenTextFilter  # noqa: E402
from test_actions import FakeKnowledge, FakeTelephony, World, phone_call  # noqa: E402

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


class Captured:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._id = logger.add(lambda m: self.lines.append(m.record["message"]), level="DEBUG")

    def close(self) -> None:
        logger.remove(self._id)

    def matching(self, needle: str) -> list[str]:
        return [line for line in self.lines if needle in line]


def everything() -> World:
    return World(knowledge=FakeKnowledge([]), telephony=FakeTelephony(), call=phone_call(), transfer_number="+923009999999")


def spoken_tally(*, calls: int = 1, spoke: bool = True, ended: bool = True) -> SpeechTally:
    """A tally as the observer would leave it after a response with one tool call."""
    tally = SpeechTally()
    tally.llm_started()
    tally.llm_calls(calls)
    tally.filter_started()
    if spoke:
        tally.filter_spoken("Right, forty trucks.")
    if ended:
        tally.filter_ended()
    return tally


async def call_capturing(world: World, name: str, **arguments: Any) -> tuple[dict[str, Any], FunctionCallResultProperties | None]:
    """One tool call through the real handler; returns the result and the properties it was delivered with."""
    captured: dict[str, Any] = {}
    delivered: list[FunctionCallResultProperties | None] = []

    async def result_callback(result: Any, *args: Any, properties: FunctionCallResultProperties | None = None, **kw: Any) -> None:
        captured.update(result if isinstance(result, dict) else {"result": result})
        delivered.append(properties)

    params = FunctionCallParams(
        function_name=name,
        tool_call_id=f"call-{len(world.results)}",
        arguments=arguments,
        llm=world.llm,
        pipeline_worker=None,
        context=LLMContext(),
        result_callback=result_callback,
    )
    await world.tools[name].handler(params)
    world.results.append(captured)
    return captured, (delivered[-1] if delivered else None)


def released(properties: FunctionCallResultProperties | None) -> bool:
    return properties is not None and properties.run_llm is False


# --- The classification ---------------------------------------------------------------


def check_classification() -> None:
    print("\n=== every tool is classified, once ===")
    check("the two classes cover every tool", set(TURN_RELEASING) | set(RESULT_BLOCKING) == set(TOOL_NAMES))
    check("and do not overlap", not (set(TURN_RELEASING) & set(RESULT_BLOCKING)))
    check("the recording tools may release the turn", set(TURN_RELEASING) == {"record_discovery", "set_interest", "record_objection", "move_to_stage", "request_meeting"})
    check("everything that acts on the world, or ends the call, blocks on its result", set(RESULT_BLOCKING) == {"search_knowledge_base", "check_calendar_availability", "book_meeting", "schedule_callback", "mark_do_not_call", "transfer_to_human", "end_call"})


# --- The tally and the filter ---------------------------------------------------------


async def observed(frames: list[Frame]) -> SpeechTally:
    """Run frames through the real filter with the observer watching, as in the bot."""
    tally = SpeechTally()
    spoken_text = SpokenTextFilter()
    await run_test(spoken_text, frames_to_send=frames, observers=[SpeechObserver(tally, llm=None, spoken_text=spoken_text)], start_timeout=10.0)
    return tally


async def check_tally_and_filter() -> None:
    print("\n=== the observer tells the tally what the model said ===")
    tally = SpeechTally()
    check("a fresh tally has no response", tally.current_response == 0 and not tally.open and await tally.wait_ended(0, 0.01))

    tally = await observed([LLMFullResponseStartFrame(), LLMTextFrame("Right, forty trucks. "), LLMTextFrame("What does that cost you?"), LLMFullResponseEndFrame()])
    check("a response with words is recorded as spoken, and ended", tally.started == 1 and tally.ended == 1 and tally.spoke_in(1) and not tally.open, tally.describe())

    tally = await observed([LLMFullResponseStartFrame(), LLMTextFrame("<think>should I record this</think>"), LLMFullResponseEndFrame()])
    check("hidden reasoning alone is not speech", not tally.spoke_in(1) and tally.ended == 1)

    tally = await observed([LLMFullResponseStartFrame(), LLMFullResponseEndFrame()])
    check("nor is an empty response", not tally.spoke_in(1))

    tally = await observed([LLMFullResponseStartFrame(), LLMTextFrame("Sure"), LLMFullResponseEndFrame()])
    check("a reply held back until the end frame still counts", tally.spoke_in(1))

    tally = await observed([LLMFullResponseStartFrame(), LLMTextFrame("One."), LLMFullResponseEndFrame(), LLMFullResponseStartFrame(), LLMFullResponseEndFrame()])
    check("responses are numbered: the first spoke, the second did not", tally.spoke_in(1) and not tally.spoke_in(2) and tally.ended == 2)

    tally = SpeechTally()
    tally.llm_started()
    check("while a response is open, waiting for its end times out", tally.open and not await tally.wait_ended(1, 0.05))
    tally.interrupted()
    check("an interruption ends every response in flight", not tally.open and await tally.wait_ended(1, 0.05))
    tally.llm_started()
    tally.filter_started()
    tally.filter_spoken("   ")
    check("whitespace is not speech", not tally.spoke_in(2))


# --- The wrapper --------------------------------------------------------------------------


async def check_wrapper() -> None:
    print("\n=== a recording tool releases the turn only when it is safe ===")
    world = everything()
    await world.says("Hello")
    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "record_discovery", pain_point="fuel bill out of control")
    check("the model spoke, one call: the result says no second request", result["success"] and released(properties), str(properties))
    check("and discovery is recorded", "fuel bill out of control" in str(world.record.pain_points))

    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "record_objection", kind="PRICE", detail="too expensive")
    check("an objection: released, and recorded", released(properties) and world.record.open_objections and world.state is ConversationState.OBJECTION_HANDLING)

    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "set_interest", level="CURIOUS", reason="asked about pricing")
    check("interest: released, and recorded", released(properties) and world.record.interest_level.value == "CURIOUS")

    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "move_to_stage", stage="qualification")
    check("a stage move: released", released(properties) and world.state is ConversationState.QUALIFICATION)

    world.conversation.speech = spoken_tally(spoke=False)
    result, properties = await call_capturing(world, "record_discovery", impact="late deliveries")
    check("the model called without speaking: the second request runs, as before", result["success"] and properties is None)
    check("and the record is written all the same", world.record.impact == "late deliveries")

    world.conversation.speech = spoken_tally(calls=2)
    result, properties = await call_capturing(world, "record_discovery", current_process="spreadsheets")
    check("two calls in one response: the second request runs", result["success"] and properties is None and world.record.current_process == "spreadsheets")

    world.conversation.speech = None
    result, properties = await call_capturing(world, "record_discovery", desired_outcome="fewer empty miles")
    check("no tally attached (a test, an older caller): the second request runs", result["success"] and properties is None)

    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "set_interest", level="MAYBE")
    check("a failed call: the second request runs, with the failure", not result["success"] and properties is None)

    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "set_interest", level="NOT_INTERESTED", reason="not for us")
    check("a no that must be closed: the second request runs so the goodbye and end_call follow", result["success"] and result["data"]["stopped_selling"] and properties is None)
    check("and the state moved", world.state is ConversationState.NOT_INTERESTED)

    world = everything()
    await world.says("Hello")
    world.conversation.speech = spoken_tally()
    await world.says("Not interested, sorry.")
    result, properties = await call_capturing(world, "record_objection", kind="NOT_INTERESTED", detail="not interested")
    check("an objection that is the no: the second request runs", result["success"] and properties is None and world.state is ConversationState.NOT_INTERESTED)

    world = everything()
    await world.says("Hello")
    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "move_to_stage", stage="nowhere")
    check("a refused move: the second request runs", not result["success"] and properties is None)

    # The wait: a response that never ends means the old behaviour.
    saved = toolkit.RESPONSE_END_WAIT_SECS
    toolkit.RESPONSE_END_WAIT_SECS = 0.05
    try:
        world.conversation.speech = spoken_tally(ended=False)
        result, properties = await call_capturing(world, "record_discovery", existing_provider="never ended")
        check("a response that has not ended in time: the second request runs, and the result is delivered", result["success"] and properties is None)
    finally:
        toolkit.RESPONSE_END_WAIT_SECS = saved

    class Broken(SpeechTally):
        async def wait_ended(self, response: int, timeout: float) -> bool:
            raise RuntimeError("tally exploded")

    captured = Captured()
    try:
        broken = Broken()
        broken.llm_started()
        broken.llm_calls(1)
        world.conversation.speech = broken
        result, properties = await call_capturing(world, "record_discovery", pain_point="tally broke")
    finally:
        captured.close()
    check("a tally that raises: the result is still delivered and the second request runs", result["success"] and properties is None and "tally broke" in str(world.record.pain_points))
    check("and the failure is logged, not exposed", any("could not decide" in line for line in captured.lines) and "exploded" not in str(result))


# --- Persistence: once ------------------------------------------------------------------------


async def check_persistence_once() -> None:
    print("\n=== the record is written once, and the sink hears once ===")
    world = everything()
    await world.says("Hello")
    world.conversation.speech = spoken_tally()
    await call_capturing(world, "record_discovery", pain_point="fuel")
    await call_capturing(world, "record_discovery", pain_point="fuel")
    check("the same fact twice is one entry", str(world.record.pain_points).count("fuel") == 1, str(world.record.pain_points))
    await call_capturing(world, "record_objection", kind="PRICE", detail="too dear")
    await call_capturing(world, "record_objection", kind="PRICE", detail="too dear")
    check("the same objection twice is one entry", len([o for o in world.record.objections if o.kind.value == "PRICE"]) == 1)
    outcome = await world.conversation.finish()
    check("the outcome carries the record", outcome["qualification"]["pain_points"] == ["fuel"] if isinstance(outcome.get("qualification"), dict) else "fuel" in str(outcome))
    check("and the sink received exactly one outcome", len(world.sink.outcomes) == 1, str(len(world.sink.outcomes)))


# --- The blocking tools ----------------------------------------------------------------------


async def check_blocking_tools() -> None:
    print("\n=== the tools whose result the model must see stay synchronous ===")
    world = everything()
    await world.says("Hello")
    world.conversation.speech = spoken_tally()  # Even with the model having spoken:
    result, properties = await call_capturing(world, "check_calendar_availability", day="2026-09-07")
    check("calendar availability answers before returning, and never releases the turn", result["success"] and result["data"]["slots"] and properties is None)
    start = result["data"]["slots"][0]["start"]
    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "book_meeting", start=start, attendee_email="sarah@example.com")
    check("booking completes before returning, and never releases the turn", result["success"] and world.record.meeting_booked and properties is None)
    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "search_knowledge_base", query="pricing")
    check("a knowledge search delivers its result first", "success" in result and properties is None)
    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "schedule_callback", when="2026-09-08T10:00")
    check("a callback delivers its result first", "success" in result and properties is None)
    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "mark_do_not_call", reason="asked")
    check("a do-not-call delivers its result first", "success" in result and properties is None and world.conversation.dnc_recorded)

    world = everything()
    await world.says("Hello")
    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "transfer_to_human", reason="asked for a person")
    check("a transfer completes its call-control action before reporting success", result["success"] and world.telephony.transfers and properties is None, str(getattr(world.telephony, "transfers", None)))

    world = everything()
    await world.says("Hello")
    world.conversation.speech = spoken_tally()
    result, properties = await call_capturing(world, "end_call", reason="done")
    check("end_call still delivers its result and ends the pipeline", result.get("success") and any(isinstance(f, EndWorkerFrame) for f in world.llm.frames) and properties is None)


# --- The pipeline: requests counted ------------------------------------------------------------


class ScriptedLLM(LLMService):
    """An LLM service that answers each context with a scripted response.

    Each script entry is ``(text, [(tool_name, arguments), ...])``: the text
    is streamed as one `LLMTextFrame`, the tool calls handed to Pipecat's own
    `run_function_calls`, exactly as the OpenAI-compatible service does, and
    the end frame pushed after — the real order, with the real handler
    execution and the real result path through the assistant aggregator.
    """

    def __init__(self, script: list[tuple[str, list[tuple[str, dict[str, Any]]]]]) -> None:
        super().__init__()
        self.script = list(script)
        self.requests = 0

    def can_generate_metrics(self) -> bool:
        return False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        self.requests += 1
        text, calls = self.script.pop(0) if self.script else ("Anything else?", [])
        await self.push_frame(LLMFullResponseStartFrame())
        if text:
            await self.push_frame(LLMTextFrame(text))
        if calls:
            await self.run_function_calls(
                [
                    FunctionCallFromLLM(function_name=name, tool_call_id=f"call-{self.requests}-{i}", arguments=arguments, context=frame.context)
                    for i, (name, arguments) in enumerate(calls)
                ]
            )
        await self.push_frame(LLMFullResponseEndFrame())


async def requests_for(script: list[tuple[str, list[tuple[str, dict[str, Any]]]]], *, tally: bool = True) -> tuple[int, World]:
    """Run one turn through a real pipeline and count the LLM requests it took."""
    world = everything()
    await world.says("Hello")
    speech = SpeechTally()
    if tally:
        world.conversation.speech = speech
    llm = ScriptedLLM(script)
    for schema in world.conversation.tools():
        llm.register_function(schema.name, schema.handler)
    context = LLMContext(messages=[{"role": "user", "content": "We run forty trucks."}], tools=world.conversation.advertised_tools())
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)
    from pipecat.pipeline.pipeline import Pipeline

    spoken_text = SpokenTextFilter()
    pipeline = Pipeline([user_aggregator, llm, spoken_text, assistant_aggregator])
    await run_test(
        pipeline,
        frames_to_send=[LLMRunFrame(), SleepFrame(1.0)],
        observers=[SpeechObserver(speech, llm=llm, spoken_text=spoken_text)],
        start_timeout=10.0,
    )
    return llm.requests, world


async def check_pipeline_requests() -> None:
    print("\n=== in a real pipeline: requests per tool turn, before and after ===")
    requests, world = await requests_for([("Right, forty trucks. What does the fuel cost you a month?", [("record_discovery", {"pain_point": "forty trucks, fuel"})])])
    check("a recording call the model spoke with is ONE request", requests == 1, str(requests))
    check("and the fact is recorded", "forty trucks" in str(world.record.pain_points))

    requests, world = await requests_for([("Right, forty trucks. What does the fuel cost you a month?", [("record_discovery", {"pain_point": "forty trucks, fuel"})])], tally=False)
    check("(before) the same turn without the tally is TWO requests", requests == 2, str(requests))

    requests, world = await requests_for([("", [("record_discovery", {"pain_point": "forty trucks, fuel"})]), ("Right, forty trucks. What does the fuel cost you?", [])])
    check("a recording call the model made without speaking is still two: the reply comes from the second", requests == 2 and "forty trucks" in str(world.record.pain_points), str(requests))

    requests, world = await requests_for([("Let me check Monday.", [("check_calendar_availability", {"day": "2026-09-07"})]), ("I have ten or half past ten on Monday.", [])])
    check("a calendar call is two requests even when the model spoke: it must see the times", requests == 2 and world.conversation.offered_slots, str(requests))

    requests, world = await requests_for([("Noted.", [("record_discovery", {"pain_point": "fuel"}), ("record_objection", {"kind": "PRICE"})]), ("Understood, let me answer that.", [])])
    check("two calls in one response: two requests", requests == 2, str(requests))

    requests, world = await requests_for([("Understood, thanks for your time.", [("set_interest", {"level": "NOT_INTERESTED"})]), ("Goodbye.", [("end_call", {})])])
    # Today end_call's own result also re-runs the LLM once more (a third
    # request), which is existing behaviour and left alone here.
    check("a no is at least two requests, so the goodbye and end_call follow", requests >= 2 and world.state is ConversationState.ENDING, str((requests, world.state.value)))


# --- Wiring ---------------------------------------------------------------------------------------


def check_wiring() -> None:
    print("\n=== wired into the bot ===")
    bot = (SERVER / "bot.py").read_text(encoding="utf-8")
    check("one tally per session", "speech = SpeechTally()" in bot)
    check("fed by an observer watching the LLM and the spoken-text filter", "SpeechObserver(speech, llm=llm, spoken_text=spoken_text)" in bot and "spoken_text = SpokenTextFilter()" in bot)
    check("attached to the conversation", "conversation.speech = speech" in bot)
    check("with the filter still in its place in the pipeline", "            spoken_text,\n            tts," in bot)
    playbook = (SERVER / "src" / "conversation" / "playbook.py").read_text(encoding="utf-8")
    check("the prompt lets the reply travel with the recording call", "put the reply in the same response as a recording call" in playbook and "Record FIRST, then reply" in playbook)
    validate = (SERVER / "validate.py").read_text(encoding="utf-8")
    check("the checks are in the validation run", '"test_tool_round_trips"' in validate)


async def main() -> int:
    print("Tool round-trip checks — no keys, no database, no network.")
    check_classification()
    await check_tally_and_filter()
    await check_wrapper()
    await check_persistence_once()
    await check_blocking_tools()
    await check_pipeline_requests()
    check_wiring()

    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
