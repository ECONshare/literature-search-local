"""Reading PDFs: the text of every paper and document, and Claude's parse.

Two readers:

- PyMuPDF reads the PDF's own text layer, locally, at once and for free.
  Every PDF that arrives is read this way, and the text is kept
  (models.PaperText), so annotation tasks, search_paper_text and the
  connector all work from it.
- Claude reads the pages themselves when asked (a parse_pdf task): a scanned
  paper with no text layer, or one whose tables and equations matter. It
  transcribes them to markdown - tables intact, equations as LaTeX - and
  that text then replaces the text layer's. Claude Code reads the PDF file
  directly; Claude Desktop is given page images through get_pdf_pages.

Every page opens with a <!-- page N --> marker, N being the PDF's own page
number - the numbering highlights and the viewer use. The number printed on
the page, which a citation needs (page 8 of an article that starts on page
71 is p. 78), is kept beside the text as its page labels (printed_page).
"""
import logging
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import claude_prompts, highlight, locks, models, pdfs, tasks

logger = logging.getLogger(__name__)

_PAGE_MARK = re.compile(r"<!-- page (\d+) -->")
# Where a page starts: a stored text's marker, or the <page N> tag a task's
# text is given in (research._annotate_text) - so a passage found in either
# is put on its page.
_PAGE_START = re.compile(r"<!-- page (\d+) -->|<page (\d+)>")

MIN_TEXT_CHARS = 200  # less than this across the whole PDF -> assume scanned
# Pages per parse_pdf task: every page comes back as markdown in one answer,
# and an answer has a length limit.
PARSE_PAGES_PER_TASK = 6
# Page images handed to Claude Desktop: enough resolution to read a footnote,
# little enough that a handful fit comfortably in one tool result.
PAGE_IMAGE_DPI = 110
MAX_IMAGES_PER_CALL = 6

# What ensure() did.
KEPT = "already kept"
READ = "read"
BUSY = "being read by something else"
NO_PDF = "no PDF"
NO_TEXT = "no readable text"
FAILED = "failed"


class NeedsOCRError(Exception):
    """The PDF has no usable text layer (a scan): Claude has to read it."""


# ------------------------------------------------------------- extraction

_NUMBERED_HEADING = re.compile(
    r"^(?:\d{1,2}(?:\.\d{1,2}){0,3}\.?|[IVX]{1,5}\.|[A-H]\.)\s+\S")
_KNOWN_HEADINGS = {
    "abstract", "introduction", "background", "literature review",
    "related work", "related literature", "theory", "hypotheses", "data",
    "data and methods", "method", "methods", "methodology", "empirical strategy",
    "results", "findings", "discussion", "conclusion", "conclusions",
    "concluding remarks", "limitations", "references", "bibliography",
    "appendix", "acknowledgements", "acknowledgments", "notes",
}


def _heading_level(spans: list[dict], text: str, body: float) -> int:
    """0 for body text, else the markdown level a line deserves - judged on
    its size and weight against the document's body text, and its shape."""
    words = text.split()
    if not words or len(text) > 120 or len(words) > 14:
        return 0
    numbered = bool(_NUMBERED_HEADING.match(text))
    if text[-1] in ".,;" and not numbered:
        return 0  # a sentence, or the tail of one
    if not any(ch.isalpha() for ch in text):
        return 0  # a page number, a table figure
    size = max(span.get("size", 0) for span in spans)
    bold = all(span.get("flags", 0) & 16 for span in spans)
    known = text.lower().strip(" .:0123456789") in _KNOWN_HEADINGS
    if size >= body * 1.5 and len(words) >= 1:
        return 1
    if size >= body * 1.18:
        return 2
    if bold and (numbered or known or (len(words) <= 8 and text[0].isupper())):
        return 3
    if numbered and known:
        return 3
    return 0


# A heading runs to a few lines at most; past that it is a bold paragraph.
_MAX_HEADING_LINES = 4
_MAX_HEADING_CHARS = 200
# Characters in one size that make it a page's own body size (an appendix
# set larger than the paper), rather than a title page's few large lines.
_PAGE_BODY_CHARS = 400


def _junk_heading(text: str, latin: bool) -> bool:
    """A line a garbled font set large or bold - "ò", "Õ", a run of
    private-use glyphs, a formula - is not a heading, however it looks:
    fewer than three letters, or letters not even half of what is there.
    `latin`: the document is written in plain Latin letters, so a heading
    with none of them is font noise too."""
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 3:
        return True
    if len(letters) < 0.5 * sum(1 for ch in text if not ch.isspace()):
        return True
    return latin and not any(ch.isascii() for ch in letters)


_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
          r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
# A line that is a date - "June 7, 2025", "7 June 2025", "2025-06-07",
# "Received: March 2020" - which a title page sets large or bold.
_DATE_LINE = re.compile(
    r"^(?:(?:received|accepted|revised|published|available online|this version"
    r"|first version|draft|version|updated|dated?)\b[\s:,]*)?"
    rf"(?:{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?\s+\d{{4}}"
    rf"|{_MONTH}\s+\d{{4}}"
    r"|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4})\.?$", re.I)
# "a) Data", "(iv) Results": an enumerator, whose bracket is not unbalanced.
_ENUMERATOR = re.compile(r"^\(?[0-9A-Za-z]{1,4}\)\s+")
# A reference-list entry set large: "Danaher, P. J. (2023). Optimal ...".
_REFERENCE = re.compile(r"\(\d{4}[a-z]?\)\s*[.,:]|^[A-Z][\w'’-]+, (?:[A-Z]\.\s?)+")
# A line of the text that markdown would take for a heading - an R or
# Python comment in a code listing - is escaped, so the outline does not.
_LOOKS_LIKE_HEADING = re.compile(r"^#{1,6}(?:[ \t]|$)")


def _balanced(text: str) -> bool:
    depth = 0
    for ch in _ENUMERATOR.sub("", text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _heading_shape(text: str) -> bool:
    """Could a whole heading read like this? Not a date ("June 7, 2025"),
    not a formula's fragment ("transformation function))"), not a reference
    entry or a list of keywords ("...; ..."), not a code comment ("# n"),
    and not starting in lower case ("methods") unless it is numbered ("2
    methods")."""
    if (_DATE_LINE.match(text) or not _balanced(text) or _REFERENCE.search(text)
            or ";" in text or text.startswith("#")):
        return False
    return (not text[:1].islower() or bool(_NUMBERED_HEADING.match(text))
            or bool(_ENUMERATOR.match(text)))


def _style(spans: list[dict]) -> tuple[float, bool]:
    """(size, bold) of a line, to tell one heading's lines from the next."""
    return (round(max(span.get("size", 0) for span in spans), 1),
            all(span.get("flags", 0) & 16 for span in spans))


def _continues(text: str, style: tuple[float, bool], latin: bool) -> bool:
    """A line _heading_level passed over that is still the heading's second
    line: bold like it, starting in lower case ("Results and" / "further
    discussion"), short, not a sentence's end."""
    return (style[1] and text[:1].islower() and len(text.split()) <= 8
            and text[-1] not in ".,;" and not _junk_heading(text, latin))


# A section heading set in the body's size runs to a dozen words; one that
# runs on in the same bold is a bold paragraph (an IEEE abstract).
_MAX_BOLD_HEADING_WORDS = 14


def _row_neighbours(entries: list[list]) -> set[int]:
    """Lines of a page that share their row with another line set well apart
    from them - table cells ("Author(s)  Source of endogeneity"), names side
    by side on a title page - which are not headings: any two such lines of
    one block, and two of different blocks when both looked like headings in
    the same style (the other column of a two-column page is body text, and
    does not count against a heading beside it)."""
    found = set()
    for i, a in enumerate(entries):
        if not a[0]:  # only a heading can be undone; compare from those
            continue
        for j, b in enumerate(entries):
            if j == i or a[3] is None or b[3] is None:
                continue
            if a[4] != b[4] and not (b[0] and a[2] == b[2]):
                continue
            (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = a[3][:4], b[3][:4]
            overlap = min(ay1, by1) - max(ay0, by0)
            size = max((a[2] or (0,))[0], (b[2] or (0,))[0], 1)
            if (overlap > 0.5 * min(ay1 - ay0, by1 - by0)
                    and max(bx0 - ax1, ax0 - bx1) > 2 * size):
                found.update((i, j))
    return found


def _merge_headings(lines: list[list], latin: bool) -> list[tuple[int, str]]:
    """A page's lines as (level, text), each heading set over several lines
    made one: a title over two lines is one entry in the outline, not two.

    `lines` is [[level, text, style, box, block]] in reading order.
    Consecutive heading lines of the same level and style, the next set just
    under the last, are one heading - unless the next is numbered (a new
    section). A centred title's lines are often blocks of their own, so the
    next block's first line may continue a heading if it is under it. A
    heading that, whole, does not read like one (_heading_shape) goes back
    to being text, line by line; so does a bold "heading" that runs on in
    the same bold past a dozen words - a bold paragraph.
    """
    groups: list[dict] = []
    for level, text, style, box, block in lines:
        prev = groups[-1] if groups else None
        if prev is not None and prev["level"] and style is not None \
                and prev["style"] is not None \
                and abs(prev["style"][0] - style[0]) <= 0.5 \
                and prev["style"][1] == style[1] \
                and (box is None or prev["box"] is None or (
                    box[1] >= prev["box"][1] + 0.3 * style[0]
                    and box[1] - prev["box"][3] <= 0.8 * style[0]
                    and (block == prev["block"]
                         or min(box[2], prev["box"][2]) > max(box[0], prev["box"][0])))):
            joins = ((level == prev["level"] and not _NUMBERED_HEADING.match(text))
                     or (level == 0 and _continues(text, style, latin)))
            words = len(prev["text"].split()) + len(text.split())
            if (prev["level"] == 3 and style[1] and block == prev["block"]
                    and prev["text"].lower().strip(" .:0123456789") not in _KNOWN_HEADINGS
                    and ((joins and words > _MAX_BOLD_HEADING_WORDS)
                         or (not joins and len(text.split()) > 8))):
                # The same bold runs on: a bold paragraph, not a heading.
                prev["paragraph"] = True
                groups.append({"level": 0, "text": text, "style": style, "box": box,
                               "block": block, "lines": [text]})
                continue
            if (joins and len(prev["lines"]) < _MAX_HEADING_LINES
                    and len(prev["text"]) + len(text) < _MAX_HEADING_CHARS
                    and not prev["text"].endswith((".", ":"))):
                # A heading is seldom hyphenated: a line-end hyphen is far
                # more likely Self-|Employment's than a broken word's, so it
                # stays, with no space after it (matching reads both alike).
                joiner = "" if prev["text"].endswith("-") else " "
                prev["text"] = f"{prev['text']}{joiner}{text}"
                prev["lines"].append(text)
                prev["box"], prev["block"] = box, block
                continue
        groups.append({"level": level, "text": text, "style": style, "box": box,
                       "block": block, "lines": [text]})
    out: list[tuple[int, str]] = []
    for group in groups:
        if (group["level"] and not group.get("paragraph")
                and _heading_shape(group["text"])):
            out.append((group["level"], group["text"]))
        else:
            out.extend((0, text) for text in group["lines"])
    return out


def _text_blocks(page_data: dict):
    return (block for block in page_data.get("blocks", []) if block.get("type") == 0)


def _page_dicts(doc) -> list[dict]:
    flags = highlight.text_flags()
    return [page.get_text("dict", flags=flags) for page in doc]


def _pages_with_headings(doc, data: list[dict] | None = None) -> list[str]:
    """Per-page text with headings marked as markdown, so a paper read from
    its text layer still has an outline and every passage a section; every
    line cleaned (highlight.clean_text), the Hangul repair decided once for
    the whole document. The body size is the size most of the document's
    characters are set in. `data`: the pages' get_text("dict"), if read."""
    flags = highlight.text_flags()
    if data is None:
        data = _page_dicts(doc)
    sizes: Counter = Counter()
    raw: list[str] = []
    for page_data in data:
        for block in _text_blocks(page_data):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    raw.append(span.get("text", ""))
                    chars = len(raw[-1].strip())
                    if chars:
                        sizes[round(span.get("size", 0), 1)] += chars
    whole = "".join(raw)
    korean = highlight.is_korean(whole)
    if not sizes:
        return [highlight.clean_text(page.get_text(flags=flags), korean=korean)
                for page in doc]
    body = sizes.most_common(1)[0][0]
    letters = [ch for ch in whole if ch.isalpha()]
    latin = bool(letters) and sum(ch.isascii() for ch in letters) >= 0.8 * len(letters)
    out = []
    for page_data in data:
        # Headings stand out from the text around them: a page set larger
        # than the rest (an appendix in 12pt) has no more headings for it.
        page_sizes: Counter = Counter()
        for block in _text_blocks(page_data):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    page_sizes[round(span.get("size", 0), 1)] += len(span.get("text", "").strip())
        page_body = body
        if page_sizes:
            size, chars = page_sizes.most_common(1)[0]
            if chars >= _PAGE_BODY_CHARS:
                page_body = max(body, size)
        lines: list[list] = []  # the page's lines, one block after another
        for block_no, block in enumerate(_text_blocks(page_data)):
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
                text = highlight.clean_text(
                    "".join(s.get("text", "") for s in line.get("spans", [])),
                    korean=korean).strip()
                if not text:
                    continue
                # A heading runs across the page, not up the side of a
                # rotated table.
                across = abs(line.get("dir", (1, 0))[0]) > 0.99
                level = _heading_level(spans, text, page_body) if spans and across else 0
                if level and _junk_heading(text, latin):
                    level = 0
                lines.append([level, text, _style(spans) if spans else None,
                              line.get("bbox"), block_no])
        for k in _row_neighbours(lines):
            lines[k][0] = 0
        out.append("\n".join(
            f"{'#' * level} {text}" if level
            else ("\\" + text if _LOOKS_LIKE_HEADING.match(text) else text)
            for level, text in _merge_headings(lines, latin)))
    return out


class PdfText(NamedTuple):
    pages: list[str]
    page_labels: dict[str, str]


def read_pdf(pdf_path) -> PdfText:
    """A PDF's text layer, page by page - headings marked as markdown, the
    text cleaned (highlight.clean_text) - and the number printed on each
    page (detect_page_labels), from one reading. NeedsOCRError for scans."""
    import pymupdf as fitz  # PyMuPDF

    flags = highlight.text_flags()
    with fitz.open(pdf_path) as doc:
        data = None
        try:
            data = _page_dicts(doc)
            pages = _pages_with_headings(doc, data)
        except Exception:
            logger.exception("Heading detection failed for %s - plain text", pdf_path)
            raw = [page.get_text(flags=flags) for page in doc]
            korean = highlight.is_korean("".join(raw))
            pages = [highlight.clean_text(text, korean=korean) for text in raw]
        try:
            labels = detect_page_labels(doc, data)
        except Exception:
            logger.exception("Could not work out the printed page numbers of %s",
                             pdf_path)
            labels = {}
    if sum(len(t.strip()) for t in pages) < MIN_TEXT_CHARS:
        raise NeedsOCRError("This PDF has no usable text layer - it is "
                            "probably a scan. Ask Claude to parse it.")
    return PdfText(pages, labels)


def extract_pages(pdf_path) -> list[str]:
    """Per-page text from the PDF's text layer, headings marked as markdown,
    cleaned; NeedsOCRError for scans."""
    return read_pdf(pdf_path).pages


# ---------------------------------------------------- printed page numbers

# Where a running header or footer sits: the first or last few rows of a
# page, within this share of its height from the edge.
_MARGIN = 0.15
_EDGE_ROWS = 3
_STANDALONE_NUMBER = re.compile(r"[0-9]{1,4}")


_PUNCTUATION = "()[]-–—|·•∣"


def _margin_numbers(page_data: dict) -> set[int]:
    """The numbers that open or close a line in one of a page's first or
    last rows - where a running header or footer prints the page number
    ("78  JOURNAL OF ...", "... ET AL.  78", "443 | Page", a footer "78"
    alone). A number inside a line is the text's, and "62:71-90" (a page
    range) or "2019," is not standalone; "- 78 -" is."""
    height = page_data.get("height") or 0
    lines = []
    for block in _text_blocks(page_data):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip():
                x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
                lines.append(((y0 + y1) / 2, text))
    rows: list[list] = []  # [vertical centre, [line texts]] - a row across columns
    for centre, text in sorted(lines, key=lambda item: item[0]):
        if rows and abs(rows[-1][0] - centre) <= 3:
            rows[-1][1].append(text)
        else:
            rows.append([centre, [text]])
    numbers = set()
    for centre, texts in rows[:_EDGE_ROWS] + rows[-_EDGE_ROWS:]:
        if height and _MARGIN * height < centre < (1 - _MARGIN) * height:
            continue
        for text in texts:
            words = [w.strip(_PUNCTUATION) for w in text.split()]
            words = [w for w in words if w]
            for word in {words[0], words[-1]} if words else ():
                if _STANDALONE_NUMBER.fullmatch(word):
                    numbers.add(int(word))
    return numbers


def _running_numbers(data: list[dict]) -> tuple[int, int, int] | None:
    """(offset, first, last) when on most pages a margin number equals the
    PDF page number plus one constant offset - pages `first` to `last`
    being those that show it - else None."""
    support: dict[int, set[int]] = {}
    for number, page_data in enumerate(data, start=1):
        for printed in _margin_numbers(page_data):
            support.setdefault(printed - number, set()).add(number)
    if not support:
        return None
    ranked = sorted(support.items(), key=lambda item: -len(item[1]))
    offset, pages = ranked[0]
    runner_up = len(ranked[1][1]) if len(ranked) > 1 else 0
    if len(pages) < max(2, (len(data) + 1) // 2) or len(pages) <= runner_up:
        return None
    return offset, min(pages), max(pages)


def detect_page_labels(doc, data: list[dict] | None = None) -> dict[str, str]:
    """The number printed on each page of an open PDF: {"8": "78", ...},
    keyed by PDF page number (from 1), for the pages it is known for.

    The PDF's own page labels (page.get_label()) when it defines them.
    Otherwise - and when its labels only count the pages 1, 2, 3 while the
    pages themselves print other numbers - the running headers and footers:
    on most pages a standalone number in the first or last rows equals the
    page's index plus one constant offset. Pages before the first that
    shows a number are numbered by the same offset (an article's first page
    seldom prints its own); pages after the last that shows one are not (an
    appendix may number itself A1, A2). `data`: the pages' get_text("dict"),
    if already read.
    """
    labels: dict[str, str] = {}
    for index in range(len(doc)):
        try:
            label = (doc[index].get_label() or "").strip()
        except Exception:
            label = ""
        if label:
            labels[str(index + 1)] = label
    plain = all(value == key for key, value in labels.items())
    if labels and not plain:
        return labels
    found = _running_numbers(data if data is not None else _page_dicts(doc))
    if found is None or (labels and found[0] == 0):
        return labels
    offset, _first, last = found
    return {str(page): str(page + offset) for page in range(1, last + 1)
            if page + offset >= 1}


def read_page_labels(pdf_path) -> dict[str, str] | None:
    """detect_page_labels for a PDF file; None when there is no file to read
    or it cannot be read (so that it is tried again later)."""
    import pymupdf as fitz

    path = Path(pdf_path)
    if not path.exists():
        return None
    try:
        with fitz.open(path) as doc:
            return detect_page_labels(doc)
    except Exception as exc:
        logger.warning("Could not read the page numbers of %s: %s", path, exc)
        return None


def printed_page(text: "models.PaperText | dict | None", pdf_page) -> str | None:
    """The number printed on PDF page `pdf_page` of a paper - "78" for the
    8th page of an article printed from page 71, as an APA citation needs
    it - from its kept text's page labels (or a labels dict); None when it
    is not known."""
    labels = text if isinstance(text, dict) else getattr(text, "page_labels", None)
    if not labels or pdf_page is None:
        return None
    try:
        return labels.get(str(int(pdf_page)))
    except (TypeError, ValueError):
        return None


def page_count(pdf_path) -> int:
    """Pages in the PDF, or 0 if it cannot be opened."""
    import pymupdf as fitz

    try:
        with fitz.open(pdf_path) as doc:
            return doc.page_count
    except Exception as exc:
        logger.warning("Could not count the pages of %s: %s", pdf_path, exc)
        return 0


def render_page_png(pdf_path, page_number: int, dpi: int = PAGE_IMAGE_DPI) -> bytes:
    """One page as a PNG, for a client that cannot read the PDF itself."""
    import pymupdf as fitz

    with fitz.open(pdf_path) as doc:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(f"page {page_number} is outside 1-{doc.page_count}")
        pixmap = doc[page_number - 1].get_pixmap(dpi=dpi)
        return pixmap.tobytes("png")


# ---------------------------------------------------------------- the text

def compose(pages: list[str]) -> str:
    """One text from per-page strings, each page opening with its marker,
    each page cleaned (highlight.clean_text - a no-op on a text layer read
    by extract_pages, which cleaned it already; Claude's transcription needs
    no Hangul repair, so none is made here)."""
    return "\n\n".join(
        f"<!-- page {number} -->\n{highlight.clean_text(text, korean=True).strip()}"
        for number, text in enumerate(pages, start=1))


def split(content: str) -> dict[int, str]:
    """{page number: that page's text} from a composed text."""
    marks = list(_PAGE_MARK.finditer(content or ""))
    pages: dict[int, str] = {}
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
        pages[int(mark.group(1))] = content[mark.end():end].strip()
    if not marks and content:
        pages[1] = content.strip()
    return pages


def page_at(content: str, offset: int) -> int | None:
    """The PDF page the character at `offset` sits on - in a stored text
    (<!-- page N --> markers) or a task's text (<page N> tags)."""
    page = None
    for mark in _PAGE_START.finditer(content or ""):
        if mark.start() > offset:
            break
        page = int(mark.group(1) or mark.group(2))
    return page


def page_offset(content: str, page: int) -> int | None:
    """Where PDF page `page` begins in the text, or None."""
    for mark in _PAGE_MARK.finditer(content):
        if int(mark.group(1)) == page:
            return mark.start()
    return None


def section_at(content: str, offset: int) -> str | None:
    """The heading the character at `offset` sits under, if the text has
    headings (Claude's parse always does; a text layer sometimes)."""
    from . import documents

    return documents._section_at(documents.outline(content), offset)


def summaries(db: Session, paper_ids: list[int]) -> dict[int, dict]:
    """paper_id -> {"reader", "pages", "characters"} for those with a text."""
    if not paper_ids:
        return {}
    rows = db.execute(
        select(models.PaperText.paper_id, models.PaperText.reader,
               models.PaperText.page_count, models.PaperText.char_count)
        .where(models.PaperText.paper_id.in_(paper_ids))
    ).all()
    return {paper_id: {"reader": reader, "pages": pages, "characters": chars}
            for paper_id, reader, pages, chars in rows}


def store(db: Session, paper: models.Paper, pages: list[str], reader: str,
          *, commit: bool = True,
          page_labels: dict[str, str] | None = None) -> models.PaperText | None:
    """Keep the text a read of this paper produced.

    Claude's parse is not replaced by the text layer's: it has the tables and
    equations, and it exists because somebody asked for it. A read that found
    no text at all keeps nothing.

    The printed page numbers go with it: `page_labels` when the read worked
    them out (read_pdf), else those already kept, else read from the PDF.
    """
    if not any(page.strip() for page in pages):
        return None
    row = db.get(models.PaperText, paper.id)
    if (row is not None and row.reader == models.READER_CLAUDE
            and reader != models.READER_CLAUDE):
        return row
    if row is None:
        row = models.PaperText(paper_id=paper.id)
        db.add(row)
    row.content = compose(pages)
    row.reader = reader
    row.page_count = len(pages)
    row.char_count = len(row.content)
    if page_labels is not None:
        row.page_labels = page_labels
    elif row.page_labels is None and paper.project is not None:
        row.page_labels = read_page_labels(pdfs.original_path(paper.project.slug,
                                                              paper.id))
    if commit:
        db.commit()
    return row


def text_of(db: Session, paper: models.Paper) -> models.PaperText | None:
    return db.get(models.PaperText, paper.id)


def ensure(paper_id: int, *, wait: bool = True) -> str:
    """Read one paper's PDF with PyMuPDF and keep its text, unless kept."""
    lock = locks.for_paper(paper_id)
    if not lock.acquire(blocking=wait):
        return BUSY
    try:
        return _ensure(paper_id)
    finally:
        lock.release()


def _ensure(paper_id: int) -> str:
    from .db import SessionLocal

    db = SessionLocal()
    try:
        paper = db.get(models.Paper, paper_id)
        if paper is None or paper.project is None:
            return NO_PDF
        if db.get(models.PaperText, paper.id) is not None:
            return KEPT
        source = pdfs.original_path(paper.project.slug, paper.id)
        if not source.exists():
            return NO_PDF
        try:
            read = read_pdf(source)
        except NeedsOCRError as exc:
            logger.info("Paper %s has no readable text: %s", paper_id, exc)
            return NO_TEXT
        if store(db, paper, read.pages, models.READER_PYMUPDF,
                 page_labels=read.page_labels) is None:
            return NO_TEXT
        logger.info("Kept the text of paper %s (%d pages)", paper_id, len(read.pages))
        return READ
    except Exception:
        logger.exception("Could not keep the text of paper %s", paper_id)
        db.rollback()
        return FAILED
    finally:
        db.close()


def download_and_read(paper_ids: list[int]) -> None:
    """Background job: fetch the PDFs, then keep the text of each that came."""
    pdfs.download_for_papers(paper_ids)
    for paper_id in paper_ids:
        ensure(paper_id)


def fill_missing() -> Counter:
    """Startup: keep the text of every PDF here that has none yet. Local and
    free, so it can run at every start."""
    from .db import SessionLocal

    db = SessionLocal()
    try:
        ids = db.execute(
            select(models.Paper.id)
            .where(models.Paper.pdf_status == models.PDF_DOWNLOADED,
                   models.Paper.id.not_in(select(models.PaperText.paper_id)))
            .order_by(models.Paper.id)
        ).scalars().all()
    finally:
        db.close()
    stats: Counter = Counter()
    for paper_id in ids:
        stats[ensure(paper_id)] += 1
    if ids:
        logger.info("Paper texts filled in at startup: %s",
                    ", ".join(f"{count} {outcome}"
                              for outcome, count in sorted(stats.items())))
    try:
        fill_page_labels()
    except Exception:
        logger.exception("Working out printed page numbers at startup failed")
    return stats


def fill_page_labels() -> int:
    """Startup: the printed page numbers of every kept text that has none
    yet - texts kept before they were worked out, or whose PDF could not be
    read then. Returns how many were filled in."""
    from .db import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(
            select(models.PaperText.paper_id, models.Project.slug)
            .join(models.Paper, models.Paper.id == models.PaperText.paper_id)
            .join(models.Project, models.Project.id == models.Paper.project_id)
            .where(models.PaperText.page_labels.is_(None))
            .order_by(models.PaperText.paper_id)
        ).all()
    finally:
        db.close()
    filled = 0
    for paper_id, slug in rows:
        labels = read_page_labels(pdfs.original_path(slug, paper_id))
        if labels is None:
            continue
        db = SessionLocal()
        try:
            db.execute(update(models.PaperText)
                       .where(models.PaperText.paper_id == paper_id,
                              models.PaperText.page_labels.is_(None))
                       .values(page_labels=labels))
            db.commit()
            filled += 1
        except Exception:
            logger.exception("Could not keep the page numbers of paper %s", paper_id)
            db.rollback()
        finally:
            db.close()
    if filled:
        logger.info("Printed page numbers worked out for %d kept text(s)", filled)
    return filled


# -------------------------------------------------------- Claude's parse

def _target_pdf(db: Session, task: models.ClaudeTask) -> Path | None:
    if task.paper_id:
        paper = db.get(models.Paper, task.paper_id)
        if paper is not None and paper.project is not None:
            return pdfs.original_path(paper.project.slug, paper.id)
    if task.document_id:
        document = db.get(models.ProjectDocument, task.document_id)
        if document is not None and document.project is not None:
            return pdfs.document_path(document.project.slug, document.id)
    return None


def parse_requested(db: Session, *, paper: models.Paper | None = None,
                    document: models.ProjectDocument | None = None) -> bool:
    """Is a parse by Claude already waiting for this paper or document?"""
    where = ({"paper_id": paper.id} if paper is not None
             else {"document_id": document.id})
    return bool(tasks.live_tasks(db, kinds=[models.TASK_PARSE_PDF], **where))


def request_parse(db: Session, *, paper: models.Paper | None = None,
                  document: models.ProjectDocument | None = None,
                  priority: int | None = None) -> int:
    """Queue Claude's parse of a PDF, a few pages per task. Returns how many
    tasks were queued (0 if one is already waiting or there is no PDF).
    Caller commits."""
    if paper is not None:
        if paper.project is None:
            return 0
        source = pdfs.original_path(paper.project.slug, paper.id)
        where = {"paper_id": paper.id}
        project_id = paper.project_id
    else:
        source = pdfs.document_path(document.project.slug, document.id)
        where = {"document_id": document.id}
        project_id = document.project_id
    if not source.exists() or parse_requested(db, paper=paper, document=document):
        return 0
    total = page_count(source)
    if total <= 0:
        return 0
    reading = uuid.uuid4().hex[:10]
    chunks = [(start, min(total, start + PARSE_PAGES_PER_TASK - 1))
              for start in range(1, total + 1, PARSE_PAGES_PER_TASK)]
    for part, (first, last) in enumerate(chunks, start=1):
        tasks.create(db, models.TASK_PARSE_PDF, project_id=project_id,
                     payload={"pages": [first, last], "part": part,
                              "parts": len(chunks), "total_pages": total,
                              "reading": reading},
                     priority=priority, **where)
    return len(chunks)


def _parse_input(db: Session, task: models.ClaudeTask) -> dict:
    first, last = task.payload["pages"]
    source = _target_pdf(db, task)
    what = {}
    if task.paper_id:
        paper = db.get(models.Paper, task.paper_id)
        what = {"paper_id": paper.id, "title": paper.title} if paper else {}
    elif task.document_id:
        document = db.get(models.ProjectDocument, task.document_id)
        what = ({"document_id": document.id, "title": document.title}
                if document else {})
    return {
        "document": what,
        "pages_to_transcribe": list(range(first, last + 1)),
        "part": f"{task.payload['part']} of {task.payload['parts']} "
                f"(the PDF has {task.payload['total_pages']} pages)",
        "how_to_see_the_pages": (
            "If you can read files on this computer (Claude Code), read the "
            f"PDF at pdf_path with your Read tool, pages {first}-{last}. "
            "Otherwise call get_pdf_pages with this task_id and "
            f"pages=\"{first}-{last}\" to receive the page images "
            f"(at most {MAX_IMAGES_PER_CALL} per call)."),
        "pdf_path": str(source) if source else None,
    }


def _parse_check(db: Session, task: models.ClaudeTask, output: dict) -> list[str]:
    first, last = task.payload["pages"]
    wanted = set(range(first, last + 1))
    given = {int(p["page"]) for p in output["pages"]}
    errors = []
    stray = sorted(given - wanted)
    if stray:
        errors.append(f"pages {stray} are not part of this task (it covers "
                      f"{first}-{last})")
    missing = sorted(wanted - given)
    if missing:
        errors.append(f"pages {missing} are missing - give every page, with an "
                      "empty markdown string for a blank one")
    return errors


def _parse_apply(db: Session, task: models.ClaudeTask, output: dict) -> dict:
    reading = task.payload.get("reading")
    where = ({"paper_id": task.paper_id} if task.paper_id
             else {"document_id": task.document_id})
    remaining = [t for t in tasks.live_tasks(db, kinds=[models.TASK_PARSE_PDF], **where)
                 if (t.payload or {}).get("reading") == reading]
    if remaining:
        return {"saved": f"pages {task.payload['pages'][0]}-{task.payload['pages'][1]}",
                "still_to_parse": len(remaining)}
    # Every part is in: assemble the document in page order.
    parts = db.execute(
        select(models.ClaudeTask).where(
            models.ClaudeTask.kind == models.TASK_PARSE_PDF,
            models.ClaudeTask.status == models.TASK_DONE,
            *(getattr(models.ClaudeTask, k) == v for k, v in where.items()))
    ).scalars().all()
    pages: dict[int, str] = {}
    for part in parts:
        if (part.payload or {}).get("reading") != reading:
            continue
        for entry in (part.result or {}).get("pages", []):
            pages[int(entry["page"])] = entry.get("markdown") or ""
    total = int(task.payload.get("total_pages") or (max(pages) if pages else 0))
    ordered = [pages.get(n, "") for n in range(1, total + 1)]
    if task.paper_id:
        paper = db.get(models.Paper, task.paper_id)
        if paper is None:
            return {"saved": "the paper is gone"}
        stored = store(db, paper, ordered, models.READER_CLAUDE, commit=False)
        if stored is None:
            return {"saved": "no text on any page - nothing kept"}
        from . import research  # research builds on this module

        queued = research.paper_text_ready(db, paper)
        return {"saved": f"the whole paper ({total} pages) as markdown",
                "next": (f"queued {queued} reading task(s) that were waiting "
                         "for this text") if queued else None}
    document = db.get(models.ProjectDocument, task.document_id)
    if document is None:
        return {"saved": "the document is gone"}
    from . import documents

    documents.store_parsed(db, document, ordered)
    return {"saved": f"the whole document ({total} pages) as markdown"}


def _parse_describe(task: models.ClaudeTask) -> str:
    first, last = task.payload["pages"]
    target = (f"paper {task.paper_id}" if task.paper_id
              else f"document {task.document_id}")
    return f"Transcribe pages {first}-{last} of {target}"


def _parse_still_needed(db: Session, task: models.ClaudeTask) -> bool:
    source = _target_pdf(db, task)
    return source is not None and source.exists()


def _parse_dropped(db: Session, task: models.ClaudeTask) -> None:
    """A part could not be done: withdraw the rest of this reading, and let
    whatever waited for the text go on without it."""
    reading = (task.payload or {}).get("reading")
    where = ({"paper_id": task.paper_id} if task.paper_id
             else {"document_id": task.document_id})
    for other in tasks.live_tasks(db, kinds=[models.TASK_PARSE_PDF], **where):
        if (other.payload or {}).get("reading") == reading:
            other.status = models.TASK_CANCELLED
            other.error = "Another part of this parse was given up on"
    if task.paper_id:
        paper = db.get(models.Paper, task.paper_id)
        if paper is not None:
            from . import research

            research.paper_text_failed(db, paper)


tasks.register(tasks.TaskKind(
    name=models.TASK_PARSE_PDF,
    title="Transcribe PDF pages to markdown",
    instructions=claude_prompts.PARSE_PDF_INSTRUCTIONS,
    schema=claude_prompts.PARSE_PDF_SCHEMA,
    build_input=_parse_input,
    apply=_parse_apply,
    describe=_parse_describe,
    check=_parse_check,
    still_needed=_parse_still_needed,
    on_dropped=_parse_dropped,
    priority=35,
))
