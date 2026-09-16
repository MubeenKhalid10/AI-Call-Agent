"""The sales conversation: what the agent is trying to do, and what it learned.

The public surface of the package. Import from here rather than from the modules
underneath, so the internal split can change without every caller moving.

    from src.conversation import CallBrief, SalesConversation, ConversationDirector

    conversation = SalesConversation(brief, sink=sink, actions=backend, knowledge_base=True)
    llm = make_llm(config, system_instruction=conversation.system_instruction())
    context = LLMContext(tools=conversation.tools())
    # ... pipeline: user_aggregator -> retriever -> ConversationDirector(conversation) -> llm

**How the pieces divide, and why.**

| Module            | Knows about                                              |
|-------------------|----------------------------------------------------------|
| `states.py`       | The ten stages and which moves between them are allowed.  |
| `qualification.py`| What the call learned. Unknown is a value, never a blank. |
| `brief.py`        | Who is being called and on whose behalf. Pure data.       |
| `playbook.py`     | Every word the model is told. No logic.                   |
| `signals.py`      | Phrases that must be acted on whatever the model does.    |
| `results.py`      | The one shape a tool result takes, and the error codes.   |
| `actions.py`      | The contract with whatever acts on the world. Protocol.   |
| `timeparse.py`    | Strict readers for the dates and times the model sends.   |
| `toolkit.py`      | The tool boundary: schema, argument checks, guard, log.   |
| `tools.py`        | The functions the model calls. Thin wrappers, no policy.  |
| `conversation.py` | The rules, and the only thing that changes any state.     |
| `director.py`     | The pipeline stage. Attaches guidance; owns no state.     |
| `sources.py`      | Turning media-stream ids into a brief.                    |
| `sink.py`         | Where consequences leave this package after the call.     |
| `transcript.py`   | What was said, verbatim and in order. Evidence, not a view.|

**Nothing in here imports the campaign database, the calendar, the carrier, or
`bot.py`.** The only three ways out are `ProspectSource` (ids in, brief out),
`ConversationSink` (facts out, after the call) and `ActionBackend` (act now,
plain data in and out) — all Protocols with plain-data signatures, implemented in
`src/campaigns/briefing.py` and `src/actions/`. That is what keeps the phase's
architecture rule true rather than aspirational::

    conversation state  ≠  LLM prompt  ≠  prospect database  ≠  RAG  ≠  telephony  ≠  calendar

The Pipecat imports are in `director.py`, `tools.py` and `toolkit.py`, which
have to be a frame processor, push an end frame, and build function schemas
respectively. Everything else in the package is testable with no pipeline, no
database, no keys and no network — which is what `tests/test_conversation.py`
and `tests/test_actions.py` are.
"""

from __future__ import annotations

from .actions import (
    ActionBackend,
    ActionOutcome,
    AttendeeDetails,
    Capabilities,
    NullActionBackend,
)
from .brief import CallBrief, CampaignBrief, ProspectBrief
from .conversation import SalesConversation
from .director import ConversationDirector
from .playbook import INSTRUCTION_PREFIX, build_system_instruction, opening_instruction, stage_block
from .qualification import (
    BuyingTimeline,
    DecisionRole,
    Intent,
    InterestLevel,
    NextAction,
    Objection,
    ObjectionKind,
    QualificationRecord,
    QualificationStatus,
)
from .results import ERROR_CODES, ActionRecord, ToolResult
from .signals import Signal, SignalReport, detect, normalize
from .sink import ConversationSink, LoggingSink
from .sources import (
    PARAM_ATTEMPT_ID,
    PARAM_CAMPAIGN_ID,
    PARAM_PROSPECT_ID,
    CallIdentifiers,
    ProspectSource,
    identifiers_from_runner_args,
    resolve_brief,
)
from .states import ConversationState, ConversationStateMachine, StateTransition
from .toolkit import AuditContext, strict_tool, validate_arguments
from .transcript import Transcript, TranscriptEntry, render_transcript

__all__ = [
    "ERROR_CODES",
    "INSTRUCTION_PREFIX",
    "PARAM_ATTEMPT_ID",
    "PARAM_CAMPAIGN_ID",
    "PARAM_PROSPECT_ID",
    "ActionBackend",
    "ActionOutcome",
    "ActionRecord",
    "AttendeeDetails",
    "AuditContext",
    "BuyingTimeline",
    "CallBrief",
    "CallIdentifiers",
    "CampaignBrief",
    "Capabilities",
    "ConversationDirector",
    "ConversationSink",
    "ConversationState",
    "ConversationStateMachine",
    "DecisionRole",
    "Intent",
    "InterestLevel",
    "LoggingSink",
    "NextAction",
    "NullActionBackend",
    "Objection",
    "ObjectionKind",
    "ProspectBrief",
    "ProspectSource",
    "QualificationRecord",
    "QualificationStatus",
    "SalesConversation",
    "Signal",
    "SignalReport",
    "StateTransition",
    "ToolResult",
    "Transcript",
    "TranscriptEntry",
    "build_system_instruction",
    "detect",
    "identifiers_from_runner_args",
    "normalize",
    "opening_instruction",
    "render_transcript",
    "resolve_brief",
    "stage_block",
    "strict_tool",
    "validate_arguments",
]
