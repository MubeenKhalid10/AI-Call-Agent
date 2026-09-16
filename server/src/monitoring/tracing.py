"""One id that follows a call through every process. Phase 22.

Phase 9 bound the *row* ids — campaign, prospect, attempt, the carrier's call
id — onto every log line, and they are still the join a person reaches for.
But they arrive one at a time: the scheduler knows the attempt before the
carrier has given it a call id, the bot learns the attempt only once the
brief resolves, a webhook carries only the call id, and the CRM syncer sees
a result id first. A `trace` is one value that is the same on all of them
from the moment the dialer takes the reservation:

    scheduler  →  telephony  →  agent  →  tools  →  database  →  webhook  →  CRM
    (dialer)      (carrier)     (bot)     (actions)  (store)      (receiver)   (syncer)

**Born once, carried three ways.** The dialer makes it (`new_trace_id`)
before it asks the carrier to dial, and then:

1. writes it on the attempt row (`call_attempts.trace_id`), which is how the
   webhook receiver, the recovery pass, the CRM syncer and the event
   deliverer find it — they all start from the row;
2. sends it to the bot as a media-stream custom parameter (`PARAM_TRACE_ID`),
   beside the prospect and attempt ids Phase 5 put there, so the bot's
   first log line already carries it, before any database read;
3. binds it on its own log context, so the placement, the carrier's answer
   and the row write share it.

A session with no dialer behind it — a browser tab, an eval run, an inbound
call — makes its own, so its lines still share one id.

**Shape.** Sixteen hex characters from the system's entropy source: short
enough to read in a terminal, wide enough never to collide, and carrying
nothing about the person. It is not a secret and must not be treated as
one; it is also not a row id and must never be parsed.

HTTP requests to the servers get a *request* id the same way
(`REQUEST_HEADER`, echoed back), kept separate from the call's trace on
purpose: one names a request, the other a call, and a webhook delivery has
both.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

#: The media-stream custom parameter the dialer sets and the bot reads. Named
#: here — the reader's side — for the reason `conversation/sources.py` gives.
PARAM_TRACE_ID = "trace_id"

#: The header a client may send to name its request, and that every server
#: echoes back so a caller can quote it. Made up when absent.
REQUEST_HEADER = "X-Aiva-Request-Id"

TRACE_LENGTH = 16

#: What an id must look like to be accepted from outside (a handshake, a
#: header): letters, digits and three punctuation marks, bounded. Anything
#: else is replaced rather than logged.
_ID_SHAPE = re.compile(r"^[A-Za-z0-9._\-]{4,64}$")


def new_trace_id() -> str:
    """A fresh correlation id."""
    return secrets.token_hex(TRACE_LENGTH // 2)


def new_request_id() -> str:
    """A fresh request id, the same shape."""
    return secrets.token_hex(TRACE_LENGTH // 2)


def clean_id(value: Any) -> str | None:
    """`value` as an id if it has the accepted shape, else None.

    Applied to every id that arrives from outside the process, so a header
    that carries a paragraph, a control character or a credential is not
    bound onto a thousand log lines.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text if _ID_SHAPE.match(text) else None


def trace_from_runner_args(runner_args: Any) -> str | None:
    """The trace id an outbound call carried in on its handshake, or None.

    The same two places `conversation/sources.py` reads the row ids from:
    the carrier's parsed `call_data.body`, or a websocket runner's plain
    `body` for a test harness.
    """
    call_data = getattr(runner_args, "call_data", None)
    body = getattr(call_data, "body", None) if call_data is not None else None
    if not isinstance(body, dict):
        body = getattr(runner_args, "body", None)
    if not isinstance(body, dict):
        return None
    return clean_id(body.get(PARAM_TRACE_ID))


__all__ = [
    "PARAM_TRACE_ID",
    "REQUEST_HEADER",
    "TRACE_LENGTH",
    "clean_id",
    "new_request_id",
    "new_trace_id",
    "trace_from_runner_args",
]
