#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Knowledge base management: upload documents, inspect them, test retrieval.

This is the other half of Phase 3. `bot.py` reads the knowledge base during a
call; this puts things into it and lets you see what the agent will find.

Run it from the `server/` directory::

    uv run ingest.py init                      # create the schema, once
    uv run ingest.py add evals/kb/*.txt        # upload documents
    uv run ingest.py list                      # what is in there
    uv run ingest.py search "your support hours"   # what the agent would retrieve
    uv run ingest.py remove pricing.pdf
    uv run ingest.py reset                     # drop everything and start over

`search` is the command to reach for first when the agent answers something
badly. It prints exactly the passages that would have been put in front of the
LLM, with their scores, so you can tell a retrieval problem (the right passage
was never fetched — fix the documents or the chunking) from a generation problem
(the passage was right there and the model still answered badly — fix the
prompt). Those two look identical from the outside and have nothing in common as
fixes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.config import Config, ConfigError
from src.documents import DocumentError, chunk, extract
from src.embeddings import EmbeddingError, make_embedder
from src.knowledge_store import KnowledgeStore, KnowledgeStoreError

load_dotenv(override=True)


async def command_init(config: Config, args: argparse.Namespace) -> int:
    """Create the extension, tables and index."""
    embedder = make_embedder(config.embedding_model)
    print(f"Loading embedding model {embedder.model_name} ...")
    dimensions = embedder.dimensions

    store = await _connect(config, dimensions, embedder.model_name, create_schema=True)
    try:
        documents, chunks = await store.counts()
    finally:
        await store.close()

    print(
        f"Knowledge base ready: {dimensions}-dimension vectors, "
        f"{documents} document(s), {chunks} chunk(s)."
    )
    return 0


async def command_add(config: Config, args: argparse.Namespace) -> int:
    """Extract, chunk, embed and store one or more documents."""
    paths = _expand(args.paths)
    if not paths:
        print("No matching files.", file=sys.stderr)
        return 1

    embedder = make_embedder(config.embedding_model)
    print(f"Loading embedding model {embedder.model_name} ...")
    store = await _connect(config, embedder.dimensions, embedder.model_name)

    added = skipped = failed = 0
    try:
        for path in paths:
            try:
                document = extract(path)
            except DocumentError as exc:
                print(f"  SKIP  {path.name}: {exc}", file=sys.stderr)
                failed += 1
                continue

            if not args.force:
                stored_hash = await store.get_document_hash(document.source)
                if stored_hash == document.content_hash:
                    print(f"  same  {document.source} (unchanged, not re-embedded)")
                    skipped += 1
                    continue

            pieces = chunk(
                document.text,
                target_words=config.kb_chunk_words,
                overlap_words=config.kb_chunk_overlap_words,
            )
            if not pieces:
                print(f"  SKIP  {document.source}: produced no chunks.", file=sys.stderr)
                failed += 1
                continue

            # Embedding is CPU-bound and this is a script, so it runs on a
            # worker thread only to keep the event loop free for the database
            # round trips either side of it.
            vectors = await asyncio.to_thread(
                embedder.embed_documents, [piece.content for piece in pieces]
            )
            stored = await store.add_document(
                source=document.source,
                title=document.title,
                content_hash=document.content_hash,
                byte_size=document.byte_size,
                chunks=[piece.content for piece in pieces],
                vectors=vectors,
            )
            words = sum(piece.word_count for piece in pieces)
            print(
                f"  ok    {document.source}  ->  {stored} chunk(s), "
                f"~{words // max(stored, 1)} words each"
            )
            added += 1

        documents, chunks = await store.counts()
    finally:
        await store.close()

    print(
        f"\n{added} added, {skipped} unchanged, {failed} failed. "
        f"Knowledge base now holds {documents} document(s), {chunks} chunk(s)."
    )
    return 1 if failed else 0


async def command_list(config: Config, args: argparse.Namespace) -> int:
    """Show what is in the knowledge base."""
    embedder = make_embedder(config.embedding_model)
    store = await _connect(config, embedder.dimensions, embedder.model_name)
    try:
        documents = await store.list_documents()
        _, chunks = await store.counts()
    finally:
        await store.close()

    if not documents:
        print("The knowledge base is empty.  Add a document:  uv run ingest.py add <file>")
        return 0

    width = max(len(document.source) for document in documents)
    print(f"{'SOURCE'.ljust(width)}  CHUNKS  SIZE      INGESTED")
    for document in documents:
        print(
            f"{document.source.ljust(width)}  {document.chunk_count:>6}  "
            f"{_human_size(document.byte_size):>8}  {document.ingested_at}"
        )
    print(f"\n{len(documents)} document(s), {chunks} chunk(s), model {documents[0].embed_model}.")
    return 0


async def command_search(config: Config, args: argparse.Namespace) -> int:
    """Show the passages the agent would retrieve for a question.

    Prints everything the nearest-neighbour search returns and marks which of
    them clear `KB_MIN_SCORE`, because seeing the ones that just missed is how
    you tell "the threshold is too high" from "the document does not say".
    """
    embedder = make_embedder(config.embedding_model)
    store = await _connect(config, embedder.dimensions, embedder.model_name)
    try:
        vector = await embedder.embed_query_async(args.query)
        # Deliberately unfiltered: the floor is applied here for display only.
        matches = await store.search(vector, limit=args.limit, min_score=0.0)
    finally:
        await store.close()

    if not matches:
        print("The knowledge base is empty.")
        return 0

    print(f"Query: {args.query!r}   (KB_MIN_SCORE = {config.kb_min_score:g})\n")
    for index, match in enumerate(matches, start=1):
        kept = "used" if match.score >= config.kb_min_score else "below threshold"
        print(f"[{index}] {match.score:.3f}  {kept}  —  {match.source} #{match.ordinal}")
        print(f"      {match.content[:400]}\n")

    used = sum(1 for match in matches if match.score >= config.kb_min_score)
    if used:
        print(f"{used} of {len(matches)} passage(s) would be given to the agent.")
    else:
        print("No passage clears the threshold — the agent would say it does not have this.")
    return 0


async def command_remove(config: Config, args: argparse.Namespace) -> int:
    """Delete a document and its chunks."""
    embedder = make_embedder(config.embedding_model)
    store = await _connect(config, embedder.dimensions, embedder.model_name)
    try:
        removed = await store.delete_document(args.source)
    finally:
        await store.close()

    if removed:
        print(f"Removed {args.source}.")
        return 0
    print(f"No document named {args.source!r}.  See:  uv run ingest.py list", file=sys.stderr)
    return 1


async def command_reset(config: Config, args: argparse.Namespace) -> int:
    """Drop the knowledge base tables and re-create them empty.

    The way out of a changed `EMBEDDING_MODEL`, a changed chunk size, or any
    other situation where what is stored no longer matches how it would be
    searched. Destructive, so it asks unless `--yes` is passed.
    """
    if not args.yes:
        print("This deletes every document in the knowledge base.")
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            print("Cancelled.")
            return 1

    embedder = make_embedder(config.embedding_model)
    # No schema verification on the way in: the point of reset is to recover
    # from a schema that no longer verifies.
    import asyncpg

    from src.knowledge_store import CHUNKS_TABLE, DOCUMENTS_TABLE

    try:
        connection = await asyncpg.connect(config.kb_database_url, timeout=10.0)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"\nCould not connect to the knowledge base.\n  {exc}\n", file=sys.stderr)
        return 1
    try:
        await connection.execute(f"DROP TABLE IF EXISTS {CHUNKS_TABLE}")
        await connection.execute(f"DROP TABLE IF EXISTS {DOCUMENTS_TABLE}")
    finally:
        await connection.close()

    print("Dropped. Re-creating ...")
    store = await _connect(config, embedder.dimensions, embedder.model_name, create_schema=True)
    await store.close()
    print("Knowledge base is empty and ready.  Add documents:  uv run ingest.py add <file>")
    return 0


async def _connect(
    config: Config,
    dimensions: int,
    model_name: str,
    *,
    create_schema: bool = False,
) -> KnowledgeStore:
    """Open the store, or exit with a readable message."""
    if not config.kb_database_url:
        raise SystemExit(
            "\nKB_DATABASE_URL is not set. Point it at a PostgreSQL database with pgvector, "
            "e.g.\n  KB_DATABASE_URL=postgresql://postgres:PASSWORD@localhost:5432/voice_agent_kb\n"
        )
    return await KnowledgeStore.connect(
        config.kb_database_url,
        dimensions=dimensions,
        embed_model=model_name,
        create_schema=create_schema,
    )


def _expand(patterns: list[str]) -> list[Path]:
    """Resolve paths, expanding globs and directories into the files inside them.

    The shell on Windows does not expand `*.txt`, so the pattern arrives here
    literally; expanding it here means the same command works in every shell.
    """
    from src.documents import SUPPORTED_SUFFIXES

    found: list[Path] = []
    for pattern in patterns:
        path = Path(pattern)
        if path.is_dir():
            found.extend(
                child
                for child in sorted(path.iterdir())
                if child.is_file() and child.suffix.lower() in SUPPORTED_SUFFIXES
            )
        elif path.exists():
            found.append(path)
        else:
            base = path.parent if path.parent != Path("") else Path(".")
            found.extend(sorted(base.glob(path.name)))

    # De-duplicate while keeping order, so `add a.txt docs/` does not ingest a
    # file twice when it appears in both.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def _human_size(byte_size: int) -> str:
    """Format a byte count for the document listing."""
    if byte_size < 1024:
        return f"{byte_size} B"
    if byte_size < 1024 * 1024:
        return f"{byte_size / 1024:.1f} KB"
    return f"{byte_size / (1024 * 1024):.1f} MB"


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="ingest.py",
        description="Manage the voice agent's knowledge base.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="create the pgvector schema").set_defaults(run=command_init)

    add = subparsers.add_parser("add", help="upload PDF or text documents")
    add.add_argument("paths", nargs="+", help="files, directories or glob patterns")
    add.add_argument(
        "--force",
        action="store_true",
        help="re-embed even if the document's text has not changed",
    )
    add.set_defaults(run=command_add)

    subparsers.add_parser("list", help="list stored documents").set_defaults(run=command_list)

    search = subparsers.add_parser("search", help="show what the agent would retrieve")
    search.add_argument("query", help="the question to search for")
    search.add_argument("-n", "--limit", type=int, default=6, help="passages to show")
    search.set_defaults(run=command_search)

    remove = subparsers.add_parser("remove", help="delete one document")
    remove.add_argument("source", help="document name as shown by `list`")
    remove.set_defaults(run=command_remove)

    reset = subparsers.add_parser("reset", help="drop and re-create the knowledge base")
    reset.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    reset.set_defaults(run=command_reset)

    return parser


def main() -> int:
    """Parse arguments and run the chosen command."""
    args = _parser().parse_args()
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    try:
        return asyncio.run(args.run(config, args))
    except (KnowledgeStoreError, EmbeddingError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
