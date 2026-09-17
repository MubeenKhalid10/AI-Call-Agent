"""The knowledge base itself: PostgreSQL with the pgvector extension.

**Why Postgres and not a vector database.** The vectors are not the whole
problem. A knowledge base also has documents, their provenance, when they were
ingested, which model embedded them, and which chunks belong to which file — all
of it relational, all of it needing to stay consistent when a document is
replaced or removed. A dedicated vector store handles the similarity search and
leaves the rest to a second system that then has to be kept in step. Postgres
does both, `ON DELETE CASCADE` makes "remove this document" a single statement,
and at the scale a sales agent's knowledge base actually reaches — hundreds to
low thousands of passages — pgvector's search is far below the latency the
conversation can notice.

**Distance.** Cosine (`<=>`), because the embedding model produces normalised
vectors and cosine is what it was trained against. The operator returns a
*distance* in [0, 2] where 0 is identical; every score this module returns is
`1 - distance`, so callers reason in similarity, where higher is better and the
threshold in `.env` reads the way you would expect.

**The index.** HNSW, built on the embedding column. It is approximate, which for
this job is the correct trade: an exact scan of a few thousand rows is fast too,
but the index keeps that true as the knowledge base grows, and a knowledge base
that grows is the whole point of one. Note the index is dimension-locked, like
the column.

**The dimension is the trap.** `vector(n)` fixes n at table-creation time, and n
comes from the embedding model. Point `EMBEDDING_MODEL` at something with a
different width and every insert fails; point it at something with the *same*
width and nothing fails at all — the vectors are simply from a different space
and the matches are quietly meaningless. That second case is why the model name
is stored per document and checked on every connection, rather than trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import asyncpg
from .pooling import pool_options

# Table names are interpolated nowhere; they are constants. Every value that
# comes from outside this module travels as a bound parameter.
DOCUMENTS_TABLE = "kb_documents"
CHUNKS_TABLE = "kb_chunks"


class KnowledgeStoreError(RuntimeError):
    """The knowledge base is unreachable or not set up. Message is for the user."""


@dataclass(frozen=True)
class Match:
    """One retrieved passage and how well it matched."""

    content: str
    """The passage text, as it will be shown to the LLM."""

    source: str
    """Document the passage came from, e.g. `pricing.pdf`."""

    title: str
    """Human-readable document title."""

    ordinal: int
    """Position of the passage within its document."""

    score: float
    """Cosine similarity in [-1, 1]. Higher is closer. Typically 0.5-0.9 here."""


@dataclass(frozen=True)
class StoredDocument:
    """A document in the knowledge base, as reported by `list_documents`."""

    source: str
    title: str
    chunk_count: int
    byte_size: int
    embed_model: str
    content_hash: str
    ingested_at: str


class KnowledgeStore:
    """Async access to the pgvector-backed knowledge base.

    One instance owns one connection pool. Build it with `connect`, which
    verifies the extension and the schema before returning, so a misconfigured
    database fails at startup with a message naming the problem rather than
    mid-call as an opaque SQL error.
    """

    def __init__(
        self, pool: asyncpg.Pool, dimensions: int, embed_model: str, *, owns_pool: bool = True
    ) -> None:
        """Prefer `KnowledgeStore.connect`; this takes an already-built pool.

        Args:
            pool: The connection pool to use.
            dimensions: Width of the embedding model's vectors.
            embed_model: Model identifier, checked against stored documents.
            owns_pool: Whether `close()` should close it. False when the pool is
                shared — see `connect(pool=...)`.
        """
        self._pool = pool
        self._dimensions = dimensions
        self._embed_model = embed_model
        self._owns_pool = owns_pool

    @property
    def pool(self) -> asyncpg.Pool:
        """The underlying pool, so another store on the same database can share it.

        Phase 11. `bot.py` hands this to `CampaignStore.connect(pool=...)` when
        both point at the same database, which takes a call from six
        connections to three — and the connection budget is what caps how many
        calls can run at once.
        """
        return self._pool

    @classmethod
    async def connect(
        cls,
        dsn: str,
        *,
        dimensions: int,
        embed_model: str,
        create_schema: bool = False,
        min_size: int = 1,
        max_size: int = 4,
        timeout: float = 10.0,
        pool: asyncpg.Pool | None = None,
    ) -> KnowledgeStore:
        """Open a pool and verify the knowledge base is usable.

        Args:
            dsn: `postgresql://user:password@host:port/database`.
            dimensions: Width of the embedding model's vectors.
            embed_model: Model identifier, checked against stored documents.
            create_schema: Create the extension, tables and index if missing.
                Ingest passes True; the bot passes False, so a bot process never
                silently creates an empty knowledge base it should have found.
            min_size: Connections held open.
            max_size: Connection ceiling.
            timeout: Seconds to wait for the initial connection.

        Returns:
            A connected store.

        Raises:
            KnowledgeStoreError: The database is unreachable, pgvector is not
                installed, the schema is missing, or the stored embeddings were
                made with a different model.
        """
        owns_pool = pool is None
        if pool is None:
            try:
                # A transaction pooler (Supabase's port 6543) needs anonymous
                # prepared statements; see src/pooling.py.
                connect_dsn, extra = pool_options(dsn)
                pool = await asyncpg.create_pool(
                    connect_dsn,
                    min_size=min_size,
                    max_size=max_size,
                    timeout=timeout,
                    command_timeout=timeout,
                    **extra,
                )
            except (OSError, asyncpg.PostgresError) as exc:
                raise KnowledgeStoreError(
                    f"Could not connect to the knowledge base at {_redact(dsn)}.\n"
                    f"  Check that PostgreSQL is running and that KB_DATABASE_URL is right.\n"
                    f"  Underlying error: {exc}"
                ) from exc

            if pool is None:  # asyncpg types this as optional.
                raise KnowledgeStoreError(f"Could not connect to {_redact(dsn)}.")

        store = cls(pool, dimensions, embed_model, owns_pool=owns_pool)
        try:
            if create_schema:
                await store.create_schema()
            await store._verify()
        except Exception:
            if owns_pool:
                await pool.close()
            raise
        return store

    async def close(self) -> None:
        """Close the pool, if this store opened it. Safe to call more than once."""
        if self._owns_pool:
            await self._pool.close()

    async def create_schema(self) -> None:
        """Create the extension, tables and index if they are not there already.

        Idempotent. Creating the extension needs a superuser (or a role granted
        it) the first time; afterwards nothing here is privileged.
        """
        async with self._pool.acquire() as connection:
            try:
                await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except asyncpg.InsufficientPrivilegeError as exc:
                raise KnowledgeStoreError(
                    "pgvector is not enabled on this database and this role may not enable it.\n"
                    "  Run once as a superuser:  CREATE EXTENSION vector;\n"
                    f"  Underlying error: {exc}"
                ) from exc
            except asyncpg.UndefinedFileError as exc:
                raise KnowledgeStoreError(
                    "PostgreSQL cannot find the pgvector extension files.\n"
                    "  The server is running but `vector.dll` and `share/extension/vector*` are "
                    "not installed into this PostgreSQL. See README.md, 'Installing pgvector'.\n"
                    f"  Underlying error: {exc}"
                ) from exc

            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {DOCUMENTS_TABLE} (
                    id           bigserial PRIMARY KEY,
                    source       text        NOT NULL UNIQUE,
                    title        text        NOT NULL,
                    content_hash text        NOT NULL,
                    byte_size    bigint      NOT NULL,
                    chunk_count  integer     NOT NULL DEFAULT 0,
                    embed_model  text        NOT NULL,
                    ingested_at  timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {CHUNKS_TABLE} (
                    id          bigserial PRIMARY KEY,
                    document_id bigint  NOT NULL
                                REFERENCES {DOCUMENTS_TABLE}(id) ON DELETE CASCADE,
                    ordinal     integer NOT NULL,
                    content     text    NOT NULL,
                    embedding   vector({self._dimensions}) NOT NULL,
                    UNIQUE (document_id, ordinal)
                )
                """
            )
            # Cosine, to match how the embeddings are compared at query time. An
            # index built for a different operator class is simply not used, and
            # the only symptom is a search that gets slower as the store grows.
            await connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {CHUNKS_TABLE}_embedding_idx
                    ON {CHUNKS_TABLE} USING hnsw (embedding vector_cosine_ops)
                """
            )

    async def add_document(
        self,
        *,
        source: str,
        title: str,
        content_hash: str,
        byte_size: int,
        chunks: list[str],
        vectors: list[list[float]],
    ) -> int:
        """Store a document and its embedded chunks, replacing any earlier version.

        Runs in one transaction: a document is never half-replaced, so a failure
        part-way through leaves the previous version intact rather than a
        document with some of its old chunks and some of its new ones.

        Args:
            source: Unique document name.
            title: Human-readable title.
            content_hash: Hash of the extracted text.
            byte_size: Source file size.
            chunks: Passage texts, in reading order.
            vectors: One vector per chunk, in the same order.

        Returns:
            Number of chunks stored.

        Raises:
            ValueError: `chunks` and `vectors` are different lengths, or a
                vector is the wrong width for this store.
        """
        if len(chunks) != len(vectors):
            raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors.")
        for index, vector in enumerate(vectors):
            if len(vector) != self._dimensions:
                raise ValueError(
                    f"Chunk {index} embedded to {len(vector)} dimensions; this knowledge base "
                    f"stores {self._dimensions}. The embedding model changed — re-create the "
                    f"schema and re-ingest every document."
                )

        async with self._pool.acquire() as connection, connection.transaction():
            # Delete-then-insert rather than an upsert of individual chunks: a
            # re-ingested document may chunk into a different number of pieces,
            # so matching them up one to one is not meaningful. The cascade
            # takes the old chunks with the old row.
            await connection.execute(f"DELETE FROM {DOCUMENTS_TABLE} WHERE source = $1", source)
            document_id: int = await connection.fetchval(
                f"""
                INSERT INTO {DOCUMENTS_TABLE}
                    (source, title, content_hash, byte_size, chunk_count, embed_model)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                source,
                title,
                content_hash,
                byte_size,
                len(chunks),
                self._embed_model,
            )
            await connection.executemany(
                f"INSERT INTO {CHUNKS_TABLE} (document_id, ordinal, content, embedding) "
                f"VALUES ($1, $2, $3, $4::vector)",
                [
                    (document_id, ordinal, content, _to_vector_literal(vector))
                    for ordinal, (content, vector) in enumerate(zip(chunks, vectors, strict=True))
                ],
            )
        return len(chunks)

    async def search(
        self,
        vector: list[float],
        *,
        limit: int = 4,
        min_score: float = 0.0,
    ) -> list[Match]:
        """Find the passages closest to a query vector.

        Args:
            vector: The query embedding.
            limit: Most passages to return.
            min_score: Drop anything below this cosine similarity. This is what
                makes "the knowledge base has nothing on that" possible: without
                it the nearest passage is always returned, however unrelated,
                and the agent will dutifully answer from it.

        Returns:
            Matches, closest first. Possibly empty.
        """
        rows = await self._pool.fetch(
            f"""
            SELECT c.content,
                   d.source,
                   d.title,
                   c.ordinal,
                   1 - (c.embedding <=> $1::vector) AS score
            FROM {CHUNKS_TABLE} c
            JOIN {DOCUMENTS_TABLE} d ON d.id = c.document_id
            WHERE 1 - (c.embedding <=> $1::vector) >= $2
            ORDER BY c.embedding <=> $1::vector
            LIMIT $3
            """,
            _to_vector_literal(vector),
            min_score,
            limit,
        )
        return [
            Match(
                content=row["content"],
                source=row["source"],
                title=row["title"],
                ordinal=row["ordinal"],
                score=float(row["score"]),
            )
            for row in rows
        ]

    async def list_documents(self) -> list[StoredDocument]:
        """Every document in the knowledge base, newest first."""
        rows = await self._pool.fetch(
            f"""
            SELECT source, title, chunk_count, byte_size, embed_model, content_hash, ingested_at
            FROM {DOCUMENTS_TABLE}
            ORDER BY ingested_at DESC, source
            """
        )
        return [
            StoredDocument(
                source=row["source"],
                title=row["title"],
                chunk_count=row["chunk_count"],
                byte_size=row["byte_size"],
                embed_model=row["embed_model"],
                content_hash=row["content_hash"],
                ingested_at=row["ingested_at"].isoformat(timespec="seconds"),
            )
            for row in rows
        ]

    async def get_document_hash(self, source: str) -> str | None:
        """Hash of a stored document's text, or None if it is not stored.

        Lets ingest skip a file whose contents have not changed instead of
        re-embedding every chunk of it.
        """
        return await self._pool.fetchval(
            f"SELECT content_hash FROM {DOCUMENTS_TABLE} WHERE source = $1", source
        )

    async def delete_document(self, source: str) -> bool:
        """Remove a document and its chunks.

        Args:
            source: The document name.

        Returns:
            True if a document was removed.
        """
        result = await self._pool.execute(
            f"DELETE FROM {DOCUMENTS_TABLE} WHERE source = $1", source
        )
        return result.rsplit(" ", 1)[-1] != "0"

    async def all_chunks(self) -> list[tuple[str, str, str, int, list[float]]]:
        """Every passage with its vector, as `(content, source, title, ordinal, embedding)`. Phase 37.

        What `knowledge_index.KnowledgeIndex` loads. The vector column comes
        back as its text form (`[0.1,0.2,...]`), which needs no pgvector codec
        on the connection, and is parsed here.
        """
        # One bulk read at startup or in the background, never on a turn — so
        # it gets its own, longer timeout than the pool's per-command one: on
        # 2026-09-17 the hosted pooler took over 10 s to return 122 rows.
        rows = await self._pool.fetch(
            f"""
            SELECT c.content, d.source, d.title, c.ordinal, c.embedding::text AS embedding
            FROM {CHUNKS_TABLE} c
            JOIN {DOCUMENTS_TABLE} d ON d.id = c.document_id
            ORDER BY d.id, c.ordinal
            """,
            timeout=120.0,
        )
        return [
            (row["content"], row["source"], row["title"], int(row["ordinal"]), _from_vector_literal(row["embedding"]))
            for row in rows
        ]

    async def fingerprint(self) -> tuple[int, int, str | None]:
        """What changes when a document is added, replaced or removed. Phase 37.

        `(documents, chunks, latest ingest time)`: one cheap query the index's
        refresher compares with the copy it holds.
        """
        row = await self._pool.fetchrow(
            f"SELECT (SELECT count(*) FROM {DOCUMENTS_TABLE}) AS documents, "
            f"       (SELECT count(*) FROM {CHUNKS_TABLE})    AS chunks, "
            f"       (SELECT max(ingested_at) FROM {DOCUMENTS_TABLE}) AS latest"
        )
        latest = row["latest"]
        return int(row["documents"]), int(row["chunks"]), latest.isoformat() if latest is not None else None

    async def counts(self) -> tuple[int, int]:
        """How much is in the knowledge base, as `(documents, chunks)`."""
        row = await self._pool.fetchrow(
            f"SELECT (SELECT count(*) FROM {DOCUMENTS_TABLE}) AS documents, "
            f"       (SELECT count(*) FROM {CHUNKS_TABLE})    AS chunks"
        )
        return int(row["documents"]), int(row["chunks"])

    async def _verify(self) -> None:
        """Check the schema exists, the dimension matches, and the model matches.

        Raises:
            KnowledgeStoreError: Any of those is wrong, with what to do about it.
        """
        async with self._pool.acquire() as connection:
            has_extension = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
            )
            if not has_extension:
                raise KnowledgeStoreError(
                    "The pgvector extension is not enabled on this database.\n"
                    "  Run:  uv run ingest.py init"
                )

            stored_dimensions = await connection.fetchval(
                """
                SELECT a.atttypmod
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                WHERE c.relname = $1 AND a.attname = 'embedding' AND a.attnum > 0
                """,
                CHUNKS_TABLE,
            )
            if stored_dimensions is None:
                raise KnowledgeStoreError(
                    "The knowledge base tables do not exist yet.\n  Run:  uv run ingest.py init"
                )
            if stored_dimensions != self._dimensions:
                raise KnowledgeStoreError(
                    f"This knowledge base stores {stored_dimensions}-dimension vectors but the "
                    f"embedding model {self._embed_model!r} produces {self._dimensions}.\n"
                    f"  The stored embeddings cannot be searched with this model.\n"
                    f"  Either set EMBEDDING_MODEL back, or re-create and re-ingest:\n"
                    f"    uv run ingest.py reset && uv run ingest.py add <your documents>"
                )

            # Same width, different model: every query still runs and every
            # score still looks plausible, but the two sets of vectors are from
            # different spaces and the matches mean nothing. Nothing else in the
            # system would ever notice, which is why this check is here.
            other_models = await connection.fetch(
                f"SELECT DISTINCT embed_model FROM {DOCUMENTS_TABLE} WHERE embed_model <> $1",
                self._embed_model,
            )
            if other_models:
                names = ", ".join(sorted(row["embed_model"] for row in other_models))
                raise KnowledgeStoreError(
                    f"Documents in this knowledge base were embedded with {names}, but "
                    f"EMBEDDING_MODEL is now {self._embed_model!r}.\n"
                    f"  Vectors from different models are not comparable, so retrieval would "
                    f"return confident nonsense.\n"
                    f"  Re-ingest every document with the current model:\n"
                    f"    uv run ingest.py reset && uv run ingest.py add <your documents>"
                )


def _to_vector_literal(vector: list[float]) -> str:
    """Format a vector the way pgvector's text input expects: `[1,2,3]`.

    Passed as text and cast with `$n::vector` in the SQL. This avoids depending
    on asyncpg type-codec registration for a type that only exists once the
    extension is installed, which is a chicken-and-egg problem during setup.
    """
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def _from_vector_literal(text: str) -> list[float]:
    """Parse pgvector's text form, `[1,2,3]`, back into floats. Phase 37."""
    inner = text.strip()[1:-1].strip()
    return [float(value) for value in inner.split(",")] if inner else []


_DSN_PASSWORD = re.compile(r"(?<=://)([^:/@]+):([^@]*)(?=@)")


def _redact(dsn: str) -> str:
    """Hide the password in a DSN so it is safe to put in a log or an error."""
    return _DSN_PASSWORD.sub(r"\1:***", dsn)
