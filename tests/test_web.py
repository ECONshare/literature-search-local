"""The web pages: each one renders, and what you do on them works.

The sources and the PDF downloads are the fakes from test_pipeline_e2e; the
queue is worked by the fake Claude there, through /mcp, as the real one would.
"""
import html
import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from litreview_local import models, pdfs, tasks
from litreview_local.config import settings
from litreview_local.db import SessionLocal
from litreview_local.web import LOCAL_DIR, _bridge_python
from litreview_local.pipeline import SOURCES

from test_pipeline_e2e import BODY, PAPERS, FakeSource, fakes, make_pdf, work_queue  # noqa: F401

SAY = "Work through my LitReview queue"
STATIC = Path(__file__).resolve().parents[1] / "litreview_local" / "static"
HX = {"HX-Request": "true", "HX-Current-URL": "http://127.0.0.1:8799/"}


def _flat(text: str) -> str:
    """A page's text with its entities read and its whitespace run together."""
    return " ".join(html.unescape(text).split())


# ------------------------------------------------------------- helpers

def _pdf_bytes(tmp_path, lines, name="upload.pdf") -> bytes:
    path = tmp_path / name
    make_pdf(path, lines)
    return path.read_bytes()


def _new_project(client, name="Remote work review") -> int:
    response = client.post("/projects", data={"name": name,
                                              "description": "Chapter 2"})
    assert response.status_code == 200
    match = re.fullmatch(r"/projects/(\d+)", response.url.path)
    assert match, response.url
    return int(match.group(1))


def _ask(client, project_id, text="Does remote work improve employee wellbeing?") -> int:
    response = client.post(f"/projects/{project_id}/questions", data={"text": text})
    assert response.status_code == 200
    match = re.fullmatch(r"/questions/(\d+)", response.url.path)
    assert match, response.url
    return int(match.group(1))


def _answered(client, claude) -> tuple[int, int]:
    """A project with RQ1 run to a cited answer, the way the pages start it."""
    project_id = _new_project(client)
    question_id = _ask(client, project_id)
    done = work_queue(claude)
    assert done[-1] == "synthesize"
    return project_id, question_id


def _rows(model, **where):
    db = SessionLocal()
    try:
        return db.query(model).filter_by(**where).all()
    finally:
        db.close()


def _row(model, row_id):
    db = SessionLocal()
    try:
        return db.get(model, row_id)
    finally:
        db.close()


def _live(**where):
    db = SessionLocal()
    try:
        return [t.kind for t in tasks.live_tasks(db, **where)]
    finally:
        db.close()


def _papers(project_id):
    return sorted(_rows(models.Paper, project_id=project_id), key=lambda p: p.id)


@pytest.fixture(autouse=True)
def _claude_seen_afresh():
    """tasks.note_client writes "Claude last seen" at most once a minute per
    process; each test's library is new, so let its first call in write."""
    tasks._seen_cache.clear()
    yield


@pytest.fixture
def user_settings():
    """Put settings.json back as it was: other tests create projects with
    the defaults it holds."""
    before = settings.user_settings()
    yield
    settings.save_user_settings(before)


# ------------------------------------------------------- every page

def test_every_page_renders_in_an_empty_library(client):
    home = client.get("/")
    assert home.status_code == 200
    # Projects is only projects; a new library points to Getting started.
    assert "No projects yet" in home.text and 'href="/start"' in home.text
    assert SAY not in home.text
    start = client.get("/start")
    assert start.status_code == 200 and "Getting started" in start.text and SAY in start.text
    # Offline: htmx is served from here, and nothing is loaded from a CDN.
    assert "/static/htmx.min.js" in home.text
    assert "unpkg" not in home.text and "cdn" not in home.text.lower()
    htmx = client.get("/static/htmx.min.js")
    assert htmx.status_code == 200 and 'version:"1.9.12"' in htmx.text
    for path in ("/tasks", "/start", "/start/advanced", "/connect", "/settings", "/claude-status",
                 "/static/style.css", "/static/app.js",
                 "/static/question.js", "/static/answer_text.js"):
        assert client.get(path).status_code == 200, path
    indicator = client.get("/claude-status").text
    assert "Claude has not connected yet" in indicator
    assert 'href="/start#claude-code"' in indicator  # it leads to connecting Claude
    assert "Nothing is waiting for Claude" in client.get("/tasks").text
    missing = client.get("/projects/999")
    assert missing.status_code == 404 and "no such project" in missing.text


def test_every_page_renders_in_a_populated_library(client, claude, fakes, tmp_path):
    project_id, question_id = _answered(client, claude)
    client.post(f"/projects/{project_id}/documents",
                data={"title": "Protocol", "description": "the plan"},
                files={"file": ("protocol.pdf", _pdf_bytes(tmp_path, BODY),
                                "application/pdf")})
    document = _rows(models.ProjectDocument, project_id=project_id)[0]
    paper = _papers(project_id)[0]
    annotation = _rows(models.Annotation, paper_id=paper.id)[0]
    client.post(f"/papers/{paper.id}/tags", data={"name": "to read"}, headers=HX)

    pages = [
        "/", f"/projects/{project_id}", f"/projects/{project_id}/papers",
        f"/projects/{project_id}/papers?tag=to+read&status=included&pdf=yes&q=remote"
        "&stage=retrieval&sort=year&question_id=" + str(question_id),
        f"/questions/{question_id}", f"/questions/{question_id}/status",
        f"/questions/{question_id}/summary", f"/questions/{question_id}/included?all=1",
        f"/questions/{question_id}/verification",
        f"/documents/{document.id}", f"/documents/{document.id}/pdf",
        f"/annotations/{annotation.id}/view?page=1", f"/annotations/{annotation.id}/pdf",
        f"/papers/{paper.id}/pdf/original", f"/papers/{paper.id}/card",
        f"/papers/{paper.id}/edit", f"/papers/{paper.id}/fit",
        f"/projects/{project_id}/uploads/{paper.id}",
        "/tasks", "/start", "/start/advanced", "/settings", "/claude-status",
    ]
    for path in pages:
        assert client.get(path).status_code == 200, path

    home = client.get("/").text
    assert "Remote work review" in home and "No projects yet" not in home
    indicator = client.get("/claude-status").text
    assert "Claude: last seen" in indicator and 'href="/tasks"' in indicator
    page = client.get(f"/questions/{question_id}").text
    assert 'class="cit"' in page and "Copy as text" in page
    # The reference list is APA, with the journal in italics.
    assert re.search(r'<ol class="references".*?<i>Journal of Work</i>', page, re.S)
    assert "Export with evidence" in page
    assert f"/questions/{question_id}/export/evidence.html" in page
    assert "Ask Claude to check the citations" in page
    citmap = json.loads(re.search(r'<script id="citmap" type="application/json">(.*?)</script>',
                                  page, re.S).group(1))
    first = next(iter(citmap.values()))
    assert first["quote"] == BODY[0] and first["located"] == "pdf"
    viewer = client.get(f"/annotations/{annotation.id}/view?page=1").text
    assert f"/annotations/{annotation.id}/pdf#page=1" in viewer
    assert client.get(f"/annotations/{annotation.id}/pdf").content[:5] == b"%PDF-"
    # A missing highlighted copy is drawn again from the stored geometry.
    highlighted = pdfs.annotation_path(_row(models.Project, project_id).slug, annotation.id)
    highlighted.unlink()
    assert client.get(f"/annotations/{annotation.id}/pdf").content[:5] == b"%PDF-"
    assert highlighted.exists()
    for fmt, marker in (("csv", "title,authors"), ("bib", "@article"), ("ris", "TY  - JOUR")):
        exported = client.get(f"/projects/{project_id}/export/{fmt}?status=all")
        assert exported.status_code == 200 and marker in exported.text, fmt
        assert "attachment" in exported.headers["content-disposition"]
    tagged = client.get(f"/projects/{project_id}/export/ris?status=all&tag=to%20read").text
    assert tagged.count("TY  - JOUR") == 1


# ------------------------------------------------------ the question

def test_a_new_question_waits_for_claude_and_can_be_stopped(client):
    project_id = _new_project(client)
    question_id = _ask(client, project_id)
    page = client.get(f"/questions/{question_id}").text
    assert "Waiting for Claude" in page
    assert "Waiting for Claude to plan the search" in page
    assert SAY in page and 'data-copy="Work through my LitReview queue"' in page
    assert "/stop" in page
    assert _live(research_question_id=question_id) == ["parse_question"]
    assert "1 task waiting" in client.get("/claude-status").text
    project = client.get(f"/projects/{project_id}").text
    assert "Waiting for Claude" in project and "RQ1" in project
    tasks_page = client.get("/tasks").text
    assert "RQ1: plan the literature search" in tasks_page and "Set aside" in tasks_page
    # The status region polls itself while the question runs.
    status = client.get(f"/questions/{question_id}/status", headers=HX)
    assert "HX-Refresh" not in status.headers and "hx-trigger" in status.text

    stopped = client.post(f"/questions/{question_id}/stop")
    assert "Stopped RQ1" in stopped.text
    assert _live(research_question_id=question_id) == []
    assert not _row(models.ResearchQuestion, question_id).running
    # Once it has settled, the poll asks for a reload.
    assert client.get(f"/questions/{question_id}/status",
                      headers=HX).headers.get("HX-Refresh") == "true"


def test_a_short_question_is_refused_with_a_reason(client):
    project_id = _new_project(client)
    response = client.post(f"/projects/{project_id}/questions", data={"text": "why?"})
    assert "Write the research question out in full" in response.text
    assert _rows(models.ResearchQuestion, project_id=project_id) == []


def test_editing_the_question_offers_both_modes(client, claude, fakes):
    _, question_id = _answered(client, claude)
    new_text = "Does working from home change how well employees sleep?"
    response = client.post(f"/questions/{question_id}/run",
                           data={"mode": "full", "text": new_text})
    assert "Searching again" in response.text
    rq = _row(models.ResearchQuestion, question_id)
    assert rq.text == new_text and rq.status == models.RQ_PARSING
    assert _live(research_question_id=question_id) == ["parse_question"]
    # While it runs, it is not started again.
    again = client.post(f"/questions/{question_id}/run", data={"mode": "evidence"})
    assert "is running" in again.text
    client.post(f"/questions/{question_id}/stop")

    response = client.post(f"/questions/{question_id}/run", data={"mode": "evidence"})
    assert "Rewriting from the evidence" in response.text
    rq = _row(models.ResearchQuestion, question_id)
    assert rq.text == new_text
    # No new search: straight to writing the answer from what was read.
    assert rq.status == models.RQ_SYNTHESIZING
    assert _live(research_question_id=question_id) == ["synthesize"]
    bad = client.post(f"/questions/{question_id}/run", data={"mode": "guess"})
    assert bad.status_code == 400


def test_asking_claude_to_change_the_answer_waits_until_it_lands(client, claude, fakes):
    _, question_id = _answered(client, claude)
    block = client.post(f"/questions/{question_id}/revise",
                        data={"instruction": "Make it one sentence."}, headers=HX)
    assert block.status_code == 200
    assert "Waiting for Claude to change the answer" in block.text
    assert "Make it one sentence." in block.text and SAY in block.text
    assert 'contenteditable="true"' not in block.text
    assert _live(research_question_id=question_id) == ["revise_answer"]
    page = client.get(f"/questions/{question_id}").text
    assert "Waiting for Claude to change the answer" in page

    task = claude.call("next_task")
    assert task["kind"] == "revise_answer"
    key = re.findall(r"\[CIT:([0-9a-f-]+)\]", task["input"]["evidence"])[0]
    claude.call("submit_task", task_id=task["task_id"],
                output={"summary": f"Remote work helps, in one sentence. [CIT:{key}]"})
    block = client.get(f"/questions/{question_id}/summary").text
    assert "Waiting for Claude to change the answer" not in block
    assert "in one sentence" in block and 'contenteditable="true"' in block

    client.post(f"/questions/{question_id}/revise", data={"instruction": "Longer."},
                headers=HX)
    cancelled = client.post(f"/questions/{question_id}/revise/cancel", headers=HX)
    assert "Waiting for Claude" not in cancelled.text
    assert _live(research_question_id=question_id) == []


def test_saving_an_edited_answer_is_sanitized_and_guarded(client, claude, fakes):
    _, question_id = _answered(client, claude)
    block = client.get(f"/questions/{question_id}/summary").text
    base = re.search(r'name="summary_base" value="([0-9a-f]+)"', block).group(1)
    key = re.search(r'data-k="([0-9]+-[0-9a-f]+)"', block).group(1)
    edited = (f'<p>Edited <script>alert(1)</script><b onclick="x()">claim</b> '
              f'<cite class="cit" data-k="{key}" contenteditable="false">(X)</cite>.</p>')
    saved = client.post(f"/questions/{question_id}/summary", headers=HX,
                        data={"summary_html": edited, "summary_base": base})
    assert saved.status_code == 200
    stored = _row(models.ResearchQuestion, question_id).summary_html
    assert "<script>" not in stored and "onclick" not in stored
    assert f'data-k="{key}"' in stored and "Edited" in stored
    # A save from a page showing an older answer hands the edit back.
    stale = client.post(f"/questions/{question_id}/summary", headers=HX,
                        data={"summary_html": "<p>Mine</p>", "summary_base": base})
    assert stale.status_code == 409 and "Not saved" in stale.text and "Mine" in stale.text
    assert _row(models.ResearchQuestion, question_id).summary_html == stored


def test_claudes_citation_check_marks_the_citations(client, claude, monkeypatch):
    from test_verification import _answered as answered_by_claude

    question_id, keys = answered_by_claude(claude, monkeypatch)
    page = client.get(f"/questions/{question_id}").text
    assert "Ask Claude to check the citations" in page
    client.post(f"/questions/{question_id}/verify")
    page = client.get(f"/questions/{question_id}").text
    assert 'id="verification"' in page
    assert "Waiting for Claude to check the citations" in page and SAY in page
    task = claude.call("next_task", kind="verify_answer")
    claude.call("submit_task", task_id=task["task_id"], output={
        "checks": [
            {"sentence": 1, "key": keys[0], "verdict": "supported", "note": ""},
            {"sentence": 2, "key": keys[1], "verdict": "unsupported",
             "note": "Nothing about costs."},
            {"sentence": 2, "key": keys[0], "verdict": "partial", "note": "Only wellbeing."}],
        "overall": "The cost claim has no support."})
    poll = client.get(f"/questions/{question_id}/verification", headers=HX)
    assert poll.headers.get("HX-Trigger") == "litreview:verified"
    page = client.get(f"/questions/{question_id}").text
    assert "The cost claim has no support." in page
    assert f'data-k="{keys[1]}" data-verdict="unsupported" data-note="Nothing about costs."' in page
    assert f'data-k="{keys[0]}" data-verdict="partial"' in page
    assert "Waiting for Claude to check" not in page


# ------------------------------------------- the articles of a question

def test_a_failed_download_gets_its_pdf_from_the_page(client, claude, fakes,
                                                      monkeypatch, tmp_path):
    def download(paper, client=None):
        if paper.doi.endswith(".5"):
            return False, "no open-access URL found"
        make_pdf(pdfs.original_path(paper.project.slug, paper.id), [paper.title] + BODY)
        return True, None

    monkeypatch.setattr(pdfs, "download_pdf", download)
    project_id, question_id = _answered(client, claude)
    failed = next(p for p in _papers(project_id) if p.doi.endswith(".5"))
    assert failed.pdf_status == models.PDF_FAILED
    panel = client.get(f"/questions/{question_id}").text
    assert "No PDF: the download failed" in panel and "Upload its PDF" in panel

    refused = client.post(f"/papers/{failed.id}/upload-pdf",
                          data={"next": f"/questions/{question_id}"},
                          files={"file": ("notes.pdf", b"not a pdf", "application/pdf")})
    assert "That file is not a PDF" in refused.text
    assert _row(models.Paper, failed.id).pdf_status == models.PDF_FAILED

    response = client.post(f"/papers/{failed.id}/upload-pdf",
                           data={"next": f"/questions/{question_id}#included-{failed.id}"},
                           files={"file": ("paper.pdf", _pdf_bytes(tmp_path, BODY),
                                           "application/pdf")})
    assert response.url.path == f"/questions/{question_id}"
    assert "Claude reads it for RQ1" in response.text
    paper = _row(models.Paper, failed.id)
    assert paper.pdf_status == models.PDF_DOWNLOADED
    assert _row(models.PaperText, failed.id) is not None
    assert _live(paper_id=failed.id) == ["annotate_paper"]


def test_a_known_article_is_found_labelled_and_added(client, claude, fakes, monkeypatch):
    project_id, question_id = _answered(client, claude)
    known = {"title": "Commuting time and happiness", "doi": "10.9/commute",
             "year": 2019, "authors": ["Cara Commute"], "abstract": "Commutes.",
             "source_ids": {"openalex": "W99"}, "issns": [], "citation_count": 3}
    monkeypatch.setitem(SOURCES, "openalex", FakeSource([known] + PAPERS[:1]))
    found = client.post(f"/questions/{question_id}/find",
                        data={"query": "Commuting time and happiness"}, headers=HX)
    assert found.status_code == 200
    assert "Exact title" in found.text
    assert "Already in this question" in found.text  # PAPERS[0] is in RQ1
    parked = next(p for p in _papers(project_id) if p.doi == "10.9/commute")
    assert parked.stage == models.STAGE_LOOKUP
    assert "Commuting time" not in client.get(f"/projects/{project_id}/papers").text

    added = client.post(f"/questions/{question_id}/include",
                        data={"paper_ids": [str(parked.id)], f"score_{parked.id}": "77"})
    assert "Adding 1 article to RQ1" in added.text
    paper = _row(models.Paper, parked.id)
    assert paper.stage != models.STAGE_LOOKUP
    assert _live(paper_id=parked.id) == ["annotate_paper"]
    assert "Commuting time" in client.get(f"/projects/{project_id}/papers").text


# ----------------------------------------------------- the article list

def test_tags_are_added_removed_and_filter_the_list(client, claude, fakes):
    project_id, _ = _answered(client, claude)
    first, second = _papers(project_id)[:2]
    block = client.post(f"/papers/{first.id}/tags", data={"name": "Method: DiD, to read"},
                        headers=HX)
    assert block.status_code == 200
    assert "Method: DiD" in block.text and "to read" in block.text
    assert 'id="tag-options" hx-swap-oob="true"' in block.text
    client.post(f"/papers/{first.id}/tags", data={"name": "method: did"}, headers=HX)
    client.post(f"/papers/{second.id}/tags", data={"name": "to read"}, headers=HX)
    assert _row(models.Paper, first.id) is not None
    db = SessionLocal()
    try:
        assert sorted(db.get(models.Paper, first.id).tag_names) == ["Method: DiD", "to read"]
    finally:
        db.close()

    listed = client.get(f"/projects/{project_id}/papers?tag=METHOD:%20did").text
    assert f'id="paper-{first.id}"' in listed and f'id="paper-{second.id}"' not in listed
    both = client.get(f"/projects/{project_id}/papers?tag=to%20read").text
    assert f'id="paper-{first.id}"' in both and f'id="paper-{second.id}"' in both
    assert "to read (2)" in both  # the filter offers the project's tags

    removed = client.post(f"/papers/{first.id}/tags/remove", data={"name": "Method: DiD"},
                          headers=HX)
    assert "Method: DiD" not in removed.text
    # No article carries it any more, so it is gone from the project.
    assert _rows(models.Tag, project_id=project_id, key="method: did") == []


def test_status_and_note_are_saved_at_once(client, claude, fakes):
    project_id, _ = _answered(client, claude)
    paper = _papers(project_id)[0]
    card = client.post(f"/papers/{paper.id}/status", data={"status": "excluded"}, headers=HX)
    assert card.status_code == 200 and "status-excluded" in card.text
    assert _row(models.Paper, paper.id).status == models.STATUS_EXCLUDED
    saved = client.post(f"/papers/{paper.id}/note", data={"note": "  Weak design.  "},
                        headers=HX)
    assert saved.text == '<span class="saved">Saved</span>'
    assert _row(models.Paper, paper.id).note == "Weak design."
    excluded = client.get(f"/projects/{project_id}/papers?status=excluded").text
    assert f'id="paper-{paper.id}"' in excluded and "Weak design." in excluded
    bad = client.post(f"/papers/{paper.id}/status", data={"status": "maybe-not"}, headers=HX)
    assert bad.status_code == 400
    assert "litreview:error" in bad.headers.get("HX-Trigger", "")


def test_card_details_parse_and_downloads(client, claude, fakes):
    project_id, _ = _answered(client, claude)
    paper = _papers(project_id)[0]
    edited = client.post(f"/papers/{paper.id}/details", headers=HX, data={
        "title": "A better title", "authors": "Ann Author\nBo Second", "year": "20x1",
        "venue": "", "doi": "", "url": "", "work_type": "", "abstract": ""})
    assert "The year has to be a year" in edited.text and "A better title" in edited.text
    edited = client.post(f"/papers/{paper.id}/details", headers=HX, data={
        "title": "A better title", "authors": "Ann Author", "year": "2021",
        "venue": "Journal of Work", "doi": paper.doi, "url": "", "work_type": "article",
        "abstract": "", "volume": "50", "issue": "1", "pages": "46-66"})
    assert "Details saved." in edited.text
    saved = _row(models.Paper, paper.id)
    assert saved.title == "A better title"
    assert (saved.volume, saved.issue, saved.pages) == ("50", "1", "46-66")
    # The card's reference is APA, with the journal and volume in italics.
    assert "<i>Journal of Work</i>, <i>50</i>(1), 46–66" in edited.text
    form = client.get(f"/papers/{paper.id}/edit").text
    assert 'name="volume" value="50"' in form and 'name="pages" value="46-66"' in form

    db = SessionLocal()
    try:
        db.get(models.Paper, paper.id).online_year = 2020
        db.commit()
    finally:
        db.close()
    assert "(online 2020)" in client.get(f"/papers/{paper.id}/card").text

    parsed = client.post(f"/papers/{paper.id}/parse", headers=HX)
    assert "Asked Claude to parse this PDF" in parsed.text
    assert "Waiting for Claude to parse it" in parsed.text
    assert _live(paper_id=paper.id, kinds=["parse_pdf"]) == ["parse_pdf"]

    db = SessionLocal()
    try:
        undownloaded = models.Paper(project_id=project_id, title="Not fetched yet",
                                    doi="10.9/later", oa_url="https://example.org/l.pdf",
                                    authors=[], issns=[], source_ids={})
        db.add(undownloaded)
        db.commit()
        later_id = undownloaded.id
    finally:
        db.close()
    response = client.post(f"/projects/{project_id}/download",
                           data={"paper_ids": [str(later_id)],
                                 "next": f"/projects/{project_id}/papers"})
    assert "Fetching 1 PDF" in response.text
    assert _row(models.Paper, later_id).pdf_status == models.PDF_DOWNLOADED
    nothing = client.post(f"/projects/{project_id}/download", data={})
    assert "Tick the articles" in nothing.text


def test_where_does_it_fit_waits_for_claude_then_files_it(client, claude, fakes):
    project_id, question_id = _answered(client, claude)
    second = _ask(client, project_id, "Does remote work change how much people sleep?")
    client.post(f"/questions/{second}/stop")
    paper = _papers(project_id)[0]
    panel = client.post(f"/papers/{paper.id}/fit", headers=HX)
    assert "Waiting for Claude" in panel.text and SAY in panel.text
    assert _live(paper_id=paper.id) == ["assess_article"]
    card = client.get(f"/papers/{paper.id}/card").text
    assert f'hx-get="/papers/{paper.id}/fit"' in card  # the card keeps the panel

    task = claude.call("next_task", kind="assess_article")
    claude.call("submit_task", task_id=task["task_id"], output={
        "fits": [{"question_id": question_id, "score": 90, "assessment": "Central."},
                 {"question_id": second, "score": 85, "assessment": "About sleep."}],
        "misfit": False, "suggested_question": "", "suggestion_reason": ""})
    panel = client.get(f"/papers/{paper.id}/fit", headers=HX).text
    assert "File the article" in panel and "About sleep." in panel
    assert "Already in this question." in panel  # RQ1 holds it

    filed = client.post(f"/projects/{project_id}/assign", data={
        "paper_id": str(paper.id), "question_ids": [str(question_id), str(second)],
        f"score_{second}": "85", "next": f"/projects/{project_id}/papers"})
    assert "Filing" in filed.text and "RQ2" in filed.text
    assert _live(paper_id=paper.id, research_question_id=second) == ["annotate_paper"]


def test_lookup_hits_stay_out_of_the_list_until_kept(client, monkeypatch):
    project_id = _new_project(client)
    hit = {"title": "An article nobody asked for", "doi": "10.9/stray", "year": 2020,
           "authors": ["N. Body"], "source_ids": {"openalex": "W5"}, "issns": []}
    for name in ("openalex", "semantic_scholar", "crossref"):
        monkeypatch.setitem(SOURCES, name, FakeSource([hit] if name == "openalex" else []))
    found = client.post(f"/projects/{project_id}/find", data={"query": "nobody asked"},
                        headers=HX)
    assert "An article nobody asked for" in found.text and "Not in the project yet" in found.text
    paper = _papers(project_id)[0]
    assert paper.stage == models.STAGE_LOOKUP
    listed = client.get(f"/projects/{project_id}/papers").text
    assert "An article nobody asked for" not in listed and "No articles yet" in listed
    assert "nobody asked" not in client.get(f"/projects/{project_id}/export/csv?status=all").text
    kept = client.post(f"/projects/{project_id}/keep", data={"paper_id": str(paper.id)},
                       headers=HX)
    assert "Kept in the project" in kept.text
    assert "An article nobody asked for" in client.get(f"/projects/{project_id}/papers").text


# ---------------------------------------------- the project page

def test_the_article_list_adds_articles_from_their_pdfs_too(client, tmp_path):
    project_id = _new_project(client)
    listed = client.get(f"/projects/{project_id}/papers").text
    assert "Add a known article" in listed and "Add articles from their PDFs" in listed
    assert f'hx-post="/projects/{project_id}/uploads"' in listed and 'name="files"' in listed
    # Side by side, each title with a hover-over for what it does.
    assert '<div class="panel-pair">' in listed
    assert 'aria-describedby="tip-find"' in listed and 'id="tip-uploads"' in listed
    pdf = _pdf_bytes(tmp_path, ["A report on remote work", "Published 2021."] + BODY)
    # Without HTMX the browser comes back to the article list, where the
    # upload shows.
    sent = client.post(f"/projects/{project_id}/uploads",
                       data={"next": f"/projects/{project_id}/papers#uploads"},
                       files=[("files", ("remote_report.pdf", pdf, "application/pdf"))])
    assert sent.url.path == f"/projects/{project_id}/papers" and "Added 1 PDF" in sent.text
    upload = _rows(models.PaperIdentification)[0]
    assert f'id="upload-{upload.paper_id}"' in sent.text
    # Somewhere off the site is not somewhere to go back to.
    elsewhere = client.post(f"/projects/{project_id}/uploads",
                            data={"next": "//evil.example/"},
                            files=[("files", ("again.pdf", pdf, "application/pdf"))])
    assert elsewhere.url.path == f"/projects/{project_id}"


def test_adding_articles_from_their_pdfs(client, tmp_path):
    project_id = _new_project(client)
    pdf = _pdf_bytes(tmp_path, ["A report on remote work", "Published 2021."] + BODY)
    rows = client.post(f"/projects/{project_id}/uploads", headers=HX, files=[
        ("files", ("remote_report.pdf", pdf, "application/pdf")),
        ("files", ("notes.txt", b"plain text", "text/plain"))])
    assert rows.status_code == 200
    assert "Waiting for Claude to identify it" in rows.text
    assert "notes.txt is not a PDF" in rows.text
    upload = _rows(models.PaperIdentification)[0]
    assert upload.status == models.IDENT_CLASSIFYING
    assert _live(paper_id=upload.paper_id) == ["identify_pdf"]
    project = client.get(f"/projects/{project_id}").text
    assert f'id="upload-{upload.paper_id}"' in project
    # Side by side with the meta documents; what each is for is a hover-over.
    assert '<div class="panel-pair">' in project and "Meta Documents" in project
    assert 'aria-describedby="tip-uploads"' in project and 'id="tip-documents"' in project
    assert 'aria-describedby="tip-ask"' in project and 'for="new-question-text"' in project

    # Without HTMX the browser is sent back to the panel.
    plain = client.post(f"/projects/{project_id}/uploads",
                        files=[("files", ("again.pdf", pdf, "application/pdf"))])
    assert plain.url.path == f"/projects/{project_id}" and "Added 1 PDF" in plain.text

    # "Is this it?" - no goes on to a web search by Claude.
    db = SessionLocal()
    try:
        ident = db.get(models.PaperIdentification, upload.paper_id)
        tasks.cancel(db, "test", paper_id=ident.paper_id)
        ident.status = models.IDENT_CONFIRM
        ident.candidate = {"record": {"title": "Remote work report", "authors": ["A B"],
                                      "year": 2021, "source": "openalex"},
                           "reason": "Same title.", "doubt": "the year differs"}
        db.commit()
    finally:
        db.close()
    row_url = f"/projects/{project_id}/uploads/{upload.paper_id}"
    row = client.get(row_url).text
    assert "Is this it?" in row and "the year differs" in row
    answered = client.post(f"{row_url}/answer", data={"answer": "no"}, headers=HX)
    assert "Waiting for Claude to find it on the web" in answered.text
    assert _live(paper_id=upload.paper_id) == ["find_web_page"]

    # A failed one can be tried again, or removed.
    db = SessionLocal()
    try:
        ident = db.get(models.PaperIdentification, upload.paper_id)
        tasks.cancel(db, "test", paper_id=ident.paper_id)
        ident.status = models.IDENT_FAILED
        ident.error = "Something went wrong"
        db.commit()
    finally:
        db.close()
    retried = client.post(f"{row_url}/retry", headers=HX)
    assert "Waiting for Claude to identify it" in retried.text
    db = SessionLocal()
    try:
        ident = db.get(models.PaperIdentification, upload.paper_id)
        tasks.cancel(db, "test", paper_id=ident.paper_id)
        ident.status = models.IDENT_FAILED
        db.commit()
    finally:
        db.close()
    removed = client.post(f"{row_url}/remove", headers=HX)
    assert removed.status_code == 200 and removed.text == ""
    assert _row(models.Paper, upload.paper_id) is None
    assert "no longer here" in client.get(row_url).text


def test_documents_upload_parse_and_delete(client, tmp_path):
    project_id = _new_project(client)
    response = client.post(f"/projects/{project_id}/documents",
                           data={"title": "Coding manual", "description": "how to code"},
                           files={"file": ("manual.pdf", _pdf_bytes(tmp_path, BODY),
                                           "application/pdf")})
    assert "Added “Coding manual”" in response.text
    document = _rows(models.ProjectDocument, project_id=project_id)[0]
    assert document.status == models.DOC_DONE and BODY[0] in document.content
    page = client.get(f"/documents/{document.id}").text
    assert "Text layer" in page and BODY[0] in page
    parsed = client.post(f"/documents/{document.id}/parse")
    assert "Asked Claude to parse" in parsed.text
    assert _live(document_id=document.id) == ["parse_pdf"]
    assert "Waiting for Claude to parse it" in client.get(f"/documents/{document.id}").text
    assert "Reading the PDF" in client.post(f"/documents/{document.id}/reread").text
    deleted = client.post(f"/documents/{document.id}/delete")
    assert "Deleted the document" in deleted.text
    assert _rows(models.ProjectDocument, project_id=project_id) == []
    assert not pdfs.document_path("remote-work-review", document.id).exists()
    bad = client.post(f"/projects/{project_id}/documents",
                      files={"file": ("x.pdf", b"nope", "application/pdf")})
    assert "not a PDF" in bad.text


def test_settings_set_how_wide_every_search_is(client, user_settings):
    first = _new_project(client, "First")
    page = client.get(f"/projects/{first}").text
    assert "<h2>How wide each search is</h2>" not in page    # on Settings only
    assert f'action="/projects/{first}/settings"' not in page
    db = SessionLocal()
    try:                                                # one set on its own page before
        db.get(models.Project, first).rank_top = 77
        db.commit()
    finally:
        db.close()
    assert "First has numbers or sources of its own" in _flat(client.get("/settings").text)
    saved = client.post("/settings", data={
        "contact_email": "me@uni.example", "fetch_per_source": "12", "rank_top": "9",
        "download_top": "3", "sources_shown": "1", "sources": ["openalex", "crossref"]})
    assert "Every project searches with these numbers" in saved.text
    assert "of its own" not in _flat(saved.text)
    project = _row(models.Project, first)
    assert (project.fetch_per_source, project.rank_top, project.download_top) == (12, 9, 3)
    assert project.sources == "openalex,crossref"
    second = _row(models.Project, _new_project(client, "Second"))
    assert (second.rank_top, second.sources) == (9, "openalex,crossref")
    # The question's text on the project page says what a run does.
    assert "Crossref (12 results from each)" in _flat(client.get(f"/projects/{first}").text)
    none = client.post("/settings", data={"sources_shown": "1", "rank_top": "5"})
    assert "Tick at least one source" in none.text
    assert _row(models.Project, first).rank_top == 9      # nothing was saved
    assert client.post(f"/projects/{first}/settings", data={}).status_code in (404, 405)


def test_project_deleting_by_name(client):
    project_id = _new_project(client, "Delete me")

    wrong = client.post(f"/projects/{project_id}/delete", data={"confirm_name": "delete me"})
    assert "Nothing was deleted" in wrong.text
    assert _row(models.Project, project_id) is not None
    right = client.post(f"/projects/{project_id}/delete", data={"confirm_name": "Delete me"})
    assert right.url.path == "/" and "Deleted Delete me" in right.text
    assert _row(models.Project, project_id) is None
    duplicate = _new_project(client, "Twice")
    again = client.post("/projects", data={"name": "Twice"})
    assert "already have a project with that name" in again.text
    assert len(_rows(models.Project, name="Twice")) == 1 and duplicate


# ---------------------------------------------------------- the queue

def test_a_stuck_task_can_be_set_aside(client, fakes):
    project_id = _new_project(client)
    question_id = _ask(client, project_id)
    task_id = _rows(models.ClaudeTask, research_question_id=question_id)[0].id
    response = client.post(f"/tasks/{task_id}/drop")
    assert "Set aside: RQ1: plan the literature search" in response.text
    assert _row(models.ClaudeTask, task_id).status == models.TASK_FAILED
    # The step goes on without it: the search ran with the question as typed,
    # and its ranking is waiting for Claude now.
    assert _row(models.ResearchQuestion, question_id).status == models.RQ_RANKING
    assert _live(research_question_id=question_id) == ["rank_papers"]
    again = client.post(f"/tasks/{task_id}/drop")
    assert "not waiting any more" in again.text


# ------------------------------------------------- settings, connect

def test_settings_are_saved(client, user_settings):
    saved = client.post("/settings", data={
        "contact_email": "me@uni.example", "s2_api_key": "k-123",
        "fetch_per_source": "30", "rank_top": "15", "download_top": "4"})
    assert "Settings saved" in saved.text
    values = settings.user_settings()
    assert values["contact_email"] == "me@uni.example" and values["s2_api_key"] == "k-123"
    assert (values["fetch_per_source"], values["rank_top"], values["download_top"]) == (30, 15, 4)
    page = client.get("/settings").text
    # The key is hidden, with an eye beside it that shows it (app.js).
    assert 'type="password" id="s2-api-key" name="s2_api_key" value="k-123"' in page
    assert 'data-reveal="s2-api-key"' in page and 'aria-label="Show the key"' in page
    assert "[data-reveal]" in client.get("/static/app.js").text
    assert str(settings.data_dir) in page and "LITREVIEW_DATA_DIR" in page
    assert str(settings.backups_dir) in page and "data-quit" in page
    refused = client.post("/settings", data={"contact_email": "not an address"})
    assert "does not look like an email address" in refused.text
    assert settings.user_settings()["contact_email"] == "me@uni.example"


def _fake_setup(config: Path, calls: list) -> types.ModuleType:
    module = types.ModuleType("litreview_local.claude_setup")
    module.RESTART_NOTE = "Quit Claude Desktop completely (tray icon / Cmd-Q) and reopen it."

    def desktop_entry(python_exe, bridge_path, *, port=None, data_dir=None):
        calls.append(("entry", python_exe, bridge_path, port))
        return {"command": python_exe, "args": [bridge_path]}

    def preview(config_path, entry, name="LitReview Local"):
        data = json.loads(Path(config_path).read_text())
        data.setdefault("mcpServers", {})[name] = entry
        return json.dumps(data, indent=2)

    def install(config_path, entry, name="LitReview Local"):
        calls.append(("install", str(config_path)))
        Path(config_path).write_text(preview(config_path, entry, name))
        return {"action": "added", "backup": str(config_path) + ".bak", "changed": True}

    module.find_desktop_config = lambda: config
    module.desktop_config_candidates = lambda: [config]
    module.desktop_entry = desktop_entry
    module.preview_desktop_change = preview
    module.install_desktop_entry = install
    module.read_desktop_entry = lambda path, name="LitReview Local": (
        json.loads(Path(path).read_text()).get("mcpServers", {}).get(name))
    module.claude_code_command = (
        lambda port: f"claude mcp add --transport http litreview http://127.0.0.1:{port}/mcp")
    return module


def _use_fake_setup(monkeypatch, config: Path, calls: list | None = None):
    import litreview_local

    fake = _fake_setup(config, calls if calls is not None else [])
    monkeypatch.setitem(sys.modules, "litreview_local.claude_setup", fake)
    monkeypatch.setattr(litreview_local, "claude_setup", fake, raising=False)
    return fake


def test_getting_started_shows_the_steps_and_writes_on_request(client, monkeypatch, tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    calls: list = []
    _use_fake_setup(monkeypatch, config, calls)

    page = html.unescape(client.get("/start").text)
    for words in ("Getting started", "Your email", "Connect Claude",
                  "Quit Claude Desktop completely", "Add LitReview to Claude Desktop",
                  "Open Claude Desktop again", "Test the connection", "Advanced setup",
                  "Ask your first question", "Create project", SAY):
        assert words in page, words
    # Getting started is only the list: the rest is on Advanced setup.
    assert "Edit Config" not in page
    # Step 2 is Claude Code: one command for every folder, then a new session.
    assert "Connect Claude Code" in page and "Connect Claude Desktop" in page
    assert ("claude mcp add --transport http --scope user litreview "
            "http://127.0.0.1:8799/mcp") in page
    assert "Open a new Claude Code session" in page
    assert page.index("Connect Claude Code") < page.index("Connect Claude Desktop") < \
        page.index("Ask your first question")
    assert 'href="/start/advanced"' in page
    # Test first, then quit Claude Desktop, add LitReview, and open it again.
    desktop = page[page.index('id="claude"'):page.index("Advanced setup")]
    steps = [desktop.index(words) for words in (
        "Test the connection", "Quit Claude Desktop completely",
        "Add LitReview to Claude Desktop", "Open Claude Desktop again")]
    assert steps == sorted(steps)
    assert "Claude Desktop lists it as “LitReview Local”." in desktop
    advanced = html.unescape(client.get("/start/advanced").text)
    for words in ("Advanced setup", "Add LitReview to Claude Desktop by hand", "Edit Config",
                  "Claude Code, other ways", "Take LitReview out of Claude Desktop",
                  "If Claude Desktop does not connect", str(config), "claude.ai"):
        assert words in advanced, words
    assert "LitReview is not in Claude Desktop's settings." in advanced
    # The entry, as it will be written (JSON-escaped), to add by hand.
    # The bridge runs on local/.venv's Python when there is one, else this one.
    assert json.dumps(_bridge_python())[1:-1] in advanced and "mcp_bridge.py" in advanced
    assert "claude mcp add --transport http litreview http://127.0.0.1:8799/mcp" in advanced
    assert str(LOCAL_DIR) in advanced
    assert '"other"' not in advanced        # Claude Desktop's own settings are not shown
    assert not any(c[0] == "install" for c in calls)  # looking writes nothing
    assert calls[0][0] == "entry" and calls[0][3] == settings.port

    written = client.post("/connect/desktop")
    text = html.unescape(written.text)
    assert "Added LitReview to Claude Desktop" in text and "press Test the connection" in text
    # The message shows in step 2, where the button was.
    assert text.index('id="claude"') < text.index("Added LitReview") < text.index('id="first"')
    assert ("install", str(config)) in calls
    servers = json.loads(config.read_text())["mcpServers"]
    assert "LitReview Local" in servers and "other" in servers
    again = html.unescape(client.get("/start").text)
    assert "LitReview is in Claude Desktop's settings, as “LitReview Local”." in again
    assert '<form method="post" action="/connect/desktop">' not in again
    assert 'action="/connect/desktop/remove"' in client.get("/start/advanced").text


def test_the_old_connect_page_is_part_of_getting_started(client):
    moved = client.get("/connect", follow_redirects=False)
    assert moved.status_code == 303 and moved.headers["location"] == "/start#claude-code"
    assert 'href="/start"' in client.get("/").text            # in the menu
    assert 'href="/start#claude-code"' in client.get("/settings").text


def test_getting_started_saves_the_email_alone(client, user_settings):
    settings.save_user_settings({"s2_api_key": "k-1"})
    refused = client.post("/start/email", data={"contact_email": "nope"})
    assert "does not look like an email address" in refused.text
    assert "Type your email address first" in client.post(
        "/start/email", data={"contact_email": " "}).text
    saved = html.unescape(client.post("/start/email",
                                      data={"contact_email": "me@uni.example"}).text)
    assert "Saved: me@uni.example. Next, connect Claude." in saved
    assert 'value="me@uni.example"' in saved
    values = settings.user_settings()
    assert values["contact_email"] == "me@uni.example" and values["s2_api_key"] == "k-1"


def test_the_desktop_test_says_what_is_wrong_and_what_to_do(client, monkeypatch, tmp_path):
    from litreview_local import web

    config = tmp_path / "claude_desktop_config.json"
    config.write_text("{}")
    fake = _use_fake_setup(monkeypatch, config)
    bridge = {"ok": True, "port": 8799}
    started = []
    monkeypatch.setattr(web, "_bridge_check", lambda entry: started.append(entry) or bridge)

    def test() -> str:
        return _flat(client.post("/connect/test/desktop", headers=HX).text)

    # The test comes first: before anything is added it checks the program
    # Claude Desktop will start, then says what to do next.
    first = test()
    assert "The program Claude Desktop will start for LitReview works." in first
    assert "LitReview is not in Claude Desktop's settings yet." in first
    assert "Next: quit Claude Desktop, press Add LitReview to Claude Desktop" in first
    assert "test-next" in first and "check-bad" not in first and "hx-get" not in first
    assert started and started[-1]["args"][0].endswith("mcp_bridge.py")
    assert config.read_text() == "{}"                   # testing wrote nothing
    # A program that does not work shows up before anything is added.
    bridge = {"ok": False, "problem": "It stopped with an error: boom"}
    broken = test()
    assert "will start for LitReview does not work" in broken and "boom" in broken
    assert "would not help until this works" in broken and "check-bad" in broken
    bridge = {"ok": True, "port": 8799}
    # Added here, then gone: Claude Desktop rewrote its settings while it was open.
    client.post("/connect/desktop")
    config.write_text("{}")
    gone = test()
    assert "not in Claude Desktop's settings any more" in gone
    assert "rewrote them while it was still open" in gone
    config.write_text(json.dumps({"mcpServers": {"LitReview Local": {"command": "elsewhere"}}}))
    assert "point at another copy of LitReview" in test()
    config.write_text("{not json")
    broken = test()
    assert "cannot be read" in broken and "check-bad" in broken
    monkeypatch.setattr(fake, "find_desktop_config", lambda: None)
    assert "Claude Desktop is not installed" in test()
    assert client.post("/connect/test/nothing", headers=HX).status_code == 404


def test_the_desktop_test_checks_the_bridge_then_waits_for_claude_desktop(
        client, monkeypatch, tmp_path):
    from conftest import Claude
    from litreview_local import web

    config = tmp_path / "claude_desktop_config.json"
    config.write_text("{}")
    _use_fake_setup(monkeypatch, config)
    client.post("/connect/desktop")

    started = []
    monkeypatch.setattr(web, "_bridge_check", lambda entry: started.append(entry) or {
        "ok": False, "problem": "It stopped with an error: boom"})
    broken = html.unescape(client.post("/connect/test/desktop", headers=HX).text)
    assert "does not work" in broken and "boom" in broken
    assert "LitReview is in Claude Desktop's settings." in broken
    assert started and started[0]["args"][0].endswith("mcp_bridge.py")

    monkeypatch.setattr(web, "_bridge_check", lambda entry: {"ok": True, "port": 8799})
    waiting = html.unescape(client.post("/connect/test/desktop", headers=HX).text)
    assert "The program Claude Desktop starts for LitReview works." in waiting
    assert "Waiting for Claude Desktop to call in" in waiting
    assert "Use LitReview Local to list my projects" in waiting
    since = re.search(r'hx-get="/connect/test/desktop\?since=(\d+)"', waiting).group(1)

    def poll(start=since):
        return client.get(f"/connect/test/desktop?since={start}", headers=HX)

    assert poll().status_code == 204         # still waiting: the page stays as it is
    # Stop waiting: one last look, and no more asking.
    assert "Stop waiting" in waiting and f"since={since}&stop=1" in waiting
    cancelled = _flat(client.get(f"/connect/test/desktop?since={since}&stop=1",
                                 headers=HX).text)
    assert "Stopped waiting for Claude Desktop." in cancelled and "hx-get" not in cancelled
    assert "The program Claude Desktop starts for LitReview works." in cancelled
    stopped = html.unescape(poll(str(int(models.utcnow().timestamp()) - 11 * 60)).text)
    assert "Claude Desktop has not called in for ten minutes" in stopped
    assert "hx-get" not in stopped
    config.write_text("{}")                  # Claude Desktop rewrote its settings
    assert "not in Claude Desktop's settings any more" in html.unescape(poll().text)
    client.post("/connect/desktop")

    Claude(client, name="claude-code")       # Claude Code is not Claude Desktop
    assert poll().status_code == 204
    Claude(client, name="claude-ai")
    done = html.unescape(poll().text)
    assert "The connection works." in done and "Claude Desktop called in" in done
    assert "hx-get" not in done              # it stops asking
    page = html.unescape(client.get("/start").text)
    assert "Claude Desktop last called in" in page


def test_the_claude_code_test_waits_for_claude_code(client):
    from conftest import Claude

    waiting = html.unescape(client.post("/connect/test/code", headers=HX).text)
    assert "Waiting for Claude Code to call in" in waiting and "/mcp" in waiting
    since = re.search(r"since=(\d+)", waiting).group(1)
    assert client.get(f"/connect/test/code?since={since}", headers=HX).status_code == 204
    stopped = _flat(client.get(f"/connect/test/code?since={since}&stop=1", headers=HX).text)
    assert "Stopped waiting for Claude Code." in stopped and "hx-get" not in stopped
    Claude(client, name="claude-code")
    done = html.unescape(client.get(f"/connect/test/code?since={since}", headers=HX).text)
    assert "The connection works." in done and "Claude Code called in" in done


def test_the_bridge_check_runs_what_claude_desktop_runs(client, tmp_path):
    import http.server
    import threading

    from litreview_local import web

    answer = {"ok": True, "app": "litreview-local", "version": "0.1.0",
              "data_dir": str(settings.data_dir)}

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(answer).encode()
            self.send_response(200 if self.path == "/health" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    entry = {"command": sys.executable, "args": [str(web.BRIDGE_PATH)],
             "env": {"LITREVIEW_PORT": str(port),
                     "LITREVIEW_DATA_DIR": str(settings.data_dir)}}
    try:
        assert web._bridge_check(entry) == {"ok": True, "port": port}
        answer["data_dir"] = str(tmp_path / "another library")
        other = web._bridge_check(entry)
        assert not other["ok"] and "another copy of LitReview" in other["problem"]
    finally:
        server.shutdown()
        server.server_close()
    closed = web._bridge_check(entry)
    assert not closed["ok"] and "not answering" in closed["problem"]
    missing = web._bridge_check({**entry, "command": str(tmp_path / "no-python.exe")})
    assert not missing["ok"] and "not there" in missing["problem"]


def test_a_session_stays_claude_desktop_across_a_restart(client):
    from conftest import Claude
    from litreview_local import mcp_server

    desktop = Claude(client, name="claude-ai")
    # What a restart forgets, with Claude Desktop (and its session) still open.
    mcp_server._clients.clear()
    mcp_server._looked_up.clear()
    tasks._seen_cache.clear()
    db = SessionLocal()
    try:
        db.query(models.AppState).filter_by(key="claude_last_seen").delete()
        db.commit()
    finally:
        db.close()
    desktop.call("list_projects")
    db = SessionLocal()
    try:
        assert tasks.last_seen(db)["client"] == "claude-ai"
    finally:
        db.close()


def test_getting_started_with_the_real_helper_writes_a_real_config(client, monkeypatch,
                                                                    tmp_path):
    from litreview_local import claude_setup, web

    config = tmp_path / "Claude" / "claude_desktop_config.json"
    config.parent.mkdir()
    monkeypatch.setattr(claude_setup, "find_desktop_config", lambda **kw: config)
    monkeypatch.setattr(web, "_bridge_check", lambda entry: {"ok": True, "port": 8799})
    # What an earlier version added, as "litreview".
    config.write_text(json.dumps({"mcpServers": {"litreview": {
        "command": sys.executable, "args": [str(tmp_path / "old" / "mcp_bridge.py")]}}}),
        encoding="utf-8")
    page = _flat(client.get("/start").text)
    assert "Add LitReview to Claude Desktop" in page
    assert "under its old name, “litreview”: this renames it “LitReview Local”" in page
    tested = _flat(client.post("/connect/test/desktop", headers=HX).text)
    assert "under its old name, “litreview”" in tested and "test-next" in tested
    added = _flat(client.post("/connect/desktop").text)
    assert "now called “LitReview Local” in Claude Desktop's settings" in added
    servers = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
    assert list(servers) == ["LitReview Local"]          # the old name is gone
    entry = servers["LitReview Local"]
    assert entry["args"][0].endswith("mcp_bridge.py")
    assert Path(entry["command"]).name.lower().startswith("python")
    # Not on the default port or library: the bridge is told where they are.
    assert entry["env"]["LITREVIEW_PORT"] == str(settings.port)
    assert entry["env"]["LITREVIEW_DATA_DIR"] == str(settings.data_dir)
    assert "LitReview is in Claude Desktop's settings, as “LitReview Local”." in _flat(
        client.get("/start").text)
    removed = _flat(client.post("/connect/desktop/remove").text)
    assert "taken out of Claude Desktop" in removed
    # Back on Advanced setup, with the message in the pane the button was in.
    assert removed.index('id="remove"') < removed.index("taken out of Claude Desktop")
    assert "LitReview is not in Claude Desktop's settings." in removed
    assert "mcpServers" not in json.loads(config.read_text(encoding="utf-8"))


def test_getting_started_without_the_helper_explains_the_manual_way(client, monkeypatch):
    import litreview_local

    monkeypatch.setitem(sys.modules, "litreview_local.claude_setup", None)
    monkeypatch.delattr(litreview_local, "claude_setup", raising=False)
    from conftest import Claude
    from litreview_local import web

    page = client.get("/start")
    assert page.status_code == 200
    assert "Settings → Developer → Edit Config" in page.text
    assert "mcp_bridge.py" in page.text and "mcpServers" in page.text
    assert "LitReview Local" in page.text
    assert page.text.index("Test the connection") < page.text.index("Edit Config")
    advanced = client.get("/start/advanced")
    assert advanced.status_code == 200 and "take the" in advanced.text
    refused = client.post("/connect/desktop")
    assert "by hand" in refused.text
    monkeypatch.setattr(web, "_bridge_check", lambda entry: {"ok": True, "port": 8799})
    tested = _flat(client.post("/connect/test/desktop", headers=HX).text)
    assert "cannot read Claude Desktop's settings" in tested and "test-next" in tested
    assert "The program Claude Desktop will start for LitReview works." in tested
    Claude(client, name="claude-ai")         # added by hand, and Claude Desktop called
    assert "The connection works." in _flat(
        client.post("/connect/test/desktop", headers=HX).text)


def test_each_claude_app_calling_in_marks_its_own_step_done(client):
    from conftest import Claude

    def steps():
        page = html.unescape(client.get("/start").text)
        code = page[page.index('id="claude-code"'):page.index('id="claude"')]
        desktop = page[page.index('id="claude"'):page.index('id="first"')]
        return code, desktop

    code, desktop = steps()
    assert "Done" not in code and "Done" not in desktop
    Claude(client, name="claude-code")
    code, desktop = steps()
    assert "Done" in code and "Claude Code last called in" in code
    assert "Done" not in desktop and "Only if you want LitReview in Claude Desktop" in desktop
    Claude(client, name="claude-ai")
    code, desktop = steps()
    assert "Done" in desktop and "Claude Desktop last called in" in desktop


# --------------------------------------------------------- the guard

def test_a_post_from_another_site_is_refused(client):
    refused = client.post("/projects", data={"name": "Planted"},
                          headers={"Origin": "https://evil.example"})
    assert refused.status_code == 403
    assert _rows(models.Project, name="Planted") == []
    ok = client.post("/projects", data={"name": "Mine"},
                     headers={"Origin": "http://127.0.0.1:8799"})
    assert ok.status_code == 200 and _rows(models.Project, name="Mine")


# ------------------------------------------------------ copy as text

COPY_CASES = [
    # A run after the full stop: merged, deduplicated, put before it.
    ([("t", "Remote work helps wellbeing. "), ("c", "(A, 2021)"), ("t", " "),
      ("c", "(A, 2021)"), ("t", " "), ("c", "(B, 2022)")],
     "Remote work helps wellbeing (A, 2021; B, 2022)."),
    # Placed right already; two sentences.
    ([("t", "First "), ("c", "(A, 2021)"), ("t", ". Second claim "), ("c", "(B, 2022)"),
      ("t", ".")], "First (A, 2021). Second claim (B, 2022)."),
    # Mid-sentence citations keep their commas.
    ([("t", "X rose "), ("c", "(A, 2021)"), ("t", ", Y fell "), ("c", "(B, 2022)"),
      ("t", ".")], "X rose (A, 2021), Y fell (B, 2022)."),
    # A quotation keeps its closing quote; the full stop moves.
    ([("t", "They wrote “it works.” "), ("c", "(A, 2021)")], "They wrote “it works” (A, 2021)."),
    # "et al." is not the end of the sentence when it carries on.
    ([("t", "Smith et al. "), ("c", "(S, 2020)"), ("t", " found more.")],
     "Smith et al. (S, 2020) found more."),
    # Cited after the full stop, followed by another full stop.
    ([("t", "Done. "), ("c", "(A, 2021)"), ("t", ".")], "Done (A, 2021)."),
    # Paragraphs stay apart; a citation does not join across them.
    ([("t", "One. "), ("c", "(A, 2021)"), ("t", "\n\n"), ("c", "(B, 2022)"),
      ("t", " opens the second.")], "One (A, 2021).\n\n(B, 2022) opens the second."),
]


def test_copy_as_text_puts_citations_before_the_full_stop(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    cases = [{"parts": [{"text": v} if k == "t" else {"cite": v} for k, v in parts],
              "want": want} for parts, want in COPY_CASES]
    script = tmp_path / "check.js"
    script.write_text(
        "const t = require(process.argv[2]);\n"
        "const cases = JSON.parse(require('fs').readFileSync(process.argv[3], 'utf8'));\n"
        "const bad = cases.map(c => [c.want, t.formatCited(c.parts)])"
        ".filter(([w, g]) => w !== g);\n"
        # The answer editor as the browser has it: paragraphs, emphasis, cites.
        "function el(tag, ...kids) {\n"
        "  const node = {nodeType: 1, tagName: tag, firstChild: null, nextSibling: null};\n"
        "  let prev = null;\n"
        "  for (const k of kids) {\n"
        "    const c = typeof k === 'string' ? {nodeType: 3, nodeValue: k, textContent: k} : k;\n"
        "    if (prev) prev.nextSibling = c; else node.firstChild = c;\n"
        "    prev = c;\n"
        "  }\n"
        "  node.textContent = kids.map(k => typeof k === 'string' ? k : k.textContent).join('');\n"
        "  return node;\n"
        "}\n"
        "const editor = el('DIV',\n"
        "  el('P', 'Remote work helps. ', el('CITE', '(A, 2021)'), ' ', el('CITE', '(A, 2021)')),\n"
        "  el('P', 'Second ', el('STRONG', 'point'), ' ', el('CITE', '(B, 2022)'), '.'));\n"
        "const want = 'Remote work helps (A, 2021).\\n\\nSecond point (B, 2022).';\n"
        "if (t.answerText(editor) !== want) bad.push([want, t.answerText(editor)]);\n"
        "const spoken = 'Remote work helps.\\n\\nSecond point.';\n"
        "if (t.spokenText(editor) !== spoken) bad.push([spoken, t.spokenText(editor)]);\n"
        "process.stdout.write(JSON.stringify(bad));\n", encoding="utf-8")
    data = tmp_path / "cases.json"
    data.write_text(json.dumps(cases), encoding="utf-8")
    result = subprocess.run([node, str(script), str(STATIC / "answer_text.js"), str(data)],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [], result.stdout


# ---------------------------------------------------------- download next

def test_download_next_puts_what_matters_to_most_questions_first(client, tmp_path):
    project_id = _new_project(client)
    db = SessionLocal()
    try:
        q1 = models.ResearchQuestion(project_id=project_id, text="First question?", number=1)
        q2 = models.ResearchQuestion(project_id=project_id, text="Second question?", number=2)
        db.add_all([q1, q2])
        db.flush()
        s1 = models.Search(project_id=project_id, research_question_id=q1.id, query="a")
        s1b = models.Search(project_id=project_id, research_question_id=q1.id, query="b")
        s2 = models.Search(project_id=project_id, research_question_id=q2.id, query="c")
        db.add_all([s1, s1b, s2])
        db.flush()

        def paper(title, **fields):
            p = models.Paper(project_id=project_id, title=title, authors=["Ann Author"],
                             year=2022, **fields)
            db.add(p)
            db.flush()
            return p

        one = paper("Strong for one question", doi="10.1/one",
                    pdf_status=models.PDF_FAILED, pdf_error="HTTP 403")
        both = paper("Good for both questions")
        weak = paper("Tangential everywhere")
        have = paper("Already in hand", pdf_status=models.PDF_DOWNLOADED)
        out = paper("Excluded by me", status=models.STATUS_EXCLUDED)
        for search, p, score in ((s1, one, 98), (s2, one, 20), (s1, both, 72),
                                 (s1b, both, 80), (s2, both, 75), (s1, weak, 60),
                                 (s2, weak, 65), (s1, have, 95), (s1, out, 99)):
            db.add(models.PaperScore(search_id=search.id, paper_id=p.id, score=score,
                                     reason=f"Reason for {p.title}"))
        db.commit()
        one_id = one.id
    finally:
        db.close()

    text = _flat(client.get(f"/projects/{project_id}").text)
    side = text[text.index('id="to-get"'):]
    # Two questions before one: 80 (its better score for RQ1) + 75 = 155, then 98.
    assert side.index("Good for both questions") < side.index("Strong for one question")
    assert ">155<" in side and "RQ1 80" in side and "RQ2 75" in side
    assert "RQ1 98" in side and "RQ2 20" in side          # a low score shows, muted
    assert "Claude: Reason for Good for both questions" in side
    for absent in ("Tangential everywhere", "Already in hand", "Excluded by me"):
        assert absent not in side
    assert "The download failed" in side and 'href="https://doi.org/10.1/one"' in side
    assert f'action="/papers/{one_id}/upload-pdf"' in side
    assert "Try the automatic download for the 1 article not tried yet" in side

    # A PDF uploaded from the list takes the article off it.
    pdf = _pdf_bytes(tmp_path, ["Strong for one question"] + BODY)
    sent = client.post(f"/papers/{one_id}/upload-pdf",
                       data={"next": f"/projects/{project_id}#to-get"},
                       files={"file": ("one.pdf", pdf, "application/pdf")})
    assert sent.url.path == f"/projects/{project_id}" and "Added the PDF" in sent.text
    after = _flat(sent.text)
    assert "Strong for one question" not in after[after.index('id="to-get"'):]


def test_download_next_says_what_will_show_when_there_is_nothing(client):
    project_id = _new_project(client)
    text = _flat(client.get(f"/projects/{project_id}").text)
    assert "Download next" in text and "Nothing to get right now" in text


def test_the_project_status_updates_itself_and_stops_when_done(client, monkeypatch):
    project_id = _new_project(client)
    working = ["reading the PDF you uploaded: “A paper”"]
    monkeypatch.setattr(tasks, "background_activity",
                        lambda db, project_id=None: list(working))
    page = client.get(f"/projects/{project_id}").text
    assert "LitReview is working" in page and "reading the PDF you uploaded" in page
    seen = re.search(rf'hx-get="/projects/{project_id}/status\?seen=([0-9a-f]+)"',
                     page).group(1)
    status = f"/projects/{project_id}/status?seen={seen}"
    assert client.get(status, headers=HX).status_code == 204      # nothing changed
    working.clear()                                                # the work is done
    done = client.get(status, headers=HX)
    assert done.status_code == 200
    assert "LitReview is working" not in done.text and "hx-get" not in done.text


def test_an_upload_from_download_next_says_what_claude_has_to_do(client, tmp_path):
    project_id = _new_project(client)
    question_id = _ask(client, project_id)
    db = SessionLocal()
    try:
        rq = db.get(models.ResearchQuestion, question_id)
        search = models.Search(project_id=project_id, research_question_id=rq.id, query="q")
        paper = models.Paper(project_id=project_id, title="A paper worth reading",
                             authors=["Ann Author"], year=2022)
        db.add_all([search, paper])
        db.flush()
        search.papers.append(paper)
        db.add(models.PaperScore(search_id=search.id, paper_id=paper.id, score=90))
        db.commit()
        paper_id = paper.id
    finally:
        db.close()
    pdf = _pdf_bytes(tmp_path, ["A paper worth reading"] + BODY)
    sent = client.post(f"/papers/{paper_id}/upload-pdf",
                       data={"next": f"/projects/{project_id}#to-get"},
                       files={"file": ("paper.pdf", pdf, "application/pdf")})
    text = _flat(sent.text)
    assert "Added the PDF of “A paper worth reading”" in text
    assert "Claude reads it for RQ1 when you say" in text
    # The reading is queued for Claude, and the top of the page says what to say.
    assert _live(paper_id=paper_id) == ["annotate_paper"]
    status = text[text.index('id="project-status"'):]
    assert "waiting for Claude" in status and SAY in status

