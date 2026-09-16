"""What makes two requests to place a call the same call. Phase 9.

The failure this prevents: a worker reserves a call, the process dies before the
carrier is asked, and the next run reserves *the same work again* — or two
workers race, or a caller retries an API request whose answer was lost. Each of
those wants to be "the third attempt at membership 12", and each would otherwise
create a second attempt row and dial a second time.

**The key is derived, not generated.** A random key handed out by the caller
protects an API against a client retrying the same request; it does nothing
about two different callers deciding to do the same thing. So the key is
computed from what the call *is* — which membership, which attempt number —
which means two independent callers who mean the same call compute the same
string without having communicated. `call_attempts.idempotency_key` is unique,
so the second insert loses to the first and the loser is handed the winner's
row.

**Why the attempt row is the idempotency record.** It already exists, it
already has the unique carrier call id on it, and it is what everything else
keys off. A separate idempotency table would be a second thing to keep in step
with it, and a lease with an expiry — the usual shape — would need a clock the
database and the workers agree on. The row's own status is the lease: while it
is live the prospect cannot be dialled, and `recovery.py` is what breaks a lease
whose holder is gone.

**Carrier-side idempotency is not assumed.** Neither Twilio's nor SignalWire's
call-creation API takes an idempotency key, so no amount of local bookkeeping
makes `place_call` safe to repeat. That is why `NEVER_RETRY` is its policy and
why an ambiguous placement becomes `UNRESOLVED` rather than another attempt.
"""

from __future__ import annotations

import hashlib
import re

#: Version prefix. A key's meaning is a contract between whoever wrote a row and
#: whoever reads it later; bumping this deliberately makes old keys not match
#: new ones, which is what you want if the *derivation* ever changes.
VERSION = "v1"

_SAFE = re.compile(r"[^A-Za-z0-9_.:@+-]")

#: Postgres column width. Keys are far shorter than this; the cap exists so a
#: caller-supplied key cannot be long enough to matter.
MAX_LENGTH = 200


def campaign_call_key(
    *, campaign_id: int, membership_id: int, attempt_number: int
) -> str:
    """The key for one attempt at one membership of one campaign.

    `v1:campaign:3:membership:12:attempt:2`. Readable on purpose: this value
    ends up in a database column and in log lines, and a key you can read tells
    you which call a duplicate was a duplicate *of* without a join.
    """
    return f"{VERSION}:campaign:{campaign_id}:membership:{membership_id}:attempt:{attempt_number}"


def manual_call_key(*, prospect_id: int, marker: str) -> str:
    """The key for a call placed outside a campaign.

    Args:
        prospect_id: Who is being called.
        marker: What makes this request distinct from the next one — a request
            id from whatever asked for the call, or a timestamp bucket. Two
            requests carrying the same marker are the same call; a caller with
            nothing meaningful to put here should pass something unique, and
            accept that it gets no protection.
    """
    return f"{VERSION}:prospect:{prospect_id}:{sanitize(marker)}"


def sanitize(value: str) -> str:
    """Make an arbitrary string safe and bounded to use inside a key.

    Anything outside a conservative alphabet is replaced, and anything too long
    is hashed rather than truncated — truncation would make two different long
    markers collide, which is exactly the failure a key exists to prevent.
    """
    cleaned = _SAFE.sub("_", (value or "").strip()) or "unspecified"
    if len(cleaned) <= 64:
        return cleaned
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:32]
    return f"{cleaned[:24]}~{digest}"


def is_valid(key: str | None) -> bool:
    """Whether a key is usable as a database value."""
    return bool(key) and len(key) <= MAX_LENGTH and _SAFE.search(key.replace(":", "")) is None


__all__ = [
    "MAX_LENGTH",
    "VERSION",
    "campaign_call_key",
    "is_valid",
    "manual_call_key",
    "sanitize",
]
