"""Generate `meridian_pricing.pdf`, the PDF fixture for the knowledge base tests.

The knowledge base accepts PDF and text, and only one of those two paths is
exercised by a `.txt` fixture. This writes a small, real PDF with a genuine text
layer so `documents._extract_pdf` is tested against a file pypdf actually has to
parse, rather than against a stand-in.

It is written by hand — a few hundred bytes of PDF syntax — rather than with a
PDF library, because the alternative is adding a document-generation dependency
to the application for the sake of one test fixture.

Regenerate after editing `PAGES`::

    uv run python evals/kb/make_pricing_pdf.py
"""

from pathlib import Path

# Deliberately overlaps `meridian_handbook.txt` on the topic (pricing) while
# carrying facts that appear *only* here. A question answered from this file is
# proof the PDF path works end to end, and not that the text file happened to
# cover it.
PAGES = [
    [
        "Meridian Fleet Systems - Price List and Discounts",
        "",
        "STANDARD LIST PRICES (per vehicle per month, billed annually)",
        "",
        "Starter        14 euros    up to 25 vehicles",
        "Standard       29 euros    minimum 25 vehicles",
        "Enterprise     44 euros    minimum 250 vehicles",
        "",
        "Route Planner add-on      6 euros per vehicle per month",
        "Driver Safety add-on      5 euros per vehicle per month",
        "Spare telematics dongle   39 euros one-off per vehicle",
        "",
        "VOLUME DISCOUNTS",
        "",
        "Discounts apply to the base plan only, never to add-ons.",
        "",
        "100 to 249 vehicles     5 percent",
        "250 to 499 vehicles     10 percent",
        "500 to 999 vehicles     15 percent",
        "1000 vehicles or more   quoted individually by the deal desk",
        "",
        "A sales representative may approve up to 10 percent without",
        "escalation. Anything deeper needs the regional director, and",
        "discounts over 25 percent need the chief revenue officer.",
    ],
    [
        "Meridian Fleet Systems - Price List and Discounts (page 2)",
        "",
        "MULTI-YEAR TERMS",
        "",
        "A two year commitment earns a further 8 percent off the base",
        "plan. A three year commitment earns 12 percent. Multi-year",
        "pricing is locked for the term and is not subject to the annual",
        "uplift.",
        "",
        "ANNUAL UPLIFT",
        "",
        "Single year contracts renew with a price uplift capped at 4",
        "percent, or the eurozone consumer price index, whichever is",
        "lower.",
        "",
        "PAYMENT TERMS",
        "",
        "Invoices are due 30 days from issue. Payment is by bank",
        "transfer or direct debit. Meridian accepts euros, pounds",
        "sterling and US dollars, and no other currency.",
        "",
        "Purchase orders are accepted from Enterprise customers only.",
        "There is no credit card payment option at any plan level.",
    ],
]

FONT_SIZE = 10.5
LINE_HEIGHT = 15
LEFT_MARGIN = 54
TOP_BASELINE = 744


def _escape(text: str) -> str:
    """Escape the three characters that are syntax inside a PDF string literal."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream(lines: list[str]) -> bytes:
    """Build one page's content stream: a text object, one Tj per line."""
    body = [f"BT /F1 {FONT_SIZE} Tf {LINE_HEIGHT} TL {LEFT_MARGIN} {TOP_BASELINE} Td"]
    for line in lines:
        # `Tj` shows the string, `T*` advances one line by the leading (TL).
        body.append(f"({_escape(line)}) Tj T*" if line else "T*")
    body.append("ET")
    return "\n".join(body).encode("latin-1")


def build(pages: list[list[str]]) -> bytes:
    """Assemble a complete PDF with one page per entry in `pages`."""
    page_count = len(pages)
    # Object numbering: 1 catalog, 2 page tree, 3 font, then for each page a
    # page object and its content stream.
    first_page_object = 4
    page_ids = [first_page_object + index * 2 for index in range(page_count)]

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{page_id} 0 R" for page_id in page_ids)
            + f"] /Count {page_count} >>"
        ).encode("latin-1"),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    for page_id, lines in zip(page_ids, pages, strict=True):
        stream = _content_stream(lines)
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>"
        ).encode("latin-1")
        objects[page_id + 1] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode("latin-1") + objects[number] + b"\nendobj\n"

    # The cross-reference table maps each object number to its byte offset.
    # Entry 0 is the mandatory free-list head.
    xref_offset = len(out)
    highest = max(objects)
    out += f"xref\n0 {highest + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for number in range(1, highest + 1):
        out += f"{offsets[number]:010d} 00000 n \n".encode("latin-1")
    out += f"trailer\n<< /Size {highest + 1} /Root 1 0 R >>\n".encode("latin-1")
    out += f"startxref\n{xref_offset}\n%%EOF\n".encode("latin-1")
    return bytes(out)


if __name__ == "__main__":
    target = Path(__file__).with_name("meridian_pricing.pdf")
    target.write_bytes(build(PAGES))
    print(f"Wrote {target} ({target.stat().st_size} bytes, {len(PAGES)} pages)")
