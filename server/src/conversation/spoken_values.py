"""Spoken phone numbers and email addresses, turned into the values a CRM wants.

The speech-to-text stage hears "zero three zero zero double one two three" and
writes exactly that. The transcript should keep those words — they are the
evidence of what was said — but the record beside it needs `03001123`, and the
calendar needs `john.smith2@gmail.com`, not "john dot smith two at gmail dot
com". This module is the layer between the two.

It is context-aware on purpose. "We have twenty employees" contains a number and
is not a phone number; "two hundred fifty thousand dollars" is an amount whose
value must survive as the caller said it. So:

* a run of digits is a phone number only when it is long enough to be one and
  does not read as a quantity (a unit after it, "thousand"/"million" inside it,
  a currency sign or a thousands separator on it) — and a shorter run counts
  only when the talk around it is about a number to reach them on;
* an address is read only where the words have the shape of one
  ("… at … dot com"), and then only with a reason to believe it is theirs —
  email was being talked about, the domain is a mail provider, or the part
  before the "at" is written the way addresses are (dots, digits, underscores);
* dates are not touched here: the model writes them as ISO 8601 for the tools
  and `timeparse.parse_when` validates them, which is already a structured
  representation;
* nothing else is converted. The words of the turn are never rewritten.

Pure functions over one short string: no I/O, no model, patterns compiled at
import. A turn costs tens of microseconds (`tests/test_spoken_values.py`
measures it), so nothing here can be heard as latency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "SpokenValue",
    "ensure_read_back",
    "find_email",
    "find_phone",
    "normalize_digits",
    "normalize_email",
    "normalize_phone",
    "speakable",
]

_ONES = {
    "zero": "0", "oh": "0", "o": "0", "nought": "0", "naught": "0", "nil": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}  # fmt: skip
# What the recogniser writes when a digit is said quickly between other digits.
# Read as digits only with a digit on both sides ("three zero for five six").
_HOMOPHONES = {"to": "2", "too": "2", "for": "4", "fore": "4", "ate": "8", "won": "1", "tree": "3"}
# "oh" and "o" are also an interjection and a letter; alone they are not a zero.
_WEAK_ZERO = frozenset({"oh", "o"})
_TEENS = {
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19",
}  # fmt: skip
_TENS = {
    "twenty": "2", "thirty": "3", "forty": "4", "fourty": "4", "fifty": "5",
    "sixty": "6", "seventy": "7", "eighty": "8", "ninety": "9",
}  # fmt: skip
_REPEATS = {
    "double": 2, "triple": 3, "treble": 3, "thrice": 3,
    "quadruple": 4, "quad": 4, "tetra": 4,
}  # fmt: skip
# Said between the groups of a number; they join a run rather than end it.
_JOINERS = frozenset({"then", "uh", "um", "er", "dash", "hyphen", "space"})
# A run that contains or touches one of these is an amount, not a number to dial.
_MAGNITUDES = frozenset({"million", "billion", "trillion", "lakh", "lakhs", "lac", "crore", "crores", "k"})
_UNITS = frozenset(
    """employees employee people persons staff reps agents seats licenses licences users customers
    clients members offices branches locations stores sites percent percentage dollars dollar bucks
    rupees rupee euros euro pounds pound dirhams riyals usd pkr gbp eur aed years year months month
    weeks week days day hours hour minutes minute seconds times calls leads deals meetings orders
    units items am pm oclock""".split()
)
_CURRENCY = frozenset({"rs", "pkr", "usd", "dollars", "rupees", "$", "£", "€"})

# A number the recogniser already wrote as digits: 0300, 555-2671, (415), +92.
_NUMERIC = re.compile(r"^\+?[\d()\-]*\d[\d()\-]*$")
# …and one it wrote as an amount: 25,000  $500  12.5  40%
_AMOUNT = re.compile(r"^[$£€]|[%$£€]$|\d[.,]\d")

_PHONE_CUE = re.compile(
    r"\b(number|phone|mobile|cell|cellphone|whatsapp|landline|extension|digits"
    r"|(call|reach|ring|text|contact) (me|him|her|us|them) (on|at))\b"
)
_EMAIL_CUE = re.compile(r"\b(e-?mail|mail|address|inbox|invite|invitation)\b")

# "No, it's …" restarts a number; it does not continue the one before it.
_CORRECTION = frozenset({"no", "not", "nope", "wrong", "sorry", "actually", "correction", "instead", "again"})

_MIN_PHONE = 7
_MIN_PHONE_UNPROMPTED = 9
_MAX_PHONE = 15  # E.164

_DOTS = frozenset({"dot", "period", "point"})
_DASHES = frozenset({"dash", "hyphen", "minus"})
_EMAIL_STOPS = frozenset(
    """is its it's it email e-mail mail address to on at me my mine the that's thats that be would use
    send yes yeah yep sure okay ok um uh so well and or as id this here""".split()
)
# Words a sentence runs on before the address starts: "absolutely john at gmail
# dot com", "please try sarah at …". Unlike the stops above they end the name
# only from the outside — never the word right before the "at" ("hello at acme
# dot com") and never a word joined to the next by a spoken dot, underscore or
# dash ("just dot john at …"). No first names in here ("will", "mark", "grant"),
# and no one-letter words: a lone letter next to an address is a spelled one.
_EMAIL_LEAD_INS = frozenset(
    """absolutely definitely certainly totally exactly obviously basically literally honestly actually
    simply just really right correct great perfect fine good cool awesome alright course please thanks
    thank welcome you your yours we he she they him her them us our his their an of for with in by
    from via through like hmm er ah oh hey hi hello listen look see try write put note take down have
    has got get reach contact ping message type enter spell spelled spelt goes say said saying called
    named know think believe guess remember want need can could should do does did also then now
    personal work business company office official new old other same best main primary""".split()
)
_EMAIL_SEPARATORS = frozenset({"dot", "period", "point", "dash", "hyphen", "minus", "underscore", "under", "plus"})
# Where the sentence before the address ended: "Absolutely. John at…", "Sure, john at…".
_SENTENCE_BREAK = ".!?:;,"
_MAIL_PROVIDERS = frozenset(
    "gmail googlemail yahoo ymail hotmail outlook live msn icloud me aol proton protonmail zoho gmx".split()
)
_WRITTEN_EMAIL = re.compile(r"[a-z0-9][a-z0-9._%+\-]*@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+")
_VALID_EMAIL = re.compile(r"^[a-z0-9](?:[a-z0-9._%+\-]*[a-z0-9_])?@[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9\-]+)*\.[a-z]{2,}$")
_AT_PHRASE = re.compile(r"\bat (?:the )?(?:rate|sign|symbol)(?: of)?\b|\bat-the-rate\b")
_SPELLED = re.compile(r"^(?:[a-z0-9]-)+[a-z0-9]$")
_EDGE_PUNCTUATION = ",.;:!?\"'“”‘’…"

_DIGIT_NAMES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


@dataclass(frozen=True)
class SpokenValue:
    """One value read out of a turn.

    Attributes:
        kind: `phone` or `email`.
        value: The normalized value: a digit string (with a leading `+` if one
            was said) or a lower-case address.
        raw: The caller's words it was read from, as the transcript has them.
        complete: False for the first part of a phone number whose rest has not
            been heard yet (the recogniser ends a turn on a pause between groups).
    """

    kind: str
    value: str
    raw: str
    complete: bool = True


def _tokens(text: str) -> list[str]:
    """Lower-case words with the punctuation around them removed; `a.b` and `a-b` survive."""
    out = []
    for piece in text.lower().split():
        token = piece.strip(_EDGE_PUNCTUATION)
        if token:
            out.append(token)
    return out


def _sentence_breaks(text: str) -> set[int]:
    """Indexes into `_tokens(text)` of the words a sentence or clause ended on.

    "Absolutely. John at gmail dot com" breaks after its first word. A pause
    inside a dictated value is written the same way ("j, o, h, n", "smith, two",
    "jane, dash sales"), so a letter, a number word, a spoken symbol, or a word
    followed by one of those is never a break.
    """
    pieces = [(piece.strip(_EDGE_PUNCTUATION), piece) for piece in text.lower().split()]
    pieces = [(token, piece) for token, piece in pieces if token]
    breaks: set[int] = set()
    for index, (token, piece) in enumerate(pieces):
        if piece.rstrip("\"'“”‘’)")[-1:] not in _SENTENCE_BREAK:
            continue
        nxt = pieces[index + 1][0] if index + 1 < len(pieces) else ""
        dictated = (token, nxt)
        if len(token) == 1 or any(
            word in _EMAIL_SEPARATORS or word in _TENS or _digit_value(word) is not None for word in dictated
        ):
            continue
        breaks.add(index)
    return breaks


def _digit_value(token: str) -> str | None:
    """The digits one token stands for on its own, or None."""
    if token in _ONES:
        return _ONES[token]
    if token in _TEENS:
        return _TEENS[token]
    if _NUMERIC.match(token) and not _AMOUNT.search(token):
        return re.sub(r"[^\d+]", "", token)
    return None


@dataclass
class _Run:
    """A stretch of consecutive number words: what it spells and where it sat."""

    digits: str
    start: int
    end: int  # exclusive
    quantity: bool = False


def _digit_runs(tokens: list[str]) -> list[_Run]:
    """Every stretch of number words in `tokens`, read the way a number is dictated."""
    runs: list[_Run] = []
    i, n = 0, len(tokens)
    while i < n:
        start = i
        digits = ""
        quantity = False
        last_single = False  # whether the previous element was one spoken digit
        while i < n:
            token = tokens[i]
            nxt = tokens[i + 1] if i + 1 < n else ""
            if token in _REPEATS and (_digit_value(nxt) is not None or nxt in _TENS):
                value = _digit_value(nxt) or _TENS[nxt] + "0"
                digits += value * _REPEATS[token]
                last_single = False
                i += 2
            elif token == "plus" and not digits and _digit_value(nxt) is not None:
                digits = "+"
                i += 1
            elif token in _WEAK_ZERO and not (digits or _digit_value(nxt) is not None or nxt in _REPEATS):
                break
            elif token in _TENS:
                ones = _ONES.get(nxt) if nxt not in _WEAK_ZERO and nxt != "zero" else None
                digits += _TENS[token] + (ones or "0")
                last_single = False
                i += 2 if ones else 1
            elif token == "hundred" and last_single:
                # "three hundred" is 300; "three hundred fifty five" is 355.
                if nxt == "and":
                    quantity = True
                    i += 1
                    nxt = tokens[i + 1] if i + 1 < n else ""
                if nxt in _TENS or nxt in _TEENS:
                    i += 1
                    continue_with = tokens[i]
                    after = tokens[i + 1] if i + 1 < n else ""
                    if continue_with in _TEENS:
                        digits += _TEENS[continue_with]
                        i += 1
                    else:
                        ones = _ONES.get(after) if after not in _WEAK_ZERO and after != "zero" else None
                        digits += _TENS[continue_with] + (ones or "0")
                        i += 2 if ones else 1
                else:
                    digits += "00"
                    i += 1
                last_single = False
            elif token == "thousand" and last_single:
                digits += "000"
                last_single = False
                i += 1
            elif token in ("hundred", "thousand") or token in _MAGNITUDES:
                if not digits:
                    break
                quantity = True
                i += 1
            elif token in _HOMOPHONES and digits and _digit_value(nxt) is not None:
                digits += _HOMOPHONES[token]
                last_single = True
                i += 1
            elif (value := _digit_value(token)) is not None:
                digits += value
                last_single = token in _ONES
                i += 1
            elif token in _JOINERS and digits and (_digit_value(nxt) is not None or nxt in _REPEATS or nxt in _TENS):
                i += 1
            else:
                break
        if i == start:
            if _AMOUNT.search(tokens[i]) and any(c.isdigit() for c in tokens[i]):
                runs.append(_Run("", i, i + 1, quantity=True))
            i += 1
            continue
        before = tokens[start - 1] if start else ""
        after = tokens[i] if i < n else ""
        if after in _UNITS or after in _MAGNITUDES or before in _CURRENCY:
            quantity = True
        runs.append(_Run(digits, start, i, quantity))
    return runs


def normalize_digits(text: str) -> str:
    """Every digit spoken in `text`, in order — for a field known to hold a number.

    "zero three zero zero, double five, seven" → `0300557`. Words that are not
    part of a number are skipped, so "it's zero three double one" → `0311`.
    """
    return "".join(run.digits for run in _digit_runs(_tokens(text)))


def normalize_phone(text: str) -> str | None:
    """`text`, known to be a phone number, as a digit string; None if it cannot be one."""
    digits = normalize_digits(text)
    plus = digits.startswith("+")
    digits = digits.replace("+", "")
    if not _MIN_PHONE <= len(digits) <= _MAX_PHONE:
        return None
    return ("+" if plus else "") + digits


def find_phone(text: str, *, context: str = "", pending: str = "") -> SpokenValue | None:
    """A phone number the caller dictated in this turn, if there is one.

    Args:
        text: What the caller said, as transcribed.
        context: What the agent said just before, which is where "what number
            can we reach you on?" lives.
        pending: The first part of a number from the caller's previous turn,
            when that turn ended before the number did.

    Returns:
        The number, or its first part (`complete=False`), or None. A quantity
        ("twenty employees", "two hundred thousand rupees") is never returned.
    """
    tokens = _tokens(text)
    runs = [run for run in _digit_runs(tokens) if run.digits and not run.quantity]
    if not runs:
        return None
    best = max(runs, key=lambda run: len(run.digits))
    digits = best.digits
    plus = digits.startswith("+")
    count = len(digits) - plus
    raw = " ".join(text.split())

    if pending and count < _MIN_PHONE_UNPROMPTED and not _CORRECTION.intersection(tokens):
        # The rest of a number: only when the turn is little else, so "three of
        # us will join" after a number does not get appended to it.
        spoken_for = sum(run.end - run.start for run in runs)
        joined = pending + "".join(run.digits for run in runs).replace("+", "")
        if spoken_for * 2 >= len(tokens) and len(joined.replace("+", "")) <= _MAX_PHONE:
            whole = len(joined.replace("+", "")) >= _MIN_PHONE
            return SpokenValue("phone", joined, raw, complete=whole)

    prompted = bool(_PHONE_CUE.search(text.lower()) or _PHONE_CUE.search(context.lower()))
    if count > _MAX_PHONE:
        return None
    if count >= (_MIN_PHONE if prompted else _MIN_PHONE_UNPROMPTED):
        return SpokenValue("phone", digits, raw)
    if prompted and count >= 3 and len(runs) == 1 and (best.end - best.start) * 2 >= len(tokens):
        return SpokenValue("phone", digits, raw, complete=False)
    return None


def _render(tokens: list[str]) -> str:
    """Address words as address characters: dot → `.`, digits as digits, the rest joined."""
    out = ""
    i, n = 0, len(tokens)
    while i < n:
        token = tokens[i]
        nxt = tokens[i + 1] if i + 1 < n else ""
        if token in _DOTS:
            out += "."
        elif token in _DASHES:
            out += "-"
        elif token == "underscore" or (token == "under" and nxt == "score"):
            out += "_"
            i += token == "under"
        elif token == "plus":
            out += "+"
        elif token in _REPEATS and nxt and (len(nxt) == 1 or nxt in _ONES):
            # "double two" is 22; "double l" and "double o" are letters.
            out += (nxt if len(nxt) == 1 else _ONES[nxt]) * _REPEATS[token]
            i += 1
        elif token in _TENS:
            ones = _ONES.get(nxt) if nxt not in _WEAK_ZERO and nxt != "zero" else None
            out += _TENS[token] + (ones or "0")
            i += 1 if ones else 0
        elif token in _TEENS:
            out += _TEENS[token]
        elif token in _ONES and token != "o":
            out += _ONES[token]
        elif _SPELLED.match(token):
            out += token.replace("-", "")
        else:
            out += token
        i += 1
    return out


def _clean_email(candidate: str) -> str | None:
    candidate = re.sub(r"\.{2,}", ".", candidate.strip(".-"))
    candidate = candidate.replace(".@", "@").replace("@.", "@")
    return candidate if _VALID_EMAIL.match(candidate) else None


def normalize_email(text: str) -> str | None:
    """`text`, known to be an email address however it was written, as a valid one.

    Accepts the written form, the spoken form ("john dot smith two at gmail dot
    com") and anything between ("john.smith at gmail dot com"). None when no
    valid address can be read from it.
    """
    lowered = _AT_PHRASE.sub(" at ", (text or "").lower())
    written = _WRITTEN_EMAIL.search(lowered)
    if written and (clean := _clean_email(written.group(0))):
        return clean
    tokens = _tokens(lowered.replace("@", " at "))
    if "at" not in tokens:
        return None
    at = len(tokens) - 1 - tokens[::-1].index("at")
    return _clean_email(f"{_render(tokens[:at])}@{_render(tokens[at + 1:])}")


def find_email(text: str, *, context: str = "") -> SpokenValue | None:
    """An email address the caller gave in this turn, if there is one.

    Args:
        text: What the caller said, as transcribed.
        context: What the agent said just before ("what's the best email?").

    Returns:
        The address, or None. "We're at forty percent" and "look at acme dot
        com" are not addresses: the first has no domain, the second no reason
        to think it is one (see the module docstring).
    """
    lowered = _AT_PHRASE.sub(" at ", text.lower())
    raw = " ".join(text.split())
    written = _WRITTEN_EMAIL.search(lowered)
    if written and (clean := _clean_email(written.group(0))):
        return SpokenValue("email", clean, raw)

    spaced = lowered.replace("@", " at ")
    tokens = _tokens(spaced)
    breaks = _sentence_breaks(spaced)
    talked_about =bool(_EMAIL_CUE.search(lowered) or _EMAIL_CUE.search(context.lower()))
    for at in (i for i, token in enumerate(tokens) if token == "at"):
        # The domain: words up to the first dot are one name ("hash makers dot
        # net"); after a dot, each label is one word, and the next plain word
        # after a label is the rest of the sentence.
        domain: list[str] = []
        dotted = False
        for token in tokens[at + 1 : at + 9]:
            if token in _DOTS or token in _DASHES:
                dotted = dotted or token in _DOTS
                domain.append(token)
            elif dotted and domain[-1] not in _DOTS and domain[-1] not in _DASHES:
                break
            elif token in _EMAIL_STOPS and token not in _MAIL_PROVIDERS:
                break
            else:
                dotted = dotted or "." in token
                domain.append(token)
        if not dotted:
            continue
        # The name: back to the word that introduced it ("my email is …"), to
        # the end of the sentence before it, or to a word the sentence merely
        # ran on ("absolutely john at …") — whichever comes first.
        local: list[str] = []
        for j in range(at - 1, max(0, at - 12) - 1, -1):
            token = tokens[j]
            # A spoken dot, underscore or dash after a word makes it part of the address.
            joined = tokens[j + 1] in _EMAIL_SEPARATORS
            if len(token) > 1 and not joined:
                if token in _EMAIL_STOPS:
                    break
                if j < at - 1 and (j in breaks or token in _EMAIL_LEAD_INS):
                    break
            local.append(token)
        local.reverse()
        if not local:
            continue
        clean = _clean_email(f"{_render(local)}@{_render(domain)}")
        if clean is None:
            continue
        name, _, host = clean.partition("@")
        address_shaped = len(local) > 1 or any(c in name for c in "._-+") or any(c.isdigit() for c in name)
        if talked_about or host.split(".")[0] in _MAIL_PROVIDERS or address_shaped:
            return SpokenValue("email", clean, raw)
    return None


def _reads_back(reply: str, value: str) -> bool:
    """Whether `reply` says `value` — in any wording that spells the same value."""
    if "@" not in value:
        return value.lstrip("+") in normalize_digits(reply).replace("+", "")
    tokens = _tokens(_AT_PHRASE.sub(" at ", reply.lower()).replace("@", " at "))
    for at in (i for i, token in enumerate(tokens) if token == "at"):
        for before in range(1, min(at, 12) + 1):
            for after in range(2, min(len(tokens) - at - 1, 8) + 1):
                candidate = f"{_render(tokens[at - before : at])}@{_render(tokens[at + 1 : at + 1 + after])}"
                if _clean_email(candidate) == value:
                    return True
    return False


def ensure_read_back(reply: str, value: str) -> str:
    """`reply`, saying `value` back to the caller in words — added only if it does not already.

    The model is asked to confirm a dictated number or address and usually
    does; measured live it skipped one in six. A read-back is how a caller
    catches a misheard address, so it is not left to chance: a reply without
    one gets a short confirmation in front of it. A value the model wrote out
    (`john@gmail.com`, `0300…`) becomes words either way — a voice reads a digit
    string as a quantity.
    """
    spoken = speakable(value)
    reply = re.sub(re.escape(value), lambda _: spoken, reply, flags=re.IGNORECASE)
    if _reads_back(reply, value):
        return reply
    if "@" not in value:
        # A sentence that says a number, and not this one, is a read-back that
        # went wrong. Two numbers in one reply is worse than one: it goes.
        sentences = re.split(r"(?<=[.!?…])\s+", reply.strip())
        reply = " ".join(s for s in sentences if len(normalize_digits(s).replace("+", "")) < 5)
    return f"So that's {spoken}. {reply.lstrip()}".rstrip()


def speakable(value: str) -> str:
    """`value` as words a voice can read back: digits one by one, symbols by name.

    `03001234567` → "zero three zero zero, one two three, four five six seven";
    `john.smith2@gmail.com` → "john dot smith two at gmail dot com". The TTS is
    never handed a digit string, which it would read as a quantity.
    """
    if "@" in value:
        names = {".": " dot ", "@": " at ", "_": " underscore ", "-": " dash ", "+": " plus "}
        words = "".join(names.get(c) or (f" {_DIGIT_NAMES[int(c)]} " if c.isdigit() else c) for c in value)
        return " ".join(words.split())
    digits = [c for c in value if c.isdigit()]
    sizes = {11: [4, 3, 4], 12: [4, 4, 4]}.get(len(digits)) or [3] * (max(len(digits) - 4, 0) // 3) + [4]
    if sum(sizes) != len(digits):
        sizes = [len(digits) - sum(sizes[1:])] + sizes[1:] if len(digits) > sum(sizes[1:]) else [len(digits)]
    groups, at = [], 0
    for size in sizes:
        groups.append(" ".join(_DIGIT_NAMES[int(d)] for d in digits[at : at + size]))
        at += size
    spoken = ", ".join(group for group in groups if group)
    return f"plus {spoken}" if value.startswith("+") else spoken
