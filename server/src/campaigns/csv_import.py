"""Reading a prospect list that somebody exported from something else.

**The assumption this module refuses to make is that the file has the columns
you expected.** Prospect lists come out of CRMs, spreadsheets and scrapers, and
every one of them spells the same six things differently: `First Name`,
`first_name`, `FirstName`, `fname`. So headers are matched against a table of
aliases after being reduced to letters and digits, which makes casing,
underscores, spaces and punctuation stop mattering all at once.

Columns that match nothing are **not** dropped. They go to `custom_data`, so a
file with `Lead Score` and `Last Touched` keeps them, and nothing about
importing an unfamiliar CSV requires a schema change.

**Nothing here writes to the database.** Parsing produces a report — what
mapped, what is missing, which rows are unusable and why — and the caller
decides what to do with it. That is what makes "show the user the mapping and
let them confirm before importing" possible without this module knowing whether
there is a UI at all, and it is why `--dry-run` in the CLI is one branch rather
than a second code path.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any

from .phone import NormalizedPhone, normalize_phone

# The fields a prospect row can fill in directly. Anything else a file contains
# is kept in `custom_data`.
KNOWN_FIELDS = (
    "first_name",
    "last_name",
    "phone",
    "email",
    "company",
    "job_title",
    "industry",
    "location",
    "website",
)

# Without these there is nobody to call, or nothing to call them on.
REQUIRED_FIELDS = ("first_name", "last_name", "phone")

# Header aliases, written as they appear in the wild. The keys are compared
# after `_key()` strips everything that is not a letter or digit and lowercases
# the rest, so "First Name", "first_name", "FirstName" and "FIRST-NAME" all
# arrive here as "firstname" and only that one spelling needs listing.
HEADER_ALIASES: dict[str, str] = {
    # first_name
    "firstname": "first_name",
    "fname": "first_name",
    "givenname": "first_name",
    "forename": "first_name",
    "contactfirstname": "first_name",
    # last_name
    "lastname": "last_name",
    "lname": "last_name",
    "surname": "last_name",
    "familyname": "last_name",
    "contactlastname": "last_name",
    # phone
    "phone": "phone",
    "phonenumber": "phone",
    "phoneno": "phone",
    "mobile": "phone",
    "mobilenumber": "phone",
    "mobileno": "phone",
    "cell": "phone",
    "cellphone": "phone",
    "telephone": "phone",
    "tel": "phone",
    "contactnumber": "phone",
    "primaryphone": "phone",
    "workphone": "phone",
    "businessphone": "phone",
    # email
    "email": "email",
    "emailaddress": "email",
    "mail": "email",
    "workemail": "email",
    "businessemail": "email",
    # company
    "company": "company",
    "companyname": "company",
    "organization": "company",
    "organisation": "company",
    "account": "company",
    "accountname": "company",
    "employer": "company",
    "business": "company",
    # job_title
    "jobtitle": "job_title",
    "title": "job_title",
    "position": "job_title",
    "role": "job_title",
    "designation": "job_title",
    # industry
    "industry": "industry",
    "sector": "industry",
    "vertical": "industry",
    # location
    "location": "location",
    "city": "location",
    "country": "location",
    "region": "location",
    "address": "location",
    # website
    "website": "website",
    "web": "website",
    "url": "website",
    "domain": "website",
    "companywebsite": "website",
}

# A full name in one column is common enough to be worth splitting, but only
# when the file has no separate first/last columns — see `ColumnMapping`.
_FULL_NAME_ALIASES = frozenset({"name", "fullname", "contactname", "contact", "leadname"})

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def _key(header: str) -> str:
    """Reduce a header to the form the alias table is keyed by."""
    return _NON_ALPHANUMERIC.sub("", header.strip().lower())


@dataclass(frozen=True)
class ColumnMapping:
    """Which column of the file feeds which field.

    Attributes:
        columns: `{field: header}` for everything recognised.
        full_name_column: Header holding a combined name, used only when the
            file has no separate first and last name columns.
        extras: Headers that matched nothing and will become `custom_data`.
        duplicates: `{field: [headers]}` where more than one column claimed the
            same field. The first is used; the rest are reported, because a file
            with both `Phone` and `Mobile` is one where the choice matters and
            silently picking is how the wrong number gets called.
    """

    columns: dict[str, str] = field(default_factory=dict)
    full_name_column: str | None = None
    extras: list[str] = field(default_factory=list)
    duplicates: dict[str, list[str]] = field(default_factory=dict)

    @property
    def missing_required(self) -> list[str]:
        """Required fields no column supplies.

        A file with a full-name column counts as supplying both name fields,
        since that is what the splitter will fill them from.
        """
        supplied = set(self.columns)
        if self.full_name_column:
            supplied |= {"first_name", "last_name"}
        return [name for name in REQUIRED_FIELDS if name not in supplied]

    @property
    def is_usable(self) -> bool:
        """Whether an import can proceed from this mapping."""
        return not self.missing_required

    def describe(self) -> list[str]:
        """Lines describing the mapping, for a person to confirm before importing."""
        lines = [f"  {header!r:>28}  ->  {name}" for name, header in sorted(self.columns.items())]
        if self.full_name_column:
            lines.append(f"  {self.full_name_column!r:>28}  ->  first_name + last_name (split)")
        for header in self.extras:
            lines.append(f"  {header!r:>28}  ->  custom_data[{header!r}]")
        for name, headers in sorted(self.duplicates.items()):
            ignored = ", ".join(repr(h) for h in headers)
            lines.append(f"  (!) several columns map to {name}; ignoring {ignored}")
        return lines


def map_headers(headers: list[str]) -> ColumnMapping:
    """Work out which column is which.

    Args:
        headers: The file's header row, as read.

    Returns:
        A mapping. Check `missing_required` before using it; a mapping that is
        missing a required field is still returned rather than raising, so the
        caller can show the user what *was* found alongside what was not.
    """
    columns: dict[str, str] = {}
    duplicates: dict[str, list[str]] = {}
    extras: list[str] = []
    full_name_column: str | None = None

    for header in headers:
        if header is None:
            continue
        cleaned = header.strip().lstrip("﻿")  # Excel writes a BOM into cell one.
        if not cleaned:
            continue

        key = _key(cleaned)
        field_name = HEADER_ALIASES.get(key)

        if field_name is None:
            if key in _FULL_NAME_ALIASES and full_name_column is None:
                full_name_column = cleaned
            else:
                extras.append(cleaned)
            continue

        if field_name in columns:
            duplicates.setdefault(field_name, []).append(cleaned)
            continue
        columns[field_name] = cleaned

    # A combined name column is only interesting when the real ones are absent.
    # A file with First Name, Last Name *and* Name should use the specific ones.
    if full_name_column and "first_name" in columns and "last_name" in columns:
        extras.append(full_name_column)
        full_name_column = None

    return ColumnMapping(
        columns=columns,
        full_name_column=full_name_column,
        extras=extras,
        duplicates=duplicates,
    )


@dataclass
class ParsedRow:
    """One line of the file, mapped onto prospect fields.

    Attributes:
        line: 1-based line number in the file, for an error message that a
            person can act on by opening the file and going to that line.
        errors: Why this row cannot be imported. Empty means it can.
    """

    line: int
    values: dict[str, Any] = field(default_factory=dict)
    custom_data: dict[str, Any] = field(default_factory=dict)
    phone: NormalizedPhone | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """Whether this row can become a prospect."""
        return not self.errors

    def describe(self) -> str:
        """One line naming the row and what is wrong with it."""
        who = " ".join(
            part for part in (self.values.get("first_name"), self.values.get("last_name")) if part
        )
        label = who or self.values.get("phone") or "(blank)"
        return f"line {self.line}: {label} — {'; '.join(self.errors)}"


@dataclass
class ParseReport:
    """Everything a file yielded, valid and not.

    Attributes:
        rows: Every non-blank row, in file order, valid ones included.
        skipped_blank: Rows that were entirely empty. Not errors — trailing
            newlines are normal — so they are counted rather than reported.
        error: Set when the file could not be read at all.
    """

    mapping: ColumnMapping
    rows: list[ParsedRow] = field(default_factory=list)
    skipped_blank: int = 0
    error: str | None = None

    @property
    def valid_rows(self) -> list[ParsedRow]:
        """The rows that can be imported."""
        return [row for row in self.rows if row.is_valid]

    @property
    def invalid_rows(self) -> list[ParsedRow]:
        """The rows that cannot, each carrying its reasons."""
        return [row for row in self.rows if not row.is_valid]

    def summary(self) -> str:
        """A one-line count, for the end of a CLI run."""
        if self.error:
            return f"could not read the file: {self.error}"
        return (
            f"{len(self.valid_rows)} usable, {len(self.invalid_rows)} unusable, "
            f"{self.skipped_blank} blank"
        )


def parse_csv(
    text: str,
    *,
    default_region: str | None = None,
    mapping: ColumnMapping | None = None,
) -> ParseReport:
    """Read a prospect CSV into rows, without touching the database.

    Every row is checked independently, so one malformed line does not cost the
    other nine hundred: the bad ones come back in `invalid_rows` with reasons
    and the rest are ready to import.

    Args:
        text: The file's contents.
        default_region: Country to assume for numbers with no country code.
            See `phone.normalize_phone` — unset means such numbers are refused
            rather than guessed at.
        mapping: Use this instead of deriving one from the headers, which is how
            a user's corrections to the automatic mapping are applied.

    Returns:
        A report. Check `error` first, then `mapping.missing_required`, then the
        rows.
    """
    if not text.strip():
        return ParseReport(mapping=ColumnMapping(), error="the file is empty")

    try:
        reader = csv.DictReader(io.StringIO(text))
        headers = list(reader.fieldnames or [])
    except csv.Error as exc:
        return ParseReport(mapping=ColumnMapping(), error=str(exc))

    if not headers:
        return ParseReport(mapping=ColumnMapping(), error="no header row")

    resolved = mapping or map_headers(headers)
    report = ParseReport(mapping=resolved)
    if not resolved.is_usable:
        # No point reading the rows: without a phone or a name every one of them
        # would carry the same error, and the mapping is what has to be fixed.
        return report

    seen_phones: dict[str, int] = {}
    seen_emails: dict[str, int] = {}

    try:
        for raw_row in reader:
            # `reader.line_num` rather than a counter: `DictReader` silently
            # drops entirely blank lines before we ever see them, and a quoted
            # field may span several lines, so any counter of our own drifts
            # away from the file after the first of either. A line number that
            # does not match the file is worse than none — it sends somebody to
            # the wrong row.
            row = _parse_row(raw_row, reader.line_num, resolved, default_region)
            if row is None:
                report.skipped_blank += 1
                continue
            _check_duplicates(row, seen_phones, seen_emails)
            report.rows.append(row)
    except csv.Error as exc:
        # A file that goes wrong part-way through — an unterminated quote, say.
        # Keep the rows already read: they are still importable, and telling
        # somebody "row 812 broke the file" is more use than refusing all 811.
        report.error = f"line {reader.line_num}: {exc}"

    return report


def _parse_row(
    raw_row: dict[str, Any],
    line: int,
    mapping: ColumnMapping,
    default_region: str | None,
) -> ParsedRow | None:
    """Turn one CSV record into a `ParsedRow`, or None if it is blank."""
    if not any((value or "").strip() for value in raw_row.values() if isinstance(value, str)):
        return None

    row = ParsedRow(line=line)

    for name, header in mapping.columns.items():
        value = (raw_row.get(header) or "").strip()
        if value:
            row.values[name] = value

    if mapping.full_name_column and "first_name" not in row.values:
        first, last = _split_name((raw_row.get(mapping.full_name_column) or "").strip())
        if first:
            row.values["first_name"] = first
        if last:
            row.values["last_name"] = last

    for header in mapping.extras:
        value = (raw_row.get(header) or "").strip()
        if value:
            row.custom_data[header] = value

    # csv.DictReader collects columns beyond the header row under None. They are
    # a sign of a ragged file, so they are reported rather than quietly kept.
    if raw_row.get(None):
        row.errors.append("more values than the header has columns")

    for name in REQUIRED_FIELDS:
        if not row.values.get(name):
            row.errors.append(f"missing {name.replace('_', ' ')}")

    if row.values.get("phone"):
        row.phone = normalize_phone(row.values["phone"], default_region=default_region)
        if not row.phone.is_dialable:
            row.errors.append(f"phone {row.phone.raw!r}: {row.phone.reason}")

    email = row.values.get("email")
    if email and not _looks_like_email(email):
        # Not fatal: an unusable email does not stop us phoning somebody, and
        # discarding the field loses data the operator may want to correct.
        row.values.pop("email")
        row.custom_data["email_invalid"] = email

    return row


def _check_duplicates(
    row: ParsedRow, seen_phones: dict[str, int], seen_emails: dict[str, int]
) -> None:
    """Flag a row that repeats a phone or email already seen in this file.

    Phone duplicates are errors — importing them means calling one person twice,
    which is the failure this whole layer exists to prevent. Email duplicates
    are only noted: a shared `info@` address across several contacts at one
    company is ordinary, and refusing the import over it would be wrong.
    """
    if row.phone and row.phone.is_dialable:
        number = row.phone.e164 or ""
        if number in seen_phones:
            row.errors.append(f"same phone as line {seen_phones[number]}")
        else:
            seen_phones[number] = row.line

    email = (row.values.get("email") or "").strip().lower()
    if email:
        if email in seen_emails:
            row.custom_data["duplicate_email_of_line"] = seen_emails[email]
        else:
            seen_emails[email] = row.line


def _split_name(full: str) -> tuple[str, str]:
    """Split a combined name into first and last.

    Deliberately simple — first word, then the rest — because anything cleverer
    is wrong for some naming convention, and the parts are shown back to the
    user before import.
    """
    parts = full.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _looks_like_email(value: str) -> bool:
    """A deliberately loose check: one @, something either side, a dot after."""
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".")
