"""Password hashing with nothing but the standard library. Phase 18.

scrypt (`hashlib.scrypt`, RFC 7914) with a random 16-byte salt, encoded as

    scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>

so the parameters travel with the hash and can be raised later without
invalidating anyone's password: `verify_password` reads them from the
string it is given, never from a constant.

**Why scrypt and not bcrypt or argon2.** Both are installed here, but only
as somebody else's transitive dependency, and a password check that stops
working when an unrelated package drops its dependency is the wrong kind
of surprise. scrypt is in the standard library on every platform this
project runs on and is a memory-hard function the OWASP cheat sheet still
lists. The parameters (`n=2**14, r=8, p=1`, 16 MiB) are its recommended
minimum; `hash_password` takes larger ones.

Comparison is constant-time. A malformed hash verifies as False rather
than raising, so a directory entry somebody mistyped cannot take the login
page down — it is reported at startup by `UserDirectory.parse` instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

SCHEME = "scrypt"
DEFAULT_N = 2**14
DEFAULT_R = 8
DEFAULT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
#: The most memory a verification may use; stops a hostile `n` in a hash
#: string from asking for gigabytes.
_MAX_MEM = 64 * 1024 * 1024

#: Passwords longer than this are refused rather than hashed: scrypt's cost
#: does not grow with length, but a request body should still have a bound.
MAX_PASSWORD_LENGTH = 1024
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P) -> str:
    """Hash a password for `DASHBOARD_USERS`.

    Raises:
        ValueError: The password is shorter than `MIN_PASSWORD_LENGTH`, longer
            than `MAX_PASSWORD_LENGTH`, or the parameters are not usable.
    """
    if not isinstance(password, str):
        raise ValueError("a password must be text")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"a password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"a password must be at most {MAX_PASSWORD_LENGTH} characters")
    if n < 2**12 or n & (n - 1) or r < 1 or p < 1 or 128 * r * n * p > _MAX_MEM:
        raise ValueError("scrypt parameters out of range")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES, maxmem=_MAX_MEM)
    return "$".join((SCHEME, str(n), str(r), str(p), _b64(salt), _b64(digest)))


def verify_password(password: str, encoded: str | None) -> bool:
    """Whether `password` produces `encoded`. False for anything malformed. Constant-time."""
    parsed = _parse(encoded)
    if parsed is None or not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
        return False
    n, r, p, salt, expected = parsed
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=_MAX_MEM
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(digest, expected)


def is_password_hash(value: str | None) -> bool:
    """Whether a string is a hash this module produced (and not, say, a plain password)."""
    return _parse(value) is not None


def _parse(encoded: str | None) -> tuple[int, int, int, bytes, bytes] | None:
    if not encoded or not isinstance(encoded, str):
        return None
    parts = encoded.strip().split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, digest = _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, TypeError):
        return None
    if n < 2**10 or n & (n - 1) or r < 1 or p < 1 or 128 * r * n * p > _MAX_MEM:
        return None
    if len(salt) < 8 or len(digest) < 16:
        return None
    return n, r, p, salt, digest


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


#: A real hash of a random password, verified against when a login names a
#: user who does not exist — so the response takes the same time either way.
DUMMY_HASH = hash_password(secrets.token_urlsafe(24))


__all__ = [
    "DUMMY_HASH",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "hash_password",
    "is_password_hash",
    "verify_password",
]
