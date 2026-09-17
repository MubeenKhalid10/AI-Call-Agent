"""Checks for the in-memory knowledge index and the retrieval timeout (Phase 37).

Run it from the `server/` directory::

    uv run python tests/test_knowledge_index.py

No PostgreSQL, no API keys, no embedding model: the store is a stub that
returns hand-written vectors, and the embedder is a stub that maps a query to
one of them. What is under test is pure logic — that the index ranks the way
pgvector's cosine search ranks, honours the score floor and the limit, knows
its own counts without asking the store, reloads only when the store's
fingerprint moves and keeps its copy when the store is unreachable, steps
aside above the size cap, and that a retrieval which outlasts the timeout is
abandoned rather than holding the turn.

The reason these exist is a browser session on 2026-09-17 in which a turn got
no answer for twelve seconds because the retrieval stage was inside a query to
a database in another region. Exit status is 0 when everything passes.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

for _name in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "CARTESIA_API_KEY"):
    os.environ.setdefault(_name, "not-used-by-these-checks")
os.environ["KB_ENABLED"] = "false"

from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402

from src.config import Config  # noqa: E402
from src.knowledge_index import KnowledgeIndex  # noqa: E402
from src.knowledge_store import Match  # noqa: E402
from src.retrieval import KnowledgeRetriever  # noqa: E402

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one check."""
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(label)


# Three passages in a 3-dimensional space, so the arithmetic can be done by eye.
ROWS = [
    ("Pricing starts at ten dollars a seat.", "pricing.pdf", "Pricing", 0, [1.0, 0.0, 0.0]),
    ("Support answers within one business day.", "handbook.txt", "Handbook", 0, [0.0, 1.0, 0.0]),
    ("We are closed on public holidays.", "handbook.txt", "Handbook", 1, [0.6, 0.8, 0.0]),
]


class StubStore:
    """The four methods the index uses, over `ROWS`, with counters and a fingerprint."""

    def __init__(self, rows=ROWS) -> None:
        self.rows = list(rows)
        self.version = 1
        self.calls: dict[str, int] = {"all_chunks": 0, "fingerprint": 0, "counts": 0, "search": 0}
        self.fail = False
        self.search_delay = 0.0

    async def all_chunks(self):
        self.calls["all_chunks"] += 1
        if self.fail:
            raise RuntimeError("connection reset by peer")
        return list(self.rows)

    async def fingerprint(self):
        self.calls["fingerprint"] += 1
        if self.fail:
            raise RuntimeError("connection reset by peer")
        return (len({r[1] for r in self.rows}), len(self.rows), f"v{self.version}")

    async def counts(self):
        self.calls["counts"] += 1
        return len({r[1] for r in self.rows}), len(self.rows)

    async def search(self, vector, *, limit, min_score):
        self.calls["search"] += 1
        if self.search_delay:
            await asyncio.sleep(self.search_delay)
        scored = sorted(((_cosine(vector, r[4]), r) for r in self.rows), key=lambda p: -p[0])
        return [
            Match(content=r[0], source=r[1], title=r[2], ordinal=r[3], score=s)
            for s, r in scored[:limit]
            if s >= min_score
        ]


class StubEmbedder:
    """Maps a query to a fixed vector; the retriever only calls `embed_query_async`."""

    dimensions = 3
    model_name = "stub"

    def __init__(self, vector) -> None:
        self.vector = vector

    async def embed_query_async(self, text: str):
        return list(self.vector)


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / ((sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5))


async def check_index() -> None:
    print("The index")
    store = StubStore()
    index = KnowledgeIndex(store, max_chunks=100, refresh_secs=0)
    check("nothing is read before load()", store.calls["all_chunks"] == 0)
    check("searches go to the store before load()", not index.in_memory)
    await index.load()
    check("load() reads every chunk once", store.calls["all_chunks"] == 1)
    check("in memory after load()", index.in_memory)
    check("counts() come from memory", await index.counts() == (2, 3) and store.calls["counts"] == 1)

    searches_before = store.calls["search"]
    matches = await index.search([1.0, 0.0, 0.0], limit=4, min_score=0.0)
    check("search() does not touch the store", store.calls["search"] == searches_before)
    check("best match first", [m.source for m in matches][:1] == ["pricing.pdf"], str(matches))
    check(
        "scores are cosine similarity",
        len(matches) >= 2 and abs(matches[0].score - 1.0) < 1e-6 and abs(matches[1].score - 0.6) < 1e-6,
        str([round(m.score, 3) for m in matches]),
    )
    check("orthogonal passage scores zero and is kept above a zero floor", len(matches) == 3)
    matches = await index.search([1.0, 0.0, 0.0], limit=4, min_score=0.5)
    check("min_score drops the rest", [m.score >= 0.5 for m in matches] == [True, True], str(matches))
    matches = await index.search([1.0, 0.0, 0.0], limit=1, min_score=0.0)
    check("limit caps the result", len(matches) == 1 and matches[0].source == "pricing.pdf")
    matches = await index.search([0.0, 0.0, 0.0], limit=4, min_score=0.0)
    check("a zero query vector matches nothing rather than dividing by zero", matches == [])
    match = (await index.search([0.6, 0.8, 0.0], limit=1, min_score=0.0))[0]
    check(
        "the Match carries the passage's metadata",
        (match.content, match.source, match.title, match.ordinal) == ROWS[2][:4],
        str(match),
    )

    print("Refreshing")
    reloaded = await index.refresh_if_changed()
    check("an unchanged fingerprint does not reload", reloaded is False and store.calls["all_chunks"] == 1)
    store.rows.append(("New passage about invoices.", "invoices.pdf", "Invoices", 0, [0.0, 0.0, 1.0]))
    store.version += 1
    reloaded = await index.refresh_if_changed()
    check("a changed fingerprint reloads", reloaded is True and store.calls["all_chunks"] == 2)
    check("the new passage is searchable", (await index.search([0.0, 0.0, 1.0], limit=1, min_score=0.9))[0].source == "invoices.pdf")
    check("counts() follow the reload", await index.counts() == (3, 4))
    store.fail = True
    store.version += 1
    reloaded = await index.refresh_if_changed()
    check("an unreachable store keeps the copy in memory", reloaded is False and index.in_memory)
    check("…and is counted, not raised", index.refresh_failures == 1)
    check("…and still answers", len(await index.search([1.0, 0.0, 0.0], limit=1, min_score=0.9)) == 1)
    store.fail = False

    print("The background refresher")
    waits: list[float] = []
    gate = asyncio.Event()

    async def fake_sleep(secs: float) -> None:
        waits.append(secs)
        await gate.wait()
        gate.clear()

    index = KnowledgeIndex(store, max_chunks=100, refresh_secs=7.0, sleep=fake_sleep)
    await index.load()
    index.start()
    index.start()
    await asyncio.sleep(0)
    check("start() is idempotent and waits the configured interval", waits == [7.0], str(waits))
    store.rows.pop()
    store.version += 1
    loads_before = index.loads
    gate.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if index.loads > loads_before:
            break
    check("the refresher reloads after a change", index.loads == loads_before + 1)
    await index.stop()
    await index.stop()
    check("stop() is safe twice", True)
    off = KnowledgeIndex(store, max_chunks=100, refresh_secs=0)
    off.start()
    check("refresh_secs=0 starts no refresher", off._task is None)

    print("The size cap")
    store = StubStore()
    index = KnowledgeIndex(store, max_chunks=2, refresh_secs=0)
    await index.load()
    check("above the cap the copy is not held", not index.in_memory and store.calls["all_chunks"] == 0)
    await index.search([1.0, 0.0, 0.0], limit=1, min_score=0.0)
    check("…and search() goes to the store", store.calls["search"] == 1)
    check("…and counts() go to the store", await index.counts() == (2, 3) and store.calls["counts"] >= 2)
    check("describe() says where searches go", "database" in index.describe(), index.describe())
    index = KnowledgeIndex(store, max_chunks=0, refresh_secs=0)
    await index.load()
    check("max_chunks=0 never holds the copy", not index.in_memory)


async def check_retriever_timeout() -> None:
    print("The retrieval timeout")
    os.environ["KB_TIMEOUT_SECS"] = "0.3"
    os.environ["KB_RETRIEVAL_MODE"] = "always"
    config = Config.from_env()
    check("KB_TIMEOUT_SECS is read", abs(config.kb_timeout_secs - 0.3) < 1e-9, str(config.kb_timeout_secs))
    check("KB_REFRESH_SECS and KB_INDEX_MAX_CHUNKS have their defaults", config.kb_refresh_secs == 60.0 and config.kb_index_max_chunks == 5000)

    store = StubStore()
    retriever = KnowledgeRetriever(store, StubEmbedder([1.0, 0.0, 0.0]), config)
    context = LLMContext(messages=[{"role": "user", "content": "How much does it cost per seat?"}])
    augmented = await retriever._augment(context)
    check("a fast store augments the context", augmented is not context and "ten dollars" in str(augmented.messages))

    store.search_delay = 2.0
    started = time.monotonic()
    augmented = await retriever._augment(context)
    elapsed = time.monotonic() - started
    check("a slow store is abandoned at the timeout", augmented is context, f"{elapsed:.2f}s")
    check("…without waiting for it", elapsed < 1.0, f"{elapsed:.2f}s")
    await asyncio.sleep(0)

    os.environ["KB_TIMEOUT_SECS"] = "0"
    config = Config.from_env()
    store = StubStore()
    store.search_delay = 0.2
    retriever = KnowledgeRetriever(store, StubEmbedder([1.0, 0.0, 0.0]), config)
    augmented = await retriever._augment(context)
    check("KB_TIMEOUT_SECS=0 waits", augmented is not context)

    index = KnowledgeIndex(StubStore(), max_chunks=100, refresh_secs=0)
    await index.load()
    os.environ["KB_TIMEOUT_SECS"] = "2"
    retriever = KnowledgeRetriever(index, StubEmbedder([0.0, 1.0, 0.0]), Config.from_env())
    augmented = await retriever._augment(context)
    check("the retriever works over the index", augmented is not context and "business day" in str(augmented.messages))


async def main() -> int:
    """Run every check and report."""
    await check_index()
    await check_retriever_timeout()
    total = sum(1 for _ in _failures)
    if _failures:
        print(f"\n{total} check(s) failed:")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
