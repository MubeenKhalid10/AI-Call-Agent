"""How many tokens the model reads per request, and where they go. Phase 31.

Run it from the `server/` directory::

    uv run python scripts/prompt_tokens.py                # the test campaign, every capability on
    uv run python scripts/prompt_tokens.py --deployment   # the campaign configured in .env
    uv run python scripts/prompt_tokens.py --json         # the same figures as JSON

It counts, with the Qwen3 tokenizer when one is in the Hugging Face cache
(Groq's own count runs about eight percent above it — chat-template overhead,
measured 2026-09-11) and otherwise with a four-characters-per-token estimate:

* the system instruction, section by section;
* the tool schemas as the OpenAI-format JSON the adapter sends, tool by tool,
  and the set advertised at each stage of the call;
* the per-turn guidance block at each stage;
* an estimated total per request at each stage, with a fixed allowance for
  the conversation history and tool results.

No keys and no network. It changes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-needed-by-this-script")
os.environ.setdefault("KB_ENABLED", "false")

from pipecat.adapters.schemas.tools_schema import ToolsSchema  # noqa: E402
from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter  # noqa: E402

from src.conversation import CallBrief, CampaignBrief, ProspectBrief, SalesConversation  # noqa: E402
from src.conversation.actions import Capabilities, NullActionBackend  # noqa: E402
from src.conversation.states import ConversationState  # noqa: E402
from src.conversation.tools import build_tools  # noqa: E402

#: Tokens allowed for the conversation history and tool results in the
#: "estimated total": what a mid-call request carried on 2026-09-11 (~5% and
#: ~4% of ~4,000).
HISTORY_ALLOWANCE = 400

STAGES = list(ConversationState)


class _Counter:
    """Tokens in a text: the Qwen3 tokenizer if cached, else characters / 4."""

    def __init__(self) -> None:
        self.exact = False
        self._tok = None
        try:
            from tokenizers import Tokenizer

            snapshots = Path.home() / ".cache/huggingface/hub/models--Qwen--Qwen3-32B/snapshots"
            found = next(snapshots.glob("*/tokenizer.json"), None)
            if found is not None:
                self._tok = Tokenizer.from_file(str(found))
                self.exact = True
        except Exception:  # noqa: BLE001 - the estimate is the fallback
            self._tok = None

    def __call__(self, text: str) -> int:
        if not text:
            return 0
        if self._tok is not None:
            return len(self._tok.encode(text).ids)
        return max(1, round(len(text) / 4))


class _Everything(NullActionBackend):
    """A backend that claims every capability, so the prompt is at its largest."""

    def __init__(self, timezone: str = "Asia/Karachi") -> None:
        super().__init__(timezone)
        self._all = Capabilities(
            can_search_knowledge=True,
            can_check_calendar=True,
            can_book_meeting=True,
            booking_requires_email=True,
            can_schedule_callback=True,
            can_transfer=True,
            timezone=timezone,
        )

    @property
    def capabilities(self) -> Capabilities:
        return self._all


def test_brief() -> CallBrief:
    """The campaign the check scripts use (Northwind Fleet), with a named prospect."""
    return CallBrief(
        prospect=ProspectBrief(prospect_id=7, first_name="Sarah", company="Meridian Logistics", job_title="Ops Director"),
        campaign=CampaignBrief(
            agent_name="Alex",
            company_name="Northwind Fleet",
            offer="fleet tracking that cuts fuel spend",
            value_points=["Customers typically cut fuel spend by about a tenth."],
            qualification_criteria=["They run a fleet of at least ten vehicles."],
            meeting_ask="a fifteen minute call with a specialist",
        ),
        campaign_id=3,
        call_attempt_id=11,
        source="campaign",
    )


def deployment_brief() -> CallBrief:
    """The campaign configured in `.env`, as `bot.py` builds it for a call with no campaign row."""
    from dotenv import load_dotenv

    load_dotenv(SERVER / ".env", override=True)
    from src.config import Config

    config = Config.from_env()
    sales = config.sales
    campaign = CampaignBrief(
        agent_name=sales.agent_name,
        company_name=sales.company_name,
        company_description=sales.company_description,
        services=list(sales.services),
        offer=sales.offer,
        value_points=list(sales.value_points),
        qualification_criteria=list(sales.qualification_criteria),
        meeting_ask=sales.meeting_ask,
        notes=list(sales.notes),
        disclosures=config.compliance_policy.disclosures(),
    )
    return CallBrief(prospect=ProspectBrief(prospect_id=7, first_name="Sarah"), campaign=campaign, source="environment")


def conversation_at(brief: CallBrief, state: ConversationState, *, spoken: bool = True) -> SalesConversation:
    """A conversation in `state`, with every capability, at a fixed moment."""
    conversation = SalesConversation(
        brief,
        actions=_Everything(),
        knowledge_base=True,
        timezone="Asia/Karachi",
        now=datetime(2026, 9, 14, 10, 0, tzinfo=UTC),
    )
    if spoken:
        # A prospect turn has happened: the opening is behind us.
        conversation.transcript.add_user("Hello, who is this?")
        conversation._user_turns = 1  # noqa: SLF001 - measurement only
    if state is not ConversationState.GREETING:
        moved = conversation.machine.transition(state, reason="measurement", trigger="measurement")
        if not moved:
            # Not reachable from GREETING in one move (DO_NOT_CALL): go via DISCOVERY.
            conversation.machine.transition(ConversationState.DISCOVERY, reason="measurement", trigger="measurement")
            conversation.machine.transition(state, reason="measurement", trigger="measurement")
    return conversation


def tool_json(schemas: list[Any]) -> list[tuple[str, str]]:
    """Each tool as the compact JSON the OpenAI-format adapter sends: (name, json)."""
    if not schemas:
        return []
    provider = OpenAILLMAdapter().to_provider_tools_format(ToolsSchema(standard_tools=list(schemas)))
    return [
        (tool["function"]["name"], json.dumps(tool, separators=(",", ":"), ensure_ascii=False))
        for tool in provider
    ]


def advertised(conversation: SalesConversation) -> list[Any]:
    """The tools the model sees at this point of the call: the stage set when the code has one, else all."""
    if hasattr(conversation, "advertised_tools"):
        tools = conversation.advertised_tools()
        return list(tools) if tools else []
    return build_tools(conversation)


def measure(brief: CallBrief) -> dict[str, Any]:
    count = _Counter()
    base = conversation_at(brief, ConversationState.DISCOVERY)
    instruction = base.system_instruction()
    sections = [s for s in instruction.split("\n\n") if s.strip()]
    system = {
        "tokens": count(instruction),
        "words": len(instruction.split()),
        "sections": [{"head": s.splitlines()[0][:48], "tokens": count(s)} for s in sections],
    }

    every_tool = tool_json(build_tools(base))
    tools_all = {name: count(text) for name, text in every_tool}

    stages = []
    for state in STAGES:
        conversation = conversation_at(brief, state)
        names = [name for name, _ in tool_json(advertised(conversation))]
        tools = sum(tools_all.get(n, 0) for n in names)
        guidance = count(conversation.guidance())
        stages.append(
            {
                "stage": state.value,
                "tools": names,
                "tool_tokens": tools,
                "guidance_tokens": guidance,
                "fixed_tokens": system["tokens"] + tools + guidance,
                "estimated_total": system["tokens"] + tools + guidance + HISTORY_ALLOWANCE,
            }
        )
    opening = conversation_at(brief, ConversationState.GREETING, spoken=False)
    opening_names = [name for name, _ in tool_json(advertised(opening))]
    return {
        "tokenizer": "Qwen3 (cached)" if count.exact else "estimate: characters / 4",
        "system": system,
        "tools_all": tools_all,
        "tools_all_total": sum(tools_all.values()),
        "opening_tools": opening_names,
        "opening_tool_tokens": sum(tools_all.get(n, 0) for n in opening_names),
        "stages": stages,
        "history_allowance": HISTORY_ALLOWANCE,
    }


def print_report(report: dict[str, Any]) -> None:
    print(f"Tokenizer: {report['tokenizer']}")
    system = report["system"]
    print(f"\nSYSTEM INSTRUCTION: {system['tokens']} tokens ({system['words']} words)")
    for section in system["sections"]:
        print(f"  {section['tokens']:>5}  {section['head']}")
    print(f"\nTOOL SCHEMAS (OpenAI-format JSON, before the provider's template): {report['tools_all_total']} tokens for all {len(report['tools_all'])}")
    for name, tokens in sorted(report["tools_all"].items(), key=lambda kv: -kv[1]):
        print(f"  {tokens:>5}  {name}")
    print(f"\nOPENING TURN (before the prospect has spoken): {len(report['opening_tools'])} tool(s), {report['opening_tool_tokens']} tokens")
    print(f"\nPER STAGE (system + tools + guidance; estimated total adds {report['history_allowance']} for history and tool results)")
    print(f"  {'stage':<20} {'tools':>5} {'tool tok':>8} {'guide':>6} {'fixed':>6} {'est. total':>10}")
    for row in report["stages"]:
        print(f"  {row['stage']:<20} {len(row['tools']):>5} {row['tool_tokens']:>8} {row['guidance_tokens']:>6} {row['fixed_tokens']:>6} {row['estimated_total']:>10}")
    for row in report["stages"]:
        print(f"  {row['stage']:<20} {', '.join(row['tools']) or '(none)'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deployment", action="store_true", help="measure the campaign configured in .env")
    parser.add_argument("--json", action="store_true", help="print the figures as JSON")
    args = parser.parse_args()
    brief = deployment_brief() if args.deployment else test_brief()
    report = measure(brief)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
