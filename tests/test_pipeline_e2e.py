"""A research question from start to cited answer, with a fake Claude.

The sources and the PDF downloads are replaced by fakes; everything else -
the queue, the state machine, text extraction, highlighting, citations and
the MCP server - is the real thing, driven the way Claude drives it.
"""
import re

import pytest

from litreview_local import models, pdfs, research, tasks
from litreview_local.db import SessionLocal
from litreview_local.pipeline import SOURCES

SURNAMES = ["Andersen", "Berg", "Carlsen", "Dahl", "Eriksen"]
PAPERS = [
    {"title": f"Remote work and wellbeing study {n}",
     "doi": f"10.1234/rw.{n}", "year": 2018 + n, "venue": "Journal of Work",
     "authors": [f"Ann {SURNAMES[n - 1]}", "Bo Second"], "citation_count": 10 * n,
     "abstract": f"We study remote work and employee wellbeing, sample {n}.",
     "url": f"https://example.org/{n}", "oa_url": f"https://example.org/{n}.pdf",
     "source_ids": {"openalex": f"W{n}"}, "issns": [], "work_type": "article"}
    for n in range(1, 6)
]

# What every fake PDF says. One sentence per line so the highlighter's
# search finds each quote on one line.
BODY = [
    "Remote work improved reported wellbeing by twelve percent.",
    "Employees working from home slept longer on average.",
    "Isolation was the main cost of remote arrangements.",
    "Managers reported no loss of productivity overall.",
]


class FakeSource:
    def __init__(self, records):
        self.records = records

    def search(self, query, max_results=50, year_from=None, year_to=None):
        return [dict(r, source="openalex") for r in self.records][:max_results]


def make_pdf(path, lines, *, text_layer=True):
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page()
    if text_layer:
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=10)
            y += 16
    else:
        page.draw_rect(fitz.Rect(72, 72, 300, 200), color=(0, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


@pytest.fixture
def fakes(monkeypatch):
    for name in ("openalex", "semantic_scholar", "crossref"):
        monkeypatch.setitem(SOURCES, name, FakeSource(PAPERS))

    def download(paper, client=None):
        make_pdf(pdfs.original_path(paper.project.slug, paper.id),
                 [paper.title] + BODY)
        return True, None

    monkeypatch.setattr(pdfs, "download_pdf", download)


def work_queue(claude, *, quote=BODY[0], summary_with_key=True, limit=50):
    """Do every task the queue hands out, the way a well-behaved Claude
    would. Returns the kinds done, in order."""
    done = []
    for _ in range(limit):
        task = claude.call("next_task")
        if task.get("task") is None and "task_id" not in task:
            return done
        kind = task["kind"]
        data = task["input"]
        if kind == "parse_question":
            output = {"concept_groups": [["remote work", "telework"],
                                         ["wellbeing", "well-being"]],
                      "query": '"remote work" wellbeing',
                      "headline": "Remote work and wellbeing",
                      "keywords": ["remote work", "wellbeing"]}
        elif kind == "rank_papers":
            output = {"rankings": [{"id": p["id"], "score": 90 - i,
                                    "reason": "On topic."}
                                   for i, p in enumerate(data["papers"])]}
        elif kind == "annotate_paper":
            assert quote in data["text"]
            output = {"findings": [{"title": "Wellbeing effect", "quote": quote,
                                    "page": 1, "why": "Supports a positive effect."}]}
        elif kind == "synthesize":
            keys = re.findall(r"\[CIT:([0-9a-f-]+)\]", data["evidence"])
            assert keys
            text = "Remote work helps wellbeing."
            if summary_with_key:
                text += f" [CIT:{keys[0]}]"
            output = {"summary": text}
        else:
            raise AssertionError(f"unexpected task {kind}")
        result = claude.call("submit_task", task_id=task["task_id"], output=output)
        assert result["accepted"]
        done.append(kind)
    raise AssertionError("the queue never emptied")


def test_question_runs_end_to_end(claude, fakes):
    project = claude.call("create_project", name="Remote work review")
    started = claude.call("ask_question", project_id=project["project_id"],
                          question="Does remote work improve employee wellbeing?")
    assert started["label"] == "RQ1"

    done = work_queue(claude)
    assert done[0] == "parse_question"
    assert done.count("rank_papers") == 1
    assert done.count("annotate_paper") == 5  # Z defaults to 6, 5 papers exist
    assert done[-1] == "synthesize"

    answer = claude.call("get_answer", question_id=started["question_id"])
    entry = answer["answers"][0]
    assert entry["status"] == "Done"
    assert "[CIT:" in entry["answer"]
    citation = entry["citations"][0]
    assert citation["located"] == "pdf"
    assert citation["quote"] == BODY[0]
    assert answer["papers"]  # the reference list, once

    highlights = claude.call("get_highlights", question_id=started["question_id"])
    assert len(highlights["papers"]) == 5
    first = highlights["papers"][0]["highlights"][0]
    assert first["located"] == "pdf"
    assert "slept longer" in first["context"]  # the sentence after the quote

    # The highlighted copy exists for every annotation.
    db = SessionLocal()
    try:
        for annotation in db.query(models.Annotation).all():
            assert pdfs.annotation_path(annotation.paper.project.slug,
                                        annotation.id).exists()
    finally:
        db.close()


def test_invalid_answers_are_refused_with_the_reason(claude, fakes):
    project = claude.call("create_project", name="Refusals")
    claude.call("ask_question", project_id=project["project_id"],
                question="Does remote work improve employee wellbeing?",
                concept_groups=[["remote work"], ["wellbeing"]],
                query="remote work wellbeing", headline="Remote work")
    task = claude.call("next_task")
    assert task["kind"] == "rank_papers"
    ids = [p["id"] for p in task["input"]["papers"]]

    message = claude.error("submit_task", task_id=task["task_id"],
                           output={"rankings": [{"id": ids[0], "score": 80,
                                                 "reason": "x"}]})
    assert "have no score" in message
    message = claude.error("submit_task", task_id=task["task_id"],
                           output={"rankings": "nope"})
    assert "should be array" in message

    # The task is still Claude's, and a good answer goes through.
    claude.call("submit_task", task_id=task["task_id"], output={
        "rankings": [{"id": i, "score": 70, "reason": "ok"} for i in ids]})


def test_a_paraphrase_is_sent_back_once(claude, fakes):
    project = claude.call("create_project", name="Quotes")
    claude.call("ask_question", project_id=project["project_id"],
                question="Does remote work improve employee wellbeing?",
                concept_groups=[["remote work"], ["wellbeing"]],
                query="remote work wellbeing", headline="Remote work")
    task = claude.call("next_task")
    claude.call("submit_task", task_id=task["task_id"], output={
        "rankings": [{"id": p["id"], "score": 70, "reason": "ok"}
                     for p in task["input"]["papers"]]})
    task = claude.call("next_task", kind="annotate_paper")
    paraphrase = {"findings": [{"title": "t", "quote": "Working remotely made "
                                "people about 12% happier.", "page": 1,
                                "why": "w"}]}
    message = claude.error("submit_task", task_id=task["task_id"], output=paraphrase)
    assert "not in the text as given" in message
    assert "does not appear in the text at all" in message
    # Insisting is accepted - but a quote that is not in the paper is never
    # stored, so nothing invented can be cited.
    result = claude.call("submit_task", task_id=task["task_id"], output=paraphrase)
    assert "dropped because their quotes are not in the paper's text" in result["saved"]
    paper_id = task["input"]["paper"]["paper_id"]
    paper = claude.call("get_paper", paper_id=paper_id)
    assert paper["annotations"][0]["highlights"] == []


def test_a_quote_with_an_invented_ending_is_refused(claude, fakes):
    """A real case: the right opening, then words the paper never had."""
    project = claude.call("create_project", name="Invented")
    started = claude.call("ask_question", project_id=project["project_id"],
                          question="Does remote work improve employee wellbeing?")
    work_queue(claude)
    paper = claude.call("search_papers", question_id=started["question_id"])["papers"][0]
    refused = claude.error(
        "add_evidence", question_id=started["question_id"],
        paper_id=paper["paper_id"],
        quote=BODY[0] + " It should never be tried with fewer than 50,000 people.",
        why="w")
    # Says where it parted company with the paper.
    assert re.search(r"its first \d+ of \d+ characters match the text", refused)
    assert "fewer than 50,000" in refused
    # A near-miss is offered back as the paper has it.
    refused = claude.error(
        "add_evidence", question_id=started["question_id"],
        paper_id=paper["paper_id"],
        quote="Remote work improved reported wellbeing by twelve per cent.",
        why="w")
    assert f'"{BODY[0]}"' in refused


def test_add_evidence_checks_the_quote(claude, fakes):
    project = claude.call("create_project", name="Evidence")
    started = claude.call("ask_question", project_id=project["project_id"],
                          question="Does remote work improve employee wellbeing?")
    work_queue(claude)
    paper = claude.call("search_papers", question_id=started["question_id"])["papers"][0]

    refused = claude.error("add_evidence", question_id=started["question_id"],
                           paper_id=paper["paper_id"],
                           quote="Managers saw productivity fall sharply overall.",
                           why="contradicts")
    assert "does not appear" in refused
    assert "Managers reported no loss of productivity overall." in refused

    added = claude.call("add_evidence", question_id=started["question_id"],
                        paper_id=paper["paper_id"], quote=BODY[3],
                        why="No productivity cost.")
    assert added["located"] == "pdf"
    key = added["citation_key"]
    saved = claude.call("save_answer", question_id=started["question_id"],
                        text=f"Productivity held up. [CIT:{key}]")
    assert saved["saved"]
    answer = claude.call("get_answer", question_id=started["question_id"])
    assert answer["answers"][0]["citations"][0]["key"] == key

    refused = claude.error("save_answer", question_id=started["question_id"],
                           text="Made up. [CIT:999-abcdef12]")
    assert "not in this question's evidence" in refused


def test_tags_and_screening_through_mcp(claude, fakes):
    project = claude.call("create_project", name="Tags")
    started = claude.call("ask_question", project_id=project["project_id"],
                          question="Does remote work improve employee wellbeing?")
    work_queue(claude)
    papers = claude.call("search_papers", question_id=started["question_id"])["papers"]
    ids = [p["paper_id"] for p in papers[:2]]
    claude.call("tag_papers", paper_ids=ids, add=["Method: DiD", "to read"])
    claude.call("tag_papers", paper_ids=ids[:1], add=["method: did"])  # same tag
    listed = claude.call("list_tags", project_id=project["project_id"])["tags"]
    assert {t["name"]: t["count"] for t in listed} == {"Method: DiD": 2, "to read": 2}
    found = claude.call("search_papers", project_id=project["project_id"],
                        tag="METHOD: did")
    assert {p["paper_id"] for p in found["papers"]} == set(ids)

    claude.call("set_screening", paper_ids=ids[:1], status="excluded")
    found = claude.call("search_papers", project_id=project["project_id"],
                        tag="to read")
    assert [p["paper_id"] for p in found["papers"]] == ids[1:]
    claude.call("tag_papers", paper_ids=ids, remove=["to read"])
    listed = claude.call("list_tags", project_id=project["project_id"])["tags"]
    assert [t["name"] for t in listed] == ["Method: DiD"]


def test_a_scan_is_parsed_by_claude_before_it_is_read(claude, fakes, monkeypatch):
    def scanned(paper, client=None):
        make_pdf(pdfs.original_path(paper.project.slug, paper.id), [],
                 text_layer=False)
        return True, None

    monkeypatch.setattr(pdfs, "download_pdf", scanned)
    monkeypatch.setitem(SOURCES, "openalex", FakeSource(PAPERS[:1]))
    monkeypatch.setitem(SOURCES, "semantic_scholar", FakeSource([]))
    monkeypatch.setitem(SOURCES, "crossref", FakeSource([]))
    project = claude.call("create_project", name="Scans")
    started = claude.call("ask_question", project_id=project["project_id"],
                          question="Does remote work improve employee wellbeing?",
                          concept_groups=[["remote work"], ["wellbeing"]],
                          query="remote work", headline="Remote work")
    task = claude.call("next_task")
    claude.call("submit_task", task_id=task["task_id"], output={
        "rankings": [{"id": p["id"], "score": 90, "reason": "ok"}
                     for p in task["input"]["papers"]]})
    task = claude.call("next_task")
    assert task["kind"] == "parse_pdf"
    assert task["input"]["pages_to_transcribe"] == [1]
    images = claude.call_raw("get_pdf_pages", task_id=task["task_id"], pages="1")
    assert any(item["type"] == "image" for item in images["content"])

    message = claude.error("submit_task", task_id=task["task_id"],
                           output={"pages": []})
    assert "missing" in message
    claude.call("submit_task", task_id=task["task_id"], output={
        "pages": [{"page": 1, "markdown": "# Results\n\n" + " ".join(BODY)}]})

    texts = claude.call("list_pdf_texts", project_id=project["project_id"])
    assert texts["papers"][0]["text"] == "Parsed by Claude"
    task = claude.call("next_task")
    assert task["kind"] == "annotate_paper"
    claude.call("submit_task", task_id=task["task_id"], output={
        "findings": [{"title": "t", "quote": BODY[1], "page": 1, "why": "w"}]})
    paper = claude.call("get_paper", paper_id=task["input"]["paper"]["paper_id"])
    highlight = paper["annotations"][0]["highlights"][0]
    # No text layer to highlight in, but found in Claude's transcription.
    assert highlight["located"] == "text"
    assert highlight["section"] == "Results"
    task = claude.call("next_task")
    assert task["kind"] == "synthesize"
    del started


def test_an_interrupted_download_resumes(claude, fakes):
    project = claude.call("create_project", name="Resume")
    started = claude.call("ask_question", project_id=project["project_id"],
                          question="Does remote work improve employee wellbeing?",
                          concept_groups=[["remote work"], ["wellbeing"]],
                          query="remote work", headline="Remote work")
    task = claude.call("next_task")
    # The app "closes" between the ranking and the downloads: the scores are
    # in, the question says downloading, nothing has been fetched.
    from litreview_local import worker

    worker.set_inline(False)
    calls = []
    monkey = worker.submit
    worker.submit = lambda fn, *a, **k: calls.append(fn.__name__)
    try:
        claude.call("submit_task", task_id=task["task_id"], output={
            "rankings": [{"id": p["id"], "score": 70, "reason": "ok"}
                         for p in task["input"]["papers"]]})
    finally:
        worker.submit = monkey
        worker.set_inline(True)
    assert calls == ["_step_download"]
    db = SessionLocal()
    try:
        rq = db.get(models.ResearchQuestion, started["question_id"])
        assert rq.status == models.RQ_QUEUED
        assert rq.run_state["step"] == "download"
    finally:
        db.close()

    stats = research.resume_all()  # what the next start does
    assert stats["resumed"] == 1
    kinds = work_queue(claude)
    assert kinds.count("annotate_paper") == 5 and kinds[-1] == "synthesize"


def test_an_abandoned_task_goes_back_to_the_queue(claude, fakes):
    project = claude.call("create_project", name="Leases")
    claude.call("ask_question", project_id=project["project_id"],
                question="Does remote work improve employee wellbeing?")
    task = claude.call("next_task")
    assert claude.call("next_task")["task"] is None  # it is taken
    db = SessionLocal()
    try:
        row = db.get(models.ClaudeTask, task["task_id"])
        row.lease_expires_at = models.utcnow()
        db.commit()
    finally:
        db.close()
    again = claude.call("next_task")
    assert again["task_id"] == task["task_id"]


def test_stopping_a_run_withdraws_its_tasks(claude, fakes):
    project = claude.call("create_project", name="Stop")
    started = claude.call("ask_question", project_id=project["project_id"],
                          question="Does remote work improve employee wellbeing?")
    db = SessionLocal()
    try:
        rq = db.get(models.ResearchQuestion, started["question_id"])
        withdrawn = research.stop_question(db, rq)
        assert withdrawn == 1
        assert rq.status == models.RQ_FAILED
        assert tasks.pending_counts(db)["total"] == 0
    finally:
        db.close()
    pending = claude.call("get_pending_work")
    assert pending["tasks_waiting"] == 0


def test_a_long_paper_comes_as_its_own_block_and_in_parts(claude, monkeypatch):
    """Paper text over a few thousand characters is a plain-text block of
    its own, and a long paper is read in parts that fit a Desktop chat."""
    from litreview_local import research

    def long_pdf(paper, client=None):
        import pymupdf as fitz

        doc = fitz.open()
        for page_no in range(12):
            page = doc.new_page()
            y = 60
            for line in BODY * 9:
                page.insert_text((50, y), f"{line} (p{page_no + 1})", fontsize=8)
                y += 11
        path = pdfs.original_path(paper.project.slug, paper.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(path))
        doc.close()
        return True, None

    monkeypatch.setattr(pdfs, "download_pdf", long_pdf)
    monkeypatch.setattr(research, "ANNOTATE_CHUNK_CHARS", 20_000)
    for name in ("openalex", "semantic_scholar", "crossref"):
        monkeypatch.setitem(SOURCES, name, FakeSource(PAPERS[:1]))
    project = claude.call("create_project", name="Long")
    claude.call("ask_question", project_id=project["project_id"],
                question="Does remote work improve employee wellbeing?",
                concept_groups=[["remote work"], ["wellbeing"]],
                query="remote work", headline="Remote work")
    task = claude.call("next_task")
    claude.call("submit_task", task_id=task["task_id"], output={
        "rankings": [{"id": p["id"], "score": 90, "reason": "ok"}
                     for p in task["input"]["papers"]]})
    task = claude.call("next_task")
    assert task["kind"] == "annotate_paper"
    assert task["_blocks"] == ["text"]  # arrived as its own block
    assert task["input"]["part"].startswith("1 of ")
    assert int(task["input"]["part"].split(" of ")[1].split(":")[0]) >= 2
    result = claude.call("submit_task", task_id=task["task_id"], output={
        "findings": [{"title": "t", "quote": BODY[0] + " (p1)", "page": 1,
                      "why": "w"}]})
    assert result["passages"] == [{"n": 1, "title": "t", "page": 1, "in_text": True}]
    assert "last part" in result["note"]
    assert "this paper" in result["saved"]
    assert "reader subagent stops here" in result["then"]
