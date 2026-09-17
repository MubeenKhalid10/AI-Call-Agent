"""An in-memory copy of the knowledge base, so a turn never waits on the database. Phase 37.

**The failure this fixes, measured on 2026-09-17.** The knowledge base moved to
the hosted PostgreSQL (Supabase, in another region) so that documents added
from the deployed application reach the bot. From then on every caller turn
paid at least two round trips across the internet inside the pipeline — a
`counts()` and the pgvector search — and when the hosted session pooler was at
its client cap, a connect attempt that waited up to ten seconds. Measured on a
browser session: retrieval took 1.6 s, 3.6 s and 6.6 s; one turn got no answer
for twelve seconds because the retrieval stage was still inside a query when
the caller interrupted, Pipecat could not cancel it, and the pipeline's
heartbeat stalled. To the caller that is an agent that stopped talking.

The knowledge base a sales agent needs is small — one document here, 122
passages; hundreds to low thousands in general. At that size the whole thing
fits in memory many times over, and a cosine search over a NumPy matrix is
well under a millisecond. So the bot loads every passage and its embedding
once at startup, answers every turn from that copy, and re-reads the database
in the background when something changed — a document added or removed from
the application — so the turn path never touches the network for knowledge.

Above `max_chunks` the index steps aside: `search` goes to the database as
before, and `retrieval.py`'s timeout is what keeps a slow database out of the
conversation. The cap is configurable (`KB_INDEX_MAX_CHUNKS`); the default is
generous for a sales knowledge base and small for a machine's memory.

The interface is the two methods `KnowledgeRetriever` uses, `counts()` and
`search()`, so the retriever does not know whether it holds the index or the
store.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from loguru import logger

from .knowledge_store import Match


@dataclass(frozen=True)
class _Passage:
    """One indexed chunk's metadata; its vector is a row of the matrix."""

    content: str
    source: str
    title: str
    ordinal: int


class KnowledgeIndex:
    """The knowledge base held in memory, refreshed from the store in the background.

    `store` is anything with the real store's `all_chunks()`, `fingerprint()`,
    `counts()` and `search()`; the checks pass a stub.
    """

    def __init__(
        self,
        store: Any,
        *,
        max_chunks: int = 5000,
        refresh_secs: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        """Create an index over `store`. Nothing is read until `load()`.

        Args:
            store: The connected knowledge store.
            max_chunks: Largest knowledge base held in memory; above it,
                searches go to the store. 0 means never hold it in memory.
            refresh_secs: How often the background refresher compares the
                store's fingerprint with the loaded copy. 0 disables it.
            clock: Monotonic seconds; the checks inject one.
            sleep: How the refresher waits; the checks inject one.
        """
        self._store = store
        self._max_chunks = max_chunks
        self._refresh_secs = refresh_secs
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._matrix: np.ndarray | None = None
        self._passages: list[_Passage] = []
        self._counts: tuple[int, int] = (0, 0)
        self._fingerprint: tuple[Any, ...] | None = None
        self._loaded_at: float | None = None
        self._delegating = False
        self._task: asyncio.Task[Any] | None = None
        self._lock = asyncio.Lock()
        self.loads = 0
        self.refresh_failures = 0

    # --- what the retriever calls ------------------------------------------------

    @property
    def in_memory(self) -> bool:
        """Whether searches are answered from memory right now."""
        return self._matrix is not None and not self._delegating

    async def counts(self) -> tuple[int, int]:
        """`(documents, chunks)` as of the last load; the store's own when delegating."""
        if self._delegating or self._loaded_at is None:
            return await self._store.counts()
        return self._counts

    async def search(self, vector: list[float], *, limit: int = 4, min_score: float = 0.0) -> list[Match]:
        """The passages closest to `vector`, best first, from memory when it is held there."""
        if self._delegating or self._matrix is None:
            return await self._store.search(vector, limit=limit, min_score=min_score)
        query = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if norm == 0.0 or self._matrix.shape[0] == 0:
            return []
        scores = self._matrix @ (query / norm)
        limit = max(1, min(limit, scores.shape[0]))
        top = np.argpartition(-scores, limit - 1)[:limit] if limit < scores.shape[0] else np.arange(scores.shape[0])
        ordered = top[np.argsort(-scores[top])]
        matches: list[Match] = []
        for index in ordered:
            score = float(scores[index])
            if score < min_score:
                break
            passage = self._passages[int(index)]
            matches.append(
                Match(
                    content=passage.content,
                    source=passage.source,
                    title=passage.title,
                    ordinal=passage.ordinal,
                    score=score,
                )
            )
        return matches

    # --- loading and refreshing --------------------------------------------------

    async def load(self) -> None:
        """Read the whole knowledge base into memory. Raises when the store cannot be read."""
        async with self._lock:
            fingerprint = tuple(await self._store.fingerprint())
            documents, chunks = await self._store.counts()
            if self._max_chunks <= 0 or chunks > self._max_chunks:
                if not self._delegating:
                    logger.warning(
                        f"KB | {chunks} chunk(s) is above KB_INDEX_MAX_CHUNKS={self._max_chunks}; "
                        "searching the database on every turn instead of memory"
                    )
                self._delegating = True
                self._matrix = None
                self._passages = []
            else:
                rows = await self._store.all_chunks()
                passages = [_Passage(content=r[0], source=r[1], title=r[2], ordinal=int(r[3])) for r in rows]
                vectors = [r[4] for r in rows]
                if vectors:
                    matrix = np.asarray(vectors, dtype=np.float32)
                    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                    norms[norms == 0.0] = 1.0
                    matrix = matrix / norms
                else:
                    matrix = np.zeros((0, 1), dtype=np.float32)
                self._matrix = matrix
                self._passages = passages
                self._delegating = False
                documents, chunks = len({p.source for p in passages}), len(passages)
            self._counts = (documents, chunks)
            self._fingerprint = fingerprint
            self._loaded_at = self._clock()
            self.loads += 1

    async def refresh_if_changed(self) -> bool:
        """Re-read the store when its fingerprint moved. Returns whether it reloaded.

        A store that cannot be reached keeps the copy already in memory: a
        stale knowledge base is better than a turn that waits on the network.
        """
        try:
            fingerprint = tuple(await self._store.fingerprint())
        except Exception as exc:  # noqa: BLE001 - logged, the snapshot stands
            self.refresh_failures += 1
            logger.warning(f"KB | could not check the knowledge base for changes: {exc}")
            return False
        if fingerprint == self._fingerprint:
            return False
        try:
            await self.load()
        except Exception as exc:  # noqa: BLE001
            self.refresh_failures += 1
            logger.warning(f"KB | the knowledge base changed but could not be re-read: {exc}")
            return False
        documents, chunks = self._counts
        logger.info(f"KB | reloaded: {documents} document(s), {chunks} chunk(s)")
        return True

    def start(self) -> None:
        """Begin refreshing in the background. Idempotent; a no-op when refreshing is off."""
        if self._refresh_secs <= 0 or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="knowledge-refresher")

    async def stop(self) -> None:
        """Stop the background refresher. Safe to call more than once."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
            pass

    async def _run(self) -> None:
        while True:
            await self._sleep(self._refresh_secs)
            await self.refresh_if_changed()

    def describe(self) -> str:
        """One line for the startup log."""
        documents, chunks = self._counts
        where = "database per turn" if self._delegating else "memory"
        refresh = f"refresh every {self._refresh_secs:g}s" if self._refresh_secs > 0 else "no refresh"
        return f"{documents} document(s), {chunks} chunk(s) searched from {where}, {refresh}"
