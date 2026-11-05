"""PDF storage and retrieval.

Folder layout, one folder per project inside <data_dir>/pdfs:

    {project_slug}/original/{paper_id}.pdf       the PDF as downloaded/uploaded
    {project_slug}/annotated/a{annotation}.pdf   one highlighted copy per annotation
    {project_slug}/documents/{document_id}.pdf   project documents

original/ files are immutable once downloaded; annotated/ files are always
regenerated from the original.
"""
import logging
from pathlib import Path

import httpx

from . import locks, models
from .config import settings
from .pipeline.http import USER_AGENT
from .pipeline.unpaywall import best_oa_url

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = 60.0
MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB safety cap


def original_path(project_slug: str, paper_id: int) -> Path:
    return Path(settings.pdf_dir) / project_slug / "original" / f"{paper_id}.pdf"


def annotation_path(project_slug: str, annotation_id: int) -> Path:
    """The highlighted copy for ONE annotation.

    Highlights are per research question - the same paper answers two
    questions with different passages - while the PDF it is drawn on is per
    paper. Keying the output by annotation is what stops the second question
    to read a paper from overwriting the highlights the first question's
    citations point at. The `a` prefix keeps these apart from the legacy
    per-paper files in the same folder.
    """
    return (Path(settings.pdf_dir) / project_slug / "annotated"
            / f"a{annotation_id}.pdf")


def document_path(project_slug: str, document_id: int) -> Path:
    """An uploaded meta-PDF (protocol, guidelines, prior review, ...)."""
    return Path(settings.pdf_dir) / project_slug / "documents" / f"{document_id}.pdf"


def _looks_like_pdf(content: bytes) -> bool:
    return content[:5] == b"%PDF-"


def download_pdf(paper: models.Paper, *, client: httpx.Client | None = None) -> tuple[bool, str | None]:
    """Fetch the open-access PDF for a paper into the project folder.

    Returns (ok, error). Tries paper.oa_url, resolving it via Unpaywall first
    if missing. Does nothing if the file already exists.
    """
    dest = original_path(paper.project.slug, paper.id)
    if dest.exists():
        return True, None

    # Two questions running at once can both reach the same paper; only one of
    # them should fetch it (see locks). By the time the second gets the lock
    # the file is usually already there.
    with locks.for_paper(paper.id):
        if dest.exists():
            return True, None
        return _fetch(paper, dest, client)


def _fetch(paper: models.Paper, dest: "Path",
           client: httpx.Client | None) -> tuple[bool, str | None]:
    url = paper.oa_url
    if not url and paper.doi:
        url = best_oa_url(paper.doi)
        if url:
            paper.oa_url = url
    if not url:
        return False, "no open-access URL found"

    owns_client = client is None
    c = client or httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True,
                               headers={"User-Agent": USER_AGENT})
    try:
        resp = c.get(url)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code} from {url}"
        content = resp.content
        if len(content) > MAX_PDF_BYTES:
            return False, "file too large"
        if not _looks_like_pdf(content):
            return False, "URL did not return a PDF (probably a landing page)"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        return True, None
    except httpx.HTTPError as exc:
        return False, f"download failed: {exc}"
    finally:
        if owns_client:
            c.close()


def download_for_papers(paper_ids: list[int]) -> None:
    """Background task: download PDFs for the given papers, updating status."""
    from .db import SessionLocal

    db = SessionLocal()
    try:
        with httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT}) as client:
            for pid in paper_ids:
                paper = db.get(models.Paper, pid)
                if paper is None or paper.project is None:
                    continue
                ok, error = download_pdf(paper, client=client)
                paper.pdf_status = models.PDF_DOWNLOADED if ok else models.PDF_FAILED
                paper.pdf_error = error
                db.commit()
                logger.info("PDF for paper %s: %s", pid, "ok" if ok else error)
    finally:
        db.close()
