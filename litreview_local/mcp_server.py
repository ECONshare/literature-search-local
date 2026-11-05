"""MCP server: LitReview Local as a tool source for the user's own Claude.

Claude Desktop reaches it through mcp_bridge.py (stdio <-> HTTP), Claude
Code directly over HTTP (local/.mcp.json). The transport is Streamable HTTP
in its simplest legal form: one POST endpoint that takes a JSON-RPC message
and answers with a JSON-RPC response - no SSE, no server-side session state.

There is no login: the app listens on 127.0.0.1 only, and main.py's guard
refuses requests whose Host or Origin is not this machine's, which keeps web
pages in a browser from reaching it.

Three kinds of tool:

- Reading the library: projects, questions, answers, highlights, papers,
  full texts, documents, references. Compact by default - a chat client's
  context is the scarce resource here.
- The work queue: next_task / submit_task. The app never calls a model; every
  step that needs one waits as a task, and these two tools are how Claude
  does them (see tasks.py).
- Changing the library on the user's behalf: asking questions, adding
  articles and evidence, saving an edited answer, tags and screening.
"""
import base64
import json
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import (__version__, citations, documents, exports, journals, models,
               paper_texts, pdfs, research, services, tags, tasks)
from .config import settings
from .db import SessionLocal, get_db

logger = logging.getLogger(__name__)

router = APIRouter()

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "litreview", "title": "LitReview Local (this computer)",
               "version": __version__}

INSTRUCTIONS = """These are the instructions of LitReview LOCAL: the user's literature-review library running on this computer, whose tools include next_task and submit_task. If you also see a hosted LitReview connector (on claude.ai, usually called "Literature review"), that is a different library on a server with different instructions - for anything on this computer, use these tools and these instructions.

LitReview Local is the user's own literature-review library, running on their computer. It searches OpenAlex, Semantic Scholar and Crossref, downloads open-access PDFs, keeps their text, highlights passages and stores everything - but it has no model of its own. Every step that needs judgment is a TASK waiting for you: planning a search, ranking results, reading a paper for a research question, writing the cited answer, transcribing a scanned PDF, identifying an uploaded PDF.

WORKING THE QUEUE. When the user asks you to work on LitReview ("work through my LitReview queue", "process my review", "parse the documents"):
1. Call next_task. It hands you one task: its instructions, its input and the exact output_schema.
2. Do what the instructions say, using the input given.
3. Call submit_task(task_id, output). The reply says what the app does next.
4. Repeat. When next_task says the app itself is busy (searching, downloading), call it again with wait_seconds=45. Stop when it says nothing is waiting.
Tasks are self-contained - you never need to remember an earlier one. Report progress to the user briefly ("ranked 40 results for RQ2, read 3 papers, wrote the answer") rather than narrating each task. If a task cannot be done (an unreadable text, say), call skip_task with the reason so the rest can go on.

QUOTES MUST BE VERBATIM. The app finds every quote you give in the PDF to highlight it. Copy quotes character for character from the text in the task - never paraphrase or tidy them. A quote that cannot be found is refused once with the reason.

CLAUDE CODE: hand annotate_paper tasks to the litreview-reader subagent (several in parallel, each calling next_task(kind="annotate_paper")), so paper texts stay out of the main conversation.
CLAUDE DESKTOP: papers fill a chat quickly. next_task says when this chat has been handed a lot of text (chat_budget); then tell the user to continue in a new chat with the same request - the queue keeps its place, nothing is lost.

CHECKING AN ANSWER (verify_answer): do it in a fresh chat or a subagent, not in the conversation that wrote the answer - a writer grading its own text is lenient.

READING THE LIBRARY. list_projects, list_questions, get_answer (the cited answer, compact; pass question_ids for several), get_highlights (verbatim passages with page, section and context), search_papers, get_paper, get_paper_text, search_paper_text, list_documents / get_document / search_documents (a project's protocol, guidelines, manuscript - check them before drafting), bibliography, list_tags. Every question carries a label - RQ1, RQ2, ... - shown on the user's screen; call questions by it, and pass question_id to the tools.

Each passage carries `located`: "pdf" (found and highlighted in the PDF), "text" (found in the paper's text, page known, but not highlightable in the PDF) or "none" (not found - treat its page as unreliable and check before relying on it).

CHANGING THE LIBRARY on the user's behalf: create_project; ask_question starts a full run for a new question (you may pass the planned search yourself to skip the planning task; check list_questions first - a near-duplicate is refused); rerun_question; find_article + include_articles add a known paper; add_evidence makes a passage you found yourself (in get_paper_text or search_paper_text) citable; save_answer stores your edit of an answer, keeping its [CIT:key] markers; tag_papers and set_screening; request_parse queues a proper transcription of a PDF whose text layer is missing or poor. Nothing here deletes anything."""

MAX_LIMIT = 100
DUPLICATE_THRESHOLD = 0.6
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for",
    "from", "how", "in", "is", "it", "of", "on", "or", "that", "the", "there",
    "these", "this", "to", "was", "what", "when", "which", "who", "why",
    "with", "any", "can", "has", "have", "if", "into", "its", "their",
}


@dataclass
class Ctx:
    base: str
    session: str
    client: str | None


class ToolError(Exception):
    """Raised by a tool handler; reported to the model as a tool error."""


class Content:
    """A tool result that is not JSON text - page images, for one."""

    def __init__(self, items: list[dict]):
        self.items = items


# Characters of task text handed to each session (roughly, each chat), so a
# Claude Desktop chat can be told when to start a new one.
_handed: dict[str, int] = {}
CHAT_BUDGET_CHARS = 350_000
# Inputs longer than this are sent as a block of their own: plain text,
# readable in parts, no JSON escapes hiding invisible characters.
OWN_BLOCK_CHARS = 4_000

# The client a session announced at initialize (Claude Desktop says
# "claude-ai", Claude Code "claude-code"), so the queue can say who took a
# task and Getting started can tell Claude Desktop from Claude Code. Kept in
# app_state as well: Claude Desktop keeps its session when LitReview
# restarts under it, and does not announce itself again.
_clients: dict[str, str] = {}
_looked_up: set[str] = set()
_SESSIONS_KEY = "mcp_sessions"
_SESSIONS_KEPT = 50


def _remember_client(db: Session, session: str, client: str) -> str:
    _clients[session] = client
    row = db.get(models.AppState, _SESSIONS_KEY)
    known = dict(row.value or {}) if row is not None else {}
    known.pop(session, None)
    known[session] = client
    while len(known) > _SESSIONS_KEPT:
        known.pop(next(iter(known)))
    if row is None:
        db.add(models.AppState(key=_SESSIONS_KEY, value=known))
    else:
        row.value = known
    return client


def _client_of(db: Session, session: str) -> str | None:
    """The client of a session - from memory, or else from before a restart."""
    if session in _clients or session in _looked_up or session == "no-session":
        return _clients.get(session)
    _looked_up.add(session)
    try:
        row = db.get(models.AppState, _SESSIONS_KEY)
    except Exception:
        db.rollback()
        return None
    client = (row.value or {}).get(session) if row is not None else None
    if isinstance(client, str) and client:
        _clients[session] = client
        return client
    return None


# ------------------------------------------------------------------- helpers

def _int(args: dict, name: str, default: int, low: int, high: int) -> int:
    value = args.get(name, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _bool(args: dict, name: str, default: bool = False) -> bool:
    value = args.get(name, default)
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes")
    return bool(value)


def _ids(value) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, (int, str)):
        value = [value]
    out = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def _iso(value) -> str | None:
    value = models.aware(value)
    return value.isoformat() if value is not None else None


def _project(db: Session, args: dict) -> models.Project:
    project = db.get(models.Project, _row(args, "project_id"))
    if project is None:
        raise ToolError(f"No project with id {args.get('project_id')!r}. Call "
                        "list_projects to see them.")
    return project


def _question(db: Session, args: dict, name: str = "question_id") -> models.ResearchQuestion:
    rq = db.get(models.ResearchQuestion, _row(args, name))
    if rq is None:
        raise ToolError(f"No research question with id {args.get(name)!r}. Call "
                        "list_questions to see them (question_id is not the "
                        "RQ number).")
    return rq


def _paper(db: Session, args: dict, name: str = "paper_id") -> models.Paper:
    paper = db.get(models.Paper, _row(args, name))
    if paper is None:
        raise ToolError(f"No paper with id {args.get(name)!r}. Call search_papers "
                        "to find one.")
    return paper


def _document(db: Session, args: dict) -> models.ProjectDocument:
    document = db.get(models.ProjectDocument, _row(args, "document_id"))
    if document is None:
        raise ToolError(f"No document with id {args.get('document_id')!r}. Call "
                        "list_documents to see them.")
    return document


def _row(args: dict, name: str) -> int:
    try:
        return int(args.get(name))
    except (TypeError, ValueError):
        return 0


def _journal_map(db: Session, papers: list) -> dict[int, models.Journal]:
    wanted: dict[str, list[int]] = {}
    for paper in papers:
        for raw in [paper.issn_l, *(paper.issns or [])]:
            issn = journals.normalize_issn(raw)
            if issn:
                wanted.setdefault(issn, []).append(paper.id)
    if not wanted:
        return {}
    rows = db.execute(
        select(models.Journal).where(models.Journal.issn.in_(list(wanted)))
    ).scalars().all()
    out: dict[int, models.Journal] = {}
    for journal in rows:
        for paper_id in wanted.get(journal.issn, []):
            known = out.get(paper_id)
            if known is None or (known.norwegian_level is None
                                 and journal.norwegian_level is not None):
                out[paper_id] = journal
    return out


def _text_format(reader: str | None) -> str:
    return "markdown (parsed by Claude)" if reader == models.READER_CLAUDE else "plain text (PDF text layer)"


def _full_texts(db: Session, papers: list) -> dict[int, dict]:
    return {paper_id: {"format": _text_format(text["reader"]),
                       "pages": text["pages"], "characters": text["characters"]}
            for paper_id, text in paper_texts.summaries(
                db, [p.id for p in papers]).items()}


def _paper_row(paper: models.Paper, ctx: Ctx, *, abstract: bool = False,
               score: tuple | None = None,
               journal: models.Journal | None = None,
               full_text: dict | None = None) -> dict:
    row = {
        "paper_id": paper.id,
        "title": paper.title,
        "authors": paper.authors or [],
        "year": paper.year,
        "venue": paper.venue,
        "volume": paper.volume,
        "issue": paper.issue,
        "pages": paper.pages,
        "year_note": citations.years_note(paper),
        "work_type": paper.work_type,
        "journal_quality": journals.tier_label(journals.tier(journal)),
        "doi": paper.doi,
        "url": (f"https://doi.org/{paper.doi}" if paper.doi else paper.url),
        "citation_count": paper.citation_count,
        "screening_status": paper.status,
        "tags": paper.tag_names,
        "pdf": paper.pdf_status,
        "full_text": full_text,
        "apa_inline": citations.apa_inline(paper.authors, paper.year),
        "apa_reference": citations.apa_reference(paper),
    }
    if abstract:
        row["abstract"] = paper.abstract
    if score is not None:
        row["relevance_score"] = score[0]
        row["relevance_reason"] = score[1]
    if paper.pdf_status == models.PDF_DOWNLOADED:
        row["pdf_url"] = f"{ctx.base}/papers/{paper.id}/pdf/original"
    return row


def _question_scores(db: Session, question_id: int) -> dict[int, tuple]:
    rows = db.execute(
        select(models.PaperScore.paper_id, models.PaperScore.score,
               models.PaperScore.reason)
        .join(models.Search, models.PaperScore.search_id == models.Search.id)
        .where(models.Search.research_question_id == question_id)
    ).all()
    best: dict[int, tuple] = {}
    for paper_id, score, reason in rows:
        current = best.get(paper_id)
        if current is None or score > current[0]:
            best[paper_id] = (score, reason)
    return best


def _question_paper_ids(question_id: int):
    return (
        select(models.search_papers.c.paper_id)
        .select_from(models.search_papers.join(
            models.Search,
            models.Search.id == models.search_papers.c.search_id))
        .where(models.Search.research_question_id == question_id)
    )


def _words(text: str) -> set[str]:
    words = set()
    for word in re.findall(r"[a-z]{3,}", (text or "").lower()):
        if word in _STOPWORDS:
            continue
        if word.endswith("ies") and len(word) > 4:
            word = word[:-3] + "y"
        elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
            word = word[:-1]
        words.add(word)
    return words


def _duplicate_of(db: Session, project_id: int,
                  text: str) -> models.ResearchQuestion | None:
    asked = _words(text)
    best, best_score = None, 0.0
    for rq in db.execute(
        select(models.ResearchQuestion)
        .where(models.ResearchQuestion.project_id == project_id)
    ).scalars().all():
        other = _words(rq.text)
        if not asked or not other:
            continue
        score = len(asked & other) / len(asked | other)
        if score > best_score:
            best, best_score = rq, score
    return best if best_score >= DUPLICATE_THRESHOLD else None


def _queue_line(db: Session, project_id: int | None = None) -> dict:
    counts = tasks.pending_counts(db, project_id)
    return {"tasks_waiting": counts["total"], "by_kind": counts["by_kind"]}


def _listed():
    """Papers that are the project's own - not parked lookup hits."""
    return models.Paper.stage != models.STAGE_LOOKUP


# ------------------------------------------------------------- read tools

def tool_list_projects(db: Session, args: dict, ctx: Ctx) -> dict:
    out = []
    for entry in services.projects_overview(db):
        project, stats = entry["project"], entry["stats"]
        out.append({
            "project_id": project.id,
            "name": project.name,
            "description": project.description,
            "papers": stats["total"],
            "research_questions": stats["questions"],
            "pdfs_downloaded": stats["pdf_done"],
            "papers_annotated": stats["annotated"],
            "tasks_waiting_for_claude": stats["waiting_for_claude"],
            "url": f"{ctx.base}/projects/{project.id}",
        })
    return {"projects": out,
            "note": None if out else ("No projects yet. create_project makes "
                                      "one, or the user can in the app.")}


def tool_list_questions(db: Session, args: dict, ctx: Ctx) -> dict:
    limit = _int(args, "limit", 50, 1, MAX_LIMIT)
    stmt = select(models.ResearchQuestion)
    if args.get("project_id"):
        project = _project(db, args)
        stmt = stmt.where(models.ResearchQuestion.project_id == project.id)
    stmt = stmt.order_by(models.ResearchQuestion.created_at.desc()).limit(limit)
    questions = sorted(db.execute(stmt).scalars().all(),
                       key=lambda rq: (rq.project_id, rq.number or 0))
    out = []
    for rq in questions:
        annotated = db.execute(
            select(func.count(func.distinct(models.Annotation.paper_id)))
            .where(models.Annotation.research_question_id == rq.id,
                   models.Annotation.status == models.ANNOT_DONE)
        ).scalar_one()
        waiting = tasks.pending_counts(db, rq.project_id)
        out.append({
            "question_id": rq.id,
            "label": rq.label,
            "project_id": rq.project_id,
            "project": rq.project.name if rq.project else None,
            "question": rq.text,
            "headline": rq.headline,
            "status": rq.status,
            "status_label": rq.status_label,
            "status_detail": rq.status_detail,
            "run_notes": (rq.run_state or {}).get("notes") or None,
            "error": rq.error if rq.status == models.RQ_FAILED else None,
            "waiting_for_claude": rq.waiting_for_claude,
            "has_answer": bool(rq.summary_html),
            "papers_read": annotated,
            "unread_pdfs": len(research.outstanding_papers(db, rq)),
            "annotations_not_in_answer": len(research.unused_annotations(db, rq)),
            "url": f"{ctx.base}/questions/{rq.id}",
        })
        del waiting
    return {"questions": out}


def _compact_answer(db: Session, rq: models.ResearchQuestion, *,
                    quotes: bool, why: bool, context: bool,
                    papers: dict[int, dict], ctx: Ctx) -> dict:
    annotations = research.question_annotations(db, rq.id)
    cmap = citations.citation_map(annotations)
    tokens = citations.html_to_tokens(rq.summary_html or "")
    check = research.verification_for(rq) or {}
    verdicts = check.get("verdicts")
    cited, unresolved = [], []
    seen = set()
    for key in citations.TOKEN_RE.findall(tokens):
        if key in seen:
            continue
        seen.add(key)
        resolved = citations.resolve_key(key, cmap)
        if resolved is None:
            unresolved.append(key)
            continue
        info = cmap[resolved]
        entry = {"key": key, "paper_id": info["paper_id"], "page": info["page"],
                 "located": info["located"]}
        if info.get("printed_page"):
            entry["printed_page"] = info["printed_page"]
        if info.get("section"):
            entry["section"] = info["section"]
        if quotes:
            entry["quote"] = info["quote"]
        if context and info.get("context"):
            entry["context"] = info["context"]
        if why:
            entry["why_relevant"] = info["why"]
        verdict = (verdicts or {}).get(key)
        if verdict:
            entry["check"] = verdict["verdict"]
            if verdict.get("note"):
                entry["check_note"] = verdict["note"]
        cited.append(entry)
        paper = next((a.paper for a in annotations if a.paper
                      and a.paper.id == info["paper_id"]), None)
        if paper is not None and paper.id not in papers:
            papers[paper.id] = {"apa_inline": citations.apa_inline(paper.authors, paper.year),
                                "title": paper.title,
                                "reference": citations.apa_reference(paper)}
    out = {"question_id": rq.id, "label": rq.label, "question": rq.text,
           "status": rq.status_label,
           "answer": tokens or None,
           "citations": cited,
           "papers_read": len(annotations),
           "url": f"{ctx.base}/questions/{rq.id}"}
    if unresolved:
        out["citations_without_evidence"] = unresolved
    if check:
        out["claude_check"] = {"checked_at": check.get("checked_at"),
                               "overall": check.get("overall")}
    if not rq.summary_html:
        out["note"] = (f"{rq.label} has no answer yet ({rq.status_label.lower()}).")
    return out


def tool_get_answer(db: Session, args: dict, ctx: Ctx) -> dict:
    ids = _ids(args.get("question_ids")) or _ids(args.get("question_id"))
    if not ids:
        raise ToolError("Pass question_id, or question_ids for several.")
    if len(ids) > 10:
        raise ToolError("At most 10 questions per call.")
    max_chars = _int(args, "max_chars", 40000, 2000, 200000)
    quotes = _bool(args, "include_quotes", True)
    why = _bool(args, "include_why", False)
    context = _bool(args, "include_context", True)
    rqs = [_question(db, {"question_id": i}) for i in ids]

    def build(quotes_on, context_on):
        papers: dict[int, dict] = {}
        answers = [_compact_answer(db, rq, quotes=quotes_on, why=why,
                                   context=context_on, papers=papers, ctx=ctx)
                   for rq in rqs]
        return {"answers": answers, "papers": papers}

    out = build(quotes, context)
    trimmed = []
    if len(json.dumps(out, default=str)) > max_chars and context:
        out = build(quotes, False)
        trimmed.append("context left out")
    if len(json.dumps(out, default=str)) > max_chars and quotes:
        out = build(False, False)
        trimmed.append("quotes left out")
    out["how_to_read"] = (
        "answer carries [CIT:key] markers; each key is in citations with its "
        "paper_id (details once, under papers), page and located: pdf = "
        "highlighted in the PDF, text = found in the paper's text but not "
        "highlightable, none = not found (check before relying on it). Cite in "
        "the style the user asks for; papers[...].reference is APA.")
    if trimmed:
        out["note"] = ("Too large for one reply, so " + " and ".join(trimmed)
                       + ". get_highlights(question_id=..., keys=[...]) gives "
                       "the full passages for the keys you need.")
    return out


def tool_search_papers(db: Session, args: dict, ctx: Ctx) -> dict:
    limit = _int(args, "limit", 20, 1, MAX_LIMIT)
    include_abstract = _bool(args, "include_abstract", True)
    rq = None
    scores: dict[int, tuple] = {}
    stmt = select(models.Paper).where(_listed())
    if args.get("question_id"):
        rq = _question(db, args)
        scores = _question_scores(db, rq.id)
        stmt = stmt.where(models.Paper.id.in_(_question_paper_ids(rq.id)))
    elif args.get("project_id"):
        project = _project(db, args)
        stmt = stmt.where(models.Paper.project_id == project.id)
    query = (args.get("query") or "").strip()
    if query:
        # Every word, anywhere in the title, abstract, authors or journal -
        # not the whole query as one phrase: "Becker", "copula adjusted" and
        # an author's name must all find what they name.
        from sqlalchemy import String, cast

        for word in [w for w in re.split(r"\s+", query.replace('"', " ")) if w][:8]:
            like = f"%{word}%"
            fields = [models.Paper.title.ilike(like), models.Paper.abstract.ilike(like),
                      cast(models.Paper.authors, String).ilike(like),
                      models.Paper.venue.ilike(like)]
            if re.fullmatch(r"(?:1[5-9]|20)\d\d", word):
                # A year names the year, online or in print.
                fields += [models.Paper.year == int(word),
                           models.Paper.online_year == int(word)]
            stmt = stmt.where(or_(*fields))
    status = args.get("status")
    if status in models.STATUSES:
        stmt = stmt.where(models.Paper.status == status)
    else:
        stmt = stmt.where(models.Paper.status != models.STATUS_EXCLUDED)
    tag = (args.get("tag") or "").strip()
    if tag:
        stmt = stmt.where(models.Paper.id.in_(
            select(models.paper_tags.c.paper_id)
            .join(models.Tag, models.Tag.id == models.paper_tags.c.tag_id)
            .where(models.Tag.key == models.tag_key(tag))))
    if args.get("year_from"):
        stmt = stmt.where(models.Paper.year >= args["year_from"])
    if args.get("year_to"):
        stmt = stmt.where(models.Paper.year <= args["year_to"])
    if _bool(args, "has_pdf"):
        stmt = stmt.where(models.Paper.pdf_status == models.PDF_DOWNLOADED)
    if _bool(args, "annotated_only"):
        annotated = select(models.Annotation.paper_id).where(
            models.Annotation.status == models.ANNOT_DONE)
        if rq is not None:
            annotated = annotated.where(
                models.Annotation.research_question_id == rq.id)
        stmt = stmt.where(models.Paper.id.in_(annotated))

    note = None
    if rq is not None:
        ranked = (
            select(models.PaperScore.paper_id.label("paper_id"),
                   func.max(models.PaperScore.score).label("score"))
            .join(models.Search, models.PaperScore.search_id == models.Search.id)
            .where(models.Search.research_question_id == rq.id)
            .group_by(models.PaperScore.paper_id)
            .subquery())
        stmt = stmt.outerjoin(ranked, ranked.c.paper_id == models.Paper.id)
        if args.get("min_score") is not None:
            stmt = stmt.where(ranked.c.score >= args["min_score"])
        matched = db.execute(
            select(func.count()).select_from(stmt.subquery())).scalar_one()
        stmt = stmt.order_by(ranked.c.score.desc().nullslast(),
                             models.Paper.citation_count.desc().nullslast())
        papers = db.execute(stmt.limit(limit)).scalars().all()
        if matched > limit:
            note = f"{matched} papers matched; showing the {limit} most relevant."
    else:
        stmt = stmt.order_by(models.Paper.citation_count.desc().nullslast(),
                             models.Paper.year.desc().nullslast())
        papers = db.execute(stmt.limit(limit)).scalars().all()

    journal_map = _journal_map(db, papers)
    texts = _full_texts(db, papers)
    rows = []
    for p in papers:
        row = _paper_row(p, ctx, abstract=include_abstract,
                         score=scores.get(p.id) if rq is not None else None,
                         journal=journal_map.get(p.id), full_text=texts.get(p.id))
        if rq is not None and p.id not in scores:
            row["relevance_score"] = None
            row["relevance_reason"] = "not ranked for this question"
        rows.append(row)
    result = {"count": len(rows), "papers": rows}
    if rq is not None:
        result["scored_against"] = rq.text
    if note:
        result["note"] = note
    return result


def tool_get_paper(db: Session, args: dict, ctx: Ctx) -> dict:
    paper = _paper(db, args)
    out = _paper_row(paper, ctx, abstract=True,
                     journal=_journal_map(db, [paper]).get(paper.id),
                     full_text=_full_texts(db, [paper]).get(paper.id))
    out["oa_url"] = paper.oa_url
    out["pdf_error"] = paper.pdf_error
    out["note_from_user"] = paper.note
    out["project"] = paper.project.name if paper.project else None
    out["sources"] = list((paper.source_ids or {}).keys())
    score_rows = db.execute(
        select(models.PaperScore.score, models.PaperScore.reason,
               models.Search.research_question_id)
        .join(models.Search, models.PaperScore.search_id == models.Search.id)
        .where(models.PaperScore.paper_id == paper.id)
    ).all()
    out["relevance"] = [{"question_id": qid, "score": score, "reason": reason}
                        for score, reason, qid in score_rows if qid is not None]
    if _bool(args, "include_annotations", True):
        out["annotations"] = [
            {"annotation_id": a.id, "question_id": a.research_question_id,
             "status": a.status,
             "highlights": [{"key": citations.finding_key(a.id, f),
                             "title": f.get("title"), "quote": f.get("quote"),
                             "page": f.get("page"),
                             "located": f.get("located") or (
                                 "pdf" if f.get("matched") else "none"),
                             "section": f.get("section")}
                            for f in (a.findings or []) if not f.get("rejected")]}
            for a in paper.annotations if a.status == models.ANNOT_DONE]
    return out


def tool_get_highlights(db: Session, args: dict, ctx: Ctx) -> dict:
    max_chars = _int(args, "max_chars", 40000, 2000, 200000)
    out = _highlights(db, args, ctx, context=_bool(args, "include_context", True))
    if len(json.dumps(out, default=str)) > max_chars:
        out = _highlights(db, args, ctx, context=False)
        out["note"] = ((out.get("note") or "") + " Context left out to fit the "
                       "size budget; ask for fewer papers or keys to see it.").strip()
    while len(json.dumps(out, default=str)) > max_chars and out["papers"]:
        dropped = out["papers"].pop()
        out["highlights_shown"] -= len(dropped["highlights"])
        out["note"] = ((out.get("note") or "") + " Some papers were left out to "
                       "fit the size budget - narrow with paper_id or keys.").strip()
    return out


def _highlights(db: Session, args: dict, ctx: Ctx, *, context: bool) -> dict:
    limit = _int(args, "limit", 60, 1, 300)
    keys = set(args.get("keys") or [])
    located_only = _bool(args, "located_only", False)
    key_annotations = sorted({int(k.split("-", 1)[0]) for k in keys
                              if str(k).split("-", 1)[0].isdigit()})
    if not args.get("question_id") and not args.get("paper_id") and not key_annotations:
        raise ToolError("Pass question_id, paper_id or keys.")
    stmt = select(models.Annotation).where(
        models.Annotation.status == models.ANNOT_DONE)
    if key_annotations and not args.get("question_id") and not args.get("paper_id"):
        # A citation key names its annotation: <annotation id>-<digest>.
        stmt = stmt.where(models.Annotation.id.in_(key_annotations))
    question = None
    if args.get("question_id"):
        question = _question(db, args)
        stmt = stmt.where(models.Annotation.research_question_id == question.id)
    if args.get("paper_id"):
        paper = _paper(db, args)
        stmt = stmt.where(models.Annotation.paper_id == paper.id)
    annotations = db.execute(stmt.order_by(models.Annotation.id)).scalars().all()

    grouped: dict[int, dict] = {}
    rejected: dict[str, str] = {}
    shown = total = nothing_found = 0
    for a in annotations:
        paper = a.paper
        if paper is None:
            continue
        if not a.findings:
            nothing_found += 1
        for f in a.findings or []:
            if f.get("rejected"):
                key = citations.finding_key(a.id, f)
                if keys and key in keys:
                    rejected[key] = f["rejected"]
                continue
            key = citations.finding_key(a.id, f)
            located = f.get("located") or ("pdf" if f.get("matched") else "none")
            if keys and key not in keys:
                continue
            if located_only and located == "none":
                continue
            total += 1
            if shown >= limit:
                continue
            shown += 1
            entry = grouped.setdefault(paper.id, {
                "paper_id": paper.id, "title": paper.title,
                "apa_inline": citations.apa_inline(paper.authors, paper.year),
                "reference": citations.apa_reference(paper),
                "question_id": a.research_question_id,
                "highlights": []})
            item = {"key": key, "heading": f.get("title"),
                    "quote": citations._shown(f.get("quote") or ""),
                    "page": f.get("page"), "located": located,
                    "why_relevant": f.get("why")}
            if f.get("printed_page"):
                item["printed_page"] = f["printed_page"]
            if f.get("section"):
                item["section"] = f["section"]
            if context and f.get("context"):
                item["context"] = f["context"]
            if f.get("added_by"):
                item["added_by"] = f["added_by"]
            item["open_at_highlight"] = (f"{ctx.base}/annotations/{a.id}/view"
                                         f"?page={f.get('page', 1)}")
            entry["highlights"].append(item)
    out = {"question": question.text if question else None,
           "papers": list(grouped.values()),
           "highlights_shown": shown,
           "papers_read_with_nothing_relevant": nothing_found}
    if rejected:
        out["rejected_keys"] = {k: f"rejected: failed the strict check - {why}"
                                for k, why in rejected.items()}
    if total > shown:
        out["note"] = (f"{total} highlights matched; showing {shown}. Narrow "
                       "with paper_id or keys, or raise limit.")
    return out


def _chunk(content: str, offset: int, max_chars: int) -> tuple[str, int]:
    chunk = content[offset:offset + max_chars]
    end = offset + len(chunk)
    if end < len(content):
        cut = chunk.rfind("\n")
        if cut > max_chars // 2:
            chunk = chunk[:cut]
            end = offset + cut
    return chunk, end


def _text_now(db: Session, paper: models.Paper) -> models.PaperText:
    """The text of a paper that has none kept yet - read on the spot from the
    PDF's text layer - or the reason it has none."""
    if paper.pdf_status != models.PDF_DOWNLOADED:
        why = {models.PDF_FAILED: "its PDF could not be fetched ("
                                  + (paper.pdf_error or "no reason recorded") + ")",
               models.PDF_PENDING: "its PDF is being fetched right now",
               }.get(paper.pdf_status, "no PDF has been fetched for it")
        raise ToolError(
            f"Paper {paper.id} has no full text: {why}. Its abstract is in "
            "get_paper. Only the user can add a PDF the sources do not offer - "
            "every article without one has an upload button in the app.")
    outcome = paper_texts.ensure(paper.id, wait=False)
    db.expire_all()
    text = db.get(models.PaperText, paper.id)
    if text is not None:
        return text
    if outcome == paper_texts.BUSY:
        raise ToolError(f"Paper {paper.id}'s PDF is being read right now - ask "
                        "again in a moment.")
    if outcome == paper_texts.NO_TEXT:
        raise ToolError(f"Paper {paper.id}'s PDF has no text layer (a scan). "
                        f"request_parse(paper_id={paper.id}) queues its "
                        "transcription as tasks for you.")
    raise ToolError(f"Paper {paper.id}'s PDF could not be read ({outcome}).")


def tool_get_paper_text(db: Session, args: dict, ctx: Ctx) -> dict:
    paper = _paper(db, args)
    text = db.get(models.PaperText, paper.id) or _text_now(db, paper)
    content = text.content
    if args.get("page") is not None:
        page = _int(args, "page", 1, 1, 100000)
        offset = paper_texts.page_offset(content, page)
        if offset is None:
            raise ToolError(f"Paper {paper.id}'s text has pages 1 to "
                            f"{text.page_count}; there is no page {page}.")
    else:
        offset = _int(args, "offset", 0, 0, max(0, len(content)))
    max_chars = _int(args, "max_chars", 20000, 500, 100000)
    chunk, end = _chunk(content, offset, max_chars)
    out = {
        "paper_id": paper.id,
        "title": paper.title,
        "apa_inline": citations.apa_inline(paper.authors, paper.year),
        "format": _text_format(text.reader),
        "pages": text.page_count,
        "characters": len(content),
        "offset": offset,
        "pages_in_chunk": [paper_texts.page_at(content, offset),
                           paper_texts.page_at(content, max(offset, end - 1))],
        "content": chunk,
        "about_pages": "Each <!-- page N --> marker opens PDF page N.",
    }
    if text.reader != models.READER_CLAUDE:
        out["about_format"] = (
            "The PDF's own text layer: tables come out flattened and equations "
            "garbled. If that matters, request_parse(paper_id) queues a proper "
            "transcription for you to do.")
    if offset == 0:
        sections = documents.outline(content)
        if sections:
            for section in sections:
                section["page"] = paper_texts.page_at(content, section["offset"])
            out["outline"] = sections
    if end < len(content):
        out["next_offset"] = end
        out["note"] = (f"Truncated: {len(content) - end} characters remain. Call "
                       f"get_paper_text again with offset={end}, or page=N.")
    out["to_cite_a_passage"] = ("add_evidence(question_id, paper_id, quote, why) "
                                "makes a passage from here citable.")
    return out


def _search_terms(query: str) -> list[str]:
    words = sorted({w.lower() for w in re.findall(r"\w+", query) if len(w) > 2})
    return words or [query.strip()]


def tool_search_paper_text(db: Session, args: dict, ctx: Ctx) -> dict:
    import unicodedata

    # The stored texts are clean, but a query can carry a ligature or a
    # compatibility character itself; searched as written it would miss.
    query = unicodedata.normalize("NFKC", (args.get("query") or "")).strip()
    if not query:
        raise ToolError("query is required.")
    context = _int(args, "context_chars", 400, 100, 2000)
    per_paper = _int(args, "hits_per_paper", 3, 1, 20)
    limit = _int(args, "limit", 10, 1, 50)
    scope = select(models.Paper.id).where(_listed())
    if args.get("question_id"):
        rq = _question(db, args)
        scope = scope.where(models.Paper.id.in_(_question_paper_ids(rq.id)))
    elif args.get("project_id"):
        project = _project(db, args)
        scope = scope.where(models.Paper.project_id == project.id)
    if args.get("paper_id"):
        paper = _paper(db, args)
        scope = scope.where(models.Paper.id == paper.id)
    else:
        scope = scope.where(models.Paper.status != models.STATUS_EXCLUDED)
    with_text = db.execute(
        select(func.count()).select_from(models.PaperText)
        .where(models.PaperText.paper_id.in_(scope))).scalar_one()
    stmt = select(models.PaperText).where(models.PaperText.paper_id.in_(scope))
    for term in _search_terms(query):
        stmt = stmt.where(models.PaperText.content.ilike(f"%{term}%"))
    found = []
    for text in db.execute(stmt.order_by(models.PaperText.paper_id)).scalars():
        hits = documents.excerpts(text.content, query, context=context,
                                  limit=per_paper)
        if hits:
            for hit in hits:
                hit["page"] = paper_texts.page_at(text.content, hit["match_offset"])
            found.append((text, hits))
    found.sort(key=lambda pair: (pair[1][0]["match"] != "phrase", -len(pair[1])))
    shown = found[:limit]
    papers = {p.id: p for p in db.execute(
        select(models.Paper)
        .where(models.Paper.id.in_([text.paper_id for text, _ in shown]))
    ).scalars()}
    out = {
        "query": query,
        "papers_with_text_in_scope": with_text,
        "papers_matched": len(found),
        "matches": [{
            "paper_id": text.paper_id,
            "title": papers[text.paper_id].title,
            "year": papers[text.paper_id].year,
            "apa_inline": citations.apa_inline(papers[text.paper_id].authors,
                                               papers[text.paper_id].year),
            "format": _text_format(text.reader),
            "hits": hits,
        } for text, hits in shown],
        "note": ("Case-insensitive and blind to line breaks; failing the phrase, "
                 "passages holding all its words are returned (see \"match\"). "
                 "add_evidence makes a hit citable in an answer."),
    }
    if not found:
        out["note"] = ("No passage matched this wording. Try fewer or plainer "
                       "words. Only papers whose PDF is in hand have a text - "
                       "search_papers searches every title and abstract.")
    return out


def _document_row(document: models.ProjectDocument, ctx: Ctx) -> dict:
    return {
        "document_id": document.id,
        "project_id": document.project_id,
        "title": document.title,
        "description": document.description,
        "status": document.status,
        "pages": document.page_count,
        "characters": document.char_count,
        "format": _text_format(document.reader),
        "error": document.error,
        "url": f"{ctx.base}/documents/{document.id}",
    }


def tool_list_documents(db: Session, args: dict, ctx: Ctx) -> dict:
    stmt = select(models.ProjectDocument)
    if args.get("project_id"):
        project = _project(db, args)
        stmt = stmt.where(models.ProjectDocument.project_id == project.id)
    docs = db.execute(stmt.order_by(models.ProjectDocument.id)).scalars().all()
    return {"documents": [_document_row(d, ctx) for d in docs],
            "note": ("The project's reference documents, not its papers. Read the "
                     "relevant one with get_document before drafting.")}


def tool_get_document(db: Session, args: dict, ctx: Ctx) -> dict:
    document = _document(db, args)
    content = document.content or ""
    if not content:
        raise ToolError(
            f"Document {document.id} has no extracted text (status: "
            f"{document.status}" + (f", {document.error}" if document.error else "")
            + f"). request_parse(document_id={document.id}) queues its "
            "transcription as tasks for you.")
    offset = _int(args, "offset", 0, 0, max(0, len(content)))
    max_chars = _int(args, "max_chars", 20000, 500, 100000)
    chunk, end = _chunk(content, offset, max_chars)
    out = _document_row(document, ctx)
    out.update({"offset": offset, "content": chunk})
    if offset == 0:
        sections = documents.outline(content)
        if sections:
            out["outline"] = sections
    if end < len(content):
        out["next_offset"] = end
        out["note"] = (f"Truncated: {len(content) - end} characters remain. Call "
                       f"get_document again with offset={end}.")
    return out


def tool_search_documents(db: Session, args: dict, ctx: Ctx) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required.")
    context = _int(args, "context_chars", 400, 100, 2000)
    per_document = _int(args, "hits_per_document", 5, 1, 20)
    stmt = select(models.ProjectDocument).where(
        models.ProjectDocument.content.isnot(None))
    if args.get("project_id"):
        project = _project(db, args)
        stmt = stmt.where(models.ProjectDocument.project_id == project.id)
    if args.get("document_id"):
        document = _document(db, args)
        stmt = stmt.where(models.ProjectDocument.id == document.id)
    docs = db.execute(stmt.order_by(models.ProjectDocument.id)).scalars().all()
    matches = []
    for document in docs:
        hits = documents.excerpts(document.content or "", query,
                                  context=context, limit=per_document)
        if hits:
            for hit in hits:
                hit["page"] = paper_texts.page_at(document.content,
                                                  hit.get("match_offset", hit["offset"]))
            matches.append({"document_id": document.id, "title": document.title,
                            "hits": hits})
    out = {"query": query, "documents_searched": len(docs), "matches": matches}
    if not matches:
        out["note"] = ("No passage matched this wording. Try fewer or plainer "
                       "words, or read the outline with get_document.")
    return out


def tool_bibliography(db: Session, args: dict, ctx: Ctx) -> dict:
    fmt = (args.get("format") or "apa").lower()
    if fmt not in ("apa", "bibtex", "ris", "csv"):
        raise ToolError("format must be one of: apa, bibtex, ris, csv")
    paper_ids = _ids(args.get("paper_ids"))
    if paper_ids:
        by_id = {p.id: p for p in db.execute(
            select(models.Paper).where(models.Paper.id.in_(paper_ids))).scalars()}
        papers = [by_id[pid] for pid in paper_ids if pid in by_id]
        missing = len(paper_ids) - len(papers)
    elif args.get("question_id"):
        rq = _question(db, args)
        cmap = citations.citation_map(research.question_annotations(db, rq.id))
        cited = [cmap[k]["paper_id"] for k in citations.cited_keys(rq.summary_html or "")
                 if k in cmap]
        by_id = {p.id: p for p in db.execute(
            select(models.Paper).where(models.Paper.id.in_(cited))).scalars()}
        papers = [by_id[pid] for pid in dict.fromkeys(cited) if pid in by_id]
        missing = 0
    elif args.get("project_id"):
        project = _project(db, args)
        stmt = select(models.Paper).where(models.Paper.project_id == project.id,
                                          _listed())
        status = args.get("status")
        if status in models.STATUSES:
            stmt = stmt.where(models.Paper.status == status)
        papers = db.execute(stmt.order_by(models.Paper.id)).scalars().all()
        missing = 0
    else:
        raise ToolError("Pass paper_ids, question_id (the papers its answer "
                        "cites), or project_id (optionally with status).")
    if fmt == "apa":
        text = "\n".join(sorted(citations.apa_reference(p) for p in papers))
    else:
        rows = [exports.paper_row(p) for p in papers]
        text = {"bibtex": exports.to_bibtex, "ris": exports.to_ris,
                "csv": exports.to_csv}[fmt](rows)
    result = {"format": fmt, "count": len(papers), "text": text}
    if missing:
        result["note"] = f"{missing} of the paper ids were not found."
    return result


def tool_list_tags(db: Session, args: dict, ctx: Ctx) -> dict:
    project = _project(db, args)
    return {"project_id": project.id, "tags": tags.with_counts(db, project.id)}


def tool_list_pdf_texts(db: Session, args: dict, ctx: Ctx) -> dict:
    """Which PDFs of a project have what kind of text - so "parse the
    documents" has a list to work from."""
    project = _project(db, args)
    papers = db.execute(
        select(models.Paper).where(models.Paper.project_id == project.id,
                                   models.Paper.pdf_status == models.PDF_DOWNLOADED)
        .order_by(models.Paper.id)).scalars().all()
    texts = paper_texts.summaries(db, [p.id for p in papers])
    waiting = {t.paper_id for t in tasks.live_tasks(db, kinds=[models.TASK_PARSE_PDF],
                                                    project_id=project.id)
               if t.paper_id}
    rows = []
    for paper in papers:
        text = texts.get(paper.id)
        rows.append({"paper_id": paper.id, "title": paper.title,
                     "text": (models.READER_LABELS.get(text["reader"], text["reader"])
                              if text else "none - the PDF has no text layer"),
                     "pages": text["pages"] if text else None,
                     "parse_queued": paper.id in waiting})
    docs = [{"document_id": d.id, "title": d.title,
             "text": (models.READER_LABELS.get(d.reader, d.reader) if d.content
                      else "none"), "pages": d.page_count,
             "parse_queued": paper_texts.parse_requested(db, document=d)}
            for d in project.documents]
    return {"papers": rows, "documents": docs,
            "note": ("\"Text layer\" is the PDF's own text, read automatically - "
                     "tables flattened, equations garbled, nothing at all for a "
                     "scan. request_parse queues a proper transcription of one "
                     "PDF as tasks for you; then work them with next_task.")}


def tool_find_article(db: Session, args: dict, ctx: Ctx) -> dict:
    rq = _question(db, args)
    query = (args.get("query") or "").strip()
    if len(query) < 4:
        raise ToolError("Give the article's title, or a distinctive phrase from it.")
    limit = _int(args, "limit", 10, 1, 25)
    candidates = research.find_candidates(db, rq.project, query, rq=rq, limit=limit)
    journal_map = _journal_map(db, [c["paper"] for c in candidates])
    texts = _full_texts(db, [c["paper"] for c in candidates])
    out = []
    for c in candidates:
        paper = c["paper"]
        row = _paper_row(paper, ctx, abstract=True, journal=journal_map.get(paper.id),
                         full_text=texts.get(paper.id))
        row.update({
            "title_match": c["title_match"],
            "already_in_this_question": c["in_question"],
            "already_read_for_it": c["already_read"],
            "pdf_in_hand": c["has_pdf"],
            "open_access_link_known": bool(paper.oa_url),
            "disregarded": c["disregarded"],
        })
        if c["in_question"] and not c["has_pdf"]:
            row["hint"] = ("Already in this question but never fetched - "
                           "include_articles fetches and reads it.")
        out.append(row)
    return {"query": query, "question_id": rq.id, "label": rq.label,
            "candidates": out,
            "note": ("Nothing has been added - new hits are parked out of the "
                     "article list until chosen. Pass the paper_ids that belong "
                     "to include_articles. Check titles and years: a preprint and "
                     "its published version are different records.")}


# --------------------------------------------------------------- the queue

def _setup_warning() -> str | None:
    """What the user must set up before runs can work well, if anything."""
    if not settings.contact_email:
        return ("No contact email is set in LitReview's Settings, so Unpaywall "
                "is off: papers without a direct open-access link will fail to "
                "download. Ask the user to add their email under Settings "
                f"({settings.base_url}/settings) before starting questions.")
    return None


def tool_get_pending_work(db: Session, args: dict, ctx: Ctx) -> dict:
    project_id = _row(args, "project_id") or None
    wait = _int(args, "wait_seconds", 0, 0, tasks.MAX_WAIT_SECONDS)
    if wait:
        db.rollback()
        tasks.wait_for_work(SessionLocal, project_id=project_id, wait_seconds=wait)
        db.expire_all()
    counts = tasks.pending_counts(db, project_id)
    listing = tasks.live_tasks(db, **({"project_id": project_id} if project_id else {}))
    shown = [{"task_id": t.id, "kind": t.kind, "about": tasks.describe(t),
              "status": t.status, "project_id": t.project_id}
             for t in sorted(listing, key=lambda t: (t.priority, t.id))[:15]]
    activity = tasks.background_activity(db, project_id)
    out = {"tasks_waiting": counts["total"], "claimed": counts["claimed"],
           "by_kind": counts["by_kind"], "next_up": shown,
           "app_is_working_on": activity}
    setup = _setup_warning()
    if setup:
        out["setup_warning"] = setup
    failed = [rq for rq in db.execute(
        select(models.ResearchQuestion)
        .where(models.ResearchQuestion.status == models.RQ_FAILED,
               *( [models.ResearchQuestion.project_id == project_id] if project_id else []))
        .order_by(models.ResearchQuestion.updated_at.desc()).limit(5)).scalars().all()
        if rq.error]
    if failed:
        out["questions_that_failed"] = [{"question_id": rq.id, "label": rq.label,
                                         "error": rq.error} for rq in failed]
    if counts["total"]:
        out["next"] = "Call next_task to take the first one."
    elif activity:
        out["next"] = ("Nothing is waiting yet, but the app is working - call "
                       "next_task with wait_seconds=45.")
    else:
        out["next"] = "Nothing is waiting. The queue is empty."
    return out


def tool_next_task(db: Session, args: dict, ctx: Ctx) -> dict:
    kind = args.get("kind") or None
    if kind and kind not in models.TASK_KINDS:
        raise ToolError(f"kind must be one of {models.TASK_KINDS}")
    wait = _int(args, "wait_seconds", 0, 0, tasks.MAX_WAIT_SECONDS)
    db.rollback()  # let go of the read snapshot while waiting
    task_id, activity = tasks.wait_and_claim(
        SessionLocal, kind=kind, project_id=_row(args, "project_id") or None,
        research_question_id=_row(args, "question_id") or None,
        client=ctx.client, wait_seconds=wait)
    if task_id is None:
        counts = tasks.pending_counts(db)
        out = {"task": None}
        if activity:
            out["app_is_working_on"] = activity
            out["next"] = ("The app is still working and will queue more tasks "
                           "when it is done. Call next_task again with "
                           "wait_seconds=45.")
        elif counts["claimed"]:
            out["next"] = (f"Nothing is waiting except {counts['claimed']} task(s) "
                           "already taken - by you in another chat or a subagent. "
                           "Their leases run out after "
                           f"{tasks.LEASE_MINUTES} minutes if abandoned.")
        else:
            out["next"] = ("Nothing is waiting - the queue is empty. Tell the user "
                           "what was done.")
        if kind and counts["total"]:
            out["other_kinds_waiting"] = counts["by_kind"]
            if counts["by_kind"].get(kind):
                out["next"] = (f"The {kind} task(s) are taken already - by "
                               "another chat or subagent.")
            else:
                out["next"] = (f"No {kind} task is waiting, but other tasks "
                               f"are: {counts['by_kind']}. Call next_task "
                               "without kind to take them.")
        return out
    db.expire_all()
    task = db.get(models.ClaudeTask, task_id)
    rendered = tasks.render(db, task)
    counts = tasks.pending_counts(db)
    rendered["queue_after_this"] = counts["open"]
    blocks = []
    for key, value in list((rendered.get("input") or {}).items()):
        if isinstance(value, str) and len(value) > OWN_BLOCK_CHARS:
            rendered["input"][key] = (f"(the {key.replace('_', ' ')} follows "
                                      "below, as its own block of plain text)")
            blocks.append({"type": "text",
                           "text": f"===== {key} =====\n{value}"})
    size = sum(len(b["text"]) for b in blocks) + len(json.dumps(rendered, default=str))
    _handed[ctx.session] = _handed.get(ctx.session, 0) + size
    if _handed[ctx.session] > CHAT_BUDGET_CHARS:
        rendered["chat_budget"] = (
            f"This chat has been handed about {_handed[ctx.session] // 1000}K "
            "characters of LitReview tasks. In Claude Desktop, after this task "
            "tell the user to continue in a new chat with the same request - "
            "the queue keeps its place.")
    if not blocks:
        return rendered
    head = json.dumps(rendered, ensure_ascii=False, indent=1, default=str)
    return Content([{"type": "text", "text": head}] + blocks)


def tool_submit_task(db: Session, args: dict, ctx: Ctx) -> dict:
    task_id = _row(args, "task_id")
    submitted = db.get(models.ClaudeTask, task_id)
    reading = submitted is not None and submitted.kind == models.TASK_ANNOTATE_PAPER
    if "output" not in args:
        raise ToolError("Pass output: an object matching the task's output_schema.")
    try:
        outcome = tasks.submit(db, task_id, args.get("output"), client=ctx.client)
    except tasks.TaskError as exc:
        raise ToolError(str(exc)) from None
    counts = tasks.pending_counts(db)
    activity = tasks.background_activity(db)
    out = {"accepted": True, "task_id": task_id, **(outcome or {}),
           "tasks_waiting": counts["open"]}
    if reading:
        out["then"] = ("This reading task is done. A reader subagent stops here "
                       "and reports; the main conversation calls next_task for "
                       "the rest.")
    elif counts["open"]:
        out["then"] = "Call next_task for the next one."
    elif activity:
        out["then"] = ("The app is working (" + "; ".join(activity) + ") - call "
                       "next_task with wait_seconds=45.")
    else:
        out["then"] = "Nothing else is waiting."
    return out


def tool_release_task(db: Session, args: dict, ctx: Ctx) -> dict:
    try:
        task = tasks.release(db, _row(args, "task_id"))
    except tasks.TaskError as exc:
        raise ToolError(str(exc)) from None
    return {"released": task.id, "status": task.status}


def tool_skip_task(db: Session, args: dict, ctx: Ctx) -> dict:
    task = db.get(models.ClaudeTask, _row(args, "task_id"))
    if task is None:
        raise ToolError(f"No task {args.get('task_id')!r}.")
    if not task.live:
        raise ToolError(f"Task {task.id} is not waiting ({task.status}).")
    reason = (args.get("reason") or "").strip() or "Skipped by Claude"
    tasks.drop(db, task, reason[:500])
    return {"skipped": task.id, "reason": reason,
            "then": "The rest of its step goes on without it. Call next_task."}


def _page_range(raw: str, total: int) -> list[int]:
    pages: list[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            pages.extend(range(max(1, lo), min(total, hi) + 1))
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= total:
                pages.append(n)
    return list(dict.fromkeys(pages))


def tool_get_pdf_pages(db: Session, args: dict, ctx: Ctx) -> Content:
    source = None
    label = ""
    if args.get("task_id"):
        task = db.get(models.ClaudeTask, _row(args, "task_id"))
        if task is None:
            raise ToolError(f"No task {args.get('task_id')!r}.")
        args = {**args, "paper_id": task.paper_id, "document_id": task.document_id}
    if args.get("paper_id"):
        paper = _paper(db, args)
        source = pdfs.original_path(paper.project.slug, paper.id)
        label = f"paper {paper.id}: {paper.title}"
    elif args.get("document_id"):
        document = _document(db, args)
        source = pdfs.document_path(document.project.slug, document.id)
        label = f"document {document.id}: {document.title}"
    if source is None or not source.exists():
        raise ToolError("Pass task_id, paper_id or document_id of a PDF that is "
                        "in the library.")
    total = paper_texts.page_count(source)
    pages = _page_range(args.get("pages") or "1", total)
    if not pages:
        raise ToolError(f"pages must name pages between 1 and {total}, e.g. \"3-5\".")
    shown = pages[:paper_texts.MAX_IMAGES_PER_CALL]
    items: list[dict] = [{"type": "text", "text": (
        f"Pages {', '.join(map(str, shown))} of {label} ({total} pages)."
        + (f" Only {len(shown)} images per call - ask for "
           f"{','.join(map(str, pages[len(shown):]))} next." if len(pages) > len(shown) else ""))}]
    for number in shown:
        png = paper_texts.render_page_png(source, number)
        items.append({"type": "text", "text": f"Page {number}:"})
        items.append({"type": "image", "mimeType": "image/png",
                      "data": base64.b64encode(png).decode("ascii")})
    return Content(items)


# ------------------------------------------------------------ write tools

def tool_create_project(db: Session, args: dict, ctx: Ctx) -> dict:
    try:
        project = services.create_project(db, args.get("name") or "",
                                          args.get("description"))
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return {"project_id": project.id, "name": project.name,
            "url": f"{ctx.base}/projects/{project.id}",
            "settings": {"results_per_source": project.fetch_per_source,
                         "ranked": project.rank_top, "read": project.download_top},
            "next": "ask_question(project_id, question) starts a review."}


def tool_ask_question(db: Session, args: dict, ctx: Ctx) -> dict:
    project = _project(db, args)
    text = (args.get("question") or "").strip()
    if len(text) < 15:
        raise ToolError("Write the research question out in full - it drives the "
                        "search, the ranking and the reading.")
    if not _bool(args, "force"):
        duplicate = _duplicate_of(db, project.id, text)
        if duplicate is not None:
            raise ToolError(
                f"{duplicate.label} already asks nearly this: \"{duplicate.text}\". "
                f"Read it with get_answer(question_id={duplicate.id}). Only if it "
                "genuinely asks something else, call again with force=true.")
    parsed = None
    if args.get("concept_groups") or args.get("query"):
        parsed = {k: args.get(k) for k in ("concept_groups", "query", "headline",
                                           "keywords", "areas", "year_from",
                                           "year_to")}
    with services.question_number_lock(project.id):
        rq = models.ResearchQuestion(project_id=project.id, text=text,
                                     created_by="mcp", mcp_session=ctx.session)
        db.add(rq)
        db.flush()
        research.start_question(db, rq, parsed=parsed, source="mcp",
                                session=ctx.session, first_run=True)
    setup = _setup_warning()
    return {"question_id": rq.id, "label": rq.label, "question": rq.text,
            "status": rq.status_label,
            **({"setup_warning": setup} if setup else {}),
            "url": f"{ctx.base}/questions/{rq.id}",
            "next": ("The app is searching now; call next_task with "
                     "wait_seconds=45 to rank the results."
                     if parsed else
                     "Its first task - planning the search - is waiting. Call "
                     "next_task.")}


def tool_rerun_question(db: Session, args: dict, ctx: Ctx) -> dict:
    rq = _question(db, args)
    mode = args.get("mode") or "search"
    if mode not in ("search", "evidence"):
        raise ToolError("mode is \"search\" (search again) or \"evidence\" "
                        "(rewrite from what is already read).")
    text = (args.get("question") or "").strip()
    if text and text != rq.text:
        rq.text = text
    research.start_question(db, rq, mode="full" if mode == "search" else "evidence",
                            source="mcp", session=ctx.session)
    return {"question_id": rq.id, "label": rq.label, "status": rq.status_label,
            "next": "Call next_task (wait_seconds=45 while the app works)."}


def tool_include_articles(db: Session, args: dict, ctx: Ctx) -> dict:
    rq = _question(db, args)
    paper_ids = _ids(args.get("paper_ids"))
    if not paper_ids:
        raise ToolError("paper_ids is required - the ids find_article returned.")
    if len(paper_ids) > 20:
        raise ToolError("Add at most 20 articles at a time.")
    scores: dict[int, float] = {}
    for key, value in (args.get("scores") or {}).items() if isinstance(args.get("scores"), dict) else []:
        try:
            scores[int(key)] = max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            continue
    wanted = db.execute(
        select(models.Paper).where(models.Paper.id.in_(paper_ids),
                                   models.Paper.project_id == rq.project_id)
    ).scalars().all()
    if not wanted:
        raise ToolError("None of those paper_ids are in this project.")
    papers, decided = research.include_papers(db, rq, [p.id for p in wanted], scores)
    from . import worker

    worker.submit(research.fetch_and_annotate, rq.id, [p.id for p in papers])
    needs_pdf = [p for p in papers if p.pdf_status != models.PDF_DOWNLOADED
                 and not p.oa_url and not p.doi]
    return {
        "question_id": rq.id, "label": rq.label,
        "included": [{"paper_id": p.id, "title": p.title,
                      "score": decided[p.id]["score"],
                      "scored_by": decided[p.id]["chosen_by"]} for p in papers],
        "now_happening": ("Their PDFs are being fetched; each one that arrives "
                          "becomes a reading task for this question. Call "
                          "next_task with wait_seconds=45 to read them. The "
                          "answer is left as it is until a rewrite: "
                          f"rerun_question(question_id={rq.id}, mode=\"evidence\")."),
        "no_way_to_fetch_these": [{"paper_id": p.id, "title": p.title,
                                   "landing_page": p.url} for p in needs_pdf],
        "about_the_pdfs": (
            ("A fetch is attempted for every article (a DOI is resolved "
             "through Unpaywall). " if settings.contact_email else
             "Unpaywall is off (no contact email in Settings), so only articles "
             "with a direct open-access link can be fetched. ")
            + "Check get_paper for how each turned out before telling the user "
              "one is unavailable; only the user can upload a PDF the sources "
              "do not offer."),
    }


def tool_add_evidence(db: Session, args: dict, ctx: Ctx) -> dict:
    rq = _question(db, args)
    paper = _paper(db, args)
    if paper.project_id != rq.project_id:
        raise ToolError("That paper is in a different project from the question.")
    try:
        result = research.add_evidence(
            db, rq, paper, args.get("quote") or "", args.get("why") or "",
            args.get("title") or "", added_by=f"Claude ({ctx.client or 'chat'})")
    except research.EvidenceRefused as exc:
        db.rollback()
        raise ToolError(str(exc)) from None
    db.commit()
    return {**result, "question_id": rq.id, "label": rq.label,
            "use": f"Cite it in an answer as [CIT:{result['citation_key']}] "
                   "(save_answer)."}


def tool_save_answer(db: Session, args: dict, ctx: Ctx) -> dict:
    rq = _question(db, args)
    if rq.running and rq.status != models.RQ_DONE:
        raise ToolError(f"{rq.label} is in the middle of a run "
                        f"({rq.status_label.lower()}); save once it is done.")
    problems = research.save_answer_text(db, rq, args.get("text") or "")
    if problems:
        db.rollback()
        raise ToolError("Not saved: " + "; ".join(problems) + ". Use only keys "
                        "from get_answer / get_highlights / add_evidence.")
    db.commit()
    return {"saved": True, "question_id": rq.id, "label": rq.label,
            "url": f"{ctx.base}/questions/{rq.id}",
            "note": "The answer in the app now reads as you wrote it."}


def tool_tag_papers(db: Session, args: dict, ctx: Ctx) -> dict:
    paper_ids = _ids(args.get("paper_ids"))
    add = [str(t) for t in (args.get("add") or []) if str(t).strip()]
    remove = [str(t) for t in (args.get("remove") or []) if str(t).strip()]
    if not paper_ids or not (add or remove):
        raise ToolError("Pass paper_ids and tags to add and/or remove.")
    if len(paper_ids) > 100:
        raise ToolError("At most 100 papers per call.")
    out = []
    for paper_id in paper_ids:
        paper = db.get(models.Paper, paper_id)
        if paper is None:
            continue
        tags.add(db, paper, add)
        if remove:
            tags.remove(db, paper, remove)
        out.append({"paper_id": paper.id, "tags": paper.tag_names})
    db.commit()
    return {"papers": out}


def tool_set_screening(db: Session, args: dict, ctx: Ctx) -> dict:
    paper_ids = _ids(args.get("paper_ids"))
    status = args.get("status")
    if status is not None and status not in models.STATUSES:
        raise ToolError(f"status must be one of {models.STATUSES}")
    note = args.get("note")
    if not paper_ids or (status is None and note is None):
        raise ToolError("Pass paper_ids and a status and/or a note.")
    changed = []
    for paper_id in paper_ids[:100]:
        paper = db.get(models.Paper, paper_id)
        if paper is None:
            continue
        if status is not None:
            paper.status = status
        if note is not None:
            paper.note = str(note).strip() or None
        changed.append({"paper_id": paper.id, "status": paper.status,
                        "note": paper.note})
    db.commit()
    return {"papers": changed,
            "note": ("Excluded papers drop out of answers and searches." if
                     status == models.STATUS_EXCLUDED else None)}


def tool_verify_answer(db: Session, args: dict, ctx: Ctx) -> dict:
    rq = _question(db, args)
    task = research.request_verification(db, rq)
    if task is None:
        raise ToolError(f"{rq.label}'s answer has no citations to check.")
    tasks.commit(db)
    return {"queued": task.id, "label": rq.label,
            "next": "Call next_task(kind=\"verify_answer\") to do the check. "
                    "Its verdicts then show on every citation, in the app and "
                    "in get_answer, until the answer is edited."}


def tool_request_parse(db: Session, args: dict, ctx: Ctx) -> dict:
    if args.get("paper_id"):
        paper = _paper(db, args)
        if paper.pdf_status != models.PDF_DOWNLOADED:
            raise ToolError(f"Paper {paper.id} has no PDF in the library.")
        queued = paper_texts.request_parse(db, paper=paper, priority=30)
        target = f"paper {paper.id}"
    elif args.get("document_id"):
        document = _document(db, args)
        queued = paper_texts.request_parse(db, document=document, priority=30)
        target = f"document {document.id}"
    else:
        raise ToolError("Pass paper_id or document_id.")
    db.commit()
    if not queued:
        return {"queued": 0, "note": f"A parse of {target} is already waiting."}
    return {"queued": queued, "target": target,
            "next": (f"{queued} transcription task(s) are waiting - call "
                     "next_task(kind=\"parse_pdf\").")}


# --------------------------------------------------------------- the list

def _schema(properties: dict | None = None, required: list | None = None) -> dict:
    out: dict = {"type": "object", "properties": properties or {}}
    if required:
        out["required"] = required
    return out


_INT = {"type": "integer"}
_STR = {"type": "string"}
_BOOL = {"type": "boolean"}

TOOLS = [
    # --- the queue
    {"name": "next_task", "title": "Take the next LitReview task",
     "description": "Take the next task waiting for you in LitReview's queue - "
                    "planning a search, ranking results, reading a paper, "
                    "writing an answer, transcribing PDF pages, identifying an "
                    "uploaded PDF. Returns its instructions, its input and the "
                    "output_schema to answer with via submit_task. With "
                    "wait_seconds it waits while the app is still searching or "
                    "downloading. Filter by kind (e.g. \"annotate_paper\" for a "
                    "reading subagent), project_id or question_id.",
     "inputSchema": _schema({"kind": {"type": "string", "enum": models.TASK_KINDS},
                             "project_id": _INT, "question_id": _INT,
                             "wait_seconds": {"type": "integer",
                                              "description": "0-50: how long to "
                                              "wait for the app's background work "
                                              "to produce a task."}}),
     "handler": tool_next_task},
    {"name": "submit_task", "title": "Hand in a finished task",
     "description": "Hand in the result of a task from next_task. output must "
                    "match the task's output_schema. A refusal says exactly what "
                    "to fix; the task stays yours, so fix it and submit again.",
     "inputSchema": _schema({"task_id": _INT,
                             "output": {"type": "object",
                                        "description": "The answer, shaped as "
                                                       "the task's output_schema."}},
                            ["task_id", "output"]),
     "handler": tool_submit_task},
    {"name": "get_pending_work", "title": "See what is waiting",
     "description": "What is waiting in LitReview's queue (by kind, with the next "
                    "few tasks) and what the app itself is working on. Use it to "
                    "tell the user what there is to do; take tasks with next_task. "
                    "With wait_seconds it first waits (claiming nothing) while "
                    "the app is still searching or downloading.",
     "inputSchema": _schema({"project_id": _INT,
                             "wait_seconds": {"type": "integer",
                                              "description": "0-50"}}),
     "handler": tool_get_pending_work},
    {"name": "release_task", "title": "Give a task back",
     "description": "Give a task you took back to the queue unfinished, e.g. "
                    "before ending a chat.",
     "inputSchema": _schema({"task_id": _INT}, ["task_id"]),
     "handler": tool_release_task},
    {"name": "skip_task", "title": "Set a task aside",
     "description": "Give up on a task that cannot be done (an unreadable text, "
                    "a page that will not render), with the reason, so the rest "
                    "of its step goes on without it. The user sees the reason.",
     "inputSchema": _schema({"task_id": _INT, "reason": _STR}, ["task_id", "reason"]),
     "handler": tool_skip_task},
    {"name": "get_pdf_pages", "title": "See PDF pages as images",
     "description": "Page images of a PDF in the library (at most 6 per call), "
                    "for transcribing it in a parse_pdf task when you cannot read "
                    "the file directly. pages like \"3-5\" or \"1,4\".",
     "inputSchema": _schema({"task_id": _INT, "paper_id": _INT, "document_id": _INT,
                             "pages": _STR}, ["pages"]),
     "handler": tool_get_pdf_pages},
    # --- reading
    {"name": "list_projects", "title": "List review projects",
     "description": "The user's literature-review projects with their counts and "
                    "how many tasks wait for you in each. Start here when you do "
                    "not know which project the user means.",
     "inputSchema": _schema(), "handler": tool_list_projects},
    {"name": "list_questions", "title": "List research questions",
     "description": "Research questions in label order (RQ1, RQ2, ...) with their "
                    "status, whether they wait for you, and whether an answer "
                    "exists. Refer to questions by label; pass question_id to "
                    "tools.",
     "inputSchema": _schema({"project_id": _INT, "limit": _INT}),
     "handler": tool_list_questions},
    {"name": "get_answer", "title": "Get cited answers",
     "description": "The cited answer to one or more research questions, "
                    "compact: the text once with [CIT:key] markers, each key "
                    "resolved to its paper, page, located state, section, quote "
                    "and context, and each paper's APA reference once. Use before "
                    "drafting anything from a question.",
     "inputSchema": _schema({"question_id": _INT,
                             "question_ids": {"type": "array", "items": _INT},
                             "include_quotes": _BOOL, "include_context": _BOOL,
                             "include_why": _BOOL,
                             "max_chars": {"type": "integer",
                                           "description": "Size budget (default "
                                                          "40000)."}}),
     "handler": tool_get_answer},
    {"name": "get_highlights", "title": "Get highlighted passages",
     "description": "The verbatim passages found in the PDFs, grouped by paper, "
                    "with page, section, a sentence of context either side, and "
                    "located (pdf / text / none). Filter by question_id, paper_id "
                    "or citation keys.",
     "inputSchema": _schema({"question_id": _INT, "paper_id": _INT,
                             "keys": {"type": "array", "items": _STR},
                             "located_only": _BOOL, "limit": _INT,
                             "include_context": _BOOL,
                             "max_chars": {"type": "integer",
                                           "description": "Size budget "
                                                          "(default 40000)."}}),
     "handler": tool_get_highlights},
    {"name": "search_papers", "title": "Search the library's papers",
     "description": "Search papers in the library (titles and abstracts) - not "
                    "the web. With question_id, ordered by that question's "
                    "relevance scores. Excluded papers are left out unless "
                    "status=\"excluded\". Filter by tag, year, has_pdf.",
     "inputSchema": _schema({"query": {"type": "string",
                                       "description": "Words, each matched in "
                                                      "title, abstract, authors "
                                                      "or journal."},
                             "question_id": _INT, "project_id": _INT,
                             "status": {"type": "string", "enum": models.STATUSES},
                             "tag": _STR, "year_from": _INT, "year_to": _INT,
                             "has_pdf": _BOOL, "annotated_only": _BOOL,
                             "min_score": {"type": "number"}, "limit": _INT,
                             "include_abstract": _BOOL}),
     "handler": tool_search_papers},
    {"name": "get_paper", "title": "Get one paper",
     "description": "One paper in full: details, tags, the user's note, its "
                    "relevance to each question and its highlighted passages.",
     "inputSchema": _schema({"paper_id": _INT, "include_annotations": _BOOL},
                            ["paper_id"]),
     "handler": tool_get_paper},
    {"name": "get_paper_text", "title": "Read a paper's full text",
     "description": "A paper's full text, in chunks, with <!-- page N --> "
                    "markers. Jump with page=N or offset. The first chunk carries "
                    "the outline when the text has headings.",
     "inputSchema": _schema({"paper_id": _INT, "page": _INT, "offset": _INT,
                             "max_chars": _INT}, ["paper_id"]),
     "handler": tool_get_paper_text},
    {"name": "search_paper_text", "title": "Search inside papers",
     "description": "Find a phrase across the full texts of the library's "
                    "papers, with the page and context of each hit.",
     "inputSchema": _schema({"query": _STR, "project_id": _INT, "question_id": _INT,
                             "paper_id": _INT, "context_chars": _INT,
                             "hits_per_paper": _INT, "limit": _INT}, ["query"]),
     "handler": tool_search_paper_text},
    {"name": "list_documents", "title": "List project documents",
     "description": "A project's reference documents - protocol, coding manual, "
                    "guidelines, the manuscript. Check before drafting.",
     "inputSchema": _schema({"project_id": _INT}), "handler": tool_list_documents},
    {"name": "get_document", "title": "Read a project document",
     "description": "A project document's text in chunks, with its outline.",
     "inputSchema": _schema({"document_id": _INT, "offset": _INT, "max_chars": _INT},
                            ["document_id"]),
     "handler": tool_get_document},
    {"name": "search_documents", "title": "Search project documents",
     "description": "Find a phrase in a project's documents.",
     "inputSchema": _schema({"query": _STR, "project_id": _INT, "document_id": _INT,
                             "context_chars": _INT, "hits_per_document": _INT},
                            ["query"]),
     "handler": tool_search_documents},
    {"name": "bibliography", "title": "Reference list",
     "description": "References as APA, BibTeX, RIS or CSV - for paper_ids, the "
                    "papers a question's answer cites (question_id), or a "
                    "project's papers (optionally by screening status).",
     "inputSchema": _schema({"format": {"type": "string",
                                        "enum": ["apa", "bibtex", "ris", "csv"]},
                             "paper_ids": {"type": "array", "items": _INT},
                             "question_id": _INT, "project_id": _INT,
                             "status": {"type": "string", "enum": models.STATUSES}}),
     "handler": tool_bibliography},
    {"name": "list_tags", "title": "List a project's tags",
     "description": "The tags used in a project, with how many papers carry each.",
     "inputSchema": _schema({"project_id": _INT}, ["project_id"]),
     "handler": tool_list_tags},
    {"name": "list_pdf_texts", "title": "Which PDFs have what text",
     "description": "For a project's PDFs and documents: whether each has the "
                    "PDF's own text layer, Claude's transcription, or no text at "
                    "all - the list to work from when asked to parse documents.",
     "inputSchema": _schema({"project_id": _INT}, ["project_id"]),
     "handler": tool_list_pdf_texts},
    {"name": "find_article", "title": "Look up a known article",
     "description": "Look an article up by title in the sources and the project, "
                    "for one question. Adds nothing - pick ids for "
                    "include_articles.",
     "inputSchema": _schema({"question_id": _INT, "query": _STR, "limit": _INT},
                            ["question_id", "query"]),
     "handler": tool_find_article},
    # --- changing the library
    {"name": "create_project", "title": "Create a project",
     "description": "Create a literature-review project.",
     "inputSchema": _schema({"name": _STR, "description": _STR}, ["name"]),
     "handler": tool_create_project},
    {"name": "ask_question", "title": "Start a research question",
     "description": "Start a full review run for a new research question. You "
                    "may plan the search yourself by passing concept_groups "
                    "(2-3 groups of synonyms; groups ANDed, terms ORed), query (a "
                    "flat 2-6 term fallback) and headline - otherwise planning it "
                    "is the first task. Check list_questions first; near-"
                    "duplicates are refused unless force=true.",
     "inputSchema": _schema({"project_id": _INT, "question": _STR,
                             "concept_groups": {"type": "array", "items": {
                                 "type": "array", "items": _STR}},
                             "query": _STR, "headline": _STR,
                             "keywords": {"type": "array", "items": _STR},
                             "year_from": _INT, "year_to": _INT, "force": _BOOL},
                            ["project_id", "question"]),
     "handler": tool_ask_question},
    {"name": "rerun_question", "title": "Run a question again",
     "description": "Run a question again, optionally reworded: mode \"search\" "
                    "searches again; mode \"evidence\" reads any PDFs it has not "
                    "read and rewrites the answer from what is collected.",
     "inputSchema": _schema({"question_id": _INT, "mode": {"type": "string",
                                                          "enum": ["search", "evidence"]},
                             "question": _STR}, ["question_id"]),
     "handler": tool_rerun_question},
    {"name": "include_articles", "title": "Add articles to a question",
     "description": "Add articles (ids from find_article) to a question: their "
                    "PDFs are fetched and each becomes a reading task. Give a "
                    "score (0-100) per paper_id to skip scoring.",
     "inputSchema": _schema({"question_id": _INT,
                             "paper_ids": {"type": "array", "items": _INT},
                             "scores": {"type": "object"}},
                            ["question_id", "paper_ids"]),
     "handler": tool_include_articles},
    {"name": "add_evidence", "title": "Make a passage citable",
     "description": "Make a passage you found yourself (in get_paper_text or "
                    "search_paper_text) citable in a question's answer. The quote "
                    "must be verbatim from the paper's text - it is checked, and "
                    "refused with the closest sentence if not found. Returns the "
                    "citation key.",
     "inputSchema": _schema({"question_id": _INT, "paper_id": _INT, "quote": _STR,
                             "why": _STR, "title": _STR},
                            ["question_id", "paper_id", "quote", "why"]),
     "handler": tool_add_evidence},
    {"name": "save_answer", "title": "Save an edited answer",
     "description": "Replace a question's answer with your text - plain "
                    "paragraphs with [CIT:key] markers from get_answer, "
                    "get_highlights or add_evidence. Unknown keys are refused. "
                    "This replaces the user's text; do it when they ask.",
     "inputSchema": _schema({"question_id": _INT, "text": _STR},
                            ["question_id", "text"]),
     "handler": tool_save_answer},
    {"name": "tag_papers", "title": "Tag papers",
     "description": "Add and/or remove free-form tags on papers.",
     "inputSchema": _schema({"paper_ids": {"type": "array", "items": _INT},
                             "add": {"type": "array", "items": _STR},
                             "remove": {"type": "array", "items": _STR}},
                            ["paper_ids"]),
     "handler": tool_tag_papers},
    {"name": "set_screening", "title": "Screen papers",
     "description": "Set the screening status (included / excluded / maybe / "
                    "unscreened) and/or the note of papers, when the user asks.",
     "inputSchema": _schema({"paper_ids": {"type": "array", "items": _INT},
                             "status": {"type": "string", "enum": models.STATUSES},
                             "note": _STR}, ["paper_ids"]),
     "handler": tool_set_screening},
    {"name": "verify_answer", "title": "Check an answer's citations",
     "description": "Queue a check of every citation in a question's answer "
                    "against its passage (supported / partial / unsupported). "
                    "Then do it with next_task. Use it before the user relies "
                    "on an answer, or when they ask whether it holds up.",
     "inputSchema": _schema({"question_id": _INT}, ["question_id"]),
     "handler": tool_verify_answer},
    {"name": "request_parse", "title": "Queue a PDF transcription",
     "description": "Queue Claude's transcription of a paper's or document's PDF "
                    "(markdown, tables, LaTeX) as parse_pdf tasks - for scans "
                    "with no text layer, or where tables and equations matter.",
     "inputSchema": _schema({"paper_id": _INT, "document_id": _INT}),
     "handler": tool_request_parse},
]

# Hints for the client (MCP 2025-06-18 tool annotations): which tools only
# read, and which reach beyond this computer (the scholarly sources). None
# deletes anything, so destructiveHint is false throughout.
_READ_ONLY = {"get_pending_work", "get_pdf_pages", "list_projects",
              "list_questions", "get_answer", "get_highlights", "search_papers",
              "get_paper", "get_paper_text", "search_paper_text",
              "list_documents", "get_document", "search_documents",
              "bibliography", "list_tags", "list_pdf_texts"}
_OPEN_WORLD = {"find_article", "ask_question", "rerun_question",
               "include_articles"}
for _tool in TOOLS:
    _tool["annotations"] = {
        "title": _tool["title"],
        "readOnlyHint": _tool["name"] in _READ_ONLY,
        "destructiveHint": False,
        "idempotentHint": _tool["name"] in _READ_ONLY,
        "openWorldHint": _tool["name"] in _OPEN_WORLD,
    }

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}

PROMPTS = [
    {"name": "work_queue", "title": "Work through my LitReview queue",
     "description": "Do everything waiting for Claude in LitReview.",
     "arguments": [],
     "text": ("Work through everything waiting in my LitReview queue. Use "
              "get_pending_work to see what there is, then next_task / "
              "submit_task until nothing is left, waiting with wait_seconds=45 "
              "while the app searches or downloads. Tell me briefly what you did "
              "at the end.")},
    {"name": "new_question", "title": "Start a research question",
     "description": "Start a new research question in a project.",
     "arguments": [{"name": "project", "description": "Project name",
                    "required": True},
                   {"name": "question", "description": "The research question",
                    "required": True}],
     "text": ("In my LitReview project \"{project}\", start this research "
              "question and work it through to a cited answer: {question}")},
    {"name": "parse_documents", "title": "Parse my PDFs properly",
     "description": "Transcribe PDFs whose text layer is missing or poor.",
     "arguments": [{"name": "project", "description": "Project name",
                    "required": False}],
     "text": ("Look at the PDFs in my LitReview project {project} with "
              "list_pdf_texts. Queue a transcription (request_parse) for every "
              "paper or document with no text, and then do the parse_pdf tasks "
              "with next_task.")},
]


# ------------------------------------------------------------------ JSON-RPC

def _result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_result(payload: Any, is_error: bool = False) -> dict:
    if isinstance(payload, Content):
        return {"content": payload.items, "isError": is_error}
    text = (payload if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False, indent=1, default=str))
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _prompt(name: str, arguments: dict) -> dict | None:
    for prompt in PROMPTS:
        if prompt["name"] == name:
            values = {a["name"]: (arguments or {}).get(a["name"], "") or ""
                      for a in prompt["arguments"]}
            text = prompt["text"]
            for key, value in values.items():
                text = text.replace("{" + key + "}", str(value))
            text = re.sub(r"\s+", " ", text.replace("project  with", "project with")).strip()
            return {"description": prompt["description"],
                    "messages": [{"role": "user",
                                  "content": {"type": "text", "text": text}}]}
    return None


def handle(db: Session, msg: Any, ctx: Ctx) -> dict | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    if not isinstance(msg, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if msg_id is None:
        return None
    if not isinstance(method, str):
        return _rpc_error(msg_id, -32600, "Invalid Request: no method")
    if not isinstance(params, dict):
        return _rpc_error(msg_id, -32602, "params must be an object")

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        client = (params.get("clientInfo") or {}).get("name")
        if client:
            ctx.client = _remember_client(db, ctx.session, str(client)[:60])
        # Unthrottled: a client connecting is the call that says it has picked
        # LitReview up, which the Connect page waits for.
        tasks.note_client(db, ctx.client, force=True)
        return _result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False},
                             "prompts": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": [
            {k: v for k, v in tool.items() if k != "handler"} for tool in TOOLS]})
    if method == "prompts/list":
        return _result(msg_id, {"prompts": [
            {k: v for k, v in p.items() if k != "text"} for p in PROMPTS]})
    if method == "prompts/get":
        prompt = _prompt(params.get("name"), params.get("arguments") or {})
        if prompt is None:
            return _rpc_error(msg_id, -32602, f"Unknown prompt: {params.get('name')}")
        return _result(msg_id, prompt)
    if method == "tools/call":
        name = params.get("name")
        tool = TOOLS_BY_NAME.get(name) if isinstance(name, str) else None
        if tool is None:
            return _rpc_error(msg_id, -32602, f"Unknown tool: {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _rpc_error(msg_id, -32602, "arguments must be an object")
        logger.info("MCP tool %s %s", name, json.dumps(args, default=str)[:160])
        try:
            tasks.note_client(db, ctx.client)
        except Exception:
            db.rollback()
        try:
            payload = tool["handler"](db, args, ctx)
        except (ToolError, services.RunRefused) as exc:
            db.rollback()
            return _result(msg_id, _tool_result(str(exc), is_error=True))
        except Exception as exc:  # a broken tool must not kill the connector
            db.rollback()
            logger.exception("MCP tool %s failed", name)
            return _result(msg_id, _tool_result(f"{name} failed: {exc}",
                                                is_error=True))
        return _result(msg_id, _tool_result(payload))
    if method == "resources/list":
        return _result(msg_id, {"resources": []})
    if method == "resources/templates/list":
        return _result(msg_id, {"resourceTemplates": []})
    return _rpc_error(msg_id, -32601, f"Method not found: {method}")


def _session_id(request: Request, payload: Any) -> str:
    existing = request.headers.get("mcp-session-id", "").strip()
    if existing:
        return existing[:64]
    if _is_initialize(payload):
        return secrets.token_hex(16)
    return "no-session"


def _is_initialize(payload: Any) -> bool:
    if isinstance(payload, list):
        return any(isinstance(m, dict) and m.get("method") == "initialize"
                   for m in payload)
    return isinstance(payload, dict) and payload.get("method") == "initialize"


@router.get("/mcp")
def mcp_get() -> Response:
    """No server-initiated SSE stream; the spec's answer to that is 405."""
    return JSONResponse({"error": "This server replies to POST only (no SSE "
                                  "stream)."}, status_code=405)


@router.delete("/mcp")
def mcp_delete() -> Response:
    return Response(status_code=204)


@router.post("/mcp")
def mcp_post(request: Request, payload: Any = Body(default=None),
             db: Session = Depends(get_db)) -> Response:
    session = _session_id(request, payload)
    ctx = Ctx(base=settings.base_url, session=session,
              client=_client_of(db, session))
    if isinstance(payload, list):
        responses = [r for r in (handle(db, m, ctx) for m in payload)
                     if r is not None]
        response = (Response(status_code=202) if not responses
                    else JSONResponse(responses))
    else:
        handled = handle(db, payload, ctx)
        response = (Response(status_code=202) if handled is None
                    else JSONResponse(handled))
    if _is_initialize(payload) and not request.headers.get("mcp-session-id"):
        response.headers["Mcp-Session-Id"] = session
    return response
