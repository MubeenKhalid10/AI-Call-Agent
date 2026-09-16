"""Signed, stateless dashboard sessions. Phase 18.

A session is a cookie whose value is

    <base64url(JSON claims)>.<base64url(HMAC-SHA256 over the claims)>

keyed by `DASHBOARD_SESSION_SECRET`. The claims are the user's name, their
role, when it was issued, when it expires, and a random nonce. Nothing is
stored on the server: two dashboard processes behind one address share the
secret and therefore the sessions, and a restart keeps everyone signed in
— unless the secret was generated at boot, in which case the startup log
says so and everyone signs in again.

**Why not itsdangerous.** It is installed, transitively; the format here
is forty lines and the verification is one `hmac.compare_digest`, which
is easier to audit than a dependency this project does not otherwise
declare. **Why the role is inside the token.** So a request is authorised
from the cookie alone, with no directory lookup per page refresh; the
trade is that a role change takes effect when the session is next issued
(at most `DASHBOARD_SESSION_TTL_SECS` later), which the docs say.

A token that fails for any reason yields `None` with a one-word reason for
the log; nothing here raises on hostile input.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from .roles import Principal, Role, parse_role

#: The cookie the dashboard sets.
COOKIE_NAME = "aiva_session"

#: A day. Long enough that an operator is not signed out mid-shift, short
#: enough that a laptop left open at the weekend is signed out by Monday.
DEFAULT_TTL_SECS = 12 * 3600
MAX_TTL_SECS = 30 * 86400

#: A secret shorter than this is refused: the HMAC is only as strong as it.
MIN_SECRET_LENGTH = 32


@dataclass(frozen=True)
class Session:
    """The claims a valid token carried."""

    user: str
    role: Role
    issued_at: int
    expires_at: int
    nonce: str

    def principal(self) -> Principal:
        """The principal this session authenticates."""
        return Principal(name=self.user, role=self.role, via="session")

    @property
    def ttl_secs(self) -> int:
        return self.expires_at - self.issued_at


def generate_secret() -> str:
    """A secret fit for `DASHBOARD_SESSION_SECRET`."""
    return secrets.token_urlsafe(48)


def issue_session(
    secret: str, user: str, role: Role, *, ttl_secs: float = DEFAULT_TTL_SECS, now: float | None = None
) -> str:
    """A signed token for a user.

    Raises:
        ValueError: The secret is too short to sign with.
    """
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(f"the session secret must be at least {MIN_SECRET_LENGTH} characters")
    moment = int(now if now is not None else time.time())
    ttl = int(max(60, min(ttl_secs, MAX_TTL_SECS)))
    claims = {
        "u": user,
        "r": role.value,
        "iat": moment,
        "exp": moment + ttl,
        "n": secrets.token_urlsafe(12),
    }
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_sign(secret, body)}"


def read_session(secret: str, token: str | None, *, now: float | None = None) -> tuple[Session | None, str]:
    """The session a token carries, or `(None, reason)`.

    The signature is checked before the claims are parsed, so a hostile
    body is never decoded; the comparison is constant-time.
    """
    if not token or not isinstance(token, str):
        return None, "no token"
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        return None, "no secret"
    if len(token) > 4096:
        return None, "token too long"
    body, _, signature = token.partition(".")
    if not body or not signature:
        return None, "malformed"
    if not hmac.compare_digest(_sign(secret, body).encode("ascii"), signature.encode("ascii", "replace")):
        return None, "bad signature"
    try:
        claims = json.loads(_unb64(body))
        user = str(claims["u"])
        role = parse_role(str(claims["r"]))
        issued_at = int(claims["iat"])
        expires_at = int(claims["exp"])
        nonce = str(claims.get("n", ""))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None, "malformed claims"
    moment = now if now is not None else time.time()
    if moment >= expires_at:
        return None, "expired"
    if issued_at > moment + 300:
        return None, "issued in the future"
    if not user:
        return None, "no user"
    return Session(user=user, role=role, issued_at=issued_at, expires_at=expires_at, nonce=nonce), "ok"


def _sign(secret: str, body: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


__all__ = [
    "COOKIE_NAME",
    "DEFAULT_TTL_SECS",
    "MAX_TTL_SECS",
    "MIN_SECRET_LENGTH",
    "Session",
    "generate_secret",
    "issue_session",
    "read_session",
]
