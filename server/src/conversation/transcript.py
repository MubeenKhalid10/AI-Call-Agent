"""What was actually said on the call, in order, kept verbatim. Phase 8.

The transcript is the one part of the call record that is *evidence* rather
than interpretation. Everything else the conversation layer produces — the
qualification record, the state path, the summary Phase 8 builds from them — is
somebody's reading of the call. This is the call. So it is stored exactly as the
pipeline reported it, it is never edited, and nothing here summarises it.

**Where the words come from, and why that matters.** The prospect's turns are
what the user aggregator wrote into the model's context, reported through
`SalesConversation.note_user_turn` by the director — the same text the detectors
ran over and the model replied to, so the transcript and the record cannot
disagree about what was said. The agent's turns are what the assistant
aggregator recorded after `transport.output()`, which is what the caller
actually *heard*: a reply the caller interrupted is stored cut off at the point
it was cut off, and marked as such, rather than as the sentence the model meant
to finish.

Timestamps are seconds since the conversation started, from a monotonic clock.
That is honest about what is known — the bot's own clock — and it is what a
reader needs to see that the prospect went quiet for forty seconds before
saying no.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

ROLE_USER = "user"
"""The person on the phone."""

ROLE_ASSISTANT = "assistant"
"""The agent."""


@dataclass(frozen=True)
class TranscriptEntry:
    """One turn, as spoken.

    Attributes:
        role: `user` or `assistant`.
        text: The words, untouched.
        at: Seconds after the conversation started that the turn was recorded.
        interrupted: For an agent turn, whether the caller cut it off — so the
            text is what was heard, not what was intended.
    """

    role: str
    text: str
    at: float
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Plain data, for the call record."""
        return {
            "role": self.role,
            "text": self.text,
            "at": self.at,
            "interrupted": self.interrupted,
        }

    @property
    def speaker(self) -> str:
        """A label for a rendered transcript."""
        return "PROSPECT" if self.role == ROLE_USER else "AGENT"


class Transcript:
    """The turns of one call, in the order they were reported."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        """Create an empty transcript.

        Args:
            clock: A monotonic clock, for tests. Defaults to `time.monotonic`.
        """
        self._clock = clock or time.monotonic
        self._started = self._clock()
        self._entries: list[TranscriptEntry] = []

    def add_user(self, text: str | None) -> TranscriptEntry | None:
        """Record a turn from the prospect. Returns the entry, or None for an empty one."""
        return self._add(ROLE_USER, text, interrupted=False)

    def add_assistant(self, text: str | None, *, interrupted: bool = False) -> TranscriptEntry | None:
        """Record a turn from the agent, as heard. Returns the entry, or None for an empty one.

        An interrupted turn that produced no words at all — cut off before the
        first token — is not recorded: there is nothing the caller heard.
        """
        return self._add(ROLE_ASSISTANT, text, interrupted=interrupted)

    def _add(self, role: str, text: str | None, *, interrupted: bool) -> TranscriptEntry | None:
        cleaned = (text or "").strip()
        if not cleaned:
            return None
        entry = TranscriptEntry(
            role=role,
            text=cleaned,
            at=round(self._clock() - self._started, 1),
            interrupted=interrupted,
        )
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        """Every turn, oldest first."""
        return tuple(self._entries)

    def __len__(self) -> int:
        """How many turns were recorded."""
        return len(self._entries)

    @property
    def user_turns(self) -> int:
        """How many turns the prospect took."""
        return sum(1 for entry in self._entries if entry.role == ROLE_USER)

    @property
    def agent_turns(self) -> int:
        """How many turns the agent took, counting an interrupted one as a turn."""
        return sum(1 for entry in self._entries if entry.role == ROLE_ASSISTANT)

    def to_list(self) -> list[dict[str, Any]]:
        """The transcript as plain data, for the call record."""
        return [entry.to_dict() for entry in self._entries]

    def render(self) -> str:
        """The transcript as readable text, one turn per line."""
        return render_transcript(self.to_list())


def render_transcript(entries: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
    """Render plain-data transcript entries as text, one turn per line.

    Tolerant of entries in any shape, because it is also used on records read
    back from the database, which this code did not necessarily write.
    """
    lines: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        speaker = "PROSPECT" if entry.get("role") == ROLE_USER else "AGENT"
        at = entry.get("at")
        stamp = f"[{float(at):6.1f}s] " if isinstance(at, (int, float)) else ""
        cut = " (interrupted)" if entry.get("interrupted") else ""
        lines.append(f"{stamp}{speaker}: {text}{cut}")
    return "\n".join(lines)
