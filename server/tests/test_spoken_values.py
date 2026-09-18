"""Checks that a dictated phone number or email address is stored as the value, not the words.

Run it from the `server/` directory::

    uv run python tests/test_spoken_values.py

The recogniser writes what it hears — "zero three zero zero double one two
three" — and the transcript should keep that. The record beside it needs
`03001123`, and "we have twenty employees" must stay a sentence. Three layers:

* `spoken_values` — the reading itself: digit by digit, double/triple/quadruple,
  groups, addresses, and the things that must *not* be read as contact details;
* `SalesConversation` — the turn is kept verbatim, the record gets the value,
  the model is handed the value for its reply, a number split across two turns
  is joined, and `book_meeting` sends the calendar a real address;
* the cost — microseconds per turn, so it cannot be heard.

Deterministic; no keys, no network, no database. Exit status is 0 when every
check passes.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from src.conversation.spoken_values import (  # noqa: E402
    ensure_read_back,
    find_email,
    find_phone,
    normalize_digits,
    normalize_email,
    normalize_phone,
    speakable,
)

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


def check_digits() -> None:
    print("\n=== digits, as dictated ===")
    cases = {
        "zero three zero zero": "0300",
        "double zero": "00",
        "double one": "11",
        "triple one": "111",
        "triple zero": "000",
        "quadruple one": "1111",
        "tetra one": "1111",
        "quadruple zero": "0000",
        "tetra zero": "0000",
        "double three five": "335",
        "triple seven two": "7772",
        "zero three zero zero, double five, seven": "0300557",
        "zero three zero zero double one two three four five six seven": "030011234567",
        "oh three hundred, one twenty three, forty five, sixty seven": "03001234567",
        "eight hundred, five five five, one thousand": "8005551000",
        "plus nine two, three zero zero, one two three four five six seven": "+923001234567",
        "three zero for five six": "30456",
        "0300 1234567": "03001234567",
        "It's (415) 555-2671.": "4155552671",
        "zero three zero zero um then double five": "030055",
    }
    for spoken, want in cases.items():
        got = normalize_digits(spoken)
        check(f"{spoken!r} -> {want}", got == want, repr(got))
    for word, digit in zip(
        ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"), "0123456789", strict=True
    ):
        got = normalize_digits(f"double {word} triple {word} quadruple {word}")
        check(f"double/triple/quadruple {word}", got == digit * 9, repr(got))
    check("a phone field of seven to fifteen digits is a number", normalize_phone("zero three zero zero, double five, seven") == "0300557")
    check("…and three digits are not", normalize_phone("one two three") is None)


def check_phone_context() -> None:
    print("\n=== a phone number, only where there is one ===")
    asked = "What's the best number to reach you on?"
    found = find_phone("Sure, it's zero three zero zero double one two three four five six seven.", context=asked)
    check("digit by digit with a double", bool(found) and found.value == "030011234567", repr(found))
    check("the caller's words are kept beside it", bool(found) and found.raw.startswith("Sure, it's zero three"), repr(found))
    found = find_phone("oh three hundred, one twenty three, forty five, sixty seven", context=asked)
    check("spoken in groups", bool(found) and found.value == "03001234567", repr(found))
    found = find_phone("triple seven, triple one, double zero, double nine")
    check("unprompted, when it is long enough to be nothing else", bool(found) and found.value == "7771110099", repr(found))

    for sentence in (
        "We have twenty employees.",
        "We have about two hundred fifty staff and three offices",
        "We spend two hundred fifty thousand rupees a month",
        "our budget is one million two hundred thousand",
        "around 25,000 dollars, maybe $30,000",
        "call me at five",
        "oh, one second please",
        "twenty twenty six was a good year for one of us",
        "we make about three hundred and fifty calls a day with nine people",
    ):
        check(f"not a number to dial: {sentence!r}", find_phone(sentence) is None, repr(find_phone(sentence)))
    check(
        "an amount stays an amount even when a number was asked for",
        find_phone("We spend two hundred fifty thousand rupees a month", context=asked) is None,
    )

    first = find_phone("my number is zero three zero zero")
    check("the first group alone is a start, not a number", bool(first) and not first.complete and first.value == "0300", repr(first))
    rest = find_phone("double five seven, one two three four", pending="0300")
    check("the next turn finishes it", bool(rest) and rest.complete and rest.value == "03005571234", repr(rest))
    check("a sentence after a number is not appended to it", find_phone("three of us will join the meeting", pending="0300") is None)
    again = find_phone("no it's zero three zero one, five five five, one two one two", context=asked, pending="0300555")
    check("a correction replaces, it does not append", bool(again) and again.value == "03015551212", repr(again))


def check_email() -> None:
    print("\n=== an email address ===")
    cases = {
        "john dot smith at gmail dot com": "john.smith@gmail.com",
        "My email is john dot smith two at gmail dot com.": "john.smith2@gmail.com",
        "it's mary underscore jane dash k at hash makers dot co dot uk thanks": "mary_jane-k@hashmakers.co.uk",
        "sure, a l i double nine at the rate yahoo dot com": "ali99@yahoo.com",
        "j-o-h-n twenty three at outlook dot com": "john23@outlook.com",
        "John.Smith2@Gmail.com": "john.smith2@gmail.com",
        "john.smith at gmail.com": "john.smith@gmail.com",
    }
    for spoken, want in cases.items():
        found = find_email(spoken, context="What's the best email for the invite?")
        check(f"{spoken!r} -> {want}", bool(found) and found.value == want, repr(found))
    # The words a sentence runs on before the address are not part of it.
    lead_ins = {
        "absolutely john at gmail dot com": "john@gmail.com",
        "Absolutely. John at Gmail dot com.": "john@gmail.com",
        "Sure, it's john at gmail dot com": "john@gmail.com",
        "okay great perfect john at gmail dot com": "john@gmail.com",
        "Yeah definitely john dot smith two at gmail dot com": "john.smith2@gmail.com",
        "Of course, please try sarah underscore k at outlook dot com": "sarah_k@outlook.com",
        "No problem. Sarah Khan at yahoo dot com": "sarahkhan@yahoo.com",
        # …while a name said as two words, a role address, a word tied on by a
        # spoken dot, and a pause inside the dictation all stay whole.
        "you can reach me john smith at gmail dot com": "johnsmith@gmail.com",
        "my email is hello at hashmakers dot net": "hello@hashmakers.net",
        "email me, contact at acme dot io": "contact@acme.io",
        "will dot smith at gmail dot com": "will.smith@gmail.com",
        "just dot john at gmail dot com": "just.john@gmail.com",
        "it's a l i at gmail dot com": "ali@gmail.com",
        "My email is J, O, H, N at gmail dot com": "john@gmail.com",
        "It's John dot Smith, two at Gmail dot com.": "john.smith2@gmail.com",
        "Her email address is Mary underscore Jane, dash sales at Hashmakers dot net.": "mary_jane-sales@hashmakers.net",
    }
    for spoken, want in lead_ins.items():
        found = find_email(spoken)
        check(f"{spoken!r} -> {want}", bool(found) and found.value == want, repr(found))
        check("…and the caller's words are kept as they were", bool(found) and found.raw == spoken, repr(found))
    for sentence in ("we're at forty percent", "look at acme dot com", "I'm at work right now"):
        check(f"not an address: {sentence!r}", find_email(sentence) is None, repr(find_email(sentence)))
    check("a tool argument in spoken form", normalize_email("john dot smith two at gmail dot com") == "john.smith2@gmail.com")
    check("a tool argument already written", normalize_email(" John.Smith2@gmail.com ") == "john.smith2@gmail.com")
    check("nonsense is refused, not guessed", normalize_email("nonsense") is None and normalize_email("") is None)


def check_read_back() -> None:
    print("\n=== read back in words ===")
    check("a number, digit by digit in groups", speakable("03001234567") == "zero three zero zero, one two three, four five six seven", speakable("03001234567"))
    check("an address, symbols by name", speakable("john.smith2@gmail.com") == "john dot smith two at gmail dot com", speakable("john.smith2@gmail.com"))
    check("no digit reaches the voice", not any(c.isdigit() for c in speakable("+923001234567") + speakable("a_b-9@x.co")))


def check_guaranteed_read_back() -> None:
    print("\n=== a read-back that does not depend on the model ===")
    email, phone = "john.smith2@gmail.com", "03005571234"
    said = "Sure, I can have details sent to john dot smith two at gmail dot com. What are you interested in?"
    check("a reply that reads the address back is left alone", ensure_read_back(said, email) == said)
    said = "Got it, I'll add Mary underscore Jane, dash sales at Hashmakers dot net to that."
    check("…whatever its capitals and commas", ensure_read_back(said, "mary_jane-sales@hashmakers.net") == said)
    missing = "Happy to send over some details, John. Is it web or mobile you'd be looking at?"
    fixed = ensure_read_back(missing, email)
    check("a reply without one gets it first", fixed == "So that's john dot smith two at gmail dot com. " + missing, fixed)
    wrong = "Got it, I'll loop in Mary, Jane, sales at Hashmakers, net."
    fixed = ensure_read_back(wrong, "mary_jane-sales@hashmakers.net")
    check("a paraphrase that lost the symbols does not count", fixed.startswith("So that's mary underscore jane dash sales at hashmakers dot net. "), fixed)
    fixed = ensure_read_back("I'll send it to John.Smith2@gmail.com today.", email)
    check("an address the model wrote out is said in words", fixed == "I'll send it to john dot smith two at gmail dot com today.", fixed)
    said = "Thanks, that's zero three double zero, five five seven, one two three four."
    check("a number read back in any grouping counts", ensure_read_back(said, phone) == said)
    fixed = ensure_read_back("Thanks, I've got 03005571234.", phone)
    check("a digit string never reaches the voice", fixed == "Thanks, I've got zero three zero zero, five five seven, one two three four.", fixed)
    garbled = "Got it, that's zero three zero, one two three, four five six. Is that the best one to reach you on?"
    fixed = ensure_read_back(garbled, "03001234567")
    check(
        "a read-back with the wrong digits is replaced, not kept beside the right one",
        fixed == "So that's zero three zero zero, one two three, four five six seven. Is that the best one to reach you on?",
        fixed,
    )
    fixed = ensure_read_back("Thanks, noted.", phone)
    check("a number left out is said first", fixed == "So that's zero three zero zero, five five seven, one two three four. Thanks, noted.", fixed)


async def check_conversation() -> None:
    print("\n=== the call: words in the transcript, values in the record ===")
    from test_conversation import RecordingSink, brief_for  # the suite's own fixtures

    from src.conversation import SalesConversation

    def make_conversation() -> SalesConversation:
        return SalesConversation(brief_for(first_name="Sarah"), sink=RecordingSink(), knowledge_base=False)

    conversation = make_conversation()
    conversation.note_agent_turn("What's the best number to reach you on?")
    said = "Sure, it's zero three zero zero double one two three four five six seven."
    await conversation.note_user_turn(said)
    record = conversation.record
    check("the record holds the digits", record.contact_phone == "030011234567", repr(record.contact_phone))
    check("…and the words they came from", record.contact_phone_heard == said, repr(record.contact_phone_heard))
    check("the transcript is what was said", conversation.transcript.to_list()[-1]["text"] == said, repr(conversation.transcript.to_list()[-1]))
    guidance = conversation.guidance()
    check("the model is handed the value", "030011234567" in guidance, guidance[-300:])
    check("…and the read-back in words", "zero three zero zero, one one two" in guidance, guidance[-300:])
    check("the block is for that reply only", "030011234567" not in conversation.guidance())

    await conversation.note_user_turn("We have twenty employees and three offices.")
    check("a quantity changes nothing", record.contact_phone == "030011234567" and "THEY GAVE" not in conversation.guidance())

    conversation.note_agent_turn("And the best email for the invite?")
    await conversation.note_user_turn("It's john dot smith two at gmail dot com.")
    check("the record holds the address", record.contact_email == "john.smith2@gmail.com", repr(record.contact_email))
    check("…and the words", record.contact_email_heard == "It's john dot smith two at gmail dot com.")
    check("the model is told which address to book with", "john.smith2@gmail.com" in conversation.guidance())
    check("the transcript keeps the words, not the value", conversation.transcript.to_list()[-1]["text"] == "It's john dot smith two at gmail dot com.")

    # The read-back is owed once, to the first response that says anything.
    check("a tool call on its own does not use it up", conversation.complete_reply("") == "" and conversation.complete_reply(" . ") == " . ")
    reply = conversation.complete_reply("Happy to send that over. What are you looking at?")
    check("the reply that left it out gets it", reply.startswith("So that's john dot smith two at gmail dot com. Happy"), reply)
    check("…once", conversation.complete_reply("Anything else?") == "Anything else?")
    check("nothing internal is in the words added", "[" not in reply and "THEY GAVE" not in reply and "@" not in reply, reply)

    lead = make_conversation()
    await lead.note_user_turn("absolutely john at gmail dot com")
    check("a lead-in word is not part of the address", lead.record.contact_email == "john@gmail.com", repr(lead.record.contact_email))
    check("…and the transcript still has it", lead.transcript.to_list()[-1]["text"] == "absolutely john at gmail dot com")
    said_back = "Thanks, that's john at gmail dot com. What's the best day for you?"
    check("a reply that already confirms is spoken as written", lead.complete_reply(said_back) == said_back)

    leaving = make_conversation()
    await leaving.note_user_turn("Stop calling me on zero three zero zero, five five seven, one two three four. Take me off your list.")
    check("an apology is not prefixed with a read-back", leaving.complete_reply("I'm sorry, I'll take you off the list.") == "I'm sorry, I'll take you off the list.")

    next_turn = make_conversation()
    await next_turn.note_user_turn("My email is john at gmail dot com.")
    await next_turn.note_user_turn("Actually, what do you do?")
    check("a read-back never said is not carried into a later turn", next_turn.complete_reply("We build software.") == "We build software.")

    split = make_conversation()
    split.note_agent_turn("What number should we call?")
    await split.note_user_turn("zero three zero zero")
    check("a first group says go on, not read back", "STARTED GIVING A PHONE NUMBER" in split.guidance())
    await split.note_user_turn("double five seven, one two three four")
    check("two turns, one number", split.record.contact_phone == "03005571234", repr(split.record.contact_phone))
    check("both halves' words are kept", "zero three zero zero" in (split.record.contact_phone_heard or "") and "double five" in (split.record.contact_phone_heard or ""))

    await conversation.note_user_turn("And copy my colleague, her email address is mary underscore jane dash sales at hashmakers dot net.")
    check("a second address becomes the latest", record.contact_email == "mary_jane-sales@hashmakers.net", repr(record.contact_email))

    outcome = await conversation.finish()
    notes = outcome["qualification"]["notes"]
    check("…and the first one is not lost", any("mary_jane-sales@hashmakers.net" in n for n in notes) and any("john.smith2@gmail.com" in n for n in notes), repr(notes))
    # The row the campaign layer builds from that outcome — what the CRM sync reads.
    import json

    from test_results import result_for, valid

    result = result_for(json.loads(json.dumps(outcome)))
    check("the stored result validates and carries the values", valid(result) and any("030011234567" in n for n in result.notes), repr(result.notes))
    check("…with the caller's words still in its transcript", any("zero three zero zero double one" in str(t) for t in result.to_dict()["transcript"]))

    split_notes = (await split.finish())["qualification"]["notes"]
    check("a number finished over two turns is noted once, whole", [n for n in split_notes if "phone" in n] == ["phone number given on the call: 03005571234"], repr(split_notes))
    check("the values reach the notes the CRM reads", any("030011234567" in n for n in notes) and any("john.smith2@gmail.com" in n for n in notes), repr(notes))
    check("the outcome carries value and words", outcome["qualification"]["contact_phone"] == "030011234567" and outcome["qualification"]["contact_phone_heard"] == said)


def check_cost() -> None:
    print("\n=== the cost ===")
    turns = (
        "Sure, it's zero three zero zero double one two three four five six seven.",
        "My email is john dot smith two at gmail dot com.",
        "We have about two hundred fifty staff and three offices, and honestly the main problem is follow up.",
    )
    start = time.perf_counter()
    rounds = 500
    for _ in range(rounds):
        for turn in turns:
            find_phone(turn, context="what number")
            find_email(turn, context="what email")
    per_turn_ms = (time.perf_counter() - start) / (rounds * len(turns)) * 1000
    check("under a millisecond a turn", per_turn_ms < 1.0, f"{per_turn_ms * 1000:.0f} µs")


async def main() -> int:
    print("Spoken-value checks — dictated numbers and addresses are stored as values; the words stay in the transcript.")
    check_digits()
    check_phone_context()
    check_email()
    check_read_back()
    check_guaranteed_read_back()
    await check_conversation()
    check_cost()
    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
