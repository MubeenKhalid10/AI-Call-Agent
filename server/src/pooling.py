"""How a connection pool is opened, given where the DSN points.

Supabase's *transaction* pooler (port 6543, the mode meant for serverless
functions: many short-lived clients over a few real connections) hands each
transaction to whichever backend is free, so a prepared statement named on
one backend does not exist on the next. asyncpg names its prepared statements
by default; with ``statement_cache_size=0`` it uses anonymous ones, which
every backend understands. The *session* pooler (5432) and a plain PostgreSQL
keep the default cache, which is faster.

A DSN can also say so explicitly with ``?pgbouncer=true`` (Prisma's
convention); the parameter is removed before asyncpg sees it, since asyncpg
would forward an unknown parameter to the server as a setting.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRANSACTION_POOLER_PORT = 6543


def pool_options(dsn: str) -> tuple[str, dict[str, Any]]:
    """The DSN to connect with and the extra ``asyncpg.create_pool`` arguments for it."""
    parts = urlsplit(dsn)
    query = parse_qsl(parts.query, keep_blank_values=True)
    flagged = any(k.lower() == "pgbouncer" and v.lower() in ("1", "true", "yes", "on") for k, v in query)
    remaining = [(k, v) for k, v in query if k.lower() != "pgbouncer"]
    cleaned = urlunsplit(parts._replace(query=urlencode(remaining))) if len(remaining) != len(query) else dsn
    try:
        port = parts.port
    except ValueError:
        port = None
    if flagged or port == TRANSACTION_POOLER_PORT:
        return cleaned, {"statement_cache_size": 0}
    return cleaned, {}
