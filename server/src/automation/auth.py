"""Keys in, signatures out: the two credentials of the automation boundary. Phase 17.

**Inbound**, an automation platform calls the API with a bearer key
(`Authorization: Bearer …`, or `X-API-Key: …` for a client that cannot set
the former). The presented key is compared in constant time against *every*
configured key, so a key is rotated by adding the new one, moving the
clients, and removing the old — never with a gap.

**Outbound**, every delivery to a webhook URL carries

    X-Aiva-Signature: t=<unix seconds>,v1=<hex>

an HMAC-SHA256 keyed by `AUTOMATION_WEBHOOK_SECRET` over `"<t>." + <the raw
body>`. The timestamp is inside the signed string, so a captured delivery
cannot be replayed later than the receiver's tolerance; the body is the exact
bytes sent, so a receiver verifies what it received rather than what it
re-serialised. `verify_signature` is the reference check for a receiver
written in Python; `n8n/README.md` shows the same check in JavaScript.

A static header (`AUTOMATION_WEBHOOK_AUTH_HEADER` / `_TOKEN`) is sent as
well, because n8n's Webhook node checks one natively and a signature check
needs a Code node. The two are independent: either proves the sender, both
together prove the sender and the body.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterable, Mapping

#: The signature header on every outbound delivery.
SIGNATURE_HEADER = "X-Aiva-Signature"

#: How far a delivery's timestamp may be from the receiver's clock before the
#: signature is refused as a replay. Five minutes, the usual figure.
DEFAULT_TOLERANCE_SECS = 300


def extract_key(headers: Mapping[str, str]) -> str | None:
    """The API key a request presented, or None.

    `Authorization: Bearer <key>` first; `X-API-Key: <key>` for a client that
    cannot set an Authorization header. Header names are matched
    case-insensitively, as HTTP requires.
    """
    lowered = {str(name).lower(): str(value) for name, value in headers.items()}
    auth = lowered.get("authorization", "").strip()
    if auth:
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    api_key = lowered.get("x-api-key", "").strip()
    return api_key or None


def key_matches(presented: str | None, accepted: Iterable[str]) -> bool:
    """Whether a presented key is one of the accepted ones, in constant time per key.

    Every accepted key is compared, whether or not an earlier one matched, so
    the time taken says nothing about which key (if any) was right.
    """
    if not presented:
        return False
    matched = False
    for key in accepted:
        if hmac.compare_digest(presented.encode("utf-8"), key.encode("utf-8")):
            matched = True
    return matched


def sign(secret: str, body: bytes, *, timestamp: int | None = None) -> str:
    """The `X-Aiva-Signature` value for a body: `t=<ts>,v1=<hex>`."""
    ts = int(timestamp if timestamp is not None else time.time())
    digest = _digest(secret, ts, body)
    return f"t={ts},v1={digest}"


def verify_signature(
    secret: str,
    header: str | None,
    body: bytes,
    *,
    tolerance_secs: float = DEFAULT_TOLERANCE_SECS,
    now: float | None = None,
) -> tuple[bool, str]:
    """Check a delivery's signature. Returns `(ok, reason)`; the reason is for a log line.

    Refuses a missing or malformed header, a timestamp outside the tolerance
    (a replay, or a clock that is wrong), and a digest that does not match
    the body. The digest comparison is constant-time.
    """
    if not header:
        return False, "no signature header"
    parts = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    raw_ts = parts.get("t")
    provided = parts.get("v1")
    if not raw_ts or not provided:
        return False, "malformed signature header"
    try:
        ts = int(raw_ts)
    except ValueError:
        return False, "malformed timestamp"
    moment = now if now is not None else time.time()
    if tolerance_secs >= 0 and abs(moment - ts) > tolerance_secs:
        return False, f"timestamp {int(moment - ts)}s from now exceeds the {tolerance_secs:g}s tolerance"
    expected = _digest(secret, ts, body)
    if not hmac.compare_digest(expected.encode("ascii"), provided.strip().lower().encode("ascii")):
        return False, "digest mismatch"
    return True, "verified"


def fingerprint(*parts: str | bytes) -> str:
    """A stable hash of a request, for the idempotency replay cache."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part if isinstance(part, bytes) else part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _digest(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


__all__ = [
    "DEFAULT_TOLERANCE_SECS",
    "SIGNATURE_HEADER",
    "extract_key",
    "fingerprint",
    "key_matches",
    "sign",
    "verify_signature",
]
