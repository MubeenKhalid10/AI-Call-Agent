"""The one shape every tool result takes, and the vocabulary of ways it can fail.

Phase 7 gives the model tools that *act* — book a meeting, schedule a callback,
transfer the call — and the rule that governs all of them is that the model must
never mistake an ambiguous answer for success. So there is exactly one result
shape, it always carries an explicit `success` boolean, and a failure always
carries a machine-readable `error_code` plus a sentence the model can act on::

    {"success": true,  "data": {...}, "error_code": null,   "message": null,   "guidance": "..."}
    {"success": false, "data": null,  "error_code": "...",  "message": "...",  "guidance": "..."}

`guidance` is the Phase 6 mechanism carried forward: the turn after a tool call
is generated from a context frame that never passes through the director, so the
tool result is the only place that can steer the sentence the model is about to
say. On a failure that sentence is the whole point — "the booking did not go
through, so tell them a colleague will confirm" is what keeps the agent honest
when the backend is not.

The error codes are a closed vocabulary rather than free text so that a test can
assert on them and a log can be grepped for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- Error codes -------------------------------------------------------------
#
# Grouped by who has to do something about them. The first group is the model's
# fault and the guidance tells it what to fix; the second is the configuration's;
# the third is the world's.

INVALID_ARGUMENTS = "invalid_arguments"
"""The arguments did not match the schema, or a value could not be parsed."""

INVALID_TIME = "invalid_time"
"""A date or time was not in the format the tool asked for."""

PAST_TIME = "past_time"
"""The requested time has already gone."""

TOO_FAR_AHEAD = "too_far_ahead"
"""The requested time is beyond the configured horizon."""

SLOT_NOT_OFFERED = "slot_not_offered"
"""`book_meeting` was asked for a time that `check_calendar_availability` never returned."""

EMAIL_REQUIRED = "email_required"
"""The calendar provider needs an attendee email address and none is known."""

NOT_AUTHORIZED = "not_authorized"
"""The action is not allowed in this call's current state."""

UNAVAILABLE = "unavailable"
"""The feature is not configured on this bot, or not applicable to this session."""

NO_PROSPECT = "no_prospect"
"""The action needs a prospect row and this call has none."""

NOT_FOUND = "not_found"
"""The referenced record does not exist."""

SLOT_TAKEN = "slot_taken"
"""The slot was free when offered and is not any more."""

EXTERNAL_ERROR = "external_error"
"""An external API — calendar, carrier, database — refused or was unreachable."""

TRANSFER_UNAVAILABLE = "transfer_unavailable"
"""No live transfer is possible on this session."""

TRANSFER_FAILED = "transfer_failed"
"""The carrier would not move the call."""

NOT_STORED = "not_stored"
"""The action was honoured on the call but nothing durable recorded it."""

INTERNAL_ERROR = "internal_error"
"""A tool raised. Reported rather than swallowed, so the model is told plainly."""

ERROR_CODES = frozenset(
    {
        INVALID_ARGUMENTS,
        INVALID_TIME,
        PAST_TIME,
        TOO_FAR_AHEAD,
        SLOT_NOT_OFFERED,
        EMAIL_REQUIRED,
        NOT_AUTHORIZED,
        UNAVAILABLE,
        NO_PROSPECT,
        NOT_FOUND,
        SLOT_TAKEN,
        EXTERNAL_ERROR,
        TRANSFER_UNAVAILABLE,
        TRANSFER_FAILED,
        NOT_STORED,
        INTERNAL_ERROR,
    }
)


@dataclass(frozen=True)
class ToolResult:
    """What a tool hands back to the model.

    Build one with `ToolResult.ok` or `ToolResult.fail`; the constructor is not
    the intended entry point, because the two factories are what guarantee that a
    failure always has a code and a success never does.

    Attributes:
        success: The only thing the model is allowed to read as "it happened".
        data: What happened, as plain JSON-able data. None on failure.
        error_code: One of the constants above. None on success.
        message: A sentence about the failure, for the model and the log.
        guidance: What to say next. Always present, because the turn after a
            tool call is steered by nothing else — see the module docstring.
    """

    success: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    message: str | None = None
    guidance: str = ""

    @classmethod
    def ok(cls, data: dict[str, Any] | None = None, *, guidance: str = "") -> ToolResult:
        """A success, optionally carrying data."""
        return cls(success=True, data=dict(data or {}), guidance=guidance)

    @classmethod
    def fail(
        cls,
        error_code: str,
        message: str,
        *,
        guidance: str = "",
        data: dict[str, Any] | None = None,
    ) -> ToolResult:
        """A failure, always with a code and a message.

        `data` is allowed on a failure for the one case where the model needs
        something to work with — the list of valid values, say — but it is never
        the record of something that happened.
        """
        if error_code not in ERROR_CODES:
            raise ValueError(f"unknown error code {error_code!r}")
        return cls(
            success=False,
            data=dict(data) if data else None,
            error_code=error_code,
            message=message,
            guidance=guidance,
        )

    def with_guidance(self, guidance: str) -> ToolResult:
        """The same result with different guidance attached."""
        return ToolResult(
            success=self.success,
            data=self.data,
            error_code=self.error_code,
            message=self.message,
            guidance=guidance,
        )

    def to_dict(self) -> dict[str, Any]:
        """The result as the model receives it. Every key is always present.

        `guidance` comes first on purpose. Measured on 2026-09-04: with it last,
        after a `data` payload of six calendar slots, Groq/Qwen read the slots
        and went back to asking discovery questions; the instruction to offer
        two of the times had scrolled out of its attention. A small model reads
        the start of a tool result the way it reads the end of a prompt.
        """
        return {
            "guidance": self.guidance,
            "success": self.success,
            "data": self.data,
            "error_code": self.error_code,
            "message": self.message,
        }


@dataclass
class ActionRecord:
    """One tool invocation, kept for the call's outcome record.

    Not the result itself — that went to the model — but what a reviewer needs:
    which tool, whether it worked, and why not.
    """

    tool: str
    success: bool
    error_code: str | None = None
    summary: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Plain data for the JSON record."""
        return {
            "tool": self.tool,
            "success": self.success,
            "error_code": self.error_code,
            "summary": self.summary,
            **self.extra,
        }
