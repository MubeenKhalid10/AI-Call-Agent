"""What the bot knows about the phone call it is currently on.

`bot.py` runs the same pipeline whether the audio arrives from a browser or from
a carrier, which is the point of the transport abstraction. But a phone call is
not a browser tab in three ways that matter, and this module is where the
difference is written down:

1. **It has an identity.** A carrier's call has an id, a caller and a callee,
   and it is either one we placed or one that came to us. Everything the bot
   logs during the call should carry that id, or a log from the bot and a log
   from `call.py` cannot be matched up afterwards.
2. **A drop is final.** WebRTC recovers from a blip and the caller comes back to
   the same session, which is why `ConnectionGuard` holds the pipeline open for
   a few seconds. A dropped phone call never reconnects to the same websocket:
   the person redials and gets a new call. Holding the session open buys
   nothing and keeps the STT websocket and the pipeline alive after the line is
   dead, so telephony passes a zero grace window.
3. **It costs money per minute.** Which makes the call's own numbers — how long
   it lasted, how many turns each side took — worth reporting next to the
   per-response latency `metrics.py` already produces.

The per-response latency measurement is *not* duplicated here. It is transport
independent and `metrics.py` owns it; this adds the call-shaped numbers around
it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from .base import TELEPHONY_TRANSPORTS

# Custom parameters `call.py` attaches to an outbound call's media stream. Two
# of them duplicate what the carrier reports about the call, because Twilio's
# media-stream handshake carries neither the caller nor the callee — only the
# ids. Without them the bot cannot log who it is talking to.
PARAM_DIRECTION = "direction"
PARAM_FROM = "from_number"
PARAM_TO = "to_number"

DIRECTION_OUTBOUND = "outbound"
DIRECTION_INBOUND = "inbound"


@dataclass
class CallSession:
    """One phone call, from the bot's side.

    Built by `from_runner_args`, which returns `None` for anything that is not a
    phone call — so `bot.py` can treat "am I on a call" as a single truthiness
    check rather than sprinkling transport-type comparisons through the wiring.
    """

    provider: str
    call_id: str | None = None
    stream_id: str | None = None
    from_number: str | None = None
    to_number: str | None = None
    direction: str = DIRECTION_INBOUND

    _connected_at: float | None = field(default=None, repr=False)
    _ended_at: float | None = field(default=None, repr=False)
    _caller_turns: int = field(default=0, repr=False)
    _agent_turns: int = field(default=0, repr=False)

    @classmethod
    def from_runner_args(cls, runner_args: Any) -> CallSession | None:
        """Build a session from the runner's arguments, or None if this is not a call.

        `create_transport` detects the carrier from the media stream's first
        message and writes both `transport_type` and the parsed `call_data` back
        onto `runner_args`, so by the time the bot has a transport this
        information is already there and no handshake is re-read. (It could not
        be re-read anyway: the websocket's message stream is single-use.)
        """
        transport_type = getattr(runner_args, "transport_type", None)
        if transport_type not in TELEPHONY_TRANSPORTS:
            return None

        call_data = getattr(runner_args, "call_data", None)
        body = _body_of(call_data)

        return cls(
            provider=transport_type,
            call_id=_attr(call_data, "call_id"),
            stream_id=_attr(call_data, "stream_id"),
            # The carrier's own from/to when it sends them (Telnyx, Exotel do);
            # otherwise the parameters an outbound call carried in with it.
            from_number=_attr(call_data, "from_number") or body.get(PARAM_FROM),
            to_number=_attr(call_data, "to_number") or body.get(PARAM_TO),
            direction=str(body.get(PARAM_DIRECTION) or DIRECTION_INBOUND),
        )

    @property
    def is_outbound(self) -> bool:
        """Whether we placed this call."""
        return self.direction == DIRECTION_OUTBOUND

    def describe(self) -> str:
        """One line identifying the call, for the log at the start of the session."""
        parts = [f"{self.direction} {self.provider}"]
        if self.call_id:
            parts.append(f"call={self.call_id}")
        if self.from_number:
            parts.append(f"from={self.from_number}")
        if self.to_number:
            parts.append(f"to={self.to_number}")
        if self.stream_id:
            parts.append(f"stream={self.stream_id}")
        return " ".join(parts)

    def on_connected(self) -> None:
        """Record that the carrier's media stream is up and audio is flowing."""
        if self._connected_at is None:
            self._connected_at = time.monotonic()
        logger.info(f"CALL | audio connected | {self.describe()}")

    def on_disconnected(self) -> None:
        """Record that the line dropped, which on a phone call means it is over."""
        if self._ended_at is None:
            self._ended_at = time.monotonic()
        logger.info(f"CALL | line closed after {self.duration_secs:.1f}s | {self.describe()}")

    def note_caller_turn(self) -> None:
        """Count one finished turn from the person on the phone."""
        self._caller_turns += 1

    def note_agent_turn(self) -> None:
        """Count one finished turn from the agent."""
        self._agent_turns += 1

    @property
    def duration_secs(self) -> float:
        """How long audio has been flowing, in seconds.

        This is the bot's view — from the media stream opening to it closing —
        and it is a second or two shorter than the duration the carrier bills,
        which starts when the call is answered.
        """
        if self._connected_at is None:
            return 0.0
        end = self._ended_at if self._ended_at is not None else time.monotonic()
        return end - self._connected_at

    def log_summary(self) -> None:
        """Log the call's own numbers at the end of the session.

        Deliberately next to, not inside, `LatencyReporter.log_summary`: that one
        reports how fast each response was, and this reports what the call was.
        Both are printed at the end of every phone session.
        """
        logger.info(
            f"CALL SUMMARY | {self.describe()} | "
            f"duration {self.duration_secs:.1f}s | "
            f"{self._caller_turns} caller turn(s), {self._agent_turns} agent turn(s)"
        )
        if self._caller_turns == 0:
            # Worth calling out: the call connected and the person never spoke.
            # Voicemail and a wrong number both look like this, and neither is
            # visible in the latency numbers.
            logger.warning(
                "CALL SUMMARY | the other end never spoke — voicemail, a wrong number, "
                "or one-way audio"
            )


def _attr(call_data: Any, name: str) -> str | None:
    """Read one field off Pipecat's `CallData`, tolerating None and absent fields."""
    if call_data is None:
        return None
    value = getattr(call_data, name, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _body_of(call_data: Any) -> dict[str, Any]:
    """The custom parameters the carrier passed through, as a plain dict."""
    body = getattr(call_data, "body", None) if call_data is not None else None
    return body if isinstance(body, dict) else {}
