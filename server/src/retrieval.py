"""Retrieval: putting the right passages in front of the LLM, every turn.

This is the processor that makes the agent answer from the knowledge base rather
than from whatever the model happens to remember. It sits between the user
context aggregator and the LLM, so it sees the conversation exactly as it is
about to be sent for inference and can add to it before it goes.

Design notes, because two of the choices here are the ones worth arguing about.

**Retrieve on the turns that could be asking for a fact, rather than exposing
search as a tool the LLM calls.** Phase 3 retrieved unconditionally; Phase 6
puts a gate in front of it (`looks_like_information_request`, controlled by
`KB_RETRIEVAL_MODE`) because a sales conversation is mostly not questions —
"yeah", "we do it by hand", "next quarter probably" — and searching a document
store for each of those returns confident, on-topic, irrelevant passages and
attaches them to a turn nobody asked a question in. The gate is deliberately
written to *skip* rather than to *allow*, so the default answer is still to
search; `KB_RETRIEVAL_MODE=always` restores Phase 3 exactly. The rest of this
argument is unchanged and is why search is not a tool:
The tool version is the more fashionable design and it is the wrong one here,
for two reasons. Latency: a tool call is a whole extra LLM round trip before the
first word is spoken, four hundred milliseconds or more onto a response budget
that Phase 2 spent its entire effort getting to around 1.3 seconds. Reliability:
a tool the model *may* call is a weaker guarantee than context it always has,
and the requirement is that the agent answers from the knowledge base — not that
it usually remembers to look. Retrieving unconditionally costs an embedding
(single-digit milliseconds, locally, off the event loop) and one indexed query.
Function calling arrives in Phase 4 for *actions*, which is what it is good at.

**The retrieved text never enters the conversation history.** The processor does
not touch the real context; it builds a shallow copy, appends the passages to
that, and sends the copy to the LLM. The context the aggregators own — the one
that becomes the transcript, and the one that grows for the whole call — stays
exactly as the caller and agent said it. If the block were appended for real,
every turn would leave its excerpts behind, the context would grow by several
hundred words a turn, and by minute three the model would be reading five stale
retrievals alongside the current one.

**When nothing matches, say so rather than staying quiet.** Below the similarity
threshold the processor still injects a block, one that reports the knowledge
base had nothing. Leaving it out would be worse than useless: the model would
see an ordinary question with no context and answer it from general knowledge,
which is the exact failure this phase exists to prevent. The wording deliberately
lets the model distinguish "you asked me something I don't have" from "you said
hello", because retrieval runs on every turn and most turns are not questions.
"""

from __future__ import annotations

import time

from loguru import logger
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext, LLMContextMessage
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .config import Config
from .embeddings import Embedder
from .knowledge_store import KnowledgeStore, Match
from .prompts import (
    KNOWLEDGE_BLOCK_FOOTER,
    KNOWLEDGE_BLOCK_HEADER,
    KNOWLEDGE_EXCERPT,
    KNOWLEDGE_NONE_BLOCK,
    is_injected_block,
    is_turn_instruction,
)

# Words that make a turn worth searching for: an interrogative, or a noun a
# prospect only uses when they want a fact about the business. See
# `looks_like_information_request` for how the two lists are used and why the
# gate is written to *skip* rather than to *allow*.
_QUESTION_WORDS = frozenset(
    {
        "what",
        "whats",
        "how",
        "why",
        "when",
        "where",
        "who",
        "whos",
        "which",
        "can",
        "could",
        "do",
        "does",
        "did",
        "is",
        "are",
        "was",
        "will",
        "would",
        "should",
        "tell",
        "explain",
        "wondering",
        "curious",
    }
)

_BUSINESS_WORDS = (
    "price",
    "pricing",
    "cost",
    "charge",
    "fee",
    "quote",
    "plan",
    "package",
    "tier",
    "product",
    "service",
    "feature",
    "integrat",
    "support",
    "contract",
    "term",
    "trial",
    "discount",
    "guarantee",
    "refund",
    "cancel",
    "security",
    "complian",
    "certif",
    "api",
    "demo",
    "offer",
    "warrant",
    "deliver",
    "install",
    "onboard",
    "sla",
    "licence",
    "license",
    "subscription",
)

# Whole utterances that are never an information request, however they are
# punctuated. Matched against the entire normalised turn, not searched inside
# it, so "no" is skipped and "no idea what your api costs" is not.
_BACKCHANNELS = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "no",
        "nope",
        "nah",
        "ok",
        "okay",
        "sure",
        "right",
        "alright",
        "fine",
        "got it",
        "i see",
        "mhm",
        "mm",
        "uh huh",
        "hmm",
        "hello",
        "hi",
        "hey",
        "good morning",
        "good afternoon",
        "good evening",
        "thanks",
        "thank you",
        "cheers",
        "bye",
        "goodbye",
        "go on",
        "carry on",
        "sorry",
        "pardon",
        "what",
        "sorry what",
        "say again",
        "not really",
        "not right now",
        "no thanks",
        "no thank you",
        "maybe",
        "possibly",
        "exactly",
        "correct",
        "of course",
    }
)


class KnowledgeRetriever(FrameProcessor):
    """Augments each inference with passages retrieved from the knowledge base.

    Place it directly before the LLM::

        transport.input() -> stt -> user_aggregator -> retriever -> llm -> ...

    Every `LLMContextFrame` heading downstream is intercepted, the caller's most
    recent message is embedded and searched, and a copy of the context carrying
    the results is forwarded in its place. Everything else passes through
    untouched.
    """

    def __init__(
        self,
        store: KnowledgeStore,
        embedder: Embedder,
        config: Config,
        **kwargs,
    ) -> None:
        """Create the retriever.

        Args:
            store: The connected knowledge base.
            embedder: Embedder for the query. Must be the same model the stored
                documents were embedded with; `KnowledgeStore` enforces that.
            config: Supplies `kb_top_k`, `kb_min_score` and the query heuristic.
            **kwargs: Passed to `FrameProcessor`.
        """
        super().__init__(**kwargs)
        self._store = store
        self._embedder = embedder
        self._top_k = config.kb_top_k
        self._min_score = config.kb_min_score
        self._short_query_words = config.kb_short_query_words
        self._log_retrieval = config.log_metrics
        # "always" is Phase 3's behaviour: search on every single turn. "auto"
        # skips the turns that cannot be an information request — see
        # `looks_like_information_request`.
        self._gated = config.kb_retrieval_mode == "auto"
        # Set on the first retrieval and never reset: an empty knowledge base is
        # a setup mistake worth one warning, not one per turn.
        self._warned_empty = False
        # Phase 29: the session's latency tracker, when `bot.py` sets one.
        # Told when a retrieval starts and how it ended, so the per-turn
        # breakdown has a KB figure; nothing else about retrieval changes.
        self.latency = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Intercept context frames on their way to the LLM.

        Args:
            frame: The frame.
            direction: Which way it is travelling.
        """
        await super().process_frame(frame, direction)

        # Only downstream frames are on their way to inference. The assistant
        # aggregator pushes context frames *upstream* too, and augmenting one of
        # those would inject the block into a path that is not a caller turn.
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            augmented = await self._augment(frame.context)
            await self.push_frame(LLMContextFrame(context=augmented), direction)
            return

        await self.push_frame(frame, direction)

    async def _augment(self, context: LLMContext) -> LLMContext:
        """Return a copy of `context` with the retrieved passages appended.

        Returns `context` itself when there is nothing to add — the knowledge
        base is empty, the last message is one of our own turn instructions, or
        retrieval failed. Never raises: a knowledge base that has gone away
        should degrade the agent to Phase 2 behaviour, not end the call.
        """
        messages = list(context.messages)
        query = _query_from(messages, self._short_query_words)
        if query is None:
            return context

        if self.latency is not None:
            self.latency.retrieval_started()

        if self._gated and not looks_like_information_request(query):
            # Nothing is injected at all on a skipped turn, not even the
            # "nothing found" block: the point of skipping is that the turn was
            # never a question about the business, so a note telling the model
            # it has no information about it would be answering something
            # nobody asked. What keeps the agent honest on a turn the gate got
            # wrong is the system prompt's rule against inventing facts.
            if self._log_retrieval:
                logger.debug(f"KB | skipped, not an information request | {query[:60]!r}")
            self._note_retrieval("skipped")
            return context

        started = time.monotonic()
        try:
            matches = await self._retrieve(query)
        except Exception as exc:
            # Postgres restarted, the network blinked, the model failed to load.
            # The caller is mid-sentence; log it and let the agent answer without
            # the knowledge base rather than dropping the turn.
            logger.error(f"Knowledge retrieval failed, answering without it: {exc}")
            self._note_retrieval("failed")
            return context

        if matches is None:
            self._note_retrieval("empty")
            return context  # Knowledge base is empty; behave as if there is none.
        self._note_retrieval(f"{len(matches)} passages" if matches else "nothing found")

        block = _format_block(matches)
        elapsed_ms = (time.monotonic() - started) * 1000
        if self._log_retrieval:
            if matches:
                best = matches[0]
                logger.info(
                    f"KB | {len(matches)} passage(s) in {elapsed_ms:.0f}ms | "
                    f"best {best.score:.2f} from {best.source} | query: {query[:60]!r}"
                )
            else:
                logger.info(
                    f"KB | nothing above {self._min_score:.2f} in {elapsed_ms:.0f}ms | "
                    f"query: {query[:60]!r}"
                )

        # A shallow copy: same messages, plus the block. `tools` and
        # `tool_choice` are carried over so that adding tools in a later phase
        # does not silently stop advertising them on knowledge turns.
        augmented = LLMContext(
            messages=[*messages, {"role": "user", "content": block}],
            tools=context.tools,
            tool_choice=context.tool_choice,
        )
        return augmented

    def _note_retrieval(self, outcome: str) -> None:
        """Phase 29: report how this turn's retrieval ended to the latency tracker, if any."""
        if self.latency is not None:
            self.latency.retrieval_finished(outcome)

    async def search(self, query: str) -> list[Match] | None:
        """Search on demand, for the `search_knowledge_base` tool. Phase 7.

        The same embedder, the same store, the same `KB_TOP_K` and
        `KB_MIN_SCORE` as the per-turn stage — the tool is a second *entry* to
        the retrieval path, not a second retrieval path. The one difference is
        that the gate does not apply: the model has said explicitly that it
        wants a fact, which is the question the gate exists to guess at.

        Returns:
            Matches, closest first and possibly empty, or None when the
            knowledge base holds nothing at all.

        Raises:
            Whatever the store or the embedder raises. The caller — the action
            service — turns that into a failed outcome; here it stays an
            exception so it is not mistaken for "nothing found".
        """
        return await self._retrieve(query.strip())

    async def _retrieve(self, query: str) -> list[Match] | None:
        """Search the knowledge base. None means the knowledge base is empty."""
        _documents, chunks = await self._store.counts()
        if chunks == 0:
            if not self._warned_empty:
                self._warned_empty = True
                logger.warning(
                    "Knowledge base is empty — the agent will answer from the model's own "
                    "knowledge. Ingest a document with:  uv run ingest.py add <file>"
                )
            return None

        vector = await self._embedder.embed_query_async(query)
        return await self._store.search(vector, limit=self._top_k, min_score=self._min_score)


def _query_from(messages: list[LLMContextMessage], short_query_words: int) -> str | None:
    """Build the search query from the conversation, or None to skip retrieval.

    The query is the caller's latest message. Two adjustments:

    * Our own turn instructions — the greeting, the idle nudges, the goodbye —
      are added to the context with `role: "user"` (see `resilience.prompt_agent`
      for why that role), so without this check the agent would search the
      knowledge base for the text of its own stage directions.
    * A very short follow-up is embedded together with the caller's previous
      message. "How much is that?" retrieves almost nothing on its own; paired
      with "Tell me about the premium plan" it retrieves the right passage. The
      threshold is low on purpose — a question long enough to stand alone should
      be searched on its own terms, not blurred with an older topic.
    """
    latest = None
    previous = None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if latest is None:
            latest = content.strip()
        else:
            previous = content.strip()
            break

    # `is_injected_block` rather than a comparison against a fixed set: Phase 6's
    # instructions are composed per call from the prospect's name and cannot be
    # compared against anything, so they are recognised by their prefix.
    if latest is None or is_injected_block(latest):
        return None

    if (
        previous
        and len(latest.split()) < short_query_words
        and not is_turn_instruction(previous)
    ):
        return f"{previous} {latest}"
    return latest


def looks_like_information_request(text: str) -> bool:
    """Whether this turn could be asking for a fact about the business.

    The gate for `KB_RETRIEVAL_MODE=auto`. Phase 3 searched on every turn, which
    is correct for an agent whose only job is answering questions and wasteful
    for one running a sales conversation, where most turns are "yeah", "we do it
    by hand at the moment", and "next quarter, probably".

    **It is written to skip, not to allow, and that asymmetry is the safety
    property.** The question it answers is "could this possibly be a request for
    information", not "is this definitely one" — because a turn wrongly searched
    costs one indexed query, and a turn wrongly skipped costs the grounding that
    Phase 3 exists to provide. So anything with a question mark, any
    interrogative, and anything mentioning a commercial noun goes through, and
    only turns with none of the three are dropped.

    Args:
        text: The caller's turn, or the paired short-follow-up query built by
            `_query_from`.

    Returns:
        True to search the knowledge base.
    """
    if not text:
        return False
    if "?" in text:
        return True

    normalized = " ".join(
        "".join(character if character.isalnum() or character.isspace() else " " for character in text)
        .lower()
        .split()
    )
    if not normalized:
        return False
    if normalized in _BACKCHANNELS:
        return False

    words = normalized.split()
    if _QUESTION_WORDS & set(words):
        return True
    return any(stem in normalized for stem in _BUSINESS_WORDS)


def _format_block(matches: list[Match]) -> str:
    """Render retrieved passages as the message appended for this one inference."""
    if not matches:
        return KNOWLEDGE_NONE_BLOCK

    excerpts = "\n\n".join(
        KNOWLEDGE_EXCERPT.format(
            number=index,
            title=match.title,
            source=match.source,
            content=match.content,
        )
        for index, match in enumerate(matches, start=1)
    )
    return f"{KNOWLEDGE_BLOCK_HEADER}\n\n{excerpts}\n\n{KNOWLEDGE_BLOCK_FOOTER}"
