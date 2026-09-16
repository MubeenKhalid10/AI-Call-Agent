"""Turning an uploaded file into the chunks that get embedded.

Two jobs, in order: get the text out of the file, then cut it into pieces small
enough to embed and specific enough to retrieve.

**Chunk size is a retrieval decision, not a formatting one.** A chunk is the unit
that gets embedded, scored and handed to the LLM, so its size sets what
retrieval can do. Chunks that are too large average several topics into one
vector, so the match is vague and the agent is handed a page when it needed a
sentence. Chunks that are too small lose the context that made them meaningful —
"it costs $49 a month" retrieves badly and reads worse when the sentence naming
the product is in the previous chunk.

The defaults here (about 120 words, 30 words of overlap) are aimed at a *voice*
agent specifically. A spoken answer is one to three sentences, so the useful
retrieval unit is a short passage, not a page: a smaller chunk both matches more
sharply and keeps the injected context short enough that it does not slow the
LLM's first token down. The overlap exists so a fact that straddles a boundary
survives in at least one chunk whole.

**Splitting follows the document's own structure where it can.** Paragraphs are
kept together and only broken when one is longer than the budget on its own,
because a paragraph break is the author's own statement about where one idea
ends — a much better guess than any fixed window.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

# Extensions we can read. Phase 3 is deliberately PDF and plain text only;
# anything else is rejected with a message naming what is supported rather than
# silently ingesting garbage.
PDF_SUFFIXES = frozenset({".pdf"})
TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".text"})
SUPPORTED_SUFFIXES = PDF_SUFFIXES | TEXT_SUFFIXES


class DocumentError(RuntimeError):
    """A document could not be read or produced no usable text."""


@dataclass(frozen=True)
class Chunk:
    """One passage of a document, as it will be embedded and stored."""

    ordinal: int
    """Position in the document, from 0. Restores reading order on retrieval."""

    content: str
    """The passage text."""

    word_count: int
    """Words in `content`. Kept for diagnostics and for tuning chunk size."""


@dataclass(frozen=True)
class ExtractedDocument:
    """A file's text, plus what is needed to detect that it has not changed."""

    source: str
    """Logical name for the document — the filename. Unique in the store."""

    title: str
    """Human-readable title. The filename without its extension, tidied."""

    text: str
    """Full extracted text, whitespace-normalised."""

    content_hash: str
    """SHA-256 of the extracted text. Re-ingesting an unchanged file is a no-op."""

    byte_size: int
    """Size of the source file on disk, for the document listing."""


def extract(path: Path) -> ExtractedDocument:
    """Read a PDF or text file and return its text.

    Args:
        path: File to read.

    Returns:
        The extracted document.

    Raises:
        DocumentError: The file is missing, of an unsupported type, or contains
            no extractable text.
    """
    if not path.is_file():
        raise DocumentError(f"{path} is not a file.")

    suffix = path.suffix.lower()
    if suffix in PDF_SUFFIXES:
        text = _extract_pdf(path)
    elif suffix in TEXT_SUFFIXES:
        text = _extract_text_file(path)
    else:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise DocumentError(
            f"{path.name} has an unsupported extension ({suffix or 'none'}). Supported: {supported}."
        )

    text = _normalise(text)
    if not text:
        raise DocumentError(
            f"{path.name} produced no text. If it is a PDF, it may be a scan of a page rather "
            f"than text — that needs OCR, which is not part of this phase."
        )

    return ExtractedDocument(
        source=path.name,
        title=_title_from_filename(path),
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        byte_size=path.stat().st_size,
    )


def chunk(text: str, *, target_words: int = 120, overlap_words: int = 30) -> list[Chunk]:
    """Split text into overlapping passages.

    Paragraphs are packed together until adding the next one would exceed
    `target_words`; a paragraph longer than the budget on its own is split on
    sentence boundaries. Consecutive chunks share `overlap_words` words, so a
    fact that lands on a boundary is complete in at least one of them.

    Args:
        text: Whitespace-normalised document text.
        target_words: Soft upper bound on words per chunk.
        overlap_words: Words repeated from the end of the previous chunk.

    Returns:
        The chunks, in reading order. Empty if `text` has no words.

    Raises:
        ValueError: `target_words` is not positive, or the overlap is not
            smaller than the target (which would never advance).
    """
    if target_words <= 0:
        raise ValueError("target_words must be positive.")
    if not 0 <= overlap_words < target_words:
        raise ValueError("overlap_words must be at least 0 and smaller than target_words.")

    pieces = _split_to_budget(text, target_words)
    if not pieces:
        return []

    chunks: list[Chunk] = []
    carry: list[str] = []  # Tail of the previous chunk, repeated for continuity.
    for piece in pieces:
        words = carry + piece.split()
        content = " ".join(words)
        chunks.append(Chunk(ordinal=len(chunks), content=content, word_count=len(words)))
        carry = words[-overlap_words:] if overlap_words else []

    return chunks


def _extract_pdf(path: Path) -> str:
    """Pull the text layer out of a PDF, one page at a time."""
    # Imported here rather than at module scope so that the text-only path, and
    # anything that merely imports this module, does not pay for pypdf.
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as exc:
        raise DocumentError(f"{path.name} is not a readable PDF: {exc}") from exc
    except Exception as exc:  # pypdf raises a wide range on malformed files.
        raise DocumentError(f"{path.name} could not be read: {exc}") from exc

    # Join on a blank line so a page break reads as a paragraph break to the
    # chunker, rather than running the last line of one page into the first line
    # of the next.
    return "\n\n".join(page for page in pages if page.strip())


def _extract_text_file(path: Path) -> str:
    """Read a text file, tolerating an unexpected encoding rather than failing."""
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # latin-1 maps every byte, so this is unreachable in practice; it is here so
    # the function has no silent path that returns nothing.
    raise DocumentError(f"{path.name} is not text in any encoding we tried.")


_WHITESPACE_RUN = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n\s*")
# A hyphen at the end of a line, which PDF text layers use for a word broken
# across lines. Left in place it produces tokens like "sub-" and "scription".
_LINE_BREAK_HYPHEN = re.compile(r"(\w)-\n(\w)")
_PARAGRAPH_SPLIT = re.compile(r"\n\n+")
# Sentence end: terminator, closing quote or bracket if any, then whitespace.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])[\"')\]]*\s+")


def _normalise(text: str) -> str:
    """Collapse whitespace while keeping paragraph breaks, which the chunker uses."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _LINE_BREAK_HYPHEN.sub(r"\1\2", text)
    text = _WHITESPACE_RUN.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    # A single newline inside a paragraph is a wrap, not a break.
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return text.strip()


def _split_to_budget(text: str, target_words: int) -> list[str]:
    """Pack paragraphs into pieces of at most `target_words` words."""
    pieces: list[str] = []
    buffer: list[str] = []
    buffered_words = 0

    def flush() -> None:
        nonlocal buffer, buffered_words
        if buffer:
            pieces.append("\n\n".join(buffer))
            buffer = []
            buffered_words = 0

    for paragraph in _PARAGRAPH_SPLIT.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        words = len(paragraph.split())
        if words > target_words:
            # Too big to pack: emit whatever is buffered, then break this
            # paragraph on sentence boundaries.
            flush()
            pieces.extend(_split_long_paragraph(paragraph, target_words))
            continue

        if buffered_words + words > target_words:
            flush()
        buffer.append(paragraph)
        buffered_words += words

    flush()
    return pieces


def _split_long_paragraph(paragraph: str, target_words: int) -> list[str]:
    """Break one over-long paragraph on sentence boundaries, then on words."""
    pieces: list[str] = []
    buffer: list[str] = []
    buffered_words = 0

    for sentence in _SENTENCE_SPLIT.split(paragraph):
        sentence = sentence.strip()
        if not sentence:
            continue

        words = sentence.split()
        if len(words) > target_words:
            # A single sentence over the budget — a table row, a run-on, or a
            # PDF whose text layer lost its punctuation. Nothing structural left
            # to cut on, so fall back to a fixed window.
            if buffer:
                pieces.append(" ".join(buffer))
                buffer, buffered_words = [], 0
            for start in range(0, len(words), target_words):
                pieces.append(" ".join(words[start : start + target_words]))
            continue

        if buffered_words + len(words) > target_words and buffer:
            pieces.append(" ".join(buffer))
            buffer, buffered_words = [], 0
        buffer.extend(words)
        buffered_words += len(words)

    if buffer:
        pieces.append(" ".join(buffer))
    return pieces


def _title_from_filename(path: Path) -> str:
    """Make a readable title from a filename: `pricing_faq.pdf` -> `Pricing Faq`."""
    stem = path.stem.replace("_", " ").replace("-", " ")
    stem = _WHITESPACE_RUN.sub(" ", stem).strip()
    return stem.title() if stem.islower() or stem.isupper() else stem or path.name
