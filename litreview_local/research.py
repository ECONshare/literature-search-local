"""Research-question orchestrator: a resumable state machine.

A research question drives the whole flow:

    parse (Claude) -> search (app) -> rank (Claude) -> download (app)
    -> read each paper (Claude; the app places the highlights)
    -> write the cited answer (Claude)

The app's own steps run in the background (worker.py) and are idempotent,
so a run interrupted by closing the app is picked up at the next start
(resume_all). Claude's steps are tasks in the queue (tasks.py) and simply
wait there - for minutes or for days - until the user asks Claude to work
through them. Each task kind is registered at the bottom of this module.

Papers already downloaded for an earlier question are never fetched again;
they only gain an annotation for the new question.
"""
import logging
import re
import uuid
from collections import Counter

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from . import (citations, claude_prompts, highlight, journals, models,
               paper_texts, pdfs, services, tasks, worker)
from .config import settings
from .db import SessionLocal
from .pipeline import SOURCE_LABELS

logger = logging.getLogger(__name__)

RANK_BATCH_SIZE = 20
# Characters of paper text per annotation task. A long paper is read in
# parts, each small enough to sit comfortably in a Claude Desktop chat.
ANNOTATE_CHUNK_CHARS = 35_000
RELEVANCE_BAND = 10  # points of score treated as "equally relevant"
# Below these scores a paper is not worth a reading: in the top Z it is
# skipped, and the walk down the ranking for a failed download stops there,
# rather than spending Claude's reading on tangential sources.
MIN_READ_SCORE = 40
BACKFILL_MIN_SCORE = 60
ABSTRACT_CHARS = 1500


def _set_status(db: Session, rq: models.ResearchQuestion, status: str,
                detail: str | None = None, *, commit: bool = True) -> None:
    rq.status = status
    rq.status_detail = detail
    if commit:
        db.commit()


def _note(rq: models.ResearchQuestion, text: str) -> None:
    """Add a line to what the run will report when it is done."""
    state = dict(rq.run_state or {})
    notes = list(state.get("notes") or [])
    if text not in notes:
        notes.append(text)
    state["notes"] = notes
    rq.run_state = state


class _Stopped(Exception):
    """The question was stopped (or deleted) while a step was working."""


def _still(db: Session, rq_id: int, *statuses: str) -> models.ResearchQuestion:
    """The question as it is now, or _Stopped if it left the given statuses -
    asked between a step's slow parts, so a Stop pressed during a search or a
    download is not overwritten by the step that was running."""
    rq = db.get(models.ResearchQuestion, rq_id)
    if rq is not None:
        db.refresh(rq)
    if rq is None or rq.status not in statuses:
        raise _Stopped()
    return rq


def _record_failure(db: Session, rq_id: int, exc: Exception) -> None:
    """Write the failure onto the question, whatever broke - rolling back
    first, and from a fresh session if even that one is unusable."""
    message = str(exc) or exc.__class__.__name__
    try:
        db.rollback()
        rq = db.get(models.ResearchQuestion, rq_id)
        if rq is not None:
            rq.error = message
            _set_status(db, rq, models.RQ_FAILED, None)
            return
    except Exception:
        logger.exception("Could not record the failure of question %s", rq_id)
    fresh = SessionLocal()
    try:
        rq = fresh.get(models.ResearchQuestion, rq_id)
        if rq is not None:
            rq.error = message
            rq.status = models.RQ_FAILED
            rq.status_detail = None
            fresh.commit()
    finally:
        fresh.close()


# ===================================================== views of a question

def question_annotations(db: Session, rq_id: int) -> list[models.Annotation]:
    """Every completed annotation for a question, including empty ones.

    An annotation with no findings means "read, and nothing relevant found" -
    a real result, not the same as never read. Disregarded papers are left
    out: their citations render as unavailable until the answer is rewritten.
    """
    annotations = db.execute(
        select(models.Annotation)
        .options(selectinload(models.Annotation.paper))
        .where(models.Annotation.research_question_id == rq_id,
               models.Annotation.status == models.ANNOT_DONE)
        .order_by(models.Annotation.id)
    ).scalars().all()
    return [a for a in annotations
            if a.paper is None or a.paper.status != models.STATUS_EXCLUDED]


def _question_paper_ids(rq_id: int):
    return (select(models.search_papers.c.paper_id)
            .select_from(models.search_papers.join(
                models.Search,
                models.Search.id == models.search_papers.c.search_id))
            .where(models.Search.research_question_id == rq_id))


def outstanding_papers(db: Session,
                       rq: models.ResearchQuestion) -> list[models.Paper]:
    """Papers this question selected and has a PDF for, but has never read -
    usually a PDF uploaded by hand after the run."""
    papers = db.execute(
        select(models.Paper)
        .where(models.Paper.id.in_(_question_paper_ids(rq.id)),
               models.Paper.stage == models.STAGE_RETRIEVAL,
               models.Paper.status != models.STATUS_EXCLUDED,
               models.Paper.pdf_status == models.PDF_DOWNLOADED)
    ).scalars().all()
    handled = {a.paper_id for a in db.execute(
        select(models.Annotation)
        .where(models.Annotation.research_question_id == rq.id,
               models.Annotation.status.in_([models.ANNOT_DONE,
                                             models.ANNOT_PENDING,
                                             models.ANNOT_NEEDS_OCR]))
    ).scalars().all()}
    return [p for p in papers if p.id not in handled]


def unused_annotations(db: Session, rq: models.ResearchQuestion,
                       annotations: list | None = None) -> list[models.Annotation]:
    """Annotations written after the answer was, so the prose predates them."""
    if rq.answer_written_at is None:
        return []
    written = models.aware(rq.answer_written_at)
    fresh = []
    for annotation in (question_annotations(db, rq.id)
                       if annotations is None else annotations):
        stamp = models.aware(annotation.updated_at or annotation.created_at)
        if stamp is not None and stamp > written:
            fresh.append(annotation)
    return fresh


def dangling_citations(db: Session, rq: models.ResearchQuestion,
                       annotations: list | None = None) -> list[str]:
    """Citations in the answer whose passage no longer exists."""
    if not rq.summary_html:
        return []
    cmap = citations.citation_map(question_annotations(db, rq.id)
                                  if annotations is None else annotations)
    return [key for key in citations.cited_keys(rq.summary_html)
            if citations.resolve_key(key, cmap) is None]


def question_updates(db: Session, rq: models.ResearchQuestion) -> dict:
    """What is waiting on one question: unread PDFs, annotations the answer
    has not seen, citations without evidence."""
    annotations = question_annotations(db, rq.id)
    return {"unread": len(outstanding_papers(db, rq)),
            "unused": len(unused_annotations(db, rq, annotations)),
            "dangling": len(dangling_citations(db, rq, annotations))}


def update_counts(db: Session, project_id: int) -> dict[int, dict]:
    counts = {}
    for rq in db.execute(
        select(models.ResearchQuestion)
        .where(models.ResearchQuestion.project_id == project_id)
    ).scalars().all():
        if rq.running:
            continue
        waiting = question_updates(db, rq)
        if any(waiting.values()):
            counts[rq.id] = waiting
    return counts


def outstanding_counts(db: Session, project_id: int) -> dict[int, int]:
    return {rq_id: c["unread"] for rq_id, c in
            update_counts(db, project_id).items() if c["unread"]}


# Findings checked against their paper's text by the strict, whole-quote
# matcher carry "v": VERIFIED. Libraries from builds before it are checked
# once at startup (reverify_findings).
VERIFIED = 2


def reverify_findings() -> Counter:
    """Startup: hold every stored passage to the strict check, once.

    A library made by an earlier build can hold quotes that never passed
    it - one accepted on its first 80 characters, one corrupted on its way
    in. Those are marked rejected (kept, so nothing is lost, but never
    evidence again: the writer does not see them and citations of them
    render as unavailable). A passage stored twice under two keys keeps
    both keys working, with the later one an alias of the first. The quote
    text itself is left as it is - a citation key is computed from it.
    """
    stats: Counter = Counter()
    db = SessionLocal()
    try:
        for annotation in db.execute(
            select(models.Annotation)
            .where(models.Annotation.status == models.ANNOT_DONE)
        ).scalars().all():
            findings = list(annotation.findings or [])
            if not findings or all(f.get("v") == VERIFIED for f in findings):
                continue
            text = db.get(models.PaperText, annotation.paper_id)
            seen: dict[str, str] = {}
            checked = []
            for finding in findings:
                finding = dict(finding)
                norm = highlight._norm_map(finding.get("quote") or "")[0]
                if finding.get("v") != VERIFIED:
                    found = (highlight.match_quote(text.content, finding.get("quote") or "")
                             if text is not None else None)
                    if found is None:
                        finding["rejected"] = ("not in the paper's text when "
                                               "re-checked by a newer build")
                        stats["rejected"] += 1
                    finding["v"] = VERIFIED
                if not finding.get("rejected"):
                    if norm in seen:
                        finding["duplicate_of"] = seen[norm]
                        stats["duplicates"] += 1
                    else:
                        seen[norm] = citations.finding_key(annotation.id, finding)
                checked.append(finding)
            annotation.findings = checked
            stats["annotations"] += 1
        db.commit()
    except Exception:
        logger.exception("Re-checking stored passages failed")
        db.rollback()
    finally:
        db.close()
    if stats["rejected"] or stats["duplicates"]:
        logger.warning("Re-checked stored passages: %d rejected as not in their "
                       "paper's text, %d duplicates", stats["rejected"],
                       stats["duplicates"])
    return stats


# Findings highlighted by the source-span locator and enriched with printed
# pages carry "hv": HIGHLIGHTED. Annotations from older builds are redone
# once at startup (rehighlight_old) - locally, no Claude, no network.
HIGHLIGHTED = 2


_CLEANED_KEY = "texts_cleaned_v1"


def clean_old_texts() -> Counter:
    """Startup, once per library: give texts stored by older builds the
    cleaning new ones get when read - ligatures expanded, soft hyphens and
    zero-width characters gone, math letters a broken font map put in the
    Hangul block put back. Searching and reading then see the paper's words
    ("efficiency", not "eﬃciency"). Quotes and citation keys are untouched;
    where each passage sits is refreshed against the cleaned text."""
    stats: Counter = Counter()
    db = SessionLocal()
    try:
        if db.get(models.AppState, _CLEANED_KEY) is not None:
            return stats
        for text in db.execute(select(models.PaperText)).scalars().all():
            korean = highlight.is_korean(text.content)
            cleaned = highlight.clean_text(text.content, korean=korean)
            if cleaned == text.content:
                continue
            text.content = cleaned
            text.char_count = len(cleaned)
            stats["papers"] += 1
            for annotation in db.execute(
                select(models.Annotation)
                .where(models.Annotation.paper_id == text.paper_id)
            ).scalars().all():
                if annotation.findings:
                    annotation.findings = highlight.refresh_findings(
                        annotation.findings, cleaned, text.page_labels)
        for document in db.execute(select(models.ProjectDocument)).scalars().all():
            if not document.content:
                continue
            cleaned = highlight.clean_text(document.content,
                                           korean=highlight.is_korean(document.content))
            if cleaned != document.content:
                document.content = cleaned
                document.char_count = len(cleaned)
                stats["documents"] += 1
        db.add(models.AppState(key=_CLEANED_KEY,
                               value={"at": models.utcnow().isoformat()}))
        db.commit()
    except Exception:
        logger.exception("Cleaning stored texts failed")
        db.rollback()
    finally:
        db.close()
    if stats:
        logger.info("Cleaned the stored texts of %d papers and %d documents",
                    stats["papers"], stats["documents"])
    return stats


def rehighlight_old() -> Counter:
    """Startup: redraw the highlights of annotations an older build placed.

    Earlier builds searched the PDF for Claude's own wording, wrote the
    rationale as a note icon over the words, and knew no printed page
    numbers; the current locator places nearly every quote from its span in
    the stored text. Quotes - and so citation keys - are left exactly as
    they are; only where each sits is recomputed, and the highlighted copies
    (focused ones included) are drawn again.
    """
    stats: Counter = Counter()
    db = SessionLocal()
    try:
        for annotation in db.execute(
            select(models.Annotation)
            .where(models.Annotation.status == models.ANNOT_DONE)
        ).scalars().all():
            findings = list(annotation.findings or [])
            if not findings or all(f.get("hv") == HIGHLIGHTED for f in findings):
                continue
            paper = annotation.paper
            text = db.get(models.PaperText, annotation.paper_id)
            if paper is None or paper.project is None or text is None:
                continue
            live = [f for f in findings if not f.get("rejected")]
            try:
                placed = highlight.highlight_annotation(
                    paper.project.slug, paper.id, annotation.id, live,
                    content=text.content)
                placed = highlight.enrich_from_text(
                    placed, text.content, paper_texts.page_at,
                    paper_texts.section_at, page_labels=text.page_labels)
            except Exception:
                logger.exception("Could not redraw annotation %s", annotation.id)
                continue
            merged, i = [], 0
            for finding in findings:
                if finding.get("rejected"):
                    merged.append(dict(finding, hv=HIGHLIGHTED))
                    continue
                new = dict(placed[i])
                i += 1
                for keep in ("quote", "title", "why", "added_by", "about", "v",
                             "duplicate_of"):
                    if keep in finding:
                        new[keep] = finding[keep]
                new["hv"] = HIGHLIGHTED
                merged.append(new)
                stats["highlighted" if new.get("matched") else "unplaced"] += 1
            annotation.findings = merged
            for stale in pdfs.annotation_path(paper.project.slug, annotation.id).parent.glob(
                    f"a{annotation.id}-focus-*.pdf"):
                try:
                    stale.unlink()
                except OSError:
                    pass
            stats["annotations"] += 1
        db.commit()
    except Exception:
        logger.exception("Redrawing old highlights failed")
        db.rollback()
    finally:
        db.close()
    if stats["annotations"]:
        logger.info("Redrew %d annotations from an older build: %d passages "
                    "highlighted, %d not placeable", stats["annotations"],
                    stats["highlighted"], stats["unplaced"])
    return stats


def evidence_digest(cmap: dict[str, dict]) -> str:
    """The passage list shown to the writing and revision tasks: each paper
    once, then its passages under their keys."""
    by_paper: dict = {}
    for key, info in cmap.items():
        if info.get("alias_of"):
            continue  # the same passage under its first key
        by_paper.setdefault(info.get("paper_id"), []).append((key, info))
    blocks = []
    for entries in by_paper.values():
        first = entries[0][1]
        lines = [f"== {first['apa']} {first['paper_title']}"]
        for key, info in entries:
            where = f"p. {info.get('page')}"
            if info.get("section"):
                where += f", {info['section']}"
            line = f"[CIT:{key}] ({where}) \"{info['quote']}\" - {info['why']}"
            if info.get("about"):
                line += f" [reports: {info['about']}]"
            if info.get("located") == "none":
                line += " [caution: not found in the paper - cite only with care]"
            lines.append(line)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def journal_tiers(db: Session,
                  papers: list[models.Paper]) -> dict[int, int | None]:
    """{paper_id: journal tier} for papers whose venue has a rating."""
    wanted: dict[str, list[int]] = {}
    for paper in papers:
        for raw in [paper.issn_l] + list(paper.issns or []):
            issn = journals.normalize_issn(raw)
            if issn:
                wanted.setdefault(issn, []).append(paper.id)
                break
    if not wanted:
        return {}
    rows = db.execute(
        select(models.Journal).where(models.Journal.issn.in_(wanted))
    ).scalars().all()
    result: dict[int, int | None] = {}
    for journal in rows:
        tier = journals.tier(journal)
        for paper_id in wanted.get(journal.issn, []):
            result[paper_id] = tier
    return result


def _ranked_papers(db: Session,
                   search: models.Search) -> tuple[list[models.Paper], bool]:
    """All papers of a search in ranking order (best first), plus whether
    that order is a real relevance ranking.

    Relevance dominates: scores are bucketed into bands of RELEVANCE_BAND
    points, and journal quality only orders papers within a band. Unrated
    venues (books, working papers) take a neutral tier.
    """
    tiers = journal_tiers(db, list(search.papers))

    def quality(paper: models.Paper) -> tuple:
        tier = tiers.get(paper.id)
        if tier is None:
            tier = journals.NEUTRAL_TIER
        return (tier, paper.citation_count or 0)

    candidates = [p for p in search.papers
                  if p.status != models.STATUS_EXCLUDED]
    scores = {s.paper_id: s.score for s in search.scores}
    ranked = [p for p in candidates if p.id in scores]
    if ranked:
        ranked.sort(key=lambda p: (int(scores[p.id]) // RELEVANCE_BAND,
                                   *quality(p)), reverse=True)
        return ranked, True
    return sorted(candidates, key=quality, reverse=True), False


# ============================================= starting, stopping, resuming

def start_question(db: Session, rq: models.ResearchQuestion, *,
                   mode: str = "full", parsed: dict | None = None,
                   source: str = "web", session: str | None = None,
                   first_run: bool = False) -> None:
    """Start a run of rq. Raises services.RunRefused if one is going.

    mode "full": plan the search (Claude, unless `parsed` is given - Claude
    in a chat may plan it itself), search, rank, download, read, write.
    mode "evidence": no new search - read the PDFs this question has never
    read, retry the failed readings, then rewrite the answer.
    """
    services.start_run(db, rq, source=source, session=session,
                       first_run=first_run)
    tasks.cancel(db, "The question was started again",
                 research_question_id=rq.id,
                 kinds=[models.TASK_PARSE_QUESTION, models.TASK_RANK_PAPERS,
                        models.TASK_SYNTHESIZE, models.TASK_REVISE_ANSWER,
                        models.TASK_VERIFY_ANSWER])
    project = rq.project
    rq.fetch_per_source = project.fetch_per_source
    rq.rank_top = project.rank_top
    rq.download_top = project.download_top
    rq.run_state = {"mode": mode}
    if mode == "evidence":
        _schedule(db, rq, "evidence")
    elif parsed:
        store_parse(rq, parsed)
        _schedule(db, rq, "search")
    else:
        tasks.create(db, models.TASK_PARSE_QUESTION, project_id=rq.project_id,
                     research_question_id=rq.id)
        _set_status(db, rq, models.RQ_PARSING,
                    "Waiting for Claude to plan the search", commit=False)
    tasks.commit(db)


def stop_question(db: Session, rq: models.ResearchQuestion) -> int:
    """Stop a run where it is. Returns how many waiting tasks were withdrawn.

    What was collected stays: papers, PDFs, finished annotations and any
    answer already written. A paper still waiting to be read is left unread.
    """
    withdrawn = tasks.cancel(db, "Stopped by the user",
                             research_question_id=rq.id)
    for annotation in rq.annotations:
        if annotation.status == models.ANNOT_PENDING and not annotation.findings:
            annotation.status = models.ANNOT_FAILED
            annotation.error = "The run was stopped before this paper was read."
    rq.status = models.RQ_DONE if rq.summary_html else models.RQ_FAILED
    rq.status_detail = None
    rq.error = None if rq.summary_html else "Stopped before an answer was written."
    rq.run_state = {}
    db.commit()
    return withdrawn


def store_parse(rq: models.ResearchQuestion, parsed: dict) -> None:
    """Keep the planned search on the question (from Claude's parse task, or
    passed straight in by Claude through ask_question)."""
    groups = [[t.strip() for t in g if isinstance(t, str) and t.strip()]
              for g in (parsed.get("concept_groups") or []) if isinstance(g, list)]
    groups = [g for g in groups if g]
    query = (parsed.get("query") or "").strip() or rq.text

    def year(value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if 1500 <= value <= 2100 else None

    rq.headline = ((parsed.get("headline") or "").strip()[:120]
                   or rq.headline or rq.text[:80])
    rq.parsed = {
        "keywords": [str(k) for k in (parsed.get("keywords") or [])][:20],
        "areas": [str(a) for a in (parsed.get("areas") or [])][:10],
        "query": query,
        "concept_groups": groups,
        "boolean": services.boolean_string(groups),
        "year_from": year(parsed.get("year_from")),
        "year_to": year(parsed.get("year_to")),
    }


_STEP_DETAIL = {
    "search": "Starting the search",
    "download": "Starting the downloads",
    "evidence": "Checking what is new",
}


def _schedule(db: Session, rq: models.ResearchQuestion, step: str) -> None:
    """Record the next background step and start it once committed."""
    state = dict(rq.run_state or {})
    state["step"] = step
    rq.run_state = state
    _set_status(db, rq, models.RQ_QUEUED, _STEP_DETAIL[step], commit=False)
    tasks.after_commit(db, worker.submit, _STEPS[step], rq.id)


def resume_all() -> Counter:
    """Startup: carry on with every run the app was closed in the middle of.

    The app's own steps are re-submitted from where run_state says they
    were; questions waiting for Claude are checked for the tasks they wait
    on (reconcile), which repairs a step that was cut off between writing
    its result and queueing the next task.
    """
    db = SessionLocal()
    stats: Counter = Counter()
    try:
        running = db.execute(
            select(models.ResearchQuestion)
            .where(models.ResearchQuestion.status.in_(models.RQ_RUNNING_STATUSES))
        ).scalars().all()
        for rq in running:
            if rq.status in models.RQ_APP_STATUSES:
                step = (rq.run_state or {}).get("step") or (
                    "download" if rq.status == models.RQ_DOWNLOADING else "search")
                if step not in _STEPS:
                    step = "search"
                tasks.after_commit(db, worker.submit, _STEPS[step], rq.id)
                stats["resumed"] += 1
            elif reconcile(db, rq):
                stats["repaired"] += 1
        tasks.commit(db)
    except Exception:
        logger.exception("Resuming interrupted runs failed")
    finally:
        db.close()
    if stats:
        logger.info("Runs picked up at startup: %s", dict(stats))
    return stats


def reconcile(db: Session, rq: models.ResearchQuestion) -> bool:
    """Make sure a question waiting for Claude has the task it waits for.
    Returns True if something had to be repaired. Caller commits."""
    live = tasks.live_tasks(db, research_question_id=rq.id)
    kinds = {t.kind for t in live}
    if rq.status == models.RQ_PARSING and models.TASK_PARSE_QUESTION not in kinds:
        tasks.create(db, models.TASK_PARSE_QUESTION, project_id=rq.project_id,
                     research_question_id=rq.id)
        return True
    if rq.status == models.RQ_RANKING and models.TASK_RANK_PAPERS not in kinds:
        search = _run_search_row(db, rq)
        if search is None:
            _schedule(db, rq, "search")
        else:
            _queue_ranking(db, rq, search)
        return True
    if rq.status == models.RQ_ANNOTATING:
        repaired = False
        for annotation in rq.annotations:
            if (annotation.status == models.ANNOT_PENDING
                    and not tasks.live_tasks(db, annotation_id=annotation.id)
                    and annotation.paper is not None):
                _queue_reading(db, rq, annotation.paper, force=True)
                repaired = True
        if not tasks.live_tasks(db, research_question_id=rq.id,
                                kinds=[models.TASK_ANNOTATE_PAPER]):
            _after_reading(db, rq)
            repaired = True
        return repaired
    if rq.status == models.RQ_SYNTHESIZING and models.TASK_SYNTHESIZE not in kinds:
        _queue_synthesis(db, rq)
        return True
    return False


def _run_search_row(db: Session, rq: models.ResearchQuestion) -> models.Search | None:
    search_id = (rq.run_state or {}).get("search_id")
    return db.get(models.Search, search_id) if search_id else None


# ================================================================ search

def _step_search(rq_id: int) -> None:
    """Background: query the sources with the planned search, then queue the
    ranking. Re-entrant: a search already recorded is not run again."""
    db = SessionLocal()
    try:
        rq = db.get(models.ResearchQuestion, rq_id)
        if rq is None or rq.status not in (models.RQ_QUEUED, models.RQ_SEARCHING):
            return
        project = rq.project
        search = _run_search_row(db, rq)
        if search is None:
            names = ", ".join(SOURCE_LABELS.get(s, s) for s in project.source_list())
            _set_status(db, rq, models.RQ_SEARCHING, f"Searching {names}")
            y = rq.rank_top or project.rank_top
            search = services.run_search(
                db, project, rq.text, project.source_list(),
                max_results=rq.fetch_per_source or project.fetch_per_source,
                research_question_id=rq.id, parsed=dict(rq.parsed or {}),
                rank_top=y)
            candidates = search.rank_candidates
            rq = _still(db, rq_id, models.RQ_SEARCHING)
            _set_status(db, rq, models.RQ_SEARCHING,
                        f"Found {search.result_count} results "
                        f"({search.new_count} new) - looking up journals")
            services.enrich_journals(db, candidates)
            state = dict(rq.run_state or {})
            state.update(search_id=search.id,
                         rank_ids=[p.id for p in candidates])
            rq.run_state = state
            failures = getattr(search, "source_errors", []) or []
            if failures and len(failures) >= len(project.source_list()) \
                    and not search.result_count:
                rq.error = ("LitReview couldn't reach the internet - "
                            + ", ".join(SOURCE_LABELS.get(n, n) for n, _ in failures)
                            + " did not answer. Check your connection (or "
                            "proxy), then press Search again.")
                rq.run_state = {}
                _set_status(db, rq, models.RQ_FAILED, None)
                return
            for name, exc in failures:
                _note(rq, source_trouble(name, exc))
            db.commit()
        rq = _still(db, rq_id, models.RQ_SEARCHING, models.RQ_QUEUED)
        _queue_ranking(db, rq, search)
        tasks.commit(db)
    except _Stopped:
        db.rollback()
        logger.info("Question %s was stopped during its search", rq_id)
    except Exception as exc:
        logger.exception("Search for question %s failed", rq_id)
        _record_failure(db, rq_id, exc)
    finally:
        db.close()


def _queue_ranking(db: Session, rq: models.ResearchQuestion,
                   search: models.Search) -> None:
    """Put the unscored candidates in front of Claude, a batch per task -
    or, with nothing to rank, go straight to the downloads. Caller commits."""
    ids = (rq.run_state or {}).get("rank_ids") or []
    scored = {s.paper_id for s in search.scores}
    excluded = {p.id for p in search.papers if p.status == models.STATUS_EXCLUDED}
    todo = [pid for pid in ids if pid not in scored and pid not in excluded]
    already = {pid for t in tasks.live_tasks(db, research_question_id=rq.id,
                                             kinds=[models.TASK_RANK_PAPERS])
               for pid in (t.payload or {}).get("paper_ids", [])}
    todo = [pid for pid in todo if pid not in already]
    if not todo and not already:
        if ids:
            search.ranked = 1
        _schedule(db, rq, "download")
        return
    batches = [todo[i:i + RANK_BATCH_SIZE]
               for i in range(0, len(todo), RANK_BATCH_SIZE)]
    for n, batch in enumerate(batches, start=1):
        tasks.create(db, models.TASK_RANK_PAPERS, project_id=rq.project_id,
                     research_question_id=rq.id,
                     payload={"search_id": search.id, "paper_ids": batch,
                              "batch": n, "batches": len(batches)})
    waiting = len(todo) + len(already)
    detail = f"Waiting for Claude to rank {waiting} results"
    notes = (rq.run_state or {}).get("notes") or []
    if notes:
        detail += " (" + "; ".join(notes) + ")"
    _set_status(db, rq, models.RQ_RANKING, detail, commit=False)


# -------------------------------------------------------------- rank task

def _rank_papers_of(db: Session, task: models.ClaudeTask) -> list[models.Paper]:
    ids = (task.payload or {}).get("paper_ids") or []
    papers = {p.id: p for p in db.execute(
        select(models.Paper).where(models.Paper.id.in_(ids))).scalars().all()}
    return [papers[i] for i in ids if i in papers]


def _rank_input(db: Session, task: models.ClaudeTask) -> dict:
    rq = task.research_question
    payload = task.payload or {}
    return {
        "question": rq.text if rq else "",
        "batch": f"{payload.get('batch', 1)} of {payload.get('batches', 1)}",
        "papers": [{"id": p.id, "title": p.title, "year": p.year,
                    "venue": p.venue,
                    "abstract": (p.abstract or "(no abstract)")[:ABSTRACT_CHARS]}
                   for p in _rank_papers_of(db, task)],
    }


def _rank_check(db: Session, task: models.ClaudeTask, output: dict) -> list[str]:
    wanted = {p.id for p in _rank_papers_of(db, task)}
    given = [int(r["id"]) for r in output["rankings"]]
    errors = []
    stray = sorted(set(given) - wanted)
    if stray:
        errors.append(f"ids {stray} are not in this batch")
    missing = sorted(wanted - set(given))
    if missing:
        errors.append(f"papers {missing} have no score - score every paper in "
                      "the batch")
    return errors


def _rank_apply(db: Session, task: models.ClaudeTask, output: dict) -> dict:
    search = db.get(models.Search, (task.payload or {}).get("search_id"))
    if search is None:
        return {"stored": 0, "next": "The search this batch belonged to is gone."}
    scores = {int(r["id"]): (float(r["score"]), (r.get("reason") or "").strip())
              for r in output["rankings"]}
    stored = services.store_scores(db, search, scores)
    return {"stored": stored, **_after_ranking(db, task, search)}


def _after_ranking(db: Session, task: models.ClaudeTask,
                   search: models.Search) -> dict:
    """The last batch of a run's ranking moves the run on to the downloads."""
    left = [t for t in tasks.live_tasks(db, kinds=[models.TASK_RANK_PAPERS])
            if (t.payload or {}).get("search_id") == search.id]
    if left:
        return {"next": f"{len(left)} more ranking batch"
                        f"{'' if len(left) == 1 else 'es'} for this question"}
    search.ranked = 1
    rq = task.research_question
    if rq is not None and rq.status == models.RQ_RANKING \
            and (rq.run_state or {}).get("search_id") == search.id:
        _schedule(db, rq, "download")
        return {"next": "Ranking done. The app is downloading the top PDFs "
                        "now; reading tasks follow in a minute or so."}
    return {"next": "Scores saved."}


def _rank_dropped(db: Session, task: models.ClaudeTask) -> None:
    search = db.get(models.Search, (task.payload or {}).get("search_id"))
    if search is not None:
        _after_ranking(db, task, search)


def _rank_describe(task: models.ClaudeTask) -> str:
    payload = task.payload or {}
    label = task.research_question.label if task.research_question else "?"
    return (f"{label}: rank {len(payload.get('paper_ids') or [])} search results "
            f"(batch {payload.get('batch', 1)} of {payload.get('batches', 1)})")


# ============================================================== downloads

def _step_download(rq_id: int) -> None:
    """Background: fetch the top Z PDFs (walking further down the ranking for
    any that fail), keep their text, and queue a reading of each."""
    db = SessionLocal()
    try:
        rq = db.get(models.ResearchQuestion, rq_id)
        if rq is None or rq.status not in (models.RQ_QUEUED, models.RQ_DOWNLOADING):
            return
        project = rq.project
        search = _run_search_row(db, rq)
        if search is None:
            raise RuntimeError("the run lost track of its search - run the "
                               "question again")
        z = rq.download_top or project.download_top
        ranked, scored = _ranked_papers(db, search)
        score_of = {s.paper_id: s.score for s in search.scores}
        if scored:
            ranked = [p for p in ranked
                      if score_of.get(p.id, 0) >= MIN_READ_SCORE]
        top = ranked[:z]
        _set_status(db, rq, models.RQ_DOWNLOADING,
                    f"Downloading PDFs for the top {len(top)} papers")

        selected: list[models.Paper] = []

        def select_paper(paper: models.Paper) -> None:
            if paper.stage == models.STAGE_SCREENING:
                paper.stage = models.STAGE_RETRIEVAL
            if paper.status == models.STATUS_UNSCREENED:
                paper.status = models.STATUS_INCLUDED
            selected.append(paper)

        downloaded = 0
        backfill_attempts = 0
        for i, paper in enumerate(ranked):
            if downloaded >= z:
                break
            if i < z:  # the top of the ranking is always attempted
                select_paper(paper)
                if paper.pdf_status == models.PDF_DOWNLOADED:
                    downloaded += 1
                    db.commit()
                    continue
                rq = _still(db, rq_id, models.RQ_DOWNLOADING)
                _set_status(db, rq, models.RQ_DOWNLOADING,
                            f"Downloading PDF {i + 1} of {len(top)}")
                ok, error = pdfs.download_pdf(paper)
                paper.pdf_status = models.PDF_DOWNLOADED if ok else models.PDF_FAILED
                paper.pdf_error = error
                if ok:
                    downloaded += 1
                db.commit()
                continue
            # Below the top Z: only while the order is a real ranking, only
            # papers whose PDF is in hand or that carry a direct OA link.
            if not scored or score_of.get(paper.id, 0) < BACKFILL_MIN_SCORE:
                break
            if paper.pdf_status == models.PDF_DOWNLOADED:
                select_paper(paper)
                downloaded += 1
                db.commit()
                continue
            if not paper.oa_url or backfill_attempts >= z:
                continue
            backfill_attempts += 1
            rq = _still(db, rq_id, models.RQ_DOWNLOADING)
            _set_status(db, rq, models.RQ_DOWNLOADING,
                        "Some top-ranked PDFs are unavailable - trying paper "
                        f"{i + 1} of {len(ranked)}")
            ok, error = pdfs.download_pdf(paper)
            if ok:
                paper.pdf_status = models.PDF_DOWNLOADED
                paper.pdf_error = None
                select_paper(paper)
                downloaded += 1
            db.commit()

        readable = [p for p in selected if p.pdf_status == models.PDF_DOWNLOADED]
        # The papers about to be read are the ones the answer will cite, so
        # their references are completed from Crossref now: volume, issue,
        # pages and the issue's year rather than the online-first one.
        _set_status(db, rq, models.RQ_DOWNLOADING,
                    "Completing the references from Crossref")
        services.polish_from_crossref(db, selected)
        for paper in readable:
            paper_texts.ensure(paper.id)
        db.expire_all()
        rq = _still(db, rq_id, models.RQ_DOWNLOADING)
        for paper in readable:
            paper = db.get(models.Paper, paper.id)
            if paper is not None:
                _queue_reading(db, rq, paper)

        failed = [p for p in top if p.pdf_status == models.PDF_FAILED]
        backfilled = sum(1 for p in readable if p not in top)
        if failed:
            names = "; ".join(f"\"{p.title[:70]}\"" for p in failed[:5])
            more = f" and {len(failed) - 5} more" if len(failed) > 5 else ""
            why = ("" if settings.contact_email else
                   " (no contact email is set, so Unpaywall could not be asked "
                   "for free copies - add one in Settings)")
            _note(rq, f"{len(failed)} of the top {len(top)} PDFs could not be "
                      f"downloaded{why}. Upload them on the question page: "
                      f"{names}{more}")
        if backfilled:
            _note(rq, f"read {backfilled} lower-ranked paper"
                      f"{'' if backfilled == 1 else 's'} in their place")
        _set_status(db, rq, models.RQ_ANNOTATING, None, commit=False)
        _after_reading(db, rq)
        tasks.commit(db)
    except _Stopped:
        db.rollback()
        logger.info("Question %s was stopped during its downloads", rq_id)
    except Exception as exc:
        logger.exception("Downloads for question %s failed", rq_id)
        _record_failure(db, rq_id, exc)
    finally:
        db.close()


def _step_evidence(rq_id: int) -> None:
    """Background: no new search - read what this question has never read,
    retry the readings that failed, then rewrite the answer."""
    db = SessionLocal()
    try:
        rq = db.get(models.ResearchQuestion, rq_id)
        if rq is None or rq.status != models.RQ_QUEUED:
            return
        todo = outstanding_papers(db, rq)
        for paper in todo:
            paper_texts.ensure(paper.id)
        retry = [a.paper for a in rq.annotations
                 if a.status in (models.ANNOT_FAILED, models.ANNOT_NEEDS_OCR)
                 and a.paper is not None
                 and a.paper.pdf_status == models.PDF_DOWNLOADED]
        db.expire_all()
        rq = db.get(models.ResearchQuestion, rq_id)
        queued = 0
        for paper in todo + retry:
            paper = db.get(models.Paper, paper.id)
            if paper is not None and _queue_reading(db, rq, paper) in ("queued", "parse"):
                queued += 1
        if queued:
            _note(rq, f"read {queued} paper{'' if queued == 1 else 's'} it had "
                      "not read before")
        _set_status(db, rq, models.RQ_ANNOTATING, None, commit=False)
        _after_reading(db, rq)
        tasks.commit(db)
    except Exception as exc:
        logger.exception("Rewriting question %s from its evidence failed", rq_id)
        _record_failure(db, rq_id, exc)
    finally:
        db.close()


def source_trouble(name: str, exc: Exception) -> str:
    """One source's failure, the way the user should read it."""
    from .pipeline import SOURCE_LABELS

    label = SOURCE_LABELS.get(name, name)
    said = str(exc).lower()
    if "rate" in said or "429" in said:
        return (f"{label} was busy (rate-limited) and returned nothing this "
                "time; the other sources were searched")
    if any(sign in said for sign in ("10061", "connect", "getaddrinfo",
                                     "name resolution", "timed out", "timeout",
                                     "proxy", "network", "ssl")):
        return f"{label} could not be reached; the other sources were searched"
    return f"{label} did not answer properly; the other sources were searched"


_STEPS = {
    "search": _step_search,
    "download": _step_download,
    "evidence": _step_evidence,
}


# ================================================================ reading

def _needs_annotating(annotation: models.Annotation | None) -> bool:
    if annotation is None:
        return True
    return annotation.status in (models.ANNOT_FAILED, models.ANNOT_NEEDS_OCR,
                                 models.ANNOT_PENDING)


_HEADING_LINE = re.compile(r"^#{1,6}\s+\S")
# A line that names a reference list without being a markdown heading -
# "References", "**REFERENCES**", "6. Bibliography".
_REF_TITLE = re.compile(
    r"^\s*(?:\*\*)?\s*(?:\d+\.?\s*)?(?:references|bibliography|works cited|"
    r"literature cited|reference list)\s*(?:\*\*)?\s*:?\s*$", re.I)
# What makes an entry a reference. On its own: a DOI, or volume(issue),
# pages. Otherwise both a year and author initials - a numbered item in the
# body ("2. Treat marginal non-normality as necessary...") has neither.
_STRONG_REFERENCE = re.compile(
    r"doi\.org/|\bdoi:\s*10\.|\b10\.\d{4,9}/"
    r"|\b\d+\s*\(\d+\)\s*[,:]\s*\d+|\b\d+\s*,\s*\d+\s*[-–]\s*\d+\b"
    r"|\bpp\.\s*\d|\bVol\.\s*\d", re.I)
_YEAR = re.compile(r"\((?:19|20)\d{2}[a-z]?\)|\b(?:19|20)\d{2}[a-z]?[.,)]")
_INITIALS = re.compile(
    r"\b[A-Z][A-Za-z'’\-]+[,.]\s+(?:[A-Z]\.\s?)+"      # Amsler, C. / Battese. G.E.
    r"|\b(?:[A-Z]\.\s?){1,3}[A-Z][a-z]+"                     # A. Prokhorov / T.J. Coelli
    r"|\b[A-Z][A-Za-z'’\-]+\s+[A-Z]{1,3}[,.]")           # Park S, (Vancouver)


def _is_reference(entry: str) -> bool:
    if len(entry) > 1200:
        return False
    return bool(_STRONG_REFERENCE.search(entry)
                or (_YEAR.search(entry) and _INITIALS.search(entry)))


def _plain_heading(line: str) -> str:
    return re.sub(r"^#{1,6}\s+", "", line).strip()


# A heading does not stop on a comma or a lowercase function word - only a
# wrapped fragment does. Case-sensitive: "Appendix A" is a heading.
_FRAGMENT_END = re.compile(r"(?:[,;:]|\b(?:and|or|of|the|in|on|for|to|with))\s*$")


def _looks_like_heading(text: str) -> bool:
    """Could this be a real heading? Older builds marked wrapped fragments
    as headings ("ing role of design, retailer-strategy, and",
    "Homburg et al. (2020)"); a heading starts with a capital or a number,
    does not stop mid-phrase, and is no citation."""
    text = text.strip()
    if not text or len(text) > 120 or not (text[0].isupper() or text[0].isdigit()):
        return False
    if _FRAGMENT_END.search(text) or re.search(r"\bet al\b", text):
        return False
    return not re.search(r"\((?:19|20)\d{2}[a-z]?\)", text)


def _is_boundary(line: str) -> bool:
    """Where a section starts: a heading that looks like one - not a
    reference entry, and not a wrapped fragment an older build marked as a
    heading - or a line naming a reference list."""
    if _REF_TITLE.match(line):
        return True
    if not _HEADING_LINE.match(line):
        return False
    text = _plain_heading(line)
    return _looks_like_heading(text) and not _is_reference(text)


def _breaks_entry(line: str) -> bool:
    """A line no reference entry continues into: a real heading, a table
    row, a bold caption ("**Table 1: ...**"), a note under a table."""
    stripped = line.strip()
    if _HEADING_LINE.match(stripped):
        return not _is_reference(_plain_heading(stripped))
    return (stripped.startswith(("|", "**", "\\*", "[Figure", "[Table"))
            or bool(re.match(r"^(?:Table|Figure|Fig\.)\s+\d", stripped)))


def _reference_lines(numbered: list[tuple[int, str]]) -> set[int]:
    """The indices of the lines that belong to reference entries, grouping
    wrapped lines into entries as _entries does."""
    groups: list[list[int]] = []
    current: list[int] = []
    for i, line in numbered:
        if _is_boundary(line) or _REF_TITLE.match(line) or _breaks_entry(line):
            if current:
                groups.append(current)
                current = []
            continue
        if not line.strip():
            if current:
                groups.append(current)
                current = []
            continue
        if current and _ENTRY_START.match(line):
            groups.append(current)
            current = []
        current.append(i)
    if current:
        groups.append(current)
    text = dict(numbered)
    out: set[int] = set()
    for group in groups:
        entry = " ".join(_plain_heading(text[i]) for i in group)
        if _is_reference(entry):
            out.update(group)
    return out


# Where a reference entry begins: "[12]", "12. Name", "Surname, X.",
# "Surname. X.", "Surname, Given," or the Vancouver "Surname XY,".
_ENTRY_START = re.compile(
    r"^\s*(?:#{1,6}\s+)?(?:\[\d{1,3}\]|\d{1,3}\.\s+[A-Z]"
    r"|[A-Z][A-Za-z'’\-]+(?:\s+(?:van|de|der|von|da|di|la|le|du|del))*"
    r"(?:[,.]\s+(?:[A-Z]\.|[A-Z][a-z]+[,.])|\s+[A-Z]{1,3}[,.]))")


def _entries(lines: list[str]) -> list[str]:
    """A stretch of text as reference entries: in a PDF's text layer each
    entry wraps over two to four lines, so a line that does not begin one is
    the rest of the one before; a blank line ends an entry too."""
    entries: list[str] = []
    current: list[str] = []
    for line in lines:
        if _is_boundary(line) or _REF_TITLE.match(line) or _breaks_entry(line):
            if current:
                entries.append(" ".join(current))
                current = []
            continue
        if not line.strip():
            if current:
                entries.append(" ".join(current))
                current = []
            continue
        if current and _ENTRY_START.match(line):
            entries.append(" ".join(current))
            current = []
        current.append(_plain_heading(line))
    if current:
        entries.append(" ".join(current))
    return entries


def _mostly_references(lines: list[str]) -> bool:
    """Is a stretch of text a reference list? Most of its entries are
    references by the evidence above, and there are enough of them to be a
    list. Judged by entry, not by line: a reference wrapped over three lines
    is one entry, and a numbered recommendation is not a reference."""
    entries = _entries(lines)
    if len(entries) < 6:
        return False
    hits = sum(1 for entry in entries if _is_reference(entry))
    return hits >= 0.6 * len(entries)


def reading_pages(content: str) -> dict[int, str]:
    """A paper's pages as Claude reads them, with reference lists left out.

    A reference list is a third of many papers and none of their evidence.
    The text past its halfway point is split at its headings, and each
    section that is mostly reference entries - a "References" section, a
    "Publications listed in Table A1" - is cut; everything else is read, so
    a manual whose every function ends with a short "References" keeps its
    documentation. A paper without headings there keeps its text."""
    pages = paper_texts.split(content)
    if not pages:
        return pages
    order = sorted(pages)
    lines: list[tuple[int, str]] = [(n, line) for n in order
                                    for line in pages[n].split("\n")]
    total = sum(len(line) for _, line in lines) or 1
    # Section boundaries: every heading line past the halfway mark.
    seen = 0
    bounds = []
    for i, (_, line) in enumerate(lines):
        if seen > total * 0.5 and _is_boundary(line):
            bounds.append(i)
        seen += len(line)
    if not bounds:
        return pages
    drop: set[int] = set()
    ends = bounds[1:] + [len(lines)]
    for begin, stop in zip(bounds, ends):
        section = [line for _, line in lines[begin:stop]]
        title = _plain_heading(lines[begin][1])
        if _REF_TITLE.match(title) or _REF_TITLE.match(lines[begin][1]):
            # A section called References: its entries go, and whatever
            # else it holds - tables a paper prints after its list - stays.
            refs = _reference_lines(list(enumerate(section, start=begin))[1:])
            if len(refs) >= 3:
                drop.add(begin)
                drop.update(refs)
        elif _mostly_references(section):
            drop.update(range(begin, stop))
    if not drop:
        return pages
    kept: dict[int, list[str]] = {}
    for i, (n, line) in enumerate(lines):
        if i not in drop:
            kept.setdefault(n, []).append(line)
    return {n: "\n".join(kept[n]).strip() for n in order
            if n in kept and "\n".join(kept[n]).strip()}


def _chunks(pages: dict[int, str]) -> list[tuple[int, int]]:
    """Page ranges of at most ANNOTATE_CHUNK_CHARS characters each."""
    ranges: list[tuple[int, int]] = []
    start = None
    size = 0
    last = None
    for number in sorted(pages):
        length = len(pages[number]) + 30
        if start is not None and size + length > ANNOTATE_CHUNK_CHARS:
            ranges.append((start, last))
            start, size = None, 0
        if start is None:
            start = number
        size += length
        last = number
    if start is not None:
        ranges.append((start, last))
    return ranges


def _still_in_question(db: Session, rq: models.ResearchQuestion,
                       paper: models.Paper) -> bool:
    return db.execute(
        select(func.count()).select_from(models.search_papers)
        .join(models.Search, models.Search.id == models.search_papers.c.search_id)
        .where(models.Search.research_question_id == rq.id,
               models.search_papers.c.paper_id == paper.id)
    ).scalar_one() > 0


def _queue_reading(db: Session, rq: models.ResearchQuestion,
                   paper: models.Paper, *, force: bool = False) -> str:
    """Queue Claude's reading of one paper for one question. Caller commits.

    Returns "kept" (already read), "waiting" (a reading is already queued),
    "parse" (no text layer: Claude parses the PDF first), "queued", or
    "skipped" (no PDF, or the paper left the question).
    """
    if paper.pdf_status != models.PDF_DOWNLOADED or not _still_in_question(db, rq, paper):
        return "skipped"
    existing = db.execute(
        select(models.Annotation)
        .where(models.Annotation.paper_id == paper.id,
               models.Annotation.research_question_id == rq.id)
    ).scalars().first()
    if existing is not None and not force and not _needs_annotating(existing):
        return "kept"
    if existing is not None and tasks.live_tasks(db, annotation_id=existing.id):
        return "waiting"
    annotation = existing or models.Annotation(
        paper_id=paper.id, research_question_id=rq.id, question=rq.text)
    annotation.question = rq.text
    if not annotation.findings:
        annotation.status = models.ANNOT_PENDING
        annotation.error = None
    db.add(annotation)
    db.flush()
    text = db.get(models.PaperText, paper.id)
    if text is None:
        if not annotation.findings:
            annotation.status = models.ANNOT_NEEDS_OCR
            annotation.error = ("This PDF has no text layer, so Claude is "
                                "asked to parse it first.")
        paper_texts.request_parse(db, paper=paper)
        return "parse"
    pages = reading_pages(text.content)
    ranges = _chunks(pages)
    reading = uuid.uuid4().hex[:10]
    for part, (first, last) in enumerate(ranges, start=1):
        tasks.create(db, models.TASK_ANNOTATE_PAPER, project_id=rq.project_id,
                     research_question_id=rq.id, paper_id=paper.id,
                     annotation_id=annotation.id,
                     payload={"pages": [first, last], "part": part,
                              "parts": len(ranges), "reading": reading,
                              "total_pages": text.page_count})
    return "queued"


def _after_reading(db: Session, rq: models.ResearchQuestion) -> None:
    """When a run's last reading is in, queue the answer. Caller commits.

    Only a run moves on by itself. Articles added to a finished question are
    read without rewriting its answer - several usually arrive together, and
    the question shows "annotations not in the answer" until the user asks
    for one rewrite over all of them.
    """
    if rq.status not in (models.RQ_ANNOTATING, models.RQ_QUEUED,
                         models.RQ_DOWNLOADING):
        return
    reading = tasks.live_tasks(db, research_question_id=rq.id,
                               kinds=[models.TASK_ANNOTATE_PAPER])
    waiting_parse = [a for a in rq.annotations
                     if a.status == models.ANNOT_NEEDS_OCR and a.paper is not None
                     and paper_texts.parse_requested(db, paper=a.paper)]
    if reading or waiting_parse:
        papers = {t.paper_id for t in reading} | {a.paper_id for a in waiting_parse}
        _set_status(db, rq, models.RQ_ANNOTATING,
                    f"Waiting for Claude to read {len(papers)} paper"
                    f"{'' if len(papers) == 1 else 's'}", commit=False)
        return
    _queue_synthesis(db, rq)


def paper_text_ready(db: Session, paper: models.Paper) -> int:
    """Claude's parse of a paper came in: queue the readings that waited for
    it, and carry on identifying it if it was an upload waiting for its text.
    Returns how many readings were queued. Caller commits."""
    from . import identify  # identify builds on this module

    identify.text_ready(db, paper)
    # The passages already read keep their quotes and highlights, but where
    # each sits - page, section, context, printed page - is taken from the
    # new text.
    text = db.get(models.PaperText, paper.id)
    if text is not None:
        for annotation in paper.annotations:
            if annotation.findings:
                annotation.findings = highlight.refresh_findings(
                    annotation.findings, text.content, text.page_labels)
    queued = 0
    for annotation in list(paper.annotations):
        if annotation.status in (models.ANNOT_NEEDS_OCR, models.ANNOT_PENDING) \
                and annotation.research_question is not None:
            if _queue_reading(db, annotation.research_question, paper,
                              force=True) == "queued":
                queued += 1
    return queued


def paper_text_failed(db: Session, paper: models.Paper) -> None:
    """Claude's parse was given up on: let the runs waiting for it go on."""
    for annotation in list(paper.annotations):
        rq = annotation.research_question
        if annotation.status == models.ANNOT_NEEDS_OCR and rq is not None:
            annotation.error = ("This PDF has no text layer and could not be "
                                "parsed. Ask Claude to parse it again from its "
                                "card.")
            _after_reading(db, rq)


# ------------------------------------------------------------ annotate task

def _annotate_text(db: Session, task: models.ClaudeTask) -> str:
    text = db.get(models.PaperText, task.paper_id) if task.paper_id else None
    if text is None:
        return ""
    first, last = (task.payload or {}).get("pages") or [1, 10 ** 6]
    pages = reading_pages(text.content)
    return "\n\n".join(f"<page {n}>\n{pages[n]}\n</page {n}>"
                       for n in sorted(pages) if first <= n <= last)


def _annotate_input(db: Session, task: models.ClaudeTask) -> dict:
    paper = task.paper
    payload = task.payload or {}
    rq = task.research_question
    first, last = payload.get("pages") or [1, 1]
    return {
        "question": rq.text if rq else "",
        "paper": {"paper_id": paper.id, "title": paper.title,
                  "authors": (paper.authors or [])[:8], "year": paper.year,
                  "venue": paper.venue} if paper else {},
        "part": (f"{payload.get('part', 1)} of {payload.get('parts', 1)}: pages "
                 f"{first}-{last} of {payload.get('total_pages', last)}"),
        "text": _annotate_text(db, task),
    }


def _annotate_check(db: Session, task: models.ClaudeTask, output: dict) -> list[str]:
    first, last = (task.payload or {}).get("pages") or [1, 10 ** 6]
    errors = []
    findings = output["findings"]
    pages_off = [i for i, f in enumerate(findings)
                 if not first - 1 <= int(f["page"]) <= last + 1]
    if pages_off:
        errors.append(f"findings {pages_off} name a page outside this part "
                      f"({first}-{last})")
    if not task.attempts:
        # A quote that is not in the text cannot be shown, highlighted or
        # trusted. Ask once for exact copies, saying where each went wrong; a
        # second answer is accepted, and whatever is still not in the text is
        # dropped rather than stored (_finish_annotation).
        paper_text = db.get(models.PaperText, task.paper_id) if task.paper_id else None
        source = paper_text.content if paper_text is not None else _annotate_text(db, task)
        astray = [(i, f) for i, f in enumerate(findings)
                  if f["quote"].strip()
                  and highlight.match_quote(source, f["quote"]) is None]
        if astray:
            lines = []
            for i, f in astray[:4]:
                label = f" (\"{(f.get('title') or '')[:40]}\")" if f.get("title") else ""
                line = (f"finding #{i + 1}{label}: "
                        f"{highlight.diagnose_miss(source, f['quote'])}")
                near = highlight.closest_passage(source, f["quote"])
                if near:
                    line += f" (closest passage, page {near[1]}: \"{near[0][:300]}\")"
                lines.append(line)
            errors.append(
                f"{len(astray)} quote(s) are not in the text as given - copy "
                "each exactly (you may shorten a quote, never reword or extend "
                "it). The whole submission is sent back this once; on the next "
                "try, quotes still not in the text are dropped and the rest "
                "kept: " + " | ".join(lines))
    empty = [i for i, f in enumerate(findings) if not f["quote"].strip()]
    if empty:
        errors.append(f"findings {empty} have an empty quote")
    return errors


def _annotate_apply(db: Session, task: models.ClaudeTask, output: dict) -> dict:
    payload = task.payload or {}
    annotation = task.annotation
    if annotation is None or task.paper is None:
        return {"saved": "the paper or its question is gone"}
    reading = payload.get("reading")
    left = [t for t in tasks.live_tasks(db, annotation_id=annotation.id)
            if (t.payload or {}).get("reading") == reading]
    n = len(output["findings"])
    if left:
        return {"saved": f"{n} passage{'' if n == 1 else 's'} from part "
                         f"{payload.get('part')} of {payload.get('parts')} of "
                         "this paper",
                "passages": [{"n": i + 1, "title": f.get("title"),
                              "page": int(f["page"]), "in_text": True}
                             for i, f in enumerate(output["findings"])],
                "note": ("Highlights are placed when the paper's last part is "
                         "in; until then each passage is only known to be in "
                         "the text."),
                "next": f"{len(left)} more part{'' if len(left) == 1 else 's'} "
                        "of this paper to read (separate tasks)"}
    outcome = _finish_annotation(db, annotation, reading, task)
    # How this part's own passages fared, by their number in the submission:
    # a reader can see which quote was not highlighted, and why.
    stored = {highlight._norm_map(f.get("quote") or "")[0]: f
              for f in (annotation.findings or [])}
    report = []
    for i, f in enumerate(output["findings"]):
        found = None
        text = db.get(models.PaperText, annotation.paper_id)
        match = highlight.match_quote(text.content, f["quote"]) if text else None
        if match:
            found = stored.get(highlight._norm_map(match["text"])[0])
        if found is None:
            report.append({"n": i + 1, "title": f.get("title"), "located": "dropped",
                           "why": "the quote is not in the paper's text"})
        else:
            entry = {"n": i + 1, "title": f.get("title"), "page": found.get("page"),
                     "located": found.get("located")}
            if found.get("located") == "text":
                entry["why"] = ("found in the text, but the PDF's text layer "
                                "could not be searched for it, so it carries "
                                "no highlight")
            report.append(entry)
    outcome["passages"] = report
    return outcome


def _finish_annotation(db: Session, annotation: models.Annotation,
                       reading: str | None, task: models.ClaudeTask | None) -> dict:
    """All parts of a reading are in: highlight the passages, store them."""
    parts = db.execute(
        select(models.ClaudeTask).where(
            models.ClaudeTask.kind == models.TASK_ANNOTATE_PAPER,
            models.ClaudeTask.annotation_id == annotation.id,
            models.ClaudeTask.status == models.TASK_DONE)
    ).scalars().all()
    findings: list[dict] = []
    for part in sorted(parts, key=lambda t: (t.payload or {}).get("part", 0)):
        if (part.payload or {}).get("reading") != reading:
            continue
        findings.extend((part.result or {}).get("findings") or [])
    paper = annotation.paper
    text = db.get(models.PaperText, paper.id)
    kept, dropped = [], 0
    for finding in findings:
        found = (highlight.match_quote(text.content, finding.get("quote") or "")
                 if text is not None else None)
        if found is None:
            dropped += 1
            continue
        kept.append(dict(finding, quote=found["text"], v=VERIFIED))
    findings = kept
    content = text.content if text is not None else None
    results = highlight.highlight_annotation(paper.project.slug, paper.id,
                                             annotation.id, findings,
                                             content=content)
    for given, result in zip(findings, results):
        result["v"] = VERIFIED
        result["hv"] = HIGHLIGHTED
        if (given.get("about") or "").strip():
            result["about"] = given["about"].strip()[:200]
    results = highlight.enrich_from_text(
        results, content, paper_texts.page_at, paper_texts.section_at,
        page_labels=text.page_labels if text is not None else None)
    annotation.findings = results
    annotation.reader = text.reader if text else None
    annotation.model = "Claude" + (f" ({task.claimed_by})"
                                   if task is not None and task.claimed_by else "")
    annotation.status = models.ANNOT_DONE
    annotation.error = None
    annotation.updated_at = models.utcnow()
    in_text = sum(1 for r in results if r.get("located") == "text")
    nowhere = sum(1 for r in results if r.get("located") == "none")
    rq = annotation.research_question
    notes = []
    if dropped:
        notes.append(f"{dropped} dropped because their quotes are not in the "
                     "paper's text")
    if in_text:
        notes.append(f"{in_text} found in the text but not highlightable in the PDF")
    if nowhere:
        notes.append(f"{nowhere} not found in the paper at all - check those quotes")
    outcome = {"saved": f"this paper: {len(results)} passage"
                        f"{'' if len(results) == 1 else 's'} kept for "
                        f"{rq.label if rq else 'the question'}"
                        + (f" ({'; '.join(notes)})" if notes else "")}
    if rq is not None:
        db.flush()
        before = rq.status
        _after_reading(db, rq)
        if before == models.RQ_ANNOTATING and rq.status == models.RQ_SYNTHESIZING:
            outcome["next"] = (f"Every paper of {rq.label} is read; writing its "
                               "answer is the next task.")
    return outcome


def _annotate_dropped(db: Session, task: models.ClaudeTask) -> None:
    annotation = task.annotation
    if annotation is None:
        return
    reading = (task.payload or {}).get("reading")
    for other in tasks.live_tasks(db, annotation_id=annotation.id):
        if (other.payload or {}).get("reading") == reading:
            other.status = models.TASK_CANCELLED
            other.error = "Another part of this paper was given up on"
    if not annotation.findings:
        annotation.status = models.ANNOT_FAILED
        annotation.error = task.error or "Claude's reading was given up on"
    if annotation.research_question is not None:
        _after_reading(db, annotation.research_question)


def _annotate_still_needed(db: Session, task: models.ClaudeTask) -> bool:
    return (task.annotation is not None and task.paper is not None
            and db.get(models.PaperText, task.paper_id) is not None)


def _annotate_describe(task: models.ClaudeTask) -> str:
    payload = task.payload or {}
    label = task.research_question.label if task.research_question else "?"
    title = (task.paper.title if task.paper else "a paper")[:70]
    part = (f" (part {payload.get('part')} of {payload.get('parts')})"
            if payload.get("parts", 1) > 1 else "")
    return f"{label}: read \"{title}\"{part}"


# ============================================================== synthesis

def _queue_synthesis(db: Session, rq: models.ResearchQuestion) -> None:
    """Queue the answer - or, with nothing to write it from, say so at once
    and finish. Caller commits."""
    annotations = question_annotations(db, rq.id)
    notes = "; ".join((rq.run_state or {}).get("notes") or []) or None
    if not annotations:
        rq.summary_html = (
            "<p>No answer yet: this question holds nothing that has been read "
            "for it - the papers it had were removed or disregarded, their "
            "PDFs never arrived, or reading them failed. Add PDFs by hand "
            "below, or add articles to the question, then run it again.</p>")
        rq.answer_written_at = models.utcnow()
        rq.run_state = {}
        _set_status(db, rq, models.RQ_DONE, notes, commit=False)
        return
    cmap = citations.citation_map(annotations)
    if not cmap:
        n = len(annotations)
        rq.summary_html = (
            f"<p>{n} paper{' was' if n == 1 else 's were'} read in full, but "
            "no passages relevant to this question were found in "
            f"{'it' if n == 1 else 'them'}.</p>")
        rq.answer_written_at = models.utcnow()
        rq.run_state = {}
        _set_status(db, rq, models.RQ_DONE, notes, commit=False)
        return
    tasks.cancel(db, "The answer is being rewritten", research_question_id=rq.id,
                 kinds=[models.TASK_VERIFY_ANSWER])
    tasks.create(db, models.TASK_SYNTHESIZE, project_id=rq.project_id,
                 research_question_id=rq.id)
    _set_status(db, rq, models.RQ_SYNTHESIZING,
                "Waiting for Claude to write the answer", commit=False)


def _evidence_for(db: Session, rq: models.ResearchQuestion) -> dict[str, dict]:
    return citations.citation_map(question_annotations(db, rq.id))


def _synth_input(db: Session, task: models.ClaudeTask) -> dict:
    rq = task.research_question
    cmap = _evidence_for(db, rq)
    papers = {info["paper_id"] for info in cmap.values()}
    return {"question": rq.text,
            "papers": len(papers),
            "passages": len(cmap),
            "evidence": evidence_digest(cmap)}


def _summary_check(db: Session, task: models.ClaudeTask, output: dict) -> list[str]:
    summary = (output.get("summary") or "").strip()
    if not summary:
        return ["summary is empty"]
    cmap = _evidence_for(db, task.research_question)
    unknown = sorted({key for key in citations.TOKEN_RE.findall(summary)
                      if citations.resolve_key(key, cmap) is None})
    errors = []
    if unknown:
        errors.append(f"these citation keys are not in the evidence list: "
                      f"{unknown} - use only keys given, exactly as written")
    if cmap and not citations.TOKEN_RE.search(summary):
        errors.append("the summary cites nothing - put a [CIT:key] marker after "
                      "every claim it supports")
    return errors


def _write_answer(db: Session, rq: models.ResearchQuestion, summary: str) -> None:
    cmap = _evidence_for(db, rq)
    summary = citations.place_citations(summary)
    text = citations.TOKEN_RE.sub(
        lambda m: f"[CIT:{citations.resolve_key(m.group(1), cmap) or m.group(1)}]",
        summary)
    rq.summary_html = citations.tokens_to_html(text, cmap)
    rq.answer_written_at = models.utcnow()


def _synth_apply(db: Session, task: models.ClaudeTask, output: dict) -> dict:
    rq = task.research_question
    _write_answer(db, rq, output["summary"])
    notes = "; ".join((rq.run_state or {}).get("notes") or []) or None
    rq.run_state = {}
    rq.error = None
    _set_status(db, rq, models.RQ_DONE, notes, commit=False)
    return {"saved": f"the answer to {rq.label}",
            "next": f"{rq.label} is done - the user can read and edit the "
                    "answer in LitReview."}


def _synth_dropped(db: Session, task: models.ClaudeTask) -> None:
    rq = task.research_question
    if rq is not None and rq.status == models.RQ_SYNTHESIZING:
        rq.error = "Writing the answer was given up on: " + (task.error or "")
        rq.run_state = {}
        _set_status(db, rq, models.RQ_DONE if rq.summary_html else models.RQ_FAILED,
                    None, commit=False)


def _synth_describe(task: models.ClaudeTask) -> str:
    rq = task.research_question
    return f"{rq.label if rq else '?'}: write the cited answer"


# --------------------------------------------------------------- revision

def request_revision(db: Session, rq: models.ResearchQuestion,
                     instruction: str) -> models.ClaudeTask:
    """Queue Claude's rewrite of the answer by the author's instruction.
    A newer instruction replaces one still waiting. Caller commits."""
    tasks.cancel(db, "Replaced by a newer instruction",
                 research_question_id=rq.id, kinds=[models.TASK_REVISE_ANSWER])
    tasks.cancel(db, "The answer is being revised", research_question_id=rq.id,
                 kinds=[models.TASK_VERIFY_ANSWER])
    return tasks.create(db, models.TASK_REVISE_ANSWER, project_id=rq.project_id,
                        research_question_id=rq.id,
                        payload={"instruction": instruction.strip()[:4000],
                                 "requested_at": models.utcnow().isoformat()},
                        priority=15)


def _revise_input(db: Session, task: models.ClaudeTask) -> dict:
    rq = task.research_question
    cmap = _evidence_for(db, rq)
    return {"question": rq.text,
            "current_draft": citations.html_to_tokens(rq.summary_html or ""),
            "instruction": (task.payload or {}).get("instruction", ""),
            "evidence": evidence_digest(cmap)}


def _revise_apply(db: Session, task: models.ClaudeTask, output: dict) -> dict:
    rq = task.research_question
    _write_answer(db, rq, output["summary"])
    return {"saved": f"the revised answer to {rq.label}"}


def _revise_describe(task: models.ClaudeTask) -> str:
    rq = task.research_question
    instruction = (task.payload or {}).get("instruction", "")
    return f"{rq.label if rq else '?'}: revise the answer - \"{instruction[:60]}\""


def save_answer_text(db: Session, rq: models.ResearchQuestion, text: str) -> list[str]:
    """Claude's own edit of an answer, through save_answer. Returns problems
    (unknown keys) instead of saving when there are any. Caller commits."""
    cmap = _evidence_for(db, rq)
    unknown = sorted({key for key in citations.TOKEN_RE.findall(text or "")
                      if citations.resolve_key(key, cmap) is None})
    if not (text or "").strip():
        return ["the text is empty"]
    if unknown:
        return [f"citation keys {unknown} are not in this question's evidence"]
    _write_answer(db, rq, text)
    return []


# ======================================================== parse question

def _parse_input(db: Session, task: models.ClaudeTask) -> dict:
    rq = task.research_question
    project = task.project
    others = [q.text for q in project.questions if q.id != rq.id][:10]
    return {"question": rq.text,
            "project": project.name,
            "project_description": project.description or "",
            "other_questions_in_this_project": others}


def _parse_apply(db: Session, task: models.ClaudeTask, output: dict) -> dict:
    rq = task.research_question
    store_parse(rq, output)
    if rq.status == models.RQ_PARSING:
        _schedule(db, rq, "search")
        return {"next": "The app is searching the sources now. Ranking tasks "
                        "for the results follow in under a minute - call "
                        "next_task with wait_seconds=45."}
    return {"next": "Saved."}


def _parse_check(db: Session, task: models.ClaudeTask, output: dict) -> list[str]:
    groups = [g for g in output.get("concept_groups") or [] if g]
    if not groups and not (output.get("query") or "").strip():
        return ["give concept_groups (2-3 groups of synonyms) and a query"]
    if len(groups) > 4:
        return ["use at most 3 concept groups - every group must appear in "
                "every result, so more groups find almost nothing"]
    return []


def _parse_still_needed(db: Session, task: models.ClaudeTask) -> bool:
    rq = task.research_question
    return rq is not None and rq.status == models.RQ_PARSING


def _parse_dropped(db: Session, task: models.ClaudeTask) -> None:
    rq = task.research_question
    if rq is not None and rq.status == models.RQ_PARSING:
        # Search with the question as typed rather than not at all.
        store_parse(rq, {"query": rq.text})
        _schedule(db, rq, "search")


def _parse_describe(task: models.ClaudeTask) -> str:
    rq = task.research_question
    return f"{rq.label if rq else '?'}: plan the literature search"


# ================================================ adding a known article

LOOKUP_PER_SOURCE = 10  # a targeted lookup, not a sweep


def find_candidates(db: Session, project: models.Project, query: str, *,
                    rq: models.ResearchQuestion | None = None,
                    limit: int = 25) -> list[dict]:
    """Look one article up in the sources and in the project.

    The same path a question's search takes - query, dedupe, merge into the
    project - but with the text as typed and nothing ranked: you are
    choosing by hand.
    """
    query = (query or "").strip()
    if not query:
        return []
    # New hits are parked in the lookup stage: they are not the project's
    # until one is picked, so a lookup cannot fill the article list.
    search = services.run_search(db, project, query, project.source_list(),
                                 max_results=LOOKUP_PER_SOURCE,
                                 new_stage=models.STAGE_LOOKUP)
    found = {p.id: p for p in search.papers}

    like = f"%{query}%"
    for paper in db.execute(
        select(models.Paper)
        .where(models.Paper.project_id == project.id,
               or_(models.Paper.title.ilike(like),
                   models.Paper.abstract.ilike(like)))
        .limit(limit)
    ).scalars().all():
        found.setdefault(paper.id, paper)

    in_question: set[int] = set()
    read: set[int] = set()
    if rq is not None:
        in_question = set(db.execute(_question_paper_ids(rq.id)).scalars().all())
        read = {a.paper_id for a in question_annotations(db, rq.id)}

    needle = re.sub(r"[^a-z0-9]+", " ", query.lower()).strip()

    def closeness(paper: models.Paper) -> tuple:
        title = re.sub(r"[^a-z0-9]+", " ", (paper.title or "").lower()).strip()
        if title == needle:
            match = 0
        elif needle and needle in title:
            match = 1
        elif needle and needle in re.sub(r"\s+", " ", (paper.abstract or "").lower()):
            match = 2
        else:
            match = 3
        return (match, paper.id in in_question, -(paper.citation_count or 0))

    # How well the title answers the text typed comes first, whether or not
    # the paper is already in the question: the article being looked for is
    # often there already (found by the search, never fetched), and sorting
    # those last used to cut exactly that one off at the limit. Exact and
    # near matches are never cut.
    ordered = sorted(found.values(), key=closeness)
    close = [p for p in ordered if closeness(p)[0] <= 1]
    shown = close + [p for p in ordered if p not in close][:max(0, limit - len(close))]
    return [
        {"paper": paper,
         "in_question": paper.id in in_question,
         "already_read": paper.id in read,
         "has_pdf": paper.pdf_status == models.PDF_DOWNLOADED,
         "disregarded": paper.status == models.STATUS_EXCLUDED,
         "title_match": ("exact", "close", "abstract", "loose")[closeness(paper)[0]]}
        for paper in shown
    ]


def question_papers(db: Session, rq: models.ResearchQuestion) -> list[dict]:
    """Every article this question holds, and what each is doing here."""
    papers = db.execute(
        select(models.Paper).where(models.Paper.id.in_(_question_paper_ids(rq.id)))
    ).scalars().all()
    if not papers:
        return []
    scores = dict(db.execute(
        select(models.PaperScore.paper_id, func.max(models.PaperScore.score))
        .join(models.Search, models.PaperScore.search_id == models.Search.id)
        .where(models.Search.research_question_id == rq.id)
        .group_by(models.PaperScore.paper_id)
    ).all())
    annotations = {a.paper_id: a for a in db.execute(
        select(models.Annotation)
        .where(models.Annotation.research_question_id == rq.id)
    ).scalars().all()}
    cmap = citations.citation_map(question_annotations(db, rq.id))
    cited: dict[int, int] = {}
    for key in citations.cited_keys(rq.summary_html or ""):
        entry = cmap.get(key)
        if entry and entry.get("paper_id"):
            cited[entry["paper_id"]] = cited.get(entry["paper_id"], 0) + 1
    rows = []
    for paper in papers:
        annotation = annotations.get(paper.id)
        rows.append({
            "paper": paper,
            "score": scores.get(paper.id),
            "annotation": annotation,
            "findings": len(annotation.findings or []) if annotation else 0,
            "cited": cited.get(paper.id, 0),
            "has_pdf": paper.pdf_status == models.PDF_DOWNLOADED,
        })
    rows.sort(key=lambda r: (-(r["score"] if r["score"] is not None else -1),
                             -(r["paper"].citation_count or 0)))
    return rows


def remove_paper_from_question(db: Session, rq: models.ResearchQuestion,
                               paper: models.Paper) -> int:
    """Take one article out of one question; the project keeps the record.
    Returns the number of annotations removed."""
    search_ids = [row[0] for row in db.execute(
        select(models.Search.id)
        .where(models.Search.research_question_id == rq.id)).all()]
    if search_ids:
        db.execute(delete(models.PaperScore).where(
            models.PaperScore.search_id.in_(search_ids),
            models.PaperScore.paper_id == paper.id))
        db.execute(delete(models.search_papers).where(
            models.search_papers.c.search_id.in_(search_ids),
            models.search_papers.c.paper_id == paper.id))
    tasks.cancel(db, "The article was taken out of the question",
                 research_question_id=rq.id, paper_id=paper.id)
    slug = paper.project.slug if paper.project else None
    removed = 0
    for annotation in db.execute(
        select(models.Annotation)
        .where(models.Annotation.research_question_id == rq.id,
               models.Annotation.paper_id == paper.id)
    ).scalars().all():
        if slug:
            try:
                pdfs.annotation_path(slug, annotation.id).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove the highlighted PDF for "
                               "annotation %s: %s", annotation.id, exc)
        db.delete(annotation)
        removed += 1
    db.commit()
    return removed


def candidate_state(db: Session, paper: models.Paper,
                    rq: models.ResearchQuestion | None = None) -> dict:
    in_question = read = False
    if rq is not None:
        in_question = _still_in_question(db, rq, paper)
        read = any(a.paper_id == paper.id for a in question_annotations(db, rq.id))
    return {"paper": paper, "in_question": in_question, "already_read": read,
            "has_pdf": paper.pdf_status == models.PDF_DOWNLOADED,
            "disregarded": paper.status == models.STATUS_EXCLUDED}


def paper_questions(db: Session, paper: models.Paper) -> list[models.ResearchQuestion]:
    """All research questions whose searches hit this paper."""
    seen: dict[int, models.ResearchQuestion] = {}
    for search in paper.searches:
        if search.research_question is not None:
            seen.setdefault(search.research_question.id, search.research_question)
    return list(seen.values())


def include_papers(db: Session, rq: models.ResearchQuestion,
                   paper_ids: list[int],
                   scores: dict[int, float] | None = None
                   ) -> tuple[list[models.Paper], dict[int, dict]]:
    """Attach chosen articles to a question, recorded as a search of their
    own. A score you give is used as given; the rest are queued for Claude to
    score against the question. Returns (papers, {paper_id: outcome})."""
    paper_ids = list(dict.fromkeys(paper_ids))
    given = dict(scores or {})
    papers = [p for p in (db.get(models.Paper, pid) for pid in paper_ids)
              if p is not None and p.project_id == rq.project_id]
    if not papers:
        return [], {}
    search = models.Search(project_id=rq.project_id, research_question_id=rq.id,
                           raw_query=None, query="(added by hand)",
                           sources="manual", result_count=len(papers),
                           new_count=0, ranked=1)
    db.add(search)
    decided: dict[int, dict] = {}
    unscored = []
    for paper in papers:
        search.papers.append(paper)
        if paper.stage == models.STAGE_LOOKUP:
            paper.stage = models.STAGE_SCREENING
        if paper.status == models.STATUS_EXCLUDED:
            paper.status = models.STATUS_INCLUDED
        if given.get(paper.id) is not None:
            score = max(0.0, min(100.0, float(given[paper.id])))
            search.scores.append(models.PaperScore(
                paper_id=paper.id, score=score, reason="Score set by hand"))
            paper.llm_score = score
            decided[paper.id] = {"score": score, "reason": "Score set by hand",
                                 "chosen_by": "you"}
        else:
            unscored.append(paper.id)
            decided[paper.id] = {"score": None, "chosen_by": "Claude",
                                 "reason": "Waiting for Claude to score it"}
    db.flush()
    if unscored:
        search.ranked = 0
        tasks.create(db, models.TASK_RANK_PAPERS, project_id=rq.project_id,
                     research_question_id=rq.id,
                     payload={"search_id": search.id, "paper_ids": unscored,
                              "batch": 1, "batches": 1, "manual": True},
                     priority=60)
    tasks.commit(db)
    return papers, decided


def include_and_read(rq_id: int, paper_ids: list[int],
                     scores: dict[int, float] | None = None) -> None:
    """Background job: attach the chosen articles, then fetch and read them."""
    db = SessionLocal()
    try:
        rq = db.get(models.ResearchQuestion, rq_id)
        if rq is None:
            return
        papers, _ = include_papers(db, rq, paper_ids, scores)
        ids = [p.id for p in papers]
    finally:
        db.close()
    if ids:
        fetch_and_annotate(rq_id, ids)


def include_across_questions(paper_id: int,
                             assignments: dict[int, float | None]) -> None:
    """Background job: file one article under several questions and read it
    for each."""
    for rq_id, score in assignments.items():
        db = SessionLocal()
        try:
            rq = db.get(models.ResearchQuestion, rq_id)
            if rq is None:
                continue
            papers, _ = include_papers(
                db, rq, [paper_id], {paper_id: score} if score is not None else None)
            attached = bool(papers)
        finally:
            db.close()
        if attached:
            fetch_and_annotate(rq_id, [paper_id])


def fetch_and_annotate(rq_id: int, paper_ids: list[int]) -> None:
    """Background job: get the PDFs for chosen articles, then queue a reading
    of each for the question. The answer is left for a rewrite."""
    db = SessionLocal()
    try:
        rq = db.get(models.ResearchQuestion, rq_id)
        if rq is None:
            return
        for paper_id in paper_ids:
            paper = db.get(models.Paper, paper_id)
            if paper is None or paper.project is None:
                continue
            if paper.pdf_status != models.PDF_DOWNLOADED:
                if paper.stage in (models.STAGE_SCREENING, models.STAGE_LOOKUP):
                    paper.stage = models.STAGE_RETRIEVAL
                if paper.status == models.STATUS_UNSCREENED:
                    paper.status = models.STATUS_INCLUDED
                ok, error = pdfs.download_pdf(paper)
                paper.pdf_status = models.PDF_DOWNLOADED if ok else models.PDF_FAILED
                paper.pdf_error = error
                db.commit()
            if paper.pdf_status == models.PDF_DOWNLOADED:
                services.polish_from_crossref(db, [paper])
                paper_texts.ensure(paper.id)
                db.expire_all()
                rq = db.get(models.ResearchQuestion, rq_id)
                paper = db.get(models.Paper, paper_id)
                _queue_reading(db, rq, paper)
                tasks.commit(db)
    finally:
        db.close()


def annotate_uploaded_paper(paper_id: int) -> None:
    """Background job after a manual PDF upload: keep the text and queue a
    reading for every question that holds the paper - and stop there. The
    answers wait for a rewrite, so a batch of uploads costs one."""
    paper_texts.ensure(paper_id)
    db = SessionLocal()
    try:
        paper = db.get(models.Paper, paper_id)
        if paper is None or paper.pdf_status != models.PDF_DOWNLOADED:
            return
        for rq in paper_questions(db, paper):
            _queue_reading(db, rq, paper, force=True)
        tasks.commit(db)
    finally:
        db.close()


# ===================================================== where does it fit?

def request_assessment(db: Session, project: models.Project,
                       paper: models.Paper) -> models.ClaudeTask | None:
    """Queue Claude's scoring of one article against every question.
    Caller commits."""
    if not project.questions:
        return None
    return tasks.create(db, models.TASK_ASSESS_ARTICLE, project_id=project.id,
                        paper_id=paper.id,
                        payload={"questions": sorted(q.id for q in project.questions)},
                        priority=25)


def assess_article(db: Session, project: models.Project,
                   paper: models.Paper) -> dict:
    """The "where does it fit?" panel: Claude's latest assessment of this
    article if there is one for the current questions, else a waiting state.

    Returns {"rows": [{rq, score, assessment, in_question, running}],
    "scored", "waiting", "best", "nothing_fits", "misfit",
    "suggested_question", "suggestion_reason", "threshold"}.
    """
    questions = db.execute(
        select(models.ResearchQuestion)
        .where(models.ResearchQuestion.project_id == project.id)
        .order_by(models.ResearchQuestion.number)
    ).scalars().all()
    current = sorted(q.id for q in questions)
    done = db.execute(
        select(models.ClaudeTask).where(
            models.ClaudeTask.kind == models.TASK_ASSESS_ARTICLE,
            models.ClaudeTask.paper_id == paper.id,
            models.ClaudeTask.status == models.TASK_DONE)
        .order_by(models.ClaudeTask.id.desc())
    ).scalars().first()
    verdict = None
    if done is not None and (done.payload or {}).get("questions") == current:
        verdict = done.result or {}
    waiting = bool(tasks.live_tasks(db, kinds=[models.TASK_ASSESS_ARTICLE],
                                    paper_id=paper.id))
    fits = {int(f["question_id"]): (max(0.0, min(100.0, float(f["score"]))),
                                    f.get("assessment") or "")
            for f in (verdict or {}).get("fits", [])}
    attached = {rq.id for rq in paper_questions(db, paper)}
    rows = []
    for rq in questions:
        score, assessment = fits.get(rq.id, (None, ""))
        rows.append({"rq": rq, "score": score, "assessment": assessment,
                     "in_question": rq.id in attached, "running": rq.running})
    scored = [row["score"] for row in rows if row["score"] is not None]
    best = max(scored) if scored else None
    return {
        "rows": rows,
        "scored": verdict is not None,
        "waiting": waiting and verdict is None,
        "best": best,
        "nothing_fits": best is not None and best < claude_prompts.FIT_THRESHOLD,
        "misfit": bool((verdict or {}).get("misfit")),
        "suggested_question": (verdict or {}).get("suggested_question", ""),
        "suggestion_reason": (verdict or {}).get("suggestion_reason", ""),
        "threshold": claude_prompts.FIT_THRESHOLD,
    }


def _assess_input(db: Session, task: models.ClaudeTask) -> dict:
    project = task.project
    paper = task.paper
    return {
        "project": project.name,
        "project_description": project.description or "",
        "research_questions": [{"question_id": rq.id, "label": rq.label,
                                "question": rq.text}
                               for rq in sorted(project.questions,
                                                key=lambda q: q.number or 0)],
        "article": {"paper_id": paper.id, "title": paper.title,
                    "year": paper.year, "venue": paper.venue,
                    "authors": (paper.authors or [])[:8],
                    "abstract": (paper.abstract or "(no abstract)")[:4000]},
    }


def _assess_check(db: Session, task: models.ClaudeTask, output: dict) -> list[str]:
    wanted = {rq.id for rq in task.project.questions}
    given = {int(f["question_id"]) for f in output["fits"]}
    errors = []
    if given - wanted:
        errors.append(f"question ids {sorted(given - wanted)} are not in this project")
    if wanted - given:
        errors.append(f"questions {sorted(wanted - given)} have no score - score "
                      "the article against every question")
    return errors


def _assess_apply(db: Session, task: models.ClaudeTask, output: dict) -> dict:
    best = max((float(f["score"]) for f in output["fits"]), default=None)
    return {"saved": "the assessment - it is on the article's panel in LitReview"
                     + (f" (best fit {best:.0f}/100)" if best is not None else "")}


def _assess_describe(task: models.ClaudeTask) -> str:
    title = (task.paper.title if task.paper else "an article")[:70]
    return f"Score \"{title}\" against the project's questions"


def _assess_still_needed(db: Session, task: models.ClaudeTask) -> bool:
    return task.paper is not None and bool(task.project.questions)


# ============================================================ registration

tasks.register(tasks.TaskKind(
    name=models.TASK_PARSE_QUESTION, title="Plan the literature search",
    instructions=claude_prompts.PARSE_QUESTION_INSTRUCTIONS,
    schema=claude_prompts.QUERY_SCHEMA, build_input=_parse_input,
    apply=_parse_apply, check=_parse_check, describe=_parse_describe,
    still_needed=_parse_still_needed, on_dropped=_parse_dropped, priority=20))

tasks.register(tasks.TaskKind(
    name=models.TASK_RANK_PAPERS, title="Rank search results by relevance",
    instructions=claude_prompts.RANK_INSTRUCTIONS,
    schema=claude_prompts.RANK_SCHEMA, build_input=_rank_input,
    apply=_rank_apply, check=_rank_check, describe=_rank_describe,
    still_needed=lambda db, t: (db.get(models.Search, (t.payload or {}).get("search_id"))
                                is not None and t.research_question is not None),
    on_dropped=_rank_dropped, priority=30))

tasks.register(tasks.TaskKind(
    name=models.TASK_ANNOTATE_PAPER, title="Read a paper for a research question",
    instructions=claude_prompts.ANNOTATE_INSTRUCTIONS,
    schema=claude_prompts.FINDINGS_SCHEMA, build_input=_annotate_input,
    apply=_annotate_apply, check=_annotate_check, describe=_annotate_describe,
    still_needed=_annotate_still_needed, on_dropped=_annotate_dropped,
    priority=40))

tasks.register(tasks.TaskKind(
    name=models.TASK_SYNTHESIZE, title="Write the cited answer",
    instructions=claude_prompts.SYNTH_INSTRUCTIONS,
    schema=claude_prompts.SUMMARY_SCHEMA, build_input=_synth_input,
    apply=_synth_apply, check=_summary_check, describe=_synth_describe,
    still_needed=lambda db, t: t.research_question is not None,
    on_dropped=_synth_dropped, priority=10))

tasks.register(tasks.TaskKind(
    name=models.TASK_REVISE_ANSWER, title="Revise the answer as the author asks",
    instructions=claude_prompts.REVISE_INSTRUCTIONS,
    schema=claude_prompts.SUMMARY_SCHEMA, build_input=_revise_input,
    apply=_revise_apply, check=_summary_check, describe=_revise_describe,
    still_needed=lambda db, t: (t.research_question is not None
                                and bool(t.research_question.summary_html)),
    priority=15))

tasks.register(tasks.TaskKind(
    name=models.TASK_ASSESS_ARTICLE, title="Score one article against every question",
    instructions=claude_prompts.FIT_INSTRUCTIONS,
    schema=claude_prompts.FIT_SCHEMA, build_input=_assess_input,
    apply=_assess_apply, check=_assess_check, describe=_assess_describe,
    still_needed=_assess_still_needed, priority=25))


# ======================================================= evidence by hand

class EvidenceRefused(Exception):
    """A passage that cannot be made citable; the message says why."""


def _attach_quietly(db: Session, rq: models.ResearchQuestion,
                    paper: models.Paper) -> None:
    """Make a paper part of a question without queueing anything for it."""
    if _still_in_question(db, rq, paper):
        return
    search = models.Search(project_id=rq.project_id, research_question_id=rq.id,
                           query="(cited as evidence)", sources="manual",
                           result_count=1, new_count=0, ranked=1)
    search.papers.append(paper)
    if paper.stage == models.STAGE_LOOKUP:
        paper.stage = models.STAGE_SCREENING
    db.add(search)


def add_evidence(db: Session, rq: models.ResearchQuestion, paper: models.Paper,
                 quote: str, why: str, title: str = "",
                 added_by: str = "Claude") -> dict:
    """Make a passage Claude found itself citable in a question's answer.

    The passage must be in the paper's stored text - that check is the only
    thing standing between an answer and a paraphrase passed off as a
    quote, so a quote that cannot be found is refused, with the closest
    sentence offered back. It is then stored as a finding of the question's
    annotation for that paper (created if the paper was never read for it),
    highlighted in the PDF where the PDF allows. Returns its citation key and
    where it was found. Caller commits.
    """
    quote = (quote or "").strip()
    if len(quote) < 15:
        raise EvidenceRefused("Give the passage itself - at least a full clause, "
                              "copied exactly from the paper's text.")
    text = db.get(models.PaperText, paper.id)
    if text is None:
        raise EvidenceRefused(
            f"Paper {paper.id} has no stored text to check the quote against "
            "- its PDF is not in hand, or has not been read yet.")
    given = quote
    found = highlight.match_quote(text.content, quote)
    if found is None:
        near = highlight.closest_passage(text.content, quote)
        hint = (f" The closest passage in the paper (page {near[1]}) is: "
                f"\"{near[0]}\" - if that is what you mean, quote it exactly."
                if near else "")
        raise EvidenceRefused(
            f"That passage does not appear in paper {paper.id}'s text, so it "
            f"cannot be cited as a quote: {highlight.diagnose_miss(text.content, quote)}."
            + hint)
    quote = found["text"]
    span = (found["start"], found["end"])
    _attach_quietly(db, rq, paper)
    annotation = db.execute(
        select(models.Annotation)
        .where(models.Annotation.paper_id == paper.id,
               models.Annotation.research_question_id == rq.id)
    ).scalars().first()
    if annotation is None:
        annotation = models.Annotation(
            paper_id=paper.id, research_question_id=rq.id, question=rq.text,
            status=models.ANNOT_DONE, findings=[], reader=text.reader,
            model=f"{added_by} (passages added by hand)")
        db.add(annotation)
        db.flush()
    existing = list(annotation.findings or [])
    wanted = highlight._norm_map(quote)[0]
    for finding in existing:
        if (not finding.get("rejected")
                and highlight._norm_map(finding.get("quote") or "")[0] == wanted):
            out = {"citation_key": citations.finding_key(annotation.id, finding),
                   "page": finding.get("page"),
                   "located": finding.get("located"),
                   "section": finding.get("section"),
                   "already_there": True}
            shown = highlight._display(finding.get("quote") or "", given)
            if " ".join(given.split()) != shown:
                out["quote_in_source"] = shown
                out["note"] = ("The paper words this passage slightly "
                               "differently - quote it as quote_in_source.")
            return out
    page = paper_texts.page_at(text.content, span[0]) or 1
    finding = {"title": (title or "").strip()[:120] or quote[:60],
               "quote": quote, "page": page, "why": (why or "").strip(),
               "added_by": added_by}
    results = highlight.highlight_annotation(paper.project.slug, paper.id,
                                             annotation.id, existing + [finding],
                                             content=text.content)
    results = highlight.enrich_from_text(results, text.content,
                                         paper_texts.page_at, paper_texts.section_at,
                                         page_labels=text.page_labels)
    for old, new in zip(existing, results):
        # Keep what the earlier passages carried beyond the geometry.
        for key in ("added_by",):
            if key in old:
                new[key] = old[key]
    results[-1]["added_by"] = added_by
    for result in results:
        result["v"] = VERIFIED
        result["hv"] = HIGHLIGHTED
    annotation.findings = results
    annotation.status = models.ANNOT_DONE
    annotation.error = None
    annotation.updated_at = models.utcnow()
    added = results[-1]
    out = {"citation_key": citations.finding_key(annotation.id, added),
           "page": added.get("page"), "printed_page": added.get("printed_page"),
           "located": added.get("located"),
           "section": added.get("section"), "already_there": False}
    if " ".join(given.split()) != " ".join((added.get("quote") or "").split()):
        out["quote_in_source"] = added.get("quote")
        out["note"] = ("Stored in the paper's own words, which differ slightly "
                       "from the quote given - quote it as quote_in_source.")
    return out


# ========================================================= verification

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])|\n\s*\n")


def answer_sentences(rq: models.ResearchQuestion) -> list[dict]:
    """The answer as numbered sentences: each with its text, the same text
    with its [CIT:key] markers in place, and the keys it carries."""
    tokens = citations.html_to_tokens(rq.summary_html or "")
    out = []
    for raw in _SENTENCE_END.split(tokens):
        sentence = re.sub(r"\s+", " ", (raw or "")).strip()
        if not sentence:
            continue
        keys = list(dict.fromkeys(citations.TOKEN_RE.findall(sentence)))
        plain = citations.TOKEN_RE.sub("", sentence)
        plain = re.sub(r"\s+([.,;:!?)])", r"\1", re.sub(r"\s+", " ", plain)).strip()
        out.append({"n": len(out) + 1, "text": plain, "marked": sentence,
                    "keys": keys})
    return out


def _sentence_id(sentence: dict) -> str:
    import hashlib

    return hashlib.sha1(sentence["text"].lower().encode("utf-8")).hexdigest()[:12]


def _answer_digest(rq: models.ResearchQuestion) -> str:
    import hashlib

    return hashlib.sha256((rq.summary_html or "").encode("utf-8")).hexdigest()[:16]


def verification_for(rq: models.ResearchQuestion) -> dict | None:
    """Claude's check of this answer, as far as the answer is still the one
    it checked: verdicts are kept per (sentence, citation), so editing two
    sentences leaves the other sentences' verdicts standing. Returns None
    when no checked sentence is left; otherwise the check with "verdicts":
    {key: weakest verdict over the current sentences citing it}, and
    "partial_check" when some cited sentences were written since."""
    check = rq.verification or {}
    pairs = check.get("pairs") or {}
    if not pairs:
        return None
    rank = {"unsupported": 0, "partial": 1, "supported": 2}
    verdicts: dict[str, dict] = {}
    unchecked = 0
    for sentence in answer_sentences(rq):
        sid = _sentence_id(sentence)
        for key in sentence["keys"]:
            found = pairs.get(f"{sid}:{key}")
            if found is None:
                unchecked += 1
                continue
            current = verdicts.get(key)
            if current is None or rank[found["verdict"]] < rank[current["verdict"]]:
                verdicts[key] = dict(found, sentence=sentence["n"])
    if not verdicts:
        return None
    out = dict(check, verdicts=verdicts)
    if unchecked:
        out["partial_check"] = (f"{unchecked} citation(s) were written after "
                                "the check and are not covered by it")
    return out


def request_verification(db: Session,
                         rq: models.ResearchQuestion) -> models.ClaudeTask | None:
    """Queue Claude's check of the answer's citations. None when there is
    nothing to check. Caller commits."""
    if not any(s["keys"] for s in answer_sentences(rq)):
        return None
    tasks.cancel(db, "Replaced by a newer check", research_question_id=rq.id,
                 kinds=[models.TASK_VERIFY_ANSWER])
    return tasks.create(db, models.TASK_VERIFY_ANSWER, project_id=rq.project_id,
                        research_question_id=rq.id,
                        payload={"digest": _answer_digest(rq)}, priority=18)


def _verify_input(db: Session, task: models.ClaudeTask) -> dict:
    rq = task.research_question
    cmap = _evidence_for(db, rq)
    sentences = [s for s in answer_sentences(rq) if s["keys"]]
    evidence = {}
    for sentence in sentences:
        for key in sentence["keys"]:
            resolved = citations.resolve_key(key, cmap)
            info = cmap.get(resolved) if resolved else None
            if info is None or key in evidence:
                continue
            evidence[key] = {"paper": f"{info['apa']} {info['paper_title']}",
                             "page": info["page"], "located": info["located"],
                             "section": info.get("section"),
                             "passage": info["quote"],
                             "context": info.get("context")}
    uncited = [{"sentence": s["n"], "text": s["text"]}
               for s in answer_sentences(rq) if not s["keys"]]
    lines = []
    for key, info in evidence.items():
        where = f"p. {info['page']}"
        if info.get("section"):
            where += f", {info['section']}"
        lines.append(f"[CIT:{key}] {info['paper']} ({where}; found: {info['located']})\n"
                     f"  passage: \"{info['passage']}\""
                     + (f"\n  context: {info['context']}" if info.get("context") else ""))
    return {"question": rq.text,
            "sentences": [{"sentence": s["n"], "text": s["marked"],
                           "keys": s["keys"]} for s in sentences],
            "uncited_sentences": uncited,
            "evidence": "\n\n".join(lines)}


def _verify_check(db: Session, task: models.ClaudeTask, output: dict) -> list[str]:
    rq = task.research_question
    wanted = {(s["n"], k) for s in answer_sentences(rq) for k in s["keys"]}
    given = {(int(c["sentence"]), c["key"]) for c in output["checks"]}
    missing = sorted(wanted - given)
    if missing:
        shown = ", ".join(f"sentence {n} / {k}" for n, k in missing[:8])
        return [f"{len(missing)} citation(s) were not checked: {shown}"]
    return []


def _verify_apply(db: Session, task: models.ClaudeTask, output: dict) -> dict:
    rq = task.research_question
    if (task.payload or {}).get("digest") != _answer_digest(rq):
        return {"saved": "nothing - the answer changed while it was being "
                         "checked; ask for a new check"}
    by_number = {s["n"]: s for s in answer_sentences(rq)}
    pairs: dict[str, dict] = {}
    for check in output["checks"]:
        sentence = by_number.get(int(check["sentence"]))
        if sentence is None:
            continue
        pairs[f"{_sentence_id(sentence)}:{check['key']}"] = {
            "verdict": check["verdict"],
            "note": (check.get("note") or "").strip()}
    rq.verification = {"digest": _answer_digest(rq),
                       "checked_at": models.utcnow().isoformat(),
                       "pairs": pairs,
                       "overall": (output.get("overall") or "").strip()}
    weak = sum(1 for v in pairs.values() if v["verdict"] != "supported")
    return {"saved": f"the check of {rq.label}'s citations: "
                     f"{len(pairs) - weak} supported, {weak} to look at"}


def _verify_describe(task: models.ClaudeTask) -> str:
    rq = task.research_question
    return f"{rq.label if rq else '?'}: check the answer's citations"


def _verify_still_needed(db: Session, task: models.ClaudeTask) -> bool:
    rq = task.research_question
    return rq is not None and (task.payload or {}).get("digest") == _answer_digest(rq)


tasks.register(tasks.TaskKind(
    name=models.TASK_VERIFY_ANSWER, title="Check an answer's citations",
    instructions=claude_prompts.VERIFY_INSTRUCTIONS,
    schema=claude_prompts.VERIFY_SCHEMA, build_input=_verify_input,
    apply=_verify_apply, check=_verify_check, describe=_verify_describe,
    still_needed=_verify_still_needed, priority=18))


def citing_answers(db: Session, paper: models.Paper) -> list[tuple[models.ResearchQuestion, int]]:
    """The answers that cite this paper, and how many times each - what
    excluding it would take out of them."""
    out = []
    for rq in paper_questions(db, paper):
        if not rq.summary_html:
            continue
        cmap = citations.citation_map(question_annotations(db, rq.id))
        cited = sum(1 for key in citations.cited_keys(rq.summary_html)
                    if (cmap.get(citations.resolve_key(key, cmap) or "") or {})
                    .get("paper_id") == paper.id)
        if cited:
            out.append((rq, cited))
    return out
