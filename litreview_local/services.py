"""Service layer: run searches, persist everything, keep project stats.

No model is called here. A search's query comes from the parse step - Claude's
answer to a parse_question task - or, for a hand lookup, from the text typed;
the ranking that follows is queued for Claude by research.py.
"""
import logging
import shutil
import threading
import time
from pathlib import Path

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import journals, locks, models
from .config import settings
from .pipeline import SOURCES, crossref
from .pipeline.dedupe import dedupe
from .pipeline.http import make_client
from .pipeline.normalize import (TYPE_ARTICLE, TYPE_CHAPTER, TYPE_PREPRINT,
                                 TYPE_REVIEW)
from .pipeline.normalize import boolean_string, title_key  # noqa: F401 (re-exported)

logger = logging.getLogger(__name__)


def run_search(db: Session, project: models.Project, raw_query: str,
               sources: list[str], *, max_results: int = 100,
               year_from: int | None = None, year_to: int | None = None,
               research_question_id: int | None = None,
               parsed: dict | None = None, rank_top: int = 0,
               new_stage: str = models.STAGE_SCREENING) -> models.Search:
    """Stage 1: fetch from the sources, dedupe, merge into the project.

    parsed is the parse step's answer ({"query", "concept_groups",
    "year_from", "year_to", ...}); without it the raw text is searched as
    typed. Sources that understand boolean queries get the concept groups
    compiled to their own syntax, the rest the flat query.

    The returned search carries `rank_candidates`: the papers to put in front
    of Claude for ranking - the top rank_top by raw source relevance (each API
    returns results relevance-ordered; a merged paper's raw rank is its best
    position in any source), minus the ones the user disregarded. 0 means all.
    """
    raw_query = raw_query.strip()
    parsed = parsed or {"query": raw_query}
    query = (parsed.get("query") or raw_query).strip() or raw_query
    year_from = year_from if year_from is not None else parsed.get("year_from")
    year_to = year_to if year_to is not None else parsed.get("year_to")

    groups = parsed.get("concept_groups") or []
    raw: list[dict] = []
    errors: list[str] = []
    failures: list[tuple[str, Exception]] = []
    for name in sources:
        module = SOURCES.get(name)
        if module is None:
            continue
        source_query = query
        compile_boolean = getattr(module, "boolean_query", None)
        if groups and compile_boolean is not None:
            source_query = compile_boolean(groups) or query
        try:
            results = module.search(source_query, max_results=max_results,
                                    year_from=year_from, year_to=year_to)
            for pos, rec in enumerate(results):
                rec["raw_rank"] = pos
            raw.extend(results)
        except Exception as exc:  # keep going if one source is down
            logger.warning("source %s failed: %s", name, exc)
            errors.append(f"{name}: {exc}")
            failures.append((name, exc))

    merged = dedupe(raw)

    search = models.Search(
        project_id=project.id,
        research_question_id=research_question_id,
        raw_query=raw_query if raw_query != query else None,
        query=query,
        sources=",".join(sources) + (" (errors: " + "; ".join(errors) + ")" if errors else ""),
        year_from=year_from,
        year_to=year_to,
        result_count=len(raw),
    )
    db.add(search)

    # The merge is a check-then-insert over this project's papers; two runs
    # over one project routinely return the same papers, so it is serialized.
    with locks.for_key(f"project-upsert:{project.id}"):
        new_count, raw_order = _merge_papers(db, project, search, merged,
                                             new_stage)
        search.new_count = new_count
        db.commit()

    candidates = [p for p in search.papers if p.status != models.STATUS_EXCLUDED]
    terms = _search_terms(parsed, raw_query)
    # Which hits Claude ranks is decided before Claude sees any of them, so
    # the cut must not fall by source order alone: a source's tenth hit can be
    # the paper that matters. Hits mentioning more of the question's concepts
    # come first, the sources' own order breaking ties.
    candidates.sort(key=lambda p: (-_term_overlap(p, terms),
                                   raw_order.get(id(p), 10 ** 9)))
    if rank_top > 0:
        candidates = candidates[:rank_top]
    search.rank_candidates = candidates
    search.source_errors = failures
    return search


def _search_terms(parsed: dict, raw_query: str) -> list[list[str]]:
    """The question's concepts as groups of lower-case phrasings - the
    concept groups when there are any, else the query's words."""
    groups = [[t.lower().strip('"') for t in group if t.strip()]
              for group in (parsed.get("concept_groups") or []) if group]
    if groups:
        return groups
    words = [w for w in (parsed.get("query") or raw_query or "").lower()
             .replace('"', " ").split() if len(w) > 3]
    return [[w] for w in words]


def _term_overlap(paper: models.Paper, groups: list[list[str]]) -> int:
    """How many of the question's concepts a hit's title and abstract name
    - the title counting twice, since it says what the paper is about."""
    title = (paper.title or "").lower()
    abstract = (paper.abstract or "").lower()
    score = 0
    for group in groups:
        if any(term in title for term in group):
            score += 2
        elif any(term in abstract for term in group):
            score += 1
    return score


def _merge_papers(db: Session, project: models.Project, search: models.Search,
                  merged: list[dict],
                  new_stage: str = models.STAGE_SCREENING) -> tuple[int, dict[int, int]]:
    """Upsert the deduplicated records into the project, attaching each to the
    search. Returns (new papers, id(paper) -> best raw rank)."""
    new_count = 0
    raw_order: dict[int, int] = {}
    for rec in merged:
        paper = _find_existing(db, project.id, rec)
        if paper is None:
            paper = models.Paper(
                project_id=project.id,
                doi=rec.get("doi"),
                title=rec.get("title") or "(untitled)",
                abstract=rec.get("abstract"),
                year=rec.get("year"),
                online_year=rec.get("online_year"),
                venue=rec.get("venue"),
                volume=rec.get("volume"),
                issue=rec.get("issue"),
                pages=rec.get("pages"),
                issn_l=rec.get("issn_l"),
                issns=rec.get("issns") or [],
                work_type=rec.get("work_type"),
                authors=rec.get("authors") or [],
                citation_count=rec.get("citation_count"),
                url=rec.get("url"),
                oa_url=rec.get("oa_url"),
                # A year that came from Crossref has been held against its
                # print date already (see _take_reference).
                source_ids={**(rec.get("source_ids") or {}),
                            **({"crossref_year": True}
                               if "crossref" in (rec.get("source_ids") or {})
                               and rec.get("year") else {})},
                dedupe_key=title_key(rec.get("title") or "", rec.get("year")),
                stage=new_stage,
            )
            db.add(paper)
            new_count += 1
        else:
            if paper.stage == models.STAGE_LOOKUP and new_stage != models.STAGE_LOOKUP:
                # A question's search found what a lookup had parked.
                paper.stage = new_stage
            # Before the source ids are merged: whether Crossref has spoken
            # for this paper yet decides whether its year may be replaced.
            _take_reference(paper, rec)
            if not paper.abstract and rec.get("abstract"):
                paper.abstract = rec["abstract"]
            if not paper.oa_url and rec.get("oa_url"):
                paper.oa_url = rec["oa_url"]
            if not paper.doi and rec.get("doi"):
                paper.doi = rec["doi"]
            if not paper.issn_l and rec.get("issn_l"):
                paper.issn_l = rec["issn_l"]
            if not paper.work_type and rec.get("work_type"):
                paper.work_type = rec["work_type"]
            if rec.get("issns"):
                merged_issns = list(paper.issns or [])
                for issn in rec["issns"]:
                    if issn not in merged_issns:
                        merged_issns.append(issn)
                paper.issns = merged_issns
            merged_ids = dict(paper.source_ids or {})
            for k, v in (rec.get("source_ids") or {}).items():
                merged_ids.setdefault(k, v)
            paper.source_ids = merged_ids
        if paper not in search.papers:
            search.papers.append(paper)
        rank = rec.get("raw_rank")
        if rank is not None:
            raw_order[id(paper)] = min(rank, raw_order.get(id(paper), rank))
    return new_count, raw_order


def _record_sources(rec: dict) -> set[str]:
    """The sources a canonical record speaks for: a merged record lists them
    in source_ids, a record straight from one source names it in source."""
    sources = set(rec.get("source_ids") or {})
    if rec.get("source"):
        sources.add(rec["source"])
    return sources


def _take_reference(paper: models.Paper, rec: dict) -> bool:
    """Bring what a record knows about where a paper appeared into the paper.
    Returns whether anything changed.

    Journal, volume, issue and pages fill gaps and never overwrite. The year
    is replaced by Crossref's - the version of record's, the issue's year -
    unless Crossref already gave this paper its year, so that one consulted
    answer (or a year corrected by hand since) is not reopened every time.
    The exception is an article Crossref listed before its issue was out,
    which gains its volume now: its year moves with the issue. online_year
    keeps the earliest year anyone has given.
    """
    changed = gained_volume = False
    for field in ("venue", "volume", "issue", "pages"):
        value = rec.get(field)
        if value and not getattr(paper, field):
            setattr(paper, field, value)
            changed = True
            gained_volume = gained_volume or field == "volume"
    year = rec.get("year")
    # Whether this paper's year has been held against Crossref's print date.
    # Its own marker, not the presence of a Crossref id: libraries from
    # before the rule have Crossref ids on papers whose year was never
    # compared (Liengaard et al., 2024, for the 2025 issue).
    crossref_spoke = bool((paper.source_ids or {}).get("crossref_year"))
    from_crossref = "crossref" in _record_sources(rec)
    if year and year != paper.year and (
            not paper.year
            or (from_crossref and (not crossref_spoke or gained_volume))):
        paper.year = year
        paper.dedupe_key = title_key(paper.title or "", year)
        changed = True
    if from_crossref and year and not crossref_spoke:
        paper.source_ids = {**(paper.source_ids or {}), "crossref_year": True}
    earliest = min((y for y in (paper.online_year, rec.get("online_year")) if y),
                   default=None)
    if earliest != paper.online_year:
        paper.online_year = earliest
        changed = True
    return changed


def enrich_journals(db: Session, papers: list[models.Paper]) -> int:
    """Make sure every candidate's journal has its quality signals cached.

    Only for the shortlist that gets ranked, and each journal is looked up
    once. Papers with no ISSN - books, working papers, preprints - are
    skipped by design.
    """
    seen: set[str] = set()
    fetched = 0
    for paper in papers:
        issns = [i for i in ([paper.issn_l] + list(paper.issns or [])) if i]
        key = next((journals.normalize_issn(i) for i in issns
                    if journals.normalize_issn(i)), None)
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            if journals.get_or_fetch(db, issns) is not None:
                fetched += 1
        except Exception as exc:  # never fail a search over a metrics lookup
            logger.warning("journal lookup failed for %s: %s", key, exc)
            db.rollback()
    return fetched


# A pause between two Crossref lookups: the polite pool asks for a steady
# pace rather than bursts.
CROSSREF_DELAY_SECONDS = 0.2
# Lookups failing one after another mean Crossref is down or out of reach,
# and the rest are not tried: the step waiting on this should not sit through
# every lookup's retries.
CROSSREF_GIVE_UP_AFTER = 2


def wants_polish(paper: models.Paper) -> bool:
    """Whether Crossref may still have something to add to this paper's
    reference: it has a DOI, and either Crossref has not given it its year
    yet (the one it has may be the online-first year), or Crossref has, but a
    journal article still lacks its volume or pages - it was in press then.
    A preprint has no version of record to find."""
    if not paper.doi or paper.work_type == TYPE_PREPRINT:
        return False
    if not (paper.source_ids or {}).get("crossref_year"):
        return True
    if paper.work_type in (None, TYPE_ARTICLE, TYPE_REVIEW):
        return not (paper.volume and paper.pages)
    if paper.work_type == TYPE_CHAPTER:
        return not paper.pages
    return False


def polish_from_crossref(db: Session, papers: list[models.Paper], *,
                         client: httpx.Client | None = None) -> int:
    """Complete the references of these papers from their Crossref records.

    OpenAlex and Semantic Scholar give the year a paper went online, where a
    reference needs the issue's, and they often lack the volume, issue or
    pages. For each paper that wants_polish, this fetches its Crossref record
    by DOI - one lookup after another, with the contact email from the
    settings for Crossref's polite pool - and fills in volume, issue, pages
    and the journal where they are missing, takes Crossref's year as the
    paper's year and keeps the earliest as online_year (see _take_reference).
    The paper's source ids gain Crossref, which is what tells the next call
    that its year has been settled.

    The lookups all happen before anything is written, then one commit, so
    no write transaction is held across a network call. Returns how many
    papers it improved. Never raises: a lookup that fails is logged and
    skipped, and a failure to save is logged and rolled back.
    """
    try:
        return _polish_from_crossref(db, papers, client)
    except Exception:  # whatever went wrong, the caller's step goes on
        logger.exception("Completing references from Crossref failed")
        return 0


def _polish_from_crossref(db: Session, papers: list[models.Paper],
                          client: httpx.Client | None) -> int:
    todo = [(paper.id, paper.doi) for paper in papers
            if paper.id is not None and wants_polish(paper)]
    if not todo:
        return 0
    found: list[tuple[int, dict]] = []
    http = make_client(client)
    try:
        failures = 0
        for n, (paper_id, doi) in enumerate(todo):
            if n:
                time.sleep(CROSSREF_DELAY_SECONDS)
            try:
                record = crossref.get_by_doi(doi, client=http)
            except Exception as exc:
                logger.warning("Crossref lookup of %s failed: %s", doi, exc)
                failures += 1
                if failures >= CROSSREF_GIVE_UP_AFTER:
                    logger.warning("Crossref is not answering; %d more "
                                   "references are left as they are",
                                   len(todo) - n - 1)
                    break
                continue
            failures = 0
            if record:
                found.append((paper_id, record))
    finally:
        if client is None:
            http.close()

    improved = 0
    try:
        for paper_id, record in found:
            paper = db.get(models.Paper, paper_id)
            if paper is None:  # deleted while Crossref was being asked
                continue
            if _take_reference(paper, record):
                improved += 1
            if "crossref" not in (paper.source_ids or {}):
                paper.source_ids = {**(paper.source_ids or {}),
                                    "crossref": record.get("source_id") or paper.doi}
        db.commit()
    except Exception as exc:
        logger.warning("Could not save what Crossref added: %s", exc)
        db.rollback()
        return 0
    if improved:
        logger.info("Crossref completed the references of %d paper%s", improved,
                    "" if improved == 1 else "s")
    return improved


def store_scores(db: Session, search: models.Search,
                 scores: dict[int, tuple[float, str]]) -> int:
    """Record relevance scores for papers of one search. Returns how many.

    Scores are per search (PaperScore), so a paper that two research
    questions both hit is scored against each question's own goal.
    Paper.llm_score mirrors the latest score for display only. A paper that
    already has a score in this search keeps it (a resubmitted batch must
    not violate the one-score-per-search rule).
    """
    existing = {s.paper_id for s in search.scores}
    in_search = {p.id: p for p in search.papers}
    stored = 0
    for paper_id, (score, reason) in scores.items():
        paper = in_search.get(paper_id)
        if paper is None or paper_id in existing:
            continue
        score = max(0.0, min(100.0, float(score)))
        search.scores.append(models.PaperScore(
            paper_id=paper_id, score=score, reason=reason))
        paper.llm_score, paper.llm_reason = score, reason
        existing.add(paper_id)
        stored += 1
    db.commit()
    return stored


def _find_existing(db: Session, project_id: int, rec: dict) -> models.Paper | None:
    if rec.get("doi"):
        paper = db.execute(
            select(models.Paper).where(models.Paper.project_id == project_id,
                                       models.Paper.doi == rec["doi"])
        ).scalar_one_or_none()
        if paper:
            return paper
    key = title_key(rec.get("title") or "", rec.get("year"))
    paper = db.execute(
        select(models.Paper).where(models.Paper.project_id == project_id,
                                   models.Paper.dedupe_key == key)
    ).scalars().first()
    if paper is not None:
        return paper
    # The sources disagree about which year a work is from - the online one
    # or the issue's - so a paper kept under one of them is the same paper
    # when a record its DOI did not find arrives with the other.
    years = {y for y in (rec.get("year"), rec.get("online_year")) if y}
    stem = title_key(rec.get("title") or "", None)  # "the normalized title|"
    if not years or stem == "|":
        return None
    same_title = db.execute(
        select(models.Paper).where(models.Paper.project_id == project_id,
                                   models.Paper.dedupe_key.like(stem + "%"))
        .order_by(models.Paper.id)
    ).scalars().all()
    return next((p for p in same_title if years & {p.year, p.online_year}), None)


def submit_to_retrieval(db: Session, project: models.Project,
                        paper_ids: list[int]) -> list[int]:
    """Fetch the PDFs for the selected papers. Returns the ids to download."""
    moved: list[int] = []
    for pid in paper_ids:
        paper = db.get(models.Paper, pid)
        if paper is None or paper.project_id != project.id:
            continue
        if paper.pdf_status == models.PDF_DOWNLOADED:
            continue  # already in hand; nothing to fetch
        if paper.stage == models.STAGE_SCREENING:
            paper.stage = models.STAGE_RETRIEVAL
            if paper.status == models.STATUS_UNSCREENED:
                paper.status = models.STATUS_INCLUDED
        paper.pdf_status = models.PDF_PENDING
        paper.pdf_error = None
        moved.append(pid)
    db.commit()
    return moved


def project_stats(db: Session, project: models.Project) -> dict:
    listed = [models.Paper.project_id == project.id,
              models.Paper.stage != models.STAGE_LOOKUP]
    by_status = dict(db.execute(
        select(models.Paper.status, func.count(models.Paper.id))
        .where(*listed)
        .group_by(models.Paper.status)
    ).all())
    by_stage = dict(db.execute(
        select(models.Paper.stage, func.count(models.Paper.id))
        .where(*listed)
        .group_by(models.Paper.stage)
    ).all())
    pdf_done = db.execute(
        select(func.count(models.Paper.id))
        .where(models.Paper.project_id == project.id,
               models.Paper.pdf_status == models.PDF_DOWNLOADED)
    ).scalar_one()
    annotated = db.execute(
        select(func.count(func.distinct(models.Annotation.paper_id)))
        .join(models.Paper, models.Annotation.paper_id == models.Paper.id)
        .where(models.Paper.project_id == project.id,
               models.Annotation.status == models.ANNOT_DONE)
    ).scalar_one()
    searches = db.execute(
        select(models.Search).where(models.Search.project_id == project.id)
        .order_by(models.Search.created_at.desc()).limit(8)
    ).scalars().all()
    questions = db.execute(
        select(func.count(models.ResearchQuestion.id))
        .where(models.ResearchQuestion.project_id == project.id)
    ).scalar_one()
    waiting = db.execute(
        select(func.count(models.ClaudeTask.id))
        .where(models.ClaudeTask.project_id == project.id,
               models.ClaudeTask.status.in_(models.TASK_LIVE_STATUSES))
    ).scalar_one()
    return {
        "total": sum(by_stage.values()),
        "questions": questions,
        "by_status": by_status,
        "screening": by_stage.get(models.STAGE_SCREENING, 0),
        "retrieval": by_stage.get(models.STAGE_RETRIEVAL, 0),
        "pdf_done": pdf_done,
        "annotated": annotated,
        "recent_searches": searches,
        "waiting_for_claude": waiting,
    }


def prune_lookups(db: Session, older_than_days: int = 7) -> int:
    """Delete lookup hits nobody picked, once they are a week old.

    Only papers still in the lookup stage, attached to no research question,
    carrying no PDF, note, tag or annotation - anything the user touched is
    theirs to keep. Returns how many went.
    """
    from datetime import timedelta

    cutoff = models.utcnow() - timedelta(days=older_than_days)
    candidates = db.execute(
        select(models.Paper).where(models.Paper.stage == models.STAGE_LOOKUP)
    ).scalars().all()
    removed = 0
    for paper in candidates:
        created = models.aware(paper.created_at)
        if created is None or created > cutoff:
            continue
        if (paper.pdf_status == models.PDF_DOWNLOADED or paper.note
                or paper.tags or paper.annotations
                or any(s.research_question_id for s in paper.searches)):
            continue
        db.delete(paper)
        removed += 1
    if removed:
        db.commit()
        logger.info("Cleared %d unpicked lookup hits", removed)
    return removed


def question_number_lock(project_id: int):
    """Held while a new question takes the next number in its project."""
    return locks.for_key(f"question-number:{project_id}")


def projects_overview(db: Session) -> list[dict]:
    projects = db.execute(
        select(models.Project).order_by(models.Project.created_at.desc())
    ).scalars().all()
    return [{"project": p, "stats": project_stats(db, p)} for p in projects]


# ------------------------------------------------------------------ projects

def create_project(db: Session, name: str,
                   description: str | None = None) -> models.Project:
    """Create a project with the user's default pipeline parameters.

    The slug names the PDF folder, so it is unique; a clash gets -2, -3
    appended. A name already in use is refused.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Project name required")
    if db.execute(select(models.Project.id)
                  .where(models.Project.name == name)).scalar_one_or_none():
        raise ValueError("You already have a project with that name")
    base_slug = slug = models.slugify(name)
    n = 2
    while db.execute(
        select(models.Project.id).where(models.Project.slug == slug)
    ).scalar_one_or_none():
        slug = f"{base_slug}-{n}"
        n += 1
    defaults = settings.project_defaults()
    project = models.Project(name=name, slug=slug,
                             description=(description or "").strip() or None,
                             **defaults)
    db.add(project)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("You already have a project with that name") from exc
    return project


def delete_project(db: Session, project: models.Project) -> None:
    """Delete a project, everything in it, and its PDF folder.

    Refused only while the app itself is working on one of its questions in
    the background (searching, downloading): that thread holds ids into the
    project. Questions merely waiting for Claude are fine to delete - their
    tasks go with them. The folder is removed after the commit, so a failed
    delete never leaves a project whose PDFs are gone.
    """
    busy = [rq for rq in project.questions
            if rq.status in models.RQ_APP_STATUSES]
    if busy:
        raise ValueError(f"{busy[0].label} is {busy[0].status_label.lower()} "
                         "right now - wait a moment and try again")
    slug = project.slug
    db.delete(project)
    db.commit()
    shutil.rmtree(Path(settings.pdf_dir) / slug, ignore_errors=True)


# --------------------------------------------------------------- run guard

class RunRefused(Exception):
    """A pipeline run was not started. The message is for the person (or the
    agent) who asked, and says what to do instead."""


_START_LOCK = threading.Lock()


def start_run(db: Session, rq: models.ResearchQuestion, *, source: str,
              session: str | None = None, first_run: bool = False) -> None:
    """Mark a question as starting a run, or refuse if one is going.

    There are no ceilings on this machine - nobody else's quota is at
    stake, and every step that costs anything is done by Claude, at the
    user's pace. What remains is the one guard that matters: one run per
    question at a time. The status write is conditional, so two callers that
    both got past the check still produce one run. first_run says the row was
    inserted moments ago by the same request (its default status is a running
    one, so "already running?" is meaningless for it).
    """
    with _START_LOCK:
        if not first_run and rq.running:
            raise RunRefused(_already_running(rq))
        seen_status = rq.status
        values = dict(status=models.RQ_QUEUED, status_detail="Starting",
                      error=None, created_by=source)
        if source == "mcp":
            values["mcp_session"] = session or None
        result = db.execute(
            update(models.ResearchQuestion)
            .where(models.ResearchQuestion.id == rq.id,
                   models.ResearchQuestion.status == seen_status)
            .values(**values)
        )
        if result.rowcount == 0:
            db.rollback()
            raise RunRefused(_already_running(rq))
        db.commit()
    db.refresh(rq)


def _already_running(rq: models.ResearchQuestion) -> str:
    return (f"{rq.label} is already running ({rq.status_label.lower()}). "
            "Let it finish, or stop it on the question page, before starting "
            "it again.")


# "Download next" on a project's page. An article matters to a question when
# Claude scored it this much or more for it (0-100, where 50 is "related but
# tangential").
IMPORTANT_SCORE = 70


def articles_to_get(db: Session, project: models.Project) -> list[dict]:
    """The articles worth getting the PDF of, most important first: the ones
    without a PDF that Claude scored IMPORTANT_SCORE or more for at least one
    of the project's questions, less those the user excluded.

    One that matters to more questions comes first; among those that matter
    to as many, the higher importance - its scores on the questions it
    matters to, added up - then the higher best score. A question that
    searched more than once keeps its best score for the article."""
    rows = db.execute(
        select(models.ResearchQuestion.id, models.PaperScore.paper_id,
               models.PaperScore.score, models.PaperScore.reason)
        .select_from(models.PaperScore)
        .join(models.Search, models.Search.id == models.PaperScore.search_id)
        .join(models.ResearchQuestion,
              models.ResearchQuestion.id == models.Search.research_question_id)
        .where(models.ResearchQuestion.project_id == project.id)
    ).all()
    best: dict[int, dict[int, tuple[float, str | None]]] = {}
    for question_id, paper_id, score, reason in rows:
        mine = best.setdefault(paper_id, {})
        if question_id not in mine or score > mine[question_id][0]:
            mine[question_id] = (score, reason)
    wanted = [paper_id for paper_id, scores in best.items()
              if any(score >= IMPORTANT_SCORE for score, _ in scores.values())]
    if not wanted:
        return []
    papers = db.execute(
        select(models.Paper).where(
            models.Paper.id.in_(wanted),
            models.Paper.project_id == project.id,
            models.Paper.stage != models.STAGE_LOOKUP,
            models.Paper.pdf_status != models.PDF_DOWNLOADED,
            models.Paper.status != models.STATUS_EXCLUDED)
    ).scalars().all()
    questions = {q.id: q for q in project.questions}
    out = []
    for paper in papers:
        scores = sorted(((questions[qid], score, reason)
                         for qid, (score, reason) in best[paper.id].items()
                         if qid in questions),
                        key=lambda s: (-s[1], s[0].number or 0))
        important = [score for _, score, _ in scores if score >= IMPORTANT_SCORE]
        if not important:
            continue
        out.append({
            "paper": paper,
            "scores": [{"question": q, "score": round(score), "reason": reason,
                        "important": score >= IMPORTANT_SCORE}
                       for q, score, reason in scores],
            "matters_to": len(important),
            "importance": round(sum(important)),
            "best": max(important),
        })
    out.sort(key=lambda a: (-a["matters_to"], -a["importance"], -a["best"],
                            (a["paper"].title or "").lower()))
    return out
