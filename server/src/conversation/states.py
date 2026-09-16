"""The stages a cold call moves through, and the rules about moving between them.

**Conversation state is not prospect data, not campaign data, not the system
prompt, and not the RAG context.** It is one small thing: where this particular
call has got to. It is deliberately a separate object from all of those so that
a state transition cannot accidentally rewrite the prompt, and reading the
prospect's record cannot accidentally change the state.

Ten states, and the interesting part is not the list but the transition table.
Two rules in it are load-bearing:

* **`DO_NOT_CALL` is forced, from anywhere, and is nearly absorbing.** It is not
  in the ordinary table at all: `force_do_not_call` bypasses it, because a
  request not to be called again must be honoured from any stage including a
  stage the model has not reported yet. Once there, the only way out is
  `ENDING`.
* **`NOT_INTERESTED` cannot go back to selling.** From there the reachable
  states are `ENDING`, `CALLBACK` and `DO_NOT_CALL` — never
  `VALUE_PROPOSITION`, never `MEETING_REQUEST`. That is the requirement "never
  repeatedly push after a clear rejection" written as a table rather than as a
  sentence in a prompt, which means it holds whatever the model decides to do.

Every refused transition is recorded and logged rather than raising. A model
that asks for something the table forbids is a normal event in a live call, not
a crash: the state simply does not move, and the call carries on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from loguru import logger


class ConversationState(StrEnum):
    """Where a cold call has got to."""

    GREETING = "GREETING"
    """The opening. Identify, say why we are calling, earn the next ten seconds."""

    DISCOVERY = "DISCOVERY"
    """Asking about their situation. Questions, not pitching."""

    QUALIFICATION = "QUALIFICATION"
    """Establishing whether this is a fit: need, timing, authority, existing solution."""

    VALUE_PROPOSITION = "VALUE_PROPOSITION"
    """Explaining the part of what we do that answers what they just told us."""

    OBJECTION_HANDLING = "OBJECTION_HANDLING"
    """They pushed back. Acknowledge first, answer briefly, never argue."""

    MEETING_REQUEST = "MEETING_REQUEST"
    """Asking for the next step."""

    CALLBACK = "CALLBACK"
    """They want to be called another time. The call is over; the relationship is not."""

    NOT_INTERESTED = "NOT_INTERESTED"
    """A clear no. Stop selling, close warmly, leave the door open."""

    DO_NOT_CALL = "DO_NOT_CALL"
    """They asked never to be called again. Stop everything and honour it."""

    ENDING = "ENDING"
    """Saying goodbye. Terminal."""

    @property
    def is_terminal(self) -> bool:
        """Whether the conversation is over once it reaches this state."""
        return self is ConversationState.ENDING

    @property
    def is_rejection(self) -> bool:
        """Whether the prospect has said no in a way that must stop the pitch."""
        return self in _REJECTIONS

    @property
    def is_selling(self) -> bool:
        """Whether it is appropriate to be advancing a sale in this state.

        The single predicate the playbook and the tools consult before doing
        anything persuasive, so "stop selling after a no" is one check rather
        than a rule repeated in five places.
        """
        return self in _SELLING


_REJECTIONS = frozenset(
    {
        ConversationState.NOT_INTERESTED,
        ConversationState.DO_NOT_CALL,
    }
)

_SELLING = frozenset(
    {
        ConversationState.GREETING,
        ConversationState.DISCOVERY,
        ConversationState.QUALIFICATION,
        ConversationState.VALUE_PROPOSITION,
        ConversationState.OBJECTION_HANDLING,
        ConversationState.MEETING_REQUEST,
    }
)

# Where each state may go next. Anything not listed is refused.
#
# Read the rows for `NOT_INTERESTED` and `DO_NOT_CALL` first: they are the two
# that encode a promise to the person on the phone rather than a preference
# about how a sales call should flow.
_ALLOWED: dict[ConversationState, frozenset[ConversationState]] = {
    ConversationState.GREETING: frozenset(
        {
            ConversationState.DISCOVERY,
            ConversationState.QUALIFICATION,
            ConversationState.VALUE_PROPOSITION,
            ConversationState.OBJECTION_HANDLING,
            ConversationState.MEETING_REQUEST,
            ConversationState.CALLBACK,
            ConversationState.NOT_INTERESTED,
            ConversationState.ENDING,
        }
    ),
    ConversationState.DISCOVERY: frozenset(
        {
            ConversationState.QUALIFICATION,
            ConversationState.VALUE_PROPOSITION,
            ConversationState.OBJECTION_HANDLING,
            ConversationState.MEETING_REQUEST,
            ConversationState.CALLBACK,
            ConversationState.NOT_INTERESTED,
            ConversationState.ENDING,
        }
    ),
    ConversationState.QUALIFICATION: frozenset(
        {
            ConversationState.DISCOVERY,
            ConversationState.VALUE_PROPOSITION,
            ConversationState.OBJECTION_HANDLING,
            ConversationState.MEETING_REQUEST,
            ConversationState.CALLBACK,
            ConversationState.NOT_INTERESTED,
            ConversationState.ENDING,
        }
    ),
    ConversationState.VALUE_PROPOSITION: frozenset(
        {
            ConversationState.DISCOVERY,
            ConversationState.QUALIFICATION,
            ConversationState.OBJECTION_HANDLING,
            ConversationState.MEETING_REQUEST,
            ConversationState.CALLBACK,
            ConversationState.NOT_INTERESTED,
            ConversationState.ENDING,
        }
    ),
    # An objection is a detour, not a destination: every selling state it could
    # have come from is reachable again once it has been answered.
    ConversationState.OBJECTION_HANDLING: frozenset(
        {
            ConversationState.DISCOVERY,
            ConversationState.QUALIFICATION,
            ConversationState.VALUE_PROPOSITION,
            ConversationState.MEETING_REQUEST,
            ConversationState.CALLBACK,
            ConversationState.NOT_INTERESTED,
            ConversationState.ENDING,
        }
    ),
    ConversationState.MEETING_REQUEST: frozenset(
        {
            ConversationState.OBJECTION_HANDLING,
            ConversationState.VALUE_PROPOSITION,
            ConversationState.DISCOVERY,
            ConversationState.CALLBACK,
            ConversationState.NOT_INTERESTED,
            ConversationState.ENDING,
        }
    ),
    # A callback is agreed, so the call is winding up. Back to DISCOVERY is
    # allowed for the one real case: "actually, go on then, what is it about?"
    ConversationState.CALLBACK: frozenset(
        {
            ConversationState.DISCOVERY,
            ConversationState.NOT_INTERESTED,
            ConversationState.ENDING,
        }
    ),
    # The rejection row. No route back to VALUE_PROPOSITION or MEETING_REQUEST,
    # by design — that absence *is* "do not keep pushing".
    ConversationState.NOT_INTERESTED: frozenset(
        {
            ConversationState.CALLBACK,
            ConversationState.ENDING,
        }
    ),
    # Absorbing except for the goodbye.
    ConversationState.DO_NOT_CALL: frozenset({ConversationState.ENDING}),
    ConversationState.ENDING: frozenset(),
}


@dataclass(frozen=True)
class StateTransition:
    """One movement between states, kept so the call can be explained afterwards.

    Attributes:
        trigger: What caused it — the name of the tool the model called, or
            `"signal"` when a deterministic phrase detector forced it. Worth
            recording separately from the reason: "the model decided" and "we
            detected the words 'stop calling me'" are different kinds of
            evidence and an operator reviewing a do-not-call needs to know
            which one it was.
    """

    previous: ConversationState
    current: ConversationState
    reason: str = ""
    trigger: str = ""
    at: float = field(default_factory=time.monotonic)

    def describe(self) -> str:
        """One line for a log."""
        detail = f" ({self.reason})" if self.reason else ""
        via = f" via {self.trigger}" if self.trigger else ""
        return f"{self.previous.value} -> {self.current.value}{via}{detail}"


class ConversationStateMachine:
    """The current state of one call, and the only thing allowed to change it.

    Not thread-safe and not asyncio-locked, because every caller is on the
    pipeline's own event loop: tool handlers, the director processor and the
    session teardown all run there, one at a time.
    """

    def __init__(self, initial: ConversationState = ConversationState.GREETING) -> None:
        """Create the machine.

        Args:
            initial: Starting state. `GREETING` for a real call; the tests set
                it directly to reach a stage without replaying the whole call.
        """
        self._state = initial
        self._history: list[StateTransition] = []
        self._refused: list[tuple[ConversationState, ConversationState, str]] = []

    @property
    def state(self) -> ConversationState:
        """The state the call is in right now."""
        return self._state

    @property
    def history(self) -> list[StateTransition]:
        """Every accepted transition, oldest first."""
        return list(self._history)

    @property
    def refused(self) -> list[tuple[ConversationState, ConversationState, str]]:
        """Every transition the table refused, as `(from, to, reason)`.

        Kept rather than dropped because a refusal is a signal about the model:
        a call where the LLM repeatedly tried to move from `NOT_INTERESTED` back
        to `VALUE_PROPOSITION` is one where the prompt is not holding, and that
        is invisible if the refusals are silent.
        """
        return list(self._refused)

    @property
    def path(self) -> list[ConversationState]:
        """Every state the call has been in, in order, starting with the first."""
        if not self._history:
            return [self._state]
        return [self._history[0].previous, *(t.current for t in self._history)]

    def can(self, target: ConversationState) -> bool:
        """Whether `target` is reachable from the current state.

        `DO_NOT_CALL` always answers True: it is forced rather than permitted,
        and a caller checking first should never be told it cannot honour a
        do-not-call request.
        """
        if target is ConversationState.DO_NOT_CALL:
            return True
        return target in _ALLOWED[self._state]

    def transition(
        self,
        target: ConversationState,
        *,
        reason: str = "",
        trigger: str = "",
    ) -> bool:
        """Move to `target` if the table allows it.

        Returns:
            True if the state moved. False when the transition was refused, or
            when `target` is the state we are already in — in which case nothing
            is recorded, because a call that keeps re-entering `DISCOVERY`
            should not produce a history of forty identical entries.
        """
        if target is ConversationState.DO_NOT_CALL:
            return self.force_do_not_call(reason=reason, trigger=trigger)

        if target is self._state:
            return False

        if target not in _ALLOWED[self._state]:
            self._refused.append((self._state, target, reason))
            logger.info(
                f"STATE | refused {self._state.value} -> {target.value}"
                f"{f' ({reason})' if reason else ''}"
            )
            return False

        return self._apply(target, reason=reason, trigger=trigger)

    def force_do_not_call(self, *, reason: str = "", trigger: str = "") -> bool:
        """Move straight to `DO_NOT_CALL` from wherever we are.

        The one transition that ignores the table. A person asking not to be
        called again is entitled to that from any point in the call, including
        one where the model has not yet reported which stage it thinks it is in.

        Returns:
            False only when we were already there.
        """
        if self._state is ConversationState.DO_NOT_CALL:
            return False
        return self._apply(ConversationState.DO_NOT_CALL, reason=reason, trigger=trigger)

    def _apply(
        self, target: ConversationState, *, reason: str, trigger: str
    ) -> bool:
        transition = StateTransition(
            previous=self._state, current=target, reason=reason, trigger=trigger
        )
        self._state = target
        self._history.append(transition)
        logger.info(f"STATE | {transition.describe()}")
        return True
