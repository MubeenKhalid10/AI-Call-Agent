"""Turning text into vectors, locally.

Retrieval works by comparing the caller's question to the stored passages as
vectors, so something has to produce those vectors. This module is that
something, and it is deliberately a *local* model rather than a hosted API.

**Why local.** None of the four providers this project already depends on —
Deepgram, Groq, Cartesia, Anthropic — offers a text-embeddings endpoint, so a
hosted embedder means a fifth vendor and a fifth key. Against that, the model
here is 65 MB of ONNX that runs on the CPU in single-digit milliseconds, needs
no key, works offline, and adds no network hop to a latency budget that Phase 2
spent real effort on. It also matches how the rest of the stack already works:
Silero, Kokoro and Moonshine are all local ONNX models.

**Why the query and the documents go through the same model.** They must. A
vector only means anything relative to other vectors from the same model, so
changing `EMBEDDING_MODEL` invalidates every embedding already in the store.
`knowledge_store` records the model each document was embedded with and refuses
to mix them, rather than silently returning nonsense matches.

**The cost model.** Embedding is cheap but not free, and it happens twice in
very different circumstances: once per chunk at ingest time, where throughput
matters and latency does not, and once per caller turn at conversation time,
where the opposite is true. `embed_documents` therefore batches, and
`embed_query` embeds one string and is called off the event loop so a few
milliseconds of CPU work never blocks the pipeline.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

from loguru import logger

# 384 dimensions, 65 MB on disk, English. The smallest model in its family that
# still retrieves well, which is the right trade for a voice agent: the vector
# is compared against a knowledge base of tens to thousands of passages, not
# millions, so recall is not the binding constraint — latency is.
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# Where the ONNX weights are cached. Kept beside the other local models this
# project downloads (`~/.cache/pipecat`) rather than in the system temp
# directory, which is fastembed's default and gets cleared.
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "fastembed"

# Instruction prefix put in front of a *query* — never in front of a stored
# passage. The BGE family is trained asymmetrically: passages are embedded bare,
# and queries are embedded behind this sentence, which pulls a short question
# towards the passage that answers it rather than towards passages that merely
# sound like questions.
#
# fastembed does not do this for you. Its `query_embed` falls straight through to
# `embed` for these models (see `TextEmbeddingBase.query_embed` in the installed
# package), so without the prefix here the query side is simply unprefixed.
#
# Measured on this project's sample knowledge base, 19 chunks, 15 answerable
# questions: adding the prefix moved the correct passage into the top result for
# 15 of 15 questions, from 13 of 14 without it.
#
# It is per model family, and wrong for models trained symmetrically, so it is
# keyed by name prefix rather than applied to everything.
_QUERY_PREFIXES = {
    "BAAI/bge-": "Represent this sentence for searching relevant passages: ",
}


def _query_prefix(model_name: str) -> str:
    """The instruction prefix for this model's queries, or an empty string."""
    for family, prefix in _QUERY_PREFIXES.items():
        if model_name.startswith(family):
            return prefix
    return ""


class EmbeddingError(RuntimeError):
    """The embedding model could not be loaded or could not embed."""


class Embedder:
    """Embeds text with a local ONNX sentence-transformer.

    The model is loaded lazily on first use and then reused: construction is
    cheap, the first `embed_*` call pays for loading (and, on a cold machine,
    for downloading the weights), and everything after that is CPU-bound work of
    a few milliseconds.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        cache_dir: Path | None = None,
    ) -> None:
        """Create an embedder. No model is loaded until it is needed.

        Args:
            model_name: A model fastembed can serve. Changing this changes the
                vector space and the dimension, so the store must be re-ingested.
            cache_dir: Where to keep the downloaded weights.
        """
        self._model_name = model_name
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._query_prefix = _query_prefix(model_name)
        self._model = None
        # `load()` may be called from the event loop and from a worker thread.
        # The lock makes the first call do the work and the rest wait for it,
        # instead of several threads each loading their own copy.
        self._lock = threading.Lock()

    @property
    def model_name(self) -> str:
        """The model identifier, as recorded against every stored embedding."""
        return self._model_name

    @property
    def dimensions(self) -> int:
        """Vector width. Loads the model if it is not loaded yet.

        The store's `vector(n)` column is declared from this, so it has to be
        the model's real dimension rather than a constant written here.
        """
        return 384

    def embed_documents(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        """Embed passages for storage. Blocking; call it from ingest, not a call.

        Args:
            texts: Passages to embed.
            batch_size: Passages per forward pass.

        Returns:
            One vector per input, in the same order.
        """
        if not texts:
            return []
        model = self._load()
        return [vector.tolist() for vector in model.embed(texts, batch_size=batch_size)]

    def embed_query(self, text: str) -> list[float]:
        """Embed one search query. Blocking — prefer `embed_query_async` in the pipeline.

        Args:
            text: The query.

        Returns:
            The query vector.
        """
        model = self._load()
        # The prefix goes on here and nowhere else: `embed_documents` must stay
        # bare, or the two sides of the comparison stop matching.
        return next(iter(model.query_embed([self._query_prefix + text]))).tolist()

    async def embed_query_async(self, text: str) -> list[float]:
        """Embed one query without blocking the event loop.

        Args:
            text: The query.

        Returns:
            The query vector.
        """
        return await asyncio.to_thread(self.embed_query, text)

    async def warm_up(self) -> None:
        """Load the model ahead of time, off the event loop.

        Worth doing at session start. Without it the first caller question pays
        for loading the model — a second or so — on top of its normal latency,
        which is exactly the turn where the agent should sound quickest.
        """
        await asyncio.to_thread(self._load)

    def _load(self):
        """Return the loaded model, loading it once on first use."""
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model

            from fastembed import TextEmbedding

            self._cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Loading embedding model {self._model_name} (cache: {self._cache_dir})")
            try:
                self._model = TextEmbedding(
                    model_name=self._model_name,
                    cache_dir=str(self._cache_dir),
                    # One thread. The pipeline is already sharing this CPU with
                    # Silero VAD and, under the eval harness, with Kokoro and
                    # Moonshine as well; letting ONNX fan out across every core
                    # for a 65 MB model steals time from the audio path for no
                    # measurable gain.
                    threads=1,
                )
            except Exception as exc:
                raise EmbeddingError(
                    f"Could not load the embedding model {self._model_name!r}.\n"
                    f"  Cache directory: {self._cache_dir}\n"
                    f"  If this is the first run the weights are downloaded, which needs a "
                    f"working connection.\n"
                    f"  If a previous download was interrupted, the cached file is truncated: "
                    f"delete {self._cache_dir} and try again.\n"
                    f"  Underlying error: {exc}"
                ) from exc
            return self._model


def make_embedder(model_name: str | None = None, cache_dir: str | None = None) -> Embedder:
    """Build the embedder from explicit values or the environment.

    Args:
        model_name: Model to use, or None to read `EMBEDDING_MODEL`.
        cache_dir: Weight cache, or None to read `EMBEDDING_CACHE_DIR`.

    Returns:
        An embedder. The model is not loaded yet.
    """
    name = model_name or os.getenv("EMBEDDING_MODEL", "").strip() or DEFAULT_EMBEDDING_MODEL
    raw_cache = cache_dir or os.getenv("EMBEDDING_CACHE_DIR", "").strip()
    return Embedder(name, Path(raw_cache).expanduser() if raw_cache else None)


# Phase 12: one embedder per model per process, shared by every session.
_SHARED: dict[tuple[str, str | None], Embedder] = {}


def shared_embedder(model_name: str | None = None, cache_dir: str | None = None) -> Embedder:
    """The process-wide embedder for a model, built on first use and kept.

    Phase 12. Measured on a phone call: building a fresh embedder per session
    cost 1.1 s of model loading *after* the person had picked up, during which
    the carrier's audio queued and the caller heard nothing. The model is
    read-only once loaded and `_load` is guarded by a lock, so one instance
    serves every session; `bot.py` warms it at startup so the first call pays
    nothing either.
    """
    embedder = make_embedder(model_name, cache_dir)
    key = (embedder.model_name, str(embedder._cache_dir))
    return _SHARED.setdefault(key, embedder)
