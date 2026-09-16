#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Checks for the knowledge base layer that need no PostgreSQL and no API keys.

Run it from the `server/` directory::

    uv run python tests/test_knowledge.py

**Why this exists alongside the eval suite.** The evals in `evals/` drive the
real bot with real speech and real vendors, which is the only way to know the
agent works — and it takes minutes per run, needs three API keys, needs the
database loaded, and its verdicts come from an LLM judge. Everything below runs
in a few seconds against a stub store, is deterministic, and covers the parts
that are pure logic: what gets searched for, what the LLM is handed, and what
happens when things are missing or broken.

The division is deliberate. If a knowledge eval fails, run this first: if these
pass, retrieval is doing its job and the problem is the model, the prompt or the
audio path. If one of these fails, the eval was never going to pass.

It is a plain script rather than a pytest suite because the project has no test
dependency and this needs none. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

# Set before importing config: these checks never reach a vendor, but `Config`
# validates that the keys exist before it will build. Any real values already in
# the environment are left alone.
for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"  # No database is opened here.

from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402

from src.config import Config  # noqa: E402
from src.conversation import (  # noqa: E402
    INSTRUCTION_PREFIX,
    CallBrief,
    ConversationState,
    ProspectBrief,
    QualificationRecord,
    opening_instruction,
    stage_block,
)
from src.documents import DocumentError, chunk, extract  # noqa: E402
from src.embeddings import make_embedder  # noqa: E402
from src.knowledge_store import Match  # noqa: E402
from src.prompts import GREETING_INSTRUCTION, IDLE_INSTRUCTIONS  # noqa: E402
from src.retrieval import (  # noqa: E402
    KnowledgeRetriever,
    _query_from,
    looks_like_information_request,
)

KB_DIR = SERVER / "evals" / "kb"
FIXTURES = ("meridian_handbook.txt", "meridian_pricing.pdf")

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print its result."""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)


class StubStore:
    """A `KnowledgeStore` stand-in that scores in memory.

    Same interface as the real one for the two methods the retriever uses, so
    the retriever under test is the real retriever — only PostgreSQL is
    replaced. Scores are cosine similarity over the same embedder the bot uses,
    so the numbers here are the numbers pgvector would return.
    """

    def __init__(self, rows: list[tuple[str, str, int, str]], vectors: list[list[float]]) -> None:
        self._rows = rows
        self._vectors = vectors

    async def counts(self) -> tuple[int, int]:
        return len({row[0] for row in self._rows}), len(self._rows)

    async def search(self, vector, *, limit: int, min_score: float) -> list[Match]:
        scored = sorted(
            ((_cosine(vector, stored), row) for stored, row in zip(self._vectors, self._rows)),
            key=lambda pair: -pair[0],
        )
        return [
            Match(content=row[3], source=row[0], title=row[1], ordinal=row[2], score=score)
            for score, row in scored[:limit]
            if score >= min_score
        ]


class EmptyStore(StubStore):
    """A knowledge base with nothing in it."""

    async def counts(self) -> tuple[int, int]:
        return 0, 0


class BrokenStore(StubStore):
    """A knowledge base that fails mid-call, as a restarted database would."""

    async def search(self, *args, **kwargs):
        raise RuntimeError("connection reset by peer")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / ((sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5))


def build_store(config: Config, embedder, store_class=StubStore):
    """Chunk and embed the fixture documents into an in-memory store."""
    rows: list[tuple[str, str, int, str]] = []
    for name in FIXTURES:
        document = extract(KB_DIR / name)
        for piece in chunk(
            document.text,
            target_words=config.kb_chunk_words,
            overlap_words=config.kb_chunk_overlap_words,
        ):
            rows.append((document.source, document.title, piece.ordinal, piece.content))
    vectors = embedder.embed_documents([row[3] for row in rows])
    return store_class(rows, vectors), rows


async def main() -> int:
    """Run every check and report."""
    config = Config.from_env()
    embedder = make_embedder(config.embedding_model)
    store, rows = build_store(config, embedder)
    print(f"Fixture: {len(rows)} chunks from {len(FIXTURES)} documents\n")

    # Phase 3's unconditional retrieval, which is what most of these checks are
    # about: what comes back and what the LLM is handed. Phase 6's gate is a
    # separate question with its own section below.
    always = replace(config, kb_retrieval_mode="always")

    async def augment(messages, target=store, using=always):
        """Run the real retriever over a context and return (original, result)."""
        retriever = KnowledgeRetriever(target, embedder, using)
        context = LLMContext(messages=list(messages))
        return context, await retriever._augment(context)

    print("=== document extraction ===")
    text_doc = extract(KB_DIR / "meridian_handbook.txt")
    pdf_doc = extract(KB_DIR / "meridian_pricing.pdf")
    check("text file extracts", "412 customers" in text_doc.text)
    check("PDF text layer extracts", "10 percent" in pdf_doc.text, f"{len(pdf_doc.text)} chars")
    check("PDF pages are both read", "PAYMENT TERMS" in pdf_doc.text)
    check(
        "hash is stable across reads",
        extract(KB_DIR / FIXTURES[0]).content_hash == text_doc.content_hash,
    )
    try:
        extract(KB_DIR / "make_pricing_pdf.py")
        check("unsupported extension is rejected", False)
    except DocumentError as exc:
        check("unsupported extension is rejected", "unsupported" in str(exc).lower())

    print("\n=== chunking ===")
    pieces = chunk(text_doc.text, target_words=60, overlap_words=15)
    check("produces chunks", len(pieces) > 5, f"{len(pieces)} chunks")
    check("respects the word budget", max(p.word_count for p in pieces) <= 60 + 15)
    check("ordinals are contiguous", [p.ordinal for p in pieces] == list(range(len(pieces))))
    check("consecutive chunks overlap", pieces[1].content.split()[0] in pieces[0].content)
    check("empty text yields nothing", chunk("") == [])
    for bad, why in (((0, 0), "target must be positive"), ((10, 10), "overlap must be smaller")):
        try:
            chunk("word " * 50, target_words=bad[0], overlap_words=bad[1])
            check(f"rejects {bad}: {why}", False)
        except ValueError:
            check(f"rejects {bad}: {why}", True)

    print("\n=== what gets searched for ===")
    check(
        "the agent's own greeting instruction is not searched",
        _query_from([{"role": "user", "content": GREETING_INSTRUCTION}], 6) is None,
    )
    check(
        "the agent's own idle nudge is not searched",
        _query_from([{"role": "user", "content": IDLE_INSTRUCTIONS[0]}], 6) is None,
    )
    follow_up = _query_from(
        [
            {"role": "user", "content": "Tell me about the Enterprise plan"},
            {"role": "assistant", "content": "It is forty four euros per vehicle."},
            {"role": "user", "content": "how much is it"},
        ],
        6,
    )
    check("a short follow-up borrows the previous turn", "Enterprise" in (follow_up or ""))
    standalone = "What are your support hours on the Standard plan?"
    check(
        "a full question stands on its own",
        _query_from(
            [
                {"role": "user", "content": "Tell me about the Enterprise plan"},
                {"role": "user", "content": standalone},
            ],
            6,
        )
        == standalone,
    )
    check("no user message at all is not searched", _query_from([], 6) is None)

    print("\n=== what the LLM is handed ===")
    original, augmented = await augment(
        [{"role": "user", "content": "What are your support hours?"}]
    )
    check("the conversation itself is untouched", len(original.messages) == 1)
    check("exactly one message is added", len(augmented.messages) == 2)
    block = augmented.messages[-1]["content"]
    check("added as a user-role message", augmented.messages[-1]["role"] == "user")
    check("the block comes after the question", "support hours" in augmented.messages[0]["content"])
    check("it carries the right passage", "7am to 7pm" in block)
    check("it carries the grounding instruction", "do not guess" in block)
    check("it does not leak scores or ordinals", "0.7" not in block and "#" not in block)

    print("\n=== degrading safely ===")
    empty_store, _ = build_store(config, embedder, EmptyStore)
    original, augmented = await augment(
        [{"role": "user", "content": "What are your support hours?"}], empty_store
    )
    check("an empty knowledge base leaves the context alone", augmented is original)

    broken_store, _ = build_store(config, embedder, BrokenStore)
    original, augmented = await augment(
        [{"role": "user", "content": "What are your support hours?"}], broken_store
    )
    check("a database failure does not raise", augmented is original)

    original, augmented = await augment([{"role": "user", "content": GREETING_INSTRUCTION}])
    check("a greeting turn does no retrieval", augmented is original)

    _, augmented = await augment([{"role": "user", "content": "Hello there"}])
    check(
        "chatter gets the 'nothing found' block, not passages",
        "nothing relevant found" in augmented.messages[-1]["content"],
    )

    print("\n=== when retrieval runs at all (KB_RETRIEVAL_MODE) ===")
    # Phase 6's gate. The property that matters is the *asymmetry*: anything
    # that could be a request for information goes through, and only turns that
    # could not be are skipped. A turn wrongly searched costs one query; a turn
    # wrongly skipped costs the grounding this whole layer exists for.
    for turn, expected in (
        ("How much does the Standard plan cost?", True),
        ("what are your support hours", True),  # No question mark: STT drops them.
        ("Do you integrate with Salesforce", True),
        ("Tell me about the contract terms", True),
        ("I'd want to know the pricing before anything else", True),
        ("yeah", False),
        ("Okay", False),
        ("no thanks", False),
        ("mhm", False),
        ("Good morning", False),
        ("we handle it all on spreadsheets at the moment", False),
        ("about forty vehicles, give or take", False),
    ):
        check(
            f"{'searches' if expected else 'skips  '} {turn[:52]!r}",
            looks_like_information_request(turn) is expected,
        )

    gated = replace(config, kb_retrieval_mode="auto")
    original, augmented = await augment([{"role": "user", "content": "yeah, exactly"}], using=gated)
    check("a skipped turn gets no block at all", augmented is original)
    _, augmented = await augment(
        [{"role": "user", "content": "What are your support hours?"}], using=gated
    )
    check("a real question still retrieves", "7am to 7pm" in augmented.messages[-1]["content"])

    print("\n=== the agent's own guidance is never searched ===")
    # Phase 6 composes its instructions per call, so they cannot be compared
    # against a fixed set the way Phase 2's constants are. The prefix is what
    # makes them recognisable, and this is the check that keeps the two layers
    # agreeing about it.
    opening = opening_instruction(CallBrief(prospect=ProspectBrief(first_name="Sarah")))
    check("the composed opening carries the marker", opening.startswith(INSTRUCTION_PREFIX))
    check(
        "the composed opening is not searched",
        _query_from([{"role": "user", "content": opening}], 6) is None,
    )
    guidance = stage_block(ConversationState.DISCOVERY, QualificationRecord())
    check(
        "a stage block is not searched",
        _query_from([{"role": "user", "content": guidance}], 6) is None,
    )

    print("\n=== questions the documents answer ===")
    for question, needle in (
        ("How much does the Standard plan cost per vehicle?", "29 euros"),
        ("What discount would we get for four hundred vehicles?", "10 percent"),
        ("Can we pay by credit card?", "credit card"),
        ("What are your support hours?", "7am to 7pm"),
        ("Do you integrate with Salesforce?", "Salesforce"),
        ("Are you ISO 27001 certified?", "ISO 27001"),
        ("How long is the contract?", "12 months"),
        ("Do you work with NetSuite?", "NetSuite"),
        ("Where is my data stored?", "Frankfurt"),
        ("Is there an API rate limit?", "600 requests"),
    ):
        _, augmented = await augment([{"role": "user", "content": question}])
        check(f"{question[:48]:<48} -> {needle!r}", needle in augmented.messages[-1]["content"])

    print("\n=== questions the documents do NOT answer ===")
    # These retrieve confident, on-topic, wrong material — a passage about
    # discounts genuinely is the nearest thing to a question about discounts.
    # No threshold separates them from real questions, so the retriever's job is
    # only to hand over the passages and the instruction; the *model* decides it
    # cannot answer. What is checked here is that the instruction always travels
    # with them, which is the part that makes the refusal possible at all.
    for question in (
        "Do you offer a discount for registered non-profits?",
        "Could we be invoiced in Japanese yen?",
        "Do you have an office in Brazil?",
    ):
        _, augmented = await augment([{"role": "user", "content": question}])
        block = augmented.messages[-1]["content"]
        check(
            f"{question[:48]:<48} -> grounded",
            "do not guess" in block or "nothing relevant found" in block,
        )

    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
