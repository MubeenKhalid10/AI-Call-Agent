"""Authentication, authorisation and hardening for the HTTP surfaces. Phase 18.

Nothing here touches a call. The bot, the pipeline, the dialer and the
scheduler do not import this package; it sits in front of the three
processes that *serve* — the dashboard, the automation API and the
standalone webhook receiver — and in `config.py`, which reads its settings.

* `roles` — `Role`, `Permission`, `Principal`, `User`, `UserDirectory`.
* `passwords` — scrypt hashing for `DASHBOARD_USERS`.
* `sessions` — signed, stateless dashboard sessions (`aiva_session`).
* `ratelimit` — a sliding-window limiter, per key, per address.
* `pii` — masking phone numbers, emails, transcripts and custom fields
  out of an answer for a principal who may not see them.
* `audit` — the audit log: an `audit_log` row and an `audit.<action>` line.
* `http` — security headers, HTTPS enforcement, the body cap, CORS, and
  the trusted-proxy rules behind `client_ip` and `effective_scheme`.

The pure modules (`roles`, `passwords`, `sessions`, `ratelimit`, `pii`) are
imported by `config.py`, so this file must stay importable with nothing
but the standard library and loguru: `audit` and `http` are imported here
too, but `http` pulls in Starlette only, which every server here already
has, and `audit` reaches the log scrubber lazily.
"""

from __future__ import annotations

from .audit import NOBODY, AuditEntry, AuditLog, AuditUnavailable
from .http import (
    DASHBOARD_CSP,
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_TRUSTED_PROXIES,
    HttpPolicy,
    SecurityMiddleware,
    client_ip,
    cors_problems,
    effective_scheme,
    install_security,
    is_loopback,
    is_trusted,
    origin_allowed,
    parse_networks,
)
from .passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    hash_password,
    is_password_hash,
    verify_password,
)
from .pii import mask_email, mask_phone, redact_pii
from .ratelimit import Decision, RateLimiter
from .roles import (
    MAX_EMAIL_LENGTH,
    NAME_RULE,
    PASSWORD_RULE,
    ROLE_PERMISSIONS,
    USER_ACTIVE,
    USER_PENDING,
    Permission,
    Principal,
    Role,
    User,
    UserDirectory,
    is_email,
    is_user_name,
    parse_role,
    validate_registration,
)
from .sessions import (
    COOKIE_NAME,
    DEFAULT_TTL_SECS,
    MIN_SECRET_LENGTH,
    Session,
    generate_secret,
    issue_session,
    read_session,
)

__all__ = [
    "COOKIE_NAME",
    "DASHBOARD_CSP",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_TRUSTED_PROXIES",
    "DEFAULT_TTL_SECS",
    "MAX_EMAIL_LENGTH",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "MIN_SECRET_LENGTH",
    "NAME_RULE",
    "PASSWORD_RULE",
    "NOBODY",
    "ROLE_PERMISSIONS",
    "USER_ACTIVE",
    "USER_PENDING",
    "AuditEntry",
    "AuditLog",
    "AuditUnavailable",
    "Decision",
    "HttpPolicy",
    "Permission",
    "Principal",
    "RateLimiter",
    "Role",
    "SecurityMiddleware",
    "Session",
    "User",
    "UserDirectory",
    "client_ip",
    "cors_problems",
    "effective_scheme",
    "generate_secret",
    "hash_password",
    "install_security",
    "is_email",
    "is_loopback",
    "is_password_hash",
    "is_trusted",
    "is_user_name",
    "issue_session",
    "mask_email",
    "mask_phone",
    "origin_allowed",
    "parse_networks",
    "parse_role",
    "read_session",
    "redact_pii",
    "validate_registration",
    "verify_password",
]
