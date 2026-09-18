#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for Phase 31: the prompt budget and per-stage tool advertising.

Run it from the `server/` directory::

    uv run python tests/test_tool_advertising.py

No keys, no database, no network. What is under test:

* which tools the model is shown at each stage of the call, through the
  real `SalesConversation` driven the way the director and Pipecat drive it
  (the detectors on what the prospect says, the real tool handlers with the
  model's arguments) — and that the tools *not* shown are still registered
  and still run;
* that Pipecat's per-context handler sync never prunes an explicitly
  registered handler — the race Phases 7 and 11 recorded as the reason not
  to do this — driven directly on a real LLM service, with the auto-registered
  path alongside to show what the explicit path avoids;
* that booking, transfer, objections, discovery, callbacks and `end_call`
  still execute, each from a stage that advertises it, and that the tool set
  the *next* request advertises is refreshed on the request's context before
  the result is delivered;
* the compacted system instruction: every rule still in it, the campaign
  context still in it, no leakage between campaigns, and its size;
* the wiring in `bot.py` and the director, by source.

Token figures use the Qwen3 tokenizer when it is in the Hugging Face cache,
and a characters-per-token estimate otherwise; the ceilings are loose so a
different tokenizer does not fail them. A plain script rather than a pytest
suite because the project has no test dependency and this needs none. Exit
status is 0 when everything passes.
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

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from pipecat.adapters.schemas.tools_schema import ToolsSchema  # noqa: E402
from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter  # noqa: E402
from pipecat.frames.frames import EndWorkerFrame  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.services.groq.llm import GroqLLMService  # noqa: E402
from pipecat.services.llm_service import FunctionCallParams  # noqa: E402
from pipecat.utils.types import NOT_GIVEN  # noqa: E402

from scripts.prompt_tokens import _Counter, measure, test_brief  # noqa: E402
from src.conversation import CallBrief, CampaignBrief, ConversationDirector, ProspectBrief, SalesConversation  # noqa: E402
from src.conversation.actions import Capabilities  # noqa: E402
from src.conversation.qualification import Intent, QualificationRecord  # noqa: E402
from src.conversation.signals import Signal  # noqa: E402
from src.conversation.states import ConversationState  # noqa: E402
from src.conversation.tools import TOOL_NAMES, advertised_tool_names, build_tools  # noqa: E402
from test_actions import FakeKnowledge, FakeLLM, FakeTelephony, World, phone_call  # noqa: E402

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def names(tools: list[Any]) -> list[str]:
    return [t.name for t in tools]


def everything() -> World:
    """A fully wired session: knowledge base, calendar, scheduler, a phone call with a transfer number."""
    return World(
        knowledge=FakeKnowledge([]),
        telephony=FakeTelephony(),
        call=phone_call(),
        transfer_number="+923009999999",
    )


async def call_with_context(world: World, context: LLMContext, name: str, **arguments: Any) -> dict[str, Any]:
    """One tool call through the real handler, with a context the caller keeps hold of."""
    captured: dict[str, Any] = {}

    async def result_callback(result: Any, *args: Any, **kw: Any) -> None:
        captured.update(result if isinstance(result, dict) else {"result": result})

    params = FunctionCallParams(
        function_name=name,
        tool_call_id=f"call-{len(world.results)}",
        arguments=arguments,
        llm=world.llm,
        pipeline_worker=None,
        context=context,
        result_callback=result_callback,
    )
    await world.tools[name].handler(params)
    world.results.append(captured)
    return captured


def tool_names_on(context: LLMContext) -> list[str]:
    tools = context.tools
    if tools is NOT_GIVEN or tools is None:
        return []
    return [t.name for t in tools.standard_tools]


# --- The table -----------------------------------------------------------------------


def check_table() -> None:
    print("\n=== which tools each stage shows ===")
    full = Capabilities(
        can_search_knowledge=True, can_check_calendar=True, can_book_meeting=True,
        can_schedule_callback=True, can_transfer=True, timezone="Asia/Karachi",
    )
    none = Capabilities()
    record = QualificationRecord()

    def at(state: ConversationState, caps: Capabilities = full, *, spoken: bool = True, slots: bool = False, signals=frozenset(), rec: QualificationRecord = record) -> list[str]:
        return advertised_tool_names(state, rec, caps, spoken=spoken, slots_offered=slots, signals=signals)

    check("before the prospect has spoken, nothing is advertised", at(ConversationState.GREETING, spoken=False) == [])
    greeting = at(ConversationState.GREETING)
    check("the greeting shows the recording tools and the calendar, not the stage mover", greeting == ["record_discovery", "set_interest", "record_objection", "search_knowledge_base", "check_calendar_availability", "mark_do_not_call"], str(greeting))
    discovery = at(ConversationState.DISCOVERY)
    check("discovery adds move_to_stage", "move_to_stage" in discovery and "book_meeting" not in discovery and "end_call" not in discovery, str(discovery))
    for state in (ConversationState.QUALIFICATION, ConversationState.VALUE_PROPOSITION, ConversationState.OBJECTION_HANDLING):
        check(f"{state.value} shows the same set as discovery", at(state) == discovery, str(at(state)))
    check("book_meeting appears once times have been offered", "book_meeting" in at(ConversationState.DISCOVERY, slots=True))
    meeting = at(ConversationState.MEETING_REQUEST)
    check("the meeting stage shows both calendar tools and end_call", {"check_calendar_availability", "book_meeting", "end_call"} <= set(meeting) and "schedule_callback" not in meeting, str(meeting))
    check("without a calendar the meeting tools give way to request_meeting", "request_meeting" in at(ConversationState.DISCOVERY, none) and "check_calendar_availability" not in at(ConversationState.DISCOVERY, none) and "search_knowledge_base" not in at(ConversationState.DISCOVERY, none))
    check("a heard callback request shows schedule_callback", "schedule_callback" in at(ConversationState.DISCOVERY, signals=frozenset({Signal.CALLBACK})))
    check("so does a named time", "schedule_callback" in at(ConversationState.VALUE_PROPOSITION, signals=frozenset({Signal.MENTIONED_TIME})))
    check("and a recorded callback intent", "schedule_callback" in at(ConversationState.DISCOVERY, rec=QualificationRecord(callback_intent=Intent.ACCEPTED)))
    check("otherwise it is not shown while selling", "schedule_callback" not in discovery)
    booked = QualificationRecord(meeting_booked=True)
    check("a booked meeting shows end_call", "end_call" in at(ConversationState.DISCOVERY, rec=booked))
    asked = QualificationRecord(human_requested=True)
    check("asking for a person shows transfer_to_human on a transferable call", "transfer_to_human" in at(ConversationState.DISCOVERY, rec=asked))
    check("but not on a session that cannot transfer", "transfer_to_human" not in at(ConversationState.DISCOVERY, none, rec=asked))
    check("and not once they have asked not to be called", "transfer_to_human" not in at(ConversationState.DO_NOT_CALL, rec=asked))
    check("the callback stage: schedule, a change of mind, do-not-call, end", at(ConversationState.CALLBACK) == ["set_interest", "schedule_callback", "mark_do_not_call", "end_call"], str(at(ConversationState.CALLBACK)))
    check("not interested: the no, do-not-call, end", at(ConversationState.NOT_INTERESTED) == ["set_interest", "mark_do_not_call", "end_call"], str(at(ConversationState.NOT_INTERESTED)))
    check("do-not-call: mark it, end", at(ConversationState.DO_NOT_CALL) == ["mark_do_not_call", "end_call"], str(at(ConversationState.DO_NOT_CALL)))
    check("ending: end (and a last-second do-not-call)", at(ConversationState.ENDING) == ["mark_do_not_call", "end_call"], str(at(ConversationState.ENDING)))
    check("every name is a real tool, in the order the model sees them", all(name in TOOL_NAMES for state in ConversationState for name in at(state)) and all(at(state) == [n for n in TOOL_NAMES if n in at(state)] for state in ConversationState))
    check("no stage shows all twelve", all(len(at(state, slots=True, rec=asked)) < len(TOOL_NAMES) for state in ConversationState))
    check("the closing stages show at most four", all(len(at(state)) <= 4 for state in (ConversationState.CALLBACK, ConversationState.NOT_INTERESTED, ConversationState.DO_NOT_CALL, ConversationState.ENDING)))


# --- Through the real conversation --------------------------------------------------------


async def check_conversation_advertises() -> None:
    print("\n=== the conversation, driven as the pipeline drives it ===")
    world = everything()
    conversation = world.conversation
    every = conversation.tools()
    check("every tool is still built, each with its handler", len(every) == 12 and all(t.handler is not None for t in every), str(names(every)))
    check("nothing is advertised before the prospect speaks", conversation.advertised_tools() == [])

    await world.says("Hello, who is this?")
    shown = conversation.advertised_tools()
    check("after their first words the greeting set is advertised", names(shown) == ["record_discovery", "set_interest", "record_objection", "search_knowledge_base", "check_calendar_availability", "mark_do_not_call"], str(names(shown)))
    check("as handler-less copies", all(t.handler is None for t in shown))
    by_name = {t.name: t for t in every}
    check("with the same description and arguments as the real tools", all(t.description == by_name[t.name].description and t.properties == by_name[t.name].properties and list(t.required) == list(by_name[t.name].required) for t in shown))
    check("that a context accepts", tool_names_on(LLMContext(tools=shown)) == names(shown))
    provider = OpenAILLMAdapter().to_provider_tools_format(ToolsSchema(standard_tools=shown))
    check("and the OpenAI-format adapter renders", [t["function"]["name"] for t in provider] == names(shown))

    # Discovery, through the real handler, with the context the request would carry.
    context = LLMContext(tools=conversation.advertised_tools())
    await world.says("We run about forty trucks and the fuel bill is out of control.")
    result = await call_with_context(world, context, "record_discovery", pain_point="fuel bill out of control")
    check("discovery still records", result["success"] and result["data"].get("recorded") and "fuel bill out of control" in str(world.record.pain_points))
    check("and the call moved to discovery", world.state is ConversationState.DISCOVERY)
    check("the request's context now advertises discovery's set, ahead of the result", "move_to_stage" in tool_names_on(context) and "book_meeting" not in tool_names_on(context), str(tool_names_on(context)))

    # An objection.
    result = await call_with_context(world, context, "record_objection", kind="PRICE", detail="too expensive")
    check("objections still record", result["success"] and world.state is ConversationState.OBJECTION_HANDLING)
    check("and record_objection stays advertised for the next one", "record_objection" in tool_names_on(context))

    # A named day: the calendar tool is already there; the check offers times, which unlocks booking.
    await world.says("Would Monday morning work on your side?")
    check("a named day keeps check_calendar_availability advertised", "check_calendar_availability" in names(conversation.advertised_tools()))
    check("and book_meeting is not yet, with no times offered", "book_meeting" not in names(conversation.advertised_tools()))
    result = await call_with_context(world, context, "check_calendar_availability", day="2026-09-07")
    slots = result["data"]["slots"]
    check("the calendar still answers", result["success"] and slots, str(result.get("message")))
    check("and once times are offered the request's context advertises book_meeting", "book_meeting" in tool_names_on(context), str(tool_names_on(context)))
    result = await call_with_context(world, context, "book_meeting", start=slots[0]["start"], attendee_email="sarah@example.com")
    check("booking still executes", result["success"] and world.record.meeting_booked, str(result.get("message")))
    check("after which end_call is advertised", "end_call" in tool_names_on(context), str(tool_names_on(context)))

    # Not interested, from a fresh call: the no makes end_call available in the same turn.
    world = everything()
    await world.says("Hello?")
    context = LLMContext(tools=world.conversation.advertised_tools())
    check("end_call is not advertised while selling", "end_call" not in tool_names_on(context))
    result = await call_with_context(world, context, "set_interest", level="NOT_INTERESTED", reason="not for us")
    check("a clear no still executes", result["success"] and world.state is ConversationState.NOT_INTERESTED)
    check("and the request that answers it advertises end_call", tool_names_on(context) == ["set_interest", "mark_do_not_call", "end_call"], str(tool_names_on(context)))
    result = await call_with_context(world, context, "end_call", reason="not interested")
    check("end_call still executes and ends the pipeline", result.get("success") and any(isinstance(f, EndWorkerFrame) for f in world.llm.frames))

    # A callback.
    world = everything()
    await world.says("Hi")
    check("schedule_callback is not advertised until it is asked for", "schedule_callback" not in names(world.conversation.advertised_tools()))
    await world.says("Can you call me back Tuesday at ten instead?")
    check("asking for a callback advertises schedule_callback", "schedule_callback" in names(world.conversation.advertised_tools()))
    context = LLMContext(tools=world.conversation.advertised_tools())
    result = await call_with_context(world, context, "schedule_callback", when="2026-09-08T10:00")
    check("callbacks still schedule", result["success"] and world.state is ConversationState.CALLBACK, str(result.get("message")))
    check("and the callback stage advertises its four", tool_names_on(context) == ["set_interest", "schedule_callback", "mark_do_not_call", "end_call"], str(tool_names_on(context)))

    # A transfer.
    world = everything()
    await world.says("Hello")
    check("transfer_to_human is not advertised until they ask for a person", "transfer_to_human" not in names(world.conversation.advertised_tools()))
    await world.says("Can I speak to a real person please?")
    check("asking for a person advertises it", "transfer_to_human" in names(world.conversation.advertised_tools()))
    result = await world.calls("transfer_to_human", reason="asked for a person")
    check("transfer still executes", result["success"], str(result.get("message")))

    # Do-not-call, heard by the detectors before the model replies.
    world = everything()
    await world.says("Hello")
    await world.says("Take me off your list please.")
    check("a do-not-call leaves mark_do_not_call and end_call", names(world.conversation.advertised_tools()) == ["mark_do_not_call", "end_call"], str(names(world.conversation.advertised_tools())))
    result = await world.calls("mark_do_not_call", reason="asked")
    check("and mark_do_not_call still executes", result["success"] or result["error_code"] == "not_stored", str(result))

    # A tool the stage does not show still runs if the model calls it: the
    # handler is registered for the call, not per request.
    world = everything()
    await world.says("Hello")
    await world.calls("set_interest", level="NOT_INTERESTED")
    check("a tool that is not advertised in this stage still runs", (await world.calls("record_discovery", pain_point="late"))["success"])


# --- Pipecat's registry: the race, driven directly --------------------------------------


def check_registry() -> None:
    print("\n=== Pipecat's handler sync: explicit registrations survive, auto-registered ones do not ===")
    world = everything()
    conversation = world.conversation
    llm = GroqLLMService(api_key="not-a-key", model="qwen/qwen3.8-27b")
    for schema in conversation.tools():
        llm.register_function(schema.name, schema.handler)
    check("every handler is registered explicitly", all(llm.has_function(n) for n in TOOL_NAMES))

    world.conversation.transcript.add_user("hello")
    world.conversation._user_turns = 1  # noqa: SLF001 - as if the prospect had spoken
    subset = conversation.advertised_tools()
    llm._sync_registered_tool_handlers(subset)  # noqa: SLF001 - what Pipecat runs on every context frame
    check("a context advertising the greeting's six leaves all twelve registered", all(llm.has_function(n) for n in TOOL_NAMES), str(names(subset)))
    llm._sync_registered_tool_handlers(NOT_GIVEN)  # noqa: SLF001
    check("a context advertising nothing (the opening) leaves all twelve registered", all(llm.has_function(n) for n in TOOL_NAMES))
    llm._sync_registered_tool_handlers([])  # noqa: SLF001
    check("and so does an empty list", all(llm.has_function(n) for n in TOOL_NAMES))

    # The path Phases 7 and 11 were right to refuse: handler-carrying schemas
    # auto-register, and a later context that drops one unregisters it.
    auto = GroqLLMService(api_key="not-a-key", model="qwen/qwen3.8-27b")
    auto._sync_registered_tool_handlers(build_tools(conversation))  # noqa: SLF001
    check("(contrast) advertising handler-carrying schemas auto-registers them", all(auto.has_function(n) for n in TOOL_NAMES))
    three = [t for t in build_tools(conversation) if t.name in ("record_discovery", "set_interest", "end_call")]
    auto._sync_registered_tool_handlers(three)  # noqa: SLF001
    check("(contrast) and a later context with three of them prunes the other nine", auto.has_function("record_discovery") and not auto.has_function("book_meeting") and not auto.has_function("mark_do_not_call"))
    check("which is why bot.py registers explicitly and advertises handler-less copies", all(t.handler is None for t in subset))


# --- The director ----------------------------------------------------------------------


async def check_director() -> None:
    print("\n=== the director advertises the stage's tools on the context and on its copy ===")
    world = everything()
    director = ConversationDirector(world.conversation)
    context = LLMContext(messages=[{"role": "user", "content": world.conversation.opening()}], tools=world.conversation.advertised_tools())
    check("the opening's context carries no tools", tool_names_on(context) == [])
    guided = director._guided(context)  # noqa: SLF001 - the copy the LLM receives
    check("nor does the opening's copy", tool_names_on(guided) == [])

    context.add_message({"role": "user", "content": "We run forty trucks."})
    await director._note_new_turn(context)  # noqa: SLF001
    guided = director._guided(context)  # noqa: SLF001
    check("after a caller turn the copy advertises the stage's set", "record_discovery" in tool_names_on(guided) and "end_call" not in tool_names_on(guided), str(tool_names_on(guided)))
    check("set on the context itself first, so a tool-result request starts from it", tool_names_on(context) == tool_names_on(guided) and guided.tools is context.tools)
    check("the guidance block is still the last message", guided.messages[-1]["role"] == "user" and "Stage:" in guided.messages[-1]["content"])

    context.add_message({"role": "user", "content": "Take me off your list please."})
    await director._note_new_turn(context)  # noqa: SLF001
    guided = director._guided(context)  # noqa: SLF001
    check("a do-not-call turn narrows the next request to its two", tool_names_on(guided) == ["mark_do_not_call", "end_call"], str(tool_names_on(guided)))


# --- The prompt ------------------------------------------------------------------------


def check_prompt() -> None:
    print("\n=== the compacted system instruction ===")
    from datetime import UTC, datetime

    world = everything()
    instruction = world.conversation.system_instruction()
    for rule, needle in (
        ("never claims to be human", "Never claim to be a human being"),
        ("never invents a product fact", "Never invent a fact about this business"),
        ("never invents prospect detail", "Never invent anything about the person"),
        ("only believes a tool's success flag", "unless a tool has just answered with success true"),
        ("treats a failed tool as nothing happened", "nothing happened: say so plainly"),
        ("never promises unsupported outcomes", "Never promise a result"),
        ("accepts a no the first time", "accept it the first time"),
        ("honours a do-not-call", "not to be called again"),
        ("speaks without markdown", "never use emoji, bullet points, markdown"),
        ("keeps turns short", "One to three sentences per turn"),
        ("asks before pitching", "Discovery before pitching"),
        ("acknowledges an objection first", "acknowledge it in their own words first"),
        ("records first, then replies", "Record FIRST, then reply"),
        ("handles being interrupted", "A reply you were cut off in is over"),
        ("offers a transfer when it can", "connect them to a colleague right now"),
        ("checks the calendar before offering times", "check_calendar_availability for the day they prefer"),
        ("books only once they choose", "book_meeting once they choose"),
        ("schedules callbacks with an exact time", "schedule_callback with it as YYYY-MM-DDTHH:MM"),
        ("searches the knowledge base for detail", "search_knowledge_base"),
        ("never reads a tool result aloud", "Never mention the tools or read a result aloud"),
        ("answers from the block and the campaign facts only", "Answer from those and nothing else"),
        ("says when it does not have something", "do not have that in front of you"),
    ):
        check(f"it still says the agent {rule}", needle in instruction)
    check("campaign context is still in it", "WHY YOU ARE CALLING" in instruction and "Northwind" in instruction)
    check("the prospect is still in it", "Sarah" in instruction and "NOT KNOWN" in instruction)
    check("the time is still in it", "THE TIME NOW" in instruction and "YYYY-MM-DD" in instruction)

    bare = SalesConversation(CallBrief(campaign=CampaignBrief(agent_name="Alex", company_name="Northwind")), knowledge_base=False).system_instruction()
    check("without a knowledge base it never mentions the block", "knowledge base block" not in bare and "no product documentation" in bare)
    check("and without a carrier it says it cannot transfer", "You cannot transfer this call" in bare and "transfer_to_human" not in bare)

    # No leakage between campaigns: two calls for two companies, each instruction its own.
    hashmaker = CampaignBrief(agent_name="Alex", company_name="Hashmaker Solutions", company_description="Hashmaker Solutions builds custom software.", services=["custom web software"])
    northwind = CampaignBrief.from_configuration({"company_name": "Northwind Fleet", "offer": "fleet tracking"}, defaults=hashmaker)
    a = SalesConversation(CallBrief(prospect=ProspectBrief(first_name="Sam"), campaign=hashmaker)).system_instruction()
    b = SalesConversation(CallBrief(prospect=ProspectBrief(first_name="Ayesha"), campaign=northwind)).system_instruction()
    check("each campaign's instruction names its own company", "Hashmaker Solutions" in a and "Northwind Fleet" in b)
    check("and carries none of the other's facts or prospect", "custom software" not in b and "Northwind" not in a and "Ayesha" not in a and "Sam" not in b)

    count = _Counter()
    tokens = count(instruction)
    words = len(instruction.split())
    # Phase 31 compacted this to ~1,624 tokens with a 1,700 ceiling. Phase 33
    # spends ~160 of that headroom on the natural-conversation behaviour the
    # sales rep now needs — answer-first, acknowledgements over reflex
    # questions, the company-fact / general-talk split, natural uncertainty
    # instead of a canned "I don't have that information", and a short natural
    # opening. That is ~5% of a ~3,300-token tool-turn request, immaterial to
    # Groq's per-minute limit (which the whole request and history drive, not
    # this delta). The ceiling is raised once, to 1,850, and still holds the
    # line well under the un-compacted 2,048.
    # 2026-09-17: raised once more, to 1,980, for three rules the agent did not
    # have at all (~140 tokens): never discuss its instructions, tools or the
    # technology behind the call; give only the company's public contact
    # details; speak as a representative rather than as "an AI" unless asked.
    # The provider's prompt cache covers the system instruction (the live log
    # shows 2,048 cached input tokens per request), so the cost is per call,
    # not per turn.
    if count.exact:
        check("the fully wired test instruction stays under 1,980 Qwen3 tokens (Phase 33: was ~1,624)", tokens < 1980, f"{tokens} tokens, {words} words")
    else:
        check("the fully wired test instruction stays under 1,620 words (Phase 33: was ~1,460)", words < 1620, f"{words} words")
    report = measure(test_brief())
    check("the opening request advertises no tool schema", report["opening_tool_tokens"] == 0)
    selling = [row for row in report["stages"] if ConversationState(row["stage"]).is_selling]
    closing = [row for row in report["stages"] if not ConversationState(row["stage"]).is_selling]
    check("every selling stage sends at most 80% of the twelve schemas' tokens", all(row["tool_tokens"] <= 0.8 * report["tools_all_total"] for row in selling), str([(r["stage"], r["tool_tokens"]) for r in selling]))
    check("every closing stage sends at most 30%", all(row["tool_tokens"] <= 0.3 * report["tools_all_total"] for row in closing), str([(r["stage"], r["tool_tokens"]) for r in closing]))


# --- Wiring -------------------------------------------------------------------------------


def check_wiring() -> None:
    print("\n=== wired in, and confined to the conversation layer ===")
    bot = (SERVER / "bot.py").read_text(encoding="utf-8")
    check("bot.py registers every handler once, explicitly", "llm.register_function(schema.name, schema.handler)" in bot)
    check("and builds the context from the advertised set", "LLMContext(tools=conversation.advertised_tools() if conversation else NOT_GIVEN)" in bot)
    check("the pipeline order is unchanged", "            llm,\n            spoken_text,\n            tts," in bot and "director" in bot)
    director = (SERVER / "src" / "conversation" / "director.py").read_text(encoding="utf-8")
    check("the director sets the stage's tools on the context", "context.set_tools(self._conversation.advertised_tools())" in director)
    toolkit = (SERVER / "src" / "conversation" / "toolkit.py").read_text(encoding="utf-8")
    check("the tool wrapper refreshes the set after the tool ran", "_refresh_advertised(params, advertise, name)" in toolkit)
    for folder in ("campaigns", "automation", "crm", "app"):
        sources = "".join(p.read_text(encoding="utf-8") for p in (SERVER / "src" / folder).glob("*.py"))
        check(f"src/{folder} is untouched by it", "advertised_tools" not in sources and "advertised_tool_names" not in sources)
    validate = (SERVER / "validate.py").read_text(encoding="utf-8")
    check("the checks are in the validation run", '"test_tool_advertising"' in validate)


async def main() -> int:
    print("Prompt budget and tool advertising checks — no keys, no database, no network.")
    check_table()
    await check_conversation_advertises()
    check_registry()
    await check_director()
    check_prompt()
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
