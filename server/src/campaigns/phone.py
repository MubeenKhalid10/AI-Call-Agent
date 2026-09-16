"""Turning whatever a CSV contains into a number that can be dialled.

**The rule this module is built around: never guess.** A phone number that fails
to normalise costs one uncalled prospect and a line in an import report. A phone
number that normalises *wrongly* calls a stranger — an AI sales agent phoning
somebody who never appeared in anybody's data, using minutes, and in several
jurisdictions breaking the law. The two failures are not comparable, so
everything here fails closed: when the country cannot be determined with
confidence, the result is "unknown", not a best guess.

That is also why this uses Google's libphonenumber rather than a regular
expression. Deciding whether `0322 1234567` is a valid Pakistani mobile is not a
parsing problem, it is a data problem — per-country prefixes, lengths and
carrier ranges that change — and a hand-rolled version answers it confidently
and sometimes wrongly, which is the one outcome that matters here.

**What normalises and what does not.** With `default_region="PK"`, all of these
are the same number and collapse to the same E.164 string, which is what stops
one person being imported three times and called three times::

    +92 322 1234567
    +923221234567
    0092 322 1234567
    0322 1234567
    (0322) 123-4567

With no default region, the first three still work — they carry their country
with them — and the last two come back `UNKNOWN`, because `0322…` is a valid
local number in more than one country and picking one would be a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import phonenumbers


class PhoneQuality(StrEnum):
    """How much we managed to establish about a number."""

    VALID = "VALID"
    """Parsed, and libphonenumber considers it a real, dialable number."""

    INVALID = "INVALID"
    """Parsed, but it is not a valid number — wrong length, impossible prefix.

    Distinct from `UNKNOWN` because the data is definitely wrong rather than
    merely incomplete: no default region will rescue it.
    """

    UNKNOWN = "UNKNOWN"
    """Could not be interpreted without guessing the country.

    Usually a local-format number with no default region configured. The fix is
    a `DEFAULT_PHONE_REGION`, or better source data — not a guess here.
    """

    EMPTY = "EMPTY"
    """There was no number at all."""

    @property
    def is_dialable(self) -> bool:
        """Whether this produced something safe to call."""
        return self is PhoneQuality.VALID


@dataclass(frozen=True)
class NormalizedPhone:
    """The result of trying to normalise one number.

    Attributes:
        raw: Exactly what came in, unchanged. Every caller stores this too, so
            that a normalisation which turns out to be wrong can be diagnosed
            against the original rather than argued about.
        e164: The dialable form, or `None` when we could not get there safely.
        quality: Why, when `e164` is `None`.
        region: The country libphonenumber decided on, when it could.
        reason: A sentence for an import report. Empty when nothing went wrong.
    """

    raw: str
    e164: str | None
    quality: PhoneQuality
    region: str | None = None
    reason: str = ""

    @property
    def is_dialable(self) -> bool:
        """Whether this number can be handed to the telephony provider."""
        return self.quality.is_dialable and bool(self.e164)


def normalize_phone(raw: str | None, *, default_region: str | None = None) -> NormalizedPhone:
    """Normalise one phone number to E.164, or explain why not.

    Args:
        raw: The number as written by whoever produced the data.
        default_region: Two-letter country code (`"PK"`, `"US"`) to assume for
            numbers with no country code of their own. Leave it unset to refuse
            such numbers rather than assume a country.

    Returns:
        A `NormalizedPhone`. Check `is_dialable` before using `e164`; the other
        cases carry a `reason` written for a person reading an import report.
    """
    text = (raw or "").strip()
    if not text:
        return NormalizedPhone(raw=text, e164=None, quality=PhoneQuality.EMPTY, reason="no number")

    # "0092 322 …" is how a lot of exported data writes an international prefix.
    # libphonenumber understands "+" and, given a region, that region's own IDD
    # prefix — but not a foreign one, so normalise the common case ourselves.
    # This is a rewrite of *notation*, not of the number: 00 is the ITU-T
    # international prefix and means exactly what + means.
    candidate = text
    if candidate.startswith("00"):
        candidate = "+" + candidate[2:].lstrip()

    try:
        parsed = phonenumbers.parse(candidate, default_region)
    except phonenumbers.NumberParseException as exc:
        return NormalizedPhone(
            raw=text,
            e164=None,
            quality=_quality_for(exc),
            reason=_explain(exc, default_region),
        )

    if not phonenumbers.is_possible_number(parsed):
        return NormalizedPhone(
            raw=text,
            e164=None,
            quality=PhoneQuality.INVALID,
            region=phonenumbers.region_code_for_number(parsed),
            reason="the wrong number of digits for its country",
        )

    if not phonenumbers.is_valid_number(parsed):
        return NormalizedPhone(
            raw=text,
            e164=None,
            quality=PhoneQuality.INVALID,
            region=phonenumbers.region_code_for_number(parsed),
            reason="not a number that exists in its country's numbering plan",
        )

    return NormalizedPhone(
        raw=text,
        e164=phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
        quality=PhoneQuality.VALID,
        region=phonenumbers.region_code_for_number(parsed),
    )


def same_number(left: str | None, right: str | None, *, default_region: str | None = None) -> bool:
    """Whether two written numbers reach the same phone.

    Only ever true when *both* normalise cleanly. Two numbers that both failed
    to normalise are not treated as equal even if their text matches, because
    what they are is unknown, and calling two unknowns the same person is the
    kind of guess this module exists to avoid.
    """
    first = normalize_phone(left, default_region=default_region)
    second = normalize_phone(right, default_region=default_region)
    return first.is_dialable and second.is_dialable and first.e164 == second.e164


def _quality_for(exc: phonenumbers.NumberParseException) -> PhoneQuality:
    """Map a parse failure onto whether better configuration could fix it."""
    if exc.error_type == phonenumbers.NumberParseException.INVALID_COUNTRY_CODE:
        # Almost always a local-format number with no region to interpret it
        # against — recoverable by setting DEFAULT_PHONE_REGION.
        return PhoneQuality.UNKNOWN
    return PhoneQuality.INVALID


def _explain(exc: phonenumbers.NumberParseException, default_region: str | None) -> str:
    """A sentence for the import report, naming the fix where there is one."""
    if exc.error_type == phonenumbers.NumberParseException.INVALID_COUNTRY_CODE:
        if default_region:
            return (
                f"no country code, and it is not a valid {default_region} number either — "
                f"write it as +<country><number>"
            )
        return (
            "no country code, and DEFAULT_PHONE_REGION is not set, so the country cannot be "
            "determined — write it as +<country><number>, or set DEFAULT_PHONE_REGION"
        )
    if exc.error_type == phonenumbers.NumberParseException.NOT_A_NUMBER:
        return "does not look like a phone number"
    if exc.error_type == phonenumbers.NumberParseException.TOO_SHORT_NSN:
        return "too short to be a phone number"
    if exc.error_type == phonenumbers.NumberParseException.TOO_LONG:
        return "too long to be a phone number"
    return "could not be parsed as a phone number"
