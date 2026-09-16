#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""The security tool: make credentials, and say how the deployment stands. Phase 18.

Run it from the `server/` directory::

    uv run security.py hash-password              # prompts; prints a hash for DASHBOARD_USERS
    uv run security.py make-key                   # an API key for AUTOMATION_*_API_KEYS
    uv run security.py make-secret                # a DASHBOARD_SESSION_SECRET
    uv run security.py check                      # the security posture of this .env

**Nothing here is stored.** `hash-password` prints a hash and forgets the
password; `make-key` prints a random key it has never written anywhere. What
you do with the output — paste it into `.env`, into n8n's credential, into a
secrets manager — is the deployment's business, and the tool says so.

**`check` is the pre-production list.** It reads `.env` through the same
`Config.from_env` every server uses and reports, line by line, whether the
dashboard has users, whether HTTPS is required, whether CORS is off,
whether the API keys are per-role, whether `.env` is tracked by git, and
what it would say at startup. It changes nothing.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.security import MIN_PASSWORD_LENGTH, generate_secret, hash_password

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ATTENTION = 2


def command_hash_password(args: argparse.Namespace) -> int:
    """Prompt for a password twice, print its hash."""
    if args.password is not None:
        print(
            "\n  WARNING: a password on the command line lands in the shell's history.\n"
            "  Prefer the prompt: run without --password.\n",
            file=sys.stderr,
        )
        password = args.password
    else:
        if not sys.stdin.isatty():
            password = sys.stdin.readline().rstrip("\r\n")
        else:
            password = getpass.getpass("Password: ")
            again = getpass.getpass("Again: ")
            if password != again:
                print("The two entries differ; nothing printed.", file=sys.stderr)
                return EXIT_FAILED
    try:
        digest = hash_password(password)
    except ValueError as exc:
        print(f"Cannot hash that: {exc}.", file=sys.stderr)
        return EXIT_FAILED
    name = args.user or "alice"
    role = args.role or "operator"
    print(digest)
    print(
        f"\n  Add to .env (one entry per user, comma-separated):\n"
        f"  DASHBOARD_USERS={name}:{role}:{digest}\n"
        f"\n  Roles: admin (everything), operator (run campaigns, see numbers and transcripts),\n"
        f"  viewer (totals and outcomes; numbers masked). Passwords need {MIN_PASSWORD_LENGTH}+ characters.\n",
        file=sys.stderr,
    )
    return EXIT_OK


def command_make_key(args: argparse.Namespace) -> int:
    """Print a fresh API key."""
    key = secrets.token_urlsafe(32)
    print(key)
    variable = {
        "admin": "AUTOMATION_API_KEYS",
        "operator": "AUTOMATION_OPERATOR_API_KEYS",
        "viewer": "AUTOMATION_VIEWER_API_KEYS",
    }[args.role]
    print(
        f"\n  Add to .env:  {variable}={key}\n"
        f"  Several keys are comma-separated; add the new one, move the clients, remove the old.\n"
        f"  Never commit it. It is scrubbed from every log line.\n",
        file=sys.stderr,
    )
    return EXIT_OK


def command_make_secret(args: argparse.Namespace) -> int:
    """Print a session secret."""
    print(generate_secret())
    print(
        "\n  Add to .env:  DASHBOARD_SESSION_SECRET=<that>\n"
        "  Changing it signs everybody out. Two dashboard processes behind one address need the same one.\n",
        file=sys.stderr,
    )
    return EXIT_OK


def _env_duplicates(path: Path | None = None) -> list[str]:
    """Variables `.env` defines more than once, in order of first appearance. Phase 23."""
    env_path = path or Path(__file__).resolve().parent / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    seen: dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        if name:
            seen[name] = seen.get(name, 0) + 1
    return [name for name, count in seen.items() if count > 1]


def _env_tracked() -> bool | None:
    """Whether `.env` is tracked by git here. None when git is not available."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def command_check(args: argparse.Namespace) -> int:
    """Report the security posture of the configuration, changing nothing."""
    from src.config import Config, ConfigError

    load_dotenv(override=True)
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_FAILED

    security = config.security
    automation = config.automation
    findings: list[tuple[str, str, str]] = []  # (level, headline, fix)

    def ok(headline: str) -> None:
        findings.append(("OK", headline, ""))

    def warn(headline: str, fix: str) -> None:
        findings.append(("WARN", headline, fix))

    def bad(headline: str, fix: str) -> None:
        findings.append(("FAIL", headline, fix))

    # The dashboard.
    if security.dashboard_auth_disabled:
        warn("the dashboard has no login (DASHBOARD_AUTH_DISABLED=true)", "unset it before serving anything but 127.0.0.1")
    elif not security.users:
        bad("the dashboard requires a login and DASHBOARD_USERS names nobody", "uv run security.py hash-password")
    else:
        ok(f"dashboard login on: {security.users.describe()}")
    if security.dashboard_auth_required and not security.session_secret:
        warn("no DASHBOARD_SESSION_SECRET: sessions end at every restart", "uv run security.py make-secret")
    elif security.session_secret:
        ok("sessions signed with DASHBOARD_SESSION_SECRET")

    # The API.
    if not automation.api_enabled:
        warn("the automation API is off (no keys)", "uv run security.py make-key  — only if you run automation.py")
    else:
        ok(
            f"automation API keys: {len(automation.api_keys)} admin, {len(automation.operator_api_keys)} operator, "
            f"{len(automation.viewer_api_keys)} viewer"
        )
        if automation.api_keys and not automation.operator_api_keys and not automation.viewer_api_keys:
            warn("every API key is an admin key", "give n8n an operator key (make-key --role operator) and keep admin keys for people")
        if automation.docs_enabled:
            warn("the OpenAPI docs are served at /api/v1/docs", "AUTOMATION_DOCS_ENABLED=false in production, if you prefer")
    if automation.delivery_enabled and not automation.webhook_secret:
        warn("outbound events are unsigned", "set AUTOMATION_WEBHOOK_SECRET and verify X-Aiva-Signature in n8n")
    elif automation.delivery_enabled:
        ok("outbound events signed (X-Aiva-Signature)")

    # Transport.
    if security.require_https:
        ok(f"HTTPS required; X-Forwarded-Proto believed from {', '.join(security.trusted_proxies)}")
    else:
        warn("HTTP is allowed (SECURITY_REQUIRE_HTTPS unset)", "set it to true behind your TLS proxy; see SECURITY.md")
    if security.cors_origins:
        warn(f"CORS allows {', '.join(security.cors_origins)}", "keep the list to the origins that need it")
    else:
        ok("CORS off (no cross-origin browser access)")
    ok(
        f"rate limits: {security.api_rate_limit}/min per key, {security.anon_rate_limit}/min anonymous, "
        f"{security.login_rate_limit}/min logins; bodies up to {security.max_body_bytes // 1024} KiB"
    )
    if security.audit_enabled:
        ok(f"audit log {'strict' if security.audit_strict else 'on'} (audit_log table; `uv run campaign.py audit`)")
    else:
        bad("the audit log is off (SECURITY_AUDIT_ENABLED=false)", "turn it back on before production")

    # Carriers.
    telephony = config.telephony
    if telephony.is_configured and not telephony.can_verify_webhooks:
        warn(f"carrier webhooks cannot be verified: {telephony.describe_webhooks()}", "set the carrier's signing key")
    elif telephony.is_configured:
        ok("carrier webhook signatures verifiable")

    # Monitoring (Phase 22): the numbers name campaign ids, error rates and
    # costs — not people — so an open /metrics on loopback is fine and one on
    # a network interface wants the bearer.
    monitoring = config.monitoring
    if not monitoring.enabled:
        warn("monitoring is off (MONITORING_ENABLED=false): no /healthz, /readyz or /metrics", "leave it on in production")
    elif monitoring.token:
        ok("/metrics behind MONITORING_TOKEN; /healthz and /readyz open for probes")
    else:
        warn("/metrics is open (no MONITORING_TOKEN)", "fine on 127.0.0.1; set a token once any server port is reachable from a network")

    # Secrets at rest.
    tracked = _env_tracked()
    if tracked:
        bad("server/.env is tracked by git", "git rm --cached .env  (then rotate every key that was ever committed)")
    elif tracked is False:
        ok("server/.env is not tracked by git")
    # Phase 23: a variable defined twice in .env is decided by its last line,
    # silently — found on this machine with TTS_PROVIDER set three times, so
    # the provider whose key was valid was not the one in use.
    duplicates = _env_duplicates()
    if duplicates:
        warn(
            f".env defines {', '.join(duplicates)} more than once; the last line wins",
            "keep one line per variable, so the value in use is the one you can see",
        )
    else:
        ok(".env defines every variable once")
    for name in ("DASHBOARD_USERS", "AUTOMATION_API_KEYS", "AUTOMATION_OPERATOR_API_KEYS", "AUTOMATION_VIEWER_API_KEYS"):
        value = os.getenv(name, "")
        if ":" in value and name == "DASHBOARD_USERS" and "scrypt$" not in value:
            bad(f"{name} carries something that is not a password hash", "never a plain password; uv run security.py hash-password")

    worst = EXIT_OK
    print("\nSecurity posture\n")
    for level, headline, fix in findings:
        print(f"  {level:<5} {headline}")
        if fix:
            print(f"        -> {fix}")
        if level == "FAIL":
            worst = EXIT_ATTENTION
        elif level == "WARN" and worst == EXIT_OK:
            worst = EXIT_ATTENTION if args.strict else EXIT_OK
    print(f"\n  Startup line: {security.describe()}\n")
    return worst


def main() -> int:
    """Parse arguments and run a command."""
    parser = argparse.ArgumentParser(prog="security.py", description="Make credentials; check the security posture.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("hash-password", help="hash a password for DASHBOARD_USERS")
    p.add_argument("--user", help="the user name, for the example line")
    p.add_argument("--role", choices=("admin", "operator", "viewer"), help="the role, for the example line")
    p.add_argument("--password", help="the password (discouraged: use the prompt)")
    p.set_defaults(run=command_hash_password)

    p = sub.add_parser("make-key", help="print a fresh API key")
    p.add_argument("--role", choices=("admin", "operator", "viewer"), default="admin")
    p.set_defaults(run=command_make_key)

    p = sub.add_parser("make-secret", help="print a fresh DASHBOARD_SESSION_SECRET")
    p.set_defaults(run=command_make_secret)

    p = sub.add_parser("check", help="report the security posture of .env; change nothing")
    p.add_argument("--strict", action="store_true", help="exit 2 on warnings as well as failures")
    p.set_defaults(run=command_check)

    args = parser.parse_args()
    return int(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
