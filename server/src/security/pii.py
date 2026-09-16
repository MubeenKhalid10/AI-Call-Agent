"""Masking the people out of a payload for a principal who may not see them. Phase 18.

A viewer may read that a call happened, how long it lasted and how it
ended; they may not read the number that was dialled, the email that was
collected, what the person said, or the custom columns an import carried
(which are whatever the CSV had, and cannot be assumed harmless).

`redact_pii` walks any JSON-shaped value and, wherever it finds a key from
`PHONE_KEYS`, `EMAIL_KEYS`, `TRANSCRIPT_KEYS` or `CUSTOM_KEYS`, replaces the
value: a phone keeps its country code and last two digits, an email its
first letter and domain, a transcript becomes `None` with
`transcript_included: false`, custom data becomes an empty object. Every
route in the API and the dashboard is covered by one walk over its answer,
so a new field in a serializer is masked by its *name*, without anyone
having to remember to add a call — which is the failure mode a per-route
approach invites.

Names are not masked. A viewer sees the dashboard's "recent calls" with
the prospect's name, as the page always showed it; the number under it is
what identifies and reaches a person, and that is what goes.
"""

from __future__ import annotations

import re
from typing import Any

PHONE_KEYS = frozenset({"phone", "phone_normalized", "to_number", "from_number", "transfer_number", "dial_number"})
EMAIL_KEYS = frozenset({"email", "attendee_email"})
TRANSCRIPT_KEYS = frozenset({"transcript"})
CUSTOM_KEYS = frozenset({"custom_data"})

MASK = "•"
_DIGITS = re.compile(r"\d")


def mask_phone(value: Any) -> Any:
    """`+923001234567` → `+92••••••••67`. Non-strings pass through as None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    digits = _DIGITS.findall(text)
    if len(digits) < 4:
        return MASK * max(len(text), 3)
    keep_tail = 2
    # Keep an explicit country code (a leading + and up to three digits) so
    # a viewer can still tell a Karachi number from a London one.
    head = ""
    if text.startswith("+"):
        code = "".join(digits[:2]) if len(digits) >= 10 else "".join(digits[:1])
        head = "+" + code
        hidden = len(digits) - len(code) - keep_tail
    else:
        hidden = len(digits) - keep_tail
    return head + MASK * max(hidden, 1) + "".join(digits[-keep_tail:])


def mask_email(value: Any) -> Any:
    """`hina@example.com` → `h•••@example.com`."""
    if value is None:
        return None
    text = str(value).strip()
    if "@" not in text:
        return MASK * 3 if text else text
    local, _, domain = text.partition("@")
    return f"{local[:1]}{MASK * 3}@{domain}"


def redact_pii(value: Any) -> Any:
    """A copy of a JSON-shaped value with the personal fields masked, recursively."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name in PHONE_KEYS:
                out[key] = mask_phone(item)
            elif name in EMAIL_KEYS:
                out[key] = mask_email(item)
            elif name in TRANSCRIPT_KEYS:
                out[key] = None
            elif name in CUSTOM_KEYS:
                out[key] = {} if isinstance(item, dict) else None
            else:
                out[key] = redact_pii(item)
        if "transcript" in out and "transcript_included" in out:
            out["transcript_included"] = False
        return out
    if isinstance(value, list):
        return [redact_pii(item) for item in value]
    if isinstance(value, tuple):
        return [redact_pii(item) for item in value]
    return value


__all__ = [
    "CUSTOM_KEYS",
    "EMAIL_KEYS",
    "PHONE_KEYS",
    "TRANSCRIPT_KEYS",
    "mask_email",
    "mask_phone",
    "redact_pii",
]
