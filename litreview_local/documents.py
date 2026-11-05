"""Project documents: meta-PDFs a review leans on rather than reviews.

A protocol, a coding manual, the funder's guidelines, a prior review, a data
dictionary, the manuscript being written - documents that carry the rules of
the review instead of its findings. They are uploaded per project and read
with PyMuPDF at once; Claude can parse one properly on request (headings,
tables, equations as LaTeX, scanned pages) through a parse_pdf task.

Unlike papers they are never searched for, never scored and never annotated:
they are reference material, kept whole so an agent can quote the exact
wording of a rule.
"""
import logging
import re

from sqlalchemy.orm import Session

from . import models, paper_texts, pdfs

logger = logging.getLogger(__name__)

MAX_DOCUMENT_BYTES = 100 * 1024 * 1024  # same ceiling as a downloaded paper


class DocumentError(Exception):
    """The upload is not usable (not a PDF, too large, ...)."""


def store_upload(db: Session, project: models.Project, *, title: str,
                 description: str, filename: str,
                 data: bytes) -> models.ProjectDocument:
    """Persist the row and the file; the caller schedules the extraction."""
    if data[:5] != b"%PDF-":
        raise DocumentError("That file is not a PDF")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentError("PDF is larger than 100 MB")

    document = models.ProjectDocument(
        project_id=project.id,
        title=(title or "").strip() or (filename or "Document").rsplit(".", 1)[0],
        description=(description or "").strip() or None,
        filename=filename or None,
        status=models.DOC_PENDING,
    )
    db.add(document)
    db.commit()  # the id names the file

    path = pdfs.document_path(project.slug, document.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return document


def remove_files(project_slug: str, document_id: int) -> None:
    path = pdfs.document_path(project_slug, document_id)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # a stale file must not block the delete
        logger.warning("Could not remove %s: %s", path, exc)


def extract_document(document_id: int) -> None:
    """Background job: read the PDF's text layer and store it.

    A document Claude has parsed keeps Claude's text: re-reading the text
    layer would replace tables and equations with their flattened text. A
    scan with no text layer is marked failed with the remedy in the error -
    ask Claude to parse it.
    """
    from .db import SessionLocal

    db = SessionLocal()
    try:
        document = db.get(models.ProjectDocument, document_id)
        if document is None or document.project is None:
            return
        if document.reader == models.READER_CLAUDE and document.content:
            document.status = models.DOC_DONE
            db.commit()
            return
        source = pdfs.document_path(document.project.slug, document.id)
        try:
            if not source.exists():
                raise FileNotFoundError("the uploaded PDF is missing")
            pages = paper_texts.extract_pages(source)
            content = paper_texts.compose(pages)
            document.content = content
            document.page_count = len(pages)
            document.char_count = len(content)
            document.reader = models.READER_PYMUPDF
            document.status = models.DOC_DONE
            document.error = None
            logger.info("Read document %s: %d pages, %d chars",
                        document.id, len(pages), len(content))
        except paper_texts.NeedsOCRError as exc:
            document.status = models.DOC_FAILED
            document.error = str(exc)
            document.reader = models.READER_PYMUPDF
        except Exception as exc:
            logger.exception("Reading document %s failed", document_id)
            document.error = str(exc)
            if not document.content:
                document.status = models.DOC_FAILED
        db.commit()
    finally:
        db.close()


def store_parsed(db: Session, document: models.ProjectDocument,
                 pages: list[str]) -> None:
    """Keep Claude's transcription of a document. Caller commits."""
    content = paper_texts.compose(pages)
    document.content = content
    document.page_count = len(pages)
    document.char_count = len(content)
    document.reader = models.READER_CLAUDE
    document.status = models.DOC_DONE
    document.error = None


# ---------------------------------------------------------------- excerpting

_WS = re.compile(r"[ 	]+")
_HEADING = re.compile(r"^(#{1,6})[ 	]+(.+?)[ 	]*$", re.M)


def outline(content: str) -> list[dict]:
    """The markdown headings, with the offset each section starts at.

    A 64-page funder guide is ten get_document calls end to end; the outline
    is one, and it tells a reader which offset to jump to.
    """
    if not content:
        return []
    return [{"level": len(m.group(1)), "heading": m.group(2).strip(),
             "offset": m.start()} for m in _HEADING.finditer(content)]


def _section_at(headings: list[dict], offset: int) -> str | None:
    """The heading a passage sits under - context for a bare excerpt."""
    found = None
    for h in headings:
        if h["offset"] > offset:
            break
        found = h["heading"]
    return found


def _clip(content: str, at: int, length: int, context: int) -> tuple[int, int]:
    return max(0, at - context), min(len(content), at + length + context)


def _hit(content: str, headings: list[dict], start: int, end: int,
         match: str, at: int) -> dict:
    """`at` is where the match itself sits; `start`-`end` is the window around
    it. The section is the match's, not the window's - the context reaches
    back 400 characters and routinely crosses a heading."""
    text = _WS.sub(" ", content[start:end]).strip()
    return {
        "offset": start,
        # Where the match itself begins, which a paper's text needs to say
        # which page a hit is on.
        "match_offset": at,
        "section": _section_at(headings, at),
        "match": match,
        "excerpt": ("..." if start else "") + text + ("..." if end < len(content) else ""),
    }


def _phrase_hits(content: str, headings: list[dict], query: str, *,
                 context: int, limit: int) -> list[dict]:
    """The phrase, tolerating any whitespace between its words.

    The extractor hard-wraps its markdown, so "evaluation criteria" is as
    likely to arrive with a newline in the middle as with a space. A literal
    search misses those, which is a silent, total failure: the phrase is in
    the document and the reader is told it is not.
    """
    words = query.split()
    if not words:
        return []
    pattern = re.compile(r"\s+".join(re.escape(w) for w in words), re.I)
    found = []
    for m in pattern.finditer(content):
        start, end = _clip(content, m.start(), len(m.group(0)), context)
        found.append(_hit(content, headings, start, end, "phrase", m.start()))
        if len(found) >= limit:
            break
    return found


def _word_hits(content: str, headings: list[dict], query: str, *,
               context: int, limit: int) -> list[dict]:
    """Passages holding every word of the query, near each other.

    The fallback when the exact phrase is absent: a reader after "PI time
    commitment" should still be shown the paragraph that spells out the
    commitment expected of a principal investigator.
    """
    words = [w for w in {w.lower() for w in re.findall(r"\w+", query)} if len(w) > 2]
    if len(words) < 2:
        return []
    low = content.lower()
    counts = {w: low.count(w) for w in words}
    if not all(counts.values()):  # a word nothing contains cannot be bridged
        return []
    anchor = min(counts, key=counts.get)
    others = [w for w in words if w != anchor]

    found, at = [], low.find(anchor)
    while at >= 0 and len(found) < limit:
        start, end = _clip(content, at, len(anchor), context)
        window = low[start:end]
        if all(w in window for w in others):
            found.append(_hit(content, headings, start, end, "all words", at))
            at = low.find(anchor, end)  # windows must not overlap
        else:
            at = low.find(anchor, at + len(anchor))
    return found


def excerpts(content: str, query: str, *, context: int = 400,
             limit: int = 5) -> list[dict]:
    """Passages matching `query`, with surrounding context.

    Returns [{"offset", "section", "match", "excerpt"}] - the offset lets a
    caller read on from there with get_document(offset=...), and `match` says
    whether the phrase itself was found or only all of its words.
    """
    if not content or not query.strip():
        return []
    headings = outline(content)
    hits = _phrase_hits(content, headings, query.strip(),
                        context=context, limit=limit)
    if hits:
        return hits
    return _word_hits(content, headings, query.strip(),
                      context=context, limit=limit)
