"""Adding an article from its PDF: finding out what the PDF is.

An uploaded PDF arrives as an article with nothing but a file name. Every one
goes through the same procedure as in the server app - with each step that
needs a model done by the user's own Claude, as a task in the queue:

S1  (app)    Read the PDF's text layer and keep the text. A scan with no
             text layer is transcribed by Claude first (a parse_pdf task).
S2  (Claude) identify_pdf: is this scholarly material likely to have a record
             in the scholarly databases, or not? And what does it say about
             itself - title, authors, year, venue, its own DOI - plus the
             search string for S3.
S3  (app)    Scholarly: a DOI printed on the PDF is looked up first, and kept
             only if the record's title is the PDF's. Otherwise OpenAlex,
             Semantic Scholar and Crossref are searched with that string, and
    (Claude) match_record: which record is this document, in this version?
             A fixed rule (match_rule) checks the pick; one it cannot confirm
             is put to the user as "Is this it?".
S4  (Claude) find_web_page: not scholarly, or nothing matched - one web page
    (app)    about the document, which the app then opens and checks for the
             document's title. A page that checks out becomes the article's
             only reference; otherwise it has no external source.

Each step writes one line into the identification's log, so the card can say
why the article ended where it did. An article that turns out to be in the
project already does not stay a duplicate: the PDF goes onto that article's
card and the upload's own row is removed.

Claude's answers only record decisions; anything that deletes rows or goes to
the network runs afterwards in the background (worker.py), so a submission
never has to wait on a web page.
"""
import html
import ipaddress
import logging
import re
import shutil
import socket
import threading
import unicodedata
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import locks, models, paper_texts, pdfs, research, services, tasks, worker
from .db import SessionLocal
from .pipeline import SOURCE_LABELS, SOURCES
from .pipeline.dedupe import dedupe
from .pipeline.http import USER_AGENT
from .pipeline.normalize import clean_doi, title_key

logger = logging.getLogger(__name__)

MAX_FILES = 20            # per upload; a slip of the file picker, not a library
FIRST_PAGES = 3           # what the classification reads
FIRST_PAGES_CHARS = 12000
PER_SOURCE = 10           # a targeted lookup, like research.find_candidates
MAX_CANDIDATES = 15       # what Claude is shown to pick from
MAX_DOI_LOOKUPS = 3
# Finished uploads stay in the panel this long, so a reload right after one
# finishes still shows how it ended. Their cards say it for good.
RECENT = timedelta(minutes=30)


class UploadError(ValueError):
    """The file cannot become an article. The message is for the person."""


class IdentifyError(Exception):
    """A step could not be done. The message is shown on the upload."""


# ------------------------------------------------------------------ upload

def title_from_filename(filename: str) -> str:
    """What to call an article before anything is known about it."""
    stem = Path(filename or "").stem
    title = " ".join(stem.replace("_", " ").split())
    return title[:300] or "Uploaded PDF"


def start(db: Session, project: models.Project, filename: str,
          content: bytes) -> models.Paper:
    """Store one uploaded PDF as a new article and open its identification.
    Commits. The caller schedules run() for it."""
    if content[:5] != b"%PDF-":
        raise UploadError(f"{filename or 'That file'} is not a PDF.")
    if len(content) > pdfs.MAX_PDF_BYTES:
        raise UploadError(f"{filename or 'That file'} is larger than "
                          f"{pdfs.MAX_PDF_BYTES // (1024 * 1024)} MB.")
    paper = models.Paper(
        project_id=project.id, title=title_from_filename(filename),
        authors=[], issns=[], source_ids={},
        # Uploading it is choosing it: the same as ticking it for download.
        status=models.STATUS_INCLUDED, stage=models.STAGE_RETRIEVAL,
        pdf_status=models.PDF_DOWNLOADED)
    db.add(paper)
    db.flush()
    dest = pdfs.original_path(project.slug, paper.id)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    except OSError:
        db.rollback()
        raise
    pages = paper_texts.page_count(dest)
    db.add(models.PaperIdentification(
        paper_id=paper.id, filename=(filename or "")[:300] or None,
        page_count=pages or None, status=models.IDENT_QUEUED,
        described={}, found={}, candidates=[], log=[]))
    db.commit()
    return paper


# ------------------------------------------------- running in the background

# The identifications being worked on right now, so a second click on Try
# again cannot run the same step twice at once.
_active: set[int] = set()
_active_lock = threading.Lock()
# Uploads that turned out to be an article the project already had, and so
# were folded into it: upload id -> the article's id. In memory: it only has
# to outlive the panel that is still asking after the upload.
_merged: dict[int, int] = {}


def merged_into(paper_id: int) -> int | None:
    return _merged.get(paper_id)


def run_many(paper_ids: list[int]) -> None:
    """Background job: start the identification of a batch of uploads."""
    for paper_id in paper_ids:
        run(paper_id)


def run(paper_id: int) -> None:
    """Background job: S1, then the classification task."""
    _guarded(paper_id, _start_identify)


def run_search(paper_id: int) -> None:
    """Background job: S3's lookups after the classification came in."""
    _guarded(paper_id, _step_search)


def run_accept(paper_id: int) -> None:
    """Background job: take the record that was picked or confirmed."""
    _guarded(paper_id, _accept_candidate)


def run_web_check(paper_id: int) -> None:
    """Background job: check the web page Claude found, then finish."""
    _guarded(paper_id, _finish_web)


def _guarded(paper_id: int, step) -> None:
    with _active_lock:
        if paper_id in _active:
            return
        _active.add(paper_id)
    db = SessionLocal()
    try:
        ident = db.get(models.PaperIdentification, paper_id)
        if ident is None or ident.paper is None or ident.paper.project is None:
            return
        if ident.status == models.IDENT_DONE:
            return
        try:
            step(db, ident)
            tasks.commit(db)
        except IdentifyError as exc:
            db.rollback()
            _fail(db, paper_id, str(exc))
        except Exception as exc:
            logger.exception("Identifying upload %s failed", paper_id)
            db.rollback()
            _fail(db, paper_id, f"Something went wrong: {exc}")
    finally:
        db.close()
        with _active_lock:
            _active.discard(paper_id)


def _fail(db: Session, paper_id: int, reason: str) -> None:
    ident = db.get(models.PaperIdentification, paper_id)
    if ident is None:
        return
    ident.status = models.IDENT_FAILED
    ident.error = reason[:1000]
    db.commit()
    logger.warning("Identifying upload %s failed: %s", paper_id, reason)


def _note(ident: models.PaperIdentification, step: str, said: str) -> None:
    # Reassigned rather than appended to: a JSON column does not notice its
    # list changing in place.
    ident.log = list(ident.log or []) + [{"step": step, "said": said}]


def _set(db: Session, ident: models.PaperIdentification, status: str,
         *, commit: bool = True) -> None:
    ident.status = status
    if commit:
        db.commit()


def resume_all() -> int:
    """Startup: carry on with every identification the app was closed in the
    middle of. Claude's steps wait in the queue; the app's own steps are run
    again, and a Claude step whose task went missing is queued again."""
    db = SessionLocal()
    resumed = 0
    try:
        rows = db.execute(
            select(models.PaperIdentification)
            .where(models.PaperIdentification.status.in_(models.IDENT_RUNNING))
        ).scalars().all()
        for ident in rows:
            kinds = {t.kind for t in tasks.live_tasks(db, paper_id=ident.paper_id)}
            nxt = (ident.found or {}).get("next")
            if ident.status == models.IDENT_QUEUED:
                tasks.after_commit(db, worker.submit, run, ident.paper_id)
            elif ident.status == models.IDENT_PARSING:
                if models.TASK_PARSE_PDF in kinds:
                    continue
                tasks.after_commit(db, worker.submit, run, ident.paper_id)
            elif ident.status == models.IDENT_SEARCHING:
                job = run_accept if nxt == "accept" else run_search
                tasks.after_commit(db, worker.submit, job, ident.paper_id)
            elif ident.status == models.IDENT_WEB and nxt == "web_check":
                tasks.after_commit(db, worker.submit, run_web_check, ident.paper_id)
            elif (ident.status == models.IDENT_CLASSIFYING
                  and models.TASK_IDENTIFY_PDF not in kinds):
                _queue_classification(db, ident)
            elif (ident.status == models.IDENT_PICKING
                  and models.TASK_MATCH_RECORD not in kinds):
                _queue_pick(db, ident)
            elif (ident.status == models.IDENT_WEB
                  and models.TASK_FIND_WEB_PAGE not in kinds):
                _queue_web(db, ident)
            else:
                continue
            resumed += 1
        tasks.commit(db)
    finally:
        db.close()
    if resumed:
        logger.info("Identifications picked up at startup: %d", resumed)
    return resumed


# -------------------------------------------------- what the person can do

def retry(db: Session, ident: models.PaperIdentification) -> bool:
    if ident.status != models.IDENT_FAILED:
        return False
    ident.error = None
    tasks.cancel(db, "Started again", paper_id=ident.paper_id,
                 kinds=[models.TASK_IDENTIFY_PDF, models.TASK_MATCH_RECORD,
                        models.TASK_FIND_WEB_PAGE])
    _set(db, ident, models.IDENT_QUEUED)
    return True


def answer(db: Session, ident: models.PaperIdentification, yes: bool) -> bool:
    """The answer to "Is this it?". Caller commits with tasks.commit(db)."""
    if ident.status != models.IDENT_CONFIRM:
        return False
    if yes:
        _note(ident, "Match", "You confirmed the record.")
        ident.found = {**(ident.found or {}), "next": "accept"}
        _set(db, ident, models.IDENT_SEARCHING, commit=False)
        tasks.after_commit(db, worker.submit, run_accept, ident.paper_id)
    else:
        _note(ident, "Match", "You said it is not that record.")
        ident.candidate = None
        _queue_web(db, ident)
    return True


def removable(ident: models.PaperIdentification) -> bool:
    """May the upload be taken back? Not while the app works on it, and not
    once a research question holds the article - by then it is evidence."""
    if ident.status in (models.IDENT_QUEUED, models.IDENT_SEARCHING):
        return False
    paper = ident.paper
    return not paper.annotations and not any(
        search.research_question_id is not None for search in paper.searches)


def remove(db: Session, ident: models.PaperIdentification) -> None:
    """Delete the uploaded article and its files. The caller checks removable."""
    paper = ident.paper
    slug, paper_id = paper.project.slug, paper.id
    with locks.for_paper(paper_id):
        tasks.cancel(db, "The upload was removed", paper_id=paper_id)
        db.delete(ident)
        text = db.get(models.PaperText, paper_id)
        if text is not None:
            db.delete(text)
        db.delete(paper)
        db.commit()
        _remove_files(slug, paper_id)


def _remove_files(slug: str, paper_id: int) -> None:
    path = pdfs.original_path(slug, paper_id)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove %s: %s", path, exc)


# ------------------------------------------------------- for the screens

def open_uploads(db: Session, project_id: int) -> list[models.PaperIdentification]:
    """The uploads the project's panel lists: every one not finished, and the
    ones that finished a moment ago."""
    recent = models.utcnow() - RECENT
    rows = db.execute(
        select(models.PaperIdentification)
        .join(models.Paper,
              models.Paper.id == models.PaperIdentification.paper_id)
        .where(models.Paper.project_id == project_id)
        .order_by(models.PaperIdentification.created_at.desc())
        .limit(100)
    ).scalars().all()
    return [ident for ident in rows
            if ident.status != models.IDENT_DONE
            or models.aware(ident.updated_at) >= recent][:50]


def for_papers(db: Session, paper_ids: list[int]) -> dict[int, models.PaperIdentification]:
    if not paper_ids:
        return {}
    return {ident.paper_id: ident for ident in db.execute(
        select(models.PaperIdentification)
        .where(models.PaperIdentification.paper_id.in_(paper_ids))
    ).scalars().all()}


def details_from(ident: models.PaperIdentification | None,
                 paper: models.Paper) -> str | None:
    """Where an uploaded article's details came from, in one sentence."""
    if ident is None:
        return None
    if ident.status != models.IDENT_DONE:
        return "the uploaded PDF, which is still being identified"
    if ident.outcome == models.IDENTIFIED_MATCHED:
        return (f"the {SOURCE_LABELS.get(ident.source, ident.source)} record "
                "the uploaded PDF was matched to")
    if ident.outcome == models.IDENTIFIED_WEB:
        return ("the uploaded PDF itself; its only external reference is "
                f"the web page {paper.url}")
    return "the uploaded PDF itself; no external source was found for it"


STATUS_LABELS = {
    models.IDENT_QUEUED: "waiting its turn",
    models.IDENT_PARSING: "reading the PDF",
    models.IDENT_CLASSIFYING: "waiting for Claude to identify it",
    models.IDENT_SEARCHING: "matching it in the scholarly sources",
    models.IDENT_PICKING: "waiting for Claude to pick its record",
    models.IDENT_CONFIRM: "is this it?",
    models.IDENT_WEB: "waiting for Claude to find it on the web",
    models.IDENT_DONE: "done",
    models.IDENT_FAILED: "failed",
}


def outcome_label(ident: models.PaperIdentification) -> str:
    if ident.outcome == models.IDENTIFIED_MATCHED:
        return f"matched: {SOURCE_LABELS.get(ident.source, ident.source or 'source')}"
    if ident.outcome == models.IDENTIFIED_WEB:
        return "web source"
    if ident.outcome == models.IDENTIFIED_NONE:
        return "no external source"
    return STATUS_LABELS.get(ident.status, ident.status)


# ------------------------------------------------------- S1 + S2 (queue)

def _start_identify(db: Session, ident: models.PaperIdentification) -> None:
    """Read the PDF and queue its classification."""
    paper = ident.paper
    ident.log = []
    ident.error = None
    ident.candidate = None
    ident.outcome = ident.source = None
    ident.candidates = []
    ident.found = {}
    _set(db, ident, models.IDENT_PARSING)
    source = pdfs.original_path(paper.project.slug, paper.id)
    if not source.exists():
        raise IdentifyError("The PDF file is missing.")
    if db.get(models.PaperText, paper.id) is None:
        try:
            with locks.for_paper(paper.id):
                pages = paper_texts.extract_pages(source)
                paper_texts.store(db, paper, pages, models.READER_PYMUPDF)
        except paper_texts.NeedsOCRError:
            paper_texts.request_parse(db, paper=paper, priority=45)
            _note(ident, "Parsed", "The PDF has no text layer, so Claude is "
                  "asked to transcribe it first.")
            db.commit()
            return
        except Exception as exc:
            raise IdentifyError(f"The PDF could not be opened ({exc}) - is the "
                                "file damaged?") from exc
        _note(ident, "Parsed", f"{len(pages)} page{'' if len(pages) == 1 else 's'} "
              "read from the PDF's text layer.")
    _queue_classification(db, ident)


def text_ready(db: Session, paper: models.Paper) -> bool:
    """Claude's transcription of an uploaded scan came in: go on to the
    classification. Caller commits."""
    ident = db.get(models.PaperIdentification, paper.id)
    if ident is None or ident.status != models.IDENT_PARSING:
        return False
    _note(ident, "Parsed", "Claude transcribed the PDF.")
    _queue_classification(db, ident)
    return True


def _first_pages(db: Session, paper: models.Paper) -> list[str]:
    text = db.get(models.PaperText, paper.id)
    if text is None:
        return []
    pages = paper_texts.split(text.content)
    return [pages[n] for n in sorted(pages)][:FIRST_PAGES]


def _queue_classification(db: Session, ident: models.PaperIdentification) -> None:
    paper = ident.paper
    found = _scan_identifiers(_first_pages(db, paper),
                              pdfs.original_path(paper.project.slug, paper.id))
    ident.found = {"hits": found["hits"], "metadata": found["metadata"]}
    tasks.create(db, models.TASK_IDENTIFY_PDF, project_id=paper.project_id,
                 paper_id=paper.id, priority=50)
    _set(db, ident, models.IDENT_CLASSIFYING, commit=False)


def _classify_input(db: Session, task: models.ClaudeTask) -> dict:
    ident = db.get(models.PaperIdentification, task.paper_id)
    found = (ident.found if ident else {}) or {}
    pages = _first_pages(db, task.paper) if task.paper else []
    first = "\n".join(f"<page {n}>\n{text.strip()}\n</page {n}>"
                      for n, text in enumerate(pages, start=1))
    return {"file_name": ident.filename if ident else None,
            "pdf_metadata": found.get("metadata") or {},
            "identifiers_found": found.get("hits") or [],
            "first_pages": first[:FIRST_PAGES_CHARS]}


def _classify_apply(db: Session, task: models.ClaudeTask, output: dict) -> dict:
    ident = db.get(models.PaperIdentification, task.paper_id)
    if ident is None or ident.status != models.IDENT_CLASSIFYING:
        return {"saved": "the upload has moved on"}
    paper = ident.paper
    described = _clean_described(output)
    ident.described = described
    ident.scholarly = described["category"] == "scholarly"
    if described["title"]:
        paper.title = described["title"]
    kind = "scholarly" if ident.scholarly else "not scholarly"
    _note(ident, "Classified",
          f"{kind.capitalize()} ({described['document_type'].replace('-', ' ')}, "
          f"confidence {described['confidence']:.0%}): {described['reason']}")
    if ident.scholarly:
        ident.found = {**(ident.found or {}), "next": "search"}
        _set(db, ident, models.IDENT_SEARCHING, commit=False)
        tasks.after_commit(db, worker.submit, run_search, paper.id)
        return {"saved": f"\"{paper.title}\" is {kind}",
                "next": "The app is looking it up in the scholarly sources; a "
                        "task to pick its record may follow."}
    _queue_web(db, ident)
    return {"saved": f"\"{paper.title}\" is {kind}",
            "next": "A web-page task for it is next."}


def _classify_describe(task: models.ClaudeTask) -> str:
    return ("Identify an uploaded PDF "
            f"(\"{(task.paper.title if task.paper else '')[:60]}\")")


# ---------------------------------------------------------- S3 (app + queue)

def _step_search(db: Session, ident: models.PaperIdentification) -> None:
    paper = ident.paper
    described = ident.described or {}
    found = ident.found or {}
    project = paper.project
    sources = [s for s in project.source_list() if s in SOURCES] or list(SOURCES)
    first_text = "\n".join(_first_pages(db, paper)[:2])
    described.setdefault("identifiers", [])
    described.setdefault("doi", "")
    found.setdefault("hits", [])

    for doi in _dois_to_try(described, found):
        record = _lookup_doi(doi, sources)
        if record is None:
            _note(ident, "DOI", f"{doi} is in none of the sources.")
            continue
        if _title_checks_out(record["title"], described.get("title") or "",
                             first_text):
            _note(ident, "DOI", f"{doi} is “{record['title']}” in "
                  f"{SOURCE_LABELS.get(record['source'], record['source'])} - "
                  "the PDF's own DOI.")
            _apply_record(db, ident, paper, record)
            return
        _note(ident, "DOI", f"{doi} belongs to “{record['title']}”, which is "
              "not this document - a work it cites, most likely.")
    db.commit()

    query = described.get("search_query") or " ".join(
        part for part in (described.get("title"),
                          _surname(described["authors"][0])
                          if described.get("authors") else "") if part)
    if not query:
        _note(ident, "Search", "The PDF gave no title to search for.")
        _queue_web(db, ident)
        return
    records: list[dict] = []
    errors: list[str] = []
    for name in sources:
        try:
            for pos, record in enumerate(SOURCES[name].search(
                    query, max_results=PER_SOURCE)):
                record["raw_rank"] = pos
                records.append(record)
        except Exception as exc:  # one source down must not end the lookup
            logger.warning("Source %s failed for upload %s: %s", name, paper.id, exc)
            errors.append(SOURCE_LABELS.get(name, name))
    candidates = sorted(dedupe(records),
                        key=lambda r: -_overlap(described.get("title"), r.get("title")))
    candidates = candidates[:MAX_CANDIDATES]
    _note(ident, "Search", f"Searched for “{query}”: {len(candidates)} "
          f"record{'' if len(candidates) == 1 else 's'} found"
          + (f" ({', '.join(errors)} did not answer)" if errors else "") + ".")
    if not candidates:
        _queue_web(db, ident)
        return
    ident.candidates = [_jsonable(r) for r in candidates]
    # A record that passes the fixed rule on its own - the PDF's DOI, or the
    # same title, a shared author and the year - needs no one to pick it.
    # Only when exactly one does: two would mean versions to tell apart.
    sure = [r for r in candidates
            if match_rule(_described_for_rule(described), r,
                          _printed_dois({"doi": described.get("doi", "")},
                                        {"hits": found.get("hits", [])}))[0]]
    if len(sure) == 1:
        record = sure[0]
        _note(ident, "Match", f"“{record['title']}” matches the PDF's title, "
              "authors and year - taken without asking.")
        _apply_record(db, ident, paper, record)
        return
    _queue_pick(db, ident)


def _described_for_rule(described: dict) -> dict:
    out = dict(described or {})
    out.setdefault("authors", [])
    out.setdefault("year", None)
    out.setdefault("title", "")
    return out


def _queue_pick(db: Session, ident: models.PaperIdentification) -> None:
    tasks.create(db, models.TASK_MATCH_RECORD, project_id=ident.paper.project_id,
                 paper_id=ident.paper_id, priority=50)
    _set(db, ident, models.IDENT_PICKING, commit=False)


def _pick_input(db: Session, task: models.ClaudeTask) -> dict:
    ident = db.get(models.PaperIdentification, task.paper_id)
    described = (ident.described if ident else {}) or {}
    rows = []
    for n, r in enumerate((ident.candidates if ident else []) or []):
        rows.append({"number": n, "title": r.get("title"),
                     "authors": (r.get("authors") or [])[:6],
                     "year": r.get("year"), "venue": r.get("venue"),
                     "type": r.get("work_type"), "doi": r.get("doi"),
                     "sources": [SOURCE_LABELS.get(s, s) for s in
                                 (r.get("source_ids") or {r.get("source"): 1})]})
    first = "\n".join(_first_pages(db, task.paper)[:1]) if task.paper else ""
    return {"the_pdf_says": {k: described.get(k) for k in (
                "title", "authors", "organization", "year", "venue",
                "document_type", "doi", "identifiers")},
            "first_page_begins": first[:2500],
            "records_found": rows}


def _pick_check(db: Session, task: models.ClaudeTask, output: dict) -> list[str]:
    ident = db.get(models.PaperIdentification, task.paper_id)
    n = len((ident.candidates if ident else []) or [])
    choice = output.get("choice")
    if not isinstance(choice, int) or not -1 <= choice < n:
        return [f"choice must be a record number from 0 to {n - 1}, or -1"]
    return []


def _pick_apply(db: Session, task: models.ClaudeTask, output: dict) -> dict:
    ident = db.get(models.PaperIdentification, task.paper_id)
    if ident is None or ident.status != models.IDENT_PICKING:
        return {"saved": "the upload has moved on"}
    reason = " ".join(str(output.get("reason") or "").split())
    choice = int(output["choice"])
    candidates = ident.candidates or []
    if choice < 0 or choice >= len(candidates):
        _note(ident, "Match", f"None of them is this document. {reason}".strip())
        _queue_web(db, ident)
        return {"saved": "no record matches", "next": "A web-page task is next."}
    record = candidates[choice]
    described = dict(ident.described or {})
    described.setdefault("authors", [])
    described.setdefault("year", None)
    described.setdefault("title", "")
    ok, why = match_rule(described, record,
                         _printed_dois({"doi": described.get("doi", "")},
                                       {"hits": (ident.found or {}).get("hits", [])}))
    if ok:
        _note(ident, "Match", f"“{record['title']}” ({why}). {reason}".strip())
        ident.candidate = {"record": record, "reason": reason}
        ident.found = {**(ident.found or {}), "next": "accept"}
        _set(db, ident, models.IDENT_SEARCHING, commit=False)
        tasks.after_commit(db, worker.submit, run_accept, ident.paper_id)
        return {"saved": f"matched to “{record['title']}”"}
    ident.candidate = {"record": record, "reason": reason, "doubt": why}
    _note(ident, "Match", f"Claude picked “{record['title']}”, but {why} - so "
          "you are asked.")
    _set(db, ident, models.IDENT_CONFIRM, commit=False)
    return {"saved": f"“{record['title']}” is put to the user to confirm ({why})"}


def _pick_describe(task: models.ClaudeTask) -> str:
    return ("Pick the record of an uploaded PDF "
            f"(\"{(task.paper.title if task.paper else '')[:60]}\")")


def _lookup_doi(doi: str, sources: list[str]) -> dict | None:
    """The record for this DOI from the first source that has it."""
    for name in ("openalex", "crossref", "semantic_scholar"):
        if name not in sources:
            continue
        lookup = getattr(SOURCES.get(name), "get_by_doi", None)
        if lookup is None:
            continue
        try:
            record = lookup(doi)
        except Exception as exc:
            logger.warning("DOI lookup of %s in %s failed: %s", doi, name, exc)
            continue
        if record and record.get("title"):
            record.setdefault("source_ids", {})
            if record.get("source") and record.get("source_id"):
                record["source_ids"].setdefault(record["source"], record["source_id"])
            return record
    return None


def _accept_candidate(db: Session, ident: models.PaperIdentification) -> None:
    record = (ident.candidate or {}).get("record")
    if not record:
        raise IdentifyError("The record to confirm is gone. Try again.")
    _apply_record(db, ident, ident.paper, record)


def _project_lock(paper: models.Paper):
    return locks.for_key(f"project-upsert:{paper.project_id}")


def _apply_record(db: Session, ident: models.PaperIdentification,
                  paper: models.Paper, record: dict) -> None:
    """Outcome A: the article becomes the record it was matched to."""
    ident.outcome = models.IDENTIFIED_MATCHED
    ident.source = record.get("source")
    ident.candidate = None
    handed_over = False
    with _project_lock(paper):
        twin = _twin(db, paper, record.get("doi"),
                     title_key(record.get("title") or "", record.get("year")))
        if twin is not None:
            handed_over = _fold_into(db, ident, paper, twin, record)
        else:
            described = ident.described or {}
            paper.title = record.get("title") or paper.title
            paper.authors = record.get("authors") or described.get("authors") or []
            paper.year = record.get("year") or described.get("year")
            paper.online_year = record.get("online_year")
            paper.venue = record.get("venue") or described.get("venue") or None
            paper.volume = record.get("volume")
            paper.issue = record.get("issue")
            paper.pages = record.get("pages")
            paper.doi = clean_doi(record.get("doi"))
            paper.issn_l = record.get("issn_l")
            paper.issns = record.get("issns") or []
            paper.work_type = record.get("work_type")
            paper.citation_count = record.get("citation_count")
            paper.url = record.get("url")
            paper.oa_url = record.get("oa_url")
            paper.abstract = record.get("abstract") or described.get("abstract") or None
            paper.source_ids = dict(record.get("source_ids") or {})
            paper.dedupe_key = title_key(paper.title, paper.year)
            ident.status = models.IDENT_DONE
            _note(ident, "Result", "Its details are the "
                  f"{SOURCE_LABELS.get(ident.source, ident.source)} record's.")
            db.commit()
    # OpenAlex is asked first, and its record carries the online-first year
    # and often no pages; Crossref has the issue's. Never raises.
    services.polish_from_crossref(db, [paper if twin is None else twin])
    if twin is None:
        services.enrich_journals(db, [paper])
    elif handed_over:
        research.annotate_uploaded_paper(twin.id)


# ------------------------------------------------------ shared rules

_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>\[\]{}|\\^`]+)", re.I)
_ARXIV = re.compile(r"arxiv(?:\.org/(?:abs|pdf)/|:\s*)(\d{4}\.\d{4,5})", re.I)
# SSRN papers carry an abstract id ("ssrn.com/abstract=5285127", "SSRN
# 5285127") and are registered in Crossref as 10.2139/ssrn.<id>.
_SSRN = re.compile(r"ssrn(?:\.com/abstract=|[\s:#.\-]*(?:id)?[\s:#]*)(\d{6,8})\b", re.I)


def _strip_doi(raw: str) -> str:
    return (clean_doi(raw.rstrip(".,;:)]}")) or "")


def _valid_doi(doi: str) -> bool:
    return bool(re.fullmatch(r"10\.\d{4,9}/\S+", doi or ""))


def _scan_identifiers(pages: list[str], source: Path) -> dict:
    """DOIs and arXiv ids printed on the first pages or in the PDF's own
    metadata, each with where it was found - so the LLM can tell the
    document's own DOI from one in a reference it cites."""
    hits: list[dict] = []
    seen: set[str] = set()

    def add(value: str, where: str, text: str, at: int) -> None:
        if value in seen or len(hits) >= 12:
            return
        seen.add(value)
        context = " ".join(text[max(0, at - 60):at + len(value) + 60].split())
        hits.append({"id": value, "where": where, "context": context})

    for number, text in enumerate(pages[:FIRST_PAGES], start=1):
        for m in _DOI.finditer(text):
            doi = _strip_doi(m.group(1))
            if _valid_doi(doi):
                add(doi, f"page {number}", text, m.start())
        for m in _ARXIV.finditer(text):
            add(f"arXiv:{m.group(1)}", f"page {number}", text, m.start())
        for m in _SSRN.finditer(text):
            add(f"SSRN:{m.group(1)}", f"page {number}", text, m.start())
    meta = _pdf_metadata(source)
    blob = " ".join(v for v in meta.values() if v)
    for m in _DOI.finditer(blob):
        doi = _strip_doi(m.group(1))
        if _valid_doi(doi):
            add(doi, "the PDF's metadata", blob, m.start())
    return {"hits": hits, "metadata": meta}


def _pdf_metadata(source: Path) -> dict:
    """The document's own title, author and subject fields, and its XMP
    packet - where publishers often put the DOI. Empty when unreadable."""
    try:
        import pymupdf as fitz

        with fitz.open(source) as doc:
            meta = doc.metadata or {}
            xmp = doc.get_xml_metadata() or ""
    except Exception:
        return {}
    out = {key: " ".join(str(meta.get(key) or "").split())[:300]
           for key in ("title", "author", "subject", "keywords")}
    dois = sorted({_strip_doi(m.group(1)) for m in _DOI.finditer(xmp)})
    if dois:
        out["xmp"] = " ".join(d for d in dois if _valid_doi(d))[:300]
    return {key: value for key, value in out.items() if value}


DOCUMENT_TYPES = [
    "journal-article", "working-paper", "preprint", "conference-paper",
    "book", "book-chapter", "thesis", "report", "policy-brief",
    "presentation", "guideline", "web-page", "news-article", "other",
]
# What each becomes on the article. Journal ratings apply to "article" only,
# and the rest are shown on the card (see normalize.py).
_WORK_TYPES = {"journal-article": "article", "thesis": "dissertation"}

CLASSIFY_SYSTEM = (
    "You identify a document for a literature-review database, from the text "
    "of its first pages. You also get its file name, the PDF's own metadata, "
    "and the DOIs and arXiv ids found in the text, each with where it was "
    "found.\n\n"
    "category: 'scholarly' if it is academic material likely to have a "
    "record in the scholarly databases (OpenAlex, Semantic Scholar, "
    "Crossref): a journal article, working paper, preprint, conference "
    "paper, book or book chapter, thesis. 'not_scholarly' if it is material "
    "unlikely to have such a record: a report or policy brief from an "
    "organization or government, slides, a guideline, a web page, a news "
    "article, a manual. Give your confidence (0-1) and one sentence of "
    "reason.\n\n"
    "Then write down what the document says about itself, copied from the "
    "text and never invented - leave out (empty string, empty list or null) "
    "anything it does not show:\n"
    "- title: the full title, with its subtitle.\n"
    "- authors: the persons who wrote it, as 'First Last', in order.\n"
    "- organization: the body that issued it, if any.\n"
    "- year: the year of this version of the document.\n"
    "- venue: the journal, the working-paper series with its number, the "
    "publisher, or the website.\n"
    "- document_type: what this PDF is.\n"
    "- doi: the document's OWN DOI - usually in the header or footer of the "
    "first page, in its citation line, or in the metadata. Never the DOI of a "
    "work it cites, and never one you would have to guess. Empty if none.\n"
    "- identifiers: other identifiers of this document itself, such as "
    "'arXiv:2407.06387', 'SSRN 1234567' or 'NBER Working Paper 31234'.\n"
    "- abstract: its abstract or summary as printed, verbatim, at most about "
    "1500 characters. Empty if it has none.\n"
    "- search_query: for a scholarly document, what to type into the "
    "scholarly databases to find it: the title (leave a long subtitle out) "
    "followed by the first author's surname. Plain words, no quotes or "
    "operators. Empty when not_scholarly."
)


def _clean_described(raw: dict) -> dict:
    """The classification's answer, with anything malformed made empty."""
    def text(key: str, limit: int = 500) -> str:
        return " ".join(str(raw.get(key) or "").split())[:limit]

    year = raw.get("year")
    if not isinstance(year, int) or not 1000 <= year <= models.utcnow().year + 1:
        year = None
    doi = _strip_doi(text("doi"))
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    doc_type = raw.get("document_type")
    return {
        "category": ("scholarly" if raw.get("category") == "scholarly"
                     else "not_scholarly"),
        "confidence": confidence,
        "reason": text("reason"),
        "title": text("title", 400),
        "authors": [" ".join(str(a).split())[:120]
                    for a in (raw.get("authors") or []) if str(a).strip()][:60],
        "organization": text("organization", 300),
        "year": year,
        "venue": text("venue", 300),
        "document_type": doc_type if doc_type in DOCUMENT_TYPES else "other",
        "doi": doi if _valid_doi(doi) else "",
        "identifiers": [text_ for text_ in (" ".join(str(i).split())[:120]
                                            for i in raw.get("identifiers") or [])
                        if text_][:10],
        "abstract": str(raw.get("abstract") or "").strip()[:3000],
        "search_query": text("search_query", 300),
    }


def _dois_to_try(described: dict, found: dict) -> list[str]:
    """The DOI the classification says is the document's own, then any
    printed on the first page or in the metadata - each is checked against
    the title, so a cited work's DOI costs a lookup and nothing more. An
    arXiv id stands in for a DOI: arXiv registers one for every preprint."""
    dois: list[str] = []
    if described["doi"]:
        dois.append(described["doi"])
    for hit in found["hits"]:
        if hit["where"] in ("page 1", "the PDF's metadata"):
            if hit["id"].lower().startswith("arxiv:"):
                dois.append(f"10.48550/arxiv.{hit['id'].split(':', 1)[1]}")
            elif hit["id"].lower().startswith("ssrn:"):
                dois.append(f"10.2139/ssrn.{hit['id'].split(':', 1)[1]}")
            else:
                dois.append(hit["id"])
    for ident_text in described["identifiers"]:
        m = re.search(r"arxiv[:\s]*(\d{4}\.\d{4,5})", ident_text, re.I)
        if m:
            dois.append(f"10.48550/arxiv.{m.group(1)}")
        m = _SSRN.search(ident_text)
        if m:
            dois.append(f"10.2139/ssrn.{m.group(1)}")
    unique: list[str] = []
    for doi in dois:
        doi = doi.lower()
        if doi not in unique:
            unique.append(doi)
    return unique[:MAX_DOI_LOOKUPS]


def _printed_dois(described: dict, found: dict) -> set[str]:
    """The DOIs the PDF itself carries: what "the DOI is the same" checks."""
    dois = {described["doi"]} if described["doi"] else set()
    dois |= {hit["id"].lower() for hit in found["hits"]
             if hit["where"] in ("page 1", "the PDF's metadata")
             and not hit["id"].lower().startswith(("arxiv:", "ssrn:"))}
    return dois


PICK_SYSTEM = (
    "You match a PDF to its record in the scholarly databases. You get what "
    "the PDF says about itself, the start of its first page, and a numbered "
    "list of records a search returned.\n\n"
    "Pick the record that is this very document: the same work, and the same "
    "version of it. A working paper or preprint and the journal article it "
    "became are different records - pick the one the PDF is, judging by the "
    "venue or series it names, its year, the DOI it prints and how its first "
    "page looks. Never pick a merely similar or related work, or a review of "
    "it. If no record is this document, answer -1."
)


def _norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


_STOPWORDS = {"the", "and", "for", "with", "from", "into", "its", "their",
              "are", "how", "what", "does", "this", "that", "via", "using"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def _tokens(text: str | None) -> set[str]:
    return {t for t in _norm(text).split() if len(t) > 2 and t not in _STOPWORDS}


def _overlap(a: str | None, b: str | None) -> float:
    """How much of a's wording b has, 0-1."""
    ta = _tokens(a)
    return len(ta & _tokens(b)) / len(ta) if ta else 0.0


def _surname(name: str) -> str:
    name = (name or "").strip()
    if "," in name:
        return _norm(name.split(",", 1)[0])
    parts = [p for p in _norm(name).split() if p not in _SUFFIXES]
    return parts[-1] if parts else ""


def _title_checks_out(record_title: str, pdf_title: str, first_text: str) -> bool:
    """Is a record found by DOI this document? Lenient on purpose - the DOI
    already points at it - and only there to catch a cited work's DOI, whose
    title would have nothing in common with this one."""
    record = _norm(record_title)
    if not record:
        return False
    if record == _norm(pdf_title):
        return True
    if len(record.split()) >= 3 and record in _norm(first_text):
        return True
    return _overlap(record_title, pdf_title) >= 0.8


def match_rule(described: dict, record: dict,
               printed_dois: set[str]) -> tuple[bool, str]:
    """The fixed check on the LLM's pick. (True, why) to take it unasked;
    (False, why not) to ask "Is this it?" instead.

    Taken when the record's DOI is printed on the PDF, or when the titles
    are the same (ignoring case, accents and punctuation), they share an
    author's surname, and their years are at most one apart - a preprint and
    its published version are often a year apart. A year missing on either
    side cannot contradict.
    """
    doi = clean_doi(record.get("doi"))
    if doi and doi in printed_dois:
        return True, "the PDF prints its DOI"
    if _norm(record.get("title")) != _norm(described["title"]):
        return False, "its title is not the PDF's"
    mine = {_surname(a) for a in described["authors"]} - {""}
    theirs = {_surname(a) for a in record.get("authors") or []} - {""}
    if not mine:
        return False, "the PDF names no author to compare"
    if not mine & theirs:
        return False, "it has no author in common with the PDF"
    year, their_year = described["year"], record.get("year")
    if year and their_year and abs(year - their_year) > 1:
        return False, f"its year ({their_year}) is not the PDF's ({year})"
    return True, "the same title, a shared author" + (
        " and the same year, give or take one" if year and their_year else "")


def _jsonable(record: dict) -> dict:
    keep = ("doi", "title", "abstract", "year", "venue", "issn_l", "issns",
            "work_type", "authors", "citation_count", "url", "oa_url",
            "source", "source_id", "source_ids",
            "online_year", "volume", "issue", "pages")
    return {key: record.get(key) for key in keep}


def _twin(db: Session, paper: models.Paper, doi: str | None,
          key: str) -> models.Paper | None:
    """The article already in the project that this upload turned out to be."""
    others = select(models.Paper).where(
        models.Paper.project_id == paper.project_id,
        models.Paper.id != paper.id)
    doi = clean_doi(doi)
    if doi:
        twin = db.execute(others.where(models.Paper.doi == doi)).scalars().first()
        if twin is not None:
            return twin
    if key and not key.startswith("|"):
        return db.execute(others.where(models.Paper.dedupe_key == key)
                          .order_by(models.Paper.id)).scalars().first()
    return None


def _fold_into(db: Session, ident: models.PaperIdentification,
               paper: models.Paper, twin: models.Paper,
               record: dict | None) -> bool:
    """The upload is an article the project already has: put the PDF on that
    article's card - unless it has one - and remove the upload's row.

    Returns whether the PDF was handed over, in which case the caller has it
    read for the article's questions - after letting go of the project lock,
    since that read takes minutes."""
    slug, upload_id, twin_id = paper.project.slug, paper.id, twin.id
    first, second = sorted((upload_id, twin_id))
    handed_over = False
    with locks.for_paper(first), locks.for_paper(second):
        if (twin.pdf_status != models.PDF_DOWNLOADED
                or not pdfs.original_path(slug, twin_id).exists()):
            _move(pdfs.original_path(slug, upload_id),
                  pdfs.original_path(slug, twin_id))
            twin.pdf_status = models.PDF_DOWNLOADED
            twin.pdf_error = None
            if twin.stage in (models.STAGE_SCREENING, models.STAGE_LOOKUP):
                twin.stage = models.STAGE_RETRIEVAL
            for annotation in twin.annotations:
                if annotation.status in (models.ANNOT_FAILED,
                                         models.ANNOT_NEEDS_OCR):
                    annotation.error = None
            text = db.get(models.PaperText, upload_id)
            if text is not None:
                kept = db.get(models.PaperText, twin_id)
                if kept is None:
                    kept = models.PaperText(paper_id=twin_id)
                    db.add(kept)
                kept.content, kept.reader = text.content, text.reader
                kept.page_labels = text.page_labels
                kept.page_count, kept.char_count = text.page_count, text.char_count
            handed_over = True
        if record is not None:
            # What the record knows that the article does not yet.
            for field in ("abstract", "oa_url", "venue", "issn_l", "work_type",
                          "url", "volume", "issue", "pages"):
                if not getattr(twin, field) and record.get(field):
                    setattr(twin, field, record[field])
            if not twin.doi and record.get("doi"):
                twin.doi = clean_doi(record["doi"])
            ids = dict(twin.source_ids or {})
            for key, value in (record.get("source_ids") or {}).items():
                ids.setdefault(key, value)
            twin.source_ids = ids

        said = ("Already in the project as this article"
                + (", so the PDF went on its card." if handed_over else
                   ", which has a PDF already - the upload was set aside."))
        if twin.status == models.STATUS_EXCLUDED:
            said += " It is disregarded there; Restore it to use it."
        twin_ident = db.get(models.PaperIdentification, twin_id)
        if twin_ident is None:
            twin_ident = models.PaperIdentification(
                paper_id=twin_id,
                filename=ident.filename, page_count=ident.page_count,
                scholarly=ident.scholarly, described=ident.described or {},
                outcome=ident.outcome, source=ident.source,
                status=models.IDENT_DONE, log=list(ident.log or []))
            db.add(twin_ident)
        _note(twin_ident, "Result", said)
        tasks.cancel(db, "The upload was folded into an existing article",
                     paper_id=upload_id)
        upload_text = db.get(models.PaperText, upload_id)
        if upload_text is not None:
            db.delete(upload_text)
        db.delete(ident)
        db.delete(paper)
        db.commit()
        _remove_files(slug, upload_id)
    _merged[upload_id] = twin_id
    logger.info("Upload %s was article %s already (%s)", upload_id, twin_id,
                "PDF handed over" if handed_over else "PDF set aside")
    return handed_over


WEB_INSTRUCTIONS = (
    "Search the web for the page where this document is published: its page "
    "on the site of the organization or publisher that issued it, a "
    "repository record, or the PDF itself. Report a page only if you saw it "
    "in your search results and it is clearly about this exact document - "
    "the same title, the same issuer, and for a report the same edition or "
    "year. Prefer the issuer's own page over aggregators, social sites and "
    "copies on unrelated sites. If no page qualifies, set found to false and "
    "leave url empty."
)




def _queue_web(db: Session, ident: models.PaperIdentification) -> None:
    tasks.create(db, models.TASK_FIND_WEB_PAGE, project_id=ident.paper.project_id,
                 paper_id=ident.paper_id, priority=60)
    ident.found = {**(ident.found or {}), "next": "web"}
    _set(db, ident, models.IDENT_WEB, commit=False)


def _web_input(db: Session, task: models.ClaudeTask) -> dict:
    ident = db.get(models.PaperIdentification, task.paper_id)
    described = (ident.described if ident else {}) or {}
    return {"document": {
        "title": described.get("title") or (task.paper.title if task.paper else ""),
        "authors": described.get("authors") or [],
        "issued_by": described.get("organization") or "",
        "year": described.get("year"),
        "venue": described.get("venue") or "",
        "type": (described.get("document_type") or "").replace("-", " "),
        "identifiers": described.get("identifiers") or []}}


def _web_apply(db: Session, task: models.ClaudeTask, output: dict) -> dict:
    ident = db.get(models.PaperIdentification, task.paper_id)
    if ident is None or ident.status != models.IDENT_WEB:
        return {"saved": "the upload has moved on"}
    url = clean_url((output.get("url") or "").strip())
    if not output.get("found") or not url:
        _note(ident, "Web", "No web page about this document was found.")
        ident.found = {**(ident.found or {}), "web": None, "next": "web_check"}
    else:
        ident.found = {**(ident.found or {}), "next": "web_check",
                       "web": {"url": url,
                               "page_title": output.get("page_title") or "",
                               "evidence": output.get("evidence") or ""}}
    tasks.after_commit(db, worker.submit, run_web_check, ident.paper_id)
    return {"saved": "the web lookup",
            "next": "The app checks the page and files the article."}


def _web_describe(task: models.ClaudeTask) -> str:
    return ("Find a web page for an uploaded document "
            f"(\"{(task.paper.title if task.paper else '')[:60]}\")")


def _finish_web(db: Session, ident: models.PaperIdentification) -> None:
    """Check the page Claude found - it must open and carry the document's
    title, or at least the title Claude saw must agree - then finish."""
    web = (ident.found or {}).get("web")
    if not web:
        _apply_described(db, ident, None)
        return
    described = ident.described or {}
    title = described.get("title") or ident.paper.title
    ok, why = _verify_page(web["url"], title, web.get("page_title") or "")
    if ok:
        _note(ident, "Web", f"{web['url']} - {why}.")
        _apply_described(db, ident, web["url"])
    else:
        _note(ident, "Web", f"Claude suggested {web['url']}, but {why}, so it "
              "is not used.")
        _apply_described(db, ident, None)


def _apply_described(db: Session, ident: models.PaperIdentification,
                     url: str | None) -> None:
    """Outcomes B and C: the article is what the PDF says it is, with the
    web page - when one checked out - as its only reference."""
    paper = ident.paper
    described = ident.described or {}
    ident.outcome = models.IDENTIFIED_WEB if url else models.IDENTIFIED_NONE
    ident.source = None
    title = described.get("title") or paper.title
    year = described.get("year")
    handed_over = False
    with _project_lock(paper):
        twin = _twin(db, paper, None, title_key(title, year))
        if twin is not None:
            handed_over = _fold_into(db, ident, paper, twin, None)
        else:
            authors = described.get("authors") or (
                [described["organization"]] if described.get("organization") else [])
            paper.title = title
            paper.authors = authors
            paper.year = year
            paper.venue = described.get("venue") or None
            doc_type = described.get("document_type") or ""
            paper.work_type = _WORK_TYPES.get(doc_type, doc_type) or None
            paper.abstract = described.get("abstract") or None
            paper.doi = None
            paper.url = url
            paper.oa_url = None
            paper.dedupe_key = title_key(title, year)
            ident.status = models.IDENT_DONE
            _note(ident, "Result",
                  "Its details are what the PDF says about itself, and that "
                  "page is its only reference." if url else
                  "Its details are what the PDF says about itself. No "
                  "external source.")
            db.commit()
    if twin is not None and handed_over:
        research.annotate_uploaded_paper(twin.id)


def _move(source: Path, dest: Path) -> None:
    if not source.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))


# Fewer words than this and a page is a shell - a script-drawn app, a bot
# check - that shows nothing to judge it by without a browser.
THIN_PAGE_WORDS = 120


def _verify_page(url: str, title: str, page_title: str) -> tuple[bool, str]:
    """Is this page about the document? (ok, why) either way.

    The page is opened and has to carry the document's title. Many sites
    cannot be checked from here - publishers turn away anything that is not
    a browser, and repositories draw the page with script - so such a page
    is taken on the title Claude reports seeing, and the log says so.
    """
    try:
        status, text = _fetch_page(url)
    except Exception as exc:
        status, text = None, ""
        logger.info("Could not open %s to check it: %s", url, exc)
    if status == 200 and text:
        if _title_on_page(title, text):
            return True, "the page carries the document's title"
        if len(_norm(text).split()) >= THIN_PAGE_WORDS:
            return False, "the page does not carry the document's title"
        why = "the page shows too little without a browser to check it"
    elif status:
        why = f"the page would not open to check (HTTP {status})"
    else:
        why = "the page would not open to check"
    if _titles_agree(title, page_title):
        return True, f"{why}, but the title Claude saw there is the document's"
    return False, f"{why}, and the title Claude saw is not the document's"


def clean_url(url: str) -> str:
    """A URL without its fragment or tracking parameters - the address the
    article keeps, and the form the search's sources are compared in."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query,
                                                     keep_blank_values=True)
                       if not k.lower().startswith("utm_")])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       query, ""))


def _same_page(a: str, b: str) -> bool:
    def key(url: str) -> str:
        cleaned = clean_url(url)
        if not cleaned:
            return ""
        parts = urlsplit(cleaned)
        host = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
        return f"{host}{parts.path.rstrip('/')}?{parts.query}"
    return bool(key(a)) and key(a) == key(b)




def _main_title(title: str) -> str:
    """The title without its subtitle - what a landing page often shows
    alone. The whole title when the part before the colon is too short to
    tell documents apart."""
    main = re.split(r"\s*[:–—]\s+|\s+-\s+", title or "", maxsplit=1)[0]
    return main if len(_tokens(main)) >= 3 else (title or "")


def _title_on_page(title: str, text: str) -> bool:
    page = _norm(text)
    for candidate in {title, _main_title(title)}:
        wanted = _norm(candidate)
        if len(wanted) >= 10 and wanted in page:
            return True
    words = _tokens(title)
    return len(words) >= 3 and len(words & set(page.split())) / len(words) >= 0.8


def _titles_agree(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or (min(len(na), len(nb)) >= 10 and (na in nb or nb in na)):
        return True
    return _overlap(a, b) >= 0.8 and _overlap(b, a) >= 0.5


PAGE_TIMEOUT = 20.0
PAGE_MAX_BYTES = 5 * 1024 * 1024


def _fetch_page(url: str) -> tuple[int, str]:
    """(HTTP status, the page's text) - for a PDF, its first two pages.

    Refuses an address on a private network: the URL came from a web search,
    and the server must not be talked into reaching its own neighbours.
    """
    host = urlsplit(url).hostname or ""
    for info in socket.getaddrinfo(host, None):
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast):
            raise ValueError(f"{host} is not a public address")
    with httpx.Client(timeout=PAGE_TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as client:
        with client.stream("GET", url) as resp:
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > PAGE_MAX_BYTES:
                    break
            status = resp.status_code
            encoding = resp.encoding or "utf-8"
    if body[:5] == b"%PDF-":
        try:
            import pymupdf as fitz

            with fitz.open(stream=body, filetype="pdf") as doc:
                return status, " ".join(doc[i].get_text()
                                        for i in range(min(2, doc.page_count)))
        except Exception:
            return status, ""
    return status, _page_text(body.decode(encoding, errors="replace"))


def _page_text(markup: str) -> str:
    """The words of an HTML page, its <meta> contents included - publishers
    put the citation title there even when the body is drawn by script."""
    metas = " ".join(re.findall(r"<meta[^>]+content=[\"']([^\"']*)[\"']",
                                markup, re.I))
    markup = re.sub(r"<(script|style)\b.*?</\1>", " ", markup,
                    flags=re.I | re.S)
    return html.unescape(metas + " " + re.sub(r"<[^>]+>", " ", markup))


# ---------------------------------------------------------- edit details

EDITABLE_TYPES = ["article", "working-paper", "preprint", "book",
                  "book-chapter", "conference-paper", "dissertation", "report",
                  "policy-brief", "web-page", "other"]


def save_details(db: Session, paper: models.Paper, *, title: str,
                 authors: str, year: str, venue: str, doi: str, url: str,
                 work_type: str, abstract: str, volume: str | None = None,
                 issue: str | None = None, pages: str | None = None) -> None:
    """Correct an article's details by hand. Commits; ValueError with a
    message for the person when a field will not do."""
    title = " ".join(title.split())
    if not title:
        raise ValueError("An article needs a title.")
    names = [" ".join(line.split()) for line in authors.splitlines()]
    year = year.strip()
    year_value = None
    if year:
        if not year.isdigit() or not 1000 <= int(year) <= models.utcnow().year + 1:
            raise ValueError("The year has to be a year, like 2021.")
        year_value = int(year)
    doi_value = _strip_doi(doi.strip()) if doi.strip() else ""
    if doi.strip() and not _valid_doi(doi_value):
        raise ValueError("That is not a DOI - one looks like 10.1234/abcd.")
    if doi_value and doi_value != paper.doi:
        other = db.execute(select(models.Paper).where(
            models.Paper.project_id == paper.project_id,
            models.Paper.doi == doi_value,
            models.Paper.id != paper.id)).scalars().first()
        if other is not None:
            raise ValueError(f"Another article in this project has that DOI: "
                             f"“{other.title}”.")
    url = url.strip()
    if url and not url.lower().startswith(("http://", "https://")):
        raise ValueError("A link has to start with http:// or https://.")
    paper.title = title[:1000]
    paper.authors = [name for name in names if name][:200]
    paper.year = year_value
    paper.venue = " ".join(venue.split()) or None
    paper.doi = doi_value or None
    paper.url = url or None
    paper.work_type = work_type.strip()[:40] or None
    paper.abstract = abstract.strip() or None
    # Volume, issue and pages only change when the form sends them, so an
    # older form without the fields leaves what Crossref filled in alone.
    if volume is not None:
        paper.volume = " ".join(volume.split())[:40] or None
    if issue is not None:
        paper.issue = " ".join(issue.split())[:40] or None
    if pages is not None:
        paper.pages = re.sub(r"\s*[-‐-―]+\s*", "-", pages.strip())[:40] or None
    paper.dedupe_key = title_key(paper.title, paper.year)
    ident = db.get(models.PaperIdentification, paper.id)
    if ident is not None:
        _note(ident, "Edited", "Its details were corrected by hand.")
    db.commit()


# ------------------------------------------------------------ the prompts

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["scholarly", "not_scholarly"]},
        "confidence": {"type": "number",
                       "description": "0-1: how sure you are of the category."},
        "reason": {"type": "string",
                   "description": "One sentence: what you saw that decided it."},
        "title": {"type": "string"},
        "authors": {"type": "array", "items": {"type": "string"}},
        "organization": {"type": "string"},
        "year": {"type": ["integer", "null"]},
        "venue": {"type": "string"},
        "document_type": {"type": "string", "enum": DOCUMENT_TYPES},
        "doi": {"type": "string"},
        "identifiers": {"type": "array", "items": {"type": "string"}},
        "abstract": {"type": "string"},
        "search_query": {"type": "string"},
    },
    "required": ["category", "confidence", "reason", "title", "authors",
                 "year", "document_type", "doi", "search_query"],
}

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": "integer",
                   "description": "The number of the record that is this "
                                  "document, or -1 if none is."},
        "reason": {"type": "string",
                   "description": "One sentence: why this record, or why none."},
    },
    "required": ["choice", "reason"],
}

WEB_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "url": {"type": "string"},
        "page_title": {"type": "string",
                       "description": "The title the page shows."},
        "evidence": {"type": "string",
                     "description": "One sentence: why this page is about "
                                    "this exact document."},
    },
    "required": ["found", "url", "page_title", "evidence"],
}

WEB_TASK_INSTRUCTIONS = (WEB_INSTRUCTIONS + " If you cannot search the web "
                         "from here, answer found=false and say so in "
                         "evidence.")


def _upload_still_needed(db: Session, task: models.ClaudeTask) -> bool:
    return db.get(models.PaperIdentification, task.paper_id) is not None


tasks.register(tasks.TaskKind(
    name=models.TASK_IDENTIFY_PDF, title="Identify an uploaded PDF",
    instructions=CLASSIFY_SYSTEM, schema=CLASSIFY_SCHEMA,
    build_input=_classify_input, apply=_classify_apply,
    describe=_classify_describe, still_needed=_upload_still_needed,
    priority=50))

tasks.register(tasks.TaskKind(
    name=models.TASK_MATCH_RECORD, title="Pick the record of an uploaded PDF",
    instructions=PICK_SYSTEM, schema=PICK_SCHEMA, build_input=_pick_input,
    apply=_pick_apply, check=_pick_check, describe=_pick_describe,
    still_needed=_upload_still_needed, priority=50))

tasks.register(tasks.TaskKind(
    name=models.TASK_FIND_WEB_PAGE,
    title="Find a web page for an uploaded document",
    instructions=WEB_TASK_INSTRUCTIONS, schema=WEB_SCHEMA,
    build_input=_web_input, apply=_web_apply, describe=_web_describe,
    still_needed=_upload_still_needed, priority=60))
