"""Unit tests for the pieces the end-to-end tests lean on."""
import pymupdf as fitz
import pytest

from litreview_local import citations, highlight, models, paper_texts, research, services, tasks
from litreview_local.db import SessionLocal
from litreview_local.pipeline import SOURCES


# --------------------------------------------------------------- citations

@pytest.mark.parametrize("raw, placed", [
    ("Effect is large. [CIT:1-abcd1234] [CIT:2-beef5678]",
     "Effect is large [CIT:1-abcd1234] [CIT:2-beef5678]."),
    ("Effect.[CIT:1-abcd1234]\n\nNext; [CIT:3-aaaa1111] more.",
     "Effect [CIT:1-abcd1234].\n\nNext [CIT:3-aaaa1111]; more."),
    ("Twice [CIT:1-abcd1234] [CIT:1-abcd1234].", "Twice [CIT:1-abcd1234]."),
    ("Plain sentence. Another one.", "Plain sentence. Another one."),
])
def test_citations_go_before_the_full_stop(raw, placed):
    assert citations.place_citations(raw) == placed


# --------------------------------------------------------- quote location

def test_a_quote_is_found_through_ligatures_hyphens_and_markdown():
    content = ("<!-- page 1 -->\nIntro.\n\n<!-- page 2 -->\n## Results\n"
               "The eﬀect of the treat-\nment was **large** and signiﬁcant.")
    span = highlight.locate_in_text(
        content, "The effect of the treatment was large and significant.")
    assert span is not None
    assert paper_texts.page_at(content, span[0]) == 2
    assert paper_texts.section_at(content, span[0]) == "Results"
    assert highlight.locate_in_text(content, "Something never written here.") is None


# ------------------------------------------------------- heading detection

def test_headings_are_marked_in_text_layer_extraction(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "A Study of Remote Work", fontsize=20)
    page.insert_text((72, 110), "1 Introduction", fontsize=10, fontname="hebo")
    y = 130
    for _ in range(12):
        page.insert_text((72, y), "Body text about remote work and wellbeing, "
                         "set in the ordinary size of the paper.", fontsize=10)
        y += 14
    path = tmp_path / "paper.pdf"
    doc.save(str(path))
    doc.close()
    pages = paper_texts.extract_pages(path)
    assert "# A Study of Remote Work" in pages[0]
    assert "### 1 Introduction" in pages[0]
    assert "\n# Body" not in pages[0] and "## Body" not in pages[0]


# -------------------------------------------------------------- validation

def test_schema_validation_names_what_is_wrong():
    schema = {"type": "object", "required": ["items"], "properties": {
        "items": {"type": "array", "items": {
            "type": "object", "required": ["id"],
            "properties": {"id": {"type": "integer"}}}}}}
    assert tasks.validate(schema, {"items": [{"id": 3}, {"id": 4.0}]}) == []
    errors = tasks.validate(schema, {"items": [{"id": "x"}, {}]})
    assert "output.items[0].id should be integer, got string" in errors
    assert "output.items[1].id is missing" in errors
    assert tasks.validate(schema, []) == ["output should be object, got array"]


# ------------------------------------------------------------------- guard

def test_requests_from_elsewhere_are_refused(client):
    assert client.get("/health").json()["app"] == "litreview-local"
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 403
    # A domain pointed at 127.0.0.1 (DNS rebinding) is not this machine.
    assert client.get("/health", headers={"Host": "rebind.example:8799"}).status_code == 403
    assert client.get("/health", headers={"Host": "127.0.0.1:8799"}).status_code == 200
    assert client.get("/health", headers={"Host": "localhost:8799"}).status_code == 200
    assert client.get("/health", headers={"Host": "127.0.0.1:9999"}).status_code == 403
    # A web page posting to the app.
    response = client.post("/mcp", headers={"Origin": "https://evil.example",
                                            "Content-Type": "application/json"},
                           json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 403
    # A "simple" cross-site POST that needs no preflight.
    response = client.post("/mcp", headers={"Content-Type": "text/plain"},
                           content='{"jsonrpc": "2.0", "id": 1, "method": "ping"}')
    assert response.status_code == 415
    ok = client.post("/mcp", headers={"Origin": "http://127.0.0.1:8799",
                                      "Content-Type": "application/json"},
                     json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert ok.status_code == 200


def test_the_mcp_prompts_are_listed_and_filled(claude):
    listed = claude.rpc("prompts/list").json()["result"]["prompts"]
    assert {p["name"] for p in listed} >= {"work_queue", "new_question", "parse_documents"}
    got = claude.rpc("prompts/get", {"name": "new_question", "arguments": {
        "project": "Thesis", "question": "Does X cause Y?"}}).json()["result"]
    text = got["messages"][0]["content"]["text"]
    assert "Thesis" in text and "Does X cause Y?" in text
    assert "next_task" in claude.instructions


# ------------------------------------------------------------ the lookups

class _Source:
    def __init__(self, records):
        self.records = records

    def search(self, query, max_results=10, year_from=None, year_to=None):
        return [dict(r) for r in self.records][:max_results]


def _record(n, title, cites):
    return {"title": title, "doi": f"10.9/x{n}", "year": 2020, "authors": ["A B"],
            "citation_count": cites, "source_ids": {"openalex": f"W{n}"},
            "abstract": "", "issns": []}


def test_a_lookup_ranks_the_title_first_and_parks_new_hits(clean_db, monkeypatch):
    title = "Addressing Endogeneity Using a Two-Stage Copula Generated Regressor Approach"
    noise = [_record(n, f"Unrelated famous paper {n}", 5000 - n) for n in range(10)]
    monkeypatch.setitem(SOURCES, "openalex", _Source(noise + [_record(99, title, 3)]))
    monkeypatch.setitem(SOURCES, "semantic_scholar", _Source([]))
    monkeypatch.setitem(SOURCES, "crossref", _Source([]))
    db = SessionLocal()
    try:
        project = services.create_project(db, "Copula")
        rq = models.ResearchQuestion(project_id=project.id, text="Copula methods?")
        db.add(rq)
        db.commit()
        # The article is already in the question (found, never fetched).
        known = models.Paper(project_id=project.id, title=title, doi="10.9/x99",
                             year=2020, authors=[], issns=[], source_ids={})
        search = models.Search(project_id=project.id, research_question_id=rq.id,
                               query="q", sources="openalex")
        search.papers.append(known)
        db.add(search)
        db.commit()

        found = research.find_candidates(db, project, title, rq=rq, limit=5)
        assert found[0]["paper"].id == known.id
        assert found[0]["in_question"] and found[0]["title_match"] == "exact"

        stages = {p.title: p.stage for p in db.query(models.Paper).all()}
        assert stages[title] == models.STAGE_SCREENING
        assert stages["Unrelated famous paper 0"] == models.STAGE_LOOKUP
        stats = services.project_stats(db, project)
        assert stats["total"] == 1  # the parked hits are not the project's

        # A week later the unpicked hits go; the known article stays.
        for paper in db.query(models.Paper).filter_by(stage=models.STAGE_LOOKUP):
            paper.created_at = models.utcnow().replace(year=2020)
        db.commit()
        assert services.prune_lookups(db) == 10
        assert db.query(models.Paper).count() == 1
    finally:
        db.close()


def test_get_answer_trims_itself_to_its_budget(claude, monkeypatch):
    from test_pipeline_e2e import BODY, PAPERS, FakeSource, make_pdf, work_queue
    from litreview_local import pdfs

    for name in ("openalex", "semantic_scholar", "crossref"):
        monkeypatch.setitem(SOURCES, name, FakeSource(PAPERS))
    monkeypatch.setattr(pdfs, "download_pdf", lambda paper, client=None: (
        make_pdf(pdfs.original_path(paper.project.slug, paper.id),
                 [paper.title] + BODY), (True, None))[1])
    project = claude.call("create_project", name="Budget")
    started = claude.call("ask_question", project_id=project["project_id"],
                          question="Does remote work improve employee wellbeing?")
    work_queue(claude)
    keys = [h["key"] for p in claude.call("get_highlights",
                                          question_id=started["question_id"])["papers"]
            for h in p["highlights"]]
    claude.call("save_answer", question_id=started["question_id"],
                text=" ".join(f"Claim {n} [CIT:{k}]." for n, k in enumerate(keys)))
    full = claude.call("get_answer", question_id=started["question_id"])
    assert len(full["answers"][0]["citations"]) == 5
    assert "quote" in full["answers"][0]["citations"][0]
    assert "context" in full["answers"][0]["citations"][0]
    small = claude.call("get_answer", question_id=started["question_id"],
                        max_chars=2000)
    assert "context" not in small["answers"][0]["citations"][0]
    assert "left out" in small["note"]


def test_a_named_background_job_says_what_it_is_doing_until_it_is_done():
    import threading

    from litreview_local import worker

    worker.set_inline(False)
    started, release, done = threading.Event(), threading.Event(), threading.Event()

    def job():
        started.set()
        release.wait(10)
        done.set()

    try:
        worker.submit_as(job, label="downloading 2 PDFs", project_id=7)
        assert started.wait(10)
        assert worker.labels(7) == ["downloading 2 PDFs"] and worker.labels() == ["downloading 2 PDFs"]
        assert worker.labels(8) == [] and worker.unnamed() == 0
        release.set()
        assert done.wait(10)
        for _ in range(100):                      # the label goes as the job ends
            if not worker.labels(7) and not worker.busy():
                break
            threading.Event().wait(0.02)
        assert worker.labels(7) == [] and worker.busy() == 0
    finally:
        release.set()
        worker.set_inline(True)

