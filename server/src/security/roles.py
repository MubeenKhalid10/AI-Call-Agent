"""Who may do what: roles, permissions, principals and the user directory. Phase 18.

Three roles, ordered, and four permissions. A role is a *set* of permissions
rather than a rank, so that a route asks for the permission it needs
(`Permission.WRITE`) and never for a role name — which is what keeps a later
role (a "supervisor" who may read transcripts but not dial) from touching
every route in the codebase.

    viewer     read   — totals, campaigns, call outcomes; phone numbers
                        masked, transcripts withheld
    operator   read, read_pii, write — everything a person running a campaign
                        does: prospects, campaigns, calls, callbacks, the
                        numbers and the transcripts
    admin      all of the above, plus manage — closing a campaign, retrying
                        the outbox, reading the audit log

A **principal** is whoever a request turned out to be: a dashboard user
behind a session cookie, or an API key behind a bearer header. The rest of
the code only ever asks `principal.can(permission)`.

The **user directory** is read from `DASHBOARD_USERS` — `name:role:hash`
entries, comma-separated, the hash from `uv run security.py hash-password`
— so a deployment's users live where its other secrets do, in the
environment, and never in a file this repository could commit.

Phase 27 adds a second source: people who sign up on the application's
Register page are rows in the `dashboard_users` table (name, email, role,
hash), added to the same directory with `add()` as they register or first
sign in, so the login route, the constant-time check and the session are
the ones Phase 18 wrote. `validate_registration` is the one place the
sign-up form's rules live; the page repeats them for immediacy only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .passwords import (
    DUMMY_HASH,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    is_password_hash,
    verify_password,
)


class Role(StrEnum):
    """The three roles. Compared by permission, never by order."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


class Permission(StrEnum):
    """What a route may ask a principal for."""

    #: Read the reporting surface: totals, campaigns, calls, results — with
    #: phone numbers masked and transcripts withheld.
    READ = "read"
    #: See the people: phone numbers, emails, transcripts, custom fields.
    READ_PII = "read_pii"
    #: Change things that make phones ring: prospects, campaigns, calls.
    WRITE = "write"
    #: Close a campaign for good, reopen outbox rows, read the audit log.
    MANAGE = "manage"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.READ}),
    Role.OPERATOR: frozenset({Permission.READ, Permission.READ_PII, Permission.WRITE}),
    Role.ADMIN: frozenset(Permission),
}


def parse_role(text: str | None) -> Role:
    """A role from its name, case-insensitively. Raises `ValueError` for anything else."""
    cleaned = (text or "").strip().lower()
    try:
        return Role(cleaned)
    except ValueError as exc:
        raise ValueError(
            f"{text!r} is not a role; use one of {', '.join(r.value for r in Role)}"
        ) from exc


@dataclass(frozen=True)
class Principal:
    """Whoever a request proved itself to be.

    Attributes:
        name: The user name, or the API key's label (`api-key#3`). Never the
            key itself, and never a password: this is what the audit log
            records.
        role: Their role.
        via: How they authenticated — `session`, `api_key` or `anonymous`
            (the dashboard with authentication deliberately switched off).
    """

    name: str
    role: Role
    via: str = "api_key"

    @property
    def permissions(self) -> frozenset[Permission]:
        """Everything this principal may do."""
        return ROLE_PERMISSIONS[self.role]

    def can(self, permission: Permission) -> bool:
        """Whether the principal holds a permission."""
        return permission in ROLE_PERMISSIONS[self.role]

    def describe(self) -> str:
        """`name (role, via)` for a log line."""
        return f"{self.name} ({self.role.value}, {self.via})"


#: What a user name may look like: short, printable, no separators the
#: directory format uses.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@\-]{0,63}$")
#: What an email address may look like for the Register page: one `@`, a
#: dot in the domain, nothing that is not printable. Deliverability is not
#: checked — nothing here sends mail; the address is a unique handle.
_EMAIL = re.compile(r"^[^\s@]{1,64}@[^\s@]+\.[^\s@]{2,}$")
MAX_EMAIL_LENGTH = 254

#: How the Register page describes the rules, for the form's hints and for
#: the API's error messages; one wording, so they cannot drift.
NAME_RULE = "letters, digits, `.`, `_`, `@`, `-`; 1 to 64 characters, starting with a letter or digit"
PASSWORD_RULE = f"at least {MIN_PASSWORD_LENGTH} characters"

#: A registered account's status. `DASHBOARD_USERS` entries are always
#: active; a sign-up that asked for operator or admin is `pending` until an
#: admin approves it, and cannot sign in before that.
USER_ACTIVE = "active"
USER_PENDING = "pending"


def is_user_name(text: str | None) -> bool:
    """Whether `text` is a usable user name (the `DASHBOARD_USERS` rule)."""
    return bool(text) and _NAME.match(text or "") is not None


def is_email(text: str | None) -> bool:
    """Whether `text` looks like an email address."""
    return bool(text) and len(text or "") <= MAX_EMAIL_LENGTH and _EMAIL.match(text or "") is not None


def validate_registration(
    name: str | None, email: str | None, password: str | None, confirm_password: str | None
) -> dict[str, str]:
    """The Register form's problems, keyed by field. Empty when the entry is usable.

    The name rule is `DASHBOARD_USERS`'s, so a registered user could equally
    have been configured by hand; the password rule is `hash_password`'s.
    """
    problems: dict[str, str] = {}
    cleaned = (name or "").strip()
    if not cleaned:
        problems["name"] = "Enter a name."
    elif not is_user_name(cleaned):
        problems["name"] = f"That name cannot be used: {NAME_RULE}."
    address = (email or "").strip()
    if not address:
        problems["email"] = "Enter an email address."
    elif not is_email(address):
        problems["email"] = "That does not look like an email address."
    if not password:
        problems["password"] = "Enter a password."
    elif len(password) < MIN_PASSWORD_LENGTH:
        problems["password"] = f"The password needs {PASSWORD_RULE}."
    elif len(password) > MAX_PASSWORD_LENGTH:
        problems["password"] = f"The password is too long (at most {MAX_PASSWORD_LENGTH} characters)."
    if not confirm_password:
        problems["confirm_password"] = "Enter the password again."
    elif password and confirm_password != password:
        problems["confirm_password"] = "The two passwords do not match."
    return problems


@dataclass(frozen=True)
class User:
    """One dashboard user: a name, a role and a password hash. Never a password.

    `email` is set for a user who registered on the application (Phase 27)
    and None for one configured in `DASHBOARD_USERS`.
    """

    name: str
    role: Role
    password_hash: str
    email: str | None = None
    status: str = USER_ACTIVE

    @property
    def active(self) -> bool:
        """Whether this user may sign in. A pending sign-up may not."""
        return self.status == USER_ACTIVE

    def principal(self) -> Principal:
        """The principal a successful login yields."""
        return Principal(name=self.name, role=self.role, via="session")


class UserDirectory:
    """The dashboard's users, from `DASHBOARD_USERS`.

    `authenticate` takes the same time whether or not the name exists: an
    unknown user is verified against a throw-away hash, so the response time
    does not say which names are real.
    """

    def __init__(self, users: Iterable[User] = ()) -> None:
        """Build the directory. Duplicate names keep the first entry."""
        self._users: dict[str, User] = {}
        self._by_email: dict[str, User] = {}
        for user in users:
            self.add(user)

    def add(self, user: User) -> bool:
        """Add a user (Phase 27: one registered on the application, or read back from its table).

        Returns False, changing nothing, when the name — or the email — is
        already taken: a `DASHBOARD_USERS` entry always wins over a row, so
        an environment-configured admin cannot be shadowed by a sign-up.
        """
        key = user.name.strip().lower()
        email = (user.email or "").strip().lower() or None
        if key in self._users or (email and email in self._by_email):
            return False
        self._users[key] = user
        if email:
            self._by_email[email] = user
        return True

    def update(self, user: User) -> bool:
        """Replace a registered (table-backed) user's entry — after an approval, or a re-read at login.

        Returns False, changing nothing, for a name that is not in the
        directory or that belongs to a `DASHBOARD_USERS` entry (those never
        change at runtime).
        """
        key = user.name.strip().lower()
        current = self._users.get(key)
        if current is None or current.email is None:
            return False
        self._by_email.pop(current.email.strip().lower(), None)
        self._users[key] = user
        if user.email:
            self._by_email[user.email.strip().lower()] = user
        return True

    def remove(self, name: str | None) -> bool:
        """Forget a registered user (a rejected sign-up). A `DASHBOARD_USERS` entry is never removed."""
        key = (name or "").strip().lower()
        current = self._users.get(key)
        if current is None or current.email is None:
            return False
        del self._users[key]
        self._by_email.pop(current.email.strip().lower(), None)
        return True

    def has_email(self, email: str | None) -> bool:
        """Whether a registered user carries this email (case-insensitively)."""
        return bool(email) and (email or "").strip().lower() in self._by_email

    @classmethod
    def parse(cls, spec: str | None, problems: list[str] | None = None) -> UserDirectory:
        """Read `name:role:hash,name:role:hash,…`.

        Every problem is appended to `problems` (or raised as `ValueError`
        when none is given) rather than silently dropping the entry: a user
        who cannot log in because of a typo should learn why at startup.
        """
        collected: list[str] = [] if problems is None else problems
        users: list[User] = []
        seen: set[str] = set()
        for index, raw in enumerate((spec or "").split(","), start=1):
            entry = raw.strip()
            if not entry:
                continue
            parts = entry.split(":", 2)
            if len(parts) != 3:
                collected.append(
                    f"DASHBOARD_USERS entry {index} is not `name:role:hash` "
                    f"(make the hash with `uv run security.py hash-password`)."
                )
                continue
            name, role_text, password_hash = (part.strip() for part in parts)
            if not _NAME.match(name):
                collected.append(
                    f"DASHBOARD_USERS entry {index}: {name!r} is not a usable user name "
                    f"(letters, digits, `.`, `_`, `@`, `-`; up to 64 characters)."
                )
                continue
            try:
                role = parse_role(role_text)
            except ValueError as exc:
                collected.append(f"DASHBOARD_USERS entry {index} ({name}): {exc}.")
                continue
            if not is_password_hash(password_hash):
                collected.append(
                    f"DASHBOARD_USERS entry {index} ({name}): the third field is not a password "
                    f"hash. Never put a plain password here; run `uv run security.py hash-password`."
                )
                continue
            if name.lower() in seen:
                collected.append(f"DASHBOARD_USERS names {name!r} twice.")
                continue
            seen.add(name.lower())
            users.append(User(name=name, role=role, password_hash=password_hash))
        if problems is None and collected:
            raise ValueError("; ".join(collected))
        return cls(users)

    def __len__(self) -> int:
        return len(self._users)

    def __bool__(self) -> bool:
        return bool(self._users)

    def get(self, name: str | None) -> User | None:
        """The user with this name — or, for a registered user, this email — case-insensitively; or None."""
        if not name:
            return None
        key = name.strip().lower()
        return self._users.get(key) or self._by_email.get(key)

    def names(self) -> tuple[str, ...]:
        """Every user name, for a startup line. Never a hash."""
        return tuple(user.name for user in self._users.values())

    def authenticate(self, name: str | None, password: str | None) -> User | None:
        """The user, when the name and password match; None otherwise, in constant time."""
        user = self.get(name)
        if user is None or password is None:
            # Spend the same work an unknown name would not otherwise cost.
            verify_password(password or "", DUMMY_HASH)
            return None
        return user if verify_password(password, user.password_hash) else None

    def describe(self) -> str:
        """`3 user(s): 1 admin, 2 operator` — names and roles only."""
        if not self._users:
            return "no users"
        counts: dict[Role, int] = {}
        for user in self._users.values():
            counts[user.role] = counts.get(user.role, 0) + 1
        return f"{len(self._users)} user(s): " + ", ".join(
            f"{count} {role.value}" for role, count in sorted(counts.items(), key=lambda item: item[0].value)
        )


__all__ = [
    "MAX_EMAIL_LENGTH",
    "NAME_RULE",
    "PASSWORD_RULE",
    "ROLE_PERMISSIONS",
    "USER_ACTIVE",
    "USER_PENDING",
    "Permission",
    "Principal",
    "Role",
    "User",
    "UserDirectory",
    "is_email",
    "is_user_name",
    "parse_role",
    "validate_registration",
]
