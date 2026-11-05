"""The pages of LitReview Local: projects, questions, articles, the queue.

A port of the server app's web UI for one person on their own computer, and
simpler on purpose:

- No login, no accounts, no sharing. main.py's guard stands in for them:
  every request must be addressed to this machine, and anything that changes
  something must not come from another web site.
- The app never calls a model. Every step that needs one is a task in
  Claude's queue (tasks.py). So a page says plainly when something is
  waiting for Claude, and gives the sentence to say in Claude Desktop or
  Claude Code; otherwise it says what the app itself is doing.

Conventions in this module:

- Background work goes through worker.submit, after this request's own
  writes are committed. In the tests the worker runs inline, on this very
  thread, and must not wait for a write lock this request still holds.
- A call that queues tasks (research, identify, paper_texts) is committed
  with tasks.commit(db). That also starts the background steps the call
  deferred, and wakes a Claude that is waiting in next_task.
- A form that changes something answers with a redirect. Its message rides
  along in a short-lived cookie and is shown once, on the next page.
- A refusal is a short message: a page of its own for a plain request, a
  toast for an HTMX one (see static/app.js).
"""
import base64
import hashlib
import html
import importlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import timezone, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               Response)
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import (__version__, citations, documents, exports, highlight, identify,
               journals, locks, models, paper_texts, pdfs, research, services,
               tags, tasks, worker)
from .config import DEFAULT_DATA_DIR, DEFAULT_PORT, settings
from .db import SessionLocal, get_db
from .pipeline import SOURCE_LABELS, unpaywall

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
LOCAL_DIR = Path(__file__).resolve().parents[1]
BRIDGE_PATH = LOCAL_DIR / "mcp_bridge.py"
MCP_JSON_PATH = LOCAL_DIR / ".mcp.json"

# The one sentence the pages tell you to say in Claude. The MCP server offers
# the same words as its work_queue prompt.
SAY = "Work through my LitReview queue"

PAGE_SIZE = 25
FLASH_COOKIE = "litreview_flash"
# "Claude: last seen ..." counts as recent for this long.
SEEN_RECENTLY = timedelta(minutes=15)

# The connection test (Getting started): the sentence to ask Claude, how
# long the page waits for Claude to call in, how long the program Claude
# Desktop starts may take to answer, and the note of when this page wrote
# LitReview into Claude Desktop's settings.
SAY_TEST = "Use LitReview Local to list my projects"
TEST_WAIT = timedelta(minutes=10)
BRIDGE_CHECK_SECONDS = 20
DESKTOP_WRITTEN_KEY = "desktop_entry_written"
# The Claude apps a test can be for, and what each calls itself.
TEST_APPS = {"desktop": "claude-ai", "code": "claude-code"}
# How many articles "Download next" lists on a project's page.
MAX_TO_GET = 30
# What Claude Desktop calls LitReview (claude_setup.DESKTOP_NAME, for a copy
# without that module): apart from a hosted connector called "Literature review".
DESKTOP_NAME = "LitReview Local"

RESTART_NOTE = (
    "Now quit Claude Desktop completely and open it again, so it loads the "
    "change. Closing its window is not enough: on Windows, right-click the "
    "Claude icon by the clock and choose Quit; on a Mac, choose Claude > Quit "
    "Claude (Cmd-Q).")

STATUS_WORDS = {
    models.STATUS_UNSCREENED: "Not screened",
    models.STATUS_INCLUDED: "Included",
    models.STATUS_MAYBE: "Maybe",
    models.STATUS_EXCLUDED: "Excluded",
}
STAGE_WORDS = {
    models.STAGE_SCREENING: "Found by a search",
    models.STAGE_RETRIEVAL: "Chosen for reading",
}
SORTS = {
    "relevance": "Relevance",
    "newest": "Newest added",
    "year": "Publication year",
    "citations": "Most cited",
    "title": "Title",
}
TITLE_MATCH_WORDS = {
    "exact": "Exact title",
    "close": "Title contains your words",
    "abstract": "Matches the abstract",
    "loose": "Loose match",
}
LOCATED_WORDS = {
    "pdf": "Highlighted in the PDF",
    "text": "In the paper's text, not highlighted in the PDF",
    "none": "Not found in the paper - check this quote",
}


# ================================================================ templates

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _ago(stamp) -> str:
    """"3 min ago", "yesterday", "4 Nov 2026"."""
    if isinstance(stamp, str):
        try:
            stamp = datetime.fromisoformat(stamp)
        except ValueError:
            return ""
    stamp = models.aware(stamp)
    if stamp is None:
        return ""
    seconds = max(0.0, (models.utcnow() - stamp).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    return _date(stamp)


def _date(stamp) -> str:
    stamp = models.aware(stamp)
    if stamp is None:
        return ""
    local = stamp.astimezone()
    return f"{local.day} {local:%b %Y}"


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _authors(paper, limit: int = 4) -> str:
    names = [a for a in (paper.authors or []) if a]
    if not names:
        return ""
    shown = ", ".join(names[:limit])
    return shown + (" et al." if len(names) > limit else "")


templates.env.globals.update(
    SAY=SAY,
    version=__version__,
    library_folder=lambda: str(settings.data_dir),
    tier_label=journals.tier_label,
    source_label=lambda name: SOURCE_LABELS.get(name, name),
    reader_label=lambda reader: models.READER_LABELS.get(reader, reader or ""),
    ident_status=lambda status: identify.STATUS_LABELS.get(status, status),
    outcome_label=identify.outcome_label,
    upload_removable=identify.removable,
    status_words=STATUS_WORDS,
    stage_words=STAGE_WORDS,
    title_match_words=TITLE_MATCH_WORDS,
    located_words=LOCATED_WORDS,
    authors=_authors,
    plural=_plural,
    # APA 7, escaped, with <i> for the journal or book.
    reference_html=lambda paper: Markup(citations.apa_reference_html(paper)),
    # "online 2021" when the paper went online in another year than its issue.
    years_note=citations.years_note,
)
templates.env.filters["ago"] = _ago
templates.env.filters["date"] = _date
# Jinja's capitalize lower-cases the rest ("waiting for claude"); this does not.
templates.env.filters["sentence"] = lambda s: (str(s)[:1].upper() + str(s)[1:]) if s else ""
# An answer's HTML, through the same allowlist a save goes through.
templates.env.filters["clean_answer"] = (
    lambda value: Markup(citations.sanitize_summary_html(value or "")))


# ================================================================== plumbing

class PageError(Exception):
    """A refusal with a message for the person, shown as a page or a toast."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class _Route(APIRoute):
    """Answers PageError and form-validation errors with words, not JSON.

    main.py owns the app, so its exception handlers are not ours to add; a
    route class does the same for every route of this router.
    """

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                return await handler(request)
            except PageError as exc:
                return _error(request, exc.status, exc.message)
            except RequestValidationError:
                return _error(request, 400, "Something in that form was missing "
                                            "or not a number. Go back and check it.")

        return guarded


router = APIRouter(route_class=_Route)

_ERROR_TITLES = {400: "That did not work", 404: "Not found", 409: "Not right now"}


def _hx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _error(request: Request, status: int, message: str) -> Response:
    if _hx(request) and request.method == "GET" and status == 404:
        # A panel polling something that has gone (deleted in another tab):
        # take the whole window somewhere real instead of failing every poll.
        return _redirect(request, "/", message, "error")
    if _hx(request):
        # A toast rather than a swap, so the card or panel the click came
        # from stays where it is (static/app.js shows it).
        return Response(status_code=status, headers={
            "HX-Reswap": "none",
            "HX-Trigger": json.dumps({"litreview:error": {"message": message}})})
    db = SessionLocal()
    try:
        return _page(request, db, "error.html", {
            "title": _ERROR_TITLES.get(status, "Something went wrong"),
            "message": message}, status_code=status)
    finally:
        db.close()


def _flash_value(text: str, kind: str) -> str:
    raw = json.dumps({"text": text[:700], "kind": kind}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _take_flash(request: Request) -> dict | None:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("text"):
        return None
    return {"text": str(data["text"])[:700],
            "kind": "error" if data.get("kind") == "error" else "ok"}


def _redirect(request: Request, url: str, text: str | None = None,
              kind: str = "ok") -> Response:
    """Send the browser to url, with a message for the page it lands on.
    An HTMX request is sent there with HX-Redirect: a 303 would be followed
    inside the request and the next page swapped into the panel."""
    if _hx(request):
        response = Response(status_code=200, headers={"HX-Redirect": url})
    else:
        response = RedirectResponse(url, status_code=303)
    if text:
        response.set_cookie(FLASH_COOKIE, _flash_value(text, kind), max_age=120,
                            httponly=True, samesite="strict", path="/")
    return response


def _safe_next(url: str, fallback: str) -> str:
    """A path on this site to go back to, or the fallback. "//host" and
    "/\\host" are refused: a browser follows both off the site."""
    url = (url or "").strip()
    if not url.startswith("/") or url.startswith("//") or "\\" in url:
        return fallback
    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        return fallback
    return url


def _here(request: Request, fallback: str) -> str:
    """The page the browser is on: for an HTMX request that is the page the
    panel sits in, not the URL the panel was fetched from."""
    if _hx(request):
        parts = urlsplit(request.headers.get("HX-Current-URL", ""))
    else:
        parts = urlsplit(str(request.url))
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return _safe_next(path, fallback)


def _int(value, default: int = 0, lo: int | None = None,
         hi: int | None = None) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None:
        number = max(lo, number)
    if hi is not None:
        number = min(hi, number)
    return number


async def _form_data(request: Request):
    """The whole form, for routes whose field names are made of ids
    (score_<id>). Starlette caches it, so declared fields still work."""
    return await request.form()


def _json_for_script(value) -> str:
    """JSON safe inside <script type="application/json">."""
    return (json.dumps(value, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026"))


# ------------------------------------------------------------ Claude seen

def _seen(db: Session) -> dict | None:
    """When Claude last called in, or None if it never has."""
    seen = tasks.last_seen(db)
    if not seen or not seen.get("at"):
        return None
    try:
        at = models.aware(datetime.fromisoformat(seen["at"]))
    except (TypeError, ValueError):
        return None
    client = seen.get("client") or "Claude"
    return {"at": at, "ago": _ago(at),
            "client": CLIENT_NAMES.get(client, client),
            "recent": models.utcnow() - at < SEEN_RECENTLY}


# What the Claude apps call themselves when they connect, and what a person
# calls them.
CLIENT_NAMES = {"claude-ai": "Claude Desktop", "claude-code": "Claude Code"}


def _app_ever(db: Session, client: str) -> dict | None:
    """When one Claude app ("claude-ai" is Claude Desktop, "claude-code"
    Claude Code) last called in, however long ago; None if it never has."""
    seen = tasks.last_seen(db) or {}
    stamp = (seen.get("clients") or {}).get(client)
    if not stamp and seen.get("client") == client:  # kept before apps were told apart
        stamp = seen.get("at")
    if not stamp:
        return None
    try:
        at = models.aware(datetime.fromisoformat(stamp))
    except (TypeError, ValueError):
        return None
    return {"at": at, "ago": _ago(at)}


def _app_seen(db: Session, client: str, since=None) -> dict | None:
    """When one Claude app last called in - and only if that was recently,
    and after `since`. For Claude Desktop, since is the moment its settings
    got LitReview: a call from before then does not mean it has picked it
    up."""
    ever = _app_ever(db, client)
    if ever is None:
        return None
    at = ever["at"]
    if since is not None and at < since:
        return None
    if models.utcnow() - at >= SEEN_RECENTLY:
        return None
    return {"at": at, "ago": _ago(at)}


def _indicator(db: Session) -> dict:
    counts = tasks.pending_counts(db)
    return {"seen": _seen(db), "waiting": counts["total"],
            "claimed": counts["claimed"]}


def _page(request: Request, db: Session, name: str, context: dict | None = None,
          status_code: int = 200) -> Response:
    flash = _take_flash(request)
    ctx = {"indicator": _indicator(db), "flash": flash, **(context or {})}
    response = templates.TemplateResponse(request, name, ctx,
                                          status_code=status_code)
    if flash:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def _partial(request: Request, name: str, context: dict,
             status_code: int = 200, headers: dict | None = None) -> Response:
    return templates.TemplateResponse(request, name, context,
                                      status_code=status_code, headers=headers)


# ------------------------------------------------------------- the rows

def _project(db: Session, project_id: int) -> models.Project:
    project = db.get(models.Project, project_id)
    if project is None:
        raise PageError(404, "There is no such project. It may have been deleted.")
    return project


def _question(db: Session, question_id: int) -> models.ResearchQuestion:
    rq = db.get(models.ResearchQuestion, question_id)
    if rq is None or rq.project is None:
        raise PageError(404, "There is no such research question.")
    return rq


def _paper(db: Session, paper_id: int) -> models.Paper:
    paper = db.get(models.Paper, paper_id)
    if paper is None or paper.project is None:
        raise PageError(404, "There is no such article. It may have been removed.")
    return paper


def _paper_in(db: Session, project: models.Project, paper_id: int) -> models.Paper:
    paper = _paper(db, paper_id)
    if paper.project_id != project.id:
        raise PageError(404, "That article is not in this project.")
    return paper


def _annotation(db: Session, annotation_id: int) -> models.Annotation:
    annotation = db.get(models.Annotation, annotation_id)
    if annotation is None or annotation.paper is None \
            or annotation.paper.project is None:
        raise PageError(404, "There is no such reading of a paper.")
    return annotation


def _document(db: Session, document_id: int) -> models.ProjectDocument:
    document = db.get(models.ProjectDocument, document_id)
    if document is None or document.project is None:
        raise PageError(404, "There is no such document.")
    return document


def _question_paper_ids(rq_id: int):
    return (select(models.search_papers.c.paper_id)
            .select_from(models.search_papers.join(
                models.Search,
                models.Search.id == models.search_papers.c.search_id))
            .where(models.Search.research_question_id == rq_id))


def _live_paper_ids(db: Session, kind: str, paper_ids: list[int]) -> set[int]:
    """Which of these papers have a task of this kind waiting for Claude."""
    if not paper_ids:
        return set()
    return set(db.execute(
        select(models.ClaudeTask.paper_id).where(
            models.ClaudeTask.kind == kind,
            models.ClaudeTask.status.in_(models.TASK_LIVE_STATUSES),
            models.ClaudeTask.paper_id.in_(paper_ids))
    ).scalars().all())


def _questions_of(db: Session, paper_ids: list[int]) -> dict[int, list]:
    """paper_id -> the research questions holding it, in label order."""
    if not paper_ids:
        return {}
    rows = db.execute(
        select(models.search_papers.c.paper_id, models.ResearchQuestion)
        .select_from(models.search_papers)
        .join(models.Search, models.Search.id == models.search_papers.c.search_id)
        .join(models.ResearchQuestion,
              models.ResearchQuestion.id == models.Search.research_question_id)
        .where(models.search_papers.c.paper_id.in_(paper_ids))
    ).all()
    out: dict[int, dict[int, models.ResearchQuestion]] = {}
    for paper_id, rq in rows:
        out.setdefault(paper_id, {})[rq.id] = rq
    return {pid: sorted(qs.values(), key=lambda q: q.number or 0)
            for pid, qs in out.items()}


def _store_uploaded_pdf(db: Session, paper: models.Paper,
                        content: bytes) -> str | None:
    """Save a PDF the person supplied for an article. Returns a refusal, or
    None once it is stored and committed. The caller schedules the reading.

    The failures it replaces are history the moment a PDF arrives, so their
    messages go. A text kept from an earlier PDF goes too: it would not be
    the text of this one.
    """
    if content[:5] != b"%PDF-":
        return "That file is not a PDF."
    if len(content) > pdfs.MAX_PDF_BYTES:
        return (f"That PDF is larger than {pdfs.MAX_PDF_BYTES // (1024 * 1024)} MB, "
                "the most LitReview keeps.")
    dest = pdfs.original_path(paper.project.slug, paper.id)
    with locks.for_paper(paper.id):
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".upload.tmp")
        tmp.write_bytes(content)
        tmp.replace(dest)
        stale = db.get(models.PaperText, paper.id)
        if stale is not None:
            db.delete(stale)
    tasks.cancel(db, "A new PDF was uploaded", paper_id=paper.id,
                 kinds=[models.TASK_PARSE_PDF])
    paper.pdf_status = models.PDF_DOWNLOADED
    paper.pdf_error = None
    for annotation in paper.annotations:
        if annotation.status in (models.ANNOT_FAILED, models.ANNOT_NEEDS_OCR):
            annotation.error = None
    # Uploading it is choosing it - and takes a lookup hit into the project.
    if paper.stage in (models.STAGE_SCREENING, models.STAGE_LOOKUP):
        paper.stage = models.STAGE_RETRIEVAL
    db.commit()
    return None


def _upload_content(upload: UploadFile) -> bytes:
    upload.file.seek(0)
    return upload.file.read()


# ================================================================ the shell

@router.get("/claude-status", response_class=HTMLResponse)
def claude_status(request: Request, db: Session = Depends(get_db)):
    """The header's Claude indicator, polled every 30 seconds."""
    return _partial(request, "_claude_indicator.html",
                    {"indicator": _indicator(db)})


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers ask for it on every page; the pages name an SVG icon."""
    return Response(status_code=204)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    """The projects, and nothing else: setting up is on Getting started."""
    return _page(request, db, "dashboard.html", {
        "overview": services.projects_overview(db),
        "set_up": bool(settings.contact_email) and _seen(db) is not None,
    })


# ================================================================= projects

@router.post("/projects")
def create_project(request: Request, db: Session = Depends(get_db),
                   name: str = Form(default=""),
                   description: str = Form(default="")):
    try:
        project = services.create_project(db, name, description)
    except ValueError as exc:
        message = str(exc)
        if message == "Project name required":
            message = "Give the project a name."
        return _redirect(request, "/#new-project", message, "error")
    return _redirect(request, f"/projects/{project.id}",
                     f"Created {project.name}. Ask its first research question below.")


def _short_title(paper: models.Paper) -> str:
    title = (paper.title or "an article").strip()
    return "“" + (title if len(title) <= 70 else title[:67].rstrip() + "…") + "”"


def _reading_label(paper: models.Paper) -> str:
    return f"reading the PDF you uploaded: {_short_title(paper)}"


def _project_status(db: Session, project: models.Project) -> dict:
    """The top of a project's page: tasks waiting for Claude, and what
    LitReview itself is doing. seen sums it up, so the page's asking again
    is answered 204 until something changed."""
    stats = services.project_stats(db, project)
    activity = tasks.background_activity(db, project.id)
    seen = hashlib.sha1(json.dumps([stats["waiting_for_claude"], activity],
                                   ensure_ascii=False).encode()).hexdigest()[:12]
    return {"project": project, "stats": stats, "activity": activity,
            "status_seen": seen}


def _project_context(db: Session, project: models.Project) -> dict:
    questions = list(project.questions)
    live = tasks.live_tasks(db, project_id=project.id)
    per_question: dict[int, int] = {}
    for task in live:
        if task.research_question_id:
            per_question[task.research_question_id] = \
                per_question.get(task.research_question_id, 0) + 1
    docs = list(project.documents)
    parsing_docs = {t.document_id for t in live
                    if t.kind == models.TASK_PARSE_PDF and t.document_id}
    return {
        **_project_status(db, project),
        "questions": questions,
        "updates": research.update_counts(db, project.id),
        "question_tasks": per_question,
        "uploads": identify.open_uploads(db, project.id),
        "documents": docs,
        "parsing_docs": parsing_docs,
        "source_labels": SOURCE_LABELS,
        "to_get": services.articles_to_get(db, project),
        "important_score": services.IMPORTANT_SCORE,
        "max_to_get": MAX_TO_GET,
    }


@router.get("/projects/{project_id}/status", response_class=HTMLResponse)
def project_status(request: Request, project_id: int, seen: str = "",
                   db: Session = Depends(get_db)):
    """The top of a project's page asking again while LitReview works. 204
    (the page stays as it is) until something changed; once the work is
    done the answer no longer asks again, so the spinner stops."""
    status = _project_status(db, _project(db, project_id))
    if status["activity"] and seen == status["status_seen"]:
        return Response(status_code=204)
    return _partial(request, "_project_status.html", status)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, project_id: int, db: Session = Depends(get_db)):
    project = _project(db, project_id)
    return _page(request, db, "project.html", _project_context(db, project))


@router.post("/projects/{project_id}/delete")
def delete_project(request: Request, project_id: int,
                   db: Session = Depends(get_db),
                   confirm_name: str = Form(default="")):
    project = _project(db, project_id)
    if confirm_name.strip() != project.name:
        return _redirect(request, f"/projects/{project.id}#delete",
                         f"Type the project's name exactly - {project.name} - to "
                         "delete it. Nothing was deleted.", "error")
    name = project.name
    try:
        services.delete_project(db, project)
    except ValueError as exc:
        return _redirect(request, f"/projects/{project.id}#delete", str(exc), "error")
    logger.info("Deleted project %s from the web page", name)
    return _redirect(request, "/", f"Deleted {name} and everything in it.")


# ---------------------------------------------------------------- questions

@router.post("/projects/{project_id}/questions")
def new_question(request: Request, project_id: int,
                 db: Session = Depends(get_db), text: str = Form(default="")):
    project = _project(db, project_id)
    text = text.strip()
    if len(text) < 10:
        return _redirect(request, f"/projects/{project.id}#new-question",
                         "Write the research question out in full - it drives the "
                         "search, the ranking and the reading.", "error")
    with services.question_number_lock(project.id):
        rq = models.ResearchQuestion(project_id=project.id, text=text,
                                     created_by="web")
        db.add(rq)
        db.flush()
        research.start_question(db, rq, source="web", first_run=True)
    return _redirect(request, f"/questions/{rq.id}")


# ------------------------------------------------------- documents

@router.post("/projects/{project_id}/documents")
def upload_document(request: Request, project_id: int,
                    db: Session = Depends(get_db),
                    file: UploadFile = File(...),
                    title: str = Form(default=""),
                    description: str = Form(default="")):
    project = _project(db, project_id)
    back = f"/projects/{project.id}#documents"
    if not file.filename:
        return _redirect(request, back, "Choose a PDF first.", "error")
    try:
        document = documents.store_upload(
            db, project, title=title, description=description,
            filename=file.filename or "", data=_upload_content(file))
    except documents.DocumentError as exc:
        return _redirect(request, back, f"{exc}. Nothing was added.", "error")
    worker.submit(documents.extract_document, document.id)
    return _redirect(request, back,
                     f"Added “{document.title}”. LitReview is reading its text now.")


@router.get("/documents/{document_id}", response_class=HTMLResponse)
def document_page(request: Request, document_id: int,
                  db: Session = Depends(get_db)):
    document = _document(db, document_id)
    content = document.content or ""
    return _page(request, db, "document.html", {
        "d": document, "project": document.project,
        "parse_waiting": paper_texts.parse_requested(db, document=document),
        "outline": documents.outline(content)[:60],
        "preview": content[:80_000], "cut": len(content) > 80_000,
        "has_pdf": pdfs.document_path(document.project.slug, document.id).exists(),
    })


@router.get("/documents/{document_id}/pdf")
def document_pdf(document_id: int, db: Session = Depends(get_db),
                 download: int = 0):
    document = _document(db, document_id)
    path = pdfs.document_path(document.project.slug, document.id)
    if not path.exists():
        raise PageError(404, "The PDF of this document is not on this computer any more.")
    return FileResponse(str(path), media_type="application/pdf",
                        filename=document.filename or f"document-{document.id}.pdf",
                        content_disposition_type="attachment" if download else "inline")


@router.post("/documents/{document_id}/reread")
def reread_document(request: Request, document_id: int,
                    db: Session = Depends(get_db)):
    document = _document(db, document_id)
    document.status = models.DOC_PENDING
    document.error = None
    db.commit()
    worker.submit(documents.extract_document, document.id)
    return _redirect(request, f"/documents/{document.id}",
                     "Reading the PDF's text again."
                     + (" Claude's parse is kept." if document.reader == models.READER_CLAUDE
                        else ""))


@router.post("/documents/{document_id}/parse")
def parse_document(request: Request, document_id: int,
                   db: Session = Depends(get_db),
                   next_url: str = Form(default="", alias="next")):
    document = _document(db, document_id)
    back = _safe_next(next_url, f"/documents/{document.id}")
    queued = paper_texts.request_parse(db, document=document, priority=30)
    tasks.commit(db)
    if queued:
        return _redirect(request, back,
                         f"Asked Claude to parse “{document.title}” "
                         f"({_plural(queued, 'part')}). Say “{SAY}” in Claude.")
    if paper_texts.parse_requested(db, document=document):
        return _redirect(request, back, "A parse of this document is already "
                                        "waiting for Claude.")
    return _redirect(request, back, "The PDF of this document is missing or cannot "
                                    "be opened, so it cannot be parsed.", "error")


@router.post("/documents/{document_id}/delete")
def delete_document(request: Request, document_id: int,
                    db: Session = Depends(get_db)):
    document = _document(db, document_id)
    project_id, slug, doc_id, title = (document.project_id, document.project.slug,
                                       document.id, document.title)
    db.delete(document)
    db.commit()
    documents.remove_files(slug, doc_id)
    return _redirect(request, f"/projects/{project_id}#documents",
                     f"Deleted the document “{title}”.")


# ------------------------------------------- adding articles from their PDFs

@router.post("/projects/{project_id}/uploads", response_class=HTMLResponse)
def upload_articles(request: Request, project_id: int,
                    db: Session = Depends(get_db),
                    files: list[UploadFile] = File(default=[]),
                    next_url: str = Form(default="", alias="next")):
    """One new article per PDF, each identified in the background. From the
    project page or the article list (next: where to go back to)."""
    project = _project(db, project_id)
    back = _safe_next(next_url, f"/projects/{project.id}#uploads")
    files = [f for f in files if f.filename]
    if not files:
        if _hx(request):
            raise PageError(400, "Choose one or more PDFs first.")
        return _redirect(request, back, "Choose one or more PDFs first.", "error")
    rows, queued, refused = [], [], []
    if len(files) > identify.MAX_FILES:
        refused.append(f"At most {identify.MAX_FILES} PDFs at a time - none of "
                       "these were added.")
        rows.append({"error": refused[-1]})
        files = []
    for upload in files:
        try:
            paper = identify.start(db, project, upload.filename or "",
                                   _upload_content(upload))
        except identify.UploadError as exc:
            refused.append(str(exc))
            rows.append({"error": str(exc)})
            continue
        rows.append({"paper_id": paper.id})
        queued.append(paper.id)
    if queued:
        worker.submit(identify.run_many, queued)
    if not _hx(request):
        if refused:
            return _redirect(request, back, " ".join(refused), "error")
        return _redirect(request, back,
                         f"Added {_plural(len(queued), 'PDF')}. Each is read, then "
                         "Claude finds out what it is.")
    db.expire_all()
    for row in rows:
        if "paper_id" in row:
            row["ident"] = db.get(models.PaperIdentification, row["paper_id"])
    return _partial(request, "_upload_rows.html", {"rows": rows, "project": project})


@router.get("/projects/{project_id}/uploads/{paper_id}", response_class=HTMLResponse)
def upload_row(request: Request, project_id: int, paper_id: int,
               db: Session = Depends(get_db)):
    """One upload's row, which polls itself while it is being identified.
    Never a 404 for an upload that is merely gone: it may have been folded
    into an article the project already had."""
    project = _project(db, project_id)
    ident = db.get(models.PaperIdentification, paper_id)
    if ident is not None and ident.paper is not None \
            and ident.paper.project_id == project.id:
        return _partial(request, "_upload_row.html", {"i": ident, "project": project})
    twin_id = identify.merged_into(paper_id)
    twin = db.get(models.Paper, twin_id) if twin_id else None
    if twin is not None and twin.project_id != project.id:
        twin = None
    return _partial(request, "_upload_gone.html", {
        "project": project, "paper_id": paper_id, "twin": twin,
        "twin_ident": db.get(models.PaperIdentification, twin.id) if twin else None})


def _upload_of(db: Session, project_id: int,
               paper_id: int) -> tuple[models.Project, models.PaperIdentification]:
    project = _project(db, project_id)
    paper = _paper_in(db, project, paper_id)
    ident = db.get(models.PaperIdentification, paper.id)
    if ident is None:
        raise PageError(404, "That article was not added from a PDF.")
    return project, ident


@router.post("/projects/{project_id}/uploads/{paper_id}/answer",
             response_class=HTMLResponse)
def upload_answer(request: Request, project_id: int, paper_id: int,
                  db: Session = Depends(get_db), answer: str = Form(default="")):
    """Yes or no to "Is this it?". No goes on to a web search by Claude."""
    project, ident = _upload_of(db, project_id, paper_id)
    if answer not in ("yes", "no"):
        raise PageError(400, "Answer yes or no.")
    if identify.answer(db, ident, answer == "yes"):
        tasks.commit(db)
    db.expire_all()
    ident = db.get(models.PaperIdentification, paper_id)
    if ident is None:
        return upload_row(request, project_id, paper_id, db)
    return _partial(request, "_upload_row.html", {"i": ident, "project": project})


@router.post("/projects/{project_id}/uploads/{paper_id}/retry",
             response_class=HTMLResponse)
def upload_retry(request: Request, project_id: int, paper_id: int,
                 db: Session = Depends(get_db)):
    project, ident = _upload_of(db, project_id, paper_id)
    if identify.retry(db, ident):
        worker.submit(identify.run, ident.paper_id)
    db.expire_all()
    ident = db.get(models.PaperIdentification, paper_id)
    if ident is None:
        return upload_row(request, project_id, paper_id, db)
    return _partial(request, "_upload_row.html", {"i": ident, "project": project})


@router.post("/projects/{project_id}/uploads/{paper_id}/remove",
             response_class=HTMLResponse)
def upload_remove(request: Request, project_id: int, paper_id: int,
                  db: Session = Depends(get_db)):
    """Take an upload back: the article and its PDF go."""
    _, ident = _upload_of(db, project_id, paper_id)
    if not identify.removable(ident):
        raise PageError(409, "It is being worked on, or a research question holds "
                             "it now. Exclude it on its card instead.")
    identify.remove(db, ident)
    return HTMLResponse("")


# ============================================================ the question

def _answer_base(rq: models.ResearchQuestion) -> str:
    """A digest of the stored answer: a save from a page showing an older
    one is refused instead of overwriting what Claude wrote meanwhile."""
    return hashlib.sha1((rq.summary_html or "").encode("utf-8")).hexdigest()


_CITE_OPEN = re.compile(r'<cite class="cit" data-k="([0-9]+-[0-9a-f]+)">')


def _mark_verdicts(summary_html: str, check: dict | None) -> str:
    """Put Claude's verdict on each citation it checked, as attributes the
    stylesheet draws a badge from and the hover popup reads the note from.
    Attributes rather than markup inside the answer: the editor's content is
    what gets saved, and the sanitizer drops them on the way back in."""
    verdicts = (check or {}).get("verdicts") or {}
    if not verdicts:
        return summary_html

    def mark(match: re.Match) -> str:
        verdict = verdicts.get(match.group(1))
        if not verdict or verdict.get("verdict") not in ("supported", "partial",
                                                          "unsupported"):
            return match.group(0)
        note = html.escape(verdict.get("note") or "", quote=True)
        return (f'<cite class="cit" data-k="{match.group(1)}" '
                f'data-verdict="{verdict["verdict"]}" data-note="{note}">')

    return _CITE_OPEN.sub(mark, summary_html)


def _verification_view(db: Session, rq: models.ResearchQuestion) -> dict:
    check = research.verification_for(rq)
    pending = bool(tasks.live_tasks(db, research_question_id=rq.id,
                                    kinds=[models.TASK_VERIFY_ANSWER]))
    counts = {"supported": 0, "partial": 0, "unsupported": 0}
    for verdict in ((check or {}).get("verdicts") or {}).values():
        if verdict.get("verdict") in counts:
            counts[verdict["verdict"]] += 1
    checkable = any(s["keys"] for s in research.answer_sentences(rq)) \
        if rq.summary_html else False
    return {"check": check, "pending": pending, "counts": counts,
            "checkable": checkable}


def _summary_context(db: Session, rq: models.ResearchQuestion,
                     annotations: list | None = None) -> dict:
    if annotations is None:
        annotations = research.question_annotations(db, rq.id)
    cmap = citations.citation_map(annotations)
    papers_by_id = {a.paper.id: a.paper for a in annotations if a.paper}
    revision = tasks.live_tasks(db, research_question_id=rq.id,
                                kinds=[models.TASK_REVISE_ANSWER])
    verification = _verification_view(db, rq)
    shown = citations.flag_missing_citations(rq.summary_html or "", cmap)
    return {
        "rq": rq,
        "summary_html": _mark_verdicts(shown, verification["check"]),
        "summary_base": _answer_base(rq),
        "references": citations.reference_list(rq.summary_html or "", cmap,
                                               papers_by_id),
        "revision": revision[0] if revision else None,
        "verification": verification,
    }


def _run_view(db: Session, rq: models.ResearchQuestion) -> dict:
    live = tasks.live_tasks(db, research_question_id=rq.id) if rq.running else []
    return {"tasks": len(live),
            "claimed": sum(1 for t in live if t.status == models.TASK_CLAIMED),
            "app_working": rq.status in models.RQ_APP_STATUSES}


def _included_context(db: Session, rq: models.ResearchQuestion,
                      show_all: bool = False) -> dict:
    """What the question holds. By default only what carries or awaits
    evidence: a run leaves every hit of its search attached, and listing a
    hundred never-fetched results would bury the handful that matter."""
    rows = research.question_papers(db, rq)
    evidence = [r for r in rows if r["annotation"] or r["has_pdf"]
                or r["paper"].pdf_status in (models.PDF_FAILED, models.PDF_PENDING)]
    shown = rows if show_all else evidence
    ids = [r["paper"].id for r in shown]
    return {"rq": rq, "included": shown, "unfetched": len(rows) - len(evidence),
            "total": len(rows), "show_all": show_all,
            "parsing": _live_paper_ids(db, models.TASK_PARSE_PDF, ids),
            "texts": paper_texts.summaries(db, ids)}


def _question_context(db: Session, rq: models.ResearchQuestion) -> dict:
    annotations = research.question_annotations(db, rq.id)
    cmap = citations.citation_map(annotations)
    groups = [{"annotation": a, "paper": a.paper,
               "items": citations.annotation_items(a, cmap)} for a in annotations]
    return {
        **_summary_context(db, rq, annotations),
        **_included_context(db, rq),
        "project": rq.project,
        "updates": research.question_updates(db, rq),
        "run": _run_view(db, rq),
        "groups": groups,
        "papers_read": len(groups),
        "papers_without_findings": sum(1 for g in groups if not g["items"]),
        "cmap_json": _json_for_script(cmap),
    }


@router.get("/questions/{question_id}", response_class=HTMLResponse)
def question_page(request: Request, question_id: int,
                  db: Session = Depends(get_db)):
    rq = _question(db, question_id)
    return _page(request, db, "question.html", _question_context(db, rq))


@router.get("/questions/{question_id}/status", response_class=HTMLResponse)
def question_status(request: Request, question_id: int,
                    db: Session = Depends(get_db)):
    """Polled while the question runs. When it settles, the whole page is
    reloaded, so the answer and the evidence appear with it."""
    rq = _question(db, question_id)
    headers = {} if rq.running else {"HX-Refresh": "true"}
    return _partial(request, "_question_status.html",
                    {"rq": rq, "run": _run_view(db, rq)}, headers=headers)


@router.post("/questions/{question_id}/run")
def run_question(request: Request, question_id: int,
                 db: Session = Depends(get_db),
                 mode: str = Form(default="full"), text: str = Form(default="")):
    """Run a question again: "full" plans and searches anew, "evidence" reads
    what it has not read and rewrites the answer, with no new search. The
    person chooses; nothing decides it for them."""
    rq = _question(db, question_id)
    back = f"/questions/{rq.id}"
    if mode not in ("full", "evidence"):
        raise PageError(400, "Choose Search again or Rewrite from the evidence.")
    if rq.running:
        return _redirect(request, back, f"{rq.label} is running "
                         f"({rq.status_label.lower()}). Stop it first, or let it "
                         "finish.", "error")
    text = text.strip()
    if text and len(text) < 10:
        return _redirect(request, back + "#question", "Write the research question "
                         "out in full.", "error")
    if text and text != rq.text:
        rq.text = text
    try:
        research.start_question(db, rq, mode=mode, source="web")
    except services.RunRefused as exc:
        db.rollback()
        return _redirect(request, back, str(exc), "error")
    if mode == "full":
        message = (f"Searching again. Claude plans the search first: say “{SAY}” "
                   "in Claude.")
    else:
        message = ("Rewriting from the evidence you have - no new search. Claude "
                   "reads any PDF not read yet, then writes the answer again.")
    return _redirect(request, back, message)


@router.post("/questions/{question_id}/stop")
def stop_question(request: Request, question_id: int,
                  db: Session = Depends(get_db)):
    rq = _question(db, question_id)
    back = f"/questions/{rq.id}"
    if not rq.running:
        return _redirect(request, back, f"{rq.label} is not running.")
    withdrawn = research.stop_question(db, rq)
    return _redirect(request, back, f"Stopped {rq.label}. What was collected stays"
                     + (f"; {_plural(withdrawn, 'waiting task')} "
                        f"{'was' if withdrawn == 1 else 'were'} withdrawn."
                        if withdrawn else "."))


# ------------------------------------------------------------- the answer

@router.get("/questions/{question_id}/summary", response_class=HTMLResponse)
def summary_block(request: Request, question_id: int,
                  db: Session = Depends(get_db)):
    """The answer block on its own - polled while a revision is waiting."""
    rq = _question(db, question_id)
    return _partial(request, "_question_summary.html", _summary_context(db, rq))


@router.get("/questions/{question_id}/verification", response_class=HTMLResponse)
def verification_block(request: Request, question_id: int,
                       db: Session = Depends(get_db)):
    """The citation check's box, polled while the check waits for Claude.
    When it has landed, the page is told so (litreview:verified): it reloads
    to put the marks on the citations, unless an edit is still unsaved."""
    rq = _question(db, question_id)
    view = _verification_view(db, rq)
    headers = ({"HX-Trigger": "litreview:verified"}
               if view["check"] and not view["pending"] else None)
    return _partial(request, "_verification.html",
                    {"rq": rq, "verification": view}, headers=headers)


@router.post("/questions/{question_id}/summary", response_class=HTMLResponse)
def save_summary(request: Request, question_id: int,
                 db: Session = Depends(get_db),
                 summary_html: str = Form(default=""),
                 summary_base: str = Form(default="")):
    rq = _question(db, question_id)
    if summary_base and summary_base != _answer_base(rq):
        # Claude wrote the answer meanwhile (a run, a revision). The edit is
        # handed back rather than written over it, or thrown away.
        context = _summary_context(db, rq)
        context["conflict"] = ("The answer was changed since this page showed "
                               "it, so your edit was not saved. It is below: copy "
                               "what you need, then reload the page.")
        context["rejected_html"] = summary_html
        return _partial(request, "_question_summary.html", context, status_code=409)
    rq.summary_html = citations.sanitize_summary_html(summary_html)
    db.commit()
    context = _summary_context(db, rq)
    context["saved"] = True
    return _partial(request, "_question_summary.html", context)


@router.post("/questions/{question_id}/revise", response_class=HTMLResponse)
def revise_answer(request: Request, question_id: int,
                  db: Session = Depends(get_db),
                  instruction: str = Form(default="")):
    """Ask Claude to change the answer: a revise_answer task. The answer
    block waits for it and swaps itself when it lands."""
    rq = _question(db, question_id)
    instruction = instruction.strip()
    if not instruction:
        raise PageError(400, "Say what Claude should change.")
    if not rq.summary_html:
        raise PageError(400, "There is no answer to change yet.")
    if rq.running:
        raise PageError(409, f"{rq.label} is running; ask for changes once it is done.")
    research.request_revision(db, rq, instruction)
    tasks.commit(db)
    if not _hx(request):
        return _redirect(request, f"/questions/{rq.id}#summary-block",
                         f"Asked Claude to change the answer. Say “{SAY}” in Claude.")
    return _partial(request, "_question_summary.html", _summary_context(db, rq))


@router.post("/questions/{question_id}/revise/cancel", response_class=HTMLResponse)
def cancel_revision(request: Request, question_id: int,
                    db: Session = Depends(get_db)):
    rq = _question(db, question_id)
    tasks.cancel(db, "Cancelled by you", research_question_id=rq.id,
                 kinds=[models.TASK_REVISE_ANSWER])
    db.commit()
    if not _hx(request):
        return _redirect(request, f"/questions/{rq.id}#summary-block",
                         "The request to Claude was cancelled.")
    return _partial(request, "_question_summary.html", _summary_context(db, rq))


# ------------------------------------------------- the articles it holds

@router.get("/questions/{question_id}/included", response_class=HTMLResponse)
def included_panel(request: Request, question_id: int,
                   db: Session = Depends(get_db), all: int = 0):
    rq = _question(db, question_id)
    return _partial(request, "_included_papers.html",
                    _included_context(db, rq, show_all=bool(all)))


@router.post("/questions/{question_id}/papers/{paper_id}/remove")
def remove_from_question(request: Request, question_id: int, paper_id: int,
                         db: Session = Depends(get_db)):
    rq = _question(db, question_id)
    back = f"/questions/{rq.id}#included-papers"
    if rq.running:
        return _redirect(request, back, f"{rq.label} is running - wait for it to "
                         "finish before changing what it holds.", "error")
    paper = _paper_in(db, rq.project, paper_id)
    title = paper.title
    removed = research.remove_paper_from_question(db, rq, paper)
    return _redirect(request, back, f"Took “{title}” out of {rq.label}"
                     + (f"; its {_plural(removed, 'reading')} went with it. Anything "
                        "the answer quoted from it reads as unavailable until you "
                        "rewrite the answer." if removed else
                        ". It had not been read for this question.")
                     + " The project keeps the article.")


@router.post("/questions/{question_id}/find", response_class=HTMLResponse)
def find_for_question(request: Request, question_id: int,
                      db: Session = Depends(get_db), query: str = Form(default="")):
    """Look up one known article in the sources and in the project."""
    rq = _question(db, question_id)
    query = query.strip()
    candidates = research.find_candidates(db, rq.project, query, rq=rq) if query else []
    return _partial(request, "_include_candidates.html", {
        "rq": rq, "query": query, "candidates": candidates,
        "journal_tiers": research.journal_tiers(db, [c["paper"] for c in candidates])})


@router.post("/questions/{question_id}/include")
def include_in_question(request: Request, question_id: int,
                        db: Session = Depends(get_db), form=Depends(_form_data)):
    """Add the ticked articles. A score typed in is used as given; Claude
    scores the rest. Then the app fetches the PDFs and Claude reads them."""
    rq = _question(db, question_id)
    back = f"/questions/{rq.id}#included-papers"
    if rq.running:
        return _redirect(request, back, f"{rq.label} is running. Add articles once "
                         "it is done, so their readings are not left out of the "
                         "answer.", "error")
    ids = list(dict.fromkeys(_int(v) for v in form.getlist("paper_ids")))
    papers = db.execute(
        select(models.Paper).where(models.Paper.id.in_([i for i in ids if i]),
                                   models.Paper.project_id == rq.project_id)
    ).scalars().all()
    if not papers:
        return _redirect(request, f"/questions/{rq.id}#add-article",
                         "Tick the articles to add first.", "error")
    scores: dict[int, float] = {}
    for paper in papers:
        raw = (form.get(f"score_{paper.id}") or "").strip()
        if raw:
            try:
                scores[paper.id] = max(0.0, min(100.0, float(raw)))
            except ValueError:
                pass
    worker.submit(research.include_and_read, rq.id, [p.id for p in papers], scores)
    unscored = len(papers) - len(scores)
    return _redirect(request, back, f"Adding {_plural(len(papers), 'article')} to "
                     f"{rq.label}. "
                     + (f"Claude scores {'it' if unscored == 1 else 'them'} first; "
                        if unscored else "")
                     + "the app fetches the PDFs and Claude reads them. The answer "
                     f"waits until you ask for a rewrite. Say “{SAY}” in Claude.")


@router.post("/questions/{question_id}/candidates/{paper_id}/upload",
             response_class=HTMLResponse)
def upload_for_candidate(request: Request, question_id: int, paper_id: int,
                         db: Session = Depends(get_db),
                         file: UploadFile = File(...)):
    """A PDF for an article straight from the lookup, without leaving it."""
    rq = _question(db, question_id)
    paper = _paper_in(db, rq.project, paper_id)
    problem = _store_uploaded_pdf(db, paper, _upload_content(file))
    if problem:
        raise PageError(400, problem)
    worker.submit_as(research.annotate_uploaded_paper, paper.id,
                     label=_reading_label(paper), project_id=paper.project_id)
    db.expire_all()
    paper = db.get(models.Paper, paper_id)
    return _partial(request, "_candidate_row.html", {
        "c": research.candidate_state(db, paper, rq), "rq": rq,
        "just_uploaded": True, "journal_tiers": research.journal_tiers(db, [paper])})


# ===================================================== the article list

def _paper_query(project: models.Project, *, status: str = "", stage: str = "",
                 pdf: str = "", tag: str = "", q: str = "", question_id: int = 0):
    stmt = select(models.Paper).where(models.Paper.project_id == project.id,
                                      models.Paper.stage != models.STAGE_LOOKUP)
    if status in models.STATUSES:
        stmt = stmt.where(models.Paper.status == status)
    if stage in models.STAGES:
        stmt = stmt.where(models.Paper.stage == stage)
    if pdf == "yes":
        stmt = stmt.where(models.Paper.pdf_status == models.PDF_DOWNLOADED)
    elif pdf == "no":
        stmt = stmt.where(models.Paper.pdf_status != models.PDF_DOWNLOADED)
    if tag.strip():
        stmt = stmt.where(models.Paper.tags.any(models.Tag.key == models.tag_key(tag)))
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(models.Paper.title.ilike(like),
                              models.Paper.abstract.ilike(like),
                              models.Paper.venue.ilike(like),
                              models.Paper.note.ilike(like)))
    if question_id:
        stmt = stmt.where(models.Paper.id.in_(_question_paper_ids(question_id)))
    return stmt


def _sorted(stmt, sort: str):
    if sort == "newest":
        return stmt.order_by(models.Paper.id.desc())
    if sort == "year":
        return stmt.order_by(models.Paper.year.desc().nullslast(), models.Paper.id)
    if sort == "citations":
        return stmt.order_by(models.Paper.citation_count.desc().nullslast(),
                             models.Paper.id)
    if sort == "title":
        return stmt.order_by(func.lower(models.Paper.title), models.Paper.id)
    return stmt.order_by(models.Paper.llm_score.desc().nullslast(),
                         models.Paper.citation_count.desc().nullslast(),
                         models.Paper.id)


def _card_context(request: Request, db: Session, papers: list,
                  project: models.Project) -> dict:
    """What the article cards need beyond the papers themselves."""
    ids = [p.id for p in papers]
    return {
        "texts": paper_texts.summaries(db, ids),
        "idents": identify.for_papers(db, ids),
        "parsing": _live_paper_ids(db, models.TASK_PARSE_PDF, ids),
        "assessing": _live_paper_ids(db, models.TASK_ASSESS_ARTICLE, ids),
        "held_by": _questions_of(db, ids),
        "journal_tiers": research.journal_tiers(db, papers),
        "back": _here(request, f"/projects/{project.id}/papers"),
        "project": project,
    }


def _card(request: Request, db: Session, paper: models.Paper,
          note: str | None = None, note_kind: str = "ok") -> Response:
    context = {"p": paper, "card_note": note, "card_note_kind": note_kind,
               **_card_context(request, db, [paper], paper.project)}
    return _partial(request, "_paper_card.html", context)


@router.get("/projects/{project_id}/papers", response_class=HTMLResponse)
def papers_list(request: Request, project_id: int, db: Session = Depends(get_db),
                status: str = "", stage: str = "", pdf: str = "", tag: str = "",
                q: str = "", question_id: str = "", sort: str = "relevance",
                page: str = "1"):
    project = _project(db, project_id)
    question = None
    qid = _int(question_id)
    if qid:
        question = db.get(models.ResearchQuestion, qid)
        if question is None or question.project_id != project.id:
            question, qid = None, 0
    sort = sort if sort in SORTS else "relevance"
    page_no = _int(page, 1, 1)
    filters = {"status": status, "stage": stage, "pdf": pdf, "tag": tag.strip(),
               "q": q.strip(), "question_id": qid}
    stmt = _paper_query(project, **filters)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    papers = db.execute(_sorted(stmt, sort).offset((page_no - 1) * PAGE_SIZE)
                        .limit(PAGE_SIZE + 1)).scalars().all()
    has_next = len(papers) > PAGE_SIZE
    papers = papers[:PAGE_SIZE]
    active = {k: v for k, v in filters.items() if v}
    if sort != "relevance":
        active["sort"] = sort

    def page_url(n: int) -> str:
        return (f"/projects/{project.id}/papers?"
                + urlencode({**active, "page": n}))

    project_tags = tags.with_counts(db, project.id)
    return _page(request, db, "papers.html", {
        **_card_context(request, db, papers, project),
        "papers": papers, "total": total,
        "listed": services.project_stats(db, project)["total"],
        "filters": filters, "filtered": bool(active.keys() - {"sort"}),
        "sort": sort, "sorts": SORTS, "question": question,
        "questions": sorted(project.questions, key=lambda r: r.number or 0),
        "project_tags": project_tags,
        "page": page_no, "has_next": has_next,
        "prev_url": page_url(page_no - 1) if page_no > 1 else None,
        "next_url": page_url(page_no + 1) if has_next else None,
        "uploads": identify.open_uploads(db, project.id),
    })


@router.post("/projects/{project_id}/download")
def download_selected(request: Request, project_id: int,
                      db: Session = Depends(get_db),
                      paper_ids: list[int] = Form(default=[]),
                      next_url: str = Form(default="", alias="next")):
    """Fetch the PDFs of the ticked articles, in the background."""
    project = _project(db, project_id)
    back = _safe_next(next_url, f"/projects/{project.id}/papers")
    if not paper_ids:
        return _redirect(request, back, "Tick the articles whose PDFs you want, "
                         "then press Download.", "error")
    moved = services.submit_to_retrieval(db, project, paper_ids)
    if not moved:
        return _redirect(request, back, "Those articles have their PDFs already.")
    worker.submit_as(paper_texts.download_and_read, moved, project_id=project.id,
                     label=f"downloading {_plural(len(moved), 'PDF')}")
    return _redirect(request, back, f"Fetching {_plural(len(moved), 'PDF')} in the "
                     "background. Reload the page in a minute to see how it went. "
                     "One that cannot be found can be uploaded on its card.")


@router.get("/projects/{project_id}/export/{fmt}")
def export_papers(project_id: int, fmt: str, db: Session = Depends(get_db),
                  status: str = "included", tag: str = ""):
    project = _project(db, project_id)
    stmt = _paper_query(project, status=status, tag=tag).order_by(models.Paper.id)
    rows = [exports.paper_row(p) for p in db.execute(stmt).scalars().all()]
    label = f"{project.slug}-{status if status in models.STATUSES else 'all'}"
    if tag.strip():
        label += "-" + models.slugify(tag)
    if fmt == "csv":
        # With a byte-order mark, so Excel reads the accents in names right.
        return Response("﻿" + exports.to_csv(rows),
                        media_type="text/csv; charset=utf-8", headers={
                            "Content-Disposition": f'attachment; filename="{label}.csv"'})
    if fmt == "bib":
        return Response(exports.to_bibtex(rows),
                        media_type="application/x-bibtex; charset=utf-8", headers={
                            "Content-Disposition": f'attachment; filename="{label}.bib"'})
    if fmt == "ris":
        return Response(exports.to_ris(rows),
                        media_type="application/x-research-info-systems; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{label}.ris"'})
    raise PageError(404, "LitReview exports CSV, BibTeX (bib) and RIS.")


# --------------------------------------------- a known article, project-wide

@router.post("/projects/{project_id}/find", response_class=HTMLResponse)
def find_for_project(request: Request, project_id: int,
                     db: Session = Depends(get_db), query: str = Form(default="")):
    project = _project(db, project_id)
    query = query.strip()
    candidates = research.find_candidates(db, project, query) if query else []
    return _partial(request, "_project_candidates.html", {
        "project": project, "query": query, "candidates": candidates,
        "journal_tiers": research.journal_tiers(db, [c["paper"] for c in candidates])})


@router.post("/projects/{project_id}/keep", response_class=HTMLResponse)
def keep_in_project(request: Request, project_id: int,
                    db: Session = Depends(get_db), paper_id: int = Form(...)):
    """Keep a lookup hit in the project, in no question for now."""
    project = _project(db, project_id)
    paper = _paper_in(db, project, paper_id)
    if paper.stage == models.STAGE_LOOKUP:
        paper.stage = models.STAGE_SCREENING
        db.commit()
    return _partial(request, "_project_candidate_row.html", {
        "c": research.candidate_state(db, paper), "project": project,
        "kept": True, "journal_tiers": research.journal_tiers(db, [paper])})


@router.post("/projects/{project_id}/candidates/{paper_id}/upload",
             response_class=HTMLResponse)
def upload_for_project_candidate(request: Request, project_id: int, paper_id: int,
                                 db: Session = Depends(get_db),
                                 file: UploadFile = File(...)):
    project = _project(db, project_id)
    paper = _paper_in(db, project, paper_id)
    problem = _store_uploaded_pdf(db, paper, _upload_content(file))
    if problem:
        raise PageError(400, problem)
    worker.submit_as(research.annotate_uploaded_paper, paper.id,
                     label=_reading_label(paper), project_id=paper.project_id)
    db.expire_all()
    paper = db.get(models.Paper, paper_id)
    return _partial(request, "_project_candidate_row.html", {
        "c": research.candidate_state(db, paper), "project": project,
        "just_uploaded": True, "journal_tiers": research.journal_tiers(db, [paper])})


# ---------------------------------------------------------- card actions

@router.get("/papers/{paper_id}/card", response_class=HTMLResponse)
def paper_card(request: Request, paper_id: int, db: Session = Depends(get_db)):
    return _card(request, db, _paper(db, paper_id))


@router.post("/papers/{paper_id}/status", response_class=HTMLResponse)
def set_status(request: Request, paper_id: int, db: Session = Depends(get_db),
               status: str = Form(default="")):
    paper = _paper(db, paper_id)
    if status not in models.STATUSES:
        raise PageError(400, "That is not a screening status.")
    # Excluding a paper takes its passages out of every answer that cites
    # it. Said at once, with the counts, rather than discovered at the next
    # rewrite.
    cited = (research.citing_answers(db, paper)
             if status == models.STATUS_EXCLUDED and paper.status != status else [])
    paper.status = status
    db.commit()
    note = None
    if cited:
        where = ", ".join(f"{rq.label}'s answer ({n} citation{'' if n == 1 else 's'})"
                          for rq, n in cited)
        note = (f"Excluded - it was cited in {where}. Those citations now show "
                "as unavailable, and the next rewrite leaves its passages out. "
                "Set it back to Included to undo.")
    if not _hx(request):
        return _redirect(request, f"/projects/{paper.project_id}/papers",
                         note or f"Marked “{paper.title}” {STATUS_WORDS[status].lower()}.")
    return _card(request, db, paper, note=note, note_kind="warn") if note \
        else _card(request, db, paper)


@router.post("/papers/{paper_id}/note", response_class=HTMLResponse)
def set_note(request: Request, paper_id: int, db: Session = Depends(get_db),
             note: str = Form(default="")):
    paper = _paper(db, paper_id)
    paper.note = note.strip()[:5000] or None
    db.commit()
    if not _hx(request):
        return _redirect(request, f"/projects/{paper.project_id}/papers", "Note saved.")
    return HTMLResponse('<span class="saved">Saved</span>')


def _tags_block(request: Request, db: Session, paper: models.Paper) -> Response:
    return _partial(request, "_paper_tags.html", {
        "p": paper, "oob": True,
        "project_tags": tags.with_counts(db, paper.project_id)})


@router.post("/papers/{paper_id}/tags", response_class=HTMLResponse)
def add_tags(request: Request, paper_id: int, db: Session = Depends(get_db),
             name: str = Form(default="")):
    """Add one tag, or several separated by commas."""
    paper = _paper(db, paper_id)
    names = [n for n in name.split(",") if models.clean_tag_name(n)]
    if names:
        tags.add(db, paper, names)
        db.commit()
    if not _hx(request):
        return _redirect(request, f"/projects/{paper.project_id}/papers#paper-{paper.id}")
    return _tags_block(request, db, paper)


@router.post("/papers/{paper_id}/tags/remove", response_class=HTMLResponse)
def remove_tag(request: Request, paper_id: int, db: Session = Depends(get_db),
               name: str = Form(default="")):
    paper = _paper(db, paper_id)
    if name.strip():
        tags.remove(db, paper, [name])
        db.commit()
    if not _hx(request):
        return _redirect(request, f"/projects/{paper.project_id}/papers#paper-{paper.id}")
    return _tags_block(request, db, paper)


def _edit_form(request: Request, paper: models.Paper, *, form: dict | None = None,
               error: str | None = None) -> Response:
    return _partial(request, "_paper_edit.html", {
        "p": paper, "form": form, "error": error, "types": identify.EDITABLE_TYPES})


@router.get("/papers/{paper_id}/edit", response_class=HTMLResponse)
def edit_details(request: Request, paper_id: int, db: Session = Depends(get_db)):
    return _edit_form(request, _paper(db, paper_id))


@router.post("/papers/{paper_id}/details", response_class=HTMLResponse)
def save_details(request: Request, paper_id: int, db: Session = Depends(get_db),
                 title: str = Form(default=""), authors: str = Form(default=""),
                 year: str = Form(default=""), venue: str = Form(default=""),
                 doi: str = Form(default=""), url: str = Form(default=""),
                 work_type: str = Form(default=""), abstract: str = Form(default=""),
                 volume: str = Form(default=""), issue: str = Form(default=""),
                 pages: str = Form(default="")):
    """A field that will not do comes back on the form with what was typed
    (a 200: HTMX swaps nothing on an error)."""
    paper = _paper(db, paper_id)
    form = {"title": title, "authors": authors, "year": year, "venue": venue,
            "doi": doi, "url": url, "work_type": work_type, "abstract": abstract,
            "volume": volume, "issue": issue, "pages": pages}
    try:
        identify.save_details(db, paper, **form)
    except ValueError as exc:
        db.rollback()
        return _edit_form(request, paper, form=form, error=str(exc))
    return _card(request, db, paper, note="Details saved.")


@router.post("/papers/{paper_id}/upload-pdf")
def upload_pdf(request: Request, paper_id: int, db: Session = Depends(get_db),
               file: UploadFile = File(...),
               next_url: str = Form(default="", alias="next")):
    """A PDF the person found themselves. It is read at once for every
    research question that holds the article; the answers wait for a
    rewrite, so a batch of uploads costs one."""
    paper = _paper(db, paper_id)
    back = _safe_next(next_url, f"/projects/{paper.project_id}/papers")
    problem = _store_uploaded_pdf(db, paper, _upload_content(file))
    if problem:
        return _redirect(request, back, problem + " Nothing was changed.", "error")
    worker.submit_as(research.annotate_uploaded_paper, paper.id,
                     label=_reading_label(paper), project_id=paper.project_id)
    held = research.paper_questions(db, paper)
    if held:
        labels = ", ".join(q.label for q in sorted(held, key=lambda q: q.number or 0))
        message = (f"Added the PDF of “{paper.title}”. Claude reads it for {labels} "
                   f"when you say “{SAY}”; the answers wait until you ask for a "
                   "rewrite.")
    else:
        message = (f"Added the PDF of “{paper.title}”. Its text is kept, so Claude "
                   "can read it.")
    return _redirect(request, back, message)


@router.post("/papers/{paper_id}/retry-pdf", response_class=HTMLResponse)
def retry_pdf(request: Request, paper_id: int, db: Session = Depends(get_db)):
    paper = _paper(db, paper_id)
    moved = services.submit_to_retrieval(db, paper.project, [paper.id])
    if moved:
        worker.submit_as(paper_texts.download_and_read, moved, project_id=paper.project_id,
                         label=f"downloading the PDF of {_short_title(paper)}")
    db.expire_all()
    paper = db.get(models.Paper, paper_id)
    note = None
    if paper.pdf_status == models.PDF_PENDING:
        note = "Trying the download again in the background - reload in a minute."
    return _card(request, db, paper, note=note)


@router.post("/papers/{paper_id}/find-oa", response_class=HTMLResponse)
def find_open_access(request: Request, paper_id: int,
                     db: Session = Depends(get_db)):
    paper = _paper(db, paper_id)
    kind = "ok"
    if paper.oa_url:
        note = "An open-access copy is known already."
    elif not paper.doi:
        note, kind = "This article has no DOI, so Unpaywall cannot look it up.", "error"
    elif not settings.contact_email:
        note, kind = ("Set your contact email on the Settings page first - "
                      "Unpaywall only answers requests that carry one."), "error"
    else:
        url = unpaywall.best_oa_url(paper.doi)
        if url:
            paper.oa_url = url
            db.commit()
            note = "Found an open-access copy. Download it with Download selected PDFs."
        else:
            note, kind = "Unpaywall knows of no open-access copy of this article.", "error"
    return _card(request, db, paper, note=note, note_kind=kind)


@router.post("/papers/{paper_id}/parse", response_class=HTMLResponse)
def parse_paper(request: Request, paper_id: int, db: Session = Depends(get_db),
                next_url: str = Form(default="", alias="next")):
    """Ask Claude to transcribe the PDF: a scan with no text layer, or one
    whose tables and equations matter."""
    paper = _paper(db, paper_id)
    back = _safe_next(next_url, f"/projects/{paper.project_id}/papers")
    if paper.pdf_status != models.PDF_DOWNLOADED:
        raise PageError(400, "Add the PDF first.")
    queued = paper_texts.request_parse(db, paper=paper, priority=30)
    tasks.commit(db)
    if queued:
        note = (f"Asked Claude to parse this PDF ({_plural(queued, 'part')}). "
                f"Say “{SAY}” in Claude.")
    elif paper_texts.parse_requested(db, paper=paper):
        note = "A parse of this PDF is already waiting for Claude."
    else:
        raise PageError(409, "The PDF file is missing or cannot be opened.")
    if _hx(request):
        return _card(request, db, paper, note=note)
    return _redirect(request, back, note)


# ------------------------------------------------------ where does it fit?

def _fit_context(db: Session, paper: models.Paper, back: str) -> dict:
    project = paper.project
    view = research.assess_article(db, project, paper)
    pending = bool(tasks.live_tasks(db, kinds=[models.TASK_ASSESS_ARTICLE],
                                    paper_id=paper.id))
    return {"paper": paper, "project": project, "pending": pending,
            "has_questions": bool(project.questions), "back": back, **view}


@router.get("/papers/{paper_id}/fit", response_class=HTMLResponse)
def fit_panel(request: Request, paper_id: int, db: Session = Depends(get_db)):
    paper = _paper(db, paper_id)
    back = _here(request, f"/projects/{paper.project_id}/papers")
    return _partial(request, "_fit_panel.html", _fit_context(db, paper, back))


@router.post("/papers/{paper_id}/fit", response_class=HTMLResponse)
def ask_fit(request: Request, paper_id: int, db: Session = Depends(get_db),
            again: str = Form(default="")):
    """Where does this article fit? Claude's answer, if it has given one for
    the project's questions as they are; otherwise a task for Claude."""
    paper = _paper(db, paper_id)
    project = paper.project
    view = research.assess_article(db, project, paper)
    if project.questions and (again or not (view["scored"] or view["waiting"])):
        research.request_assessment(db, project, paper)
        tasks.commit(db)
    back = _here(request, f"/projects/{paper.project_id}/papers")
    return _partial(request, "_fit_panel.html", _fit_context(db, paper, back))


@router.post("/projects/{project_id}/assign")
def assign_to_questions(request: Request, project_id: int,
                        db: Session = Depends(get_db), form=Depends(_form_data)):
    """File one article under the questions ticked - or under none."""
    project = _project(db, project_id)
    paper = _paper_in(db, project, _int(form.get("paper_id")))
    back = _safe_next(str(form.get("next") or ""), f"/projects/{project.id}/papers")
    held = {rq.id for rq in research.paper_questions(db, paper)}
    assignments: dict[int, float | None] = {}
    chosen = []
    for raw in form.getlist("question_ids"):
        rq = db.get(models.ResearchQuestion, _int(raw))
        if rq is None or rq.project_id != project.id or rq.running \
                or rq.id in held or rq.id in assignments:
            continue
        try:
            score = max(0.0, min(100.0, float(form.get(f"score_{rq.id}") or "")))
        except ValueError:
            score = None  # Claude scores it against that question
        assignments[rq.id] = score
        chosen.append(rq)
    if not assignments:
        if paper.stage == models.STAGE_LOOKUP:
            paper.stage = models.STAGE_SCREENING
            db.commit()
            return _redirect(request, back, f"Kept “{paper.title}” in the project, "
                             "in no question for now.")
        return _redirect(request, back, "Nothing was ticked, so the article stays "
                         "where it is.")
    worker.submit(research.include_across_questions, paper.id, assignments)
    labels = ", ".join(rq.label for rq in sorted(chosen, key=lambda q: q.number or 0))
    return _redirect(request, back, f"Filing “{paper.title}” under {labels}. The app "
                     "fetches its PDF and Claude reads it for each - say "
                     f"“{SAY}” in Claude. The answers wait until you ask for a "
                     "rewrite.")


# ======================================================================= PDFs

@router.get("/papers/{paper_id}/pdf/{kind}")
def paper_pdf(paper_id: int, kind: str, db: Session = Depends(get_db),
              download: int = 0):
    paper = _paper(db, paper_id)
    if kind != "original":
        raise PageError(404, "Each article has its original PDF; the highlighted "
                             "copies are opened from the question that read it.")
    path = pdfs.original_path(paper.project.slug, paper.id)
    if not path.exists():
        raise PageError(404, "LitReview does not have the PDF of this article.")
    return FileResponse(str(path), media_type="application/pdf",
                        filename=f"{paper.id}-{models.slugify(paper.title)[:60]}.pdf",
                        content_disposition_type="attachment" if download else "inline")


def _highlighted_pdf(annotation: models.Annotation) -> Path | None:
    """This reading's highlighted copy, redrawn from the stored geometry if
    the file is missing. None means there is none: serve the original."""
    paper = annotation.paper
    path = pdfs.annotation_path(paper.project.slug, annotation.id)
    if path.exists():
        return path
    try:
        return highlight.rebuild_highlighted(annotation, paper)
    except Exception as exc:  # a redraw failure must not hide the paper
        logger.warning("Could not redraw the highlights of annotation %s: %s",
                       annotation.id, exc)
        return None


@router.get("/annotations/{annotation_id}/pdf")
def annotation_pdf(annotation_id: int, db: Session = Depends(get_db),
                   download: int = 0, focus: str = ""):
    annotation = _annotation(db, annotation_id)
    paper = annotation.paper
    path = None
    if focus:
        # The citation clicked, drawn in its own colour among the others.
        try:
            path = highlight.focused_copy(annotation, paper, focus)
        except Exception:
            logger.exception("Could not draw the focused copy of %s", annotation_id)
    path = path or _highlighted_pdf(annotation) or pdfs.original_path(
        paper.project.slug, paper.id)
    if not path.exists():
        raise PageError(404, "LitReview does not have the PDF of this article.")
    return FileResponse(str(path), media_type="application/pdf",
                        filename=f"{paper.id}-highlighted-{annotation.id}.pdf",
                        content_disposition_type="attachment" if download else "inline")


@router.get("/annotations/{annotation_id}/view", response_class=HTMLResponse)
def annotation_view(request: Request, annotation_id: int,
                    db: Session = Depends(get_db), page: int = 0):
    """The highlighted PDF in the browser's own viewer, opened at a page."""
    annotation = _annotation(db, annotation_id)
    paper = annotation.paper
    findings = annotation.findings or []
    first = page or next((int(f.get("page") or 1) for f in findings
                          if f.get("located") == "pdf" or f.get("matched")), 1)
    return _page(request, db, "viewer.html", {
        "a": annotation, "p": paper, "rq": annotation.research_question,
        "first_page": max(1, int(first)),
        "highlighted": _highlighted_pdf(annotation) is not None,
        "has_pdf": pdfs.original_path(paper.project.slug, paper.id).exists()})


# ================================================================ the queue

_IDENTIFY_KINDS = (models.TASK_IDENTIFY_PDF, models.TASK_MATCH_RECORD,
                   models.TASK_FIND_WEB_PAGE)


def _task_link(task: models.ClaudeTask) -> str:
    """Where in the app the thing a task is about can be seen."""
    if task.research_question_id:
        return f"/questions/{task.research_question_id}"
    if task.document_id:
        return f"/documents/{task.document_id}"
    if task.paper is not None:
        if task.kind in _IDENTIFY_KINDS:
            return f"/projects/{task.project_id}#uploads"
        return (f"/projects/{task.project_id}/papers?"
                + urlencode({"q": task.paper.title[:120]}) + f"#paper-{task.paper_id}")
    return f"/projects/{task.project_id}"


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, db: Session = Depends(get_db)):
    live = tasks.live_tasks(db)
    live.sort(key=lambda t: (t.status != models.TASK_CLAIMED, t.priority, t.id))
    rows = []
    for task in live:
        kind = tasks.kind_of(task.kind)
        rows.append({
            "task": task, "about": tasks.describe(task),
            "kind": kind.title if kind else task.kind,
            "project": task.project, "link": _task_link(task),
            "age": _ago(task.created_at),
            "claimed": task.status == models.TASK_CLAIMED,
            "claimed_ago": _ago(task.claimed_at) if task.claimed_at else "",
            "lease_until": models.aware(task.lease_expires_at),
            "tries": task.attempts or 0, "problem": task.error if task.attempts else None,
        })
    recent = db.execute(
        select(models.ClaudeTask)
        .where(models.ClaudeTask.status.in_([models.TASK_DONE, models.TASK_FAILED]))
        .order_by(models.ClaudeTask.completed_at.desc().nullslast())
        .limit(12)
    ).scalars().all()
    return _page(request, db, "tasks.html", {
        "rows": rows,
        "recent": [{"task": t, "about": tasks.describe(t), "link": _task_link(t),
                    "when": _ago(t.completed_at)} for t in recent],
        "activity": tasks.background_activity(db),
        "seen": _seen(db),
    })


@router.post("/tasks/{task_id}/drop")
def drop_task(request: Request, task_id: int, db: Session = Depends(get_db)):
    """Set a stuck task aside: the step it belongs to goes on without it."""
    task = db.get(models.ClaudeTask, task_id)
    if task is None:
        raise PageError(404, "There is no such task.")
    if not task.live:
        return _redirect(request, "/tasks", "That task is not waiting any more.")
    about = tasks.describe(task)
    tasks.drop(db, task, "Set aside on the Tasks page")
    # drop() commits with a plain commit; whatever its step queued to run
    # after the commit (the downloads after a last ranking batch, say) is
    # started here.
    tasks.commit(db)
    return _redirect(request, "/tasks", f"Set aside: {about}. The rest of its step "
                     "goes on without it.")


# ============================================================ Connect Claude

def _claude_setup():
    """litreview_local.claude_setup, or None if this copy does not have it."""
    name = f"{__package__}.claude_setup"
    try:
        return importlib.import_module(name)
    except ImportError:
        return None
    except Exception:
        logger.exception("Could not load %s", name)
        return None


def _location_env() -> dict:
    """What the bridge must be told when the app is not on its defaults:
    Claude Desktop starts it with almost no environment of its own."""
    env = {}
    if settings.port != DEFAULT_PORT:
        env["LITREVIEW_PORT"] = str(settings.port)
    if os.path.normcase(str(settings.data_dir)) != os.path.normcase(
            str(DEFAULT_DATA_DIR.resolve())):
        env["LITREVIEW_DATA_DIR"] = str(settings.data_dir)
    return env


def _bridge_python() -> str:
    """The Python Claude Desktop should run the bridge with: local/.venv's,
    which the launcher keeps current, rather than whatever Python happens to
    be running the app - that one may be a temporary copy."""
    venv = LOCAL_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv) if venv.exists() else sys.executable


def _python_warning(python: str) -> str | None:
    import tempfile

    temp = os.path.normcase(os.path.realpath(tempfile.gettempdir()))

    def inside(path) -> bool:
        return os.path.normcase(os.path.realpath(str(path))).startswith(temp)

    if inside(LOCAL_DIR):
        return ("This copy of LitReview is in a temporary folder "
                f"({LOCAL_DIR}), which can be cleaned away - for example "
                "when it was opened straight from a downloaded ZIP. Move the "
                "LitReview folder somewhere permanent, such as Documents, "
                "start it from there, and add it to Claude Desktop again.")
    if inside(python):
        return ("The Python LitReview is running with lives in a temporary "
                "folder, which may be cleaned away. Start LitReview with the "
                "launcher (start-windows.bat or start-mac.command) once, so it "
                "makes local/.venv, and add it to Claude Desktop from there.")
    return None


def _desktop_log(setup):
    finder = getattr(setup, "find_desktop_log", None) if setup else None
    try:
        return finder() if finder else None
    except Exception:
        return None


def _desktop_entry(setup) -> dict:
    python = _bridge_python()
    if setup is None:
        entry = {"command": python, "args": [str(BRIDGE_PATH)]}
    else:
        try:
            entry = setup.desktop_entry(python, str(BRIDGE_PATH),
                                        port=settings.port,
                                        data_dir=str(settings.data_dir))
        except TypeError:  # an older helper without the location arguments
            entry = setup.desktop_entry(python, str(BRIDGE_PATH))
    env = _location_env()
    if env and not entry.get("env"):
        entry = {**entry, "env": env}
    return entry


def _fallback_locations() -> list[str]:
    if sys.platform == "win32":
        return [r"%APPDATA%\Claude\claude_desktop_config.json",
                r"%LOCALAPPDATA%\Packages\Claude_...\LocalCache\Roaming\Claude"
                r"\claude_desktop_config.json (Microsoft Store version)"]
    if sys.platform == "darwin":
        return ["~/Library/Application Support/Claude/claude_desktop_config.json"]
    return ["~/.config/Claude/claude_desktop_config.json"]


def _connect_context(db: Session) -> dict:
    """What Connect Claude, on Getting started, shows: where Claude
    Desktop's settings are, the entry LitReview adds to them, and whether
    Claude has called in."""
    setup = _claude_setup()
    entry = _desktop_entry(setup)
    ctx = {
        "setup_missing": setup is None, "entry": entry,
        "bridge_path": BRIDGE_PATH, "bridge_exists": BRIDGE_PATH.exists(),
        "python": _bridge_python(), "port": settings.port, "platform": sys.platform,
        "desktop_log": _desktop_log(setup),
        "python_warning": _python_warning(_bridge_python()),
        "mcp_url": f"{settings.base_url}/mcp", "local_dir": LOCAL_DIR,
        "mcp_json": MCP_JSON_PATH, "mcp_json_exists": MCP_JSON_PATH.exists(),
        "seen": _seen(db), "restart_note": RESTART_NOTE,
        "config_path": None, "config_exists": False, "candidates": [],
        "error": None, "already": False, "replaces": None, "desktop_seen": None,
        "renames": None, "desktop_name": _desktop_name(setup),
        "snippet": json.dumps({"mcpServers": {_desktop_name(setup): entry}}, indent=2,
                              ensure_ascii=False),
        "code_command": f"claude mcp add --transport http litreview "
                        f"{settings.base_url}/mcp",
        "code_command_user": f"claude mcp add --transport http --scope user litreview "
                             f"{settings.base_url}/mcp",
        "code_called": _app_ever(db, "claude-code"),
        "desktop_called": _app_ever(db, "claude-ai"),
    }
    if setup is None:
        ctx["candidates"] = _fallback_locations()
        return ctx
    if hasattr(setup, "manual_snippet"):
        ctx["snippet"] = setup.manual_snippet(entry)
    ctx["code_command"] = setup.claude_code_command(settings.port)
    try:
        ctx["code_command_user"] = setup.claude_code_command(settings.port, scope="user")
    except TypeError:  # a helper without scopes
        pass
    ctx["restart_note"] = getattr(setup, "RESTART_NOTE", RESTART_NOTE)
    try:
        ctx["candidates"] = [str(p) for p in setup.desktop_config_candidates()]
        config_path = setup.find_desktop_config()
    except Exception as exc:
        logger.warning("Could not look for Claude Desktop's settings: %s", exc)
        config_path = None
    if config_path is None:
        return ctx
    ctx["config_path"] = str(config_path)
    ctx["config_exists"] = Path(config_path).is_file()
    try:
        # Refuses a file that cannot be changed safely (not JSON, say).
        setup.preview_desktop_change(config_path, entry)
        current = _current_entry(setup, config_path)
        old = _old_entries(setup, config_path) if current is None else {}
    except (ValueError, OSError) as exc:
        ctx["error"] = str(exc)
        return ctx
    ctx["desktop_seen"] = _app_seen(db, "claude-ai", _desktop_written(db, config_path))
    # Already added means writing would change nothing; an entry of another
    # copy of LitReview (another folder, another Python) is replaced.
    ctx["already"] = current is not None and current == entry
    if current is not None and current != entry:
        ctx["replaces"] = json.dumps(current, indent=2, ensure_ascii=False)
    if old:  # under the name an earlier version gave it: adding renames it
        ctx["renames"] = sorted(old)[0]
    return ctx


def _desktop_name(setup) -> str:
    """What Claude Desktop calls LitReview."""
    return getattr(setup, "DESKTOP_NAME", None) or DESKTOP_NAME


def _current_entry(setup, config_path) -> dict | None:
    """LitReview's entry in Claude Desktop's settings now, if any."""
    if hasattr(setup, "read_desktop_entry"):
        return setup.read_desktop_entry(config_path)
    return _entry_in(config_path)


def _old_entries(setup, config_path) -> dict:
    """LitReview's entries under the names earlier versions gave it, which
    adding it renames ({old name: entry})."""
    finder = getattr(setup, "old_desktop_entries", None)
    return finder(config_path) if finder else {}


def _state(db: Session, key: str):
    row = db.get(models.AppState, key)
    return row.value if row is not None else None


def _set_state(db: Session, key: str, value) -> None:
    """Keep (or, for None, forget) one small fact in app_state."""
    row = db.get(models.AppState, key)
    if value is None:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(models.AppState(key=key, value=value))
    else:
        row.value = value
    db.commit()


def _same_path(a, b) -> bool:
    def norm(path) -> str:
        return os.path.normcase(os.path.realpath(str(path)))
    return norm(a) == norm(b)


def _noted_write(db: Session, config_path) -> datetime | None:
    """When this page last wrote LitReview into this settings file."""
    noted = _state(db, DESKTOP_WRITTEN_KEY)
    if not isinstance(noted, dict) or not noted.get("at"):
        return None
    if noted.get("path") and not _same_path(noted["path"], config_path):
        return None
    try:
        return models.aware(datetime.fromisoformat(noted["at"]))
    except (TypeError, ValueError):
        return None


def _desktop_written(db: Session, config_path) -> datetime | None:
    """When LitReview's entry went into Claude Desktop's settings: noted
    when this page wrote it, else the file's own time (an entry added by
    hand). Claude Desktop rewrites the file while it runs, which moves the
    file's time but not the noted one."""
    noted = _noted_write(db, config_path)
    if noted is not None:
        return noted
    try:
        return datetime.fromtimestamp(Path(config_path).stat().st_mtime,
                                      tz=timezone.utc)
    except (OSError, TypeError, ValueError):
        return None


def _launcher_name() -> str:
    return "start-mac.command" if sys.platform == "darwin" else "start-windows.bat"


def _bridge_check(entry: dict) -> dict:
    """Start the program Claude Desktop starts for LitReview - the entry's
    own command, arguments and environment, without this app's LITREVIEW_
    and PYTHON settings, which Claude Desktop would not pass on - with
    --check. It answers in one line of JSON whether it reaches LitReview.
    It asks /health only, so it does not count as Claude calling in."""
    command = str(entry.get("command") or "")
    if not command:
        return {"ok": False, "problem": "Claude Desktop's entry for LitReview "
                                        "names no program to start."}
    args = [str(a) for a in (entry.get("args") or [])]
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("LITREVIEW_", "PYTHONPATH", "PYTHONHOME"))}
    env.update({str(k): str(v) for k, v in (entry.get("env") or {}).items()})
    extra = {}
    if os.name == "nt":  # no console window flashing up
        extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run([command, *args, "--check"], capture_output=True,
                              stdin=subprocess.DEVNULL, env=env,
                              timeout=BRIDGE_CHECK_SECONDS, **extra)
    except FileNotFoundError:
        return {"ok": False, "problem": f"The Python it is set to start with is "
                                        f"not there: {command}."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "problem": f"It did not answer within "
                                        f"{BRIDGE_CHECK_SECONDS} seconds."}
    except OSError as exc:
        return {"ok": False, "problem": f"It could not be started: {exc}."}
    lines = done.stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        status = json.loads(lines[-1]) if lines else None
    except ValueError:
        status = None
    if not isinstance(status, dict):
        errors = done.stderr.decode("utf-8", "replace").strip().splitlines()
        why = errors[-1] if errors else f"exit code {done.returncode}"
        return {"ok": False, "problem": f"It stopped with an error: {why}"}
    if not status.get("ok"):
        return {"ok": False, "problem": str(status.get("error")
                                            or "It could not reach LitReview.")}
    there = (status.get("server") or {}).get("data_dir")
    if there and not _same_path(there, settings.data_dir):
        return {"ok": False, "problem": f"It reaches another copy of LitReview, "
                                        f"whose library is {there}."}
    return {"ok": True, "port": status.get("port")}


def _test_result(app: str, state: str, **fields) -> dict:
    """What _connect_test.html shows. state: "ok" (Claude called in),
    "wait" (all is ready; waiting for Claude), "stopped" (no call within
    TEST_WAIT), "cancelled" (Stop waiting pressed), "next" (not connected
    yet, nothing wrong: what to do next) or "bad" (a problem, with its fix)."""
    return {"app": app, "client": CLIENT_NAMES[TEST_APPS[app]], "state": state,
            "checks": [], "problem": None, "detail": None, "fix": None,
            "seen": None, "since": "", "log": None, "say_test": SAY_TEST,
            "platform": sys.platform, **fields}


def _desktop_test(db: Session, since: datetime | None = None) -> dict:
    """Test the Claude Desktop connection. It is the first step on Getting
    started, so it is pressed before LitReview is in Claude Desktop's
    settings as well as after. A fresh test (since None) starts the program
    Claude Desktop starts for LitReview - or, not added yet, will start -
    to see that LitReview's own side works. Not added yet, it then says
    what to do next; added, it waits for Claude Desktop to call in. The
    page asks again every few seconds, passing since; those polls read the
    settings again but do not start the program again."""
    checks: list[str] = []
    setup = _claude_setup()
    log = _desktop_log(setup)
    entry = _desktop_entry(setup)

    def bad(problem: str, fix: str, detail: str | None = None) -> dict:
        return _test_result("desktop", "bad", checks=checks, problem=problem,
                            fix=fix, detail=detail, log=log)

    def to_do(problem: str, fix: str) -> dict:
        """Not connected yet, and nothing is wrong: what to do next."""
        return _test_result("desktop", "next", checks=checks, problem=problem,
                            fix=fix)

    def fails_before_adding() -> dict | None:
        """The program Claude Desktop will start for LitReview, before it is
        added: there is no point adding something that does not work."""
        if since is not None:
            return None
        bridge = _bridge_check(entry)
        if not bridge["ok"]:
            return bad("The program Claude Desktop will start for LitReview does "
                       "not work.",
                       f"Close LitReview, start it again with {_launcher_name()}, "
                       f"and test again. Adding LitReview to Claude Desktop would "
                       f"not help until this works.", detail=bridge["problem"])
        checks.append("The program Claude Desktop will start for LitReview works.")
        return None

    if setup is None:
        # Claude Desktop's settings cannot be read here: LitReview is added
        # by hand, and Claude Desktop calling in shows that it worked.
        failed = fails_before_adding()
        if failed:
            return failed
        seen = _app_seen(db, "claude-ai")
        if seen:
            return _test_result("desktop", "ok", checks=checks, seen=seen)
        return to_do("This copy of LitReview cannot read Claude Desktop's settings, "
                     "so it cannot see whether LitReview is in them.",
                     "If it is not, add it by hand with the steps below. Then "
                     "open Claude Desktop again and press Test the connection "
                     "once more.")
    try:
        config_path = setup.find_desktop_config()
    except Exception:
        config_path = None
    if config_path is None:
        return bad("Claude Desktop is not installed on this computer, or it "
                   "has never been opened.",
                   "Install it from claude.ai/download, open it once and sign "
                   "in, then reload this page.")
    try:
        current = _current_entry(setup, config_path)
        old = _old_entries(setup, config_path) if current is None else {}
    except (ValueError, OSError) as exc:
        return bad("Claude Desktop's settings file cannot be read.",
                   "Claude Desktop cannot read it either. Fix the file, or "
                   "rename it so Claude Desktop starts a new one, then reload "
                   "this page.", detail=str(exc))
    if current != entry:
        # Not added yet, gone again, under an old name, or another copy's.
        failed = fails_before_adding()
        if failed:
            return failed
        if current is not None:
            return bad("Claude Desktop's settings point at another copy of LitReview.",
                       "Quit Claude Desktop, press Add LitReview to Claude Desktop "
                       "to point them at this copy, and open Claude Desktop again.")
        if old:
            return to_do(f"Claude Desktop has LitReview under its old name, "
                         f"“{sorted(old)[0]}”.",
                         f"Next: quit Claude Desktop, press Add LitReview to Claude "
                         f"Desktop, which renames it “{_desktop_name(setup)}”, and "
                         f"open Claude Desktop again. Then press Test the "
                         f"connection once more.")
        if _noted_write(db, config_path) is not None:
            return bad("LitReview is not in Claude Desktop's settings any more.",
                       "Claude Desktop most likely rewrote them while it was "
                       "still open. Quit it completely, press Add LitReview to "
                       "Claude Desktop again, and then open Claude Desktop.")
        return to_do("LitReview is not in Claude Desktop's settings yet.",
                     "Next: quit Claude Desktop, press Add LitReview to Claude "
                     "Desktop, and open Claude Desktop again. Then press Test "
                     "the connection once more.")
    checks.append("LitReview is in Claude Desktop's settings.")
    if since is None:
        bridge = _bridge_check(current)
        if not bridge["ok"]:
            return bad("The program Claude Desktop starts for LitReview does not "
                       "work.",
                       f"Quit Claude Desktop. Close LitReview and start it again "
                       f"with {_launcher_name()}, press Add LitReview to Claude "
                       f"Desktop, and open Claude Desktop again.",
                       detail=bridge["problem"])
    checks.append("The program Claude Desktop starts for LitReview works.")
    now = models.utcnow()
    since = since or now
    seen = _app_seen(db, "claude-ai", _desktop_written(db, config_path))
    if seen:
        return _test_result("desktop", "ok", checks=checks, seen=seen)
    if now - since >= TEST_WAIT:
        return _test_result(
            "desktop", "stopped", checks=checks, log=log,
            fix="Quit Claude Desktop completely - closing its window is not "
                "enough - open it again, and press Test the connection once more.")
    return _test_result("desktop", "wait", checks=checks,
                        since=str(int(since.timestamp())))


def _code_test(db: Session, since: datetime | None = None) -> dict:
    """Test the Claude Code connection. Claude Code reaches LitReview
    directly, so all there is to see is whether it calls in."""
    now = models.utcnow()
    since = since or now
    seen = _app_seen(db, "claude-code")
    if seen:
        return _test_result("code", "ok", seen=seen)
    if now - since >= TEST_WAIT:
        return _test_result(
            "code", "stopped",
            fix="In Claude Code, type /mcp: litreview should be listed as "
                "connected. If it is not there, paste the command from step 2 "
                "of Getting started into Claude Code, then open a new session.")
    return _test_result("code", "wait", since=str(int(since.timestamp())))


def _entry_in(config_path) -> dict | None:
    """LitReview's entry in a Claude Desktop config, read by hand (for a
    claude_setup without read_desktop_entry)."""
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8-sig") or "{}")
    except FileNotFoundError:
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers.get(DESKTOP_NAME) if isinstance(servers, dict) else None


@router.get("/start", response_class=HTMLResponse)
def start_page(request: Request, db: Session = Depends(get_db),
               at: str = ""):
    """Getting started: your email, connecting Claude, the first question.
    `at` names the step a message is about, so it shows in that step."""
    ctx = _connect_context(db)
    ctx.update({
        "contact_email": settings.contact_email,
        "projects": db.scalar(select(func.count(models.Project.id))) or 0,
        "questions": db.scalar(select(func.count(models.ResearchQuestion.id))) or 0,
        "flash_at": at if at in ("email", "claude") else "",
    })
    return _page(request, db, "start.html", ctx)


@router.get("/start/advanced", response_class=HTMLResponse)
def advanced_setup(request: Request, db: Session = Depends(get_db), at: str = ""):
    """Advanced setup: Claude Desktop by hand, Claude Code, taking LitReview
    out of Claude Desktop, and what to do when Claude does not connect -
    everything Getting started's list leaves out."""
    ctx = _connect_context(db)
    ctx["flash_at"] = "remove" if at == "remove" else ""
    return _page(request, db, "advanced.html", ctx)


@router.get("/connect")
def connect_page(request: Request):
    """The old Connect Claude page: it is part of Getting started now."""
    return RedirectResponse("/start#claude-code", status_code=303)


@router.post("/start/email")
def save_email(request: Request, contact_email: str = Form(default="")):
    """Step 1 of Getting started: the contact email alone (Settings has it
    too, with the rest)."""
    email = contact_email.strip()
    if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        text = (f"“{email}” does not look like an email address. Nothing was "
                "saved." if email else "Type your email address first.")
        return _redirect(request, "/start?at=email#email", text, "error")
    settings.save_user_settings({"contact_email": email})
    return _redirect(request, "/start?at=email#email",
                     f"Saved: {email}. Next, connect Claude.")


def _run_test(db: Session, app: str, since: datetime | None) -> dict:
    if app == "desktop":
        return _desktop_test(db, since)
    if app == "code":
        return _code_test(db, since)
    raise PageError(404, "There is no such test.")


@router.post("/connect/test/{app}", response_class=HTMLResponse)
def connect_test(request: Request, app: str, db: Session = Depends(get_db)):
    """Test the connection button: what LitReview can check by itself,
    then the wait for Claude to call in."""
    return _partial(request, "_connect_test.html", {"t": _run_test(db, app, None)})


@router.get("/connect/test/{app}", response_class=HTMLResponse)
def connect_test_poll(request: Request, app: str, since: str = "",
                      stop: bool = False, db: Session = Depends(get_db)):
    """The test asking again while it waits. Still waiting answers 204,
    which leaves the page as it is (no redraw, nothing read out again).
    stop: Stop waiting was pressed - the answer as it stands, with no more
    asking."""
    try:
        started = datetime.fromtimestamp(int(since), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        started = models.utcnow()
    result = _run_test(db, app, started)
    if result["state"] == "wait":
        if not stop:
            return Response(status_code=204)
        result = {**result, "state": "cancelled"}
    return _partial(request, "_connect_test.html", {"t": result})


@router.post("/connect/desktop/remove")
def connect_desktop_remove(request: Request, db: Session = Depends(get_db)):
    """Take LitReview out of Claude Desktop's settings again - for a moved or
    deleted LitReview folder, or simply a change of mind."""
    back = "/start/advanced?at=remove#remove"
    setup = _claude_setup()
    if setup is None:
        return _redirect(request, back, "This copy of LitReview cannot change Claude "
                         "Desktop's settings itself.", "error")
    try:
        config_path = setup.find_desktop_config()
        result = setup.remove_desktop_entry(config_path) if config_path else None
    except (ValueError, OSError) as exc:
        return _redirect(request, back, f"Nothing was changed: {exc}", "error")
    _set_state(db, DESKTOP_WRITTEN_KEY, None)
    if not result or result.get("action") == "absent":
        return _redirect(request, back, "LitReview was not in Claude Desktop's "
                         "settings, so nothing was changed.")
    return _redirect(request, back, "LitReview was taken out of Claude Desktop's "
                     "settings (the file as it was is kept as a backup). Quit "
                     "Claude Desktop completely and open it again for it to notice.")


@router.post("/connect/desktop")
def connect_desktop(request: Request, db: Session = Depends(get_db)):
    """Write LitReview into claude_desktop_config.json - only when the button
    is pressed. Everything else in the file stays as it is."""
    back = "/start?at=claude#claude"
    setup = _claude_setup()
    if setup is None:
        return _redirect(request, back, "This copy of LitReview cannot write Claude "
                         "Desktop's settings itself. Add the entry by hand as shown "
                         "below.", "error")
    try:
        config_path = setup.find_desktop_config()
    except Exception:
        config_path = None
    if config_path is None:
        return _redirect(request, back, "Claude Desktop does not seem to be installed "
                         "on this computer. Install it, open it once, then come back "
                         "to this page.", "error")
    entry = _desktop_entry(setup)
    try:
        result = setup.install_desktop_entry(config_path, entry)
    except (ValueError, OSError) as exc:
        return _redirect(request, back, f"Nothing was written: {exc}", "error")
    action = (result or {}).get("action")
    if action == "unchanged":
        message = ("LitReview was already in Claude Desktop's settings, so nothing "
                   "was changed. Open Claude Desktop and press Test the connection.")
    else:
        _set_state(db, DESKTOP_WRITTEN_KEY, {"at": models.utcnow().isoformat(),
                                             "path": str(config_path)})
        name = _desktop_name(setup)
        message = {
            "updated": "Claude Desktop's settings now point at this copy of LitReview",
            "renamed": f"LitReview is now called “{name}” in Claude Desktop's settings",
        }.get(action, f"Added LitReview to Claude Desktop's settings, as “{name}”")
        if (result or {}).get("backup"):
            message += " (the file as it was is kept as a backup)"
        message += ". Now open Claude Desktop again, and press Test the connection."
    logger.info("Wrote the Claude Desktop entry to %s (%s)", config_path, action)
    return _redirect(request, back, message)


# ================================================================= settings

def _backups() -> list[dict]:
    folder = settings.backups_dir
    try:
        files = sorted(folder.glob("litreview-*.db"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    out = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({"name": path.name, "size": stat.st_size,
                    "when": datetime.fromtimestamp(stat.st_mtime).astimezone()})
    return out


def _differing_projects(db: Session) -> list[str]:
    """Projects that still search with numbers or sources of their own, set
    on their pages before Settings held them for every project."""
    wanted = settings.project_defaults()
    return [p.name for p in db.execute(
        select(models.Project).order_by(models.Project.name)).scalars()
        if any(getattr(p, key) != value for key, value in wanted.items())]


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db, "settings.html", {
        "values": settings.user_settings(),
        "sources": settings.project_defaults()["sources"].split(","),
        "source_labels": SOURCE_LABELS,
        "differ": _differing_projects(db),
        "data_dir": settings.data_dir,
        "from_env": bool(os.environ.get("LITREVIEW_DATA_DIR", "").strip()),
        "default_data_dir": DEFAULT_DATA_DIR,
        "backups_dir": settings.backups_dir, "backups": _backups(),
        "logs_dir": settings.logs_dir, "settings_file": settings.settings_file,
        "port": settings.port, "platform": sys.platform,
    })


@router.post("/settings")
def save_settings(request: Request, db: Session = Depends(get_db),
                  contact_email: str = Form(default=""),
                  s2_api_key: str = Form(default=""),
                  fetch_per_source: str = Form(default=""),
                  rank_top: str = Form(default=""),
                  download_top: str = Form(default=""),
                  sources: list[str] = Form(default=[]),
                  sources_shown: str = Form(default="")):
    """Settings. How wide each search is goes to every project, old and new
    (the question's own copy, taken when it was asked, stays as it is)."""
    email = contact_email.strip()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return _redirect(request, "/settings#email", f"“{email}” does not look like "
                         "an email address. Nothing was saved.", "error")
    changes = {"contact_email": email, "s2_api_key": s2_api_key.strip()}
    for key, raw in (("fetch_per_source", fetch_per_source), ("rank_top", rank_top),
                     ("download_top", download_top)):
        if str(raw).strip():
            changes[key] = raw
    if sources_shown:
        chosen = [key for key in SOURCE_LABELS if key in sources]
        if not chosen:
            return _redirect(request, "/settings#defaults", "Tick at least one source "
                             "to search. Nothing was saved.", "error")
        changes["sources"] = ",".join(chosen)
    settings.save_user_settings(changes)
    wanted = settings.project_defaults()
    for project in db.execute(select(models.Project)).scalars():
        for key, value in wanted.items():
            setattr(project, key, value)
    db.commit()
    return _redirect(request, "/settings", "Settings saved. Every project searches "
                     "with these numbers from its next run.")
